# Work Action

> **Part of the do-work skill.** Invoked when routing determines the user wants to process the queue. Processes requests from the `do-work/` folder in your project.

An orchestrated build system that processes request files created by the do action. Every request gets planned, verified, and implemented — the plan's depth scales to the task, from a single line for config changes to a multi-step strategy for new features.

## Request Files as Living Logs

**Each request file becomes a complete historical record of the work performed.** As you process a request, you append sections documenting each phase:

1. **Triage** - Complexity assessment and route label
2. **Plan** - Implementation plan (depth scales to complexity)
3. **Plan Verification** - Coverage analysis of plan against requirements
4. **Exploration** - Codebase findings (when plan indicates)
5. **Implementation Summary** - What was changed
6. **Testing** - Test results and coverage

This traceability ensures:
- You can review what was planned vs what was done
- Failed requests show exactly where things went wrong
- Triage accuracy can be evaluated over time
- The full context is preserved for future reference

## Architecture

```
work action (orchestrator - lightweight, stays in loop)
  │
  ├── For each pending request:
  │     │
  │     ├── TRIAGE: Assess complexity → label (A/B/C)
  │     │
  │     ├── PLAN: Proportional to complexity (1 line → multi-step)
  │     │
  │     ├── VERIFY PLAN: Coverage analysis against requirements
  │     │
  │     ├── EXPLORE: When plan references unknown files/patterns
  │     │
  │     ├── IMPLEMENT: general-purpose agent (has plan + optional exploration)
  │     │
  │     └── TEST: Run and verify tests
  │
  └── Loop continues until queue empty
```

Every request flows through the same pipeline. The complexity label (A/B/C) tells the planner how deep to go — it does not gate whether planning happens.

## Sub-agent Compatibility

This document uses "spawn agent" language. Use your platform's subagent or multi-agent mechanism when available. If your tool does not support subagents, run the phases sequentially in the same session and clearly label outputs as Plan, Explore, and Implementation summaries.

## Complexity Assessment

Before spawning any agents, quickly assess the request's complexity. This produces a **route label** (A/B/C) that tells the planner how deep to go. The label is for calibration, not gating — every request gets planned and verified regardless of route.

### Route A: Simple

The plan should be 1-3 lines. Exploration is unlikely to be needed.

**Indicators:**
- Bug fix with clear reproduction steps or error message
- Value, property, or config change
- Adding/removing a single UI element
- Styling or visual tweaks
- Request explicitly names specific files to modify
- Well-specified with obvious scope (under ~50 words, clear outcome)
- Copy/text changes
- Toggling a feature flag or setting

**Examples:**
- "Change the button color from blue to green"
- "Fix the crash when clicking submit with empty form"
- "Add a tooltip to the save button"
- "Update the API timeout from 30s to 60s"
- "Remove the deprecated banner from the header"

### Route B: Medium

The plan should include steps for discovering patterns/locations. Exploration will likely be needed.

**Indicators:**
- Clear outcome but unspecified location
- Request mentions "like the existing X" or "match the pattern"
- Need to find similar implementations to follow
- Modifying something that exists but location unknown
- Adding something that should match existing conventions

**Examples:**
- "Add form validation like we have on the login page"
- "Create a new API endpoint following our existing patterns"
- "Add the same loading state we use elsewhere"
- "Fix the styling to match the rest of the settings panel"

### Route C: Complex

The plan should be detailed with multiple steps, dependencies, and a testing approach. Exploration is almost certainly needed.

**Indicators:**
- New feature requiring multiple components
- Architectural changes or new patterns
- Ambiguous scope ("improve", "refactor", "optimize")
- Touches multiple systems or layers
- Integration with external services
- Request is long (100+ words) with many requirements
- Introduces new concepts to the codebase
- User explicitly asked for a plan or design

**Examples:**
- "Add user authentication with OAuth"
- "Implement dark mode across the app"
- "Refactor the state management to use Zustand"
- "Add real-time sync between devices"
- "Improve the search performance"

### Assessment Decision Flow

```
Read the request file
     │
     ├── Does it name specific files AND have clear changes?
     │     └── Yes → Route A (Simple)
     │
     ├── Is it a bug fix with clear reproduction?
     │     └── Yes → Route A (Simple)
     │
     ├── Is it a simple value/config/copy change?
     │     └── Yes → Route A (Simple)
     │
     ├── Is the outcome clear but location/pattern unknown?
     │     └── Yes → Route B (Medium)
     │
     ├── Is it ambiguous, multi-system, or architectural?
     │     └── Yes → Route C (Complex)
     │
     └── Default: Route B (Medium)
```

**When uncertain, prefer Route B.** The planner will calibrate depth based on the label, and verify-plan catches any gaps regardless. Getting the label slightly wrong is low-cost because every route gets the same quality gates.

## Folder Structure

```
do-work/
├── REQ-018-pending-task.md       # Pending (root = queue)
├── REQ-019-another-task.md
├── user-requests/                 # User Request folders (verbatim input + assets)
│   ├── UR-003/
│   │   ├── input.md              # Verbatim original input
│   │   └── assets/
│   │       └── REQ-017-screenshot.png
│   └── UR-004/
│       └── input.md
├── assets/                        # Legacy assets (pre-UR system)
│   └── CONTEXT-002-old-batch.md   # Legacy context docs
├── working/                       # Currently being processed
│   └── REQ-020-in-progress.md
│   └── REQ-020-in-progress.claim.json
└── archive/                       # Completed work
    ├── UR-001/                    # Archived as self-contained unit
    │   ├── input.md
    │   ├── REQ-013-feature.md
    │   └── assets/
    │       └── screenshot.png
    ├── UR-002/
    │   ├── input.md
    │   ├── REQ-014-feature.md
    │   └── REQ-015-feature.md
    ├── REQ-010-legacy-task.md     # Legacy REQs (no UR) archive directly
    └── CONTEXT-001-auth-system.md # Legacy context docs archive directly
```

- **Root `do-work/`**: The queue - ONLY pending `REQ-*.md` files live here
- **`user-requests/`**: UR folders with verbatim input and assets per user request
- **`working/`**: Claimed REQs live here alongside a `.claim.json` sidecar that names the owning session
- **`archive/`**: Completed UR folders (self-contained units) AND legacy REQs/CONTEXT docs
- **`assets/`**: Legacy screenshots and context documents (pre-UR system)

**Immutability rule:** Files in `working/` and `archive/` are locked. Only the work action's own processing pipeline may modify files in `working/` (updating frontmatter, appending workflow sections). The do action and all other external processes must never reach into these folders. If a change request comes in for something already claimed or completed, the do action creates a new addendum REQ in the queue — it never touches the original. See the do action docs for the `addendum_to` field.

**User Request (UR) lifecycle:**
- Created in `do-work/user-requests/` by the do action
- Referenced by REQ files via `user_request: UR-NNN` frontmatter
- When ALL related REQs are complete: UR folder moves from `user-requests/` to `archive/`, with completed REQ files pulled into the UR folder

**Backward compatibility:**
- REQs without `user_request` field (legacy) archive directly to `do-work/archive/` as before
- REQs with `context_ref` (legacy) trigger CONTEXT doc archival when all related REQs complete
- The `do-work/assets/` folder is only used for legacy items — new assets go into UR folders

## Request File Schema

Request files use YAML frontmatter for tracking. Fields are added progressively by the do and work actions.

```yaml
---
# Set by do action when created
id: REQ-001
title: Short descriptive title
status: pending
created_at: 2025-01-26T10:00:00Z
user_request: UR-001  # Links to originating user request (may be absent on legacy REQs)

# Set by work action when claimed
claimed_at: 2025-01-26T10:30:00Z
route: A | B | C

# Set by work action when finished
completed_at: 2025-01-26T10:45:00Z
status: completed | failed
commit: abc1234  # Git commit hash (if git repo)
error: "Description of failure"  # Only if failed
---
```

### Field Reference

| Field | Set By | When | Description |
|-------|--------|------|-------------|
| `id` | do | Creation | Request identifier (REQ-NNN) |
| `title` | do | Creation | Short title for the request |
| `status` | Both | Throughout | Current state (see flow below) |
| `created_at` | do | Creation | ISO timestamp when request was captured |
| `user_request` | do | Creation | UR identifier linking to originating user request (absent on legacy REQs) |
| `claimed_at` | work | Claim | ISO timestamp when work began |
| `route` | work | Triage | Complexity route (A/B/C) |
| `completed_at` | work | Completion | ISO timestamp when work finished |
| `commit` | work | Commit | Git commit hash (omitted if not a git repo) |
| `error` | work | Failure | Error description if status is `failed` |

### Status Flow

```
pending (in do-work/, set by do action)
    → claimed (moved to working/ with sidecar claim file, set by work action)
    → [planning] → [exploring] → implementing → testing
    → completed (moved to archive/)
    ↘ failed (moved to archive/ with error)
```

Brackets indicate optional phases based on route.

## Workflow

**CRITICAL: Orchestrator Responsibilities**

The work action is an **orchestrator**. You (the orchestrator) are responsible for ALL file management operations. Spawned agents do implementation work but do NOT touch request files or folder structure.

**You must do these yourself (not delegate to agents):**
- Take the atomic work claim and move the request into `working/` (Step 2)
- Update frontmatter status fields (Steps 2, 3, 7)
- Write triage/plan/exploration/testing sections to the request file
- Move request file to `archive/` folder (Step 7)
- Create the git commit (Step 8)

**Agents do:**
- Planning (Route C)
- Exploring (Routes B, C)
- Implementation (all routes)
- Writing/running tests

---

### Step 0: Write Your Work Order

**Before touching any files**, write out the following checklist for the request you are about to process. Treat it as a live work order — check items off as you complete each step. Do not start Step 1 until you have written this out.

```
Work order — [REQ-NNN]:
[ ] Step 0.5: Establish session identity per actions/session-identity.md
[ ] Step 1:   Identify next REQ-*.md in do-work/
[ ] Step 2:   Atomic claim (`claim_work_request`) → move to working/, create sidecar claim, update frontmatter
[ ] Step 3:   Triage → assign route A/B/C, append ## Triage section
[ ] Step 4:   Plan → spawn Plan agent, append ## Plan section
[ ] Step 4.5: *** MANDATORY *** Verify Plan → enumerate requirements, map to plan steps, fix gaps, append ## Plan Verification section
[ ] Step 5:   Explore → spawn Explore agent OR append "Exploration: Not needed"
[ ] Step 6:   Implement → spawn implementation agent, capture summary
[ ] Step 6.5: Run tests → append ## Testing section
[ ] Step 7:   Archive → update frontmatter, move file per UR/legacy logic
[ ] Step 8:   Commit → git add -A && git commit (git repos only)
[ ] Step 9:   Loop or exit
```

Step 0.5 must be satisfied before any `do-work/` write. When the work action loops across multiple REQs in the same session, session identity is established once and refreshed via heartbeat — not re-established per REQ.

Marking Step 4.5 as done means `## Plan Verification` is written to the request file. Marking Step 6.5 as done means `## Testing` is written to the request file. Checking a box without the artifact does not count.

### Step 1: Find Next Request

**[Orchestrator action - do this yourself]**

1. **List** (don't read) `REQ-*.md` filenames in `do-work/` folder
2. Sort by filename (REQ-001 before REQ-002)
3. Pick the first one

**Important**: Do NOT read the contents of all request files. Only list filenames to find the next one. You'll read the chosen request in Step 3.

If no request files found, report completion and exit.

**Do not auto-recover claims here.** Anything already in `do-work/working/` is presumed live during the normal `do work` loop. Recovery is a separate explicit path (`do work resume`) because the claim attempt itself must never guess about liveness. This action only claims files that are still in the root queue.

### Step 2: Claim the Request

**[Orchestrator action - do this yourself, BEFORE spawning any agents]**

1. Create `do-work/working/` folder if it doesn't exist
2. Enforce **one work claim per session**:
   - Before claiming a new REQ, check `do-work/working/*.claim.json` for a claim already owned by the current `session_id`
   - If this session already owns one, resume or finish that REQ first. Do **not** claim a second request
3. Attempt the claim with the REQ-004 helper:

```python
from lib.concurrency import claim_work_request

claim = claim_work_request(
    "do-work",
    request_path="REQ-XXX-slug.md",
    session_id="<current-session-id>",
    operation="work",
)
```

This helper is the policy boundary for REQ-004:
- The claim point is an **exclusive-create** of `do-work/working/REQ-XXX-slug.claim.json`
- On success, the queued REQ is immediately moved into `do-work/working/REQ-XXX-slug.md`
- If the move fails, the helper removes the just-created claim before re-raising
- If another session already won, the helper raises `ClaimHeldError` naming the winning session and heartbeat

4. On successful claim, update the frontmatter in the moved REQ file:

```yaml
---
status: claimed
claimed_at: 2025-01-26T10:30:00Z
---
```

5. The claim sidecar must be `cat`-readable and use the shared claim schema:

```json
{
  "claim_id": "REQ-004",
  "session_id": "2026-04-18T00-21-52Z-f2d85402",
  "operation": "work",
  "scope": "req-claim:REQ-004",
  "affected_paths": ["do-work/working/REQ-004-atomic-req-claim.md"],
  "acquired_at": "2026-04-18T00:22:00Z",
  "last_heartbeat": "2026-04-18T00:22:00Z"
}
```

6. While the REQ remains claimed, refresh the claim heartbeat on the same cadence as the session heartbeat:

```python
from lib.concurrency import refresh_claim_heartbeat

refresh_claim_heartbeat("do-work/working/REQ-XXX-slug.claim.json")
```

Refresh before and after any operation that might block for a long time.

7. If `ClaimHeldError` is raised:
   - Surface the error verbatim so the losing session can see who won
   - Log the conflict in your progress output
   - Pick a different queued REQ or exit cleanly
   - Do **not** spin forever retrying the same claim

### Step 3: Triage

**[Orchestrator action - do this yourself]**

Read the request content and apply the assessment decision flow. Update frontmatter with the chosen route:

```yaml
---
status: claimed
claimed_at: 2025-01-26T10:30:00Z
route: B
---
```

**Write the triage decision into the request file** (append after the original content):

```markdown
---

## Triage

**Route: [A/B/C]** - [Simple/Medium/Complex]

**Reasoning:** [1-2 sentences explaining why this complexity label was chosen]
```

Examples:
- Route A: "Route A - Simple. Bug fix with clear reproduction steps and named file."
- Route B: "Route B - Medium. Clear feature but need to find existing patterns."
- Route C: "Route C - Complex. New feature spanning multiple systems with dependencies."

This creates a record for retrospective analysis - you can later evaluate whether complexity assessments were accurate by comparing the route label to the plan depth and actual time spent.

**The request file is a living log** - every phase of work gets documented in it so there's full traceability of what happened.

Report the assessment briefly to the user:
- Route A: "Simple request — planning will be lightweight"
- Route B: "Medium complexity — will plan and likely explore codebase"
- Route C: "Complex request — detailed planning ahead"

### Step 4: Planning Phase (All routes)

**[Spawn agent — plan depth scales to complexity]**

Every request gets a plan. The route label from triage tells the planner how deep to go:

- **Route A**: Plan should be 1-3 lines. Name the file(s) and the change. Don't over-think it.
- **Route B**: Plan should identify what needs to be found/matched, plus the implementation steps.
- **Route C**: Plan should be detailed — files, ordering, dependencies, architecture decisions, testing approach.

Spawn a **Plan agent** with this prompt structure:

```
You are planning the implementation for this request:

---
[Full content of the request file]
---

Complexity assessment: Route [A/B/C] - [Simple/Medium/Complex]

Project context:
- This is a [describe project type based on package.json/Cargo.toml]
- Key directories: [list from exploring project structure]

Create an implementation plan proportional to the complexity:
- Route A (Simple): 1-3 lines. Name the file(s) and what changes. That's it.
- Route B (Medium): Identify patterns to match or locations to find, plus implementation steps.
- Route C (Complex): Detailed plan with files, ordering, dependencies, architecture decisions, and testing approach.

Be specific about file paths and function names where possible.
If your plan includes steps that require finding unknown files or discovering
existing patterns, note them clearly — these signal that exploration is needed.
```

**After Plan agent returns:**
- Store the plan output for the next phase
- **Write the plan into the request file** (append after Triage section):

```markdown
## Plan

[Full output from the Plan agent]

*Generated by Plan agent*
```

### Step 4.5: Verify Plan (All routes)

**[Mandatory step -- runs automatically after planning]**

Verify the plan against the REQ to ensure every requirement is addressed. Fix any gaps in the plan directly. Do not ask the user — just fix and document.

**Skip condition:** If the user said "skip verification" in their original request, skip this step.

**1. Enumerate source items:**

Read the REQ file (in `do-work/working/`). Extract a numbered list of every discrete requirement, constraint, UX detail, dependency, and scope cue. Also read the UR input.md (via the REQ's `user_request` field) to catch anything that applies to this REQ but didn't make it into the REQ text.

**2. Map each item to the plan:**

For each enumerated item, find where the plan addresses it. Classify each:
- **Full** -- the plan includes a step that clearly addresses this item
- **Partial** -- the plan touches on this but doesn't fully address it
- **Missing** -- the plan does not address this item at all

**3. Calculate coverage:**

Coverage % = (full + 0.5 x partial) / total x 100

**4. Auto-fix the plan (do not ask -- just fix):**

For each missing or partial item, edit the plan directly:
- Missing feature? Add a new implementation step
- Missing constraint? Add to an existing step or create a constraints note
- Missing test coverage? Add to the testing approach
- Keep additions proportional — don't bloat a simple plan for a minor constraint

**5. Store results — append to REQ file immediately after the `## Plan` section:**

```markdown
## Plan Verification

**Source**: REQ-NNN (X items enumerated)
**Pre-fix coverage**: 80% (7 full, 2 partial, 1 missing)
**Post-fix coverage**: 100% (10/10 items addressed)

### Coverage Map

| # | Requirement | Plan Step | Status |
|---|------------|-----------|--------|
| 1 | [requirement] | Step N: [description] | Full |
| 2 | [requirement] | Step N: [description] | Partial -> Fixed |
| 3 | [requirement] | -- | Missing -> Fixed (added Step N) |

### Fixes Applied

- Added Step N: [description]
- Expanded Step N to include [detail]

*Verified by verify-plan action*
```

For Route A plans (1-3 lines), verification will be fast — a simple task has few items to enumerate. The value is consistency: every request gets the same quality gate.

See [verify-plan action](./verify-plan.md) for the full protocol.

> **Gate:** `## Plan Verification` must be written to the request file before you start Step 5. If you skipped it or only did it mentally, go back and write it now.

### Step 5: Exploration Phase (When plan indicates)

**[Spawn agent if plan references unknown files/patterns, then orchestrator writes results to file]**

Since every request now has a plan, the plan itself signals whether exploration is needed:

- **Exploration needed:** The plan references patterns to match, files to discover, or conventions to follow without naming specific paths. Plan steps like "find where X is implemented" or "match existing Y pattern" are clear signals.
- **Exploration not needed:** The plan names specific files and exact changes. There's nothing to discover.

When in doubt, explore. The cost is low and the context it provides helps the builder.

Spawn an **Explore agent** with this prompt:

```
Based on this implementation plan:

---
[Plan from previous phase]
---

For this request:
[Brief summary of the request]

Find and read the relevant files that will need to be modified or that contain patterns we should follow. Focus on:
1. Files mentioned or implied by the plan
2. Similar existing implementations we should match
3. Type definitions and interfaces we'll need
4. Test files that show testing patterns

Return a summary of what you found, including:
- Key code patterns to follow
- Existing types/interfaces to use
- Any concerns or blockers discovered
```

**After Explore agent returns:**
- Update request status to `implementing`
- Store the exploration output for the next phase
- **Write exploration findings into the request file** (append after Plan Verification section):

```markdown
## Exploration

[Summary output from the Explore agent - key files, patterns found, concerns]

*Generated by Explore agent*
```

**If exploration is skipped** (plan is fully specified), document it:

```markdown
## Exploration

**Not needed** -- plan names specific files and changes. No discovery required.

*Skipped by work action*
```

### Step 6: Implementation Phase (All routes)

**[Spawn agent - agent does the actual code changes]**

Every request now has a plan (and optionally exploration context). Spawn a **general-purpose agent** with the appropriate context:

**With exploration context:**
```
You are implementing this request:

## Request
[Full content of request file]

## Implementation Plan
[Plan from Step 4]

## Codebase Context
[Output from Explore agent]

## Instructions

Implement the changes according to the plan, using the patterns and locations
identified in the codebase context. You have full access to edit files and
run shell commands.

Key guidelines:
- Follow existing code patterns identified in the codebase context
- Make minimal, focused changes
- If you encounter blockers, document them clearly
- If you deviate from the plan, note why

Testing requirements:
- Identify existing tests related to the changes
- Write new tests for new functionality, following patterns from the codebase context
- For bug fixes, add regression tests that would have caught the bug
- Update existing tests if behavior intentionally changed

When complete, provide a summary of:
1. What files were changed/created
2. Any deviations from the plan and why
3. What tests exist and what new tests were written
4. Any follow-up items needed
```

**Without exploration context (exploration was skipped):**
```
You are implementing this request:

## Request
[Full content of request file]

## Implementation Plan
[Plan from Step 4]

## Instructions

Implement the changes according to the plan. You have full access to edit
files and run shell commands, including exploring the codebase if you need
additional context.

Key guidelines:
- Make minimal, focused changes
- If you encounter blockers, document them clearly
- If you deviate from the plan, note why

Testing requirements:
- If the project has tests, identify tests related to your changes
- Write new tests for new functionality or bug fixes (regression tests)
- Update existing tests if behavior intentionally changed

When complete, provide a summary of:
1. What files were changed/created
2. Any deviations from the plan and why
3. What tests exist and what new tests were written
4. Any follow-up items needed
```

**After implementation agent returns:**
- Capture the summary output
- Update request status to `testing`
- Proceed to testing phase

### Step 6.5: Testing Phase (All routes)

**[Orchestrator runs tests and may spawn agent for new tests]**

Before marking work as complete, verify that tests pass and appropriate test coverage exists.

**1. Detect testing infrastructure:**

Look for common test configurations:
- `package.json` scripts containing "test"
- `jest.config.*`, `vitest.config.*`, `playwright.config.*`
- `pytest.ini`, `pyproject.toml` with pytest section
- `Cargo.toml` (Rust has built-in testing)
- `*_test.go` files (Go has built-in testing)
- `*.spec.*`, `*.test.*` files in the codebase

If no testing infrastructure is detected, skip this phase and proceed to archiving. Note in the implementation summary: "No testing infrastructure detected."

**2. Identify relevant tests:**

Based on what files were modified during implementation:
- Find existing test files that cover the modified code
- Check if the change warrants new tests

**Tests are warranted when:**
- New functionality was added (new functions, components, endpoints)
- Bug fixes that should have regression tests
- Behavioral changes that could break existing functionality
- New edge cases or error handling paths

**Tests may not be needed for:**
- Documentation changes
- Config value changes (unless config affects behavior significantly)
- Pure refactoring with existing test coverage
- Cosmetic/styling changes

**3. Run existing related tests:**

```bash
# Example: JavaScript/TypeScript with Jest
npm test -- --testPathPattern="relevant-pattern"

# Example: Python with pytest
pytest tests/relevant_test.py -v

# Example: Rust
cargo test relevant_module

# Example: Go
go test ./path/to/package -v
```

Run tests that are relevant to the changed code, not the entire test suite (unless the suite is fast).

**4. If tests fail:**

- Do NOT mark the request as complete
- Return to implementation phase to fix the failing tests
- The implementation agent should:
  - Analyze the test failure
  - Fix the implementation OR fix the test if the test was incorrect
  - Re-run the tests

**Loop until tests pass or it becomes clear the request cannot be completed.**

**5. If new tests are needed:**

Spawn a **general-purpose agent** to write tests:

```
The following changes were made for this request:

## Request
[Brief summary]

## Changes Made
[Files created/modified from implementation summary]

## Task
Write appropriate tests for these changes. Follow the existing testing patterns in the codebase.

Guidelines:
- Match the testing style/framework already in use
- Cover the happy path and key edge cases
- For bug fixes, add a regression test that would have caught the bug
- Keep tests focused and readable
- Place test files according to project conventions

After writing tests, run them to verify they pass.
```

**6. Verify all tests pass:**

Run the full relevant test suite one final time:

```bash
# Run tests and capture exit code
npm test  # or pytest, cargo test, go test, etc.
```

**If tests pass:** Update status and proceed to archiving
**If tests fail:** Return to implementation to fix issues

**Write testing results into the request file** (append after Implementation Summary):

```markdown
## Testing

**Tests run:** [command used]
**Result:** ✓ All tests passing (X tests)

**New tests added:**
- tests/new-feature.spec.ts - covers happy path and error cases

**Existing tests verified:**
- tests/related-feature.spec.ts - still passing

*Verified by work action*
```

Or for failures that were resolved:

```markdown
## Testing

**Initial run:** ✗ 2 tests failing
**Issue:** Implementation didn't handle null case
**Fix:** Added null check in handleSubmit()
**Final run:** ✓ All tests passing (X tests)

*Verified by work action*
```

### Step 7: Archive and Continue

**[Orchestrator action - do this yourself, AFTER all agents complete]**

**IMPORTANT: You must perform these file operations yourself. Do not skip them.**

**On success - do ALL of these steps:**

1. **Update the request file frontmatter** (in `do-work/working/`):
```yaml
---
status: completed
claimed_at: 2025-01-26T10:30:00Z
completed_at: 2025-01-26T10:45:00Z
route: B
---
```

2. **Append implementation summary** to the request file (if not already present):
```markdown
## Implementation Summary

[Summary from the implementation agent]

*Completed by work action (Route [A/B/C])*
```

3. **Create archive folder** if it doesn't exist:
```bash
mkdir -p do-work/archive
```

**Claim release rule:** The `.claim.json` sidecar stays in place until the REQ has been transitioned out of `working/` or explicitly rolled back to the queue. Never drop the claim first and then attempt the move.

**REQ-002 note:** REQ-004 owns the queue → working transition. REQ-006 upgrades the archive step here to the same `atomic_rename(...)` discipline, with a short per-UR archival lock for the "last REQ finished" transition.

4. **Archive the request file** — behavior depends on whether the REQ has a UR:

**If REQ has `user_request: UR-NNN` (new system):**
   - Call `archive_completed_request(do_work_root, working_request_path=..., session_id="<current-session-id>", operation="work")`
   - That helper first atomically moves the REQ out of `working/` into `do-work/archive/`
   - Then it takes a short per-UR lock (`ur-archival:UR-NNN`), re-reads the UR's `requests` array, and checks whether every listed REQ is already archived
   - If **all REQs are already archived**, it gathers any loose `archive/REQ-*.md` files for that UR into the open UR folder and atomically renames `do-work/user-requests/UR-NNN/` to `do-work/archive/UR-NNN/`
   - If another session already finished that UR archival, the helper returns a clean `already-archived` outcome — no error, no second move attempt
   - If not all REQs are archived yet, the helper leaves the REQ in `do-work/archive/` root for now and returns `not-ready`
   - **Important:** "final REQ" means "final REQ already archived", not merely `status: completed` in `working/`. The last-one-standing decision happens after this REQ's own archive move
   - Release the sidecar claim only after `archive_completed_request(...)` returns successfully

**If REQ has `context_ref` (legacy system):**
   - Call the same `archive_completed_request(...)` helper
   - The helper moves the REQ into `do-work/archive/`, then takes a short `ur-archival:<context-id>` lock for the shared CONTEXT document
   - If all listed REQs are already archived, it atomically renames the CONTEXT file from `do-work/assets/` into `do-work/archive/`
   - If another session already moved that CONTEXT file, the helper returns `already-archived` cleanly
   - Release the sidecar claim only after `archive_completed_request(...)` returns successfully

**If REQ has neither `user_request` nor `context_ref` (standalone legacy):**
   - Call `archive_completed_request(...)`
   - The helper performs the single atomic rename from `do-work/working/REQ-XXX.md` to `do-work/archive/REQ-XXX.md`
   - Release the sidecar claim only after the helper returns successfully

**Complete request file structure after archival:**

```markdown
---
id: REQ-007
title: Add user avatar component
status: completed
created_at: 2025-01-26T09:30:00Z
claimed_at: 2025-01-26T11:00:00Z
route: B
completed_at: 2025-01-26T11:08:00Z
commit: a1b2c3d
---

# Add User Avatar Component

## What
[Original request]

## Context
[Original context]

---

## Triage

**Route: B** - Medium

**Reasoning:** Clear feature but need to find existing component patterns.

## Plan

1. Find existing component patterns in src/components/ (similar to avatar)
2. Create UserAvatar component following discovered patterns
3. Add to user profile section
4. Add tests matching existing component test patterns

*Generated by Plan agent*

## Plan Verification

**Source**: REQ-007 (3 items enumerated)
**Pre-fix coverage**: 100% (3/3 items addressed)

*Verified by verify-plan action*

## Exploration

- Found similar component at src/components/ExistingFeature.tsx
- Uses pattern X for state management
- Tests in tests/existing-feature.spec.ts

*Generated by Explore agent*

## Implementation Summary

- Created src/components/NewFeature.tsx
- Added tests in tests/new-feature.spec.ts

*Completed by work action (Route B)*

## Testing

**Tests run:** npm test -- --testPathPattern="new-feature"
**Result:** ✓ All tests passing (4 tests)

**New tests added:**
- tests/new-feature.spec.ts - covers rendering, click handler, edge cases

*Verified by work action*
```

**All routes have Plan and Plan Verification sections.** The plan's depth scales to the complexity — one line for a config change, a full strategy for a new feature. The verification coverage is consistent regardless of plan size.

**Timestamps tell the story:**
- `created_at` → `claimed_at`: How long request sat in queue
- `claimed_at` → `completed_at`: How long implementation took
- Route + timestamps: Compare complexity vs actual time spent

**On abort before a terminal outcome is recorded** (user cancels, crash-safe rollback path, orchestration error before the REQ is intentionally marked `completed` or `failed`) - do ALL of these steps:

1. **Move the REQ back to the queue root** so it can be retried later:
```python
from lib.concurrency import atomic_rename, release_claim

atomic_rename(
    "do-work/working/REQ-XXX-slug.md",
    "do-work/REQ-XXX-slug.md",
    transition="working->queue rollback for REQ-XXX",
)
release_claim(
    "do-work/working/REQ-XXX-slug.claim.json",
    expected_session_id="<current-session-id>",
)
```

2. **Reset non-terminal work fields** (`status: pending`; remove `claimed_at`, `route`, `completed_at`, `commit`, and any transient error field that only described this aborted attempt).

3. **If the state is ambiguous, fail loud instead of guessing.** Example: if the REQ file move succeeded but the rollback target is occupied, stop and tell the user exactly what is stranded in `working/`. Do not auto-merge or auto-recover here; use the explicit `do work resume` flow below.

**On terminal failure** (you intentionally record `status: failed` as the final outcome for this REQ) - do ALL of these steps:

1. **Update frontmatter with error** (in `do-work/working/`):
```yaml
---
status: failed
claimed_at: 2025-01-26T10:30:00Z
route: B
error: "Brief description of what went wrong"
---
```

2. **Create archive folder** if it doesn't exist:
```bash
mkdir -p do-work/archive
```

3. **Move the request file to archive** (failed items are also archived, status tells the story):
```bash
mv do-work/working/REQ-XXX-slug.md do-work/archive/
```
Note: Failed REQs always go directly to `do-work/archive/`, NOT into UR folders. A failed REQ does not count as "complete" for UR archival purposes — the UR folder stays in `user-requests/` until all REQs succeed or the user explicitly archives.

4. **Release the sidecar claim** only after the REQ has left `working/`:
```python
release_claim(
    "do-work/working/REQ-XXX-slug.claim.json",
    expected_session_id="<current-session-id>",
)
```

5. **Report the failure to the user**

### Step 8: Commit Changes (Git repos only)

**[Orchestrator action - do this yourself]**

If the project is Git-backed, create a **single commit** containing all changes made for this request. This provides a clean rollback/cherry-pick surface per request.

**Check for Git:**
```bash
git rev-parse --git-dir 2>/dev/null
```

If this fails (not a Git repo), skip this step entirely.

**Stage and commit (single operation):**
```bash
# Stage ALL current changes - code, request file, assets
git add -A

# Single commit with structured message
git commit -m "$(cat <<'EOF'
[REQ-003] Dark Mode (Route C)

Implements: do-work/archive/REQ-003-dark-mode.md

- Created src/stores/theme-store.ts
- Modified src/components/settings/SettingsPanel.tsx
- Updated tailwind.config.js

Co-Authored-By: Assistant <noreply@your-tool>
EOF
)"
```

**Commit message format:**
```
[{id}] {title} (Route {route})

Implements: do-work/archive/{filename}

{implementation summary as bullet points}

Co-Authored-By: Assistant <noreply@your-tool>
```

If your platform standardizes co-author lines, use its preferred identity. For Claude Code, use `Claude <noreply@anthropic.com>`. If your tool does not use co-author lines, omit the line.

**Important commit rules:**
- **ONE commit per request** - do not analyze files individually or create multiple commits
- **Stage everything** with `git add -A` - includes code changes, archived request file, and any assets
- **No pre-commit hook bypass** - if hooks fail, fix the issue and retry
- **Failed requests get committed too** - the archived request with `status: failed` documents what was attempted

**If commit fails:**
- Report the error to the user
- Do NOT retry with different commit strategies
- Continue to next request (changes remain uncommitted but archived)

### Step 9: Loop or Exit

**[Orchestrator action - do this yourself]**

After archiving and committing:

1. **Re-check** root `do-work/` folder for `REQ-*.md` files (fresh check, not cached list)
2. If found: Report what was completed, then start Step 1 again
3. If empty: **Run the cleanup action** (see [cleanup action](./cleanup.md)), then report final summary and exit

The cleanup action consolidates the archive and organizes legacy files. REQ-006 keeps the work action on a short per-UR archival lock instead of sharing cleanup's global lock; REQ-009 therefore needs cleanup to re-scan between structural moves rather than assume its initial directory view stays fresh.

This fresh check on each loop means newly added requests get picked up automatically.

---

### Orchestrator Checklist (per request)

Use this checklist to ensure you don't skip critical steps:

```
□ Step 1: List REQ-*.md files in do-work/, pick first one
□ Step 2: Ensure this session does not already hold another work claim
□ Step 2: claim_work_request(...) → creates do-work/working/REQ-XXX-slug.claim.json and moves REQ into working/
□ Step 2: Update frontmatter: status: claimed, claimed_at: <timestamp>
□ Step 3: Read request, decide route (A/B/C), update frontmatter with route
□ Step 3: Append ## Triage section to request file (including Planning status)
□ Step 4: Spawn Plan agent (depth scales to route), append ## Plan section
□ Step 4.5: Run verify-plan — enumerate, map, fix plan, store coverage
□ Step 5: If plan indicates exploration needed: Spawn Explore agent, append ## Exploration section
□ Step 5: If plan is fully specified: Append "Exploration: Not needed" section
□ Step 6: Spawn implementation agent
□ Step 6: Refresh session + claim heartbeat around long-running operations
□ Step 6.5: Run tests, append ## Testing section
□ Step 7: Update frontmatter: status: completed, completed_at: <timestamp>
□ Step 7: Append ## Implementation Summary section
□ Step 7: Archive REQ with archive_completed_request(...), then release the matching .claim.json sidecar
□ Step 7: If user_request exists → helper takes per-UR lock, re-checks archived REQs, and either archives the UR or returns not-ready/already-archived
□ Step 7: If context_ref exists → helper takes the shared-parent archival lock and either archives the CONTEXT file or returns not-ready/already-archived
□ Step 7: If neither → helper performs the single working→archive atomic rename
□ Abort path: If orchestration aborts before a terminal outcome, move REQ back to queue and release the claim
□ Step 8: git add -A && git commit (if git repo)
□ Step 9: Check for more requests, loop or exit
□ Step 9: If exiting: Run cleanup action (close completed URs, consolidate loose REQs)
```

**Common mistakes to avoid:**
- Spawning implementation agent before taking the atomic claim (`claim_work_request(...)`)
- Completing implementation without moving file to `archive/`
- Forgetting to update status in frontmatter
- Letting agents handle file management (they shouldn't)
- Skipping planning for simple requests (all routes get planned — the plan just scales down)
- Skipping verify-plan (it's mandatory for all routes unless user said "skip verification")
- Forgetting to check/archive related UR folders or legacy context documents
- Archiving a UR folder before all its REQs are complete

---

## Progress Reporting

Keep the user informed with brief updates:

```
Processing REQ-003-dark-mode.md...
  Triage: Complex (Route C)
  Planning...     [done]
  Verify Plan...  [done] 90% → 100% (2 items fixed)
  Exploring...    [done]
  Implementing... [done]
  Testing...      [done] ✓ 12 tests passing
  Archiving...    [done]
  Committing...   [done] → abc1234

Processing REQ-004-fix-typo.md...
  Triage: Simple (Route A)
  Planning...     [done] (1 line)
  Verify Plan...  [done] 100%
  Implementing... [done]
  Testing...      [done] ✓ 3 tests passing
  Archiving...    [done]
  Committing...   [done] → def5678

Found 1 more pending request. Continuing...

Processing REQ-005-add-tooltip.md...
  Triage: Simple (Route A)
  Planning...     [done] (1 line)
  Verify Plan...  [done] 100%
  Implementing... [done]
  Testing...      [done] ✓ 2 tests passing
  Archiving...    [done]
  Committing...   [done] → ghi9012

All 3 requests completed:
  - REQ-003 (Route C) → abc1234
  - REQ-004 (Route A) → def5678
  - REQ-005 (Route A) → ghi9012
```

For non-Git projects, the commit step is skipped:
```
Processing REQ-003-dark-mode.md...
  Triage: Complex (Route C)
  Planning...     [done]
  Verify Plan...  [done] 100% (no fixes needed)
  Exploring...    [done]
  Implementing... [done]
  Testing...      [done] ✓ 8 tests passing
  Archiving...    [done]
  (not a git repo, skipping commit)

Completed.
```

## Error Handling

### Plan agent fails
- Mark request as `failed` with error
- Continue to next request (don't block the queue)

### Explore agent fails
- Proceed to implementation anyway with reduced context
- Note the limitation in the implementation prompt
- Builder can explore on its own if needed

### Implementation agent fails
- Mark request as `failed`
- Preserve any plan and exploration outputs in the request file for retry

### Tests fail repeatedly
- After 3 attempts to fix failing tests, mark request as `failed`
- Include the test failure details in the error field
- Preserve the implementation work done (it may be correct, tests may need adjustment)
- Note in the request file what tests failed and why fixes didn't work

### Commit fails
- Report the error to the user (usually pre-commit hook failure)
- Do NOT retry with `--no-verify` or alternative strategies
- Continue to next request - changes remain uncommitted but are archived
- User can manually fix and commit later

### Unrecoverable error
- Stop the loop
- Report clearly what happened
- Leave queue state intact for manual recovery

## Explicit Recovery Path

### `do work resume`

`resume` is the **only** work-action path allowed to recover orphaned REQ claims. It never runs implicitly inside Step 1 or Step 2 of the normal work loop, and it never trusts age alone.

The recovery rule is strict:

- **Recover only when both signals agree**:
  - the claim heartbeat is stale, and
  - the owning session record is also stale **and** its PID is absent on the current host
- **Fail loud on every ambiguity**:
  - session record missing
  - hostname mismatch
  - PID still alive
  - conflicting or inconsistent heartbeat evidence

If any claim in `do-work/working/` is ambiguous, stop and surface the evidence. Do not partially "clean things up" and keep going.

#### Resume workflow

1. Establish session identity per `actions/session-identity.md`.
2. List `do-work/working/*.claim.json`. If none exist, report "nothing to recover" and exit.
3. Inspect every claim with the REQ-005 helper:

```python
from lib.concurrency import inspect_work_claim_recovery

inspection = inspect_work_claim_recovery(
    "do-work",
    claim_path="do-work/working/REQ-XXX-slug.claim.json",
)
```

4. Treat verdicts as follows:
   - `live` → stop; the claim is still active.
   - `stale` → stop; the heartbeat is stale but the process evidence is ambiguous, so recovery would guess.
   - `foreign-host` → stop; this filesystem may be shared with another machine, and recovery is not allowed here.
   - `missing-session-record` → stop; without the session record, recovery cannot prove process absence.
   - `recoverable` → eligible for explicit recovery.
5. If **any** claim is not `recoverable`, print the inspection evidence verbatim and halt without mutating anything.
6. If **all** claims are `recoverable`, recover them one by one:

```python
from lib.concurrency import recover_orphaned_work_claim

result = recover_orphaned_work_claim(
    "do-work",
    claim_path="do-work/working/REQ-XXX-slug.claim.json",
    recovering_session_id="<current-session-id>",
)
```

This helper does four things atomically enough for the explicit recovery path:

- moves the REQ from `do-work/working/` back to the root queue
- writes a recovery log entry to `do-work/.recovery-log/<timestamp>-REQ-XXX.json`
- removes the `.claim.json` sidecar
- deletes the originating session record in `do-work/.sessions/`

7. After each successful recovery, update the REQ frontmatter in the returned queue file with breadcrumbs:

```yaml
---
status: pending
recovered_from_session: 2026-04-18T00-21-52Z-f2d85402
recovered_at: 2026-04-18T00:40:00Z
recovered_last_heartbeat: 2026-04-18T00:22:00Z
recovery_note: "Recovered from stale heartbeat + absent PID on same host. Prior work may be partial."
---
```

Use the values returned by `recover_orphaned_work_claim(...)`. Do **not** erase earlier workflow sections that may already be present in the REQ body; the next session needs to see the partial history.

8. Surface the recovery log path(s) to the user. Recovery must be obvious on disk:

```json
{
  "recovered_at": "2026-04-18T00:40:00Z",
  "claim_id": "REQ-005",
  "released_session_id": "2026-04-18T00-21-52Z-f2d85402",
  "recovered_by_session_id": "2026-04-18T00-39-10Z-1a2b3c4d",
  "claim_path": "do-work/working/REQ-005-evidence-based-orphan-recovery.claim.json",
  "working_request_path": "do-work/working/REQ-005-evidence-based-orphan-recovery.md",
  "queue_request_path": "do-work/REQ-005-evidence-based-orphan-recovery.md",
  "evidence": {
    "verdict": "recoverable",
    "reason": "claim heartbeat is stale, the owning session heartbeat is stale, and the owning PID is absent on this host"
  }
}
```

9. Only after the explicit recovery pass finishes cleanly may the operator run the normal `do work` loop again.

## Commands

### `do work`
Process all pending requests in order. Claims are atomic and session-bound. This command does **not** auto-recover anything already in `working/`; use `do work resume` when recovery evidence needs to be checked explicitly.

### `do work resume`
Inspect every claimed REQ in `do-work/working/` and recover only the unequivocal orphans: stale claim heartbeat, stale session heartbeat, same host, and absent PID. Any ambiguity halts the resume pass and surfaces the evidence instead of guessing.

### `do work REQ-005` (future enhancement)
Process a specific request by number, regardless of status.

### `do work --dry-run` (future enhancement)
Show what would be processed and their triage routes without making changes.

## What This Action Does NOT Do

- Create new request files (use the do action for that)
- Make architectural decisions beyond what's in the request
- Run without user being present (this is supervised automation)
- Modify requests that are already `completed` or `in_progress` by another agent
- Allow external modification of files in `working/` or `archive/` — these are immutable to all actions except the work pipeline itself
- Skip planning for any request, regardless of complexity (every request gets a plan)

## Example Session

```
User: do work

Assistant: Checking do-work/ folder...
Found 3 pending requests. Starting with REQ-003-dark-mode.md...
Triage: Complex (Route C) - new feature across multiple components

[Spawns Plan agent]
Planning complete. Key steps:
  1. Add theme context provider
  2. Create useTheme hook
  3. Add toggle to settings
  4. Update Tailwind config

Verifying plan coverage...
  Items: 8 enumerated from request
  Pre-fix: 88% (7 full, 0 partial, 1 missing)
  Fixed: Added Step 5 for system preference detection
  Post-fix: 100%

[Spawns Explore agent]
Found relevant patterns:
  - Existing context in src/stores/
  - Settings panel at src/components/settings/
  - Tailwind config at tailwind.config.js

[Spawns implementation agent]
Implementation complete:
  - Created src/stores/theme-store.ts
  - Modified src/components/settings/SettingsPanel.tsx
  - Updated tailwind.config.js

Testing: Running related tests...
  - Added tests/theme-store.spec.ts
  - All 12 tests passing ✓

Archived REQ-003-dark-mode.md
Committed → abc1234

Continuing with REQ-004-fix-submit-crash.md...
Triage: Simple (Route A) - bug fix with clear reproduction

[Spawns Plan agent]
Plan: "Fix null check in src/components/Form.tsx handleSubmit(). Add regression test."

Verifying plan coverage...
  Items: 2 enumerated from request
  Coverage: 100%

[Spawns implementation agent]
Implementation complete:
  - Fixed null check in src/components/Form.tsx:42

Testing: Running related tests...
  - Added regression test in tests/form.spec.ts
  - All 8 tests passing ✓

Archived REQ-004-fix-submit-crash.md
Committed → def5678

Continuing with REQ-005-change-timeout.md...
Triage: Simple (Route A) - config value change

[Spawns Plan agent]
Plan: "Update API_TIMEOUT in src/config.ts from 30000 to 60000."

Verifying plan coverage...
  Items: 1 enumerated from request
  Coverage: 100%

[Spawns implementation agent]
Implementation complete:
  - Updated API_TIMEOUT in src/config.ts from 30000 to 60000

Testing: No tests needed for config value change

Archived REQ-005-change-timeout.md
Committed → ghi9012

All 3 requests completed:
  - REQ-003: Route C → abc1234
  - REQ-004: Route A → def5678
  - REQ-005: Route A → ghi9012
```

## Retrospective Value

After running the work action, archived request files contain their full history:

**REQ-003-dark-mode.md (Route C):**
```markdown
---
id: REQ-003
title: Dark Mode
status: completed
created_at: 2025-01-26T09:00:00Z
claimed_at: 2025-01-26T10:30:00Z
route: C
completed_at: 2025-01-26T10:52:00Z
commit: abc1234
---

# Dark Mode

## What
Implement dark mode across the app.

---

## Triage

**Route: C** - Complex

**Reasoning:** New feature requiring theme system, multiple component updates, and Tailwind configuration changes.

**Planning:** Required

## Plan

### Implementation Strategy

1. **Create theme store** (src/stores/theme-store.ts)
   - Use Zustand for state management (matches existing patterns)
   - Store light/dark preference
   - Persist to localStorage

2. **Add useTheme hook** (src/hooks/useTheme.ts)
   - Expose current theme and toggle function
   - Handle system preference detection

3. **Update Tailwind config** (tailwind.config.js)
   - Enable darkMode: 'class'
   - Add dark variants for color palette

4. **Add toggle to settings panel** (src/components/settings/SettingsPanel.tsx)
   - Add ThemeToggle component
   - Position in appearance section

5. **Update key components**
   - Header, sidebar, main content areas
   - Use theme-aware Tailwind classes

### Testing Approach
- Unit tests for theme store
- Component tests for toggle behavior
- Visual regression if available

*Generated by Plan agent*

## Plan Verification

**Source**: REQ-003 (8 items enumerated)
**Pre-fix coverage**: 88% (7 full, 0 partial, 1 missing)
**Post-fix coverage**: 100% (8/8 items addressed)

### Fixes Applied

- Added Step 5: Detect and respect system color scheme preference

*Verified by verify-plan action*

## Exploration

- Existing stores in src/stores/ use Zustand pattern
- Settings panel at src/components/settings/SettingsPanel.tsx
- Tailwind config supports darkMode: 'class'

*Generated by Explore agent*

## Implementation Summary

- Created src/stores/theme-store.ts
- Modified 4 components for dark mode support
- Updated tailwind.config.js

*Completed by work action (Route C)*

## Testing

**Tests run:** npm test
**Result:** ✓ All tests passing (24 tests)

**New tests added:**
- tests/stores/theme-store.spec.ts - store state and persistence
- tests/components/ThemeToggle.spec.ts - toggle behavior

**Existing tests verified:**
- tests/components/SettingsPanel.spec.ts - updated for new toggle

*Verified by work action*
```

**REQ-005-change-timeout.md (Route A):**
```markdown
---
id: REQ-005
title: Change API Timeout
status: completed
created_at: 2025-01-26T09:15:00Z
claimed_at: 2025-01-26T10:55:00Z
route: A
completed_at: 2025-01-26T10:56:00Z
commit: ghi9012
---

# Change API Timeout

## What
Update the API timeout from 30s to 60s.

---

## Triage

**Route: A** - Simple

**Reasoning:** Single config value change, file explicitly mentioned in request.

## Plan

Update API_TIMEOUT in src/config.ts from 30000 to 60000.

*Generated by Plan agent*

## Plan Verification

**Source**: REQ-005 (1 item enumerated)
**Pre-fix coverage**: 100% (1/1 items addressed)

*Verified by verify-plan action*

## Exploration

**Not needed** -- plan names specific file and change. No discovery required.

*Skipped by work action*

## Implementation Summary

- Updated API_TIMEOUT in src/config.ts from 30000 to 60000

*Completed by work action (Route A)*

## Testing

**Tests run:** N/A
**Result:** Config value change, no tests needed

*Verified by work action*
```

This lets you:
- Review what planning recommended vs what was actually done
- Identify requests where complexity assessment was off (plan depth vs actual effort)
- Track patterns in request complexity over time
- Debug failed requests by seeing what context was gathered
- Analyze throughput: timestamps show queue wait time and implementation time
- Calibrate assessments: compare route label to actual plan depth and time spent

**Git integration benefits (when applicable):**
- **Rollback**: `git revert <commit>` undoes one complete request
- **Cherry-pick**: `git cherry-pick <commit>` pulls a specific fix to another branch
- **Bisect**: Find which request introduced a bug
- **Blame**: Commit message links to full request documentation
