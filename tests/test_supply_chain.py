"""v0.15.1: CI actions are pinned to commit SHAs, permissions are least-privilege, the
image is rebuilt weekly and the base image is pinned by digest."""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


class WorkflowTests(unittest.TestCase):
    def test_every_uses_is_a_full_sha(self):
        self.assertTrue(WORKFLOWS)
        for wf in WORKFLOWS:
            for line in wf.read_text().splitlines():
                found = re.search(r"uses:\s*([^\s#]+)", line)
                if found and not found.group(1).startswith("./"):
                    with self.subTest(workflow=wf.name, uses=found.group(1)):
                        self.assertRegex(found.group(1), r"@[0-9a-f]{40}$")

    def test_top_level_permissions_are_read_only(self):
        for wf in WORKFLOWS:
            text = wf.read_text()
            with self.subTest(workflow=wf.name):
                self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n")

    def test_every_job_declares_its_permissions(self):
        for wf in WORKFLOWS:
            text = wf.read_text().split("\njobs:\n", 1)[1]
            jobs = re.split(r"(?m)^  (?=[\w-]+:\n)", text)[1:]
            for job in jobs:
                with self.subTest(workflow=wf.name, job=job.split(":", 1)[0]):
                    self.assertIn("\n    permissions:\n", job)

    def test_weekly_schedule(self):
        text = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
        self.assertRegex(text, r"schedule:\n\s+#.*\n\s+- cron: \"[^\"]+ \* \* \d\"")
        self.assertIn("no-cache: ${{ github.event_name == 'schedule' }}", text)


class DockerfileTests(unittest.TestCase):
    def test_base_image_pinned_by_digest(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertRegex(text, r"(?m)^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$")
        self.assertIn("apt-get upgrade -y", text)

    def test_compose_is_hardened_and_library_left_to_owner(self):
        text = (ROOT / "docker-compose.yml").read_text()
        for needle in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "- /tmp"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?m)^      - \S+:/library:rw$")
        self.assertRegex(text, r"(?m)^      # - \S+:/library:ro$")

    def test_template_is_hardened(self):
        text = (ROOT / "templates" / "audiobind.xml").read_text()
        extra = re.search(r"<ExtraParams>(.*)</ExtraParams>", text).group(1)
        for needle in ("--read-only", "--tmpfs /tmp", "--cap-drop=ALL", "no-new-privileges:true"):
            self.assertIn(needle, extra)


if __name__ == "__main__":
    unittest.main()
