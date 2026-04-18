import errno
import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib.concurrency import (
    AtomicWriteError,
    ClaimRecord,
    ClaimFormatError,
    CollisionError,
    CrossDeviceError,
    ForeignReleaseError,
    LockHandle,
    LockHeldError,
    LockInfo,
    ScopeError,
    StaleRenameError,
    acquire_lock,
    atomic_rename,
    atomic_write,
    classify_lock,
    inspect_lock,
    read_claim,
    refresh_heartbeat,
    release_lock,
    write_claim,
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


class ConcurrencyPrimitivesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

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


if __name__ == "__main__":
    unittest.main()
