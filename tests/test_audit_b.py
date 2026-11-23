"""Regressions from the Library, Settings and Series usability audit."""

import re
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from audiobind import web
from audiobind.state import State, UnitState


class AuditUsabilityTests(unittest.TestCase):

    def test_exception_rows_have_one_labelled_detail_action(self):
        with tempfile.TemporaryDirectory() as temp, closing(State(Path(temp))) as state:
            for status in ("failed", "held", "review"):
                state.save(UnitState(path="Books/" + status, status=status, confidence="MEDIUM"))
            page = web.render(state)
        rows = re.findall(r'<tr class="urow".*?</tr>', page, re.S)
        for status, label, target in (
            ("failed", "View conversion error", "reason"),
            ("held", "Compare runtimes", "reason"),
            ("review", "Check Audible match", "match"),
        ):
            row = next(row for row in rows if f'data-group="{status}"' in row)
            self.assertEqual(row.count('class="btn ab-row-detail"'), 1)
            self.assertIn(f'''abOpenRowDetail(this,'{target}')">{label}</button>''', row)
            self.assertIn('aria-haspopup="menu"', row)
        self.assertIn('class="ab-detail-match" tabindex="-1"', page)

    def test_match_cell_is_displayed_at_tablet_widths(self):
        tablet = web.STYLES.split("/* Library rows through 820 px */", 1)[-1]
        tablet = tablet.split("/* Expanded detail", 1)[0]
        self.assertTrue(tablet.startswith("\n@media(max-width:820px)"))
        rule = re.search(r'td\.ab-match\{([^}]+)\}', tablet).group(1)
        self.assertIn("display:flex", rule)
        self.assertIn('content:"Match"', tablet)
