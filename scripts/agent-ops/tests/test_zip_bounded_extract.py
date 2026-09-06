"""Issue #2524 AC3/AC4/AC6: regression coverage for
`scripts/agent-ops/zip_bounded_extract.py`'s standard-`zipfile`-based
bounded entry selection/read, proving the STORED-branch entry-storage-order
bug (Current Validated Scope) is fixed: accept/reject never depends on
where the target entry physically sits relative to other entries in the
archive.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "zip_bounded_extract.py"
_MODULE_NAME = "zip_bounded_extract_issue_2524"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
zbe = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = zbe
_spec.loader.exec_module(zbe)

TARGET_NAME = "target.json"
UNRELATED_NAME = "unrelated_large.bin"


def _build_zip(entries: list[tuple[str, str]], compression: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def _select_and_read(payload: bytes, target_name: str, max_read_bytes: int) -> bytes:
    zf = zipfile.ZipFile(io.BytesIO(payload))
    selected, dest = zbe.select_entry(zf.infolist(), {target_name: "unused-dest"})
    assert selected is not None, "target entry must resolve to exactly one match"
    return zbe.read_bounded_entry(zf, selected, max_read_bytes)


# ---------------------------------------------------------------------------
# AC4: STORED/DEFLATED boundary + storage-order regression matrix
# ---------------------------------------------------------------------------

COMPRESSIONS = [
    pytest.param(zipfile.ZIP_STORED, id="stored"),
    pytest.param(zipfile.ZIP_DEFLATED, id="deflated"),
]


@pytest.mark.parametrize("compression", COMPRESSIONS)
def test_target_below_limit_accepted(compression: int):
    content = "a" * 10
    payload = _build_zip([(TARGET_NAME, content)], compression)
    data = _select_and_read(payload, TARGET_NAME, max_read_bytes=32)
    assert len(data) <= 32
    assert data.decode("ascii") == content


@pytest.mark.parametrize("compression", COMPRESSIONS)
def test_target_exactly_at_limit_accepted(compression: int):
    max_read_bytes = 32
    content = "a" * max_read_bytes
    payload = _build_zip([(TARGET_NAME, content)], compression)
    data = _select_and_read(payload, TARGET_NAME, max_read_bytes=max_read_bytes)
    assert len(data) == max_read_bytes
    assert data.decode("ascii") == content


@pytest.mark.parametrize("compression", COMPRESSIONS)
def test_target_over_limit_rejected(compression: int):
    max_read_bytes = 32
    content = "a" * (max_read_bytes + 1)
    payload = _build_zip([(TARGET_NAME, content)], compression)
    data = _select_and_read(payload, TARGET_NAME, max_read_bytes=max_read_bytes)
    assert len(data) > max_read_bytes


@pytest.mark.parametrize("compression", COMPRESSIONS)
def test_small_target_before_large_unrelated_entry_not_misclassified(compression: int):
    """Issue #2524 core regression: the old STORED branch sliced the raw
    buffer past the target entry's own data, so a small target stored
    BEFORE a large unrelated entry could be misjudged as exceeding the
    read bound. Must be accepted here."""
    max_read_bytes = 1000
    small_content = "z" * 10
    large_content = "u" * 500_000
    payload = _build_zip(
        [(TARGET_NAME, small_content), (UNRELATED_NAME, large_content)], compression
    )
    data = _select_and_read(payload, TARGET_NAME, max_read_bytes=max_read_bytes)
    assert len(data) <= max_read_bytes, "must not be misclassified as read_size_exceeded"
    assert data.decode("ascii") == small_content


@pytest.mark.parametrize("compression", COMPRESSIONS)
def test_large_unrelated_entry_before_small_target_still_accepted(compression: int):
    max_read_bytes = 1000
    small_content = "z" * 10
    large_content = "u" * 500_000
    payload = _build_zip(
        [(UNRELATED_NAME, large_content), (TARGET_NAME, small_content)], compression
    )
    data = _select_and_read(payload, TARGET_NAME, max_read_bytes=max_read_bytes)
    assert len(data) <= max_read_bytes
    assert data.decode("ascii") == small_content


@pytest.mark.parametrize("compression", COMPRESSIONS)
def test_storage_order_never_changes_accept_reject_outcome(compression: int):
    """Same target/unrelated pair, swap physical storage order: the
    accept/reject outcome (and the exact bytes recovered) must be
    identical either way."""
    max_read_bytes = 1000
    small_content = "order-independent-content"
    large_content = "w" * 250_000

    payload_target_first = _build_zip(
        [(TARGET_NAME, small_content), (UNRELATED_NAME, large_content)], compression
    )
    payload_target_last = _build_zip(
        [(UNRELATED_NAME, large_content), (TARGET_NAME, small_content)], compression
    )

    data_first = _select_and_read(payload_target_first, TARGET_NAME, max_read_bytes=max_read_bytes)
    data_last = _select_and_read(payload_target_last, TARGET_NAME, max_read_bytes=max_read_bytes)

    assert data_first == data_last == small_content.encode("ascii")
    assert len(data_first) <= max_read_bytes
    assert len(data_last) <= max_read_bytes


@pytest.mark.parametrize("compression", COMPRESSIONS)
def test_oversized_target_over_limit_regardless_of_unrelated_entry_position(compression: int):
    """Companion negative case: a genuinely oversized target must still be
    rejected as over-limit no matter which side of an unrelated entry it
    sits on -- the fix must not accidentally make oversized targets pass."""
    max_read_bytes = 100
    oversized_content = "o" * (max_read_bytes + 50)
    unrelated_content = "n" * 200

    payload_target_first = _build_zip(
        [(TARGET_NAME, oversized_content), (UNRELATED_NAME, unrelated_content)], compression
    )
    payload_target_last = _build_zip(
        [(UNRELATED_NAME, unrelated_content), (TARGET_NAME, oversized_content)], compression
    )

    data_first = _select_and_read(payload_target_first, TARGET_NAME, max_read_bytes=max_read_bytes)
    data_last = _select_and_read(payload_target_last, TARGET_NAME, max_read_bytes=max_read_bytes)

    assert len(data_first) > max_read_bytes
    assert len(data_last) > max_read_bytes


# ---------------------------------------------------------------------------
# AC3: missing / duplicate cardinality (test names include "missing"/"duplicate")
# ---------------------------------------------------------------------------


def test_missing_target_entry_rejected():
    payload = _build_zip([("unrelated.json", "x")], zipfile.ZIP_STORED)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    selected, dest = zbe.select_entry(zf.infolist(), {TARGET_NAME: "unused-dest"})
    assert selected is None
    assert dest is None


def test_duplicate_target_entry_rejected():
    payload = _build_zip(
        [(TARGET_NAME, "first"), (TARGET_NAME, "second")], zipfile.ZIP_STORED
    )
    zf = zipfile.ZipFile(io.BytesIO(payload))
    selected, dest = zbe.select_entry(zf.infolist(), {TARGET_NAME: "unused-dest"})
    assert selected is None
    assert dest is None


def test_multiple_accepted_version_names_coexisting_ambiguity_rejected():
    """Two DIFFERENT accepted names (e.g. V2/V3 migration-window filenames)
    both present, one-each -- must still be rejected as ambiguous even
    though neither individual name is itself duplicated."""
    name_v2 = "evidence_v2.json"
    name_v3 = "evidence_v3.json"
    payload = _build_zip([(name_v2, "v2-content"), (name_v3, "v3-content")], zipfile.ZIP_STORED)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    selected, dest = zbe.select_entry(zf.infolist(), {name_v2: "dest-v2", name_v3: "dest-v3"})
    assert selected is None
    assert dest is None


def test_exactly_one_accepted_version_name_present_not_ambiguous():
    """Companion positive regression: only ONE of two accepted names
    present is unambiguous and must resolve."""
    name_v2 = "evidence_v2.json"
    name_v3 = "evidence_v3.json"
    payload = _build_zip([(name_v2, "v2-content")], zipfile.ZIP_STORED)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    selected, dest = zbe.select_entry(zf.infolist(), {name_v2: "dest-v2", name_v3: "dest-v3"})
    assert selected is not None
    assert selected.filename == name_v2
    assert dest == "dest-v2"


# ---------------------------------------------------------------------------
# Unsupported compression regression (existing Issue #2505 guarantee
# preserved by the new module)
# ---------------------------------------------------------------------------


def test_unsupported_compression_bzip2_rejected_before_extraction():
    payload = _build_zip([(TARGET_NAME, "hello world")], zipfile.ZIP_BZIP2)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    selected, _dest = zbe.select_entry(zf.infolist(), {TARGET_NAME: "unused-dest"})
    assert selected is not None
    assert selected.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


def test_unsupported_compression_lzma_rejected_before_extraction():
    payload = _build_zip([(TARGET_NAME, "hello world")], zipfile.ZIP_LZMA)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    selected, _dest = zbe.select_entry(zf.infolist(), {TARGET_NAME: "unused-dest"})
    assert selected is not None
    assert selected.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------------------
# End-to-end CLI smoke test: the actual subprocess entry point, proving the
# module is invocable exactly as the workflow invokes it.
# ---------------------------------------------------------------------------


def test_cli_end_to_end_success(tmp_path: Path):
    payload = _build_zip([(TARGET_NAME, '{"ok":true}')], zipfile.ZIP_STORED)
    payload_file = tmp_path / "gh_payload.bin"
    payload_file.write_bytes(payload)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"with open({str(payload_file)!r}, 'rb') as fh:\n"
        "    sys.stdout.buffer.write(fh.read())\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    dest = tmp_path / "out" / TARGET_NAME
    dest.parent.mkdir()
    status_file = tmp_path / "status.kv"
    import os

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        [
            sys.executable,
            str(_MODULE_PATH),
            "--label",
            "test",
            "--zip-url",
            "repos/squne121/loop-protocol/actions/artifacts/1/zip",
            "--max-download-bytes",
            "2097152",
            "--max-entries",
            "16",
            "--max-read-bytes",
            "1000000",
            "--status-output-file",
            str(status_file),
            "--target",
            TARGET_NAME,
            str(dest),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "STATUS=ok" in status_file.read_text(encoding="utf-8")
    assert dest.read_text(encoding="utf-8") == '{"ok":true}'


def test_cli_end_to_end_read_size_exceeded(tmp_path: Path):
    max_read_bytes = 16
    payload = _build_zip([(TARGET_NAME, "a" * (max_read_bytes + 1))], zipfile.ZIP_STORED)
    payload_file = tmp_path / "gh_payload.bin"
    payload_file.write_bytes(payload)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"with open({str(payload_file)!r}, 'rb') as fh:\n"
        "    sys.stdout.buffer.write(fh.read())\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    dest = tmp_path / "out" / TARGET_NAME
    dest.parent.mkdir()
    status_file = tmp_path / "status.kv"
    import os

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        [
            sys.executable,
            str(_MODULE_PATH),
            "--label",
            "test",
            "--zip-url",
            "repos/squne121/loop-protocol/actions/artifacts/1/zip",
            "--max-download-bytes",
            "2097152",
            "--max-entries",
            "16",
            "--max-read-bytes",
            str(max_read_bytes),
            "--status-output-file",
            str(status_file),
            "--target",
            TARGET_NAME,
            str(dest),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode != 0
    assert "STATUS=read_size_exceeded" in status_file.read_text(encoding="utf-8")
    assert not dest.exists()
