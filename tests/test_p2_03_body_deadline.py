import socket
import threading
import unittest
from unittest import mock

from audiobind.web import Handler


class RequestBodyDeadline(unittest.TestCase):
    def test_drips_cannot_extend_json_or_form_deadline(self):
        for route, body in (
            ("_read_json", b'{"action":"convert"}'),
            ("_handle_login", b"username=reader"),
            ("_handle_setup", b"username=reader"),
        ):
            server, client = socket.socketpair()
            with self.subTest(route=route), server, client:
                with server.makefile("rb") as rfile:
                    handler = object.__new__(Handler)
                    handler.connection = server
                    handler.rfile = rfile
                    handler.headers = {
                        "Content-Length": str(len(body)),
                        "Content-Type": "application/x-www-form-urlencoded",
                    }
                    handler._send = mock.Mock()
                    handler._origin_refusal = mock.Mock(return_value=None)
                    handler._setup_pending = mock.Mock(return_value=route == "_handle_login")
                    handler.BODY_DEADLINE = 0.1
                    server.settimeout(1)
                    stop = threading.Event()

                    def drip(payload, sender, finished):
                        for byte in payload:
                            if finished.wait(0.025):
                                return
                            sender.sendall(bytes([byte]))

                    thread = threading.Thread(target=drip, args=(body, client, stop))
                    thread.start()
                    try:
                        getattr(handler, route)()
                    finally:
                        stop.set()
                        thread.join()
                    handler._send.assert_called_once_with(408, b"request body timed out", "text/plain")
                    self.assertTrue(handler.close_connection)
