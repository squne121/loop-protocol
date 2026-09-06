#!/usr/bin/env python3
"""zip_bounded_extract.py (Issue #2524)

Standard-`zipfile`-based replacement for the inline `bounded_extract()` /
local-file-header struct parser / raw-DEFLATE loop / hand-rolled CRC-32
check that used to live only as a heredoc inside
`.github/workflows/visual-impact-trusted-consumer.yml`'s `download` step
(Issue #2505 / PR #2514).

Bug this module fixes (Issue #2524 Current Validated Scope): the old inline
STORED branch sliced the RAW downloaded ZIP byte buffer
(`buffer[data_start : data_start + max_read_bytes + 1]`) to decide whether
the target entry exceeded the read bound. Because that slice has no idea
where the target entry's OWN data actually ends, any bytes belonging to a
DIFFERENT entry (or the central directory) stored immediately after the
target entry in the physical archive layout were silently included in the
size judgement -- so accept/reject depended on entry STORAGE ORDER, not on
the target entry's own size. This module never slices the raw buffer by a
manually-computed offset: every bounded read goes through
`zipfile.ZipFile.open(<ZipInfo>).read(N)`, which the stdlib bounds to that
entry's own compressed/decompressed extent regardless of what comes before
or after it in the archive -- so entry storage order can never change the
accept/reject outcome.

Out of scope (Issue #2524 explicit): the old code's separate guarantee that
a STORED entry's PHYSICAL bytes on disk cannot lie about being larger than
the entry's own declared `file_size` (a hostile-producer "declared size
lie" defence). Standard `zipfile.ZipExtFile.read()` trusts the entry's own
declared size/compressed_size to decide when it has reached EOF, exactly
like any other consumer of the stdlib `zipfile` module; reimplementing a
raw-byte-scan to relitigate that trust is explicitly out of scope for this
Issue (Current Validated Scope: "the standard zipfile.ZipFile/ZipInfo
based" module, not a byte-level re-verifier).

CLI shape is unchanged from the old heredoc so the workflow's invocation
(`--label`/`--zip-url`/`--max-download-bytes`/`--max-entries`/
`--max-read-bytes`/`--target NAME DEST`/`--status-output-file`) keeps
working unmodified.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import zipfile
import zlib

STATUS_OK = "ok"
STATUS_DOWNLOAD_SIZE_EXCEEDED = "download_size_exceeded"
STATUS_ENTRY_COUNT_EXCEEDED = "entry_count_exceeded"
STATUS_ENTRY_AMBIGUOUS_OR_MISSING = "entry_ambiguous_or_missing"
STATUS_READ_SIZE_EXCEEDED = "read_size_exceeded"
STATUS_INVALID_ZIP = "invalid_zip"
STATUS_DOWNLOAD_COMMAND_FAILED = "download_command_failed"
# Issue #2505 PR #2514 review fix_delta P1-2 (preserved): reject any
# compress method other than STORED/DEFLATED before ever opening the entry
# -- neither of the excluded methods has a bounded-output primitive this
# module can rely on, and the Issue's Current Validated Scope explicitly
# preserves the STORED/DEFLATED-only compression policy.
STATUS_UNSUPPORTED_COMPRESSION = "unsupported_compression_method"
# Issue #2505 PR #2514 review fix_delta P1-1 (preserved): a default failure
# status this module writes for itself the instant ANY unforeseen exception
# escapes the main body below -- so a status file always exists once this
# process has started running Python code, even for a failure mode no
# individual `except` clause anticipated.
STATUS_INTERNAL_ERROR = "internal_error"

EXIT_CODES = {
    STATUS_OK: 0,
    STATUS_DOWNLOAD_SIZE_EXCEEDED: 10,
    STATUS_ENTRY_COUNT_EXCEEDED: 11,
    STATUS_ENTRY_AMBIGUOUS_OR_MISSING: 12,
    STATUS_READ_SIZE_EXCEEDED: 13,
    STATUS_INVALID_ZIP: 14,
    STATUS_DOWNLOAD_COMMAND_FAILED: 15,
    STATUS_UNSUPPORTED_COMPRESSION: 16,
    STATUS_INTERNAL_ERROR: 17,
}

_SUPPORTED_COMPRESS_TYPES = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


def write_status(path: str, status: str, artifact: str, limit) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"STATUS={status}\n")
        fh.write(f"ARTIFACT={artifact}\n")
        fh.write(f"LIMIT={'' if limit is None else limit}\n")


def bounded_download(zip_url: str, max_download_bytes: int):
    """Stream `gh api <zip_url>` into a bounded in-memory buffer, aborting
    and reaping the process the instant more than max_download_bytes have
    been received. Never writes the response to disk (no post-hoc `stat`
    of a downloaded file).

    Uses `read1()` (a SINGLE low-level read, returning whatever is
    immediately available without blocking to fill the full requested
    size -- unlike `read()`, which blocks until either the full amount
    requested or EOF) so an over-limit byte that has already arrived is
    detected immediately even if the sender never sends another
    chunk-sized burst and never closes the pipe (Issue #2505 PR #2514
    review fix_delta P2-2, preserved)."""
    proc = subprocess.Popen(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            zip_url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    buffer = bytearray()
    chunk_size = 65536
    assert proc.stdout is not None
    try:
        while True:
            want = min(chunk_size, max_download_bytes - len(buffer) + 1)
            chunk = proc.stdout.read1(want)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > max_download_bytes:
                proc.kill()
                proc.wait()
                return STATUS_DOWNLOAD_SIZE_EXCEEDED, None
    finally:
        proc.stdout.close()
    returncode = proc.wait()
    if returncode != 0:
        return STATUS_DOWNLOAD_COMMAND_FAILED, None
    return STATUS_OK, bytes(buffer)


def select_entry(
    entries: list[zipfile.ZipInfo], targets: dict[str, str]
) -> tuple[zipfile.ZipInfo | None, str | None]:
    """Evaluate ambiguity against the FULL original entry list (never a
    name-narrowed subset): exactly one physical entry whose filename is one
    of the accepted names -- 0 matches (missing), >=2 matches of the SAME
    accepted name (duplicate), or one-each of two DIFFERENT accepted names
    (co-existence) are all rejected by this single condition."""
    matches = [info for info in entries if info.filename in targets]
    if len(matches) != 1:
        return None, None
    selected = matches[0]
    return selected, targets[selected.filename]


def read_bounded_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, max_read_bytes: int) -> bytes:
    """Read at most `max_read_bytes + 1` bytes of `info`'s data via the
    standard `zipfile.ZipExtFile` machinery (`zf.open(info)`), never by
    slicing the raw downloaded archive buffer at a manually-computed
    offset.

    This is the Issue #2524 core fix: `ZipExtFile.read(n)` bounds its
    output to THIS entry's own compressed/decompressed extent using
    `info.header_offset` internally -- it is completely unaffected by
    what other entries (or the central directory) happen to be stored
    immediately before or after this one in the archive, so entry storage
    order can never change the accept/reject outcome (unlike the old
    buffer-slicing implementation this replaces).

    When the entry's full content is consumed within the `max_read_bytes +
    1` budget, `zipfile` itself verifies the entry's CRC-32 as part of
    reaching EOF (`ZipExtFile._update_crc`) and raises `zipfile.BadZipFile`
    on mismatch -- no separate CRC check is implemented here. When the
    entry is NOT fully consumed within the budget (oversized), no CRC
    check is performed (same as the old implementation, which also skipped
    CRC verification for over-limit entries)."""
    with zf.open(info) as fh:
        return fh.read(max_read_bytes + 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--zip-url", required=True)
    parser.add_argument("--max-download-bytes", type=int, required=True)
    parser.add_argument("--max-entries", type=int, required=True)
    parser.add_argument("--max-read-bytes", type=int, required=True)
    parser.add_argument("--target", nargs=2, metavar=("NAME", "DEST"), action="append", required=True)
    parser.add_argument("--status-output-file", required=True)
    args = parser.parse_args()
    targets = {name: dest for name, dest in args.target}

    # Wrap the entire processing body in a broad `except Exception` so that
    # ANY uncaught failure (including one no individual `except` clause
    # below anticipated) still leaves a status file behind (Issue #2505
    # PR #2514 review fix_delta P1-1 point 3, preserved).
    try:
        status, buffer = bounded_download(args.zip_url, args.max_download_bytes)
        if status != STATUS_OK:
            limit = args.max_download_bytes if status == STATUS_DOWNLOAD_SIZE_EXCEEDED else None
            write_status(args.status_output_file, status, args.label, limit)
            return EXIT_CODES[status]

        try:
            zf = zipfile.ZipFile(io.BytesIO(buffer))
            entries = zf.infolist()
        except zipfile.BadZipFile:
            write_status(args.status_output_file, STATUS_INVALID_ZIP, args.label, None)
            return EXIT_CODES[STATUS_INVALID_ZIP]

        if len(entries) > args.max_entries:
            write_status(args.status_output_file, STATUS_ENTRY_COUNT_EXCEEDED, args.label, args.max_entries)
            return EXIT_CODES[STATUS_ENTRY_COUNT_EXCEEDED]

        selected, dest_file = select_entry(entries, targets)
        if selected is None:
            write_status(args.status_output_file, STATUS_ENTRY_AMBIGUOUS_OR_MISSING, args.label, None)
            return EXIT_CODES[STATUS_ENTRY_AMBIGUOUS_OR_MISSING]

        # Issue #2505 PR #2514 review fix_delta P1-2 (preserved): reject any
        # compress method other than STORED/DEFLATED BEFORE ever attempting
        # to open/decompress the entry.
        if selected.compress_type not in _SUPPORTED_COMPRESS_TYPES:
            write_status(args.status_output_file, STATUS_UNSUPPORTED_COMPRESSION, args.label, None)
            return EXIT_CODES[STATUS_UNSUPPORTED_COMPRESSION]

        try:
            data = read_bounded_entry(zf, selected, args.max_read_bytes)
        except (zipfile.BadZipFile, NotImplementedError, EOFError, RuntimeError, zlib.error):
            write_status(args.status_output_file, STATUS_INVALID_ZIP, args.label, None)
            return EXIT_CODES[STATUS_INVALID_ZIP]

        if len(data) > args.max_read_bytes:
            write_status(args.status_output_file, STATUS_READ_SIZE_EXCEEDED, args.label, args.max_read_bytes)
            return EXIT_CODES[STATUS_READ_SIZE_EXCEEDED]

        with open(dest_file, "wb") as out:
            out.write(data)

        write_status(args.status_output_file, STATUS_OK, args.label, None)
        return 0
    except Exception:
        try:
            write_status(args.status_output_file, STATUS_INTERNAL_ERROR, args.label, None)
        except OSError:
            pass
        return EXIT_CODES[STATUS_INTERNAL_ERROR]


if __name__ == "__main__":
    sys.exit(main())
