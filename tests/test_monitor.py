"""Unit tests for the `monitor` CLI family (add/list/verify/remove).

Every side effect runs through a recording fake (run_ssh/run_nft/run_local) and a
tmp config.json, so nothing here touches a live host, the network, /etc,
systemctl, nft, or a real SSH connection. The password is only ever supplied via
the environment and must never appear on any recorded argv.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import ups_orchestrator
from ups_orchestrator import cli

# The operator's real policy-drop input base chain (mirrors the live box). The
# upsd accept is spliced INTO this chain, so tests must seed the nft file with it
# rather than an empty file — an empty file has no `hook input` chain and the
# splice would (correctly) error.
POLICY_DROP_RULESET = """\
table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iif "lo" accept
    }
}
"""


def _seed_nft(path: Path) -> Path:
    path.write_text(POLICY_DROP_RULESET)
    return path


class FakeSSH:
    """Records (alias, command, stdin); returns queued canned tuples.

    The 3-arg shape is load-bearing: the secret-safe remote write pipes the
    password-bearing config on stdin, never argv, so the recorded stdin lets a
    test assert the secret never reached the command string.
    """

    def __init__(self, responses: list[tuple[int, str, str]] | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._responses = list(responses or [])
        self.default = (0, "", "")

    def __call__(self, alias: str, command: str, stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append((alias, command, stdin))
        if self._responses:
            return self._responses.pop(0)
        return self.default

    @property
    def commands(self) -> list[str]:
        return [c for _a, c, _s in self.calls]

    @property
    def stdins(self) -> list[str | None]:
        return [s for _a, _c, s in self.calls]


class FakeNft:
    """Records the nft config path handed to ``nft -f`` and returns canned rc."""

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[str] = []
        self.rc = rc

    def __call__(self, path: str) -> tuple[int, str, str]:
        self.calls.append(path)
        return self.rc, "", "" if self.rc == 0 else "nft failed"


class FakeLocal:
    """Records (argv, stdin) for local installs/restarts; returns canned rc."""

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.rc = rc

    def __call__(self, argv, stdin: str | None = None) -> tuple[int, str, str]:  # noqa: ANN001
        self.calls.append((list(argv), stdin))
        return self.rc, "", "" if self.rc == 0 else "local failed"


def _write_config(
    path: Path, machines: list[dict] | None = None, extra: dict | None = None
) -> None:
    data: dict[str, object] = {
        "upses": {"cyberpower": {"label": "CyberPower"}},
        "nut_server": {"listen": ["127.0.0.1", "192.168.1.125"], "port": 3493},
        "monitored_machines": machines or [],
    }
    if extra:
        data.update(extra)
    path.write_text(json.dumps(data))


@pytest.fixture
def cfg_path(monkeypatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    _write_config(cfg)
    monkeypatch.setenv("UPS_ORCH_CONFIG", str(cfg))
    # Point the nft config at a writable tmp file seeded with the operator's
    # policy-drop input chain, so apply_nft splices into it without touching /etc.
    nft = tmp_path / "main.nft"
    _seed_nft(nft)
    monkeypatch.setattr(cli, "_NFT_PATH", str(nft))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(nft))
    return cfg


def _machine(name: str, ip: str = "192.168.1.114", ssh: str = "") -> dict:
    return {
        "name": name,
        "ssh": ssh or name,
        "ups": "cyberpower",
        "powervalue": 1,
        "os": "arch",
        "shutdown_cmd": "/sbin/shutdown -h now",
        "ip": ip,
        "backup": {"enabled": False, "kind": "remote"},
    }


# --- list ---------------------------------------------------------------------


def test_list_empty(cfg_path, capsys) -> None:
    assert cli.main(["monitor", "list"]) == 0
    assert "no machines enrolled" in capsys.readouterr().out.lower()


def test_list_prints_entries(cfg_path, capsys) -> None:
    _write_config(cfg_path, machines=[_machine("mt"), _machine("spark", ip="192.168.1.120")])
    assert cli.main(["monitor", "list"]) == 0
    out = capsys.readouterr().out
    assert "mt" in out and "spark" in out
    assert "192.168.1.114" in out and "cyberpower" in out


# --- verify -------------------------------------------------------------------


def test_verify_ok(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt")])
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    assert cli.main(["monitor", "verify", "mt"]) == 0


def test_verify_fail(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt")])
    ssh = FakeSSH([(1, "", "connection refused")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    assert cli.main(["monitor", "verify", "mt"]) == 1


def test_verify_unknown(cfg_path) -> None:
    assert cli.main(["monitor", "verify", "ghost"]) == 2


def test_verify_config_ups_metachar_rejected_rc2(cfg_path, monkeypatch) -> None:
    # A config with a shell-metachar `ups` must be refused at verify with rc 2,
    # never executed over SSH.
    _write_config(cfg_path, machines=[_machine("mt")])
    data = json.loads(cfg_path.read_text())
    data["monitored_machines"][0]["ups"] = "x; touch /tmp/pwned"
    cfg_path.write_text(json.dumps(data))
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    assert cli.main(["monitor", "verify", "mt"]) == 2
    assert ssh.calls == []


def test_verify_bad_primary_ip_rejected_rc2(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt")])
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    assert cli.main(["monitor", "verify", "mt", "--primary-ip", "not-an-ip"]) == 2
    assert ssh.calls == []


def test_verify_timeout_passed_through(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt")])
    seen: dict[str, object] = {}

    def _fake_verify(alias, ups, primary, run_ssh, *, timeout, deep):  # noqa: ANN001
        seen["timeout"] = timeout
        seen["deep"] = deep
        return True, "OL"

    monkeypatch.setattr(cli.nutclient, "verify_secondary", _fake_verify)
    assert cli.main(["monitor", "verify", "mt", "--timeout", "42"]) == 0
    assert seen["timeout"] == 42
    assert seen["deep"] is False


def test_verify_deep_triggers_journal_check(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt")])
    seen: dict[str, object] = {}

    def _fake_verify(alias, ups, primary, run_ssh, *, timeout, deep):  # noqa: ANN001
        seen["deep"] = deep
        return True, "OL"

    monkeypatch.setattr(cli.nutclient, "verify_secondary", _fake_verify)
    assert cli.main(["monitor", "verify", "mt", "--deep"]) == 0
    assert seen["deep"] is True


# --- remove -------------------------------------------------------------------


def test_remove_unknown(cfg_path) -> None:
    assert cli.main(["monitor", "remove", "ghost"]) == 2


def test_remove_order_disarm_firewall_persist(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt"), _machine("spark", ip="192.168.1.120")])
    order: list[str] = []

    ssh = FakeSSH()

    def _rec_ssh(alias, command, stdin=None):  # noqa: ANN001
        order.append("disarm")
        return ssh(alias, command, stdin)

    def _rec_nft(path):  # noqa: ANN001
        order.append("firewall")
        return 0, "", ""

    orig_persist = cli._monitor_persist

    def _rec_persist(*a, **k):  # noqa: ANN001
        order.append("persist")
        return orig_persist(*a, **k)

    monkeypatch.setattr(cli, "_monitor_run_ssh", _rec_ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", _rec_nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    monkeypatch.setattr(cli, "_monitor_persist", _rec_persist)
    assert cli.main(["monitor", "remove", "mt"]) == 0
    # disarm must precede firewall which must precede persist.
    assert order.index("disarm") < order.index("firewall") < order.index("persist")


def test_remove_keep_remote_skips_disarm(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt")])
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    assert cli.main(["monitor", "remove", "mt", "--keep-remote"]) == 0
    assert ssh.calls == []


def test_remove_persists_last_firewall_failure_leaves_config(cfg_path, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt"), _machine("spark", ip="192.168.1.120")])
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft(rc=1))
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    rc = cli.main(["monitor", "remove", "mt"])
    assert rc == 4
    # Config must be UNCHANGED: mt still present because persist never ran.
    data = json.loads(cfg_path.read_text())
    names = {m["name"] for m in data["monitored_machines"]}
    assert names == {"mt", "spark"}


def test_remove_rewrites_nft_from_survivors_empty_ip_filtered(cfg_path, monkeypatch) -> None:
    _write_config(
        cfg_path,
        machines=[_machine("mt"), _machine("spark", ip="192.168.1.120"), _machine("noip", ip="")],
    )
    captured: dict[str, object] = {}

    def _fake_apply_nft(path, saddrs, run_nft, restart_bouncer, reload_path=None):  # noqa: ANN001
        captured["saddrs"] = list(saddrs)
        captured["reload_path"] = reload_path
        restart_bouncer()
        return 0, "", ""

    monkeypatch.setattr(cli.nutclient, "apply_nft", _fake_apply_nft)
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    assert cli.main(["monitor", "remove", "mt"]) == 0
    # Survivors are spark (has ip) and noip (empty ip filtered out); mt removed.
    assert captured["saddrs"] == ["192.168.1.120"]


def test_remove_preserves_survivor_unknown_keys(cfg_path, monkeypatch) -> None:
    # WR-02: removing one machine must not strip an operator key (_comment) from
    # a SURVIVING machine's entry when the config is rewritten.
    mt = _machine("mt")
    spark = _machine("spark", ip="192.168.1.120")
    spark["_comment"] = "workstation"
    _write_config(cfg_path, machines=[mt, spark])
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    assert cli.main(["monitor", "remove", "mt"]) == 0
    data = json.loads(cfg_path.read_text())
    survivor = next(m for m in data["monitored_machines"] if m["name"] == "spark")
    assert survivor["_comment"] == "workstation"


def test_remove_dry_run_mutates_nothing(cfg_path, monkeypatch, capsys) -> None:
    _write_config(cfg_path, machines=[_machine("mt")])
    ssh = FakeSSH()
    nft = FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    assert cli.main(["monitor", "remove", "mt", "--dry-run"]) == 0
    assert ssh.calls == [] and nft.calls == []
    data = json.loads(cfg_path.read_text())
    assert {m["name"] for m in data["monitored_machines"]} == {"mt"}


# --- add ----------------------------------------------------------------------

_PW = "s3cr3t-secondary-pw"
# The literal SSH_CONNECTION line a real secondary shell echoes back:
# "<client_ip> <client_port> <server_ip> <server_port>". Field 1 is the client
# source address upsd actually sees.
_SSH_CONN = "192.168.1.114 22 192.168.1.125 3493\n"


@pytest.fixture
def add_env(monkeypatch, tmp_path: Path):
    """Root gate satisfied, /etc paths redirected to tmp, secret in env."""
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    conf = tmp_path / "upsd.conf"
    users = tmp_path / "upsd.users"
    conf.write_text("")
    users.write_text("")
    monkeypatch.setattr(cli, "_UPSD_CONF_PATH", str(conf))
    monkeypatch.setattr(cli, "_UPSD_USERS_PATH", str(users))
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    return {"conf": conf, "users": users, "tmp": tmp_path}


def _add_ssh_all_ok(*, with_ip: bool = False) -> FakeSSH:
    """A FakeSSH whose queued responses drive a clean add end-to-end.

    ``with_ip=True`` omits the leading SSH_CONNECTION probe response, matching a
    run where ``--ip`` is passed and the probe is skipped.
    """
    responses = [
        (0, "Linux\n/usr/bin/pacman\n", ""),  # detect_os
        (0, "", ""),  # install_nut_client
        (0, "", ""),  # write_remote_nut_config: cat upsmon (empty → clean)
        (0, "", ""),  # write_remote_nut_config: cat nut.conf
        (0, "", ""),  # write upsmon.conf
        (0, "", ""),  # write nut.conf
        (0, "", ""),  # enable_nut_monitor
        (0, "OL\n", ""),  # verify shallow upsc
        (0, "", ""),  # verify deep journal (no failures)
    ]
    if not with_ip:
        responses.insert(0, (0, _SSH_CONN, ""))  # echo $SSH_CONNECTION
    return FakeSSH(responses)


def test_add_missing_password_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.delenv(cli._SECRET_ENV, raising=False)
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 2


def test_add_bad_shutdown_cmd_quote_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--ip",
            "1.2.3.4",
            "--shutdown-cmd",
            'foo "bar"',
        ]
    )
    assert rc == 2


def test_add_bad_ip_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    rc = cli.main(
        ["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "not-an-ip"]
    )
    assert rc == 2


def test_add_ups_metachar_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "x; touch /tmp/pwned",
            "--ip",
            "1.2.3.4",
        ]
    )
    assert rc == 2


def test_add_bad_primary_ip_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--ip",
            "1.2.3.4",
            "--primary-ip",
            "$(id)",
        ]
    )
    assert rc == 2


def test_add_dual_regime_refused_without_force_rc2(cfg_path, monkeypatch) -> None:
    _write_config(
        cfg_path,
        extra={
            "upses": {
                "cyberpower": {
                    "label": "CyberPower",
                    "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
                }
            }
        },
    )
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 2


def test_add_native_dual_regime_refused_across_a_DIFFERENT_ups(cfg_path, monkeypatch) -> None:
    # The CLI half of the cross-UPS blocker. `--method native --ups cyberpower3` with an
    # enabled `mt` target on cyberpower used to call a narrowed `dual_regime_conflicts`,
    # find nothing, and write silently. A native authority is keyed to no UPS in this
    # file, so the gate must fire wherever the colliding target lives.
    _write_config(
        cfg_path,
        extra={
            "upses": {
                "cyberpower": {
                    "label": "CyberPower",
                    "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
                },
                "cyberpower3": {"label": "CyberPower3"},
            }
        },
    )
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower3",
            "--method",
            "native",
            "--ip",
            "1.2.3.4",
        ]
    )
    assert rc == 2
    # The config was not written: no record was enrolled behind the operator's back.
    assert json.loads(cfg_path.read_text())["monitored_machines"] == []


def test_add_PUSH_dual_regime_still_allowed_across_a_different_ups(cfg_path, monkeypatch) -> None:
    # The preserved carve-out, pinned so the fix above cannot over-reach: an `ssh` push
    # on cyberpower3 fires only on cyberpower3's outage, so a same-named target on
    # cyberpower is a different power domain and the gate must stay silent. rc 3 is the
    # LATER remote-config guard, which is what proves the dual-regime gate did not fire.
    _write_config(
        cfg_path,
        extra={
            "upses": {
                "cyberpower": {
                    "label": "CyberPower",
                    "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
                },
                "cyberpower3": {"label": "CyberPower3"},
            }
        },
    )
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    rc = cli.main(
        ["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower3", "--method", "ssh"]
    )
    assert rc == 0
    (rec,) = json.loads(cfg_path.read_text())["monitored_machines"]
    assert (rec["name"], rec["shutdown_method"], rec["ups"]) == ("mt", "ssh", "cyberpower3")


def test_add_dual_regime_allowed_with_force(cfg_path, add_env, monkeypatch) -> None:
    _write_config(
        cfg_path,
        extra={
            "upses": {
                "cyberpower": {
                    "label": "CyberPower",
                    "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
                }
            }
        },
    )
    ssh = _add_ssh_all_ok(with_ip=True)
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(
        ["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4", "--force"]
    )
    assert rc == 0


def test_add_dry_run_applies_nothing(cfg_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    ssh = FakeSSH()
    local = FakeLocal()
    nft = FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", local)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--ip",
            "1.2.3.4",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert ssh.calls == [] and local.calls == [] and nft.calls == []
    data = json.loads(cfg_path.read_text())
    assert data["monitored_machines"] == []
    assert _PW not in capsys.readouterr().out


def test_add_full_sequence_order(cfg_path, add_env, monkeypatch) -> None:
    ssh = _add_ssh_all_ok()
    local = FakeLocal()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", local)
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower"])
    assert rc == 0
    cmds = ssh.commands
    # resolve-ip precedes detect precedes install precedes remote write precedes enable.
    i_resolve = next(i for i, c in enumerate(cmds) if "SSH_CONNECTION" in c)
    i_detect = next(i for i, c in enumerate(cmds) if "uname -s" in c)
    i_install = next(i for i, c in enumerate(cmds) if "pacman -Qq" in c or "dpkg-query" in c)
    i_write = next(i for i, c in enumerate(cmds) if "/dev/stdin" in c)
    i_enable = next(i for i, c in enumerate(cmds) if "nut-monitor" in c and "enable" in c)
    assert i_resolve < i_detect < i_install < i_write < i_enable


def test_add_password_reaches_both_files(cfg_path, add_env, monkeypatch) -> None:
    ssh = _add_ssh_all_ok()
    local = FakeLocal()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", local)
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower"])
    assert rc == 0
    # upsd.users write: the password is on the local install's STDIN.
    local_stdins = [s for _argv, s in local.calls if s is not None]
    assert any(_PW in s for s in local_stdins), "password missing from upsd.users write"
    # remote upsmon.conf: the password is on the SSH install's STDIN.
    ssh_stdins = [s for s in ssh.stdins if s is not None]
    assert any(_PW in s for s in ssh_stdins), "password missing from remote upsmon.conf"
    # The password appears in NO argv (local or ssh command strings).
    assert all(_PW not in c for c in ssh.commands)
    assert all(_PW not in tok for argv, _s in local.calls for tok in argv)


def test_add_ssh_connection_field1_parse(cfg_path, add_env, monkeypatch) -> None:
    ssh = _add_ssh_all_ok()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower"])
    assert rc == 0
    data = json.loads(cfg_path.read_text())
    entry = next(m for m in data["monitored_machines"] if m["name"] == "mt")
    assert entry["ip"] == "192.168.1.114"


# --- LIVE BUG #2: remote IP resolved to the gateway over a WAN/NAT SSH path ----


def test_parse_route_src_extracts_src_field() -> None:
    out = "192.168.1.125 dev eth0 src 192.168.1.114 uid 0 \n    cache \n"
    assert cli._parse_route_src(out) == "192.168.1.114"


def test_parse_route_src_none_when_absent_or_invalid() -> None:
    assert cli._parse_route_src("192.168.1.125 dev eth0 uid 0\n") is None
    assert cli._parse_route_src("... src not-an-ip ...") is None


def test_resolve_remote_ip_prefers_route_src_over_ssh_connection_gateway(monkeypatch) -> None:
    # The live bug: over a WAN/NAT SSH path, $SSH_CONNECTION field 1 is the
    # GATEWAY (192.168.1.1), not the machine's real LAN IP. `ip route get
    # <primary>` on the remote returns the true src (192.168.1.114) upsd sees.
    calls: list[str] = []

    def fake_ssh(alias, command, stdin=None):  # noqa: ANN001
        calls.append(command)
        if "route get" in command:
            return 0, "192.168.1.125 dev eth0 src 192.168.1.114 uid 0\n", ""
        if "SSH_CONNECTION" in command:
            return 0, "192.168.1.1 41000 203.0.113.9 22\n", ""  # gateway!
        return 0, "", ""

    monkeypatch.setattr(cli, "_monitor_run_ssh", fake_ssh)
    ip = cli._resolve_remote_ip("mt", None, "192.168.1.125")
    assert ip == "192.168.1.114"  # route src, NOT the gateway
    assert any("ip -o route get 192.168.1.125" in c for c in calls)


def test_resolve_remote_ip_falls_back_to_ssh_connection_when_route_fails(monkeypatch) -> None:
    def fake_ssh(alias, command, stdin=None):  # noqa: ANN001
        if "route get" in command:
            return 1, "", "network unreachable"
        if "SSH_CONNECTION" in command:
            return 0, "192.168.1.114 22 192.168.1.125 3493\n", ""
        return 0, "", ""

    monkeypatch.setattr(cli, "_monitor_run_ssh", fake_ssh)
    assert cli._resolve_remote_ip("mt", None, "192.168.1.125") == "192.168.1.114"


def test_resolve_remote_ip_explicit_overrides_everything(monkeypatch) -> None:
    def fake_ssh(alias, command, stdin=None):  # noqa: ANN001
        raise AssertionError("no probe should run when --ip is explicit")

    monkeypatch.setattr(cli, "_monitor_run_ssh", fake_ssh)
    assert cli._resolve_remote_ip("mt", "10.0.0.5", "192.168.1.125") == "10.0.0.5"


def test_add_persists_route_src_not_gateway(cfg_path, add_env, monkeypatch) -> None:
    # End-to-end: --primary-ip given (so the remote route-get runs toward it), a
    # WAN SSH path returns the gateway on $SSH_CONNECTION. The persisted entry
    # must carry the route src (192.168.1.114), never the gateway.
    def fake_ssh(alias, command, stdin=None):  # noqa: ANN001
        if "route get" in command:
            return 0, "192.168.1.125 dev eth0 src 192.168.1.114 uid 0\n", ""
        if "SSH_CONNECTION" in command:
            return 0, "192.168.1.1 41000 203.0.113.9 22\n", ""  # gateway
        if "uname -s" in command:
            return 0, "Linux\n/usr/bin/pacman\n", ""
        if "ups.status" in command:
            return 0, "OL\n", ""
        return 0, "", ""

    monkeypatch.setattr(cli, "_monitor_run_ssh", fake_ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--primary-ip",
            "192.168.1.125",
        ]
    )
    assert rc == 0
    data = json.loads(cfg_path.read_text())
    entry = next(m for m in data["monitored_machines"] if m["name"] == "mt")
    assert entry["ip"] == "192.168.1.114"  # route src, not the gateway 192.168.1.1


# --- LIVE BUG #3: primary LAN LISTEN auto-detect silently failed --------------


def test_resolve_primary_ip_prefers_lan_listen(monkeypatch, tmp_path) -> None:
    from ups_orchestrator.config import Config

    cfg = tmp_path / "config.json"
    _write_config(cfg)  # listen includes 192.168.1.125
    conf = Config.load(cfg)
    assert cli._resolve_primary_ip(conf, None, "192.168.1.114") == "192.168.1.125"


def test_resolve_primary_ip_autodetects_via_route_when_only_loopback(monkeypatch, tmp_path) -> None:
    # No --primary-ip and only a loopback LISTEN: the old code silently returned
    # 127.0.0.1, so no LAN LISTEN was written and enrollment failed at verify.
    # Now it routes locally toward the machine and uses the src.
    from ups_orchestrator.config import Config

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "upses": {"cyberpower": {"label": "CyberPower"}},
                "nut_server": {"listen": ["127.0.0.1", "::1"], "port": 3493},
                "monitored_machines": [],
            }
        )
    )
    conf = Config.load(cfg)
    monkeypatch.setattr(
        cli,
        "_monitor_run_local_probe",
        lambda argv: (0, "192.168.1.114 dev eth0 src 192.168.1.125 uid 0\n", ""),
    )
    assert cli._resolve_primary_ip(conf, None, "192.168.1.114") == "192.168.1.125"


def test_resolve_primary_ip_none_when_autodetect_fails(monkeypatch, tmp_path) -> None:
    from ups_orchestrator.config import Config

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "upses": {"cyberpower": {"label": "CyberPower"}},
                "nut_server": {"listen": ["127.0.0.1"], "port": 3493},
                "monitored_machines": [],
            }
        )
    )
    conf = Config.load(cfg)
    monkeypatch.setattr(cli, "_monitor_run_local_probe", lambda argv: (1, "", "no route"))
    assert cli._resolve_primary_ip(conf, None, "192.168.1.114") is None


def test_add_errors_rc2_when_primary_ip_undetectable(cfg_path, add_env, monkeypatch) -> None:
    # End-to-end: only loopback LISTEN, no --primary-ip, route auto-detect fails
    # → clean rc 2 telling the operator to pass --primary-ip, NOT a silent
    # localhost-only enrollment that fails later at verify.
    cfg_path.write_text(
        json.dumps(
            {
                "upses": {"cyberpower": {"label": "CyberPower"}},
                "nut_server": {"listen": ["127.0.0.1"], "port": 3493},
                "monitored_machines": [],
            }
        )
    )
    monkeypatch.setattr(cli, "_monitor_run_local_probe", lambda argv: (1, "", "no route"))
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 2


def test_add_persists_machine_without_password(cfg_path, add_env, monkeypatch) -> None:
    _write_config(cfg_path, extra={"_comment": "keep me"})
    ssh = _add_ssh_all_ok(with_ip=True)
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 0
    raw = cfg_path.read_text()
    assert _PW not in raw
    data = json.loads(raw)
    assert data["_comment"] == "keep me"  # unknown key preserved
    entry = next(m for m in data["monitored_machines"] if m["name"] == "mt")
    assert "password" not in entry


def test_add_idempotent_no_duplicate(cfg_path, add_env, monkeypatch) -> None:
    _write_config(cfg_path, machines=[_machine("mt", ip="9.9.9.9")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 0
    data = json.loads(cfg_path.read_text())
    names = [m["name"] for m in data["monitored_machines"]]
    assert names == ["mt"]  # replaced, not duplicated
    assert data["monitored_machines"][0]["ip"] == "1.2.3.4"


def test_add_reenroll_preserves_machine_unknown_key(cfg_path, add_env, monkeypatch) -> None:
    # WR-02: re-adding an existing machine (idempotent replace) must keep that
    # machine's own operator key (_comment), not just top-level config keys.
    mt = _machine("mt", ip="9.9.9.9")
    mt["_comment"] = "kitchen pi"
    _write_config(cfg_path, machines=[mt])
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 0
    data = json.loads(cfg_path.read_text())
    entry = next(m for m in data["monitored_machines"] if m["name"] == "mt")
    assert entry["_comment"] == "kitchen pi"
    assert entry["ip"] == "1.2.3.4"  # known field still updated


def test_add_whitespace_name_shadows_existing_no_duplicate(cfg_path, add_env, monkeypatch) -> None:
    # WR-04(b): a padded --name (" mt ") must normalize like _monitor_find and
    # shadow the existing "mt", replacing it rather than persisting a duplicate.
    _write_config(cfg_path, machines=[_machine("mt", ip="9.9.9.9")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(
        ["monitor", "add", " mt ", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"]
    )
    assert rc == 0
    data = json.loads(cfg_path.read_text())
    names = [m["name"].strip() for m in data["monitored_machines"]]
    assert names.count("mt") == 1  # shadowed, not duplicated


def test_add_non_root_rc4(cfg_path, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    conf = tmp_path / "upsd.conf"
    users = tmp_path / "upsd.users"
    conf.write_text("")
    users.write_text("")
    monkeypatch.setattr(cli, "_UPSD_CONF_PATH", str(conf))
    monkeypatch.setattr(cli, "_UPSD_USERS_PATH", str(users))
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok())
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 4


def test_add_remote_install_fail_rc3(cfg_path, add_env, monkeypatch) -> None:
    ssh = FakeSSH(
        [
            (0, _SSH_CONN, ""),  # resolve ip
            (0, "Linux\n/usr/bin/pacman\n", ""),  # detect
            (1, "", "no network"),  # install fails
        ]
    )
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower"])
    assert rc == 3


def test_add_verify_fail_rc5(cfg_path, add_env, monkeypatch) -> None:
    ssh = FakeSSH(
        [
            (0, _SSH_CONN, ""),  # resolve
            (0, "Linux\n/usr/bin/pacman\n", ""),  # detect
            (0, "", ""),  # install
            (0, "", ""),  # cat upsmon
            (0, "", ""),  # cat nut.conf
            (0, "", ""),  # write upsmon
            (0, "", ""),  # write nut.conf
            (0, "", ""),  # enable
            (1, "", "connection refused"),  # verify shallow fails
        ]
    )
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower"])
    assert rc == 5


def test_add_no_firewall_skips_nft(cfg_path, add_env, monkeypatch) -> None:
    nft = FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok())
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--no-firewall"])
    assert rc == 0
    assert nft.calls == []


def test_add_missing_base_chain_file_errors_rc4(cfg_path, add_env, monkeypatch):
    # LIVE BUG #1: the accept is spliced into the operator's EXISTING input base
    # chain, so its file is a hard prerequisite. A missing file must surface as a
    # clean rc-4 (bootstrap_primary firewall failure), NOT a silent
    # "write a fresh useless table" (the old dedicated-file behaviour that never
    # opened the port on a policy-drop firewall).
    nft_path = add_env["tmp"] / "nftables.d" / "main.nft"  # parent absent
    assert not nft_path.parent.exists()
    nft = FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_NFT_PATH", str(nft_path))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(nft_path))
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 4
    assert nft.calls == []  # never reloaded — the splice failed before nft -f


def test_add_no_firewall_leaves_existing_managed_rule_untouched(cfg_path, add_env, monkeypatch):
    # CR-02: --no-firewall must NOT rewrite the nft file. An existing managed
    # accept (authorizing other secondaries) must survive byte-for-byte; the old
    # bug passed saddrs=[] which dropped the whole rule.
    from ups_orchestrator import nutclient

    nft_path = add_env["tmp"] / "u.nft"
    existing, _ = nutclient.upsert_nft_input_chain(POLICY_DROP_RULESET, ["192.168.1.99"])
    nft_path.write_text(existing)
    nft = FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok())
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_NFT_PATH", str(nft_path))
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--no-firewall"])
    assert rc == 0
    assert nft.calls == []
    assert nft_path.read_text() == existing  # rule preserved, secondary still authorized


def test_add_no_restart_bouncer_skips_bouncer(cfg_path, add_env, monkeypatch) -> None:
    restarts: list[int] = []
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: restarts.append(1))
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok())
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(
        ["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--no-restart-bouncer"]
    )
    assert rc == 0
    assert restarts == []


def test_add_refuse_on_existing_monitor_line_without_force(cfg_path, add_env, monkeypatch) -> None:
    # The remote already has an operator MONITOR line outside our marker block.
    ssh = FakeSSH(
        [
            (0, _SSH_CONN, ""),  # resolve
            (0, "Linux\n/usr/bin/pacman\n", ""),  # detect
            (0, "", ""),  # install
            (0, "MONITOR myups@host 1 admin pw primary\n", ""),  # cat upsmon (operator config)
            (0, "", ""),  # cat nut.conf
        ]
    )
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower"])
    assert rc == 3  # write_remote_nut_config guard refusal → remote bootstrap failure


# --- T-02-23: _monitor_persist preserves config file metadata -----------------
#
# `_monitor_persist` writes /etc/ups-orchestrator/config.json via the same
# temp+fsync+replace idiom as StateStore.save; a bare Path.replace stripped
# the installer's 0640 root:nut + run-user ACL down to 0600 root:root on the
# live box (see 02-09-PLAN.md). These prove the fix using state.py's shared
# helper, entirely under tmp_path.


def test_monitor_persist_preserves_mode_group_acl(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write_config(cfg, machines=[_machine("mt")])
    os.chmod(cfg, 0o640)

    current_gid = cfg.stat().st_gid
    alt_gid = next((gid for gid in os.getgroups() if gid != current_gid), None)
    if alt_gid is not None:
        os.chown(cfg, -1, alt_gid)

    acl_supported = shutil.which("setfacl") is not None and shutil.which("getfacl") is not None
    if acl_supported:
        setfacl = subprocess.run(
            ["setfacl", "-m", "u:65534:r", str(cfg)], capture_output=True, text=True
        )
        acl_supported = setfacl.returncode == 0

    cli._monitor_persist(cfg, [_machine("mt"), _machine("spark", ip="192.168.1.120")])

    after = json.loads(cfg.read_text())
    assert len(after["monitored_machines"]) == 2  # the write actually happened

    # Mode is always assertable — this is the core of the fix and never skips.
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    # Group and ACL are only assertable where the environment supports them;
    # the mode assertion above means the test itself is never skipped outright.
    if alt_gid is not None:
        assert cfg.stat().st_gid == alt_gid
    if acl_supported:
        acl = subprocess.run(
            ["getfacl", "-n", "--omit-header", str(cfg)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "user:65534:r--" in acl


def test_named_temporaryfile_confined_to_known_sites() -> None:
    # Source-level guard: the defect is a PATTERN (raw tempfile.NamedTemporaryFile
    # + bare .replace()), and the only durable defence against a third copy is a
    # test that notices the pattern re-appearing anywhere else under src/.
    #
    # IF-02/IF-08 narrowed this from two sites to one: `state.write_text_preserving_
    # metadata` is now the whole project's writer, and `_monitor_persist` calls it
    # rather than carrying its own copy.
    src_dir = Path(ups_orchestrator.__file__).parent
    hits = {
        str(path.relative_to(src_dir))
        for path in sorted(src_dir.rglob("*.py"))
        if "NamedTemporaryFile" in path.read_text()
    }
    assert hits == {"state.py"}, (
        "NamedTemporaryFile appeared outside state.write_text_preserving_metadata "
        "(T-02-23, IF-02, IF-08) — a new temp-file write must go through that "
        "helper instead of reimplementing the unprotected temp+replace idiom"
    )


def test_every_shipped_systemd_unit_is_installed_by_something() -> None:
    """IF-09: two units shipped in deploy/systemd/ and were installed by nothing.

    ``ups-orchestrator-selftest.service`` and ``.timer`` referenced
    ``/usr/local/bin/ups-orchestrator selftest`` and looked like a supported
    feature, but neither ``install.sh`` nor ``install-user-service.sh`` ever copied
    them anywhere — so ``systemctl --user start ups-orchestrator-selftest`` failed
    with "unit not found" on every deployment that ever existed. A shipped-but-
    unreachable unit is the same class of drift as a documented-but-missing verb,
    so guard the whole directory rather than those two names.
    """
    repo = Path(__file__).resolve().parent.parent
    units = {p.name for p in (repo / "deploy" / "systemd").iterdir() if p.is_file()}
    assert units, "deploy/systemd/ is empty — the guard would pass vacuously"
    installers = "\n".join(
        (repo / "deploy" / name).read_text() for name in ("install.sh", "install-user-service.sh")
    )
    orphans = sorted(name for name in units if name not in installers)
    assert orphans == [], (
        "shipped systemd unit(s) that no installer references — they cannot be "
        f"started on any deployment (IF-09): {orphans}"
    )


def test_installer_and_docs_grant_serial_device_access() -> None:
    """IF-03: nothing shipped granted the serial transport's device access.

    ``grep -rn dialout deploy/ docs/ Makefile README.md`` returned ZERO hits.
    ``install.sh`` grants the ``nut`` group, ACLs on /etc and /var/lib, and a
    poweroff sudoers entry — every privilege the daemon needs except the one the
    serial transport needs, and the serial transport opens ``/dev/ttyUSB*`` ``"wb"``
    from that same run-user-owned ``systemd --user`` unit. Fresh installs therefore
    failed every serial push and every ``shutdown rehearse`` with PermissionError;
    the development box masked it by already being in ``dialout``.

    A source-level assertion is the honest oracle here: the fix IS a deployment
    grant, and this suite must never add a user to a group or open a real device.
    """
    repo = Path(__file__).resolve().parent.parent
    install = (repo / "deploy" / "install.sh").read_text()
    assert "usermod -aG dialout" in install, (
        "deploy/install.sh does not grant the run user serial device access — every "
        "serial push and 'shutdown rehearse' fails with PermissionError on a fresh "
        "install (IF-03)"
    )
    assert '"$RUN_USER"' in install.split("usermod -aG dialout")[1][:20], (
        "the dialout grant must target the run user that owns the systemd --user "
        "watch unit, not root or a literal"
    )

    deployment = (repo / "docs" / "Deployment.md").read_text()
    assert "dialout" in deployment, "docs/Deployment.md does not mention dialout (IF-03)"
    # The second half of IF-03: the upssched dispatcher runs as `nut`, which is not
    # in dialout, so the docs must answer whether the push is reachable from there
    # rather than leaving it to fail during an outage.
    # Whitespace-normalised: the claim must survive a markdown re-wrap.
    assert "not reachable from the NUT event path" in " ".join(deployment.split()), (
        "docs/Deployment.md must state plainly whether the serial push is reachable "
        "from the `nut`-user upssched path (IF-03)"
    )


def test_no_bare_temp_rename_survives_anywhere_in_the_tree() -> None:
    """The other half of the same guard, and the one IF-02/IF-08 slipped through.

    Confining ``NamedTemporaryFile`` to one module says nothing about a writer that
    builds its temp path by hand — which is exactly what ``audit._write_marker``
    (``path.with_suffix(".tmp")``) and the ``disable-live-shutdown-targets`` heredoc
    did, in Python and in shell respectively, for the whole of Phase 2. Both ended in
    a bare ``tmp.replace(dest)``, which hands the destination the temp file's mode,
    owner and ACL (T-02-23). Scan ``src/`` AND ``deploy/``: the second one is where
    the higher-blast-radius copy lived, and a guard that only reads ``src/`` would
    have found nothing.

    Log ROTATION is a different operation and deliberately not matched: there the
    source is the real file, already carrying the metadata that should survive.
    """
    repo = Path(__file__).resolve().parent.parent
    # Anchored at statement start, so the prose in this repo that NAMES the pattern
    # (the comments and docstrings recording IF-02/IF-08) is not itself an offender.
    bare_rename = re.compile(r"^\s*tmp\w*\s*\.replace\s*\(")
    offenders = []
    for directory in (repo / "src", repo / "deploy"):
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if bare_rename.search(line):
                    offenders.append(f"{path.relative_to(repo)}:{lineno}: {line.strip()}")
    # The single legitimate site: the rename inside the metadata-preserving helper
    # itself, which has already carried mode/owner/ACL onto the temp file.
    assert offenders == ["src/ups_orchestrator/state.py:276: tmp_path.replace(dest_path)"], (
        "a bare temp-file rename over a destination reappeared (IF-02, IF-08, "
        "T-02-23) — it strips the destination's mode, owner and ACL. Use "
        "state.write_text_preserving_metadata / write_json_preserving_metadata:\n"
        + "\n".join(offenders)
    )


# =============================================================================
# 02-03 Task 1 — `monitor add` branches on --method (BL-02 / IB-03, T-02-10,
# T-02-11, T-02-12, T-02-23 transition guard, T-02-54 flag split, NEW-2)
#
# INV-DECLARED: every gate and every persist below reads the DECLARED
# `shutdown_method`. A record's effect is never consulted here.
# =============================================================================


def _entry(cfg_path: Path, name: str) -> dict:
    """The persisted monitored_machines entry for ``name``."""
    data = json.loads(cfg_path.read_text())
    return next(m for m in data["monitored_machines"] if m["name"].strip() == name)


def _machine_entry(
    name: str,
    *,
    method: str,
    ssh: str = "",
    ups: str = "cyberpower",
    ip: str = "",
    device: str = "",
    baud: int | None = None,
    cmd: str = "/sbin/shutdown -h now",
) -> dict:
    """A monitored_machines entry carrying an EXPLICIT shutdown_method."""
    entry: dict = {
        "name": name,
        "ssh": ssh,
        "ups": ups,
        "powervalue": 1,
        "os": "auto",
        "shutdown_cmd": cmd,
        "ip": ip,
        "backup": {"enabled": False, "kind": "remote"},
        "shutdown_method": method,
        "serial_device": device,
    }
    if baud is not None:
        entry["serial_baud"] = baud
    return entry


def _no_privileged_seams(monkeypatch) -> tuple[FakeSSH, FakeLocal, FakeNft, list]:
    """Wire every privileged seam to a recorder, so a record-only add proves it ran none."""
    ssh, local, nft = FakeSSH(), FakeLocal(), FakeNft()
    probes: list = []
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", local)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    monkeypatch.setattr(
        cli, "_monitor_run_local_probe", lambda argv: (probes.append(list(argv)), (1, "", ""))[1]
    )
    return ssh, local, nft, probes


_UPS_WITH_MT_TARGET = {
    "upses": {
        "cyberpower": {
            "label": "CyberPower",
            "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
        }
    }
}


# --- serial: record-only, before the password lookup --------------------------


def test_add_serial_records_device_and_baud_and_runs_no_privileged_step(
    cfg_path, monkeypatch
) -> None:
    # The env secret is DELETED: a serial add that still succeeds proves the
    # record-only branch runs BEFORE the secondary-password lookup (T-02-11).
    monkeypatch.delenv(cli._SECRET_ENV, raising=False)
    ssh, local, nft, probes = _no_privileged_seams(monkeypatch)
    rc = cli.main(
        [
            "monitor",
            "add",
            "spark",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-device",
            "/dev/ttyUSB0",
            "--serial-baud",
            "9600",
        ]
    )
    assert rc == 0
    assert ssh.calls == [] and local.calls == [] and nft.calls == [] and probes == []
    entry = _entry(cfg_path, "spark")
    assert entry["shutdown_method"] == "serial"
    assert entry["serial_device"] == "/dev/ttyUSB0"
    assert entry["serial_baud"] == 9600
    assert entry["ssh"] == ""  # no alias required, and none invented
    assert entry["ip"] == ""  # `ip` is written only by the native enrollment path
    from ups_orchestrator.config import MonitoredMachine

    assert MonitoredMachine.from_dict(entry).shutdown_method == "serial"


def test_add_serial_missing_baud_rc2_names_9600(cfg_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    _no_privileged_seams(monkeypatch)
    with caplog.at_level("ERROR"):
        rc = cli.main(
            [
                "monitor",
                "add",
                "spark",
                "--method",
                "serial",
                "--ups",
                "cyberpower",
                "--serial-device",
                "/dev/ttyUSB0",
            ]
        )
    assert rc == 2
    assert "9600" in caplog.text
    assert json.loads(cfg_path.read_text())["monitored_machines"] == []


@pytest.mark.parametrize("baud", ["0", "-1", "fast", "9600.5"])
def test_add_serial_invalid_baud_rc2_no_silent_fallback(cfg_path, monkeypatch, baud) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    _no_privileged_seams(monkeypatch)
    rc = cli.main(
        [
            "monitor",
            "add",
            "spark",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-device",
            "/dev/ttyUSB0",
            "--serial-baud",
            baud,
        ]
    )
    assert rc == 2
    assert json.loads(cfg_path.read_text())["monitored_machines"] == []


def test_add_serial_missing_device_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    _no_privileged_seams(monkeypatch)
    rc = cli.main(
        [
            "monitor",
            "add",
            "spark",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-baud",
            "9600",
        ]
    )
    assert rc == 2


def test_add_serial_dry_run_prints_record_only_plan_and_persists_nothing(
    cfg_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    ssh, local, nft, _p = _no_privileged_seams(monkeypatch)
    rc = cli.main(
        [
            "monitor",
            "add",
            "spark",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-device",
            "/dev/ttyUSB0",
            "--serial-baud",
            "9600",
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "record-only" in out and "/dev/ttyUSB0" in out and "9600" in out
    assert ssh.calls == [] and local.calls == [] and nft.calls == []
    assert json.loads(cfg_path.read_text())["monitored_machines"] == []


# --- ssh / none: record-only --------------------------------------------------


def test_add_ssh_records_alias_only(cfg_path, monkeypatch) -> None:
    monkeypatch.delenv(cli._SECRET_ENV, raising=False)
    ssh, local, nft, _p = _no_privileged_seams(monkeypatch)
    rc = cli.main(["monitor", "add", "mt", "--method", "ssh", "--ssh", "mt", "--ups", "cyberpower"])
    assert rc == 0
    assert ssh.calls == [] and local.calls == [] and nft.calls == []
    entry = _entry(cfg_path, "mt")
    assert entry["shutdown_method"] == "ssh"
    assert entry["ssh"] == "mt"
    assert entry["serial_device"] == "" and "serial_baud" not in entry
    assert entry["ip"] == ""


def test_add_ssh_without_alias_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    _no_privileged_seams(monkeypatch)
    assert cli.main(["monitor", "add", "mt", "--method", "ssh", "--ups", "cyberpower"]) == 2


def test_add_none_records_no_active_authority(cfg_path, monkeypatch) -> None:
    monkeypatch.delenv(cli._SECRET_ENV, raising=False)
    ssh, local, nft, _p = _no_privileged_seams(monkeypatch)
    rc = cli.main(["monitor", "add", "shelf", "--method", "none"])
    assert rc == 0
    assert ssh.calls == [] and local.calls == [] and nft.calls == []
    entry = _entry(cfg_path, "shelf")
    assert entry["shutdown_method"] == "none"
    assert entry["ssh"] == "" and entry["ups"] == "" and entry["ip"] == ""


# --- BL-02 / IB-03: no persist path may write the dataclass default -----------


def test_add_native_default_method_persists_native_not_none(cfg_path, add_env, monkeypatch) -> None:
    # BL-02: `--ups` with no `--method` is the Phase-1 invocation. It must persist
    # `native`, and a from_dict round-trip of the persisted dict must return it.
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 0
    entry = _entry(cfg_path, "mt")
    assert entry["shutdown_method"] == "native"
    from ups_orchestrator.config import MonitoredMachine

    assert MonitoredMachine.from_dict(entry).shutdown_method == "native"


def test_add_reenroll_of_live_native_record_persists_native_not_none(
    cfg_path, add_env, monkeypatch
) -> None:
    # IB-03, the LIVE instance of BL-02: re-running `monitor add spark` is Phase 1's
    # documented repair action, and step 6 re-arms spark's remote upsmon. A persisted
    # `none` would declare the box opted out while it still self-halts on FSD, and
    # both 02-06's detector and 02-07's projector key on the method — so a corrupted
    # record is invisible to them by construction. A FIRST-enrollment test passes
    # while this path stays broken, so the existing record is present here.
    _write_config(cfg_path, machines=[_machine("spark", ip="192.168.1.120")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")
    rc = cli.main(
        ["monitor", "add", "spark"]
        + ["--ssh", "spark", "--ups", "cyberpower", "--ip", "192.168.1.120"]
    )
    assert rc == 0
    entry = _entry(cfg_path, "spark")
    assert entry["shutdown_method"] == "native", "re-enroll wrote the dataclass default (BL-02)"
    from ups_orchestrator.config import MonitoredMachine

    assert MonitoredMachine.from_dict(entry).shutdown_method == "native"


def test_add_dual_regime_candidate_carries_the_resolved_method(cfg_path, monkeypatch) -> None:
    # The gate candidate matters as much as the persisted entry: the refusal text
    # reports the method, and a candidate defaulting to `none` sends the operator to
    # fix the wrong thing.
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    _no_privileged_seams(monkeypatch)
    seen: dict[str, object] = {}

    def _capture(machines, upses):  # noqa: ANN001
        seen["methods"] = {m.name: m.shutdown_method for m in machines}
        return ()

    monkeypatch.setattr(cli, "dual_regime_conflicts", _capture)
    rc = cli.main(
        [
            "monitor",
            "add",
            "spark",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-device",
            "/dev/ttyUSB0",
            "--serial-baud",
            "9600",
        ]
    )
    assert rc == 0
    assert seen["methods"] == {"spark": "serial"}


def test_remove_leaves_survivor_declared_method_unchanged(cfg_path, monkeypatch) -> None:
    # The survivor rewrite is the path that freezes the whole file into explicit
    # methods. A survivor carrying a load notice keeps its DECLARATION (INV-DEGRADE).
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="ssh", ssh="mt"),
            _machine_entry("spark", method="native", ssh="spark", ip="192.168.1.120"),
        ],
        extra=_UPS_WITH_MT_TARGET,
    )
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    assert cli.main(["monitor", "remove", "spark"]) == 0
    # mt is disarmed at load (it collides with an enabled legacy target) but its
    # DECLARATION must survive the rewrite untouched.
    assert _entry(cfg_path, "mt")["shutdown_method"] == "ssh"


# --- transition guard: keyed on the DECLARED method (T-02-23) -----------------


@pytest.mark.parametrize("method", ["serial", "ssh", "none"])
def test_add_native_to_push_transition_refused_rc2(cfg_path, monkeypatch, caplog, method) -> None:
    _write_config(cfg_path, machines=[_machine("spark", ip="192.168.1.120")])
    ssh, local, nft, _p = _no_privileged_seams(monkeypatch)
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    argv = ["monitor", "add", "spark", "--method", method, "--ups", "cyberpower"]
    if method == "serial":
        argv += ["--serial-device", "/dev/ttyUSB0", "--serial-baud", "9600"]
    if method == "ssh":
        argv += ["--ssh", "spark"]
    with caplog.at_level("ERROR"):
        rc = cli.main(argv)
    assert rc == 2
    assert "monitor remove spark" in caplog.text
    assert ssh.calls == []  # no implicit cross-host disarm
    # The declaration on disk is untouched.
    assert "shutdown_method" not in _entry(cfg_path, "spark")


def test_add_transition_guard_not_opened_by_a_load_degrade(cfg_path, monkeypatch, caplog) -> None:
    # A declared-native record that also collides with an enabled legacy target
    # carries an ERROR notice. `disarmed` carves native out, so its EFFECTIVE method
    # is still native — but the guard must key on the DECLARATION regardless, which
    # is what keeps a future notice class from opening a native->push switch over a
    # live remote upsmon.
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="native", ssh="mt", ip="192.168.1.114")],
        extra=_UPS_WITH_MT_TARGET,
    )
    _no_privileged_seams(monkeypatch)
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    from ups_orchestrator.config import Config

    loaded = Config.load(cfg_path)
    machine = loaded.monitored_machines[0]
    assert machine.load_notices, "fixture must carry a load notice for this test to mean anything"
    assert machine.shutdown_method == "native"
    with caplog.at_level("ERROR"):
        rc = cli.main(
            ["monitor", "add", "mt", "--method", "ssh", "--ssh", "mt", "--ups", "cyberpower"]
        )
    assert rc == 2
    assert "monitor remove mt" in caplog.text
    assert _entry(cfg_path, "mt")["shutdown_method"] == "native"


def test_add_force_gate_still_refuses_a_disarmed_record(cfg_path, monkeypatch) -> None:
    # `dual_regime_conflicts` reads DECLARED state, so the --force gate keeps firing
    # against a config `Config.load` already disarmed.
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="ssh", ssh="mt")],
        extra=_UPS_WITH_MT_TARGET,
    )
    _no_privileged_seams(monkeypatch)
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    argv = ["monitor", "add", "mt", "--method", "ssh", "--ssh", "mt", "--ups", "cyberpower"]
    assert cli.main(argv) == 2
    assert cli.main([*argv, "--force"]) == 0


# --- T-02-54: --force no longer authorises a remote NUT config clobber --------


def _ssh_with_operator_monitor_line() -> FakeSSH:
    return FakeSSH(
        [
            (0, "Linux\n/usr/bin/pacman\n", ""),  # detect (--ip given, no probe)
            (0, "", ""),  # install
            (0, "MONITOR myups@host 1 admin pw primary\n", ""),  # operator's own upsmon.conf
            (0, "", ""),  # cat nut.conf
            (0, "", ""),  # write upsmon.conf
            (0, "", ""),  # write nut.conf
            (0, "", ""),  # enable
            (0, "OL\n", ""),  # verify shallow
            (0, "", ""),  # verify deep
        ]
    )


def _native_add_env(add_env, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_monitor_run_local", FakeLocal())
    monkeypatch.setattr(cli, "_monitor_run_nft", FakeNft())
    monkeypatch.setattr(cli, "_NFT_PATH", str(add_env["tmp"] / "u.nft"))
    monkeypatch.setattr(cli, "_NFT_RELOAD_PATH", str(add_env["tmp"] / "u.nft"))
    _seed_nft(add_env["tmp"] / "u.nft")


def test_force_alone_does_not_authorise_the_remote_config_overwrite(
    cfg_path, add_env, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "_monitor_run_ssh", _ssh_with_operator_monitor_line())
    _native_add_env(add_env, monkeypatch)
    rc = cli.main(
        ["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4", "--force"]
    )
    assert rc == 3  # the remote guard still refuses; --force is local-only now


def test_force_remote_config_authorises_the_remote_overwrite(
    cfg_path, add_env, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "_monitor_run_ssh", _ssh_with_operator_monitor_line())
    _native_add_env(add_env, monkeypatch)
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--ip",
            "1.2.3.4",
            "--force-remote-config",
        ]
    )
    assert rc == 0


# --- T-02-10: the ssh alias is validated for SHAPE ----------------------------


@pytest.mark.parametrize("alias", ["-oProxyCommand=id", "mt; touch /tmp/pwned", "a b", "$(id)"])
def test_add_rejects_an_option_shaped_or_metachar_ssh_alias_rc2(
    cfg_path, monkeypatch, caplog, alias
) -> None:
    # `--ssh=<value>` is the single-token spelling — the only one by which an
    # option-shaped alias reaches our validator at all, since argparse rejects the
    # two-token form itself. Both roads end at rc 2; this one exercises OUR guard.
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    ssh, _l, _n, _p = _no_privileged_seams(monkeypatch)
    with caplog.at_level("ERROR"):
        rc = cli.main(["monitor", "add", "mt", f"--ssh={alias}", "--ups", "cyberpower"])
    assert rc == 2
    assert alias in caplog.text
    assert ssh.calls == []


def test_add_accepts_a_plain_ssh_alias(cfg_path, monkeypatch) -> None:
    monkeypatch.delenv(cli._SECRET_ENV, raising=False)
    _no_privileged_seams(monkeypatch)
    argv = ["monitor", "add", "mt", "--method", "ssh", "--ssh", "mt-01.lan", "--ups", "cyberpower"]
    assert cli.main(argv) == 0
    assert _entry(cfg_path, "mt")["ssh"] == "mt-01.lan"


# --- NEW-2: a push record's default shutdown_cmd is the ESCALATED form --------


@pytest.mark.parametrize("method", ["serial", "ssh"])
def test_add_push_defaults_to_the_escalated_shutdown_cmd(cfg_path, monkeypatch, method) -> None:
    from ups_orchestrator.config import requires_root_escalation

    monkeypatch.delenv(cli._SECRET_ENV, raising=False)
    _no_privileged_seams(monkeypatch)
    argv = ["monitor", "add", "mt", "--method", method, "--ups", "cyberpower"]
    if method == "serial":
        argv += ["--serial-device", "/dev/ttyUSB0", "--serial-baud", "9600"]
    else:
        argv += ["--ssh", "mt"]
    assert cli.main(argv) == 0
    cmd = _entry(cfg_path, "mt")["shutdown_cmd"]
    assert cmd == "sudo /sbin/shutdown -h now"
    assert not requires_root_escalation(cmd)


def test_add_native_keeps_the_unescalated_shutdown_cmd(cfg_path, add_env, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_monitor_run_ssh", _add_ssh_all_ok(with_ip=True))
    _native_add_env(add_env, monkeypatch)
    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "1.2.3.4"])
    assert rc == 0
    # upsmon runs SHUTDOWNCMD as root, so native's default is correct unescalated.
    assert _entry(cfg_path, "mt")["shutdown_cmd"] == "/sbin/shutdown -h now"


def test_add_push_shutdown_cmd_double_quote_still_rejected_rc2(cfg_path, monkeypatch) -> None:
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    _no_privileged_seams(monkeypatch)
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--method",
            "ssh",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--shutdown-cmd",
            'foo "bar"',
        ]
    )
    assert rc == 2


# =============================================================================
# 02-03 Task 2 — method- and degrade-aware list/verify/remove, plus the
# `watch`-startup degrade surface (RA-01, T-02-46, T-02-47, T-02-48, IW-04/06)
#
# INV-DECLARED again: every branch below reads `shutdown_method`, never
# `effective_method`. Branching on the effect would render a temporarily
# degraded machine identically to a deliberately-declared `none`.
# =============================================================================


class _RecNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, note):  # noqa: ANN001
        from ups_orchestrator.notify import DeliveryResult

        self.sent.append(note)
        return DeliveryResult(configured=True, ok=True)


def _load(cfg_path: Path):
    from ups_orchestrator.config import Config

    return Config.load(cfg_path)


# --- list ---------------------------------------------------------------------


def test_list_shows_declared_and_effective_method(cfg_path, capsys) -> None:
    # IW-04: today the line prints only `backup:on/off`, so the live spark renders
    # as `backup:off` while its governing authority is `native` — the CLI reports
    # the RETIRED flag and omits the one that decides whether the box dies.
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("spark", method="native", ssh="spark", ip="192.168.1.120"),
            # blank ups => structurally unprojectable => disarmed at load
            _machine_entry("mt", method="serial", ups="", device="/dev/ttyUSB0", baud=9600),
        ],
    )
    assert cli.main(["monitor", "list"]) == 0
    out = capsys.readouterr().out
    assert "method=native" in out
    assert "method=serial" in out and "effective:none" in out


def test_list_renders_degraded_notices(cfg_path, capsys) -> None:
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="serial", ups="", device="/dev/ttyUSB0", baud=9600)],
    )
    assert cli.main(["monitor", "list"]) == 0
    out = capsys.readouterr().out
    assert "DEGRADED CONFIG" in out
    assert "mt" in out and "can never fire" in out


def test_list_renders_degraded_notices_with_zero_machines(cfg_path, capsys) -> None:
    # A config can have zero machines and a disabled legacy target: the
    # `no machines enrolled` early return must STILL reach the notices.
    _write_config(
        cfg_path,
        extra={
            "upses": {
                "cyberpower": {
                    "label": "CyberPower",
                    "shutdown_targets": [{"name": "ghost", "enabled": True, "host": ""}],
                }
            }
        },
    )
    assert cli.main(["monitor", "list"]) == 0
    out = capsys.readouterr().out
    assert "no machines enrolled" in out
    assert "DEGRADED CONFIG" in out and "blank host" in out


def test_list_prints_no_degrade_block_when_clean(cfg_path, capsys) -> None:
    _write_config(cfg_path, machines=[_machine_entry("spark", method="native", ssh="spark")])
    assert cli.main(["monitor", "list"]) == 0
    assert "DEGRADED" not in capsys.readouterr().out


# --- verify: the four mutually distinguishable shapes (T-02-46) ---------------


def test_verify_disarmed_push_record_is_rc1_and_names_its_declaration(
    cfg_path, monkeypatch, capsys
) -> None:
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="serial", ups="", device="/dev/ttyUSB0", baud=9600)],
    )
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    rc = cli.main(["monitor", "verify", "mt"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "DISARMED (declared serial)" in out
    assert "can never fire" in out  # the reason travels with the verdict
    assert "no active shutdown authority" not in out


def test_verify_declared_none_without_ups_is_rc0_and_probes_nothing(
    cfg_path, monkeypatch, capsys
) -> None:
    _write_config(cfg_path, machines=[_machine_entry("shelf", method="none", ups="")])
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    rc = cli.main(["monitor", "verify", "shelf"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no active shutdown authority" in out
    assert "DISARMED" not in out
    assert ssh.calls == []


def test_verify_native_with_a_load_notice_still_probes_the_secondary(
    cfg_path, monkeypatch, capsys
) -> None:
    # 02-06: config cannot disarm a native authority. The remote upsmon is the only
    # thing that decides, and this probe is the only evidence this box has about it.
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="native", ssh="mt", ip="192.168.1.114")],
        extra=_UPS_WITH_MT_TARGET,
    )
    assert _load(cfg_path).monitored_machines[0].load_notices
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    rc = cli.main(["monitor", "verify", "mt"])
    out = capsys.readouterr().out
    assert rc == 0
    assert ssh.calls, "a declared-native record must ALWAYS run the secondary probe"
    assert "OK" in out
    assert "monitor remove mt" in out  # the only real disarm, named


def test_verify_declared_none_with_ups_advisory_probes_and_is_rc1_when_answered(
    cfg_path, monkeypatch, capsys
) -> None:
    # BL-02's exact signature. Reporting "no active authority" here would falsely
    # reassure the one operator who most needs the truth.
    _write_config(cfg_path, machines=[_machine_entry("spark", method="none", ups="cyberpower")])
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    rc = cli.main(["monitor", "verify", "spark"])
    out = capsys.readouterr().out
    assert rc == 1
    assert ssh.calls
    assert "monitor remove spark" in out
    assert "no active shutdown authority" not in out


def test_verify_declared_none_with_ups_is_rc0_when_no_secondary_answers(
    cfg_path, monkeypatch, capsys
) -> None:
    """IF-10: it printed FAIL and exited 0. A script keying on rc read the opposite.

    The rc semantics are the deliberate ones — rc 1 means "an undeclared authority is
    still live" — so a `none` record with nothing answering is genuinely rc 0. The
    defect was the WORD: the probe's raw OK/FAIL answers "did a secondary reply?",
    which for every non-native probe reason is the inverse of the verdict.
    """
    _write_config(cfg_path, machines=[_machine_entry("spark", method="none", ups="cyberpower")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH([(1, "", "connection refused")]))

    rc = cli.main(["monitor", "verify", "spark"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "spark: OK" in out
    assert "spark: FAIL" not in out, "printed FAIL while exiting 0 (IF-10)"


@pytest.mark.parametrize(
    ("method", "extra_fields", "probe_rc"),
    [
        ("none", {"ups": "cyberpower"}, 0),  # undeclared authority IS live -> rc 1
        ("none", {"ups": "cyberpower"}, 1),  # nothing answers -> rc 0
        ("native", {"ssh": "mt", "ip": "192.168.1.114"}, 0),
        ("native", {"ssh": "mt", "ip": "192.168.1.114"}, 1),
        ("ssh", {"ssh": "mt", "ups": "cyberpower", "ip": "192.168.1.114"}, 0),
        ("ssh", {"ssh": "mt", "ups": "cyberpower", "ip": "192.168.1.114"}, 1),
    ],
)
def test_verify_summary_word_never_disagrees_with_the_exit_code(
    cfg_path, monkeypatch, capsys, method, extra_fields, probe_rc
) -> None:
    # The invariant behind IF-10, over every probe reason and both probe outcomes:
    # whatever `monitor verify` prints as its per-machine verdict must be the same
    # answer the exit code gives, because scripts trust the rc and operators trust
    # the line. Either alone being wrong is recoverable; disagreeing is not.
    _write_config(cfg_path, machines=[_machine_entry("m1", method=method, **extra_fields)])
    monkeypatch.setattr(
        cli,
        "_monitor_run_ssh",
        FakeSSH([(probe_rc, "OL\n" if probe_rc == 0 else "", "" if probe_rc == 0 else "refused")]),
    )

    rc = cli.main(["monitor", "verify", "m1"])
    out = capsys.readouterr().out

    verdicts = [
        line.split("m1: ", 1)[1].split(" ", 1)[0] for line in out.splitlines() if "m1: " in line
    ]
    verdicts = [v for v in verdicts if v in ("OK", "FAIL")]
    assert verdicts, f"no verdict line printed for {method}/{probe_rc}: {out!r}"
    expected = "OK" if rc == 0 else "FAIL"
    assert set(verdicts) == {expected}, (
        f"declared {method!r}, probe rc {probe_rc}: printed {verdicts} but exited {rc}"
    )


def test_verify_declared_ssh_with_a_stale_enrollment_ip_advisory_probes_rc1(
    cfg_path, monkeypatch, capsys
) -> None:
    # IW-05: `ip` is written ONLY by the native enrollment path, so a push record
    # carrying one is a probable hand-edited former native secondary.
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="ssh", ssh="mt", ups="cyberpower", ip="192.168.1.114")
        ],
    )
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    rc = cli.main(["monitor", "verify", "mt"])
    out = capsys.readouterr().out
    assert rc == 1
    assert ssh.calls
    assert "DISARMED (declared ssh)" in out
    assert "monitor remove mt" in out


def test_verify_serial_checks_device_presence_through_the_injected_stat(
    cfg_path, monkeypatch, capsys
) -> None:
    _write_config(
        cfg_path,
        machines=[
            _machine_entry(
                "spark", method="serial", ups="cyberpower", device="/dev/ttyUSB0", baud=9600
            )
        ],
    )
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    seen: list[str] = []

    def _fake_stat(path: str):  # noqa: ANN202
        seen.append(path)
        return os.stat_result((stat.S_IFCHR | 0o660, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    rc = cli._monitor_verify(_load(cfg_path), ["spark"], stat_fn=_fake_stat)
    out = capsys.readouterr().out
    assert rc == 0
    assert seen == ["/dev/ttyUSB0"]  # no real /dev node was ever reached
    assert "9600" in out
    assert ssh.calls == []  # no ssh alias, no NUT probe


def test_verify_serial_missing_device_is_rc1(cfg_path, capsys) -> None:
    _write_config(
        cfg_path,
        machines=[
            _machine_entry(
                "spark", method="serial", ups="cyberpower", device="/dev/ttyUSB0", baud=9600
            )
        ],
    )

    def _fake_stat(path: str):  # noqa: ANN202
        raise FileNotFoundError(path)

    rc = cli._monitor_verify(_load(cfg_path), ["spark"], stat_fn=_fake_stat)
    assert rc == 1
    assert "/dev/ttyUSB0" in capsys.readouterr().out


def test_verify_ssh_probes_the_alias_and_runs_no_nut_check(cfg_path, monkeypatch, capsys) -> None:
    _write_config(
        cfg_path, machines=[_machine_entry("mt", method="ssh", ssh="mt", ups="cyberpower")]
    )
    ssh = FakeSSH([(0, "", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    called: list[str] = []
    monkeypatch.setattr(
        cli.nutclient,
        "verify_secondary",
        lambda *a, **k: (called.append("nut"), (True, "OL"))[1],
    )
    rc = cli.main(["monitor", "verify", "mt"])
    assert rc == 0
    assert ssh.calls and called == []
    assert "mt" in capsys.readouterr().out


def test_verify_ssh_unreachable_alias_is_rc1(cfg_path, monkeypatch) -> None:
    _write_config(
        cfg_path, machines=[_machine_entry("mt", method="ssh", ssh="mt", ups="cyberpower")]
    )
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH([(255, "", "no route to host")]))
    assert cli.main(["monitor", "verify", "mt"]) == 1


# --- remove: method-aware and ambiguity-refusing (T-02-48) --------------------


def test_remove_non_native_skips_the_remote_disarm_but_not_the_local_nft_rewrite(
    cfg_path, monkeypatch
) -> None:
    """ME-C4: the gate bundled two actions with very different blast radii.

    The REMOTE disarm SSHes into another host and runs `sudo systemctl` — skipping
    it for a record this tool never enrolled natively is right, and is what this
    still asserts. The nft rewrite recomputes a purely LOCAL file from the survivor
    list; skipping it was what left a stale saddr in the managed set for a host
    with no native record.

    This test previously asserted `nft.calls == []`, i.e. it encoded the defect.
    """
    _write_config(
        cfg_path,
        machines=[
            _machine_entry(
                "spark", method="serial", ups="cyberpower", device="/dev/ttyUSB0", baud=9600
            ),
            _machine_entry("mt", method="native", ssh="mt", ip="192.168.1.114"),
        ],
    )
    ssh, nft = FakeSSH(), FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    assert cli.main(["monitor", "remove", "spark"]) == 0
    assert ssh.calls == [], "no remote host may be touched for a non-native record"
    assert nft.calls, "the local, idempotent saddr rewrite must still run"
    data = json.loads(cfg_path.read_text())
    assert [m["name"] for m in data["monitored_machines"]] == ["mt"]
    # The native survivor is untouched, declaration and upsd accept included.
    assert _entry(cfg_path, "mt")["shutdown_method"] == "native"
    assert "192.168.1.114" in Path(cli._NFT_PATH).read_text()


def test_remove_non_native_revokes_the_stale_saddr_it_was_still_granted(
    cfg_path, monkeypatch
) -> None:
    """The concrete leak: a former native secondary hand-edited to `ssh`.

    `_survivor_saddrs` keeps only DECLARED-native records (HI-C2), so this
    record's ip is not in the set the rewrite computes — but nothing ever ran the
    rewrite, so the accept it had been granted survived its own removal.
    """
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="native", ssh="mt", ip="192.168.1.114"),
            _machine_entry("spark", method="native", ssh="spark", ip="192.168.1.120"),
        ],
    )
    ssh, nft = FakeSSH(), FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    # Seed the managed set with both, the way a native enrollment of each would.
    assert cli.main(["monitor", "remove", "spark"]) == 0
    assert "192.168.1.120" not in Path(cli._NFT_PATH).read_text()

    # Now demote mt to a push record by hand and remove it: the remote disarm is
    # skipped (correct), and its saddr must still go.
    _write_config(
        cfg_path, machines=[_machine_entry("mt", method="ssh", ssh="mt", ups="cyberpower")]
    )
    ssh.calls.clear()
    assert cli.main(["monitor", "remove", "mt"]) == 0
    assert ssh.calls == []
    assert "192.168.1.114" not in Path(cli._NFT_PATH).read_text()


def test_remove_non_native_persists_even_when_the_nft_sweep_fails(cfg_path, monkeypatch) -> None:
    """The sweep is a repair, not the operator's request.

    Refusing the removal on a failed rewrite would leave BOTH the record and the
    stale saddr — strictly worse than leaving the saddr alone and saying so. The
    native path keeps its fatal rc 4, where the accept is that record's own.
    """
    _write_config(
        cfg_path,
        machines=[_machine_entry("spark", method="ssh", ssh="spark", ups="cyberpower")],
    )
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    monkeypatch.setattr(cli, "_NFT_PATH", "/nonexistent/main.nft")  # apply_nft -> rc 2

    assert cli.main(["monitor", "remove", "spark"]) == 0
    assert json.loads(cfg_path.read_text())["monitored_machines"] == []


def test_remove_native_still_disarms_and_rewrites_the_firewall(cfg_path, monkeypatch) -> None:
    # A native survivor keeps the managed saddr set non-empty, so `apply_nft` has a
    # real rewrite to do (an empty set with no managed block is a legitimate no-op).
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="native", ssh="mt", ip="1.2.3.4"),
            _machine_entry("spark", method="native", ssh="spark", ip="192.168.1.120"),
        ],
    )
    ssh, nft = FakeSSH(), FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)
    assert cli.main(["monitor", "remove", "mt"]) == 0
    assert ssh.calls and nft.calls


def test_remove_refuses_an_ambiguous_name_rc2(cfg_path, monkeypatch) -> None:
    # 02-06 KEEPS every duplicate and disarms them all, so `_monitor_find`'s
    # first-wins would delete an arbitrary one of them.
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="ssh", ssh="mt"),
            _machine_entry("MT", method="ssh", ssh="mt2"),
        ],
    )
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    assert cli.main(["monitor", "remove", "mt"]) == 2
    assert ssh.calls == []
    data = json.loads(cfg_path.read_text())
    assert len(data["monitored_machines"]) == 2  # nothing deleted


# --- watch startup degrade surface + IW-06 -----------------------------------


@pytest.fixture
def watch_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("UPS_ORCH_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("UPS_ORCH_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("UPS_ORCH_NOTIFICATION_LOG", str(tmp_path / "notify.jsonl"))
    monkeypatch.setenv("UPS_ORCH_SAMPLES", str(tmp_path / "samples.jsonl"))
    monkeypatch.setattr(cli, "dispatch", lambda *_a, **_k: None)

    def _sleep(_s: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", _sleep)
    return tmp_path


def test_watch_startup_sends_exactly_one_aggregated_notification(
    cfg_path, watch_env, monkeypatch, caplog
) -> None:
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="serial", ups="", device="/dev/ttyUSB0", baud=9600),
            _machine_entry("spark", method="ssh", ssh="spark", ups="nosuchups"),
        ],
    )
    rec = _RecNotifier()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: rec)
    with caplog.at_level("WARNING"), pytest.raises(KeyboardInterrupt):
        cli.main(["watch"])
    assert len(rec.sent) == 1, "one aggregated notification, never one per notice"
    body = rec.sent[0].title + rec.sent[0].body + str(rec.sent[0].fields)
    assert "mt" in body and "spark" in body
    assert "mt" in caplog.text and "spark" in caplog.text


def test_watch_startup_sends_nothing_when_the_config_is_clean(
    cfg_path, watch_env, monkeypatch
) -> None:
    _write_config(cfg_path, machines=[_machine_entry("spark", method="native", ssh="spark")])
    rec = _RecNotifier()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: rec)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["watch"])
    assert rec.sent == []


def test_watch_returns_nonzero_when_the_config_cannot_be_loaded(cfg_path, watch_env) -> None:
    cfg_path.write_text("{ this is not json")
    assert cli.main(["watch"]) != 0


def test_event_returns_nonzero_when_the_config_cannot_be_loaded(cfg_path, watch_env) -> None:
    # IW-06: upssched-cmd.sh invokes this for onbatt/lowbatt/remote_shutdown, so a
    # config that cannot be loaded used to turn every real NUT power event into a
    # silent no-op that reported SUCCESS.
    cfg_path.write_text("{ this is not json")
    assert cli.main(["onbatt", "cyberpower"]) != 0


# --- BL-C1: a config-authored ssh alias reaching the ssh argv ------------------
#
# T-02-10 hardened the argparse boundary (`--ssh`) and 02-06 hardened the loader for
# a declared-`ssh` record. Neither covers a NATIVE record: `_transport_notices`
# applies the alias rule only under `method == "ssh"`, and `MonitoredMachine.disarmed`
# is False for native by construction — so `monitor verify` and `monitor remove` were
# the only checkpoints, and both were re-validating `ups` while handing `ssh` straight
# to the argv.

_INJECTED_ALIAS = "-oProxyCommand=touch /tmp/pwn"


def test_verify_refuses_an_option_shaped_config_alias_rc2(cfg_path, monkeypatch) -> None:
    _write_config(
        cfg_path,
        machines=[_machine_entry("evil", method="native", ssh=_INJECTED_ALIAS, ip="192.168.1.9")],
    )
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)

    assert cli.main(["monitor", "verify", "evil"]) == 2
    assert ssh.calls == []  # the alias never reached an ssh argv


def test_remove_refuses_to_run_the_remote_disarm_with_an_option_shaped_alias(
    cfg_path, monkeypatch
) -> None:
    _write_config(
        cfg_path,
        machines=[_machine_entry("evil", method="native", ssh=_INJECTED_ALIAS, ip="192.168.1.9")],
    )
    ssh, nft = FakeSSH(), FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)

    assert cli.main(["monitor", "remove", "evil"]) == 2
    assert ssh.calls == []
    # ...and the record is still on disk, so the operator can fix or re-remove it.
    assert [m["name"] for m in json.loads(cfg_path.read_text())["monitored_machines"]] == ["evil"]


def test_remove_with_keep_remote_still_completes_for_a_bad_alias(cfg_path, monkeypatch) -> None:
    # The refusal is scoped to the step that uses the alias. Removing the record is
    # the remedy for a bad alias, so the verb must not become unusable.
    _write_config(
        cfg_path,
        machines=[_machine_entry("evil", method="native", ssh=_INJECTED_ALIAS, ip="192.168.1.9")],
    )
    ssh, nft = FakeSSH(), FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)

    assert cli.main(["monitor", "remove", "evil", "--keep-remote"]) == 0
    assert ssh.calls == []
    assert json.loads(cfg_path.read_text())["monitored_machines"] == []


def test_verify_secondary_rejects_an_option_shaped_alias_at_the_shared_sink() -> None:
    # Belt-and-braces one layer down: verify_secondary documented that it did NOT
    # validate the alias, which made every caller the only checkpoint.
    from ups_orchestrator import nutclient

    calls: list[tuple[str, str, str | None]] = []

    def _record(alias: str, command: str, stdin: str | None = None) -> tuple[int, str, str]:
        calls.append((alias, command, stdin))
        return 0, "OL", ""

    ok, detail = nutclient.verify_secondary(_INJECTED_ALIAS, "cyberpower", "192.168.1.125", _record)

    assert ok is False
    assert "alias" in detail
    assert calls == []


def test_add_refuses_an_ambiguous_name_rc2_and_deletes_nothing(cfg_path, monkeypatch) -> None:
    # BL-C2. `_monitor_find` is first-wins so the transition guard inspected an
    # arbitrary duplicate, while `others` filtered out EVERY match so the persist
    # deleted all of them. The pair below is the dangerous ordering: the FIRST record
    # is the harmless ssh one the guard would see, the SECOND is the live native
    # secondary the persist would delete — with no remote teardown, no nft revoke, and
    # the box then declared a push target while its own upsmon stays armed.
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("spark", method="ssh", ssh="spark", ups=""),
            dict(
                _machine_entry("Spark", method="native", ssh="spark", ip="192.168.1.120"),
                _comment="the LIVE native secondary",
            ),
        ],
    )
    before = cfg_path.read_text()
    ssh, local, nft, _bounce = _no_privileged_seams(monkeypatch)

    rc = cli.main(
        ["monitor", "add", "spark", "--method", "ssh", "--ssh", "spark", "--ups", "cyberpower"]
    )

    assert rc == 2
    assert cfg_path.read_text() == before  # byte-identical: nothing deleted, nothing added
    assert ssh.calls == [] and nft.calls == [] and local.calls == []


def test_add_still_works_for_an_unambiguous_re_enrollment(cfg_path, monkeypatch) -> None:
    # The guard must not fire on the ordinary single-record re-enrolment path.
    _write_config(
        cfg_path, machines=[_machine_entry("mt", method="ssh", ssh="mt", ups="cyberpower")]
    )
    _no_privileged_seams(monkeypatch)

    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-device",
            "/dev/ttyUSB0",
            "--serial-baud",
            "9600",
        ]
    )

    assert rc == 0
    assert _entry(cfg_path, "mt")["shutdown_method"] == "serial"


# --- HI-C2: the nft saddr set is built from validated native IPs only ----------


def test_survivor_saddrs_drops_a_non_literal_ip_and_a_non_native_record(caplog) -> None:
    # `_survivor_saddrs` handed m.ip verbatim to render_nft_accept_rule, which joins
    # the members into `ip saddr { … } accept` and hands the ruleset to `nft -f` AS
    # ROOT. `_valid_ip` guarded only the --ip argparse path, so a hand-edited record
    # could close the brace and append its own accept above the policy drop. Second
    # half: only a native secondary talks to upsd, so a serial/ssh record carrying a
    # stale enrollment ip had no business in the set.
    from ups_orchestrator.config import MonitoredMachine

    machines = (
        MonitoredMachine(name="ok", shutdown_method="native", ip="192.168.1.120"),
        MonitoredMachine(
            name="evil",
            shutdown_method="native",
            ip='1.2.3.4 } accept comment "o"\n        ip saddr 0.0.0.0/0 accept comment "pwn',
        ),
        MonitoredMachine(name="pushy", shutdown_method="ssh", ssh="pushy", ip="192.168.1.121"),
        MonitoredMachine(name="blank", shutdown_method="native", ip=""),
    )

    with caplog.at_level("WARNING"):
        assert cli._survivor_saddrs(machines) == ["192.168.1.120"]
    assert "evil" in caplog.text  # the drop is explained, not silent


def test_render_nft_accept_rule_refuses_a_non_literal_member() -> None:
    # Belt-and-braces at the shared sink: nothing but an IP literal is a legitimate
    # member of a set that root loads.
    from ups_orchestrator import nutclient

    with pytest.raises(ValueError, match="IPv4 literals"):
        nutclient.render_nft_accept_rule(["1.2.3.4 } accept; ip saddr 0.0.0.0/0 accept #"])


def test_add_does_not_grant_an_upsd_accept_to_a_non_native_record(cfg_path, monkeypatch) -> None:
    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="ssh", ssh="mt", ups="cyberpower", ip="192.168.1.114"),
            _machine_entry("spark", method="native", ssh="spark", ip="192.168.1.120"),
        ],
    )
    assert cli._survivor_saddrs(cli._load_config().monitored_machines) == ["192.168.1.120"]


# --- LO-C3: the process tripwire is ARMED, in this file, at every entry point ---


def test_process_tripwire_is_armed_in_this_file(no_real_processes) -> None:
    """`test_monitor.py` drives every enrollment path and had no tripwire at all.

    The old one was a plain helper four `test_cli.py` tests called by hand, so it
    was opt-in rather than armed and it never covered this file — the one that
    exercises the ssh/nft/systemctl/upsc seams. It is autouse now, so it is in
    force here without this file asking for it.
    """
    with pytest.raises(AssertionError, match="escaped the test fakes"):
        subprocess.run(["true"], capture_output=True)
    assert len(no_real_processes) == 1
    no_real_processes.clear()  # the block above IS the assertion, not a leak


def test_process_tripwire_covers_more_than_subprocess_run(no_real_processes) -> None:
    """The old tripwire patched `subprocess.run` alone — four ways around it."""
    spawns = [
        lambda: subprocess.run(["true"], capture_output=True),
        lambda: subprocess.Popen(["true"]),
        lambda: os.system("true"),  # noqa: S605 — patched; never reaches a shell
        lambda: os.execv("/bin/true", ["true"]),
    ]
    for spawn in spawns:
        with pytest.raises(AssertionError, match="escaped the test fakes"):
            spawn()
    assert len(no_real_processes) == len(spawns)
    no_real_processes.clear()


def test_process_tripwire_allows_only_the_named_acl_binaries() -> None:
    """`state._copy_acl` shells out to getfacl/setfacl on every persist (T-02-SC).

    That pair is the ONE documented exception, keyed on the binary name, so the
    allowance cannot be widened by accident into "any local command".
    """
    from conftest import _allowed

    assert _allowed((["getfacl", "--omit-header", "--", "/tmp/x"],))
    assert _allowed((["/usr/bin/setfacl", "--set-file=-", "--", "/tmp/x"],))
    assert not _allowed((["ssh", "mt", "true"],))
    assert not _allowed((["nft", "-f", "/etc/nftables.conf"],))
    assert not _allowed((["systemctl", "restart", "nut-monitor"],))
    assert not _allowed((["upsc", "cyberpower@192.168.1.125"],))
    assert not _allowed(([],))
    assert not _allowed(())


# --- LO-C5: the degrade banner is not a terminal control channel ---------------


def test_list_degrade_banner_neutralises_control_characters_in_a_machine_name(
    cfg_path, capsys
) -> None:
    """A config-authored name must not be able to erase the banner reporting on it.

    Same class as w34 MED-06 (`status.py`): `subject`/`message` carry machine
    names, device paths and ssh aliases verbatim out of the config, and
    `\x1b[2J\x1b[H` clears the screen and homes the cursor. Routed through the same
    `_safe` helper MED-06 introduced, so there is one predicate and not two.
    """
    hostile = "mt\x1b[2J\x1b[H"
    _write_config(
        cfg_path,
        machines=[
            _machine_entry(hostile, method="serial", ups="", device="/dev/ttyUSB0", baud=9600)
        ],
    )
    assert cli.main(["monitor", "list"]) == 0
    out = capsys.readouterr().out

    banner = out[out.index("DEGRADED CONFIG") :]
    assert "\x1b" not in banner  # the escape never reaches the terminal
    assert "mt?[2J?[H" in banner  # ...and the name is still identifiable


# --- LO-C2: the transition guard normalises like every other method comparison -


def test_transition_guard_refuses_an_unnormalised_native_declaration(
    cfg_path, monkeypatch, caplog
) -> None:
    """`from_dict` lower-cases the field; `MonitoredMachine` does not promise it.

    `_monitor_add` builds `MonitoredMachine` directly (twice), so the guard's
    correctness rested on a property of a constructor it does not use. Drive it
    with the Config object itself so the comparison — not the loader — is what is
    under test: an unnormalised `"Native"` must still be refused, because letting
    it through is the exact native->push double-shutdown T-02-23 exists to close.
    """
    from ups_orchestrator.config import Config, MonitoredMachine, UpsConfig

    cfg = Config(
        webhook_url="",
        upses={"cyberpower": UpsConfig(name="cyberpower", label="CyberPower")},
        monitored_machines=(
            MonitoredMachine(
                name="spark",
                ssh="spark",
                ups="cyberpower",
                shutdown_method="Native",  # never normalised by any constructor
                ip="192.168.1.120",
            ),
        ),
    )
    before = cfg_path.read_text()
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)

    with caplog.at_level("ERROR"):
        rc = cli._monitor_add(
            cfg, cfg_path, ["spark", "--method", "ssh", "--ssh", "spark", "--ups", "cyberpower"]
        )

    assert rc == 2
    assert "already enrolled as a NATIVE secondary" in caplog.text
    assert cfg_path.read_text() == before  # the live native record is untouched
    assert ssh.calls == []


# --- LO-C6: --force's help describes the authorisation it actually carries -----


def test_force_help_text_names_only_the_dual_regime_refusal(cfg_path, capsys) -> None:
    """Post-T-02-54 `--force` gates ONE thing; the help still said "guards" (plural).

    The two flags are the phase's only anti-conflation surface an operator reads
    before typing, so a stale plural is what makes someone reach for `--force`
    expecting it to clear the native transition guard too.
    """
    with pytest.raises(SystemExit):
        cli.main(["monitor", "add", "--help"])
    out = capsys.readouterr().out

    assert "dual-regime" in out
    assert "refuse-on-existing guards" not in out
    # ...and the other half is still described as the separate authorisation it is.
    assert "--force-remote-config" in out


# --- LO-C4: --shutdown-cmd rejects a newline, not only a double-quote ----------


def test_add_refuses_a_newline_in_shutdown_cmd(cfg_path, monkeypatch, caplog) -> None:
    """The guard mirrored `render_upsmon_conf` and both rejected only the quote.

    `SHUTDOWNCMD "<cmd>"` is one directive on one line, so the newline ends it and
    `NOTIFYCMD /tmp/x` becomes a further upsmon directive in the SECONDARY's
    /etc/nut/upsmon.conf. Refused at the argparse boundary, before any record is
    written and before anything is sent to that machine.
    """
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    before = cfg_path.read_text()

    with caplog.at_level("ERROR"):
        rc = cli.main(
            [
                "monitor",
                "add",
                "mt",
                "--method",
                "ssh",
                "--ssh",
                "mt",
                "--ups",
                "cyberpower",
                "--shutdown-cmd",
                "sudo /sbin/shutdown -h now\nNOTIFYCMD /tmp/x",
            ]
        )

    assert rc == 2
    assert "control character" in caplog.text
    assert cfg_path.read_text() == before
    assert ssh.calls == []


def test_add_still_refuses_a_double_quote_in_shutdown_cmd(cfg_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--method",
            "ssh",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--shutdown-cmd",
            '/sbin/shutdown -h now"; rm -rf /',
        ]
    )
    assert rc == 2


# --- LO-C1: `monitor add --dry-run` contacts nothing, not just mutates nothing -


def test_add_dry_run_contacts_no_host_and_runs_no_local_probe(
    cfg_path, monkeypatch, capsys
) -> None:
    """The plan used to be printed BELOW two SSH round-trips and a route probe.

    `_resolve_remote_ip` runs `ip -o route get <primary>` and `echo $SSH_CONNECTION`
    ON THE MACHINE, and `_resolve_primary_ip` runs a local `ip -o route get`. Both
    are read-only, so nothing was mutated — but the flag's contract is that an
    operator can rehearse an enrollment against a machine that is not there, and it
    was not being honoured. No `--ip`/`--primary-ip` here, so the old code had to
    ask a host for both.
    """
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    ssh, local, nft = FakeSSH(), FakeLocal(), FakeNft()
    probes: list[list[str]] = []
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local", local)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(
        cli, "_monitor_run_local_probe", lambda argv: (probes.append(list(argv)), (0, "", ""))[1]
    )
    # A loopback-only LISTEN is what forces `_resolve_primary_ip` past its config
    # short-circuit and into the local route probe.
    _write_config(cfg_path, extra={"nut_server": {"listen": ["127.0.0.1"], "port": 3493}})

    rc = cli.main(["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--dry-run"])

    assert rc == 0
    assert ssh.calls == [], "a dry run must not open an ssh connection to the machine"
    assert probes == [], "a dry run must not run a local route probe"
    assert local.calls == [] and nft.calls == []
    out = capsys.readouterr().out
    assert "<unresolved" in out  # ...and it says so rather than inventing a value
    assert _PW not in out
    assert json.loads(cfg_path.read_text())["monitored_machines"] == []


def test_add_dry_run_still_shows_operator_supplied_values(cfg_path, monkeypatch, capsys) -> None:
    """What the operator typed needs no host to learn, so it is still rendered."""
    monkeypatch.setenv(cli._SECRET_ENV, _PW)
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())

    rc = cli.main(
        [
            "monitor",
            "add",
            "mt",
            "--ssh",
            "mt",
            "--ups",
            "cyberpower",
            "--ip",
            "192.168.1.114",
            "--primary-ip",
            "192.168.1.125",
            "--dry-run",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "resolve ip: 192.168.1.114" in out
    assert "LISTEN 192.168.1.125" in out
    assert "'192.168.1.114'" in out  # ...and it reaches the previewed nft saddr set


# --- ME-C3: --serial-device gets the loader's rule at the argparse boundary -----


@pytest.mark.parametrize("device", ["/etc/passwd", "ttyUSB0", "/dev/", "../dev/ttyUSB0"])
def test_add_refuses_a_serial_device_the_loader_would_disarm(cfg_path, device) -> None:
    """The CLI must not accept a value `Config.load` disarms on the next read.

    `--serial-baud` was strictly validated and `--serial-device` was checked only
    for emptiness, so the operator got `recorded x (shutdown_method=serial)` for a
    machine that will never shut down. The serial writer opens the device with mode
    'wb', which TRUNCATES a regular file — which is why the loader has the rule.
    """
    before = cfg_path.read_text()
    rc = cli.main(
        [
            "monitor",
            "add",
            "x",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-device",
            device,
            "--serial-baud",
            "9600",
        ]
    )
    assert rc == 2
    assert cfg_path.read_text() == before


def test_add_serial_device_under_dev_is_still_accepted(cfg_path) -> None:
    rc = cli.main(
        [
            "monitor",
            "add",
            "x",
            "--method",
            "serial",
            "--ups",
            "cyberpower",
            "--serial-device",
            "/dev/serial/by-id/usb-console",
            "--serial-baud",
            "9600",
        ]
    )
    assert rc == 0
    # ...and the record it wrote survives the loader without a disarm — which is the
    # property the CLI check exists to guarantee.
    machine = cli._monitor_find(cli._load_config(), "x")
    assert machine is not None and not machine.disarmed


# --- ME-C1: verify resolves the primary the way `add` does ---------------------


def test_verify_never_probes_the_secondarys_own_localhost(cfg_path, monkeypatch) -> None:
    """`upsc <ups>@127.0.0.1` runs ON THE SECONDARY — it asks the wrong upsd.

    `_monitor_primary_ip` validated nothing and fell back to loopback, so with a
    loopback-only LISTEN and no `--primary-ip` an armed native secondary was told
    `FAIL — Connection refused` (rc 1): an operator reading that concludes a
    protected box is unprotected. It reads as a false OK the other way round if
    that box happens to run its own upsd with a same-named UPS.
    """
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="native", ssh="mt", ip="")],
        extra={"nut_server": {"listen": ["127.0.0.1"], "port": 3493}},
    )
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_local_probe", lambda _argv: (1, "", "no route"))

    rc = cli.main(["monitor", "verify", "mt"])

    assert rc == 2, "an unresolvable primary is a refusal, not a probe of the wrong host"
    assert ssh.calls == []
    assert not any("127.0.0.1" in cmd for cmd in ssh.commands)


def test_verify_uses_the_primary_ip_override_the_way_add_does(cfg_path, monkeypatch) -> None:
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="native", ssh="mt", ip="")],
        extra={"nut_server": {"listen": ["127.0.0.1"], "port": 3493}},
    )
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)

    assert cli.main(["monitor", "verify", "mt", "--primary-ip", "192.168.1.125"]) == 0
    assert "cyberpower@192.168.1.125" in ssh.commands[0]


def test_verify_falls_back_to_the_local_route_probe_like_add(cfg_path, monkeypatch) -> None:
    """The third rung of `_resolve_primary_ip`: route toward the secondary.

    This is what `monitor add` does to learn the primary's own address on the path
    to a machine, and sharing it is the point — the two commands must not disagree
    about which address the MONITOR line was written with.
    """
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="native", ssh="mt", ip="192.168.1.114")],
        extra={"nut_server": {"listen": ["127.0.0.1"], "port": 3493}},
    )
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(
        cli,
        "_monitor_run_local_probe",
        lambda _argv: (0, "192.168.1.114 dev eth0 src 192.168.1.125 uid 0\n", ""),
    )

    assert cli.main(["monitor", "verify", "mt"]) == 0
    assert "cyberpower@192.168.1.125" in ssh.commands[0]


# --- ME-C2: the advisory's own remedy has to be runnable ----------------------


def test_verify_native_with_a_blank_ups_is_rc1_not_rc2(cfg_path, monkeypatch, capsys) -> None:
    """`Config` cannot disarm a native authority, so the advisory says: go verify.

    `valid_nut_name("")` is False, so the command that advisory names answered
    rc 2 — which in every other branch here means "bad input to the command", i.e.
    a script (and an operator) reading it concludes the machine name was typo'd.
    The remedy the phase designed for this exact state was unreachable. A blank
    `ups` is not bad input; it is the state being diagnosed.
    """
    _write_config(cfg_path, machines=[_machine_entry("mt", method="native", ssh="mt", ups="")])
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)

    rc = cli.main(["monitor", "verify", "mt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert ssh.calls == []  # there is no `upsc <ups>@<primary>` to run
    assert "cannot probe" in out
    assert "monitor remove mt" in out  # ...and the real disarm is named
    assert "no active shutdown authority" not in out  # never the reassuring answer


def test_verify_blank_ups_does_not_take_the_metachar_branch(cfg_path, monkeypatch, caplog) -> None:
    """A blank value is not an injection, and must not be reported as one."""
    _write_config(cfg_path, machines=[_machine_entry("mt", method="native", ssh="mt", ups="")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())

    with caplog.at_level("ERROR"):
        assert cli.main(["monitor", "verify", "mt"]) == 1
    assert "is invalid" not in caplog.text


def test_verify_metachar_ups_is_still_rc2_after_the_blank_carve_out(cfg_path, monkeypatch) -> None:
    """The charset check keeps its own rc — the blank branch sits ABOVE it, not
    instead of it."""
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="native", ssh="mt", ups="x; touch /tmp/pwned")],
    )
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)

    assert cli.main(["monitor", "verify", "mt"]) == 2
    assert ssh.calls == []


def test_verify_blank_ssh_alias_still_reaches_the_probe(cfg_path, monkeypatch) -> None:
    """Guard on the ME-C2 fix: a blank ALIAS is a different thing from a blank UPS.

    `_verify_ssh_alias`'s option-shaped refusal is keyed on `machine.ssh.strip()`
    being non-empty on purpose — a blank alias is not an injection, and BL-02's
    advisory probe (`none` carrying a `ups`) is exactly a record with no alias.
    Rejecting it would silence the probe that finds a stray live secondary.
    """
    _write_config(cfg_path, machines=[_machine_entry("spark", method="none", ups="cyberpower")])
    ssh = FakeSSH([(0, "OL\n", "")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)

    assert cli.main(["monitor", "verify", "spark"]) == 1
    assert ssh.calls and ssh.calls[0][0] == ""  # probed over a blank alias, deliberately


# --- ME-C5: the rc an option-shaped alias actually produces -------------------


def test_verify_declared_ssh_with_an_option_shaped_alias_is_rc1_not_rc2(
    cfg_path, monkeypatch, capsys
) -> None:
    """The documented contract said rc 2; the command has always returned rc 1.

    `_transport_notices` disarms a declared-`ssh` record whose alias is
    option-shaped, so `_monitor_verify` returns from its `machine.disarmed` gate
    before `_verify_ssh_alias` is ever called. rc 1 is the better answer — the
    operator is told the machine will not fire, not that they typed something
    wrong — so the CODE is right and the rc table was wrong. Pinned here so the
    two cannot drift again.
    """
    _write_config(
        cfg_path,
        machines=[_machine_entry("mt", method="ssh", ssh="-oProxyCommand=id", ups="cyberpower")],
    )
    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)

    rc = cli.main(["monitor", "verify", "mt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "DISARMED (declared ssh)" in out
    assert ssh.calls == []  # the alias never reaches an ssh argv


def test_verify_ssh_alias_sink_still_refuses_an_option_shaped_alias(monkeypatch, caplog) -> None:
    """The rc 2 branch is defence in depth at the SINK, so it is tested at the sink.

    `_verify_ssh_alias` puts the alias into a real `ssh` argv. Today nothing gets
    past the loader to reach it — which is exactly why `grep` found no test and
    the branch was reported as dead code. Deleting it would leave the sink relying
    entirely on `_transport_notices` staying in step with it; testing it directly
    is what makes that coupling safe to lose.
    """
    from ups_orchestrator.config import MonitoredMachine

    ssh = FakeSSH()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    hostile = MonitoredMachine(
        name="mt", ssh="-oProxyCommand=touch /tmp/pwn", shutdown_method="ssh"
    )

    with caplog.at_level("ERROR"):
        assert cli._verify_ssh_alias(hostile) == 2
    assert ssh.calls == []
    assert "is invalid" in caplog.text


def test_verify_ssh_alias_sink_accepts_a_plain_alias(monkeypatch) -> None:
    from ups_orchestrator.config import MonitoredMachine

    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH([(0, "", "")]))
    assert cli._verify_ssh_alias(MonitoredMachine(name="mt", ssh="mt", shutdown_method="ssh")) == 0


# --- ME-C4: `monitor add --method ssh/serial/none` revokes the ip it sheds ------


def test_add_record_only_revokes_a_stale_saddr_the_record_no_longer_owns(
    cfg_path, monkeypatch
) -> None:
    """The other half of ME-C4's "no command in the family revokes a shed ip".

    The IW-05 shape: a former native secondary hand-edited to a push method but
    still carrying its enrollment `ip`, whose accept is still in the managed set.
    `_survivor_saddrs`'s native filter (HI-C2) already excludes it from the
    computed set — but the record-only branch returned before ever reaching an nft
    step, so nothing recomputed the file and the accept survived indefinitely.
    """
    from ups_orchestrator import nutclient

    # Seed the managed set the way this machine's native enrollment left it.
    seeded, _changed = nutclient.upsert_nft_input_chain(POLICY_DROP_RULESET, ["192.168.1.114"])
    Path(cli._NFT_PATH).write_text(seeded)
    assert "192.168.1.114" in Path(cli._NFT_PATH).read_text()

    _write_config(
        cfg_path,
        machines=[
            _machine_entry("mt", method="ssh", ssh="mt", ups="cyberpower", ip="192.168.1.114")
        ],
    )
    ssh, nft = FakeSSH(), FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", ssh)
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)

    rc = cli.main(["monitor", "add", "mt", "--method", "ssh", "--ssh", "mt", "--ups", "cyberpower"])

    assert rc == 0
    assert ssh.calls == [], "a record-only add must not contact any host"
    assert nft.calls, "the local, idempotent saddr rewrite must run"
    assert "192.168.1.114" not in Path(cli._NFT_PATH).read_text()


def test_add_record_only_opens_nothing_for_the_machine_it_records(cfg_path, monkeypatch) -> None:
    """The rewrite must never become an nft OPENING for a non-native record.

    T-02-11/P2-02: a machine with no NUT enrollment of its own gets no upsd.users,
    no LISTEN, no accept. `_survivor_saddrs`'s native filter is what guarantees it,
    and this pins that the new nft call cannot smuggle one in.
    """
    _write_config(cfg_path, machines=[])
    nft = FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)
    monkeypatch.setattr(cli, "_monitor_restart_bouncer", lambda: None)

    rc = cli.main(
        [
            "monitor",
            "add",
            "box",
            "--method",
            "ssh",
            "--ssh",
            "box",
            "--ups",
            "cyberpower",
            "--ip",
            "192.168.1.130",
        ]
    )

    assert rc == 0
    text = Path(cli._NFT_PATH).read_text()
    assert "192.168.1.130" not in text
    assert "3493" not in text  # no upsd accept was rendered at all
    # ...and `ip` is still written only by the native enrollment path.
    assert _entry(cfg_path, "box")["ip"] == ""


def test_add_record_only_persists_even_when_the_nft_sweep_fails(cfg_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    monkeypatch.setattr(cli, "_NFT_PATH", "/nonexistent/main.nft")  # apply_nft -> rc 2

    rc = cli.main(["monitor", "add", "box", "--method", "none"])

    assert rc == 0
    assert _entry(cfg_path, "box")["shutdown_method"] == "none"


def test_add_record_only_no_firewall_skips_the_sweep(cfg_path, monkeypatch) -> None:
    nft = FakeNft()
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH())
    monkeypatch.setattr(cli, "_monitor_run_nft", nft)

    rc = cli.main(["monitor", "add", "box", "--method", "none", "--no-firewall"])

    assert rc == 0 and nft.calls == []


# --- F5: every config-authored surface is sanitised, not just the banner -------
#
# MED-06 fixed `status.py`; LO-C5 then had to fix the degrade banner in `cli.py`;
# and `monitor list` was still printing the same raw machine name FOUR LINES ABOVE
# the sanitised copy. Three rounds of one defect, because the rule lived at call
# sites. It lives at the render boundary now (`cli._say`, `ConfigNotice.__str__`).

_HOSTILE_NAME = "mt\x1b[2J\x1b[H\x07\x00"


def _assert_inert(text: str, label: str) -> None:
    for ch, name in ((("\x1b"), "ESC"), ("\x07", "BEL"), ("\x00", "NUL")):
        assert ch not in text, f"{label} still emits a raw {name}"


def test_monitor_list_machine_line_is_sanitised_not_just_the_banner(cfg_path, capsys) -> None:
    """The measured defect: the raw name four lines above the sanitised one."""
    _write_config(
        cfg_path,
        machines=[
            _machine_entry(_HOSTILE_NAME, method="serial", ups="", device="/dev/x", baud=9600)
        ],
    )
    assert cli.main(["monitor", "list"]) == 0
    _assert_inert(capsys.readouterr().out, "monitor list")


def test_monitor_verify_output_is_sanitised(cfg_path, monkeypatch, capsys) -> None:
    _write_config(
        cfg_path,
        machines=[_machine_entry(_HOSTILE_NAME, method="native", ssh="mt", ups="cyberpower")],
    )
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH([(0, "OL\n", "")]))
    cli.main(["monitor", "verify", _HOSTILE_NAME])
    _assert_inert(capsys.readouterr().out, "monitor verify")


def test_config_notice_str_is_sanitised_so_the_journal_is_too(caplog, cfg_path) -> None:
    """`logger.error("config degrade: %s", notice)` renders through `__str__`.

    `journalctl` pages the journal through a terminal, so a control sequence in a
    log line is the same defect wearing a different hat.
    """
    _write_config(
        cfg_path,
        machines=[
            _machine_entry(_HOSTILE_NAME, method="serial", ups="", device="/dev/x", baud=9600)
        ],
    )
    with caplog.at_level("WARNING"):
        assert cli.main(["monitor", "list"]) == 0
    _assert_inert(caplog.text, "the journal")


def test_config_notice_fields_are_left_raw(cfg_path) -> None:
    """INV-DEGRADE: a notice is a VALUE. Sanitising is a rendering, not a mutation.

    `__str__` is sanitised so every logger call is; the fields themselves keep
    exactly what the operator wrote, so anything that needs the real name has it.
    """
    from ups_orchestrator.config import ConfigNotice

    n = ConfigNotice(severity="error", subject=_HOSTILE_NAME, message="x\x1by")
    assert n.subject == _HOSTILE_NAME
    assert n.message == "x\x1by"
    _assert_inert(str(n), "ConfigNotice.__str__")


def test_safe_text_leaves_newline_and_tab_alone() -> None:
    """They are legitimate layout in a multi-line notice and move no cursor."""
    from ups_orchestrator.config import safe_text

    assert safe_text("a\nb\tc") == "a\nb\tc"
    assert safe_text("a\x1bb") == "a?b"
    assert safe_text("a\x00b\x07c\x7fd") == "a?b?c?d"


# --- F2: the CLI never lets a v6 address reach the IPv4 saddr set --------------


def test_add_refuses_an_ipv6_ip(cfg_path, caplog) -> None:
    with caplog.at_level("ERROR"):
        rc = cli.main(
            ["monitor", "add", "mt", "--ssh", "mt", "--ups", "cyberpower", "--ip", "2001:db8::1"]
        )
    assert rc == 2
    assert "IPv4" in caplog.text


def test_survivor_saddrs_drops_an_ipv6_record_ip(caplog) -> None:
    from ups_orchestrator.config import MonitoredMachine

    machines = (
        MonitoredMachine(name="v6", shutdown_method="native", ip="2001:db8::1"),
        MonitoredMachine(name="v4", shutdown_method="native", ip="192.168.1.120"),
    )
    with caplog.at_level("WARNING"):
        assert cli._survivor_saddrs(machines) == ["192.168.1.120"]
    assert "v6" in caplog.text and "IPv4" in caplog.text


def test_resolve_remote_ip_rejects_a_v6_ssh_connection_fallback(monkeypatch) -> None:
    """A v6-reachable secondary's $SSH_CONNECTION field 1 is a v6 literal.

    Accepted, it was written to the record's `ip` and then rendered into the
    IPv4-only saddr set.
    """
    monkeypatch.setattr(
        cli, "_monitor_run_ssh", lambda _a, _c, _s: (0, "2001:db8::1 4242 2001:db8::2 22\n", "")
    )
    assert cli._resolve_remote_ip("mt", None, "") is None


def test_resolve_remote_ip_rejects_a_v6_route_src(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_monitor_run_ssh",
        lambda _a, cmd, _s: (
            (0, "2001:db8::2 dev eth0 src 2001:db8::1 uid 0\n", "")
            if "route get" in cmd
            else (1, "", "")
        ),
    )
    assert cli._resolve_remote_ip("mt", None, "192.168.1.125") is None
