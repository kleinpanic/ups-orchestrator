"""Power dashboard: a rendered PNG of live + historical UPS draw for Discord.

Live values come from ``nut.read_snapshot``; the draw history comes from the
recorder's JSONL samples (``estimated_load_watts`` per UPS). Rendering uses
matplotlib's headless Agg backend, so it runs from a systemd service with no
display. The renderer is imported lazily so the rest of the CLI keeps working
where matplotlib is not installed.
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ups_orchestrator.config import Config
from ups_orchestrator.nut import UpsSnapshot, read_snapshot

# Per-UPS line colours, assigned in config order and reused for the cards.
_PALETTE = ["#38bdf8", "#a78bfa", "#f472b6", "#34d399", "#fbbf24", "#fb7185"]


def _read_series(
    sample_path: Path, names: list[str], cutoff: float, *, max_files: int = 6
) -> dict[str, list[tuple[float, int]]]:
    """Return ``{ups: [(unix_time, watts), ...]}`` newer than ``cutoff``.

    Walks the active samples file then its rotations (``.1``, ``.2``, …) newest
    first, stopping once the window is covered or ``max_files`` are read.
    """
    series: dict[str, list[tuple[float, int]]] = {n: [] for n in names}
    candidates = [sample_path] + [
        sample_path.with_name(f"{sample_path.name}.{i}") for i in range(1, max_files)
    ]
    for path in candidates:
        earliest = min((t for pts in series.values() for t, _ in pts), default=float("inf"))
        if earliest <= cutoff:
            break
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 60_000_000))
                # Drop the first (likely partial) line after a mid-file seek.
                lines = fh.read().decode("utf-8", "replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
                t = float(rec["unix_time"])
            except (ValueError, KeyError, TypeError):
                continue
            if t < cutoff:
                continue
            upses = rec.get("upses", {})
            for n in names:
                w = upses.get(n, {}).get("estimated_load_watts")
                if isinstance(w, (int, float)):
                    series[n].append((t, int(w)))
    for n in names:
        series[n].sort()
    return series


def _daily_energy_kwh(
    series: dict[str, list[tuple[float, int]]],
    names: list[str],
    *,
    max_gap: float = 120.0,
) -> list[tuple[str, dict[str, float]]]:
    """Integrate watt samples into kWh per local calendar day, per UPS.

    Energy for each sample interval is ``watts * seconds``; intervals longer than
    ``max_gap`` (recorder restarts/gaps) are dropped so a gap can't inflate a
    day. Returns ``[(label, {ups: kwh}), ...]`` in chronological order.
    """
    buckets: dict[tuple[int, int], dict[str, float]] = {}
    labels: dict[tuple[int, int], str] = {}
    for n in names:
        pts = series[n]
        for i in range(len(pts) - 1):
            t0, w0 = pts[i]
            dt = pts[i + 1][0] - t0
            if dt <= 0 or dt > max_gap:
                continue
            lt = time.localtime(t0)
            key = (lt.tm_year, lt.tm_yday)
            buckets.setdefault(key, dict.fromkeys(names, 0.0))[n] += w0 * dt / 3_600_000.0
            labels[key] = time.strftime("%a %m-%d", lt)
    return [(labels[k], buckets[k]) for k in sorted(buckets)]


def _fmt_runtime(sec: int | None) -> str:
    if not sec:
        return "—"
    if sec >= 3600:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    return f"{sec // 60}m"


def render_png(
    cfg: Config,
    sample_path: Path,
    *,
    hours: int = 168,
    host: str = "",
    now: float | None = None,
) -> bytes:
    """Render the dashboard to PNG bytes. Raises ImportError if matplotlib absent."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    names = list(cfg.upses)
    colors = {n: _PALETTE[i % len(_PALETTE)] for i, n in enumerate(names)}
    snaps: dict[str, UpsSnapshot] = {}
    for n in names:
        try:
            snaps[n] = read_snapshot(n)
        except Exception:  # noqa: BLE001 — a dead UPS must not sink the render
            snaps[n] = UpsSnapshot(None, None, None, None, None)
    series = _read_series(sample_path, names, cutoff)

    bg, panel, grid, fg, muted = "#0b1220", "#0f1b2e", "#1e293b", "#e2e8f0", "#94a3b8"
    fig = plt.figure(figsize=(12, 9), dpi=120)
    fig.patch.set_facecolor(bg)
    gs = fig.add_gridspec(3, len(names), height_ratios=[1.0, 1.7, 1.15], hspace=0.5, wspace=0.14)

    daily = _daily_energy_kwh(series, names)
    week_kwh = sum(v for _, d in daily for v in d.values())
    stamp = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now))
    total_w = sum(int((s.load or 0) / 100 * (s.realpower_nominal or 0)) for s in snaps.values())
    title = f"UPS Power — {host}" if host else "UPS Power"
    fig.text(0.5, 0.97, title, ha="center", color=fg, fontsize=20, fontweight="bold")
    fig.text(
        0.5,
        0.942,
        f"{stamp} · live + {hours}h history · draw ≈ {total_w} W · "
        f"usage ≈ {week_kwh:.1f} kWh over window",
        ha="center",
        color=muted,
        fontsize=10,
    )

    from matplotlib.patches import Rectangle

    # Per-UPS draw stats over the window, for card footers and the chart legend.
    stats: dict[str, tuple[int | None, int | None]] = {}
    for n in names:
        ys_all = [w for _, w in series[n]]
        stats[n] = (sum(ys_all) // len(ys_all), max(ys_all)) if ys_all else (None, None)

    def _gauge(ax: object, x: float, y: float, w: float, frac: float, color: str) -> None:
        frac = max(0.0, min(1.0, frac))
        for width, fill in ((w, grid), (w * frac, color)):
            ax.add_patch(  # type: ignore[attr-defined]
                Rectangle(
                    (x, y),
                    width,
                    0.055,
                    transform=ax.transAxes,  # type: ignore[attr-defined]
                    facecolor=fill,
                    edgecolor="none",
                    clip_on=False,
                )
            )

    # --- summary cards -------------------------------------------------------
    for i, n in enumerate(names):
        ax = fig.add_subplot(gs[0, i])
        ax.set_facecolor(panel)
        for spine in ax.spines.values():
            spine.set_color(grid)
        ax.set_xticks([])
        ax.set_yticks([])
        s = snaps[n]
        col = colors[n]
        online = (s.status or "").startswith("OL")
        badge = "#22c55e" if online else "#ef4444"
        name_part, _, model_part = (cfg.upses[n].label or n).partition(" - ")
        nominal = s.realpower_nominal or 0
        watts = int((s.load or 0) / 100 * nominal)
        charge, load = s.charge, s.load

        ax.add_patch(
            Rectangle(
                (0, 0.93),
                1,
                0.07,
                transform=ax.transAxes,
                facecolor=col,
                edgecolor="none",
                clip_on=False,
            )
        )
        ax.text(
            0.06,
            0.80,
            name_part[:22],
            transform=ax.transAxes,
            color=fg,
            fontsize=12,
            fontweight="bold",
            va="center",
        )
        if model_part:
            ax.text(
                0.06,
                0.66,
                model_part[:26],
                transform=ax.transAxes,
                color=muted,
                fontsize=8.5,
                va="center",
            )
        ax.text(
            0.06,
            0.50,
            s.status or "?",
            transform=ax.transAxes,
            color=badge,
            fontsize=10.5,
            fontweight="bold",
            va="center",
        )
        ax.text(
            0.94,
            0.50,
            f"{watts} W",
            transform=ax.transAxes,
            color=fg,
            fontsize=16,
            fontweight="bold",
            ha="right",
            va="center",
        )
        alarm_on = s.alarm is not None and s.alarm.strip().lower() not in ("", "none")
        test_bad = s.test_result is not None and "fail" in s.test_result.lower()
        if alarm_on or test_bad:
            ax.text(
                0.5,
                0.50,
                "⚠ ALARM" if alarm_on else "⚠ TEST",
                transform=ax.transAxes,
                color="#ef4444",
                fontsize=9,
                fontweight="bold",
                ha="center",
                va="center",
            )
        # battery gauge (charge %, with the raw pack voltage — the aging signal)
        batt_v = f" · {s.battery_voltage:.1f} V" if s.battery_voltage is not None else ""
        ax.text(
            0.06,
            0.375,
            f"battery {charge if charge is not None else '—'}%{batt_v}",
            transform=ax.transAxes,
            color=muted,
            fontsize=8.5,
            va="center",
        )
        ax.text(
            0.94,
            0.375,
            _fmt_runtime(s.runtime_seconds),
            transform=ax.transAxes,
            color=muted,
            fontsize=8.5,
            ha="right",
            va="center",
        )
        _gauge(ax, 0.06, 0.285, 0.88, (charge or 0) / 100, "#22c55e")
        # load gauge
        ax.text(
            0.06,
            0.175,
            f"load {load if load is not None else '—'}% of {nominal} W",
            transform=ax.transAxes,
            color=muted,
            fontsize=8.5,
            va="center",
        )
        ax.text(
            0.94,
            0.175,
            f"in {s.input_voltage if s.input_voltage is not None else '—'} V",
            transform=ax.transAxes,
            color=muted,
            fontsize=8.5,
            ha="right",
            va="center",
        )
        _gauge(ax, 0.06, 0.085, 0.88, (load or 0) / 100, col)

    # --- history chart -------------------------------------------------------
    ax = fig.add_subplot(gs[1, :])
    ax.set_facecolor(panel)
    for spine in ax.spines.values():
        spine.set_color(grid)
    ax.tick_params(colors=muted, labelsize=9)
    ax.grid(True, color=grid, linewidth=0.6, alpha=0.7)
    ax.set_title("Estimated draw over time", color=fg, fontsize=13, fontweight="bold", loc="left")
    ax.set_ylabel("watts", color=muted, fontsize=10)
    any_data = False
    for n in names:
        pts = series[n]
        if len(pts) < 2:
            continue
        any_data = True
        xs = [t / 86400 for t, _ in pts]  # matplotlib date number (days since epoch)
        ys = [w for _, w in pts]
        avg_w, pk_w = stats[n]
        ax.plot(xs, ys, color=colors[n], linewidth=1.4, label=f"{n}  avg {avg_w}W · peak {pk_w}W")
        ax.fill_between(xs, ys, color=colors[n], alpha=0.10)
        # live value at the right edge
        ax.annotate(
            f"{ys[-1]} W",
            xy=(xs[-1], ys[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            color=colors[n],
            fontsize=9,
            fontweight="bold",
            va="center",
            clip_on=False,
        )
    if any_data:
        # Clamp the left edge to where data actually begins, so a recorder that
        # doesn't yet reach back the full window doesn't render a dead gap.
        earliest = min(pts[0][0] for n in names if (pts := series[n]))
        left = max(cutoff, earliest)
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate(rotation=0, ha="center")
        ax.set_xlim(left / 86400, now / 86400)
        ax.set_ylim(bottom=0)
        ax.margins(x=0.02)
        leg = ax.legend(
            loc="upper left",
            facecolor=panel,
            edgecolor=grid,
            labelcolor=fg,
            fontsize=9,
            ncol=len(names),
        )
        leg.get_frame().set_alpha(0.9)
    else:
        ax.text(
            0.5,
            0.5,
            "no recorder history in window",
            transform=ax.transAxes,
            ha="center",
            color=muted,
            fontsize=12,
        )
        ax.set_xticks([])
        ax.set_yticks([])

    # --- daily energy usage (kWh per calendar day) ---------------------------
    ax2 = fig.add_subplot(gs[2, :])
    ax2.set_facecolor(panel)
    for spine in ax2.spines.values():
        spine.set_color(grid)
    ax2.tick_params(colors=muted, labelsize=9)
    ax2.grid(True, axis="y", color=grid, linewidth=0.6, alpha=0.7)
    ax2.set_title("Daily energy usage (kWh)", color=fg, fontsize=13, fontweight="bold", loc="left")
    ax2.set_ylabel("kWh", color=muted, fontsize=10)
    if daily:
        day_labels = [lbl for lbl, _ in daily]
        idx = list(range(len(day_labels)))
        bw = 0.8 / max(1, len(names))
        for j, n in enumerate(names):
            vals = [d.get(n, 0.0) for _, d in daily]
            offs = [x + j * bw for x in idx]
            ax2.bar(offs, vals, width=bw, color=colors[n], label=n)
            for x, v in zip(offs, vals, strict=True):
                if v >= 0.05:
                    ax2.text(x, v, f"{v:.1f}", ha="center", va="bottom", color=muted, fontsize=7)
        ax2.set_xticks([x + bw * (len(names) - 1) / 2 for x in idx])
        ax2.set_xticklabels(day_labels)
        ax2.set_ylim(bottom=0)
    else:
        ax2.text(
            0.5,
            0.5,
            "no usage data in window",
            transform=ax2.transAxes,
            ha="center",
            color=muted,
            fontsize=12,
        )
        ax2.set_xticks([])
        ax2.set_yticks([])

    import io

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()


def _multipart(png: bytes, payload: dict[str, object], filename: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    ctype = mimetypes.types_map.get(".png", "image/png")
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
    parts.append(b"Content-Type: application/json\r\n\r\n")
    parts.append(json.dumps(payload).encode() + b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    parts.append(png + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def post_png(
    webhook_url: str,
    png: bytes,
    *,
    content: str = "",
    filename: str = "power-dashboard.png",
    username: str = "UPS Orchestrator",
    avatar_url: str = "",
    timeout: float = 15.0,
) -> tuple[bool, int]:
    """Upload the PNG to a Discord webhook as an attachment. Returns (ok, status)."""
    if not webhook_url:
        return False, 0
    payload: dict[str, object] = {"username": username}
    if content:
        payload["content"] = content
    if avatar_url:
        payload["avatar_url"] = avatar_url
    body, boundary = _multipart(png, payload, filename)
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            # Discord's edge blocks the default python-urllib UA (403).
            "User-Agent": "ups-orchestrator (+https://github.com/kleinpanic/ups-orchestrator)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300, resp.status
    except urllib.error.HTTPError as exc:
        return False, exc.code
    except (urllib.error.URLError, OSError):
        return False, 0
