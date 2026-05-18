#!/usr/bin/env python3
"""Run Gemini CLI through a strict headless delegation contract."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml as _yaml_module
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

DEFAULT_MODEL = "gemini-3-flash-preview"
DEFAULT_TIMEOUT_SEC = 600
RETRY_LIMIT = 2

# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

DEFAULT_MODEL_ROUTING: dict[str, Any] = {
    "default_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"],
    "roles": {
        "code_research": {"model_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"]},
        "web_research": {"model_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"]},
        "github_research": {"model_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"]},
        "implementation": {"model_chain": ["gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash"]},
        "issue_authoring": {"model_chain": ["gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash"]},
    },
}

_DEFAULT_MODEL_ROUTING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "model_routing.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *override* into *base* (non-destructive copy)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_model_routing(config_path: Path | None = None) -> dict[str, Any]:
    """Load model routing configuration, merging optional YAML override into defaults.

    Args:
        config_path: Path to YAML override file. Defaults to
            ``config/model_routing.yaml`` next to this script.
            Pass an explicit path in tests for hermetic injection.

    Returns:
        Merged routing config dict with ``default_chain`` and ``roles`` keys.

    Raises:
        ValueError: If config file has invalid YAML, invalid structure,
            or produces an empty chain.
    """
    routing = dict(DEFAULT_MODEL_ROUTING)

    effective_path = config_path if config_path is not None else _DEFAULT_MODEL_ROUTING_CONFIG_PATH
    if effective_path.exists():
        if not _YAML_AVAILABLE:
            warnings.warn(
                f"PyYAML is not installed; ignoring model_routing config file {effective_path} "
                "and using DEFAULT_MODEL_ROUTING. Install pyyaml to enable YAML override.",
                RuntimeWarning,
                stacklevel=2,
            )
            return routing
        try:
            raw = effective_path.read_text(encoding="utf-8")
            override = _yaml_module.safe_load(raw)
        except _yaml_module.YAMLError as exc:
            raise ValueError(f"model_routing config {effective_path}: invalid YAML: {exc}") from exc

        if override is None:
            pass  # empty file → no override
        elif not isinstance(override, dict):
            raise ValueError(
                f"model_routing config {effective_path}: expected a YAML mapping, got {type(override).__name__}"
            )
        else:
            routing = _deep_merge(routing, override)

    # Validate default_chain
    default_chain = routing.get("default_chain")
    if not isinstance(default_chain, list) or len(default_chain) == 0:
        raise ValueError("model_routing default_chain must be a non-empty list")
    for entry in default_chain:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"model_routing default_chain contains invalid entry: {entry!r}")

    # Validate roles
    roles = routing.get("roles", {})
    if not isinstance(roles, dict):
        raise ValueError("model_routing roles must be a mapping when present")
    for role_name, role_cfg in roles.items():
        if not isinstance(role_cfg, dict):
            raise ValueError(f"model_routing roles[{role_name!r}] must be a mapping")
        chain = role_cfg.get("model_chain")
        if not isinstance(chain, list) or len(chain) == 0:
            raise ValueError(f"model_routing roles[{role_name!r}].model_chain must be a non-empty list")
        for entry in chain:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    f"model_routing roles[{role_name!r}].model_chain contains invalid entry: {entry!r}"
                )

    return routing


def resolve_model_chain(
    request: Mapping[str, Any],
    routing: dict[str, Any] | None = None,
) -> tuple[list[str], str | None]:
    """Resolve the model chain for *request*.

    Resolution order:
    1. If ``request["model"]`` is explicitly set → single-entry chain (no downgrade).
    2. If ``request["role"]`` is set and known → chain from ``roles[role]["model_chain"]``.
    3. Otherwise → ``default_chain``.

    Returns:
        (chain, error_or_none):  *error_or_none* is a non-empty string with
        ``reason_code: unknown_role`` or ``reason_code: empty_chain`` if the
        chain cannot be resolved safely, in which case *chain* is ``[]``.
    """
    if routing is None:
        routing = load_model_routing()

    explicit_model = request.get("model")
    if isinstance(explicit_model, str) and explicit_model.strip():
        return [explicit_model.strip()], None

    role = request.get("role")
    if role is not None:
        roles = routing.get("roles", {})
        if role not in roles:
            return [], f"unknown_role: {role!r} is not defined in model_routing; valid roles: {sorted(roles)}"
        chain = roles[role].get("model_chain", [])
        if not chain:
            return [], f"empty_chain: roles[{role!r}].model_chain is empty"
        return list(chain), None

    default_chain = routing.get("default_chain", [])
    if not default_chain:
        return [], "empty_chain: default_chain is empty"
    return list(default_chain), None
ALLOWED_TOOL_PROFILES = {"no_tools", "grounded_research", "local_asset_research", "proposal_only", "github_research"}
LOCAL_ASSET_RESEARCH_PROFILE = "local_asset_research"
GROUNDED_RESEARCH_PROFILE = "grounded_research"
PROPOSAL_ONLY_PROFILE = "proposal_only"
GITHUB_RESEARCH_PROFILE = "github_research"

# github_research: allowed gh subcommand argv prefixes (first two tokens of argv)
GITHUB_RESEARCH_ALLOWED_ARGV_PREFIXES: frozenset[tuple[str, ...]] = frozenset({
    ("issue", "list"),
    ("issue", "view"),
    ("pr", "list"),
    ("pr", "view"),
    ("pr", "diff"),
    ("search", "issues"),
    ("search", "prs"),
    ("label", "list"),
    ("repo", "view"),
    ("api",),  # GET only — validated per argv
})
# github_research: denied gh subcommand patterns (text-level secondary defense)
GITHUB_RESEARCH_DENIED_SUBCOMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bgh\s+issue\s+(?:comment|edit|create|close|reopen|delete|lock|unlock|transfer|pin|unpin)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bgh\s+pr\s+(?:create|edit|comment|merge|close|reopen|review|ready|checkout)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bgh\s+label\s+(?:create|edit|delete|clone)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bgh\s+release\s+(?:create|edit|delete|upload)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bgh\s+repo\s+(?:create|edit|delete|fork|clone|sync|archive|rename)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgh\s+secret\b", re.IGNORECASE),
    re.compile(r"\bgh\s+variable\b", re.IGNORECASE),
    re.compile(r"\bgh\s+workflow\s+run\b", re.IGNORECASE),
    re.compile(r"\bgh\s+run\s+cancel\b", re.IGNORECASE),
    re.compile(
        r"\bgh\s+api\b.{0,100}(?:-X[\s=]+(?:POST|PATCH|PUT|DELETE)|--method[\s=]+(?:POST|PATCH|PUT|DELETE))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgh\s+auth\s+(?:login|logout)\b", re.IGNORECASE),
)
# github_research: allowed text patterns (at least one must appear)
GITHUB_RESEARCH_ALLOWED_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh\s+issue\s+(?:list|view)\b", re.IGNORECASE),
    re.compile(r"\bgh\s+pr\s+(?:list|view|diff)\b", re.IGNORECASE),
    re.compile(r"\bgh\s+search\s+(?:issues|prs)\b", re.IGNORECASE),
    re.compile(r"\bgh\s+label\s+list\b", re.IGNORECASE),
    re.compile(r"\bgh\s+repo\s+view\b", re.IGNORECASE),
    re.compile(r"\bgh\s+api\b", re.IGNORECASE),
)
PROPOSAL_ONLY_ALLOWED_OUTPUTS = (
    "implementation_draft",
    "issue_authoring_draft",
    "patch_proposal",
    "command_plan",
)
PROPOSAL_ONLY_FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:\b(?:write|edit|modify|rewrite|delete|remove|create|update)\b.{0,30}\b(?:file|files|repo|repository|source|code)\b)"
            r"|(?:\b(?:write|edit|modify|rewrite|delete|remove|create|update)\b.{0,30}(?:[/\w.-]+\.(?:py|md|json|toml|ya?ml|txt)))"
            r"|(?:\bfile\s+(?:write|edit)\b)"
            r"|(?:ファイル|コード|リポジトリ).{0,12}(?:を書き換|を編集|を変更|を削除|を追加|を作成)"
            r"|(?:[/\w.-]+\.(?:py|md|json|toml|ya?ml|txt)).{0,20}(?:を編集|を書き換|を変更|を削除|を追加)"
            r"|(?:apply[_ -]?patch)",
            re.IGNORECASE,
        ),
        "proposal_only forbids direct file write/edit requests",
    ),
    (
        re.compile(
            r"(?:\b(?:run|execute|invoke)\b.{0,30}\b(?:shell|command|commands|bash|sh|python|pytest|just)\b)"
            r"|(?:(?:shell|command|commands).{0,12}\b(?:run|execute|invoke)\b)"
            r"|(?:\b(?:bash|sh|python|pytest|just|git|gh)\b.{0,20}(?:run|execute|実行|実施))"
            r"|(?:コマンド|シェル).{0,12}(?:を実行|を実施)"
            r"|(?:実行|実施).{0,12}(?:コマンド|シェル)",
            re.IGNORECASE,
        ),
        "proposal_only forbids shell execution requests",
    ),
    (
        re.compile(
            r"(?:\bgh\s+(?:issue|pr)\s+(?:comment|edit|create|review)\b)"
            r"|(?:\bgit\s+(?:commit|push|merge)\b)"
            r"|(?:\b(?:commit|push|merge)\b.{0,20}\b(?:result|results|change|changes|branch|pr|pull request)\b)"
            r"|(?:post_to_issue_url)"
            r"|(?:GitHub.{0,12}(?:write|comment|mutation|post|edit))"
            r"|(?:GitHub.{0,12}(?:書き込み|更新|投稿))",
            re.IGNORECASE,
        ),
        "proposal_only forbids GitHub mutation requests",
    ),
)
PROPOSAL_ONLY_CLAUSE_SPLIT_PATTERN = re.compile(
    r"(?:"
    r";"
    r"|\n"
    r"|。"
    r"|！"
    r"|？"
    r"|(?<=[.!?])\s+(?=(?:[A-Z]|[Ii]nstead\b|[Tt]hen\b|[Nn]ext\b))"
    r"|,\s+(?=(?:instead|then|next)\b)"
    r")+"
)
SERENA_MCP_SERVER_NAME = "serena"
SERENA_READ_ONLY_TOOLS = frozenset({
    "find_file",
    "find_referencing_symbols",
    "find_symbol",
    "get_symbols_overview",
    "list_dir",
    "search_for_pattern",
})
SERENA_DANGEROUS_TOOLS = frozenset({
    "activate_project",
    "create_text_file",
    "delete_lines",
    "execute_shell_command",
    "insert_after_symbol",
    "insert_at_line",
    "insert_before_symbol",
    "prepare_for_new_conversation",
    "read_file_content",
    "read_memory",
    "remove_project",
    "replace_lines",
    "replace_regex",
    "replace_symbol_body",
    "restart_language_server",
    "switch_modes",
    "think_about_collected_information",
    "think_about_task_adherence",
    "think_about_whether_you_are_done",
    "write_file",
    "write_memory",
})
VAGUE_OBJECTIVE_PHRASES = {
    "analyze",
    "check",
    "debug",
    "deep dive",
    "evaluate",
    "examine",
    "explore",
    "find out",
    "help",
    "investigate",
    "look into",
    "research",
    "review",
    "something",
    "stuff",
    "task",
    "test",
    "todo",
    "work",
}
_PATH_PATTERN = re.compile(
    r'[/\\]'
    r'|\.(?:py|log|md|txt|json|yaml|yml|toml|cfg|ini|sh|bat|ps1)\b'
    r'|:\d+'
)
MODEL_CAPACITY_PATTERNS = (
    "MODEL_CAPACITY_EXHAUSTED",
    "RESOURCE_EXHAUSTED",
)
# Matches HTTP 429 in context (e.g. "HTTP 429", "status: 429", "code: 429", "error: 429").
# Plain "429" substring is intentionally excluded to avoid false positives on "4290 tokens" etc.
_HTTP_429_RE = re.compile(
    r"(?:HTTP\s+|status[:\s]+|code[:\s]+|error[:\s]+)429\b",
    re.IGNORECASE,
)
SUMMARY_HEADING_PATTERNS = (
    re.compile(r"^\s{0,3}#{1,6}\s*summary\s*$", re.IGNORECASE),
    re.compile(r"^\s{0,3}#{1,6}\s*(?:要約|概要)\s*$"),
    re.compile(r"^\s*[-*]\s*summary\s*$", re.IGNORECASE),
    re.compile(r"^\s*[-*]\s*(?:要約|概要)\s*$"),
    re.compile(r"^\s*summary\s*:?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:要約|概要)\s*:?\s*$"),
)


class RequestValidationError(ValueError):
    """Raised when delegation_request_v1 fails validation."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")


def _append_ndjson(path: Path, payload: Mapping[str, Any]) -> None:
    """Append a single JSON object as one line to an NDJSON file (newline-delimited JSON)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=False))
        handle.write("\n")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _is_vague_objective(objective: str) -> bool:
    normalized = _normalize_text(objective)
    if not normalized:
        return True
    if normalized in VAGUE_OBJECTIVE_PHRASES:
        return True

    # Language-independent specificity: path separator, file extension, or line number.
    # Search normalized (lowercase) so uppercase extensions like ".LOG" are also matched.
    if _PATH_PATTERN.search(normalized):
        return False

    tokens = normalized.split()
    if len(tokens) < 2:
        # Multi-character objective (e.g. Japanese) with sufficient length is not vague.
        # Threshold of 10: roughly 2-3 Japanese words, well above single-verb noise.
        if len(normalized) >= 10:
            return False
        return True

    vague_tokens = {
        "analyze",
        "check",
        "debug",
        "deep",
        "dive",
        "evaluate",
        "examine",
        "explore",
        "find",
        "help",
        "investigate",
        "look",
        "research",
        "review",
        "something",
        "stuff",
        "task",
        "test",
        "work",
    }
    if all(token in vague_tokens for token in tokens):
        return True

    concrete_markers = ("/", ".", "-", ":", "_")
    if not any(any(marker in token for marker in concrete_markers) for token in tokens):
        if len(tokens) < 3:
            return True
    return False


_CREDENTIAL_REGEX = re.compile(
    r"(?<![A-Za-z0-9_])("
    r"gh[posur]_[A-Za-z0-9]{10,}"
    r"|github_pat_[A-Za-z0-9_]{10,}"
    r"|sk-[A-Za-z0-9]{10,}"
    r"|sk_(?:live|test)_[A-Za-z0-9]{10,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{16,}"
    r"|xox[bpars]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r")"
)


def _contains_credential(s: str) -> bool:
    return bool(_CREDENTIAL_REGEX.search(s))


def _truncate_repr(value: Any, max_length: int = 200) -> str:
    def _scan(v: Any) -> bool:
        if isinstance(v, str):
            return _contains_credential(v)
        if isinstance(v, (list, tuple)):
            return any(_scan(x) for x in v)
        if isinstance(v, dict):
            return any(_scan(k) or _scan(x) for k, x in v.items())
        return False

    if _scan(value):
        return f"<redacted: type={type(value).__name__} length={len(repr(value))}>"
    r = repr(value)
    if len(r) > max_length:
        return r[:max_length] + "...<truncated>"
    return r


def _validate_string_list(name: str, value: Any, minimum_length: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list):
        return [f"{name} must be a list (received: {_truncate_repr(value)})"]
    if len(value) < minimum_length:
        return [f"{name} must contain at least {minimum_length} item(s) (received: {_truncate_repr(value)})"]
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{name}[{index}] must be a non-empty string (received: {_truncate_repr(item)})")
    return errors


def _validate_proposal_only_request(request: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if request.get("post_to_issue_url"):
        errors.append("proposal_only forbids post_to_issue_url")

    text_fragments: list[str] = []
    for key in ("objective", "inline_context"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            text_fragments.append(value)
    instructions = request.get("instructions")
    if isinstance(instructions, list):
        text_fragments.extend(item for item in instructions if isinstance(item, str) and item.strip())

    for fragment in text_fragments:
        clauses = [clause.strip() for clause in PROPOSAL_ONLY_CLAUSE_SPLIT_PATTERN.split(fragment) if clause.strip()]
        for clause in clauses:
            for pattern, message in PROPOSAL_ONLY_FORBIDDEN_PATTERNS:
                if pattern.search(clause):
                    errors.append(message)
    return errors


def _validate_proposal_only_output_sections(output_sections: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(output_sections, list):
        return errors
    allowed = set(PROPOSAL_ONLY_ALLOWED_OUTPUTS)
    invalid_sections = [
        section
        for section in output_sections
        if isinstance(section, str) and section.strip() and section not in allowed
    ]
    if invalid_sections:
        errors.append(
            "proposal_only output_sections must be drawn from: "
            + ", ".join(PROPOSAL_ONLY_ALLOWED_OUTPUTS)
            + f" (got: {', '.join(invalid_sections)})"
        )
    return errors


def _extract_method_value(token: str, next_token: str | None) -> str | None:
    """Extract the HTTP method value from a gh api argv token pair.

    Handles both space-separated (``--method POST``, ``-X POST``) and
    equals-separated (``--method=POST``, ``-X=POST``) forms.
    Returns the method string (e.g. ``"POST"``) or ``None`` if the token is
    not a method flag.
    """
    if token.startswith("--method="):
        return token.split("=", 1)[1]
    if token == "--method" and next_token is not None:
        return next_token
    if token.startswith("-X="):
        return token.split("=", 1)[1]
    if token == "-X" and next_token is not None:
        return next_token
    return None


def _validate_github_research_argv(argv: list[str]) -> list[str]:
    """Validate a single gh command argv (without the leading 'gh') for github_research profile.

    Returns a list of error strings (empty means allowed).
    """
    errors: list[str] = []
    if not argv:
        errors.append("github_research gh_commands entry must have at least one argv element")
        return errors

    subcommand = argv[0].lower()
    # api endpoint: only GET allowed
    if subcommand == "api":
        # Reject gh api graphql (always uses POST)
        if len(argv) >= 2 and argv[1].lower() == "graphql":
            errors.append("github_research: gh api graphql is not allowed (always uses POST)")
            return errors

        # Reject implicit-POST flags: -f/-F/--field/--raw-field/--input imply a non-GET request
        implicit_post_flags = {"-f", "-F", "--field", "--raw-field", "--input"}
        for token in argv:
            if token in implicit_post_flags:
                errors.append(
                    f"github_research: gh api with {token} implies a non-GET request and is not allowed"
                )

        # Check for explicit non-GET method flags (both space-separated and =-separated forms)
        for i, token in enumerate(argv):
            next_token = argv[i + 1] if i + 1 < len(argv) else None
            method_value = _extract_method_value(token, next_token)
            if method_value is not None and method_value.upper() in ("POST", "PATCH", "PUT", "DELETE"):
                errors.append(
                    f"github_research: gh api with {token} {method_value.upper()} is not allowed (read-only GET only)"
                )
        return errors

    # Other subcommands: check against allowed prefix list
    if len(argv) >= 2:
        prefix = (argv[0].lower(), argv[1].lower())
    else:
        prefix = (argv[0].lower(),)

    # Match against allowed prefixes
    allowed = any(
        (len(allowed_prefix) == 1 and prefix[0] == allowed_prefix[0])
        or (len(allowed_prefix) >= 2 and len(prefix) >= 2 and prefix[0] == allowed_prefix[0] and prefix[1] == allowed_prefix[1])
        for allowed_prefix in GITHUB_RESEARCH_ALLOWED_ARGV_PREFIXES
    )
    if not allowed:
        errors.append(
            f"github_research: gh {' '.join(argv[:2])} is not in the allowed subcommand list"
        )
    return errors


def _validate_github_research_request(request: Mapping[str, Any]) -> list[str]:
    """Validate request for github_research profile.

    Two-layer defense:
    (a) argv-based validation for request.gh_commands entries (primary, strictest)
    (b) text-based scanning of objective/instructions/inline_context for denied gh subcommand patterns
    """
    errors: list[str] = []

    # Deny post_to_issue_url (write mutation)
    if request.get("post_to_issue_url"):
        errors.append("github_research forbids post_to_issue_url")

    # (a) argv-based validation for gh_commands
    gh_commands = request.get("gh_commands")
    if gh_commands is not None:
        if not isinstance(gh_commands, list):
            errors.append("github_research gh_commands must be a list when present")
        elif len(gh_commands) == 0:
            errors.append(
                "github_research_command_denied: gh_commands must not be empty when present (omit field instead)"
            )
        else:
            for idx, entry in enumerate(gh_commands):
                if not isinstance(entry, dict):
                    errors.append(f"github_research gh_commands[{idx}] must be an object with 'argv' field")
                    continue
                argv = entry.get("argv")
                if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                    errors.append(f"github_research gh_commands[{idx}].argv must be a list of strings")
                    continue
                errors.extend(_validate_github_research_argv(argv))

    # (b) text-based secondary defense: scan objective/instructions/inline_context
    text_fragments: list[str] = []
    for key in ("objective", "inline_context"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            text_fragments.append(value)
    instructions = request.get("instructions")
    if isinstance(instructions, list):
        text_fragments.extend(item for item in instructions if isinstance(item, str) and item.strip())

    full_text = " ".join(text_fragments)

    # Check for denied patterns
    for pattern in GITHUB_RESEARCH_DENIED_SUBCOMMAND_PATTERNS:
        if pattern.search(full_text):
            errors.append("github_research_command_denied")
            break

    # If no gh_commands and no allowed text pattern found in text, require at least one allowed pattern
    if gh_commands is None:
        allowed_found = any(pattern.search(full_text) for pattern in GITHUB_RESEARCH_ALLOWED_TEXT_PATTERNS)
        if not allowed_found and not errors:
            errors.append(
                "github_research requires at least one allowed gh subcommand in objective/instructions "
                "(gh issue list/view, gh pr list/view/diff, gh search issues/prs, gh label list, "
                "gh repo view, gh api)"
            )

    return errors


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _resolve_context_file(raw_path: str, base_dir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_local_asset_research_settings(repo_root: Path | None = None) -> list[str]:
    root = repo_root or _repo_root()
    settings_path = root / ".gemini" / "settings.json"
    errors: list[str] = []
    try:
        settings = _load_json(settings_path)
    except FileNotFoundError:
        return [f"local_asset_research requires {settings_path}"]
    except json.JSONDecodeError as exc:
        return [f"local_asset_research requires valid JSON in {settings_path}: {exc}"]
    if not isinstance(settings, Mapping):
        return [f"local_asset_research requires {settings_path} to contain a JSON object"]

    mcp = settings.get("mcp")
    allowed_servers = mcp.get("allowed") if isinstance(mcp, Mapping) else None
    if allowed_servers != [SERENA_MCP_SERVER_NAME]:
        errors.append("local_asset_research requires .gemini/settings.json mcp.allowed to equal ['serena']")

    servers = settings.get("mcpServers")
    if not isinstance(servers, Mapping):
        return errors + ["local_asset_research requires .gemini/settings.json mcpServers"]
    serena = servers.get(SERENA_MCP_SERVER_NAME)
    if not isinstance(serena, Mapping):
        return errors + ["local_asset_research requires .gemini/settings.json mcpServers.serena"]

    command = serena.get("command")
    args = serena.get("args")
    if command != "uvx" or not isinstance(args, list) or "serena" not in args or "--project-from-cwd" not in args:
        errors.append("local_asset_research requires WSL-local Serena MCP command: uvx ... serena ... --project-from-cwd")

    trust = serena.get("trust", False)
    if trust is not False:
        errors.append("local_asset_research requires mcpServers.serena.trust to be false")

    include_tools = serena.get("includeTools")
    if not isinstance(include_tools, list) or not include_tools:
        errors.append("local_asset_research requires mcpServers.serena.includeTools read-only allowlist")
    elif not all(isinstance(tool, str) for tool in include_tools):
        errors.append("local_asset_research requires includeTools to contain only strings")
    else:
        include_set = set(include_tools)
        unexpected = sorted(include_set - SERENA_READ_ONLY_TOOLS)
        missing = sorted(SERENA_READ_ONLY_TOOLS - include_set)
        dangerous = sorted(include_set & SERENA_DANGEROUS_TOOLS)
        if unexpected:
            errors.append(f"local_asset_research has unverified MCP tools in includeTools: {', '.join(unexpected)}")
        if missing:
            errors.append(f"local_asset_research read-only includeTools is incomplete: {', '.join(missing)}")
        if dangerous:
            errors.append(f"local_asset_research includes dangerous Serena MCP tools: {', '.join(dangerous)}")

    exclude_tools = serena.get("excludeTools", [])
    if not isinstance(exclude_tools, list):
        errors.append("local_asset_research requires excludeTools to be a list when present")
    elif not SERENA_DANGEROUS_TOOLS.issubset(set(exclude_tools)):
        missing_excludes = sorted(SERENA_DANGEROUS_TOOLS - set(exclude_tools))
        errors.append(f"local_asset_research dangerous tool denylist is incomplete: {', '.join(missing_excludes)}")

    return errors


def validate_request(request: Mapping[str, Any], request_path: Path | None = None) -> list[str]:
    errors: list[str] = []

    schema = request.get("schema")
    if schema != "delegation_request_v1":
        errors.append("schema must equal delegation_request_v1")

    objective = request.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        errors.append("objective must be a non-empty string")
    elif _is_vague_objective(objective):
        errors.append("objective is too vague")

    errors.extend(_validate_string_list("instructions", request.get("instructions"), 2))

    tool_profile = request.get("tool_profile")
    if tool_profile not in ALLOWED_TOOL_PROFILES:
        errors.append("tool_profile must be one of: no_tools, grounded_research, local_asset_research, proposal_only, github_research")
    elif tool_profile == LOCAL_ASSET_RESEARCH_PROFILE:
        if request.get("post_to_issue_url"):
            errors.append("local_asset_research forbids post_to_issue_url")
        errors.extend(_validate_local_asset_research_settings())
    elif tool_profile == PROPOSAL_ONLY_PROFILE:
        errors.extend(_validate_proposal_only_request(request))
    elif tool_profile == GITHUB_RESEARCH_PROFILE:
        errors.extend(_validate_github_research_request(request))

    errors.extend(_validate_string_list("output_sections", request.get("output_sections"), 1))
    if tool_profile == PROPOSAL_ONLY_PROFILE:
        errors.extend(_validate_proposal_only_output_sections(request.get("output_sections")))
    errors.extend(_validate_string_list("context_files", request.get("context_files"), 1))

    timeout_sec = request.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    if not isinstance(timeout_sec, int) or timeout_sec <= 0:
        errors.append("timeout_sec must be a positive integer when present")

    model = request.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        errors.append("model must be a non-empty string when present")

    if isinstance(request.get("context_files"), list):
        base_dir = request_path.parent if request_path is not None else Path.cwd()
        repo_root = _repo_root().resolve() if tool_profile == LOCAL_ASSET_RESEARCH_PROFILE else None
        for raw_path in request["context_files"]:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            candidate = _resolve_context_file(raw_path, base_dir)
            if repo_root is not None and not _is_relative_to(candidate, repo_root):
                errors.append(
                    "local_asset_research context file must be inside repository: "
                    f"{_truncate_repr(raw_path)} -> {_truncate_repr(str(candidate))}"
                )
                continue
            if not candidate.exists():
                errors.append(f"missing context file: {_truncate_repr(raw_path)}")
            elif not candidate.is_file():
                errors.append(f"context file is not a file: {_truncate_repr(raw_path)}")

    return errors


def _read_context_files(context_files: list[str], base_dir: Path) -> list[dict[str, str]]:
    contexts: list[dict[str, str]] = []
    for raw_path in context_files:
        candidate = _resolve_context_file(raw_path, base_dir)
        text = candidate.read_text(encoding="utf-8")
        try:
            display_path = str(candidate.relative_to(base_dir))
        except ValueError:
            display_path = str(candidate)
        contexts.append({
            "path": display_path,
            "content": text,
        })
    return contexts


def build_prompt(request: Mapping[str, Any], context_documents: list[dict[str, str]]) -> str:
    lines: list[str] = []
    lines.append("You are a Gemini CLI headless delegation worker.")
    lines.append("Follow the request exactly and keep the response scoped to the requested sections.")
    lines.append("")
    lines.append(f"Objective: {request['objective']}")
    lines.append(f"Tool profile: {request['tool_profile']}")
    lines.append(f"Model: {request.get('model', DEFAULT_MODEL)}")
    lines.append(f"Approval mode: plan")
    lines.append("")
    lines.append("Execution rules:")
    lines.append("- Do not edit files.")
    lines.append("- Do not run shell commands.")
    if request["tool_profile"] == LOCAL_ASSET_RESEARCH_PROFILE:
        lines.append("- Serena MCP may be used only for read-only local asset research inside the current repository.")
        lines.append("- Allowed Serena MCP tools are: find_file, find_referencing_symbols, find_symbol, get_symbols_overview, list_dir, search_for_pattern.")
        lines.append("- Do not use shell execution, file edit/write tools, GitHub write tools, memory write/read tools, or arbitrary paths outside the repository.")
        lines.append("- post_to_issue_url is forbidden for this profile; return the answer only in this process result.")
    elif request["tool_profile"] == PROPOSAL_ONLY_PROFILE:
        lines.append("- Return proposal text only; do not claim that you executed commands or mutated files.")
        lines.append("- Allowed deliverables are bounded drafts such as implementation_draft, issue_authoring_draft, patch_proposal, and command_plan.")
        lines.append("- Final file edits, shell execution, and GitHub mutations stay on the Codex side.")
        lines.append("- post_to_issue_url is forbidden for this profile; return the answer only in this process result.")
    elif request["tool_profile"] == GITHUB_RESEARCH_PROFILE:
        lines.append("- Read-only GitHub research only. Do not attempt to write, comment, or mutate any GitHub resource.")
        lines.append("- post_to_issue_url is forbidden for this profile; return the answer only in this process result.")
        lines.append("- Use only the gh command outputs already provided above; do not request additional gh executions.")
    else:
        lines.append("- Do not search the repository beyond the provided context files.")
    if request["tool_profile"] == "grounded_research":
        lines.append("- Google Search grounding is allowed when it is necessary for the answer.")
        lines.append("- Shell execution and file edits remain forbidden.")
    elif request["tool_profile"] == "no_tools":
        lines.append("- No tools are allowed.")
    elif request["tool_profile"] == PROPOSAL_ONLY_PROFILE:
        lines.append("- Treat the response as a draft for a downstream Codex worker, not as an executed result.")
    lines.append("")
    lines.append("Instructions:")
    for index, instruction in enumerate(request["instructions"], start=1):
        lines.append(f"{index}. {instruction}")
    lines.append("")
    if request.get("inline_context"):
        lines.append("Inline context:")
        lines.append(str(request["inline_context"]))
        lines.append("")
    lines.append("Context files:")
    for context in context_documents:
        lines.append(f"--- BEGIN CONTEXT FILE: {context['path']} ---")
        lines.append(context["content"])
        lines.append(f"--- END CONTEXT FILE: {context['path']} ---")
    lines.append("")
    lines.append("Required output sections:")
    for section in request["output_sections"]:
        lines.append(f"- {section}")
    lines.append("")
    lines.append("Return only the answer content. Do not wrap it in markdown fences.")
    return "\n".join(lines)


def _build_raw_command(model: str, prompt: str = "") -> list[str]:
    return [
        "gemini",
        "--model",
        model,
        "--approval-mode",
        "plan",
        "--skip-trust",
        "--prompt",
        prompt,
        "--output-format",
        "json",
    ]


def _build_run_invocation(
    requested_model: str,
    prompt: str,
    tool_profile: str,
) -> tuple[list[str], str | None, Path | None]:
    """Return the Gemini CLI command, stdin prompt, and cwd for a request.

    Both `local_asset_research` and `grounded_research` pass the prompt via stdin
    to avoid ARG_MAX limits when context is large. `local_asset_research` also
    sets cwd to the repo root so MCP Serena tools can resolve paths correctly.
    Other profiles preserve the existing argv prompt route.
    """
    if tool_profile == LOCAL_ASSET_RESEARCH_PROFILE:
        return _build_raw_command(requested_model, ""), prompt, _repo_root()
    if tool_profile == GROUNDED_RESEARCH_PROFILE:
        return _build_raw_command(requested_model, ""), prompt, None
    if tool_profile == GITHUB_RESEARCH_PROFILE:
        # Set cwd to repo root so gh can resolve the repository
        return _build_raw_command(requested_model, ""), prompt, _repo_root()
    return _build_raw_command(requested_model, prompt), None, None


def _extract_actual_model(stats: Mapping[str, Any] | None) -> str:
    if not isinstance(stats, Mapping):
        return "unknown"
    models = stats.get("models")
    if not isinstance(models, Mapping) or not models:
        return "unknown"
    for model_name in models.keys():
        if isinstance(model_name, str) and model_name.strip():
            return model_name
    return "unknown"


def _split_warnings(stderr: str | None) -> list[str]:
    if not stderr:
        return []
    return [line.strip() for line in stderr.splitlines() if line.strip()]


def _is_retryable_capacity_failure(returncode: int, stdout: str, stderr: str) -> bool:
    if returncode == 0:
        return False
    combined = "\n".join([stdout or "", stderr or ""])
    return any(pattern in combined for pattern in MODEL_CAPACITY_PATTERNS) or bool(
        _HTTP_429_RE.search(combined)
    )


def _run_gemini(
    command: list[str],
    timeout_sec: int,
    prompt: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="gemini-headless-") as temp_dir:
        return subprocess.run(
            command,
            input=prompt,
            cwd=str(cwd or temp_dir),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )


def _parse_envelope(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON envelope: {exc}"
    if not isinstance(parsed, dict):
        return None, "Gemini envelope must be a JSON object"
    return parsed, None


def _normalize_response_text(response: Any) -> str | None:
    if response is None:
        return None
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False, sort_keys=True)


def _truncate_summary(text: str, limit: int = 240) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _derive_summary(response_text: str | None) -> str | None:
    if not response_text:
        return None

    lines = [line.strip() for line in response_text.splitlines() if line.strip()]
    if not lines:
        return None

    for index, line in enumerate(lines):
        if any(pattern.match(line) for pattern in SUMMARY_HEADING_PATTERNS):
            for candidate in lines[index + 1 :]:
                if candidate and not any(pattern.match(candidate) for pattern in SUMMARY_HEADING_PATTERNS):
                    return _truncate_summary(candidate)

    for line in lines:
        if not any(pattern.match(line) for pattern in SUMMARY_HEADING_PATTERNS):
            return _truncate_summary(line)

    return _truncate_summary(lines[0])


def _build_result_surface(
    *,
    ok: bool,
    response_text: str | None,
    comment_url: str | None = None,
    post_requested: bool = False,
    post_result: str | None = None,
) -> dict[str, Any]:
    summary = _derive_summary(response_text)

    if comment_url:
        primary_artifact_type = "github_comment_url"
        primary_artifact = comment_url
        next_action = "Open the comment URL only if detailed evidence is needed."
    elif ok and response_text:
        primary_artifact_type = "inline_response_text"
        primary_artifact = "response_text"
        next_action = "Use this summary first and read response_text only when detailed evidence is needed."
    else:
        primary_artifact_type = "none"
        primary_artifact = None
        next_action = "Inspect warnings and failure_reason before retrying or escalating."

    if post_requested and post_result and post_result != "success" and ok and response_text:
        next_action = (
            "Comment posting failed; use this summary first, inspect warnings/post_result, "
            "and read response_text only if detailed evidence is needed."
        )

    return {
        "mode": "artifact-first",
        "summary": summary,
        "primary_artifact_type": primary_artifact_type,
        "primary_artifact": primary_artifact,
        "next_action": next_action,
    }


def _collect_error_search_sources(value: Any) -> list[tuple[str, str]]:
    """Collect searchable text from a Gemini envelope error payload.

    The search walks every scalar leaf in the payload and preserves a path-like
    label so rate-limit detection can distinguish code/status/reason leaves from
    generic message text.
    """

    texts: list[tuple[str, str]] = []

    def add_text(path: str, candidate: Any) -> None:
        if isinstance(candidate, str):
            cleaned = candidate.strip()
            if cleaned:
                texts.append((path, cleaned))
        elif isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            texts.append((path, str(candidate)))

    def visit(node: Any, path: str = "error") -> None:
        if node is None:
            return
        if isinstance(node, Mapping):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else key
                if isinstance(child, (Mapping, list)):
                    visit(child, child_path)
                else:
                    add_text(child_path, child)
            return

        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{path}[{index}]")
            return

        add_text(path, node)

    visit(value)
    return texts


def _is_capacity_signal(source_path: str, source_text: str) -> bool:
    normalized = source_text.casefold()
    if any(pattern.casefold() in normalized for pattern in MODEL_CAPACITY_PATTERNS):
        return True
    if _HTTP_429_RE.search(source_text):
        return True
    return any(
        phrase in normalized
        for phrase in (
            "too many requests",
            "rate limit",
            "quota exhausted",
            "quota",
            "resource exhausted",
            "model capacity",
        )
    )


def _log_model_downgrade_event(from_model: str, to_model: str, reason: str) -> None:
    """Emit a structured log event for a model downgrade.

    The event is printed to stderr so it appears in logs without polluting
    the JSON result surface. Format is machine-parseable JSON.
    """
    event = json.dumps(
        {"event": "model_downgrade", "from": from_model, "to": to_model, "reason": reason},
        ensure_ascii=False,
    )
    print(f"[gemini-headless] {event}", file=sys.stderr)


def run_delegation(
    request: Mapping[str, Any],
    request_path: Path | None = None,
    _routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation_errors = validate_request(request, request_path=request_path)
    requested_model = str(request.get("model", DEFAULT_MODEL))
    tool_profile = str(request.get("tool_profile", "unknown"))

    request_warnings: list[str] = []
    _timeout_raw = request.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    if tool_profile == "grounded_research" and isinstance(_timeout_raw, (int, float)) and _timeout_raw < 300:
        request_warnings.append(
            f"grounded_research requires timeout_sec >= 300 (got {_timeout_raw});"
            " request may time out during Google Search tool calls"
        )

    base_result: dict[str, Any] = {
        "schema": "delegation_result/v1",
        "ok": False,
        "requested_model": requested_model,
        "actual_model": "unknown",
        "tool_profile": tool_profile,
        "exit_code": 1,
        "result_surface": {
            "mode": "artifact-first",
            "summary": None,
            "primary_artifact_type": "none",
            "primary_artifact": None,
            "next_action": "Inspect warnings and failure_reason before retrying or escalating.",
        },
        "response_text": None,
        "stats": None,
        "stderr": None,
        "warnings": [],
        "failure_reason": None,
        "raw_command": [],
        "model_chain": [],
        "model_downgrades": [],
    }

    if validation_errors:
        base_result["stderr"] = "\n".join(validation_errors)
        base_result["warnings"] = validation_errors[:] + request_warnings
        base_result["failure_reason"] = validation_errors[0]
        # github_research: propagate failure_class for denied commands
        if tool_profile == GITHUB_RESEARCH_PROFILE and any(
            "github_research_command_denied" in e
            or "is not in the allowed subcommand list" in e
            or "forbids post_to_issue_url" in e
            or "-X" in e
            or "--method" in e
            or "implies a non-GET request" in e
            or "gh api graphql is not allowed" in e
            for e in validation_errors
        ):
            base_result["failure_class"] = "github_research_command_denied"
        return base_result

    # Resolve model chain
    try:
        routing = _routing if _routing is not None else load_model_routing()
        model_chain, chain_error = resolve_model_chain(request, routing)
    except ValueError as exc:
        base_result["failure_reason"] = f"model_routing config error: {exc}"
        base_result["warnings"] = request_warnings + [str(exc)]
        base_result["reason_code"] = "routing_config_invalid"
        return base_result

    if chain_error:
        base_result["failure_reason"] = chain_error
        base_result["warnings"] = request_warnings + [chain_error]
        if "unknown_role" in chain_error:
            base_result["reason_code"] = "unknown_role"
        else:
            base_result["reason_code"] = "empty_chain"
        return base_result

    base_result["model_chain"] = list(model_chain)

    base_dir = request_path.parent if request_path is not None else Path.cwd()

    # github_research: execute gh_commands and prepend output to inline_context
    gh_commands_output: str | None = None
    if tool_profile == GITHUB_RESEARCH_PROFILE:
        gh_commands = request.get("gh_commands")
        if isinstance(gh_commands, list) and gh_commands:
            gh_output_parts: list[str] = []
            gh_success_count = 0
            gh_attempted_count = 0
            for entry in gh_commands:
                if not isinstance(entry, dict):
                    continue
                argv = entry.get("argv")
                if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                    continue
                gh_attempted_count += 1
                try:
                    gh_proc = subprocess.run(
                        ["gh"] + argv,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=str(_repo_root()),
                        check=False,
                    )
                    cmd_str = "gh " + " ".join(argv)
                    if gh_proc.returncode == 0:
                        gh_success_count += 1
                        gh_output_parts.append(f"## gh command: {cmd_str}\n{gh_proc.stdout.strip()}")
                    else:
                        gh_output_parts.append(
                            f"## gh command: {cmd_str}\n[exit {gh_proc.returncode}] {gh_proc.stderr.strip()}"
                        )
                        base_result["warnings"].append(
                            f"github_research: gh {' '.join(argv)} exited {gh_proc.returncode}: {gh_proc.stderr.strip()}"
                        )
                except Exception as exc:
                    base_result["warnings"].append(f"github_research: gh command error: {exc}")
            if gh_output_parts:
                gh_commands_output = "\n\n".join(gh_output_parts)
            # Fail-close if all gh_commands failed (auth issue or environment problem)
            if gh_attempted_count > 0 and gh_success_count == 0:
                base_result["ok"] = False
                base_result["failure_class"] = "gh_auth_required"
                base_result["failure_reason"] = (
                    "all gh_commands failed; check `gh auth status` and preflight"
                )
                return base_result

    # local_asset_research / proposal_only: execute gh_commands with warn-on-denied
    elif tool_profile in (LOCAL_ASSET_RESEARCH_PROFILE, PROPOSAL_ONLY_PROFILE):
        gh_commands = request.get("gh_commands")
        if isinstance(gh_commands, list) and gh_commands:
            gh_output_parts_warn: list[str] = []
            for idx, entry in enumerate(gh_commands):
                if not isinstance(entry, dict):
                    request_warnings.append(
                        f"{tool_profile}: gh_commands[{idx}] must be an object with 'argv' field; skipping"
                    )
                    continue
                argv = entry.get("argv")
                if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                    request_warnings.append(
                        f"{tool_profile}: gh_commands[{idx}].argv must be a list of strings; skipping"
                    )
                    continue
                # allowlist validation: invalid argv -> warn + skip (no fail-close)
                argv_errors = _validate_github_research_argv(argv)
                if argv_errors:
                    for err in argv_errors:
                        request_warnings.append(
                            f"{tool_profile}: gh_commands[{idx}] argv denied: {err}; skipping"
                        )
                    continue
                # valid argv: execute
                try:
                    gh_proc = subprocess.run(
                        ["gh"] + argv,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=str(_repo_root()),
                        check=False,
                    )
                    cmd_str = "gh " + " ".join(argv)
                    if gh_proc.returncode == 0:
                        gh_output_parts_warn.append(f"## gh command: {cmd_str}\n{gh_proc.stdout.strip()}")
                    else:
                        gh_output_parts_warn.append(
                            f"## gh command: {cmd_str}\n[exit {gh_proc.returncode}] {gh_proc.stderr.strip()}"
                        )
                        request_warnings.append(
                            f"{tool_profile}: gh {' '.join(argv)} exited {gh_proc.returncode}: {gh_proc.stderr.strip()}"
                        )
                except Exception as exc:
                    request_warnings.append(f"{tool_profile}: gh command error: {exc}")
            if gh_output_parts_warn:
                gh_commands_output = "\n\n".join(gh_output_parts_warn)

    # Merge gh_commands output into inline_context
    existing_inline = request.get("inline_context") or ""
    if gh_commands_output:
        merged_inline_context = f"## GitHub Research Results\n{gh_commands_output}\n\n{existing_inline}".strip()
    else:
        merged_inline_context = existing_inline or None

    # Build a mutable copy of request with merged inline_context for prompt building
    if merged_inline_context and merged_inline_context != existing_inline:
        merged_request: Mapping[str, Any] = {**request, "inline_context": merged_inline_context}
    else:
        merged_request = request

    context_documents = _read_context_files(list(request["context_files"]), base_dir)
    prompt = build_prompt(merged_request, context_documents)
    timeout_sec = int(request.get("timeout_sec", DEFAULT_TIMEOUT_SEC))

    # --- Model chain loop ---
    warnings: list[str] = request_warnings[:]
    model_downgrades: list[dict[str, str]] = []
    last_completed: subprocess.CompletedProcess[str] | None = None
    last_command: list[str] = []
    final_model: str = model_chain[0] if model_chain else requested_model
    chain_exhausted = False

    for model_index, current_model in enumerate(model_chain):
        final_model = current_model
        command, stdin_prompt, run_cwd = _build_run_invocation(current_model, prompt, tool_profile)
        last_command = command
        model_quota_exhausted = False

        try:
            for attempt in range(RETRY_LIMIT + 1):
                try:
                    completed = _run_gemini(command, timeout_sec, stdin_prompt, run_cwd)
                except subprocess.TimeoutExpired:
                    warnings.append(f"timeout after {timeout_sec}s on attempt {attempt + 1} (model={current_model})")
                    if attempt < RETRY_LIMIT:
                        time.sleep(min(2**attempt, 4))
                        continue
                    base_result.update({
                        "exit_code": 124,
                        "stderr": f"timeout after {timeout_sec}s",
                        "warnings": warnings,
                        "failure_reason": f"timeout after {timeout_sec}s",
                        "raw_command": command,
                        "actual_model": current_model,
                        "model_chain": list(model_chain),
                        "model_downgrades": model_downgrades,
                    })
                    return base_result
                last_completed = completed
                if completed.returncode == 0:
                    break
                if _is_retryable_capacity_failure(completed.returncode, completed.stdout, completed.stderr):
                    warnings.append(
                        f"retryable capacity failure detected on attempt {attempt + 1} (model={current_model}); retrying same model"
                    )
                    if attempt < RETRY_LIMIT:
                        time.sleep(min(2**attempt, 4))
                        continue
                    # Per-model retry budget exhausted with quota error → try next model
                    model_quota_exhausted = True
                break
        except Exception as exc:
            base_result.update({
                "stderr": str(exc),
                "warnings": warnings + [str(exc)],
                "failure_reason": str(exc),
                "raw_command": command,
                "actual_model": current_model,
                "model_chain": list(model_chain),
                "model_downgrades": model_downgrades,
            })
            return base_result

        # If per-model retry budget exhausted due to quota, try next model
        if model_quota_exhausted and model_index < len(model_chain) - 1:
            next_model = model_chain[model_index + 1]
            downgrade_event = {
                "from": current_model,
                "to": next_model,
                "reason": "quota_model_downgrade",
            }
            model_downgrades.append(downgrade_event)
            warnings.append(
                f"model_downgrade: quota exhausted on {current_model!r}; downgrading to {next_model!r}"
            )
            # structured log event
            _log_model_downgrade_event(current_model, next_model, "quota_model_downgrade")
            continue  # try next model

        if model_quota_exhausted:
            # Last model in chain also quota-exhausted → chain fully exhausted
            chain_exhausted = True

        # Success or non-quota failure — stop iterating chain
        break

    if chain_exhausted:
        # All models in chain exhausted with quota errors
        base_result.update({
            "ok": False,
            "actual_model": final_model,
            "exit_code": last_completed.returncode if last_completed is not None else 1,
            "warnings": warnings,
            "failure_reason": "model_chain_exhausted: all models in chain failed with quota errors",
            "reason_code": "model_chain_exhausted",
            "raw_command": last_command,
            "model_chain": list(model_chain),
            "model_downgrades": model_downgrades,
            "result_surface": _build_result_surface(ok=False, response_text=None),
        })
        return base_result

    assert last_completed is not None
    stdout = last_completed.stdout or ""
    stderr = last_completed.stderr or ""
    warnings.extend(_split_warnings(stderr))

    envelope, parse_error = _parse_envelope(stdout)
    if parse_error:
        warnings.append(parse_error)
        base_result["stderr"] = stderr or None
        base_result["warnings"] = warnings
        base_result["exit_code"] = last_completed.returncode
        base_result["failure_reason"] = parse_error
        base_result["raw_command"] = last_command
        base_result["actual_model"] = final_model
        base_result["model_chain"] = list(model_chain)
        base_result["model_downgrades"] = model_downgrades
        return base_result

    response_text = _normalize_response_text(envelope.get("response"))
    stats = envelope.get("stats") if isinstance(envelope.get("stats"), Mapping) else envelope.get("stats")
    actual_model_from_stats = _extract_actual_model(stats if isinstance(stats, Mapping) else None)
    actual_model_value = actual_model_from_stats if actual_model_from_stats != "unknown" else final_model
    ok = last_completed.returncode == 0 and bool(response_text) and "error" not in envelope
    if "error" in envelope and isinstance(envelope["error"], Mapping):
        warnings.append("Gemini envelope included an error object")

    # Determine failure_reason if ok=False
    failure_reason: str | None = None
    reason_code: str | None = None
    if not ok:
        rate_limit_sources: list[str] = []
        for warning in warnings:
            if _is_capacity_signal("warning", warning):
                rate_limit_sources.append(warning)
        for source_field, source_text in _collect_error_search_sources(envelope.get("error")):
            if _is_capacity_signal(source_field, source_text):
                rate_limit_sources.append(source_text)
        rate_limit_warnings = rate_limit_sources
        if not bool(response_text) and last_completed.returncode == 0:
            if rate_limit_warnings:
                failure_reason = f"response_text is empty; rate limit detected: {rate_limit_warnings[0]}"
            else:
                failure_reason = "response_text is empty"
        elif last_completed.returncode != 0:
            if rate_limit_warnings:
                failure_reason = f"exit code {last_completed.returncode}; rate limit detected: {rate_limit_warnings[0]}"
            else:
                failure_reason = f"exit code {last_completed.returncode}"
        elif "error" in envelope:
            if rate_limit_warnings:
                failure_reason = f"Gemini envelope contained an error; rate limit detected: {rate_limit_warnings[0]}"
            else:
                failure_reason = "Gemini envelope contained an error"

    base_result.update(
        {
            "ok": ok,
            "actual_model": actual_model_value,
            "exit_code": last_completed.returncode,
            "result_surface": _build_result_surface(ok=ok, response_text=response_text),
            "response_text": response_text,
            "stats": stats,
            "stderr": stderr or None,
            "warnings": warnings,
            "failure_reason": failure_reason,
            "raw_command": last_command,
            "model_chain": list(model_chain),
            "model_downgrades": model_downgrades,
        }
    )
    if reason_code:
        base_result["reason_code"] = reason_code

    # AC-5: post_to_issue_url が指定されており、ok=True の場合のみ gh issue comment を実行する
    post_to_issue_url = request.get("post_to_issue_url")
    if post_to_issue_url and base_result["ok"]:
        response_text = base_result.get("response_text") or ""
        try:
            post_proc = subprocess.run(
                ["gh", "issue", "comment", str(post_to_issue_url), "--body", response_text],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if post_proc.returncode == 0:
                # gh issue comment は成功時に comment URL を stdout に出力する
                base_result["comment_url"] = post_proc.stdout.strip()
                base_result["post_result"] = "success"
            else:
                base_result["warnings"].append(
                    f"post_to_issue_url: gh issue comment failed (exit {post_proc.returncode}): {post_proc.stderr.strip()}"
                )
                base_result["post_result"] = f"failed: {post_proc.stderr.strip()}"
        except Exception as exc:
            base_result["warnings"].append(f"post_to_issue_url: unexpected error: {exc}")
            base_result["post_result"] = f"error: {exc}"

        base_result["result_surface"] = _build_result_surface(
            ok=bool(base_result["ok"]),
            response_text=base_result.get("response_text"),
            comment_url=base_result.get("comment_url"),
            post_requested=True,
            post_result=base_result.get("post_result"),
        )

    return base_result


_COMPACT_EXCLUDED_FIELDS = ("stats", "raw_command")


def _apply_compact(result: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *result* with top-level verbose fields removed.

    Fields listed in ``_COMPACT_EXCLUDED_FIELDS`` (``stats``, ``raw_command``) are
    stripped from the flat top-level dict.

    Note: This function operates on *top-level* keys of a flat result dict.
    It is distinct from ``_strip_verbose_subfields`` in ``preflight_gemini_headless.py``,
    which removes verbose *subfields* from nested section dicts (version, help, smoke).
    """
    return {k: v for k, v in result.items() if k not in _COMPACT_EXCLUDED_FIELDS}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Omit stats and raw_command from output JSON to reduce context window usage.",
    )
    parser.add_argument(
        "--output-format",
        choices=["json", "ndjson"],
        default="json",
        help="Output format: 'json' (default, overwrite) or 'ndjson' (append, one JSON object per line).",
    )
    return parser


def _print_stdout_summary(result: dict[str, Any], output_file: Path) -> None:
    if result["ok"]:
        response_text = result.get("response_text")
        if response_text:
            print(response_text)
        else:
            print("[gemini-headless] warning: response_text is empty")
    else:
        warnings: list[str] = result.get("warnings") or []
        if warnings:
            print(warnings[0])
        else:
            print("[gemini-headless] error: delegation failed (no failure reason available; see result JSON)")
    print(f"[gemini-headless] result saved to: {output_file}")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    request = _load_json(args.request_file)
    if not isinstance(request, Mapping):
        result = {
            "schema": "delegation_result/v1",
            "ok": False,
            "requested_model": DEFAULT_MODEL,
            "actual_model": "unknown",
            "tool_profile": "unknown",
            "exit_code": 1,
            "result_surface": _build_result_surface(ok=False, response_text=None),
            "response_text": None,
            "stats": None,
            "stderr": "request file must contain a JSON object",
            "warnings": ["request file must contain a JSON object"],
            "failure_reason": "request file must contain a JSON object",
            "raw_command": [],
        }
    else:
        result = run_delegation(request, request_path=args.request_file)
    if args.compact:
        result = _apply_compact(result)
    if args.output_format == "ndjson":
        _append_ndjson(args.output_file, result)
    else:
        _dump_json(args.output_file, result)
    _print_stdout_summary(result, args.output_file)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
