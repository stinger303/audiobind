"""Chapter tier selection in build_chapters: source-embedded, audnex, per-file."""

import unittest
from unittest.mock import patch

from audiobind import encode
from audiobind.units import BookUnit, Part


def fake_probe(durations: dict):
    def probe(path, timeout=60):
        class Info:
            duration = durations.get(path, 0.0)
            codec = ""

        return Info()

    return probe


def fake_chapters(per_file: dict):
    def probe_chapters(path):
        return per_file.get(path, [])

    return probe_chapters


def unit(parts: dict[str, list[str]]):
    return BookUnit(
        path="X",
        name="X",
        parent="",
        parts=[Part(name=name, files=files) for name, files in parts.items()],
    )


# two embedded chapters per part file, 100s and 200s marks
CH1 = {"start_time": "0.0", "end_time": "100.0", "tags": {"title": "Chapter One"}}
CH2 = {"start_time": "100.0", "end_time": "300.0", "tags": {"title": "Chapter Two"}}

# one Audible chapter covering the whole 300s runtime
AUDNEX = [{"lengthMs": 300000, "title": "Chapter One"}]


class TestSourceEmbedded(unittest.TestCase):
    def test_single_part_m4b_prefers_source_over_audnex(self):
        u = unit({"Book": ["/src/book.m4b"]})
        durations, chapters_map = {"/src/book.m4b": 300.0}, {"/src/book.m4b": [CH1, CH2]}
        with (
            patch.object(encode, "probe", fake_probe(durations)),
            patch.object(encode, "probe_chapters", fake_chapters(chapters_map)),
        ):
            chapters, label = encode.build_chapters(AUDNEX, 300.0, u)
        self.assertIn("source-embedded", label)
        self.assertEqual([c["title"] for c in chapters], ["Chapter One", "Chapter Two"])
        self.assertEqual(chapters[0]["end"], 100.0)

    def test_offsets_accumulate_across_parts_and_titles_survive(self):
        u = unit({"Part 1": ["/src/a.mp3"], "Part 2": ["/src/b.mp3"]})
        durations = {"/src/a.mp3": 300.0, "/src/b.mp3": 300.0}
        chapters_map = {
            "/src/a.mp3": [CH1, CH2],
            "/src/b.mp3": [
                {"start_time": "0.0", "end_time": "100.0", "tags": {"title": "Disc 2, Ch 1"}},
                {"start_time": "100.0", "end_time": "300.0", "tags": {"title": "Disc 2, Ch 2"}},
            ],
        }
        with (
            patch.object(encode, "probe", fake_probe(durations)),
            patch.object(encode, "probe_chapters", fake_chapters(chapters_map)),
        ):
            chapters, label = encode.build_chapters([], 600.0, u)
        self.assertIn("source-embedded", label)
        self.assertEqual(len(chapters), 4)
        # second part's chapters sit at cursor 300
        self.assertEqual(chapters[2]["start"], 300.0)
        self.assertEqual(chapters[2]["end"], 400.0)
        self.assertEqual(chapters[3]["end"], 600.0)
        self.assertEqual(chapters[2]["title"], "Disc 2, Ch 1")

    def test_multi_part_with_audnex_prefers_audnex(self):
        u = unit({"Part 1": ["/src/a.m4b"], "Part 2": ["/src/b.m4b"]})
        durations = {"/src/a.m4b": 300.0, "/src/b.m4b": 300.0}
        chapters_map = {"/src/a.m4b": [CH1, CH2], "/src/b.m4b": [CH1, CH2]}
        with (
            patch.object(encode, "probe", fake_probe(durations)),
            patch.object(encode, "probe_chapters", fake_chapters(chapters_map)),
        ):
            chapters, label = encode.build_chapters([{"lengthMs": 600000, "title": "Ch 1"}], 600.0, u)
        self.assertIn("audnex", label)
        self.assertEqual(chapters[0]["title"], "Ch 1")
        self.assertEqual(chapters[0]["end"], 600.0)

    def test_unprobeable_duration_rejects_source_chapters(self):
        u = unit({"Book": ["/src/book.m4b"]})
        with (
            patch.object(encode, "probe", fake_probe({"/src/book.m4b": 0.0})),
            patch.object(encode, "probe_chapters", fake_chapters({"/src/book.m4b": [CH1, CH2]})),
        ):
            chapters, label = encode.build_chapters([], 300.0, u)
        self.assertIn("per-file", label)

    def test_audnex_only_when_sources_have_no_tags(self):
        u = unit({"Book": ["/src/a.mp3"]})
        with (
            patch.object(encode, "probe", fake_probe({"/src/a.mp3": 300.0})),
            patch.object(encode, "probe_chapters", fake_chapters({})),
        ):
            chapters, label = encode.build_chapters(AUDNEX, 300.0, u)
        self.assertIn("audnex", label)
        self.assertEqual(len(chapters), 1)

    def test_per_file_fallback_when_nothing_else(self):
        u = unit({"Book": ["/src/a.mp3"]})
        with (
            patch.object(encode, "probe", fake_probe({"/src/a.mp3": 300.0})),
            patch.object(encode, "probe_chapters", fake_chapters({})),
        ):
            chapters, label = encode.build_chapters([], 300.0, u)
        self.assertIn("per-file", label)
        self.assertEqual(chapters[0]["title"], "a")


if __name__ == "__main__":
    unittest.main()
