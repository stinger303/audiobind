"""0.19.0-dev14: Settings > Library "Match Audiobookshelf layout" - previewed, journaled moves."""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import abs_layout, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def detail(authors, series="", sequence="", title="", output="", overrides=None):
    data = {"match": {"authors": authors, "series": series, "sequence": sequence, "title": title}}
    if output:
        data["output"] = {"path": output}
    if overrides:
        data["overrides"] = overrides
    return json.dumps(data)


def tree(root: pathlib.Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class LayoutCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.out = base / "output"
        self.out.mkdir()
        self.cfg = base / "config"
        self.cfg.mkdir()
        self.saved = (CONFIG.output_root, CONFIG.state_dir)
        CONFIG.output_root, CONFIG.state_dir = self.out, self.cfg
        self.state = State(base / "state")

    def tearDown(self):
        CONFIG.output_root, CONFIG.state_dir = self.saved
        self.state.close()
        self.tmp.cleanup()

    def book(self, key, rel, authors, title, series="", sequence="", status="converted", extra=(), overrides=None):
        """A published book at OUTPUT_ROOT/rel (the main file) plus its row."""
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        for name in extra:
            (path.parent / name).write_bytes(b"x")
        self.state.save(UnitState(path=key, status=status, title=title, author=(authors or [""])[0],
                                  output=str(path), detail=detail(authors, series, sequence, title, str(path),
                                                                  overrides)))
        return path

    def journals(self):
        return sorted(self.cfg.glob("abs-layout-*.json"))


class TestPlan(LayoutCase):
    def test_dry_run_writes_nothing(self):
        self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3", extra=("cover.jpg",))
        before, row_before = tree(self.out), self.state.get("a")
        result = abs_layout.plan(self.state)
        self.assertEqual([m.public() for m in result["moves"]], [{
            "from": str(self.out / "Author/Saga/03 - Book/Book.m4b"),
            "to": str(self.out.resolve() / "Author/Saga/3 - Book/Book.m4b"),
            "state_key": "a"}])
        self.assertEqual(tree(self.out), before)
        self.assertEqual(self.state.get("a"), row_before)
        self.assertEqual(self.journals(), [])
        self.assertEqual(list(self.cfg.iterdir()), [])

    def test_already_correct_rows_are_no_ops(self):
        self.book("a", "Author/Saga/3 - Book/Book.m4b", ["Author"], "Book", "Saga", "3")
        self.book("b", "Author/Solo/Solo.m4b", ["Author"], "Solo")
        result = abs_layout.plan(self.state)
        self.assertEqual(result["moves"], [])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["unchanged"], 2)
        out = abs_layout.apply(self.state)
        self.assertEqual(out["moved"], [])
        self.assertEqual(out["journal"], "")
        self.assertEqual(self.journals(), [])

    def test_only_converted_rows_are_considered(self):
        self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3", status="ready")
        self.assertEqual(abs_layout.plan(self.state)["moves"], [])

    def test_skip_without_output(self):
        self.state.save(UnitState(path="a", status="converted", title="Book", detail=detail(["Author"])))
        skipped = abs_layout.plan(self.state)["skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("no published file", skipped[0]["reason"])

    def test_skip_missing_source(self):
        path = self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3")
        path.unlink()
        skipped = abs_layout.plan(self.state)["skipped"]
        self.assertEqual([s["from"] for s in skipped], [str(path)])
        self.assertIn("missing", skipped[0]["reason"])

    def test_skip_outside_output_root(self):
        elsewhere = pathlib.Path(self.tmp.name) / "elsewhere" / "Book" / "Book.m4b"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_bytes(b"audio")
        self.state.save(UnitState(path="a", status="converted", title="Book", output=str(elsewhere),
                                  detail=detail(["Author"], "Saga", "1", "Book")))
        skipped = abs_layout.plan(self.state)["skipped"]
        self.assertIn("not inside OUTPUT_ROOT", skipped[0]["reason"])
        abs_layout.apply(self.state)
        self.assertTrue(elsewhere.exists())

    def test_skip_when_target_exists(self):
        self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3")
        (self.out / "Author/Saga/3 - Book").mkdir()
        (self.out / "Author/Saga/3 - Book/keep.txt").write_text("mine")
        result = abs_layout.plan(self.state)
        self.assertEqual(result["moves"], [])
        self.assertIn("already exists", result["skipped"][0]["reason"])
        abs_layout.apply(self.state)
        self.assertEqual((self.out / "Author/Saga/3 - Book/keep.txt").read_text(), "mine")
        self.assertTrue((self.out / "Author/Saga/03 - Book/Book.m4b").exists())

    def test_skip_foreign_audio(self):
        self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3",
                  extra=("Book.mp3", "Other Book.m4b"))
        result = abs_layout.plan(self.state)
        self.assertEqual(result["moves"], [])
        self.assertIn("1 audio file(s) that belong to other books", result["skipped"][0]["reason"])

    def test_own_second_format_is_not_foreign(self):
        self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3", extra=("Book.mp3",))
        self.assertEqual(len(abs_layout.plan(self.state)["moves"]), 1)

    def test_skip_folder_shared_with_another_row(self):
        self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3")
        self.state.save(UnitState(path="b", status="converted", title="Book",
                                  output=str(self.out / "Author/Saga/03 - Book/Book.m4b"),
                                  detail=detail(["Author"], "Saga", "3", "Book")))
        result = abs_layout.plan(self.state)
        self.assertEqual(result["moves"], [])
        self.assertEqual({s["reason"] for s in result["skipped"]}, {"another book in Audiobind is filed in this folder"})

    def test_target_from_stored_metadata(self):
        # several authors joined, a series without a sequence, the conversion-time override wins
        self.book("a", "Pratchett/Good Omens/Good Omens.m4b", ["Terry Pratchett", "Neil Gaiman"], "Good Omens")
        self.book("b", "X/Old/Old.m4b", ["X"], "Old", overrides={"series": "Saga"})
        moves = {m.state_key: m.dst for m in abs_layout.plan(self.state)["moves"]}
        root = self.out.resolve()
        self.assertEqual(moves["a"], root / "Terry Pratchett & Neil Gaiman/Good Omens/Good Omens.m4b")
        self.assertEqual(moves["b"], root / "X/Saga/Old/Old.m4b")


class TestApply(LayoutCase):
    def test_apply_moves_updates_output_and_journals(self):
        src = self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3",
                        extra=("cover.jpg", "metadata.json", "Book.mp3"))
        result = abs_layout.apply(self.state)
        new = self.out.resolve() / "Author/Saga/3 - Book/Book.m4b"
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["moved"], [{"from": str(src), "to": str(new), "state_key": "a"}])
        self.assertFalse((self.out / "Author/Saga/03 - Book").exists())
        for name in ("Book.m4b", "Book.mp3", "cover.jpg", "metadata.json"):
            self.assertTrue((new.parent / name).exists(), name)
        row = self.state.get("a")
        self.assertEqual(row.output, str(new))
        self.assertEqual(json.loads(row.detail)["output"]["path"], str(new))
        [journal] = self.journals()
        self.assertEqual(result["journal"], str(journal))
        self.assertRegex(journal.name, r"^abs-layout-\d{8}T\d{6}Z\.json$")
        entries = json.loads(journal.read_text())["moves"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["state_key"], "a")
        self.assertEqual(entries[0]["from"], str(src.parent.resolve()))
        self.assertEqual(entries[0]["to"], str(new.parent))
        # a second run finds nothing to do
        self.assertEqual(abs_layout.plan(self.state)["unchanged"], 1)

    def test_emptied_author_folder_is_removed(self):
        self.book("a", "Pratchett/Good Omens/Good Omens.m4b", ["Terry Pratchett", "Neil Gaiman"], "Good Omens")
        abs_layout.apply(self.state)
        self.assertFalse((self.out / "Pratchett").exists())
        self.assertTrue(self.out.exists())
        self.assertTrue((self.out / "Terry Pratchett & Neil Gaiman/Good Omens/Good Omens.m4b").exists())

    def test_file_only_rename_keeps_the_folder(self):
        # the folder is right, only the file name is not: rename the files, not the folder
        self.book("a", "Author/Book/old name.m4b", ["Author"], "Book", extra=("old name.mp3", "cover.jpg"))
        result = abs_layout.apply(self.state)
        self.assertEqual(len(result["moved"]), 1)
        folder = self.out / "Author/Book"
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Book.m4b", "Book.mp3", "cover.jpg"])
        self.assertEqual(self.state.get("a").output, str(folder.resolve() / "Book.m4b"))
        entry = json.loads(self.journals()[0].read_text())["moves"][0]
        self.assertEqual(entry["kind"], "file")
        self.assertEqual(len(entry["files"]), 2)

    def test_partial_failure_is_reported_and_the_run_carries_on(self):
        self.book("a", "Author/Saga/01 - First/First.m4b", ["Author"], "First", "Saga", "1")
        self.book("b", "Author/Saga/02 - Second/Second.m4b", ["Author"], "Second", "Saga", "2")
        real = os.rename

        def flaky(src, dst):
            if "First" in str(src):
                raise PermissionError(13, "Permission denied", str(src))
            return real(src, dst)

        with mock.patch("audiobind.abs_layout.os.rename", side_effect=flaky):
            result = abs_layout.apply(self.state)
        self.assertEqual([f["state_key"] for f in result["failed"]], ["a"])
        self.assertEqual(result["failed"][0]["error"], "Permission denied")
        self.assertEqual([m["state_key"] for m in result["moved"]], ["b"])
        self.assertEqual(self.state.get("a").output, str(self.out / "Author/Saga/01 - First/First.m4b"))
        self.assertEqual(self.state.get("b").output, str(self.out.resolve() / "Author/Saga/2 - Second/Second.m4b"))
        entries = json.loads(self.journals()[0].read_text())["moves"]
        self.assertEqual([e["state_key"] for e in entries], ["b"])

    def test_cross_device_move_falls_back_to_shutil(self):
        self.book("a", "Author/Saga/03 - Book/Book.m4b", ["Author"], "Book", "Saga", "3")
        real = os.rename

        def exdev(src, dst):
            if pathlib.Path(src).is_dir():
                raise OSError(18, "Invalid cross-device link")
            return real(src, dst)

        with mock.patch("audiobind.abs_layout.os.rename", side_effect=exdev):
            result = abs_layout.apply(self.state)
        self.assertEqual(len(result["moved"]), 1)
        self.assertTrue((self.out / "Author/Saga/3 - Book/Book.m4b").exists())


class TestDev15Hardening(LayoutCase):
    """0.19.0-dev15 audit M3."""

    def test_symlinked_target_folder_outside_the_root_is_skipped(self):
        src = self.book("a", "Old/Book/Book.m4b", ["Linked"], "Book")
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.out / "Linked")
        # as if the author folder became a link after the path was built and checked
        with mock.patch.object(abs_layout, "target_for", return_value=self.out / "Linked" / "Book" / "Book.m4b"):
            result = abs_layout.plan(self.state)
            applied = abs_layout.apply(self.state)
        self.assertEqual(result["moves"], [])
        self.assertEqual(result["skipped"][0]["reason"], "the new path would leave OUTPUT_ROOT")
        self.assertEqual(applied["moved"], [])
        self.assertTrue(src.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_file_only_rename_in_a_shared_folder_is_skipped(self):
        src = self.book("a", "Author/Book/old name.m4b", ["Author"], "Book")
        self.state.save(UnitState(path="b", status="ready", title="Book", output=str(src)))
        result = abs_layout.plan(self.state)
        self.assertEqual(result["moves"], [])
        self.assertEqual(result["skipped"][0]["reason"], "another book in Audiobind is filed in this folder")
        abs_layout.apply(self.state)
        self.assertTrue(src.exists())

    def test_journal_keeps_renames_made_before_a_failure(self):
        self.book("a", "Author/Book/old name.m4b", ["Author"], "Book", extra=("old name.mp3",))
        real = os.rename

        def fail_on_main(a, b):
            if str(a).endswith(".m4b"):
                raise PermissionError(13, "Permission denied", str(a))
            return real(a, b)

        with mock.patch("audiobind.abs_layout.os.rename", side_effect=fail_on_main):
            result = abs_layout.apply(self.state)
        self.assertEqual([f["state_key"] for f in result["failed"]], ["a"])
        [entry] = json.loads(self.journals()[0].read_text())["moves"]
        folder = (self.out / "Author/Book").resolve()
        self.assertEqual(entry["files"], [{"from": str(folder / "old name.mp3"), "to": str(folder / "Book.mp3")}])
        self.assertTrue((folder / "Book.mp3").exists())

    def test_target_comes_from_metadata_json(self):
        src = self.book("a", "Old/Old Title/Old Title.m4b", ["Wrong Author"], "Old Title", "Wrong Saga", "9")
        (src.parent / "metadata.json").write_text(json.dumps(
            {"title": "Real Title", "authors": ["Ann", "Bob"], "series": ["Saga #2"]}), encoding="utf-8")
        [move] = abs_layout.plan(self.state)["moves"]
        self.assertEqual(move.dst, self.out.resolve() / "Ann & Bob/Saga/2 - Real Title/Real Title.m4b")

    def test_metadata_json_without_series_files_a_standalone(self):
        src = self.book("a", "Old/Book/Book.m4b", ["Author"], "Book", "Saga", "3")
        (src.parent / "metadata.json").write_text(json.dumps({"title": "Book", "series": []}), encoding="utf-8")
        [move] = abs_layout.plan(self.state)["moves"]
        self.assertEqual(move.dst, self.out.resolve() / "Author/Book/Book.m4b")  # authors fell back to the row

    def test_unreadable_metadata_json_falls_back_to_the_row(self):
        src = self.book("a", "Old/Book/Book.m4b", ["Author"], "Book", "Saga", "3")
        (src.parent / "metadata.json").write_text("{not json", encoding="utf-8")
        [move] = abs_layout.plan(self.state)["moves"]
        self.assertEqual(move.dst, self.out.resolve() / "Author/Saga/3 - Book/Book.m4b")


class TestEndpoint(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.saved_state_dir = CONFIG.state_dir
        CONFIG.state_dir = self.root / "state"
        src = self.out / "Author/Saga/03 - Book/Book.m4b"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"audio")
        self.state.save(UnitState(path="a", status="converted", title="Book", output=str(src),
                                  detail=detail(["Author"], "Saga", "3", "Book")))
        self.src = src

    def tearDown(self):
        CONFIG.state_dir = self.saved_state_dir
        super().tearDown()

    def test_dry_run_returns_the_plan(self):
        status, body = self.post({"dry_run": True}, path="/api/library/abs-layout")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["moves"], [{"from": str(self.src),
                                          "to": str(self.out.resolve() / "Author/Saga/3 - Book/Book.m4b"),
                                          "state_key": "a"}])
        self.assertEqual(body["skipped"], [])
        self.assertTrue(self.src.exists())

    def test_apply_needs_confirm(self):
        status, body = self.post({"dry_run": False}, path="/api/library/abs-layout")
        self.assertEqual(status, 400)
        self.assertTrue(self.src.exists())

    def test_apply(self):
        status, body = self.post({"dry_run": False, "confirm": True}, path="/api/library/abs-layout")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["renamed"], 1)
        self.assertEqual(body["failed_count"], 0)
        self.assertTrue((self.out / "Author/Saga/3 - Book/Book.m4b").exists())

    def test_refused_while_encoding(self):
        with self.state.unit_writer("a"):
            status, body = self.post({"dry_run": False, "confirm": True}, path="/api/library/abs-layout")
        self.assertEqual(status, 409)
        self.assertTrue(self.src.exists())


class TestSettingsCard(unittest.TestCase):
    def test_card_warning_buttons_and_modal(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                html = web.render_settings(state)
            finally:
                state.close()
        self.assertIn('id="library"', html)
        self.assertIn("Match Audiobookshelf layout", html)
        self.assertIn("This moves and renames folders inside /output. Audiobookshelf may treat a renamed "
                      "folder as a new book and lose its listening progress.", html)
        self.assertIn('onclick="abAbsPreview()">Preview</button>', html)
        self.assertIn('onclick="abAbsAsk()">Apply</button>', html)
        self.assertIn('id="ab-modal"', html)
        self.assertIn("Renamed ' + n + ' book(s) to the Audiobookshelf layout.", html)


if __name__ == "__main__":
    unittest.main()
