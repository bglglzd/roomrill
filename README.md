# Roomrill

Durable room-state leases with priority arbitration, manual overrides and version-guarded commands in a synthetic light/media simulator. A temporary request cannot restore an old state over a newer manual choice in the simulator's transactional model.

Python 3.11 or newer; no runtime dependencies.

```sh
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/roomrill demo --serve
```

On Windows use `.venv\Scripts\python.exe` and `.venv\Scripts\roomrill.exe`. Open the printed numeric loopback URL. The local workspace persists in `.roomrill/demo.sqlite`; use `--db another.sqlite` before `demo` for a fresh path. Ctrl+C stops that server.

The simulator uses a labelled fixture clock and SQLite-local atomic version checks. It does not control hardware or claim that Home Assistant's ordinary service API provides compare-and-swap. An optional OpenAI-compatible model endpoint can propose a typed draft; the operator must review and create the lease explicitly.

[Specification](docs/spec.md) · [Architecture](docs/architecture.md) · [Operations](docs/operations.md) · [Evaluation](docs/evaluation.md) · [MIT license](LICENSE)
