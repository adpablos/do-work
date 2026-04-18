---
id: REQ-001
title: Session identity and heartbeat infrastructure
status: pending
created_at: 2026-04-17T23:50:00Z
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
