---
id: REQ-007
title: Verify-action serialization via document locks
status: pending
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002]
batch: parallel-safety
---

# Verify-Action Serialization via Document Locks

## What

Both verify flows — verify-request (coverage of REQ vs UR input) and verify-plan (coverage of REQ implementation plan) — read a document, compute coverage, and write fixes back to the same document. Today, two sessions verifying the same file can overwrite each other's edits or read half-written state. This REQ introduces per-document locks so verifies on the same target serialize cleanly.

## Detailed Requirements

- **Per-document exclusive lock.** Acquire a document-scoped lock (using REQ-002 primitives) on the target REQ or UR file for the full read-compute-write cycle. Two sessions pointed at the same document must serialize; verifies on different documents proceed in parallel.
- **Lock carries session identity.** Per UR-001 claim model, the verify lock records `{session_id, started_at, last_heartbeat, operation: "verify-request" or "verify-plan", target_path}`.
- **Hold for the whole cycle.** The lock is held from the point verify begins reading the document through the point the verification section has been rewritten atomically. Releasing earlier reintroduces the race.
- **Atomic write-back.** The final write of the `## Verification` section to the REQ file uses REQ-002's atomic write-then-rename helper so a reader during the window sees either the old content or the new content — never a half-written state.
- **Clear conflict message.** If the lock is already held, the second verify must fail with a readable message naming the holding session and the document path. No silent retry loop, no queueing behavior here — the user can re-run when the first finishes.
- **Applies to both verify flows.** verify-request (after capture or manual `do work verify`) and verify-plan (after planning inside the work action) both use this protocol. The verify-plan variant locks the in-flight REQ file it is rewriting.
- **Automatic verify in capture is covered too.** Step 5.5 of the do action runs verification automatically. That automatic run must take the same lock, not bypass it.
- **Do not lock the UR itself for verify-request.** The lock is on the *REQ file being updated*, since that is what gets written. The UR `input.md` is read-only during verify.

## Constraints

- **All batch constraints apply**, especially:
  - Filesystem-only.
  - Fail loud on contention.
- **No retry loops.** A failed lock acquisition returns an actionable error, not a wait-and-retry that can deadlock.
- **Scope: verify flows only.** The capture action's file creation is protected by REQ-003 / REQ-008; the work action's REQ file edits during planning are under the work claim from REQ-004. This REQ only adds the per-document lock specific to verify's read-modify-write pattern.

## Dependencies

- **Blocked by:** REQ-001 (session ID), REQ-002 (exclusive lock + atomic write primitives).
- **Blocks:** none.

## Builder Guidance

- **Certainty level: firm.** Done-criterion #4 ("Verify actions cannot overwrite each other's output") is non-negotiable.
- **Target files: `actions/verify-request.md`, `actions/verify-plan.md`, and the Step 5.5 block in `actions/do.md`.** All three flows must take the same lock type.
- **Keep the lock path derivation obvious.** E.g., `do-work/.locks/verify-REQ-NNN.lock` — the relationship between document and lock file should be predictable so a user debugging can find it with `ls`.
- **Add a test** that fires two verify-requests at the same REQ in parallel and asserts the second fails with an actionable message while the first produces correct output.

## Open Questions

- Lock-file naming convention for UR-level targets vs REQ-level targets. Decide during planning so it mirrors REQ-002's scope vocabulary.
- Whether verify-plan, which runs inside an already-claimed work context, needs an additional lock at all or is already serialized by the work claim on the same REQ. Likely yes (distinct operation), but justify during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "Verify actions stomp each other" is the fifth failure mode.

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (4/4 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "The verification steps read a REQ or UR, compute coverage, and write fixes back to the same file. Two sessions verifying the same document overwrite each other's work" (Failure mode) | Detailed Requirements — Per-document exclusive lock, Hold for the whole cycle | Full |
| 2 | "One reads a half-written state from the other" (Failure mode) | Detailed Requirements — Atomic write-back | Full |
| 3 | "Nothing marks 'verification in progress'" (Failure mode) | Detailed Requirements — Lock carries session identity | Full |
| 4 | "Verify locks a single document" (Resolved decision — Concurrency model) | Detailed Requirements — Per-document exclusive lock | Full |

*Verified by verify-request action*
