# Session Identity Action

> **Part of the do-work skill.** Defines the session identity and heartbeat protocol that every coordinated action relies on. This file is the foundation for the parallel-safety work — claims, orphan recovery, and foreign-edit detection all consume the session record format defined here.

A lightweight, filesystem-only protocol so two do-work sessions running in parallel can tell each other apart, detect when one of them has died, and recover safely without guessing.

## What This Action Does

On every session start, the executing agent establishes a **session identity** — a stable `{session_id, hostname, pid, started_at}` tuple — and writes it to disk as a **session record**. While the session is doing coordinated work (capturing, claiming REQs, verifying, cleaning up), it updates a `last_heartbeat` timestamp at a bounded cadence. On graceful exit, it releases the record. On crash, the record is left behind so future sessions can recognize the abandonment using process-absence evidence rather than a TTL guess.

This action does **not** implement claims, locks, foreign-edit detection, or orphan recovery. Those are separate REQs in the parallel-safety batch and they consume this protocol. The rule: **every action establishes session identity before taking any coordinated step.**

## When This Runs

- **On every session start, before any coordinated action.** The do, work, verify-request, verify-plan, and cleanup actions each invoke this protocol as their Step 0 (before their own work order).
- **Before any write to `do-work/` beyond read-only inspection.** A session that only reads (e.g., the version action reporting the current version, or `do work changelog` printing the changelog) does not need to establish session identity.
- **Once per session.** The session ID is stable for the lifetime of the session. Repeated invocations of the protocol within the same session are no-ops that refresh the heartbeat.

## Session Record Format

Session records live on disk at:

```
do-work/.sessions/<session_id>.json
```

Each record is a single JSON file with the following required fields:

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Unique identifier. See Generation Rules below. |
| `hostname` | string | The machine hostname as reported by `hostname` or equivalent. Used to scope recovery checks to the current host (UR-001 is single-machine). |
| `pid` | integer | The OS process ID of the session. Used for process-absence evidence during orphan recovery. |
| `started_at` | ISO-8601 string | The session's start time (UTC). |
| `last_heartbeat` | ISO-8601 string | The most recent heartbeat update (UTC). Equal to `started_at` at creation. |
| `operation` | string | The action currently running (`"do"`, `"work"`, `"verify-request"`, `"verify-plan"`, `"cleanup"`, or `"idle"`). Updated as the session moves between actions in the same process. |

Example record:

```json
{
  "session_id": "2026-04-17T23-57-00Z-a7f3c9",
  "hostname": "alejandro-laptop",
  "pid": 41823,
  "started_at": "2026-04-17T23:57:00Z",
  "last_heartbeat": "2026-04-17T23:57:30Z",
  "operation": "work"
}
```

### Session ID Generation Rules

The session ID must be unique across concurrent sessions on the same machine and readable enough that a human debugging can tell one session from another.

Required shape: `<ISO-timestamp-compressed>-<random-hex>`.

- ISO-timestamp-compressed: the session start time in UTC, with colons replaced by dashes — e.g., `2026-04-17T23-57-00Z`.
- Random hex: at least 6 lowercase hex characters. More is fine.

Example valid IDs:
- `2026-04-17T23-57-00Z-a7f3c9`
- `2026-04-17T23-57-00Z-8b2e4d0a1f`

Why this format: the timestamp makes sessions human-sortable in `ls`; the random hex prevents collision when two sessions start in the same second; the combination is short enough to paste into logs or error messages without eye strain.

Do not use PID alone — PIDs get reused and are opaque. Do not use UUIDs without a timestamp prefix — they are unsortable.

## Heartbeat Cadence and Stale Threshold

- **Default heartbeat cadence: 30 seconds.** While a session is running a coordinated action, it updates `last_heartbeat` at least this often.
- **Default stale threshold: 2 minutes.** A session record whose `last_heartbeat` is older than this is a candidate for orphan recovery (but is *never* auto-reclaimed on timestamp alone — see below).

Both values are tunable. If a future action needs a longer cadence because it does slow I/O, document that at the call site and keep the stale threshold at least 2× the cadence.

### Heartbeat Emission Inside Long Actions

If an action performs a single long-blocking operation (e.g., a shell command that takes a minute), the agent must:

- Refresh the heartbeat immediately before the blocking operation starts.
- Refresh it immediately after the operation returns.
- If the operation is expected to exceed the stale threshold, break it into chunks with heartbeat refreshes between, or document in the action that this operation is expected to appear stale — future orphan-recovery logic (REQ-005) will then require additional evidence before treating it as dead.

Heartbeats are **per-session**, not **per-claim**. A session that holds multiple locks or claims still writes one heartbeat to its session record. Claims reference the session ID; they do not carry their own heartbeat.

## Lifecycle

### Session Start

Every action begins with this sequence, before any write to `do-work/` beyond read-only inspection:

1. If the current process has already established a session record (check via an in-process variable or by matching PID against existing records), skip to step 5.
2. Generate a `session_id` per the rules above.
3. Capture `hostname`, `pid`, `started_at` (now, UTC).
4. Create `do-work/.sessions/` if it does not exist. Write the session record as `do-work/.sessions/<session_id>.json`. **Use the atomic-write primitive from REQ-002 once it is available**; until then, write to a temp name and rename atomically.
5. Set `operation` to the current action name.
6. Update `last_heartbeat` to now.

If step 4 fails (disk full, permissions, read-only filesystem), **fail loud**: refuse to proceed with the action. A session without identity cannot participate in coordinated work.

### Session Running

During the action:

- Update `last_heartbeat` before and after any operation that could exceed the stale threshold.
- Update `operation` whenever the action changes phase (e.g., work action moving from `planning` to `implementing` — free-form string, useful for debugging).
- Do **not** modify `session_id`, `hostname`, `pid`, or `started_at` after creation.

### Session Exit — Graceful

When the action completes normally (success or expected failure):

- Delete `do-work/.sessions/<session_id>.json`.
- Do this even if the action failed with an error — a graceful exit releases identity regardless of whether the work succeeded.

Graceful release means future sessions see the absence of the record as proof that this session is gone. They do not have to rely on process-absence evidence.

### Session Exit — Crash

When the action is killed (SIGKILL, power loss, agent crash, session window closed):

- The record remains on disk.
- Future sessions checking for orphan claims (logic defined in REQ-005) use `hostname + pid` to probe process liveness on the current host.
- If the PID is absent (or reused by a process with a different start-time where detectable) **and** the heartbeat is stale, the record is recoverable.
- If either signal is ambiguous, **fail loud**: surface the orphan for user resolution rather than guess.

REQ-001 defines the **data** needed for this recovery. REQ-005 implements the **logic**. Do not attempt recovery from inside this action.

## Debuggability Contract

A user troubleshooting do-work must be able to understand session state with no do-work knowledge beyond this document:

- `ls do-work/.sessions/` reveals every live or orphaned session.
- `cat do-work/.sessions/<session_id>.json` reveals the session's full state.
- The session ID format makes the start time visible in the filename alone.

No binary formats. No hidden files beyond the `.sessions/` folder. No database.

## What This Protocol Does NOT Cover

Strictly out of scope for REQ-001 (each item has its own REQ in the parallel-safety batch):

- **Shared concurrency primitives** (lockfiles, atomic renames as a library) — REQ-002.
- **Atomic ID allocation** — REQ-003.
- **REQ claim format and atomic claim operation** — REQ-004.
- **Orphan claim recovery logic** — REQ-005.
- **Atomic UR archival** — REQ-006.
- **Verify document locks** — REQ-007.
- **Capture atomicity and rollback** — REQ-008.
- **Cleanup global lock** — REQ-009.
- **Foreign-edit detection at claim and commit** — REQ-010.

If a change under this action creeps into any of these, stop and escalate. They are sequenced deliberately.

## Exemptions

Actions that perform only read-only inspection of `do-work/` and make no claims, no locks, no writes to coordinated state are exempt from establishing session identity. Currently that means:

- `version` action when reporting the current version or printing the changelog.

All other actions (`do`, `work`, `verify-request`, `verify-plan`, `cleanup`) must establish session identity at Step 0.

## Assumed Environment

- **Single machine, single filesystem.** `hostname` is a meaningful scope for "is this the same host?" checks. Cross-host coordination is out of scope (UR-001).
- **POSIX-ish filesystem.** Atomic rename within the same filesystem is available. `do-work/.sessions/` lives on the same filesystem as the rest of `do-work/`.
- **Process identity via OS PID.** Same host, same user. PID reuse is a known edge case — surface it for user resolution during orphan recovery; do not auto-recover ambiguous cases.

## Gitignore

Session records are ephemeral runtime state. `do-work/.sessions/` must be in `.gitignore`. Committing a session record to git would falsely imply an active session in a shared repo.
