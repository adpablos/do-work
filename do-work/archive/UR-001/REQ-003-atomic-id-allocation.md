---
id: REQ-003
title: Atomic ID allocation for REQs and URs
status: completed
created_at: 2026-04-17T23:50:00Z
claimed_at: 2026-04-18T16:45:28Z
completed_at: 2026-04-18T16:45:28Z
route: C
user_request: UR-001
related: [REQ-001, REQ-002, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009, REQ-010]
batch: parallel-safety
---

# Atomic ID Allocation for REQs and URs

## What

Replace the current "scan highest ID and increment" pattern in the capture action with a lock-guarded allocation so two parallel sessions cannot mint the same REQ-NNN or UR-NNN. Uniqueness must be structural — a property of the allocation primitive — not a best-effort scan.

## Detailed Requirements

- **Lock-guarded allocation.** ID allocation (REQ-NNN, UR-NNN) must happen under the short ID-allocation lock defined in REQ-002. Scan → increment → write → release, all inside the held lock.
- **One lock per ID namespace.** REQs and URs have separate lock scopes so concurrent capture requests do not serialize when they do not need to.
- **Scan authoritative locations.** When computing "next ID", the scan must cover every place a REQ/UR can live: `do-work/` (queue), `do-work/working/`, `do-work/archive/`, `do-work/user-requests/`, and `do-work/archive/UR-*/`. Missing any of these reintroduces collisions.
- **Write-then-release.** The allocation is only complete when the new UR folder / REQ file exists on disk (via the atomic write helper from REQ-002). The lock is released after the write, not before — otherwise a second session can re-derive the same ID from a still-empty directory.
- **Failure atomicity.** If the allocation fails mid-flow (disk full, permissions, crash), the lock must be released and partial state must not persist (see REQ-008 for full capture rollback).
- **Deterministic ordering is not required.** Two captures launched simultaneously may receive IDs in either order; the only requirement is that they receive different IDs and neither silently overwrites the other.
- **Scope: capture only.** The work action does not allocate IDs. This REQ only modifies capture (`actions/do.md` flow). Ensure no other action mints IDs.
- **Loud conflict detection.** If the allocator detects an existing file at the "next" ID (e.g., created by a legacy tool outside the lock), it must fail loudly with the conflicting path — not silently skip.

## Constraints

- **All batch constraints apply**, especially:
  - Filesystem-only. No sequence-generator service.
  - Fail loud on any inconsistency.
  - Debuggable by `ls`: allocation must leave no hidden state.
- **Minimal lock scope.** Hold the ID-allocation lock only long enough to scan + write the placeholder. Do not hold it for the entire capture flow; capture content can be written after the ID is minted.
- **No renaming after allocation.** Once an ID is assigned, it is fixed. No "rebalancing" or renumbering.

## Dependencies

- **Blocked by:** REQ-001 (session ID), REQ-002 (shared primitives — specifically the exclusive lockfile and the atomic write helper).
- **Blocks:** REQ-008 (capture atomicity builds on top of this).

## Builder Guidance

- **Certainty level: firm.** Done-criterion #1 ("Uniqueness is structural, not best-effort") is non-negotiable.
- **Target file: `actions/do.md`.** Touch only the "Determine the next REQ/UR number" steps and the surrounding file-creation flow. Do not rewrite unrelated parts of capture.
- **Concurrency tests required.** This is exactly the kind of race a unit test with simulated parallel callers can demonstrate — add tests that fire two allocations at once and assert they produce distinct IDs.

## Open Questions

- Where the ID-allocation lockfile lives on disk. Suggested: `do-work/.locks/id-allocation-req.lock` and `do-work/.locks/id-allocation-ur.lock`. Final choice per REQ-002 conventions.
- Whether to bake in a safety check that also verifies no in-progress lockfile for the newly-minted ID exists elsewhere. Decide during planning.

## Full Context

See [user-requests/UR-001/input.md](./user-requests/UR-001/input.md) — "ID collisions on new REQs and URs" is the first failure mode listed.

---
*Source: See UR-001/input.md for full verbatim input*

## Verification

**Source**: UR-001/input.md
**Pre-fix coverage**: 100% (4/4 items)

### Coverage Map

| # | Item | REQ Section | Status |
|---|------|-------------|--------|
| 1 | "Two sessions running capture in parallel both see the same highest ID and both mint the next one" (Failure mode — ID collisions) | Detailed Requirements — Lock-guarded allocation, Write-then-release | Full |
| 2 | "No atomic counter, no uniqueness enforcement" (Failure mode — ID collisions) | Detailed Requirements — Lock-guarded allocation | Full |
| 3 | "Two sessions running in parallel cannot create REQs or URs with colliding IDs. Uniqueness is structural, not best-effort" (Done-criteria #1) | Detailed Requirements — Lock-guarded allocation, Scan authoritative locations | Full |
| 4 | "Capture holds a short lock on ID allocation" (Resolved decision — Concurrency model) | Detailed Requirements — Minimal lock scope (Constraints); Lock-guarded allocation | Full |

*Verified by verify-request action*

## Exploration

**Not needed** — the REQ already names the exact touch points: capture flow in `actions/do.md`, shared primitives in `lib/concurrency.py`, and concurrency coverage in `lib/concurrency_test.py`. No extra discovery pass was required beyond reading the existing library and the capture contract.

*Skipped by work action*

## Implementation Summary

### Files created

None.

### Files modified

- `lib/concurrency.py` — added namespace-specific ID allocation scopes plus `allocate_ur_input(...)` / `allocate_req_file(...)` to mint IDs under short locks, scan authoritative locations, write placeholders before release, and fail loudly on collisions.
- `lib/concurrency_test.py` — added allocator coverage for UR/REQ scan sources, write-failure cleanup, cross-namespace lock independence, and concurrent REQ allocation producing unique IDs.
- `actions/concurrency-primitives.md` — documented the new allocator APIs, lock scopes, scan coverage, and test expectations.
- `actions/do.md` — replaced manual "scan highest ID" guidance with mandatory allocator calls in both simple and complex capture flows.

### Implementation notes

- REQ and UR allocation now use separate lock scopes: `id-allocation:req` and `id-allocation:ur`.
- The allocator creates the actual placeholder file under the lock, not just the number, so a second capture cannot re-derive the same ID from an empty directory.
- Contention on a just-created lockfile now tolerates the tiny window before the holder finishes writing JSON into the lockfile; contenders retry lock inspection instead of failing on a transient partial read.
- Conflict detection is loud by design: if the computed "next" identifier already exists anywhere authoritative, the allocator raises with the conflicting path.

### Deviations from plan

None. The implementation stayed inside capture docs plus the shared concurrency runtime/tests.

*Completed by work action (Route C)*

## Testing

**Command run:**

```bash
python3 -m unittest lib.concurrency_test -v
```

**Result:** Pass (23/23 tests green)

**Coverage exercised by the suite:**

- Existing lock primitive behavior: acquire/release/inspect, holder details on contention, heartbeat refresh, foreign-release protection, stale/orphan classification.
- Atomic filesystem primitives: rename success/failure modes and atomic-write crash cleanup.
- REQ-003 allocator behavior: authoritative scan coverage for REQ and UR namespaces, loud next-ID conflict detection, cleanup when UR placeholder write fails, and separate REQ/UR lock namespaces.
- Concurrency behavior: 20 spawned processes race for one lock and exactly one wins; parallel REQ allocations still serialize into unique IDs.

*Verified by work action*
