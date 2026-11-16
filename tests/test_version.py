"""The version the app reports must be the version it was released as.

``__version__`` is what the web UI shows and what goes out in the tracker User-Agent;
``pyproject.toml`` is what the image is tagged with. They drifted once — every dev8..dev12
bump touched only pyproject, so the UI kept claiming dev7 — and nothing noticed. This test
is that notice.
"""
import re
import unittest
from pathlib import Path

from audiobind import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


class TestVersionAgrees(unittest.TestCase):
    def test_pyproject_version_matches_init(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        # only the [project] table's version line, not a dependency pin
        head = text.split("[project]", 1)[1].split("\n[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"', head, re.M)
        self.assertIsNotNone(match, "no version line in [project]")
        self.assertEqual(match.group(1), __version__,
                         "pyproject.toml and audiobind.__version__ disagree - bump both")

    def test_version_is_a_real_string(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+(?:-dev\d+)?$")


if __name__ == "__main__":
    unittest.main()
