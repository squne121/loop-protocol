#!/usr/bin/env python3
"""
check-visual-artifact-pipeline.py — structural validator for the e2e visual
regression evidence pipeline in .github/workflows/ci.yml.

Enforces the artifact/summary wiring contract defined in
docs/dev/visual-baseline-registry.md §5 by **structurally parsing the YAML**
(not grep), with hard-fail value checks (not mere key presence), and by
**cross-validating** the per-capture fingerprint declared in the summary
against the actual Playwright config / test sources so it cannot silently
drift.

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

Active capture (not suite) extension (Issue #1387):
  the summary and validator do not treat the VRT evidence pipeline as a
  single suite-level fingerprint. Each individual VRT **capture** (an
  individual `toHaveScreenshot()` / `expectDomOverlayScreenshot()` /
  `expectCanvasVisualCueScreenshot()` call site) is enumerated, and the
  summary's declared per-capture fingerprint (embedded in the workflow
  between the `ACTIVE_VRT_CAPTURES_BEGIN` / `_END` markers) is
  cross-validated field-by-field against a fresh, independent derivation
  from the actual spec files / playwright.config.ts /
  docs/dev/visual-baseline-registry.md / tests/e2e/visual-utils.ts registry
  maturity map. There is deliberately no suite-level fingerprint left to
  drift silently (PR #1813 review fix, P1 Blocker 2): the old
  `m2-combat-mvp.spec.ts`-only fingerprint parser has been removed.
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
# NOTE (PR #1813 review fix, P1 Blocker 2): "baseline path" / "animations=disabled"
# were removed — those were leftover suite-level fixed strings that
# contradicted the per-capture table (which has its own, possibly differing,
# comparator/directory per row). The run-wide environment fingerprint tokens
# below (runner/node/Playwright/browser/project/viewport/deviceScaleFactor/
# snapshotPathTemplate) describe the single CI run's environment, not a fixed
# baseline path, so they are not suite-fingerprint drift risks.
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
    # Active capture (not suite) extension (AC1, AC2).
    "active capture",
    "screenshot directory",
]

# Artifact absence-state contract (AC7): the summary must be able to classify
# each upload step outcome into exactly these four states, based on the
# official GitHub `upload-artifact` step outputs (PR #1813 review fix, P1
# Blocker 3) — not a local post-hoc directory scan, which can no longer be
# trusted to reflect the uploaded lane's contents once a later step in the
# same job reuses the same directory name for a different lane.
ARTIFACT_STATUS_STATES = ("uploaded", "no_files", "step_not_run", "upload_failed")
ARTIFACT_STATUS_REQUIRED_TOKENS = [
    "steps.upload-playwright-report.outcome",
    "steps.upload-test-results.outcome",
    "steps.upload-playwright-report.outputs.artifact-id",
    "steps.upload-test-results.outputs.artifact-id",
    *ARTIFACT_STATUS_STATES,
]

DEFAULT_WORKFLOW = ".github/workflows/ci.yml"
DEFAULT_PW_CONFIG = "playwright.config.ts"
DEFAULT_E2E_DIR = "tests/e2e"
DEFAULT_VISUAL_UTILS = "tests/e2e/visual-utils.ts"
DEFAULT_REGISTRY_DOC = "docs/dev/visual-baseline-registry.md"
DEFAULT_COMPONENT_DIR = "tests/component"
VITEST_COMPONENT_TEST_GLOB = "*.vrt.test.ts"

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

# Playwright device preset -> browser engine family. Only the presets this
# repo's playwright.config.ts actually uses need an entry; an unrecognised
# preset is a hard fail (fail-closed derivation) rather than a silent guess
# (PR #1813 review fix, P1 Blocker 1 — every CAPTURES field must be
# independently re-derivable, including browser/project/deviceScaleFactor).
KNOWN_DEVICE_BROWSER_FAMILY = {
    "Desktop Chrome": "chromium",
    "Desktop Firefox": "firefox",
    "Desktop Safari": "webkit",
}
# `deviceScaleFactor` default for each preset, per Playwright's own device
# descriptors (docs/dev/visual-baseline-registry.md §1: "deviceScaleFactor
# は既定 1" for Desktop Chrome). playwright.config.ts does not override
# deviceScaleFactor, so this default applies unless an explicit
# `deviceScaleFactor:` appears in the `use` block (checked separately).
KNOWN_DEVICE_DEFAULT_DSF = {
    "Desktop Chrome": 1,
    "Desktop Firefox": 1,
    "Desktop Safari": 2,
}

# Every active capture currently shares the single job-level `test-results/`
# artifact (docs/dev/visual-baseline-registry.md §"capture 単位の CI 証跡契約") —
# there is no per-capture/per-suite artifact wiring yet, so this is a fixed,
# checkable constant rather than something derived per spec file.
REQUIRED_ARTIFACT_SCOPE = "job"
REQUIRED_DIGEST_ENV = "TEST_RESULTS_DIGEST"


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


# ---------------------------------------------------------------------------
# TypeScript-lite lexing helpers — string/template-literal/comment aware, so
# balanced-paren and top-level-comma scanning cannot be confused by a `(`,
# `)`, or `,` that only appears inside a string, template literal, or comment
# (PR #1813 review fix, P2 Blocker 5). Length-preserving: indices into the
# masked text remain valid indices into the original text.
# ---------------------------------------------------------------------------


def _mask_ts_noise(text: str) -> str:
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            while j < n and text[j] != quote:
                if text[j] == "\\":
                    j += 2
                    continue
                j += 1
            j = min(j + 1, n)
            for k in range(i + 1, min(j - 1, n)):
                out[k] = "x"
            i = j
            continue
        i += 1
    return "".join(out)


def _find_balanced_call(masked_text: str, open_paren_idx: int) -> int:
    """Return the index just past the matching ')' for the '(' at open_paren_idx.

    Scans `masked_text` (string/comment interiors neutralised by
    `_mask_ts_noise`) so parens inside a string/template literal/comment
    cannot desynchronise the depth count.
    """
    depth = 0
    for i in range(open_paren_idx, len(masked_text)):
        ch = masked_text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced parentheses in capture call site")


def _split_top_level_args(inner: str, masked_inner: str) -> list[str]:
    """Split `inner` (original text) on top-level commas, using `masked_inner`
    (same length/content region, string/comment interiors neutralised) to
    decide comma/bracket depth so a comma inside a string/template literal is
    never mistaken for an argument separator."""
    args: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(masked_inner):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(inner[start:i])
            start = i + 1
    args.append(inner[start:])
    return [a.strip() for a in args]


def _is_guard_only_call(text: str, call_start: int, call_end: int) -> bool:
    """True if this call site is a negative/guard test (`.rejects.toThrow(...)`),
    not a real, executed capture."""
    prefix = re.sub(r"\s+", " ", text[max(0, call_start - 80) : call_start]).strip()
    if prefix.endswith("expect(") or prefix.endswith("expect( "):
        return True
    suffix = text[call_end : call_end + 160]
    return ".rejects" in suffix


def _parse_screenshot_name(arg_text: str) -> tuple[str | None, list[str]]:
    """Parse a `toHaveScreenshot()` / registry-helper name argument.

    Supports a single quoted string (`'...'` / `"..."` / `` `...` ``, PR
    #1813 review fix, P2 Blocker 5 — double-quote support) and an
    `Array<string>` name (joined with `/`, mirroring Playwright's own
    path-segment join for array names). Anything else is a parse failure
    (hard fail, not a silent skip).
    """
    stripped = arg_text.strip()
    if stripped.startswith("["):
        parts = re.findall(r"""(['"`])((?:(?!\1).)*)\1""", stripped)
        if not parts:
            return None, [f"could not parse any quoted segment in array name {arg_text!r}"]
        return "/".join(p[1] for p in parts), []
    m = re.match(r"""^(['"`])((?:(?!\1).)*)\1$""", stripped)
    if m:
        return m.group(2), []
    return None, [f"unrecognized screenshot name argument: {arg_text!r}"]


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


def _parse_vitest_comparator(options_blob: str) -> tuple[str | None, str | None, list[str]]:
    """Return (comparator_kind, comparator_value, errors) from a Vitest
    Browser Mode `toMatchScreenshot()` `comparatorOptions` blob (AC10, Issue
    #1389). Vitest's counterpart of Playwright's `maxDiffPixels` /
    `maxDiffPixelRatio` (`_parse_comparator` above, unmodified by this
    Issue): `allowedMismatchedPixels` / `allowedMismatchedPixelRatio` are
    mutually exclusive absolute-count/ratio axes, `threshold` is a separate
    per-pixel color-difference axis that may be declared on its own."""
    px = re.findall(r"allowedMismatchedPixels:\s*(\d+(?:\.\d+)?)", options_blob)
    ratio = re.findall(r"allowedMismatchedPixelRatio:\s*(\d+(?:\.\d+)?)", options_blob)
    threshold = re.findall(r"threshold:\s*(\d+(?:\.\d+)?)", options_blob)
    errors: list[str] = []
    if px and ratio:
        errors.append(
            "both allowedMismatchedPixels and allowedMismatchedPixelRatio declared (mutually exclusive)"
        )
        return None, None, errors
    if px:
        return "allowedMismatchedPixels", px[0], errors
    if ratio:
        return "allowedMismatchedPixelRatio", ratio[0], errors
    if threshold:
        return "threshold", threshold[0], errors
    errors.append("neither allowedMismatchedPixels, allowedMismatchedPixelRatio, nor threshold declared")
    return None, None, errors


def _parse_path_template_override(options_blob: str) -> str | None:
    """Assertion-specific `pathTemplate` option (PR #1813 review fix, P2
    Blocker 5) — overrides the global `snapshotPathTemplate` for this one
    capture's directory resolution when present."""
    m = re.search(r"pathTemplate:\s*['\"]([^'\"]+)['\"]", options_blob)
    return m.group(1) if m else None


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


def parse_registry_filename_to_id(registry_md_text: str) -> dict[str, str]:
    """Extract {screenshot PNG filename: registry id} from the
    `docs/dev/visual-baseline-registry.md` §3 contract table's
    `artifact/test` column (PR #1813 review fix, P1 Blocker 1).

    Direct `.toHaveScreenshot()` call sites (not routed through a registry
    helper) never pass a `registryId` at the call site — Playwright's own
    `toHaveScreenshot()` API has no such parameter. The registry table is the
    only independent, machine-readable source of truth associating these
    legacy captures with a registry id, so it is used (in addition to the
    in-source registryId argument for helper-routed captures) to
    cross-validate the workflow's declared `registry_id` for every capture,
    not only the helper-routed ones.
    """
    mapping: dict[str, str] = {}
    for line in registry_md_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("|---"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("id", ""):
            continue
        registry_id = cells[0]
        artifact_cell = cells[3]
        for fname in re.findall(r"([\w.-]+\.png)", artifact_cell):
            mapping[fname] = registry_id
    return mapping


def parse_playwright_snapshot_config(pw_config_text: str) -> dict[str, object]:
    """Extract the run-wide constants every active capture is cross-validated
    against: `testDir`, `snapshotPathTemplate`, `viewport`, `project` name,
    `browser` family, and `deviceScaleFactor` (PR #1813 review fix, P1
    Blocker 1 — these fields were previously declared but never verified)."""
    result: dict[str, object] = {}
    m = re.search(r"testDir:\s*['\"]([^'\"]+)['\"]", pw_config_text)
    if m:
        result["test_dir"] = m.group(1).lstrip("./")
    m = re.search(r"snapshotPathTemplate:\s*['\"]([^'\"]+)['\"]", pw_config_text)
    if m:
        result["snapshot_path_template"] = m.group(1)
    m = re.search(r"viewport:\s*\{\s*width:\s*(\d+)\s*,\s*height:\s*(\d+)\s*\}", pw_config_text)
    if m:
        result["viewport"] = f"{m.group(1)}x{m.group(2)}"
    m = re.search(r"projects:\s*\[(.*?)\n\s*\]", pw_config_text, re.DOTALL)
    if m:
        projects_block = m.group(1)
        nm = re.search(r"name:\s*['\"]([^'\"]+)['\"]", projects_block)
        if nm:
            result["project"] = nm.group(1)
        dm = re.search(r"devices\[['\"]([^'\"]+)['\"]\]", projects_block)
        if dm:
            device_name = dm.group(1)
            result["device_name"] = device_name
            result["browser"] = KNOWN_DEVICE_BROWSER_FAMILY.get(device_name)
            result["device_scale_factor"] = KNOWN_DEVICE_DEFAULT_DSF.get(device_name)
    # An explicit override anywhere in the config file wins over the device
    # preset default.
    dsf_override = re.search(r"deviceScaleFactor:\s*(\d+)", pw_config_text)
    if dsf_override:
        result["device_scale_factor"] = int(dsf_override.group(1))
    return result


def resolve_capture_directory(test_dir: str, snapshot_path_template: str, spec_filename: str) -> str:
    """Resolve the directory (excluding filename) a capture's baseline lives in.

    `{testFilePath}` resolves to the spec file's path relative to `testDir`
    (here: just the filename, since VRT specs live directly under `testDir`).
    """
    resolved = snapshot_path_template.replace("{testDir}", test_dir).replace("{testFilePath}", spec_filename)
    # Strip the trailing `{arg}{ext}` filename placeholder, keep the directory.
    directory = resolved.rsplit("/", 1)[0] + "/"
    return directory


def extract_derived_active_captures(
    e2e_dir: Path,
    pw_snapshot_config: dict[str, object],
    registry_maturity: dict[str, str],
    registry_filename_to_id: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Statically re-derive the ground-truth set of active VRT captures from
    the real spec sources (not suites — individual call sites), skipping
    guard-only negative-test call sites and `pending-baseline` scenarios."""
    captures: list[dict] = []
    errors: list[str] = []
    registry_filename_to_id = registry_filename_to_id or {}
    test_dir = pw_snapshot_config.get("test_dir", "tests/e2e")
    template = pw_snapshot_config.get("snapshot_path_template", "{testDir}/__screenshots__/{testFilePath}/{arg}{ext}")
    browser = pw_snapshot_config.get("browser")
    project = pw_snapshot_config.get("project")
    viewport = pw_snapshot_config.get("viewport")
    device_scale_factor = pw_snapshot_config.get("device_scale_factor")

    if browser is None:
        errors.append("could not resolve browser family from playwright.config.ts device preset")
    if project is None:
        errors.append("could not parse project name from playwright.config.ts")
    if viewport is None:
        errors.append("could not parse viewport from playwright.config.ts")
    if device_scale_factor is None:
        errors.append("could not resolve deviceScaleFactor from playwright.config.ts")

    if not e2e_dir.is_dir():
        return captures, [f"e2e spec directory not found: {e2e_dir}"]

    common_fields = {
        "browser": browser,
        "project": project,
        "viewport": viewport,
        "device_scale_factor": device_scale_factor,
        "artifact_scope": REQUIRED_ARTIFACT_SCOPE,
        "digest_env": REQUIRED_DIGEST_ENV,
        "retention_days": REQUIRED_RETENTION_DAYS,
    }

    # rglob (not glob): Playwright's own `testDir` is walked recursively, so a
    # spec placed in a subdirectory must not be silently excluded (PR #1813
    # review fix, P2 Blocker 5).
    for spec_path in sorted(e2e_dir.rglob("*.spec.ts")):
        if not spec_path.is_file():
            # `rglob("*.spec.ts")` also matches directories with that literal
            # name (e.g. `__screenshots__/m2-combat-mvp.spec.ts/`, the
            # baseline PNG directory for that spec) — only real files are
            # spec sources.
            continue
        text = spec_path.read_text(encoding="utf-8")
        masked = _mask_ts_noise(text)
        spec_filename = spec_path.name

        # 1. Registry-helper call sites (expectDomOverlayScreenshot / expectCanvasVisualCueScreenshot).
        for helper_name in REGISTRY_HELPER_CALLS:
            for m in re.finditer(rf"\b{helper_name}\(", masked):
                open_idx = m.end() - 1
                call_end = _find_balanced_call(masked, open_idx)
                if _is_guard_only_call(text, m.start(), call_end):
                    continue
                inner = text[open_idx + 1 : call_end - 1]
                masked_inner = masked[open_idx + 1 : call_end - 1]
                if not inner.strip():
                    errors.append(f"{spec_path}: {helper_name}() called with no arguments")
                    continue
                args = _split_top_level_args(inner, masked_inner)
                if len(args) < 3:
                    errors.append(f"{spec_path}: malformed {helper_name}() call (too few args)")
                    continue
                screenshot_name, name_errs = _parse_screenshot_name(args[1])
                for e in name_errs:
                    errors.append(f"{spec_path}::{helper_name}: {e}")
                registry_m = re.search(r"""['"]([^'"]+)['"]""", args[2])
                if screenshot_name is None or not registry_m:
                    if not name_errs:
                        errors.append(f"{spec_path}: could not parse registryId in {helper_name}() call")
                    continue
                registry_id = registry_m.group(1)
                maturity = registry_maturity.get(registry_id)
                if maturity == "pending-baseline":
                    # Fails closed at runtime (visual-utils.ts) — never active.
                    continue
                options_blob = args[3] if len(args) > 3 else ""
                comparator_kind, comparator_value, cmp_errors = _parse_comparator(options_blob)
                for e in cmp_errors:
                    errors.append(f"{spec_path}::{screenshot_name}: {e}")
                path_template_override = _parse_path_template_override(options_blob)
                captures.append(
                    {
                        "capture_id": f"{spec_filename}::{screenshot_name}",
                        "spec_file": f"tests/e2e/{spec_filename}",
                        "screenshot_name": screenshot_name,
                        "registry_id": registry_id,
                        "maturity": maturity,
                        "directory": resolve_capture_directory(
                            test_dir, path_template_override or template, spec_filename
                        ),
                        "comparator_kind": comparator_kind,
                        "comparator_value": comparator_value,
                        # Both helpers apply the shared freeze CSS via stylePath.
                        "style_path": True,
                        **common_fields,
                    }
                )

        # 2. Direct `.toHaveScreenshot(` call sites (not routed through a helper).
        for m in re.finditer(r"\.toHaveScreenshot\(", masked):
            open_idx = m.end() - 1
            call_end = _find_balanced_call(masked, open_idx)
            if _is_guard_only_call(text, m.start(), call_end):
                continue
            inner = text[open_idx + 1 : call_end - 1]
            masked_inner = masked[open_idx + 1 : call_end - 1]
            if not inner.strip():
                errors.append(
                    f"{spec_path}: toHaveScreenshot() called with no arguments (implicit "
                    "name) is not supported by the static active-capture validator; pass an "
                    "explicit name argument"
                )
                continue
            args = _split_top_level_args(inner, masked_inner)
            if not args:
                errors.append(f"{spec_path}: malformed toHaveScreenshot() call (no args)")
                continue
            first_arg = args[0].strip()
            if first_arg.startswith("{"):
                # Options-only call (implicit, test-title-derived name) — a
                # real, executed Playwright call this validator cannot
                # statically resolve to a capture_id. Hard fail (not a
                # silent skip, PR #1813 review fix, P2 Blocker 5) so a real
                # active capture is never invisibly excluded from the
                # contract.
                errors.append(
                    f"{spec_path}: options-only toHaveScreenshot({{...}}) (implicit name) is "
                    "not supported by the static active-capture validator; pass an explicit "
                    "name argument"
                )
                continue
            screenshot_name, name_errs = _parse_screenshot_name(first_arg)
            if screenshot_name is None:
                for e in name_errs:
                    errors.append(f"{spec_path}: {e}")
                continue
            options_blob = args[1] if len(args) > 1 else ""
            comparator_kind, comparator_value, cmp_errors = _parse_comparator(options_blob)
            for e in cmp_errors:
                errors.append(f"{spec_path}::{screenshot_name}: {e}")
            path_template_override = _parse_path_template_override(options_blob)
            registry_id = registry_filename_to_id.get(screenshot_name)
            captures.append(
                {
                    "capture_id": f"{spec_filename}::{screenshot_name}",
                    "spec_file": f"tests/e2e/{spec_filename}",
                    "screenshot_name": screenshot_name,
                    "registry_id": registry_id,
                    "maturity": registry_maturity.get(registry_id) if registry_id else None,
                    "directory": resolve_capture_directory(test_dir, path_template_override or template, spec_filename),
                    "comparator_kind": comparator_kind,
                    "comparator_value": comparator_value,
                    "style_path": "stylePath" in options_blob,
                    **common_fields,
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


# Every declared/derived capture field the workflow's CAPTURES literal
# claims, that this validator can independently re-derive and therefore MUST
# cross-validate (PR #1813 review fix, P1 Blocker 1). `directory`,
# `comparator_kind`, `comparator_value`, and `style_path` keep their own
# dedicated checks below (pre-existing, more specific wording); this table
# covers every other contract field that was previously declared but never
# verified.
_EXACT_MATCH_FIELDS = (
    "spec_file",
    "screenshot_name",
    "registry_id",
    "browser",
    "project",
    "viewport",
    "device_scale_factor",
    "artifact_scope",
    "digest_env",
    "retention_days",
)


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
                f"{capture_id}: comparator_kind must be exactly one of maxDiffPixels/maxDiffPixelRatio, got {kind!r}"
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

        # Every other contract field this validator can independently
        # re-derive (PR #1813 review fix, P1 Blocker 1).
        for field in _EXACT_MATCH_FIELDS:
            declared_value = declared_capture.get(field)
            derived_value = derived_capture.get(field)
            if declared_value != derived_value:
                failures.append(
                    f"{capture_id}: declared {field} {declared_value!r} does not match derived "
                    f"{field} {derived_value!r}"
                )

    return failures


# ---------------------------------------------------------------------------
# Vitest Browser Mode component VRT support (AC10, Issue #1389)
#
# Parallel, additive audit path for `tests/component/**/*.vrt.test.ts`
# (Vitest Browser Mode `toMatchScreenshot()`), independent of the
# Playwright-only `jobs.e2e` ACTIVE_VRT_CAPTURES declared/derived
# cross-validation above (which this Issue does not touch). The
# `component-vrt-report` CI job is non-required/report-only (Issue #1389 In
# Scope), so there is no declared-in-workflow CAPTURES literal to
# cross-validate against here -- this function makes the derived captures
# themselves the audit target: every `.toMatchScreenshot(` call site must
# resolve to a real comparator and the reserved Vitest snapshot root.
# ---------------------------------------------------------------------------


def extract_derived_vitest_component_captures(component_dir: Path) -> tuple[list[dict], list[str]]:
    """Statically re-derive active Vitest Browser Mode component VRT captures
    from `tests/component/**/*.vrt.test.ts`. Mirrors the direct
    `.toHaveScreenshot(` branch of `extract_derived_active_captures` above
    (unmodified by this Issue), but for `.toMatchScreenshot(` call sites."""
    captures: list[dict] = []
    errors: list[str] = []
    if not component_dir.is_dir():
        return captures, errors

    for spec_path in sorted(component_dir.rglob(VITEST_COMPONENT_TEST_GLOB)):
        if not spec_path.is_file():
            continue
        text = spec_path.read_text(encoding="utf-8")
        masked = _mask_ts_noise(text)
        spec_filename = spec_path.name

        for m in re.finditer(r"\.toMatchScreenshot\(", masked):
            open_idx = m.end() - 1
            call_end = _find_balanced_call(masked, open_idx)
            if _is_guard_only_call(text, m.start(), call_end):
                continue
            inner = text[open_idx + 1 : call_end - 1]
            masked_inner = masked[open_idx + 1 : call_end - 1]
            if not inner.strip():
                errors.append(
                    f"{spec_path}: toMatchScreenshot() called with no arguments (implicit "
                    "name) is not supported by the static active-capture validator; pass an "
                    "explicit name argument"
                )
                continue
            args = _split_top_level_args(inner, masked_inner)
            if not args:
                errors.append(f"{spec_path}: malformed toMatchScreenshot() call (no args)")
                continue
            first_arg = args[0].strip()
            if first_arg.startswith("{"):
                errors.append(
                    f"{spec_path}: options-only toMatchScreenshot({{...}}) (implicit name) is "
                    "not supported by the static active-capture validator; pass an explicit "
                    "name argument"
                )
                continue
            screenshot_name, name_errs = _parse_screenshot_name(first_arg)
            if screenshot_name is None:
                for e in name_errs:
                    errors.append(f"{spec_path}: {e}")
                continue
            options_blob = args[1] if len(args) > 1 else ""
            comparator_kind, comparator_value, cmp_errors = _parse_vitest_comparator(options_blob)
            for e in cmp_errors:
                errors.append(f"{spec_path}::{screenshot_name}: {e}")
            captures.append(
                {
                    "capture_id": f"{spec_filename}::{screenshot_name}",
                    "spec_file": f"tests/component/{spec_filename}",
                    "screenshot_name": screenshot_name,
                    "directory": f"{VITEST_COMPONENT_SNAPSHOT_ROOT_PREFIX}{spec_filename}/",
                    "comparator_kind": comparator_kind,
                    "comparator_value": comparator_value,
                }
            )

    return captures, errors


def validate_vitest_component_captures(captures: list[dict]) -> list[str]:
    """Hard-fail (not presence-only) checks for each derived Vitest component
    VRT capture: directory must be under the reserved Vitest component
    snapshot root (never the Playwright root), and a recognised comparator
    must be declared."""
    failures: list[str] = []
    for capture in captures:
        capture_id = capture["capture_id"]
        directory = capture.get("directory", "")
        if not directory.startswith(VITEST_COMPONENT_SNAPSHOT_ROOT_PREFIX):
            failures.append(
                f"{capture_id}: directory '{directory}' is outside the reserved Vitest "
                f"component snapshot root '{VITEST_COMPONENT_SNAPSHOT_ROOT_PREFIX}'"
            )
        kind = capture.get("comparator_kind")
        if kind not in ("allowedMismatchedPixels", "allowedMismatchedPixelRatio", "threshold"):
            failures.append(
                f"{capture_id}: comparator_kind must be one of allowedMismatchedPixels/"
                f"allowedMismatchedPixelRatio/threshold, got {kind!r}"
            )
        if capture.get("comparator_value") in (None, ""):
            failures.append(f"{capture_id}: comparator_value is missing")
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
    e2e_dir = Path(argv[3]) if len(argv) > 3 else Path(DEFAULT_E2E_DIR)
    visual_utils = Path(argv[4]) if len(argv) > 4 else Path(DEFAULT_VISUAL_UTILS)
    registry_doc = Path(argv[5]) if len(argv) > 5 else Path(DEFAULT_REGISTRY_DOC)
    component_dir = Path(argv[6]) if len(argv) > 6 else Path(DEFAULT_COMPONENT_DIR)

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
                f"upload step for {required_path} uses '{uses}' not in allowed pin {sorted(ALLOWED_UPLOAD_USES)}",
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
                f"upload step for {required_path} if-no-files-found='{inff}' must be '{REQUIRED_IF_NO_FILES_FOUND}'",
            )

        retention = with_block.get("retention-days")
        try:
            rv = int(retention)
        except (TypeError, ValueError):
            rv = None
        if rv != REQUIRED_RETENTION_DAYS:
            _fail(
                failures,
                f"upload step for {required_path} retention-days={retention!r} must be {REQUIRED_RETENTION_DAYS}",
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

        # Artifact absence-state classification (AC7).
        failures.extend(check_artifact_status_wiring(summary_blob))

        # Active capture (not suite) cross-validation (AC1-AC5, AC9).
        pw_config_text = pw_config.read_text(encoding="utf-8") if pw_config.is_file() else ""
        visual_utils_text = visual_utils.read_text(encoding="utf-8") if visual_utils.is_file() else ""
        registry_doc_text = registry_doc.read_text(encoding="utf-8") if registry_doc.is_file() else ""
        registry_maturity = parse_registry_maturity(visual_utils_text)
        registry_filename_to_id = parse_registry_filename_to_id(registry_doc_text)
        pw_snapshot_config = parse_playwright_snapshot_config(pw_config_text)

        declared_captures, declared_errors = extract_declared_active_captures(workflow_text)
        for e in declared_errors:
            _fail(failures, f"active capture (declared): {e}")

        derived_captures, derived_errors = extract_derived_active_captures(
            e2e_dir, pw_snapshot_config, registry_maturity, registry_filename_to_id
        )
        for e in derived_errors:
            _fail(failures, f"active capture (derived): {e}")

        if not declared_errors and not derived_errors:
            for f in cross_validate_active_captures(declared_captures, derived_captures, registry_maturity):
                _fail(failures, f"active capture: {f}")

    # Vitest Browser Mode component VRT audit (AC10, Issue #1389) — additive,
    # independent of the jobs.e2e-only checks above.
    vitest_captures, vitest_errors = extract_derived_vitest_component_captures(component_dir)
    for e in vitest_errors:
        _fail(failures, f"component vrt capture (derived): {e}")
    if not vitest_errors:
        for f in validate_vitest_component_captures(vitest_captures):
            _fail(failures, f"component vrt capture: {f}")

    return _emit(path, upload_steps, upload_ids, summary_ok, failures)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
