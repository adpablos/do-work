---
id: REQ-008
title: Capture atomicity and rollback
status: completed
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002, REQ-003]
batch: parallel-safety
claimed_at: 2026-04-20T22:49:30Z
route: C
completed_at: 2026-04-20T23:35:00Z
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

## Triage

Route C.

This changes the core capture protocol across runtime code and docs, adds a new repairable staging state, makes Step 5.5 part of the atomic unit, and needs crash-oriented regression coverage. The scope crosses ID allocation, staged publishing, rollback policy, and operator-facing repair behavior.

## Plan

1. Add a staged capture transaction to `lib/concurrency.py` with a readable manifest, staged UR/REQ allocation helpers, a resumable publish step, and explicit draft-preserving abort/repair flows.
2. Extend authoritative ID scans so staged captures reserve UR/REQ IDs until repair discards or finishes them. Document the chosen policy: failed drafts keep IDs reserved; clean discard returns them to the pool; interrupted publish resumes forward instead of rewinding.
3. Add regression tests for successful staged publish, fail-loud detection of pre-existing partial state, failed-draft discard with ID reuse, and a simulated crash after the first publish move.
4. Rewrite `actions/do.md` and `actions/concurrency-primitives.md` so Step 5 stages everything, Step 5.5 verifies staged REQs, Step 5.6 commits or preserves a failed draft, and repair is explicit and idempotent.

## Plan Verification

**Pre-fix coverage**: 100% (8/8 items)

### Coverage Map

| # | Requirement | Plan Step | Status |
|---|-------------|-----------|--------|
| 1 | Stage UR + REQs and publish only via atomic rename after everything is ready | Step 1 + Step 4 | Full |
| 2 | Roll back or mark failed before final commit | Step 1 | Full |
| 3 | Detect pre-existing partial state at capture start and fail loud or repair explicitly | Step 1 + Step 4 | Full |
| 4 | Decide/document whether allocated IDs return to the pool | Step 2 + Step 4 | Full |
| 5 | Make Step 5.5 verification part of the atomic unit | Step 1 + Step 4 | Full |
| 6 | Keep repair idempotent on a clean state | Step 1 + Step 3 + Step 4 | Full |
| 7 | Simulate a mid-capture crash and prove recoverable or clean rollback behavior | Step 3 | Full |
| 8 | Preserve verbatim UR input even when capture fails | Step 1 + Step 4 | Full |

## Exploration

- `actions/do.md` still published `user-requests/UR-NNN/input.md` and `REQ-NNN-*.md` directly during Step 5, then ran Step 5.5 afterward. A crash between those steps could leave visible partial capture state.
- `allocate_ur_input(...)` and `allocate_req_file(...)` only scanned final visible locations, so abandoned staging would have allowed silent ID reuse unless the scan surface changed.
- There was no capture-specific manifest, no capture-global coordination point, and no explicit repair helper analogous to REQ-005's claim recovery.
- The existing shared primitives already covered the two building blocks we needed: short lockfiles and atomic rename/write. The missing piece was transaction state that survives a crash and can be resumed or discarded explicitly.

## Implementation

- Added capture transaction primitives in `lib/concurrency.py`: `begin_capture_transaction(...)`, `allocate_staged_ur_input(...)`, `allocate_staged_req_file(...)`, `commit_capture_transaction(...)`, `abort_capture_transaction(...)`, `repair_capture_state(...)`, plus readable `CaptureManifest` / `CaptureItem` records and the new `capture-global` scope.
- Switched ID reservation semantics so `.capture-staging/CAP-*/...` counts as authoritative for both UR and REQ scans. Chosen policy: a preserved failed draft keeps its IDs reserved; explicit discard frees them; interrupted publish resumes forward instead of trying to reuse partially published IDs.
- Made commit resumable: if a crash happens after an `atomic_rename(...)` but before the manifest is updated, repair re-detects the already-published item from disk and continues publishing the remaining items.
- Rewrote the do-action contract so capture now stages first, verifies staged REQs in Step 5.5, and only publishes after verification is present everywhere. The docs now tell operators to fail loud on stale staging and use `repair_capture_state(...)` instead of silently deleting evidence.
- Updated the concurrency contract docs with the new staging/repair API, authoritative scan surface, `capture-global` scope, and the explicit ID-release decision.

## Testing

- Added regression coverage for successful staged capture commit into final UR/REQ locations.
- Added regression coverage for fail-loud startup when a failed `CAP-*` staging folder already exists.
- Added regression coverage for explicit repair that discards a failed draft and returns its IDs to the pool.
- Added regression coverage for a simulated crash after the first publish move, followed by repair that resumes the interrupted commit.
- Ran the full suite: `python3 -m unittest lib.concurrency_test -v` (`58` tests passed)
