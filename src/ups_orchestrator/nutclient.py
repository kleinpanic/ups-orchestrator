"""Stdlib-only, fully-injectable engine for NUT secondary enrollment.

Every config artifact is rendered as a pure string function, and every side
effect runs through an injected ``run_local`` / ``run_ssh`` / ``run_nft``
callable (mirroring :class:`ups_orchestrator.events.Deps`), so the whole module
unit-tests against canned command output with zero live hosts.

Two research corrections are load-bearing and enforced here:

* A secondary's ``upsmon.conf`` OMITS ``POWERDOWNFLAG`` entirely — powering off
  the UPS is primary-only. A netclient must never disarm a UPS it does not own.
* The firewall rule lives in a dedicated ``table inet ups_orchestrator`` with
  its own hooked input chain, never an edit to the user's ``filter``/``input``
  chain, so a reload coexists with crowdsec by construction.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

# A NUT UPS name is restricted to this charset (letters, digits, dot, dash,
# underscore) by upsd itself. Any other byte — a space, semicolon, backtick,
# ``$`` — cannot be a real UPS name, so a value carrying one is an injection
# attempt against the remote shell string built in :func:`verify_secondary`,
# not a typo. Reject rather than escape.
_NUT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_nut_name(name: str) -> bool:
    """True iff ``name`` is a syntactically valid NUT UPS name. PURE."""
    return bool(_NUT_NAME_RE.match(name))


def valid_ip(value: str) -> bool:
    """True iff ``value`` is a valid IPv4/IPv6 literal. PURE."""
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


# --- injected runner types (mirror events.Deps) -------------------------------

RunLocal = Callable[[Sequence[str]], tuple[int, str, str]]
# argv, stdin -> rc, out, err. The optional stdin channel carries the
# password-bearing upsd.users content for a secret-safe local write via
# ``install /dev/stdin`` (the secret never lands on argv).
RunLocalStdin = Callable[[Sequence[str], "str | None"], tuple[int, str, str]]
# alias, command, stdin -> rc, out, err. The optional stdin channel carries the
# password-bearing config for a secret-safe remote write (creds never on argv).
RunSSH = Callable[[str, str, "str | None"], tuple[int, str, str]]
RunNft = Callable[[str], tuple[int, str, str]]


# --- default side effects (live-only, excluded from the coverage floor) -------


def _default_run_local(argv: Sequence[str]) -> tuple[int, str, str]:  # pragma: no cover
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _default_run_ssh(
    alias: str, command: str, stdin: str | None = None
) -> tuple[int, str, str]:  # pragma: no cover
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, command],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --- OS classification --------------------------------------------------------


def _classify_os(uname_s: str, has_pacman: bool, has_apt: bool) -> str:
    """Map a remote probe to ``arch``/``ubuntu``/``debian``/``unknown`` (PURE).

    ``pacman`` present → arch. Else ``apt`` present → the Debian-family default
    label ``ubuntu`` (a caller with distro knowledge may override to
    ``debian``). Else ``unknown``.
    """
    if has_pacman:
        return "arch"
    if has_apt:
        return "ubuntu"
    return "unknown"


_DETECT_PROBE = "uname -s; command -v pacman || true; command -v apt-get || true"


def detect_os(alias: str, run_ssh: RunSSH) -> str:
    """One remote probe → :func:`_classify_os`. Impure only in the run_ssh call."""
    rc, out, _err = run_ssh(alias, _DETECT_PROBE, None)
    lowered = out.lower()
    return _classify_os(out, has_pacman="pacman" in lowered, has_apt="apt-get" in lowered)


# --- package install + service enable (idempotent, query-then-act) ------------

_INSTALL_ARCH = "pacman -Qq nut >/dev/null 2>&1 || sudo pacman -S --needed --noconfirm nut"
_INSTALL_DEBIAN = (
    "dpkg-query -W -f='${Status}' nut-client 2>/dev/null | grep -q '^install ok installed$' "
    "|| sudo apt-get install -y nut-client"
)


def install_nut_client(alias: str, os_kind: str, run_ssh: RunSSH) -> tuple[int, str, str]:
    """Idempotently install the client package for ``os_kind`` over SSH.

    Never installs or enables ``nut-server``/``nut-driver@`` — Arch's ``nut`` is
    monolithic (server present but gated off on ``netclient``); a secondary
    never runs upsd.
    """
    cmd = _INSTALL_ARCH if os_kind == "arch" else _INSTALL_DEBIAN
    return run_ssh(alias, cmd, None)


_ENABLE_MONITOR = (
    "systemctl is-enabled --quiet nut-monitor.service "
    "&& systemctl is-active --quiet nut-monitor.service "
    "|| sudo systemctl enable --now nut-monitor.service"
)


def enable_nut_monitor(alias: str, run_ssh: RunSSH) -> tuple[int, str, str]:
    """Idempotently enable ``nut-monitor.service`` (and nothing else) over SSH."""
    return run_ssh(alias, _ENABLE_MONITOR, None)


# --- pure config renderers ----------------------------------------------------

_UPSMON_BEGIN = "# BEGIN ups-orchestrator MANAGED"
_UPSMON_END = "# END ups-orchestrator MANAGED"

_UPSMON_NOTIFY_FLAGS = ("ONBATT", "LOWBATT", "FSD", "COMMBAD", "SHUTDOWN", "NOCOMM")


def render_nut_conf() -> str:
    """``MODE=netclient`` — the single switch that starts ``nut-monitor`` and
    keeps ``nut-server`` from ever starting. PURE."""
    return "MODE=netclient\n"


def render_upsmon_conf(
    ups: str,
    primary: str,
    user: str,
    pw: str,
    shutdown_cmd: str,
    powervalue: int = 1,
    deadtime: int = 30,
    upssched_path: str = "/usr/bin/upssched",
) -> str:
    """Render a secondary ``upsmon.conf`` marker block. PURE.

    The MONITOR line embeds the plaintext password and the ``secondary`` role.
    The block deliberately carries no directive that would power off the UPS —
    that is primary-only (RESEARCH.md Correction #2).

    ``shutdown_cmd`` is emitted inside ``SHUTDOWNCMD "<cmd>"``; a double-quote
    would break the NUT parser or inject a second directive, so it is rejected
    rather than escaped.
    """
    if '"' in shutdown_cmd:
        raise ValueError("shutdown_cmd must not contain a double-quote character")
    lines = [
        _UPSMON_BEGIN,
        f"MONITOR {ups}@{primary} {powervalue} {user} {pw} secondary",
        "MINSUPPLIES 1",
        f'SHUTDOWNCMD "{shutdown_cmd}"',
        "POLLFREQ 5",
        "POLLFREQALERT 5",
        "HOSTSYNC 15",
        f"DEADTIME {deadtime}",
        "NOCOMMWARNTIME 300",
        "RBWARNTIME 43200",
        f"NOTIFYCMD {upssched_path}",
        *(f"NOTIFYFLAG {flag} SYSLOG+EXEC" for flag in _UPSMON_NOTIFY_FLAGS),
        _UPSMON_END,
    ]
    return "\n".join(lines) + "\n"


# --- primary-side upsd.conf / upsd.users upserts (pure) -----------------------


def _strip_managed_block(text: str) -> str:
    """Remove any ``# BEGIN/END ups-orchestrator MANAGED`` block from ``text``."""
    start = text.find(_UPSMON_BEGIN)
    if start == -1:
        return text
    end = text.find(_UPSMON_END, start)
    if end == -1:
        return text
    end = text.find("\n", end)
    end = len(text) if end == -1 else end + 1
    return text[:start] + text[end:]


def upsert_upsd_listen(text: str, lan_ip: str, port: int = 3493) -> tuple[str, bool]:
    """Add a LAN ``LISTEN`` line inside the MANAGED block, idempotently. PURE.

    Returns ``(new_text, changed)``. If ``LISTEN <lan_ip> <port>`` is already
    present anywhere, returns ``(text, False)`` — the operator's own localhost
    LISTEN lines are never touched. Otherwise the LAN line is (re)rendered inside
    a ``# BEGIN/END ups-orchestrator MANAGED`` block appended to the file.
    """
    listen_line = f"LISTEN {lan_ip} {port}"
    if listen_line in text:
        return text, False
    stripped = _strip_managed_block(text)
    block = f"{_UPSMON_BEGIN}\n{listen_line}\n{_UPSMON_END}\n"
    if stripped == "" or stripped.endswith("\n"):
        new_text = stripped + block
    else:
        new_text = stripped + "\n" + block
    return new_text, new_text != text


def upsert_upsd_users(text: str, user: str, password: str) -> tuple[str, bool]:
    """Insert/replace the secondary's MANAGED upsd.users block. PURE, rotation-aware.

    Renders ``[<user>]`` / ``    password = <password>`` / ``    upsmon secondary``
    inside a ``# BEGIN/END ups-orchestrator MANAGED`` block. An existing MANAGED
    block is REPLACED (so a changed or placeholder password rotates in); re-running
    with the same password is a byte-identical no-op. ``password`` is the REAL
    env-sourced secret supplied by the CLI — the committed ``CHANGE_ME`` placeholder
    lives only in the deploy snippet and is never sticky here.
    """
    stripped = _strip_managed_block(text)
    block = (
        f"{_UPSMON_BEGIN}\n"
        f"[{user}]\n"
        f"    password = {password}\n"
        "    upsmon secondary\n"
        f"{_UPSMON_END}\n"
    )
    if stripped == "" or stripped.endswith("\n"):
        new_text = stripped + block
    else:
        new_text = stripped + "\n" + block
    return new_text, new_text != text


# --- dedicated-table nftables render / upsert (pure) --------------------------

_NFT_BEGIN = "# BEGIN UPS-ORCHESTRATOR MANAGED"
_NFT_END = "# END UPS-ORCHESTRATOR MANAGED"


def render_nft_block(saddrs: Sequence[str], port: int = 3493) -> str:
    """Render a self-contained ``table inet ups_orchestrator`` marker block. PURE.

    The table owns its own hooked input chain at ``priority filter - 5`` with
    ``policy accept``, so it coexists with crowdsec by construction and never
    edits the user's chains. Empty ``saddrs`` → empty string (the table is
    dropped entirely rather than left matching nothing).
    """
    if not saddrs:
        return ""
    members = ", ".join(saddrs)
    return (
        f"{_NFT_BEGIN}\n"
        "table inet ups_orchestrator {\n"
        "    chain input {\n"
        "        type filter hook input priority filter - 5; policy accept;\n"
        f'        tcp dport {port} ip saddr {{ {members} }} accept comment "upsd NUT secondaries"\n'
        "    }\n"
        "}\n"
        f"{_NFT_END}\n"
    )


def _strip_nft_block(text: str) -> str:
    """Remove any existing MANAGED block (and its trailing newline) from ``text``."""
    start = text.find(_NFT_BEGIN)
    if start == -1:
        return text
    end = text.find(_NFT_END, start)
    if end == -1:
        return text
    end = text.find("\n", end)
    end = len(text) if end == -1 else end + 1
    return text[:start] + text[end:]


def upsert_nft_block(text: str, saddrs: Sequence[str], port: int = 3493) -> tuple[str, bool]:
    """Insert/replace/drop the MANAGED nft block. PURE.

    Returns ``(new_text, changed)`` where ``changed`` is ``False`` only when the
    result is byte-identical to ``text`` (idempotent). Empty ``saddrs`` removes
    the block; a present block is replaced in place; an absent block is appended.
    """
    stripped = _strip_nft_block(text)
    block = render_nft_block(saddrs, port)
    if not block:
        new_text = stripped
    elif stripped == "" or stripped.endswith("\n"):
        new_text = stripped + block
    else:
        new_text = stripped + "\n" + block
    return new_text, new_text != text


# --- remote config guard (pure) ----------------------------------------------


def _has_non_marker_monitor(upsmon_text: str) -> bool:
    inside = False
    for line in upsmon_text.splitlines():
        stripped = line.strip()
        if stripped == _UPSMON_BEGIN:
            inside = True
            continue
        if stripped == _UPSMON_END:
            inside = False
            continue
        if not inside and stripped.startswith("MONITOR "):
            return True
    return False


def remote_config_guard(
    existing_upsmon: str, existing_nut_conf: str, force: bool = False
) -> tuple[bool, str]:
    """Refuse to overwrite operator config unless ``force``. PURE.

    Refuses when ``existing_upsmon`` carries a ``MONITOR`` line outside our
    MANAGED block (an operator's own config), or when ``existing_nut_conf`` sets
    ``MODE=standalone``/``MODE=netserver`` (demoting a real NUT server). An
    empty, all-comment, or marker-only file is allowed.
    """
    if force:
        return True, "forced"
    if _has_non_marker_monitor(existing_upsmon):
        return False, "existing non-marker MONITOR line — refusing to overwrite (use force)"
    for line in existing_nut_conf.splitlines():
        mode = line.strip()
        if mode in ("MODE=standalone", "MODE=netserver"):
            return False, f"nut.conf sets {mode} — refusing to demote a NUT server (use force)"
    return True, "clean"


# --- impure orchestration -----------------------------------------------------

_INSTALL_CMD = "sudo install -m 0640 -o root -g nut /dev/stdin {path}"


def write_remote_nut_config(
    alias: str,
    upsmon_text: str,
    nut_conf_text: str,
    upsmon_path: str,
    nut_conf_path: str,
    run_ssh: RunSSH,
    force: bool = False,
) -> tuple[int, str]:
    """Read the remote configs, guard, then write both via stdin. IMPURE.

    On guard refusal returns a non-zero rc and the reason without writing. The
    password-bearing ``upsmon_text`` is piped on STDIN into
    ``install … /dev/stdin`` — it never touches argv and is never logged.
    """
    _rc, existing_upsmon, _e = run_ssh(alias, f"cat {upsmon_path} 2>/dev/null || true", None)
    _rc, existing_nut, _e = run_ssh(alias, f"cat {nut_conf_path} 2>/dev/null || true", None)
    allowed, reason = remote_config_guard(existing_upsmon, existing_nut, force=force)
    if not allowed:
        return 1, reason
    rc, _out, err = run_ssh(alias, _INSTALL_CMD.format(path=upsmon_path), upsmon_text)
    if rc != 0:
        return rc, err or "failed to write upsmon.conf"
    rc, _out, err = run_ssh(alias, _INSTALL_CMD.format(path=nut_conf_path), nut_conf_text)
    if rc != 0:
        return rc, err or "failed to write nut.conf"
    return 0, "written"


_STATUS_TOKENS = ("OL", "OB", "LB")


def verify_secondary(
    alias: str,
    ups: str,
    primary: str,
    run_ssh: RunSSH,
    *,
    timeout: int = 10,
    deep: bool = False,
) -> tuple[bool, str]:
    """Read-only enrollment check. IMPURE only in the run_ssh calls.

    Shallow: ``upsc <ups>@<primary> ups.status`` must exit 0 and report a
    recognizable status token. Deep: additionally greps the ``nut-monitor``
    journal for an auth failure — because ``upsc`` reads are unauthenticated, a
    plain status read does NOT prove the upsd.users password matched.

    ``ups`` and ``primary`` are interpolated into a remote shell command, so
    they are re-validated at this sink (config-sourced values reach here without
    passing the argparse boundary): a bad ``ups`` name or ``primary`` IP is
    refused with ``(False, reason)`` rather than executed.
    """
    if not valid_nut_name(ups):
        return False, f"invalid UPS name: {ups!r}"
    if not valid_ip(primary):
        return False, f"invalid primary IP: {primary!r}"
    if timeout <= 0:
        return False, f"timeout must be positive, got {timeout}"
    # Bound the remote `upsc` with coreutils `timeout` so --timeout is honoured
    # for the command runtime, not just the SSH connect (the default runner's
    # fixed ConnectTimeout). A hung upsd read then fails at <timeout>s instead of
    # blocking enrollment indefinitely.
    rc, out, err = run_ssh(alias, f"timeout {timeout} upsc {ups}@{primary} ups.status", None)
    if rc != 0:
        return False, err or "upsc failed"
    status = out.strip()
    if not any(tok in status.split() for tok in _STATUS_TOKENS):
        return False, f"unrecognized status: {status!r}"
    if deep:
        _rc, jout, _e = run_ssh(
            alias,
            "journalctl -u nut-monitor -n 50 --no-pager | grep -Ei 'login|denied|failure' || true",
            None,
        )
        if jout.strip():
            return False, f"auth failure in nut-monitor journal: {jout.strip()}"
    return True, status


def apply_nft(
    path: str,
    saddrs: Sequence[str],
    run_nft: RunNft,
    restart_bouncer: Callable[[], None],
) -> tuple[int, str, str]:
    """Upsert the nft block, reload, then restart the bouncer. IMPURE.

    Reloading flushes crowdsec's tables, so ``restart_bouncer`` MUST run after
    every successful reload — the bouncer does not auto-recreate them. When the
    upsert makes no change, the reload and restart are skipped entirely.

    On first enrollment the nft include file does not exist yet — nothing else
    creates it. A missing file (or missing parent dir) is treated as empty so
    the first ``monitor add`` writes a fresh table instead of raising
    ``FileNotFoundError`` after upsd was already reconfigured and restarted.
    """
    conf = Path(path)
    conf.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = conf.read_text()
    except FileNotFoundError:
        text = ""
    new_text, changed = upsert_nft_block(text, saddrs)
    if not changed:
        return 0, "no change", ""
    conf.write_text(new_text)
    rc, out, err = run_nft(path)
    if rc != 0:
        return rc, out, err
    restart_bouncer()
    return 0, out, err


# --- primary-side bootstrap (impure orchestration) ----------------------------

_INSTALL_LOCAL = ("install", "-m", "0640", "-o", "root", "-g", "nut", "/dev/stdin")
_RESTART_NUT = ("systemctl", "restart", "nut-server")
_REDACTED = "<redacted>"


def _redact(text: str, secret: str) -> str:
    """Replace every occurrence of ``secret`` with a placeholder token."""
    return text.replace(secret, _REDACTED) if secret else text


def bootstrap_primary(
    *,
    lan_ip: str,
    port: int,
    user: str,
    password: str,
    saddrs: Sequence[str] | None,
    upsd_conf_path: str,
    upsd_users_path: str,
    nft_path: str,
    run_local: RunLocalStdin,
    run_nft: RunNft,
    restart_bouncer: Callable[[], None],
    is_root: bool,
    dry_run: bool = False,
) -> tuple[int, list[str]]:
    """Bootstrap the primary: expose upsd on the LAN and authorize the secondary.

    Ordered (RESEARCH.md §5): read+upsert upsd.conf LISTEN → read+upsert
    upsd.users with the REAL ``password`` → if either changed, write both back
    (upsd.users via ``install /dev/stdin`` so the secret is piped on stdin, never
    argv) then ``systemctl restart nut-server`` (a FULL restart — upsd reads
    LISTEN only at startup, a reload would leave it on localhost) → ``apply_nft``
    (plan 02: nft reload + mandatory crowdsec-bouncer restart; never a bare
    reload here). ``password`` is redacted in every step_log line and in the
    non-root / dry-run diff.

    Root gate: when ``is_root`` is false nothing is mutated — the intended diff
    and command list (password redacted) are returned with exit code 4, so a
    non-root run never half-applies /etc. ``dry_run`` collects the same plan,
    mutates nothing, and returns 0. A failing restart or nft short-circuits with
    exit code 4 before later steps.

    ``saddrs=None`` SKIPS the firewall step entirely (``--no-firewall``). This is
    distinct from an empty list: an empty ``saddrs`` would drop the managed nft
    table and revoke LAN access for every already-enrolled secondary, so add
    never passes ``[]`` here — table teardown belongs only to the genuine
    last-secondary-removed path in ``remove``.
    """
    conf_text = Path(upsd_conf_path).read_text()
    users_text = Path(upsd_users_path).read_text()
    new_conf, conf_changed = upsert_upsd_listen(conf_text, lan_ip, port)
    new_users, users_changed = upsert_upsd_users(users_text, user, password)
    changed = conf_changed or users_changed

    nft_plan = (
        "skip (--no-firewall)"
        if saddrs is None
        else f"apply {list(saddrs)} then restart crowdsec-firewall-bouncer"
    )
    log: list[str] = [
        f"upsd.conf LISTEN {lan_ip} {port}: {'add' if conf_changed else 'present'}",
        f"upsd.users [{user}]: {'write' if users_changed else 'unchanged'} (password {_REDACTED})",
        f"restart: {'systemctl restart nut-server' if changed else 'skip (no change)'}",
        f"nft: {nft_plan}",
    ]

    if not is_root:
        log.insert(
            0, "not root: refusing to half-apply — run as root: sudo ups-orchestrator monitor add …"
        )
        log.append("--- upsd.users (would write) ---")
        log.append(_redact(new_users, password))
        return 4, log

    if dry_run:
        return 0, log

    if changed:
        rc, _out, err = run_local([*_INSTALL_LOCAL, upsd_conf_path], new_conf)
        if rc != 0:
            log.append(f"upsd.conf write failed: {_redact(err, password)}")
            return 4, log
        rc, _out, err = run_local([*_INSTALL_LOCAL, upsd_users_path], new_users)
        if rc != 0:
            log.append(f"upsd.users write failed: {_redact(err, password)}")
            return 4, log
        rc, _out, err = run_local(list(_RESTART_NUT), None)
        if rc != 0:
            log.append(f"nut-server restart failed: {_redact(err, password)}")
            return 4, log

    if saddrs is not None:
        rc, out, err = apply_nft(nft_path, saddrs, run_nft, restart_bouncer)
        if rc != 0:
            log.append(f"nft apply failed: {_redact(err, password)}")
            return 4, log
    log.append("done")
    return 0, log
