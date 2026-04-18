---
id: REQ-005
title: Evidence-based orphan claim recovery
status: pending
created_at: 2026-04-17T23:50:00Z
user_request: UR-001
related: [REQ-001, REQ-002, REQ-004]
batch: parallel-safety
---

# Evidence-Based Orphan Claim Recovery

## What

Replace the current "claim is older than 1 hour → auto-unclaim" logic with a recovery mechanism that uses session identity and process evidence. A claim becomes recoverable only when its heartbeat is stale **and** the originating process is demonstrably absent on the current host. Slow sessions keep their work; truly crashed sessions are detected quickly.

## Detailed Requirements

- **Two-signal recovery.** A stale claim is never reclaimed on timestamp alone. Both signals are required:
  - Heartbeat older than the stale threshold from REQ-001.
  - No live process on the current host matching the PID and hostname recorded in the claim (from REQ-001's process identification).
- **No pure TTL.** Explicitly reject any path that makes "old enough = dead" the sole criterion. UR-001 binding decision.
- **Single-machine scope.** Hostname check is meaningful because UR-001 constrains the system to one machine per filesystem. If a claim's hostname does not match the current host, treat as "not recoverable here" — the user may be running on a different machine with shared storage. Fail loud in that case.
- **Fail-loud ambiguity.** If the heartbeat is stale and the process appears to still exist (e.g., PID is reused by an unrelated process), the recovery logic must refuse to auto-recover and surface the claim for the user to resolve. Guessing is forbidden (UR-001 recovery policy).
- **Recovery is an explicit action, not an implicit side effect.** Orphan detection happens in a dedicated path — either a manual command, an explicit step at session start, or a periodic check — not hidden inside every claim attempt. The work action's claim attempt (REQ-004) assumes every existing claim is live and reports a conflict; it does not silently reclaim.
- **Recovery moves the REQ to a known state.** A recovered claim returns the REQ to the queue with breadcrumbs: a note in the REQ's frontmatter or a sidecar record capturing "recovered from session X, heartbeat last at Y, prior work may be partial." The next session picking it up sees the history.
- **Recovery log.** Every orphan recovery is logged on disk (`do-work/.recovery-log` or similar) with timestamp, session ID freed, and evidence used. No silent reclaims.
- **Cleanup of session records.** When recovery fires, the originating session's record (from REQ-001) is also cleaned up, not just the claim.

## Constraints

- **All batch constraints apply**, especially:
  - Fail loud on ambiguity (UR-001 recovery policy).
  - Auto-recover only the unequivocal — this REQ defines "unequivocal" as "stale heartbeat AND process absent on same hostname."
  - Filesystem-only.
- **No raising the stale threshold to hide races.** The tuning is in REQ-001. This REQ does not move the threshold around.
- **Process-absence check is host-local.** Do not attempt cross-host probes — out of scope per UR-001.

## Dependencies

- **Blocked by:** REQ-001 (session ID + PID + hostname recorded in claim), REQ-002 (claim format, stale-lock inspection helper), REQ-004 (claim shape this REQ interprets).
- **Blocks:** none directly, but a working recovery mechanism is a precondition for trusting parallel execution in the long run.

## Builder Guidance

- **Certainty level: firm.** The user resolved this as binding: evidence + heartbeat, not pure TTL.
- **Keep the UX loud.** On recovery, the log entry must be unambiguous and the user must be able to see what happened by inspecting the filesystem. The enemy of this UR is silent recovery that obscures what really happened.
- **Target files: `actions/work.md`** for the at-start orphan sweep hook; whatever command entry point makes sense for explicit recovery (`do work resume` already exists per CHANGELOG — extend it rather than invent a new command).
- **Do not try to "improve" the recovery by adding heuristics** like "the REQ looks almost done so finish it automatically." Fail loud, let the user decide.

## Open Questions

- Does recovery run automatically at session start for *this* session's own abandoned claims (if any), or only via explicit command? Suggested: explicit only, per fail-loud policy. Confirm during planning.
- How to detect process existence portably (macOS + Linux) without heavy deps. `kill -0 <pid>` works for same-user processes; formalize during planning.
- PID reuse edge case: the PID originally recorded is now owned by an unrelated process. This is the "appears to still exist" ambiguity — fail loud, but document explicitly how the check distinguishes reused PIDs (e.g., start-time comparison if cheaply available).

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "Orphaned claims from crashed sessions — or stolen from slow ones" is the third failure mode.

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (5/5 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "An old `claimed_at` timestamp is treated as a signal that the claiming session died" — and that is wrong in both directions (Failure mode) | Detailed Requirements — Two-signal recovery, No pure TTL | Full |
| 2 | "A legitimate but slow session loses its work to the unclaim" (Failure mode) | Detailed Requirements — Two-signal recovery | Full |
| 3 | "A genuinely crashed session's REQ gets stolen before any recovery signal" (Failure mode) | Detailed Requirements — Two-signal recovery, Recovery log | Full |
| 4 | "A claim is recoverable only when the heartbeat is stale *and* the originating process is not detectable on the current host" (Resolved decision — Claim model) | Detailed Requirements — Two-signal recovery, Single-machine scope | Full |
| 5 | "Auto-recover only the unequivocal… ambiguous situations must surface to the user and halt" (Resolved decision — Recovery policy) | Detailed Requirements — Fail-loud ambiguity, Recovery is an explicit action | Full |

*Verified by verify-request action*
