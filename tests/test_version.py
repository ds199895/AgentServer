import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.version import DEVELOPMENT_BUILD, frontend_build_sha, resolve_build_sha, verify_release_pair


class BuildVersionTests(unittest.TestCase):
    def test_resolves_environment_then_marker_then_development(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(DEVELOPMENT_BUILD, resolve_build_sha(root))
                (root / "BUILD_SHA").write_text("abc1234\n", encoding="utf-8")
                self.assertEqual("abc1234", resolve_build_sha(root))
                with patch.dict(os.environ, {"AGENTSERVER_BUILD_SHA": "def5678"}):
                    self.assertEqual("def5678", resolve_build_sha(root))

    def test_production_rejects_mixed_frontend_and_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            web_dist = Path(directory)
            (web_dist / "build.json").write_text(
                json.dumps({"build_sha": "a" * 40}), encoding="utf-8"
            )
            self.assertEqual("a" * 40, frontend_build_sha(web_dist))
            self.assertEqual(
                "a" * 40,
                verify_release_pair("a" * 40, web_dist, production=True),
            )
            with self.assertRaisesRegex(RuntimeError, "build mismatch"):
                verify_release_pair("b" * 40, web_dist, production=True)
            with self.assertRaisesRegex(RuntimeError, "missing or invalid"):
                verify_release_pair(DEVELOPMENT_BUILD, web_dist, production=True)


if __name__ == "__main__":
    unittest.main()
