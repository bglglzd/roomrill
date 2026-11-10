"""Measure full reconciliation on a declared small synthetic ledger."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import time

from roomrill.demo import request
from roomrill.engine import Engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new private output directory")
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 10000:
        parser.error("iterations must be1..10000")
    args.output.mkdir(parents=True, exist_ok=False)
    engine = Engine(args.output / "workload.sqlite")
    engine.initialize(demo=True)
    for index in range(20):
        engine.create(
            request(
                f"benchmark-{index:02d}",
                role="operator",
                priority=index,
                duration_seconds=3600,
                targets={"light": {"on": True, "brightness": index * 4 + 10}},
            )
        )
    timings = []
    for _ in range(args.iterations):
        started = time.perf_counter_ns()
        engine.reconcile()
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
    source = Path(__file__).resolve().parents[1]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True)
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=source, capture_output=True, text=True
    )
    state = engine.state()
    ordered = sorted(timings)
    result = {
        "scope": "Actual full Engine.reconcile including SQLite validation/encoding/commit; synthetic device only, not hardware/model latency",
        "workload": {
            "rooms": 2,
            "active_leases": 20,
            "targeted_scopes": 1,
            "iterations": args.iterations,
            "fixture_clock_fixed": state["clock"],
            "history_retained": True,
        },
        "environment": {
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        },
        "source": {
            "head": sha.stdout.strip() if sha.returncode == 0 else None,
            "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
            "component_sha256": {
                name: hashlib.sha256((source / "roomrill" / name).read_bytes()).hexdigest()
                for name in ("engine.py", "store.py", "validation.py")
            },
        },
        "milliseconds": {
            "median": statistics.median(timings),
            "p95": ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)],
            "min": min(timings),
            "max": max(timings),
        },
        "samples_ms": timings,
        "final_winner": state["rooms"][0]["scopes"]["light"]["winner"]["id"],
        "limitations": [
            "One fixed small workload on one machine",
            "No concurrency or network in the timed region",
            "Snapshot validation and retained history cost grow with ledger size",
            "Fixture duration is not a wall-time performance measurement",
        ],
    }
    with (args.output / "result.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps({key: value for key, value in result.items() if key != "samples_ms"}, indent=2)
    )


if __name__ == "__main__":
    main()
