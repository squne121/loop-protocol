#!/usr/bin/env python3
"""
check-visual-artifact-pipeline.py — structural validator for the e2e visual
regression evidence pipeline in .github/workflows/ci.yml.

Validates the artifact/summary wiring contract defined in
docs/dev/visual-baseline-registry.md §5 by **structurally parsing the YAML**
(not grep). Outputs VISUAL_ARTIFACT_PIPELINE_CHECK_V1 to stdout.

Exit code: 0 = pass, 1 = contract violation, 2 = usage / parse error.

Checks (jobs.e2e.steps):
  - upload-artifact step exists for path `playwright-report/`
  - upload-artifact step exists for path `test-results/`
  - each upload step has an `id`
  - each upload step `if:` is an allowed condition (`${{ !cancelled() }}`,
    or `always()` which is allowed-but-flagged; bare `failure()`/`success()`
    are rejected because evidence must persist on success and failure)
  - each upload step declares `retention-days` (within 1..90) and `if-no-files-found`
  - a summary step exists AFTER the last upload step whose `run` references
    `$GITHUB_STEP_SUMMARY` and an upload step `outputs.artifact-url`
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
    print("status: error")
    print("error: PyYAML is required (pip install pyyaml / uv add pyyaml)")
    sys.exit(2)

ALLOWED_IF = {"${{ !cancelled() }}", "!cancelled()"}
FLAGGED_IF = {"${{ always() }}", "always()"}
RETENTION_MIN, RETENTION_MAX = 1, 90
REQUIRED_PATHS = ("playwright-report/", "test-results/")


def fail(failures: list[str], msg: str) -> None:
    failures.append(msg)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(".github/workflows/ci.yml")
    if not path.is_file():
        print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
        print("status: error")
        print(f"error: workflow file not found: {path}")
        return 2

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
        print("status: error")
        print(f"error: YAML parse failure: {exc}")
        return 2

    failures: list[str] = []

    jobs = (doc or {}).get("jobs")
    if not isinstance(jobs, dict) or "e2e" not in jobs:
        print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
        print("status: fail")
        print("checked_file: " + str(path))
        print("- missing jobs.e2e")
        return 1

    steps = jobs["e2e"].get("steps")
    if not isinstance(steps, list):
        print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
        print("status: fail")
        print("checked_file: " + str(path))
        print("- jobs.e2e.steps is not a list")
        return 1

    # Locate upload-artifact steps and their index, keyed by `with.path`.
    upload_steps: dict[str, dict] = {}
    last_upload_index = -1
    upload_ids: list[str] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        if uses.startswith("actions/upload-artifact"):
            with_block = step.get("with") or {}
            wpath = str(with_block.get("path", "")).strip()
            upload_steps[wpath] = {"step": step, "index": idx}
            last_upload_index = idx
            step_id = step.get("id")
            if step_id:
                upload_ids.append(str(step_id))

    for required in REQUIRED_PATHS:
        if required not in upload_steps:
            fail(failures, f"missing upload-artifact step with path: {required}")
            continue
        step = upload_steps[required]["step"]
        with_block = step.get("with") or {}

        if not step.get("id"):
            fail(failures, f"upload step for {required} has no `id:` (needed for outputs.artifact-url)")

        cond = str(step.get("if", "")).strip()
        if cond in ALLOWED_IF:
            pass
        elif cond in FLAGGED_IF:
            fail(failures, f"upload step for {required} uses always() — registry §5 requires documented rationale; prefer ${{{{ !cancelled() }}}}")
        else:
            fail(failures, f"upload step for {required} has disallowed if: '{cond}' (expected ${{{{ !cancelled() }}}})")

        retention = with_block.get("retention-days")
        if retention is None:
            fail(failures, f"upload step for {required} missing retention-days")
        else:
            try:
                rv = int(retention)
                if not (RETENTION_MIN <= rv <= RETENTION_MAX):
                    fail(failures, f"upload step for {required} retention-days {rv} out of range [1,90]")
            except (TypeError, ValueError):
                fail(failures, f"upload step for {required} retention-days not an integer: {retention!r}")

        if "if-no-files-found" not in with_block:
            fail(failures, f"upload step for {required} missing if-no-files-found policy")

    # Summary step: must come AFTER the last upload step, reference
    # $GITHUB_STEP_SUMMARY and an upload step output artifact-url.
    summary_ok = False
    if last_upload_index >= 0:
        for idx in range(last_upload_index + 1, len(steps)):
            step = steps[idx]
            if not isinstance(step, dict):
                continue
            run = str(step.get("run", ""))
            env = step.get("env") or {}
            env_blob = " ".join(str(v) for v in env.values()) if isinstance(env, dict) else ""
            blob = run + " " + env_blob
            if "GITHUB_STEP_SUMMARY" in run and "artifact-url" in blob:
                summary_ok = True
                break
    if not summary_ok:
        fail(failures, "no summary step after upload steps that writes $GITHUB_STEP_SUMMARY and references outputs.artifact-url")

    print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
    print("checked_file: " + str(path))
    print("upload_paths_found: " + ",".join(sorted(p for p in upload_steps if p)))
    print("upload_ids: " + ",".join(upload_ids))
    print("summary_after_upload: " + ("true" if summary_ok else "false"))
    if failures:
        print("status: fail")
        for f in failures:
            print(f"- {f}")
        return 1
    print("status: pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
