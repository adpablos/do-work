---
id: REQ-001
title: Session identity and heartbeat infrastructure
status: completed
created_at: 2026-04-17T23:50:00Z
claimed_at: 2026-04-17T23:57:00Z
completed_at: 2026-04-18T00:05:00Z
route: C
user_request: UR-001
related: [REQ-002, REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010]
batch: parallel-safety
---

# Session Identity and Heartbeat Infrastructure

## What

Every do-work session must generate a unique session ID at start and emit a periodic heartbeat while it is active. This is the foundation every other REQ in `parallel-safety` builds on — claims, orphan recovery, and foreign-edit detection all need stable session identity.

## Detailed Requirements

- **Session ID generation.** On session start, generate a unique session ID. It must be unique across concurrent sessions on the same machine and identifiable enough that a human reading a claim file can tell which session holds it. Include at minimum a random component and a creation timestamp.
- **Process identification.** The session ID must be accompanied by enough host information to later verify whether the originating process is still alive on the current machine. Required fields include OS PID and hostname (single-machine scope per UR-001 batch constraints).
- **Heartbeat mechanism.** While a session is doing any coordinated work (holding a claim, running capture, running verify, running cleanup), it must update a heartbeat timestamp at a bounded cadence. The cadence must be short enough that stale-claim detection is responsive and long enough that the filesystem is not beaten up. Default: heartbeat every 30 seconds, stale threshold 2 minutes — tunable, documented, not guessed at each call site.
- **Heartbeat scope.** The heartbeat is per-session, not per-claim. One heartbeat file per session; individual claims reference the session ID. A session with multiple active claims should not be writing one heartbeat per claim.
- **Session start-up contract.** Every action entry point (do / work / verify / cleanup) must obtain a session ID before taking any coordinated action. Reusing a stale session ID from a prior run is forbidden.
- **Graceful release.** When a session exits normally, it should release its session record so recovery logic does not have to prove it is dead. Crash-exit must still be recoverable via process-absence evidence (see REQ-005).
- **Debuggability.** Session records live on disk in a location that is inspectable with `ls` and `cat` (filesystem-only batch constraint). No hidden state.

## Constraints

- **Filesystem-only.** No SQLite, no separate index process, no daemon. Session and heartbeat state lives in plain files.
- **Single machine, single filesystem.** No cross-host coordination needed.
- **Fail loud.** If a session cannot write its session record (disk full, permissions, etc.), it must refuse to proceed — not silently continue without identity.
- **Debuggable by `ls`.** A user inspecting the state directory must be able to tell which sessions are live.
- **No runtime daemon.** The heartbeat is emitted by the action itself during coordinated work; it is not a separate background process that outlives the session.

## Dependencies

- **Blocks:** REQ-002, REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010. Every other REQ in this batch assumes session identity exists.
- **Blocked by:** none. This is the first foundational REQ.

## Builder Guidance

- **Certainty level: firm.** The user resolved this as a binding decision (claim model: session identity + heartbeat). Do not re-open lease-vs-explicit or suggest a TTL-only model.
- **Keep the scheme boring.** Random hex + timestamp + hostname + PID in a JSON or YAML file is enough. Over-engineering this slows everything that depends on it.
- **This REQ is an enabler.** Do not try to also fix claims, IDs, or cleanup here — those are separate REQs. Ship the identity + heartbeat primitive and stop.

## Open Questions

- Exact on-disk location for session records. Suggested: `do-work/.sessions/<session_id>.json`. Subject to builder discretion as long as debuggability and filesystem-only constraints are met.
- Heartbeat emission strategy for long-blocking operations (e.g., a slow shell command inside work): is the heartbeat refreshed before and after, or is a background emitter acceptable within the process lifetime? Decide during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) for the complete PRD, including all failure modes this infrastructure exists to fix and the binding architectural decisions.

---
*Source: See UR-001/input.md for full verbatim input*

## Triage

**Route: C** — Complex

**Reasoning:** Foundational infrastructure introducing a new cross-cutting protocol (session identity + heartbeat) that every subsequent REQ in this batch depends on. Touches every action's entry point and introduces a new on-disk format. No existing pattern to follow in the repo — the primitive does not exist yet.

## Plan

### Nature of this skill

`do-work` is a **markdown-instruction skill**. Actions are `.md` files that instruct an agent (Claude, etc.) how to behave. "Implementing" REQ-001 means adding a documented protocol that agents will follow, plus the on-disk file schema that any future code or agent will consume. No executable runtime is introduced here — that is deliberate and consistent with the filesystem-only batch constraint.

### Deliverables

1. **New file: `actions/session-identity.md`** — the canonical protocol document. Covers:
   - Session ID format and generation rules
   - On-disk record format (`do-work/.sessions/<session_id>.json`)
   - Required fields: `session_id`, `started_at`, `last_heartbeat`, `hostname`, `pid`, `operation`
   - Heartbeat cadence (30 s default) and stale threshold (2 min default), both tunable
   - Session-start contract (every action establishes identity before any coordinated step)
   - Graceful release on normal exit; recovery-evidence contract on crash
   - Debuggability guarantee (`ls`/`cat` inspection)
   - Explicit statement of what is **not** in REQ-001 (claims, locks, foreign-edit detection — those live in later REQs)

2. **`SKILL.md` routing update:**
   - Add a new section pointing to `actions/session-identity.md` under Action References
   - Add a "Session protocol" note in the routing section: every action must establish session identity at start
   - No routing-table changes needed (no new user-facing command)

3. **Step 0 updates in existing action files:** the work-order checklist at the top of each action gets a new first item "Establish session identity per `actions/session-identity.md`". Files to touch:
   - `actions/do.md` (Step 0 checklist)
   - `actions/work.md` (Step 0 checklist)
   - `actions/verify-request.md` (any top-of-file checklist or introductory step)
   - `actions/verify-plan.md` (same)
   - `actions/cleanup.md` (same)
   - `actions/version.md` — read-only version reporting; add only if it does coordinated work. Most likely skip with a note.

4. **`.gitignore` update:** add `do-work/.sessions/` so live session records are not committed. Heartbeat files are ephemeral runtime state.

5. **Version + CHANGELOG:** bump `actions/version.md` (minor: new protocol file), add changelog entry per `CLAUDE.md` conventions.

### Explicit non-goals (stay out of this REQ)

- **No changes to the claim format** beyond defining the session-record format. REQ-002 builds the claim format on top of this.
- **No wiring into actual locking logic.** REQ-004 (work claim), REQ-007 (verify lock), REQ-009 (cleanup lock), etc., consume the session ID — they do not belong here.
- **No orphan recovery logic.** REQ-005 owns that.
- **No heartbeat-emission inside long-running tools.** Address only in REQ-001's protocol doc as guidance — concrete implementation is per-action and can mature later.

### Implementation order

1. Write `actions/session-identity.md` (the source of truth).
2. Update `SKILL.md` to reference it.
3. Update each action's Step 0 to pre-pend "Establish session identity."
4. Update `.gitignore` to exclude `do-work/.sessions/`.
5. Bump version, update CHANGELOG.
6. Verify by reading back each touched file and confirming the protocol reference is present and consistent.

### Testing approach

No executable test infrastructure exists for this markdown skill. Testing = manual verification:
- Re-read each edited file and confirm the session-identity reference is present and internally consistent.
- Grep for stale "claimed_at older than 1 hour" TTL-only logic that would conflict with REQ-001's heartbeat model — flag (do not fix; that's REQ-005) but note explicitly in the implementation summary.
- Confirm the `.gitignore` line exists.
- Confirm `CLAUDE.md`'s pre-commit bump-version rule is satisfied by the version/changelog edits.

### Risk notes

- **Meta risk:** this REQ's changes land in the files that this very session loaded at start. Post-commit behavior applies to the *next* session only. Acceptable per the meta-use guardrails in `docs/prd/README.md`.
- **Backwards compatibility:** existing action files are additive — nothing is removed or renamed. Old `claimed_at` logic in `actions/work.md` Stale Claim Check is flagged but not modified here.

*Generated by Plan agent (inline execution under auto mode)*

## Plan Verification

**Source**: REQ-001 (9 items enumerated from Detailed Requirements + Constraints)
**Pre-fix coverage**: 100% (9/9 items addressed)

### Coverage Map

| # | Requirement | Plan Step | Status |
|---|-------------|-----------|--------|
| 1 | Session ID generation at start, unique across concurrent sessions | Deliverable 1 (session-identity.md — format/generation rules) | Full |
| 2 | Process identification: PID + hostname in session record | Deliverable 1 (required fields include hostname, pid) | Full |
| 3 | Heartbeat mechanism with bounded cadence (default 30 s, stale 2 min) | Deliverable 1 (heartbeat cadence section) | Full |
| 4 | Heartbeat per-session, not per-claim | Deliverable 1 (session-record scope) | Full |
| 5 | Session start-up contract — every action entry point obtains identity before coordinated action | Deliverables 2 + 3 (SKILL.md note + Step 0 updates across all actions) | Full |
| 6 | Graceful release on normal exit; recoverable via process absence on crash | Deliverable 1 (release section, recovery-evidence contract) | Full |
| 7 | Debuggability by `ls` and `cat` | Deliverable 1 (on-disk record format specified in plain text / JSON) | Full |
| 8 | Filesystem-only, no SQLite, no daemon | Deliverable 1 (explicit statement) + Deliverable 4 (`.gitignore` for local-only session records) | Full |
| 9 | Fail loud if session record cannot be written | Deliverable 1 (failure behavior in protocol) | Full |

### Constraint cross-check

| Constraint from UR-001 | Addressed by |
|------------------------|--------------|
| Filesystem-only | Deliverable 1 explicitly states; no runtime dependencies added |
| Single machine, single filesystem | Deliverable 1 hostname field enforces single-host scope |
| Fail loud | Deliverable 1 failure behavior |
| Debuggable by `ls` | Deliverable 1 on-disk format readable as plain text |
| No runtime daemon | Deliverable 1 explicitly states; heartbeat is in-session |

### Non-goal check

The plan explicitly excludes claim format (REQ-002), claim wiring (REQ-004), orphan recovery (REQ-005), verify locks (REQ-007), cleanup lock (REQ-009), and foreign-edit detection (REQ-010). Scope boundaries respected.

*Verified by verify-plan action*

## Exploration

Inventory of action files in `actions/`:

| File | Has Step 0 / Checklist? | Touch needed |
|------|-------------------------|--------------|
| `do.md` | Yes — "Step 0: Declare Your Capture Order" (line 389) | Yes — prepend session-identity to checklist |
| `work.md` | Yes — "Step 0: Write Your Work Order" (line 262) | Yes — prepend session-identity to checklist |
| `verify-request.md` | No explicit Step 0, but has Workflow section starting at line 29 | Yes — prepend session-identity as a new workflow step |
| `verify-plan.md` | No explicit Step 0, but has Workflow section | Yes — prepend session-identity as a new workflow step |
| `cleanup.md` | No explicit Step 0, has "What It Does" with passes | Yes — prepend session-identity before Pass 1 |
| `version.md` | No — read-only, just version/changelog reporting | Skip. Explicitly documented in session-identity.md as exempt (no coordinated writes). |

Existing stale-claim logic in `work.md` (line 295) uses pure `claimed_at > 1 hour` TTL. That is incompatible with REQ-001's heartbeat model — flag in implementation summary as "to be replaced by REQ-005," do not modify here.

`.gitignore` currently ignores `docs/prd/*.md` only — needs `do-work/.sessions/` added so ephemeral session records never land in git.

No other references to session IDs, heartbeats, PIDs, or hostnames exist in the codebase (confirmed by absence of matches when grepping the action files).

*Generated by Explore agent (inline)*

## Implementation Summary

### Files created

- `actions/session-identity.md` — canonical protocol document. Defines session ID format (`<ISO-timestamp-compressed>-<random-hex>`), on-disk record schema (`do-work/.sessions/<session_id>.json` with `session_id`, `hostname`, `pid`, `started_at`, `last_heartbeat`, `operation`), default heartbeat cadence (30 s) and stale threshold (2 min), full lifecycle (start / running / graceful exit / crash), debuggability contract, and explicit non-goals (every other REQ in the parallel-safety batch).

### Files modified

- `SKILL.md` — new "Session Protocol" section points to `actions/session-identity.md`; Action References list updated to include the new action at the top.
- `actions/do.md` — Step 0 "Capture order" checklist gets a new Step 0.5 "Establish session identity per actions/session-identity.md", followed by a fail-loud note.
- `actions/work.md` — Step 0 "Work order" checklist gets the same Step 0.5, plus a note that identity is established once per session (not per REQ in a loop).
- `actions/verify-request.md` — New "Step 0: Establish Session Identity" prepended to Workflow. Notes that when running as Step 5.5 inside do, identity is already established — refresh heartbeat.
- `actions/verify-plan.md` — New "Step 0: Establish Session Identity" prepended to Workflow. Notes standalone-invocation case.
- `actions/cleanup.md` — Pre-Pass-1 note requiring session identity establishment before any folder move. Covers both the auto-invoked (end of work loop) and manual (`do work cleanup`) paths.
- `.gitignore` — added `do-work/.sessions/` so ephemeral session records never land in git.
- `actions/version.md` — bumped from 0.13.3 to 0.14.0 (minor: new protocol file + behavior change in all coordinated actions).
- `CHANGELOG.md` — added "0.14.0 — The Name Tag" entry at top.

### Deviations from plan

None. All six deliverables from the plan landed as specified. `actions/version.md` was touched for the version bump itself, not for adding session-identity wiring — it remains exempt from the protocol (documented in session-identity.md "Exemptions").

### Follow-up items / flags

- **Stale-claim TTL in `actions/work.md` line 295** ("Stale claim check: `claimed_at` older than 1 hour → unclaim") uses pure timestamp logic incompatible with REQ-001's heartbeat model. **Flagged, not modified.** REQ-005 (evidence-based orphan recovery) is the right REQ to replace it; doing it here would scope-creep.
- The protocol says "Use the atomic-write primitive from REQ-002 once it is available; until then, write to a temp name and rename atomically." This is intentional — REQ-002 will retrofit the primitive. No action needed now.
- No claim wiring, no lock acquisition, no foreign-edit detection introduced. Scope held.

*Completed by work action (Route C)*

## Testing

**Test infrastructure:** None. This is a markdown-instruction skill; no executable runtime exists to exercise.

**Smoke test performed:**

1. Grep across the repo for `session-identity` — returns 9 files: the new action, the 5 updated action files, SKILL.md, CHANGELOG.md, .gitignore, and the working REQ itself. Every expected reference is present.
2. Confirmed `actions/session-identity.md` exists with the canonical `# Session Identity Action` heading.
3. Manually verified each of the 5 updated action files contains a reference to `./session-identity.md` (or `./actions/session-identity.md` from SKILL.md) — no stale or broken links.
4. `actions/version.md` confirmed at 0.14.0.
5. `CHANGELOG.md` entry at top is "0.14.0 — The Name Tag" with 2026-04-17 date, matching CLAUDE.md conventions.
6. `.gitignore` confirmed to exclude `do-work/.sessions/`.

**Result:** All references land, no dangling links, no regressions in the routing files visible from inspection.

**Caveat:** The runtime behavior ("agent actually establishes session identity at start of next invocation") cannot be verified in this session — the current session loaded SKILL.md and the action files before these edits existed, so it still operates under the pre-REQ-001 protocol. Verification happens in the next fresh session that reads these updated instructions. This is the known meta-use tradeoff documented in `docs/prd/README.md`.

*Verified by work action*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (5/5 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | Claims carry session identity so abandonment is detectable rather than guessed from a clock (Done-criteria #2) | Detailed Requirements — Session ID generation, Process identification | Full |
| 2 | Every session generates a session ID on start and writes it into the claim (Resolved decision — Claim model) | Detailed Requirements — Session start-up contract | Full |
| 3 | Claim file records `{session_id, started_at, last_heartbeat}` (Resolved decision — Claim model) | Detailed Requirements — Heartbeat mechanism, Heartbeat scope | Full |
| 4 | Filesystem-only, debuggable by `ls` (Resolved decision — Coordination substrate) | Constraints — Filesystem-only, Debuggable by `ls` | Full |
| 5 | Single machine, single filesystem (Out of scope) | Constraints — Single machine, single filesystem | Full |

*Verified by verify-request action*
