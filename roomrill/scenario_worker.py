"""Owned JSON-line worker for the fixed synthetic scenario adapter.

The fixture seed and clock hooks are test controls, never public device APIs.
Only the operations listed here can cross the process boundary.
"""

import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from .engine import Engine, initial
from .validation import Error, iso, loads, require, stamp

ROOMS = {"meeting-a": "meeting"}
PAYLOAD_KEYS = {"id", "room", "targets", "priority", "duration_seconds", "restore"}


def mapped_payload(body):
    """Rename documented vocabulary without discarding untrusted fields."""
    require(type(body) is dict, "invalid_input", "Lease body must be an object")
    # Unknown owner/role/other keys reach the real closed product boundary.
    result = copy.deepcopy(body)
    if "restore" in result:
        require("restore_policy" not in result, "invalid_input", "Conflicting policy fields")
        result["restore_policy"] = result.pop("restore")
    if type(result.get("room")) is str:
        result["room"] = ROOMS.get(result["room"], result["room"])
    return result


def only(data, allowed, required=()):
    require(
        type(data) is dict and set(data) <= set(allowed) and set(required) <= set(data),
        "adapter_input",
        "Unknown or missing worker fields",
    )


class Worker:
    def __init__(self, path, source_time):
        self.source_time = stamp(source_time)
        self.engine = Engine(path, clock=lambda: self.source_time)
        self.identity = {
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "instance": uuid.uuid4().hex,
        }

    def snapshot(self):
        with self.engine.store.transaction() as state:
            raw = copy.deepcopy(state)
        return {"raw": raw, "state": self.engine.state()}

    def seed(self, data):
        only(data, {"epoch", "device", "observed", "clock_mode"}, {"epoch"})
        mode = data.get("clock_mode", "fixture")
        require(mode in ("fixture", "operational_injected_utc"), "adapter_input", "Clock mode")
        state = initial("demo" if mode == "fixture" else "operational", stamp(data["epoch"]))
        state["automatic_delivery"] = False
        for kind in ("device", "observed"):
            for scope, item in data.get(kind, {}).items():
                require(scope in ("light", "media"), "adapter_input", "Unknown seed scope")
                only(item, {"value", "version"}, {"value", "version"})
                state["rooms"]["meeting"]["scopes"][scope][kind].update(copy.deepcopy(item))
        self.engine._validate(state)
        self.engine.store.initialize(state)

    def execute(self, action, data):
        engine = self.engine
        if action == "seed":
            self.seed(data)
        elif action == "time":
            only(data, {"at"}, {"at"})
            self.source_time = stamp(data["at"])
            with engine.store.transaction(write=True) as state:
                require(
                    self.source_time >= stamp(state["clock"]), "adapter_input", "Fixture rollback"
                )
                if state["mode"] == "demo":
                    # Advancing a test instant must not reconcile or apply commands.
                    state["clock"] = iso(self.source_time)
        elif action in ("submit", "raw_submit"):
            fields = (
                {"body", "operator_context"}
                if action == "submit"
                else {"document", "operator_context"}
            )
            only(data, fields, fields)
            body = loads(data["document"]) if action == "raw_submit" else data["body"]
            return engine.submit(mapped_payload(body), data["operator_context"])
        elif action == "source":
            only(data, {"at"}, {"at"})
            self.source_time = stamp(data["at"])
        elif action == "settle":
            only(data, set())
            engine.delivery_mode(True)
            try:
                for _ in range(20):
                    with engine.store.transaction() as state:
                        before = copy.deepcopy(state)
                    engine.reconcile()
                    with engine.store.transaction() as state:
                        after = copy.deepcopy(state)
                    for snapshot in (before, after):
                        snapshot.pop("last_reconcile_ms", None)
                    if before == after:
                        break
                else:
                    raise Error("adapter_unstable", "Reconciliation did not settle")
            finally:
                engine.delivery_mode(False)
        elif action == "plan":
            only(data, set())
            engine.reconcile()
        elif action == "deliver":
            only(data, {"command_id"}, {"command_id"})
            return engine.deliver(data["command_id"])
        elif action in ("manual", "external"):
            only(data, {"scope", "value"}, {"scope", "value"})
            return getattr(engine, action)("meeting", data["scope"], data["value"])
        elif action == "link":
            only(data, {"scope", "online"}, {"scope", "online"})
            require(data["scope"] in ("light", "media"), "adapter_input", "Unknown link scope")
            engine.link("meeting", data["online"])
        elif action == "cancel":
            only(data, {"lease_id"}, {"lease_id"})
            return engine.cancel(data["lease_id"])
        elif action == "rollback":
            only(data, {"requested_time"}, {"requested_time"})
            with engine.store.transaction() as state:
                delta = (stamp(data["requested_time"]) - stamp(state["clock"])).total_seconds()
            require(delta.is_integer(), "adapter_input", "Whole fixture seconds required")
            engine.advance(int(delta))
        elif action == "inspect":
            only(data, set())
        else:
            raise Error("adapter_input", "Unknown worker action")
        return None

    def request(self, request):
        only(request, {"id", "action", "data"}, {"id", "action", "data"})
        response = {"request_id": request["id"], "identity": self.identity}
        try:
            response["result"] = self.execute(request["action"], request["data"])
            response["ok"] = True
        except Error as exc:
            response.update(ok=False, error={"code": exc.code, "message": str(exc)})
        if self.engine.store.path.exists():
            response.update(self.snapshot())
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--source-time", required=True)
    args = parser.parse_args()
    worker = Worker(args.db, args.source_time)
    here = Path(__file__).resolve()
    print(
        json.dumps(
            {
                "hello": worker.identity,
                "module": str(here),
                "sha256": hashlib.sha256(here.read_bytes()).hexdigest(),
            }
        ),
        flush=True,
    )
    while True:
        line = sys.stdin.buffer.readline(131073)
        if not line:
            break
        require(
            len(line) <= 131072 and line.endswith(b"\n"), "adapter_input", "IPC line exceeds limit"
        )
        request = loads(line)
        print(
            json.dumps(worker.request(request), allow_nan=False, separators=(",", ":")), flush=True
        )


if __name__ == "__main__":
    main()
