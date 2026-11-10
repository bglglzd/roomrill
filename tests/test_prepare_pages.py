"""Accepted archive deployment boundaries; all asset fixtures are synthetic."""

from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_pages", ROOT / "scripts/prepare-pages.py")
PAGES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PAGES)
SOURCE = "a" * 40
VERSION = "0.1.0"


def encoded(value):
    return (json.dumps(value, sort_keys=True) + "\n").encode()


class AcceptedPagesTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="roomrill-pages-fixture-")
        self.addCleanup(self.owned.cleanup)
        self.path = Path(self.owned.name)
        self.archive = self.path / "accepted.zip"
        self.manifest = self.path / "release-manifest.json"
        self.output = self.path / "pages"
        self.files = {
            name: ("Private synthetic parser fixture: " + name).encode() for name in PAGES.STATIC
        }
        self.files["recorded.json"] = encoded(
            {
                "format": 1,
                "scope": "Recorded synthetic engine states; no hardware or live controls",
                "stages": [
                    {
                        "id": "baseline",
                        "label": "Synthetic parser fixture",
                        "state": {"mode": "synthetic-local-simulator"},
                    }
                ],
            }
        )
        self.files["LICENSE"] = (ROOT / "LICENSE").read_bytes()
        self.data = {
            "schema_version": 1,
            "product": "roomrill",
            "version": VERSION,
            "source_sha": SOURCE,
        }
        self.bind_static()

    def bind_static(self):
        self.data["static_files"] = [
            {"path": name, "bytes": len(self.files[name]), "sha256": PAGES.digest(self.files[name])}
            for name in sorted(PAGES.STATIC)
        ]
        self.data["license_sha256"] = PAGES.digest(self.files["LICENSE"])
        self.files[PAGES.OWNERSHIP] = encoded(
            {
                "format": "roomrill-demo-output-v1",
                "product": "roomrill",
                "version": VERSION,
                "source_sha": SOURCE,
                "files": self.data["static_files"],
                "license_sha256": self.data["license_sha256"],
            }
        )
        self.data["ownership_sha256"] = PAGES.digest(self.files[PAGES.OWNERSHIP])

    def save(self, entries=None, compression=zipfile.ZIP_DEFLATED):
        entries = entries or [
            ("roomrill-demo/" + name, data, stat.S_IFREG | 0o644)
            for name, data in self.files.items()
        ]
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, data, mode in entries:
                    info = zipfile.ZipInfo(name)
                    # Preserve deliberately malformed names even on Windows,
                    # whose ZipInfo constructor normalizes backslashes.
                    info.filename = name
                    info.compress_type = compression
                    info.external_attr = mode << 16
                    archive.writestr(info, data)
        self.archive.write_bytes(buffer.getvalue())
        self.data["artifacts"] = [
            {"name": f"roomrill-{VERSION}-py3-none-any.whl", "bytes": 0, "sha256": "0" * 64},
            {"name": f"roomrill-{VERSION}.tar.gz", "bytes": 0, "sha256": "1" * 64},
            {
                "name": f"roomrill-demo-{VERSION}.zip",
                "bytes": self.archive.stat().st_size,
                "sha256": PAGES.digest(self.archive.read_bytes()),
            },
        ]
        self.save_manifest()

    def save_manifest(self):
        self.manifest.write_bytes(encoded(self.data))

    def prepare(self, **changes):
        arguments = dict(
            manifest_path=self.manifest,
            archive_path=self.archive,
            source_sha=SOURCE,
            manifest_sha256=PAGES.digest(self.manifest.read_bytes()),
            output=self.output,
        )
        arguments.update(changes)
        return PAGES.prepare(**arguments)

    def refused(self, **changes):
        with self.assertRaises(ValueError):
            self.prepare(**changes)
        self.assertFalse(
            self.output.exists(), "Invalid input must not create partial deployment output"
        )

    def test_accepted_bytes_deploy_without_rebuild_or_input_changes(self):
        self.save()
        before = (self.archive.read_bytes(), self.manifest.read_bytes())
        result = self.prepare()
        self.assertEqual(result["files"], 8)
        self.assertEqual({path.name for path in self.output.iterdir()}, PAGES.PERMITTED)
        for name, data in self.files.items():
            self.assertEqual((self.output / name).read_bytes(), data)
        self.assertEqual((self.archive.read_bytes(), self.manifest.read_bytes()), before)

    def test_existing_output_is_retained(self):
        self.save()
        self.output.mkdir()
        (self.output / "useful.txt").write_bytes(b"retain")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual((self.output / "useful.txt").read_bytes(), b"retain")
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_wrong_manifest_digest_or_source_refuses_before_output(self):
        self.save()
        self.refused(manifest_sha256="0" * 64)
        self.refused(source_sha="b" * 40)
        self.refused(source_sha="short")

    def test_manifest_schema_and_product_are_not_inferred_from_the_zip_name(self):
        self.save()
        for key, value in (("schema_version", True), ("schema_version", 2), ("product", "other")):
            with self.subTest(field=key, value=value):
                original = self.data[key]
                self.data[key] = value
                self.save_manifest()
                self.refused()
                self.data[key] = original

    def test_changed_archive_and_static_member_digest_refuse(self):
        self.save()
        self.archive.write_bytes(self.archive.read_bytes() + b"changed")
        self.refused()
        self.files["app.js"] += b"changed"
        self.save()  # Rebind archive only; its accepted per-file digest stays old.
        self.refused()

    def test_duplicate_and_missing_members_are_not_hidden_by_inventory_length(self):
        self.save()
        rows = [
            ("roomrill-demo/" + name, data, stat.S_IFREG | 0o644)
            for name, data in self.files.items()
        ]
        rows = [row for row in rows if row[0] != "roomrill-demo/app.css"]
        rows.append(("roomrill-demo/app.js", self.files["app.js"], stat.S_IFREG | 0o644))
        self.save(rows)
        self.refused()
        self.save(rows[:-1])
        self.refused()

    def test_unknown_absolute_traversal_and_backslash_names_refuse(self):
        for bad in (
            "roomrill-demo/../escaped",
            "/index.html",
            "roomrill-demo\\index.html",
            "roomrill-demo/extra.js",
        ):
            with self.subTest(member=bad):
                rows = [
                    (
                        bad if name == "index.html" else "roomrill-demo/" + name,
                        data,
                        stat.S_IFREG | 0o644,
                    )
                    for name, data in self.files.items()
                ]
                self.save(rows)
                self.refused()
        self.assertFalse((self.path / "escaped").exists())

    def test_symlink_fifo_and_directory_modes_refuse(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFDIR | 0o755):
            with self.subTest(mode=mode):
                rows = [
                    (
                        "roomrill-demo/" + name,
                        data,
                        mode if name == "index.html" else stat.S_IFREG | 0o644,
                    )
                    for name, data in self.files.items()
                ]
                self.save(rows)
                self.refused()

    def test_uncompressed_size_and_compression_bombs_refuse(self):
        self.files["app.js"] = b"A" * (PAGES.MAX_BYTES + 1)
        self.bind_static()
        self.save()
        self.refused()
        self.files["app.js"] = b"A" * (1024 * 1024)
        self.bind_static()
        self.save()
        self.refused()

    def test_unsupported_compression_refuses_even_with_accepted_hash(self):
        self.save(compression=zipfile.ZIP_BZIP2)
        self.refused()

    def test_license_and_ownership_content_are_bound_independently(self):
        self.files["LICENSE"] = b"Different license"
        self.bind_static()
        self.save()
        self.refused()
        self.files["LICENSE"] = (ROOT / "LICENSE").read_bytes()
        self.bind_static()
        ownership = json.loads(self.files[PAGES.OWNERSHIP])
        ownership["source_sha"] = "b" * 40
        self.files[PAGES.OWNERSHIP] = encoded(ownership)
        self.data["ownership_sha256"] = PAGES.digest(self.files[PAGES.OWNERSHIP])
        self.save()
        self.refused()

    def test_recording_requires_synthetic_format_with_accepted_member_digest(self):
        self.files["recorded.json"] = encoded(
            {"format": 1, "scope": "Operational recording", "stages": []}
        )
        self.bind_static()
        self.save()
        self.refused()

    def test_recording_refuses_live_urls_private_paths_and_credential_fields(self):
        original = json.loads(self.files["recorded.json"])
        for key, value in (
            ("token", "do-not-publish"),
            ("endpoint", "http://localhost:1234/v1"),
            ("note", "https://example.invalid/live"),
            ("note", "evidence/private-ledger.sqlite"),
            ("note", "C:\\private\\.git\\config"),
        ):
            with self.subTest(field=key, value=value):
                data = deepcopy(original)
                data["stages"][0]["state"][key] = value
                self.files["recorded.json"] = encoded(data)
                self.bind_static()
                self.save()
                self.refused()

    def test_malformed_duplicate_and_boolean_inventory_rows_refuse(self):
        self.save()
        original = deepcopy(self.data)
        for mutate in (
            lambda row: row.update(bytes=True),
            lambda row: row.update(path=[]),
            lambda row: row.update(sha256="short"),
        ):
            self.data = deepcopy(original)
            mutate(self.data["static_files"][0])
            self.save_manifest()
            self.refused()
        self.data = deepcopy(original)
        self.data["static_files"][1] = self.data["static_files"][0]
        self.save_manifest()
        self.refused()
        self.manifest.write_bytes(b'{"product":"roomrill","product":"roomrill"}')
        self.refused()


if __name__ == "__main__":
    unittest.main()
