"""Persistence, competing OS processes and the actual operational timer loop."""

import http.client
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from roomrill.demo import recording, request
from roomrill.engine import Engine
from roomrill.validation import Error

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DurableRuntime(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="roomrill-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "ledger.sqlite"

    def test_command_history_uses_creation_order_instead_of_identifier_order(self):
        engine = Engine(self.path)
        engine.initialize(demo=True)
        serial = [9999]

        def descending_id(prefix):
            serial[0] -= 1
            return f"{prefix}-{serial[0]}"

        with patch("roomrill.engine.fresh_id", side_effect=descending_id):
            for index in range(6):
                engine.create(
                    request(
                        f"lease-{index}",
                        priority=index,
                        targets={"light": {"on": True, "brightness": index + 10}},
                    )
                )
        reopened = Engine(self.path).state()
        self.assertEqual(
            [command["lease_id"] for command in reopened["commands"]],
            [f"lease-{index}" for index in reversed(range(6))],
        )

    def test_manual_override_and_expiry_do_not_cross_room_boundaries(self):
        engine = Engine(self.path)
        engine.initialize(demo=True)
        engine.create(
            request(
                "meeting-call",
                duration_seconds=10,
                targets={"light": {"on": True, "brightness": 60}},
            )
        )
        engine.create(
            request(
                "studio-call",
                room="studio",
                duration_seconds=20,
                targets={"light": {"on": True, "brightness": 90}},
            )
        )
        studio = engine.state()["rooms"][1]["scopes"]["light"]
        engine.manual("meeting", "light", {"on": True, "brightness": 45})
        engine.advance(10)
        state = engine.state()
        self.assertEqual(
            state["rooms"][0]["scopes"]["light"]["device"]["value"], {"on": True, "brightness": 45}
        )
        self.assertEqual(state["rooms"][1]["scopes"]["light"]["device"], studio["device"])
        self.assertEqual(state["rooms"][1]["scopes"]["light"]["winner"]["id"], "studio-call")
        engine.advance(10)
        self.assertEqual(
            engine.state()["rooms"][1]["scopes"]["light"]["device"]["value"],
            {"on": False, "brightness": 0},
        )

    def test_two_real_cli_writers_preserve_both_requests_and_one_current_winner(self):
        engine = Engine(self.path)
        engine.initialize(demo=True)
        processes = []
        try:
            for name, priority, brightness in (("A", 10, 40), ("B", 20, 80)):
                document = self.root / (name + ".json")
                document.write_text(
                    json.dumps(
                        request(
                            name,
                            priority=priority,
                            targets={"light": {"on": True, "brightness": brightness}},
                        )
                    ),
                    encoding="utf-8",
                )
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-m",
                            "roomrill",
                            "--db",
                            str(self.path),
                            "create",
                            str(document),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=FLAGS,
                    )
                )
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertIn(json.loads(stdout)["id"], ("A", "B"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        state = Engine(self.path).state()
        self.assertEqual({item["id"] for item in state["leases"]}, {"A", "B"})
        self.assertEqual({item["sequence"] for item in state["leases"]}, {1, 2})
        light = state["rooms"][0]["scopes"]["light"]
        self.assertEqual(light["winner"]["id"], "B")
        self.assertEqual(light["device"]["value"], {"on": True, "brightness": 80})
        read = subprocess.run(
            [sys.executable, "-B", "-m", "roomrill", "--db", str(self.path), "state"],
            capture_output=True,
            timeout=10,
            creationflags=FLAGS,
        )
        self.assertEqual(read.returncode, 0, read.stderr.decode())
        self.assertEqual(json.loads(read.stdout), state)

    def test_corrupted_snapshot_fails_closed_and_preserves_original_file(self):
        engine = Engine(self.path)
        engine.initialize(demo=True)
        engine.create(request())
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE snapshot SET body=? WHERE id=1", (b"{}",))
            connection.commit()
        finally:
            connection.close()
        original = self.path.read_bytes()
        for operation in (engine.state, engine.reconcile):
            with self.assertRaises(Error) as failure:
                operation()
            self.assertEqual(failure.exception.code, "integrity")
            self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaises(Error) as failure:
            engine.initialize(demo=True)
        self.assertEqual(failure.exception.code, "exists")
        self.assertEqual(self.path.read_bytes(), original)

    def test_recording_is_generated_by_engine_and_contains_no_expired_desired_application(self):
        result = recording()
        self.assertEqual(len(result["stages"]), 6)
        final = result["stages"][-1]["state"]
        light = final["rooms"][0]["scopes"]["light"]
        media = final["rooms"][0]["scopes"]["media"]
        self.assertEqual(light["device"]["value"], {"on": True, "brightness": 45})
        self.assertEqual(media["device"]["value"], "stopped")
        self.assertIsNone(light["winner"])
        self.assertEqual(
            [
                item["kind"]
                for item in final["commands"]
                if item["status"] == "applied" and item["applied_at"] == final["clock"]
            ],
            ["restore"],
        )

    def test_operational_server_expires_without_fixture_advance_and_closes_normally(self):
        engine = Engine(self.path)
        engine.initialize()
        script = (
            "import sys,threading;from roomrill.engine import Engine;from roomrill.server import serve;"
            "stop=threading.Event();"
            "threading.Thread(target=lambda:(sys.stdin.readline(),stop.set()),daemon=True).start();"
            "serve(Engine(sys.argv[1]),stop_event=stop)"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(self.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=FLAGS,
        )
        line = queue.Queue()
        reader = threading.Thread(target=lambda: line.put(process.stdout.readline()), daemon=True)
        reader.start()
        connection = None
        try:
            url = line.get(timeout=10).decode().strip()
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
            port = int(url.split(":")[-1].removesuffix("/"))
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/session")
            response = connection.getresponse()
            token = json.loads(response.read())["token"]
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            data = request(duration_seconds=1, targets={"light": {"on": True, "brightness": 80}})
            connection.request(
                "POST",
                "/api/leases",
                json.dumps(data).encode(),
                {"Content-Type": "application/json", "X-Roomrill-Token": token},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            created = json.loads(response.read())
            self.assertEqual(created["rooms"][0]["scopes"]["light"]["device"]["version"], 1)
            connection.close()
            deadline = time.monotonic() + 4.5
            state = created
            while time.monotonic() < deadline:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/api/state")
                response = connection.getresponse()
                state = json.loads(response.read())
                connection.close()
                if state["rooms"][0]["scopes"]["light"]["device"]["version"] == 2:
                    break
                time.sleep(0.05)
            light = state["rooms"][0]["scopes"]["light"]
            self.assertEqual(light["device"]["version"], 2)
            self.assertEqual(light["device"]["value"], {"on": False, "brightness": 0})
            self.assertEqual(state["mode"], "local-simulator")
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.stdin.write(b"stop\n")
                process.stdin.flush()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                self.fail("Owned operational server failed to close normally")
            reader.join(2)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertFalse(reader.is_alive())


if __name__ == "__main__":
    unittest.main()
