"""Execute fixed authored synthetic acceptance cases in isolated worker processes.

Usage: python -m roomrill.scenarios --cases examples/scenarios-v1.json --output NEWDIR
This measures the declared simulator cases, not blind model accuracy or hardware safety.
"""

import argparse
import copy
import datetime as dt
import hashlib
import importlib.metadata
import json
import platform
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from .validation import Error, stamp

SCOPES = {"light", "media"}
ACTION_FIELDS = {
    "submit": {"body", "operator_context"},
    "settle": set(),
    "snapshot": {"capture_as"},
    "manual_brightness": {"brightness", "provenance"},
    "manual_device_event": {"scope", "value", "provenance"},
    "external_device_event": {"scope", "value", "provenance"},
    "set_link": {"scope", "online"},
    "plan": {"capture_command_as", "scope"},
    "deliver": {"command_ref"},
    "advance_clock": set(),
    "reopen_process": {"same_database"},
    "cancel": {"lease_id"},
    "attempt_clock_rollback": {"requested_time"},
    "inspect": set(),
    "reject_requests": {"operator_context", "bodies", "requests"},
    "reject_raw_json": {"operator_context", "documents"},
    "attempt_mutation_with_utc_source": {"source_time", "body", "operator_context"},
    "inspect_with_utc_source": {"source_time"},
}
EXPECT_FIELDS = {
    "device",
    "observed",
    "winners",
    "ownership",
    "automatic_receipt",
    "losers",
    "sequence_order",
    "suppressed",
    "unsuppressed",
    "captured_command",
    "pending",
    "automatic_application_receipts",
    "delivery",
    "application_receipts_for_command",
    "closure_proof",
    "events",
    "lease_fields",
    "different_command_ids",
    "clock",
    "no_device_write_since",
    "same_domain_state_as",
    "prior_receipts_preserved",
    "process_identity_changed",
    "receipt_replay_of",
    "error",
    "error_visible",
    "rejected",
    "idempotent",
}
NESTED = {
    "automatic_receipt": {
        "kind",
        "lease_id",
        "scope",
        "command_ref",
        "before",
        "after",
        "before_version",
        "after_version",
        "logical_time",
        "closure_proof_required",
    },
    "captured_command": {
        "kind",
        "scope",
        "lease_id",
        "value",
        "expected_version",
        "issued_at",
        "deadline",
    },
    "closure_proof": {
        "ref",
        "baseline",
        "last_ack_value",
        "last_ack_version",
        "ended_lease_id",
        "restore",
        "uninterrupted_ownership",
    },
    "delivery": {"applied_now", "reason_visible", "reason_in"},
    "ownership": {"open", "baseline", "last_ack_version", "deciding_lease_id", "restore"},
    "lease_fields": {"expires_at", "sequence_unchanged"},
    "events": {"kind", "lease_id", "scope", "human_attribution"},
    "losers": {"lease_id", "scope", "winner_id", "reason"},
}


class AdapterError(Exception):
    """A scenario instruction cannot be represented by this adapter."""


def fields(data, allowed, label):
    if type(data) is not dict or not set(data) <= set(allowed):
        raise AdapterError(f"Unknown or invalid {label} fields: {data!r}")


def write_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def domain(raw):
    return {key: value for key, value in raw.items() if key not in {"events", "last_reconcile_ms"}}


def receipts(raw):
    return {key: value for key, value in raw["commands"].items() if value["status"] == "applied"}


def room(raw):
    return raw["rooms"]["meeting"]["scopes"]


def utc_now():
    return dt.datetime.now(dt.UTC).isoformat()


class Controller:
    """One bounded, task-owned subprocess; no shell, server or foreground window."""

    def __init__(self, database, at, evidence):
        self.database, self.evidence = database.absolute(), evidence
        self.count = 0
        self.processes = []
        self.transcript = (evidence / "ipc.jsonl").open("x", encoding="utf-8")
        try:
            self.start(at)
        except Exception:
            self.close()
            raise

    def record(self, value):
        self.transcript.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        self.transcript.flush()

    def start(self, at):
        command = [sys.executable]
        if sys.flags.isolated:
            command.append("-I")
        command += [
            "-B",
            "-u",
            "-m",
            "roomrill.scenario_worker",
            "--db",
            str(self.database),
            "--source-time",
            at,
        ]
        stderr_path = self.evidence / f"worker-{len(self.processes) + 1}.stderr.txt"
        self.stderr = stderr_path.open("xb")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.lines = queue.Queue()
        process, lines = self.process, self.lines

        def read_lines():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=read_lines, daemon=True).start()
        entry = {
            "command": command,
            "launcher_pid": process.pid,
            "started_at_utc": utc_now(),
            "cwd": str(Path.cwd()),
            "database": str(self.database),
        }
        self.processes.append(entry)
        self.record({"process_start": entry})
        hello = self.receive()
        identity = hello.get("hello", {})
        # Windows virtual-environment executables may be redirector parents.
        if process.pid not in (identity.get("pid"), identity.get("parent_pid")):
            raise AdapterError("Worker process identity does not match owned process tree")
        entry.update(pid=identity["pid"], hello=hello)
        self.record({"process_identity": entry})

    def receive(self):
        try:
            line = self.lines.get(timeout=30)
        except queue.Empty as exc:
            raise AdapterError("Worker response timed out after30 seconds") from exc
        if line is None:
            raise AdapterError(f"Owned worker exited: {self.process.poll()}")
        if len(line) > 8 * 1024 * 1024:
            raise AdapterError("Worker response exceeds8MiB")
        return json.loads(line)

    def call(self, action, **data):
        self.count += 1
        request = {"id": self.count, "action": action, "data": data}
        raw = json.dumps(request, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(raw) > 65536:
            raise AdapterError("Worker request exceeds64KiB")
        started = time.perf_counter()
        self.process.stdin.write(raw)
        self.process.stdin.flush()
        response = self.receive()
        if response.get("request_id") != self.count:
            raise AdapterError("Worker response ID mismatch")
        self.record(
            {
                "request": request,
                "response": response,
                "wall_ms": (time.perf_counter() - started) * 1000,
            }
        )
        return response

    def stop(self):
        if getattr(self, "process", None) is None:
            return
        process = self.process
        process.stdin.close()
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=5)
        self.processes[-1].update(exit_code=code, stopped_at_utc=utc_now())
        self.record({"process_stop": self.processes[-1]})
        process.stdout.close()
        self.stderr.close()
        self.process = None

    def restart(self, at):
        old = self.processes[-1]
        self.stop()
        self.start(at)
        new = self.processes[-1]
        return (
            old["pid"] != new["pid"]
            and old["hello"]["hello"]["instance"] != new["hello"]["hello"]["instance"]
            and old["exit_code"] == 0
            and old["database"] == new["database"]
        )

    def close(self):
        self.stop()
        self.transcript.close()


class Case:
    def __init__(self, spec, defaults, epoch, output):
        self.spec, self.output = spec, output
        self.aliases, self.snapshots, self.original_leases = {}, {}, {}
        self.assertions, self.steps, self.seen_receipts = [], [], set()
        self.metrics = {
            "unwanted_restorations": 0,
            "expired_desired_target_executions": 0,
            "invalid_automatic_applications": 0,
            "automatic_application_receipts": 0,
        }
        fields(
            spec.get("initial_override", {}),
            {"device", "observed", "clock_mode"},
            "initial_override",
        )
        seed = {
            "epoch": epoch,
            "device": defaults["device"],
            "observed": defaults["observed"],
            "clock_mode": "fixture",
            **spec.get("initial_override", {}),
        }
        self.controller = Controller(output / "case.sqlite", epoch, output)
        try:
            self.current = self.controller.call("seed", **seed)
            self.must_ok(self.current)
        except Exception:
            self.controller.close()
            raise
        self.defaults = defaults

    @staticmethod
    def must_ok(response):
        if not response["ok"]:
            raise AdapterError(f"Adapter operation failed: {response['error']}")

    def check(self, path, expected, actual, timestamp=False):
        if timestamp:
            equal = (
                type(expected) is str and type(actual) is str and stamp(expected) == stamp(actual)
            )
        else:
            equal = type(expected) is type(actual) and expected == actual
        self.assertions.append(
            {"path": path, "expected": expected, "actual": actual, "passed": equal}
        )

    def partial(self, label, expected, actual, allowed, timestamps=()):
        fields(expected, allowed, label)
        for key, value in expected.items():
            self.check(f"{label}.{key}", value, actual.get(key), key in timestamps)

    def command_view(self, command):
        return {
            "kind": "closure" if command["kind"] == "restore" else command["kind"],
            "scope": command["scope"],
            "lease_id": command["lease_id"],
            "value": command["target"],
            "expected_version": command["expected_version"],
            "issued_at": command["issued_at"],
            "deadline": command["deadline"],
        }

    def receipt_view(self, command):
        return {
            "kind": self.command_view(command)["kind"],
            "lease_id": command["lease_id"],
            "scope": command["scope"],
            "command_ref": command["id"],
            "before": command["before"]["value"],
            "after": command["after"]["value"],
            "before_version": command["before"]["version"],
            "after_version": command["after"]["version"],
            "logical_time": command["applied_at"],
            "closure_proof_required": bool(command.get("closure_proof")),
        }

    def prove_receipts(self, response):
        """Check actual new writes independently of the case's selected checkpoint."""
        raw = response["raw"]
        for command_id, command in receipts(raw).items():
            if command_id in self.seen_receipts:
                continue
            self.seen_receipts.add(command_id)
            self.metrics["automatic_application_receipts"] += 1
            lease = raw["leases"][command["lease_id"]]
            applied = stamp(command["applied_at"])
            valid = (
                command["id"] == command_id
                and command["before"]["version"] == command["expected_version"]
                and command["after"]["version"] == command["before"]["version"] + 1
                and command["after"]["value"] == command["target"]
                and command["after"]["last_command"] == command_id
                and stamp(command["issued_at"]) <= applied < stamp(command["deadline"])
                and command["generation"] == raw["generation"]
            )
            if command["kind"] == "desired":
                expired = not stamp(lease["created_at"]) <= applied < stamp(lease["expires_at"])
                self.metrics["expired_desired_target_executions"] += int(expired)
                scope = command["scope"]
                winner = next(r for r in response["state"]["rooms"] if r["id"] == command["room"])[
                    "scopes"
                ][scope]["winner"]
                valid = (
                    valid
                    and not expired
                    and not lease["cancelled"]
                    and scope not in lease["suppressed_scopes"]
                    and winner is not None
                    and winner["id"] == lease["id"]
                    and command["target"] == lease["targets"][scope]
                )
            else:
                proof = command.get("closure_proof")
                eligible = [
                    item
                    for item in raw["leases"].values()
                    if item["room"] == command["room"]
                    and command["scope"] in item["targets"]
                    and command["scope"] not in item["suppressed_scopes"]
                    and not item["cancelled"]
                    and stamp(item["created_at"]) <= applied < stamp(item["expires_at"])
                ]
                closure_valid = bool(
                    proof
                    and proof["restore_policy"] == "compare"
                    and not eligible
                    and proof["fence"] == command["fence"]
                    and proof["lease_id"] == command["lease_id"]
                    and proof["baseline"] == command["target"]
                    and proof["last_version"] == command["before"]["version"]
                    and proof["last_value"] == command["before"]["value"]
                )
                self.metrics["unwanted_restorations"] += int(not (valid and closure_valid))
                valid = valid and closure_valid
            self.metrics["invalid_automatic_applications"] += int(not valid)
            self.check(f"global.receipt.{command_id}.valid", True, bool(valid))

    def observe(self, expected, response, context):
        fields(expected, EXPECT_FIELDS, "expectation")
        raw, state = response["raw"], response["state"]
        slots = room(raw)
        public = next(r for r in state["rooms"] if r["id"] == "meeting")["scopes"]
        all_receipts = receipts(raw)
        for key, value in expected.items():
            if key in ("device", "observed"):
                fields(value, SCOPES, key)
                for scope, wanted in value.items():
                    self.partial(f"{key}.{scope}", wanted, slots[scope][key], {"value", "version"})
            elif key in ("winners", "pending"):
                fields(value, SCOPES, key)
                for scope, wanted in value.items():
                    item = public[scope]["winner"]
                    actual = (
                        (item["id"] if item else None)
                        if key == "winners"
                        else bool(public[scope]["pending"])
                    )
                    self.check(f"{key}.{scope}", wanted, actual)
            elif key == "ownership":
                fields(value, SCOPES, key)
                for scope, wanted in value.items():
                    own = slots[scope]["ownership"]
                    actual = {"open": own is not None}
                    if own:
                        actual.update(
                            baseline=own["baseline"],
                            last_ack_version=own["last_version"],
                            deciding_lease_id=own["lease_id"],
                            restore=own["restore_policy"],
                        )
                    self.partial(f"ownership.{scope}", wanted, actual, NESTED[key])
            elif key in ("suppressed", "unsuppressed"):
                fields(value, SCOPES, key)
                for scope, lease_ids in value.items():
                    for lease_id in lease_ids:
                        item = raw["leases"].get(lease_id)
                        self.check(
                            f"{key}.{scope}.{lease_id}.targeted_lease_present",
                            True,
                            item is not None and scope in item["targets"],
                        )
                        actual = item is not None and scope in item["suppressed_scopes"]
                        self.check(f"{key}.{scope}.{lease_id}", key == "suppressed", actual)
            elif key == "losers":
                for index, wanted in enumerate(value):
                    fields(wanted, NESTED[key], key)
                    item = next(
                        (item for item in state["leases"] if item["id"] == wanted["lease_id"]), None
                    )
                    resolution = item["resolution"].get(wanted["scope"], {}) if item else {}
                    actual = {
                        "lease_id": item["id"] if item else None,
                        "scope": wanted["scope"],
                        **resolution,
                    }
                    self.partial(f"losers.{index}", wanted, actual, NESTED[key])
            elif key == "sequence_order":
                actual = sorted(value, key=lambda lease_id: raw["leases"][lease_id]["sequence"])
                self.check(key, value, actual)
                self.check(
                    key + ".unique", len(value), len({raw["leases"][i]["sequence"] for i in value})
                )
            elif key == "captured_command":
                command = self.aliases[context["capture_command_as"]]["command"]
                self.partial(
                    key, value, self.command_view(command), NESTED[key], {"issued_at", "deadline"}
                )
            elif key == "different_command_ids":
                ids = [self.aliases[alias]["command"]["id"] for alias in value]
                self.check(key, len(ids), len(set(ids)))
            elif key == "closure_proof":
                fields(value, NESTED[key], key)
                captured = self.aliases[value["ref"]]
                command, own = captured["command"], captured["ownership"]
                actual = {
                    "ref": value["ref"],
                    "baseline": own["baseline"] if own else None,
                    "last_ack_value": own["last_value"] if own else None,
                    "last_ack_version": own["last_version"] if own else None,
                    "ended_lease_id": own["lease_id"] if own else None,
                    "restore": own["restore_policy"] if own else None,
                    "uninterrupted_ownership": bool(own and own["fence"] == command["fence"]),
                }
                self.partial(key, value, actual, NESTED[key])
            elif key == "automatic_receipt":
                fields(value, NESTED[key], key)
                wanted = copy.deepcopy(value)
                if "command_ref" in wanted:
                    wanted["command_ref"] = self.aliases[wanted["command_ref"]]["command"]["id"]
                candidates = []
                for receipt in all_receipts.values():
                    view = self.receipt_view(receipt)
                    if all(
                        view[k] == wanted[k]
                        for k in ("kind", "lease_id", "scope", "command_ref")
                        if k in wanted
                    ):
                        candidates.append(receipt)
                self.check(key + ".present", True, bool(candidates))
                if candidates:
                    selected = candidates[-1]
                    context.setdefault("selected_receipt_ids", []).append(selected["id"])
                    self.partial(
                        key, wanted, self.receipt_view(selected), NESTED[key], {"logical_time"}
                    )
            elif key == "automatic_application_receipts":
                self.check(key, value, len(all_receipts))
            elif key == "application_receipts_for_command":
                fields(value, {"ref", "count"}, key)
                command_id = self.aliases[value["ref"]]["command"]["id"]
                self.check(key, value["count"], int(command_id in all_receipts))
            elif key == "delivery":
                fields(value, NESTED[key], key)
                command_id = self.aliases[context["command_ref"]]["command"]["id"]
                command = response.get("result") or raw["commands"][command_id]
                before_receipts = receipts(context["before"])
                actual = {
                    "applied_now": command_id in all_receipts and command_id not in before_receipts,
                    "reason_visible": bool(command.get("reason")),
                    "reason_in": command.get("reason"),
                }
                for subkey, wanted in value.items():
                    if subkey == "reason_in":
                        self.check("delivery.reason_in", True, actual[subkey] in wanted)
                        context["actual_delivery_reason"] = actual[subkey]
                    else:
                        self.check("delivery." + subkey, wanted, actual[subkey])
            elif key == "receipt_replay_of":
                command_id = self.aliases[value]["command"]["id"]
                original = receipts(context["before"]).get(command_id)
                actual = all_receipts.get(command_id)
                self.check(key + ".original_present", True, original is not None)
                self.check(key, original, actual)
                self.check(key + ".response", original, response.get("result"))
            elif key == "events":
                for index, wanted in enumerate(value):
                    fields(wanted, NESTED[key], key)
                    candidates = []
                    for item in raw["events"]:
                        item = copy.deepcopy(item)
                        if (
                            item["kind"] == "target_already_satisfied"
                            and item.get("owned") is False
                        ):
                            item["kind"] = "fulfilled_noop"
                        elif (
                            item["kind"] in ("external_version_observed", "external_device_change")
                            and item.get("attribution") == "external_unattributed"
                        ):
                            item["kind"] = "external_unattributed"
                            identities = [
                                item.get(name) for name in ("human_attribution", "owner", "actor")
                            ]
                            item["human_attribution"] = next(
                                (name for name in identities if name is not None), None
                            )
                        if all(
                            type(item.get(k)) is type(v) and item.get(k) == v
                            for k, v in wanted.items()
                        ):
                            candidates.append(item)
                    self.check(f"events.{index}.present", True, bool(candidates))
                    if candidates:
                        context.setdefault("selected_event_sequences", []).append(
                            candidates[-1]["sequence"]
                        )
            elif key == "lease_fields":
                for lease_id, wanted in value.items():
                    item = raw["leases"].get(lease_id, {})
                    actual = {
                        "expires_at": item.get("expires_at"),
                        "sequence_unchanged": item.get("sequence")
                        == self.original_leases.get(lease_id, {}).get("sequence"),
                    }
                    self.partial(
                        f"lease_fields.{lease_id}", wanted, actual, NESTED[key], {"expires_at"}
                    )
            elif key == "clock":
                self.check(key, value, raw["clock"], True)
            elif key == "same_domain_state_as":
                original = domain(self.snapshots[value]["raw"])
                actual = domain(raw)
                # Digests keep assertion summaries small; full snapshots remain in IPC/step evidence.
                self.check(
                    key,
                    hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest(),
                    hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest(),
                )
            elif key == "no_device_write_since":
                old = self.snapshots[value]["raw"]
                self.check(
                    key + ".device",
                    {s: room(old)[s]["device"] for s in SCOPES},
                    {s: slots[s]["device"] for s in SCOPES},
                )
                self.check(key + ".receipts", receipts(old), all_receipts)
            elif key == "prior_receipts_preserved":
                if type(value) is str:
                    old = self.snapshots[value]["raw"]
                elif value is True:
                    old = context["before"]
                else:
                    raise AdapterError("Unsupported prior_receipts_preserved value")
                old_receipts = receipts(old)
                self.check(key, old_receipts, {k: all_receipts.get(k) for k in old_receipts})
            elif key == "process_identity_changed":
                self.check(key, value, context.get("process_identity_changed", False))
            elif key == "error":
                self.check(key, value, response.get("error", {}).get("code"))
            elif key in ("error_visible", "rejected"):
                actual = (
                    not response["ok"]
                    if key == "rejected"
                    else bool(response.get("error", {}).get("message"))
                )
                self.check(key, value, actual)
            elif key == "idempotent":
                result = response.get("result")
                old = context["before"]["leases"].get(context["body"]["id"])
                self.check(key, value, bool(response["ok"] and old is not None and result == old))
            else:
                raise AdapterError(f"Unimplemented expectation: {key}")

    def action(self, step):
        action = step["action"]
        if action not in ACTION_FIELDS:
            raise AdapterError(f"Unknown action: {action}")
        fields(
            step, ACTION_FIELDS[action] | {"id", "action", "at", "expect", "each_expect"}, "step"
        )
        self.current = self.controller.call("time", at=step["at"])
        self.must_ok(self.current)
        context = {**step, "before": copy.deepcopy(self.current["raw"])}
        if action in ("submit", "attempt_mutation_with_utc_source"):
            if "source_time" in step:
                self.must_ok(self.controller.call("source", at=step["source_time"]))
            response = self.controller.call(
                "submit", body=step["body"], operator_context=step["operator_context"]
            )
        elif action in ("settle", "plan"):
            response = self.controller.call(action)
            self.must_ok(response)
            if action == "plan":
                scope = step["scope"]
                if scope not in SCOPES:
                    raise AdapterError("Unknown plan scope")
                candidates = [
                    c
                    for c in response["raw"]["commands"].values()
                    if c["room"] == "meeting" and c["scope"] == scope and c["status"] == "pending"
                ]
                if len(candidates) != 1:
                    raise AdapterError(f"Expected one real pending command, got {len(candidates)}")
                alias = step["capture_command_as"]
                if alias in self.aliases:
                    raise AdapterError("Command alias reuse")
                self.aliases[alias] = {
                    "command": copy.deepcopy(candidates[0]),
                    "ownership": copy.deepcopy(room(response["raw"])[scope]["ownership"]),
                }
        elif action == "deliver":
            command_id = self.aliases[step["command_ref"]]["command"]["id"]
            context["actual_command_id"] = command_id
            response = self.controller.call("deliver", command_id=command_id)
        elif action in ("manual_device_event", "external_device_event", "manual_brightness"):
            provenance = step["provenance"]
            fields(provenance, {"kind", "actor"}, "provenance")
            if action == "manual_brightness":
                brightness = step["brightness"]
                if type(brightness) is not int:
                    raise AdapterError("Brightness must be an integer")
                scope, target = "light", {"on": brightness > 0, "brightness": brightness}
            else:
                scope, target = step["scope"], step["value"]
            response = self.controller.call(
                "external" if action == "external_device_event" else "manual",
                scope=scope,
                value=target,
            )
        elif action == "set_link":
            response = self.controller.call("link", scope=step["scope"], online=step["online"])
        elif action == "cancel":
            response = self.controller.call("cancel", lease_id=step["lease_id"])
        elif action == "attempt_clock_rollback":
            response = self.controller.call("rollback", requested_time=step["requested_time"])
        elif action == "reopen_process":
            if step["same_database"] is not True:
                raise AdapterError("Reopen requires the same database")
            context["process_identity_changed"] = self.controller.restart(step["at"])
            response = self.controller.call("inspect")
        elif action in ("snapshot", "inspect", "advance_clock", "inspect_with_utc_source"):
            if "source_time" in step:
                self.must_ok(self.controller.call("source", at=step["source_time"]))
            response = self.controller.call("inspect")
            if action == "snapshot":
                alias = step["capture_as"]
                if alias in self.snapshots:
                    raise AdapterError("Snapshot alias reuse")
                self.snapshots[alias] = copy.deepcopy(response)
        elif action in ("reject_requests", "reject_raw_json"):
            attempts = []
            if action == "reject_raw_json":
                requests = [
                    {"document": text, "operator_context": step["operator_context"]}
                    for text in step["documents"]
                ]
            elif "requests" in step:
                requests = step["requests"]
            else:
                requests = [
                    {"body": body, "operator_context": step["operator_context"]}
                    for body in step["bodies"]
                ]
            for index, request in enumerate(requests):
                fields(request, {"document", "body", "operator_context"}, "rejection request")
                begin = len(self.assertions)
                response = self.controller.call(
                    "raw_submit" if action == "reject_raw_json" else "submit", **request
                )
                self.observe(step["each_expect"], response, context)
                self.prove_receipts(response)
                attempts.append(
                    {
                        "index": index,
                        "request": request,
                        "response": response,
                        "assertions": copy.deepcopy(self.assertions[begin:]),
                    }
                )
            if not attempts:
                raise AdapterError("Rejection batch must contain attempts")
            context["attempts"] = attempts
        else:
            raise AdapterError(f"Unimplemented action: {action}")
        if "each_expect" in step and action not in ("reject_requests", "reject_raw_json"):
            raise AdapterError("each_expect is only valid for independent rejection attempts")
        expected = step.get("expect", {})
        if (
            not response["ok"]
            and not (expected.keys() & {"error", "rejected", "error_visible"})
            and action not in ("reject_requests", "reject_raw_json")
        ):
            self.check("action.accepted", True, False)
        self.observe(expected, response, context)
        self.prove_receipts(response)
        for lease_id, item in response["raw"]["leases"].items():
            self.original_leases.setdefault(lease_id, copy.deepcopy(item))
        self.current = response
        return response, context

    def run(self):
        try:
            for step in self.spec["steps"]:
                started = time.perf_counter()
                begin = len(self.assertions)
                record = {"id": step["id"], "instruction": step, "started_at_utc": utc_now()}
                try:
                    response, context = self.action(step)
                    record.update(response=response, observation_context=context)
                except (
                    AdapterError,
                    Error,
                    KeyError,
                    TypeError,
                    ValueError,
                    OSError,
                    StopIteration,
                ) as exc:
                    record["adapter_error"] = {"type": type(exc).__name__, "message": str(exc)}
                    self.check("adapter.completed", True, False)
                    # A malformed instruction must not leave later assertions looking successful.
                    record["assertions"] = self.assertions[begin:]
                    record["wall_ms"] = (time.perf_counter() - started) * 1000
                    self.steps.append(record)
                    write_json(
                        self.output / f"step-{len(self.steps):02d}-{step['id']}.json", record
                    )
                    break
                record["assertions"] = self.assertions[begin:]
                record["passed"] = all(item["passed"] for item in record["assertions"])
                record["wall_ms"] = (time.perf_counter() - started) * 1000
                self.steps.append(record)
                write_json(self.output / f"step-{len(self.steps):02d}-{step['id']}.json", record)
        finally:
            self.controller.close()
        failed = [
            dict(step=step["id"], **assertion)
            for step in self.steps
            for assertion in step["assertions"]
            if not assertion["passed"]
        ]
        result = {
            "id": self.spec["id"],
            "title": self.spec["title"],
            "passed": not failed and len(self.steps) == len(self.spec["steps"]),
            "expected_steps": len(self.spec["steps"]),
            "executed_steps": len(self.steps),
            "assertions": len(self.assertions),
            "failures": failed,
            "metrics": self.metrics,
            "command_aliases": self.aliases,
            "processes": self.controller.processes,
        }
        write_json(self.output / "result.json", result)
        return result


def environment():
    module = Path(__file__).resolve()
    git = None
    for parent in module.parents:
        if (parent / ".git").exists() and (parent / "roomrill/scenarios.py").resolve() == module:
            completed = subprocess.run(
                ["git", "-C", str(parent), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            status = subprocess.run(
                ["git", "-C", str(parent), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            git = {
                "source_sha": completed.stdout.strip(),
                "dirty_paths": status.stdout.splitlines(),
            }
            break
    try:
        version = importlib.metadata.version("roomrill")
    except importlib.metadata.PackageNotFoundError:
        version = "source-checkout"
    return {
        "python": sys.version,
        "executable": sys.executable,
        "isolated": bool(sys.flags.isolated),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "sqlite": sqlite3.sqlite_version,
        "package_version": version,
        "git": git,
        "module_hashes": {
            name: digest(module.with_name(name))
            for name in (
                "scenarios.py",
                "scenario_worker.py",
                "engine.py",
                "store.py",
                "validation.py",
            )
        },
    }


def run(cases_path, output):
    cases_path, output = Path(cases_path).resolve(), Path(output).resolve()
    corpus_bytes = cases_path.read_bytes()
    corpus = json.loads(corpus_bytes)
    if corpus.get("schema_version") != "roomrill.authored-acceptance.v1":
        raise AdapterError("Unsupported authored scenario schema")
    cases = corpus["cases"]
    if not cases:
        raise AdapterError("An empty scenario set cannot pass")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise AdapterError("Duplicate case IDs")
    for case in cases:
        if not case["id"].isascii() or not case["id"].isalnum():
            raise AdapterError("Unsafe case ID")
        step_ids = [step["id"] for step in case["steps"]]
        if len(set(step_ids)) != len(step_ids):
            raise AdapterError("Duplicate step IDs")
        if any(
            not step_id.isascii() or not all(c.isalnum() or c in "-_" for c in step_id)
            for step_id in step_ids
        ):
            raise AdapterError("Unsafe step ID")
    output.mkdir(parents=True, exist_ok=False)
    (output / "input-cases.json").write_bytes(corpus_bytes)
    started = time.perf_counter()
    metadata = {
        "schema": "roomrill.scenario-execution.v1",
        "set_kind": corpus["set_kind"],
        "claims": {"blind_model_accuracy": False, "hardware_safety": False},
        "input_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "case_count": len(cases),
        "ordered_step_count": sum(len(case["steps"]) for case in cases),
        "started_at_utc": utc_now(),
        "environment": environment(),
        "command": [sys.executable, *sys.orig_argv[1:]],
        "adapter_mapping": {
            "room": {"meeting-a": "meeting"},
            "restore": "restore_policy",
            "closure_kind": "restore",
            "receipts": "actual applied command records",
            "set_link": "room-level Engine.link; this corpus constrains offline light only",
            "at": "trusted fixture Store clock advance without reconciliation; operational case uses Engine clock injection",
            "manual_actor": "synthetic case provenance retained, Engine does not store a human identity",
        },
    }
    write_json(output / "environment.json", metadata)
    results = []
    for spec in cases:
        case_output = output / spec["id"]
        case_output.mkdir()
        try:
            result = Case(
                spec, corpus["default_initial"], corpus["fixture_epoch"], case_output
            ).run()
        except (
            AdapterError,
            Error,
            KeyError,
            TypeError,
            ValueError,
            OSError,
            StopIteration,
        ) as exc:
            result = {
                "id": spec["id"],
                "passed": False,
                "executed_steps": 0,
                "failures": [{"path": "case.initialization", "actual": str(exc), "passed": False}],
            }
            write_json(case_output / "result.json", result)
        results.append(result)
    failed = [result["id"] for result in results if not result["passed"]]
    summary = {
        **metadata,
        "finished_at_utc": utc_now(),
        "wall_seconds": time.perf_counter() - started,
        "passed": not failed,
        "passed_cases": len(results) - len(failed),
        "failed_cases": failed,
        "executed_steps": sum(result["executed_steps"] for result in results),
        "metrics": {
            key: sum(result.get("metrics", {}).get(key, 0) for result in results)
            for key in (
                "unwanted_restorations",
                "expired_desired_target_executions",
                "invalid_automatic_applications",
                "automatic_application_receipts",
            )
        },
        "cases": results,
    }
    write_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="A new directory; existing evidence is refused"
    )
    args = parser.parse_args()
    try:
        result = run(args.cases, args.output)
    except (AdapterError, FileExistsError, OSError, ValueError, KeyError) as exc:
        print(f"Scenario run refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"{result['passed_cases']}/{result['case_count']} cases passed; {result['executed_steps']}/{result['ordered_step_count']} steps; "
        f"failed={','.join(result['failed_cases']) or 'none'}; evidence={args.output.resolve()}"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
