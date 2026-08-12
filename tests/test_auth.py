import tempfile
import unittest
from pathlib import Path

from app.auth import SessionSigner, UserStore, hash_password, verify_password


class PasswordTests(unittest.TestCase):
    def test_password_hash_round_trip(self) -> None:
        encoded = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        self.assertNotIn("correct horse", encoded)

    def test_user_password_can_be_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "users.db")
            store.ensure_user("admin", "first-password")
            self.assertTrue(store.authenticate("admin", "first-password"))
            self.assertTrue(store.update_password("admin", "second-password"))
            self.assertFalse(store.authenticate("admin", "first-password"))
            self.assertTrue(store.authenticate("admin", "second-password"))


class SessionSignerTests(unittest.TestCase):
    def test_signed_session_round_trip_and_tamper_rejection(self) -> None:
        signer = SessionSigner(b"test-secret")
        token = signer.issue("admin")
        self.assertEqual("admin", signer.verify(token))
        self.assertIsNone(signer.verify(token + "tampered"))


if __name__ == "__main__":
    unittest.main()
