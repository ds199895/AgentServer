from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.execution.runtime_lock import RuntimeInstanceLock


class RuntimeInstanceLockTests(unittest.TestCase):
    def test_second_runtime_fails_closed_and_release_allows_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.lock"
            first = RuntimeInstanceLock(path)
            second = RuntimeInstanceLock(path)
            first.acquire()
            self.assertTrue(first.acquired)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with self.assertRaisesRegex(RuntimeError, "exactly one API worker"):
                second.acquire()

            first.release()
            second.acquire()
            self.assertTrue(second.acquired)
            second.release()

    def test_context_manager_releases_after_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.lock"
            with self.assertRaisesRegex(ValueError, "injected"):
                with RuntimeInstanceLock(path):
                    raise ValueError("injected")
            replacement = RuntimeInstanceLock(path)
            replacement.acquire()
            replacement.release()
