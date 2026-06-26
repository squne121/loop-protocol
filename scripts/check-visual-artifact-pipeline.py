#!/usr/bin/env python3
"""
check-visual-artifact-pipeline.py — structural validator for the e2e visual
regression evidence pipeline in .github/workflows/ci.yml.

Enforces the artifact/summary wiring contract defined in
docs/dev/visual-baseline-registry.md §5 by **structurally parsing the YAML**
(not grep), with hard-fail value checks (not mere key presence), and by
**cross-validating** the hardcoded summary fingerprint against the actual
Playwright config / test sources so the fingerprint cannot silently drift.

Outputs VISUAL_ARTIFACT_PIPELINE_CHECK_V1 to stdout.
Exit code: 0 = pass, 1 = contract violation, 2 = usage / parse error.

Contract (registry §5) — each is a HARD FAIL, not a range/presence check:
  jobs.e2e upload steps for `playwright-report/` and `test-results/`:
    - uses     == exact allowed pin (default: actions/upload-artifact@v6)
    - if       == "${{ !cancelled() }}"  (always()/failure() rejected)
    - id       == the contract id for that path
    - with.name== the contract name for that path
    - with.path== the contract path
    - if-no-files-found == "warn"   (value, not presence)
    - retention-days    == 30       (value, not range)
  summary step (AFTER the uploads) whose run writes $GITHUB_STEP_SUMMARY and
  references both upload steps' outputs.artifact-url, plus every required
  fingerprint token.
  cross-validation: viewport / snapshotPathTemplate / maxDiffPixels echoed in
  the summary must match the values actually declared in playwright.config.ts
  and tests/e2e/m2-combat-mvp.spec.ts.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
    print("status: error")
    print("error: PyYAML is required (uv sync / pip install pyyaml)")
    sys.exit(2)

# --- Contract constants (registry §5). Version policy: upload-artifact is pinned
# to @v6 to stay consistent with the rest of .github/workflows/ci.yml (all
# existing upload-artifact uses are @v6). Bumping the allowed pin is a
# deliberate, reviewed change recorded in the registry "version policy" section.
ALLOWED_UPLOAD_USES = {"actions/upload-artifact@v6"}
REQUIRED_IF = "${{ !cancelled() }}"
REQUIRED_RETENTION_DAYS = 30
REQUIRED_IF_NO_FILES_FOUND = "warn"

# path -> required (id, name) for that upload step
EXPECTED_UPLOADS = {
    "playwright-report/": {"id": "upload-playwright-report", "name": "playwright-report"},
    "test-results/": {"id": "upload-test-results", "name": "test-results"},
}

# Tokens the summary step's run/env MUST contain (artifact URL wiring + fingerprint).
SUMMARY_REQUIRED_TOKENS = [
    "steps.upload-playwright-report.outputs.artifact-url",
    "steps.upload-test-results.outputs.artifact-url",
    "GITHUB_STEP_SUMMARY",
    "runner",
    "node",
    "Playwright",
    "browser",
    "project",
    "viewport",
    "deviceScaleFactor",
    "snapshotPathTemplate",
    "baseline path",
    "animations=disabled",
]

DEFAULT_WORKFLOW = ".github/workflows/ci.yml"
DEFAULT_PW_CONFIG = "playwright.config.ts"
DEFAULT_SPEC = "tests/e2e/m2-combat-mvp.spec.ts"


def _fail(failures: list[str], msg: str) -> None:
    failures.append(msg)


def _emit(checked_file, upload_paths, upload_ids, summary_ok, failures, extra=None):
    print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
    print("checked_file: " + str(checked_file))
    print("upload_paths_found: " + ",".join(sorted(p for p in upload_paths if p)))
    print("upload_ids: " + ",".join(upload_ids))
    print("summary_after_upload: " + ("true" if summary_ok else "false"))
    for k, v in (extra or {}).items():
        print(f"{k}: {v}")
    if failures:
        print("status: fail")
        for f in failures:
            print(f"- {f}")
        return 1
    print("status: pass")
    return 0


def _parse_playwright_fingerprint(pw_config: Path, spec: Path) -> tuple[dict, list[str]]:
    """Read the real config/spec so the summary fingerprint can be cross-checked.

    Returns (values, soft_errors). soft_errors are reported as failures only if
    the corresponding summary token exists to compare against.
    """
    values: dict[str, str] = {}
    errs: list[str] = []

    if pw_config.is_file():
        text = pw_config.read_text(encoding="utf-8")
        m = re.search(r"viewport:\s*\{\s*width:\s*(\d+)\s*,\s*height:\s*(\d+)\s*\}", text)
        if m:
            values["viewport"] = f"{m.group(1)}x{m.group(2)}"
        else:
            errs.append(f"could not parse viewport from {pw_config}")
        m = re.search(r"snapshotPathTemplate:\s*['\"]([^'\"]+)['\"]", text)
        if m:
            values["snapshotPathTemplate"] = m.group(1)
        else:
            errs.append(f"could not parse snapshotPathTemplate from {pw_config}")
    else:
        errs.append(f"playwright config not found: {pw_config}")

    if spec.is_file():
        text = spec.read_text(encoding="utf-8")
        diffs = set(re.findall(r"maxDiffPixels:\s*(\d+)", text))
        if len(diffs) == 1:
            values["maxDiffPixels"] = diffs.pop()
        elif len(diffs) == 0:
            errs.append(f"no maxDiffPixels found in {spec}")
        else:
            errs.append(f"inconsistent maxDiffPixels values in {spec}: {sorted(diffs)}")
    else:
        errs.append(f"spec not found: {spec}")

    return values, errs


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_WORKFLOW)
    pw_config = Path(argv[2]) if len(argv) > 2 else Path(DEFAULT_PW_CONFIG)
    spec = Path(argv[3]) if len(argv) > 3 else Path(DEFAULT_SPEC)

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
        return _emit(path, {}, [], False, ["missing jobs.e2e"])

    steps = jobs["e2e"].get("steps")
    if not isinstance(steps, list):
        return _emit(path, {}, [], False, ["jobs.e2e.steps is not a list"])

    # Locate upload-artifact steps keyed by `with.path`.
    upload_steps: dict[str, dict] = {}
    last_upload_index = -1
    upload_ids: list[str] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        # exact action name match before '@' so look-alikes (e.g.
        # actions/upload-artifact-malicious@v6) cannot satisfy the contract.
        action_name = uses.split("@", 1)[0]
        if action_name == "actions/upload-artifact":
            with_block = step.get("with") or {}
            wpath = str(with_block.get("path", "")).strip()
            upload_steps[wpath] = {"step": step, "index": idx, "uses": uses}
            last_upload_index = idx
            if step.get("id"):
                upload_ids.append(str(step.get("id")))

    for required_path, contract in EXPECTED_UPLOADS.items():
        if required_path not in upload_steps:
            _fail(failures, f"missing upload-artifact step with path: {required_path}")
            continue
        entry = upload_steps[required_path]
        step = entry["step"]
        with_block = step.get("with") or {}

        uses = str(entry["uses"]).strip()
        if uses not in ALLOWED_UPLOAD_USES:
            _fail(
                failures,
                f"upload step for {required_path} uses '{uses}'"
                f" not in allowed pin {sorted(ALLOWED_UPLOAD_USES)}",
            )

        cond = str(step.get("if", "")).strip()
        if cond != REQUIRED_IF:
            _fail(failures, f"upload step for {required_path} if='{cond}' must be exactly '{REQUIRED_IF}'")

        sid = str(step.get("id", "")).strip()
        if sid != contract["id"]:
            _fail(failures, f"upload step for {required_path} id='{sid}' must be '{contract['id']}'")

        wname = str(with_block.get("name", "")).strip()
        if wname != contract["name"]:
            _fail(failures, f"upload step for {required_path} with.name='{wname}' must be '{contract['name']}'")

        inff = str(with_block.get("if-no-files-found", "")).strip()
        if inff != REQUIRED_IF_NO_FILES_FOUND:
            _fail(
                failures,
                f"upload step for {required_path} if-no-files-found='{inff}'"
                f" must be '{REQUIRED_IF_NO_FILES_FOUND}'",
            )

        retention = with_block.get("retention-days")
        try:
            rv = int(retention)
        except (TypeError, ValueError):
            rv = None
        if rv != REQUIRED_RETENTION_DAYS:
            _fail(
                failures,
                f"upload step for {required_path}"
                f" retention-days={retention!r} must be {REQUIRED_RETENTION_DAYS}",
            )

    # Summary step: AFTER the last upload, references $GITHUB_STEP_SUMMARY.
    summary_blob = None
    summary_ok = False
    if last_upload_index >= 0:
        for idx in range(last_upload_index + 1, len(steps)):
            step = steps[idx]
            if not isinstance(step, dict):
                continue
            run = str(step.get("run", ""))
            env = step.get("env") or {}
            env_blob = " ".join(str(v) for v in env.values()) if isinstance(env, dict) else ""
            if "GITHUB_STEP_SUMMARY" in run:
                summary_ok = True
                summary_blob = run + "\n" + env_blob
                break
    if not summary_ok:
        _fail(failures, "no summary step after upload steps that writes $GITHUB_STEP_SUMMARY")
    else:
        for tok in SUMMARY_REQUIRED_TOKENS:
            if tok not in summary_blob:
                _fail(failures, f"summary step missing required token: {tok}")

        # Cross-validate echoed fingerprint against real config/spec (Major 1).
        fp, fp_errs = _parse_playwright_fingerprint(pw_config, spec)
        if "viewport" in fp:
            if f"viewport: {fp['viewport']}" not in summary_blob:
                _fail(failures, f"summary viewport does not match playwright.config ({fp['viewport']})")
        if "snapshotPathTemplate" in fp:
            if fp["snapshotPathTemplate"] not in summary_blob:
                _fail(
                    failures,
                    f"summary snapshotPathTemplate does not match"
                    f" playwright.config ({fp['snapshotPathTemplate']})",
                )
        if "maxDiffPixels" in fp:
            if f"maxDiffPixels={fp['maxDiffPixels']}" not in summary_blob:
                _fail(failures, f"summary maxDiffPixels does not match spec (maxDiffPixels={fp['maxDiffPixels']})")
        # fp parse errors only matter when we expected to compare.
        for e in fp_errs:
            _fail(failures, f"fingerprint cross-validation: {e}")

    return _emit(path, upload_steps, upload_ids, summary_ok, failures)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
