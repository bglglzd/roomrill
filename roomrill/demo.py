"""Synthetic fixture and recorded states are produced by the actual engine."""

import json
from pathlib import Path
import tempfile

from .engine import Engine
from .validation import require


def request(lease_id="call", **changes):
    data = {
        "id": lease_id,
        "room": "meeting",
        "owner": "Mira",
        "role": "member",
        "priority": 30,
        "duration_seconds": 1200,
        "restore_policy": "compare",
        "targets": {"light": {"on": True, "brightness": 70}, "media": "paused"},
    }
    data.update(changes)
    return data


def workspace(path):
    engine = Engine(path)
    if not Path(path).exists():
        engine.initialize(demo=True)
        engine.create(request())
    require(
        engine.state()["mode"] == "synthetic-local-simulator",
        "fixture_only",
        "Demo cannot overwrite an operational workspace",
    )
    return engine


def recording():
    # The caller may package these outputs; no private operational ledger is read.
    with tempfile.TemporaryDirectory(prefix="roomrill-recording-") as temporary:
        engine = Engine(Path(temporary) / "demo.sqlite")
        engine.initialize(demo=True)
        stages = []

        def capture(stage_id, label):
            stages.append({"id": stage_id, "label": label, "state": engine.state()})

        capture("baseline", "Initial room")
        engine.create(request())
        capture("active", "20-minute request")
        engine.create(
            request(
                "focus",
                owner="Noor",
                priority=40,
                duration_seconds=300,
                targets={"light": {"on": True, "brightness": 90}},
            )
        )
        capture("conflict", "Higher-priority light request")
        engine.manual("meeting", "light", {"on": True, "brightness": 45})
        capture("manual", "Manual light choice retained")
        engine.link("meeting", False)
        engine.advance(1200)
        capture("offline", "Leases expire while disconnected")
        engine.link("meeting", True)
        capture("reconnected", "Reconnect: no expired desired target")
        return {
            "format": 1,
            "scope": "Recorded synthetic engine states; no hardware or live controls",
            "stages": stages,
        }


def export_recording(path):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(recording(), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
