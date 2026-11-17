"""DOWNLOAD_CLIENT: the default changes nothing, a typo stops the start, the choice and the other
clients' settings round-trip on the Settings page (secrets never reach the browser), and a client
that cannot even be built degrades to "not available" instead of raising into the watcher."""

import json
import os
import re
from unittest import mock

from audiobind import config, qbit, watcher, web
from audiobind.clients import base
from audiobind.clients.deluge import DelugeClient
from audiobind.clients.rtorrent import RTorrentClient
from audiobind.clients.transmission import TransmissionClient
from audiobind.config import CONFIG

try:
    from test_watcher_gui_settings import WatcherSettingsCase
except ImportError:
    from tests.test_watcher_gui_settings import WatcherSettingsCase


class TestConfig(WatcherSettingsCase):
    def test_the_default_is_qbittorrent_and_every_new_setting_is_empty(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("DOWNLOAD_CLIENT", "DELUGE_",
                                                                             "TRANSMISSION_", "RTORRENT_"))}
        with mock.patch.dict(os.environ, clean, clear=True):
            loaded = config.Config.from_env()
        self.assertEqual(loaded.download_client, "qbittorrent")
        for key in config.CLIENT_SETTING_KEYS[1:]:
            self.assertEqual(getattr(loaded, key), "", key)

    def test_a_typo_stops_the_start(self):
        with mock.patch.dict(os.environ, {"DOWNLOAD_CLIENT": "qbitorrent"}):
            with self.assertRaisesRegex(RuntimeError, "DOWNLOAD_CLIENT is not valid"):
                config.Config.from_env()
        with mock.patch.dict(os.environ, {"DOWNLOAD_CLIENT": " Deluge "}):
            self.assertEqual(config.Config.from_env().download_client, "deluge")

    def test_passwords_are_kept_out_of_repr(self):
        loaded = config.Config(deluge_pass="DP-1", transmission_pass="TP-2", rtorrent_pass="RP-3")
        for secret in ("DP-1", "TP-2", "RP-3"):
            self.assertNotIn(secret, repr(loaded))


class TestDefaultIsUnchanged(WatcherSettingsCase):
    def test_make_client_is_make_qbit(self):
        self.assertEqual(watcher.client_kind(), "qbittorrent")
        self.assertEqual(watcher.client_label(), "qBittorrent")
        client = watcher.make_client()
        self.assertIsInstance(client, qbit.QbitClient)
        self.assertEqual(client.url, "http://env-qbit:8080")
        sentinel = object()
        with mock.patch.object(watcher, "make_qbit", return_value=sentinel):
            self.assertIs(watcher.make_client(), sentinel, "a patched make_qbit is still what the watcher uses")

    def test_page_shows_qbittorrent_selected_and_the_others_hidden(self):
        page = self.page()
        self.assertIn('<div class="ab-acct-title">Download client</div>', page)
        self.assertIn('<option value="qbittorrent" selected>qBittorrent</option>', page)
        for kind in ("deluge", "transmission", "rtorrent"):
            self.assertIn(f'<div class="ab-dc" data-client="{kind}" hidden>', page)
        self.assertIn('<div class="ab-dc" data-client="qbittorrent">', page)
        self.assertIn(">Test connection</button>", page)
        self.assertIn(">Test qBittorrent</button>", page)


class TestChoosingAnotherClient(WatcherSettingsCase):
    def test_each_client_is_built_from_its_own_saved_settings(self):
        cases = {
            "deluge": ({"deluge_url": "http://deluge:8112", "deluge_pass": "dpw"}, DelugeClient, "http://deluge:8112"),
            "transmission": ({"transmission_url": "http://tr:9091", "transmission_user": "u",
                              "transmission_pass": "tpw"}, TransmissionClient, "http://tr:9091/transmission/rpc"),
            "rtorrent": ({"rtorrent_url": "scgi://rt:5000"}, RTorrentClient, "scgi://rt:5000"),
        }
        for kind, (settings, cls, url) in cases.items():
            with self.subTest(kind=kind):
                status, body, _raw = self.save({"settings": {"download_client": kind, **settings}})
                self.assertEqual(status, 200, body)
                client = watcher.make_client()
                self.assertIsInstance(client, cls)
                self.assertEqual(client.url, url)
                self.assertEqual(watcher.client_label(), config.DOWNLOAD_CLIENTS[kind])
                page = self.page()
                self.assertIn(f'<option value="{kind}" selected>', page)
                self.assertIn(f'<div class="ab-dc" data-client="{kind}">', page)
                self.assertIn('<div class="ab-dc" data-client="qbittorrent" hidden>', page)
                self.assertIn(f">Test {config.DOWNLOAD_CLIENTS[kind]}</button>", page)

    def test_secrets_never_reach_the_page(self):
        self.save({"settings": {"download_client": "deluge", "deluge_pass": "UNIQUE-DELUGE-PW",
                                "transmission_pass": "UNIQUE-TR-PW", "rtorrent_pass": "UNIQUE-RT-PW"}})
        page = self.page()
        for secret in ("UNIQUE-DELUGE-PW", "UNIQUE-TR-PW", "UNIQUE-RT-PW"):
            self.assertNotIn(secret, page)
        self.assertEqual(config.setting_effective("deluge_pass"), "UNIQUE-DELUGE-PW")
        status, _body, _raw = self.save({"clear": ["deluge_pass"]})
        self.assertEqual(status, 200)
        self.assertEqual(config.setting_effective("deluge_pass"), "")

    def test_bad_values_are_refused_with_plain_messages(self):
        for settings, message in (({"download_client": "utorrent"}, "download_client: must be one of"),
                                  ({"rtorrent_url": "scgi://rt"}, "rTorrent URL:"),
                                  ({"deluge_url": "ftp://deluge"}, "Deluge URL:"),
                                  ({"transmission_url": "http://u:p@tr:9091"}, "Transmission URL:")):
            with self.subTest(settings=settings):
                status, body, _raw = self.save({"settings": settings})
                self.assertEqual(status, 400)
                self.assertIn(message, body["error"])

    def test_disabled_reason_names_the_chosen_clients_settings(self):
        self.save({"settings": {"download_client": "deluge"}})
        self.assertEqual(watcher.disabled_reason(),
                         "DELUGE_URL / DELUGE_PASS are not set - the watcher has nowhere to send a torrent")
        self.save({"settings": {"download_client": "transmission", "transmission_url": "http://tr:9091"}})
        self.assertEqual(watcher.disabled_reason(), "", "Transmission may run without a login")


class TestUnusableClientDegrades(WatcherSettingsCase):
    def test_no_address_is_not_available_never_a_raise(self):
        self.save({"settings": {"download_client": "rtorrent"}})
        client = watcher.make_client()
        for call in (client.login, client.version, lambda: client.progress(["a" * 40]),
                     lambda: client.incomplete("c", 0.5), lambda: client.snatch_state("c"),
                     lambda: client.add_torrent(b"d4:infod1:ai1eee")):
            with self.assertRaisesRegex(base.ClientError, "RTORRENT_URL is not set"):
                call()
        client.logout()  # never raises
        self.assertEqual(watcher.probe_qbit(), (False, "RTORRENT_URL is not set"))
        with self.assertRaises(watcher._QbitDown) as ctx:
            watcher.WATCHER._qbit({})
        self.assertIn("rTorrent is not available (RTORRENT_URL is not set)", str(ctx.exception))

    def test_unreachable_test_connection_route_fails_closed(self):
        self.save({"settings": {"download_client": "transmission", "transmission_url": "http://127.0.0.1:9",
                                "transmission_pass": "UNIQUE-TR-9"}})
        status, _h, body = self.call("/api/watcher/test-qbit", {})
        self.assertEqual(status, 502)
        answer = json.loads(body)
        self.assertIs(answer["ok"], False)
        self.assertIn("Transmission at 127.0.0.1:9 could not be reached", answer["message"])
        self.assertNotIn("UNIQUE-TR-9", body)

    def test_the_status_row_names_the_client(self):
        self.save({"settings": {"download_client": "deluge", "deluge_url": "http://deluge:8112"}})
        row = re.search(r"<dt>Deluge</dt><dd>(.*?)</dd>", web.render_watcher_section(self.state))
        self.assertIsNotNone(row)
        self.assertIn("deluge:8112", row.group(1))
