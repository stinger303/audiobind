"""The page ships its own CSS/JS: check they are syntactically sound and self-contained."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiobind import web


class TestPageAssets(unittest.TestCase):
    def test_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "page.js"
            js.write_text(web.SCRIPT, encoding="utf-8")
            proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_external_assets(self):
        """Hard-refresh-friendly: nothing may be fetched from a CDN."""
        page = web.PAGE + web.STYLES + web.SCRIPT
        for marker in ("http://", "https://", "//cdn", "fonts.googleapis"):
            self.assertNotIn(marker, page, f"page references {marker}")

    def test_braces_in_styles_and_script_are_not_format_placeholders(self):
        """STYLES/SCRIPT are inserted, not formatted: a stray {} must stay harmless."""
        self.assertIn("{", web.STYLES)
        self.assertIn("{", web.SCRIPT)
        placeholders = set(re.findall(r"\{(\w+)\}", web.PAGE))
        self.assertIn("styles", placeholders)
        self.assertIn("script", placeholders)


if __name__ == "__main__":
    unittest.main()
