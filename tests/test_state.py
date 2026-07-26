from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ups_orchestrator import state as state_mod
from ups_orchestrator.state import StateStore


def test_save_fsyncs_before_replace(monkeypatch, tmp_path) -> None:
    # Durability: the state tempfile must be fsync'd before the atomic replace.
    calls = {"fsync": 0}
    monkeypatch.setattr(
        state_mod.os, "fsync", lambda _fd: calls.__setitem__("fsync", calls["fsync"] + 1)
    )
    store = StateStore(tmp_path / "state.json")
    store.get("ups1").onbatt_since = 123
    store.save()

    assert calls["fsync"] >= 1
    assert json.loads((tmp_path / "state.json").read_text())["ups1"]["onbatt_since"] == 123


def test_state_save_creates_parent_and_writes_json(tmp_path) -> None:
    path = tmp_path / "missing" / "state.json"
    store = StateStore(path)
    store.get("ups1").onbatt_since = 123

    store.save()

    assert json.loads(path.read_text()) == {
        "ups1": {
            "recent_loads": [],
            "last_load_step_notified": None,
            "last_tick_notified": None,
            "last_status": None,
            "onbatt_since": 123,
            "shutdowns_sent": [],
            "onbatt_notified": False,
        }
    }


def test_state_recovers_from_corrupt_json(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ this is not valid json ")
    store = StateStore(path)  # must not raise
    st = store.get("ups1")
    assert st.onbatt_since is None  # fell back to a fresh default
    # And it can re-save cleanly over the corrupt file.
    st.onbatt_since = 5
    store.save()
    assert json.loads(path.read_text())["ups1"]["onbatt_since"] == 5


def test_state_from_dict_ignores_non_numeric_recent_loads(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"ups1": {"recent_loads": [40, "x", 30, None]}}))
    store = StateStore(path)
    assert store.get("ups1").recent_loads == [40, 30]


# --- T-02-23: metadata-preserving atomic replace ------------------------------
#
# `tempfile.NamedTemporaryFile` creates at 0600 owned by the writer, and a bare
# `Path.replace` makes the destination inherit the TEMP file's mode/owner/ACL,
# silently stripping whatever the installer set on the real file. These tests
# pin that the shared helper (used by `StateStore.save`) copies the
# destination's metadata onto the temp file before the rename.


def _alt_gid(current: int) -> int | None:
    """A supplementary group id this process belongs to, other than ``current``."""
    for gid in os.getgroups():
        if gid != current:
            return gid
    return None


def test_save_preserves_mode_over_existing_file(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{}")
    os.chmod(path, 0o640)

    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    store.save()

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_save_preserves_group_where_settable(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{}")
    current_gid = path.stat().st_gid
    alt_gid = _alt_gid(current_gid)
    if alt_gid is None:
        pytest.skip("process has no alternate supplementary group to test with")
    os.chown(path, -1, alt_gid)

    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    store.save()

    assert path.stat().st_gid == alt_gid


def test_save_over_missing_destination_still_works(tmp_path) -> None:
    path = tmp_path / "state.json"
    assert not path.exists()

    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    store.save()  # must not raise on a first-ever write

    assert json.loads(path.read_text())["ups1"]["onbatt_since"] == 1


def test_save_preserves_acl_when_supported(tmp_path) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("setfacl/getfacl not available on this system")
    path = tmp_path / "state.json"
    path.write_text("{}")
    # A uid unlikely to already appear on the destination's ACL, so this is a
    # genuinely new entry beyond what the owner/group/other bits already imply.
    setfacl = subprocess.run(
        ["setfacl", "-m", "u:65534:r", str(path)], capture_output=True, text=True
    )
    if setfacl.returncode != 0:
        pytest.skip(f"filesystem does not support POSIX ACLs: {setfacl.stderr.strip()}")

    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    store.save()

    acl = subprocess.run(
        ["getfacl", "-n", "--omit-header", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "user:65534:r--" in acl


def _acl_or_skip(path) -> None:
    """Put a named-user ACL on ``path``, or skip if this box/filesystem cannot."""
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("setfacl/getfacl not available on this system")
    done = subprocess.run(["setfacl", "-m", "u:65534:r", str(path)], capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip(f"filesystem does not support POSIX ACLs: {done.stderr.strip()}")


def _acl_tooling_absent(monkeypatch) -> None:
    """Simulate a minimal Debian: the `acl` package (getfacl/setfacl) is not installed."""

    def _no_binary(argv, **_kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(state_mod.subprocess, "run", _no_binary)


def test_save_does_not_widen_group_access_when_the_acl_cannot_be_carried(
    monkeypatch, tmp_path
) -> None:
    # HI-02. With an ACL present the mode's group bits are the ACL MASK, not a grant:
    # `user::rw- user:65534:r-- group::--- mask::r--` displays as 0640 while group has
    # no access at all. Copying that mode and dropping the ACL — what happens when the
    # `acl` package is absent — hands group a real read the destination never granted,
    # i.e. this helper would create the very permissions bug it exists to fix.
    path = tmp_path / "state.json"
    path.write_text("{}")
    os.chmod(path, 0o600)
    _acl_or_skip(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640  # the mask, displayed as a grant

    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    _acl_tooling_absent(monkeypatch)
    store.save()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # narrower, never wider
    assert json.loads(path.read_text())["ups1"]["onbatt_since"] == 1  # and it still wrote


def test_save_keeps_group_bits_when_there_is_no_acl_to_carry(monkeypatch, tmp_path) -> None:
    # The other half of HI-02: a plain 0640 with no ACL is a genuine group grant, so
    # withholding it there would be a gratuitous narrowing of every ordinary file on a
    # box without the `acl` package.
    path = tmp_path / "state.json"
    path.write_text("{}")
    os.chmod(path, 0o640)

    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    _acl_tooling_absent(monkeypatch)
    store.save()

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_save_completes_and_warns_when_metadata_read_fails(monkeypatch, tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    path.write_text("{}")
    store = StateStore(path)  # constructed before the patch, so _load()'s own
    store.get("ups1").onbatt_since = 1  # stat-based exists() check is unaffected

    # F7 moved the destination stat from `Path.stat` to `os.stat`, so that it and
    # the temp file's stat go through one seam; patch the seam the code uses.
    real_stat = state_mod.os.stat

    def _flaky_stat(target, *a, **k):  # noqa: ANN001, ANN002, ANN003
        if str(target) == str(path):
            raise OSError("stat blew up")
        return real_stat(target, *a, **k)

    monkeypatch.setattr(state_mod.os, "stat", _flaky_stat)
    with caplog.at_level("WARNING"):
        store.save()  # must complete, not raise, despite the stat failure

    assert json.loads(path.read_text())["ups1"]["onbatt_since"] == 1
    assert any("state.json" in rec.message for rec in caplog.records)


def test_save_payload_roundtrips_through_reload(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.get("ups1").onbatt_since = 42
    store.get("ups1").shutdowns_sent = ["low_battery"]
    store.save()

    reloaded = StateStore(path)
    st = reloaded.get("ups1")
    assert st.onbatt_since == 42
    assert st.shutdowns_sent == ["low_battery"]


# --- IF-02: the shipped "turn shutdown off" script ----------------------------


def _disable_script():  # noqa: ANN202 — a loaded module object
    """Load ``deploy/disable-shutdown-targets.py`` without spawning anything."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "deploy" / "disable-shutdown-targets.py"
    spec = importlib.util.spec_from_file_location("_disable_shutdown_targets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_disable_script_keeps_the_live_configs_mode_owner_and_acl(tmp_path) -> None:
    """IF-02: the documented way to turn shutdown off must not widen the config.

    ``install.sh`` writes ``/etc/ups-orchestrator/config.json`` as ``0640 root:nut``
    plus ``setfacl -m u:<run-user>:r``, and the file holds a Discord webhook URL. The
    script's heredoc ended in a bare ``tmp.replace(path)``, so running it as root
    handed the destination the temp file's metadata — the root umask default,
    world-readable, ACL gone. Nothing breaks visibly, so the regression is invisible
    until someone re-checks the mode.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "webhook_url": "https://discord.example/webhooks/secret",
                "shutdown": {"enabled": True},
                "upses": {
                    "ups1": {
                        "label": "U1",
                        "shutdown_scope": "remote",
                        "shutdown_targets": [{"name": "mt", "kind": "remote", "enabled": True}],
                    }
                },
            }
        )
    )
    os.chmod(cfg, 0o640)
    _acl_or_skip(cfg)

    removed = _disable_script().disable_shutdown_targets(cfg)

    assert removed == 1
    after = json.loads(cfg.read_text())
    assert after["shutdown"]["enabled"] is False
    assert after["upses"]["ups1"]["shutdown_targets"] == []
    assert "shutdown_scope" not in after["upses"]["ups1"]
    assert after["webhook_url"] == "https://discord.example/webhooks/secret"  # untouched

    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640, "the config was left world-readable"
    acl = subprocess.run(
        ["getfacl", "-n", "--omit-header", str(cfg)], capture_output=True, text=True, check=True
    ).stdout
    assert "user:65534:r--" in acl, "the installer's ACL was stripped"


def test_disable_script_leaves_monitored_machines_alone(tmp_path) -> None:
    # INV-DECLARED: a declared `native` machine's authority lives in that box's own
    # /etc and no config change here disarms it. A "disable shutdown" script that
    # rewrote shutdown_method would be reporting a disarm it did not perform.
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "monitored_machines": [
                    {"name": "spark", "ups": "cyberpower3", "shutdown_method": "native"}
                ],
                "upses": {"ups1": {"label": "U1"}},
            }
        )
    )

    _disable_script().disable_shutdown_targets(cfg)

    after = json.loads(cfg.read_text())
    assert after["monitored_machines"] == [
        {"name": "spark", "ups": "cyberpower3", "shutdown_method": "native"}
    ]


# --- IF-08: the boot-audit marker ---------------------------------------------


def test_boot_audit_marker_write_preserves_mode_and_acl(tmp_path) -> None:
    # Same class as IF-02 at audit.py:535. install.sh puts a DEFAULT ACL on
    # /var/lib/ups-orchestrator so a fresh inode usually inherits one anyway — but
    # that is a property of a directory this code does not control, and
    # $UPS_ORCH_STATE can move the marker out from under it.
    from ups_orchestrator import audit

    marker = tmp_path / "boot-audit.json"
    marker.write_text("{}")
    os.chmod(marker, 0o640)
    _acl_or_skip(marker)

    audit._write_marker(marker, "boot-id-1")

    assert json.loads(marker.read_text())["boot_id"] == "boot-id-1"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o640
    acl = subprocess.run(
        ["getfacl", "-n", "--omit-header", str(marker)], capture_output=True, text=True, check=True
    ).stdout
    assert "user:65534:r--" in acl


# --- IF-01: two writers, one file ---------------------------------------------
#
# The shipped deployment has klein's long-lived `watch` process and the `nut` user's
# upssched dispatcher both writing `state.json`. `shutdowns_sent` is the fire-once
# dedupe for projected machines, so a lost entry is a SECOND shutdown command sent to
# a box that is already going down.


def test_reload_if_changed_picks_up_another_writers_state(tmp_path) -> None:
    # The auditor's exact interleaving: the event path records two pushes, and the
    # long-lived poll loop's next look must see them rather than its startup copy.
    path = tmp_path / "state.json"
    watch = StateStore(path)  # the long-lived `watch` process
    watch.get("ups1").onbatt_since = 100
    watch.save()

    event = StateStore(path)  # a separate `remote-shutdown` / upssched invocation
    event.get("ups1").shutdowns_sent = ["mt", "spark"]
    event.save()

    assert watch.reload_if_changed() is True
    assert watch.get("ups1").shutdowns_sent == ["mt", "spark"]
    # ...and an unchanged file is not re-read, so the poll loop does not pay for a
    # parse every tick.
    assert watch.reload_if_changed() is False


def test_save_keeps_a_concurrent_writers_shutdowns_sent(tmp_path) -> None:
    # The window `reload_if_changed` alone cannot close: the event path writes DURING
    # a tick, after that tick's reload and before the save that ends it. Union, so the
    # ledger only ever grows within an outage — erring toward "already sent", which is
    # the direction that cannot produce a second shutdown.
    path = tmp_path / "state.json"
    watch = StateStore(path)
    watch.get("ups1").onbatt_since = 100
    watch.save()

    event = StateStore(path)
    event.get("ups1").shutdowns_sent = ["mt"]
    event.save()

    watch.get("ups1").last_tick_notified = 200  # the in-flight tick's own bookkeeping
    watch.save()

    on_disk = json.loads(path.read_text())["ups1"]
    assert on_disk["shutdowns_sent"] == ["mt"]  # not discarded
    assert on_disk["last_tick_notified"] == 200  # and this writer's work still landed


def test_save_adopts_a_ups_only_the_other_writer_knows_about(tmp_path) -> None:
    # The two writers can hold different config views (a UPS added while `watch` is
    # up). Rewriting the whole file would delete the other's record outright.
    path = tmp_path / "state.json"
    watch = StateStore(path)
    watch.get("ups1").onbatt_since = 100
    watch.save()

    event = StateStore(path)
    event.get("ups2").shutdowns_sent = ["spark"]
    event.save()

    watch.save()

    assert json.loads(path.read_text())["ups2"]["shutdowns_sent"] == ["spark"]


def test_reload_is_inert_while_the_file_does_not_exist_yet(tmp_path) -> None:
    # First-ever run: nothing on disk to reload from, and the poll loop's in-memory
    # work must not be thrown away by a check that mistakes "absent" for "changed".
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.get("ups1").shutdowns_sent = ["mt"]

    assert store.reload_if_changed() is False
    assert store.get("ups1").shutdowns_sent == ["mt"]

    store.save()
    assert json.loads(path.read_text())["ups1"]["shutdowns_sent"] == ["mt"]
    assert store.reload_if_changed() is False  # our own write is not "external"


def test_save_completes_when_the_acl_helpers_hang(monkeypatch, tmp_path, caplog) -> None:
    # MED-02. _copy_acl is on StateStore.save, which the watch loop runs every poll.
    # An unbounded getfacl on a stale NFS/CIFS mount or a wedged FUSE filesystem
    # blocks in D state and takes handle_tick with it — no traceback, no exit, and
    # Restart=always never fires because the process is still alive. That is the
    # T-02-25 hang class, in code merged in the same phase. TimeoutExpired is not an
    # OSError, so the old `except OSError` would not have caught it either.
    path = tmp_path / "state.json"
    path.write_text("{}")
    store = StateStore(path)
    store.get("ups1").onbatt_since = 1

    seen: list[float | None] = []

    def _hung(argv, **kw):  # noqa: ANN001, ANN003
        seen.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired(cmd=argv[0], timeout=kw.get("timeout") or 0)

    monkeypatch.setattr(state_mod.subprocess, "run", _hung)
    with caplog.at_level("WARNING"):
        store.save()  # must return, not hang and not raise

    assert seen and all(t == 5 for t in seen)  # every ACL call is bounded
    assert json.loads(path.read_text())["ups1"]["onbatt_since"] == 1


def test_replace_follows_a_symlinked_destination(tmp_path) -> None:
    # MED-03. Path.stat() follows a symlink; Path.replace() does not — it renames
    # OVER the link path. A deployment that symlinks config.json into a git-managed
    # or /srv location was silently detached by the first write: the daemon then read
    # a file the operator did not edit and edited a file the daemon did not read, and
    # because the metadata copied was the TARGET's, nothing looked wrong.
    real = tmp_path / "real.json"
    real.write_text("{}")
    os.chmod(real, 0o640)
    link = tmp_path / "config.json"
    link.symlink_to(real)

    tmp = tmp_path / ".config.json.tmp"
    tmp.write_text('{"new": 1}')
    state_mod.replace_preserving_metadata(tmp, link)

    assert link.is_symlink(), "the operator's symlink was replaced by a regular file"
    assert json.loads(real.read_text()) == {"new": 1}  # the real file got the write
    assert stat.S_IMODE(real.stat().st_mode) == 0o640  # ...and kept its mode


def test_state_store_save_through_a_symlink_keeps_the_link(tmp_path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "state.json"
    link.symlink_to(real)

    store = StateStore(link)
    store.get("ups1").onbatt_since = 7
    store.save()

    assert link.is_symlink()
    assert json.loads(real.read_text())["ups1"]["onbatt_since"] == 7


def test_chown_eperm_as_root_is_logged_and_as_a_user_is_not(monkeypatch, tmp_path, caplog) -> None:
    # MED-08. "Expected for an unprivileged writer" is true; as ROOT an EPERM from
    # chown means a restricted-capability container, an immutable attribute or an
    # idmapped mount, and the file then silently keeps the writer's ownership. T-02-23
    # exists because the live box lost its ACL and nobody noticed for weeks; silencing
    # the one signal that would show a recurrence recreates the conditions.
    path = tmp_path / "state.json"
    path.write_text("{}")

    def _eperm(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(state_mod.os, "chown", _eperm)

    monkeypatch.setattr(state_mod.os, "geteuid", lambda: 1000)
    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    with caplog.at_level("WARNING"):
        store.save()
    # An unprivileged writer whose ownership does NOT change stays quiet: nothing
    # was lost, so there is nothing to report.
    assert caplog.records == []

    monkeypatch.setattr(state_mod.os, "geteuid", lambda: 0)
    with caplog.at_level("WARNING"):
        store.save()
    assert any("could not preserve owner/group" in rec.message for rec in caplog.records)


def test_chown_eperm_warns_when_ownership_actually_flips_even_unprivileged(
    monkeypatch, tmp_path, caplog
) -> None:
    """F7: the MED-08 warning was gated on `geteuid() == 0`.

    The daemon runs as `klein`, so production ALWAYS took the DEBUG branch — and
    `state.json` has two writers (klein's `watch`, nut's `upssched` via the
    dispatcher), which is precisely the configuration where a failed chown flips
    ownership silently. The one signal for a T-02-23 recurrence was aimed at an
    identity that never fires it.

    The question that matters is not "am I root" but "does this change the file's
    owner", so that is what is asked now.
    """
    path = tmp_path / "state.json"
    path.write_text("{}")

    def _eperm(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")

    real_stat = state_mod.os.stat

    def _dest_owned_by_someone_else(target, *a, **k):
        st = real_stat(target, *a, **k)
        if str(target) == str(path):
            # The destination belongs to `nut`; the writer is `klein`.
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 991, 991) + tuple(st)[6:]
            )
        return st

    monkeypatch.setattr(state_mod.os, "chown", _eperm)
    monkeypatch.setattr(state_mod.os, "stat", _dest_owned_by_someone_else)
    monkeypatch.setattr(state_mod.os, "geteuid", lambda: 1000)  # NOT root

    store = StateStore(path)
    store.get("ups1").onbatt_since = 1
    with caplog.at_level("WARNING"):
        store.save()

    assert any("could not preserve owner/group" in rec.message for rec in caplog.records)
    assert any("T-02-23" in rec.message for rec in caplog.records)
