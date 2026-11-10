"""Clean-source builds and the exact installable artifact, using owned temporary files."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "scripts/build-release.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="roomrill-source-fixture-")
        self.addCleanup(self.owned.cleanup)
        self.path = Path(self.owned.name)
        self.repository = self.path / "repository"
        self.repository.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        (self.repository / ".gitattributes").write_bytes(b"* text eol=lf\n")
        (self.repository / ".gitignore").write_bytes(b"ignored*\n")
        (self.repository / "source.txt").write_bytes(b"Exact source bytes\n")
        self.commit()
        self.destination = self.path / "snapshot"
        self.destination.mkdir()

    def git(self, *arguments):
        return subprocess.check_output(
            ["git", *arguments], cwd=self.repository, stderr=subprocess.PIPE
        )

    def commit(self):
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Roomrill test fixture",
            "-c",
            "user.email=248948303+bglglzd@users.noreply.github.com",
            "commit",
            "-q",
            "-m",
            "Synthetic source fixture",
        )

    def snapshot(self):
        with patch.object(BUILDER, "ROOT", self.repository):
            return BUILDER.snapshot(self.destination)

    def test_committed_inventory_ignores_local_ignored_files(self):
        (self.repository / "ignored-local.py").write_bytes(b"Not part of the source commit\n")
        sha, tree, rows = self.snapshot()
        self.assertEqual(sha, self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(tree, self.git("rev-parse", "HEAD^{tree}").decode().strip())
        self.assertEqual(
            {row["path"] for row in rows}, {".gitattributes", ".gitignore", "source.txt"}
        )
        for row in rows:
            data = (self.destination / row["path"]).read_bytes()
            self.assertEqual(row["bytes"], len(data))
            self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())

    def test_hidden_working_change_and_untracked_source_are_refused(self):
        (self.repository / "source.txt").write_bytes(b"Hidden change\n")
        self.git("update-index", "--assume-unchanged", "--", "source.txt")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        with self.assertRaisesRegex(ValueError, "Working bytes differ"):
            self.snapshot()
        self.git("update-index", "--no-assume-unchanged", "--", "source.txt")
        self.git("checkout", "--", "source.txt")
        (self.repository / "untracked.py").write_bytes(b"Not committed\n")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            self.snapshot()

    def test_git_link_entries_are_refused(self):
        # Git's index can represent a symlink even where creating one needs OS privilege.
        target = self.path / "link-target"
        target.write_bytes(b"outside")
        blob = self.git("hash-object", "-w", str(target)).decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000," + blob + ",link")
        self.git(
            "-c",
            "user.name=Roomrill test fixture",
            "-c",
            "user.email=248948303+bglglzd@users.noreply.github.com",
            "commit",
            "-q",
            "-m",
            "Synthetic symlink fixture",
        )
        self.git("config", "core.symlinks", "false")
        self.git("checkout", "--", "link")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        # Snapshot reads the committed modes independently of the local OS link support.
        with self.assertRaisesRegex(ValueError, "symlinks or submodules"):
            self.snapshot()

    def test_changed_build_configuration_and_wrong_tool_versions_refuse_before_output(self):
        (self.repository / "pyproject.toml").write_text(
            '[project]\nname="roomrill"\nversion="0.1.0"\n'
            '[build-system]\nrequires=["unaccepted-backend"]\n'
            'build-backend="setuptools.build_meta"\n',
            encoding="utf-8",
        )
        self.commit()
        output = self.path / "candidate"
        with patch.object(BUILDER, "ROOT", self.repository):
            with self.assertRaisesRegex(ValueError, "build configuration"):
                BUILDER.build(output)
            with patch.object(BUILDER, "installed_version", return_value="0.0.0"):
                with self.assertRaisesRegex(ValueError, "exact build tools"):
                    BUILDER.build(output)
        self.assertFalse(output.exists())


class ReleaseArtifactTests(unittest.TestCase):
    def test_actual_build_retains_original_bytes_and_installs_outside_checkout(self):
        with tempfile.TemporaryDirectory(prefix="roomrill-package-test-") as temporary:
            base = Path(temporary)
            repository = base / "source"
            repository.mkdir()
            # An owned committed fixture uses the actual public package and packaging files.
            for name in ("roomrill", "docs", "examples", "tests", "scripts", ".github"):
                shutil.copytree(
                    ROOT / name,
                    repository / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            for name in (
                "LICENSE",
                "README.md",
                "pyproject.toml",
                "requirements-dev.txt",
                ".gitattributes",
                ".gitignore",
                "MANIFEST.in",
            ):
                shutil.copyfile(ROOT / name, repository / name)
            for name in ("CHANGELOG.md", "CONTRIBUTING.md", "THIRD_PARTY_NOTICES.md"):
                if (ROOT / name).is_file():
                    shutil.copyfile(ROOT / name, repository / name)

            def git(*args):
                return subprocess.check_output(
                    ["git", *args], cwd=repository, stderr=subprocess.PIPE
                )

            git("init", "-q")
            git("config", "core.autocrlf", "false")
            git("add", ".")
            git(
                "-c",
                "user.name=Roomrill test fixture",
                "-c",
                "user.email=248948303+bglglzd@users.noreply.github.com",
                "commit",
                "-q",
                "-m",
                "Synthetic package fixture",
            )
            output = base / "candidate"
            with patch.object(BUILDER, "ROOT", repository):
                manifest = BUILDER.build(output)
                with self.assertRaisesRegex(ValueError, "fresh output"):
                    BUILDER.build(output)
            self.assertEqual(manifest["source_sha"], git("rev-parse", "HEAD").decode().strip())
            original = {path.name: path.read_bytes() for path in output.iterdir()}
            self.assertEqual(
                set(original),
                {
                    "roomrill-0.1.0-py3-none-any.whl",
                    "roomrill-0.1.0.tar.gz",
                    "roomrill-demo-0.1.0.zip",
                    "release-manifest.json",
                },
            )
            for artifact in manifest["artifacts"]:
                data = original[artifact["name"]]
                self.assertEqual(artifact["bytes"], len(data))
                self.assertEqual(artifact["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(manifest["toolchain"]["python"], sys.version.split()[0])
            self.assertEqual(
                {name: manifest["toolchain"][name] for name in BUILDER.PINNED}, BUILDER.PINNED
            )
            self.assertEqual(
                manifest["source_tree_sha256"],
                hashlib.sha256(BUILDER.encoded(manifest["source_files"])).hexdigest(),
            )
            for name, digest in manifest["configuration_sha256"].items():
                self.assertEqual(
                    digest, hashlib.sha256((repository / name).read_bytes()).hexdigest()
                )
            with zipfile.ZipFile(output / "roomrill-demo-0.1.0.zip") as archive:
                recorded = json.loads(archive.read("roomrill-demo/recorded.json"))
                self.assertEqual(
                    [stage["id"] for stage in recorded["stages"]],
                    [
                        "baseline",
                        "active",
                        "conflict",
                        "manual",
                        "offline",
                        "reconnected",
                    ],
                )
                self.assertTrue(
                    all(
                        stage["state"]["mode"] == "synthetic-local-simulator"
                        for stage in recorded["stages"]
                    )
                )
            with tarfile.open(output / "roomrill-0.1.0.tar.gz") as archive:
                members = {
                    item.name.removeprefix("roomrill-0.1.0/")
                    for item in archive.getmembers()
                    if item.isfile()
                }
                self.assertTrue(
                    {
                        "docs/spec.md",
                        "examples/scenarios-v1.json",
                        "tests/test_release.py",
                        "scripts/build-release.py",
                        ".github/workflows/ci.yml",
                        "requirements-dev.txt",
                    }
                    <= members
                )
                self.assertFalse(
                    any(set(Path(name).parts) & {".git", ".venv", "evidence"} for name in members)
                )
            environment = base / "installed"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            work = base / "work"
            work.mkdir()
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    str(output / "roomrill-0.1.0-py3-none-any.whl"),
                ],
                cwd=work,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            result = subprocess.check_output(
                [
                    str(python),
                    "-I",
                    "-m",
                    "roomrill",
                    "--db",
                    "fixture.sqlite",
                    "demo",
                ],
                cwd=work,
            )
            state = json.loads(result)
            self.assertEqual(state["mode"], "synthetic-local-simulator")
            meeting = next(room for room in state["rooms"] if room["id"] == "meeting")
            self.assertEqual(
                meeting["scopes"]["light"]["device"]["value"], {"on": True, "brightness": 70}
            )
            # Installation and execution use the retained bytes, without an acceptance rebuild.
            self.assertEqual(original, {path.name: path.read_bytes() for path in output.iterdir()})


if __name__ == "__main__":
    unittest.main()
