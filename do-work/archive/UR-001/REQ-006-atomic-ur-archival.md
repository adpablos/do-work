---
id: REQ-006
title: Atomic UR archival on final REQ completion
status: completed
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002, REQ-004]
batch: parallel-safety
claimed_at: 2026-04-20T21:16:19Z
route: C
completed_at: 2026-04-20T21:27:18Z
---

# Atomic UR Archival on Final REQ Completion

## What

When the final REQ belonging to a UR completes, the UR folder must be moved from `do-work/user-requests/UR-NNN/` to `do-work/archive/UR-NNN/` atomically, with exactly one session winning the transition. Today, two sessions finishing the last two REQs both evaluate "all done" as true and race to move the folder — one succeeds, the other errors or clobbers. That race must become impossible.

## Detailed Requirements

- **Atomic "last-one-standing" decision.** Deciding whether "all REQs for this UR are done" and moving the UR folder is a single critical section, protected by the REQ-002 primitives. The check and the move cannot be interleaved with another session's check for the same UR.
- **One winner, clean loser.** If two sessions both finish a REQ in the same UR at approximately the same time, exactly one of them wins the archival. The loser observes "someone else already archived it" and continues its own completion flow without error.
- **Atomic folder move.** Use REQ-002's atomic-rename helper. A partial move (some contents moved, some not) is not acceptable. If atomic `rename` across devices is not available, document that and fail loud rather than fake atomicity with `cp -r` + `rm -rf`.
- **Same protection for legacy context files.** The PRD explicitly calls out the same race for legacy context files shared across multiple REQs. Treat legacy `assets/CONTEXT-*.md` archival with the same atomic guarantees.
- **REQ archival ordering.** Archiving individual REQ files into the UR's archive folder (part of the normal work-action flow) must happen before the UR folder is moved. The "final REQ" detection runs after that REQ's own archive is complete.
- **Idempotent completion.** If archival has already happened (loser case), the remaining completion steps must recognize the new path and continue without failing. No "folder not found" errors on the loser.
- **Do not over-scope.** This REQ does not change what goes into the archive or rewrite the archival content rules. It only makes the existing transition concurrency-safe.

## Constraints

- **All batch constraints apply**, especially:
  - Filesystem-only (atomic rename or fail loud).
  - Fail loud — an archival-race-triggered clobber must never go silent.
- **File immutability rule still holds.** `archive/` contents remain immutable. This REQ only governs the transition *into* archive.
- **Scope: archival transition only.** Cleanup-triggered archival is covered by REQ-009, not here. This REQ is about the work-action flow when the last REQ of a UR completes.

## Dependencies

- **Blocked by:** REQ-001 (session ID recorded in archival lock), REQ-002 (atomic rename, exclusive lock), REQ-004 (claim lifecycle — the completing session holds the claim that triggered the archival check).
- **Blocks:** none directly. Related to REQ-009 (cleanup archival) which has its own lock scope.

## Builder Guidance

- **Certainty level: firm.** Done-criterion #3 ("Folder moves safe under contention: one session wins cleanly or both abort with a clear conflict") is non-negotiable.
- **Target files: `actions/work.md`** (archival step at the end of REQ processing). Keep edits tightly scoped to the "all related REQs complete → move UR" transition.
- **Do not introduce a new lock that outlives the archival moment.** The lock is held only for the check + move, and is released immediately. No long-held UR-level locks.
- **Write a reproducible concurrency test.** Simulate two "final completions" against the same UR and assert exactly one archival happens and the other returns cleanly.

## Open Questions

- Lock scope name: per-UR lock or global "archival" lock. Per-UR is finer-grained (preferred) but requires UR-identified lockfiles. Decide during planning.
- Behavior if the UR folder is already in `archive/` but the completing REQ is still in `working/` (corrupted prior state from before this REQ lands). Recovery policy says fail loud — document the exact message.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "Archival races on shared parents" is the fourth failure mode.

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (4/4 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "Two sessions completing the final pair of REQs in the same UR both evaluate 'all done' as true and both try to move the folder" (Failure mode) | Detailed Requirements — Atomic "last-one-standing" decision, One winner clean loser | Full |
| 2 | "One succeeds, the other errors or clobbers" (Failure mode) | Detailed Requirements — Atomic folder move, Idempotent completion | Full |
| 3 | "The same pattern exists for legacy context files shared across multiple REQs" (Failure mode) | Detailed Requirements — Same protection for legacy context files | Full |
| 4 | "Folder moves (archival, cleanup) are safe under contention: one session wins cleanly or both abort with a clear conflict. No half-moves, no silent duplicates" (Done-criteria #3) | Detailed Requirements — Atomic "last-one-standing" decision, Atomic folder move, One winner clean loser | Full |

*Verified by verify-request action*

## Triage

**Route: C** - Complex

**Reasoning:** The request changes the coordinated completion path across work and cleanup, introduces new shared archival helpers, and needs concurrency tests that prove the loser path stays clean.

*Recorded by work action*

## Plan

1. Add reusable archival helpers to `lib/concurrency.py` so work can archive the completed REQ first, then make the last-REQ decision under a short per-UR/shared-parent lock.
2. Choose option (b) with REQ-009: keep work on per-UR locks and document that cleanup must re-scan under its own global lock instead of forcing work through `cleanup-global`.
3. Cover the new behavior with direct archival tests plus a parallel race that proves one winner and clean losers.
4. Rewrite the work/cleanup docs to call the helper instead of hand-rolled `mv` logic.

*Generated by work action*

## Plan Verification

**Plan coverage:** 5/5 requirement clusters addressed

- Atomic last-one-standing decision: covered by the new archival helpers and the retrying per-UR/shared-parent lock.
- One winner, clean loser: covered by the parallel UR archival regression test and `already-archived` helper outcome.
- Atomic folder move + fail loud: covered by `atomic_rename(...)` usage and the explicit corrupted-state error when a UR is already archived while its REQ is still in `working/`.
- Legacy context protection: covered by the shared-parent archival helper for `context_ref`.
- REQ-009 coordination choice: covered by documenting option (b) in `actions/work.md`, `actions/cleanup.md`, and the primitive contract.

*Verified by work action*

## Exploration

- `actions/work.md` still described raw `mv` commands and counted `status: completed` files in `working/` as "done", which let the final-REQ decision race against another session's archive move.
- `actions/cleanup.md` still assumed it could act on a stale scan and even referenced `working/` during closure, which conflicts with cleanup's own "do not touch working" boundary.
- `lib/concurrency.py` already had the right substrate: `atomic_rename(...)`, typed scopes, and the reserved `ur-archival:*` scope prefix were present, but no caller was using them for archival yet.
- The clean coordination line with REQ-009 is option (b): work keeps fine-grained per-UR locks; cleanup keeps its global short lock and re-scans between structural moves.

*Generated by work action*

## Implementation Summary

- Added `archive_completed_request(...)`, `archive_user_request_if_complete(...)`, and `archive_legacy_context_if_complete(...)` to `lib/concurrency.py` with reusable result objects, frontmatter parsing, short archival-lock retries, and fail-loud duplicate-state checks.
- Wired the last-REQ rule so a REQ is archived out of `working/` first, and only then can the helper decide whether the UR or shared CONTEXT parent can move atomically.
- Added regression coverage in `lib/concurrency_test.py` for the happy path, not-ready path, corrupted pre-archived UR state, legacy CONTEXT archival, and a parallel UR race with exactly one archival winner.
- Updated `actions/work.md`, `actions/cleanup.md`, and `actions/concurrency-primitives.md` to document the helper-based flow and the explicit REQ-009 coordination choice.

*Completed by work action (Route C)*

## Testing

**Tests run:** `python3 -m unittest lib.concurrency_test`
**Result:** ✓ All tests passing (43 tests)

**Coverage added:**
- Request archival helper closes a UR when the last REQ finishes.
- Request archival helper leaves the UR open when another REQ is still unarchived.
- Corrupted "UR already archived while REQ still in working" state fails loud.
- Legacy shared CONTEXT archival works and is idempotent on re-entry.
- Parallel UR archival produces one winner and clean `already-archived` losers.

*Verified by work action*
