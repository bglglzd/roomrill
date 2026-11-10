"""Actual loopback boundaries; synthetic model responses are not live inference."""

import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from roomrill.demo import request
from roomrill.engine import Engine
from roomrill import model
from roomrill.server import make_server
from roomrill.validation import Error, loads


@contextlib.contextmanager
def running(server):
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        if thread.is_alive():
            raise AssertionError("Owned test server did not close")


class LocalBoundaries(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="roomrill-boundary-")
        self.addCleanup(self.temporary.cleanup)
        self.engine = Engine(Path(self.temporary.name) / "ledger.sqlite")
        self.engine.initialize(demo=True)

    def test_nonascii_token_is_structured_refusal_without_ledger_write(self):
        before = self.engine.store.path.read_bytes()
        with running(make_server(self.engine)) as port:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            self.addCleanup(connection.close)
            connection.request(
                "POST",
                "/api/reconcile",
                b"{}",
                {"Content-Type": "application/json", "X-Roomrill-Token": "é"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertEqual(json.loads(response.read())["error"]["code"], "token_refused")
        self.assertEqual(before, self.engine.store.path.read_bytes())

    def test_origin_transfer_coding_and_duplicate_length_refuse_before_mutation(self):
        before = self.engine.store.path.read_bytes()
        with running(make_server(self.engine)) as port:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/session")
            response = connection.getresponse()
            token = json.loads(response.read())["token"]
            connection.close()
            for extra in (
                (("Origin", "https://invalid.example"),),
                (("Transfer-Encoding", "chunked"),),
                (("Content-Length", "2"),),
            ):
                with self.subTest(extra=extra):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        connection.putrequest("POST", "/api/reconcile")
                        for name, item in (
                            ("Content-Type", "application/json"),
                            ("X-Roomrill-Token", token),
                            ("Content-Length", "2"),
                            *extra,
                        ):
                            connection.putheader(name, item)
                        connection.endheaders(b"{}")
                        response = connection.getresponse()
                        self.assertGreaterEqual(response.status, 400)
                        self.assertIn(
                            json.loads(response.read())["error"]["code"],
                            ("origin_refused", "invalid_input"),
                        )
                    finally:
                        connection.close()
        self.assertEqual(before, self.engine.store.path.read_bytes())

    def test_proposal_cannot_supply_operator_authority(self):
        before = self.engine.store.path.read_bytes()
        payload = {k: v for k, v in request().items() if k not in ("owner", "role")}
        for field in ("owner", "role", "service", "starts_at"):
            with self.subTest(field=field), self.assertRaises(Error):
                self.engine.submit(
                    {**payload, field: "operator"}, {"owner": "Mira", "role": "member"}
                )
        self.assertEqual(before, self.engine.store.path.read_bytes())

    def test_duplicate_nonfinite_and_unpaired_unicode_are_refused(self):
        for raw in ('{"a":1,"a":1}', '{"a":NaN}', '{"a":1e400}', '"\\ud800"', "[" * 3000):
            with self.subTest(raw=raw[:30]), self.assertRaises(Error):
                loads(raw)

    def test_fixture_backward_advance_preserves_file(self):
        before = self.engine.store.path.read_bytes()
        with self.assertRaises(Error) as failure:
            self.engine.advance(-1)
        self.assertEqual(failure.exception.code, "clock_rollback")
        self.assertEqual(before, self.engine.store.path.read_bytes())


class SyntheticModelHTTP(unittest.TestCase):
    def setUp(self):
        self.draft = {
            "room": "meeting",
            "targets": {"light": {"on": True, "brightness": 35}},
            "priority": 10,
            "duration_seconds": 60,
            "restore_policy": "compare",
        }

    def publisher(self, *, content=None, redirect=None, slow=False, oversized=False):
        expected = self.draft if content is None else content
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                size = int(self.headers["Content-Length"])
                seen.append(json.loads(self.rfile.read(size)))
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", redirect)
                    self.end_headers()
                    return
                if slow:
                    time.sleep(2)
                raw = (
                    b"x" * 65537
                    if oversized
                    else json.dumps(
                        {
                            "model": "synthetic-fixture",
                            "choices": [{"message": {"content": json.dumps(expected)}}],
                        }
                    ).encode()
                )
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler), seen

    def test_actual_http_worker_preserves_schema_and_returns_draft_without_authority(self):
        server, seen = self.publisher()
        with (
            running(server) as port,
            patch.dict(
                "os.environ",
                {
                    "http_proxy": "http://127.0.0.1:1",
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                },
            ),
        ):
            result = model.propose(
                "Synthetic fixture request",
                endpoint=f"http://127.0.0.1:{port}/v1/chat/completions",
                model="synthetic-fixture",
                rooms=["meeting"],
            )
        self.assertEqual(result["draft"], self.draft)
        self.assertTrue(result["requires_confirmation"])
        schema = seen[0]["response_format"]["json_schema"]["schema"]
        self.assertEqual(
            list(schema["oneOf"][0]["properties"]),
            ["room", "targets", "priority", "duration_seconds", "restore_policy"],
        )
        self.assertNotIn("owner", result["draft"])

    def test_syntactically_valid_but_unsafe_model_fields_are_refused(self):
        for draft in (
            {**self.draft, "owner": "model"},
            {**self.draft, "targets": {"lock": "unlocked"}},
            {**self.draft, "targets": {"light": {"on": False, "brightness": 50}}},
        ):
            with self.subTest(draft=draft):
                server, _ = self.publisher(content=draft)
                with running(server) as port, self.assertRaises(Error):
                    model.propose(
                        "Synthetic invalid fixture",
                        endpoint=f"http://127.0.0.1:{port}/v1/chat/completions",
                        model="synthetic-fixture",
                        rooms=["meeting"],
                    )

    def test_redirect_is_not_followed(self):
        target, seen = self.publisher()
        with running(target) as target_port:
            source, _ = self.publisher(
                redirect=f"http://127.0.0.1:{target_port}/v1/chat/completions"
            )
            with running(source) as source_port, self.assertRaises(Error) as failure:
                model.propose(
                    "Synthetic redirect fixture",
                    endpoint=f"http://127.0.0.1:{source_port}/v1/chat/completions",
                    model="synthetic-fixture",
                    rooms=["meeting"],
                )
        self.assertEqual(failure.exception.code, "model_unavailable")
        self.assertEqual(seen, [])

    def test_oversized_response_is_refused(self):
        server, _ = self.publisher(oversized=True)
        with running(server) as port, self.assertRaises(Error) as failure:
            model.propose(
                "Synthetic oversized fixture",
                endpoint=f"http://127.0.0.1:{port}/v1/chat/completions",
                model="synthetic-fixture",
                rooms=["meeting"],
            )
        self.assertEqual(failure.exception.code, "model_unavailable")

    def test_wall_deadline_reaps_only_the_owned_worker(self):
        server, _ = self.publisher(slow=True)
        children = []
        original = subprocess.Popen

        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process

        started = time.monotonic()
        with (
            running(server) as port,
            patch.object(model.subprocess, "Popen", side_effect=capture),
            self.assertRaises(Error) as failure,
        ):
            model.propose(
                "Synthetic slow fixture",
                endpoint=f"http://127.0.0.1:{port}/v1/chat/completions",
                model="synthetic-fixture",
                rooms=["meeting"],
                deadline_seconds=1,
            )
        self.assertEqual(failure.exception.code, "model_timeout")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())


if __name__ == "__main__":
    unittest.main()
