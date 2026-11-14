"""0.18.0-dev9: sessions survive a restart (State DB), "Keep me signed in for 30 days",
and the remembered-username cookie (a convenience, never a credential)."""

import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from audiobind import auth as auth_mod
from audiobind import web
from audiobind.state import State

try:
    from test_account_web import PASSWORD, USER, AccountCase
except ImportError:
    from tests.test_account_web import PASSWORD, USER, AccountCase

DAY = 24 * 3600
SESSION = auth_mod.COOKIE_NAME
REMEMBERED = auth_mod.USER_COOKIE_NAME


def set_cookies(headers) -> list[str]:
    return headers.get_all("Set-Cookie") or []


def jar(**cookies) -> dict:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1_000_000.0]
        self.state = State(Path(self.tmp.name))
        self.addCleanup(self.state.connection.close)

    def sessions(self) -> auth_mod.Sessions:
        sessions = auth_mod.Sessions(clock=lambda: self.now[0])
        sessions.attach(self.state)
        return sessions

    def test_ttl_is_per_session(self):
        sessions = self.sessions()
        week = sessions.create(1)
        month = sessions.create(1, ttl=auth_mod.REMEMBER_TTL)
        self.now[0] += 8 * DAY
        self.assertFalse(sessions.valid(week))
        self.assertTrue(sessions.valid(month))
        self.assertEqual(auth_mod.SESSION_TTL, 7 * DAY)
        self.assertEqual(auth_mod.REMEMBER_TTL, 30 * DAY)

    def test_session_survives_a_restart_and_expired_does_not(self):
        sessions = self.sessions()
        short = sessions.create(1)
        long = sessions.create(1, ttl=auth_mod.REMEMBER_TTL)
        self.now[0] += 10 * DAY  # the short one has expired, the 30-day one has not
        restarted = self.sessions()  # a new process: empty memory, reload from the DB
        self.assertTrue(restarted.valid(long, 1))
        self.assertFalse(restarted.valid(short, 1))
        self.assertEqual(len(restarted), 1)
        self.now[0] += 21 * DAY
        self.assertFalse(self.sessions().valid(long, 1))

    def test_db_holds_a_hash_not_the_session_id(self):
        sid = self.sessions().create(1)
        rows = self.state.connection.execute("SELECT * FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(sid, [str(v) for v in tuple(rows[0])])
        self.assertEqual(len(rows[0]["sid_hash"]), 64)

    def test_credential_change_keeps_only_the_current_session_across_a_restart(self):
        sessions = self.sessions()
        mine = sessions.create(1)
        other = sessions.create(1)
        sessions.bump(2, keep=mine)
        for table in (sessions, self.sessions()):
            self.assertTrue(table.valid(mine, 2))
            self.assertFalse(table.valid(other, 2))

    def test_stale_version_session_is_invalid_after_restart(self):
        sid = self.sessions().create(1)
        self.assertFalse(self.sessions().valid(sid, 2))

    def test_revoke_persists(self):
        sessions = self.sessions()
        sid = sessions.create(1)
        sessions.revoke(sid)
        self.assertFalse(self.sessions().valid(sid, 1))

    def test_cap_drops_the_oldest(self):
        sessions = self.sessions()
        first = sessions.create(1)
        for _ in range(auth_mod.MAX_SESSIONS):
            self.now[0] += 1
            sessions.create(1)
        self.assertEqual(len(sessions), auth_mod.MAX_SESSIONS)
        self.assertFalse(sessions.valid(first, 1))
        count = self.state.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(count, auth_mod.MAX_SESSIONS)

    def test_cookie_flags_follow_the_chosen_lifetime(self):
        cookie = auth_mod.session_cookie("abc", auth_mod.REMEMBER_TTL)
        for part in ("Max-Age=2592000", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, cookie)
        self.assertIn("Max-Age=604800", auth_mod.session_cookie("abc"))


class RememberWebTests(AccountCase):
    def setUp(self):
        super().setUp()
        self.state.create_credentials(USER, auth_mod.hash_password(PASSWORD))
        auth_mod.SESSIONS.attach(self.state)
        self.addCleanup(auth_mod.SESSIONS.clear)

    def sid_of(self, headers) -> str:
        cookie = next(c for c in set_cookies(headers) if c.startswith(SESSION + "="))
        return auth_mod.cookie_value(cookie.split(";", 1)[0])

    def expiry_of(self, headers) -> float:
        key = auth_mod.Sessions._key(self.sid_of(headers))
        row = self.state.connection.execute("SELECT expires_at FROM sessions WHERE sid_hash = ?", (key,)).fetchone()
        return row[0]

    def test_checked_gives_thirty_days(self):
        _, headers, _ = self.form("/login", {"username": USER, "password": PASSWORD, "remember": "1"})
        session = next(c for c in set_cookies(headers) if c.startswith(SESSION))
        self.assertIn("Max-Age=2592000", session)
        self.assertAlmostEqual(self.expiry_of(headers) - time.time(), 30 * DAY, delta=120)

    def test_unchecked_gives_seven_days(self):
        _, headers, _ = self.login()
        session = next(c for c in set_cookies(headers) if c.startswith(SESSION))
        self.assertIn("Max-Age=604800", session)
        self.assertAlmostEqual(self.expiry_of(headers) - time.time(), 7 * DAY, delta=120)

    def test_login_page_has_the_unchecked_checkbox(self):
        _, _, body = self.raw("/login")
        self.assertIn(b'name="remember"', body)
        self.assertIn(b"Keep me signed in for 30 days", body)
        self.assertIn(b"this browser only", body)
        self.assertNotIn(b"checked", body)

    def test_username_cookie_set_prefills_and_is_escaped(self):
        _, headers, _ = self.login()
        user_cookie = next(c for c in set_cookies(headers) if c.startswith(REMEMBERED + "="))
        for part in ("Max-Age=2592000", "Path=/", "HttpOnly", "SameSite=Strict"):
            self.assertIn(part, user_cookie)
        page = self.raw("/login", headers=jar(**{REMEMBERED: USER}))[2]
        self.assertIn(f'value="{USER}"'.encode(), page)
        evil = urllib.parse.quote('"><script>alert(1)</script>', safe="")
        page = self.raw("/login", headers=jar(**{REMEMBERED: evil}))[2]
        self.assertNotIn(b"<script>alert", page)

    def test_explicit_username_beats_the_cookie(self):
        page = web.render_login("nope", "typed", remembered="fromcookie")
        self.assertIn('value="typed"', page)
        self.assertNotIn("fromcookie", page)
        self.assertIn('value="fromcookie"', web.render_login(remembered="fromcookie"))

    def test_username_cookie_is_not_a_credential(self):
        only = jar(**{REMEMBERED: USER})
        self.assertEqual(self.raw("/api/units", headers=only)[0], 401)
        self.assertEqual(self.raw("/", headers=only)[0], 303)
        as_session = jar(**{REMEMBERED: USER, SESSION: USER})
        self.assertEqual(self.raw("/api/units", headers=as_session)[0], 401)

    def test_thirty_day_session_survives_a_server_restart(self):
        _, headers, _ = self.form("/login", {"username": USER, "password": PASSWORD, "remember": "1"})
        cookie = jar(**{SESSION: self.sid_of(headers)})
        self.assertEqual(self.raw("/api/units", headers=cookie)[0], 200)
        auth_mod.SESSIONS._expiry.clear()  # the process "restarts": memory is gone
        auth_mod.SESSIONS._version.clear()
        self.assertEqual(self.raw("/api/units", headers=cookie)[0], 401)
        auth_mod.SESSIONS.attach(self.state)  # startup reloads from the DB
        self.assertEqual(self.raw("/api/units", headers=cookie)[0], 200)

    def test_logout_revokes_session_and_keeps_username_cookie(self):
        _, headers, _ = self.login()
        cookie = jar(**{SESSION: self.sid_of(headers)})
        status, out, _ = self.raw("/logout", headers=cookie)
        self.assertEqual(status, 303)
        cookies = set_cookies(out)
        self.assertTrue(any(c.startswith(SESSION + "=;") and "Max-Age=0" in c for c in cookies))
        self.assertFalse(any(c.startswith(REMEMBERED) for c in cookies))
        self.assertEqual(self.raw("/api/units", headers=cookie)[0], 401)
        auth_mod.SESSIONS.attach(self.state)
        self.assertEqual(self.raw("/api/units", headers=cookie)[0], 401)

    def test_account_change_keeps_current_session_only(self):
        _, h1, _ = self.login()
        _, h2, _ = self.login()
        mine = jar(**{SESSION: self.sid_of(h1)})
        other = jar(**{SESSION: self.sid_of(h2)})
        new = "another long one"
        status, _, _ = self.account(
            {"action": "password", "current": PASSWORD, "new_password": new, "confirm": new}, mine
        )
        self.assertEqual(status, 200)
        auth_mod.SESSIONS.attach(self.state)  # even after a restart
        self.assertEqual(self.raw("/api/units", headers=mine)[0], 200)
        self.assertEqual(self.raw("/api/units", headers=other)[0], 401)


if __name__ == "__main__":
    unittest.main()
