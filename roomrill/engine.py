"""Lease arbitration and guarded device application in one durable simulator."""

import copy
import datetime as dt
import time
import uuid

from .store import Store
from .validation import encoded, identifier, integer, iso, lease, require, stamp, value

TTL = 5
SCOPES = ("light", "media")


def fresh_id(prefix):
    return prefix + "-" + uuid.uuid4().hex


def event(state, kind, **data):
    state["events"].append(
        {
            "sequence": len(state["events"]) + 1,
            "at": state["clock"],
            "kind": kind,
            **copy.deepcopy(data),
        }
    )


def initial(mode, now):
    rooms = {}
    for room_id, name in (("meeting", "Meeting room"), ("studio", "Studio")):
        scopes = {}
        for scope, target in (("light", {"on": False, "brightness": 0}), ("media", "stopped")):
            device = {"value": target, "version": 0, "last_command": None}
            scopes[scope] = {
                "device": copy.deepcopy(device),
                "observed": copy.deepcopy(device),
                "ownership": None,
            }
        rooms[room_id] = {"id": room_id, "name": name, "online": True, "scopes": scopes}
    return {
        "format": 1,
        "mode": mode,
        "clock": iso(now),
        "automatic_delivery": True,
        "sequence": 0,
        "generation": 0,
        "rooms": rooms,
        "leases": {},
        "commands": {},
        "events": [],
    }


class Engine:
    def __init__(self, path, clock=None):
        self.store = Store(path)
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def initialize(self, *, demo=False):
        self.store.initialize(
            initial(
                "demo" if demo else "operational",
                stamp("2030-01-01T09:00:00Z") if demo else self.clock(),
            )
        )
        return self.state()

    def _validate(self, state):
        require(
            state.get("format") == 1 and state.get("mode") in ("demo", "operational"),
            "integrity",
            "Unsupported controller snapshot",
        )
        stamp(state["clock"])
        require(
            set(state["rooms"]) == {"meeting", "studio"}
            and type(state["automatic_delivery"]) is bool,
            "integrity",
            "Invalid room/controller inventory",
        )
        integer(state["sequence"], 0, 2**53, "Stored sequence")
        integer(state["generation"], 0, 2**53, "Stored generation")
        for room in state["rooms"].values():
            require(
                type(room["online"]) is bool and set(room["scopes"]) == set(SCOPES),
                "integrity",
                "Invalid device inventory",
            )
            for scope, slot in room["scopes"].items():
                for kind in ("device", "observed"):
                    value(scope, slot[kind]["value"])
                    integer(slot[kind]["version"], 0, 2**53, "Device version")
                own = slot["ownership"]
                if own:
                    value(scope, own["baseline"])
                    require(
                        own["lease_id"] in state["leases"]
                        and own["fence"]
                        and own["last_version"] <= slot["device"]["version"],
                        "integrity",
                        "Invalid ownership proof",
                    )
        for lease_id, item in state["leases"].items():
            lease(item["submitted"], state["rooms"])
            require(
                lease_id == item["id"] == item["submitted"]["id"]
                and item["expires_at"] > item["created_at"],
                "integrity",
                "Invalid persisted lease",
            )
        for command_id, command in state["commands"].items():
            require(
                command["id"] == command_id
                and command["lease_id"] in state["leases"]
                and command["status"] in ("pending", "applied", "refused", "superseded"),
                "integrity",
                "Invalid persisted command",
            )
            value(command["scope"], command["target"])

    def _now(self, state):
        now = stamp(state["clock"]) if state["mode"] == "demo" else self.clock()
        require(
            now >= stamp(state["clock"]),
            "clock_rollback",
            "Clock moved backwards; reads remain available",
        )
        state["clock"] = iso(now)
        return now

    def _run(self, operation, *, bump=False, reconcile=True):
        with self.store.transaction(write=True) as state:
            self._validate(state)
            now = self._now(state)
            self._observe(state)
            before_operation = encoded(state) if bump else None
            result = operation(state, now)
            if bump and encoded(state) != before_operation:
                state["generation"] += 1
            if reconcile:
                self._reconcile(state, stamp(state["clock"]))
            self._validate(state)
            return copy.deepcopy(result)

    def _eligible(self, state, item, scope, now):
        return (
            not item["cancelled"]
            and scope in item["targets"]
            and scope not in item["suppressed_scopes"]
            and stamp(item["created_at"]) <= now < stamp(item["expires_at"])
        )

    def _winner(self, state, room_id, scope, now):
        candidates = [
            item
            for item in state["leases"].values()
            if item["room"] == room_id and self._eligible(state, item, scope, now)
        ]
        return (
            max(candidates, key=lambda item: (item["priority"], item["sequence"]))
            if candidates
            else None
        )

    def create(self, data):
        def operation(state, now):
            lease(data, state["rooms"])
            old = state["leases"].get(data["id"])
            if old:
                require(
                    encoded(old["submitted"]) == encoded(data),
                    "id_conflict",
                    "Lease ID already has different content",
                )
                return old
            state["sequence"] += 1
            item = {
                **copy.deepcopy(data),
                "submitted": copy.deepcopy(data),
                "sequence": state["sequence"],
                "created_at": iso(now),
                "expires_at": iso(now + dt.timedelta(seconds=data["duration_seconds"])),
                "cancelled": False,
                "suppressed_scopes": [],
                "satisfied": {},
            }
            state["leases"][item["id"]] = item
            event(
                state,
                "lease_created",
                lease_id=item["id"],
                owner=item["owner"],
                priority=item["priority"],
            )
            return item

        return self._run(operation, bump=True)

    def cancel(self, lease_id):
        identifier(lease_id)

        def operation(state, now):
            require(lease_id in state["leases"], "not_found", "Unknown lease")
            item = state["leases"][lease_id]
            if not item["cancelled"]:
                item["cancelled"] = True
                event(state, "lease_cancelled", lease_id=lease_id)
            return item

        return self._run(operation, bump=True)

    def _room_scope(self, state, room_id, scope):
        require(
            type(room_id) is str and room_id in state["rooms"] and scope in SCOPES,
            "invalid_input",
            "Unknown room/scope",
        )
        return state["rooms"][room_id], state["rooms"][room_id]["scopes"][scope]

    def manual(self, room_id, scope, target):
        value(scope, target)

        def operation(state, now):
            room, slot = self._room_scope(state, room_id, scope)
            device = slot["device"]
            device.update(
                value=copy.deepcopy(target), version=device["version"] + 1, last_command=None
            )
            event(
                state,
                "manual_device_change",
                room=room_id,
                scope=scope,
                value=target,
                version=device["version"],
                observed=room["online"],
            )
            return device

        return self._run(operation, bump=True)

    def external(self, room_id, scope, target):
        """An unattributed synthetic device event, with the same ownership boundary."""
        value(scope, target)

        def operation(state, now):
            room, slot = self._room_scope(state, room_id, scope)
            device = slot["device"]
            device.update(
                value=copy.deepcopy(target), version=device["version"] + 1, last_command=None
            )
            event(
                state,
                "external_device_change",
                room=room_id,
                scope=scope,
                value=target,
                version=device["version"],
                observed=room["online"],
                attribution="external_unattributed",
            )
            return device

        return self._run(operation, bump=True)

    def link(self, room_id, online):
        require(type(online) is bool, "invalid_input", "Online must be boolean")

        def operation(state, now):
            require(
                type(room_id) is str and room_id in state["rooms"], "invalid_input", "Unknown room"
            )
            state["rooms"][room_id]["online"] = online
            event(state, "link_changed", room=room_id, online=online)

        self._run(operation, bump=True)

    def delivery_mode(self, automatic):
        require(type(automatic) is bool, "invalid_input", "Automatic delivery must be boolean")
        self._run(lambda state, now: state.update(automatic_delivery=automatic))

    def advance(self, seconds):
        integer(seconds, 0, 86400, "Advance seconds")

        def operation(state, now):
            require(
                state["mode"] == "demo",
                "fixture_only",
                "Only a synthetic fixture clock can advance",
            )
            state["clock"] = iso(now + dt.timedelta(seconds=seconds))
            event(state, "fixture_clock_advanced", seconds=seconds)

        self._run(operation, bump=True)

    def reconcile(self):
        self._run(lambda state, now: None)

    def _observe(self, state):
        for room in state["rooms"].values():
            if not room["online"]:
                continue
            for scope, slot in room["scopes"].items():
                device = slot["device"]
                if device["version"] == slot["observed"]["version"]:
                    continue
                # Owned applications update observations in the same transaction.
                suppressed = []
                for item in state["leases"].values():
                    if (
                        item["room"] == room["id"]
                        and scope in item["targets"]
                        and scope not in item["suppressed_scopes"]
                    ):
                        item["suppressed_scopes"].append(scope)
                        suppressed.append(item["id"])
                slot["ownership"] = None
                slot["observed"] = copy.deepcopy(device)
                event(
                    state,
                    "external_version_observed",
                    room=room["id"],
                    scope=scope,
                    version=device["version"],
                    suppressed=suppressed,
                    attribution="external_unattributed",
                )

    def _refusal(self, state, command, now, *, offline=True):
        room, slot = self._room_scope(state, command["room"], command["scope"])
        if now >= stamp(command["deadline"]):
            return "deadline"
        if command["generation"] != state["generation"]:
            return "resolver_changed"
        winner = self._winner(state, room["id"], command["scope"], now)
        if command["kind"] == "desired":
            if (
                not winner
                or winner["id"] != command["lease_id"]
                or winner["targets"][command["scope"]] != command["target"]
            ):
                return "lease_not_current"
        else:
            if winner:
                return "new_winner"
        if not room["online"]:
            return "offline" if offline else None
        if slot["device"]["version"] != command["expected_version"]:
            return "device_version_changed"
        if command["kind"] == "restore":
            own = slot["ownership"]
            if (
                not own
                or own["fence"] != command["fence"]
                or own["last_version"] != slot["device"]["version"]
                or own["last_value"] != slot["device"]["value"]
                or own["baseline"] != command["target"]
                or own["restore_policy"] != "compare"
            ):
                return "ownership_not_proven"
        return None

    def _queue(self, state, room_id, scope, kind, item, target, now, own):
        slot = state["rooms"][room_id]["scopes"][scope]
        for command in state["commands"].values():
            if (
                command["status"] == "pending"
                and command["room"] == room_id
                and command["scope"] == scope
                and command["kind"] == kind
                and command["lease_id"] == item["id"]
                and command["target"] == target
            ):
                return
        command = {
            "id": fresh_id("command"),
            "room": room_id,
            "scope": scope,
            "kind": kind,
            "lease_id": item["id"],
            "target": copy.deepcopy(target),
            "expected_version": slot["observed"]["version"],
            "generation": state["generation"],
            "issued_at": iso(now),
            "deadline": iso(now + dt.timedelta(seconds=TTL)),
            "fence": own["fence"] if own else None,
            "status": "pending",
            "reason": "awaiting_ack" if state["rooms"][room_id]["online"] else "offline",
        }
        state["commands"][command["id"]] = command
        event(
            state,
            "command_queued",
            command_id=command["id"],
            room=room_id,
            scope=scope,
            lease_id=item["id"],
            command_kind=kind,
        )

    def _plan(self, state, now):
        self._observe(state)
        for command in state["commands"].values():
            if command["status"] == "pending":
                reason = self._refusal(state, command, now, offline=False)
                room = state["rooms"][command["room"]]
                slot = room["scopes"][command["scope"]]
                if (
                    not reason
                    and command["kind"] == "desired"
                    and room["online"]
                    and slot["observed"]["value"] == command["target"]
                ):
                    reason = "already_satisfied"
                if reason:
                    command.update(status="superseded", reason=reason)
                    event(state, "command_superseded", command_id=command["id"], reason=reason)
        for room in state["rooms"].values():
            for scope, slot in room["scopes"].items():
                winner = self._winner(state, room["id"], scope, now)
                own, observed = slot["ownership"], slot["observed"]
                if winner:
                    target = winner["targets"][scope]
                    if room["online"] and observed["value"] == target:
                        if (
                            own
                            and own["last_version"] == observed["version"]
                            and own["last_value"] == observed["value"]
                        ):
                            if own["lease_id"] != winner["id"]:
                                own.update(
                                    lease_id=winner["id"], restore_policy=winner["restore_policy"]
                                )
                                event(
                                    state,
                                    "no_op_adoption",
                                    room=room["id"],
                                    scope=scope,
                                    lease_id=winner["id"],
                                    version=observed["version"],
                                )
                        marker = str(observed["version"])
                        if winner["satisfied"].get(scope) != marker:
                            winner["satisfied"][scope] = marker
                            event(
                                state,
                                "target_already_satisfied",
                                room=room["id"],
                                scope=scope,
                                lease_id=winner["id"],
                                owned=own is not None,
                            )
                    else:
                        self._queue(state, room["id"], scope, "desired", winner, target, now, own)
                elif own:
                    if own["restore_policy"] == "leave":
                        event(
                            state,
                            "ownership_left",
                            room=room["id"],
                            scope=scope,
                            lease_id=own["lease_id"],
                        )
                        slot["ownership"] = None
                    elif (
                        own["last_version"] != observed["version"]
                        or own["last_value"] != observed["value"]
                    ):
                        event(
                            state,
                            "restore_refused",
                            room=room["id"],
                            scope=scope,
                            reason="ownership_not_proven",
                        )
                        slot["ownership"] = None
                    elif own["baseline"] == observed["value"]:
                        event(
                            state,
                            "restore_no_op",
                            room=room["id"],
                            scope=scope,
                            lease_id=own["lease_id"],
                        )
                        slot["ownership"] = None
                    else:
                        self._queue(
                            state,
                            room["id"],
                            scope,
                            "restore",
                            state["leases"][own["lease_id"]],
                            own["baseline"],
                            now,
                            own,
                        )

    def _apply(self, state, command, now):
        if command["status"] != "pending":
            return
        refusal = self._refusal(state, command, now)
        if refusal:
            command.update(status="refused", reason=refusal)
            event(state, "command_refused", command_id=command["id"], reason=refusal)
            return
        room, slot = self._room_scope(state, command["room"], command["scope"])
        before = copy.deepcopy(slot["device"])
        own = slot["ownership"]
        item = state["leases"][command["lease_id"]]
        slot["device"].update(
            value=copy.deepcopy(command["target"]),
            version=before["version"] + 1,
            last_command=command["id"],
        )
        after = copy.deepcopy(slot["device"])
        if command["kind"] == "desired":
            slot["ownership"] = {
                "fence": own["fence"] if own else fresh_id("fence"),
                "baseline": copy.deepcopy(own["baseline"] if own else before["value"]),
                "lease_id": item["id"],
                "restore_policy": item["restore_policy"],
                "last_version": after["version"],
                "last_value": copy.deepcopy(after["value"]),
            }
        else:
            slot["ownership"] = None
        slot["observed"] = copy.deepcopy(after)
        command.update(
            status="applied",
            reason="lease_current" if command["kind"] == "desired" else "owned_compare_restore",
            applied_at=iso(now),
            before=before,
            after=after,
            closure_proof=copy.deepcopy(own) if command["kind"] == "restore" else None,
        )
        event(
            state,
            "device_applied",
            command_id=command["id"],
            room=command["room"],
            scope=command["scope"],
            lease_id=command["lease_id"],
            command_kind=command["kind"],
            before=before,
            after=after,
            closure_proof=command["closure_proof"],
        )

    def _reconcile(self, state, now):
        started = time.perf_counter_ns()
        self._plan(state, now)
        if state["automatic_delivery"]:
            for command in list(state["commands"].values()):
                if command["status"] == "pending" and state["rooms"][command["room"]]["online"]:
                    self._apply(state, command, now)
            self._plan(state, now)
        state["last_reconcile_ms"] = (time.perf_counter_ns() - started) / 1_000_000

    def deliver(self, command_id):
        identifier(command_id)

        def operation(state, now):
            require(command_id in state["commands"], "not_found", "Unknown command")
            # Observe external versions before deciding to apply a delayed command.
            self._observe(state)
            command = state["commands"][command_id]
            self._apply(state, command, now)
            return command

        return self._run(operation)

    def state(self):
        with self.store.transaction() as state:
            self._validate(state)
            now = stamp(state["clock"]) if state["mode"] == "demo" else self.clock()
            out = copy.deepcopy(state)
        out["now"] = iso(now)
        out["health"] = (
            {
                "code": "clock_rollback",
                "message": "Clock moved backwards; new mutations are refused",
                "mutations_allowed": False,
            }
            if now < stamp(state["clock"])
            else {"code": "ok", "message": "Controller clock is current", "mutations_allowed": True}
        )
        out["mode"] = "synthetic-local-simulator" if state["mode"] == "demo" else "local-simulator"
        leases = []
        for item in out["leases"].values():
            item["status"] = (
                "cancelled"
                if item["cancelled"]
                else "expired"
                if now >= stamp(item["expires_at"])
                else "suppressed"
                if set(item["targets"]) <= set(item["suppressed_scopes"])
                else "active"
            )
            leases.append(item)
        out["leases"] = sorted(leases, key=lambda item: item["sequence"], reverse=True)
        rooms = []
        for room in out["rooms"].values():
            for scope, slot in room["scopes"].items():
                winner = self._winner(state, room["id"], scope, now)
                slot["winner"] = copy.deepcopy(winner)
                slot["pending"] = [
                    copy.deepcopy(c)
                    for c in state["commands"].values()
                    if c["status"] == "pending" and c["room"] == room["id"] and c["scope"] == scope
                ]
                slot["reason"] = (
                    "highest_priority_then_newer_sequence" if winner else "no_eligible_lease"
                )
            rooms.append(room)
        out["rooms"] = rooms
        out["commands"] = list(out["commands"].values())[-100:][::-1]
        out["events"] = out["events"][-100:][::-1]
        out["scope"] = "SQLite-local synthetic device CAS; no hardware actuation"
        return out
