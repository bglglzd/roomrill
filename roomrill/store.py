"""Small simulator snapshots: device CAS and controller state commit together."""

import contextlib
import hashlib
from pathlib import Path
import sqlite3

from .validation import Error, encoded, require

APPLICATION = 1381125196


class Store:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def initialize(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("xb"):
                pass
        except FileExistsError as exc:
            raise Error("exists", "Ledger exists; choose another path or reopen it") from exc
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION}")
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "CREATE TABLE snapshot (id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL, sha256 TEXT NOT NULL)"
            )
            raw = encoded(state)
            connection.execute(
                "INSERT INTO snapshot VALUES(1,?,?)", (raw, hashlib.sha256(raw).hexdigest())
            )
            connection.commit()
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self, *, write=False):
        require(self.path.is_file(), "not_initialized", "Initialize a new ledger first")
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None
        )
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            require(
                connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION
                and connection.execute("PRAGMA user_version").fetchone()[0] == 1,
                "database_format",
                "Unsupported ledger format; preserve the file",
            )
            require(
                connection.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                "integrity",
                "SQLite integrity failure; preserve the file",
            )
            columns = [r[1] for r in connection.execute("PRAGMA table_info(snapshot)")]
            require(
                columns == ["id", "body", "sha256"],
                "database_format",
                "Unsupported snapshot schema",
            )
            rows = connection.execute("SELECT id,body,sha256 FROM snapshot").fetchall()
            require(len(rows) == 1 and rows[0][0] == 1, "integrity", "Invalid snapshot inventory")
            raw, checksum = rows[0][1:]
            require(
                type(raw) is bytes and hashlib.sha256(raw).hexdigest() == checksum,
                "integrity",
                "Snapshot checksum mismatch; preserve the file",
            )
            # Operational input limits do not cap the retained private ledger.
            import json

            state = json.loads(raw)
            yield state
            if write:
                new = encoded(state)
                if new != raw:
                    connection.execute(
                        "UPDATE snapshot SET body=?,sha256=? WHERE id=1",
                        (new, hashlib.sha256(new).hexdigest()),
                    )
            connection.commit()
        except Error:
            connection.rollback()
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError) as exc:
            connection.rollback()
            raise Error(
                "integrity", "Cannot read or commit this ledger; preserve the original"
            ) from exc
        finally:
            connection.close()
