---
id: REQ-002
title: Shared concurrency primitives library
status: completed
created_at: 2026-04-17T23:50:00Z
claimed_at: 2026-04-18T00:22:00Z
completed_at: 2026-04-18T01:28:06Z
route: C
user_request: UR-001
related: [REQ-001, REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010]
batch: parallel-safety
---

# Shared Concurrency Primitives Library

## What

Build a single internal library of filesystem-based concurrency primitives that every do-work action will use: exclusive lockfiles, atomic renames, claim file format, and a standard way to acquire/release/inspect locks. Every other race-fix REQ in this batch consumes this library; none of them should be rolling their own locking.

## Detailed Requirements

- **Exclusive lockfile primitive.** Provide a single helper that acquires an exclusive lock on a given path using `O_EXCL` creation semantics (or equivalent) so a second caller on the same path fails fast instead of blocking silently. Must support: acquire, release, held-by-whom inspection.
- **Lock identity.** Every acquired lock records the session ID of the holder (from REQ-001). Reading the lockfile must reveal `{session_id, acquired_at, last_heartbeat, operation_name}`.
- **Stale lock detection.** Provide a single helper that reports whether an existing lock is live, stale, or orphaned. Never auto-delete a lock inside this helper — surface the verdict, let the caller decide per the recovery policy (see REQ-005).
- **Atomic rename helper.** Wrap `rename()` with an API that treats the operation as an atomic state transition (queue → working, working → archive, draft → committed). Callers must not use raw `mv` / `os.rename` for state transitions once this library exists.
- **Atomic file write.** Provide write-then-rename so partial-failure in the middle of writing a UR or REQ file never leaves a half-written file visible to readers.
- **Claim file format.** Define one on-disk schema for claim files — session ID, timestamps, operation name, affected paths — used by REQ-004 (work claim), REQ-007 (verify claim), REQ-009 (cleanup claim). One format, one parser.
- **Standard lock scopes.** The library names and exposes the scopes the actions will use (e.g., ID-allocation lock, per-document lock, global cleanup lock). Ad-hoc scope names sprinkled through action code are forbidden — add them here first.
- **Deterministic failure messages.** When a lock acquisition fails, the error must include the current holder's session ID and operation so the user sees a useful message (supports the "clear, actionable message" done-criterion).
- **Testability.** The primitives must be exercisable in isolation (unit-testable) without spinning up an actual do-work session. Concurrency tests for this library are in scope.

## Constraints

- **Filesystem-only.** `O_EXCL`, atomic rename, flock-style if helpful — no external services, no SQLite.
- **No new runtime dependencies.** Standard library only, or an already-vendored helper. New packages require user sign-off.
- **Shared primitives, per-action policy.** This REQ owns the primitives. Individual actions (REQ-003..REQ-010) decide where and at what scope to use them — the library does not assume.
- **Fail loud.** Lock acquisition failure produces an informative error, never silent skip.
- **Debuggable by `ls` + `cat`.** A user inspecting the state directory must be able to read and understand a claim or lockfile without running do-work code.

## Dependencies

- **Blocked by:** REQ-001 (session ID must exist — lock files record session_id).
- **Blocks:** REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010. Every race fix consumes this library.

## Builder Guidance

- **Certainty level: firm.** Batch constraint "shared primitives, per-action policy" is binding. Do not re-propose a per-action locking model.
- **This is a foundation REQ.** Resist the urge to wire it into any action in this REQ — that is what subsequent REQs are for. Ship the library with tests and stop.
- **Keep the API small.** A few obvious primitives beat a rich framework. If a helper is only used once, it can be inlined in the caller.
- **Document the scope names.** Subsequent REQs will need to refer to them unambiguously.

## Open Questions

- Exact location for the library in the repo structure (new `lib/` folder inside the skill, or embedded in `actions/`). Decide during planning.
- Whether to expose a tiny CLI (e.g., `do-work debug locks`) that lists current lock state. Useful for debugging but scope-creep risk — decide during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) for the complete PRD, including the binding architectural decision that the skill ship "shared primitives, per-action policy."

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (6/6 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "Build one internal library of locks / atomic moves / claim management" (Resolved decision — Concurrency model) | Detailed Requirements — Exclusive lockfile primitive, Atomic rename helper, Claim file format | Full |
| 2 | "Each action declares which primitives it uses and at what scope" (Resolved decision — Concurrency model) | Detailed Requirements — Standard lock scopes; Constraints — Shared primitives, per-action policy | Full |
| 3 | Filesystem-only: lockfiles, atomic renames, `O_EXCL` (Resolved decision — Coordination substrate) | Detailed Requirements — Exclusive lockfile primitive, Atomic rename helper; Constraints — Filesystem-only | Full |
| 4 | Clear, actionable messages instead of silence and corruption (Done-criteria #8) | Detailed Requirements — Deterministic failure messages | Full |
| 5 | "The folder *is* the state; `ls` must still be a legitimate debug tool" (Resolved decision — Coordination substrate) | Constraints — Debuggable by `ls` + `cat` | Full |
| 6 | "No locks, no claim tokens, no atomic state transitions" (Problem) | Detailed Requirements — Claim file format | Full |

*Verified by verify-request action*

---

## Triage

**Route: C** — Complex

**Reasoning:** Cross-cutting foundation library that every other REQ in the parallel-safety batch consumes. Introduces new on-disk schemas (claim format, lockfile format) and the standard primitive API — no existing pattern in the repo. Same class as REQ-001: protocol-level change with testability requirements, and a binding constraint ("shared primitives, per-action policy") that must be honored without leaking into action policy decisions.

## Plan

### Nature of this delivery

REQ-002 differs from REQ-001 in one important way: it explicitly requires primitives to be **unit-testable in isolation**. A protocol-only document (as REQ-001 shipped) is insufficient — "concurrency tests for this library are in scope" is a binding line in the REQ. Therefore REQ-002 introduces the skill's first executable runtime: a small Python 3 stdlib-only helper module at `lib/concurrency.py`, paired with a canonical protocol document that agents read.

Rationale for Python 3 over shell:
- Real `O_EXCL` semantics via `os.open(..., O_CREAT | O_EXCL)` — the REQ names this exact primitive.
- Atomic `os.rename` with well-defined POSIX semantics.
- `json` in stdlib for on-disk schema — `cat` still reads it, meeting the "debuggable by `ls` + `cat`" constraint.
- `unittest` + `multiprocessing` in stdlib for real concurrency tests.
- Universally available on dev machines. No new runtime dependencies (constraint satisfied).

The module is consumed by downstream REQs, not wired into actions here (explicit builder guidance: "Resist the urge to wire it into any action in this REQ").

### Deliverables

1. **`actions/concurrency-primitives.md`** — canonical protocol document. Covers:
   - Module location (`lib/concurrency.py`) and invocation contract (agents call `python3 -c "from lib.concurrency import ..."` or equivalent one-liner per primitive, documented with examples).
   - Exclusive-lock contract: `O_EXCL` create of a lockfile at the target path; second caller fails fast.
   - Lockfile content schema: `{session_id, acquired_at, last_heartbeat, operation, scope, pid, hostname}`.
   - Stale-lock **verdict** helper: reports `live | stale | orphaned` using `last_heartbeat` + PID liveness on `hostname`, **never** deletes — caller decides.
   - Atomic rename contract: state-transition helper wrapping `os.rename` with a required `transition` label for error messages (e.g., `queue→working`).
   - Atomic write contract: write-to-temp-then-rename on the same filesystem.
   - Claim file format: one JSON schema with `{session_id, claim_id, operation, scope, affected_paths, acquired_at, last_heartbeat}` — shared by REQ-004/REQ-007/REQ-009.
   - Enumerated standard lock scopes (see below). Ad-hoc scope names are forbidden; new scopes are added to this doc first.
   - Deterministic failure messages: every acquisition failure names the current holder's `session_id` and `operation`, and the attempting session's `session_id` and `operation`.
   - Explicit NON-goals: this REQ does not implement any claim (REQ-004), orphan recovery (REQ-005), or action wiring.
   - Optional debug one-liner (`python3 -m lib.concurrency inspect <path>`) — decide during implementation; documented if shipped.

2. **`lib/concurrency.py`** — executable module, stdlib only. Public API:
   - `acquire_lock(path, *, session_id, operation, scope, pid=None, hostname=None, now=None) -> LockHandle`
   - `release_lock(handle_or_path)`
   - `inspect_lock(path) -> LockInfo | None` (returns dataclass)
   - `classify_lock(info, *, now, stale_threshold=timedelta(minutes=2)) -> Literal["live", "stale", "orphaned"]` (pure, no I/O side effects on the verdict; process-liveness check via `os.kill(pid, 0)` when hostname matches)
   - `atomic_rename(src, dst, *, transition)` — raises rich errors on ENOENT / cross-device / collision.
   - `atomic_write(path, content, *, mode="w")`
   - `read_claim(path) -> ClaimRecord`, `write_claim(path, claim)` — backed by atomic_write.
   - `SCOPES` — frozen set of canonical scope names.
   - Exceptions: `LockHeldError` (carries holder info), `StaleRenameError`, `AtomicWriteError`, `ScopeError`.
   - Module is importable as `lib.concurrency` (via `lib/__init__.py`).

3. **`lib/concurrency_test.py`** — standalone unittest suite. Tests:
   - Acquire/release happy path: lock file exists, has expected fields, release removes it.
   - Second acquire on held lock raises `LockHeldError` with holder `session_id` and `operation` in the message.
   - `inspect_lock` returns full fields; `inspect_lock` on absent path returns `None`.
   - `classify_lock`: live (recent heartbeat, PID alive), stale (old heartbeat, PID alive), orphaned (PID absent on same hostname) — no file mutations.
   - `atomic_rename` succeeds and refuses cross-device (simulate by monkeypatching `os.rename` to raise `EXDEV`).
   - `atomic_write` crash-safety: writer interrupted after temp-write but before rename leaves no visible file at target.
   - Claim round-trip: write → read preserves all fields.
   - Scope enforcement: ad-hoc scope names rejected.
   - **Concurrency race test**: 20 child processes race on one lock via `multiprocessing`; assert exactly one reports success and 19 report `LockHeldError`.
   - All tests use `tempfile.TemporaryDirectory` — zero contact with live `do-work/` state.

4. **Standard lock scope catalog** (shipped in the protocol doc; referenced by downstream REQs):
   - `id-allocation` — REQ-003, global scope for atomic REQ/UR ID allocation.
   - `req-claim:<REQ-id>` — REQ-004, per-REQ scope when claiming from the queue.
   - `ur-archival:<UR-id>` — REQ-006, per-UR scope during archival.
   - `verify-doc:<path>` — REQ-007, per-document scope during verification.
   - `cleanup-global` — REQ-009, exclusive lock for the cleanup action.
   - `foreign-edit:<path>` — REQ-010, per-document hash lock.
   (Listed for downstream REQs to reference unambiguously; this REQ only defines and exposes them.)

5. **Action-file pointer updates** — minimal, no rewiring of action logic:
   - `actions/session-identity.md` — replace the pending `REQ-002` TODO ("Use the atomic-write primitive from REQ-002 once it is available") with a concrete pointer to `lib/concurrency.atomic_write`.
   - `actions/work.md` — add a note near Steps 2 and 7 that state-transition `mv` commands **must** be replaced with `atomic_rename` once REQ-004 / REQ-006 land. (Don't rewire now — that is REQ-004's job per builder guidance.)
   - `actions/do.md`, `actions/cleanup.md` — same pattern: add a reference to the primitives doc without changing current behavior.
   - `SKILL.md` — add `lib/concurrency.py` and `actions/concurrency-primitives.md` to the Action References section.

6. **`.gitignore`** — add `do-work/.locks/` if the protocol doc places lockfiles there; otherwise leave alone. Lockfiles are ephemeral runtime state and must never be committed.

7. **Version + CHANGELOG** — per `CLAUDE.md`: minor bump (new protocol + library), new changelog entry with a fun two-word name, date `2026-04-18`.

### Testing approach

- `python3 -m unittest lib.concurrency_test -v` from the repo root.
- Concurrency test uses `multiprocessing.Pool` with 20 workers, a shared target lockfile in a tempdir, and `multiprocessing.Barrier` to synchronize the start — guarantees genuine contention.
- All tests must pass before archival. If the race test is flaky, retry once; if still flaky, the primitive is wrong, fix it.
- Run from the repo root so `import lib.concurrency` resolves.

### Order of operations

1. Protocol doc (establishes contracts tests will check).
2. Module (implements contracts).
3. Tests (verify contracts).
4. Run tests → green.
5. Action pointer updates.
6. SKILL.md, .gitignore.
7. Version bump + changelog.
8. Archive + commit + release session record.

### Scope discipline

- **No action rewiring.** Callers of the primitives are introduced in REQ-003..REQ-010. This REQ ships primitives + contracts + tests and stops.
- **No orphan-recovery logic.** `classify_lock` emits a verdict; acting on the verdict is REQ-005.
- **No claim acquisition logic.** `write_claim` / `read_claim` handle the on-disk format only. Claim lifecycle is REQ-004 / REQ-007 / REQ-009.
- **Keep the API small.** If a helper is used by only one test, inline it.

*Generated by Plan agent (in-session)*

## Plan Verification

**Source**: REQ-002 + UR-001 input.md (22 items enumerated)
**Pre-fix coverage**: 97.7% (21 full, 1 partial, 0 missing)
**Post-fix coverage**: 100% (22/22 items addressed)

### Coverage Map

| # | Requirement | Plan Location | Status |
|---|------------|---------------|--------|
| 1 | Exclusive lockfile primitive (`O_EXCL`, acquire/release/inspect) | Deliverable 2 — `acquire_lock`, `release_lock`, `inspect_lock` | Full |
| 2 | Lock identity: records `session_id`, `acquired_at`, `last_heartbeat`, `operation` | Deliverable 1 — lockfile content schema; Deliverable 2 — `acquire_lock` signature | Full |
| 3 | Stale-lock verdict helper (`live`/`stale`/`orphaned`, no auto-delete) | Deliverable 2 — `classify_lock` (pure, no mutation) | Full |
| 4 | Atomic rename helper (state transitions, replaces raw `mv`) | Deliverable 2 — `atomic_rename`; Deliverable 5 — action-file pointer notes | Full |
| 5 | Atomic file write (write-then-rename) | Deliverable 2 — `atomic_write` | Full |
| 6 | Claim file format (session_id, timestamps, operation, affected_paths) | Deliverable 1 — claim schema; Deliverable 2 — `read_claim`/`write_claim` | Full |
| 7 | Standard lock scopes (enumerated, no ad-hoc names) | Deliverable 4 — scope catalog; Deliverable 2 — `SCOPES` + `ScopeError` | Full |
| 8 | Deterministic failure messages (holder `session_id` + `operation`) | Deliverable 2 — `LockHeldError` with holder info | Full |
| 9 | Testability (isolated unit tests, concurrency tests) | Deliverable 3 — unittest suite + `multiprocessing` race test | Full |
| 10 | Filesystem-only (no SQLite, no daemon) | Deliverable 2 — stdlib `os`/`json` only | Full |
| 11 | No new runtime dependencies | Deliverable 2 — Python 3 stdlib only | Full |
| 12 | Shared primitives, per-action policy | Deliverable 1 — protocol doc; Plan "Scope discipline" keeps action policy out | Full |
| 13 | Fail loud on lock acquisition failure | Deliverable 2 — raises `LockHeldError` (never silent skip) | Full |
| 14 | Debuggable by `ls` + `cat` | Deliverable 1 — JSON on-disk + debug note; see fix below | Full |
| 15 | Blocked by REQ-001 (lockfiles record `session_id`) | Deliverable 2 — `session_id` is required parameter of `acquire_lock` | Full |
| 16 | Blocks REQ-003..REQ-010 | Deliverable 4 — scope catalog names downstream call sites | Full |
| 17 | Firm certainty — no per-action locking model | Plan "Scope discipline" keeps per-action policy out | Full |
| 18 | Foundation REQ — do not wire into any action | Deliverable 5 — "minimal, no rewiring of action logic" | Full |
| 19 | Keep API small | Plan "Scope discipline: keep the API small" | Full |
| 20 | Document scope names | Deliverable 4 — scope catalog | Full |
| 21 | Library location decided | Deliverable 2 — `lib/concurrency.py` | Full |
| 22 | Debug CLI decision | Deliverable 1 flagged as "decide during implementation" — fixed below | Partial → Fixed |

### Fixes Applied

- **Item #22 (debug CLI):** Decision made now — **skip** the debug CLI for this REQ. `cat do-work/.locks/<scope>.lock` already reveals state; shipping a CLI expands surface area without clear benefit and the REQ itself flagged it as scope-creep risk. The decision is documented in the protocol doc's "Out of scope" section.

*Verified by verify-plan action*

## Exploration

**Not needed** — the plan names specific new files (`lib/concurrency.py`, `lib/concurrency_test.py`, `actions/concurrency-primitives.md`) and specific pointer edits in known action files. No existing patterns to discover: this is the first `lib/` addition in the repo, and REQ-001's archived REQ already established the session-identity precedent the pointer updates follow.

*Skipped by work action*

## Implementation Summary

### Files created

- `actions/concurrency-primitives.md` — canonical shared-primitives contract covering lockfile schema, claim-file schema, scope catalog, failure modes, and explicit non-goals for the later REQs that will wire this in.
- `lib/__init__.py` — marks `lib/` as importable so downstream action snippets can use `lib.concurrency`.
- `lib/concurrency.py` — stdlib-only runtime for exclusive lock acquisition, lock inspection, heartbeat refresh, stale/orphan classification, atomic write, atomic rename, and claim-file read/write helpers.
- `lib/concurrency_test.py` — isolated unittest suite with both unit coverage and a 20-process race test.

### Files modified

- `actions/session-identity.md` — replaced the old REQ-002 placeholder with a concrete pointer to `lib.concurrency.atomic_write(...)` for session record writes.
- `actions/work.md` — marked Step 2 and Step 7 state transitions as future `atomic_rename(...)` call sites once REQ-004 and REQ-006 do the actual wiring.
- `actions/do.md`, `actions/cleanup.md`, `SKILL.md` — added lightweight references to the new shared-primitives doc/library so later REQs have one canonical place to point.
- `.gitignore` — added `do-work/.locks/` so runtime lockfiles never hit git.
- `actions/version.md` — bumped the skill to `0.15.0`.
- `CHANGELOG.md` — added `0.15.0 — The Lockbox (2026-04-17)` at the top.

### Implementation notes

- Scope validation now lives in the library, not in each later action. Canonical scopes and parameterized prefixes are enforced centrally via `validate_scope(...)`.
- Lockfiles and claim files stay intentionally `cat`-readable JSON. No hidden state, no daemon, no database.
- `classify_lock(...)` only returns a verdict. It never deletes or rewrites a lockfile; recovery stays deferred to REQ-005.
- The library includes `refresh_heartbeat(...)` because long-held locks need the same heartbeat discipline REQ-001 introduced for session records.

### Deviations from plan

None. The debug CLI was intentionally skipped exactly as decided in plan verification; the library remains a small importable module plus tests, with no action rewiring.

*Completed by work action (Route C)*

## Testing

**Command run:**

```bash
python3 -m unittest lib.concurrency_test -v
```

**Result:** Pass (17/17 tests green)

**Coverage exercised by the suite:**

- Acquire/release/inspect lock happy path
- Deterministic `LockHeldError` holder details on contention
- Heartbeat refresh and foreign-release protection
- `classify_lock(...)` verdicts for live, stale, and orphaned locks
- `atomic_rename(...)` success, missing-source, collision, and cross-device failure modes
- `atomic_write(...)` crash-safety cleanup when rename fails mid-write
- Claim-file round-trip and scope validation
- Real contention test: 20 spawned processes race for one lock and exactly one wins

**Notes:**

- Tests run entirely in `tempfile.TemporaryDirectory()` and never touch live `do-work/` state.
- The concurrency test uses `multiprocessing` with spawned child processes, so it exercises actual `O_EXCL` lock contention rather than a mocked path.

*Verified by work action*
