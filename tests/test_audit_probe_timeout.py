"""C5: the audit never stores a duration for a file ffprobe timed out on.

audit_unit() used to record probe()'s 0.0 for a timeout as a measured zero. plan_chapters() trusts a
supplied audit.durations whose keys match the unit's files, so a forced conversion ("Convert anyway",
which skips the unreadable-file guard) passed 0.0 to _source_chapters(), which returned [] for the
whole book: the silent chapter loss C4 closed everywhere else.
"""

import unittest
from unittest import mock

from audiobind import catalog, encode, pipeline
from audiobind.probe import Probe, ProbeTimeout
from audiobind.units import BookUnit, Part

try:
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.test_ingest_pipeline import KEY, PHM, PipelineCase

GOOD = Probe(duration=3600.0, codec="mp3", channels="2")


def answering(timed_out=(), empty=()):
    """A probe() fake: ProbeTimeout for ``timed_out`` names (when asked to raise), an answered
    but empty Probe() for ``empty`` names, a normal hour for the rest."""

    def fake(path, timeout=60, raise_on_timeout=False):
        name = str(path).rsplit("/", 1)[-1]
        if name in timed_out:
            if raise_on_timeout:
                raise ProbeTimeout(path, timeout, "duration")
            return Probe()
        return Probe() if name in empty else GOOD

    return fake


def unit(*files):
    return BookUnit(path="/src/X", name="X", parent="/src", parts=[Part(name="X", files=list(files))])


class TestAudit(unittest.TestCase):
    def audit(self, fake, files=("/src/a.mp3", "/src/b.mp3")):
        with (
            mock.patch.object(pipeline, "probe", side_effect=fake),
            mock.patch.object(pipeline.os.path, "getsize", return_value=1000),
            mock.patch.object(pipeline.matching, "match_unit", return_value=pipeline.matching.Match()),
            mock.patch.object(pipeline.hardcover, "evidence_for", return_value=None),
        ):
            return pipeline.audit_unit(unit(*files))

    def test_a_timed_out_file_has_no_duration_key(self):
        audit = self.audit(answering(timed_out={"b.mp3"}))
        self.assertEqual(audit.durations, {"/src/a.mp3": 3600.0})
        self.assertNotIn("/src/b.mp3", audit.durations)

    def test_it_counts_as_unreadable_and_says_why(self):
        audit = self.audit(answering(timed_out={"b.mp3"}))
        self.assertEqual(audit.unreadable, 1)
        self.assertIn("ffprobe timed out on 1 file(s): runtime unknown", audit.notes)
        self.assertIn("1 unreadable file(s)", audit.notes)

    def test_the_total_is_unknown_not_a_short_sum(self):
        audit = self.audit(answering(timed_out={"b.mp3"}))
        self.assertEqual(audit.hours, 0.0)  # not 1.0 (a.mp3 alone)

    def test_the_audit_asks_for_the_timeout_instead_of_a_zero(self):
        probed = mock.Mock(side_effect=answering())
        with (
            mock.patch.object(pipeline, "probe", probed),
            mock.patch.object(pipeline.os.path, "getsize", return_value=1),
            mock.patch.object(pipeline.matching, "match_unit", return_value=pipeline.matching.Match()),
            mock.patch.object(pipeline.hardcover, "evidence_for", return_value=None),
        ):
            pipeline.audit_unit(unit("/src/a.mp3"))
        self.assertTrue(probed.call_args.kwargs.get("raise_on_timeout"))

    def test_a_file_ffprobe_answers_for_with_no_duration_is_unchanged(self):
        audit = self.audit(answering(empty={"b.mp3"}))
        self.assertEqual(audit.durations, {"/src/a.mp3": 3600.0, "/src/b.mp3": 0.0})  # key kept
        self.assertEqual(audit.unreadable, 1)
        self.assertEqual(audit.hours, 1.0)
        self.assertEqual(audit.notes, ["1 unreadable file(s)"])

    def test_healthy_files_are_unchanged(self):
        audit = self.audit(answering())
        self.assertEqual(audit.durations, {"/src/a.mp3": 3600.0, "/src/b.mp3": 3600.0})
        self.assertEqual((audit.unreadable, audit.hours, audit.notes), (0, 2.0, []))

    def test_the_audits_durations_make_plan_chapters_probe_again_and_fail(self):
        audit = self.audit(answering(timed_out={"b.mp3"}))
        with (
            mock.patch.object(encode, "probe", side_effect=answering(timed_out={"b.mp3"})),
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
        ):
            with self.assertRaises(RuntimeError) as caught:
                encode.plan_chapters(audit.unit, "B000000001", durations=audit.durations or None)
        self.assertIn("/src/b.mp3", str(caught.exception))


class TestForcedConversion(PipelineCase):
    def run_unit(self, probe, **kw):
        self.add_torrent()
        unit_ = pipeline.ingest.find_any_unit(KEY)
        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", side_effect=probe),
            mock.patch.object(encode, "probe", side_effect=probe),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
        ):
            pipeline.process_unit(self.state, unit_, log=lambda *_: None, **kw)
        return self.state.get(KEY)

    def test_convert_anyway_fails_the_book_instead_of_converting_without_chapters(self):
        row = self.run_unit(answering(timed_out={"02.mp3"}), force=True, manual=True, reencode=True)
        self.assertEqual(self.encode_calls, [])  # nothing was encoded
        self.assertEqual(row.status, "failed")
        self.assertIn("02.mp3", row.note)
        self.assertIn("timed out", row.note)

    def test_a_normal_run_still_does_not_convert_it(self):
        row = self.run_unit(answering(timed_out={"02.mp3"}), manual=True)
        self.assertEqual(self.encode_calls, [])
        self.assertNotEqual(row.status, "converted")
        self.assertIn("timed out", row.note)

    def test_a_forced_conversion_of_a_healthy_book_still_converts(self):
        row = self.run_unit(answering(), force=True, manual=True, reencode=True)
        self.assertEqual(len(self.encode_calls), 1)
        self.assertEqual(row.status, "converted")


if __name__ == "__main__":
    unittest.main()
