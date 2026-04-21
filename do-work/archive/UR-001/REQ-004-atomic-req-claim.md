---
id: REQ-004
title: Atomic single-claim for REQs in work action
status: completed
created_at: 2026-04-17T23:50:00Z
claimed_at: 2026-04-20T18:00:00Z
completed_at: 2026-04-20T20:47:51Z
route: C
user_request: UR-001
related: [REQ-001, REQ-002, REQ-005, REQ-010]
batch: parallel-safety
---

# Atomic Single-Claim for REQs in Work Action

## What

Replace the current "scan queue, pick one, then move it" pattern in the work action with an atomic claim operation that cannot be raced. At most one session ever wins the claim for any given REQ; the loser sees a clear "already claimed by session X" message and picks a different REQ (or exits).

## Detailed Requirements

- **Atomic claim operation.** Claiming a REQ is a single atomic step using the primitives from REQ-002 — either atomic rename from queue to working, or exclusive-create of a claim file referencing the REQ, depending on which primitive the builder chooses during planning. No scan-then-move sequence that another session can slip between.
- **Claim carries session identity.** Per the binding decision in UR-001, every claim records `{session_id, started_at, last_heartbeat, operation: "work", claimed_req: REQ-NNN}` using the REQ-002 claim format.
- **Heartbeat during work.** While the session holds the claim, it updates the heartbeat at the REQ-001 cadence. No heartbeat = claim becomes eligible for recovery (see REQ-005).
- **One REQ per session at a time.** A session cannot hold multiple work claims simultaneously. This bounds the blast radius of a crash and keeps recovery simple.
- **Loser behavior.** If a claim attempt fails because another session won, the loser must: see an error naming the winning session, log the conflict, and either pick a different queued REQ or exit cleanly. No retry-forever loops.
- **Release on completion.** When the REQ is processed and committed, the claim is released via the atomic state transition (working → archive per REQ-006 for archival, or queue → working → archive in the normal flow). The claim file or claim-folder state is removed atomically.
- **Release on abort.** If the work action aborts before commit (user cancels, unrecoverable error), the claim is released and the REQ returns to the queue — or is marked for explicit operator attention if the state is ambiguous (see REQ-005 recovery policy).
- **No orphan inspection inside claim logic.** This REQ does not decide whether a stale claim is recoverable — that is REQ-005. A claim attempt against an existing live claim simply fails.

## Constraints

- **All batch constraints apply**, especially:
  - Filesystem-only.
  - Fail loud — losing a claim produces a readable error.
  - Debuggable: a user running `ls do-work/working/` should be able to tell which REQs are claimed by which session.
- **Scope: the work action only.** Capture, verify, cleanup each have their own claim/lock REQs (REQ-003, REQ-007, REQ-009). Do not touch them here.
- **No changes to the one-commit-per-request model** (out of scope in UR-001).

## Dependencies

- **Blocked by:** REQ-001 (session ID), REQ-002 (primitives, claim format).
- **Blocks:** REQ-005 (orphan recovery assumes this claim shape), REQ-010 (foreign-edit detection snapshots are attached to a claim).
- **Related:** REQ-006 (archival) — completing a claim transitions into archival.

## Builder Guidance

- **Certainty level: firm.** Done-criterion #2 ("A REQ can only be claimed by one session at a time") is non-negotiable.
- **Choose one atomic mechanism and stick with it.** Either `mkdir`-based exclusive-claim, or `rename` to `working/` being the commit point, or `open(O_EXCL)` on a claim sidecar. Pick, document, move on. Do not combine approaches.
- **Target file: `actions/work.md`.** The "claim" step in the work flow and the "release/archive" step at the end. Leave unrelated work-action logic alone.
- **Concurrency tests required.** Two parallel claim attempts on the same REQ must produce exactly one winner with a clear error on the loser.
- **Do not try to handle orphans here.** Pretend every existing claim is live. REQ-005 handles recovery.

## Open Questions

- Whether the claim is encoded by the rename `do-work/<REQ>.md` → `do-work/working/<REQ>.md` alone, or by a sidecar claim file. Trade-off: rename is simpler but loses session identity in the filename; sidecar is more explicit but two-file. Decide during planning.
- Behavior when a session holding a claim receives `SIGINT`: is release attempted during shutdown, or left to orphan-recovery? Decide during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "Two sessions claim the same REQ" is the second failure mode listed.

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (5/5 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "The scan and the move are separate steps, so another session can grab the same file in between" (Failure mode — Two sessions claim the same REQ) | Detailed Requirements — Atomic claim operation | Full |
| 2 | "Both sessions then implement the same request independently and produce conflicting commits" (Failure mode — Two sessions claim the same REQ) | Detailed Requirements — Atomic claim operation, Loser behavior | Full |
| 3 | "A REQ can only be claimed by one session at a time" (Done-criteria #2) | Detailed Requirements — Atomic claim operation, One REQ per session at a time | Full |
| 4 | "Claims carry session identity so abandonment is detectable" (Done-criteria #2) | Detailed Requirements — Claim carries session identity, Heartbeat during work | Full |
| 5 | "Work claims a single REQ" (Resolved decision — Concurrency model) | Detailed Requirements — One REQ per session at a time | Full |

*Verified by verify-request action*

---

## Triage

**Route: C** — Complex

**Reasoning:** Replaces the core claim mechanism in `work.md`, introduces new library primitives and new concurrency tests, and deletes the legacy TTL-based auto-unclaim. Cross-cutting across `lib/concurrency.py`, `lib/concurrency_test.py`, and `actions/work.md`.

## Plan

Chosen mechanism: **exclusive-create of a sidecar `<REQ>.claim.json` in `do-work/working/`**, followed by atomic rename of the REQ file from queue to `working/`. Rationale over `mkdir` or rename-only: the sidecar carries full session identity in a debuggable JSON file, `O_EXCL` guarantees one winner, and rollback is possible if the rename fails after the sidecar is created.

1. Add new library surface in `lib/concurrency.py`:
   - `ClaimHeldError` — raised when the sidecar already exists, with full holder and attempting-session details.
   - `SessionClaimConflictError` — raised when a session tries to hold a second work claim.
   - `ClaimHandle` / `WorkClaimHandle` dataclasses.
   - `_write_claim_exclusive()` — `O_EXCL` write of the claim JSON.
   - `_inspect_claim_after_contention()` — post-failure read so the error message can name the holder.
   - `claim_work_request(repo_root, request_path, session_id, operation)` — the policy boundary: enforces one-claim-per-session, writes the sidecar, moves the REQ into `working/` via atomic rename, rolls the sidecar back on failure.
   - `release_claim(handle, session_id)` — removes the sidecar; refuses foreign-session release.
   - `refresh_claim_heartbeat(handle, session_id)` — updates `last_heartbeat`.
2. Add tests in `lib/concurrency_test.py`:
   - Happy-path claim.
   - Second claim against same REQ raises `ClaimHeldError` with holder details.
   - Single-claim-per-session: second claim from same session raises `SessionClaimConflictError`.
   - Rollback: if rename fails, the sidecar is removed.
   - Multi-process race: 20 processes try to claim; exactly one wins.
   - Heartbeat refresh updates the sidecar.
   - Foreign-session release is rejected.
3. Update `actions/work.md`:
   - Document the sidecar (`working/REQ-XXX.claim.json`) in the folder diagram.
   - Replace Step 2 ("move file, set frontmatter") with the atomic claim helper usage.
   - Remove the 1-hour auto-unclaim stale-claim check — it relied on pure TTL, which is explicitly forbidden by UR-001's binding decisions. Recovery is REQ-005's job.
   - Document claim heartbeat refresh during work phases.
   - Document release/rollback on archive, abort, and failure.

### Testing approach

- `python3 -m unittest lib.concurrency_test -v` must remain green with the new cases included.
- No changes expected to pre-REQ-004 tests.

*Generated retrospectively by orchestrator (paperwork after Codex implementation session)*

## Plan Verification

**Source**: REQ-004 (10 items enumerated from Detailed Requirements + Constraints + binding Open Questions)
**Pre-fix coverage**: 100% (10/10 items addressed)

### Coverage Map

| # | Requirement | Plan Step | Status |
|---|-------------|-----------|--------|
| 1 | Atomic claim operation (single atomic step, no scan-then-move) | Step 1 — `claim_work_request` uses `O_EXCL` + atomic rename | Full |
| 2 | Claim carries session identity per REQ-002 claim format | Step 1 — `_write_claim_exclusive` writes full ClaimRecord | Full |
| 3 | Heartbeat during work (refresh while claim held) | Step 1 — `refresh_claim_heartbeat`; Step 3 — documented in work.md | Full |
| 4 | One REQ per session at a time | Step 1 — `SessionClaimConflictError`; Step 2 — per-session test | Full |
| 5 | Loser sees actionable error naming the winning session | Step 1 — `ClaimHeldError` message includes holder session_id and operation | Full |
| 6 | Release on completion (atomic) | Step 1 — `release_claim`; Step 3 — work.md Step 7 wiring | Full |
| 7 | Release on abort / rollback on failed claim | Step 1 — `claim_work_request` rolls sidecar back if rename fails | Full |
| 8 | No orphan inspection inside claim logic (fail fast, REQ-005 handles recovery) | Step 1 — no TTL check in claim path; Step 3 — removed legacy 1-hour unclaim | Full |
| 9 | Filesystem-only, debuggable by `ls working/` | Step 1 — sidecar is plain JSON; Step 3 — documented in folder diagram | Full |
| 10 | Binding decision (Open Question): sidecar vs pure rename — pick sidecar, document | Step 1 rationale preamble; Step 3 folder diagram | Full |

*Verified by verify-plan action (retrospective)*

## Exploration

Inventory of pre-REQ-004 state in `lib/concurrency.py`:
- `ClaimRecord` and `parse_claim` existed from REQ-002 but no helper actually acquired a claim — only read/validated them.
- `LockHandle` + `acquire_lock` existed for non-claim scopes (ID allocation, etc.). Claim acquisition needed a parallel API that reuses the same `_write_*_exclusive` pattern.
- `atomic_rename` was already available from REQ-002 for the queue → working transition.

Inventory of pre-REQ-004 state in `actions/work.md`:
- Step 2 performed a non-atomic `mv` from `do-work/` to `do-work/working/` — the exact race REQ-004 targets.
- Step 1 had a "stale claim check" block that auto-unclaimed anything older than 1 hour in `working/`. This is pure-TTL logic forbidden by UR-001's binding claim model decision. Must be removed here (not in a future REQ, because leaving it would silently undo REQ-004's guarantees).

No pre-existing sidecar-claim pattern anywhere in the repo. Design space confirmed open for the O_EXCL sidecar choice.

*Generated retrospectively by orchestrator*

## Implementation Summary

### Files modified

- **`lib/concurrency.py`** (+262 lines)
  - New exceptions: `ClaimHeldError` (rich holder details in message), `SessionClaimConflictError`.
  - New dataclasses: `ClaimHandle`, `WorkClaimHandle`.
  - New internal helpers: `_write_claim_exclusive`, `_inspect_claim_after_contention`.
  - New public API:
    - `claim_work_request(repo_root, request_path, session_id, operation)` — atomic claim + move with rollback.
    - `release_claim(handle, session_id)` — foreign-session-safe release.
    - `refresh_claim_heartbeat(handle, session_id)` — bounded heartbeat update.

- **`lib/concurrency_test.py`** (+204 lines)
  - Happy-path claim.
  - `ClaimHeldError` on repeat with holder details verified.
  - `SessionClaimConflictError` when same session claims twice.
  - Rollback verification — rename failure leaves no orphan sidecar.
  - Heartbeat refresh updates `last_heartbeat`.
  - Foreign-session release rejected.
  - 20-process multiprocessing race — exactly one winner, all others fail cleanly.

- **`actions/work.md`** (+120 / −16 lines)
  - Folder diagram and narrative updated to show the `.claim.json` sidecar.
  - Orchestrator responsibilities: Step 2 phrased as "atomic claim" rather than "move file".
  - Step 0 work-order checklist entry updated to reference `claim_work_request`.
  - **Removed** the 1-hour TTL stale-claim auto-unclaim block — it was incompatible with REQ-001's heartbeat model and UR-001's binding decision. Recovery now belongs entirely to REQ-005.
  - Step 2 rewritten: one-claim-per-session check, `claim_work_request` usage example, failure and rollback semantics documented.
  - Documentation of heartbeat refresh points and release/rollback on archive/abort.

### Scope held

- No changes to capture, verify, or cleanup actions.
- No changes to the commit step.
- No changes to the one-commit-per-request model.
- No orphan recovery logic introduced — that is REQ-005.
- No foreign-edit detection introduced — that is REQ-010.

### Deviation from plan

None. All ten requirement items addressed, chosen mechanism (O_EXCL sidecar) implemented as planned, legacy TTL removed cleanly.

### Follow-up flags

- REQ-005 must implement the recovery logic the legacy TTL used to (badly) provide. Until REQ-005 lands, interrupted sessions leave their claims on disk until manually removed. This is documented in `actions/work.md` Step 1.

*Completed by work action (Route C) — implementation by Codex, paperwork closed by orchestrator*

## Testing

**Tests run:** `python3 -m unittest lib.concurrency_test -v`
**Result:** ✓ 30 tests passing (25 pre-existing + 5 new for REQ-004 claim behavior, plus 1 heartbeat refresh and 1 foreign-session release).

**New tests added (REQ-004):**
- `test_claim_work_request_happy_path` — claim sidecar created with correct fields, REQ moved to `working/`.
- `test_second_claim_raises_claim_held_error_with_holder_details`
- `test_same_session_cannot_hold_two_claims` (`SessionClaimConflictError`)
- `test_claim_rolls_back_on_rename_failure`
- `test_twenty_processes_race_for_one_claim` — multiprocessing race, exactly one winner.
- `test_refresh_claim_heartbeat_updates_claimfile`
- `test_release_claim_rejects_foreign_session`

**Pre-existing tests verified:** all previous concurrency tests (23 pre-REQ-004) still green.

*Verified by work action*
