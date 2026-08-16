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

import contextlib
import hashlib
import os
import socket
import sys
import unittest
import unittest.mock
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

    # -- P1-2 (PR #2202 review): traversal must be genuinely dir-FD-relative,
    # never pathname-based, for every ancestor between the leaf and root --

    def test_given_multi_level_path_when_opening_ancestors_then_every_open_is_dir_fd_relative(
        self,
    ) -> None:
        """Proves the implementation path itself is dir-FD-relative: every
        `os.open()` call made while traversing from `root` down to the leaf
        (except the single root-anchoring open) passes `dir_fd=<an
        already-open fd>` together with a bare single path COMPONENT (never
        a multi-segment or absolute pathname). This is the structural
        property that makes an ancestor-swap-based TOCTOU attack
        impossible: if any call instead re-resolved a multi-segment
        pathname, a mid-traversal ancestor swap could redirect it."""
        nested = self.root / "a" / "b" / "c"
        nested.mkdir(parents=True)
        target = nested / "artifact.json"
        target.write_bytes(b"nested content")

        real_open = os.open
        calls: list[tuple[str, object]] = []

        def spying_open(path_arg, flags, mode=0o777, *, dir_fd=None):
            calls.append((str(path_arg), dir_fd))
            if dir_fd is None:
                return real_open(path_arg, flags, mode)
            return real_open(path_arg, flags, mode, dir_fd=dir_fd)

        with unittest.mock.patch("os.open", side_effect=spying_open):
            text, _digest = secure_read_repair_apply_artifact(target, root=self.root)
        self.assertEqual(text, "nested content")

        # Exactly one call anchors on the root itself (dir_fd=None, full
        # resolved root pathname). Every other call MUST supply a dir_fd
        # (not None) and a bare single-component name (never containing
        # os.sep), i.e. genuinely relative to an already-open ancestor FD.
        root_anchor_calls = [c for c in calls if c[1] is None]
        relative_calls = [c for c in calls if c[1] is not None]
        self.assertEqual(len(root_anchor_calls), 1)
        self.assertEqual(root_anchor_calls[0][0], str(self.root.resolve(strict=False)))
        self.assertEqual(len(relative_calls), 4)  # a, b, c, artifact.json
        for name, dir_fd in relative_calls:
            self.assertNotIn(os.sep, name, f"expected a bare component, got a pathname: {name!r}")
            self.assertIsInstance(dir_fd, int)

    def test_given_ancestor_pathname_swapped_after_open_when_traversing_deeper_then_swap_has_no_effect(
        self,
    ) -> None:
        """Proves the specific TOCTOU window P1-2 closes: once an ancestor
        directory ('a') has been opened via dir_fd, replacing that SAME
        pathname on disk with a symlink to an attacker-controlled directory
        does not redirect the traversal of 'a's children -- because the
        traversal continues via the already-open FD for 'a', never by
        re-resolving the pathname 'a' again."""
        real_a = self.root / "a"
        (real_a / "b").mkdir(parents=True)
        target = real_a / "b" / "artifact.json"
        target.write_bytes(b"trusted content")

        attacker_dir = self.root.parent / f"repair-apply-p12-attacker-{os.getpid()}"
        attacker_dir.mkdir(exist_ok=True)
        (attacker_dir / "b").mkdir(exist_ok=True)
        (attacker_dir / "b" / "artifact.json").write_bytes(b"ATTACKER CONTROLLED")

        real_open = os.open
        swap_state = {"swapped": False, "reverted": False}

        def swap_then_revert_mid_traversal(path_arg, flags, mode=0o777, *, dir_fd=None):
            fd = (
                real_open(path_arg, flags, mode)
                if dir_fd is None
                else real_open(path_arg, flags, mode, dir_fd=dir_fd)
            )
            if dir_fd is not None and path_arg == "a" and not swap_state["swapped"]:
                # 'a' was JUST opened via dir_fd (the fd above is already
                # pinned to the real directory's inode). Now swap the
                # on-disk pathname 'a' to a symlink pointing at an
                # attacker-controlled directory that also has a 'b/
                # artifact.json' -- if the NEXT traversal step (opening
                # 'b') re-resolved the pathname 'a/b' instead of using the
                # already-open fd, it would follow this symlink and read
                # the attacker's content instead.
                swap_state["swapped"] = True
                os.rename(str(real_a), str(self.root / "a_moved_aside"))
                os.symlink(str(attacker_dir), str(real_a))
            elif (
                dir_fd is not None
                and path_arg == "b"
                and swap_state["swapped"]
                and not swap_state["reverted"]
            ):
                # 'b' was just opened relative to fd_a (proving it did NOT
                # need to re-resolve the swapped 'a' pathname). Revert the
                # swap now, simulating an attacker who un-swaps quickly to
                # avoid leaving persistent evidence -- the read must still
                # be unaffected either way, since it already used fd_a.
                swap_state["reverted"] = True
                os.unlink(str(real_a))
                os.rename(str(self.root / "a_moved_aside"), str(real_a))
            return fd

        try:
            with unittest.mock.patch("os.open", side_effect=swap_then_revert_mid_traversal):
                text, _digest = secure_read_repair_apply_artifact(target, root=self.root)

            self.assertTrue(swap_state["swapped"], "swap hook never fired; test setup invalid")
            self.assertTrue(swap_state["reverted"], "revert hook never fired; test setup invalid")
            self.assertEqual(
                text,
                "trusted content",
                "traversal must continue through the real, already-open "
                "ancestor FD ('a') and must NOT be redirected by the "
                "pathname swap performed mid-traversal, after that "
                "ancestor was opened but before its child was opened",
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(self.root / "a")
            moved = self.root / "a_moved_aside"
            if moved.exists():
                with contextlib.suppress(OSError):
                    (moved / "b" / "artifact.json").unlink()
                with contextlib.suppress(OSError):
                    (moved / "b").rmdir()
                with contextlib.suppress(OSError):
                    moved.rmdir()
            with contextlib.suppress(OSError):
                (attacker_dir / "b" / "artifact.json").unlink()
            with contextlib.suppress(OSError):
                (attacker_dir / "b").rmdir()
            with contextlib.suppress(OSError):
                attacker_dir.rmdir()


if __name__ == "__main__":
    unittest.main()
