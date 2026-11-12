"""0.16.0-dev2: the account - password hashing, the credentials table, reset-password CLI."""

import io
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from audiobind import auth as auth_mod
from audiobind import cli
from audiobind.config import CONFIG
from audiobind.state import State

FAST = 1000  # PBKDF2 rounds for tests that are not about the round count


class FakeStdin(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class HashingTests(unittest.TestCase):
    def test_default_is_pbkdf2_sha256_600k_with_16_byte_salt(self):
        self.assertEqual(auth_mod.ITERATIONS, 600_000)
        hashed = auth_mod.hash_password("hunter2hunter2")
        self.assertEqual(hashed.algo, "pbkdf2_sha256")
        self.assertEqual(hashed.iterations, 600_000)
        self.assertEqual(len(bytes.fromhex(hashed.salt)), 16)
        self.assertEqual(len(bytes.fromhex(hashed.digest)), 32)
        self.assertTrue(auth_mod.verify_password("hunter2hunter2", hashed))

    def test_salted_per_hash(self):
        one = auth_mod.hash_password("same password", FAST)
        two = auth_mod.hash_password("same password", FAST)
        self.assertNotEqual(one.salt, two.salt)
        self.assertNotEqual(one.digest, two.digest)
        self.assertTrue(auth_mod.verify_password("same password", one))
        self.assertTrue(auth_mod.verify_password("same password", two))

    def test_wrong_password_and_garbage_rows_fail(self):
        hashed = auth_mod.hash_password("right password", FAST)
        self.assertFalse(auth_mod.verify_password("wrong password", hashed))
        self.assertFalse(auth_mod.verify_password("", hashed))
        bad = auth_mod.PasswordHash("md5", FAST, hashed.salt, hashed.digest)
        self.assertFalse(auth_mod.verify_password("right password", bad))
        self.assertFalse(auth_mod.verify_password("right password", auth_mod.PasswordHash(
            "pbkdf2_sha256", FAST, "not hex", hashed.digest)))

    def test_repr_hides_secrets(self):
        hashed = auth_mod.hash_password("right password", FAST)
        self.assertNotIn(hashed.salt, repr(hashed))
        self.assertNotIn(hashed.digest, repr(hashed))

    def test_check_login_needs_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            state.create_credentials("alice", auth_mod.hash_password("password-1", FAST))
            creds = state.get_credentials()
            state.close()
        self.assertTrue(auth_mod.check_login(creds, "alice", "password-1"))
        self.assertFalse(auth_mod.check_login(creds, "alice", "password-2"))
        self.assertFalse(auth_mod.check_login(creds, "bob", "password-1"))
        self.assertFalse(auth_mod.check_login(creds, "", "password-1"))
        self.assertFalse(auth_mod.check_login(creds, "alice", ""))

    def test_check_login_does_the_hash_work_even_for_a_wrong_username(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            state.create_credentials("alice", auth_mod.hash_password("password-1", FAST))
            creds = state.get_credentials()
            state.close()
        with mock.patch.object(auth_mod, "_pbkdf2", wraps=auth_mod._pbkdf2) as spy:
            auth_mod.check_login(creds, "not-alice", "password-1")
            auth_mod.check_login(creds, "alice", "wrong-password")
        self.assertEqual(spy.call_count, 2)

    def test_rules(self):
        self.assertEqual(auth_mod.username_problem("a"), "")
        self.assertEqual(auth_mod.username_problem("x" * 64), "")
        self.assertTrue(auth_mod.username_problem(""))
        self.assertTrue(auth_mod.username_problem("x" * 65))
        self.assertTrue(auth_mod.username_problem("bad\nname"))
        self.assertEqual(auth_mod.password_problem("12345678"), "")
        self.assertTrue(auth_mod.password_problem("1234567"))
        self.assertTrue(auth_mod.password_problem("12345678", "12345679"))
        self.assertEqual(auth_mod.password_problem("12345678", "12345678"), "")


class SessionVersionTests(unittest.TestCase):
    def test_session_only_valid_under_its_version(self):
        sessions = auth_mod.Sessions()
        sid = sessions.create(version=3)
        self.assertTrue(sessions.valid(sid, 3))
        self.assertFalse(sessions.valid(sid, 4))
        self.assertFalse(sessions.valid(sid, 3))  # a stale session is dropped, not revived

    def test_bump_keeps_only_the_named_session(self):
        sessions = auth_mod.Sessions()
        mine, other = sessions.create(1), sessions.create(1)
        sessions.bump(2, keep=mine)
        self.assertTrue(sessions.valid(mine, 2))
        self.assertFalse(sessions.valid(other, 2))
        self.assertFalse(sessions.valid(other))

    def test_bearer_cache_is_tied_to_the_version(self):
        cache = auth_mod.BearerCache(size=2)
        cache.add("pw", 1)
        self.assertTrue(cache.hit("pw", 1))
        self.assertFalse(cache.hit("pw", 2))
        self.assertFalse(cache.hit("other", 1))
        cache.add("a", 1)
        cache.add("b", 1)
        self.assertFalse(cache.hit("pw", 1))  # bounded


class CredentialsTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name))

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_fresh_db_has_no_account(self):
        self.assertIsNone(self.state.get_credentials())
        self.assertFalse(self.state.has_credentials())
        self.assertEqual(self.state.credentials_version(), 0)

    def test_columns(self):
        cols = {r[1] for r in self.state.connection.execute("PRAGMA table_info(credentials)")}
        self.assertTrue({"username", "password_hash", "salt", "algo", "created_at"} <= cols)

    def test_single_account_only(self):
        self.assertEqual(self.state.create_credentials("alice", auth_mod.hash_password("password-1", FAST)), 1)
        self.assertEqual(self.state.create_credentials("mallory", auth_mod.hash_password("password-2", FAST)), 0)
        creds = self.state.get_credentials()
        self.assertEqual(creds.username, "alice")
        self.assertTrue(auth_mod.verify_password("password-1", creds))
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.connection.execute(
                "INSERT INTO credentials (id, username, password_hash, salt, algo, iterations, "
                "created_at, updated_at) VALUES (2,'x','x','x','x',1,0,0)"
            )

    def test_no_plaintext_and_fresh_salt_on_every_change(self):
        self.state.create_credentials("alice", auth_mod.hash_password("plain-text-1", FAST))
        first = self.state.get_credentials()
        self.state.set_password(auth_mod.hash_password("plain-text-1", FAST))
        second = self.state.get_credentials()
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.password_hash, second.password_hash)
        dump = "\n".join(self.state.connection.iterdump())
        self.assertNotIn("plain-text-1", dump)
        self.assertNotIn(second.password_hash, repr(second))
        self.assertNotIn(second.salt, repr(second))

    def test_every_change_bumps_the_version(self):
        self.state.create_credentials("alice", auth_mod.hash_password("password-1", FAST))
        self.assertEqual(self.state.credentials_version(), 1)
        self.assertEqual(self.state.set_password(auth_mod.hash_password("password-2", FAST)), 2)
        self.assertEqual(self.state.set_username("alicia"), 3)
        self.assertEqual(self.state.set_credentials("root", auth_mod.hash_password("password-3", FAST)), 4)
        creds = self.state.get_credentials()
        self.assertEqual((creds.username, creds.version), ("root", 4))
        self.assertTrue(auth_mod.verify_password("password-3", creds))

    def test_change_without_account_is_a_no_op(self):
        self.assertEqual(self.state.set_password(auth_mod.hash_password("password-1", FAST)), 0)
        self.assertEqual(self.state.set_username("alice"), 0)
        self.assertIsNone(self.state.get_credentials())

    def test_survives_reopen(self):
        self.state.create_credentials("alice", auth_mod.hash_password("password-1", FAST))
        self.state.close()
        self.state = State(pathlib.Path(self.tmp.name))
        self.assertEqual(self.state.get_credentials().username, "alice")


class ResetPasswordCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        patches = [
            mock.patch.object(CONFIG, "state_dir", self.dir),
            mock.patch.object(CONFIG, "auth_token", ""),
            mock.patch.object(auth_mod, "ITERATIONS", FAST),
            mock.patch.object(cli, "setup_logging"),
            # preflight would want library/output mounts: reset-password must not need them
            mock.patch.object(CONFIG, "preflight", side_effect=AssertionError("preflight ran")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, stdin, secrets=(), typed=()):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", stdin), \
                mock.patch("getpass.getpass", side_effect=list(secrets)) as gp, \
                mock.patch("builtins.input", side_effect=list(typed)), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue(), gp

    def creds(self):
        state = State(self.dir)
        try:
            return state.get_credentials()
        finally:
            state.close()

    def test_creates_account_on_fresh_db(self):
        code, out, err, gp = self.run_cli(
            ["reset-password"], FakeStdin(tty=True), secrets=["new-password", "new-password"], typed=["alice"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(gp.call_count, 2)  # hidden prompts, password + confirm
        creds = self.creds()
        self.assertEqual(creds.username, "alice")
        self.assertTrue(auth_mod.verify_password("new-password", creds))
        self.assertIn("created", out)
        self.assertNotIn("new-password", out + err)
        self.assertNotIn(creds.password_hash, out + err)

    def test_replaces_existing_account(self):
        state = State(self.dir)
        state.create_credentials("alice", auth_mod.hash_password("old-password", FAST))
        old = state.get_credentials()
        state.close()
        code, out, err, _ = self.run_cli(
            ["reset-password", "--username", "bob"], FakeStdin(tty=True), secrets=["fresh-password"] * 2
        )
        self.assertEqual(code, 0, err)
        creds = self.creds()
        self.assertEqual(creds.username, "bob")
        self.assertTrue(auth_mod.verify_password("fresh-password", creds))
        self.assertFalse(auth_mod.verify_password("old-password", creds))
        self.assertNotEqual(creds.salt, old.salt)
        self.assertGreater(creds.version, old.version)  # every session from before is over
        self.assertIn("replaced", out)

    def test_username_prompt_defaults_to_current(self):
        state = State(self.dir)
        state.create_credentials("alice", auth_mod.hash_password("old-password", FAST))
        state.close()
        code, _, err, _ = self.run_cli(
            ["reset-password"], FakeStdin(tty=True), secrets=["fresh-password"] * 2, typed=[""]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.creds().username, "alice")

    def test_refuses_non_tty_without_username(self):
        state = State(self.dir)
        state.create_credentials("alice", auth_mod.hash_password("old-password", FAST))
        before = state.get_credentials()
        state.close()
        code, _, err, gp = self.run_cli(["reset-password"], FakeStdin("x\n", tty=False))
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)
        gp.assert_not_called()
        self.assertEqual(self.creds(), before)

    def test_non_tty_with_username_reads_stdin(self):
        code, _, err, gp = self.run_cli(
            ["reset-password", "--username", "ci"], FakeStdin("piped-password\n", tty=False)
        )
        self.assertEqual(code, 0, err)
        gp.assert_not_called()
        self.assertTrue(auth_mod.verify_password("piped-password", self.creds()))

    def test_cannot_blank_auth(self):
        for stdin, secrets in ((FakeStdin("\n"), ()), (FakeStdin(""), ()), (FakeStdin(tty=True), ["", ""])):
            code, _, err, _ = self.run_cli(["reset-password", "--username", "ci"], stdin, secrets=secrets)
            self.assertEqual(code, 2, err)
        self.assertIsNone(self.creds())

    def test_mismatch_and_bad_username_change_nothing(self):
        code, _, err, _ = self.run_cli(
            ["reset-password", "--username", "alice"], FakeStdin(tty=True), secrets=["password-1", "password-2"]
        )
        self.assertEqual(code, 2)
        self.assertIn("do not match", err)
        code, _, _, _ = self.run_cli(["reset-password", "--username", "x" * 65], FakeStdin(tty=True))
        self.assertEqual(code, 2)
        self.assertIsNone(self.creds())

    def test_ctrl_c_changes_nothing(self):
        code, _, err, _ = self.run_cli(["reset-password"], FakeStdin(tty=True), typed=[KeyboardInterrupt()])
        self.assertEqual(code, 1)
        self.assertIsNone(self.creds())


if __name__ == "__main__":
    unittest.main()
