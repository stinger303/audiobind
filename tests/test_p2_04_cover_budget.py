import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from audiobind import catalog, web
from audiobind.config import CONFIG
from audiobind.state import UnitState


class PublishedCoverBudget(unittest.TestCase):
    def test_published_cover_uses_catalog_byte_budget(self):
        with TemporaryDirectory() as temp, mock.patch.object(catalog, "MAX_BODY", 16):
            root = Path(temp)
            folder = root / "Book"
            folder.mkdir()
            cover = folder / "cover.jpg"
            handler = object.__new__(web.Handler)
            handler.state = mock.Mock()
            handler.state.get.return_value = UnitState(
                path="Book", status="converted", output=str(folder / "Book.m4b"),
            )
            handler._send = mock.Mock()
            with mock.patch.object(CONFIG, "output_root", root):
                cover.write_bytes(b"x" * 16)
                handler._send_cover("", "Book")
                self.assertEqual(handler._send.call_args.args[:3], (200, b"x" * 16, "image/jpeg"))
                with cover.open("wb") as handle:
                    handle.truncate(1024 * 1024)
                handler._send_cover("", "Book")
                self.assertEqual(handler._send.call_args.args[0], 413, "oversized cover was served")
