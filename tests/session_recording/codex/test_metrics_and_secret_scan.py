#!/usr/bin/env python3

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex"
METRICS = REPO_ROOT / "scripts" / "session-recording" / "collect-codex-pilot-metrics.mjs"
SCAN_MODULE = REPO_ROOT / "scripts" / "session-recording" / "codex-metadata-scan.mjs"


def run_node_module(source: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_secret_exposure_detects_all_canary_transforms():
    output = run_node_module(
        f"import {{ buildSyntheticCanaryVariants, scanTextForSyntheticCanary }} from {json.dumps(str(SCAN_MODULE))};"
        "const text = buildSyntheticCanaryVariants().map((item) => item.value).join(' ');"
        "console.log(JSON.stringify(scanTextForSyntheticCanary(text)));"
    )
    findings = json.loads(output)
    assert findings == [
        "raw",
        "sha256",
        "base64",
        "hex",
        "urlencoded",
        "json_escaped",
        "unicode_escaped",
    ]


def test_metrics_sources_authoritative_fixture():
    output_path = REPO_ROOT / "tmp" / "codex-pilot-metrics.json"
    result = subprocess.run(
        ["node", str(METRICS), "--fixture", str(FIXTURES / "exec-jsonl.ndjson"), "--out", str(output_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text())
    assert payload["token_usage"] == {
        "availability": "measured",
        "source": "codex_event_metadata",
        "prompt": 11,
        "completion": 7,
        "total": 18,
    }
    assert payload["latency_source"] == "monotonic_event_clock"
    assert payload["human_intervention_source"] == "manual_event_ledger"


def test_metrics_sources_unavailable_without_authoritative_usage(tmp_path: Path):
    fixture = tmp_path / "events.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"event_type": "codex_turn_started", "monotonic_ms": 10}),
            json.dumps({"event_type": "manual_intervention", "monotonic_ms": 20}),
            json.dumps({"event_type": "codex_turn_finished", "monotonic_ms": 30}),
        ])
    )
    output_path = tmp_path / "metrics.json"
    result = subprocess.run(
        ["node", str(METRICS), "--fixture", str(fixture), "--out", str(output_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text())
    assert payload["token_usage"] == {
        "availability": "unavailable",
        "source": "none",
        "prompt": None,
        "completion": None,
        "total": None,
    }
    assert payload["human_intervention_count"] == 1
