import errno
import json
import multiprocessing
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest import mock

from lib.concurrency import (
    abort_capture_transaction,
    AtomicWriteError,
    begin_capture_transaction,
    CleanupClaimHandle,
    CleanupClaimRecord,
    commit_capture_transaction,
    ConcurrencyError,
    acquire_verification_lock,
    allocate_staged_req_file,
    allocate_staged_ur_input,
    archive_completed_request,
    archive_legacy_context_if_complete,
    archive_user_request_if_complete,
    ClaimHandle,
    ClaimTreeState,
    ClaimHeldError,
    ClaimFileFingerprint,
    ClaimRecord,
    ClaimFormatError,
    CollisionError,
    CrossDeviceError,
    ForeignReleaseError,
    LockHandle,
    LockHeldError,
    LockInfo,
    RecoveryNotAllowedError,
    ScopedStageResult,
    ScopeError,
    SessionFormatError,
    SessionRecord,
    StaleRenameError,
    TreeStateViolationError,
    acquire_lock,
    allocate_req_file,
    allocate_ur_input,
    atomic_rename,
    atomic_write,
    capture_claim_tree_state,
    claim_work_request,
    claim_cleanup,
    classify_lock,
    inspect_session_record,
    inspect_work_claim_recovery,
    inspect_lock,
    read_claim,
    read_cleanup_claim,
    read_session_record,
    recover_orphaned_work_claim,
    refresh_cleanup_claim_heartbeat,
    refresh_claim_heartbeat,
    release_cleanup,
    release_claim,
    release_capture_transaction,
    refresh_heartbeat,
    release_lock,
    replace_markdown_section,
    repair_capture_state,
    SessionClaimConflictError,
    verify_and_stage_claim_scope,
    verification_lock_path,
    write_session_record,
    write_claim,
    write_cleanup_claim,
    rewrite_markdown_section_atomic,
)


def _race_worker(lock_path: str, start_event, release_event, result_queue) -> None:
    start_event.wait()
    try:
        handle = acquire_lock(
            lock_path,
            session_id=f"session-{os.getpid()}",
            operation="race",
            scope="cleanup-global",
        )
    except LockHeldError as exc:
        result_queue.put(("held", str(exc)))
        return

    result_queue.put(("acquired", handle.info.session_id))
    release_event.wait(timeout=2)
    release_lock(handle)


def _work_claim_race_worker(
    do_work_root: str,
    request_path: str,
    start_event,
    result_queue,
) -> None:
    start_event.wait()
    session_id = f"session-{os.getpid()}"
    try:
        handle = claim_work_request(
            do_work_root,
            request_path=request_path,
            session_id=session_id,
            operation="work",
        )
    except ClaimHeldError as exc:
        result_queue.put(("held", session_id, str(exc)))
        return

    result_queue.put(("acquired", session_id, handle.claim_path))


def _ur_archival_race_worker(
    do_work_root: str,
    ur_id: str,
    start_event,
    result_queue,
) -> None:
    start_event.wait()
    session_id = f"session-{os.getpid()}"
    try:
        result = archive_user_request_if_complete(
            do_work_root,
            ur_id=ur_id,
            session_id=session_id,
            operation="work",
        )
    except Exception as exc:
        result_queue.put(("error", session_id, type(exc).__name__, str(exc)))
        return

    result_queue.put(("ok", session_id, result.outcome, result.archive_path))


def _cleanup_claim_race_worker(
    do_work_root: str,
    start_event,
    release_event,
    result_queue,
) -> None:
    start_event.wait()
    session_id = f"session-{os.getpid()}"
    try:
        handle = claim_cleanup(
            do_work_root,
            session_id=session_id,
            operation="cleanup",
        )
    except LockHeldError as exc:
        result_queue.put(("held", session_id, str(exc)))
        return
    except Exception as exc:
        result_queue.put(("error", session_id, type(exc).__name__, str(exc)))
        return

    result_queue.put(("acquired", session_id, handle.path))
    release_event.wait(timeout=2)
    release_cleanup(handle)


def _verify_request_race_worker(
    do_work_root: str,
    target_path: str,
    start_event,
    release_event,
    result_queue,
) -> None:
    start_event.wait()
    session_id = f"session-{os.getpid()}"
    try:
        handle = acquire_verification_lock(
            do_work_root,
            target_path=target_path,
            session_id=session_id,
            operation="verify-request",
        )
    except LockHeldError as exc:
        result_queue.put(("held", session_id, str(exc)))
        return

    try:
        result_queue.put(("acquired", session_id, handle.path))
        release_event.wait(timeout=2)
        rewrite_markdown_section_atomic(
            target_path,
            heading="Verification",
            new_section=(
                "## Verification\n\n"
                f"winner: {session_id}\n\n"
                "*Verified by verify-request action*\n"
            ),
        )
    finally:
        release_lock(handle)


class ConcurrencyPrimitivesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def _init_git_repo(self) -> None:
        subprocess.run(
            ["git", "init"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()

    def _req_frontmatter(
        self,
        req_id: str,
        *,
        title: str = "Example request",
        status: str = "completed",
        user_request: Optional[str] = None,
        context_ref: Optional[str] = None,
    ) -> str:
        lines = [
            "---",
            f"id: {req_id}",
            f"title: {title}",
            f"status: {status}",
            "created_at: 2026-04-18T00:00:00Z",
        ]
        if user_request is not None:
            lines.append(f"user_request: {user_request}")
        if context_ref is not None:
            lines.append(f"context_ref: {context_ref}")
        lines.extend(["---", "", f"# {title}", ""])
        return "\n".join(lines)

    def _ur_input(self, ur_id: str, requests: list[str]) -> str:
        return "\n".join(
            [
                "---",
                f"id: {ur_id}",
                "title: Batch",
                "created_at: 2026-04-18T00:00:00Z",
                f"requests: [{', '.join(requests)}]",
                "---",
                "",
                "# Batch",
                "",
            ]
        )

    def _context_input(self, requests: list[str]) -> str:
        return "\n".join(
            [
                "---",
                "id: CONTEXT-001",
                "title: Legacy batch",
                f"requests: [{', '.join(requests)}]",
                "---",
                "",
                "# Legacy batch",
                "",
            ]
        )

    def test_acquire_release_and_inspect_lock(self) -> None:
        path = self.root / "locks" / "cleanup.lock"
        handle = acquire_lock(
            path,
            session_id="session-1",
            operation="work",
            scope="cleanup-global",
        )

        self.assertTrue(path.exists())
        info = inspect_lock(path)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.session_id, "session-1")
        self.assertEqual(info.operation, "work")
        self.assertEqual(info.scope, "cleanup-global")
        self.assertEqual(handle.info.session_id, "session-1")

        release_lock(handle)
        self.assertIsNone(inspect_lock(path))

    def test_second_acquire_raises_lockhelderror_with_holder_details(self) -> None:
        path = self.root / "locks" / "req.lock"
        first = acquire_lock(
            path,
            session_id="holder-session",
            operation="work",
            scope="req-claim:REQ-002",
        )
        self.addCleanup(release_lock, first)

        with self.assertRaises(LockHeldError) as ctx:
            acquire_lock(
                path,
                session_id="attempting-session",
                operation="verify",
                scope="req-claim:REQ-002",
            )

        msg = str(ctx.exception)
        self.assertIn("holder-session", msg)
        self.assertIn("operation: work", msg)
        self.assertIn("attempting-session", msg)
        self.assertIn("operation: verify", msg)

    def test_inspect_lock_missing_returns_none(self) -> None:
        self.assertIsNone(inspect_lock(self.root / "missing.lock"))

    def test_refresh_heartbeat_updates_lockfile(self) -> None:
        path = self.root / "locks" / "heartbeat.lock"
        initial = datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc)
        updated = initial + timedelta(seconds=45)
        handle = acquire_lock(
            path,
            session_id="session-1",
            operation="work",
            scope="cleanup-global",
            now=initial,
        )

        refresh_heartbeat(handle, now=updated)
        info = inspect_lock(path)
        assert info is not None
        self.assertEqual(info.last_heartbeat, "2026-04-18T00:00:45Z")
        self.assertEqual(handle.info.last_heartbeat, "2026-04-18T00:00:45Z")

    def test_verification_lock_path_uses_predictable_req_name(self) -> None:
        do_work_root = self.root / "do-work"
        lock_path = verification_lock_path(
            do_work_root,
            target_path=do_work_root / "REQ-007-verify-document-locks.md",
        )

        self.assertEqual(
            lock_path,
            do_work_root / ".locks" / "verify-REQ-007.lock",
        )

    def test_replace_markdown_section_appends_or_replaces_by_heading(self) -> None:
        original = (
            "# Request\n\n"
            "## Plan\n\n"
            "Plan text.\n\n"
            "## Verification\n\n"
            "Old verification.\n"
        )

        updated = replace_markdown_section(
            original,
            heading="Verification",
            new_section=(
                "## Verification\n\n"
                "New verification.\n"
            ),
        )

        self.assertIn("New verification.", updated)
        self.assertNotIn("Old verification.", updated)
        self.assertEqual(updated.count("## Verification"), 1)

    def test_release_lock_rejects_foreign_handle(self) -> None:
        path = self.root / "locks" / "foreign.lock"
        first = acquire_lock(
            path,
            session_id="session-1",
            operation="work",
            scope="cleanup-global",
        )
        fake_handle = LockHandle(path=first.path, info=LockInfo(
            session_id="session-2",
            operation=first.info.operation,
            scope=first.info.scope,
            acquired_at=first.info.acquired_at,
            last_heartbeat=first.info.last_heartbeat,
            pid=first.info.pid,
            hostname=first.info.hostname,
        ))

        with self.assertRaises(ForeignReleaseError):
            release_lock(fake_handle)

        release_lock(first)

    def test_classify_lock_live(self) -> None:
        handle = acquire_lock(
            self.root / "locks" / "live.lock",
            session_id="session-1",
            operation="work",
            scope="cleanup-global",
            now=datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc),
        )
        verdict = classify_lock(
            handle.info,
            now=datetime(2026, 4, 18, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(verdict, "live")
        release_lock(handle)

    def test_classify_lock_stale_when_pid_alive(self) -> None:
        stale_info = acquire_lock(
            self.root / "locks" / "stale.lock",
            session_id="session-1",
            operation="work",
            scope="cleanup-global",
            now=datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc),
        ).info
        verdict = classify_lock(
            stale_info,
            now=datetime(2026, 4, 18, 0, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(verdict, "stale")
        release_lock(self.root / "locks" / "stale.lock")

    def test_classify_lock_orphaned_when_pid_missing(self) -> None:
        handle = acquire_lock(
            self.root / "locks" / "orphan.lock",
            session_id="session-1",
            operation="work",
            scope="cleanup-global",
            now=datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc),
        )
        info = type(handle.info)(
            session_id=handle.info.session_id,
            operation=handle.info.operation,
            scope=handle.info.scope,
            acquired_at=handle.info.acquired_at,
            last_heartbeat=handle.info.last_heartbeat,
            pid=999999,
            hostname=handle.info.hostname,
        )

        with mock.patch("lib.concurrency._pid_alive", return_value=False):
            verdict = classify_lock(
                info,
                now=datetime(2026, 4, 18, 0, 3, tzinfo=timezone.utc),
            )

        self.assertEqual(verdict, "orphaned")
        release_lock(handle)

    def test_atomic_rename_success(self) -> None:
        src = self.root / "queue.md"
        dst = self.root / "working.md"
        src.write_text("payload", encoding="utf-8")

        atomic_rename(src, dst, transition="queue->working")
        self.assertFalse(src.exists())
        self.assertEqual(dst.read_text(encoding="utf-8"), "payload")

    def test_atomic_rename_rejects_missing_source(self) -> None:
        with self.assertRaises(StaleRenameError):
            atomic_rename(
                self.root / "missing.md",
                self.root / "dst.md",
                transition="queue->working",
            )

    def test_atomic_rename_rejects_existing_destination(self) -> None:
        src = self.root / "src.md"
        dst = self.root / "dst.md"
        src.write_text("src", encoding="utf-8")
        dst.write_text("dst", encoding="utf-8")

        with self.assertRaises(CollisionError):
            atomic_rename(src, dst, transition="queue->working")

    def test_atomic_rename_rejects_cross_device(self) -> None:
        src = self.root / "src.md"
        dst = self.root / "dst.md"
        src.write_text("src", encoding="utf-8")

        with mock.patch(
            "lib.concurrency.os.rename",
            side_effect=OSError(errno.EXDEV, "cross-device"),
        ):
            with self.assertRaises(CrossDeviceError):
                atomic_rename(src, dst, transition="queue->working")

    def test_atomic_write_crash_safety(self) -> None:
        target = self.root / "request.json"

        with mock.patch(
            "lib.concurrency.os.rename",
            side_effect=OSError(errno.EIO, "disk I/O error"),
        ):
            with self.assertRaises(AtomicWriteError):
                atomic_write(target, '{"ok": true}\n')

        self.assertFalse(target.exists())
        leftovers = list(self.root.glob("request.json.*.tmp.*"))
        self.assertEqual(leftovers, [])

    def test_allocate_ur_input_scans_active_and_archived_ur_locations(self) -> None:
        (self.root / "user-requests" / "UR-003").mkdir(parents=True, exist_ok=True)
        (self.root / "archive" / "UR-009").mkdir(parents=True, exist_ok=True)

        allocation = allocate_ur_input(
            self.root,
            session_id="session-1",
            operation="do",
            content="ur placeholder\n",
        )

        self.assertEqual(allocation.identifier, "UR-010")
        self.assertEqual(Path(allocation.path), self.root / "user-requests" / "UR-010" / "input.md")
        self.assertEqual(
            Path(allocation.path).read_text(encoding="utf-8"),
            "ur placeholder\n",
        )
        self.assertFalse(Path(allocation.lock_path).exists())

    def test_allocate_req_file_scans_queue_working_archive_and_archived_ur_locations(self) -> None:
        (self.root / "REQ-002-queued.md").write_text("queued\n", encoding="utf-8")
        (self.root / "working").mkdir(parents=True, exist_ok=True)
        (self.root / "working" / "REQ-007-working.md").write_text("working\n", encoding="utf-8")
        (self.root / "archive").mkdir(parents=True, exist_ok=True)
        (self.root / "archive" / "REQ-009-archived.md").write_text("archive\n", encoding="utf-8")
        (self.root / "archive" / "UR-011").mkdir(parents=True, exist_ok=True)
        (self.root / "archive" / "UR-011" / "REQ-012-inside-ur.md").write_text(
            "nested\n",
            encoding="utf-8",
        )

        allocation = allocate_req_file(
            self.root,
            session_id="session-1",
            operation="do",
            slug="new-request",
            content="req placeholder\n",
        )

        self.assertEqual(allocation.identifier, "REQ-013")
        self.assertEqual(
            Path(allocation.path),
            self.root / "REQ-013-new-request.md",
        )
        self.assertEqual(
            Path(allocation.path).read_text(encoding="utf-8"),
            "req placeholder\n",
        )
        self.assertFalse(Path(allocation.lock_path).exists())

    def test_allocate_ur_input_cleans_up_directory_on_write_failure(self) -> None:
        with mock.patch(
            "lib.concurrency.atomic_write",
            side_effect=AtomicWriteError("disk full"),
        ):
            with self.assertRaises(AtomicWriteError):
                allocate_ur_input(
                    self.root,
                    session_id="session-1",
                    operation="do",
                    content="ur placeholder\n",
                )

        self.assertFalse((self.root / "user-requests" / "UR-001").exists())

    def test_allocate_req_file_fails_loud_on_conflicting_next_id_path(self) -> None:
        conflict = self.root / "archive" / "REQ-004-existing.md"
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text("existing\n", encoding="utf-8")

        with mock.patch("lib.concurrency._next_identifier_number", return_value=4):
            with self.assertRaises(CollisionError) as ctx:
                allocate_req_file(
                    self.root,
                    session_id="session-1",
                    operation="do",
                    slug="new-request",
                    content="req placeholder\n",
                )

        self.assertIn("REQ-004", str(ctx.exception))
        self.assertIn(str(conflict), str(ctx.exception))

    def test_allocate_ur_input_uses_a_different_lock_namespace_than_req_allocation(self) -> None:
        req_lock = acquire_lock(
            self.root / ".locks" / "id-allocation-req.lock",
            session_id="session-1",
            operation="do",
            scope="id-allocation:req",
        )
        self.addCleanup(release_lock, req_lock)

        allocation = allocate_ur_input(
            self.root,
            session_id="session-2",
            operation="do",
            content="ur placeholder\n",
        )

        self.assertEqual(allocation.identifier, "UR-001")
        self.assertTrue((self.root / ".locks" / "id-allocation-req.lock").exists())

    def test_parallel_req_allocations_produce_unique_ids(self) -> None:
        worker_count = 4
        barrier = threading.Barrier(worker_count)
        results: list[tuple[str, str, str]] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                allocation = allocate_req_file(
                    self.root,
                    session_id=f"session-{index}",
                    operation="do",
                    slug="parallel-capture",
                    content="placeholder\n",
                )
                with result_lock:
                    results.append(("ok", allocation.identifier, allocation.path))
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,), daemon=True)
            for index in range(worker_count)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])

        successes = [result for result in results if result[0] == "ok"]
        self.assertEqual(len(successes), worker_count)
        identifiers = {result[1] for result in successes}
        self.assertEqual(len(identifiers), worker_count)
        self.assertEqual(
            sorted(identifiers),
            [f"REQ-{n:03d}" for n in range(1, worker_count + 1)],
        )
        for _, _, path in successes:
            self.assertTrue(Path(path).exists())

    def test_capture_transaction_stages_then_commits_to_final_locations(self) -> None:
        do_work_root = self.root / "do-work"
        transaction = begin_capture_transaction(
            do_work_root,
            session_id="session-1",
            operation="do",
            now=datetime(2026, 4, 20, 23, 0, tzinfo=timezone.utc),
        )
        self.addCleanup(release_capture_transaction, transaction)

        ur = allocate_staged_ur_input(
            transaction,
            do_work_root=do_work_root,
            content=(
                "---\n"
                "id: UR-001\n"
                "title: Example\n"
                "created_at: 2026-04-20T23:00:00Z\n"
                "requests: []\n"
                "word_count: 2\n"
                "---\n\n"
                "# Example\n\n"
                "## Full Verbatim Input\n\n"
                "ship it\n"
            ),
            now=datetime(2026, 4, 20, 23, 0, tzinfo=timezone.utc),
        )
        req = allocate_staged_req_file(
            transaction,
            do_work_root=do_work_root,
            slug="ship-it",
            content=(
                "---\n"
                "id: REQ-001\n"
                "title: Ship it\n"
                "status: pending\n"
                "created_at: 2026-04-20T23:00:00Z\n"
                "user_request: UR-001\n"
                "---\n\n"
                "# Ship it\n\n"
                "## What\n\n"
                "Ship it.\n\n"
                "## Verification\n\n"
                "*Verified by verify-request action*\n"
            ),
            now=datetime(2026, 4, 20, 23, 0, 1, tzinfo=timezone.utc),
        )

        atomic_write(
            ur.path,
            (
                "---\n"
                "id: UR-001\n"
                "title: Example\n"
                "created_at: 2026-04-20T23:00:00Z\n"
                "requests: [REQ-001]\n"
                "word_count: 2\n"
                "---\n\n"
                "# Example\n\n"
                "## Full Verbatim Input\n\n"
                "ship it\n"
            ),
        )

        manifest = commit_capture_transaction(
            transaction,
            now=datetime(2026, 4, 20, 23, 0, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(manifest.status, "committed")
        self.assertTrue((do_work_root / "user-requests" / ur.identifier / "input.md").exists())
        self.assertTrue((do_work_root / f"{req.identifier}-ship-it.md").exists())
        self.assertFalse(Path(req.path).exists())
        self.assertFalse(Path(ur.path).exists())

    def test_capture_start_fails_loud_when_failed_stage_exists(self) -> None:
        do_work_root = self.root / "do-work"
        transaction = begin_capture_transaction(
            do_work_root,
            session_id="session-1",
            operation="do",
            now=datetime(2026, 4, 20, 23, 5, tzinfo=timezone.utc),
        )
        ur = allocate_staged_ur_input(
            transaction,
            do_work_root=do_work_root,
            content="---\nid: UR-001\ntitle: Draft\ncreated_at: 2026-04-20T23:05:00Z\nrequests: []\nword_count: 1\n---\n",
            now=datetime(2026, 4, 20, 23, 5, tzinfo=timezone.utc),
        )
        abort_capture_transaction(
            transaction,
            reason="simulated failure",
            now=datetime(2026, 4, 20, 23, 5, 1, tzinfo=timezone.utc),
            preserve_draft=True,
        )
        release_capture_transaction(transaction)

        with self.assertRaises(ConcurrencyError) as ctx:
            begin_capture_transaction(
                do_work_root,
                session_id="session-2",
                operation="do",
                now=datetime(2026, 4, 20, 23, 6, tzinfo=timezone.utc),
            )

        self.assertIn("repair_capture_state", str(ctx.exception))
        self.assertIn("CAP-", str(ctx.exception))
        self.assertTrue((do_work_root / ".capture-staging").exists())
        self.assertTrue(Path(ur.path).exists())

    def test_abort_refuses_when_status_is_committing_regardless_of_preserve_draft(self) -> None:
        """Regression: abort_capture_transaction must not mark a committing
        transaction as failed, because commit may have already published
        some items — marking failed would split-brain published files
        against a failed manifest. Raise regardless of preserve_draft."""
        do_work_root = self.root / "do-work"
        transaction = begin_capture_transaction(
            do_work_root,
            session_id="session-1",
            operation="do",
            now=datetime(2026, 4, 20, 23, 20, tzinfo=timezone.utc),
        )
        allocate_staged_ur_input(
            transaction,
            do_work_root=do_work_root,
            content="---\nid: UR-001\ntitle: Draft\ncreated_at: 2026-04-20T23:20:00Z\nrequests: []\nword_count: 1\n---\n",
            now=datetime(2026, 4, 20, 23, 20, tzinfo=timezone.utc),
        )

        # Simulate a commit in flight by flipping the on-disk manifest to
        # status="committing". This is the exact state that would exist if
        # commit_capture_transaction had started publishing items and was
        # interrupted mid-way.
        with open(transaction.manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["status"] = "committing"
        with open(transaction.manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        for preserve in (True, False):
            with self.assertRaises(ConcurrencyError) as ctx:
                abort_capture_transaction(
                    transaction,
                    reason=f"racing abort with preserve_draft={preserve}",
                    now=datetime(2026, 4, 20, 23, 20, 1, tzinfo=timezone.utc),
                    preserve_draft=preserve,
                )
            self.assertIn("already committing", str(ctx.exception))
            self.assertIn("repair_capture_state", str(ctx.exception))

    def test_repair_capture_state_discards_failed_stage_and_releases_reserved_ids(self) -> None:
        do_work_root = self.root / "do-work"
        transaction = begin_capture_transaction(
            do_work_root,
            session_id="session-1",
            operation="do",
            now=datetime(2026, 4, 20, 23, 10, tzinfo=timezone.utc),
        )
        ur = allocate_staged_ur_input(
            transaction,
            do_work_root=do_work_root,
            content="---\nid: UR-001\ntitle: Draft\ncreated_at: 2026-04-20T23:10:00Z\nrequests: []\nword_count: 1\n---\n",
            now=datetime(2026, 4, 20, 23, 10, tzinfo=timezone.utc),
        )
        req = allocate_staged_req_file(
            transaction,
            do_work_root=do_work_root,
            slug="draft",
            content="---\nid: REQ-001\ntitle: Draft\nstatus: pending\ncreated_at: 2026-04-20T23:10:00Z\nuser_request: UR-001\n---\n",
            now=datetime(2026, 4, 20, 23, 10, 1, tzinfo=timezone.utc),
        )
        abort_capture_transaction(
            transaction,
            reason="simulated failure",
            now=datetime(2026, 4, 20, 23, 10, 2, tzinfo=timezone.utc),
            preserve_draft=True,
        )
        release_capture_transaction(transaction)

        repair = repair_capture_state(
            do_work_root,
            session_id="session-2",
            operation="do",
            now=datetime(2026, 4, 20, 23, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(repair.outcome, "discarded-draft")
        self.assertFalse((do_work_root / ".capture-staging").exists())

        replacement_ur = allocate_ur_input(
            do_work_root,
            session_id="session-3",
            operation="do",
            content="fresh\n",
        )
        replacement_req = allocate_req_file(
            do_work_root,
            session_id="session-3",
            operation="do",
            slug="fresh",
            content="fresh\n",
        )
        self.assertEqual(replacement_ur.identifier, ur.identifier)
        self.assertEqual(replacement_req.identifier, req.identifier)

    def test_repair_capture_state_resumes_commit_after_crash_mid_publish(self) -> None:
        do_work_root = self.root / "do-work"
        transaction = begin_capture_transaction(
            do_work_root,
            session_id="session-1",
            operation="do",
            now=datetime(2026, 4, 20, 23, 20, tzinfo=timezone.utc),
        )
        self.addCleanup(
            lambda: release_capture_transaction(transaction)
            if Path(transaction.lock.path).exists()
            else None
        )
        ur = allocate_staged_ur_input(
            transaction,
            do_work_root=do_work_root,
            content=(
                "---\n"
                "id: UR-001\n"
                "title: Crashy\n"
                "created_at: 2026-04-20T23:20:00Z\n"
                "requests: []\n"
                "word_count: 2\n"
                "---\n\n"
                "# Crashy\n\n"
                "## Full Verbatim Input\n\n"
                "crash now\n"
            ),
            now=datetime(2026, 4, 20, 23, 20, tzinfo=timezone.utc),
        )
        req = allocate_staged_req_file(
            transaction,
            do_work_root=do_work_root,
            slug="crashy",
            content=(
                "---\n"
                "id: REQ-001\n"
                "title: Crashy\n"
                "status: pending\n"
                "created_at: 2026-04-20T23:20:00Z\n"
                "user_request: UR-001\n"
                "---\n\n"
                "# Crashy\n\n"
                "## What\n\n"
                "Crash now.\n\n"
                "## Verification\n\n"
                "*Verified by verify-request action*\n"
            ),
            now=datetime(2026, 4, 20, 23, 20, 1, tzinfo=timezone.utc),
        )
        atomic_write(
            ur.path,
            (
                "---\n"
                "id: UR-001\n"
                "title: Crashy\n"
                "created_at: 2026-04-20T23:20:00Z\n"
                "requests: [REQ-001]\n"
                "word_count: 2\n"
                "---\n\n"
                "# Crashy\n\n"
                "## Full Verbatim Input\n\n"
                "crash now\n"
            ),
        )

        real_atomic_rename = atomic_rename
        rename_calls = {"count": 0}

        def crash_after_first_publish(src, dst, *, transition):
            rename_calls["count"] += 1
            real_atomic_rename(src, dst, transition=transition)
            if rename_calls["count"] == 1:
                raise RuntimeError("simulated crash after publish")

        with mock.patch(
            "lib.concurrency.atomic_rename",
            side_effect=crash_after_first_publish,
        ):
            with self.assertRaises(RuntimeError):
                commit_capture_transaction(
                    transaction,
                    now=datetime(2026, 4, 20, 23, 20, 2, tzinfo=timezone.utc),
                )

        release_capture_transaction(transaction)

        repair = repair_capture_state(
            do_work_root,
            session_id="session-2",
            operation="do",
            now=datetime(2026, 4, 20, 23, 21, tzinfo=timezone.utc),
        )

        self.assertEqual(repair.outcome, "resumed-commit")
        self.assertTrue((do_work_root / "user-requests" / ur.identifier / "input.md").exists())
        self.assertTrue((do_work_root / f"{req.identifier}-crashy.md").exists())
        self.assertFalse((do_work_root / ".capture-staging").exists())

    def test_claim_round_trip(self) -> None:
        claim = ClaimRecord(
            claim_id="REQ-002",
            session_id="session-1",
            operation="work",
            scope="req-claim:REQ-002",
            affected_paths=("do-work/working/REQ-002.md",),
            acquired_at="2026-04-18T00:00:00Z",
            last_heartbeat="2026-04-18T00:00:30Z",
        )
        path = self.root / "claims" / "req-002.json"

        write_claim(path, claim)
        loaded = read_claim(path)
        self.assertEqual(loaded, claim)

    def test_claim_cleanup_writes_cleanup_claim_and_lock(self) -> None:
        do_work_root = self.root / "do-work"
        handle = claim_cleanup(
            do_work_root,
            session_id="session-1",
            operation="cleanup",
            now=datetime(2026, 4, 20, 22, 10, tzinfo=timezone.utc),
        )

        claim_path = do_work_root / ".claims" / "cleanup.claim.json"
        lock_path = do_work_root / ".locks" / "cleanup-global.lock"
        self.assertEqual(handle.path, str(claim_path))
        self.assertTrue(claim_path.exists())
        self.assertTrue(lock_path.exists())

        claim = read_cleanup_claim(claim_path)
        self.assertEqual(
            claim,
            CleanupClaimRecord(
                session_id="session-1",
                started_at="2026-04-20T22:10:00Z",
                last_heartbeat="2026-04-20T22:10:00Z",
                operation="cleanup",
            ),
        )
        release_cleanup(handle)
        self.assertFalse(claim_path.exists())
        self.assertFalse(lock_path.exists())

    def test_refresh_cleanup_claim_heartbeat_updates_claimfile(self) -> None:
        path = self.root / "do-work" / ".claims" / "cleanup.claim.json"
        claim = CleanupClaimRecord(
            session_id="session-1",
            started_at="2026-04-20T22:00:00Z",
            last_heartbeat="2026-04-20T22:00:00Z",
            operation="cleanup",
        )
        handle = CleanupClaimHandle(
            lock=LockHandle(
                path=str(self.root / "do-work" / ".locks" / "cleanup-global.lock"),
                info=LockInfo(
                    session_id="session-1",
                    operation="cleanup",
                    scope="cleanup-global",
                    acquired_at="2026-04-20T22:00:00Z",
                    last_heartbeat="2026-04-20T22:00:00Z",
                    pid=1234,
                    hostname="test-host",
                ),
            ),
            path=str(path),
            claim=claim,
        )

        write_cleanup_claim(path, claim)
        refresh_cleanup_claim_heartbeat(
            handle,
            now=datetime(2026, 4, 20, 22, 0, 45, tzinfo=timezone.utc),
        )

        loaded = read_cleanup_claim(path)
        self.assertEqual(loaded.last_heartbeat, "2026-04-20T22:00:45Z")
        self.assertEqual(handle.claim.last_heartbeat, "2026-04-20T22:00:45Z")

    def test_claim_cleanup_fails_loud_if_claim_file_already_exists(self) -> None:
        do_work_root = self.root / "do-work"
        claim_path = do_work_root / ".claims" / "cleanup.claim.json"
        write_cleanup_claim(
            claim_path,
            CleanupClaimRecord(
                session_id="session-dead",
                started_at="2026-04-20T21:00:00Z",
                last_heartbeat="2026-04-20T21:30:00Z",
                operation="cleanup",
            ),
        )

        with self.assertRaises(ConcurrencyError) as ctx:
            claim_cleanup(
                do_work_root,
                session_id="session-1",
                operation="cleanup",
            )

        message = str(ctx.exception)
        self.assertIn("session-dead", message)
        self.assertIn("REQ-005 evidence path", message)
        self.assertFalse((do_work_root / ".locks" / "cleanup-global.lock").exists())

    def test_parallel_cleanup_claims_produce_one_winner_and_named_holder(self) -> None:
        do_work_root = self.root / "do-work"
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        release_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_cleanup_claim_race_worker,
                args=(str(do_work_root), start_event, release_event, result_queue),
            )
            for _ in range(2)
        ]

        for process in processes:
            process.start()

        start_event.set()
        results = [result_queue.get(timeout=5) for _ in range(2)]
        release_event.set()

        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        acquired = [result for result in results if result[0] == "acquired"]
        held = [result for result in results if result[0] == "held"]
        errors = [result for result in results if result[0] == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(held), 1)

        winner_session = acquired[0][1]
        loser_message = held[0][2]
        self.assertIn(winner_session, loser_message)
        self.assertIn("cleanup-global", loser_message)
        self.assertFalse((do_work_root / ".claims" / "cleanup.claim.json").exists())
        self.assertFalse((do_work_root / ".locks" / "cleanup-global.lock").exists())

    def test_refresh_claim_heartbeat_updates_claimfile(self) -> None:
        claim = ClaimRecord(
            claim_id="REQ-002",
            session_id="session-1",
            operation="work",
            scope="req-claim:REQ-002",
            affected_paths=("do-work/working/REQ-002.md",),
            acquired_at="2026-04-18T00:00:00Z",
            last_heartbeat="2026-04-18T00:00:00Z",
        )
        path = self.root / "working" / "REQ-002.claim.json"
        handle = ClaimHandle(path=str(path), claim=claim)

        write_claim(path, claim)
        refresh_claim_heartbeat(
            handle,
            now=datetime(2026, 4, 18, 0, 0, 45, tzinfo=timezone.utc),
        )

        loaded = read_claim(path)
        self.assertEqual(loaded.last_heartbeat, "2026-04-18T00:00:45Z")
        self.assertEqual(handle.claim.last_heartbeat, "2026-04-18T00:00:45Z")

    def test_release_claim_rejects_foreign_session(self) -> None:
        claim = ClaimRecord(
            claim_id="REQ-002",
            session_id="holder-session",
            operation="work",
            scope="req-claim:REQ-002",
            affected_paths=("do-work/working/REQ-002.md",),
            acquired_at="2026-04-18T00:00:00Z",
            last_heartbeat="2026-04-18T00:00:30Z",
        )
        path = self.root / "working" / "REQ-002.claim.json"

        write_claim(path, claim)
        with self.assertRaises(ForeignReleaseError):
            release_claim(path, expected_session_id="other-session")

        self.assertTrue(path.exists())
        release_claim(path, expected_session_id="holder-session")
        self.assertFalse(path.exists())

    def test_session_record_round_trip(self) -> None:
        record = SessionRecord(
            session_id="session-1",
            hostname="test-host",
            pid=1234,
            started_at="2026-04-20T20:00:00Z",
            last_heartbeat="2026-04-20T20:00:30Z",
            operation="work",
        )
        path = self.root / "do-work" / ".sessions" / "session-1.json"

        write_session_record(path, record)
        loaded = read_session_record(path)
        self.assertEqual(loaded, record)
        inspected = inspect_session_record(self.root / "do-work", "session-1")
        self.assertEqual(inspected, record)

    def test_inspect_work_claim_recovery_reports_missing_session_record(self) -> None:
        do_work_root = self.root / "do-work"
        request_path = do_work_root / "REQ-005-orphan.md"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("queued\n", encoding="utf-8")
        claim_time = datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc)
        handle = claim_work_request(
            do_work_root,
            request_path=request_path,
            session_id="session-ghost",
            operation="work",
            now=claim_time,
        )

        inspection = inspect_work_claim_recovery(
            do_work_root,
            claim_path=handle.claim_path,
            now=claim_time + timedelta(minutes=5),
        )

        self.assertEqual(inspection.verdict, "missing-session-record")
        self.assertIn("missing", inspection.reason)

    def test_inspect_work_claim_recovery_reports_foreign_host(self) -> None:
        do_work_root = self.root / "do-work"
        request_path = do_work_root / "REQ-005-foreign.md"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("queued\n", encoding="utf-8")
        claim_time = datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc)
        handle = claim_work_request(
            do_work_root,
            request_path=request_path,
            session_id="session-foreign",
            operation="work",
            now=claim_time,
        )
        write_session_record(
            do_work_root / ".sessions" / "session-foreign.json",
            SessionRecord(
                session_id="session-foreign",
                hostname="other-host",
                pid=999999,
                started_at="2026-04-20T20:00:00Z",
                last_heartbeat="2026-04-20T20:00:00Z",
                operation="work",
            ),
        )

        with mock.patch("lib.concurrency.socket.gethostname", return_value="local-host"):
            inspection = inspect_work_claim_recovery(
                do_work_root,
                claim_path=handle.claim_path,
                now=claim_time + timedelta(minutes=5),
            )

        self.assertEqual(inspection.verdict, "foreign-host")
        self.assertIn("other-host", inspection.reason)

    def test_inspect_work_claim_recovery_reports_stale_when_process_still_exists(self) -> None:
        do_work_root = self.root / "do-work"
        request_path = do_work_root / "REQ-005-stale.md"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("queued\n", encoding="utf-8")
        claim_time = datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc)
        handle = claim_work_request(
            do_work_root,
            request_path=request_path,
            session_id="session-stale",
            operation="work",
            now=claim_time,
        )
        write_session_record(
            do_work_root / ".sessions" / "session-stale.json",
            SessionRecord(
                session_id="session-stale",
                hostname="local-host",
                pid=1234,
                started_at="2026-04-20T20:00:00Z",
                last_heartbeat="2026-04-20T20:00:00Z",
                operation="work",
            ),
        )

        with mock.patch("lib.concurrency.socket.gethostname", return_value="local-host"):
            with mock.patch("lib.concurrency._pid_alive", return_value=True):
                inspection = inspect_work_claim_recovery(
                    do_work_root,
                    claim_path=handle.claim_path,
                    now=claim_time + timedelta(minutes=5),
                )

        self.assertEqual(inspection.verdict, "stale")
        self.assertTrue(inspection.process_alive)

    def test_inspect_work_claim_recovery_reports_recoverable_when_stale_and_pid_absent(self) -> None:
        do_work_root = self.root / "do-work"
        request_path = do_work_root / "REQ-005-recoverable.md"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("queued\n", encoding="utf-8")
        claim_time = datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc)
        handle = claim_work_request(
            do_work_root,
            request_path=request_path,
            session_id="session-dead",
            operation="work",
            now=claim_time,
        )
        write_session_record(
            do_work_root / ".sessions" / "session-dead.json",
            SessionRecord(
                session_id="session-dead",
                hostname="local-host",
                pid=4321,
                started_at="2026-04-20T20:00:00Z",
                last_heartbeat="2026-04-20T20:00:00Z",
                operation="work",
            ),
        )

        with mock.patch("lib.concurrency.socket.gethostname", return_value="local-host"):
            with mock.patch("lib.concurrency._pid_alive", return_value=False):
                inspection = inspect_work_claim_recovery(
                    do_work_root,
                    claim_path=handle.claim_path,
                    now=claim_time + timedelta(minutes=5),
                )

        self.assertEqual(inspection.verdict, "recoverable")
        self.assertFalse(inspection.process_alive)

    def test_recover_orphaned_work_claim_moves_request_back_to_queue_and_logs(self) -> None:
        do_work_root = self.root / "do-work"
        request_path = do_work_root / "REQ-005-recover-me.md"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("queued\n", encoding="utf-8")
        claim_time = datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc)
        handle = claim_work_request(
            do_work_root,
            request_path=request_path,
            session_id="session-dead",
            operation="work",
            now=claim_time,
        )
        write_session_record(
            do_work_root / ".sessions" / "session-dead.json",
            SessionRecord(
                session_id="session-dead",
                hostname="local-host",
                pid=4321,
                started_at="2026-04-20T20:00:00Z",
                last_heartbeat="2026-04-20T20:00:00Z",
                operation="work",
            ),
        )

        with mock.patch("lib.concurrency.socket.gethostname", return_value="local-host"):
            with mock.patch("lib.concurrency._pid_alive", return_value=False):
                result = recover_orphaned_work_claim(
                    do_work_root,
                    claim_path=handle.claim_path,
                    recovering_session_id="session-rescuer",
                    now=claim_time + timedelta(minutes=5),
                )

        self.assertEqual(result.claim_id, "REQ-005")
        self.assertTrue((do_work_root / "REQ-005-recover-me.md").exists())
        self.assertFalse((do_work_root / "working" / "REQ-005-recover-me.md").exists())
        self.assertFalse(Path(handle.claim_path).exists())
        self.assertFalse((do_work_root / ".sessions" / "session-dead.json").exists())
        log = json.loads(Path(result.log_path).read_text(encoding="utf-8"))
        self.assertEqual(log["claim_id"], "REQ-005")
        self.assertEqual(log["released_session_id"], "session-dead")
        self.assertEqual(log["recovered_by_session_id"], "session-rescuer")

    def test_recover_orphaned_work_claim_rejects_ambiguous_recovery(self) -> None:
        do_work_root = self.root / "do-work"
        request_path = do_work_root / "REQ-005-ambiguous.md"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("queued\n", encoding="utf-8")
        claim_time = datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc)
        handle = claim_work_request(
            do_work_root,
            request_path=request_path,
            session_id="session-liveish",
            operation="work",
            now=claim_time,
        )
        write_session_record(
            do_work_root / ".sessions" / "session-liveish.json",
            SessionRecord(
                session_id="session-liveish",
                hostname="local-host",
                pid=1234,
                started_at="2026-04-20T20:00:00Z",
                last_heartbeat="2026-04-20T20:00:00Z",
                operation="work",
            ),
        )

        with mock.patch("lib.concurrency.socket.gethostname", return_value="local-host"):
            with mock.patch("lib.concurrency._pid_alive", return_value=True):
                with self.assertRaises(RecoveryNotAllowedError):
                    recover_orphaned_work_claim(
                        do_work_root,
                        claim_path=handle.claim_path,
                        recovering_session_id="session-rescuer",
                        now=claim_time + timedelta(minutes=5),
                    )

        self.assertTrue(Path(handle.claim_path).exists())
        self.assertTrue((do_work_root / "working" / "REQ-005-ambiguous.md").exists())

    def test_claim_schema_rejects_unknown_scope(self) -> None:
        path = self.root / "claims" / "bad.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "claim_id": "REQ-002",
                    "session_id": "session-1",
                    "operation": "work",
                    "scope": "made-up-scope",
                    "affected_paths": [],
                    "acquired_at": "2026-04-18T00:00:00Z",
                    "last_heartbeat": "2026-04-18T00:00:30Z",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ScopeError):
            read_claim(path)

    def test_acquire_lock_rejects_unknown_scope(self) -> None:
        with self.assertRaises(ScopeError):
            acquire_lock(
                self.root / "locks" / "bad.lock",
                session_id="session-1",
                operation="work",
                scope="unknown-scope",
            )

    def test_claim_work_request_moves_file_and_writes_claim(self) -> None:
        queue_req = self.root / "REQ-004-atomic-req-claim.md"
        queue_req.write_text("payload\n", encoding="utf-8")

        handle = claim_work_request(
            self.root,
            request_path=queue_req,
            session_id="session-1",
            operation="work",
            now=datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc),
        )

        working_req = self.root / "working" / "REQ-004-atomic-req-claim.md"
        claim_path = self.root / "working" / "REQ-004-atomic-req-claim.claim.json"
        self.assertFalse(queue_req.exists())
        self.assertEqual(Path(handle.request_path), working_req)
        self.assertEqual(Path(handle.claim_path), claim_path)
        self.assertEqual(working_req.read_text(encoding="utf-8"), "payload\n")

        claim = read_claim(claim_path)
        self.assertEqual(claim.claim_id, "REQ-004")
        self.assertEqual(claim.session_id, "session-1")
        self.assertEqual(
            claim.affected_paths,
            (str(working_req),),
        )

    def test_claim_work_request_reports_existing_holder(self) -> None:
        queue_req = self.root / "REQ-004-atomic-req-claim.md"
        queue_req.write_text("payload\n", encoding="utf-8")

        claim_work_request(
            self.root,
            request_path=queue_req,
            session_id="winner-session",
            operation="work",
        )

        with self.assertRaises(ClaimHeldError) as ctx:
            claim_work_request(
                self.root,
                request_path=self.root / "REQ-004-atomic-req-claim.md",
                session_id="loser-session",
                operation="work",
            )

        msg = str(ctx.exception)
        self.assertIn("winner-session", msg)
        self.assertIn("loser-session", msg)
        self.assertIn("REQ-004", msg)

    def test_claim_work_request_rejects_second_claim_for_same_session(self) -> None:
        first_req = self.root / "REQ-004-atomic-req-claim.md"
        second_req = self.root / "REQ-005-orphan-recovery.md"
        first_req.write_text("first\n", encoding="utf-8")
        second_req.write_text("second\n", encoding="utf-8")

        claim_work_request(
            self.root,
            request_path=first_req,
            session_id="session-1",
            operation="work",
        )

        with self.assertRaises(SessionClaimConflictError) as ctx:
            claim_work_request(
                self.root,
                request_path=second_req,
                session_id="session-1",
                operation="work",
            )

        self.assertIn("REQ-004", str(ctx.exception))
        self.assertTrue(second_req.exists())

    def test_claim_work_request_rolls_back_claim_if_move_fails(self) -> None:
        queue_req = self.root / "REQ-004-atomic-req-claim.md"
        queue_req.write_text("payload\n", encoding="utf-8")
        claim_path = self.root / "working" / "REQ-004-atomic-req-claim.claim.json"

        with mock.patch(
            "lib.concurrency.atomic_rename",
            side_effect=StaleRenameError("queue entry vanished"),
        ):
            with self.assertRaises(StaleRenameError):
                claim_work_request(
                    self.root,
                    request_path=queue_req,
                    session_id="session-1",
                    operation="work",
                )

        self.assertFalse(claim_path.exists())
        self.assertTrue(queue_req.exists())

    def test_claim_work_request_attaches_git_tree_state_snapshot(self) -> None:
        self._init_git_repo()
        (self.root / "do-work").mkdir(parents=True, exist_ok=True)
        queue_req = self.root / "do-work" / "REQ-010-foreign-edit-detection.md"
        queue_req.write_text("payload\n", encoding="utf-8")
        self._git("add", "do-work/REQ-010-foreign-edit-detection.md")
        self._git("commit", "-m", "baseline")
        expected_head = self._git("rev-parse", "HEAD")

        handle = claim_work_request(
            self.root / "do-work",
            request_path="REQ-010-foreign-edit-detection.md",
            session_id="session-1",
            operation="work",
            repo_root=self.root,
            now=datetime(2026, 4, 20, 21, 40, tzinfo=timezone.utc),
        )

        claim = read_claim(handle.claim_path)
        assert claim.tree_state is not None
        self.assertEqual(claim.tree_state.head_sha, expected_head)
        self.assertEqual(claim.tree_state.preexisting_dirty_paths, ())
        self.assertEqual(
            claim.tree_state.scope_paths,
            ("do-work/working/REQ-010-foreign-edit-detection.md",),
        )
        self.assertEqual(
            [fingerprint.path for fingerprint in claim.tree_state.scope_fingerprints],
            ["do-work/working/REQ-010-foreign-edit-detection.md"],
        )

    def test_claim_work_request_refuses_dirty_git_tree_by_default(self) -> None:
        self._init_git_repo()
        (self.root / "do-work").mkdir(parents=True, exist_ok=True)
        queue_req = self.root / "do-work" / "REQ-010-foreign-edit-detection.md"
        queue_req.write_text("payload\n", encoding="utf-8")
        self._git("add", "do-work/REQ-010-foreign-edit-detection.md")
        self._git("commit", "-m", "baseline")
        (self.root / "foreign.txt").write_text("other work\n", encoding="utf-8")

        with self.assertRaises(TreeStateViolationError) as ctx:
            claim_work_request(
                self.root / "do-work",
                request_path="REQ-010-foreign-edit-detection.md",
                session_id="session-1",
                operation="work",
                repo_root=self.root,
            )

        self.assertIn("git working tree is already dirty", str(ctx.exception))
        self.assertIn("foreign.txt", str(ctx.exception))
        self.assertTrue(queue_req.exists())

    def test_verify_and_stage_claim_scope_rejects_foreign_edit_between_claim_and_commit(self) -> None:
        self._init_git_repo()
        (self.root / "do-work").mkdir(parents=True, exist_ok=True)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        queue_req = self.root / "do-work" / "REQ-010-foreign-edit-detection.md"
        queue_req.write_text("payload\n", encoding="utf-8")
        (self.root / "src" / "owned.py").write_text("print('owned')\n", encoding="utf-8")
        (self.root / "src" / "foreign.py").write_text("print('foreign')\n", encoding="utf-8")
        self._git("add", "do-work/REQ-010-foreign-edit-detection.md", "src/owned.py", "src/foreign.py")
        self._git("commit", "-m", "baseline")

        handle = claim_work_request(
            self.root / "do-work",
            request_path="REQ-010-foreign-edit-detection.md",
            session_id="session-1",
            operation="work",
            repo_root=self.root,
            now=datetime(2026, 4, 20, 21, 45, tzinfo=timezone.utc),
        )

        capture_claim_tree_state(
            self.root,
            claim_handle_or_path=handle,
            scope_paths=(
                "src/owned.py",
                handle.request_path,
            ),
            expected_session_id="session-1",
        )

        (self.root / "src" / "owned.py").write_text("print('owned changed')\n", encoding="utf-8")
        (self.root / "src" / "foreign.py").write_text("print('foreign changed')\n", encoding="utf-8")

        with self.assertRaises(TreeStateViolationError) as ctx:
            verify_and_stage_claim_scope(
                self.root,
                claim_handle_or_path=handle,
                current_request_path=handle.request_path,
                expected_session_id="session-1",
                now=datetime(2026, 4, 20, 21, 46, tzinfo=timezone.utc),
            )

        message = str(ctx.exception)
        self.assertIn("foreign changes detected", message)
        self.assertIn("src/foreign.py", message)
        self.assertIn("session-1", message)

    def test_verify_and_stage_claim_scope_rejects_staged_only_foreign_edit(self) -> None:
        """Regression: a foreign change staged via `git add` then reverted in
        the working tree (`git restore --worktree`) leaves the index dirty
        but the working tree clean. _git_dirty_paths must check the staged
        diff too, otherwise the staged change slips into the scoped commit
        silently."""
        self._init_git_repo()
        (self.root / "do-work").mkdir(parents=True, exist_ok=True)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        queue_req = self.root / "do-work" / "REQ-010-foreign-edit-detection.md"
        queue_req.write_text("payload\n", encoding="utf-8")
        (self.root / "src" / "owned.py").write_text("print('owned')\n", encoding="utf-8")
        (self.root / "src" / "foreign.py").write_text("print('foreign')\n", encoding="utf-8")
        self._git(
            "add",
            "do-work/REQ-010-foreign-edit-detection.md",
            "src/owned.py",
            "src/foreign.py",
        )
        self._git("commit", "-m", "baseline")

        handle = claim_work_request(
            self.root / "do-work",
            request_path="REQ-010-foreign-edit-detection.md",
            session_id="session-1",
            operation="work",
            repo_root=self.root,
            now=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        )

        capture_claim_tree_state(
            self.root,
            claim_handle_or_path=handle,
            scope_paths=(
                "src/owned.py",
                handle.request_path,
            ),
            expected_session_id="session-1",
        )

        # Foreign session: stage a change to foreign.py, then revert the
        # working tree only (leaving the index dirty). Pre-fix, this
        # state was invisible to verify_and_stage_claim_scope and the
        # staged change would be included in the scoped commit.
        (self.root / "src" / "foreign.py").write_text(
            "print('foreign staged')\n", encoding="utf-8"
        )
        self._git("add", "src/foreign.py")
        self._git("restore", "--worktree", "src/foreign.py")

        with self.assertRaises(TreeStateViolationError) as ctx:
            verify_and_stage_claim_scope(
                self.root,
                claim_handle_or_path=handle,
                current_request_path=handle.request_path,
                expected_session_id="session-1",
                now=datetime(2026, 4, 21, 12, 1, tzinfo=timezone.utc),
            )

        message = str(ctx.exception)
        self.assertIn("foreign changes detected", message)
        self.assertIn("src/foreign.py", message)

    def test_verify_and_stage_claim_scope_stages_only_snapshot_paths(self) -> None:
        self._init_git_repo()
        (self.root / "do-work").mkdir(parents=True, exist_ok=True)
        (self.root / "do-work" / "archive").mkdir(parents=True, exist_ok=True)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        queue_req = self.root / "do-work" / "REQ-010-foreign-edit-detection.md"
        queue_req.write_text("payload\n", encoding="utf-8")
        (self.root / "src" / "owned.py").write_text("print('owned')\n", encoding="utf-8")
        self._git("add", "do-work/REQ-010-foreign-edit-detection.md", "src/owned.py")
        self._git("commit", "-m", "baseline")

        handle = claim_work_request(
            self.root / "do-work",
            request_path="REQ-010-foreign-edit-detection.md",
            session_id="session-1",
            operation="work",
            repo_root=self.root,
            now=datetime(2026, 4, 20, 21, 50, tzinfo=timezone.utc),
        )

        capture_claim_tree_state(
            self.root,
            claim_handle_or_path=handle,
            scope_paths=("src/owned.py",),
            expected_session_id="session-1",
        )

        (self.root / "src" / "owned.py").write_text("print('owned changed')\n", encoding="utf-8")
        archived_request = self.root / "do-work" / "archive" / "REQ-010-foreign-edit-detection.md"
        (self.root / handle.request_path).rename(archived_request)
        result = verify_and_stage_claim_scope(
            self.root,
            claim_handle_or_path=handle,
            current_request_path=archived_request,
            expected_session_id="session-1",
            now=datetime(2026, 4, 20, 21, 51, tzinfo=timezone.utc),
        )

        self.assertEqual(
            result.staged_paths,
            (
                "do-work/REQ-010-foreign-edit-detection.md",
                "do-work/archive/REQ-010-foreign-edit-detection.md",
                "src/owned.py",
            ),
        )
        self.assertCountEqual(
            self._git("diff", "--cached", "--name-only").splitlines(),
            [
                "do-work/archive/REQ-010-foreign-edit-detection.md",
                "src/owned.py",
            ],
        )
        self.assertNotIn(
            " D do-work/REQ-010-foreign-edit-detection.md",
            self._git("status", "--short"),
        )

    def test_twenty_processes_race_for_one_lock(self) -> None:
        lock_path = str(self.root / "locks" / "race.lock")
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        release_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_race_worker,
                args=(lock_path, start_event, release_event, result_queue),
            )
            for _ in range(20)
        ]

        for process in processes:
            process.start()

        start_event.set()
        results = [result_queue.get(timeout=5) for _ in range(20)]
        release_event.set()

        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        acquired = [result for result in results if result[0] == "acquired"]
        held = [result for result in results if result[0] == "held"]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(held), 19)
        self.assertFalse(Path(lock_path).exists())

    def test_parallel_work_claims_produce_exactly_one_winner(self) -> None:
        queue_req = self.root / "REQ-004-atomic-req-claim.md"
        queue_req.write_text("payload\n", encoding="utf-8")

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_work_claim_race_worker,
                args=(str(self.root), str(queue_req), start_event, result_queue),
            )
            for _ in range(8)
        ]

        for process in processes:
            process.start()

        start_event.set()
        results = [result_queue.get(timeout=5) for _ in range(8)]

        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        acquired = [result for result in results if result[0] == "acquired"]
        held = [result for result in results if result[0] == "held"]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(held), 7)

        winner_session = acquired[0][1]
        for _, _, message in held:
            self.assertIn(winner_session, message)

        self.assertFalse(queue_req.exists())
        self.assertTrue((self.root / "working" / "REQ-004-atomic-req-claim.md").exists())
        self.assertTrue((self.root / "working" / "REQ-004-atomic-req-claim.claim.json").exists())

    def test_parallel_verify_request_locking_produces_one_winner_and_named_holder(self) -> None:
        do_work_root = self.root / "do-work"
        do_work_root.mkdir(parents=True, exist_ok=True)
        target = do_work_root / "REQ-007-verify-document-locks.md"
        target.write_text(
            "# Verify Target\n\n"
            "## Verification\n\n"
            "Original verification.\n",
            encoding="utf-8",
        )

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        release_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_verify_request_race_worker,
                args=(str(do_work_root), str(target), start_event, release_event, result_queue),
            )
            for _ in range(2)
        ]

        for process in processes:
            process.start()

        start_event.set()
        results = [result_queue.get(timeout=5) for _ in range(2)]
        release_event.set()

        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        acquired = [result for result in results if result[0] == "acquired"]
        held = [result for result in results if result[0] == "held"]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(held), 1)

        winner_session = acquired[0][1]
        held_message = held[0][2]
        self.assertIn(winner_session, held_message)
        self.assertIn("REQ-007-verify-document-locks.md", held_message)
        self.assertIn("verify-REQ-007.lock", held_message)
        self.assertFalse((do_work_root / ".locks" / "verify-REQ-007.lock").exists())
        self.assertIn(
            f"winner: {winner_session}",
            target.read_text(encoding="utf-8"),
        )

    def test_archive_completed_request_closes_ur_when_last_req_finishes(self) -> None:
        working_dir = self.root / "working"
        archive_dir = self.root / "archive"
        ur_dir = self.root / "user-requests" / "UR-001"
        working_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)
        ur_dir.mkdir(parents=True, exist_ok=True)

        (ur_dir / "input.md").write_text(
            self._ur_input("UR-001", ["REQ-006", "REQ-007"]),
            encoding="utf-8",
        )
        (archive_dir / "REQ-007-peer.md").write_text(
            self._req_frontmatter("REQ-007", user_request="UR-001"),
            encoding="utf-8",
        )
        working_req = working_dir / "REQ-006-atomic-ur-archival.md"
        working_req.write_text(
            self._req_frontmatter("REQ-006", user_request="UR-001"),
            encoding="utf-8",
        )

        result = archive_completed_request(
            self.root,
            working_request_path=working_req,
            session_id="session-1",
        )

        self.assertEqual(result.outcome, "archived-parent")
        self.assertFalse(working_req.exists())
        self.assertFalse((archive_dir / "REQ-007-peer.md").exists())
        self.assertFalse(ur_dir.exists())
        self.assertTrue((archive_dir / "UR-001" / "input.md").exists())
        self.assertTrue((archive_dir / "UR-001" / "REQ-006-atomic-ur-archival.md").exists())
        self.assertTrue((archive_dir / "UR-001" / "REQ-007-peer.md").exists())
        assert result.parent_result is not None
        self.assertEqual(result.parent_result.outcome, "archived")

    def test_archive_completed_request_leaves_ur_open_when_other_reqs_are_not_archived(self) -> None:
        working_dir = self.root / "working"
        archive_dir = self.root / "archive"
        ur_dir = self.root / "user-requests" / "UR-001"
        working_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)
        ur_dir.mkdir(parents=True, exist_ok=True)

        (ur_dir / "input.md").write_text(
            self._ur_input("UR-001", ["REQ-006", "REQ-007"]),
            encoding="utf-8",
        )
        working_req = working_dir / "REQ-006-atomic-ur-archival.md"
        working_req.write_text(
            self._req_frontmatter("REQ-006", user_request="UR-001"),
            encoding="utf-8",
        )

        result = archive_completed_request(
            self.root,
            working_request_path=working_req,
            session_id="session-1",
        )

        self.assertEqual(result.outcome, "archived-root")
        self.assertTrue((archive_dir / "REQ-006-atomic-ur-archival.md").exists())
        self.assertTrue((ur_dir / "input.md").exists())
        assert result.parent_result is not None
        self.assertEqual(result.parent_result.outcome, "not-ready")
        self.assertEqual(result.parent_result.missing_request_ids, ("REQ-007",))

    def test_archive_completed_request_fails_loud_if_ur_was_already_archived(self) -> None:
        working_dir = self.root / "working"
        archive_ur_dir = self.root / "archive" / "UR-001"
        working_dir.mkdir(parents=True, exist_ok=True)
        archive_ur_dir.mkdir(parents=True, exist_ok=True)

        working_req = working_dir / "REQ-006-atomic-ur-archival.md"
        working_req.write_text(
            self._req_frontmatter("REQ-006", user_request="UR-001"),
            encoding="utf-8",
        )

        with self.assertRaises(ConcurrencyError) as ctx:
            archive_completed_request(
                self.root,
                working_request_path=working_req,
                session_id="session-1",
            )

        self.assertIn("already archived", str(ctx.exception))
        self.assertTrue(working_req.exists())

    def test_archive_completed_request_archives_legacy_context_once_all_reqs_are_done(self) -> None:
        working_dir = self.root / "working"
        archive_dir = self.root / "archive"
        assets_dir = self.root / "assets"
        working_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)

        (assets_dir / "CONTEXT-001-batch.md").write_text(
            self._context_input(["REQ-006"]),
            encoding="utf-8",
        )
        working_req = working_dir / "REQ-006-legacy.md"
        working_req.write_text(
            self._req_frontmatter(
                "REQ-006",
                context_ref="assets/CONTEXT-001-batch.md",
            ),
            encoding="utf-8",
        )

        result = archive_completed_request(
            self.root,
            working_request_path=working_req,
            session_id="session-1",
        )

        self.assertEqual(result.outcome, "archived-root")
        self.assertTrue((archive_dir / "REQ-006-legacy.md").exists())
        self.assertTrue((archive_dir / "CONTEXT-001-batch.md").exists())
        self.assertFalse((assets_dir / "CONTEXT-001-batch.md").exists())
        assert result.parent_result is not None
        self.assertEqual(result.parent_result.kind, "legacy-context")
        self.assertEqual(result.parent_result.outcome, "archived")

    def test_archive_legacy_context_returns_already_archived_after_first_winner(self) -> None:
        assets_dir = self.root / "assets"
        archive_dir = self.root / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)

        (archive_dir / "REQ-006-legacy.md").write_text(
            self._req_frontmatter("REQ-006"),
            encoding="utf-8",
        )
        (assets_dir / "CONTEXT-001-batch.md").write_text(
            self._context_input(["REQ-006"]),
            encoding="utf-8",
        )

        first = archive_legacy_context_if_complete(
            self.root,
            context_ref="assets/CONTEXT-001-batch.md",
            session_id="session-1",
        )
        second = archive_legacy_context_if_complete(
            self.root,
            context_ref="assets/CONTEXT-001-batch.md",
            session_id="session-2",
        )

        self.assertEqual(first.outcome, "archived")
        self.assertEqual(second.outcome, "already-archived")

    def test_parallel_ur_archival_has_one_winner_and_clean_losers(self) -> None:
        archive_dir = self.root / "archive"
        ur_dir = self.root / "user-requests" / "UR-001"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ur_dir.mkdir(parents=True, exist_ok=True)

        (ur_dir / "input.md").write_text(
            self._ur_input("UR-001", ["REQ-006", "REQ-007"]),
            encoding="utf-8",
        )
        (archive_dir / "REQ-006-a.md").write_text(
            self._req_frontmatter("REQ-006", user_request="UR-001"),
            encoding="utf-8",
        )
        (archive_dir / "REQ-007-b.md").write_text(
            self._req_frontmatter("REQ-007", user_request="UR-001"),
            encoding="utf-8",
        )

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_ur_archival_race_worker,
                args=(str(self.root), "UR-001", start_event, result_queue),
            )
            for _ in range(6)
        ]

        for process in processes:
            process.start()

        start_event.set()
        results = [result_queue.get(timeout=5) for _ in range(6)]

        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        archived = [result for result in results if result[0] == "ok" and result[2] == "archived"]
        already_archived = [
            result for result in results if result[0] == "ok" and result[2] == "already-archived"
        ]
        errors = [result for result in results if result[0] == "error"]

        self.assertEqual(len(archived), 1)
        self.assertEqual(len(already_archived), 5)
        self.assertEqual(errors, [])
        self.assertTrue((archive_dir / "UR-001" / "REQ-006-a.md").exists())
        self.assertTrue((archive_dir / "UR-001" / "REQ-007-b.md").exists())
        self.assertFalse((self.root / "user-requests" / "UR-001").exists())


if __name__ == "__main__":
    unittest.main()
