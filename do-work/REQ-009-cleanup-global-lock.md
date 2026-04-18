---
id: REQ-009
title: Cleanup contention via global short lock
status: pending
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002, REQ-006]
batch: parallel-safety
---

# Cleanup Contention via Global Short Lock

## What

The cleanup action reads directory state, decides what to move, then moves it — non-atomically. Two cleanups running at once, or cleanup running while work is completing REQs, each operate on stale views of the other's completed moves. This REQ serializes cleanup against itself and against the structural moves work performs, producing a consistent archive.

## Detailed Requirements

- **Global cleanup lock.** Cleanup takes a short global lock (REQ-002 primitives) for the duration of its scan-and-move cycle. Two concurrent cleanups do not interleave; the second gets a clear "cleanup already running in session X" error.
- **Cooperation with work-action archival.** The final-REQ archival in REQ-006 and the cleanup action's folder moves must not race. Either:
  - (a) cleanup's global lock is also respected by REQ-006's archival critical section, or
  - (b) cleanup re-scans after each move, holding a finer lock per target, and tolerates the work action archiving a UR between scan and move.
  Builder picks (a) or (b) during planning. Whichever is chosen, the outcome must be: no duplicate archive entries, no shadowed folders, no "missing file" errors from cleanup on a folder work just moved.
- **Short lock, not long.** Cleanup operations must not hold the global lock for long-running work. If a cleanup step is slow (e.g., archiving a huge UR), decompose: hold the lock for the index-updating decision, do the move under the atomic-rename primitive, release quickly. The global lock is about serializing decisions, not I/O throughput.
- **Claim file.** The cleanup claim records `{session_id, started_at, last_heartbeat, operation: "cleanup"}` per the UR-001 claim model.
- **Stale cleanup recovery.** If the global lock is held but its heartbeat is stale and the owning process is absent (evidence from REQ-005), it is recoverable by the same rules — fail loud, log, do not auto-recover if ambiguous.
- **Idempotency.** Cleanup must be safe to re-run on already-cleaned state. This is mostly a property of the existing action; the requirement here is to preserve it once locking is introduced.
- **No bypass for manual ops.** If a user runs `do work cleanup` while another session's cleanup is active, they get the clear "already running" message — not a silent second pass.

## Constraints

- **All batch constraints apply**, especially:
  - Filesystem-only.
  - Fail loud on contention.
- **No global lock on non-cleanup operations.** Capture, work claims, and verify do not take the global cleanup lock. Cross-scope contention only exists at the archival touch point with REQ-006.
- **Scope: `actions/cleanup.md`.** Touch the scan-and-move flow and the entry point, nothing else.

## Dependencies

- **Blocked by:** REQ-001 (session ID), REQ-002 (lock + atomic rename primitives).
- **Related:** REQ-006 (archival). The builder must coordinate with REQ-006 during planning to pick the (a)-vs-(b) strategy above.
- **Blocks:** none.

## Builder Guidance

- **Certainty level: firm.** Done-criterion #3 ("Folder moves are safe under contention: one winner, clean conflict") applies to cleanup too.
- **Target file: `actions/cleanup.md`.** Add the lock acquisition at the start and release on exit (including error paths). Leave the substantive cleanup logic intact where possible.
- **Decide (a) vs (b) early.** Option (a) is simpler but introduces a global-lock dependency in REQ-006. Option (b) is more complex but keeps REQ-006 lighter. The call affects REQ-006's planning, so coordinate.
- **Add a test** with two parallel cleanup invocations on the same state and assert exactly one proceeds and the archive is consistent.
- **Surface the lock in the CLI.** If the second cleanup sees the lock held, the message should tell the user which session holds it and when it started.

## Open Questions

- Whether to expose a "force" flag for emergency cleanup when a stale lock is detected but process-absence cannot be confirmed. The recovery policy says fail loud — pushing a force-flag in introduces an escape hatch that can be misused. Decide during planning; default is "no such flag."
- Exact interaction with `do work cleanup` vs a cleanup implicitly triggered at work-action end (if any). Document the boundary.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "Cleanup contention" is the seventh failure mode.

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (4/4 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "Cleanup reads directory state, decides what to move, then moves it — non-atomically" (Failure mode) | Detailed Requirements — Global cleanup lock, Short lock not long | Full |
| 2 | "If cleanup runs concurrently with work, or two cleanups run at once, each operates on a stale view" (Failure mode) | Detailed Requirements — Global cleanup lock, Cooperation with work-action archival | Full |
| 3 | "The archive ends up with duplicates, shadowed folders, or missing-file errors" (Failure mode) | Detailed Requirements — Cooperation with work-action archival, Idempotency | Full |
| 4 | "Cleanup takes a short global lock" (Resolved decision — Concurrency model) | Detailed Requirements — Global cleanup lock, Short lock not long | Full |

*Verified by verify-request action*
