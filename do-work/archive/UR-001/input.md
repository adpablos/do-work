---
id: UR-001
title: do-work runs safely in parallel
created_at: 2026-04-17T23:50:00Z
requests: [REQ-001, REQ-002, REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010]
word_count: 1611
---

# do-work runs safely in parallel

## Summary

The user routinely runs multiple do-work sessions in parallel — autonomous queue processors, ad-hoc capture, cleanup — alongside manual edits. The skill has no coordination primitives (no locks, no claim tokens, no atomic transitions, no session identity) and fails silently under that load: ID collisions, double-claimed REQs, stolen or orphaned work, archival races, commits that absorb foreign changes. This UR captures the full set of failure modes plus binding architectural decisions the user and I agreed on before capture.

## Extracted Requests

| ID | Title | Summary |
|----|-------|---------|
| REQ-001 | Session identity + heartbeat infrastructure | Every session generates a unique ID at start, writes heartbeat periodically. Foundation for claim tracking. |
| REQ-002 | Shared concurrency primitives library | One internal library of lockfiles, atomic renames, O_EXCL operations, claim file format — used by every action. |
| REQ-003 | Atomic ID allocation for REQs and URs | Replace scan-and-increment with lock-guarded allocation so parallel captures cannot collide. |
| REQ-004 | Atomic single-claim for REQs in work | Replace scan+move with atomic claim carrying session ID so only one session wins. |
| REQ-005 | Evidence-based orphan claim recovery | Recover a claim only when heartbeat is stale AND originating process is absent on this host — no clock guessing. |
| REQ-006 | Atomic UR archival on final REQ completion | Serialize the "last REQ done → move UR" transition so two sessions cannot race to archive the same folder. |
| REQ-007 | Verify-action serialization via document locks | Lock the target document during verify so parallel verifies cannot stomp each other's edits. |
| REQ-008 | Capture atomicity and rollback | Make UR+REQ creation all-or-nothing; on partial failure, clean up or leave recoverable breadcrumbs. |
| REQ-009 | Cleanup contention via global short lock | Serialize cleanup against itself and against work so stale directory scans cannot produce duplicates or shadowed folders. |
| REQ-010 | Foreign-edit detection at claim and commit | Snapshot intended files + HEAD at claim, re-verify at commit, refuse to absorb foreign changes via `git add -A`. |

## Batch Constraints

These apply to **every** REQ in this UR. They were agreed with the user before capture and are binding.

- **Coordination substrate: filesystem-only.** Lockfiles, atomic renames, `O_EXCL`. No SQLite, no separate index process. The folder *is* the state; `ls` must still be a legitimate debug tool. No REQ may introduce a runtime dependency that breaks this.
- **Claim model: session identity + heartbeat.** Every session generates a session ID on start and writes it into any claim it holds. Claim files record `{session_id, started_at, last_heartbeat}`. A claim is only recoverable when the heartbeat is stale *and* the originating process is not detectable on the current host. Pure TTL-expiry is forbidden.
- **Foreign-edit detection: at claim and at commit, not continuously.** At claim: snapshot the set of files the REQ intends to touch and the current HEAD. At commit: re-verify that no session outside the claim touched those files. No continuous polling during implementation.
- **Recovery policy: fail loud, auto-recover only the unequivocal.** Auto-recovery is allowed only when the evidence is unambiguous (e.g., orphan claim whose session's process no longer exists on this machine). Any ambiguous situation must surface to the user and halt — never guess.
- **Concurrency model: shared primitives, per-action policy.** REQ-002 builds the primitives; every other REQ uses them. Capture holds a short lock on ID allocation; work claims a single REQ; verify locks a single document; cleanup takes a short global lock.
- **Silent failure is the enemy.** When two sessions would do something incompatible, the second one must see a clear, actionable message — never silence and corruption later.
- **Scope:** single machine, single filesystem. Distributed locking is out of scope.
- **Scope:** the one-commit-per-request model is a design choice and is **not** to be changed.
- **Scope:** semantic merge of overlapping code changes between sessions is out of scope — this work is about structural safety of the skill's own state.

## Full Verbatim Input

# PRD: do-work runs safely in parallel

## Problem

`do-work` was designed assuming one session works on one request at a time on an otherwise-quiet working tree. Reality has diverged: the user routinely runs multiple do-work sessions in parallel — one processing the queue autonomously, another handling ad-hoc requests, sometimes a third doing cleanup or intake — alongside occasional manual edits. The skill has no coordination primitives for this. No locks, no claim tokens, no atomic state transitions, no session identity. Under that load, the result is silent corruption, lost work, and duplicated IDs — none of which surface as errors at the time they happen.

## Failure modes observed

Each of these is a distinct race or missing-primitive. They are listed roughly by how often they bite, not by priority.

### ID collisions on new REQs and URs
When a new request arrives, the skill picks the next available ID by scanning existing IDs and incrementing. Two sessions running capture in parallel both see the same highest ID and both mint the next one — two different requests with the same name, neither aware of the other. No atomic counter, no uniqueness enforcement.

### Two sessions claim the same REQ
The queue is a folder; a session "claims" a REQ by moving it from the queue folder to a working folder. The scan and the move are separate steps, so another session can grab the same file in between. Both sessions then implement the same request independently and produce conflicting commits, each claiming to have done the work.

### Orphaned claims from crashed sessions — or stolen from slow ones
An old `claimed_at` timestamp is treated as a signal that the claiming session died, and the REQ gets auto-unclaimed after an hour. There is no session identity attached to the claim, only the timestamp. A legitimate but slow session loses its work to the unclaim. A genuinely crashed session's REQ gets stolen before any recovery signal. The threshold is a guess that is wrong in both directions.

### Archival races on shared parents
When the last REQ in a UR completes, the UR folder is moved to archive. Two sessions completing the final pair of REQs in the same UR both evaluate "all done" as true and both try to move the folder — one succeeds, the other errors or clobbers. The same pattern exists for legacy context files shared across multiple REQs.

### Verify actions stomp each other
The verification steps read a REQ or UR, compute coverage, and write fixes back to the same file. Two sessions verifying the same document overwrite each other's work, or one reads a half-written state from the other. Nothing marks "verification in progress".

### Partial-failure orphans
Capture creates a UR folder, writes frontmatter listing the REQs, and creates each REQ file — in sequence, not atomically. If any step fails mid-flow, the remaining state is inconsistent: REQ files referencing a UR that doesn't list them, or a UR listing REQs that don't exist on disk. No rollback, no detection, no automatic repair. The next session may skip the orphans silently.

### Cleanup contention
Cleanup reads directory state, decides what to move, then moves it — non-atomically. If cleanup runs concurrently with work, or two cleanups run at once, each operates on a stale view of the other's completed moves. The archive ends up with duplicates, shadowed folders, or missing-file errors.

### Commits absorb unrelated parallel changes
At commit time the skill uses `git add -A` to stage "everything" — code changes, the archived request file, assets. If the working tree has concurrent edits, those unrelated changes are swallowed into the request's commit. The message reads "Implements REQ-XYZ" but the contents include foreign work. This has happened in practice: a slash command and an unrelated permission tweak were absorbed into two different feature commits and had to be extracted by hand. Traceability breaks (`git blame` and `git log -- <path>` point at features the file has no relation to), and rolling back the request also rolls back the foreign work.

### Shared working tree during implementation
During implementation, the agent reads source files, edits them, runs tests, and commits. If the user or another session edits those files mid-flow, the agent is running tests on code it didn't write, and the commit captures whatever the tree happens to contain at commit time — not what the agent built. The commit-absorption above is the tail end of this problem; the upstream version is that the agent cannot trust the tree it is working on.

## Why this matters

Every failure here is silent. There is no error at the time, no warning in a log — just two REQs with the same ID, or a commit that quietly reverted parallel work, or a UR that cannot be archived because the folder moved from under it. Symptoms surface days later as mysterious duplicates, missing work, or git history that no longer makes sense. By then, untangling is expensive and the original context is gone.

Parallel do-work is not a hypothetical future — it is the current reality, and the convenience of "fire up another session" is real. The alternative (single-threaded execution with a human scheduler) wastes the capability. The practical question is whether the skill earns trust under the load it actually sees, or whether every parallel run has to be audited afterward.

## What "done" looks like

- Two sessions running in parallel cannot create REQs or URs with colliding IDs. Uniqueness is structural, not best-effort.
- A REQ can only be claimed by one session at a time. Claims carry session identity so abandonment is detectable rather than guessed from a clock.
- Folder moves (archival, cleanup) are safe under contention: either one session wins cleanly or both abort with a clear conflict. No half-moves, no silent duplicates.
- Verify actions cannot overwrite each other's output. Either the skill serializes them or detects the conflict and fails loudly.
- Partial failures leave a recoverable state — fully applied or fully rolled back — never half-applied with no breadcrumbs.
- Commits contain only the work that belongs to the request. Foreign changes in the working tree are excluded cleanly, or the session refuses to commit until the situation is resolved.
- The skill is explicit about what working-tree state it assumes and refuses or warns when those assumptions are violated.
- When two sessions would do something incompatible, the second one sees a clear, actionable message — not silence and corruption later.

## Out of scope

- Distributed locking across machines — all sessions currently run on one machine and one filesystem.
- Performance work beyond what safety requires.
- Changing the one-commit-per-request model; that is a design choice, not a concurrency problem.
- Semantic merge of two parallel sessions implementing overlapping code changes. The goal is structural safety of the skill's own state, not general-purpose conflict resolution.
- Historical cleanup of commits already contaminated by this bug. That is user-initiated surgery, not skill behavior.

## Resolved decisions

These answers to the open questions below are **binding constraints** for every REQ. Do not re-open them inside individual REQ plans.

- **Coordination substrate: filesystem-only.** Lockfiles, atomic renames, `O_EXCL`. No SQLite, no separate index process. The folder *is* the state; `ls` must still be a legitimate debug tool.
- **Claim model: session identity + heartbeat.** Every session generates a session ID on start and writes it into the claim. The claim file records `{session_id, started_at, last_heartbeat}`. A claim is recoverable only when the heartbeat is stale *and* the originating process is not detectable on the current host. Pure TTL-expiry is not acceptable.
- **Foreign-edit detection: at claim and at commit.** At claim: snapshot the set of files the REQ intends to touch and the current HEAD. At commit: re-verify that no session outside the claim touched those files. No continuous polling during implementation.
- **Recovery policy: fail loud, auto-recover only the inequivocal.** Auto-recover is allowed only when the evidence is unambiguous (e.g., orphan claim with a session ID whose process no longer exists on this machine). Any situation that would require merging content, guessing intent, or choosing between two plausible states must surface to the user and halt.
- **Concurrency model: shared primitives, per-action policy.** Build one internal library of locks / atomic moves / claim management. Each action (capture, work, verify, cleanup) declares which primitives it uses and at what scope. Capture holds a short lock on ID allocation; work claims a single REQ; verify locks a single document; cleanup takes a short global lock.

## Open questions

- Is filesystem-based coordination (lockfiles, atomic renames, O_EXCL) sufficient, or does the skill need a small persistent index as its source of truth?
- Should the claim model be lease-based (TTL with heartbeat renewal) or explicit (session registers, emits heartbeats, releases on exit)?
- Where is the right layer to detect foreign edits — at claim time, at commit time, or continuously during implementation?
- How much should the skill try to recover inconsistent state automatically versus surfacing it for the user to resolve? Either bias has its own failure mode.
- Should verification, capture, work, and cleanup share a single concurrency model, or does each have different enough access patterns to warrant its own?

## Context notes

- Failure modes were identified by auditing the skill's action files. A detailed risk inventory with file/line references exists if useful; concrete references are intentionally omitted here to keep the PRD at the intent level.
- Several failure modes will surface more often as automation increases (scheduled sessions, loops, background agents). The cost of doing nothing grows over time.

---
*Captured: 2026-04-17T23:50:00Z*
