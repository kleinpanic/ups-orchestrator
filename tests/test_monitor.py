"""Unit tests for the `monitor` CLI family (add/list/verify/remove).

Every side effect runs through a recording fake (run_ssh/run_nft/run_local) and a
tmp config.json, so nothing here touches a live host, the network, /etc,
systemctl, nft, or a real SSH connection. The password is only ever supplied via
the environment and must never appear on any recorded argv.
"""

from __future__ import annotations

import json
import os
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
    src_dir = Path(ups_orchestrator.__file__).parent
    hits = {
        str(path.relative_to(src_dir))
        for path in sorted(src_dir.rglob("*.py"))
        if "NamedTemporaryFile" in path.read_text()
    }
    assert hits == {"cli.py", "state.py"}, (
        "NamedTemporaryFile appeared outside cli.py/_monitor_persist and "
        "state.py/StateStore.save (T-02-23, 02-09-PLAN.md) — a new temp-file "
        "write must go through state.replace_preserving_metadata instead of "
        "reimplementing the unprotected temp+replace idiom"
    )
