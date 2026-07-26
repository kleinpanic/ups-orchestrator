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
    rc = cli.main(
        ["monitor", "add", "mt", "--method", "ssh", "--ssh", "mt", "--ups", "cyberpower"]
    )
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
    cfg_path, monkeypatch
) -> None:
    _write_config(cfg_path, machines=[_machine_entry("spark", method="none", ups="cyberpower")])
    monkeypatch.setattr(cli, "_monitor_run_ssh", FakeSSH([(1, "", "connection refused")]))
    assert cli.main(["monitor", "verify", "spark"]) == 0


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


def test_remove_non_native_skips_the_nut_disarm_and_the_nft_rewrite(
    cfg_path, monkeypatch
) -> None:
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
    assert ssh.calls == [] and nft.calls == []
    data = json.loads(cfg_path.read_text())
    assert [m["name"] for m in data["monitored_machines"]] == ["mt"]
    # The native survivor is untouched, declaration included.
    assert _entry(cfg_path, "mt")["shutdown_method"] == "native"


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
        machines=[
            _machine_entry("evil", method="native", ssh=_INJECTED_ALIAS, ip="192.168.1.9")
        ],
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
        machines=[
            _machine_entry("evil", method="native", ssh=_INJECTED_ALIAS, ip="192.168.1.9")
        ],
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
        machines=[
            _machine_entry("evil", method="native", ssh=_INJECTED_ALIAS, ip="192.168.1.9")
        ],
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
