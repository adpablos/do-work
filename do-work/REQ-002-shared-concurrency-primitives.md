---
id: REQ-002
title: Shared concurrency primitives library
status: pending
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010]
batch: parallel-safety
---

# Shared Concurrency Primitives Library

## What

Build a single internal library of filesystem-based concurrency primitives that every do-work action will use: exclusive lockfiles, atomic renames, claim file format, and a standard way to acquire/release/inspect locks. Every other race-fix REQ in this batch consumes this library; none of them should be rolling their own locking.

## Detailed Requirements

- **Exclusive lockfile primitive.** Provide a single helper that acquires an exclusive lock on a given path using `O_EXCL` creation semantics (or equivalent) so a second caller on the same path fails fast instead of blocking silently. Must support: acquire, release, held-by-whom inspection.
- **Lock identity.** Every acquired lock records the session ID of the holder (from REQ-001). Reading the lockfile must reveal `{session_id, acquired_at, last_heartbeat, operation_name}`.
- **Stale lock detection.** Provide a single helper that reports whether an existing lock is live, stale, or orphaned. Never auto-delete a lock inside this helper — surface the verdict, let the caller decide per the recovery policy (see REQ-005).
- **Atomic rename helper.** Wrap `rename()` with an API that treats the operation as an atomic state transition (queue → working, working → archive, draft → committed). Callers must not use raw `mv` / `os.rename` for state transitions once this library exists.
- **Atomic file write.** Provide write-then-rename so partial-failure in the middle of writing a UR or REQ file never leaves a half-written file visible to readers.
- **Claim file format.** Define one on-disk schema for claim files — session ID, timestamps, operation name, affected paths — used by REQ-004 (work claim), REQ-007 (verify claim), REQ-009 (cleanup claim). One format, one parser.
- **Standard lock scopes.** The library names and exposes the scopes the actions will use (e.g., ID-allocation lock, per-document lock, global cleanup lock). Ad-hoc scope names sprinkled through action code are forbidden — add them here first.
- **Deterministic failure messages.** When a lock acquisition fails, the error must include the current holder's session ID and operation so the user sees a useful message (supports the "clear, actionable message" done-criterion).
- **Testability.** The primitives must be exercisable in isolation (unit-testable) without spinning up an actual do-work session. Concurrency tests for this library are in scope.

## Constraints

- **Filesystem-only.** `O_EXCL`, atomic rename, flock-style if helpful — no external services, no SQLite.
- **No new runtime dependencies.** Standard library only, or an already-vendored helper. New packages require user sign-off.
- **Shared primitives, per-action policy.** This REQ owns the primitives. Individual actions (REQ-003..REQ-010) decide where and at what scope to use them — the library does not assume.
- **Fail loud.** Lock acquisition failure produces an informative error, never silent skip.
- **Debuggable by `ls` + `cat`.** A user inspecting the state directory must be able to read and understand a claim or lockfile without running do-work code.

## Dependencies

- **Blocked by:** REQ-001 (session ID must exist — lock files record session_id).
- **Blocks:** REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010. Every race fix consumes this library.

## Builder Guidance

- **Certainty level: firm.** Batch constraint "shared primitives, per-action policy" is binding. Do not re-propose a per-action locking model.
- **This is a foundation REQ.** Resist the urge to wire it into any action in this REQ — that is what subsequent REQs are for. Ship the library with tests and stop.
- **Keep the API small.** A few obvious primitives beat a rich framework. If a helper is only used once, it can be inlined in the caller.
- **Document the scope names.** Subsequent REQs will need to refer to them unambiguously.

## Open Questions

- Exact location for the library in the repo structure (new `lib/` folder inside the skill, or embedded in `actions/`). Decide during planning.
- Whether to expose a tiny CLI (e.g., `do-work debug locks`) that lists current lock state. Useful for debugging but scope-creep risk — decide during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) for the complete PRD, including the binding architectural decision that the skill ship "shared primitives, per-action policy."

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (6/6 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "Build one internal library of locks / atomic moves / claim management" (Resolved decision — Concurrency model) | Detailed Requirements — Exclusive lockfile primitive, Atomic rename helper, Claim file format | Full |
| 2 | "Each action declares which primitives it uses and at what scope" (Resolved decision — Concurrency model) | Detailed Requirements — Standard lock scopes; Constraints — Shared primitives, per-action policy | Full |
| 3 | Filesystem-only: lockfiles, atomic renames, `O_EXCL` (Resolved decision — Coordination substrate) | Detailed Requirements — Exclusive lockfile primitive, Atomic rename helper; Constraints — Filesystem-only | Full |
| 4 | Clear, actionable messages instead of silence and corruption (Done-criteria #8) | Detailed Requirements — Deterministic failure messages | Full |
| 5 | "The folder *is* the state; `ls` must still be a legitimate debug tool" (Resolved decision — Coordination substrate) | Constraints — Debuggable by `ls` + `cat` | Full |
| 6 | "No locks, no claim tokens, no atomic state transitions" (Problem) | Detailed Requirements — Claim file format | Full |

*Verified by verify-request action*
