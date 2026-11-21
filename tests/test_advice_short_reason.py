"""0.21.0-dev3: the one-line WHY label, the "Needs you" groups, and the held-book copy fix."""

import unittest

from audiobind import advice

DAMAGED_HELD = "1 unreadable file(s); 2 parts combined; runtime 1.38h vs catalog 7.72h"

# (status, confidence, note, detail, expected key, expected label)
CASES = (
    ("review", "MEDIUM", "3 unreadable file(s)", None, "damaged_files", "Damaged files"),
    ("review", "NONE", "", None, "not_on_audible", "Not on Audible"),
    ("review", "COLLECTION", "", None, "collection", "Looks like a collection"),
    ("review", "LOW", "", None, "low_match", "Low match"),
    ("review", "MEDIUM", "", None, "medium_match", "Medium match"),
    ("review", "HIGH", "", None, "check_match", "Check the match"),
    ("review", "", "", None, "check_match", "Check the match"),
    ("held", "HIGH", "unsafe file name: a line break in 'x'", None, "unsafe_name", "Unsafe file name"),
    ("held", "", "no catalog match for 'Foo'", None, "no_catalog_match", "No catalog match"),
    ("held", "HIGH", "runtime 1.38h vs catalog 7.72h", None, "runtime_short", "Runtime 82% short"),
    ("held", "HIGH", "runtime 12.95h vs catalog 7.50h", None, "runtime_long", "Runtime 73% long"),
    (
        "held",
        "HIGH",
        "",
        {"source": {"hours": 11.58}, "match": {"hours": 7.47}},
        "runtime_long",
        "Runtime 55% long",
    ),
    ("held", "HIGH", "runtime mismatch", None, "runtime_gap", "Runtime differs"),
    ("held", "HIGH", "", None, "runtime_gap", "Runtime differs"),
    ("held", "MEDIUM", DAMAGED_HELD, None, "damaged_files", "Damaged files"),
    ("failed", "", "ffmpeg exited 1", None, "encoder_error", "Encoder error"),
    ("failed", "HIGH", "", None, "encoder_error", "Encoder error"),
    # C4: the note is the evidence - a refusal never reached the encoder; a signal names its cause
    ("failed", "HIGH", "ffprobe could not read 02.mp3, so the book's length is unknown", None,
     "unmeasured", "Couldn't measure a file"),
    ("held", "HIGH", "Not converted: one file could not be measured, so the converted book could not be "
     "checked.", None, "unmeasured", "Couldn't measure a file"),
    ("failed", "HIGH", "ffmpeg was killed by SIGKILL (usually the system running out of memory) while "
     "encoding", None, "encoder_killed", "Encoder was stopped"),
    ("interrupted", "", "", None, "stopped", "Stopped part way"),
    ("converted", "HIGH", "", None, "converted", ""),
    ("ready", "HIGH", "", None, "converted", ""),
    ("", "", "", None, "converted", ""),
)


class TestShortReason(unittest.TestCase):
    def test_every_branch_returns_its_key_and_label(self):
        for status, conf, note, detail, key, label in CASES:
            with self.subTest(status=status, conf=conf, note=note):
                self.assertEqual(advice.short_reason(status, conf, note, detail), (key, label))

    def test_every_problem_key_has_a_group_and_a_short_label(self):
        for status, conf, note, detail, _key, _label in CASES:
            key, label = advice.short_reason(status, conf, note, detail)
            if key == "converted":
                self.assertEqual(label, "")
                continue
            self.assertIn(key, advice.GROUP_REASONS)
            self.assertTrue(1 <= len(label.split()) <= 6, label)

    def test_every_table_key_is_covered_by_a_case(self):
        covered = {case[4] for case in CASES} - {"converted"}
        self.assertEqual(covered, set(advice.GROUP_REASONS))

    def test_group_headers_are_plain_sentences(self):
        for key, (title, sentence) in advice.GROUP_REASONS.items():
            with self.subTest(key=key):
                self.assertIn(title, ("Failed", "Held", "Stopped", "Review"))
                self.assertTrue(sentence.endswith("."), sentence)
                for word in ("pinned", "wedge", "locked", "LOW", "MEDIUM", "HIGH", "ASIN pin"):
                    self.assertNotIn(word, sentence)

    def test_the_runtime_header_is_the_approved_sentence(self):
        expected = (
            "Your files and Audible's runtime disagree by more than your 15 % tolerance. That gap "
            "usually means a different edition (radio drama, abridged, omnibus) or missing files."
        )
        for key in ("runtime_short", "runtime_long", "runtime_gap"):
            self.assertEqual(advice.GROUP_REASONS[key], ("Held", expected))

    def test_group_order_is_by_severity(self):
        self.assertEqual(advice.group_order(), ("failed", "held", "interrupted", "review"))
        self.assertEqual(set(advice.group_order()), set(advice.ATTENTION_STATUSES))

    def test_reason_numbers(self):
        self.assertEqual(
            advice.reason_numbers("held", "MEDIUM", DAMAGED_HELD), "1 file unreadable · 1.38 h vs 7.72 h · 82% short"
        )
        self.assertEqual(advice.reason_numbers("held", "HIGH", "runtime 12.95h vs catalog 7.50h"), "12.95 h vs 7.50 h")
        self.assertEqual(advice.reason_numbers("review", "MEDIUM", "2 unreadable file(s)"), "2 files unreadable")
        self.assertEqual(advice.reason_numbers("review", "MEDIUM", ""), "")
        self.assertEqual(advice.reason_numbers("converted", "HIGH", ""), "")


class TestHeldCopy(unittest.TestCase):
    def test_unreadable_files_lead_and_the_edition_is_not_blamed(self):
        why, todo = advice.explain("held", "MEDIUM", DAMAGED_HELD, {"source": {"files": 18}})
        self.assertEqual(
            why,
            "One of your 18 source files can't be read, which probably explains why your files run "
            "1.38 hours when Audible lists 7.72 for this edition (82% shorter).",
        )
        text = why + " " + todo
        self.assertNotIn("different edition", text)
        self.assertNotIn("omnibus", text)
        self.assertIn("Convert anyway", todo)
        self.assertIn("Retry now", todo)

    def test_unreadable_wording_rules(self):
        cases = (
            ("2 unreadable; runtime 1.38h vs catalog 7.72h", {"source": {"files": 18}}, "2 of your 18 source files"),
            ("1 unreadable; runtime 1.38h vs catalog 7.72h", None, "One source file can't be read, which"),
            ("1 unreadable", {"source": {"files": 4}}, "One of your 4 source files can't be read."),
            ("1 unreadable", None, "One source file can't be read."),
            ("3 unreadable", None, "3 source files can't be read."),
        )
        for note, detail, lead in cases:
            with self.subTest(note=note):
                why, _ = advice.explain("held", "MEDIUM", note, detail)
                self.assertTrue(why.startswith(lead), why)
                self.assertNotIn("different edition", why)

    def test_longer_files_say_longer(self):
        why, _ = advice.explain("held", "HIGH", "1 unreadable; runtime 9.00h vs catalog 6.00h", {"source": {"files": 3}})
        self.assertIn("(50% longer).", why)

    def test_a_plain_runtime_gap_still_names_the_edition(self):
        why, _ = advice.explain("held", "HIGH", "runtime 12.95h vs catalog 7.50h")
        self.assertIn("different edition", why)
        self.assertIn("73% longer", why)


if __name__ == "__main__":
    unittest.main()
