"""Stdlib-only, fully-injectable engine for NUT secondary enrollment.

Every config artifact is rendered as a pure string function, and every side
effect runs through an injected ``run_local`` / ``run_ssh`` / ``run_nft``
callable (mirroring :class:`ups_orchestrator.events.Deps`), so the whole module
unit-tests against canned command output with zero live hosts.

Two research corrections are load-bearing and enforced here:

* A secondary's ``upsmon.conf`` OMITS ``POWERDOWNFLAG`` entirely — powering off
  the UPS is primary-only. A netclient must never disarm a UPS it does not own.
* The firewall accept is spliced into the operator's own input base chain — the
  one that owns the ``input`` hook and (typically) carries ``policy drop`` — via
  a marked, idempotent, reversible rule. A self-contained table at negative
  priority does NOT work here: nftables evaluates every base chain on the hook,
  and an accept in a ``policy accept`` −5 chain is terminal only for that chain,
  so the packet still reaches the ``policy drop`` priority-0 chain and is dropped
  (the live enrollment symptom: ``upsc`` timed out, the port never opened). The
  rule stays crowdsec-safe — the bouncer is restarted after every ``nft -f``.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

# The alias-shape rule is shared with Config.load and with the CLI's argparse
# boundary rather than re-spelled here — one predicate, every ssh sink (BL-C1).
from ups_orchestrator.config import valid_ssh_alias

# A NUT UPS name is restricted to this charset (letters, digits, dot, dash,
# underscore) by upsd itself. Any other byte — a space, semicolon, backtick,
# ``$`` — cannot be a real UPS name, so a value carrying one is an injection
# attempt against the remote shell string built in :func:`verify_secondary`,
# not a typo. Reject rather than escape.
_NUT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_nut_name(name: str) -> bool:
    """True iff ``name`` is a syntactically valid NUT UPS name. PURE."""
    return bool(_NUT_NAME_RE.match(name))


# LO-C4. `SHUTDOWNCMD "<cmd>"` is ONE directive on ONE line of the secondary's
# /etc/nut/upsmon.conf. Only the double-quote was rejected, so a newline closed the
# line and everything after it became a further upsmon directive of the operator's
# choosing — e.g. `--shutdown-cmd $'sudo /sbin/shutdown -h now\nNOTIFYCMD /tmp/x'`
# installs a NOTIFYCMD on a third machine. Operator == attacker here, so this is a
# foot-gun rather than a boundary crossing, but the check exists for exactly this
# shape and was rejecting the narrower half of it.
_CMD_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def valid_shutdown_cmd(cmd: str) -> bool:
    """True iff ``cmd`` can be embedded in ``SHUTDOWNCMD "<cmd>"`` verbatim. PURE.

    Rejected rather than escaped: a shutdown command is the one string on the
    secondary that must be read exactly as written, and NUT's own parser has no
    escape this could round-trip through.
    """
    return '"' not in cmd and _CMD_CTRL_RE.search(cmd) is None


def valid_ip(value: str) -> bool:
    """True iff ``value`` is a valid IPv4/IPv6 literal. PURE."""
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


def valid_ipv4(value: str) -> bool:
    """True iff ``value`` is a valid IPv**4** literal. PURE.

    F2: ``ip saddr`` is nftables' IPv4 matcher — the IPv6 spelling is ``ip6
    saddr``, and a v6 literal inside ``ip saddr { … }`` makes ``nft -f`` reject
    the WHOLE ruleset. `valid_ip` accepts both families, so a v6 address (reachable
    via ``monitor add --ip 2001:db8::1``, or — before 03-08 removed that rung —
    from the ``$SSH_CONNECTION`` fallback on a v6-reachable secondary) rendered a
    file the kernel will not load.
    """
    try:
        return isinstance(ipaddress.ip_address(value.strip()), ipaddress.IPv4Address)
    except ValueError:
        return False


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
# argv -> rc, out, err. The READ half of the nft seam, deliberately a separate
# type from ``RunNft``: that one takes a PATH and writes the kernel's ruleset,
# this one takes an argv and only reads it. Nothing in this module could see the
# running ruleset before, which is why file-versus-running drift was invisible —
# ``apply_nft`` decides "no change" by comparing the FILE to what the FILE would
# become and never asks the kernel.
RunNftRead = Callable[[Sequence[str]], tuple[int, str, str]]


# --- default side effects (live-only, excluded from the coverage floor) -------


def _default_run_local(argv: Sequence[str]) -> tuple[int, str, str]:  # pragma: no cover
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _default_run_ssh(
    alias: str, command: str, stdin: str | None = None
) -> tuple[int, str, str]:  # pragma: no cover
    proc = subprocess.run(
        # BL-C1: `--` terminates OpenSSH's option parsing, so an option-shaped alias
        # that reached here is read as a destination rather than as `-oProxyCommand=`.
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", alias, command],
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

    ``shutdown_cmd`` is emitted inside ``SHUTDOWNCMD "<cmd>"``; a double-quote or
    any control character would break the NUT parser or inject a further directive
    onto the secondary, so it is rejected rather than escaped (LO-C4).
    """
    if not valid_shutdown_cmd(shutdown_cmd):
        raise ValueError("shutdown_cmd must not contain a double-quote or any control character")
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


def _active_directives(text: str) -> list[str]:
    """Return the non-comment, non-blank directive lines of ``text``, stripped."""
    lines = (raw.strip() for raw in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def upsert_upsd_listen(text: str, lan_ip: str, port: int = 3493) -> tuple[str, bool]:
    """Add loopback + LAN ``LISTEN`` lines inside the MANAGED block, idempotently. PURE.

    Returns ``(new_text, changed)``. Only the lines that are not already ACTIVE
    outside the MANAGED block are rendered, so an operator's own ``LISTEN`` lines
    are honoured rather than duplicated.

    The loopback line is not optional. ``upsd`` listens on ``localhost:3493``
    when the file contains NO ``LISTEN`` statement at all, and Debian ships the
    file with every ``LISTEN`` commented out — so a file that reads as "no LISTEN
    configured" is really "listening on localhost". Writing the first explicit
    LISTEN REPLACES that implicit default, which cost two failures:

    1. Every bare ``upsc <name>`` (localhost) was refused, silently zeroing the
       recorder, the daily report, ``audit``, ``baseline`` and the dashboard.
    2. Worse, on a boot where ``eth0`` had no DHCP lease yet, the LAN address was
       the ONLY candidate; ``upsd`` logged "no listening interface available" and
       exited, and systemd's restart limit then left nut-server dead entirely.

    Loopback always exists before networking does, so keeping it in the block
    makes ``upsd`` survive an early boot and keeps local clients working.
    """
    lan_line = f"LISTEN {lan_ip} {port}"
    loopback_line = f"LISTEN 127.0.0.1 {port}"
    stripped = _strip_managed_block(text)
    active = _active_directives(stripped)
    # dict.fromkeys dedupes while keeping order: a caller that passes 127.0.0.1 as
    # the LAN address (a single-homed host, or the repair path when no LAN LISTEN
    # exists yet) makes both candidates the same string, and upsd would get it twice.
    candidates = dict.fromkeys((loopback_line, lan_line))
    needed = [line for line in candidates if line not in active]
    if not needed:
        return text, False
    block = f"{_UPSMON_BEGIN}\n" + "\n".join(needed) + f"\n{_UPSMON_END}\n"
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


# --- base-input-chain nftables render / upsert (pure) -------------------------
#
# LIVE BUG (device-death of the negative-priority dedicated table): the original
# design placed the accept in its OWN ``table inet ups_orchestrator`` base chain
# at ``priority filter - 5; policy accept``. nftables evaluates EVERY base chain
# registered on the ``input`` hook in ascending priority order; an ``accept``
# verdict is terminal only for the chain that issues it — it does NOT stop the
# packet from also traversing the operator's ``priority 0; policy drop`` base
# chain, whose ``policy drop`` then drops the packet. So the port never opened
# on a policy-drop firewall (upsc TIMED OUT rather than being refused).
#
# The accept must therefore be inserted INTO the base chain that owns the
# ``input`` hook — the one carrying ``policy drop`` — so the drop chain itself
# accepts the packet. We render a marked, idempotent, reversible rule and splice
# it in just after that chain's ct established/related accept.

_NFT_BEGIN = "# BEGIN UPS-ORCHESTRATOR MANAGED"
_NFT_END = "# END UPS-ORCHESTRATOR MANAGED"

# The SECOND managed block: the table-scope named-counter objects the rules above
# reference. It needs its own marker pair because a named counter is a sibling of
# the chain, not a statement inside it.
#
# THE MARKER TRAP. This marker must NOT be a prefix of — nor contain, nor be
# contained by — ``_NFT_BEGIN``. Research suggested
# ``# BEGIN UPS-ORCHESTRATOR MANAGED OBJECTS``, which is a strict prefix SUPERSET
# of ``# BEGIN UPS-ORCHESTRATOR MANAGED``. ``_strip_nft_block`` locates a block
# with a single ``text.find``, and the objects block sits EARLIER in the file
# (table scope precedes chain scope), so ``find(_NFT_BEGIN)`` would land on the
# objects block, strip from there through the RULE block's ``# END``, and silently
# delete the chain's opening lines with it. ``COUNTERS`` shares no prefix with
# ``MANAGED``, and a test pins that property in both directions so a rename cannot
# quietly reintroduce the collision.
_NFT_COUNTERS_BEGIN = "# BEGIN UPS-ORCHESTRATOR COUNTERS"
_NFT_COUNTERS_END = "# END UPS-ORCHESTRATOR COUNTERS"

# Every managed marker pair, stripped as a set so a second block can never
# accumulate. Order is irrelevant — the pairs are mutually non-overlapping by the
# rule above, which is what makes a per-pair ``find`` safe.
_NFT_MARKER_PAIRS = (
    (_NFT_BEGIN, _NFT_END),
    (_NFT_COUNTERS_BEGIN, _NFT_COUNTERS_END),
)

# Named counters. MODULE CONSTANTS, never caller input (T-03-29): these tokens are
# emitted into a file `nft -f` loads as root, and the whole reason the address set
# is re-validated at the render sink is that such a token can close a brace and
# append a rule above the operator's policy drop. Keep them constants.
#
# Names are checked against the three this box's main.nft already declares
# (cnt_services_kindle_dashboard, cnt_geoip_drop_cn, cnt_geoip_drop_ru) — no
# collision. Collision would not be an EEXIST anyway: main.nft opens with
# `table inet main {}` + `delete table inet main`, so the table and every object
# in it is rebuilt from scratch on each load (03-PREREQ-RESOLVED-nft-reload.md).
_NFT_COUNTER_ALLOWED = "upsd_allowed"
_NFT_COUNTER_DENIED = "upsd_denied"

# Matches the enclosing table's opening line, so the objects block can be inserted
# at TABLE scope. Kept deliberately loose on the table's family/name (the live box
# is `table inet main`, the tests use `table inet filter`) and strict on the brace
# being the last thing on the line — a table whose body opens on the next line is
# not a shape this repo writes or the operator's file uses.
_NFT_TABLE_OPEN_RE = re.compile(r"^([ \t]*)table\s+[A-Za-z0-9_]+\s+\S+\s*\{[ \t]*$", re.MULTILINE)

# Pins the real input base chain by its ``hook input`` declaration (the chain
# name is free-form: input/INPUT/inbound). We only splice into a chain that
# actually owns the input hook, so the accept lands where a policy-drop chain
# evaluates it.
_NFT_INPUT_HOOK_RE = re.compile(r"^([ \t]*)type\s+filter\s+hook\s+input\b", re.MULTILINE)
# A ct established/related accept inside that chain — our rule goes right after
# it so the conntrack fast-path stays first and our narrow accept precedes any
# later drop/reject in the same chain.
_NFT_CT_ACCEPT_RE = re.compile(
    r"^([ \t]*)ct\s+state\s+.*\b(?:established|related)\b.*accept.*$",
    re.MULTILINE | re.IGNORECASE,
)


def render_nft_counter_objects(indent: str = "    ") -> str:
    """Render the marked TABLE-scope named-counter declarations. PURE.

    A named counter is a table-scope OBJECT — a sibling of the chain, not a
    statement inside it — so it cannot live in the block
    :func:`render_nft_accept_rule` produces. Hence a second marked block, and
    hence the marker-prefix rule documented at ``_NFT_COUNTERS_BEGIN``.

    The names are emitted UNQUOTED. The asymmetry is easy to get backwards and is
    a hard failure in one direction only: ``counter "upsd_allowed" { … }`` is a
    syntax error (verified against nft 1.1.3: *unexpected quoted string, expecting
    string*), while the quoted form IS legal — and is what ``nft`` itself prints —
    in the ``counter name "upsd_allowed"`` REFERENCE the rules use. A rejected
    declaration means the whole file fails to load, taking the operator's
    policy-drop chain with it at the next boot.
    """
    return (
        f"{indent}{_NFT_COUNTERS_BEGIN}\n"
        f"{indent}counter {_NFT_COUNTER_ALLOWED} {{\n"
        f'{indent}    comment "tcp/3493 accepted from an enrolled NUT secondary"\n'
        f"{indent}}}\n"
        f"{indent}counter {_NFT_COUNTER_DENIED} {{\n"
        f'{indent}    comment "tcp/3493 from a source the enrolled set does not contain"\n'
        f"{indent}}}\n"
        f"{indent}{_NFT_COUNTERS_END}\n"
    )


def render_nft_accept_rule(
    saddrs: Sequence[str], port: int = 3493, indent: str = "        "
) -> str:
    """Render the marked accept + denied-counter rules for the input base chain. PURE.

    Returns a ``# BEGIN/END`` marker block wrapping TWO lines at ``indent``:

    1. ``tcp dport <port> ip saddr { … } counter name "upsd_allowed" accept`` —
       the counter reference sits BEFORE the verdict. A verdict terminates rule
       evaluation, so a counter placed after it never increments and still parses
       (verified: ``accept counter name "x"`` is accepted by nft). That mutant
       reads zero forever and is indistinguishable from "nothing connected".
    2. ``tcp dport <port> counter name "upsd_denied"`` — deliberately VERDICT-LESS,
       so the packet keeps traversing the chain and meets the chain's own policy.
       Anything reaching it did not match the set above (the accept is terminal for
       what it matches), so it counts exactly the tcp/3493 packets from sources the
       enrolled set does not contain. That is the failure that matters — a
       secondary blocked because its address is not in the set — and the allowed
       counter is structurally blind to it. Adding a verdict here would open
       tcp/3493 to EVERY source above the operator's policy drop (T-03-31).

    Empty ``saddrs`` → empty string (the rule is removed rather than left matching
    an empty set, which ``nft -f`` rejects).

    HI-C2: every member is re-validated HERE, at the shared sink, because this
    string is loaded by ``nft -f`` as root and a member that closes the brace can
    append an arbitrary rule — including ``ip saddr 0.0.0.0/0 accept`` — above the
    operator's policy drop. Refuse rather than escape: nothing but an IP literal is
    a legitimate member. The counter names get the same treatment for free by never
    being caller input at all.
    """
    if not saddrs:
        return ""
    # F2: IPv4 specifically. `ip saddr` is the IPv4 matcher (the v6 spelling is
    # `ip6 saddr`), so a v6 member does not merely fail to match — it makes
    # `nft -f` reject the ENTIRE ruleset, which is how the operator's policy drop
    # ends up not loading at the next boot.
    bad = [s for s in saddrs if not valid_ipv4(s)]
    if bad:
        raise ValueError(
            f"nft saddr members must be IPv4 literals (this is an 'ip saddr' set, "
            f"not 'ip6 saddr'); got {bad!r}"
        )
    members = ", ".join(s.strip() for s in saddrs)
    return (
        f"{indent}{_NFT_BEGIN}\n"
        f"{indent}tcp dport {port} ip saddr {{ {members} }} "
        f'counter name "{_NFT_COUNTER_ALLOWED}" accept comment "upsd NUT secondaries"\n'
        f"{indent}tcp dport {port} "
        f'counter name "{_NFT_COUNTER_DENIED}" '
        f'comment "upsd NUT secondaries: source not in the enrolled set"\n'
        f"{indent}{_NFT_END}\n"
    )


def _strip_one_nft_block(text: str, begin: str, end_marker: str) -> str:
    """Remove ONE ``begin``/``end_marker`` block (and its leading indent) from ``text``.

    Strips from the start of the ``# BEGIN`` line — including its own indentation
    — through the newline after ``# END``, so re-inserting is a byte-clean replace
    that leaves no dangling blank-indented line behind.
    """
    start = text.find(begin)
    if start == -1:
        return text
    line_start = text.rfind("\n", 0, start)
    line_start = 0 if line_start == -1 else line_start + 1
    end = text.find(end_marker, start)
    if end == -1:
        return text
    end = text.find("\n", end)
    end = len(text) if end == -1 else end + 1
    return text[:line_start] + text[end:]


def _strip_nft_block(text: str) -> str:
    """Remove EVERY managed block — rule and counter-object — from ``text``.

    The single ``find`` this used to be was correct only while there was one
    managed block. With two, the second was never stripped and ACCUMULATED a
    duplicate on every ``monitor add``; duplicate ``counter`` declarations are a
    hard ``nft -f`` failure, so the third enrollment would have started failing the
    reload — and, with the F2 rollback in place, failing every enrollment from then
    on (T-03-30).

    Loops per pair rather than stripping once, so a file that already carries
    duplicates from a hand edit (or from a build that shipped the single-``find``
    bug) is cleaned up rather than half-cleaned. Each iteration removes a whole
    block, so the loop is bounded by the number of blocks in the text.
    """
    for begin, end_marker in _NFT_MARKER_PAIRS:
        while True:
            shorter = _strip_one_nft_block(text, begin, end_marker)
            if shorter == text:
                break
            text = shorter
    return text


def upsert_nft_input_chain(text: str, saddrs: Sequence[str], port: int = 3493) -> tuple[str, bool]:
    """Insert/replace/drop the MANAGED accept inside the input base chain. PURE.

    Splices the marked accept into the input base chain that owns the ``input``
    hook (typically ``policy drop``) — the ONLY place an accept is honoured
    against a policy-drop firewall (see the module note above). The rule is placed
    right after the chain's ct established/related accept when present, else
    immediately after the chain's opening brace, so it sits near the top and
    precedes any later drop/reject in the same chain.

    TWO blocks are written, at two scopes: the counter OBJECTS just inside the
    enclosing table's brace (before the chain, so the objects are declared before
    the rules that reference them — a rule naming an undeclared counter is a hard
    ``nft -f`` failure), and the accept + denied rules inside the chain.

    Returns ``(new_text, changed)``; ``changed`` is ``False`` only when the result
    is byte-identical (idempotent). Empty ``saddrs`` removes BOTH blocks. Raises
    :class:`ValueError` when no ``hook input`` base chain exists, or when that
    chain has no enclosing ``table … {`` line — refusing to write a rule that
    would silently land nowhere useful, or a counter reference with no declaration.
    """
    stripped = _strip_nft_block(text)
    if not saddrs:
        return stripped, stripped != text

    hook = _NFT_INPUT_HOOK_RE.search(stripped)
    if hook is None:
        raise ValueError(
            "no `type filter hook input` base chain found — cannot insert the "
            "upsd accept where a policy-drop chain will honour it"
        )

    # The enclosing table: the LAST `table … {` line before the hook match. Found by
    # scanning backwards rather than by name, for the same reason the chain is found
    # by its hook rather than its name — the live box is `table inet main` and the
    # tests are `table inet filter`.
    table = None
    for match in _NFT_TABLE_OPEN_RE.finditer(stripped, 0, hook.start()):
        table = match
    if table is None:
        raise ValueError(
            "no enclosing `table … {` line found above the input base chain — the "
            "named counters are TABLE-scope objects and have nowhere to be declared, "
            "and a rule referencing an undeclared counter makes nft -f reject the "
            "whole ruleset"
        )

    # Prefer to sit right after the ct established/related accept in the same
    # chain (keeps the conntrack fast-path first). Fall back to just after the
    # chain's opening brace.
    ct = _NFT_CT_ACCEPT_RE.search(stripped, hook.end())
    if ct is not None:
        insert_at = ct.end() + 1  # past the ct line's trailing newline
        indent = ct.group(1) or "        "
    else:
        # No ct fast-path: sit right after the `type … hook input …` line so the
        # accept is the first concrete rule in the chain. Reuse that line's
        # indentation (captured group 1).
        indent = hook.group(1) or "        "
        nl = stripped.find("\n", hook.end())
        insert_at = nl + 1 if nl != -1 else len(stripped)
    rule = render_nft_accept_rule(saddrs, port, indent=indent)

    table_nl = stripped.find("\n", table.end())
    table_at = table_nl + 1 if table_nl != -1 else len(stripped)
    objects = render_nft_counter_objects(indent=(table.group(1) or "") + "    ")

    # LATER site first. Both offsets are into `stripped`; inserting at the earlier
    # one would shift the later by len(objects) and land the rule mid-line.
    new_text = stripped[:insert_at] + rule + stripped[insert_at:]
    new_text = new_text[:table_at] + objects + new_text[table_at:]
    return new_text, new_text != text


# --- reading the RUNNING ruleset's counters (seam + pure parser) --------------

_NFT_LIST_COUNTERS: tuple[str, ...] = ("nft", "-j", "list", "counters")


def _default_run_nft_read(argv: Sequence[str]) -> tuple[int, str, str]:  # pragma: no cover
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def parse_nft_counters(
    payload: str, table: str, family: str = "inet"
) -> dict[str, tuple[int, int]]:
    """``nft -j list counters`` output → ``{name: (packets, bytes)}``. PURE.

    Scoped to ``family``/``table``: a counter of the same name in a DIFFERENT
    table belongs to another subsystem, and reporting it as ours would be a
    confident wrong answer about whether our own block is even loaded.

    NEVER raises. Malformed JSON, an unexpected shape, a truncated document and an
    empty string all yield an empty mapping. This decorates a command whose exit
    code is a machine's shutdown verdict; a diagnostic that crashes the command it
    decorates is worse than no diagnostic at all.
    """
    try:
        doc = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    entries = doc.get("nftables")
    if not isinstance(entries, list):
        return {}
    out: dict[str, tuple[int, int]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        counter = entry.get("counter")
        if not isinstance(counter, dict):
            continue
        if counter.get("table") != table or counter.get("family") != family:
            continue
        name = counter.get("name")
        packets = counter.get("packets")
        chunk = counter.get("bytes")
        if not isinstance(name, str):
            continue
        # `bool` is an `int` subclass; a JSON `true` here is a shape error, not a
        # count of one.
        pkt = packets if isinstance(packets, int) and not isinstance(packets, bool) else 0
        byt = chunk if isinstance(chunk, int) and not isinstance(chunk, bool) else 0
        out[name] = (pkt, byt)
    return out


def read_nft_counters(
    table: str, run_nft_read: RunNftRead, family: str = "inet"
) -> tuple[bool, dict[str, tuple[int, int]]]:
    """``(readable, counters)`` from the RUNNING ruleset. IMPURE only in the runner.

    ``readable`` is False when ``nft`` itself failed — notably the non-root case,
    where listing counters needs ``CAP_NET_ADMIN``. That is a DIFFERENT fact from
    an empty mapping: an empty mapping on a SUCCESSFUL read means the managed block
    is not in the running ruleset, which is the cheap partial detector for
    file-versus-running drift. Collapsing the two would report a privilege problem
    as a missing firewall rule.
    """
    rc, out, _err = run_nft_read(_NFT_LIST_COUNTERS)
    if rc != 0:
        return False, {}
    return True, parse_nft_counters(out, table, family)


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
    refused with ``(False, reason)`` rather than executed. BL-C1: ``alias`` is now
    re-validated here too. The docstring used to say it was NOT, which made every
    caller the only checkpoint — and `monitor verify`/`monitor remove` were checking
    the other field.
    """
    if alias.strip() and not valid_ssh_alias(alias):
        # A BLANK alias is a different defect (there is no host to probe) and is left
        # to the runner to fail on, exactly as it does today; this guard is about a
        # value that ssh would read as an option.
        return False, f"invalid ssh alias: {alias!r}"
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
    reload_path: str | None = None,
) -> tuple[int, str, str]:
    """Splice the accept into the input base chain, reload, restart the bouncer.

    IMPURE. ``path`` is the file that CONTAINS the operator's input base chain
    (e.g. ``/etc/nftables.d/main.nft``); the accept is inserted into that chain
    so a ``policy drop`` firewall honours it. This is a hard prerequisite — unlike
    the old dedicated-table file, this file is NOT created on demand: a missing
    file or a file with no ``hook input`` base chain is a clear error (return code
    2) rather than a silently-useless write.

    Reloading flushes crowdsec's tables, so ``restart_bouncer`` MUST run after
    every successful reload — the bouncer does not auto-recreate them. When the
    upsert makes no change, the reload and restart are skipped entirely.

    ``reload_path`` is the file handed to ``nft -f`` (default: ``path``). Where
    the base chain lives in an ``include``d fragment, the operator reloads the
    top-level ``/etc/nftables.conf`` that pulls it in; pass it here so the full
    ruleset — not just the fragment — is reloaded.
    """
    conf = Path(path)
    try:
        text = conf.read_text()
    except FileNotFoundError:
        return 2, "", f"{path}: nftables base-chain file not found — cannot open the upsd port"
    try:
        new_text, changed = upsert_nft_input_chain(text, saddrs)
    except ValueError as exc:
        return 2, "", str(exc)
    if not changed:
        return 0, "no change", ""
    conf.write_text(new_text)
    rc, out, err = run_nft(reload_path or path)
    if rc != 0:
        # F2: the write happened BEFORE `nft -f` got to judge it, so a rejected
        # ruleset used to be left on disk permanently. `nft -f` is atomic, so the
        # RUNNING ruleset is untouched by a failed load — but `nftables.service`
        # reads this file at boot, so the next reboot came up with the operator's
        # policy-drop chain not loaded at all. Roll the file back to exactly what
        # was read, so a failed reload changes nothing anywhere.
        try:
            conf.write_text(text)
        except OSError as exc:  # pragma: no cover — the read above already succeeded
            return (
                rc,
                out,
                (
                    f"{err}\nCRITICAL: could not restore {path} after a failed nft reload "
                    f"({exc}); that file will not load at boot — restore it by hand"
                ),
            )
        return (
            rc,
            out,
            (
                f"{err}\n{path} was restored to its previous contents; the running "
                f"ruleset is unchanged (nft -f is atomic)"
            ),
        )
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
    nft_reload_path: str | None = None,
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
        rc, out, err = apply_nft(
            nft_path, saddrs, run_nft, restart_bouncer, reload_path=nft_reload_path
        )
        if rc != 0:
            log.append(f"nft apply failed: {_redact(err, password)}")
            return 4, log
    log.append("done")
    return 0, log
