---
id: REQ-010
title: Foreign-edit detection at claim and commit
status: completed
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002, REQ-004]
batch: parallel-safety
claimed_at: 2026-04-20T21:32:07Z
route: C
completed_at: 2026-04-20T21:42:50Z
---

# Foreign-Edit Detection at Claim and Commit

## What

Covers two tightly coupled failure modes from UR-001: "Commits absorb unrelated parallel changes" and "Shared working tree during implementation." The binding decision is to detect foreign edits at two well-defined moments — **claim time** and **commit time** — not continuously. The work action must snapshot its scope when it claims a REQ and re-verify that scope before it commits. `git add -A` must be replaced with a scoped-staging approach. Foreign changes cause a loud halt, not silent absorption.

## Detailed Requirements

- **Claim-time snapshot.** When the work action claims a REQ (REQ-004), capture:
  - The set of files the session intends to modify for this REQ (the "REQ scope"). If the exact set is not yet knowable (the plan may not exist yet), snapshot after planning completes, before implementation starts. Document the exact moment during planning.
  - The current `git` HEAD commit SHA.
  - Optionally, content hashes of the scoped files at claim time for stronger equality at commit.
  Store this snapshot in the claim file (REQ-002 claim format) or a sidecar record, named so it is associated with the claim.
- **Commit-time re-verification.** Before staging files for the final commit, re-verify that:
  - No file outside the REQ scope has been modified since the claim snapshot (excluding the REQ file itself and its archive motion, which are expected changes).
  - The HEAD has not advanced (or advanced only by this session's own commits).
  - Files *inside* the REQ scope have no unexpected modifications attributable to another session — ideally detected by hash/mtime comparison against the claim snapshot plus this session's own edits.
- **Replace `git add -A`.** Staging at commit must be scoped. Either `git add <explicit file list derived from the snapshot>` or equivalent — never `git add -A` or `git add .`. Foreign changes must not be staged.
- **Loud halt on foreign change.** If re-verification detects any foreign modification, the commit is aborted with a clear message naming:
  - The file(s) modified.
  - The claim's session ID and operation.
  - The remediation: investigate what else is running and either stop it or revert the foreign change before resuming.
- **Do not auto-revert.** The session does not try to "helpfully" undo foreign edits. Per UR-001 recovery policy: fail loud, surface to the user. The user decides how to resolve.
- **Legitimate concurrent edits are not in scope.** Two sessions both intentionally touching overlapping code is out of scope per UR-001. This REQ's job is to *detect* that the situation has occurred and halt, not to merge.
- **Tree-state contract on claim.** When claiming, if the working tree is already dirty with unrelated changes, the session either:
  - Refuses the claim with a clear message, or
  - Records the pre-existing dirty state in the claim snapshot so it can distinguish "this was here before I started" from "this appeared while I was working."
  Builder chooses during planning; the default should be refuse-and-surface per fail-loud policy.
- **Document the assumption.** Add to `actions/work.md` an explicit "Assumed working-tree state" paragraph that states what the action expects and what it does when the assumption is violated. This addresses done-criterion #7 directly.
- **Heartbeat-aware re-check.** If a session's own heartbeat has gone stale between claim and commit (e.g., the session was paused), treat this as a suspicious state and re-snapshot + re-verify as if starting fresh. Do not commit based on a possibly-overtaken claim.

## Constraints

- **All batch constraints apply**, especially:
  - Detection at claim and at commit only — no continuous polling during implementation (binding decision).
  - Fail loud on any detected foreign change.
  - Recovery policy: auto-recover only the unequivocal. Foreign edits are *not* unequivocal and must always surface.
- **Do not change the one-commit-per-request model** (out of scope).
- **No semantic merge** of overlapping edits (out of scope).
- **No distributed tree probing** — single machine, single filesystem.

## Dependencies

- **Blocked by:** REQ-001 (session ID, hostname in claim), REQ-002 (claim format to attach snapshot), REQ-004 (the claim whose snapshot this REQ populates).
- **Blocks:** none directly, but this is the REQ that earns the user's trust in the commit scope.

## Builder Guidance

- **Certainty level: firm.** Done-criteria #6 (commits contain only request work) and #7 (skill declares tree assumptions and refuses violations) are non-negotiable.
- **Target file: `actions/work.md`.** Specifically the claim step (where the snapshot is attached) and the commit step (where `git add -A` lives today and must be replaced).
- **Context — this is why the PRD was written.** The PRD flags a real incident: "a slash command and an unrelated permission tweak were absorbed into two different feature commits and had to be extracted by hand." The halting behavior is the whole point; do not soften it with "best-effort" language.
- **Write a test that simulates foreign edits.** Start a work claim, inject a change to a file outside the REQ scope from a different session, reach the commit step, assert the action halts with the expected message.
- **Be precise about "REQ scope."** It is the files the session actually edited under this claim, tracked during implementation, not a static list inferred up-front. The snapshot at claim time captures the *baseline*; what gets committed is the diff between baseline and commit time, filtered to files the session itself touched.

## Open Questions

- How to robustly track "which files this session edited" inside the work action — via `git diff` against the claim-time SHA, via explicit builder instrumentation, or via filesystem watch (expensive). Decide during planning; prefer `git diff` against the claim baseline.
- How to represent the claim snapshot on disk so it is inspectable (`ls`/`cat`) per the debuggability constraint. Likely a JSON sidecar next to the claim file.
- Exact behavior when a pre-existing dirty tree is detected at claim time: refuse by default, or warn-and-proceed with the dirty state recorded. Default per fail-loud is refuse; confirm during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "Commits absorb unrelated parallel changes" (8th failure mode) and "Shared working tree during implementation" (9th failure mode). The "What done looks like" section explicitly calls out commit scope and tree assumptions (#6 and #7).

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (8/8 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "At commit time the skill uses `git add -A` to stage 'everything'… those unrelated changes are swallowed into the request's commit" (Failure mode — Commits absorb) | Detailed Requirements — Replace `git add -A`, Commit-time re-verification | Full |
| 2 | "A slash command and an unrelated permission tweak were absorbed into two different feature commits and had to be extracted by hand" (Failure mode — Commits absorb, real incident) | Detailed Requirements — Loud halt on foreign change; Builder Guidance — why the PRD was written | Full |
| 3 | "Traceability breaks (`git blame` and `git log -- <path>` point at features the file has no relation to)" (Failure mode) | Detailed Requirements — Replace `git add -A`, Commit-time re-verification | Full |
| 4 | "The agent cannot trust the tree it is working on" (Failure mode — Shared working tree) | Detailed Requirements — Claim-time snapshot, Tree-state contract on claim, Heartbeat-aware re-check | Full |
| 5 | "The commit captures whatever the tree happens to contain at commit time — not what the agent built" (Failure mode — Shared working tree) | Detailed Requirements — Commit-time re-verification, Replace `git add -A` | Full |
| 6 | "Commits contain only the work that belongs to the request. Foreign changes… excluded cleanly, or the session refuses to commit" (Done-criteria #6) | Detailed Requirements — Replace `git add -A`, Loud halt on foreign change, Do not auto-revert | Full |
| 7 | "The skill is explicit about what working-tree state it assumes and refuses or warns when those assumptions are violated" (Done-criteria #7) | Detailed Requirements — Tree-state contract on claim, Document the assumption | Full |
| 8 | "At claim: snapshot the set of files the REQ intends to touch and the current HEAD. At commit: re-verify" (Resolved decision — Foreign-edit detection) | Detailed Requirements — Claim-time snapshot, Commit-time re-verification | Full |

*Verified by verify-request action*

## Triage

**Route: C** - Complex

**Reasoning:** This crosses the claim lifecycle, commit lifecycle, shared concurrency primitives, Git integration, tests, and the documented work-action contract. The change is localized to a few files, but the behavior spans multiple phases of the skill and has to fail loud instead of degrading silently.

## Plan

1. Extend the claim schema in `lib/concurrency.py` so a work claim can carry inspectable Git tree-state: claim-time `HEAD`, pre-existing dirty paths, and scoped file fingerprints attached to the `.claim.json` sidecar.
2. Add REQ-010 helpers in `lib/concurrency.py` for two policy points used by `actions/work.md`: freeze the scoped commit snapshot once the implementation files are known, then re-verify/stage only that scope at commit time while rejecting foreign edits, stale claim heartbeats, and moved `HEAD`.
3. Add isolated Git-backed tests in `lib/concurrency_test.py` for claim-time snapshotting, dirty-tree refusal, foreign-edit rejection between claim and commit, and scoped staging that proves `git add -A` is gone from the workflow contract.
4. Rewrite `actions/work.md` so the work action is explicit about its tree-state assumption, when the scope snapshot is frozen, and how commit-time staging is performed without absorbing unrelated changes. Update `actions/concurrency-primitives.md` to reflect the extended claim schema and new helper surface.
5. Finish the request log, bump `actions/version.md` to `0.19.0`, add the changelog entry, archive `REQ-010`, run the concurrency suite, and create one `[REQ-010] ...` commit.

## Plan Verification

**Source**: REQ-010 + UR-001 batch constraints (11 items enumerated)
**Pre-fix coverage**: 100% (11/11 items addressed)
**Post-fix coverage**: 100% (11/11 items addressed)

### Coverage Map

| # | Requirement / constraint | Plan step | Status |
|---|--------------------------|-----------|--------|
| 1 | Attach claim-time snapshot to the `.claim.json` sidecar | Step 1 | Full |
| 2 | Capture claim-time `HEAD` | Step 1 | Full |
| 3 | Re-verify at commit time before staging | Step 2 | Full |
| 4 | Reject files outside the REQ scope | Step 2 | Full |
| 5 | Replace `git add -A` with scoped staging | Step 2 + Step 4 | Full |
| 6 | Fail loud with clear remediation and no auto-revert | Step 2 + Step 4 | Full |
| 7 | Dirty-tree contract on claim must be explicit and refuse by default | Step 1 + Step 4 | Full |
| 8 | Heartbeat-aware commit re-check | Step 2 | Full |
| 9 | Document done-criterion #7 in `actions/work.md` | Step 4 | Full |
| 10 | Add a regression test for foreign edits between claim and commit | Step 3 | Full |
| 11 | Keep the change end-to-end: version, changelog, archive, single final commit | Step 5 | Full |

### Fixes Applied

- No plan gaps surfaced during verification; the initial plan already covered the code, tests, docs, and release hygiene required by REQ-010.

*Verified during implementation*

## Exploration

No extra codebase exploration was needed beyond the existing concurrency surfaces. The affected behavior was already concentrated in `lib/concurrency.py`, `lib/concurrency_test.py`, `actions/work.md`, and the shared concurrency contract docs.

## Implementation

- Extended `ClaimRecord` with an inspectable `tree_state` payload and added explicit claim-snapshot dataclasses for scoped file fingerprints.
- Updated `claim_work_request(...)` so Git-backed claims snapshot `HEAD` and refuse a dirty tree by default before the REQ is moved into `working/`.
- Added `capture_claim_tree_state(...)` to freeze the scoped commit snapshot after planning, and `verify_and_stage_claim_scope(...)` to re-check heartbeat freshness, `HEAD`, and foreign dirty paths before staging only the approved scope.
- Allowed the commit gate to ignore the workflow's own structural noise (`.claim.json`, queue→working rename lineage, archived REQ path) while still halting on truly foreign edits.
- Expanded the isolated concurrency suite with Git-backed tests that cover claim snapshots, dirty-tree refusal, foreign-edit rejection, and scoped staging.
- Rewrote the work-action contract to document the assumed tree state, the scope-freeze step, and the scoped commit gate that replaces `git add -A`.

## Testing

**Tests run:** `python3 -m unittest lib.concurrency_test -v`
**Result:** ✓ All tests passing (47 tests)

**Coverage added for REQ-010:**

- Claim-time snapshot is attached to the sidecar with claim-time `HEAD`
- Dirty tree at claim time fails loud and names the offending path
- Foreign edit introduced between claim and commit aborts with a clear message
- Scoped staging stages only the frozen snapshot paths instead of staging the whole tree

*Verified by work action*
