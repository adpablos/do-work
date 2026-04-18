---
id: REQ-004
title: Atomic single-claim for REQs in work action
status: pending
created_at: 2026-04-17T23:50:00Z
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
