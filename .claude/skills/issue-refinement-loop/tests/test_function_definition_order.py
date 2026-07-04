"""
test_function_definition_order.py

Regression tests for Issue #1334: run_refinement_preflight.py used to define
`_build_scope_delta_authority_evidence` AFTER both its first call site and the
`if __name__ == "__main__":` trigger, causing a guaranteed `NameError` at
script execution time (the module-level `if __name__ == "__main__": main()`
line ran `main()` -> `run_preflight()` -> `_build_scope_delta_authority_evidence(...)`
before the module had reached the `def _build_scope_delta_authority_evidence`
statement further down in the file).

VC rg keywords verified in this file:
  - test_definition_before_first_call  (AC1)
  - test_definition_before_main_trigger (AC2)
  - test_module_imports_without_nameerror (AC3)
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
TARGET_SCRIPT = SCRIPTS_DIR / "run_refinement_preflight.py"

TARGET_FUNCTION_NAME = "_build_scope_delta_authority_evidence"


def _parse_target_ast() -> ast.Module:
    source = TARGET_SCRIPT.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(TARGET_SCRIPT))


def _find_function_def_line(tree: ast.Module, name: str) -> int:
    """Return the line number of the top-level `def <name>(...)` statement."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.lineno
    raise AssertionError(f"function definition not found: {name}")


def _find_first_call_line(tree: ast.Module, name: str) -> int:
    """Return the line number of the first `name(...)` call anywhere in the module."""
    call_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            call_lines.append(node.lineno)
    if not call_lines:
        raise AssertionError(f"no call site found for: {name}")
    return min(call_lines)


def _find_main_trigger_line(tree: ast.Module) -> int:
    """Return the line number of the top-level `if __name__ == "__main__":` statement."""
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                return node.lineno
    raise AssertionError('no top-level `if __name__ == "__main__":` trigger found')


def test_definition_before_first_call() -> None:
    """GIVEN run_refinement_preflight.py
    WHEN the module is parsed via AST
    THEN the def line of _build_scope_delta_authority_evidence must come before
    its first call site's line number (else the call would raise NameError when
    executed as a module-level side effect before the def is reached).
    """
    tree = _parse_target_ast()
    def_line = _find_function_def_line(tree, TARGET_FUNCTION_NAME)
    call_line = _find_first_call_line(tree, TARGET_FUNCTION_NAME)
    assert def_line < call_line, (
        f"{TARGET_FUNCTION_NAME} def (line {def_line}) must be defined before its "
        f"first call site (line {call_line})"
    )


def test_definition_before_main_trigger() -> None:
    """GIVEN run_refinement_preflight.py
    WHEN the module is parsed via AST
    THEN the def line of _build_scope_delta_authority_evidence must come before
    the `if __name__ == "__main__":` trigger, so the function is always defined
    by the time `main()` could possibly invoke it.
    """
    tree = _parse_target_ast()
    def_line = _find_function_def_line(tree, TARGET_FUNCTION_NAME)
    main_trigger_line = _find_main_trigger_line(tree)
    assert def_line < main_trigger_line, (
        f"{TARGET_FUNCTION_NAME} def (line {def_line}) must be defined before the "
        f'`if __name__ == "__main__":` trigger (line {main_trigger_line})'
    )


def test_module_imports_without_nameerror() -> None:
    """GIVEN run_refinement_preflight.py
    WHEN the module is loaded via importlib.util (module-level code executed)
    THEN no NameError is raised and the target function is defined on the module.
    """
    spec = importlib.util.spec_from_file_location(
        "run_refinement_preflight_definition_order_check", TARGET_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except NameError as exc:  # pragma: no cover - regression guard
        raise AssertionError(f"module import raised NameError: {exc}") from exc

    assert hasattr(module, TARGET_FUNCTION_NAME), (
        f"expected module attribute {TARGET_FUNCTION_NAME!r} after import"
    )
