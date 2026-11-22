"""Set up downloading at 390, 768, 1024 and 1440 px: nothing clipped, no control out of reach.

There is no browser in the test environment, so this asserts the structural properties that decide it, on
every page of the guide (steps 1-4 and the end state): every box and choice takes the shared ``.ab-in`` class
whose wizard rule caps it at its container's width; no wizard rule sets a fixed width wider than a 390 px
phone's content column; every row of buttons wraps; the fieldset can shrink (a fieldset's default min-width is
its content's); long unbroken text (addresses, titles, paths) breaks; below 1080 px the rail leaves the
layout, and at 1440 px the guide's column fits beside it. Every class the guide uses is ``ab-`` prefixed."""

import re

from audiobind import web, wizard

try:
    from test_wizard_step4 import SITE, Step4Case
except ImportError:
    from tests.test_wizard_step4 import SITE, Step4Case

WIDTHS = (390, 768, 1024, 1440)
#: the narrowest content column the guide gets: a 390 px phone minus the shell's side padding
PHONE_COLUMN = 390 - 2 * 12


def rules(selector_part):
    """{selector: declarations} for every rule in STYLES whose selector mentions ``selector_part``."""
    found = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", web.STYLES):
        if selector_part in selector:
            found[selector.strip()] = body
    return found


class TestResponsive(Step4Case):
    def test_the_watcher_guide_link_fits_in_the_settings_column(self):
        self.configure()
        page = web.render_settings(self.state, "watcher")
        tab = re.search(r'<div class="ab-tabpanel" id="ab-tab-watcher" data-tab="watcher">.*?</section>',
                        page, re.S).group(0)
        self.assertRegex(tab, r'<div class="ab-acct-who ab-setup-guide"><a href="/setup/downloading">')
        self.assertIn("overflow-wrap:anywhere", rules(".ab-setup-guide")[".ab-setup-guide a"])
        self.assertRegex(tab, r'<div class="ab-acct-row ab-watch-actions">[^<]*<button')
        self.assertIn("flex-wrap:wrap", rules(".ab-watch-actions")[".ab-watch-actions"])
        self.assertIn("min-width:0", rules(".ab-main")[".ab-main"])
        self.assertIn("*{box-sizing:border-box", web.STYLES)
        drawer = re.search(r"@media\(max-width:(\d+)px\)\{\s*\.ab-navbtn.*?\.ab-rail\{position:fixed",
                           web.STYLES, re.S)
        breakpoint = int(drawer.group(1))
        rail = int(re.search(r"\.ab-rail\{width:(\d+)px", web.STYLES).group(1))
        column = re.search(r'<section class="ab-settings" style="padding:(\d+)px;max-width:(\d+)px;">',
                           page)
        padding, ceiling = map(int, column.groups())
        for width in WIDTHS:
            with self.subTest(width=width):
                available = width - (0 if width <= breakpoint else rail)
                # The column is auto-width with a ceiling; padding is inside its border box.
                self.assertGreaterEqual(min(available, ceiling) - 2 * padding, PHONE_COLUMN - 24)

    def test_the_settings_page_does_not_name_a_tracker_site(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                if configured:
                    self.configure()
                page = web.render_settings(self.state, "watcher").lower()
                self.assertNotIn(SITE, page)

    def pages(self):
        self.configure()
        self.check_again(["2001:db8:ffff:ffff:ffff:ffff:ffff:ffff"])  # the longest address there is
        for step in ("1", "2", "3", "4", "done"):
            yield step, self.get(f"{wizard.PATH}?step={step}")[1]

    @staticmethod
    def guide(page):
        return re.search(r'<div class="ab-wiz">.*</div>', page, re.S).group(0)

    def test_every_box_and_choice_is_capped_at_its_container(self):
        css = rules(".ab-wiz")
        self.assertIn("width:100%", css[".ab-wiz .ab-in"])
        self.assertIn("box-sizing:border-box", css[".ab-wiz .ab-in"])
        self.assertIn("max-width:100%", css[".ab-wiz select.ab-in"])
        for step, page in self.pages():
            for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", self.guide(page)):
                if re.search(r'type="(checkbox|radio)"', tag):
                    continue
                with self.subTest(step=step, control=tag[:60]):
                    self.assertIn('class="ab-in"', tag)

    def test_no_wizard_rule_is_wider_than_a_phone(self):
        for selector, body in rules("ab-wiz").items():
            for prop, px in re.findall(r"(?<![-\w])((?:min-)?width):(\d+)px", body):
                with self.subTest(selector=selector, prop=prop):
                    self.assertLessEqual(int(px), PHONE_COLUMN)

    def test_every_row_of_buttons_wraps(self):
        css = rules(".ab-wiz")
        self.assertIn("flex-wrap:wrap", css[".ab-wiz .ab-acct-row"])
        self.assertIn("flex-wrap:wrap", css[".ab-wiz-steps"])
        for step, page in self.pages():
            guide = self.guide(page)
            for button in re.findall(r'<(?:button|a) class="btn[^"]*"[^>]*id="([^"]+)"', guide):
                with self.subTest(step=step, button=button):
                    before = guide[:guide.index(f'id="{button}"')]
                    self.assertGreater(before.rfind('class="ab-acct-row'), before.rfind("</div>"),
                                       "every button sits in a wrapping row")

    def test_the_search_settings_group_can_shrink(self):
        self.assertIn("min-width:0", rules(".ab-wiz-group")[".ab-wiz-group"])

    def test_long_unbroken_text_breaks(self):
        css = rules(".ab-wiz")
        for selector in (".ab-wiz code", ".ab-wiz-ip", ".ab-wiz .ab-fb,.ab-wiz .ab-test-detail",
                         ".ab-wiz-checks li", ".ab-wiz-title"):
            with self.subTest(selector=selector):
                self.assertIn("overflow-wrap:anywhere", css[selector])
        for step, page in self.pages():
            with self.subTest(step=step):
                if step in ("3", "done"):
                    self.assertIn('<code class="ab-wiz-ip">2001:db8:ffff:ffff:ffff:ffff:ffff:ffff</code>',
                                  page)

    def test_the_rail_leaves_the_layout_below_1080_and_the_column_fits_beside_it_at_1440(self):
        drawer = re.search(r"@media\(max-width:(\d+)px\)\{\s*\.ab-navbtn.*?\.ab-rail\{position:fixed",
                           web.STYLES, re.S)
        self.assertIsNotNone(drawer)
        breakpoint = int(drawer.group(1))
        rail = int(re.search(r"\.ab-rail\{[^}]*?(?<![-\w])width:(\d+)px", web.STYLES).group(1))
        column_rule = rules(".ab-wiz")[".ab-wiz"]
        # the column only has a ceiling: below it, it is as wide as the space it gets, and it may shrink
        self.assertNotRegex(column_rule, r"(?<![-\w])width:")
        self.assertIn("min-width:0", column_rule)
        column = int(re.search(r"max-width:(\d+)px", column_rule).group(1))
        for width in WIDTHS:
            with self.subTest(width=width):
                beside = 0 if width <= breakpoint else rail
                space = width - beside - 2 * 24
                self.assertGreaterEqual(space, PHONE_COLUMN - 24, "a usable column at every width")
                if width > breakpoint:
                    self.assertLessEqual(column, space, "beside the rail the whole column fits")

    def test_every_class_the_guide_uses_is_ab_prefixed(self):
        for step, page in self.pages():
            for attr in re.findall(r'class="([^"]*)"', self.guide(page)):
                for name in attr.split():
                    with self.subTest(step=step, name=name):
                        self.assertTrue(name.startswith("ab-") or name == "btn", name)
