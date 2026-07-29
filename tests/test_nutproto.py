"""Tests for the raw NUT protocol client.

Every negative test here asserts on the RECORDED LINE LIST — which lines went on
the wire, in what order — not merely on ``result.ok``. A boolean is a value both
branches of most mutants produce, so a test that checks only the boolean cannot
tell a working guard from a missing one. Each test names, in its docstring, the
mutation it kills.

No test in this file opens a socket, and that is enforced twice: structurally, by
``test_the_transport_is_a_required_argument`` (the transport parameters have no
default, so nothing can silently construct one), and at runtime by the
``no_real_sockets`` autouse fixture below. The suite-wide tripwire in
``tests/conftest.py`` covers process spawns only — a ``socket.create_connection``
would walk straight past it and reach the live ``upsd``, where an FSD would force
a real shutdown.
"""

from __future__ import annotations

import inspect
import socket
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ups_orchestrator import nutproto
from ups_orchestrator.nutproto import (
    FSD_ACK,
    ClientListResult,
    FsdResult,
    connect,
    list_clients,
    logout,
    set_fsd,
)

USER = "upsfsd"
# A distinctive literal, so "is the password anywhere it should not be" is an
# unambiguous substring search rather than a guess.
PASSWORD = "sekrit-PLAINTEXT-do-not-leak-8471"
UPS = "cyberpower"


# --- the socket tripwire (this file only; conftest's covers processes) --------


@pytest.fixture(autouse=True)
def no_real_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Refuse every real socket for the duration of a test.

    Recorded as well as raised: ``set_fsd`` catches broad exceptions by contract,
    so an ``AssertionError`` raised inside it would be swallowed and the escape
    would be invisible. The recording is asserted empty after the test body.
    """
    escaped: list[str] = []

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        escaped.append(f"socket.create_connection{args!r}")
        raise AssertionError(f"a real socket escaped the fakes: {args!r}")

    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield escaped
    assert not escaped, "real sockets escaped the fakes: " + "; ".join(escaped)


# --- recording fakes ----------------------------------------------------------


class RecordingSend:
    """Records every line handed to it; answers from a canned queue."""

    def __init__(self, responses: list[str], *, raises: BaseException | None = None) -> None:
        self.lines: list[str] = []
        self._responses = list(responses)
        self._raises = raises

    def __call__(self, line: str) -> str:
        self.lines.append(line)
        if self._raises is not None:
            raise self._raises
        return self._responses.pop(0) if self._responses else ""


class RecordingBlock:
    """Block-transport twin of :class:`RecordingSend`."""

    def __init__(self, block: list[str], *, raises: BaseException | None = None) -> None:
        self.lines: list[str] = []
        self._block = list(block)
        self._raises = raises

    def __call__(self, line: str) -> list[str]:
        self.lines.append(line)
        if self._raises is not None:
            raise self._raises
        return list(self._block)


def _three_ok() -> RecordingSend:
    return RecordingSend(["OK", "OK", FSD_ACK])


# --- the exchange -------------------------------------------------------------


def test_sends_username_then_password_then_fsd_in_order() -> None:
    """Kills: a reordered exchange, a skipped USERNAME line, and — because the
    third line is asserted verbatim — an ``FSD`` sent with NO argument, which is
    the all-UPSes-at-once behaviour ``upsmon -c fsd`` has."""
    send = _three_ok()
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert send.lines == [f"USERNAME {USER}", f"PASSWORD {PASSWORD}", f"FSD {UPS}"]
    assert send.lines[2] == "FSD cyberpower"  # the UPS NAME is on the FSD line
    assert result.ok is True


def test_stops_when_username_is_refused() -> None:
    """Kills: dropping the early return that ends the exchange at the first
    refusal. A mutant that blasts all three lines records 3 entries and puts the
    plaintext password on the wire to a server that already refused the user."""
    send = RecordingSend(["ERR ACCESS-DENIED"])
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert send.lines == [f"USERNAME {USER}"]
    assert not any(PASSWORD in line for line in send.lines)
    assert result.ok is False
    assert result.failed_line == "USERNAME"


def test_stops_when_password_is_refused() -> None:
    """Kills: the same dropped early return, one line later — an FSD issued after
    the credential was refused would be a command the server never authorised."""
    send = RecordingSend(["OK", "ERR ACCESS-DENIED"])
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert send.lines == [f"USERNAME {USER}", f"PASSWORD {PASSWORD}"]
    assert result.ok is False
    assert result.failed_line == "PASSWORD"


def test_bare_ok_to_the_fsd_line_is_a_failure() -> None:
    """Kills: weakening ``FSD_ACK`` to the bare ``OK`` that USERNAME and PASSWORD
    also return.

    The highest-value test in this file. ``OK`` is exactly what the two preceding
    lines answer, so a client checking for an ``OK`` prefix would report success
    for a server that authenticated us and never set the flag — and the operator
    would believe machines had been told to shut down when none had.
    """
    send = RecordingSend(["OK", "OK", "OK"])
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert result.ok is False
    assert result.failed_line == "FSD"
    assert send.lines == [f"USERNAME {USER}", f"PASSWORD {PASSWORD}", f"FSD {UPS}"]


def test_the_fsd_acknowledgement_is_distinct_from_the_auth_ok() -> None:
    """Kills: weakening ``FSD_ACK`` at the constant itself, independently of any
    exchange. The wire value is ``OK FSD-SET`` (``server/netmisc.c:93``)."""
    assert FSD_ACK == "OK FSD-SET"
    assert FSD_ACK != "OK"


@pytest.mark.parametrize(
    "bad",
    [
        "cyber power",  # a space ends the argument; the rest becomes a second one
        "cyberpower\nFSD cyberpower3",  # a newline appends a WHOLE second command
        "cyberpower;reboot",
        "",
    ],
)
def test_refuses_a_bad_ups_name_and_sends_nothing(bad: str) -> None:
    """Kills: dropping the ``valid_nut_name`` guard. Discriminating because a
    mutant that drops it records 3 lines whose third carries the injected second
    command — while the correct code records ZERO."""
    send = _three_ok()
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=bad)

    assert send.lines == []
    assert result.ok is False
    assert result.failed_line == "VALIDATION"


def test_refuses_a_password_containing_a_newline_and_sends_nothing() -> None:
    """Kills: dropping the control-character guard on the credentials. A newline
    closes the PASSWORD line and turns its tail into a further protocol command —
    here, an FSD against a UPS nobody asked about."""
    send = _three_ok()
    result = set_fsd(send, user=USER, password="pw\nFSD cyberpower3", upsname=UPS)

    assert send.lines == []
    assert result.ok is False
    assert result.failed_line == "VALIDATION"


def test_refuses_a_username_containing_a_control_character_and_sends_nothing() -> None:
    """Kills: applying the control-character guard to the password only, leaving
    the USERNAME line injectable."""
    send = _three_ok()
    result = set_fsd(send, user="upsfsd\nFSD cyberpower3", password=PASSWORD, upsname=UPS)

    assert send.lines == []
    assert result.ok is False
    assert result.failed_line == "VALIDATION"


def test_failure_detail_never_contains_the_password() -> None:
    """Kills: building the failure detail from the line that was SENT rather than
    from the line's NAME. The PASSWORD line's bytes are the secret itself, so a
    detail built from them leaks it into every log, event record and Discord embed
    the result reaches."""
    send = RecordingSend(["OK", "ERR ACCESS-DENIED"])
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert result.ok is False
    assert PASSWORD not in result.detail
    assert PASSWORD not in repr(result)
    assert PASSWORD not in str(result)


def test_a_server_that_echoes_the_password_back_is_scrubbed() -> None:
    """Kills: dropping the ``_scrub`` of the server's reply. Nothing this module
    composes contains the password, so the remaining way one could come back out
    is a server (or a MITM on 3493) reflecting it in an error line."""
    send = RecordingSend(["OK", f"ERR ACCESS-DENIED for {PASSWORD}"])
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert result.ok is False
    assert PASSWORD not in result.detail
    assert "<redacted>" in result.detail


def test_a_transport_that_raises_returns_a_failure_not_an_exception() -> None:
    """Kills: removing the broad catch the runner contract requires. An exception
    escaping onto the shutdown path strands every target sequenced behind it."""
    send = RecordingSend([], raises=OSError("connection refused"))
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert isinstance(result, FsdResult)
    assert result.ok is False
    assert result.failed_line == "USERNAME"
    assert send.lines == [f"USERNAME {USER}"]


def test_a_transport_timeout_is_reported_rather_than_hanging() -> None:
    """Kills: a read with no timeout. The fake raises what a timed-out socket read
    raises; the assertion that only ONE line was recorded proves the client did not
    go on to retry or push the password at a server that had already wedged."""
    send = RecordingSend([], raises=TimeoutError("timed out"))
    result = set_fsd(send, user=USER, password=PASSWORD, upsname=UPS)

    assert result.ok is False
    assert "TimeoutError" in result.detail
    assert send.lines == [f"USERNAME {USER}"]
    assert PASSWORD not in result.detail


def test_a_raising_transport_never_leaks_the_password_through_the_exception() -> None:
    """Kills: interpolating an unscrubbed ``str(exc)`` into the detail. A transport
    that was handed the PASSWORD line can put it in its own error message — a
    socket write error naming the buffer it failed on is the realistic shape."""

    class _Leaky(RecordingSend):
        def __call__(self, line: str) -> str:
            self.lines.append(line)
            # Fails on the PASSWORD line specifically, and names the buffer it
            # failed on — so the exception string carries the plaintext secret.
            if line.startswith("PASSWORD"):
                raise OSError(f"write failed on {line!r}")
            return "OK"

    leaky = _Leaky([])
    result = set_fsd(leaky, user=USER, password=PASSWORD, upsname=UPS)

    assert result.ok is False
    assert result.failed_line == "PASSWORD"
    assert leaky.lines == [f"USERNAME {USER}", f"PASSWORD {PASSWORD}"]
    assert PASSWORD not in result.detail
    assert PASSWORD not in repr(result)
    assert "<redacted>" in result.detail


def test_success_path_reports_the_ups_it_set() -> None:
    """Kills: a result that does not carry the UPS it acted on. A caller logging
    the result would otherwise be free to name the wrong device — and FSD is a
    sticky latch, so a misattributed one cannot be walked back."""
    send = _three_ok()
    result = set_fsd(send, user=USER, password=PASSWORD, upsname="cyberpower3")

    assert result.ok is True
    assert result.upsname == "cyberpower3"
    assert result.failed_line == ""
    assert send.lines[2] == "FSD cyberpower3"


def test_the_transport_is_a_required_argument() -> None:
    """Kills: giving the transport a socket-constructing default.

    This is the structural guard that keeps a live ``upsd`` out of the unit suite.
    ``tests/conftest.py``'s tripwire patches ``subprocess`` and the ``os`` spawn
    family and does NOT cover sockets, so a default here would be reachable from
    any test that forgot to inject — and the effect of reaching it is a real,
    irreversible FSD on a real UPS.
    """
    for func, param in ((set_fsd, "send"), (list_clients, "send_block"), (logout, "send")):
        sig = inspect.signature(func)
        assert param in sig.parameters, f"{func.__name__} lost its transport parameter"
        assert sig.parameters[param].default is inspect.Parameter.empty, (
            f"{func.__name__}({param}=...) grew a default — a unit test can now reach the live upsd"
        )
        assert sig.parameters[param].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_connect_always_applies_a_finite_timeout() -> None:
    """Kills: ``timeout=None`` on the live connection. ``None`` means block
    forever, and a wedged ``upsd`` would then hold the shutdown path open
    indefinitely instead of failing it."""
    timeout = inspect.signature(connect).parameters["timeout"].default
    assert isinstance(timeout, (int, float))
    assert 0 < timeout < 120


# --- LIST CLIENT --------------------------------------------------------------


def test_list_client_sends_the_singular_command_naming_the_ups() -> None:
    """Kills: sending ``LIST CLIENTS`` (plural), which upsd answers with
    ``ERR INVALID-ARGUMENT`` — and sending it with no UPS argument."""
    block = RecordingBlock(
        [f"BEGIN LIST CLIENT {UPS}", f"CLIENT {UPS} 127.0.0.1", f"END LIST CLIENT {UPS}"]
    )
    result = list_clients(block, upsname=UPS)

    assert block.lines == [f"LIST CLIENT {UPS}"]
    assert result.ok is True
    assert result.clients == ("127.0.0.1",)


def test_list_client_ignores_client_lines_for_another_ups() -> None:
    """Kills: harvesting every ``CLIENT`` line regardless of which UPS it names.
    A live defect on this host has a secondary declared against one UPS while
    logged in to another, so wrong-UPS lines are not hypothetical."""
    block = RecordingBlock(
        [
            f"BEGIN LIST CLIENT {UPS}",
            f"CLIENT {UPS} 192.168.1.114",
            "CLIENT cyberpower3 192.168.1.121",
            f"END LIST CLIENT {UPS}",
        ]
    )
    result = list_clients(block, upsname=UPS)

    assert result.clients == ("192.168.1.114",)


def test_list_client_err_is_a_failure_not_an_empty_census() -> None:
    """Kills: treating an ``ERR`` reply as a zero-length client list. "upsd does
    not know that UPS" would then read as "every secondary has drained" — the
    exact misreading that would let a caller proceed as though shutdown finished."""
    block = RecordingBlock(["ERR UNKNOWN-UPS"])
    result = list_clients(block, upsname=UPS)

    assert isinstance(result, ClientListResult)
    assert result.ok is False
    assert result.clients == ()


def test_list_client_refuses_a_bad_ups_name_and_sends_nothing() -> None:
    """Kills: dropping the name guard on the census path. Discriminating on the
    recording: the mutant sends a line carrying the injected second command."""
    block = RecordingBlock([])
    result = list_clients(block, upsname="cyberpower\nFSD cyberpower3")

    assert block.lines == []
    assert result.ok is False


def test_list_client_transport_failure_is_a_failure_not_an_empty_census() -> None:
    """Kills: letting a transport exception escape, or converting it to ok=True
    with an empty list — which again reads as "everyone has drained"."""
    block = RecordingBlock([], raises=OSError("connection reset"))
    result = list_clients(block, upsname=UPS)

    assert result.ok is False
    assert result.clients == ()


# --- framing ------------------------------------------------------------------


def _reader(lines: list[str]) -> Callable[[], str]:
    queue = list(lines)

    def _readline() -> str:
        return queue.pop(0) if queue else ""

    return _readline


def test_read_block_stops_at_end_list() -> None:
    """Kills: reading past ``END LIST`` into the next command's reply, which would
    desynchronise every later exchange on the same connection."""
    readline = _reader(
        [f"BEGIN LIST CLIENT {UPS}\n", f"CLIENT {UPS} 127.0.0.1\n", f"END LIST CLIENT {UPS}\n"]
    )
    block = nutproto._read_block(readline)

    assert block[-1] == f"END LIST CLIENT {UPS}"
    assert len(block) == 3


def test_read_block_returns_an_err_reply_immediately() -> None:
    """Kills: appending an ``ERR`` line to the block and reading on. upsd answers a
    refused ``LIST`` with ONE line and no ``END LIST``, so a reader that kept going
    would consume the next command's reply — or, on an idle connection, block until
    the socket timeout and turn a clean refusal into a spurious timeout."""
    readline = _reader(["ERR UNKNOWN-UPS\n"])
    block = nutproto._read_block(readline)

    assert block == ["ERR UNKNOWN-UPS"]


def test_read_block_is_bounded_against_an_endless_server() -> None:
    """Kills: an unbounded read loop. A socket timeout only catches a server that
    goes SILENT — one dribbling a line at a time resets it forever, so the ceiling
    is the only thing that ends the loop."""

    def _endless() -> str:
        return f"CLIENT {UPS} 127.0.0.1\n"

    with pytest.raises(OSError, match="END LIST"):
        nutproto._read_block(_endless)


def test_read_line_raises_on_a_closed_connection() -> None:
    """Kills: treating an empty read as a blank line. A closed socket returns ""
    immediately and forever, so a loop that tolerated it would spin at 100% CPU
    rather than fail — and no timeout would fire, because nothing blocks."""
    readline = _reader([])
    with pytest.raises(OSError, match="closed the connection"):
        nutproto._read_line(readline)


def test_read_line_propagates_a_timeout_rather_than_swallowing_it() -> None:
    """Kills: catching the timeout inside the framing layer. It must reach
    ``set_fsd``'s handler, which turns it into a NAMED failure — a swallowed one
    would surface as an empty response and be misreported as a refusal."""

    def _times_out() -> str:
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        nutproto._read_line(_times_out)


def test_logout_is_sent_verbatim_and_never_raises() -> None:
    """Kills: dropping the ``LOGOUT``, which leaves a stale entry in the very
    census ``list_clients`` reads; and letting a failing logout raise out of the
    connection teardown, which would mask the exchange's own result."""
    send = RecordingSend(["OK Goodbye"])
    assert logout(send) == "OK Goodbye"
    assert send.lines == ["LOGOUT"]

    failing = RecordingSend([], raises=OSError("already closed"))
    assert "LOGOUT failed" in logout(failing)
    assert failing.lines == ["LOGOUT"]
