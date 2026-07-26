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

Active capture (not suite) extension (Issue #1387):
  the summary and validator no longer treat the VRT evidence pipeline as a
  single suite-level fingerprint. Each individual VRT **capture** (an
  individual `toHaveScreenshot()` / `expectDomOverlayScreenshot()` call site)
  is enumerated, and the summary's declared per-capture fingerprint (embedded
  in the workflow between the `ACTIVE_VRT_CAPTURES_BEGIN` / `_END` markers)
  is cross-validated against a fresh, independent derivation from the actual
  spec files / playwright.config.ts / visual-utils.ts registry maturity map.
"""
from __future__ import annotations

import ast
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
    # Active capture (not suite) extension (AC1, AC2).
    "active capture",
    "screenshot directory",
]

# Artifact absence-state contract (AC7): the summary must be able to classify
# each upload step outcome into exactly these four states.
ARTIFACT_STATUS_STATES = ("uploaded", "no_files", "step_not_run", "upload_failed")
ARTIFACT_STATUS_REQUIRED_TOKENS = [
    "steps.upload-playwright-report.outcome",
    "steps.upload-test-results.outcome",
    *ARTIFACT_STATUS_STATES,
]

DEFAULT_WORKFLOW = ".github/workflows/ci.yml"
DEFAULT_PW_CONFIG = "playwright.config.ts"
DEFAULT_SPEC = "tests/e2e/m2-combat-mvp.spec.ts"
DEFAULT_E2E_DIR = "tests/e2e"
DEFAULT_VISUAL_UTILS = "tests/e2e/visual-utils.ts"

ACTIVE_CAPTURES_BEGIN_MARKER = "ACTIVE_VRT_CAPTURES_BEGIN"
ACTIVE_CAPTURES_END_MARKER = "ACTIVE_VRT_CAPTURES_END"

# Reserved snapshot-root prefixes (AC6, AC9(f)): Playwright E2E VRT baselines
# and the (not-yet-introduced) Vitest component VRT baselines must never
# share a directory root. `docs/dev/visual-baseline-registry.md` §5 documents
# this contract; these constants are the single source of truth both the
# validator and (indirectly, via review) future Vitest wiring must respect.
PLAYWRIGHT_SNAPSHOT_ROOT_PREFIX = "tests/e2e/__screenshots__/"
VITEST_COMPONENT_SNAPSHOT_ROOT_PREFIX = "tests/component/__screenshots__/"

# Helper functions in tests/e2e/visual-utils.ts that themselves apply
# `stylePath` (the shared freeze CSS) and are not raw `toHaveScreenshot()`
# call sites. Both require a `registryId` (3rd positional arg).
REGISTRY_HELPER_CALLS = ("expectDomOverlayScreenshot", "expectCanvasVisualCueScreenshot")


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


# ---------------------------------------------------------------------------
# Active capture (not suite) support — Issue #1387
# ---------------------------------------------------------------------------


def parse_registry_maturity(visual_utils_text: str) -> dict[str, str]:
    """Extract the `VISUAL_BASELINE_REGISTRY_MATURITY` map from visual-utils.ts.

    Returns {registry_id: maturity}. Structural regex parse (not a full TS
    parser) — sufficient because the object is a flat string-to-string-literal
    record by construction (typechecked in visual-utils.ts itself).
    """
    m = re.search(
        r"VISUAL_BASELINE_REGISTRY_MATURITY[^=]*=\s*\{(.*?)\n\s*\}",
        visual_utils_text,
        re.DOTALL,
    )
    if not m:
        return {}
    body = m.group(1)
    return dict(re.findall(r"'([^']+)':\s*'([^']+)'", body))


def parse_playwright_snapshot_config(pw_config_text: str) -> dict[str, str]:
    """Extract `testDir` and `snapshotPathTemplate` from playwright.config.ts."""
    result: dict[str, str] = {}
    m = re.search(r"testDir:\s*['\"]([^'\"]+)['\"]", pw_config_text)
    if m:
        result["test_dir"] = m.group(1).lstrip("./")
    m = re.search(r"snapshotPathTemplate:\s*['\"]([^'\"]+)['\"]", pw_config_text)
    if m:
        result["snapshot_path_template"] = m.group(1)
    return result


def resolve_capture_directory(test_dir: str, snapshot_path_template: str, spec_filename: str) -> str:
    """Resolve the directory (excluding filename) a capture's baseline lives in.

    `{testFilePath}` resolves to the spec file's path relative to `testDir`
    (here: just the filename, since VRT specs live directly under `testDir`).
    """
    resolved = snapshot_path_template.replace("{testDir}", test_dir).replace(
        "{testFilePath}", spec_filename
    )
    # Strip the trailing `{arg}{ext}` filename placeholder, keep the directory.
    directory = resolved.rsplit("/", 1)[0] + "/"
    return directory


def _find_balanced_call(text: str, open_paren_idx: int) -> int:
    """Return the index just past the matching ')' for the '(' at open_paren_idx."""
    depth = 0
    for i in range(open_paren_idx, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced parentheses in capture call site")


def _split_top_level_args(inner: str) -> list[str]:
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        args.append("".join(current))
    return [a.strip() for a in args]


def _is_guard_only_call(text: str, call_start: int, call_end: int) -> bool:
    """True if this call site is a negative/guard test (`.rejects.toThrow(...)`),
    not a real, executed capture."""
    prefix = re.sub(r"\s+", " ", text[max(0, call_start - 80) : call_start]).strip()
    if prefix.endswith("expect(") or prefix.endswith("expect( "):
        return True
    suffix = text[call_end : call_end + 160]
    return ".rejects" in suffix


def _parse_comparator(options_blob: str) -> tuple[str | None, str | None, list[str]]:
    """Return (comparator_kind, comparator_value, errors) from an options blob."""
    px = re.findall(r"maxDiffPixels:\s*(\d+(?:\.\d+)?)", options_blob)
    ratio = re.findall(r"maxDiffPixelRatio:\s*(\d+(?:\.\d+)?)", options_blob)
    errors: list[str] = []
    if px and ratio:
        errors.append("both maxDiffPixels and maxDiffPixelRatio declared (mutually exclusive)")
        return None, None, errors
    if px:
        return "maxDiffPixels", px[0], errors
    if ratio:
        return "maxDiffPixelRatio", ratio[0], errors
    errors.append("neither maxDiffPixels nor maxDiffPixelRatio declared")
    return None, None, errors


def extract_derived_active_captures(
    e2e_dir: Path, pw_snapshot_config: dict[str, str], registry_maturity: dict[str, str]
) -> tuple[list[dict], list[str]]:
    """Statically re-derive the ground-truth set of active VRT captures from
    the real spec sources (not suites — individual call sites), skipping
    guard-only negative-test call sites and `pending-baseline` scenarios."""
    captures: list[dict] = []
    errors: list[str] = []
    test_dir = pw_snapshot_config.get("test_dir", "tests/e2e")
    template = pw_snapshot_config.get(
        "snapshot_path_template", "{testDir}/__screenshots__/{testFilePath}/{arg}{ext}"
    )

    if not e2e_dir.is_dir():
        return captures, [f"e2e spec directory not found: {e2e_dir}"]

    for spec_path in sorted(e2e_dir.glob("*.spec.ts")):
        text = spec_path.read_text(encoding="utf-8")
        spec_filename = spec_path.name

        # 1. Registry-helper call sites (expectDomOverlayScreenshot / expectCanvasVisualCueScreenshot).
        for helper_name in REGISTRY_HELPER_CALLS:
            for m in re.finditer(rf"\b{helper_name}\(", text):
                open_idx = m.end() - 1
                call_end = _find_balanced_call(text, open_idx)
                if _is_guard_only_call(text, m.start(), call_end):
                    continue
                inner = text[open_idx + 1 : call_end - 1]
                if not inner.strip():
                    # Bare `functionName()` mention (e.g. inside a docstring/
                    # comment cross-reference) — not a real call site.
                    continue
                args = _split_top_level_args(inner)
                if len(args) < 3:
                    errors.append(f"{spec_path}: malformed {helper_name}() call (too few args)")
                    continue
                name_m = re.search(r"'([^']+)'", args[1])
                registry_m = re.search(r"'([^']+)'", args[2])
                if not name_m or not registry_m:
                    errors.append(f"{spec_path}: could not parse name/registryId in {helper_name}() call")
                    continue
                screenshot_name = name_m.group(1)
                registry_id = registry_m.group(1)
                maturity = registry_maturity.get(registry_id)
                if maturity == "pending-baseline":
                    # Fails closed at runtime (visual-utils.ts) — never active.
                    continue
                options_blob = args[3] if len(args) > 3 else ""
                comparator_kind, comparator_value, cmp_errors = _parse_comparator(options_blob)
                for e in cmp_errors:
                    errors.append(f"{spec_path}::{screenshot_name}: {e}")
                captures.append(
                    {
                        "capture_id": f"{spec_filename}::{screenshot_name}",
                        "spec_file": f"tests/e2e/{spec_filename}",
                        "screenshot_name": screenshot_name,
                        "registry_id": registry_id,
                        "maturity": maturity,
                        "directory": resolve_capture_directory(test_dir, template, spec_filename),
                        "comparator_kind": comparator_kind,
                        "comparator_value": comparator_value,
                        # Both helpers apply the shared freeze CSS via stylePath.
                        "style_path": True,
                    }
                )

        # 2. Direct `.toHaveScreenshot(` call sites (not routed through a helper).
        for m in re.finditer(r"\.toHaveScreenshot\(", text):
            open_idx = m.end() - 1
            call_end = _find_balanced_call(text, open_idx)
            if _is_guard_only_call(text, m.start(), call_end):
                continue
            inner = text[open_idx + 1 : call_end - 1]
            if not inner.strip():
                # Bare `toHaveScreenshot()` mention (docstring/comment
                # cross-reference) — not a real call site.
                continue
            args = _split_top_level_args(inner)
            if not args:
                errors.append(f"{spec_path}: malformed toHaveScreenshot() call (no args)")
                continue
            name_m = re.search(r"'([^']+)'", args[0])
            if not name_m:
                # toHaveScreenshot() may be called with no explicit name; skip
                # (not a named baseline this contract enumerates).
                continue
            screenshot_name = name_m.group(1)
            options_blob = args[1] if len(args) > 1 else ""
            comparator_kind, comparator_value, cmp_errors = _parse_comparator(options_blob)
            for e in cmp_errors:
                errors.append(f"{spec_path}::{screenshot_name}: {e}")
            captures.append(
                {
                    "capture_id": f"{spec_filename}::{screenshot_name}",
                    "spec_file": f"tests/e2e/{spec_filename}",
                    "screenshot_name": screenshot_name,
                    "registry_id": None,
                    "maturity": None,
                    "directory": resolve_capture_directory(test_dir, template, spec_filename),
                    "comparator_kind": comparator_kind,
                    "comparator_value": comparator_value,
                    "style_path": "stylePath" in options_blob,
                }
            )

    return captures, errors


def extract_declared_active_captures(workflow_text: str) -> tuple[list[dict], list[str]]:
    """Parse the `CAPTURES = [...]` python literal embedded in the workflow's
    summary step, delimited by ACTIVE_VRT_CAPTURES_BEGIN/_END markers."""
    errors: list[str] = []
    begin_idx = workflow_text.find(ACTIVE_CAPTURES_BEGIN_MARKER)
    end_idx = workflow_text.find(ACTIVE_CAPTURES_END_MARKER)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        return [], [f"missing {ACTIVE_CAPTURES_BEGIN_MARKER}/{ACTIVE_CAPTURES_END_MARKER} markers in workflow"]

    block = workflow_text[begin_idx:end_idx]
    m = re.search(r"CAPTURES\s*=\s*(\[.*\])", block, re.DOTALL)
    if not m:
        return [], ["could not locate CAPTURES = [...] literal between active-capture markers"]
    literal_text = m.group(1)
    # The literal is indented as embedded YAML `run:` block content; dedent by
    # stripping the minimum common leading whitespace before literal_eval.
    lines = literal_text.splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    min_indent = min(indents) if indents else 0
    dedented = "\n".join(line[min_indent:] if line.strip() else line for line in lines)
    try:
        captures = ast.literal_eval(dedented)
    except (SyntaxError, ValueError) as exc:
        return [], [f"could not parse CAPTURES literal: {exc}"]
    if not isinstance(captures, list):
        return [], ["CAPTURES literal is not a list"]
    return captures, errors


def cross_validate_active_captures(
    declared: list[dict], derived: list[dict], registry_maturity: dict[str, str]
) -> list[str]:
    """Cross-validate the workflow's declared per-capture fingerprint against
    the freshly re-derived ground truth. Returns a list of hard-fail messages
    (empty == valid)."""
    failures: list[str] = []

    declared_ids = [c.get("capture_id") for c in declared]
    if len(declared_ids) != len(set(declared_ids)):
        seen: set[str] = set()
        dupes: set[str] = set()
        for cid in declared_ids:
            if cid in seen:
                dupes.add(cid)
            seen.add(cid)
        failures.append(f"duplicate declared capture_id(s): {sorted(dupes)}")

    derived_by_id = {c["capture_id"]: c for c in derived}
    declared_by_id = {c.get("capture_id"): c for c in declared}

    missing_in_declared = sorted(set(derived_by_id) - set(declared_by_id))
    if missing_in_declared:
        failures.append(f"active capture(s) not declared in workflow summary: {missing_in_declared}")

    extra_in_declared = sorted(set(declared_by_id) - set(derived_by_id))
    if extra_in_declared:
        failures.append(
            f"workflow declares capture(s) that are not real active captures (directory drift, "
            f"stale, or pending-baseline): {extra_in_declared}"
        )

    for capture_id, declared_capture in declared_by_id.items():
        # Pending-baseline registered as active (AC9(e)).
        registry_id = declared_capture.get("registry_id")
        if registry_id is not None and registry_maturity.get(registry_id) == "pending-baseline":
            failures.append(
                f"{capture_id}: registryId '{registry_id}' is pending-baseline and must not be "
                "declared as an active capture"
            )

        # Playwright / Vitest baseline root mixing (AC6, AC9(f)).
        directory = declared_capture.get("directory", "")
        if directory.startswith(VITEST_COMPONENT_SNAPSHOT_ROOT_PREFIX):
            failures.append(
                f"{capture_id}: directory '{directory}' is under the reserved Vitest component "
                "snapshot root; Playwright and Vitest baseline roots must not mix"
            )
        elif not directory.startswith(PLAYWRIGHT_SNAPSHOT_ROOT_PREFIX):
            failures.append(
                f"{capture_id}: directory '{directory}' is outside the Playwright snapshot root "
                f"'{PLAYWRIGHT_SNAPSHOT_ROOT_PREFIX}'"
            )

        # Comparator exclusivity (AC5).
        kind = declared_capture.get("comparator_kind")
        if kind not in ("maxDiffPixels", "maxDiffPixelRatio"):
            failures.append(
                f"{capture_id}: comparator_kind must be exactly one of "
                f"maxDiffPixels/maxDiffPixelRatio, got {kind!r}"
            )
        if declared_capture.get("comparator_value") in (None, ""):
            failures.append(f"{capture_id}: comparator_value is missing")

        # Artifact digest wiring (AC9(g)).
        if not declared_capture.get("digest_env"):
            failures.append(f"{capture_id}: missing digest_env (artifact digest wiring)")

        derived_capture = derived_by_id.get(capture_id)
        if derived_capture is None:
            continue  # already reported above as extra_in_declared

        if declared_capture.get("directory") != derived_capture.get("directory"):
            failures.append(
                f"{capture_id}: declared directory '{declared_capture.get('directory')}' does not "
                f"match derived directory '{derived_capture.get('directory')}' (directory drift)"
            )
        if declared_capture.get("comparator_kind") != derived_capture.get("comparator_kind"):
            failures.append(
                f"{capture_id}: declared comparator_kind "
                f"'{declared_capture.get('comparator_kind')}' does not match derived "
                f"'{derived_capture.get('comparator_kind')}'"
            )
        if str(declared_capture.get("comparator_value")) != str(derived_capture.get("comparator_value")):
            failures.append(
                f"{capture_id}: declared comparator_value "
                f"'{declared_capture.get('comparator_value')}' does not match derived "
                f"'{derived_capture.get('comparator_value')}'"
            )
        if bool(declared_capture.get("style_path")) != bool(derived_capture.get("style_path")):
            failures.append(
                f"{capture_id}: declared style_path={declared_capture.get('style_path')!r} does not "
                f"match derived style_path={derived_capture.get('style_path')!r}"
            )

    return failures


def check_artifact_status_wiring(summary_run_text: str) -> list[str]:
    """AC7: the summary step must classify each upload step's outcome into
    uploaded / no_files / step_not_run / upload_failed — not just presence."""
    failures: list[str] = []
    for tok in ARTIFACT_STATUS_REQUIRED_TOKENS:
        if tok not in summary_run_text:
            failures.append(f"summary step missing artifact-status token: {tok}")
    return failures


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_WORKFLOW)
    pw_config = Path(argv[2]) if len(argv) > 2 else Path(DEFAULT_PW_CONFIG)
    spec = Path(argv[3]) if len(argv) > 3 else Path(DEFAULT_SPEC)
    e2e_dir = Path(argv[4]) if len(argv) > 4 else Path(DEFAULT_E2E_DIR)
    visual_utils = Path(argv[5]) if len(argv) > 5 else Path(DEFAULT_VISUAL_UTILS)

    if not path.is_file():
        print("VISUAL_ARTIFACT_PIPELINE_CHECK_V1")
        print("status: error")
        print(f"error: workflow file not found: {path}")
        return 2

    workflow_text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(workflow_text)
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
            step_env_obj = step.get("env") or {}
            env_blob = " ".join(str(v) for v in step_env_obj.values()) if isinstance(step_env_obj, dict) else ""
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

        # Artifact absence-state classification (AC7).
        failures.extend(check_artifact_status_wiring(summary_blob))

        # Active capture (not suite) cross-validation (AC1-AC5, AC9).
        pw_config_text = pw_config.read_text(encoding="utf-8") if pw_config.is_file() else ""
        visual_utils_text = visual_utils.read_text(encoding="utf-8") if visual_utils.is_file() else ""
        registry_maturity = parse_registry_maturity(visual_utils_text)
        pw_snapshot_config = parse_playwright_snapshot_config(pw_config_text)

        declared_captures, declared_errors = extract_declared_active_captures(workflow_text)
        for e in declared_errors:
            _fail(failures, f"active capture (declared): {e}")

        derived_captures, derived_errors = extract_derived_active_captures(
            e2e_dir, pw_snapshot_config, registry_maturity
        )
        for e in derived_errors:
            _fail(failures, f"active capture (derived): {e}")

        if not declared_errors and not derived_errors:
            for f in cross_validate_active_captures(declared_captures, derived_captures, registry_maturity):
                _fail(failures, f"active capture: {f}")

    return _emit(path, upload_steps, upload_ids, summary_ok, failures)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
