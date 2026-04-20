"""Shared concurrency primitives for the do-work skill.

See actions/concurrency-primitives.md for the full contract. This module ships
the executable primitives; every other REQ in the parallel-safety batch
(REQ-003..REQ-010) consumes them.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import socket
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union

PathLike = Union[str, os.PathLike]

CANONICAL_SCOPES: frozenset[str] = frozenset({
    "id-allocation:req",
    "id-allocation:ur",
    "cleanup-global",
})

PARAMETERIZED_SCOPE_PREFIXES: frozenset[str] = frozenset({
    "req-claim",
    "ur-archival",
    "verify-doc",
    "foreign-edit",
})

SCOPES: frozenset[str] = CANONICAL_SCOPES | frozenset(
    f"{p}:*" for p in PARAMETERIZED_SCOPE_PREFIXES
)


class ConcurrencyError(Exception):
    """Base class for all errors raised by this module."""


class LockHeldError(ConcurrencyError):
    """Raised when acquire_lock finds a lockfile already present.

    Carries enough holder info for a caller to render a useful message.
    """

    def __init__(
        self,
        *,
        path: PathLike,
        scope: str,
        holder: "LockInfo",
        attempting_session_id: str,
        attempting_operation: str,
    ) -> None:
        self.path = os.fspath(path)
        self.scope = scope
        self.holder = holder
        self.attempting_session_id = attempting_session_id
        self.attempting_operation = attempting_operation
        msg = (
            f"lock {self.path!r} is held\n"
            f"  scope:          {scope}\n"
            f"  held_by:        session {holder.session_id} "
            f"(operation: {holder.operation})\n"
            f"  acquired_at:    {holder.acquired_at}\n"
            f"  last_heartbeat: {holder.last_heartbeat}\n"
            f"  attempting:     session {attempting_session_id} "
            f"(operation: {attempting_operation})"
        )
        super().__init__(msg)


class ClaimFormatError(ConcurrencyError):
    """Raised when a claim file fails to parse or is missing required fields."""


class ClaimHeldError(ConcurrencyError):
    """Raised when a claim file already exists for the requested resource."""

    def __init__(
        self,
        *,
        path: PathLike,
        claim: "ClaimRecord",
        attempting_session_id: str,
        attempting_operation: str,
    ) -> None:
        self.path = os.fspath(path)
        self.claim = claim
        self.attempting_session_id = attempting_session_id
        self.attempting_operation = attempting_operation
        msg = (
            f"claim {self.path!r} is already held\n"
            f"  claim_id:       {claim.claim_id}\n"
            f"  held_by:        session {claim.session_id} "
            f"(operation: {claim.operation})\n"
            f"  acquired_at:    {claim.acquired_at}\n"
            f"  last_heartbeat: {claim.last_heartbeat}\n"
            f"  attempting:     session {attempting_session_id} "
            f"(operation: {attempting_operation})"
        )
        super().__init__(msg)


class SessionClaimConflictError(ConcurrencyError):
    """Raised when one session tries to hold multiple work claims at once."""


class ScopeError(ConcurrencyError):
    """Raised when a scope name is not in the canonical catalog."""


class StaleRenameError(ConcurrencyError):
    """Raised when atomic_rename's source does not exist (ENOENT)."""


class CrossDeviceError(ConcurrencyError):
    """Raised when atomic_rename crosses filesystems (EXDEV)."""


class CollisionError(ConcurrencyError):
    """Raised when atomic_rename's destination already exists."""


class AtomicWriteError(ConcurrencyError):
    """Raised when atomic_write fails (disk full, permissions, etc.)."""


class ForeignReleaseError(ConcurrencyError):
    """Raised when release_lock is called with a handle that no longer matches
    the on-disk lockfile (the lock was replaced by a different session)."""


@dataclass(frozen=True)
class AllocatedId:
    identifier: str
    number: int
    namespace: Literal["req", "ur"]
    path: str
    lock_path: str


REQ_ID_RE = re.compile(r"^REQ-(\d+)(?:-.+)?\.md$")
UR_ID_RE = re.compile(r"^UR-(\d+)$")
ID_NAMESPACE_PREFIX: dict[Literal["req", "ur"], str] = {
    "req": "REQ",
    "ur": "UR",
}
ID_NAMESPACE_SCOPE: dict[Literal["req", "ur"], str] = {
    "req": "id-allocation:req",
    "ur": "id-allocation:ur",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _coerce_do_work_root(path: PathLike) -> Path:
    return Path(os.fspath(path))


def _id_lock_path(do_work_root: PathLike, namespace: Literal["req", "ur"]) -> Path:
    root = _coerce_do_work_root(do_work_root)
    return root / ".locks" / f"id-allocation-{namespace}.lock"


def _format_identifier(namespace: Literal["req", "ur"], number: int) -> str:
    return f"{ID_NAMESPACE_PREFIX[namespace]}-{number:03d}"


def _extract_identifier_number(path: Path, namespace: Literal["req", "ur"]) -> Optional[int]:
    matcher = REQ_ID_RE if namespace == "req" else UR_ID_RE
    match = matcher.match(path.name)
    if match is None:
        return None
    return int(match.group(1))


def _iter_authoritative_req_paths(do_work_root: PathLike) -> Iterable[Path]:
    root = _coerce_do_work_root(do_work_root)
    patterns = (
        "REQ-*.md",
        "working/REQ-*.md",
        "archive/REQ-*.md",
        "archive/UR-*/REQ-*.md",
    )
    for pattern in patterns:
        yield from root.glob(pattern)


def _iter_authoritative_ur_paths(do_work_root: PathLike) -> Iterable[Path]:
    root = _coerce_do_work_root(do_work_root)
    patterns = (
        "user-requests/UR-*",
        "archive/UR-*",
    )
    for pattern in patterns:
        yield from root.glob(pattern)


def _iter_authoritative_paths(
    do_work_root: PathLike,
    namespace: Literal["req", "ur"],
) -> Iterable[Path]:
    if namespace == "req":
        yield from _iter_authoritative_req_paths(do_work_root)
        return
    yield from _iter_authoritative_ur_paths(do_work_root)


def _scan_existing_numbers(
    do_work_root: PathLike,
    namespace: Literal["req", "ur"],
) -> list[int]:
    numbers = []
    for path in _iter_authoritative_paths(do_work_root, namespace):
        number = _extract_identifier_number(path, namespace)
        if number is not None:
            numbers.append(number)
    return numbers


def _next_identifier_number(
    do_work_root: PathLike,
    namespace: Literal["req", "ur"],
) -> int:
    numbers = _scan_existing_numbers(do_work_root, namespace)
    return max(numbers, default=0) + 1


def _find_conflicting_identifier_path(
    do_work_root: PathLike,
    namespace: Literal["req", "ur"],
    identifier: str,
) -> Optional[Path]:
    number = int(identifier.split("-", 1)[1])
    for path in _iter_authoritative_paths(do_work_root, namespace):
        existing = _extract_identifier_number(path, namespace)
        if existing == number:
            return path
    return None


def _write_new_file(path: PathLike, content: str, *, transition: str) -> None:
    target = Path(os.fspath(path))
    temp_target = target.with_name(f"{target.name}.allocating.{secrets.token_hex(4)}")
    try:
        atomic_write(temp_target, content)
        atomic_rename(temp_target, target, transition=transition)
    except Exception:
        try:
            os.unlink(temp_target)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def _acquire_id_allocation_lock(
    do_work_root: PathLike,
    *,
    namespace: Literal["req", "ur"],
    session_id: str,
    operation: str,
    now: Optional[datetime] = None,
    timeout_seconds: float = 5.0,
    retry_interval_seconds: float = 0.05,
) -> LockHandle:
    lock_path = _id_lock_path(do_work_root, namespace)
    deadline = time.monotonic() + timeout_seconds

    while True:
        try:
            return acquire_lock(
                lock_path,
                session_id=session_id,
                operation=operation,
                scope=ID_NAMESPACE_SCOPE[namespace],
                now=now,
            )
        except LockHeldError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(retry_interval_seconds)


def validate_scope(scope: str) -> None:
    """Raise ScopeError if scope is not canonical."""
    if scope in CANONICAL_SCOPES:
        return
    if ":" in scope:
        prefix, _, suffix = scope.partition(":")
        if prefix in PARAMETERIZED_SCOPE_PREFIXES and suffix:
            return
    raise ScopeError(
        f"unknown scope {scope!r}. "
        f"Canonical scopes: {sorted(CANONICAL_SCOPES)}. "
        f"Parameterized prefixes: {sorted(PARAMETERIZED_SCOPE_PREFIXES)} "
        f"(format: '<prefix>:<identifier>'). "
        f"Add new scopes to actions/concurrency-primitives.md and lib/concurrency.py."
    )


@dataclass(frozen=True)
class LockInfo:
    session_id: str
    operation: str
    scope: str
    acquired_at: str
    last_heartbeat: str
    pid: int
    hostname: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LockInfo":
        required = {
            "session_id", "operation", "scope",
            "acquired_at", "last_heartbeat", "pid", "hostname",
        }
        missing = required - set(d)
        if missing:
            raise ClaimFormatError(
                f"lockfile missing fields: {sorted(missing)}"
            )
        validate_scope(d["scope"])
        return cls(**{k: d[k] for k in required})


@dataclass
class LockHandle:
    path: str
    info: LockInfo


@dataclass
class ClaimHandle:
    path: str
    claim: "ClaimRecord"


@dataclass
class WorkClaimHandle:
    request_path: str
    claim_path: str
    claim: "ClaimRecord"


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    session_id: str
    operation: str
    scope: str
    affected_paths: tuple[str, ...]
    acquired_at: str
    last_heartbeat: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClaimRecord":
        required = {
            "claim_id", "session_id", "operation", "scope",
            "affected_paths", "acquired_at", "last_heartbeat",
        }
        missing = required - set(d)
        if missing:
            raise ClaimFormatError(
                f"claim missing fields: {sorted(missing)}"
            )
        validate_scope(d["scope"])
        return cls(
            claim_id=d["claim_id"],
            session_id=d["session_id"],
            operation=d["operation"],
            scope=d["scope"],
            affected_paths=tuple(d["affected_paths"]),
            acquired_at=d["acquired_at"],
            last_heartbeat=d["last_heartbeat"],
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["affected_paths"] = list(self.affected_paths)
        return d


def atomic_write(path: PathLike, content: str, *, mode: str = "w") -> None:
    """Write content to path via write-to-temp-then-rename.

    Readers on path never see a half-written file: either the previous content
    or the new content, never a partial blend.
    """
    target = os.fspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    suffix = f".tmp.{secrets.token_hex(4)}"
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".", suffix=suffix, dir=directory)
    try:
        with os.fdopen(fd, mode) as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.rename(tmp, target)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise AtomicWriteError(f"atomic_write to {target!r} failed: {e}") from e


def atomic_rename(src: PathLike, dst: PathLike, *, transition: str) -> None:
    """Atomic state transition. Raises rich errors instead of POSIX rename's
    silent overwrite / cryptic errnos.
    """
    src_s = os.fspath(src)
    dst_s = os.fspath(dst)
    if not os.path.exists(src_s):
        raise StaleRenameError(
            f"atomic_rename[{transition}]: source {src_s!r} does not exist"
        )
    if os.path.exists(dst_s):
        raise CollisionError(
            f"atomic_rename[{transition}]: destination {dst_s!r} already exists. "
            "Atomic rename refuses silent overwrite."
        )
    try:
        os.rename(src_s, dst_s)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise CrossDeviceError(
                f"atomic_rename[{transition}]: {src_s!r} and {dst_s!r} "
                "are on different filesystems — cannot rename atomically."
            ) from e
        if e.errno == errno.ENOENT:
            raise StaleRenameError(
                f"atomic_rename[{transition}]: source {src_s!r} vanished mid-op"
            ) from e
        raise


def _write_lockfile_exclusive(path: str, info: LockInfo) -> None:
    """Create path with O_CREAT|O_EXCL and write info as JSON. Raises
    FileExistsError if the lockfile already exists.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(info), f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _write_claim_exclusive(path: str, claim: ClaimRecord) -> None:
    """Create path with O_CREAT|O_EXCL and write the claim JSON."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(claim.to_dict(), f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _inspect_lock_after_contention(
    path: str,
    *,
    attempts: int = 5,
    retry_interval_seconds: float = 0.01,
) -> Optional[LockInfo]:
    last_error: Optional[ClaimFormatError] = None
    for _ in range(attempts):
        try:
            return inspect_lock(path)
        except ClaimFormatError as exc:
            last_error = exc
            time.sleep(retry_interval_seconds)
    if last_error is not None:
        raise last_error
    return None


def _inspect_claim_after_contention(
    path: str,
    *,
    attempts: int = 5,
    retry_interval_seconds: float = 0.01,
) -> Optional[ClaimRecord]:
    last_error: Optional[ClaimFormatError] = None
    for _ in range(attempts):
        try:
            return read_claim(path)
        except ClaimFormatError as exc:
            last_error = exc
            time.sleep(retry_interval_seconds)
    if last_error is not None:
        raise last_error
    return None


def _req_identifier_from_path(path: Path) -> str:
    number = _extract_identifier_number(path, "req")
    if number is None:
        raise ConcurrencyError(
            f"work claim requires a REQ filename, got {os.fspath(path)!r}"
        )
    return _format_identifier("req", number)


def _work_request_path(do_work_root: PathLike, request_path: PathLike) -> Path:
    root = _coerce_do_work_root(do_work_root)
    candidate = Path(os.fspath(request_path))
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _work_claim_path(working_request_path: Path) -> Path:
    return working_request_path.with_suffix(".claim.json")


def _iter_work_claim_paths(do_work_root: PathLike) -> Iterable[Path]:
    root = _coerce_do_work_root(do_work_root)
    yield from (root / "working").glob("*.claim.json")


def acquire_lock(
    path: PathLike,
    *,
    session_id: str,
    operation: str,
    scope: str,
    pid: Optional[int] = None,
    hostname: Optional[str] = None,
    now: Optional[datetime] = None,
) -> LockHandle:
    """Acquire exclusive lock at path. Fails fast with LockHeldError if held."""
    validate_scope(scope)
    target = os.fspath(path)
    pid = pid if pid is not None else os.getpid()
    hostname = hostname if hostname is not None else socket.gethostname()
    now_s = _iso(now or _utcnow())
    info = LockInfo(
        session_id=session_id,
        operation=operation,
        scope=scope,
        acquired_at=now_s,
        last_heartbeat=now_s,
        pid=pid,
        hostname=hostname,
    )
    try:
        _write_lockfile_exclusive(target, info)
    except FileExistsError:
        holder = _inspect_lock_after_contention(target)
        if holder is None:
            # Rare race: file existed at O_EXCL but was released before we read.
            # Retry once.
            try:
                _write_lockfile_exclusive(target, info)
                return LockHandle(path=target, info=info)
            except FileExistsError:
                holder = _inspect_lock_after_contention(target)
        if holder is None:
            raise LockHeldError(
                path=target,
                scope=scope,
                holder=LockInfo(
                    session_id="<unknown>",
                    operation="<unknown>",
                    scope=scope,
                    acquired_at="<unknown>",
                    last_heartbeat="<unknown>",
                    pid=0,
                    hostname="<unknown>",
                ),
                attempting_session_id=session_id,
                attempting_operation=operation,
            )
        raise LockHeldError(
            path=target,
            scope=scope,
            holder=holder,
            attempting_session_id=session_id,
            attempting_operation=operation,
        )
    return LockHandle(path=target, info=info)


def inspect_lock(path: PathLike) -> Optional[LockInfo]:
    """Return LockInfo for the lockfile at path, or None if no lockfile."""
    target = os.fspath(path)
    try:
        with open(target, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise ClaimFormatError(f"lockfile {target!r} is not valid JSON: {e}") from e
    return LockInfo.from_dict(data)


def release_lock(handle_or_path: Union[LockHandle, PathLike]) -> None:
    """Remove the lockfile. Raises ForeignReleaseError if the on-disk content
    no longer matches the handle's session_id.
    """
    if isinstance(handle_or_path, LockHandle):
        path = handle_or_path.path
        expected_session = handle_or_path.info.session_id
    else:
        path = os.fspath(handle_or_path)
        expected_session = None

    current = inspect_lock(path)
    if current is None:
        return  # idempotent: already released
    if expected_session is not None and current.session_id != expected_session:
        raise ForeignReleaseError(
            f"refusing to release {path!r}: held by session "
            f"{current.session_id!r}, handle claims {expected_session!r}"
        )
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def refresh_heartbeat(
    handle_or_path: Union[LockHandle, PathLike],
    *,
    now: Optional[datetime] = None,
) -> None:
    """Rewrite last_heartbeat on the lockfile via atomic write."""
    if isinstance(handle_or_path, LockHandle):
        path = handle_or_path.path
        expected_session = handle_or_path.info.session_id
    else:
        path = os.fspath(handle_or_path)
        expected_session = None

    current = inspect_lock(path)
    if current is None:
        raise ConcurrencyError(
            f"refresh_heartbeat: no lockfile at {path!r}"
        )
    if expected_session is not None and current.session_id != expected_session:
        raise ForeignReleaseError(
            f"refusing to refresh heartbeat on {path!r}: held by session "
            f"{current.session_id!r}, handle claims {expected_session!r}"
        )
    updated = LockInfo(
        session_id=current.session_id,
        operation=current.operation,
        scope=current.scope,
        acquired_at=current.acquired_at,
        last_heartbeat=_iso(now or _utcnow()),
        pid=current.pid,
        hostname=current.hostname,
    )
    atomic_write(path, json.dumps(asdict(updated), indent=2, sort_keys=True) + "\n")
    if isinstance(handle_or_path, LockHandle):
        handle_or_path.info = updated


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive.
        return True
    except OSError:
        return False
    return True


def classify_lock(
    info: LockInfo,
    *,
    now: datetime,
    stale_threshold: timedelta = timedelta(minutes=2),
) -> Literal["live", "stale", "orphaned"]:
    """Classify an existing lock. Pure — no filesystem mutation, no I/O beyond
    a liveness probe of the holder's PID.

    Cross-host always returns 'stale' (UR-001 is single-machine).
    """
    local_host = socket.gethostname()
    last_hb = _parse_iso(info.last_heartbeat)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = now - last_hb

    if age < stale_threshold:
        return "live"

    if info.hostname != local_host:
        return "stale"

    return "orphaned" if not _pid_alive(info.pid) else "stale"


def read_claim(path: PathLike) -> ClaimRecord:
    target = os.fspath(path)
    try:
        with open(target, "r") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise ClaimFormatError(f"claim file {target!r} not found") from e
    except json.JSONDecodeError as e:
        raise ClaimFormatError(f"claim file {target!r} is not valid JSON: {e}") from e
    return ClaimRecord.from_dict(data)


def write_claim(path: PathLike, claim: ClaimRecord) -> None:
    atomic_write(path, json.dumps(claim.to_dict(), indent=2, sort_keys=True) + "\n")


def release_claim(
    handle_or_path: Union[ClaimHandle, PathLike],
    *,
    expected_session_id: Optional[str] = None,
) -> None:
    """Remove a claim file, optionally asserting ownership."""
    if isinstance(handle_or_path, ClaimHandle):
        path = handle_or_path.path
        if expected_session_id is None:
            expected_session_id = handle_or_path.claim.session_id
    else:
        path = os.fspath(handle_or_path)

    try:
        current = read_claim(path)
    except ClaimFormatError as exc:
        if not os.path.exists(path):
            return
        raise

    if expected_session_id is not None and current.session_id != expected_session_id:
        raise ForeignReleaseError(
            f"refusing to release claim {path!r}: held by session "
            f"{current.session_id!r}, expected {expected_session_id!r}"
        )

    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def refresh_claim_heartbeat(
    handle_or_path: Union[ClaimHandle, PathLike],
    *,
    now: Optional[datetime] = None,
) -> None:
    """Rewrite last_heartbeat on a claim file via atomic write."""
    if isinstance(handle_or_path, ClaimHandle):
        path = handle_or_path.path
        expected_session_id = handle_or_path.claim.session_id
    else:
        path = os.fspath(handle_or_path)
        expected_session_id = None

    current = read_claim(path)
    if expected_session_id is not None and current.session_id != expected_session_id:
        raise ForeignReleaseError(
            f"refusing to refresh claim heartbeat on {path!r}: held by session "
            f"{current.session_id!r}, expected {expected_session_id!r}"
        )

    updated = ClaimRecord(
        claim_id=current.claim_id,
        session_id=current.session_id,
        operation=current.operation,
        scope=current.scope,
        affected_paths=current.affected_paths,
        acquired_at=current.acquired_at,
        last_heartbeat=_iso(now or _utcnow()),
    )
    write_claim(path, updated)
    if isinstance(handle_or_path, ClaimHandle):
        handle_or_path.claim = updated


def claim_work_request(
    do_work_root: PathLike,
    *,
    request_path: PathLike,
    session_id: str,
    operation: str = "work",
    now: Optional[datetime] = None,
) -> WorkClaimHandle:
    """Claim a queued REQ for the work action with an exclusive claim file.

    The exclusive-create of the claim sidecar is the claim point. Once that
    succeeds, the queued REQ is atomically renamed into working/. If the move
    fails, the just-created claim is rolled back before the error is raised.
    """
    root = _coerce_do_work_root(do_work_root)
    queue_request = _work_request_path(root, request_path)
    if queue_request.parent != root:
        raise ConcurrencyError(
            "work claims must start from the do-work/ queue root"
        )

    req_id = _req_identifier_from_path(queue_request)
    working_dir = root / "working"
    os.makedirs(working_dir, exist_ok=True)
    working_request = working_dir / queue_request.name
    claim_path = _work_claim_path(working_request)

    for existing_path in _iter_work_claim_paths(root):
        existing = _inspect_claim_after_contention(os.fspath(existing_path))
        if existing is None:
            continue
        if existing.session_id == session_id and existing.claim_id != req_id:
            raise SessionClaimConflictError(
                f"session {session_id!r} already holds work claim "
                f"{existing.claim_id!r} at {os.fspath(existing_path)!r}"
            )

    now_s = _iso(now or _utcnow())
    claim = ClaimRecord(
        claim_id=req_id,
        session_id=session_id,
        operation=operation,
        scope=f"req-claim:{req_id}",
        affected_paths=(os.fspath(working_request),),
        acquired_at=now_s,
        last_heartbeat=now_s,
    )

    try:
        _write_claim_exclusive(os.fspath(claim_path), claim)
    except FileExistsError:
        holder = _inspect_claim_after_contention(os.fspath(claim_path))
        if holder is None:
            holder = claim
        raise ClaimHeldError(
            path=claim_path,
            claim=holder,
            attempting_session_id=session_id,
            attempting_operation=operation,
        )

    try:
        atomic_rename(
            queue_request,
            working_request,
            transition=f"queue->working for {req_id}",
        )
    except Exception:
        release_claim(claim_path, expected_session_id=session_id)
        raise

    return WorkClaimHandle(
        request_path=os.fspath(working_request),
        claim_path=os.fspath(claim_path),
        claim=claim,
    )


def allocate_ur_input(
    do_work_root: PathLike,
    *,
    session_id: str,
    operation: str,
    content: str,
    now: Optional[datetime] = None,
) -> AllocatedId:
    root = _coerce_do_work_root(do_work_root)
    lock_path = _id_lock_path(root, "ur")
    handle = _acquire_id_allocation_lock(
        root,
        namespace="ur",
        session_id=session_id,
        operation=operation,
        now=now,
    )
    try:
        number = _next_identifier_number(root, "ur")
        identifier = _format_identifier("ur", number)
        conflict = _find_conflicting_identifier_path(root, "ur", identifier)
        if conflict is not None:
            raise CollisionError(
                f"allocate_ur_input: next id {identifier} already exists at "
                f"{os.fspath(conflict)!r}"
            )

        ur_dir = root / "user-requests" / identifier
        input_path = ur_dir / "input.md"
        try:
            os.makedirs(ur_dir, exist_ok=False)
        except FileExistsError as exc:
            raise CollisionError(
                f"allocate_ur_input: destination directory {os.fspath(ur_dir)!r} "
                "already exists"
            ) from exc

        try:
            _write_new_file(
                input_path,
                content,
                transition=f"allocate input for {identifier}",
            )
        except Exception:
            try:
                os.rmdir(ur_dir)
            except OSError:
                pass
            raise

        return AllocatedId(
            identifier=identifier,
            number=number,
            namespace="ur",
            path=os.fspath(input_path),
            lock_path=os.fspath(lock_path),
        )
    finally:
        release_lock(handle)


def allocate_req_file(
    do_work_root: PathLike,
    *,
    session_id: str,
    operation: str,
    slug: str,
    content: str,
    now: Optional[datetime] = None,
) -> AllocatedId:
    root = _coerce_do_work_root(do_work_root)
    lock_path = _id_lock_path(root, "req")
    handle = _acquire_id_allocation_lock(
        root,
        namespace="req",
        session_id=session_id,
        operation=operation,
        now=now,
    )
    try:
        number = _next_identifier_number(root, "req")
        identifier = _format_identifier("req", number)
        conflict = _find_conflicting_identifier_path(root, "req", identifier)
        if conflict is not None:
            raise CollisionError(
                f"allocate_req_file: next id {identifier} already exists at "
                f"{os.fspath(conflict)!r}"
            )

        target = root / f"{identifier}-{slug}.md"
        _write_new_file(
            target,
            content,
            transition=f"allocate queue file for {identifier}",
        )
        return AllocatedId(
            identifier=identifier,
            number=number,
            namespace="req",
            path=os.fspath(target),
            lock_path=os.fspath(lock_path),
        )
    finally:
        release_lock(handle)


__all__ = [
    "CANONICAL_SCOPES",
    "PARAMETERIZED_SCOPE_PREFIXES",
    "SCOPES",
    "ConcurrencyError",
    "LockHeldError",
    "ClaimFormatError",
    "ClaimHeldError",
    "SessionClaimConflictError",
    "ScopeError",
    "StaleRenameError",
    "CrossDeviceError",
    "CollisionError",
    "AtomicWriteError",
    "ForeignReleaseError",
    "AllocatedId",
    "LockInfo",
    "LockHandle",
    "ClaimHandle",
    "WorkClaimHandle",
    "ClaimRecord",
    "validate_scope",
    "acquire_lock",
    "release_lock",
    "inspect_lock",
    "refresh_heartbeat",
    "classify_lock",
    "atomic_rename",
    "atomic_write",
    "read_claim",
    "write_claim",
    "release_claim",
    "refresh_claim_heartbeat",
    "claim_work_request",
    "allocate_ur_input",
    "allocate_req_file",
]
