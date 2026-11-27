import struct
from collections import Counter
from unittest.mock import patch

from audiobind import coveraudit, drift
from audiobind.state import UnitState
from tests.test_library_drift import DriftCase


def png(width, height):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


def jpeg(width, height, marker=0xC0):
    return (
        b"\xff\xd8\xff\xe0\x00\x04AB\xff" + bytes([marker])
        + b"\x00\x08\x08" + struct.pack(">HH", height, width)
    )


class TestCoverAudit(DriftCase):
    def book(self, name, cover, asin="", status="converted"):
        (self.lib / name).mkdir()
        folder = self.out / name
        folder.mkdir()
        output = folder / "book.m4b"
        output.touch()
        if cover is not None:
            (folder / "cover.jpg").write_bytes(cover)
        self.state.save(UnitState(name, status, asin=asin, output=str(output)))

    def test_header_dimensions(self):
        image = self.out / "header"
        for marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC7, 0xC9, 0xCB, 0xCD, 0xCF):
            with self.subTest(marker=marker):
                image.write_bytes(jpeg(800, 600, marker))
                self.assertEqual(coveraudit.dimensions(image), (800, 600))
        for header, expected in (
            (png(500, 500), (500, 500)),
            (png(0, 500), None),
            (jpeg(800, 600)[:-1], None),
            (b"\xff\xd8\xff\xe0\xff\xff", None),
            (b"garbage", None),
        ):
            image.write_bytes(header)
            self.assertEqual(coveraudit.dimensions(image), expected)
        self.assertIsNone(coveraudit.dimensions(self.out / "absent"))

    def test_all_classes_cross_asin_duplicates_and_read_only(self):
        self.book("Good", png(500, 500), "B000000001")
        self.book("Copy", png(500, 500), "B000000002", "ready")
        self.book("Small", jpeg(499, 700), "B000000003")
        self.book("Same ASIN small", jpeg(499, 700), "B000000003")
        self.book("Not square", jpeg(600, 1000))
        self.book("Missing", None)
        self.book("Unreadable", b"?")
        self.state.save(UnitState("Dry run", "ready"))
        before = self.snapshot()
        with patch("audiobind.catalog.download", side_effect=AssertionError("audit cannot fetch")):
            drift.report(self.state)
            report = coveraudit.report(self.state)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report["counts"], Counter(ok=2, missing=1, too_small=2, not_square=1, unreadable=1))
        issues = {entry["path"]: entry for entry in report["offenders"]}
        self.assertEqual({key: row["classification"] for key, row in issues.items()}, {
            "Missing": "missing", "Not square": "not_square", "Same ASIN small": "too_small",
            "Small": "too_small", "Unreadable": "unreadable",
        })
        self.assertEqual((issues["Small"]["width"], issues["Small"]["height"]), (499, 700))
        self.assertEqual(issues["Small"]["asin"], "B000000003")
        self.assertEqual(len(report["duplicates"]), 1)
        self.assertEqual([entry["path"] for entry in report["duplicates"][0]["units"]], ["Copy", "Good"])
