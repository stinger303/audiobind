"""D1: data-safety fixes from the 2026-11-20 audit (CORE-02/03/04, WEBAPI-01/02, STATECFG-01).

Each test asks one question: can this make a book disappear, duplicate, or be recorded as done
when it is not?
"""

import pathlib
import unittest
from unittest import mock

from audiobind import catalog, encode, ingest, matching, pipeline

try:
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.test_ingest_pipeline import KEY, PHM, PipelineCase


class PublishCase(PipelineCase):
    """convert_unit with a fake encoder, the output format and failures under the test's control."""

    OTHER = "Andy Weir - Project Hail Mary (Other Edition) [TRACKER]"
    BOOK = {"title": "Project Hail Mary", "authors": [{"name": "Andy Weir"}]}

    def setUp(self):
        super().setUp()
        self.add_torrent()
        self.add_torrent(name=self.OTHER)
        self.cover_src = self.root / "cover-src.jpg"
        self.output_format = "M4B + MP3"

    def fake_encode(self, unit, match, book, work_dir, out_path, **kw):
        self.encode_calls.append(kw["fmt"])
        work_dir.mkdir(parents=True, exist_ok=True)
        temp = work_dir / f"output.{kw['fmt']}"
        temp.write_bytes(f"{self.tag}-{kw['fmt']}".encode())
        self.cover_src.write_bytes(f"{self.tag}-cover".encode())
        return encode.EncodeResult(path=temp, duration=7200.0, size=3, chapters=2,
                                   chapters_source="test", cover=self.cover_src)

    def patches(self, fail_place=None, fail_sidecar=False):
        real_place = encode._place
        real_meta = encode.write_abs_metadata

        def place(source, dest, move):
            if fail_place is not None and fail_place(dest):
                raise OSError(28, "No space left on device")
            return real_place(source, dest, move)

        def meta(path, *args, **kw):
            if fail_sidecar:
                if path.exists():
                    path.write_bytes(b"")  # a full volume can cut the old file short first
                raise OSError(28, "No space left on device")
            return real_meta(path, *args, **kw)

        return [
            mock.patch.object(catalog, "book", return_value=self.BOOK),
            mock.patch.object(pipeline, "effective_val",
                              side_effect=lambda k: self.output_format if k == "output_format" else None),
            mock.patch.object(encode, "plan_chapters", return_value=mock.Mock(chapters=[])),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
            mock.patch.object(encode, "_place", side_effect=place),
            mock.patch.object(encode, "write_abs_metadata", side_effect=meta),
        ]

    def convert(self, key, tag, reencode=False, **fail):
        self.tag = tag
        unit = ingest.find_any_unit(key)
        audit = pipeline.Audit(unit=unit, match=matching.Match(product=PHM, confidence="HIGH"), action="convert")
        patches = self.patches(**fail)
        for p in patches:
            p.start()
        try:
            pipeline.convert_unit(audit, log=lambda *_: None, reencode=reencode)
        finally:
            for p in reversed(patches):
                p.stop()
        return pathlib.Path(audit.out_path)

    @staticmethod
    def listing(folder: pathlib.Path) -> list[str]:
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --------------------------------------------------------------------------- CORE-02
class Core02RollbackCoversPlacedAudioTests(PublishCase):
    """An error after publish_result placed the audio (the cover, the stat) must roll it back."""

    @staticmethod
    def cover_fails(dest):
        return dest.name == "cover.jpg"

    def test_a_cover_error_on_the_first_format_leaves_no_audio_and_no_cover(self):
        with self.assertRaises(OSError):
            self.convert(KEY, "first", fail_place=self.cover_fails)
        m4b = pipeline.library.m4b_path("", ["Andy Weir"], "Project Hail Mary")
        self.assertFalse(m4b.exists(), "the M4B placed before the cover error was left published")
        self.assertFalse(m4b.with_suffix(".mp3").exists())
        self.assertFalse((m4b.parent / "cover.jpg").exists())
        self.assertEqual(self.listing(m4b.parent), [])

    def test_a_cover_error_during_a_replace_leaves_the_previous_pair_intact(self):
        m4b = self.convert(KEY, "first")
        mp3, cover = m4b.with_suffix(".mp3"), m4b.parent / "cover.jpg"
        before = self.listing(m4b.parent)
        with self.assertRaises(OSError):
            self.convert("ingest:" + self.OTHER, "second", reencode=True, fail_place=self.cover_fails)
        self.assertEqual((m4b.read_bytes(), mp3.read_bytes(), cover.read_bytes()),
                         (b"first-m4b", b"first-mp3", b"first-cover"))
        self.assertEqual(self.listing(m4b.parent), before)


if __name__ == "__main__":
    unittest.main()
