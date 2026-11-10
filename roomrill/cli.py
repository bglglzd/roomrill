"""Local operator commands, shared with the browser controller."""

import argparse
import json
from pathlib import Path
import sys

from . import demo, model, server
from .engine import Engine
from .validation import Error, keys, read_input


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="roomrill", description="Durable synthetic room leases and guarded device commands"
    )
    parser.add_argument("--db", type=Path, help="Private SQLite ledger path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize an operational simulator ledger")
    commands.add_parser("state", help="Inspect state without mutation")
    create = commands.add_parser("create", help="Validate and create a lease from JSON")
    create.add_argument("file", type=Path)
    manual = commands.add_parser("manual", help="Apply a synthetic manual device event from JSON")
    manual.add_argument("file", type=Path)
    for name, help_text in (
        ("cancel", "Cancel a lease"),
        ("deliver", "Deliver one pending guarded command"),
    ):
        child = commands.add_parser(name, help=help_text)
        child.add_argument("id")
    link = commands.add_parser("link", help="Change a synthetic room connection")
    link.add_argument("room")
    link_mode = link.add_mutually_exclusive_group(required=True)
    link_mode.add_argument("--online", action="store_true")
    link_mode.add_argument("--offline", action="store_true")
    delivery = commands.add_parser(
        "delivery", help="Choose automatic or explicitly delayed acknowledgement"
    )
    delivery_mode = delivery.add_mutually_exclusive_group(required=True)
    delivery_mode.add_argument("--automatic", action="store_true")
    delivery_mode.add_argument("--manual", action="store_true")
    advance = commands.add_parser("advance", help="Advance the synthetic demo clock")
    advance.add_argument("seconds", type=int)
    commands.add_parser("reconcile", help="Observe and resolve current leases and closures")
    for name, help_text in (
        ("serve", "Run the operational local UI and reconciliation loop"),
        ("demo", "Open or resume the synthetic fixture workspace"),
    ):
        child = commands.add_parser(name, help=help_text)
        if name == "demo":
            child.add_argument("--serve", action="store_true")
        child.add_argument("--port", type=int, default=0)
        child.add_argument("--model-endpoint")
        child.add_argument("--model")
        child.add_argument("--model-key-env")
    propose = commands.add_parser("draft", help="Request a model proposal without creating a lease")
    propose.add_argument("text")
    propose.add_argument("--endpoint", required=True)
    propose.add_argument("--model", required=True)
    propose.add_argument("--key-env")
    export = commands.add_parser("record", help="Export actual labelled synthetic engine states")
    export.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    path = args.db or Path(
        ".roomrill/demo.sqlite" if args.command == "demo" else ".roomrill/ledger.sqlite"
    )
    try:
        engine = Engine(path)
        result = None
        if args.command == "init":
            result = engine.initialize()
        elif args.command == "demo":
            engine = demo.workspace(path)
            result = engine.state()
        elif args.command == "state":
            result = engine.state()
        elif args.command == "create":
            result = engine.create(read_input(args.file))
        elif args.command == "manual":
            data = read_input(args.file)
            keys(data, {"room", "scope", "value"})
            engine.manual(data["room"], data["scope"], data["value"])
        elif args.command == "cancel":
            engine.cancel(args.id)
        elif args.command == "deliver":
            result = engine.deliver(args.id)
        elif args.command == "link":
            engine.link(args.room, args.online)
        elif args.command == "delivery":
            engine.delivery_mode(args.automatic)
        elif args.command == "advance":
            engine.advance(args.seconds)
        elif args.command == "reconcile":
            engine.reconcile()
        elif args.command == "draft":
            result = model.propose(
                args.text,
                endpoint=args.endpoint,
                model=args.model,
                rooms=[r["id"] for r in engine.state()["rooms"]],
                key_env=args.key_env,
            )
        elif args.command == "record":
            demo.export_recording(args.file)
            result = {"written": str(args.file), "scope": "Recorded synthetic engine outputs"}
        if args.command == "serve" or (args.command == "demo" and args.serve):
            server.serve(
                engine,
                port=args.port,
                endpoint=args.model_endpoint,
                model_id=args.model,
                key_env=args.model_key_env,
            )
        else:
            print(
                json.dumps(
                    engine.state() if result is None else result,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
            )
        return 0
    except (Error, OSError, UnicodeError) as exc:
        print(
            json.dumps(
                {
                    "error": {
                        "code": exc.code if isinstance(exc, Error) else "file_error",
                        "message": str(exc)
                        if isinstance(exc, Error)
                        else "Cannot access the requested file",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
