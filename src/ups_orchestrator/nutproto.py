"""Stdlib-only NUT network-protocol client: per-UPS FSD, plus a client census.

Why this exists at all. NUT 2.8.1 ships no CLI that sets the forced-shutdown flag
on ONE named UPS. ``upsc`` is read-only, ``upscmd`` sends ``INSTCMD``, ``upsrw``
sends ``SET VAR`` (and ``ups.status`` is not writable), ``upssched`` just runs a
shell command. ``upsmon`` *does* send ``FSD`` — but only from ``forceshutdown()``,
which loops **every** ``ST_PRIMARY`` UPS and then shuts the local host down. So
``upsmon -c fsd`` on this primary would FSD all three UPSes and power off the
machine issuing it. There is no per-UPS stock path; this module is it.

The mechanism is first-class, not a workaround: ``upsmon``'s own ``setfsd()``
sends exactly ``FSD <upsname>``, and ``net_fsd()`` (``server/netmisc.c:67-94``)
sets ``ups->fsd = 1`` on precisely that UPS after a single
``user_checkaction(..., "FSD")``. No ``LOGIN`` is required. The whole exchange is
three lines::

    USERNAME <user>     -> OK
    PASSWORD <password> -> OK
    FSD <upsname>       -> OK FSD-SET

**FSD IS A STICKY LATCH.** ``ups->fsd`` is assigned ``1`` at
``server/netmisc.c:92`` and is assigned ``0`` **nowhere** in the server. Only
restarting ``upsd`` clears it, and ``man 8 upsmon`` states this as a design
decision, not an oversight:

    "the 'FSD' flag can not be removed from the data server unless its daemon is
    restarted."

Every caller must treat a successful :func:`set_fsd` as irreversible for the life
of the running ``upsd``. This is the central constraint on any later power-restore
work: a restore path cannot un-set the flag, it can only decide whether the
conditions to restart ``upsd`` are met (which is only safe while every UPS reads
``OL``).

Design, matching the seams this codebase already uses (``events.Deps``,
``nutclient``'s ``RunSSH``/``RunNft``, ``report``'s snapshot reader):

* The protocol logic is PURE apart from calling an INJECTED transport. That
  transport is a **required positional argument with no default** — see
  :func:`set_fsd`. ``tests/conftest.py``'s tripwire patches ``subprocess`` and the
  ``os`` spawn family; it does **not** cover sockets, so a default that built one
  would sail past it and reach the live ``upsd``. The absent default is the only
  structural guard, and ``tests/test_nutproto.py`` asserts it by introspection.
* The socket lives in exactly one place, :func:`connect`, marked
  ``# pragma: no cover — live only``, exactly as ``nutclient._default_run_ssh`` is.
* Nothing here raises on the shutdown path: every entry point returns a result
  object, because a runner that escapes strands everything sequenced after it
  (the contract ``events.py`` already enforces).
* The password is never interpolated into a returned detail, and every string that
  comes back from the server is scrubbed of it on the way out.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

# One spelling of each predicate, not two. `valid_nut_name` is the shared rule for
# every sink that interpolates a UPS name (nutclient uses it at the remote-shell
# sink); `_CMD_CTRL_RE` is the shared "no control character may close this line"
# character class. `valid_shutdown_cmd` itself does NOT fit here because it also
# rejects a double-quote — legitimate in a password, and meaningless to the NUT
# wire protocol, which has no quoting on the USERNAME/PASSWORD lines.
from ups_orchestrator.nutclient import _CMD_CTRL_RE as _CTRL_RE
from ups_orchestrator.nutclient import valid_nut_name

# The FSD line's acknowledgement is DISTINCT from the bare `OK` that USERNAME and
# PASSWORD return (`server/netmisc.c:93` sends "OK FSD-SET\n"). Accepting a bare
# `OK` here would report success for a server that authenticated us and then never
# set the flag — the operator would believe machines had been told to shut down
# when none had. Kept as a module constant so there is one literal to assert
# against and one for the mutation harness to patch.
FSD_ACK = "OK FSD-SET"
_OK = "OK"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3493
DEFAULT_TIMEOUT = 10.0

_REDACTED = "<redacted>"

# A protocol block (`BEGIN LIST … / END LIST …`) is bounded. A socket timeout only
# catches a server that goes SILENT; a server that dribbles one line forever keeps
# resetting it, so the read loop needs its own ceiling as well.
_MAX_BLOCK_LINES = 256

_LINE_USERNAME = "USERNAME"
_LINE_PASSWORD = "PASSWORD"
_LINE_FSD = "FSD"
_LINE_VALIDATION = "VALIDATION"
_LINE_LIST_CLIENT = "LIST CLIENT"


# --- injected transport types (mirror nutclient's runner types) ----------------

# One line out (WITHOUT a trailing newline), the server's single-line reply back,
# stripped. Raises on transport failure; every caller here converts that into a
# result object.
Send = Callable[[str], str]
# One line out, a whole `BEGIN LIST … / END LIST …` block back as stripped lines
# (or a single `ERR …` line).
SendBlock = Callable[[str], list[str]]
# A file-like `readline` — the seam the block reader is tested through, so the
# framing logic is exercised without a socket.
ReadLine = Callable[[], str]


# --- results ------------------------------------------------------------------


@dataclass(frozen=True)
class FsdResult:
    """Outcome of one FSD exchange. Carries no credential, by construction.

    ``failed_line`` names WHICH line was refused (``USERNAME`` / ``PASSWORD`` /
    ``FSD`` / ``VALIDATION``), never the bytes that were sent — the PASSWORD line's
    bytes are the secret itself. ``detail`` is built from that name plus the
    server's own (scrubbed) reply.
    """

    ok: bool
    upsname: str
    failed_line: str
    detail: str


@dataclass(frozen=True)
class ClientListResult:
    """Outcome of a ``LIST CLIENT <upsname>`` census.

    ``clients`` holds the addresses ``upsd`` currently has logged in FOR THIS UPS.

    **This is evidence, never a liveness decision.** ``upsd`` drops a client from
    the list only on ``LOGOUT`` or on the socket closing. A secondary killed by a
    dead switch produces neither, so its entry goes STALE and persists — plausibly
    until TCP keepalive gives up, since ``upsd.conf`` has no client idle timeout.
    That is precisely the scenario a fallback would exist for, so "still listed"
    cannot mean "still alive" and "gone from the list" cannot mean "finished
    shutting down" (with ``SHUTDOWNEXIT`` at its default, ``upsmon`` logs out when
    the shutdown is *initiated*, not *complete*). Log the census as a breadcrumb;
    do not branch on it.
    """

    ok: bool
    upsname: str
    clients: tuple[str, ...]
    detail: str


# --- validation ---------------------------------------------------------------


def _valid_credential(value: str) -> bool:
    """True iff ``value`` can be the tail of a single protocol line. PURE.

    Refuse rather than escape: the NUT wire protocol has no escape a newline could
    round-trip through, so a ``\\n`` in a username or password would close its line
    and turn everything after it into a further protocol command of the caller's
    choosing — ``FSD`` on a UPS nobody asked about, for instance.
    """
    return _CTRL_RE.search(value) is None


def _scrub(text: str, secret: str) -> str:
    """Replace every occurrence of ``secret`` in ``text``. PURE.

    Applied at the trust boundary — to strings that came back from the SERVER or
    out of a transport exception. Nothing this module composes itself ever contains
    the password, so this is the second line of defence, not the first.
    """
    return text.replace(secret, _REDACTED) if secret else text


# --- the exchange -------------------------------------------------------------


def set_fsd(send: Send, *, user: str, password: str, upsname: str) -> FsdResult:
    """Set the forced-shutdown flag on exactly ONE named UPS. Returns, never raises.

    ``send`` is a REQUIRED positional argument and deliberately has NO default: a
    default that constructed a socket would let a unit test reach the live ``upsd``
    and force a real shutdown, and the conftest tripwire does not cover sockets.
    Pass :func:`connect`'s first yielded callable in production, a fake in tests.

    All three inputs are validated BEFORE a single byte is sent, and the exchange
    stops at the first refused line — so a server that rejects the username never
    sees the password.

    Remember that success here is IRREVERSIBLE until ``upsd`` restarts; see the
    module docstring.
    """
    if not valid_nut_name(upsname):
        return FsdResult(
            ok=False,
            upsname=upsname,
            failed_line=_LINE_VALIDATION,
            detail=f"invalid UPS name: {upsname!r}",
        )
    if not _valid_credential(user):
        return FsdResult(
            ok=False,
            upsname=upsname,
            failed_line=_LINE_VALIDATION,
            detail="username contains a control character",
        )
    if not _valid_credential(password):
        return FsdResult(
            ok=False,
            upsname=upsname,
            failed_line=_LINE_VALIDATION,
            detail="password contains a control character",
        )

    exchange = (
        (_LINE_USERNAME, f"USERNAME {user}", _OK),
        (_LINE_PASSWORD, f"PASSWORD {password}", _OK),
        (_LINE_FSD, f"FSD {upsname}", FSD_ACK),
    )
    for name, line, expected in exchange:
        try:
            response = send(line)
        except Exception as exc:
            # Broad by contract: a transport that escapes onto the shutdown path
            # strands every target sequenced after it (events.py:64-69).
            detail = f"{name} failed: {type(exc).__name__}: {_scrub(str(exc), password)}"
            return FsdResult(ok=False, upsname=upsname, failed_line=name, detail=detail)
        if response.strip() != expected:
            detail = f"{name} refused: {_scrub(response.strip(), password)!r}"
            return FsdResult(ok=False, upsname=upsname, failed_line=name, detail=detail)
    return FsdResult(ok=True, upsname=upsname, failed_line="", detail=f"FSD set on {upsname}")


def list_clients(send_block: SendBlock, *, upsname: str) -> ClientListResult:
    """Census of the clients ``upsd`` has logged in for ``upsname``. Never raises.

    The command is ``LIST CLIENT``, **singular** — ``LIST CLIENTS`` returns
    ``ERR INVALID-ARGUMENT`` (measured). It needs no authentication on this build,
    so it is safe to poll from an unprivileged path.

    A ``CLIENT`` line naming a DIFFERENT UPS is ignored rather than counted: the
    census must answer "who is on this UPS", and a live defect on this very host
    (a secondary declared against one UPS while logged in to another) is exactly
    how a wrong-UPS line would show up.

    An ``ERR`` reply is a FAILURE, not an empty census — "upsd does not know that
    UPS" must never be read as "every secondary has drained".

    See :class:`ClientListResult` for why the result is evidence only.
    """
    if not valid_nut_name(upsname):
        return ClientListResult(
            ok=False, upsname=upsname, clients=(), detail=f"invalid UPS name: {upsname!r}"
        )
    try:
        lines = send_block(f"{_LINE_LIST_CLIENT} {upsname}")
    except Exception as exc:
        detail = f"{_LINE_LIST_CLIENT} failed: {type(exc).__name__}: {exc}"
        return ClientListResult(ok=False, upsname=upsname, clients=(), detail=detail)
    if lines and lines[0].startswith("ERR "):
        detail = f"{_LINE_LIST_CLIENT} refused: {lines[0]!r}"
        return ClientListResult(ok=False, upsname=upsname, clients=(), detail=detail)
    fields = (line.split() for line in lines)
    clients = tuple(
        parts[2]
        for parts in fields
        if len(parts) == 3 and parts[0] == "CLIENT" and parts[1] == upsname
    )
    detail = f"{len(clients)} client(s) logged in to {upsname}"
    return ClientListResult(ok=True, upsname=upsname, clients=clients, detail=detail)


def logout(send: Send) -> str:
    """Send ``LOGOUT`` so ``upsd`` drops us from its client list. Never raises.

    Worth doing even though the socket close would eventually do it: a clean
    ``LOGOUT`` is the only prompt removal ``upsd`` implements, and leaving a stale
    entry behind pollutes the very census :func:`list_clients` reads.
    """
    try:
        return send("LOGOUT")
    except Exception as exc:
        return f"LOGOUT failed: {type(exc).__name__}"


# --- framing (pure, driven through a readline seam) ---------------------------


def _read_line(readline: ReadLine) -> str:
    """One stripped protocol line, or ``OSError`` on EOF.

    An empty return from ``readline`` means the peer closed. Treating it as a blank
    line would spin a read loop forever against a dead socket, which a socket
    timeout does NOT catch — the call returns immediately, it does not block.
    """
    line = readline()
    if not line:
        raise OSError("upsd closed the connection before answering")
    return line.strip()


def _read_block(readline: ReadLine) -> list[str]:
    """Read a ``BEGIN LIST … / END LIST …`` block, or a single ``ERR`` line.

    Bounded at :data:`_MAX_BLOCK_LINES`: a server that never sends ``END LIST`` but
    keeps dribbling data resets the socket timeout on every line, so the ceiling is
    the only thing that ends the loop.
    """
    lines: list[str] = []
    for _ in range(_MAX_BLOCK_LINES):
        line = _read_line(readline)
        if line.startswith("ERR "):
            return [line]
        lines.append(line)
        if line.startswith("END LIST"):
            return lines
    raise OSError(f"upsd sent over {_MAX_BLOCK_LINES} lines with no END LIST")


# --- the single live transport (excluded from the coverage floor) -------------


@contextmanager
def connect(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT
) -> Iterator[tuple[Send, SendBlock]]:  # pragma: no cover — live only
    """Yield ``(send, send_block)`` bound to a real ``upsd``. THE ONLY SOCKET HERE.

    ``timeout`` bounds BOTH the connect and every subsequent read (the socket
    timeout is inherited by the file wrapper), so a wedged ``upsd`` fails the caller
    instead of hanging it — which on the shutdown path would hold up every target
    sequenced behind this one.

    ``LOGOUT`` is sent on the way out, before the socket closes, so ``upsd`` drops
    us from its client list promptly rather than leaving a stale entry.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    stream = sock.makefile("rw", encoding="ascii", newline="\n")

    def _send(line: str) -> str:
        stream.write(line + "\n")
        stream.flush()
        return _read_line(stream.readline)

    def _send_block(line: str) -> list[str]:
        stream.write(line + "\n")
        stream.flush()
        return _read_block(stream.readline)

    try:
        yield _send, _send_block
    finally:
        logout(_send)
        stream.close()
        sock.close()
