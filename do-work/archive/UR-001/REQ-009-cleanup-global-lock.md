---
id: REQ-009
title: Cleanup contention via global short lock
status: completed
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002, REQ-006]
batch: parallel-safety
claimed_at: 2026-04-20T22:03:44Z
route: C
completed_at: 2026-04-20T22:10:07Z
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

## Triage

**Route: C** - Complex

**Reasoning:** The change introduces a new cleanup-specific concurrency contract, touches the shared primitives module, and needs a real race test plus action-doc rewrites so the execution model is unambiguous.

*Recorded by work action*

## Plan

1. Add cleanup-specific primitives to `lib/concurrency.py`: one short global lock helper, one lightweight cleanup claim file, heartbeat helpers, and explicit fail-loud behavior when stale state is ambiguous.
2. Lock in option `(b)` from REQ-006/REQ-009: cleanup re-scans between structural moves under its own global lock, while shared parent moves stay protected by REQ-006's per-UR archival lock.
3. Cover the new behavior in `lib/concurrency_test.py`, including the required two-cleanup race where one run wins and the other reports the holder session.
4. Rewrite `actions/cleanup.md` and `actions/concurrency-primitives.md` so the runtime contract matches the code and explicitly forbids a force bypass.

*Generated by work action*

## Plan Verification

**Plan coverage:** 6/6 requirement clusters addressed

- Global cleanup lock: covered by the new `claim_cleanup(...)` path over `cleanup-global`.
- Cleanup claim file shape: covered by a dedicated cleanup-claim record with `{session_id, started_at, last_heartbeat, operation}`.
- REQ-006 coordination choice `(b)`: covered by the doc updates and the explicit "scan once, move once, re-scan" contract.
- Short-lock discipline: covered by the helper/docs split between decision windows and `atomic_rename(...)` for the actual move.
- Idempotency + no force flag: covered by fail-loud handling when stale cleanup state is ambiguous and by keeping cleanup on re-scan semantics instead of destructive retries.
- Parallel cleanup proof: covered by the two-process regression test requiring one winner and a loser message naming the winner session.

*Verified by work action*

## Exploration

- The repo does not ship an executable cleanup runner yet; cleanup behavior lives in `actions/cleanup.md`, while the runnable concurrency surface lives in `lib/concurrency.py`.
- `lib/concurrency.py` already had the right base pieces for REQ-009: typed scopes, `acquire_lock(...)`, `refresh_heartbeat(...)`, `atomic_rename(...)`, and the REQ-006 archival helpers that cleanup can call with `operation="cleanup"`.
- REQ-006 had already documented option `(b)`, so the missing piece was not archival logic in work but a cleanup-specific entrypoint that serializes cleanup against itself and records ownership clearly on disk.
- The existing generic `ClaimRecord` is REQ/file-scoped; cleanup needed its own lightweight global claim format because it claims the cleanup run itself rather than a specific REQ path.

*Generated by work action*

## Implementation

- Added cleanup-specific primitives in `lib/concurrency.py`: `CleanupClaimRecord`, `CleanupClaimHandle`, `claim_cleanup(...)`, `refresh_cleanup_claim_heartbeat(...)`, `refresh_cleanup_heartbeat(...)`, `release_cleanup_claim(...)`, and `release_cleanup(...)`.
- Wired cleanup ownership to two on-disk breadcrumbs: `do-work/.locks/cleanup-global.lock` for exclusion and `do-work/.claims/cleanup.claim.json` for the lightweight `{session_id, started_at, last_heartbeat, operation}` claim.
- Made ambiguous stale state fail loud: if a cleanup claim file already exists without a clean acquisition path, cleanup now stops and points the caller at the explicit REQ-005 recovery path instead of guessing or offering a `force` bypass.
- Rewrote `actions/cleanup.md` to describe the real execution pattern: claim cleanup, scan under the short global lock, perform one `atomic_rename(...)`, then re-scan before the next structural move.
- Updated `actions/concurrency-primitives.md` so cleanup's global lock/claim API is documented separately from the REQ-scoped work/verify claim schema.

*Completed by work action (Route C)*

## Testing

**Tests run:** `python3 -m unittest lib.concurrency_test -v`
**Result:** ✓ All tests passing (51 tests)

**Coverage added:**
- Cleanup claim writes both the global lock and the lightweight cleanup claim with the expected fields.
- Cleanup claim heartbeat refresh updates the on-disk `last_heartbeat`.
- Pre-existing cleanup claim state fails loud and points to the REQ-005 recovery path.
- Two parallel cleanups produce exactly one winner, and the loser error names the winning session while leaving no stray lock/claim files behind.

*Verified by work action*
