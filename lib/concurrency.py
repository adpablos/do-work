"""Shared concurrency primitives for the do-work skill.

See actions/concurrency-primitives.md for the full contract. This module ships
the executable primitives; every other REQ in the parallel-safety batch
(REQ-003..REQ-010) consumes them.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union

PathLike = Union[str, os.PathLike]

CANONICAL_SCOPES: frozenset[str] = frozenset({
    "capture-global",
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


class CaptureFormatError(ConcurrencyError):
    """Raised when a capture manifest fails to parse or is missing fields."""


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


class SessionFormatError(ConcurrencyError):
    """Raised when a session record fails to parse or is missing fields."""


class RecoveryNotAllowedError(ConcurrencyError):
    """Raised when explicit orphan recovery is attempted without clear evidence."""


class TreeStateViolationError(ConcurrencyError):
    """Raised when the claim-time or commit-time tree-state contract is violated."""


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
        ".capture-staging/*/reqs/REQ-*.md",
    )
    for pattern in patterns:
        yield from root.glob(pattern)


def _iter_authoritative_ur_paths(do_work_root: PathLike) -> Iterable[Path]:
    root = _coerce_do_work_root(do_work_root)
    patterns = (
        "user-requests/UR-*",
        "archive/UR-*",
        ".capture-staging/*/user-requests/UR-*",
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
class CleanupClaimRecord:
    session_id: str
    started_at: str
    last_heartbeat: str
    operation: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupClaimRecord":
        required = {"session_id", "started_at", "last_heartbeat", "operation"}
        missing = required - set(d)
        if missing:
            raise ClaimFormatError(
                f"cleanup claim missing fields: {sorted(missing)}"
            )
        operation = d["operation"]
        if operation != "cleanup":
            raise ClaimFormatError(
                f"cleanup claim operation must be 'cleanup', got {operation!r}"
            )
        return cls(
            session_id=d["session_id"],
            started_at=d["started_at"],
            last_heartbeat=d["last_heartbeat"],
            operation=operation,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CleanupClaimHandle:
    lock: LockHandle
    path: str
    claim: CleanupClaimRecord


@dataclass(frozen=True)
class ClaimFileFingerprint:
    path: str
    sha256: Optional[str]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClaimFileFingerprint":
        required = {"path", "sha256"}
        missing = required - set(d)
        if missing:
            raise ClaimFormatError(
                f"claim file fingerprint missing fields: {sorted(missing)}"
            )
        return cls(path=d["path"], sha256=d["sha256"])

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class ClaimTreeState:
    repo_root: str
    head_sha: str
    captured_at: str
    preexisting_dirty_paths: tuple[str, ...]
    scope_paths: tuple[str, ...]
    scope_fingerprints: tuple[ClaimFileFingerprint, ...]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClaimTreeState":
        required = {
            "repo_root",
            "head_sha",
            "captured_at",
            "preexisting_dirty_paths",
            "scope_paths",
            "scope_fingerprints",
        }
        missing = required - set(d)
        if missing:
            raise ClaimFormatError(
                f"claim tree_state missing fields: {sorted(missing)}"
            )
        return cls(
            repo_root=d["repo_root"],
            head_sha=d["head_sha"],
            captured_at=d["captured_at"],
            preexisting_dirty_paths=tuple(d["preexisting_dirty_paths"]),
            scope_paths=tuple(d["scope_paths"]),
            scope_fingerprints=tuple(
                ClaimFileFingerprint.from_dict(item)
                for item in d["scope_fingerprints"]
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "head_sha": self.head_sha,
            "captured_at": self.captured_at,
            "preexisting_dirty_paths": list(self.preexisting_dirty_paths),
            "scope_paths": list(self.scope_paths),
            "scope_fingerprints": [
                fingerprint.to_dict()
                for fingerprint in self.scope_fingerprints
            ],
        }


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    session_id: str
    operation: str
    scope: str
    affected_paths: tuple[str, ...]
    acquired_at: str
    last_heartbeat: str
    tree_state: Optional[ClaimTreeState] = None

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
            tree_state=(
                None
                if d.get("tree_state") is None
                else ClaimTreeState.from_dict(d["tree_state"])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["affected_paths"] = list(self.affected_paths)
        if self.tree_state is None:
            d["tree_state"] = None
        else:
            d["tree_state"] = self.tree_state.to_dict()
        return d


@dataclass(frozen=True)
class ScopedStageResult:
    claim_id: str
    head_sha: str
    staged_paths: tuple[str, ...]


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    hostname: str
    pid: int
    started_at: str
    last_heartbeat: str
    operation: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRecord":
        required = {
            "session_id", "hostname", "pid",
            "started_at", "last_heartbeat", "operation",
        }
        missing = required - set(d)
        if missing:
            raise SessionFormatError(
                f"session record missing fields: {sorted(missing)}"
            )
        return cls(**{k: d[k] for k in required})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkClaimRecoveryInspection:
    claim_path: str
    request_path: str
    queue_path: str
    session_record_path: str
    claim: ClaimRecord
    session_record: Optional[SessionRecord]
    verdict: Literal["live", "stale", "recoverable", "foreign-host", "missing-session-record"]
    reason: str
    claim_heartbeat_stale: bool
    session_heartbeat_stale: Optional[bool]
    process_alive: Optional[bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_path": self.claim_path,
            "request_path": self.request_path,
            "queue_path": self.queue_path,
            "session_record_path": self.session_record_path,
            "claim": self.claim.to_dict(),
            "session_record": (
                None if self.session_record is None else self.session_record.to_dict()
            ),
            "verdict": self.verdict,
            "reason": self.reason,
            "claim_heartbeat_stale": self.claim_heartbeat_stale,
            "session_heartbeat_stale": self.session_heartbeat_stale,
            "process_alive": self.process_alive,
        }


@dataclass(frozen=True)
class RecoveredWorkClaim:
    claim_id: str
    released_session_id: str
    recovered_by_session_id: str
    recovered_at: str
    claim_path: str
    working_request_path: str
    queue_request_path: str
    log_path: str
    claim_last_heartbeat: str
    session_last_heartbeat: str
    session_pid: int
    session_hostname: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParentArchivalResult:
    kind: Literal["user-request", "legacy-context"]
    identifier: str
    outcome: Literal["not-ready", "archived", "already-archived"]
    archive_path: str
    missing_request_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RequestArchivalResult:
    request_id: str
    outcome: Literal["archived-root", "archived-parent"]
    request_path: str
    parent_result: Optional[ParentArchivalResult]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.parent_result is not None:
            data["parent_result"] = self.parent_result.to_dict()
        return data


@dataclass(frozen=True)
class CaptureItem:
    kind: Literal["ur-dir", "req"]
    identifier: str
    staged_path: str
    final_path: str
    state: Literal["staged", "published"] = "staged"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CaptureItem":
        required = {"kind", "identifier", "staged_path", "final_path", "state"}
        missing = required - set(d)
        if missing:
            raise CaptureFormatError(
                f"capture item missing fields: {sorted(missing)}"
            )
        kind = d["kind"]
        state = d["state"]
        if kind not in {"ur-dir", "req"}:
            raise CaptureFormatError(
                f"capture item kind must be 'ur-dir' or 'req', got {kind!r}"
            )
        if state not in {"staged", "published"}:
            raise CaptureFormatError(
                f"capture item state must be 'staged' or 'published', got {state!r}"
            )
        return cls(
            kind=kind,
            identifier=d["identifier"],
            staged_path=d["staged_path"],
            final_path=d["final_path"],
            state=state,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CaptureManifest:
    capture_id: str
    session_id: str
    operation: str
    created_at: str
    updated_at: str
    status: Literal["staging", "failed", "committing", "committed"]
    preserve_verbatim_input_on_failure: bool
    failure_reason: Optional[str]
    items: tuple[CaptureItem, ...]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CaptureManifest":
        required = {
            "capture_id",
            "session_id",
            "operation",
            "created_at",
            "updated_at",
            "status",
            "preserve_verbatim_input_on_failure",
            "failure_reason",
            "items",
        }
        missing = required - set(d)
        if missing:
            raise CaptureFormatError(
                f"capture manifest missing fields: {sorted(missing)}"
            )
        status = d["status"]
        if status not in {"staging", "failed", "committing", "committed"}:
            raise CaptureFormatError(
                "capture manifest status must be one of "
                "'staging', 'failed', 'committing', 'committed'"
            )
        return cls(
            capture_id=d["capture_id"],
            session_id=d["session_id"],
            operation=d["operation"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            status=status,
            preserve_verbatim_input_on_failure=bool(
                d["preserve_verbatim_input_on_failure"]
            ),
            failure_reason=d["failure_reason"],
            items=tuple(CaptureItem.from_dict(item) for item in d["items"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "session_id": self.session_id,
            "operation": self.operation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "preserve_verbatim_input_on_failure": self.preserve_verbatim_input_on_failure,
            "failure_reason": self.failure_reason,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class CaptureTransaction:
    lock: LockHandle
    manifest_path: str
    manifest: CaptureManifest


@dataclass(frozen=True)
class CaptureRepairResult:
    capture_id: Optional[str]
    outcome: Literal[
        "noop",
        "discarded-draft",
        "resumed-commit",
        "cleaned-committed",
    ]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _capture_lock_path(do_work_root: PathLike) -> Path:
    root = _coerce_do_work_root(do_work_root)
    return root / ".locks" / "capture-global.lock"


def _capture_staging_root(do_work_root: PathLike) -> Path:
    root = _coerce_do_work_root(do_work_root)
    return root / ".capture-staging"


def _capture_stage_dir(do_work_root: PathLike, capture_id: str) -> Path:
    return _capture_staging_root(do_work_root) / capture_id


def _capture_manifest_path(stage_dir: PathLike) -> Path:
    stage = Path(os.fspath(stage_dir))
    return stage / "manifest.json"


def _iter_capture_stage_dirs(do_work_root: PathLike) -> Iterable[Path]:
    staging_root = _capture_staging_root(do_work_root)
    if not staging_root.exists():
        return ()
    return sorted(
        path for path in staging_root.iterdir()
        if path.is_dir()
    )


def _read_capture_manifest(path: PathLike) -> CaptureManifest:
    raw = Path(os.fspath(path)).read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaptureFormatError(
            f"capture manifest {os.fspath(path)!r} contains invalid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise CaptureFormatError(
            f"capture manifest {os.fspath(path)!r} must be a JSON object"
        )
    return CaptureManifest.from_dict(parsed)


def _write_capture_manifest(path: PathLike, manifest: CaptureManifest) -> None:
    atomic_write(
        path,
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
    )


def _update_capture_manifest(
    handle: CaptureTransaction,
    manifest: CaptureManifest,
) -> CaptureManifest:
    _write_capture_manifest(handle.manifest_path, manifest)
    handle.manifest = manifest
    return manifest


def _capture_item_source_path(item: CaptureItem) -> Path:
    return Path(item.staged_path if item.state == "staged" else item.final_path)


def _capture_item_exists(item: CaptureItem) -> tuple[bool, bool]:
    source = Path(item.staged_path)
    final = Path(item.final_path)
    return source.exists(), final.exists()


def _cleanup_capture_stage_dir(stage_dir: PathLike) -> None:
    stage_path = Path(os.fspath(stage_dir))
    staging_root = stage_path.parent
    shutil.rmtree(stage_path)
    try:
        staging_root.rmdir()
    except OSError:
        pass


def _capture_commit_ready_manifest(
    manifest: CaptureManifest,
) -> tuple[CaptureItem, tuple[CaptureItem, ...]]:
    ur_items = tuple(item for item in manifest.items if item.kind == "ur-dir")
    req_items = tuple(item for item in manifest.items if item.kind == "req")
    if len(ur_items) != 1:
        raise ConcurrencyError(
            f"capture {manifest.capture_id} must stage exactly one UR folder "
            f"before commit; found {len(ur_items)}"
        )
    if not req_items:
        raise ConcurrencyError(
            f"capture {manifest.capture_id} staged no REQ files"
        )
    return ur_items[0], req_items


def _validate_capture_commit(manifest: CaptureManifest) -> None:
    ur_item, req_items = _capture_commit_ready_manifest(manifest)
    ur_staged_exists, ur_final_exists = _capture_item_exists(ur_item)
    if ur_staged_exists and ur_final_exists:
        raise ConcurrencyError(
            f"capture {manifest.capture_id} found both staged and final UR copies for "
            f"{ur_item.identifier}; refusing ambiguous commit state"
        )
    if not ur_staged_exists and not ur_final_exists:
        raise ConcurrencyError(
            f"capture {manifest.capture_id} lost both staged and final UR copies for "
            f"{ur_item.identifier}; repair required"
        )
    ur_dir = Path(ur_item.staged_path if ur_staged_exists else ur_item.final_path)
    input_path = ur_dir / "input.md"
    if not input_path.exists():
        raise ConcurrencyError(
            f"capture {manifest.capture_id} cannot commit: expected "
            f"{os.fspath(input_path)!r} to exist"
        )

    request_ids = tuple(_parse_ur_requests(input_path))
    expected_request_ids = tuple(item.identifier for item in req_items)
    if tuple(sorted(request_ids)) != tuple(sorted(expected_request_ids)):
        raise ConcurrencyError(
            f"capture {manifest.capture_id} cannot commit: UR requests array "
            f"{list(request_ids)!r} does not match staged REQs "
            f"{list(expected_request_ids)!r}"
        )

    for req_item in req_items:
        staged_exists, final_exists = _capture_item_exists(req_item)
        if staged_exists and final_exists:
            raise ConcurrencyError(
                f"capture {manifest.capture_id} found both staged and final copies for "
                f"{req_item.identifier}; refusing ambiguous commit state"
            )
        if not staged_exists and not final_exists:
            raise ConcurrencyError(
                f"capture {manifest.capture_id} lost both staged and final copies for "
                f"{req_item.identifier}; repair required"
            )
        req_path = Path(
            req_item.staged_path if staged_exists else req_item.final_path
        )
        if not req_path.exists():
            raise ConcurrencyError(
                f"capture {manifest.capture_id} cannot commit: expected "
                f"{os.fspath(req_path)!r} to exist"
            )
        frontmatter = _read_frontmatter(req_path)
        if frontmatter.get("user_request") != ur_item.identifier:
            raise ConcurrencyError(
                f"capture {manifest.capture_id} cannot commit: "
                f"{os.fspath(req_path)!r} has user_request "
                f"{frontmatter.get('user_request')!r}, expected {ur_item.identifier!r}"
            )
        req_text = req_path.read_text(encoding="utf-8")
        if "## Verification" not in req_text:
            raise ConcurrencyError(
                f"capture {manifest.capture_id} cannot commit: "
                f"{os.fspath(req_path)!r} is missing its Verification section"
            )

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


def _write_cleanup_claim_exclusive(path: str, claim: CleanupClaimRecord) -> None:
    """Create path with O_CREAT|O_EXCL and write the cleanup-claim JSON."""
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


def _cleanup_lock_path(do_work_root: PathLike) -> Path:
    root = _coerce_do_work_root(do_work_root)
    return root / ".locks" / "cleanup-global.lock"


def _cleanup_claim_path(do_work_root: PathLike) -> Path:
    root = _coerce_do_work_root(do_work_root)
    return root / ".claims" / "cleanup.claim.json"


def _session_record_path(do_work_root: PathLike, session_id: str) -> Path:
    root = _coerce_do_work_root(do_work_root)
    return root / ".sessions" / f"{session_id}.json"


def _verification_target_path(do_work_root: PathLike, target_path: PathLike) -> Path:
    root = _coerce_do_work_root(do_work_root)
    target = Path(os.fspath(target_path))
    if target.is_absolute():
        return target
    return root / target


def _verification_lock_identifier(target_path: Path) -> str:
    req_number = _extract_identifier_number(target_path, "req")
    if req_number is not None:
        return _format_identifier("req", req_number)

    if target_path.name == "input.md":
        ur_number = _extract_identifier_number(target_path.parent, "ur")
        if ur_number is not None:
            return _format_identifier("ur", ur_number)

    digest = hashlib.sha256(os.fspath(target_path).encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", target_path.stem).strip("-")
    if stem:
        return f"{stem}-{digest}"
    return f"doc-{digest}"


def _verification_scope(do_work_root: PathLike, target_path: Path) -> str:
    root = _coerce_do_work_root(do_work_root)
    try:
        scope_target = os.fspath(target_path.relative_to(root))
    except ValueError:
        scope_target = os.fspath(target_path)
    return f"verify-doc:{scope_target}"


def verification_lock_path(do_work_root: PathLike, *, target_path: PathLike) -> Path:
    root = _coerce_do_work_root(do_work_root)
    target = _verification_target_path(root, target_path)
    identifier = _verification_lock_identifier(target)
    return root / ".locks" / f"verify-{identifier}.lock"


def acquire_verification_lock(
    do_work_root: PathLike,
    *,
    target_path: PathLike,
    session_id: str,
    operation: str,
    now: Optional[datetime] = None,
) -> LockHandle:
    if operation not in {"verify-request", "verify-plan"}:
        raise ConcurrencyError(
            "verification lock operation must be 'verify-request' or "
            f"'verify-plan', got {operation!r}"
        )

    root = _coerce_do_work_root(do_work_root)
    target = _verification_target_path(root, target_path)
    return acquire_lock(
        verification_lock_path(root, target_path=target),
        session_id=session_id,
        operation=operation,
        scope=_verification_scope(root, target),
        now=now,
    )


def replace_markdown_section(
    document: str,
    *,
    heading: str,
    new_section: str,
) -> str:
    expected_heading = f"## {heading}"
    normalized = new_section.rstrip("\n") + "\n"
    if not normalized.startswith(expected_heading):
        raise ConcurrencyError(
            f"replacement section must start with {expected_heading!r}"
        )

    pattern = re.compile(
        rf"(?ms)^## {re.escape(heading)}\n.*?(?=^## |\Z)"
    )
    updated, count = pattern.subn(normalized, document, count=1)
    if count:
        return updated

    stripped = document.rstrip("\n")
    if not stripped:
        return normalized
    return stripped + "\n\n" + normalized


def rewrite_markdown_section_atomic(
    path: PathLike,
    *,
    heading: str,
    new_section: str,
) -> str:
    target = Path(os.fspath(path))
    current = target.read_text(encoding="utf-8")
    updated = replace_markdown_section(
        current,
        heading=heading,
        new_section=new_section,
    )
    atomic_write(target, updated)
    return updated


def _coerce_repo_root(path: PathLike) -> Path:
    return Path(os.fspath(path)).resolve()


def _discover_repo_root(start_path: PathLike) -> Optional[Path]:
    start = Path(os.fspath(start_path))
    if start.is_file():
        start = start.parent
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.fspath(start),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    resolved = proc.stdout.strip()
    if not resolved:
        return None
    return Path(resolved).resolve()


def _run_git(repo_root: PathLike, *args: str) -> str:
    root = _coerce_repo_root(repo_root)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=os.fspath(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ConcurrencyError(
            f"failed to run git {' '.join(args)!r} from {os.fspath(root)!r}: {exc}"
        ) from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise ConcurrencyError(
            f"git {' '.join(args)!r} failed in {os.fspath(root)!r}: "
            f"{stderr or 'unknown git error'}"
        )
    return proc.stdout


def _git_head(repo_root: PathLike) -> str:
    return _run_git(repo_root, "rev-parse", "HEAD").strip()


def _normalize_repo_relative_path(repo_root: PathLike, path: PathLike) -> str:
    root = _coerce_repo_root(repo_root)
    candidate = Path(os.fspath(path))
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise ConcurrencyError(
                f"path {os.fspath(path)!r} is outside repo root {os.fspath(root)!r}"
            ) from exc
    return candidate.as_posix()


def _git_dirty_paths(repo_root: PathLike) -> tuple[str, ...]:
    tracked = {
        line.strip()
        for line in _run_git(repo_root, "diff", "--name-only", "HEAD", "--").splitlines()
        if line.strip()
    }
    untracked = {
        line.strip()
        for line in _run_git(
            repo_root,
            "ls-files",
            "--others",
            "--exclude-standard",
        ).splitlines()
        if line.strip()
    }
    return tuple(sorted(tracked | untracked))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_scope_fingerprints(
    repo_root: PathLike,
    scope_paths: Iterable[PathLike],
) -> tuple[ClaimFileFingerprint, ...]:
    root = _coerce_repo_root(repo_root)
    normalized = sorted(
        {
            _normalize_repo_relative_path(root, path)
            for path in scope_paths
        }
    )
    fingerprints: list[ClaimFileFingerprint] = []
    for relative_path in normalized:
        target = root / relative_path
        sha256: Optional[str]
        if target.exists():
            if not target.is_file():
                raise ConcurrencyError(
                    f"claim scope path {relative_path!r} exists but is not a file"
                )
            sha256 = _sha256_file(target)
        else:
            sha256 = None
        fingerprints.append(
            ClaimFileFingerprint(path=relative_path, sha256=sha256)
        )
    return tuple(fingerprints)


def _build_claim_tree_state(
    repo_root: PathLike,
    *,
    captured_at: str,
    preexisting_dirty_paths: Iterable[str],
    scope_paths: Iterable[PathLike],
    head_sha: Optional[str] = None,
) -> ClaimTreeState:
    root = _coerce_repo_root(repo_root)
    normalized_dirty = tuple(
        sorted(
            {
                _normalize_repo_relative_path(root, path)
                for path in preexisting_dirty_paths
            }
        )
    )
    scope_fingerprints = _capture_scope_fingerprints(root, scope_paths)
    normalized_scope = tuple(
        fingerprint.path
        for fingerprint in scope_fingerprints
    )
    return ClaimTreeState(
        repo_root=os.fspath(root),
        head_sha=head_sha or _git_head(root),
        captured_at=captured_at,
        preexisting_dirty_paths=normalized_dirty,
        scope_paths=normalized_scope,
        scope_fingerprints=scope_fingerprints,
    )


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def _parse_frontmatter_value(raw: str) -> Any:
    value = raw.strip()
    if value == "":
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [
            item.strip().strip("'\"")
            for item in inner.split(",")
            if item.strip()
        ]
    if value.lower() == "null":
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value.strip("'\"")


def _read_frontmatter(path: PathLike) -> dict[str, Any]:
    target = Path(os.fspath(path))
    text = target.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ConcurrencyError(
            f"expected YAML frontmatter at {os.fspath(target)!r}"
        )

    frontmatter: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        frontmatter[key.strip()] = _parse_frontmatter_value(raw_value)
    return frontmatter


def _archival_lock_path(do_work_root: PathLike, identifier: str) -> Path:
    root = _coerce_do_work_root(do_work_root)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", identifier)
    return root / ".locks" / f"ur-archival-{slug}.lock"


def _acquire_archival_lock(
    do_work_root: PathLike,
    *,
    identifier: str,
    session_id: str,
    operation: str,
    now: Optional[datetime] = None,
    timeout_seconds: float = 5.0,
    retry_interval_seconds: float = 0.05,
) -> LockHandle:
    lock_path = _archival_lock_path(do_work_root, identifier)
    deadline = time.monotonic() + timeout_seconds
    scope = f"ur-archival:{identifier}"

    while True:
        try:
            return acquire_lock(
                lock_path,
                session_id=session_id,
                operation=operation,
                scope=scope,
                now=now,
            )
        except LockHeldError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(retry_interval_seconds)


def _request_candidates(root: Path, *patterns: str) -> list[Path]:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(root.glob(pattern))
    return sorted(candidates)


def _find_unique_request_path(
    root: Path,
    req_id: str,
    *,
    patterns: tuple[str, ...],
    label: str,
) -> Optional[Path]:
    matches = _request_candidates(root, *[pattern.format(req_id=req_id) for pattern in patterns])
    if len(matches) > 1:
        raise ConcurrencyError(
            f"refusing to continue: {req_id} appears multiple times in {label}: "
            f"{', '.join(os.fspath(path) for path in matches)}"
        )
    return matches[0] if matches else None


def _parse_ur_requests(input_path: Path) -> tuple[str, ...]:
    frontmatter = _read_frontmatter(input_path)
    raw_requests = frontmatter.get("requests")
    if not isinstance(raw_requests, list):
        raise ConcurrencyError(
            f"expected requests array in {os.fspath(input_path)!r}"
        )
    requests = tuple(str(item) for item in raw_requests)
    if not requests:
        raise ConcurrencyError(
            f"expected at least one REQ in {os.fspath(input_path)!r}"
        )
    return requests


def _resolve_context_path(do_work_root: PathLike, context_ref: str) -> Path:
    root = _coerce_do_work_root(do_work_root)
    return _work_request_path(root, context_ref)


def _recovery_log_path(
    do_work_root: PathLike,
    *,
    claim_id: str,
    recovered_at: str,
) -> Path:
    root = _coerce_do_work_root(do_work_root)
    stamp = recovered_at.replace(":", "-")
    return root / ".recovery-log" / f"{stamp}-{claim_id}.json"


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


def read_session_record(path: PathLike) -> SessionRecord:
    target = os.fspath(path)
    try:
        with open(target, "r") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise SessionFormatError(f"session record {target!r} not found") from e
    except json.JSONDecodeError as e:
        raise SessionFormatError(
            f"session record {target!r} is not valid JSON: {e}"
        ) from e
    return SessionRecord.from_dict(data)


def write_session_record(path: PathLike, record: SessionRecord) -> None:
    atomic_write(
        path,
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
    )


def inspect_session_record(
    do_work_root: PathLike,
    session_id: str,
) -> Optional[SessionRecord]:
    path = _session_record_path(do_work_root, session_id)
    try:
        return read_session_record(path)
    except SessionFormatError as exc:
        if not os.path.exists(path):
            return None
        raise


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


def read_cleanup_claim(path: PathLike) -> CleanupClaimRecord:
    target = os.fspath(path)
    try:
        with open(target, "r") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise ClaimFormatError(f"cleanup claim file {target!r} not found") from e
    except json.JSONDecodeError as e:
        raise ClaimFormatError(
            f"cleanup claim file {target!r} is not valid JSON: {e}"
        ) from e
    return CleanupClaimRecord.from_dict(data)


def write_cleanup_claim(path: PathLike, claim: CleanupClaimRecord) -> None:
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


def release_cleanup_claim(
    handle_or_path: Union[CleanupClaimHandle, PathLike],
    *,
    expected_session_id: Optional[str] = None,
) -> None:
    """Remove a cleanup claim file, optionally asserting ownership."""
    if isinstance(handle_or_path, CleanupClaimHandle):
        path = handle_or_path.path
        if expected_session_id is None:
            expected_session_id = handle_or_path.claim.session_id
    else:
        path = os.fspath(handle_or_path)

    try:
        current = read_cleanup_claim(path)
    except ClaimFormatError:
        if not os.path.exists(path):
            return
        raise

    if expected_session_id is not None and current.session_id != expected_session_id:
        raise ForeignReleaseError(
            f"refusing to release cleanup claim {path!r}: held by session "
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
        tree_state=current.tree_state,
    )
    write_claim(path, updated)
    if isinstance(handle_or_path, ClaimHandle):
        handle_or_path.claim = updated


def refresh_cleanup_claim_heartbeat(
    handle_or_path: Union[CleanupClaimHandle, PathLike],
    *,
    now: Optional[datetime] = None,
) -> None:
    """Rewrite last_heartbeat on a cleanup claim file via atomic write."""
    if isinstance(handle_or_path, CleanupClaimHandle):
        path = handle_or_path.path
        expected_session_id = handle_or_path.claim.session_id
    else:
        path = os.fspath(handle_or_path)
        expected_session_id = None

    current = read_cleanup_claim(path)
    if expected_session_id is not None and current.session_id != expected_session_id:
        raise ForeignReleaseError(
            f"refusing to refresh cleanup claim heartbeat on {path!r}: held by "
            f"session {current.session_id!r}, expected {expected_session_id!r}"
        )

    updated = CleanupClaimRecord(
        session_id=current.session_id,
        started_at=current.started_at,
        last_heartbeat=_iso(now or _utcnow()),
        operation=current.operation,
    )
    write_cleanup_claim(path, updated)
    if isinstance(handle_or_path, CleanupClaimHandle):
        handle_or_path.claim = updated


def claim_cleanup(
    do_work_root: PathLike,
    *,
    session_id: str,
    operation: str = "cleanup",
    now: Optional[datetime] = None,
) -> CleanupClaimHandle:
    """Claim the global cleanup run with a short lock plus a heartbeat file."""
    if operation != "cleanup":
        raise ConcurrencyError(
            f"cleanup claim operation must be 'cleanup', got {operation!r}"
        )

    root = _coerce_do_work_root(do_work_root)
    lock = acquire_lock(
        _cleanup_lock_path(root),
        session_id=session_id,
        operation=operation,
        scope="cleanup-global",
        now=now,
    )
    claim_path = _cleanup_claim_path(root)
    now_s = _iso(now or _utcnow())
    claim = CleanupClaimRecord(
        session_id=session_id,
        started_at=now_s,
        last_heartbeat=now_s,
        operation=operation,
    )
    try:
        _write_cleanup_claim_exclusive(os.fspath(claim_path), claim)
    except FileExistsError:
        try:
            existing = read_cleanup_claim(claim_path)
            message = (
                f"cleanup claim {os.fspath(claim_path)!r} already exists\n"
                f"  held_by:        session {existing.session_id} "
                f"(operation: {existing.operation})\n"
                f"  started_at:     {existing.started_at}\n"
                f"  last_heartbeat: {existing.last_heartbeat}\n"
                "  remediation:    fail loud and recover explicitly via the "
                "REQ-005 evidence path before retrying."
            )
        except ClaimFormatError as exc:
            message = (
                f"cleanup claim {os.fspath(claim_path)!r} already exists but could "
                f"not be read cleanly: {exc}\n"
                "  remediation:    fail loud and recover explicitly via the "
                "REQ-005 evidence path before retrying."
            )
        release_lock(lock)
        raise ConcurrencyError(message)
    except Exception:
        release_lock(lock)
        raise

    return CleanupClaimHandle(
        lock=lock,
        path=os.fspath(claim_path),
        claim=claim,
    )


def refresh_cleanup_heartbeat(
    handle: CleanupClaimHandle,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Refresh the cleanup lockfile and cleanup claim with the same timestamp."""
    refreshed_at = now or _utcnow()
    refresh_heartbeat(handle.lock, now=refreshed_at)
    refresh_cleanup_claim_heartbeat(handle, now=refreshed_at)


def release_cleanup(
    handle: CleanupClaimHandle,
    *,
    expected_session_id: Optional[str] = None,
) -> None:
    """Release the cleanup claim first, then drop the global cleanup lock."""
    release_cleanup_claim(handle, expected_session_id=expected_session_id)
    release_lock(handle.lock)


def claim_work_request(
    do_work_root: PathLike,
    *,
    request_path: PathLike,
    session_id: str,
    operation: str = "work",
    repo_root: Optional[PathLike] = None,
    refuse_dirty_tree: bool = True,
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
    repo_root_path = (
        _coerce_repo_root(repo_root)
        if repo_root is not None
        else _discover_repo_root(root.parent)
    )
    claim_time_dirty_paths: tuple[str, ...] = ()
    claim_time_head_sha: Optional[str] = None

    if repo_root_path is not None:
        claim_time_dirty_paths = _git_dirty_paths(repo_root_path)
        claim_time_head_sha = _git_head(repo_root_path)
        if claim_time_dirty_paths and refuse_dirty_tree:
            dirty_display = ", ".join(claim_time_dirty_paths)
            raise TreeStateViolationError(
                f"refusing to claim {req_id}: git working tree is already dirty\n"
                f"  session:      {session_id} (operation: {operation})\n"
                f"  dirty_paths:  {dirty_display}\n"
                "  remediation:  clean, stash, or commit the existing work before "
                "claiming this REQ."
            )

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

    if repo_root_path is not None:
        tree_state = _build_claim_tree_state(
            repo_root_path,
            captured_at=now_s,
            preexisting_dirty_paths=claim_time_dirty_paths,
            scope_paths=(working_request,),
            head_sha=claim_time_head_sha,
        )
        claim = ClaimRecord(
            claim_id=claim.claim_id,
            session_id=claim.session_id,
            operation=claim.operation,
            scope=claim.scope,
            affected_paths=claim.affected_paths,
            acquired_at=claim.acquired_at,
            last_heartbeat=claim.last_heartbeat,
            tree_state=tree_state,
        )
        write_claim(claim_path, claim)

    return WorkClaimHandle(
        request_path=os.fspath(working_request),
        claim_path=os.fspath(claim_path),
        claim=claim,
    )


def capture_claim_tree_state(
    repo_root: PathLike,
    *,
    claim_handle_or_path: Union[WorkClaimHandle, ClaimHandle, PathLike],
    scope_paths: Iterable[PathLike],
    expected_session_id: Optional[str] = None,
) -> ClaimRecord:
    if isinstance(claim_handle_or_path, WorkClaimHandle):
        claim_path = claim_handle_or_path.claim_path
        if expected_session_id is None:
            expected_session_id = claim_handle_or_path.claim.session_id
    elif isinstance(claim_handle_or_path, ClaimHandle):
        claim_path = claim_handle_or_path.path
        if expected_session_id is None:
            expected_session_id = claim_handle_or_path.claim.session_id
    else:
        claim_path = os.fspath(claim_handle_or_path)

    current = read_claim(claim_path)
    if expected_session_id is not None and current.session_id != expected_session_id:
        raise ForeignReleaseError(
            f"refusing to update claim tree state on {claim_path!r}: held by session "
            f"{current.session_id!r}, expected {expected_session_id!r}"
        )

    existing_tree_state = current.tree_state
    if existing_tree_state is None:
        raise TreeStateViolationError(
            f"claim {claim_path!r} has no tree_state snapshot to update"
        )

    updated_tree_state = _build_claim_tree_state(
        repo_root,
        captured_at=existing_tree_state.captured_at,
        preexisting_dirty_paths=existing_tree_state.preexisting_dirty_paths,
        scope_paths=scope_paths,
        head_sha=existing_tree_state.head_sha,
    )
    updated = ClaimRecord(
        claim_id=current.claim_id,
        session_id=current.session_id,
        operation=current.operation,
        scope=current.scope,
        affected_paths=current.affected_paths,
        acquired_at=current.acquired_at,
        last_heartbeat=current.last_heartbeat,
        tree_state=updated_tree_state,
    )
    write_claim(claim_path, updated)
    if isinstance(claim_handle_or_path, WorkClaimHandle):
        claim_handle_or_path.claim = updated
    elif isinstance(claim_handle_or_path, ClaimHandle):
        claim_handle_or_path.claim = updated
    return updated


def verify_and_stage_claim_scope(
    repo_root: PathLike,
    *,
    claim_handle_or_path: Union[WorkClaimHandle, ClaimHandle, PathLike],
    current_request_path: PathLike,
    expected_session_id: Optional[str] = None,
    now: Optional[datetime] = None,
    stale_threshold: timedelta = timedelta(minutes=2),
) -> ScopedStageResult:
    if isinstance(claim_handle_or_path, WorkClaimHandle):
        claim_path = claim_handle_or_path.claim_path
        if expected_session_id is None:
            expected_session_id = claim_handle_or_path.claim.session_id
    elif isinstance(claim_handle_or_path, ClaimHandle):
        claim_path = claim_handle_or_path.path
        if expected_session_id is None:
            expected_session_id = claim_handle_or_path.claim.session_id
    else:
        claim_path = os.fspath(claim_handle_or_path)

    claim = read_claim(claim_path)
    if expected_session_id is not None and claim.session_id != expected_session_id:
        raise ForeignReleaseError(
            f"refusing to verify commit scope for {claim_path!r}: held by session "
            f"{claim.session_id!r}, expected {expected_session_id!r}"
        )

    tree_state = claim.tree_state
    if tree_state is None:
        raise TreeStateViolationError(
            f"claim {claim_path!r} has no tree_state snapshot; cannot verify commit scope"
        )

    current_time = now or _utcnow()
    claim_age = current_time - _parse_iso(claim.last_heartbeat)
    if claim_age >= stale_threshold:
        raise TreeStateViolationError(
            f"refusing to commit {claim.claim_id}: claim heartbeat is stale\n"
            f"  session:      {claim.session_id} (operation: {claim.operation})\n"
            f"  last_heartbeat: {claim.last_heartbeat}\n"
            "  remediation:  refresh or re-establish the claim, then re-snapshot the "
            "tree before retrying the commit."
        )

    current_head = _git_head(repo_root)
    if current_head != tree_state.head_sha:
        raise TreeStateViolationError(
            f"refusing to commit {claim.claim_id}: HEAD moved since claim time\n"
            f"  session:      {claim.session_id} (operation: {claim.operation})\n"
            f"  claimed_head: {tree_state.head_sha}\n"
            f"  current_head: {current_head}\n"
            "  remediation:  inspect what advanced HEAD, then re-claim or re-snapshot "
            "before retrying."
        )

    request_path = _normalize_repo_relative_path(repo_root, current_request_path)
    scope_paths = set(tree_state.scope_paths)
    scope_paths.add(request_path)
    structural_paths = {
        _normalize_repo_relative_path(repo_root, claim_path),
    }
    if claim.affected_paths:
        working_request_path = _normalize_repo_relative_path(
            repo_root,
            claim.affected_paths[0],
        )
        structural_paths.add(working_request_path)
        working_request = Path(working_request_path)
        if len(working_request.parts) >= 3 and working_request.parts[1] == "working":
            queue_request = Path(working_request.parts[0]) / working_request.name
            structural_paths.add(queue_request.as_posix())

    allowed_dirty_paths = (
        set(tree_state.preexisting_dirty_paths)
        | scope_paths
        | structural_paths
    )
    dirty_paths = set(_git_dirty_paths(repo_root))
    staged_structural_paths: set[str] = set()
    if claim.affected_paths:
        working_request_path = _normalize_repo_relative_path(
            repo_root,
            claim.affected_paths[0],
        )
        working_request = Path(working_request_path)
        if len(working_request.parts) >= 3 and working_request.parts[1] == "working":
            queue_request = Path(working_request.parts[0]) / working_request.name
            staged_structural_paths.add(queue_request.as_posix())

    foreign_paths = tuple(sorted(dirty_paths - allowed_dirty_paths))
    if foreign_paths:
        foreign_display = ", ".join(foreign_paths)
        raise TreeStateViolationError(
            f"refusing to commit {claim.claim_id}: foreign changes detected outside "
            "the claim snapshot\n"
            f"  session:      {claim.session_id} (operation: {claim.operation})\n"
            f"  foreign_paths:{foreign_display}\n"
            "  remediation:  investigate what else is running and either stop it or "
            "revert the foreign change before resuming."
        )

    staged_paths = tuple(sorted(scope_paths | staged_structural_paths))
    if not staged_paths:
        raise TreeStateViolationError(
            f"refusing to commit {claim.claim_id}: the claim snapshot has no scoped paths"
        )
    _run_git(repo_root, "add", "--all", "--", *staged_paths)
    return ScopedStageResult(
        claim_id=claim.claim_id,
        head_sha=current_head,
        staged_paths=staged_paths,
    )


def inspect_work_claim_recovery(
    do_work_root: PathLike,
    *,
    claim_path: PathLike,
    now: Optional[datetime] = None,
    stale_threshold: timedelta = timedelta(minutes=2),
) -> WorkClaimRecoveryInspection:
    root = _coerce_do_work_root(do_work_root)
    claim = read_claim(claim_path)
    if not claim.affected_paths:
        raise RecoveryNotAllowedError(
            f"claim {os.fspath(claim_path)!r} has no affected_paths"
        )

    working_request = Path(claim.affected_paths[0])
    queue_request = root / working_request.name
    session_path = _session_record_path(root, claim.session_id)
    current = now or _utcnow()
    claim_age = current - _parse_iso(claim.last_heartbeat)
    claim_stale = claim_age >= stale_threshold

    session_record = inspect_session_record(root, claim.session_id)
    if not claim_stale:
        return WorkClaimRecoveryInspection(
            claim_path=os.fspath(claim_path),
            request_path=os.fspath(working_request),
            queue_path=os.fspath(queue_request),
            session_record_path=os.fspath(session_path),
            claim=claim,
            session_record=session_record,
            verdict="live",
            reason=(
                f"claim heartbeat {claim.last_heartbeat} is within the stale threshold"
            ),
            claim_heartbeat_stale=False,
            session_heartbeat_stale=(
                None if session_record is None else
                current - _parse_iso(session_record.last_heartbeat) >= stale_threshold
            ),
            process_alive=None if session_record is None else _pid_alive(session_record.pid),
        )

    if session_record is None:
        return WorkClaimRecoveryInspection(
            claim_path=os.fspath(claim_path),
            request_path=os.fspath(working_request),
            queue_path=os.fspath(queue_request),
            session_record_path=os.fspath(session_path),
            claim=claim,
            session_record=None,
            verdict="missing-session-record",
            reason=(
                f"claim heartbeat is stale but session record {os.fspath(session_path)!r} "
                "is missing; recovery would have to guess"
            ),
            claim_heartbeat_stale=True,
            session_heartbeat_stale=None,
            process_alive=None,
        )

    session_age = current - _parse_iso(session_record.last_heartbeat)
    session_stale = session_age >= stale_threshold

    if session_record.hostname != socket.gethostname():
        return WorkClaimRecoveryInspection(
            claim_path=os.fspath(claim_path),
            request_path=os.fspath(working_request),
            queue_path=os.fspath(queue_request),
            session_record_path=os.fspath(session_path),
            claim=claim,
            session_record=session_record,
            verdict="foreign-host",
            reason=(
                f"session {session_record.session_id} was recorded on host "
                f"{session_record.hostname!r}; current host is {socket.gethostname()!r}"
            ),
            claim_heartbeat_stale=True,
            session_heartbeat_stale=session_stale,
            process_alive=None,
        )

    process_alive = _pid_alive(session_record.pid)
    if not session_stale:
        return WorkClaimRecoveryInspection(
            claim_path=os.fspath(claim_path),
            request_path=os.fspath(working_request),
            queue_path=os.fspath(queue_request),
            session_record_path=os.fspath(session_path),
            claim=claim,
            session_record=session_record,
            verdict="stale",
            reason=(
                "claim heartbeat is stale but the owning session record is still fresh"
            ),
            claim_heartbeat_stale=True,
            session_heartbeat_stale=False,
            process_alive=process_alive,
        )

    if process_alive:
        return WorkClaimRecoveryInspection(
            claim_path=os.fspath(claim_path),
            request_path=os.fspath(working_request),
            queue_path=os.fspath(queue_request),
            session_record_path=os.fspath(session_path),
            claim=claim,
            session_record=session_record,
            verdict="stale",
            reason=(
                f"claim and session heartbeats are stale but PID {session_record.pid} "
                "still exists on this host; recovery is ambiguous"
            ),
            claim_heartbeat_stale=True,
            session_heartbeat_stale=True,
            process_alive=True,
        )

    return WorkClaimRecoveryInspection(
        claim_path=os.fspath(claim_path),
        request_path=os.fspath(working_request),
        queue_path=os.fspath(queue_request),
        session_record_path=os.fspath(session_path),
        claim=claim,
        session_record=session_record,
        verdict="recoverable",
        reason=(
            "claim heartbeat is stale, the owning session heartbeat is stale, "
            "and the owning PID is absent on this host"
        ),
        claim_heartbeat_stale=True,
        session_heartbeat_stale=True,
        process_alive=False,
    )


def recover_orphaned_work_claim(
    do_work_root: PathLike,
    *,
    claim_path: PathLike,
    recovering_session_id: str,
    now: Optional[datetime] = None,
    stale_threshold: timedelta = timedelta(minutes=2),
) -> RecoveredWorkClaim:
    inspection = inspect_work_claim_recovery(
        do_work_root,
        claim_path=claim_path,
        now=now,
        stale_threshold=stale_threshold,
    )
    if inspection.verdict != "recoverable" or inspection.session_record is None:
        raise RecoveryNotAllowedError(
            f"cannot recover {os.fspath(claim_path)!r}: {inspection.reason}"
        )

    recovered_at = _iso(now or _utcnow())
    working_request = inspection.request_path
    queue_request = inspection.queue_path
    log_path = _recovery_log_path(
        do_work_root,
        claim_id=inspection.claim.claim_id,
        recovered_at=recovered_at,
    )
    log_entry = {
        "recovered_at": recovered_at,
        "claim_id": inspection.claim.claim_id,
        "released_session_id": inspection.claim.session_id,
        "recovered_by_session_id": recovering_session_id,
        "claim_path": inspection.claim_path,
        "working_request_path": working_request,
        "queue_request_path": queue_request,
        "evidence": inspection.to_dict(),
    }

    atomic_rename(
        working_request,
        queue_request,
        transition=f"orphan-recovery working->queue for {inspection.claim.claim_id}",
    )
    try:
        atomic_write(
            log_path,
            json.dumps(log_entry, indent=2, sort_keys=True) + "\n",
        )
    except Exception:
        try:
            atomic_rename(
                queue_request,
                working_request,
                transition=(
                    f"orphan-recovery rollback for {inspection.claim.claim_id}"
                ),
            )
        except Exception as rollback_exc:
            raise ConcurrencyError(
                "orphan recovery moved the request but failed to write the recovery "
                f"log at {os.fspath(log_path)!r}; rollback also failed"
            ) from rollback_exc
        raise

    release_claim(
        inspection.claim_path,
        expected_session_id=inspection.claim.session_id,
    )

    session_path = inspection.session_record_path
    try:
        os.unlink(session_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConcurrencyError(
            f"recovered {inspection.claim.claim_id} but failed to delete session "
            f"record {session_path!r}: {exc}"
        ) from exc

    return RecoveredWorkClaim(
        claim_id=inspection.claim.claim_id,
        released_session_id=inspection.claim.session_id,
        recovered_by_session_id=recovering_session_id,
        recovered_at=recovered_at,
        claim_path=inspection.claim_path,
        working_request_path=working_request,
        queue_request_path=queue_request,
        log_path=os.fspath(log_path),
        claim_last_heartbeat=inspection.claim.last_heartbeat,
        session_last_heartbeat=inspection.session_record.last_heartbeat,
        session_pid=inspection.session_record.pid,
        session_hostname=inspection.session_record.hostname,
    )


def archive_user_request_if_complete(
    do_work_root: PathLike,
    *,
    ur_id: str,
    session_id: str,
    operation: str = "work",
    now: Optional[datetime] = None,
) -> ParentArchivalResult:
    root = _coerce_do_work_root(do_work_root)
    open_ur_dir = root / "user-requests" / ur_id
    archive_ur_dir = root / "archive" / ur_id
    handle = _acquire_archival_lock(
        root,
        identifier=ur_id,
        session_id=session_id,
        operation=operation,
        now=now,
    )
    try:
        if archive_ur_dir.exists():
            if open_ur_dir.exists():
                raise ConcurrencyError(
                    f"refusing to archive {ur_id}: both {os.fspath(open_ur_dir)!r} "
                    f"and {os.fspath(archive_ur_dir)!r} exist"
                )
            return ParentArchivalResult(
                kind="user-request",
                identifier=ur_id,
                outcome="already-archived",
                archive_path=os.fspath(archive_ur_dir),
                missing_request_ids=(),
            )

        if not open_ur_dir.exists():
            raise ConcurrencyError(
                f"refusing to archive {ur_id}: expected open folder "
                f"{os.fspath(open_ur_dir)!r} but it does not exist"
            )

        request_ids = _parse_ur_requests(open_ur_dir / "input.md")
        loose_paths: list[Path] = []
        missing_ids: list[str] = []

        for req_id in request_ids:
            in_open_ur = _find_unique_request_path(
                root,
                req_id,
                patterns=(f"user-requests/{ur_id}/{{req_id}}-*.md",),
                label=f"user-requests/{ur_id}",
            )
            in_archive_root = _find_unique_request_path(
                root,
                req_id,
                patterns=("archive/{req_id}-*.md",),
                label="archive root",
            )
            in_archive_ur = _find_unique_request_path(
                root,
                req_id,
                patterns=(f"archive/{ur_id}/{{req_id}}-*.md",),
                label=f"archive/{ur_id}",
            )
            found = [path for path in (in_open_ur, in_archive_root, in_archive_ur) if path is not None]
            if len(found) > 1:
                raise ConcurrencyError(
                    f"refusing to archive {ur_id}: {req_id} exists in multiple archival "
                    f"locations: {', '.join(os.fspath(path) for path in found)}"
                )
            if not found:
                missing_ids.append(req_id)
                continue
            if in_archive_root is not None:
                loose_paths.append(in_archive_root)

        if missing_ids:
            return ParentArchivalResult(
                kind="user-request",
                identifier=ur_id,
                outcome="not-ready",
                archive_path=os.fspath(archive_ur_dir),
                missing_request_ids=tuple(missing_ids),
            )

        for loose_path in loose_paths:
            target = open_ur_dir / loose_path.name
            if target.exists():
                raise ConcurrencyError(
                    f"refusing to archive {ur_id}: destination {os.fspath(target)!r} "
                    "already exists before moving loose archived REQs"
                )

        for loose_path in loose_paths:
            atomic_rename(
                loose_path,
                open_ur_dir / loose_path.name,
                transition=f"archive-root->open-ur for {loose_path.stem}",
            )

        atomic_rename(
            open_ur_dir,
            archive_ur_dir,
            transition=f"user-request archive for {ur_id}",
        )
        return ParentArchivalResult(
            kind="user-request",
            identifier=ur_id,
            outcome="archived",
            archive_path=os.fspath(archive_ur_dir),
            missing_request_ids=(),
        )
    finally:
        release_lock(handle)


def archive_legacy_context_if_complete(
    do_work_root: PathLike,
    *,
    context_ref: str,
    session_id: str,
    operation: str = "work",
    now: Optional[datetime] = None,
) -> ParentArchivalResult:
    root = _coerce_do_work_root(do_work_root)
    open_context = _resolve_context_path(root, context_ref)
    archive_context = root / "archive" / open_context.name
    identifier = open_context.stem
    handle = _acquire_archival_lock(
        root,
        identifier=identifier,
        session_id=session_id,
        operation=operation,
        now=now,
    )
    try:
        if archive_context.exists():
            if open_context.exists():
                raise ConcurrencyError(
                    f"refusing to archive {identifier}: both {os.fspath(open_context)!r} "
                    f"and {os.fspath(archive_context)!r} exist"
                )
            return ParentArchivalResult(
                kind="legacy-context",
                identifier=identifier,
                outcome="already-archived",
                archive_path=os.fspath(archive_context),
                missing_request_ids=(),
            )
        if not open_context.exists():
            raise ConcurrencyError(
                f"refusing to archive {identifier}: expected context file "
                f"{os.fspath(open_context)!r} but it does not exist"
            )

        frontmatter = _read_frontmatter(open_context)
        raw_requests = frontmatter.get("requests")
        if not isinstance(raw_requests, list):
            raise ConcurrencyError(
                f"expected requests array in {os.fspath(open_context)!r}"
            )

        missing_ids: list[str] = []
        for req_id in (str(item) for item in raw_requests):
            archived_path = _find_unique_request_path(
                root,
                req_id,
                patterns=(
                    "archive/{req_id}-*.md",
                    "archive/UR-*/{req_id}-*.md",
                ),
                label="archive",
            )
            if archived_path is None:
                missing_ids.append(req_id)

        if missing_ids:
            return ParentArchivalResult(
                kind="legacy-context",
                identifier=identifier,
                outcome="not-ready",
                archive_path=os.fspath(archive_context),
                missing_request_ids=tuple(missing_ids),
            )

        atomic_rename(
            open_context,
            archive_context,
            transition=f"legacy-context archive for {identifier}",
        )
        return ParentArchivalResult(
            kind="legacy-context",
            identifier=identifier,
            outcome="archived",
            archive_path=os.fspath(archive_context),
            missing_request_ids=(),
        )
    finally:
        release_lock(handle)


def archive_completed_request(
    do_work_root: PathLike,
    *,
    working_request_path: PathLike,
    session_id: str,
    operation: str = "work",
    now: Optional[datetime] = None,
) -> RequestArchivalResult:
    root = _coerce_do_work_root(do_work_root)
    working_request = _work_request_path(root, working_request_path)
    expected_parent = root / "working"
    if working_request.parent != expected_parent:
        raise ConcurrencyError(
            f"completed request archival expects a file in {os.fspath(expected_parent)!r}, "
            f"got {os.fspath(working_request)!r}"
        )

    if not working_request.exists():
        raise ConcurrencyError(
            f"completed request archival expected {os.fspath(working_request)!r} to exist"
        )

    frontmatter = _read_frontmatter(working_request)
    request_id = _req_identifier_from_path(working_request)
    archive_dir = root / "archive"
    os.makedirs(archive_dir, exist_ok=True)

    user_request = frontmatter.get("user_request")
    if isinstance(user_request, str) and user_request:
        open_ur_dir = root / "user-requests" / user_request
        archive_ur_dir = archive_dir / user_request
        if archive_ur_dir.exists() and not open_ur_dir.exists():
            raise ConcurrencyError(
                f"refusing to archive {request_id}: {user_request} is already archived "
                f"at {os.fspath(archive_ur_dir)!r} while {os.fspath(working_request)!r} "
                "is still in working/"
            )
        if not open_ur_dir.exists():
            raise ConcurrencyError(
                f"refusing to archive {request_id}: user_request folder "
                f"{os.fspath(open_ur_dir)!r} is missing"
            )

    request_archive_path = archive_dir / working_request.name
    atomic_rename(
        working_request,
        request_archive_path,
        transition=f"working->archive for {request_id}",
    )

    parent_result: Optional[ParentArchivalResult] = None
    if isinstance(user_request, str) and user_request:
        parent_result = archive_user_request_if_complete(
            root,
            ur_id=user_request,
            session_id=session_id,
            operation=operation,
            now=now,
        )
        if parent_result.outcome in {"archived", "already-archived"}:
            final_request_path = archive_dir / user_request / working_request.name
            if final_request_path.exists():
                return RequestArchivalResult(
                    request_id=request_id,
                    outcome="archived-parent",
                    request_path=os.fspath(final_request_path),
                    parent_result=parent_result,
                )

    context_ref = frontmatter.get("context_ref")
    if isinstance(context_ref, str) and context_ref:
        parent_result = archive_legacy_context_if_complete(
            root,
            context_ref=context_ref,
            session_id=session_id,
            operation=operation,
            now=now,
        )

    return RequestArchivalResult(
        request_id=request_id,
        outcome="archived-root",
        request_path=os.fspath(request_archive_path),
        parent_result=parent_result,
    )


def begin_capture_transaction(
    do_work_root: PathLike,
    *,
    session_id: str,
    operation: str = "do",
    now: Optional[datetime] = None,
    preserve_verbatim_input_on_failure: bool = True,
) -> CaptureTransaction:
    root = _coerce_do_work_root(do_work_root)
    capture_lock = acquire_lock(
        _capture_lock_path(root),
        session_id=session_id,
        operation=operation,
        scope="capture-global",
        now=now,
    )

    staging_root = _capture_staging_root(root)
    os.makedirs(staging_root, exist_ok=True)

    try:
        blocking_stages: list[str] = []
        for stage_dir in _iter_capture_stage_dirs(root):
            manifest_path = _capture_manifest_path(stage_dir)
            if not manifest_path.exists():
                blocking_stages.append(
                    f"{stage_dir.name} (missing manifest; run repair_capture_state)"
                )
                continue
            manifest = _read_capture_manifest(manifest_path)
            if manifest.status == "committed":
                _cleanup_capture_stage_dir(stage_dir)
                continue
            blocking_stages.append(
                f"{manifest.capture_id} ({manifest.status}; run repair_capture_state)"
            )

        if blocking_stages:
            raise ConcurrencyError(
                "capture start found pre-existing staged state:\n  - "
                + "\n  - ".join(blocking_stages)
            )

        created_at = _iso(now or _utcnow())
        capture_id = (
            "CAP-"
            + created_at.replace(":", "-")
            + "-"
            + secrets.token_hex(4)
        )
        stage_dir = _capture_stage_dir(root, capture_id)
        stage_dir.mkdir(parents=True, exist_ok=False)
        manifest = CaptureManifest(
            capture_id=capture_id,
            session_id=session_id,
            operation=operation,
            created_at=created_at,
            updated_at=created_at,
            status="staging",
            preserve_verbatim_input_on_failure=preserve_verbatim_input_on_failure,
            failure_reason=None,
            items=(),
        )
        manifest_path = _capture_manifest_path(stage_dir)
        _write_capture_manifest(manifest_path, manifest)
        return CaptureTransaction(
            lock=capture_lock,
            manifest_path=os.fspath(manifest_path),
            manifest=manifest,
        )
    except Exception:
        release_lock(capture_lock)
        raise


def release_capture_transaction(handle: CaptureTransaction) -> None:
    release_lock(handle.lock)


def allocate_staged_ur_input(
    transaction: CaptureTransaction,
    *,
    do_work_root: PathLike,
    content: str,
    now: Optional[datetime] = None,
) -> AllocatedId:
    root = _coerce_do_work_root(do_work_root)
    manifest = _read_capture_manifest(transaction.manifest_path)
    if manifest.status != "staging":
        raise ConcurrencyError(
            f"capture {manifest.capture_id} is {manifest.status}; cannot stage a UR"
        )
    if any(item.kind == "ur-dir" for item in manifest.items):
        raise ConcurrencyError(
            f"capture {manifest.capture_id} already staged a UR folder"
        )

    handle = _acquire_id_allocation_lock(
        root,
        namespace="ur",
        session_id=manifest.session_id,
        operation=manifest.operation,
        now=now,
    )
    try:
        number = _next_identifier_number(root, "ur")
        identifier = _format_identifier("ur", number)
        conflict = _find_conflicting_identifier_path(root, "ur", identifier)
        if conflict is not None:
            raise CollisionError(
                f"allocate_staged_ur_input: next id {identifier} already exists at "
                f"{os.fspath(conflict)!r}"
            )

        stage_dir = Path(transaction.manifest_path).parent
        ur_dir = stage_dir / "user-requests" / identifier
        input_path = ur_dir / "input.md"
        try:
            os.makedirs(ur_dir, exist_ok=False)
        except FileExistsError as exc:
            raise CollisionError(
                f"allocate_staged_ur_input: destination directory "
                f"{os.fspath(ur_dir)!r} already exists"
            ) from exc

        try:
            _write_new_file(
                input_path,
                content,
                transition=f"stage input for {identifier}",
            )
        except Exception:
            try:
                os.rmdir(ur_dir)
            except OSError:
                pass
            raise

        updated = CaptureManifest(
            capture_id=manifest.capture_id,
            session_id=manifest.session_id,
            operation=manifest.operation,
            created_at=manifest.created_at,
            updated_at=_iso(now or _utcnow()),
            status=manifest.status,
            preserve_verbatim_input_on_failure=manifest.preserve_verbatim_input_on_failure,
            failure_reason=manifest.failure_reason,
            items=manifest.items + (
                CaptureItem(
                    kind="ur-dir",
                    identifier=identifier,
                    staged_path=os.fspath(ur_dir),
                    final_path=os.fspath(root / "user-requests" / identifier),
                ),
            ),
        )
        _update_capture_manifest(transaction, updated)
        return AllocatedId(
            identifier=identifier,
            number=number,
            namespace="ur",
            path=os.fspath(input_path),
            lock_path=os.fspath(_id_lock_path(root, "ur")),
        )
    finally:
        release_lock(handle)


def allocate_staged_req_file(
    transaction: CaptureTransaction,
    *,
    do_work_root: PathLike,
    slug: str,
    content: str,
    now: Optional[datetime] = None,
) -> AllocatedId:
    root = _coerce_do_work_root(do_work_root)
    manifest = _read_capture_manifest(transaction.manifest_path)
    if manifest.status != "staging":
        raise ConcurrencyError(
            f"capture {manifest.capture_id} is {manifest.status}; cannot stage a REQ"
        )

    handle = _acquire_id_allocation_lock(
        root,
        namespace="req",
        session_id=manifest.session_id,
        operation=manifest.operation,
        now=now,
    )
    try:
        number = _next_identifier_number(root, "req")
        identifier = _format_identifier("req", number)
        conflict = _find_conflicting_identifier_path(root, "req", identifier)
        if conflict is not None:
            raise CollisionError(
                f"allocate_staged_req_file: next id {identifier} already exists at "
                f"{os.fspath(conflict)!r}"
            )

        stage_dir = Path(transaction.manifest_path).parent
        target = stage_dir / "reqs" / f"{identifier}-{slug}.md"
        _write_new_file(
            target,
            content,
            transition=f"stage queue file for {identifier}",
        )
        updated = CaptureManifest(
            capture_id=manifest.capture_id,
            session_id=manifest.session_id,
            operation=manifest.operation,
            created_at=manifest.created_at,
            updated_at=_iso(now or _utcnow()),
            status=manifest.status,
            preserve_verbatim_input_on_failure=manifest.preserve_verbatim_input_on_failure,
            failure_reason=manifest.failure_reason,
            items=manifest.items + (
                CaptureItem(
                    kind="req",
                    identifier=identifier,
                    staged_path=os.fspath(target),
                    final_path=os.fspath(root / target.name),
                ),
            ),
        )
        _update_capture_manifest(transaction, updated)
        return AllocatedId(
            identifier=identifier,
            number=number,
            namespace="req",
            path=os.fspath(target),
            lock_path=os.fspath(_id_lock_path(root, "req")),
        )
    finally:
        release_lock(handle)


def abort_capture_transaction(
    transaction: CaptureTransaction,
    *,
    reason: str,
    now: Optional[datetime] = None,
    preserve_draft: Optional[bool] = None,
) -> CaptureManifest:
    manifest = _read_capture_manifest(transaction.manifest_path)
    if preserve_draft is None:
        preserve_draft = manifest.preserve_verbatim_input_on_failure

    updated_at = _iso(now or _utcnow())
    if preserve_draft:
        updated = CaptureManifest(
            capture_id=manifest.capture_id,
            session_id=manifest.session_id,
            operation=manifest.operation,
            created_at=manifest.created_at,
            updated_at=updated_at,
            status="failed",
            preserve_verbatim_input_on_failure=manifest.preserve_verbatim_input_on_failure,
            failure_reason=reason,
            items=manifest.items,
        )
        return _update_capture_manifest(transaction, updated)

    if manifest.status == "committing":
        raise ConcurrencyError(
            f"capture {manifest.capture_id} is already committing; use "
            "repair_capture_state(...) to finish or inspect it"
        )

    _cleanup_capture_stage_dir(Path(transaction.manifest_path).parent)
    updated = CaptureManifest(
        capture_id=manifest.capture_id,
        session_id=manifest.session_id,
        operation=manifest.operation,
        created_at=manifest.created_at,
        updated_at=updated_at,
        status="failed",
        preserve_verbatim_input_on_failure=manifest.preserve_verbatim_input_on_failure,
        failure_reason=reason,
        items=manifest.items,
    )
    transaction.manifest = updated
    return updated


def commit_capture_transaction(
    transaction: CaptureTransaction,
    *,
    now: Optional[datetime] = None,
) -> CaptureManifest:
    manifest = _read_capture_manifest(transaction.manifest_path)
    if manifest.status == "failed":
        raise ConcurrencyError(
            f"capture {manifest.capture_id} is marked failed; repair or discard it "
            "before retrying"
        )

    _validate_capture_commit(manifest)
    if manifest.status != "committing":
        manifest = CaptureManifest(
            capture_id=manifest.capture_id,
            session_id=manifest.session_id,
            operation=manifest.operation,
            created_at=manifest.created_at,
            updated_at=_iso(now or _utcnow()),
            status="committing",
            preserve_verbatim_input_on_failure=manifest.preserve_verbatim_input_on_failure,
            failure_reason=manifest.failure_reason,
            items=manifest.items,
        )
        manifest = _update_capture_manifest(transaction, manifest)

    items = list(manifest.items)
    for index, item in enumerate(items):
        if item.state == "published":
            continue

        staged_exists, final_exists = _capture_item_exists(item)
        if staged_exists and final_exists:
            raise ConcurrencyError(
                f"capture {manifest.capture_id} found both staged and final copies for "
                f"{item.identifier}; refusing ambiguous commit state"
            )
        if not staged_exists and not final_exists:
            raise ConcurrencyError(
                f"capture {manifest.capture_id} lost both staged and final copies for "
                f"{item.identifier}; repair required"
            )

        if staged_exists and not final_exists:
            os.makedirs(Path(item.final_path).parent, exist_ok=True)
            atomic_rename(
                item.staged_path,
                item.final_path,
                transition=f"capture commit for {item.identifier}",
            )

        items[index] = CaptureItem(
            kind=item.kind,
            identifier=item.identifier,
            staged_path=item.staged_path,
            final_path=item.final_path,
            state="published",
        )
        manifest = CaptureManifest(
            capture_id=manifest.capture_id,
            session_id=manifest.session_id,
            operation=manifest.operation,
            created_at=manifest.created_at,
            updated_at=_iso(now or _utcnow()),
            status="committing",
            preserve_verbatim_input_on_failure=manifest.preserve_verbatim_input_on_failure,
            failure_reason=manifest.failure_reason,
            items=tuple(items),
        )
        manifest = _update_capture_manifest(transaction, manifest)

    manifest = CaptureManifest(
        capture_id=manifest.capture_id,
        session_id=manifest.session_id,
        operation=manifest.operation,
        created_at=manifest.created_at,
        updated_at=_iso(now or _utcnow()),
        status="committed",
        preserve_verbatim_input_on_failure=manifest.preserve_verbatim_input_on_failure,
        failure_reason=manifest.failure_reason,
        items=manifest.items,
    )
    return _update_capture_manifest(transaction, manifest)


def repair_capture_state(
    do_work_root: PathLike,
    *,
    session_id: str,
    operation: str = "do",
    capture_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> CaptureRepairResult:
    root = _coerce_do_work_root(do_work_root)
    current_time = now or _utcnow()
    lock_path = _capture_lock_path(root)
    try:
        capture_lock = acquire_lock(
            lock_path,
            session_id=session_id,
            operation=operation,
            scope="capture-global",
            now=current_time,
        )
    except LockHeldError as exc:
        verdict = classify_lock(exc.holder, now=current_time)
        if verdict != "orphaned":
            raise ConcurrencyError(
                "capture repair refuses to steal a live or ambiguous capture lock:\n"
                f"  holder: {exc.holder.session_id}\n"
                f"  verdict: {verdict}"
            ) from exc
        os.unlink(lock_path)
        capture_lock = acquire_lock(
            lock_path,
            session_id=session_id,
            operation=operation,
            scope="capture-global",
            now=current_time,
        )

    try:
        stage_dirs = list(_iter_capture_stage_dirs(root))
        if not stage_dirs:
            return CaptureRepairResult(
                capture_id=None,
                outcome="noop",
                detail="no capture staging state found",
            )

        if capture_id is not None:
            stage_dir = _capture_stage_dir(root, capture_id)
            if stage_dir not in stage_dirs:
                return CaptureRepairResult(
                    capture_id=capture_id,
                    outcome="noop",
                    detail=f"{capture_id} not present under .capture-staging/",
                )
        else:
            if len(stage_dirs) > 1:
                raise ConcurrencyError(
                    "capture repair found multiple staged captures; specify capture_id: "
                    + ", ".join(path.name for path in stage_dirs)
                )
            stage_dir = stage_dirs[0]

        manifest_path = _capture_manifest_path(stage_dir)
        if not manifest_path.exists():
            raise ConcurrencyError(
                f"capture stage {stage_dir.name} is missing manifest.json; "
                "manual inspection required"
            )

        manifest = _read_capture_manifest(manifest_path)
        if manifest.status == "committed":
            _cleanup_capture_stage_dir(stage_dir)
            return CaptureRepairResult(
                capture_id=manifest.capture_id,
                outcome="cleaned-committed",
                detail="removed committed staging leftovers",
            )

        if manifest.status in {"staging", "failed"}:
            _cleanup_capture_stage_dir(stage_dir)
            return CaptureRepairResult(
                capture_id=manifest.capture_id,
                outcome="discarded-draft",
                detail="discarded staged draft and released reserved IDs",
            )

        transaction = CaptureTransaction(
            lock=capture_lock,
            manifest_path=os.fspath(manifest_path),
            manifest=manifest,
        )
        commit_capture_transaction(transaction, now=current_time)
        _cleanup_capture_stage_dir(stage_dir)
        return CaptureRepairResult(
            capture_id=manifest.capture_id,
            outcome="resumed-commit",
            detail="finished an interrupted capture commit and cleaned staging",
        )
    finally:
        release_lock(capture_lock)


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
    "CaptureFormatError",
    "ClaimHeldError",
    "SessionClaimConflictError",
    "ScopeError",
    "StaleRenameError",
    "CrossDeviceError",
    "CollisionError",
    "AtomicWriteError",
    "ForeignReleaseError",
    "SessionFormatError",
    "RecoveryNotAllowedError",
    "TreeStateViolationError",
    "AllocatedId",
    "LockInfo",
    "LockHandle",
    "ClaimHandle",
    "WorkClaimHandle",
    "CleanupClaimRecord",
    "CleanupClaimHandle",
    "ClaimFileFingerprint",
    "ClaimTreeState",
    "ClaimRecord",
    "SessionRecord",
    "WorkClaimRecoveryInspection",
    "RecoveredWorkClaim",
    "ParentArchivalResult",
    "RequestArchivalResult",
    "ScopedStageResult",
    "CaptureItem",
    "CaptureManifest",
    "CaptureTransaction",
    "CaptureRepairResult",
    "validate_scope",
    "acquire_lock",
    "acquire_verification_lock",
    "release_lock",
    "inspect_lock",
    "refresh_heartbeat",
    "classify_lock",
    "atomic_rename",
    "atomic_write",
    "verification_lock_path",
    "replace_markdown_section",
    "rewrite_markdown_section_atomic",
    "read_session_record",
    "write_session_record",
    "inspect_session_record",
    "read_claim",
    "write_claim",
    "read_cleanup_claim",
    "write_cleanup_claim",
    "release_claim",
    "release_cleanup_claim",
    "refresh_claim_heartbeat",
    "refresh_cleanup_claim_heartbeat",
    "claim_cleanup",
    "refresh_cleanup_heartbeat",
    "release_cleanup",
    "claim_work_request",
    "capture_claim_tree_state",
    "verify_and_stage_claim_scope",
    "inspect_work_claim_recovery",
    "recover_orphaned_work_claim",
    "archive_user_request_if_complete",
    "archive_legacy_context_if_complete",
    "archive_completed_request",
    "begin_capture_transaction",
    "release_capture_transaction",
    "allocate_staged_ur_input",
    "allocate_staged_req_file",
    "abort_capture_transaction",
    "commit_capture_transaction",
    "repair_capture_state",
    "allocate_ur_input",
    "allocate_req_file",
]
