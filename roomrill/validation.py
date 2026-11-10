"""Closed inputs at the local operator and model boundaries."""

import datetime as dt
import json
import re


class Error(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise Error(code, message)


def keys(value, expected):
    require(
        type(value) is dict and set(value) == set(expected),
        "invalid_input",
        "Unexpected or missing fields",
    )


def integer(value, low, high, label):
    require(
        type(value) is int and low <= value <= high,
        "invalid_input",
        f"{label} must be an integer in {low}..{high}",
    )
    return value


def identifier(value):
    require(
        type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value),
        "invalid_input",
        "Use a short ASCII identifier",
    )
    return value


def stamp(value):
    require(
        type(value) is str and len(value) <= 40,
        "invalid_input",
        "Explicit-offset timestamp required",
    )
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(
            parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "invalid_input",
            "Explicit timezone required",
        )
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise Error("invalid_input", "Invalid timestamp") from exc


def iso(value):
    return (
        value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def value(scope, target):
    require(scope in ("light", "media"), "invalid_input", "Supported scopes are light and media")
    if scope == "media":
        require(
            type(target) is str and target in ("playing", "paused", "stopped"),
            "invalid_input",
            "Unsupported media state",
        )
    else:
        keys(target, {"on", "brightness"})
        require(type(target["on"]) is bool, "invalid_input", "Light on must be boolean")
        integer(target["brightness"], 0, 100, "Brightness")
        require(
            (target["on"] and target["brightness"] > 0)
            or (not target["on"] and target["brightness"] == 0),
            "invalid_input",
            "Off requires brightness0; on requires1..100",
        )
    return target


def draft(data, rooms):
    keys(data, {"room", "targets", "priority", "duration_seconds", "restore_policy"})
    require(type(data["room"]) is str and data["room"] in rooms, "invalid_input", "Unknown room")
    require(
        type(data["targets"]) is dict and 1 <= len(data["targets"]) <= 2,
        "invalid_input",
        "Choose light, media or both",
    )
    for scope, target in data["targets"].items():
        value(scope, target)
    integer(data["priority"], 0, 100, "Priority")
    integer(data["duration_seconds"], 1, 86400, "Duration seconds")
    require(
        type(data["restore_policy"]) is str and data["restore_policy"] in ("compare", "leave"),
        "invalid_input",
        "Choose compare or leave",
    )
    return data


def lease(data, rooms):
    keys(
        data,
        {
            "id",
            "owner",
            "role",
            "room",
            "targets",
            "priority",
            "duration_seconds",
            "restore_policy",
        },
    )
    identifier(data["id"])
    require(
        type(data["owner"]) is str
        and 1 <= len(data["owner"]) <= 64
        and data["owner"].strip() == data["owner"]
        and all(ord(c) >= 32 for c in data["owner"]),
        "invalid_input",
        "Owner must be a short plain label",
    )
    require(
        type(data["role"]) is str and data["role"] in ("member", "operator"),
        "invalid_input",
        "Unsupported role",
    )
    draft({k: v for k, v in data.items() if k not in ("id", "owner", "role")}, rooms)
    integer(data["priority"], 0, 50 if data["role"] == "member" else 100, "Role priority")
    return data


def lease_payload(data, rooms):
    """An untrusted proposal cannot supply the operator's owner or role."""
    keys(data, {"id", "room", "targets", "priority", "duration_seconds", "restore_policy"})
    identifier(data["id"])
    draft({k: v for k, v in data.items() if k != "id"}, rooms)
    return data


def loads(raw):
    def pairs(items):
        result = {}
        for key, item in items:
            require(key not in result, "invalid_input", "Duplicate JSON key")
            result[key] = item
        return result

    def constant(_):
        raise Error("invalid_input", "Nonfinite JSON number")

    try:
        require(len(raw) <= 65536, "invalid_input", "JSON exceeds64KiB")
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        encoded(result)
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Error("invalid_input", "Invalid bounded UTF8 JSON") from exc


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def read_input(path):
    with path.open("rb") as stream:
        return loads(stream.read(65537))
