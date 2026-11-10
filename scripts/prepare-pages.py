"""Validate and deploy an accepted Roomrill demo archive without rebuilding."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import zipfile

STATIC = {"index.html", "app.js", "app.css", "mark.svg", "icon.svg", "recorded.json"}
OWNERSHIP = "roomrill-demo-output.json"
PERMITTED = STATIC | {"LICENSE", OWNERSHIP}
MAX_BYTES = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bounded(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("File exceeds the supported size")
    return data


def document(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Unsupported JSON constant: " + value)

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def inventory(rows, names, key):
    if not isinstance(rows, list) or len(rows) != len(names):
        raise ValueError("Unexpected file inventory")
    result = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {key, "bytes", "sha256"}
            or not isinstance(row[key], str)
            or row[key] not in names
            or row[key] in result
            or type(row["bytes"]) is not int
            or not 0 <= row["bytes"] <= MAX_BYTES
            or not valid_hash(row["sha256"])
        ):
            raise ValueError("Invalid or duplicate inventory row")
        result[row[key]] = row
    return result


def recorded_example(data):
    if (
        not isinstance(data, dict)
        or set(data) != {"format", "scope", "stages"}
        or type(data["format"]) is not int
        or data["format"] != 1
        or data["scope"] != "Recorded synthetic engine states; no hardware or live controls"
        or not isinstance(data["stages"], list)
        or not 1 <= len(data["stages"]) <= 32
    ):
        raise ValueError("Expected a labelled recorded synthetic engine demonstration")
    seen = set()
    for stage in data["stages"]:
        if (
            not isinstance(stage, dict)
            or set(stage) != {"id", "label", "state"}
            or not isinstance(stage["id"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", stage["id"])
            or stage["id"] in seen
            or not isinstance(stage["label"], str)
            or not stage["label"]
            or not isinstance(stage["state"], dict)
            or stage["state"].get("mode") != "synthetic-local-simulator"
        ):
            raise ValueError("Unsupported recorded stage")
        seen.add(stage["id"])

    def public_value(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower().replace("-", "_") in {
                    "token",
                    "access_token",
                    "api_key",
                    "password",
                    "secret",
                    "endpoint",
                }:
                    raise ValueError("Recording contains credential or live endpoint fields")
                public_value(child)
        elif isinstance(value, list):
            for child in value:
                public_value(child)
        elif isinstance(value, str) and re.search(
            r"(?:https?|wss?)://|(?:^|[/\\])(?:evidence|\.git|\.venv)(?:[/\\]|$)",
            value,
            re.IGNORECASE,
        ):
            raise ValueError("Recording contains a live URL or private path")

    public_value(data)


def prepare(manifest_path, archive_path, source_sha, manifest_sha256, output):
    if not isinstance(source_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("Expected full source SHA")
    if not valid_hash(manifest_sha256):
        raise ValueError("Expected accepted manifest SHA256")
    if output.exists() or output.is_symlink():
        raise ValueError("Choose a fresh output directory")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("Choose an existing non-symlink output parent")
    manifest_bytes = bounded(manifest_path, 128 * 1024)
    if digest(manifest_bytes) != manifest_sha256:
        raise ValueError("Accepted release manifest digest differs")
    manifest = document(manifest_bytes)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("product") != "roomrill"
        or manifest.get("source_sha") != source_sha
    ):
        raise ValueError("Release product/source differs from the accepted checkout")
    version = manifest.get("version")
    if version != "0.1.0":
        raise ValueError("Unsupported release version")
    archive_name = f"roomrill-demo-{version}.zip"
    artifacts = inventory(
        manifest.get("artifacts"),
        {archive_name, f"roomrill-{version}.tar.gz", f"roomrill-{version}-py3-none-any.whl"},
        "name",
    )
    archive_bytes = bounded(archive_path, MAX_BYTES)
    artifact = artifacts[archive_name]
    if len(archive_bytes) != artifact["bytes"] or digest(archive_bytes) != artifact["sha256"]:
        raise ValueError("Demo archive differs from the accepted artifact")
    expected = inventory(manifest.get("static_files"), STATIC, "path")
    if not valid_hash(manifest.get("license_sha256")) or not valid_hash(
        manifest.get("ownership_sha256")
    ):
        raise ValueError("Missing license/ownership digest")
    files = {}
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        entries = archive.infolist()
        if len(entries) != len(PERMITTED) or sum(item.file_size for item in entries) > MAX_BYTES:
            raise ValueError("Unexpected archive size/inventory")
        for item in entries:
            name = item.filename.removeprefix("roomrill-demo/")
            mode = item.external_attr >> 16
            if (
                item.filename != "roomrill-demo/" + name
                or item.orig_filename != item.filename
                or name not in PERMITTED
                or name in files
                or item.is_dir()
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or item.flag_bits & 1
                or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or item.file_size > MAX_BYTES
                or item.file_size > max(1, item.compress_size) * 200
            ):
                raise ValueError("Unsupported archive member")
            data = archive.read(item)
            if len(data) != item.file_size:
                raise ValueError("Archive member size differs")
            if name in expected and (
                len(data) != expected[name]["bytes"] or digest(data) != expected[name]["sha256"]
            ):
                raise ValueError("Static file digest differs: " + name)
            files[name] = data
    if set(files) != PERMITTED:
        raise ValueError("Incomplete archive inventory")
    if digest(files[OWNERSHIP]) != manifest["ownership_sha256"]:
        raise ValueError("Demo ownership digest differs")
    ownership = document(files[OWNERSHIP])
    if ownership != {
        "format": "roomrill-demo-output-v1",
        "product": "roomrill",
        "version": version,
        "source_sha": source_sha,
        "files": manifest["static_files"],
        "license_sha256": manifest["license_sha256"],
    }:
        raise ValueError("Demo ownership manifest differs")
    if (
        digest(files["LICENSE"]) != manifest["license_sha256"]
        or files["LICENSE"] != (ROOT / "LICENSE").read_bytes()
    ):
        raise ValueError("Release license differs from source")
    recorded_example(document(files["recorded.json"]))
    # All archive/manifest checks finish before creating any output path.
    output.mkdir()
    for name, data in files.items():
        with (output / name).open("xb") as stream:
            stream.write(data)
    return {
        "source_sha": source_sha,
        "manifest_sha256": manifest_sha256,
        "archive_sha256": digest(archive_bytes),
        "files": len(files),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                prepare(
                    args.manifest, args.archive, args.source_sha, args.manifest_sha256, args.out
                ),
                indent=2,
            )
        )
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, RecursionError) as error:
        parser.exit(1, "Accepted demo refused: " + str(error) + "\n")
