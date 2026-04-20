# Concurrency Primitives

> **Part of the do-work skill.** Defines the shared concurrency primitives every coordinated action uses. This document is the contract; `lib/concurrency.py` is the implementation. Every other REQ in the parallel-safety batch (REQ-003..REQ-010) consumes primitives from here — none roll their own.

## What This Provides

A filesystem-only toolbox that lets sessions coordinate without races: exclusive lockfiles, atomic renames, atomic writes, atomic REQ/UR ID allocation for capture, a claim file format, and a catalog of canonical lock scopes. All primitives are stdlib-only Python 3 and consult the session-identity record (REQ-001) so every lock knows which session holds it.

This is a **library of primitives**, not a policy. Individual actions decide when and at what scope to use each primitive (see REQ-003..REQ-010). The library does not assume. The rule: **never hand-roll a lock, a claim, or a state transition — call the library.**

## Module Location

```
lib/
  __init__.py
  concurrency.py          # the library
  concurrency_test.py     # isolated unittest suite (uses tempdir, not live state)
```

Run tests from the repo root:

```bash
python3 -m unittest lib.concurrency_test -v
```

Actions invoke primitives via Python one-liners — example patterns are shown in each section below.

## Agent Invocation Contract

The primitives are called from action markdown by running `python3 -c '...'` (or equivalent) against the `lib.concurrency` module. The action file documents **what** to do; the library enforces **how**. Every primitive:

- Fails loud with a descriptive exception on the first ambiguous signal.
- Reports holder session ID + operation in lock-contention errors.
- Makes no hidden decisions on behalf of the caller (no auto-delete, no auto-recover, no TTL guess).

## Primitives

### 1. Exclusive Lockfile

**Contract:** Acquire an exclusive lock by creating a lockfile at a caller-specified path using `O_CREAT | O_EXCL` semantics. A second caller on the same path fails fast with `LockHeldError` — never blocks silently.

**Lockfile on-disk schema** (JSON, `cat`-readable):

```json
{
  "session_id": "2026-04-18T00-21-52Z-f2d85402",
  "operation": "work",
  "scope": "req-claim:REQ-002",
  "acquired_at": "2026-04-18T00:22:00Z",
  "last_heartbeat": "2026-04-18T00:22:00Z",
  "pid": 21586,
  "hostname": "alejandro-laptop"
}
```

**API:**

- `acquire_lock(path, *, session_id, operation, scope, pid=None, hostname=None, now=None) -> LockHandle`
- `release_lock(handle_or_path)` — idempotent removal of the lockfile the handle points at. Raises if the lockfile content no longer matches the handle (foreign release attempt).
- `inspect_lock(path) -> LockInfo | None` — read-only; returns `None` if no lockfile, dataclass with all schema fields otherwise.
- `refresh_heartbeat(handle_or_path, *, now=None)` — rewrites `last_heartbeat` via atomic write. Callers holding long-running locks use this on the session-identity heartbeat cadence (30 s default).

**Recommended on-disk location:** `do-work/.locks/<scope>.lock` (caller supplies the full path; library does not dictate the root). Lock roots must be in `.gitignore`.

### 2. Stale-Lock Verdict

**Contract:** Classify an existing lock as `live`, `stale`, or `orphaned`. **Never mutates the filesystem.** The caller decides what to do with the verdict — recovery policy is REQ-005's job, not this library's.

**API:**

- `classify_lock(info, *, now, stale_threshold=timedelta(minutes=2)) -> Literal["live", "stale", "orphaned"]`

**Verdict rules:**

- `live` — `now - last_heartbeat < stale_threshold`. The holder is writing heartbeats; leave it alone.
- `stale` — heartbeat is older than the threshold **but** the PID is still alive on the current host (same `hostname`, `os.kill(pid, 0)` succeeds). The session may be paused; do not auto-recover.
- `orphaned` — heartbeat is stale **and** the PID is absent on the current host. Eligible for recovery by the caller's policy.

Cross-host cases (`info.hostname != local hostname`) always return `stale` — UR-001 is single-machine; cross-host recovery is out of scope.

### 3. Atomic Rename

**Contract:** Wrap `os.rename` so every state transition (queue→working, working→archive, draft→committed) is named and atomic. Raw `os.rename` and shell `mv` are forbidden for state transitions once this library is available.

**API:**

- `atomic_rename(src, dst, *, transition)` — `transition` is a human-readable label surfaced in error messages (e.g., `"queue→working for REQ-002"`). Raises `StaleRenameError` on `ENOENT`, `CrossDeviceError` on `EXDEV`, `CollisionError` if `dst` already exists (POSIX `rename` overwrites silently — the library refuses).

### 4. Atomic Write

**Contract:** Writes never leave a half-written file visible to a reader. Always write-to-temp-then-rename on the same filesystem.

**API:**

- `atomic_write(path, content, *, mode="w")` — writes to `<path>.tmp.<hex>`, `fsync`s, then `os.rename` onto `path`. The temp name is unique per call so two concurrent writers don't collide on the temp file itself (they may still race on the final rename — last-writer-wins, which is the documented POSIX semantics and is the caller's responsibility to serialize when needed).

### 5. Atomic ID Allocation

**Contract:** Capture never hand-scans for `REQ-NNN` / `UR-NNN`. It calls the allocator, which acquires the namespace-specific ID lock, scans every authoritative location for that namespace, writes the placeholder on disk, and only then releases the lock.

**API:**

- `allocate_ur_input(do_work_root, *, session_id, operation, content, now=None) -> AllocatedId`
- `allocate_req_file(do_work_root, *, session_id, operation, slug, content, now=None) -> AllocatedId`

**Namespace behavior:**

- REQs use `id-allocation:req` and lock path `do-work/.locks/id-allocation-req.lock`
- URs use `id-allocation:ur` and lock path `do-work/.locks/id-allocation-ur.lock`
- The allocator retries briefly on lock contention because parallel capture is expected; the underlying `acquire_lock(...)` primitive remains fail-fast.

**Authoritative scan coverage:**

- `allocate_req_file(...)` scans `do-work/REQ-*.md`, `do-work/working/REQ-*.md`, `do-work/archive/REQ-*.md`, and `do-work/archive/UR-*/REQ-*.md`
- `allocate_ur_input(...)` scans `do-work/user-requests/UR-*` and `do-work/archive/UR-*`

**Write-then-release guarantee:**

- UR allocation is not complete until `do-work/user-requests/UR-NNN/input.md` exists on disk.
- REQ allocation is not complete until `do-work/REQ-NNN-slug.md` exists on disk.
- If the placeholder write fails, the allocator releases the lock and removes any directory it created.
- If the "next" ID already exists anywhere authoritative, allocation fails loudly with the conflicting path.

### 6. Claim File Format

**Contract:** One JSON schema for every claim file produced by REQ-004 (work claim), REQ-007 (verify claim), REQ-009 (cleanup claim). One parser, one writer.

**Schema:**

```json
{
  "claim_id": "REQ-002",
  "session_id": "2026-04-18T00-21-52Z-f2d85402",
  "operation": "work",
  "scope": "req-claim:REQ-002",
  "affected_paths": ["do-work/working/REQ-002-shared-concurrency-primitives.md"],
  "acquired_at": "2026-04-18T00:22:00Z",
  "last_heartbeat": "2026-04-18T00:22:00Z",
  "tree_state": {
    "repo_root": "/repo",
    "head_sha": "63477edd608c83afaddd6de04ae3f721c4f57954",
    "captured_at": "2026-04-18T00:22:00Z",
    "preexisting_dirty_paths": [],
    "scope_paths": ["do-work/working/REQ-002-shared-concurrency-primitives.md"],
    "scope_fingerprints": [
      {
        "path": "do-work/working/REQ-002-shared-concurrency-primitives.md",
        "sha256": "..."
      }
    ]
  }
}
```

**API:**

- `write_claim(path, claim)` — backed by `atomic_write`.
- `read_claim(path) -> ClaimRecord` — returns a dataclass; raises `ClaimFormatError` on schema mismatch or invalid JSON.
- `capture_claim_tree_state(repo_root, *, claim_handle_or_path, scope_paths, expected_session_id=None)` — rewrites the scoped file snapshot while preserving claim-time `HEAD` and dirty-tree evidence.
- `verify_and_stage_claim_scope(repo_root, *, claim_handle_or_path, current_request_path, expected_session_id=None, now=None, stale_threshold=timedelta(minutes=2))` — REQ-010's commit gate: re-verify `HEAD`, reject foreign dirty paths, and stage only the scoped snapshot.

Claim lifecycle (when to create, refresh, release) is **not** defined here — that is per-action policy (REQ-004/REQ-007/REQ-009).

### 6.5 Session Record + Explicit Recovery Helpers

**Contract:** REQ-005's orphan recovery is explicit, evidence-based, and filesystem-visible. The library provides read/write helpers for session records plus an inspection/recovery API for work claims. The action decides when to call them (`do work resume`), but the library enforces the "no guessing" rule.

**Session record API:**

- `write_session_record(path, record)` — backed by `atomic_write`.
- `read_session_record(path) -> SessionRecord`
- `inspect_session_record(do_work_root, session_id) -> SessionRecord | None`

**Work-claim recovery API:**

- `inspect_work_claim_recovery(do_work_root, *, claim_path, now=None, stale_threshold=timedelta(minutes=2)) -> WorkClaimRecoveryInspection`
- `recover_orphaned_work_claim(do_work_root, *, claim_path, recovering_session_id, now=None, stale_threshold=timedelta(minutes=2)) -> RecoveredWorkClaim`

**Inspection verdicts:**

- `live` — claim heartbeat is still fresh; recovery is not allowed.
- `stale` — the claim is stale but the owning session still looks active or ambiguous (fresh session heartbeat, live PID, or conflicting evidence).
- `foreign-host` — the session record names a different hostname; recovery is not allowed on this machine.
- `missing-session-record` — the claim is stale but the originating session record is gone, so process absence cannot be proven.
- `recoverable` — claim heartbeat stale, session heartbeat stale, hostname matches, and PID absent on the current host.

`recover_orphaned_work_claim(...)` is intentionally narrow: it moves the REQ from `working/` back to the queue, writes a JSON log entry into `do-work/.recovery-log/`, removes the `.claim.json` sidecar, and deletes the originating session record. It raises `RecoveryNotAllowedError` if the evidence is anything other than `recoverable`.

### 7. Archival Helpers

**Contract:** Work and cleanup do not hand-roll the "is this the last REQ?" scan. They call the archival helpers, which take a short `ur-archival:<identifier>` lock, re-scan authoritative archive locations, and either perform the atomic parent move or return a clean "not ready / already archived" result.

**API:**

- `archive_completed_request(do_work_root, *, working_request_path, session_id, operation="work", now=None) -> RequestArchivalResult`
- `archive_user_request_if_complete(do_work_root, *, ur_id, session_id, operation="work", now=None) -> ParentArchivalResult`
- `archive_legacy_context_if_complete(do_work_root, *, context_ref, session_id, operation="work", now=None) -> ParentArchivalResult`

**Behavior notes:**

- `archive_completed_request(...)` always archives the REQ out of `working/` first. The "last one standing" decision happens only after that REQ is already in `do-work/archive/`.
- `archive_user_request_if_complete(...)` counts only already-archived REQs (`archive/` root or `archive/UR-NNN/`), never `status: completed` files still sitting in `working/`.
- A second caller racing the same UR or CONTEXT path does not raise on the happy path. It retries briefly, then sees `already-archived` once the winner finishes.
- Cleanup may call the same helpers with `operation="cleanup"` while holding its own short global lock; REQ-006 intentionally chose per-UR archival locks plus cleanup re-scan rather than making work wait on `cleanup-global`.

### 8. Standard Lock Scopes

**Canonical catalog.** Scope names are canonical and typed. Ad-hoc scope strings are rejected by `acquire_lock` via `ScopeError`. New scopes must be added to this document **and** to `SCOPES` in `lib/concurrency.py` before they can be used in action code.

| Scope name             | Used by     | Purpose                                                           |
|------------------------|-------------|-------------------------------------------------------------------|
| `id-allocation:req`    | REQ-003     | Short lock for atomic REQ ID allocation during capture.           |
| `id-allocation:ur`     | REQ-003     | Short lock for atomic UR ID allocation during capture.            |
| `req-claim:<REQ-id>`   | REQ-004     | Per-REQ lock when claiming a queue entry.                         |
| `ur-archival:<UR-id>`  | REQ-006     | Per-UR lock during final-REQ-done archival.                       |
| `verify-doc:<path>`    | REQ-007     | Per-document lock during verify-request/verify-plan.              |
| `cleanup-global`       | REQ-009     | Exclusive lock for the cleanup action.                            |
| `foreign-edit:<path>`  | REQ-010     | Per-document claim at claim-time / re-verify at commit-time.      |

Parameterized scopes (those with `:<...>`) accept any suffix; the library validates the scope **prefix** against the canonical list.

### 9. Deterministic Failure Messages

Every lock-acquisition failure carries:

- The attempting session's `session_id` and `operation`.
- The holder's `session_id`, `operation`, `acquired_at`, `last_heartbeat`.
- The lock path and scope.

Example `LockHeldError` message:

```
LockHeldError: lock 'do-work/.locks/req-claim:REQ-002.lock' is held
  scope:         req-claim:REQ-002
  held_by:       session 2026-04-18T00-10-00Z-deadbeef (operation: work)
  acquired_at:   2026-04-18T00:10:05Z
  last_heartbeat:2026-04-18T00:20:45Z
  attempting:    session 2026-04-18T00-21-52Z-f2d85402 (operation: work)
```

Actions surface this error verbatim to the user. Do not catch and translate.

## Exceptions

All raised from `lib.concurrency`:

- `LockHeldError` — second acquire on a live lock.
- `ClaimFormatError` — invalid claim JSON or missing fields.
- `SessionFormatError` — invalid session-record JSON or missing fields.
- `ScopeError` — unknown scope name.
- `ForeignReleaseError` — a lock handle no longer matches the on-disk lockfile.
- `StaleRenameError` — `ENOENT` during rename (source vanished).
- `CrossDeviceError` — `EXDEV` during rename (not same filesystem).
- `CollisionError` — destination already exists for `atomic_rename`.
- `AtomicWriteError` — I/O failure during `atomic_write` (disk full, permissions, etc.).
- `RecoveryNotAllowedError` — orphan recovery was attempted without unequivocal evidence.

## Debuggability Guarantee

A user troubleshooting the skill must be able to understand lock/claim state with `ls` and `cat` alone:

- `ls do-work/.locks/` reveals every held lock.
- `cat do-work/.locks/<scope>.lock` reveals the holder.
- `cat do-work/working/<something>.claim.json` reveals active claims (when REQ-004..REQ-009 land).
- `cat do-work/.recovery-log/<timestamp>-REQ-XXX.json` reveals every explicit orphan recovery event.

No binary formats. No hidden state outside the documented directories. No database.

## What This Library Does NOT Do

Out of scope for REQ-002 — each item is owned by a later REQ in the batch:

- **Claim lifecycle** (when to create/refresh/release claims) — REQ-004, REQ-007, REQ-009.
- **Orphan recovery decision** — REQ-005. This library emits the verdict; it does not act on it.
- **Capture rollback beyond placeholder allocation** — REQ-008 owns the multi-file transaction / cleanup policy after IDs are minted.
- **UR archival atomicity** — REQ-006 consumes `ur-archival:<UR-id>` scope.
- **Foreign-edit detection logic** — REQ-010 consumes `foreign-edit:<path>` scope.
- **Action wiring.** REQ-002 ships the primitives. Callers are introduced in REQ-003..REQ-010.
- **Debug CLI.** Skipped by design — `cat` is enough. The optional `do-work debug locks` subcommand flagged in the REQ's Open Questions is **not** shipped; callers use `inspect_lock` directly or `cat` the lockfile.

## Testability

All primitives are exercised by `lib/concurrency_test.py` against a `tempfile.TemporaryDirectory`. Tests include:

- Unit coverage: acquire/release/inspect, classify verdicts, atomic rename failure modes, atomic write visibility, claim round-trip, scope validation.
- Allocation coverage: authoritative-path scans for REQ and UR IDs, loud collision detection, cleanup on placeholder-write failure, and separate REQ/UR lock namespaces.
- Concurrency coverage: 20 `multiprocessing` workers race on one lock; exactly one wins, and 4 parallel REQ allocations serialize into 4 unique IDs.

Run from the repo root:

```bash
python3 -m unittest lib.concurrency_test -v
```

Tests must pass before any downstream REQ consumes the library. A failing concurrency test indicates the primitive is wrong — **fix the primitive, not the test**.

## Assumed Environment

- **Python 3.9+.** Uses dataclasses, `typing.Literal`, `os.PathLike`, stdlib only.
- **POSIX-ish filesystem.** Atomic rename within the same filesystem. `os.open(..., O_CREAT | O_EXCL)` semantics available.
- **Same-host, same-user.** PID liveness via `os.kill(pid, 0)`. Cross-host locking is out of scope.

## Gitignore

Lock roots are ephemeral runtime state and must never be committed. Add to `.gitignore`:

```
do-work/.locks/
```
