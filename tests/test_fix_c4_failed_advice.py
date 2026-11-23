"""C4: the advice for a failed (or refused) book says what its note says happened.

Refusals explain why conversion never started. Signal failures identify the signal
and ordinary conversion failures show the recorded error.
"""

import unittest

from audiobind import advice, encode

REFUSAL = encode.unmeasured_reason(["/library/Andy Weir/Project Hail Mary/02 Broken.mp3"])
SIGKILL = encode._signalled(encode.killed_by_signal(-9), "encoding")
SIGTERM = encode._signalled(encode.killed_by_signal(-15), "encoding")


class TestC4RefusalAdvice(unittest.TestCase):
    def test_a_refusal_is_not_an_encoder_error(self):
        for status in ("failed", "held"):
            with self.subTest(status=status):
                key, label = advice.short_reason(status, "HIGH", REFUSAL)
                self.assertNotEqual(label, "Conversion failed")
                self.assertEqual(key, "unmeasured")
                why, what = advice.explain(status, "HIGH", REFUSAL)
                self.assertNotIn("encoder", why.lower())
                self.assertNotIn("usual suspect", why)
                self.assertIn("could not measure", why)
                self.assertIn("nothing was converted", why)
                self.assertIn("02 Broken.mp3", why)
                self.assertIn("Retry now", what)
                self.assertNotIn("Encoder", advice.GROUP_REASONS[key][1])

    def test_the_held_note_c3_records_is_recognised(self):
        from audiobind import pipeline

        note = pipeline.unmeasured_held_note(REFUSAL)
        self.assertEqual(advice.short_reason("held", "HIGH", note)[0], "unmeasured")
        why, _what = advice.explain("held", "HIGH", note)
        self.assertNotIn("edition", why)  # never the runtime-hold copy


class TestC4SignalAdvice(unittest.TestCase):
    def test_sigkill_puts_memory_first(self):
        key, label = advice.short_reason("failed", "HIGH", SIGKILL)
        self.assertEqual(key, "encoder_killed")
        self.assertNotEqual(label, "Conversion failed")
        why, what = advice.explain("failed", "HIGH", SIGKILL)
        low = why.lower()
        self.assertIn("memory", low)
        self.assertNotIn("usual suspect", low)
        self.assertNotIn("damaged", low)
        self.assertLess(what.lower().index("memory"), what.lower().index("retry now"))
        sentence = advice.GROUP_REASONS[key][1].lower()
        self.assertIn("view conversion error", sentence)

    def test_another_signal_does_not_claim_memory(self):
        key, _label = advice.short_reason("failed", "HIGH", SIGTERM)
        self.assertEqual(key, "encoder_killed")
        why, _what = advice.explain("failed", "HIGH", SIGTERM)
        self.assertIn("SIGTERM", why)
        self.assertIn("stopped", why.lower())

    def test_an_ordinary_encoder_failure_shows_the_recorded_error(self):
        self.assertEqual(advice.short_reason("failed", "", "ffmpeg exited 1"),
                         ("encoder_error", "Conversion failed"))
        why, _what = advice.explain("failed", "", "ffmpeg exited 1")
        self.assertEqual(why, "Conversion failed. Recorded error: ffmpeg exited 1.")


if __name__ == "__main__":
    unittest.main()
