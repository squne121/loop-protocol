#!/usr/bin/env python3
"""
SSOT discovery matcher.
Reads docs/dev/ssot-registry.md and returns SSOT_DISCOVERY_RESULT_V1 YAML.
"""
import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import yaml


SELECTOR_VERSION = "ssot-section-selector/v1"
DEFAULT_SECTION_CHAR_BUDGET = 4_000


def _is_fence(line: str) -> bool:
    """Return whether a line opens or closes a supported fenced code block."""
    return bool(re.match(r"^ {0,3}(`{3,}|~{3,})", line))


def parse_markdown_sections(text: str) -> list[dict]:
    """Parse ATX and Setext headings, excluding headings inside fenced code.

    The returned end_line_exclusive is one-indexed and points at the next
    heading of the same or higher level, or one past the final document line.
    """
    lines = text.splitlines(keepends=True)
    headings = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_fence(line):
            in_fence = not in_fence
            index += 1
            continue
        if not in_fence:
            atx = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", line.rstrip("\r\n"))
            if atx:
                headings.append({"heading": atx.group(2).strip(), "heading_level": len(atx.group(1)), "start_line": index + 1})
                index += 1
                continue
            if index + 1 < len(lines):
                setext = re.match(r"^ {0,3}(=+|-+)[ \t]*$", lines[index + 1].rstrip("\r\n"))
                if setext and line.strip():
                    headings.append({"heading": line.strip(), "heading_level": 1 if setext.group(1)[0] == "=" else 2, "start_line": index + 1})
                    index += 2
                    continue
        index += 1

    for position, section in enumerate(headings):
        section["end_line_exclusive"] = len(lines) + 1
        for following in headings[position + 1:]:
            if following["heading_level"] <= section["heading_level"]:
                section["end_line_exclusive"] = following["start_line"]
                break
    return headings


def _git_value(repo_root: Path, args: list[str]) -> str | None:
    result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _github_repo_slug(repo_root: Path) -> str | None:
    remote = _git_value(repo_root, ["remote", "get-url", "origin"])
    if not remote:
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
    return match.group(1) if match else None


def select_section_matches(repo_root: Path, matched: list[dict], keywords: list[str], char_budget: int) -> tuple[list[dict], list[dict]]:
    """Return bounded evidence for headings that directly match an input keyword."""
    source_commit = _git_value(repo_root, ["rev-parse", "HEAD"])
    repo_slug = _github_repo_slug(repo_root)
    matches, outcomes = [], []
    normalized_keywords = [keyword.casefold() for keyword in keywords if keyword.strip()]
    for document in matched:
        path = document["path"]
        file_path = repo_root / path
        if not file_path.exists():
            outcomes.append({"path": path, "reason_code": "document_not_found"})
            continue
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        headings = parse_markdown_sections(text)
        selected = [section for section in headings if any(keyword in section["heading"].casefold() for keyword in normalized_keywords)]
        if not selected:
            outcomes.append({"path": path, "reason_code": "section_not_found"})
            continue
        blob_sha = _git_value(repo_root, ["rev-parse", f"HEAD:{path}"])
        lines = text.splitlines(keepends=True)
        for section in selected:
            content = "".join(lines[section["start_line"] - 1:section["end_line_exclusive"] - 1])
            char_count = len(content)
            if char_count > char_budget:
                outcomes.append({"path": path, "heading": section["heading"], "reason_code": "section_budget_exceeded", "char_count": char_count, "char_budget": char_budget})
                continue
            permalink = None
            if repo_slug and source_commit:
                permalink = f"https://github.com/{repo_slug}/blob/{source_commit}/{path}#L{section['start_line']}-L{section['end_line_exclusive'] - 1}"
            matches.append({
                "path": path,
                "source_commit": source_commit,
                "blob_sha": blob_sha,
                "content_sha256": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
                "heading": section["heading"],
                "heading_level": section["heading_level"],
                "start_line": section["start_line"],
                "end_line_exclusive": section["end_line_exclusive"],
                "permalink": permalink,
                "selector_version": SELECTOR_VERSION,
                "selection_reason_code": "heading_keyword_match",
                "char_count": char_count,
                "char_budget": char_budget,
            })
            outcomes.append({"path": path, "heading": section["heading"], "reason_code": "selected"})
    return matches, outcomes


def get_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return Path.cwd()


def parse_registry(registry_path: Path) -> tuple:
    """
    Parse ssot-registry.md.
    Returns (entries, directory_mappings, warnings).

    entries format:
      [{"id": ..., "path": ..., "keywords": [...], "sections": [...], ...}]

    directory_mappings format:
      [{"pattern": "src/state/**", "ssots": ["docs/..."]}]

    For backwards compatibility, the function can be called as:
      entries, directory_mappings = parse_registry(...)
    or:
      entries, directory_mappings, warnings = parse_registry(...)
    """
    text = registry_path.read_text(encoding="utf-8")

    entries = []
    directory_mappings = []
    parse_warnings = []

    # Extract YAML entries (- id: ... blocks)
    # Split by "- id:" to get individual entries
    entry_blocks = re.split(r'\n(?=- id:)', text)
    for block in entry_blocks:
        block = block.strip()
        if not block.startswith("- id:"):
            continue
        # Strip YAML document separator lines (---) and Markdown section headers
        # which cause multi-document parse errors or YAML key errors
        block = re.split(r'\n---\s*\n|\n---\s*$|\n(?=\S)', block)[0].strip()
        try:
            parsed = yaml.safe_load(block)
            if isinstance(parsed, list) and parsed:
                entry = parsed[0]
            elif isinstance(parsed, dict):
                entry = parsed
            else:
                continue
            if "id" in entry and "path" in entry:
                # Normalize keywords to list
                kws = entry.get("keywords", [])
                if isinstance(kws, str):
                    kws = [k.strip() for k in kws.split(",")]
                entry["keywords"] = kws
                entries.append(entry)
        except yaml.YAMLError as e:
            parse_warnings.append(f"YAML parse error in registry block (skipped): {e}")

    # Extract directory_mappings YAML block
    # Look for ```yaml ... ``` block under "## ディレクトリ" section
    dir_section_match = re.search(
        r'## ディレクトリ.*?SSOT.*?マッピング.*?\n(.*?)(?=\n##|\Z)',
        text, re.DOTALL
    )
    if dir_section_match:
        dir_content = dir_section_match.group(1)
        # Try to find fenced YAML block
        fenced = re.search(r'```ya?ml\n(.*?)```', dir_content, re.DOTALL)
        if fenced:
            try:
                dm_data = yaml.safe_load(fenced.group(1))
                if isinstance(dm_data, dict) and "directory_mappings" in dm_data:
                    directory_mappings = dm_data["directory_mappings"]
            except yaml.YAMLError:
                pass
        else:
            # Try bare YAML block
            try:
                dm_data = yaml.safe_load(dir_content)
                if isinstance(dm_data, dict) and "directory_mappings" in dm_data:
                    directory_mappings = dm_data["directory_mappings"]
            except yaml.YAMLError:
                pass

    return entries, directory_mappings, parse_warnings


def match_keywords(keywords, entries, docs_dir):
    """Match keywords against entries and docs files. Returns (matched, unmatched_keywords)."""
    matched = {}  # path -> {"path", "relevance", "reason", "sections"}
    unmatched = []

    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        kw_matched = False

        # Check registry entries first
        for entry in entries:
            path = entry.get("path", "")
            entry_kws = [k.lower() for k in entry.get("keywords", [])]
            title = entry.get("title", "").lower()
            desc = entry.get("description", "").lower()
            kw_lower = kw.lower()

            if kw_lower in entry_kws or kw_lower in title:
                relevance = "high"
                reason = f"registry keyword/title match for '{kw}'"
            elif kw_lower in desc:
                relevance = "medium"
                reason = f"registry description match for '{kw}'"
            else:
                # Check actual file content
                file_path = docs_dir.parent / path
                if not file_path.exists():
                    continue
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                lines = content.split("\n")
                heading_match = any(
                    re.search(rf'(?i)^#+\s+.*{re.escape(kw)}', line)
                    for line in lines
                )
                if heading_match:
                    relevance = "high"
                    reason = f"heading match for '{kw}'"
                elif kw_lower in content.lower():
                    relevance = "medium"
                    reason = f"body match for '{kw}'"
                else:
                    continue

            kw_matched = True
            existing = matched.get(path)
            if existing is None or _relevance_rank(relevance) < _relevance_rank(existing["relevance"]):
                matched[path] = {
                    "path": path,
                    "relevance": relevance,
                    "reason": reason,
                    "sections": entry.get("sections", []),
                }

        if not kw_matched:
            unmatched.append(kw)

    return list(matched.values()), unmatched


def match_paths(paths, directory_mappings, entries):
    """Match target paths against directory mappings. Returns (matched, unmatched_paths)."""
    matched = {}  # path -> doc
    unmatched_paths = []

    for p in paths:
        p = p.strip()
        if not p:
            continue
        p_matched = False

        for dm in directory_mappings:
            pattern = dm.get("pattern", "")
            # Directory prefix match: strip trailing /** and require path separator
            # to avoid matching sibling directories (e.g. src/state/** must not match src/stateful/)
            prefix = pattern.removesuffix("/**").rstrip("/")
            if p.startswith(prefix + "/") or p.rstrip("/") == prefix:
                ssots = dm.get("ssots", [])
                for ssot_path in ssots:
                    if ssot_path not in matched:
                        # Find sections from entries
                        entry = next((e for e in entries if e.get("path") == ssot_path), {})
                        matched[ssot_path] = {
                            "path": ssot_path,
                            "relevance": "low",
                            "reason": f"directory mapping from {pattern}",
                            "sections": entry.get("sections", []),
                }
                p_matched = True

        if not p_matched:
            unmatched_paths.append(p)

    return list(matched.values()), unmatched_paths


def _relevance_rank(r):
    return {"high": 0, "medium": 1, "low": 2}.get(r, 3)


def merge_results(kw_matched, path_matched):
    """Merge keyword and path matches, keeping highest relevance per path."""
    merged = {}
    for doc in kw_matched + path_matched:
        path = doc["path"]
        existing = merged.get(path)
        if existing is None or _relevance_rank(doc["relevance"]) < _relevance_rank(existing["relevance"]):
            merged[path] = doc

    # Sort by relevance
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(merged.values(), key=lambda d: order.get(d["relevance"], 3))


def emit_result(
    keywords,
    paths,
    matched,
    unmatched_keywords,
    unmatched_paths,
    errors=None,
    warnings=None,
    section_limited_matches=None,
    section_selection_outcomes=None,
):
    errors = errors or []
    warnings = warnings or []

    if errors:
        status = "failed"
    elif unmatched_keywords or unmatched_paths:
        status = "partial"
    else:
        status = "ok"

    result = {
        "SSOT_DISCOVERY_RESULT_V1": {
            "status": status,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated_by": "ssot-discovery",
            "inputs": {
                "task_keywords": keywords,
                "target_paths": paths,
            },
            "matched_documents": [
                {
                    "path": d["path"],
                    "relevance": d["relevance"],
                    "reason": d["reason"],
                    "sections": d.get("sections", []) or [],
                }
                for d in matched
            ],
            # Optional v1 extension. Consumers that do not recognize this key
            # continue to use matched_documents unchanged.
            "section_limited_matches": section_limited_matches or [],
            "section_selection_outcomes": section_selection_outcomes or [],
            "unmatched_keywords": unmatched_keywords,
            "unmatched_paths": unmatched_paths,
            "notes": ["SSOT registry: docs/dev/ssot-registry.md"],
            "warnings": warnings,
            "errors": errors,
        }
    }
    return yaml.safe_dump(result, allow_unicode=True, sort_keys=False, default_flow_style=False)


def main():
    parser = argparse.ArgumentParser(description="SSOT discovery matcher")
    parser.add_argument("--keywords", default="", help="Comma-separated keywords")
    parser.add_argument("--paths", default="", help="Comma-separated target paths")
    parser.add_argument("--section-char-budget", type=int, default=DEFAULT_SECTION_CHAR_BUDGET, help="Maximum characters per selected section")
    args = parser.parse_args()

    if args.section_char_budget <= 0:
        parser.error("--section-char-budget must be greater than zero")

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else []
    paths = [p.strip() for p in args.paths.split(",") if p.strip()] if args.paths else []

    repo_root = get_repo_root()
    docs_dir = repo_root / "docs"
    registry_path = repo_root / "docs" / "dev" / "ssot-registry.md"

    if not docs_dir.exists():
        print(emit_result(keywords, paths, [], keywords, paths, errors=[f"docs/ not found at {docs_dir}"]), end="")
        sys.exit(2)

    if not registry_path.exists():
        print(
            emit_result(
                keywords,
                paths,
                [],
                keywords,
                paths,
                errors=[f"ssot-registry.md not found at {registry_path}"]
            ),
            end=""
        )
        sys.exit(2)

    try:
        entries, directory_mappings, parse_warnings = parse_registry(registry_path)
    except Exception as e:
        print(emit_result(keywords, paths, [], keywords, paths, errors=[f"Failed to parse registry: {e}"]), end="")
        sys.exit(2)

    kw_matched, unmatched_keywords = match_keywords(keywords, entries, docs_dir) if keywords else ([], [])
    path_matched, unmatched_paths = match_paths(paths, directory_mappings, entries) if paths else ([], [])

    matched = merge_results(kw_matched, path_matched)

    section_limited_matches, section_selection_outcomes = select_section_matches(
        repo_root, matched, keywords, args.section_char_budget
    )
    print(emit_result(
        keywords, paths, matched, unmatched_keywords, unmatched_paths,
        warnings=parse_warnings,
        section_limited_matches=section_limited_matches,
        section_selection_outcomes=section_selection_outcomes,
    ), end="")


if __name__ == "__main__":
    main()
