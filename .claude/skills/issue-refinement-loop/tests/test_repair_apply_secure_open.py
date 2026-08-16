"""Behavioral tests for `secure_read_repair_apply_artifact` (Issue #2039 AC7).

GIVEN a repair_apply artifact candidate on disk, WHEN the FD-based secure
reader opens it, THEN it must accept a well-formed regular file within the
containment root and reject leaf symlinks, parent symlinks, FIFOs, sockets,
devices, parent substitution, oversize content, and digest mismatch --
without ever trusting bytes read through an unsafe path.

NOTE: this is a partial-implementation Issue #2039 test file covering AC7
only. It does not exercise AC1-AC6/AC8-AC11 wiring (not yet implemented).
"""

from __future__ import annotations

import hashlib
import os
import socket
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from run_refinement_preflight import (  # noqa: E402
    RepairApplySecureOpenError,
    secure_read_repair_apply_artifact,
)


class SecureReadRepairApplyArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write(self, rel: str, content: bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    # -- GIVEN a regular file WHEN read THEN it succeeds and digest matches --

    def test_given_regular_utf8_file_when_read_then_returns_text_and_digest(self) -> None:
        content = "hello repair_apply\n".encode("utf-8")
        path = self._write("artifact.json", content)
        expected_digest = hashlib.sha256(content).hexdigest()

        text, digest = secure_read_repair_apply_artifact(path, root=self.root)

        self.assertEqual(text, "hello repair_apply\n")
        self.assertEqual(digest, expected_digest)

    def test_given_expected_digest_matches_when_read_then_succeeds(self) -> None:
        content = b"payload"
        path = self._write("artifact.json", content)
        digest = hashlib.sha256(content).hexdigest()

        text, returned_digest = secure_read_repair_apply_artifact(
            path, root=self.root, expected_sha256=digest
        )

        self.assertEqual(text, "payload")
        self.assertEqual(returned_digest, digest)

    def test_given_expected_digest_mismatch_when_read_then_rejects(self) -> None:
        path = self._write("artifact.json", b"payload")

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(
                path, root=self.root, expected_sha256="0" * 64
            )
        self.assertIn("repair_apply_digest_mismatch", str(ctx.exception))

    # -- WHEN leaf is a symlink THEN reject --

    def test_given_leaf_symlink_when_read_then_rejects(self) -> None:
        target = self._write("real.json", b"real content")
        link = self.root / "link.json"
        os.symlink(target, link)

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(link, root=self.root)
        self.assertIn("repair_apply_leaf_is_symlink", str(ctx.exception))

    # -- WHEN a parent directory is a symlink THEN reject --

    def test_given_parent_dir_symlink_when_read_then_rejects(self) -> None:
        real_dir = self.root / "real_dir"
        real_dir.mkdir()
        target = real_dir / "artifact.json"
        target.write_bytes(b"content")

        linked_dir = self.root / "linked_dir"
        os.symlink(real_dir, linked_dir, target_is_directory=True)
        path_via_link = linked_dir / "artifact.json"

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(path_via_link, root=self.root)
        self.assertIn("repair_apply_ancestor_is_symlink", str(ctx.exception))

    # -- WHEN resolved parent is outside root (parent substitution) THEN reject --

    def test_given_parent_outside_root_when_read_then_rejects(self) -> None:
        outside_dir = Path(self._tmpdir.name).parent / f"outside-{os.getpid()}"
        outside_dir.mkdir(exist_ok=True)
        try:
            outside_file = outside_dir / "artifact.json"
            outside_file.write_bytes(b"outside content")

            with self.assertRaises(RepairApplySecureOpenError) as ctx:
                secure_read_repair_apply_artifact(outside_file, root=self.root)
            message = str(ctx.exception)
            self.assertTrue(
                "repair_apply_parent_outside_root" in message
                or "repair_apply_ancestor_root_not_found" in message
            )
        finally:
            outside_file_path = outside_dir / "artifact.json"
            if outside_file_path.exists():
                outside_file_path.unlink()
            outside_dir.rmdir()

    # -- WHEN leaf is a FIFO THEN reject --

    def test_given_fifo_leaf_when_read_then_rejects(self) -> None:
        fifo_path = self.root / "fifo.json"
        os.mkfifo(fifo_path)

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(fifo_path, root=self.root)
        self.assertIn("repair_apply_leaf_not_regular_file", str(ctx.exception))

    # -- WHEN leaf is a unix domain socket THEN reject --

    def test_given_socket_leaf_when_read_then_rejects(self) -> None:
        socket_path = self.root / "sock.json"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(socket_path))

            with self.assertRaises(RepairApplySecureOpenError) as ctx:
                secure_read_repair_apply_artifact(socket_path, root=self.root)
            self.assertIn("repair_apply_leaf_not_regular_file", str(ctx.exception))
        finally:
            sock.close()

    # -- WHEN leaf is a directory THEN reject --

    def test_given_directory_leaf_when_read_then_rejects(self) -> None:
        dir_path = self.root / "adir.json"
        dir_path.mkdir()

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(dir_path, root=self.root)
        self.assertIn("repair_apply_leaf_not_regular_file", str(ctx.exception))

    # -- WHEN leaf does not exist THEN reject --

    def test_given_missing_leaf_when_read_then_rejects(self) -> None:
        missing_path = self.root / "missing.json"

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(missing_path, root=self.root)
        self.assertIn("repair_apply_leaf_not_found", str(ctx.exception))

    # -- WHEN content exceeds max_bytes THEN reject --

    def test_given_oversize_content_when_read_then_rejects(self) -> None:
        path = self._write("big.json", b"x" * 4096)

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(path, root=self.root, max_bytes=1024)
        self.assertIn("repair_apply_leaf_oversize", str(ctx.exception))

    # -- WHEN content is not valid UTF-8 THEN reject --

    def test_given_non_utf8_content_when_read_then_rejects(self) -> None:
        path = self._write("bad_encoding.json", b"\xff\xfe\xfa")

        with self.assertRaises(RepairApplySecureOpenError) as ctx:
            secure_read_repair_apply_artifact(path, root=self.root)
        self.assertIn("repair_apply_leaf_not_utf8", str(ctx.exception))

    # -- WHEN root itself is used as the leaf's direct parent THEN succeeds --

    def test_given_leaf_directly_under_root_when_read_then_succeeds(self) -> None:
        path = self.root / "direct.json"
        path.write_bytes(b"ok")

        text, digest = secure_read_repair_apply_artifact(path, root=self.root)
        self.assertEqual(text, "ok")
        self.assertEqual(digest, hashlib.sha256(b"ok").hexdigest())


if __name__ == "__main__":
    unittest.main()
