"""0.16.0-dev2: first-run setup, username + password sign-in, legacy AUTH_TOKEN, Settings >
Account, Bearer with the account password - through the real HTTP handler."""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from audiobind import auth as auth_mod
from audiobind import web
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

USER = "reader"
PASSWORD = "correct horse battery"
TOKEN = "legacy-shared-token"
FAST = 1000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


class AccountCase(LiveServerCase):
    signed_in = False  # every test here starts from a fresh database, nobody signed in

    def setUp(self):
        super().setUp()
        self.saved_token = CONFIG.auth_token
        CONFIG.auth_token = ""
        patcher = mock.patch.object(auth_mod, "ITERATIONS", FAST)
        patcher.start()
        self.addCleanup(patcher.stop)
        for table in (auth_mod.SESSIONS, auth_mod.LIMITER, auth_mod.BEARERS):
            table.clear()
        src = self.lib / "Author" / "Book"
        os.makedirs(src)
        (src / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="Author/Book", status="review"))

    def tearDown(self):
        CONFIG.auth_token = self.saved_token
        for table in (auth_mod.SESSIONS, auth_mod.LIMITER, auth_mod.BEARERS):
            table.clear()
        CONTROL.resume()
        CONTROL.clear_scan()
        super().tearDown()

    # -- helpers ---------------------------------------------------------------------------

    def raw(self, path, data=None, headers=None):
        """(status, headers, body) without following redirects."""
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with OPENER.open(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            out = exc.code, exc.headers, exc.read()
            exc.close()
            return out

    def form(self, path, fields, headers=None):
        body = urllib.parse.urlencode(fields).encode()
        return self.raw(path, body, {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def json_post(self, path, payload, headers=None):
        status, hdrs, body = self.raw(
            path, json.dumps(payload).encode(), {"Content-Type": "application/json", **(headers or {})}
        )
        return status, hdrs, json.loads(body or b"{}")

    @staticmethod
    def jar(headers):
        sid = auth_mod.cookie_value(headers["Set-Cookie"].split(";", 1)[0])
        return {"Cookie": f"{auth_mod.COOKIE_NAME}={sid}"}

    def setup_account(self, username=USER, password=PASSWORD):
        status, headers, _ = self.form("/setup", {"username": username, "password": password, "confirm": password})
        self.assertEqual(status, 303)
        return self.jar(headers)

    def login(self, username=USER, password=PASSWORD):
        return self.form("/login", {"username": username, "password": password})

    def account(self, payload, jar):
        return self.json_post("/api/account", payload, jar)


class FirstRunTests(AccountCase):
    def test_fresh_db_sends_every_page_to_setup(self):
        for path in ("/", "/settings", "/series", "/?status=review", "/login", "/nonexistent"):
            status, headers, _ = self.raw(path)
            self.assertEqual(status, 303, path)
            self.assertEqual(headers["Location"], "/setup", path)

    def test_fresh_db_refuses_api_and_posts_with_a_pointer_to_setup(self):
        status, _, body = self.raw("/api/units")
        self.assertEqual(status, 403)
        self.assertNotIn(b"Author/Book", body)
        self.assertEqual(json.loads(body)["setup"], "/setup")
        status, _, _ = self.json_post("/api/unit", {"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 403)
        self.assertTrue((self.lib / "Author" / "Book").exists())
        status, _, _ = self.json_post("/api/control", {"action": "pause"})
        self.assertEqual(status, 403)
        self.assertFalse(CONTROL.paused)
        self.assertEqual(self.raw("/cover?asin=B000000000")[0], 403)

    def test_setup_icons_and_health_stay_reachable(self):
        status, _, body = self.raw("/setup")
        self.assertEqual(status, 200)
        for needle in (b'name="username"', b'name="password"', b'name="confirm"', b'action="/setup"', b"--ab-bg"):
            self.assertIn(needle, body)
        self.assertIn(self.raw("/favicon.svg")[0], (200, 404))
        self.assertIn(self.raw("/healthz")[0], (200, 503))

    def test_setup_creates_the_account_and_signs_in(self):
        status, headers, _ = self.form("/setup", {"username": f"  {USER} ", "password": PASSWORD, "confirm": PASSWORD})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Max-Age=604800"):
            self.assertIn(flag, cookie)
        jar = self.jar(headers)
        self.assertEqual(self.raw("/", headers=jar)[0], 200)
        self.assertIn(b"/logout", self.raw("/", headers=jar)[2])
        creds = self.state.get_credentials()
        self.assertEqual(creds.username, USER)  # trimmed
        self.assertTrue(auth_mod.verify_password(PASSWORD, creds))

    def test_setup_is_refused_afterwards(self):
        jar = self.setup_account()
        status, headers, _ = self.raw("/setup", headers=jar)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        status, headers, _ = self.raw("/setup")  # not signed in: goes to sign-in, never the form
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        status, headers, _ = self.form("/setup", {"username": "mallory", "password": "mallory-pass", "confirm": "mallory-pass"})
        self.assertEqual(status, 303)
        self.assertIsNone(headers.get("Set-Cookie"))
        status, _, body = self.json_post("/setup", {"username": "mallory", "password": "mallory-pass", "confirm": "mallory-pass"})
        self.assertEqual(status, 409)
        creds = self.state.get_credentials()
        self.assertEqual(creds.username, USER)
        self.assertTrue(auth_mod.verify_password(PASSWORD, creds))

    def test_setup_validation(self):
        cases = [
            ({"username": "", "password": PASSWORD, "confirm": PASSWORD}, b"Enter a username"),
            ({"username": "x" * 65, "password": PASSWORD, "confirm": PASSWORD}, b"at most 64"),
            ({"username": USER, "password": "short", "confirm": "short"}, b"at least 8"),
            ({"username": USER, "password": PASSWORD, "confirm": PASSWORD + "!"}, b"do not match"),
            ({"username": USER, "password": PASSWORD}, b"do not match"),
        ]
        for fields, message in cases:
            status, _, body = self.form("/setup", fields)
            self.assertEqual(status, 400, fields)
            self.assertIn(message, body)
        self.assertEqual(self.form("/setup", {"username": "x" * 64, "password": "12345678", "confirm": "12345678"})[0], 303)

    def test_setup_keeps_the_typed_username_escaped(self):
        status, _, body = self.form("/setup", {"username": '"><b>x', "password": "short", "confirm": "short"})
        self.assertEqual(status, 400)
        self.assertIn(b'value="&quot;&gt;&lt;b&gt;x"', body)

    def test_setup_refuses_cross_site_posts(self):
        status, _, _ = self.form(
            "/setup", {"username": USER, "password": PASSWORD, "confirm": PASSWORD}, {"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.state.get_credentials())

    def test_setup_still_needs_an_allowed_host(self):
        status, _, _ = self.form(
            "/setup", {"username": USER, "password": PASSWORD, "confirm": PASSWORD}, {"Host": "evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.state.get_credentials())


class LoginTests(AccountCase):
    def setUp(self):
        super().setUp()
        self.state.create_credentials(USER, auth_mod.hash_password(PASSWORD))

    def test_login_page_has_both_fields_required(self):
        status, _, body = self.raw("/login")
        self.assertEqual(status, 200)
        self.assertIn(b'name="username"', body)
        self.assertIn(b'name="password"', body)
        self.assertEqual(body.count(b"required"), 2)
        self.assertNotIn(b"AUTH_TOKEN", body)

    def test_login_success(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        jar = self.jar(headers)
        status, _, body = self.raw("/api/units", headers=jar)
        self.assertEqual(status, 200)
        self.assertIn(b"Author/Book", body)

    def test_json_login(self):
        status, headers, body = self.json_post("/login", {"username": USER, "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn(f"{auth_mod.COOKIE_NAME}=", headers["Set-Cookie"])

    def test_wrong_username_and_wrong_password_look_the_same(self):
        bad_user = self.login("someone-else", PASSWORD)
        bad_pass = self.login(USER, "wrong password")
        self.assertEqual(bad_user[0], 401)
        self.assertEqual(bad_pass[0], 401)
        self.assertIsNone(bad_user[1].get("Set-Cookie"))
        self.assertIsNone(bad_pass[1].get("Set-Cookie"))
        # the re-rendered form keeps what was typed in the username box; beyond that, identical
        def strip_typed(body):
            return re.sub(rb'name="username" type="text" value="[^"]*"', b"", body)

        self.assertEqual(strip_typed(bad_user[2]), strip_typed(bad_pass[2]))
        self.assertIn(b"That username or password is not right.", bad_user[2])
        j_user = self.json_post("/login", {"username": "nobody", "password": PASSWORD})
        j_pass = self.json_post("/login", {"username": USER, "password": "nope-nope"})
        self.assertEqual((j_user[0], j_user[2]), (j_pass[0], j_pass[2]))

    def test_same_hash_work_whichever_field_is_wrong(self):
        with mock.patch.object(auth_mod, "_pbkdf2", wraps=auth_mod._pbkdf2) as spy:
            self.login("someone-else", PASSWORD)
            after_bad_user = spy.call_count
            self.login(USER, "wrong password")
        self.assertEqual(after_bad_user, 1)
        self.assertEqual(spy.call_count, 2)

    def test_missing_fields_fail_generically(self):
        for fields in ({"username": USER}, {"password": PASSWORD}, {"token": PASSWORD}, {}):
            status, _, body = self.form("/login", fields)
            self.assertEqual(status, 401, fields)
            self.assertIn(b"not right", body)

    def test_lockout_still_fires(self):
        for i in range(5):
            self.assertEqual(self.login(USER if i % 2 else "nobody", "wrong password")[0], 401)
        status, headers, body = self.login()  # even the right password waits out the lock
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIn(b"Too many failed sign-ins", body)

    def test_api_without_credentials_is_401(self):
        status, headers, body = self.raw("/api/units")
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        self.assertEqual(self.raw("/")[0], 303)

    def test_logout(self):
        _, headers, _ = self.login()
        jar = self.jar(headers)
        status, headers, _ = self.raw("/logout", headers=jar)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.assertEqual(self.raw("/api/state", headers=jar)[0], 401)


class BearerTests(AccountCase):
    def setUp(self):
        super().setUp()
        self.state.create_credentials(USER, auth_mod.hash_password(PASSWORD))

    def test_bearer_accepts_the_account_password(self):
        status, _, body = self.raw("/api/units", headers={"Authorization": f"Bearer {PASSWORD}"})
        self.assertEqual(status, 200)
        self.assertIn(b"Author/Book", body)
        status, _, _ = self.raw(
            "/api/control",
            json.dumps({"action": "pause"}).encode(),
            {"Authorization": f"Bearer {PASSWORD}", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)

    def test_wrong_bearer_is_401(self):
        status, headers, _ = self.raw("/api/units", headers={"Authorization": "Bearer not-it"})
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        self.assertEqual(self.raw("/api/units", headers={"Authorization": f"Bearer {USER}"})[0], 401)

    def test_bearer_not_accepted_for_pages(self):
        self.assertEqual(self.raw("/", headers={"Authorization": f"Bearer {PASSWORD}"})[0], 303)

    def test_wrong_bearers_count_towards_the_lockout(self):
        for _ in range(5):
            self.raw("/api/units", headers={"Authorization": "Bearer guess"})
        self.assertEqual(self.raw("/api/units", headers={"Authorization": f"Bearer {PASSWORD}"})[0], 429)

    def test_repeat_bearer_does_not_rehash(self):
        self.assertEqual(self.raw("/api/state", headers={"Authorization": f"Bearer {PASSWORD}"})[0], 200)
        with mock.patch.object(auth_mod, "_pbkdf2", wraps=auth_mod._pbkdf2) as spy:
            for _ in range(3):
                self.assertEqual(self.raw("/api/state", headers={"Authorization": f"Bearer {PASSWORD}"})[0], 200)
        self.assertEqual(spy.call_count, 0)

    def test_auth_token_is_not_a_bearer_once_an_account_exists(self):
        CONFIG.auth_token = TOKEN
        self.assertEqual(self.raw("/api/units", headers={"Authorization": f"Bearer {TOKEN}"})[0], 401)
        self.assertEqual(self.raw("/api/units", headers={"Authorization": f"Bearer {PASSWORD}"})[0], 200)


class LegacyTokenTests(AccountCase):
    def setUp(self):
        super().setUp()
        CONFIG.auth_token = TOKEN

    def test_setup_is_skipped(self):
        status, headers, _ = self.raw("/")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        status, headers, _ = self.raw("/setup")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertIn(b"AUTH_TOKEN", self.raw("/login")[2])

    def test_setup_post_cannot_claim_a_token_protected_install(self):
        status, _, _ = self.json_post("/setup", {"username": "mallory", "password": "mallory-pass", "confirm": "mallory-pass"})
        self.assertEqual(status, 409)
        status, headers, _ = self.form("/setup", {"username": "mallory", "password": "mallory-pass", "confirm": "mallory-pass"})
        self.assertEqual(status, 303)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIsNone(self.state.get_credentials())

    def test_admin_or_any_username_with_the_token(self):
        for name in ("admin", "whoever"):
            status, headers, _ = self.login(name, TOKEN)
            self.assertEqual(status, 303, name)
            self.assertEqual(self.raw("/", headers=self.jar(headers))[0], 200)
        self.assertEqual(self.login("", TOKEN)[0], 401)  # both fields required
        self.assertEqual(self.login("admin", "not the token")[0], 401)

    def test_bearer_token_still_works(self):
        self.assertEqual(self.raw("/api/units", headers={"Authorization": f"Bearer {TOKEN}"})[0], 200)

    def test_credentials_take_precedence_once_they_exist(self):
        _, headers, _ = self.login("admin", TOKEN)
        token_jar = self.jar(headers)
        self.assertEqual(self.raw("/", headers=token_jar)[0], 200)
        self.state.create_credentials(USER, auth_mod.hash_password(PASSWORD))  # e.g. reset-password CLI
        self.assertEqual(self.raw("/", headers=token_jar)[0], 303)  # the token session is over
        self.assertEqual(self.login("admin", TOKEN)[0], 401)
        self.assertEqual(self.login(USER, TOKEN)[0], 401)
        self.assertEqual(self.form("/login", {"token": TOKEN})[0], 401)
        self.assertEqual(self.raw("/api/units", headers={"Authorization": f"Bearer {TOKEN}"})[0], 401)
        self.assertEqual(self.login()[0], 303)
        self.assertNotIn(b"AUTH_TOKEN", self.raw("/login")[2])

    def test_settings_offers_to_create_the_account(self):
        _, headers, _ = self.login("admin", TOKEN)
        jar = self.jar(headers)
        page = self.raw("/settings", headers=jar)[2].decode()
        self.assertIn('data-acct="create"', page)
        self.assertNotIn('data-acct="password"', page)
        status, _, body = self.account(
            {"action": "create", "username": USER, "new_password": PASSWORD, "confirm": PASSWORD, "current": "wrong"}, jar
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.state.get_credentials())
        status, _, body = self.account(
            {"action": "create", "username": USER, "new_password": PASSWORD, "confirm": PASSWORD, "current": TOKEN}, jar
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["username"], USER)
        self.assertEqual(self.raw("/", headers=jar)[0], 200)  # this browser stays in
        self.assertEqual(self.login("admin", TOKEN)[0], 401)
        self.assertEqual(self.login()[0], 303)


class SettingsAccountTests(AccountCase):
    def setUp(self):
        super().setUp()
        self.jar_a = self.setup_account()
        _, headers, _ = self.login()
        self.jar_b = self.jar(headers)

    def test_settings_page_has_the_account_section(self):
        page = self.raw("/settings", headers=self.jar_a)[2].decode()
        self.assertIn('class="ab-acct"', page)
        self.assertIn(f'<b id="ab-acct-user">{USER}</b>', page)
        self.assertIn('data-acct="password"', page)
        self.assertIn('data-acct="username"', page)
        self.assertIn("audiobind reset-password", page)
        self.assertIn("function abAccountSubmit", page)

    def test_wrong_current_password_is_rejected(self):
        status, _, body = self.account(
            {"action": "password", "current": "not it", "new_password": "brand-new-pass", "confirm": "brand-new-pass"},
            self.jar_a,
        )
        self.assertEqual(status, 403)
        self.assertIn("current password", body["error"])
        self.assertTrue(auth_mod.verify_password(PASSWORD, self.state.get_credentials()))
        self.assertEqual(self.raw("/", headers=self.jar_b)[0], 200)  # nothing was ended

    def test_wrong_current_passwords_count_towards_the_lockout(self):
        for _ in range(5):
            self.account({"action": "username", "current": "guess", "username": "x"}, self.jar_a)
        status, headers, _ = self.account({"action": "username", "current": PASSWORD, "username": "x"}, self.jar_a)
        self.assertEqual(status, 429)
        self.assertEqual(self.state.get_credentials().username, USER)

    def test_change_password_keeps_this_session_and_ends_the_others(self):
        status, _, body = self.account(
            {"action": "password", "current": PASSWORD, "new_password": "brand-new-pass", "confirm": "brand-new-pass"},
            self.jar_a,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.raw("/", headers=self.jar_a)[0], 200)
        self.assertEqual(self.raw("/api/state", headers=self.jar_a)[0], 200)
        self.assertEqual(self.raw("/", headers=self.jar_b)[0], 303)
        self.assertEqual(self.raw("/api/state", headers=self.jar_b)[0], 401)
        self.assertEqual(self.login(USER, PASSWORD)[0], 401)
        self.assertEqual(self.login(USER, "brand-new-pass")[0], 303)

    def test_change_password_ends_cached_bearers(self):
        self.assertEqual(self.raw("/api/state", headers={"Authorization": f"Bearer {PASSWORD}"})[0], 200)
        self.account(
            {"action": "password", "current": PASSWORD, "new_password": "brand-new-pass", "confirm": "brand-new-pass"},
            self.jar_a,
        )
        self.assertEqual(self.raw("/api/state", headers={"Authorization": f"Bearer {PASSWORD}"})[0], 401)
        self.assertEqual(self.raw("/api/state", headers={"Authorization": "Bearer brand-new-pass"})[0], 200)

    def test_new_password_rules(self):
        for new, confirm in (("short", "short"), ("long-enough-1", "long-enough-2")):
            status, _, _ = self.account(
                {"action": "password", "current": PASSWORD, "new_password": new, "confirm": confirm}, self.jar_a
            )
            self.assertEqual(status, 400)
        self.assertTrue(auth_mod.verify_password(PASSWORD, self.state.get_credentials()))
        self.assertEqual(self.raw("/", headers=self.jar_b)[0], 200)

    def test_change_username_needs_the_current_password(self):
        status, _, _ = self.account({"action": "username", "username": "renamed"}, self.jar_a)
        self.assertEqual(status, 403)
        status, _, body = self.account({"action": "username", "username": " renamed ", "current": PASSWORD}, self.jar_a)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["username"], "renamed")
        self.assertEqual(self.raw("/", headers=self.jar_a)[0], 200)
        self.assertEqual(self.raw("/", headers=self.jar_b)[0], 303)
        self.assertEqual(self.login(USER, PASSWORD)[0], 401)
        self.assertEqual(self.login("renamed", PASSWORD)[0], 303)
        status, _, _ = self.account({"action": "username", "username": "", "current": PASSWORD}, self.jar_a)
        self.assertEqual(status, 400)

    def test_create_is_refused_when_an_account_exists(self):
        status, _, _ = self.account(
            {"action": "create", "username": "x", "new_password": "12345678", "confirm": "12345678", "current": PASSWORD},
            self.jar_a,
        )
        self.assertEqual(status, 409)

    def test_account_endpoint_keeps_the_cross_site_guard(self):
        body = json.dumps({"action": "username", "username": "evil", "current": PASSWORD}).encode()
        status, _, _ = self.raw("/api/account", body, {**self.jar_a, "Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.raw(
            "/api/account", body, {**self.jar_a, "Content-Type": "application/json", "Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.state.get_credentials().username, USER)

    def test_account_endpoint_needs_sign_in(self):
        status, _, _ = self.json_post("/api/account", {"action": "username", "username": "x", "current": PASSWORD})
        self.assertEqual(status, 401)
        self.assertEqual(self.state.get_credentials().username, USER)


class NoLeakTests(AccountCase):
    def test_passwords_and_hashes_never_reach_logs_or_responses(self):
        new_password = "second password 2"
        root = logging.getLogger()
        with self.assertLogs(level=logging.DEBUG) as logs:
            jar = self.setup_account()
            self.login(USER, "a wrong password")
            self.login()
            self.raw("/api/units", headers={"Authorization": f"Bearer {PASSWORD}"})
            self.raw("/api/units", headers={"Authorization": "Bearer a wrong bearer"})
            self.account(
                {"action": "password", "current": PASSWORD, "new_password": new_password, "confirm": new_password}, jar
            )
            bodies = [
                self.raw("/api/state", headers=jar)[2],
                self.raw("/api/units", headers=jar)[2],
                self.raw("/settings", headers=jar)[2],
                self.raw("/", headers=jar)[2],
            ]
            logging.getLogger("audiobind").info("probe")  # assertLogs needs one record at least
        self.assertIs(logging.getLogger(), root)
        creds = self.state.get_credentials()
        text = "\n".join(logs.output) + "\n" + b"\n".join(bodies).decode("utf-8", "replace")
        for secret in (PASSWORD, new_password, "a wrong password", "a wrong bearer", creds.password_hash, creds.salt):
            self.assertNotIn(secret, text)
        self.assertIn("account created", "\n".join(logs.output))

    def test_each_hash_has_its_own_salt(self):
        self.setup_account()
        first = self.state.get_credentials()
        jar = self.jar(self.login()[1])
        self.account({"action": "password", "current": PASSWORD, "new_password": PASSWORD, "confirm": PASSWORD}, jar)
        second = self.state.get_credentials()
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.password_hash, second.password_hash)
        self.assertNotIn(PASSWORD, first.password_hash + second.password_hash)


class RenderTests(AccountCase):
    def test_signout_link_once_an_account_exists(self):
        self.assertNotIn("/logout", web.render(self.state))
        self.state.create_credentials(USER, auth_mod.hash_password(PASSWORD))
        self.assertIn("/logout", web.render(self.state))
        self.assertIn("/logout", web.render_settings(self.state))

    def test_pages_use_numeric_refs_and_own_classes(self):
        setup = web.render_setup()
        self.assertIn("&#183;", setup)
        self.assertIn('class="ab-setup"', setup)
        self.assertNotIn('class="ab-login"', setup)
        self.state.create_credentials(USER, auth_mod.hash_password(PASSWORD))
        section = web.render_account_section(self.state)
        self.assertNotIn("ab-setting-label", section)
        self.assertIn('class="ab-acct-form"', section)
