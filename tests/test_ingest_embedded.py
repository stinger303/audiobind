"""0.16.0-dev3: an ingest unit whose cleaned name matches nothing retries on its embedded tags.

A single m4b torrent often has a junk name but correct title/artist tags. The fallback runs
only for ingest units, only when the folder match is NONE, and the detail records which name
source won ("folder" or "embedded").
"""

import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from audiobind import catalog, ingest, pipeline, probe, web
from audiobind.catalog import Product
from audiobind.config import CONFIG
from audiobind.state import State, UnitState
from audiobind.units import BookUnit, Part

try:
    from test_ingest_discovery import touch, tree_snapshot
    from test_ingest_pipeline import PipelineCase
    from test_match_evidence import evidence_detail
except ImportError:
    from tests.test_ingest_discovery import touch, tree_snapshot
    from tests.test_ingest_pipeline import PipelineCase
    from tests.test_match_evidence import evidence_detail

JUNK = "01 Untitled Upload [CA].m4b"
JUNK_KEY = "ingest:" + JUNK
GUILTY = Product(asin="B07GUILTY1", title="The Guilty", authors=["David Baldacci"], hours=2.0, language="english")
GUILTY_TAGS = {"title": "The Guilty (Unabridged)", "artist": "David Baldacci; Kyf Brewer", "album": "The Guilty"}


def search_only(product, title_word):
    """A fake catalog.search that knows one book, found only by a title containing ``title_word``."""

    def fake(title=None, author=None, limit=10):
        return [product] if title and title_word.lower() in title.lower() else []

    return fake


def ffprobe_result(payload):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


class TestProbeTags(unittest.TestCase):
    def test_tags_are_lowercased_and_format_wins_over_stream(self):
        payload = {
            "streams": [{"tags": {"TITLE": "Stream Title", "language": "eng"}}],
            "format": {"tags": {"title": "Format Title", "ARTIST": "Some Author", "empty": "  "}},
        }
        with mock.patch.object(probe.subprocess, "run", return_value=ffprobe_result(payload)) as run:
            tags = probe.probe_tags("/x/book.m4b")
        self.assertEqual(tags, {"title": "Format Title", "language": "eng", "artist": "Some Author"})
        # untrusted media: the protocol whitelist goes in front of the input, like every probe
        argv = run.call_args.args[0]
        self.assertIn("-protocol_whitelist", argv)
        self.assertEqual(argv[-1], "/x/book.m4b")

    def test_failures_are_empty(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=OSError("no ffprobe")):
            self.assertEqual(probe.probe_tags("/x"), {})
        with mock.patch.object(probe.subprocess, "run", return_value=ffprobe_result({})):
            self.assertEqual(probe.probe_tags("/x"), {})
        bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="not json", stderr="")
        with mock.patch.object(probe.subprocess, "run", return_value=bad):
            self.assertEqual(probe.probe_tags("/x"), {})


class TestEmbeddedNames(unittest.TestCase):
    def unit(self, n_files=1):
        files = [f"/ingest/t/{i:02d}.mp3" for i in range(n_files)]
        return BookUnit(path="/ingest/t", name="t", parent="/ingest", parts=[Part(name="t", files=files)])

    def test_single_file_uses_title_and_first_artist_cleaned(self):
        with mock.patch.object(ingest, "probe_tags", return_value=dict(GUILTY_TAGS)) as tags:
            self.assertEqual(ingest.embedded_names(self.unit()), ("David Baldacci", "The Guilty"))
        tags.assert_called_once_with("/ingest/t/00.mp3")

    def test_several_files_prefer_the_album(self):
        with mock.patch.object(ingest, "probe_tags", return_value={"title": "Chapter 01", "album": "Artemis",
                                                                   "album_artist": "Andy Weir",
                                                                   "artist": "Narrator"}):
            self.assertEqual(ingest.embedded_names(self.unit(3)), ("Andy Weir", "Artemis"))

    def test_composer_is_the_last_resort_and_nothing_is_empty(self):
        with mock.patch.object(ingest, "probe_tags", return_value={"composer": "Jane Doe"}):
            self.assertEqual(ingest.embedded_names(self.unit()), ("Jane Doe", ""))
        with mock.patch.object(ingest, "probe_tags", return_value={}):
            self.assertEqual(ingest.embedded_names(self.unit()), ("", ""))
        empty = BookUnit(path="/x", name="x", parent="/", parts=[])
        self.assertEqual(ingest.embedded_names(empty), ("", ""))


class TestEmbeddedFallback(PipelineCase):
    def setUp(self):
        super().setUp()
        self.tags = mock.patch.object(ingest, "probe_tags", return_value=dict(GUILTY_TAGS))
        self.tag_probe = self.tags.start()
        self.addCleanup(self.tags.stop)

    def detail(self, key):
        return json.loads(self.state.get(key).detail)

    def test_a_junk_name_matches_on_embedded_tags_and_converts(self):
        touch(str(self.ingest_root / JUNK))
        before = tree_snapshot(self.ingest_root)
        summary = self.scan_with(search_only(GUILTY, "guilty"))
        row = self.state.get(JUNK_KEY)
        self.assertEqual(row.asin, "B07GUILTY1")
        self.assertEqual(row.confidence, "HIGH")
        self.assertEqual(row.status, "converted")
        self.assertEqual(summary["converted"], 1)
        match = self.detail(JUNK_KEY)["match"]
        self.assertEqual(match["name_source"], "embedded")
        self.assertEqual(match["query_title"], "The Guilty")
        self.assertEqual(match["query_author"], "David Baldacci")
        self.assertIn("embedded tags", row.note)
        self.assertNotIn("author taken from catalog", row.note)
        self.tag_probe.assert_called_once()
        self.assertEqual(tree_snapshot(self.ingest_root), before)  # tags are read, never written

    def scan_with(self, search):
        """scan(), but catalog.search answers through ``search`` instead of a fixed list."""
        return self.scan(hours_per_file=7200.0, search=search)

    def test_not_used_when_the_folder_name_matches(self):
        # the folder name finds a book (any tier above NONE): tags are never even read
        self.add_torrent()
        self.scan()
        key = "ingest:" + "Andy Weir - Project Hail Mary (2021) [MAM]"
        self.assertEqual(self.detail(key)["match"]["name_source"], "folder")
        self.assertEqual(self.state.get(key).status, "converted")
        self.tag_probe.assert_not_called()

    def test_not_used_when_the_folder_match_is_low(self):
        weak = Product(asin="B0WEAK0001", title="Project Something Else", authors=["Other Person"],
                       hours=2.0, language="english")
        self.add_torrent()
        self.scan(products=(weak,))
        key = "ingest:" + "Andy Weir - Project Hail Mary (2021) [MAM]"
        row = self.state.get(key)
        self.assertNotEqual(row.confidence, "NONE")
        self.assertEqual(self.detail(key)["match"]["name_source"], "folder")
        self.tag_probe.assert_not_called()

    def test_embedded_that_also_finds_nothing_keeps_the_folder_result(self):
        touch(str(self.ingest_root / JUNK))
        self.scan_with(lambda **_kw: [])
        row = self.state.get(JUNK_KEY)
        self.assertEqual(row.status, "review")
        self.assertEqual(row.confidence, "NONE")
        match = self.detail(JUNK_KEY)["match"]
        self.assertEqual(match["name_source"], "folder")
        self.assertEqual(match["query_title"], "Untitled Upload")  # the cleaned folder name
        self.tag_probe.assert_called_once()

    def test_tags_that_repeat_the_folder_name_do_not_search_again(self):
        touch(str(self.ingest_root / "Untitled Upload.m4b"))
        self.tag_probe.return_value = {"title": "Untitled Upload"}
        searches = []
        self.scan_with(lambda **kw: searches.append(kw) or [])
        self.assertTrue(searches)
        self.assertTrue(all(q.get("title") == "Untitled Upload" for q in searches if "title" in q))
        self.assertEqual(self.detail("ingest:Untitled Upload.m4b")["match"]["name_source"], "folder")

    def test_no_tags_means_review_as_before(self):
        touch(str(self.ingest_root / JUNK))
        self.tag_probe.return_value = {}
        self.scan_with(lambda **_kw: [])
        self.assertEqual(self.state.get(JUNK_KEY).status, "review")
        self.assertEqual(self.detail(JUNK_KEY)["match"]["name_source"], "folder")

    def test_a_failed_asin_pin_never_falls_back(self):
        touch(str(self.ingest_root / JUNK))
        self.state.set_asin_override(JUNK_KEY, "B0DEADPIN1")
        with mock.patch.object(catalog, "book", return_value={}):
            unit = ingest.find_any_unit(JUNK_KEY)
            with mock.patch.object(catalog, "search", side_effect=search_only(GUILTY, "guilty")):
                audit = pipeline.audit_unit(unit, "B0DEADPIN1")
        self.assertEqual(audit.match.asin_pin_failed, "B0DEADPIN1")
        self.assertEqual(audit.name_source, "folder")
        self.tag_probe.assert_not_called()

    def test_library_units_never_fall_back(self):
        touch(str(CONFIG.library_root / "Untitled Upload" / "01.mp3"))
        CONFIG.ingest_dir = ""
        self.scan_with(search_only(GUILTY, "guilty"))
        row = self.state.get("Untitled Upload")
        self.assertEqual(row.source, "library")
        self.assertEqual(row.confidence, "NONE")
        self.assertEqual(self.detail("Untitled Upload")["match"]["name_source"], "folder")
        self.tag_probe.assert_not_called()


class TestEmbeddedSourceInThePanel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def page_for(self, **match_kw):
        self.state.save(
            UnitState(path="ingest:Junk", status="review", confidence="LOW", asin="B0FD4CV4BF",
                      title="The Power of Habit", detail=evidence_detail(**match_kw), hours=10.5)
        )
        return web.render(self.state)

    def test_an_embedded_match_says_so(self):
        self.assertIn("embedded tags, not the torrent name", self.page_for(name_source="embedded"))

    def test_a_folder_match_does_not(self):
        self.assertNotIn("embedded tags", self.page_for(name_source="folder"))
        self.assertNotIn("embedded tags", self.page_for())  # rows recorded before dev3


if __name__ == "__main__":
    unittest.main()
