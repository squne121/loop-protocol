"""scripts/claude-gpt/tests/test_issue_2274_spark_evidence_semantics.py

Issue #2651: GPT-5.3-Codex-Spark delegation is retired repository-wide.
This file used to extract the `SPARK_EVIDENCE_PY` heredoc body (Issue
#2274 AC17/AC18's live Spark delegation evidence builder) from
`runtime_smoke_test.sh` at test-collection time and drive it as a real
subprocess against synthetic stream-json fixtures. `runtime_smoke_test.sh`
no longer embeds that heredoc at all -- the entire `--spark-delegation`
live E2E harness it belonged to (including the evidence builder, the
`SPARK_DELEGATION_EVIDENCE_V2` schema, and the lifecycle/proxy-correlation
matrix this suite exercised) was removed; `--spark-delegation` is now
rejected immediately at argv pre-scan with a deterministic non-zero exit
(see `test_runtime_smoke_test_spark_delegation_retired.py`, the new file
this same Issue adds for that specific contract).

Replaced (file path kept, per Issue #2651 Allowed Paths -- no file
deletion) with a negative regression suite scoped to this file's own prior
subject (the evidence builder's existence/extractability), not a
duplicate of the new dedicated retirement test file.
"""

from __future__ import annotations

import pathlib
import re

RUNTIME_SMOKE_SH = pathlib.Path(__file__).resolve().parents[1] / "runtime_smoke_test.sh"

_HEREDOC_RE = re.compile(
    r"cat > \"\$SPARK_EVIDENCE_PY\" <<'SPARK_EVIDENCE_PY_EOF'\n(.*?)\nSPARK_EVIDENCE_PY_EOF\n",
    re.DOTALL,
)

_RETIRED_SPARK_IDENTIFIERS = (
    "SPARK_EVIDENCE_PY",
    "SPARK_DELEGATION_EVIDENCE_V2",
    "resolvedModel",
    "modelsUsed",
    "claude_code_evidence_schema_unsupported",
)


def test_spark_evidence_py_heredoc_no_longer_embedded():
    """GIVEN the current `runtime_smoke_test.sh` source
    WHEN searched for the `SPARK_EVIDENCE_PY` heredoc this file used to
    extract and drive as a subprocess
    THEN no match is found -- the live Spark delegation evidence builder
    has been removed entirely (Issue #2651), not merely disabled."""
    text = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert _HEREDOC_RE.search(text) is None


def test_no_spark_evidence_schema_identifiers_remain_executable():
    """GIVEN the current `runtime_smoke_test.sh` source
    WHEN searched for the AC17/AC18 evidence-schema identifiers this suite
    used to validate (`SPARK_DELEGATION_EVIDENCE_V2`, `resolvedModel`,
    `modelsUsed`, the `claude_code_evidence_schema_unsupported` blocked
    reason)
    THEN none of them remain anywhere in the file -- there is no dormant
    evidence-schema code path left that could be silently reawakened."""
    text = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    for identifier in _RETIRED_SPARK_IDENTIFIERS:
        assert identifier not in text, identifier


def test_spark_delegation_flag_is_rejected_before_any_evidence_logic_runs():
    """GIVEN the current `runtime_smoke_test.sh` source
    WHEN searched for the `--spark-delegation` retired-rejection branch
    THEN it appears at the argv pre-scan near the top of the file (before
    the SUT/proxy identity resolution, preflight, and evidence-directory
    setup that the old live harness depended on), confirming the retired
    branch is reached unconditionally and immediately -- never falling
    through to any evidence-building logic."""
    text = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    pre_scan_idx = text.index("--spark-delegation)")
    # The retired mode is rejected in the first ~50 lines of the script,
    # well before any SUT/proxy/evidence-directory setup.
    line_number = text.count("\n", 0, pre_scan_idx) + 1
    assert line_number < 60, line_number
    assert "exit 2" in text[pre_scan_idx : pre_scan_idx + 400]
