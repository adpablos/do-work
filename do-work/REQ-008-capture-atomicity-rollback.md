---
id: REQ-008
title: Capture atomicity and rollback
status: pending
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002, REQ-003]
batch: parallel-safety
---

# Capture Atomicity and Rollback

## What

Today the do action creates a UR folder, writes the UR frontmatter, and creates each REQ file in sequence. If any step fails mid-flow, the remaining state is inconsistent and nobody detects it. This REQ makes capture all-or-nothing — either the full UR + all REQ files + cross-references land on disk, or the partial state is cleaned up or left with breadcrumbs that explicit recovery can repair.

## Detailed Requirements

- **All-or-nothing capture.** A successful capture produces: the UR folder with a fully-populated `input.md` including the final `requests: [...]` array, all REQ files referenced, each with `user_request: UR-NNN` set, and each with its `## Verification` section written by Step 5.5. Anything less is a partial failure and must be handled by this REQ's rollback path.
- **Staged write, atomic commit.** Write the UR folder and REQ files under a temporary state (e.g., a `capturing/` staging folder, or hidden `.tmp-UR-NNN/` next to the target) and transition them to their final names via the REQ-002 atomic rename helper only after every file has been written successfully. Readers never see half-formed state.
- **Automatic rollback on failure.** If any step fails before the final commit (disk full, crash before Step 5.5 completes, permissions, interrupt), the staging state is removed or marked as failed. The allocated REQ/UR IDs are released back for reuse, or — if release is unsafe — recorded in a failure log so the operator knows which IDs were burned.
- **Breadcrumbs on unrecoverable failure.** If the session crashes such that cleanup cannot run in-process, leave enough evidence on disk that an explicit `do work resume`-style recovery (or a dedicated repair command) can detect the partial state and either complete it or remove it. No silent orphans.
- **Detection of pre-existing partial state.** On capture start, detect leftover staging from a prior crashed capture and either repair it or fail loud with a clear message pointing the user at the repair command. Do not silently ignore.
- **Step 5.5 is part of the atomic unit.** Verification is mandatory (`actions/do.md`). A capture that created REQs but crashed before Step 5.5 wrote their `## Verification` sections is still partial — the commit does not happen until every REQ has its verification appended.
- **Preserve the UR verbatim input even on failure.** If the capture produced the UR's `input.md` but failed on the REQs, the verbatim input is valuable enough that rollback should optionally preserve it as a recoverable draft rather than delete it. Builder discretion during planning.
- **Idempotent repair.** A repair step must be safe to re-run. Running it on a clean state is a no-op.

## Constraints

- **All batch constraints apply**, especially:
  - Fail loud on any detected inconsistency.
  - Filesystem-only.
  - Recovery policy: auto-recover only the unequivocal; ambiguous partial state surfaces for the user.
- **No backwards-incompatible change to the final UR / REQ format.** The atomicity is how they land, not what they contain.
- **Scope: the do action.** Do not try to fix partial-failure in the work or verify actions here — those have their own REQs for claim/lock atomicity.

## Dependencies

- **Blocked by:** REQ-001 (session ID — attached to any staging folder to distinguish crashed sessions), REQ-002 (atomic rename + atomic write + lock primitives), REQ-003 (atomic ID allocation — capture cannot be atomic if ID allocation itself is racy).
- **Blocks:** none directly.

## Builder Guidance

- **Certainty level: firm.** Done-criterion #5 ("Partial failures leave a recoverable state — fully applied or fully rolled back — never half-applied with no breadcrumbs") is non-negotiable.
- **Target file: `actions/do.md`.** Step 5 and Step 5.5 structure needs to change to a staged-then-commit flow. Do not rewrite unrelated parts of the capture workflow.
- **Choose staging-folder or suffix-rename; document the choice.** Either approach works — decide explicitly and write it down so readers of the action file can follow.
- **Think about `SIGINT` during capture.** Graceful release is nice; the correctness requirement is that the state left on disk is either complete or detectably partial.
- **Do not conflate with REQ-005.** REQ-005 recovers *work-action claims*. This REQ recovers *capture-staging folders*. Different primitives, different state.

## Open Questions

- Whether partial-capture repair is folded into `do work resume` (existing command per CHANGELOG 0.12.4) or deserves its own entry point. Decide during planning.
- On ID release: if REQ-003 minted REQ-017 and capture failed after that mint, is REQ-017 safely reusable, or is it burned? Trade-off between debuggability and ID dense-packing. Decide during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "Partial-failure orphans" is the sixth failure mode.

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (5/5 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "Capture creates a UR folder, writes frontmatter listing the REQs, and creates each REQ file — in sequence, not atomically" (Failure mode) | Detailed Requirements — All-or-nothing capture, Staged write atomic commit | Full |
| 2 | "REQ files referencing a UR that doesn't list them, or a UR listing REQs that don't exist on disk" (Failure mode) | Detailed Requirements — All-or-nothing capture, Step 5.5 is part of the atomic unit | Full |
| 3 | "No rollback, no detection, no automatic repair. The next session may skip the orphans silently" (Failure mode) | Detailed Requirements — Automatic rollback on failure, Breadcrumbs on unrecoverable failure, Detection of pre-existing partial state | Full |
| 4 | "Partial failures leave a recoverable state — fully applied or fully rolled back — never half-applied with no breadcrumbs" (Done-criteria #5) | Detailed Requirements — All-or-nothing capture, Breadcrumbs on unrecoverable failure, Idempotent repair | Full |
| 5 | Fail loud on ambiguous partial state (Resolved decision — Recovery policy) | Detailed Requirements — Detection of pre-existing partial state | Full |

*Verified by verify-request action*
