import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from audiobind import cli, drift
from audiobind.config import CONFIG
from tests.live import LiveServerCase
from tests.test_library_drift import DriftCase


class TestDriftCLI(DriftCase):
    def test_cli_drift_json_matches_report_without_initializing_state(self):
        (self.lib / "untracked").mkdir()
        stream = io.StringIO()
        before = self.snapshot()
        with (
            patch.object(CONFIG, "state_dir", self.state.path.parent),
            patch.object(cli, "setup_logging", side_effect=AssertionError("audit must not open a log")),
            redirect_stdout(stream),
        ):
            self.assertEqual(cli.main(["library", "drift", "--json", "--limit", "0"]), 0)
        self.assertEqual(json.loads(stream.getvalue()), drift.report(self.state, limit=0))
        self.assertEqual(self.snapshot(), before)


class TestDriftRoute(LiveServerCase):
    def test_get_drift_schema_and_post_refusal(self):
        (self.lib / "untracked").mkdir()
        status, body = self.request("/api/library/drift?limit=0")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"missing_sources", "missing_outputs", "orphans", "truncated"})
        self.assertEqual(body, drift.report(self.state, limit=0))
        self.assertEqual(self.request("/api/library/drift?limit=-1")[0], 400)
        self.assertEqual(self.post({}, "/api/library/drift")[0], 405)


class TestCoversCLI(DriftCase):
    def test_cli_cover_report_json_without_initializing_state(self):
        stream = io.StringIO()
        before = self.snapshot()
        with (
            patch.object(CONFIG, "state_dir", self.state.path.parent),
            patch.object(cli, "setup_logging", side_effect=AssertionError("audit must not open a log")),
            redirect_stdout(stream),
        ):
            self.assertEqual(cli.main(["covers", "audit", "--json"]), 0)
        self.assertEqual(json.loads(stream.getvalue()), {
            "counts": {"ok": 0, "missing": 0, "too_small": 0, "not_square": 0, "unreadable": 0},
            "offenders": [], "duplicates": [],
        })
        self.assertEqual(self.snapshot(), before)


class TestCoversRoute(LiveServerCase):
    def test_get_cover_schema_and_post_refusal(self):
        status, body = self.request("/api/library/covers")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"counts", "offenders", "duplicates"})
        self.assertEqual(self.post({}, "/api/library/covers")[0], 405)


class TestAuditCards(LiveServerCase):
    def test_cards_link_to_reports_in_library_and_review(self):
        for path in ("/?status=all", "/?status=attention"):
            status, body = self.request(path, raw=True)
            self.assertEqual(status, 200)
            for name, url in ((b"drift", b"drift"), (b"covers", b"covers")):
                card = body.split(b'id="ab-audit-' + name + b'"', 1)
                self.assertEqual(len(card), 2, path)
                self.assertIn(b'href="/api/library/' + url + b'"', card[1].split(b"</section>", 1)[0])
