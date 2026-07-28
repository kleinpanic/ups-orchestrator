"""Unit tests for the injectable, stdlib-only NUT secondary enrollment engine.

Every side effect is exercised through a fake runner that records what it was
handed and returns canned tuples, so nothing here touches a live host, the
network, /etc, systemctl, or nft.
"""

from __future__ import annotations

import pytest

from ups_orchestrator import nutclient

# A realistic policy-drop input base chain, mirroring the live box: the operator
# owns the `input` hook at priority 0 with `policy drop`, and a ct
# established/related fast-path near the top. The upsd accept must land INSIDE
# this chain (after the ct accept) — a self-contained negative-priority table
# would be traversed but its accept could not override this chain's drop.
POLICY_DROP_RULESET = """\
table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iif "lo" accept
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
    }
    chain output {
        type filter hook output priority filter; policy accept;
    }
}
"""


class FakeSSH:
    """Records (alias, command, stdin) calls; returns queued canned tuples.

    The 3-arg shape is load-bearing: the secret-safe remote write pipes the
    password-bearing config on stdin, never on argv, so the recorded stdin lets
    a test assert the secret never reached the command string.
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


# --- _classify_os (pure) ------------------------------------------------------


def test_classify_os_arch():
    assert nutclient._classify_os("Linux", has_pacman=True, has_apt=False) == "arch"


def test_classify_os_arch_wins_when_both_present():
    # pacman is checked first; a box with both managers is treated as arch.
    assert nutclient._classify_os("Linux", has_pacman=True, has_apt=True) == "arch"


def test_classify_os_ubuntu():
    assert nutclient._classify_os("Linux", has_pacman=False, has_apt=True) == "ubuntu"


def test_classify_os_debian_label_via_override_is_ubuntu_default():
    # apt-present, pacman-absent defaults to the Debian-family label "ubuntu";
    # the caller may override to "debian" downstream.
    assert nutclient._classify_os("Linux", has_pacman=False, has_apt=True) == "ubuntu"


def test_classify_os_unknown():
    assert nutclient._classify_os("Linux", has_pacman=False, has_apt=False) == "unknown"


# --- detect_os (impure only in the run_ssh call) ------------------------------


def test_detect_os_arch():
    ssh = FakeSSH([(0, "Linux\n/usr/bin/pacman\n", "")])
    assert nutclient.detect_os("mt", ssh) == "arch"
    # One probe only, no stdin passed.
    assert len(ssh.calls) == 1
    assert ssh.calls[0][2] is None


def test_detect_os_ubuntu():
    ssh = FakeSSH([(0, "Linux\n\n/usr/bin/apt-get\n", "")])
    assert nutclient.detect_os("spark", ssh) == "ubuntu"


# --- install_nut_client -------------------------------------------------------


def test_install_arch_command():
    ssh = FakeSSH()
    nutclient.install_nut_client("mt", "arch", ssh)
    cmd = ssh.commands[0]
    assert "pacman -Qq nut" in cmd
    assert "pacman -S --needed --noconfirm nut" in cmd


def test_install_ubuntu_command():
    ssh = FakeSSH()
    nutclient.install_nut_client("spark", "ubuntu", ssh)
    cmd = ssh.commands[0]
    assert "dpkg-query -W" in cmd
    assert "apt-get install -y nut-client" in cmd


def test_install_never_touches_server_or_driver():
    for os_kind, alias in (("arch", "mt"), ("ubuntu", "spark"), ("debian", "spark")):
        ssh = FakeSSH()
        nutclient.install_nut_client(alias, os_kind, ssh)
        for cmd in ssh.commands:
            assert "nut-server" not in cmd
            assert "nut-driver" not in cmd


# --- enable_nut_monitor -------------------------------------------------------


def test_enable_targets_only_nut_monitor():
    ssh = FakeSSH()
    nutclient.enable_nut_monitor("mt", ssh)
    joined = " ".join(ssh.commands)
    assert "nut-monitor.service" in joined
    assert "nut-server" not in joined
    assert "nut-driver" not in joined


# --- render_nut_conf (pure) ---------------------------------------------------


def test_render_nut_conf_is_exactly_netclient():
    assert nutclient.render_nut_conf() == "MODE=netclient\n"


# --- render_upsmon_conf (pure) ------------------------------------------------


def _upsmon(**overrides):
    kwargs = {
        "ups": "cyberpower",
        "primary": "192.168.1.125",
        "user": "upsmon_secondary",
        "pw": "s3cr3t",
        "shutdown_cmd": "/sbin/shutdown -h now",
    }
    kwargs.update(overrides)
    return nutclient.render_upsmon_conf(**kwargs)


def test_upsmon_monitor_line_is_secondary_with_powervalue_one():
    text = _upsmon()
    assert "MONITOR cyberpower@192.168.1.125 1 upsmon_secondary s3cr3t secondary" in text


def test_upsmon_is_wrapped_in_marker_block():
    text = _upsmon()
    assert "# BEGIN ups-orchestrator MANAGED" in text
    assert "# END ups-orchestrator MANAGED" in text


def test_upsmon_deadtime_defaults_to_30():
    assert "DEADTIME 30" in _upsmon()
    assert "DEADTIME 20" not in _upsmon()


def test_upsmon_carries_shutdowncmd():
    assert 'SHUTDOWNCMD "/sbin/shutdown -h now"' in _upsmon()


def test_upsmon_substitutes_upssched_path():
    text = _upsmon(upssched_path="/sbin/upssched")
    assert "NOTIFYCMD /sbin/upssched" in text


def test_upsmon_has_all_notifyflags():
    text = _upsmon()
    for flag in ("ONBATT", "LOWBATT", "FSD", "COMMBAD", "SHUTDOWN", "NOCOMM"):
        assert f"NOTIFYFLAG {flag}" in text
        assert "SYSLOG+EXEC" in text


def test_upsmon_omits_killpower_directives():
    # Correction #2: a secondary never powers off a UPS it doesn't own. The
    # rendered config must carry none of the primary-only killpower tokens.
    # (Forbidden literals are built here so the module never contains them.)
    text = _upsmon().lower()
    forbidden = ["power" + "downflag", "upsdrvctl", "upsmon -k", "/etc/kill" + "power"]
    for token in forbidden:
        assert token not in text


def test_upsmon_rejects_quote_in_shutdown_cmd():
    # A double-quote inside SHUTDOWNCMD "<cmd>" would break the NUT parser or
    # inject a second directive; reject rather than escape.
    with pytest.raises(ValueError):
        _upsmon(shutdown_cmd='/sbin/shutdown -h now"; rm -rf /')


def test_upsmon_rejects_a_newline_in_shutdown_cmd():
    """LO-C4: the quote was rejected and the NEWLINE was not.

    `SHUTDOWNCMD "<cmd>"` is one directive on one line, so a newline ends it and
    everything after becomes a further upsmon directive in the SECONDARY's
    /etc/nut/upsmon.conf — a third machine's config, written by us.
    """
    with pytest.raises(ValueError, match="control character"):
        _upsmon(shutdown_cmd="sudo /sbin/shutdown -h now\nNOTIFYCMD /tmp/x")


def test_valid_shutdown_cmd_is_the_one_predicate_for_both_sinks():
    assert nutclient.valid_shutdown_cmd("sudo /sbin/shutdown -h now")
    assert not nutclient.valid_shutdown_cmd('halt"')
    assert not nutclient.valid_shutdown_cmd("halt\nNOTIFYCMD /tmp/x")
    assert not nutclient.valid_shutdown_cmd("halt\rNOTIFYCMD /tmp/x")
    assert not nutclient.valid_shutdown_cmd("halt\x00")


# --- render_upsd_* snippets (pure) --------------------------------------------


# --- render_nft_accept_rule / upsert_nft_input_chain (pure) -------------------
#
# LIVE BUG #1 regression: the accept must be spliced INTO the operator's
# policy-drop input base chain (where nftables honours it), NOT into a
# self-contained negative-priority table whose accept the drop chain overrides.


def test_accept_rule_is_a_marked_chain_line_not_a_table():
    rule = nutclient.render_nft_accept_rule(["192.168.1.40", "192.168.1.50"])
    assert "tcp dport 3493 ip saddr { 192.168.1.40, 192.168.1.50 } accept" in rule
    assert "# BEGIN UPS-ORCHESTRATOR MANAGED" in rule
    # No dedicated table / hooked chain of our own — the old broken form.
    assert "table inet ups_orchestrator" not in rule
    assert "hook input" not in rule


def test_accept_rule_empty_saddrs_is_empty():
    assert nutclient.render_nft_accept_rule([]) == ""


def test_upsert_lands_inside_policy_drop_chain_after_ct_accept():
    """The accept is honoured only if it sits in the chain owning the input hook.

    Pins that the marked accept lands (a) inside the `policy drop` base chain's
    braces, (b) after its ct established/related accept, and (c) before the chain
    closes — i.e. in the evaluation path where a policy-drop firewall accepts it.
    """
    text, changed = nutclient.upsert_nft_input_chain(POLICY_DROP_RULESET, ["192.168.1.114"])
    assert changed is True
    rule_line = "tcp dport 3493 ip saddr { 192.168.1.114 } accept"
    assert rule_line in text

    # It falls between this chain's ct-accept and the chain's closing brace, i.e.
    # inside the policy-drop chain's evaluation path.
    chain_open = text.index("type filter hook input priority filter; policy drop;")
    ct_pos = text.index("ct state established,related accept", chain_open)
    rule_pos = text.index(rule_line)
    chain_close = text.index("chain forward")  # next chain marks our chain's end
    assert ct_pos < rule_pos < chain_close
    # And it is indented as a chain rule (inside the braces), not column 0.
    line = next(ln for ln in text.splitlines() if rule_line in ln)
    assert line.startswith("        ")


def test_upsert_no_input_hook_chain_raises():
    # A ruleset with no `hook input` base chain has nowhere the accept would be
    # honoured — refuse rather than write a silently-useless rule.
    with pytest.raises(ValueError, match="hook input"):
        nutclient.upsert_nft_input_chain("table inet filter {\n}\n", ["192.168.1.40"])


def test_upsert_is_idempotent():
    once, _ = nutclient.upsert_nft_input_chain(POLICY_DROP_RULESET, ["192.168.1.40"])
    twice, changed = nutclient.upsert_nft_input_chain(once, ["192.168.1.40"])
    assert changed is False
    assert twice == once


def test_upsert_rewrites_set_on_change():
    once, _ = nutclient.upsert_nft_input_chain(POLICY_DROP_RULESET, ["192.168.1.40"])
    twice, changed = nutclient.upsert_nft_input_chain(once, ["192.168.1.40", "192.168.1.50"])
    assert changed is True
    assert "192.168.1.50" in twice
    # Only one MANAGED block — the old rule was replaced in place, not appended.
    assert twice.count("# BEGIN UPS-ORCHESTRATOR MANAGED") == 1


def test_upsert_empty_saddrs_drops_rule():
    once, _ = nutclient.upsert_nft_input_chain(POLICY_DROP_RULESET, ["192.168.1.40"])
    dropped, changed = nutclient.upsert_nft_input_chain(once, [])
    assert changed is True
    assert "192.168.1.40" not in dropped
    assert "# BEGIN UPS-ORCHESTRATOR MANAGED" not in dropped
    # Stripping the marked rule restores the original ruleset byte-for-byte.
    assert dropped == POLICY_DROP_RULESET


def test_upsert_falls_back_to_after_brace_without_ct_accept():
    # A chain with no ct established/related line: the accept goes right after the
    # chain's opening brace (still inside the policy-drop chain).
    ruleset = (
        "table inet filter {\n"
        "    chain input {\n"
        "        type filter hook input priority filter; policy drop;\n"
        '        iif "lo" accept\n'
        "    }\n"
        "}\n"
    )
    text, changed = nutclient.upsert_nft_input_chain(ruleset, ["192.168.1.40"])
    assert changed is True
    hook_pos = text.index("hook input")
    rule_pos = text.index("tcp dport 3493")
    lo_pos = text.index('iif "lo" accept')
    assert hook_pos < rule_pos < lo_pos


# --- remote_config_guard (pure) ----------------------------------------------


def test_guard_allows_clean_files():
    allowed, _ = nutclient.remote_config_guard("", "MODE=netclient\n")
    assert allowed is True


def test_guard_allows_marker_only_monitor():
    upsmon = nutclient.render_upsmon_conf(
        ups="cyberpower",
        primary="192.168.1.125",
        user="upsmon_secondary",
        pw="pw",
        shutdown_cmd="/sbin/shutdown -h now",
    )
    allowed, _ = nutclient.remote_config_guard(upsmon, "MODE=netclient\n")
    assert allowed is True


def test_guard_refuses_non_marker_monitor_line():
    allowed, reason = nutclient.remote_config_guard(
        "MONITOR myups@localhost 1 admin pw primary\n", "MODE=netclient\n"
    )
    assert allowed is False
    assert reason


def test_guard_refuses_mode_standalone_unless_force():
    allowed, _ = nutclient.remote_config_guard("", "MODE=standalone\n")
    assert allowed is False
    allowed_forced, _ = nutclient.remote_config_guard("", "MODE=standalone\n", force=True)
    assert allowed_forced is True


def test_guard_refuses_mode_netserver():
    allowed, _ = nutclient.remote_config_guard("", "MODE=netserver\n")
    assert allowed is False


# --- write_remote_nut_config (impure, stdin-only secret write) ----------------


def test_write_remote_pipes_secret_on_stdin_only():
    ssh = FakeSSH([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", "")])
    upsmon_text = "MONITOR cyberpower@p 1 u SUPERSECRET secondary\n"
    rc, _msg = nutclient.write_remote_nut_config(
        "mt",
        upsmon_text,
        "MODE=netclient\n",
        "/etc/nut/upsmon.conf",
        "/etc/nut/nut.conf",
        ssh,
    )
    assert rc == 0
    # The password never appears in any command argv...
    for _alias, cmd, _stdin in ssh.calls:
        assert "SUPERSECRET" not in cmd
    # ...but was handed to the writer on stdin.
    assert any(stdin == upsmon_text for _a, _c, stdin in ssh.calls)
    # The write goes through install ... /dev/stdin.
    assert any("install -m 0640 -o root -g nut /dev/stdin" in cmd for _a, cmd, _s in ssh.calls)


def test_write_remote_refuses_when_guard_refuses():
    # Existing upsmon has a non-marker MONITOR line -> guard refuses -> no write.
    existing = "MONITOR other@host 1 admin pw primary\n"
    ssh = FakeSSH([(0, existing, ""), (0, "MODE=netclient\n", "")])
    rc, msg = nutclient.write_remote_nut_config(
        "mt",
        "MONITOR x@y 1 u pw secondary\n",
        "MODE=netclient\n",
        "/etc/nut/upsmon.conf",
        "/etc/nut/nut.conf",
        ssh,
    )
    assert rc != 0
    assert msg
    # Only the two read probes ran; no install write was issued.
    assert not any("install" in cmd for _a, cmd, _s in ssh.calls)


# --- verify_secondary ---------------------------------------------------------


def test_verify_ok_on_online_status():
    ssh = FakeSSH([(0, "OL", "")])
    ok, token = nutclient.verify_secondary("mt", "cyberpower", "192.168.1.125", ssh)
    assert ok is True
    assert "OL" in token


def test_verify_fail_on_error():
    ssh = FakeSSH([(1, "", "Connection refused")])
    ok, _ = nutclient.verify_secondary("mt", "cyberpower", "192.168.1.125", ssh)
    assert ok is False


def test_verify_fail_on_unrecognized_status_despite_rc_zero():
    # rc 0 but the output carries no OL/OB/LB token — a bare success exit does
    # not prove upsd returned a real status, so verify must still fail.
    ssh = FakeSSH([(0, "Unknown UPS: cyberpower", "")])
    ok, _ = nutclient.verify_secondary("mt", "cyberpower", "192.168.1.125", ssh)
    assert ok is False


def test_verify_threads_timeout_into_remote_command():
    # WR-03: --timeout must reach the remote invocation, not be silently ignored.
    ssh = FakeSSH([(0, "OL", "")])
    ok, _ = nutclient.verify_secondary("mt", "cyberpower", "192.168.1.125", ssh, timeout=42)
    assert ok is True
    assert "timeout 42 upsc cyberpower@192.168.1.125" in ssh.commands[0]


def test_verify_rejects_nonpositive_timeout():
    ssh = FakeSSH()
    ok, reason = nutclient.verify_secondary("mt", "cyberpower", "192.168.1.125", ssh, timeout=0)
    assert ok is False
    assert "timeout must be positive" in reason
    assert ssh.calls == []


def test_verify_rejects_ups_name_metachar_without_running():
    # A --ups / config value carrying a shell metachar must be refused at the
    # sink BEFORE it is interpolated into the remote `upsc` command.
    ssh = FakeSSH()
    ok, reason = nutclient.verify_secondary("mt", "x; touch /tmp/pwned", "192.168.1.125", ssh)
    assert ok is False
    assert "invalid UPS name" in reason
    assert ssh.calls == []  # nothing was executed


def test_verify_rejects_bad_primary_ip_without_running():
    ssh = FakeSSH()
    ok, reason = nutclient.verify_secondary("mt", "cyberpower", "$(id)", ssh)
    assert ok is False
    assert "invalid primary IP" in reason
    assert ssh.calls == []


def test_valid_nut_name_accepts_charset_rejects_metachars():
    assert nutclient.valid_nut_name("cyberpower_1.2-3") is True
    for bad in ("x; rm -rf /", "a b", "`id`", "$(id)", "a@b", ""):
        assert nutclient.valid_nut_name(bad) is False


def test_verify_deep_catches_auth_failure():
    # Shallow upsc read succeeds, but the journal shows a login failure -> deep
    # verify must fail (an unauthenticated status read does not prove the
    # password matched).
    ssh = FakeSSH([(0, "OL", ""), (0, "upsmon: Login failure on cyberpower", "")])
    ok, reason = nutclient.verify_secondary("mt", "cyberpower", "192.168.1.125", ssh, deep=True)
    assert ok is False
    assert reason


# --- upsert_upsd_listen (pure) -----------------------------------------------


def test_upsd_listen_adds_lan_line_in_marker_block():
    text, changed = nutclient.upsert_upsd_listen("LISTEN 127.0.0.1 3493\n", "192.168.1.125")
    assert changed is True
    assert "LISTEN 192.168.1.125 3493" in text
    assert "# BEGIN ups-orchestrator MANAGED" in text
    assert "# END ups-orchestrator MANAGED" in text
    # The operator's existing localhost line is preserved, not rewritten away.
    assert "LISTEN 127.0.0.1 3493" in text


def test_upsd_listen_is_idempotent_on_repeat():
    once, _ = nutclient.upsert_upsd_listen("LISTEN 127.0.0.1 3493\n", "192.168.1.125")
    twice, changed = nutclient.upsert_upsd_listen(once, "192.168.1.125")
    assert changed is False
    assert twice == once


def test_upsd_listen_respects_custom_port():
    text, changed = nutclient.upsert_upsd_listen("", "192.168.1.125", port=3494)
    assert changed is True
    assert "LISTEN 192.168.1.125 3494" in text


def _active(text: str) -> list[str]:
    lines = (raw.strip() for raw in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_upsd_listen_keeps_loopback_when_file_has_no_active_listen():
    # THE REGRESSION. Debian ships upsd.conf with every LISTEN commented out,
    # and upsd reads "no LISTEN at all" as "listen on localhost". Writing only
    # the LAN line replaced that implicit default: bare `upsc` was refused for
    # two days, and a boot before eth0 had a DHCP lease left upsd with no
    # bindable address at all, so it exited and systemd gave up on nut-server.
    conf = "# LISTEN 127.0.0.1 3493\n# LISTEN ::1 3493\n"
    text, changed = nutclient.upsert_upsd_listen(conf, "192.168.1.125")
    assert changed is True
    assert "LISTEN 127.0.0.1 3493" in _active(text)
    assert "LISTEN 192.168.1.125 3493" in _active(text)


def test_upsd_listen_adds_loopback_to_a_lan_only_managed_block():
    # Repairs a host already damaged by the old behaviour: the LAN line is
    # present inside the MANAGED block, loopback is missing, and re-running
    # must add loopback WITHOUT dropping the LAN line while restripping.
    damaged = f"{nutclient._UPSMON_BEGIN}\nLISTEN 192.168.1.125 3493\n{nutclient._UPSMON_END}\n"
    text, changed = nutclient.upsert_upsd_listen(damaged, "192.168.1.125")
    assert changed is True
    assert _active(text) == ["LISTEN 127.0.0.1 3493", "LISTEN 192.168.1.125 3493"]


def test_upsd_listen_does_not_duplicate_an_operators_own_loopback_line():
    conf = "LISTEN 127.0.0.1 3493\n"
    text, _ = nutclient.upsert_upsd_listen(conf, "192.168.1.125")
    assert _active(text).count("LISTEN 127.0.0.1 3493") == 1


# --- upsert_upsd_users (pure, rotation-aware) --------------------------------


def test_upsd_users_writes_real_password_in_marker_block():
    text, changed = nutclient.upsert_upsd_users("", "upsmon_secondary", "s3cr3t")
    assert changed is True
    assert "[upsmon_secondary]" in text
    assert "password = s3cr3t" in text
    assert "upsmon secondary" in text
    assert "# BEGIN ups-orchestrator MANAGED" in text
    assert "# END ups-orchestrator MANAGED" in text


def test_upsd_users_idempotent_on_same_password():
    once, _ = nutclient.upsert_upsd_users("", "upsmon_secondary", "s3cr3t")
    twice, changed = nutclient.upsert_upsd_users(once, "upsmon_secondary", "s3cr3t")
    assert changed is False
    assert twice == once


def test_upsd_users_rotates_on_different_password():
    once, _ = nutclient.upsert_upsd_users("", "upsmon_secondary", "old-pw")
    twice, changed = nutclient.upsert_upsd_users(once, "upsmon_secondary", "new-pw")
    assert changed is True
    assert "password = new-pw" in twice
    assert "old-pw" not in twice
    # Only one MANAGED block — the old one was replaced, not appended.
    assert twice.count("# BEGIN ups-orchestrator MANAGED") == 1


def test_upsd_users_overwrites_change_me_placeholder():
    # A pre-existing MANAGED block carrying the committed CHANGE_ME placeholder
    # must be replaced with the real secret — the placeholder is never sticky.
    placeholder, _ = nutclient.upsert_upsd_users("", "upsmon_secondary", "CHANGE_ME")
    real, changed = nutclient.upsert_upsd_users(placeholder, "upsmon_secondary", "s3cr3t")
    assert changed is True
    assert "password = s3cr3t" in real
    assert "CHANGE_ME" not in real


# --- apply_nft (impure orchestration) ----------------------------------------


def test_apply_nft_restarts_bouncer_after_reload(tmp_path):
    conf = tmp_path / "main.nft"
    conf.write_text(POLICY_DROP_RULESET)
    order: list[str] = []

    def run_nft(path: str) -> tuple[int, str, str]:
        order.append(f"nft:{path}")
        return 0, "", ""

    def restart_bouncer() -> None:
        order.append("restart")

    rc, _out, _err = nutclient.apply_nft(str(conf), ["192.168.1.40"], run_nft, restart_bouncer)
    assert rc == 0
    assert order == [f"nft:{conf}", "restart"]
    # The accept landed inside the policy-drop chain, not a table of our own.
    written = conf.read_text()
    assert "tcp dport 3493 ip saddr { 192.168.1.40 } accept" in written
    assert "table inet ups_orchestrator" not in written


def test_apply_nft_reload_path_reloads_toplevel_not_fragment(tmp_path):
    # The base chain lives in an included fragment; `nft -f` must reload the
    # top-level file that pulls it in, so the whole ruleset is reloaded.
    conf = tmp_path / "main.nft"
    conf.write_text(POLICY_DROP_RULESET)
    reloaded: list[str] = []
    rc, _out, _err = nutclient.apply_nft(
        str(conf),
        ["192.168.1.40"],
        lambda p: (reloaded.append(p), (0, "", ""))[1],
        lambda: None,
        reload_path="/etc/nftables.conf",
    )
    assert rc == 0
    assert reloaded == ["/etc/nftables.conf"]


def test_apply_nft_no_change_skips_reload(tmp_path):
    conf = tmp_path / "main.nft"
    first, _ = nutclient.upsert_nft_input_chain(POLICY_DROP_RULESET, ["192.168.1.40"])
    conf.write_text(first)
    called: list[str] = []

    def run_nft(path: str) -> tuple[int, str, str]:
        called.append("nft")
        return 0, "", ""

    def restart_bouncer() -> None:
        called.append("restart")

    rc, _out, _err = nutclient.apply_nft(str(conf), ["192.168.1.40"], run_nft, restart_bouncer)
    assert rc == 0
    assert called == []


def test_apply_nft_missing_base_chain_file_is_error(tmp_path):
    # LIVE BUG #1: the base-chain file is a hard prerequisite — the accept must
    # land in the operator's existing input chain, so a missing file is a clear
    # rc-2 error, NOT a silent "write a fresh useless table" as the old code did.
    conf = tmp_path / "nftables.d" / "main.nft"  # does not exist
    called: list[str] = []
    rc, _out, err = nutclient.apply_nft(
        str(conf),
        ["192.168.1.40"],
        lambda p: (called.append("nft"), (0, "", ""))[1],
        lambda: called.append("restart"),
    )
    assert rc == 2
    assert called == []  # never reloaded or restarted
    assert "not found" in err


def test_apply_nft_no_input_hook_chain_is_error(tmp_path):
    # A file present but with no `hook input` base chain: rc 2, no reload.
    conf = tmp_path / "main.nft"
    conf.write_text("table inet filter {\n}\n")
    called: list[str] = []
    rc, _out, err = nutclient.apply_nft(
        str(conf),
        ["192.168.1.40"],
        lambda p: (called.append("nft"), (0, "", ""))[1],
        lambda: called.append("restart"),
    )
    assert rc == 2
    assert called == []
    assert "hook input" in err


# --- bootstrap_primary (impure orchestration) --------------------------------

SECRET = "REALsecret42"


class FakeLocal:
    """Records (argv, stdin) local-runner calls; returns queued canned tuples.

    The stdin channel is load-bearing: the password-bearing upsd.users write is
    piped on stdin via ``install /dev/stdin`` so the secret never reaches argv,
    and the recorded stdin lets a test prove it.
    """

    def __init__(self, responses: list[tuple[int, str, str]] | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self._responses = list(responses or [])
        self.default = (0, "", "")

    def __call__(self, argv, stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append((tuple(argv), stdin))
        if self._responses:
            return self._responses.pop(0)
        return self.default

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [a for a, _s in self.calls]


def _bootstrap_kwargs(tmp_path, **overrides):
    upsd_conf = tmp_path / "upsd.conf"
    upsd_conf.write_text("LISTEN 127.0.0.1 3493\n")
    upsd_users = tmp_path / "upsd.users"
    upsd_users.write_text("")
    nft = tmp_path / "nftables.conf"
    nft.write_text(POLICY_DROP_RULESET)
    kwargs = {
        "lan_ip": "192.168.1.125",
        "port": 3493,
        "user": "upsmon_secondary",
        "password": SECRET,
        "saddrs": ["192.168.1.40"],
        "upsd_conf_path": str(upsd_conf),
        "upsd_users_path": str(upsd_users),
        "nft_path": str(nft),
        "is_root": True,
    }
    kwargs.update(overrides)
    return kwargs, upsd_conf, upsd_users, nft


def test_bootstrap_primary_success_orders_steps_and_pipes_secret_on_stdin(tmp_path):
    kwargs, _conf, users, _nft = _bootstrap_kwargs(tmp_path)
    order: list[str] = []
    local = FakeLocal()

    def run_local(argv, stdin=None):
        joined = " ".join(argv)
        if "restart" in joined and "nut-server" in joined:
            order.append("restart-nut")
        elif "install" in joined:
            order.append("install")
        return local(argv, stdin)

    def run_nft(path):
        order.append("nft")
        return 0, "", ""

    def restart_bouncer():
        order.append("bouncer")

    rc, log = nutclient.bootstrap_primary(
        run_local=run_local,
        run_nft=run_nft,
        restart_bouncer=restart_bouncer,
        **kwargs,
    )
    assert rc == 0
    # nut-server restart happens BEFORE the nft reload; bouncer restart AFTER nft.
    assert order.index("restart-nut") < order.index("nft") < order.index("bouncer")
    # The real password reached the upsd.users writer on stdin, never on argv.
    users_stdin = [
        stdin for argv, stdin in local.calls if stdin is not None and "password = " in stdin
    ]
    assert users_stdin, "upsd.users content was never piped on stdin"
    assert f"password = {SECRET}" in users_stdin[0]
    for argv, _stdin in local.calls:
        assert SECRET not in " ".join(argv)
    # install writes 0640 via /dev/stdin.
    assert any("install -m 0640 -o root -g nut /dev/stdin" in " ".join(a) for a in local.argvs)


def test_bootstrap_primary_redacts_password_in_step_log(tmp_path):
    kwargs, _conf, _users, _nft = _bootstrap_kwargs(tmp_path)
    rc, log = nutclient.bootstrap_primary(
        run_local=FakeLocal(),
        run_nft=lambda p: (0, "", ""),
        restart_bouncer=lambda: None,
        **kwargs,
    )
    assert rc == 0
    for line in log:
        assert SECRET not in line


def test_bootstrap_primary_dry_run_mutates_nothing(tmp_path):
    kwargs, conf, users, nft = _bootstrap_kwargs(tmp_path)
    conf_before, users_before, nft_before = (
        conf.read_text(),
        users.read_text(),
        nft.read_text(),
    )
    effected: list[str] = []
    rc, log = nutclient.bootstrap_primary(
        run_local=lambda a, s=None: (effected.append("local"), (0, "", ""))[1],
        run_nft=lambda p: (effected.append("nft"), (0, "", ""))[1],
        restart_bouncer=lambda: effected.append("bouncer"),
        dry_run=True,
        **kwargs,
    )
    assert rc == 0
    assert effected == []
    assert conf.read_text() == conf_before
    assert users.read_text() == users_before
    assert nft.read_text() == nft_before
    for line in log:
        assert SECRET not in line


def test_bootstrap_primary_non_root_refuses_to_half_apply(tmp_path):
    kwargs, conf, users, nft = _bootstrap_kwargs(tmp_path, is_root=False)
    conf_before, users_before, nft_before = (
        conf.read_text(),
        users.read_text(),
        nft.read_text(),
    )
    effected: list[str] = []
    rc, log = nutclient.bootstrap_primary(
        run_local=lambda a, s=None: (effected.append("local"), (0, "", ""))[1],
        run_nft=lambda p: (effected.append("nft"), (0, "", ""))[1],
        restart_bouncer=lambda: effected.append("bouncer"),
        **kwargs,
    )
    assert rc == 4
    assert effected == []
    assert conf.read_text() == conf_before
    assert users.read_text() == users_before
    assert nft.read_text() == nft_before
    # The diff/commands are surfaced with the password redacted.
    joined = "\n".join(log)
    assert SECRET not in joined
    assert "root" in joined.lower()


def test_bootstrap_primary_restarts_when_only_users_changed(tmp_path):
    # upsd.conf already carries the LAN LISTEN (conf_changed False) but the user
    # block is new (users_changed True) — the "either changed" predicate must
    # still fire the nut-server restart. Pins the OR (not AND) in the guard.
    kwargs, conf, _users, _nft = _bootstrap_kwargs(tmp_path)
    conf.write_text("LISTEN 127.0.0.1 3493\nLISTEN 192.168.1.125 3493\n")
    restarted: list[str] = []

    def run_local(argv, stdin=None):
        if "restart" in " ".join(argv) and "nut-server" in " ".join(argv):
            restarted.append("restart")
        return 0, "", ""

    rc, _log = nutclient.bootstrap_primary(
        run_local=run_local,
        run_nft=lambda p: (0, "", ""),
        restart_bouncer=lambda: None,
        **kwargs,
    )
    assert rc == 0
    assert restarted == ["restart"]


def test_bootstrap_primary_failing_nut_restart_short_circuits_before_nft(tmp_path):
    kwargs, _conf, _users, _nft = _bootstrap_kwargs(tmp_path)

    def run_local(argv, stdin=None):
        if "restart" in " ".join(argv) and "nut-server" in " ".join(argv):
            return 1, "", "restart failed"
        return 0, "", ""

    nft_called: list[str] = []
    rc, log = nutclient.bootstrap_primary(
        run_local=run_local,
        run_nft=lambda p: (nft_called.append("nft"), (0, "", ""))[1],
        restart_bouncer=lambda: None,
        **kwargs,
    )
    assert rc == 4
    assert nft_called == []


def test_bootstrap_primary_conf_write_failure_short_circuits(tmp_path):
    # The first install (upsd.conf) failing aborts before writing upsd.users or
    # restarting; the error is redacted.
    kwargs, _conf, _users, _nft = _bootstrap_kwargs(tmp_path)
    installs: list[str] = []

    def run_local(argv, stdin=None):
        if "install" in " ".join(argv):
            installs.append("install")
            return 1, "", f"denied {SECRET}"
        return 0, "", ""

    rc, log = nutclient.bootstrap_primary(
        run_local=run_local,
        run_nft=lambda p: (0, "", ""),
        restart_bouncer=lambda: None,
        **kwargs,
    )
    assert rc == 4
    assert installs == ["install"]  # aborted after the first write
    assert all(SECRET not in line for line in log)


def test_bootstrap_primary_failing_nft_returns_four(tmp_path):
    kwargs, _conf, _users, _nft = _bootstrap_kwargs(tmp_path)
    rc, log = nutclient.bootstrap_primary(
        run_local=lambda a, s=None: (0, "", ""),
        run_nft=lambda p: (1, "", "nft parse error"),
        restart_bouncer=lambda: None,
        **kwargs,
    )
    assert rc == 4
    assert any("nft apply failed" in line for line in log)


# --- F2: the saddr set is IPv4, and a rejected ruleset is never left on disk ---


def test_valid_ipv4_rejects_the_v6_family():
    assert nutclient.valid_ipv4("192.168.1.114")
    assert not nutclient.valid_ipv4("2001:db8::1")
    assert not nutclient.valid_ipv4("::1")
    assert not nutclient.valid_ipv4("not-an-ip")
    # ...while the general predicate still accepts both, for LISTEN/--primary-ip.
    assert nutclient.valid_ip("2001:db8::1")


def test_render_nft_accept_rule_refuses_an_ipv6_member():
    """`ip saddr` is nftables' IPv4 matcher; the v6 spelling is `ip6 saddr`.

    A v6 literal there does not merely fail to match — `nft -f` rejects the WHOLE
    ruleset, which is how the operator's policy drop stops loading.
    """
    with pytest.raises(ValueError, match="ip6 saddr"):
        nutclient.render_nft_accept_rule(["2001:db8::1"])
    with pytest.raises(ValueError, match="IPv4 literals"):
        nutclient.render_nft_accept_rule(["192.168.1.114", "2001:db8::1"])


def test_apply_nft_restores_the_file_when_the_reload_is_rejected(tmp_path):
    """The write happened BEFORE `nft -f` got to judge it.

    `nft -f` is atomic, so a failed load leaves the RUNNING ruleset untouched —
    but `nftables.service` reads this file at boot, so a rejected ruleset left on
    disk means the next reboot comes up with no policy-drop chain at all.
    """
    conf = tmp_path / "main.nft"
    original = (
        "table inet filter {\n"
        "    chain input {\n"
        "        type filter hook input priority filter; policy drop;\n"
        "        ct state established,related accept\n"
        "    }\n"
        "}\n"
    )
    conf.write_text(original)
    calls: list[str] = []

    def _rejecting_nft(path: str) -> tuple[int, str, str]:
        calls.append(path)
        return 1, "", "Error: syntax error, unexpected junk"

    rc, _out, err = nutclient.apply_nft(str(conf), ["192.168.1.114"], _rejecting_nft, lambda: None)

    assert rc == 1
    assert calls, "the reload was attempted"
    assert conf.read_text() == original, "a rejected ruleset must not survive on disk"
    assert "restored" in err


def test_apply_nft_keeps_the_file_when_the_reload_succeeds(tmp_path):
    conf = tmp_path / "main.nft"
    conf.write_text(
        "table inet filter {\n"
        "    chain input {\n"
        "        type filter hook input priority filter; policy drop;\n"
        "        ct state established,related accept\n"
        "    }\n"
        "}\n"
    )
    restarted: list[bool] = []
    rc, _out, _err = nutclient.apply_nft(
        str(conf), ["192.168.1.114"], lambda _p: (0, "", ""), lambda: restarted.append(True)
    )
    assert rc == 0
    assert "192.168.1.114" in conf.read_text()
    assert restarted == [True]


def test_upsd_listen_emits_loopback_once_when_lan_ip_is_loopback():
    # A single-homed host (or the repair path on a conf with no LAN LISTEN) passes
    # 127.0.0.1 as the LAN address, making both candidate lines identical. upsd
    # must not be handed the same LISTEN twice.
    text, changed = nutclient.upsert_upsd_listen("", "127.0.0.1")
    assert changed is True
    assert _active(text) == ["LISTEN 127.0.0.1 3493"]
