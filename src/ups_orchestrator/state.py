"""Per-UPS persisted state, written atomically.

NUT invokes the orchestrator once per event, so anything we need to remember
across invocations (when a UPS went on battery, whether we already fired a
one-shot action) lives here. State is keyed by NUT UPS name so multiple UPSes
never clobber each other.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class UpsState:
    """Mutable per-UPS bookkeeping."""

    onbatt_since: int | None = None
    shutdowns_sent: list[str] = field(default_factory=list)
    last_tick_notified: int | None = None
    last_status: str | None = None
    recent_loads: list[int] = field(default_factory=list)
    last_load_step_notified: int | None = None
    # True once the ON BATTERY page has been sent for the current outage — keeps a
    # transfer that clears within the notify grace (a blip or our own self-test)
    # from ever paging, and stops the poll loop re-paging a persistent outage.
    onbatt_notified: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> UpsState:
        raw_sent = data.get("shutdowns_sent", [])
        sent = [str(x) for x in raw_sent] if isinstance(raw_sent, list) else []
        raw_recent = data.get("recent_loads", [])
        recent = (
            [int(x) for x in raw_recent if isinstance(x, (int, float))]
            if isinstance(raw_recent, list)
            else []
        )
        if not recent:
            # Pre-window state files stored a single last_load.
            legacy = _opt_int(data.get("last_load"))
            if legacy is not None:
                recent = [legacy]
        return cls(
            onbatt_since=_opt_int(data.get("onbatt_since")),
            shutdowns_sent=sent,
            last_tick_notified=_opt_int(data.get("last_tick_notified")),
            last_status=str(data["last_status"]) if data.get("last_status") is not None else None,
            recent_loads=recent,
            last_load_step_notified=_opt_int(data.get("last_load_step_notified")),
            onbatt_notified=bool(data.get("onbatt_notified", False)),
        )


def _opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


_ACL_XATTR = "system.posix_acl_access"
# Bound the ACL helpers so a wedged filesystem cannot hang the poll loop (MED-02).
# Matches the timeout _default_serial_shutdown already puts on its stty.
_ACL_TIMEOUT = 5


def _has_posix_acl(path: Path) -> bool:
    """Does ``path`` carry a POSIX ACL, over and above its mode bits?

    Read through the xattr rather than through ``getfacl``, deliberately. The whole
    hazard HI-02 describes is a box where Debian's ``acl`` package is absent, so a
    detector that shells out to the same missing binary as the transfer would be blind
    in exactly the case it exists to catch. ``os.listxattr`` is stdlib and needs no
    package. A filesystem that cannot hold an xattr cannot hold a POSIX ACL either, so
    an OSError here is a truthful "no ACL" rather than a swallowed failure.
    """
    try:
        return _ACL_XATTR in os.listxattr(path)
    except OSError:
        return False


def _copy_acl(dest_path: Path, tmp_path: Path) -> bool:
    """Best-effort copy of ``dest_path``'s POSIX ACL onto ``tmp_path``.

    Returns whether the ACL was actually carried, so the caller can decline to widen
    the mode when it was not (HI-02).

    `shutil.copymode`/`copystat` do not carry a POSIX ACL, and the ACL is
    exactly what the live box lost (T-02-23). No pylibacl dependency is added
    (T-02-SC): this shells out to the platform's own getfacl/setfacl. Their
    absence, an unreadable ACL, or a filesystem without ACL support all log a
    warning and return rather than raise — a persist that cannot copy an ACL
    must still complete, because refusing to write is worse than a narrower
    ACL.

    MED-02: both calls are bounded by ``timeout``. This runs on ``StateStore.save``,
    which the watch loop runs every poll, so on a stale NFS/CIFS mount or a wedged
    FUSE filesystem an unbounded ``getfacl`` blocks in D state and takes
    ``handle_tick`` with it — no traceback, no exit, and ``Restart=always`` never
    fires because the process is still alive. That is the T-02-25 hang class this
    phase graded CRITICAL. ``TimeoutExpired`` is NOT an ``OSError``, so it is caught
    by name.

    LO-08: the path is ``--``-terminated. No shell is involved, so there is no
    injection, but a path beginning with ``-`` (operator-supplied via
    ``--config``/``--state``) would otherwise be parsed as an option.
    """
    try:
        got = subprocess.run(
            ["getfacl", "--omit-header", "--", str(dest_path)],
            capture_output=True,
            text=True,
            timeout=_ACL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "could not read ACL of %s (getfacl unavailable or hung?): %s", dest_path, exc
        )
        return False
    if got.returncode != 0:
        logger.warning("could not read ACL of %s: %s", dest_path, got.stderr.strip())
        return False
    try:
        applied = subprocess.run(
            ["setfacl", "--set-file=-", "--", str(tmp_path)],
            input=got.stdout,
            capture_output=True,
            text=True,
            timeout=_ACL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "could not apply ACL preserving %s (setfacl unavailable or hung?): %s", dest_path, exc
        )
        return False
    if applied.returncode != 0:
        logger.warning("could not apply ACL preserving %s: %s", dest_path, applied.stderr.strip())
        return False
    return True


def _preserved_mode(dest_path: Path, st: os.stat_result, *, acl_carried: bool) -> int:
    """The mode to put on the temp file — never WIDER than the destination's grant.

    When a POSIX ACL is present the mode's group bits are the ACL **mask**, not a
    grant: ``user::rw- user:nobody:r-- group::--- mask::r--`` displays as ``0640``
    while group has no access at all. Copying that mode while dropping the ACL —
    which is what happens on a minimal Debian with no ``acl`` package — hands group
    a real read the destination never granted. This function exists to FIX a
    permissions bug (T-02-23) and must not be able to create one, so when the ACL
    could not be carried the group bits are withheld. Failing safe NARROW is the
    documented preference; failing safe wide is not.
    """
    mode = stat.S_IMODE(st.st_mode)
    if acl_carried or not mode & stat.S_IRWXG or not _has_posix_acl(dest_path):
        return mode
    logger.warning(
        "withholding group access on %s: its POSIX ACL could not be carried onto the "
        "replacement, and the mode's group bits are that ACL's mask rather than a grant",
        dest_path,
    )
    return mode & ~stat.S_IRWXG


def replace_preserving_metadata(tmp_path: Path, dest_path: Path) -> None:
    """Atomically replace ``dest_path`` with ``tmp_path``, preserving the
    destination's mode, owner/group and POSIX ACL when it already exists.

    `tempfile.NamedTemporaryFile` creates at 0600 owned by the writer, and a
    bare `Path.replace` makes the destination inherit the TEMP file's mode,
    owner and ACL wholesale — the destination inode does not survive, so
    neither does its ACL (T-02-23). Each metadata kind is preserved
    independently; a failure logs a warning naming the destination and
    continues rather than raising, because the caller (``monitor
    add``/``remove`` or the poll loop's state save) has no recovery path. A
    destination that does not exist yet has nothing to inherit and is not an
    error.

    MED-03: a symlinked destination is RESOLVED first. ``Path.stat()`` follows a
    symlink but ``Path.replace()`` does not — it renames over the link path — so a
    deployment that symlinks ``/etc/ups-orchestrator/config.json`` into a git-managed
    or ``/srv`` location (a normal pattern for config under version control) was
    silently detached by the first ``monitor add``: from then on the daemon read a
    file the operator did not edit and edited a file the daemon did not read, with
    the link's target metadata copied so nothing looked wrong. The atomic rename now
    lands where the link points.
    """
    # os.path.islink rather than Path.is_symlink: it swallows its own OSError, so a
    # destination that cannot even be lstat'd stays on the ordinary path below.
    if os.path.islink(dest_path):
        dest_path = Path(os.path.realpath(dest_path))
    try:
        # `os.stat` rather than `Path.stat` so the destination and the temp file are
        # read through the SAME seam (see the ownership-flip check below), and so a
        # test can drive a two-writer destination without needing root to create one.
        st: os.stat_result | None = os.stat(dest_path)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        logger.warning("could not stat %s to preserve its metadata: %s", dest_path, exc)
        st = None
    if st is not None:
        # The ACL is transferred FIRST so the mode can be narrowed when it could not be
        # (HI-02). `setfacl --set-file` also rewrites the mode bits from the ACL, and
        # the chmod that follows re-applies the destination's own mode — whose group
        # bits are the destination's mask — so the net ACL is unchanged either way.
        acl_carried = _copy_acl(dest_path, tmp_path)
        try:
            os.chmod(tmp_path, _preserved_mode(dest_path, st, acl_carried=acl_carried))
        except OSError as exc:
            logger.warning("could not preserve mode of %s: %s", dest_path, exc)
        try:
            os.chown(tmp_path, st.st_uid, st.st_gid)
        except PermissionError as exc:
            # MED-08: the "expected, not actionable" rationale holds for an
            # unprivileged writer and NOT otherwise. EPERM from chown as root means
            # something genuinely unexpected — a restricted-capability container, an
            # immutable attribute, an idmapped mount — and the file then silently
            # keeps the writer's ownership, which is how T-02-23's live box lost its
            # ACL unnoticed for weeks. Keep the quiet path narrow rather than
            # silencing the one signal that would show a recurrence.
            #
            # F7: gating that WARNING on `geteuid() == 0` aimed it at an identity
            # that never fires it. The daemon runs as `klein`, so production always
            # took the DEBUG branch — and `state.json` has TWO writers (klein's
            # `watch` and nut's `upssched` via the dispatcher), which is exactly the
            # configuration where a failed chown silently flips ownership. The one
            # signal for a T-02-23 recurrence was pointed away from the only case
            # that produces one.
            #
            # The question that matters is not "am I root" but "does this change the
            # file's owner". Ask that instead: the temp file is owned by whoever is
            # writing, so a mismatch against the destination IS the flip. The root
            # case is kept as well — EPERM from chown as root means something
            # genuinely unexpected (a restricted-capability container, an immutable
            # attribute, an idmapped mount) and is worth a line either way.
            # `os.stat` rather than `Path.stat` on purpose: the temp file's OWN
            # owner is the writer's, and reading it (instead of assuming
            # euid/egid) keeps this correct under a setgid parent directory, where
            # the group is inherited from the directory and only the uid flips.
            try:
                tmp_st = os.stat(tmp_path)
                ownership_flips = (tmp_st.st_uid, tmp_st.st_gid) != (st.st_uid, st.st_gid)
            except OSError:
                ownership_flips = True  # cannot tell: report rather than swallow
            if ownership_flips or os.geteuid() == 0:
                logger.warning(
                    "could not preserve owner/group of %s: %s — it will now be owned by "
                    "uid %d gid %d instead of uid %d gid %d. This file has more than one "
                    "writer, and a silent ownership flip is how the live box lost its ACL "
                    "unnoticed (T-02-23).",
                    dest_path,
                    exc,
                    os.geteuid(),
                    os.getegid(),
                    st.st_uid,
                    st.st_gid,
                )
            else:
                logger.debug(
                    "not preserving owner/group of %s (unprivileged writer, ownership "
                    "unchanged): %s",
                    dest_path,
                    exc,
                )
        except OSError as exc:
            logger.warning("could not preserve owner/group of %s: %s", dest_path, exc)
    tmp_path.replace(dest_path)


def write_text_preserving_metadata(dest_path: Path, text: str) -> tuple[int, int, int, int] | None:
    """Replace ``dest_path``'s contents with ``text``: temp + fsync + atomic rename.

    THE writer for every file this project owns. Four call sites had grown their own
    copy of this six-line dance — ``StateStore.save``, ``_monitor_persist``,
    ``audit._write_marker`` and ``deploy/disable-live-shutdown-targets.sh`` — and two
    of them (IF-02, IF-08) never got the ``replace_preserving_metadata`` migration
    that T-02-23 introduced, so they kept the bare ``Path.replace`` that hands the
    destination the TEMP file's 0600-owned-by-the-writer metadata. The
    ``disable-live-shutdown-targets`` case is the sharp one: it is the documented way
    to turn shutdown off, it runs as root against
    ``/etc/ups-orchestrator/config.json``, and it silently took that file from
    ``0640 root:nut`` plus the installer's ACL to the root umask default — i.e. made
    a file holding a Discord webhook world-readable, with no warning and nothing
    visibly broken.

    A helper that only some writers use is how that recurred, so there is now one
    helper and no writer outside it. ``tests/test_state.py`` greps for a bare
    ``.replace(`` on a destination path to keep it that way.

    The parent directory is created if absent, the temp file is unlinked on any
    failure, and the fsync happens before the rename so a power loss cannot leave the
    destination pointing at a partial inode.

    Returns the ``file_identity`` of the inode just written, read off the temp file
    BEFORE the rename carries it into place. ``StateStore`` needs that identity to
    tell its own write from another process's; callers that do not care ignore it.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=dest_path.parent,
            prefix=f".{dest_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(text)
            # Durability: flush userspace + kernel buffers before the atomic rename,
            # so a power loss right after it can't leave the destination pointing at
            # a zero-length/partial inode. Matches recorder.py/jsonlog.py.
            tmp.flush()
            os.fsync(tmp.fileno())
        # Read the key off the TEMP inode, before the rename that carries it to the
        # destination. Statting the destination afterwards would race a writer that
        # got in first, and the caller would then adopt THEIR key as its own and
        # never notice their content. `replace_preserving_metadata` may chmod and
        # chown in between, and neither touches dev/ino/size/mtime_ns.
        stamp = file_identity(tmp_path)
        replace_preserving_metadata(tmp_path, dest_path)
        return stamp
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def write_json_preserving_metadata(dest_path: Path, payload: object, *, indent: int | None) -> None:
    """``write_text_preserving_metadata`` for a JSON document, newline-terminated."""
    write_text_preserving_metadata(
        dest_path, json.dumps(payload, indent=indent, sort_keys=True) + "\n"
    )


def file_identity(path: Path) -> tuple[int, int, int, int] | None:
    """Identity of the file's CURRENT contents, or ``None`` when it is absent.

    ``(dev, ino, size, mtime_ns)``. The inode number is the load-bearing member:
    every write here lands via ``replace_preserving_metadata``, i.e. a rename of a
    fresh temp inode over the destination, so an external write always changes
    ``st_ino`` even when size and mtime happen to match. A missing file compares
    equal to itself, so a store that has never seen the file does not reload on
    every check.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _read_states(path: Path) -> dict[str, UpsState]:
    """Parse the state file, or ``{}`` for absent/unreadable/corrupt/wrong-shape."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {name: UpsState.from_dict(data) for name, data in raw.items() if isinstance(data, dict)}


class StateStore:
    """Loads/saves a ``{ups_name: UpsState}`` map from a JSON file.

    IF-01: this file has TWO writers in the shipped deployment — klein's long-lived
    ``watch`` process and the ``nut`` user's ``upssched`` dispatcher (plus an
    operator-run ``remote-shutdown``, which takes the same event path). The load
    used to happen once, in ``__init__``, so the ``watch`` process kept an in-memory
    copy for its whole lifetime and rewrote the entire file every poll. Anything the
    event path wrote in between was discarded — including ``shutdowns_sent``, which
    Phase 2 promoted to the fire-once dedupe for projected machines. A machine
    already pushed by ``remote-shutdown`` was therefore pushed a SECOND time on the
    poll loop's next tick.

    Two seams close it, and both are needed because they cover different windows:

    * ``reload_if_changed`` — called at the top of each poll cycle, so the dedupe
      decision is made against what is actually on disk;
    * ``save``'s union of ``shutdowns_sent`` — covers a write that lands DURING a
      tick, between that reload and the save that ends it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._states: dict[str, UpsState] = {}
        # The file identity `self._states` was built from. Used to tell "nobody else
        # has written since we last looked" from "someone has".
        self._seen: tuple[int, int, int, int] | None = None
        self._load()

    def _load(self) -> None:
        # Stat BEFORE reading, deliberately. A write landing between the two then
        # records a stale key against fresh content, which costs one redundant
        # reload; the other order would record a fresh key against stale content and
        # the reload would never happen at all.
        stamp = file_identity(self.path)
        self._states = _read_states(self.path)
        self._seen = stamp

    def reload_if_changed(self) -> bool:
        """Re-read the file if another process has written it. Returns whether it did.

        Long-lived callers (the ``watch`` loop) must call this before making any
        fire-once decision. Callers that construct a store per invocation (the NUT
        event path) are already current and need not.
        """
        if file_identity(self.path) == self._seen:
            return False
        self._load()
        return True

    def _absorb_external_writes(self) -> None:
        """Fold another writer's ``shutdowns_sent`` into ours before overwriting them.

        Union rather than last-writer-wins, and ONLY for this field. ``shutdowns_sent``
        is a fire-once ledger, so the two possible errors are not symmetric: dropping
        an entry sends a second shutdown to a box that is already going down, while
        keeping one that the other writer had just cleared at most defers a push by a
        single poll — the next ONBATT/ONLINE transition clears it again on both sides.
        The union therefore always errs toward "already sent".

        Every other field stays last-writer-wins. They are monitoring bookkeeping
        (notification cadence, load window, status) where a lost update costs a
        duplicate Discord line, not a duplicate shutdown.

        A UPS present on disk but absent here is adopted wholesale: it belongs to a
        writer with a different config view, and rewriting the file would otherwise
        delete its state outright.
        """
        if file_identity(self.path) == self._seen:
            return
        for name, on_disk in _read_states(self.path).items():
            mine = self._states.get(name)
            if mine is None:
                self._states[name] = on_disk
                continue
            for sent in on_disk.shutdowns_sent:
                if sent not in mine.shutdowns_sent:
                    mine.shutdowns_sent.append(sent)

    def get(self, ups_name: str) -> UpsState:
        """Return the state for ``ups_name``, creating a blank one on first use."""
        return self._states.setdefault(ups_name, UpsState())

    def save(self) -> None:
        """Atomically persist all UPS states (write to temp, then replace)."""
        self._absorb_external_writes()
        payload = {name: asdict(st) for name, st in self._states.items()}
        self._seen = write_text_preserving_metadata(
            self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
