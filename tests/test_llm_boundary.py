import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "review_watcher"
ALLOWED_MODULE = "reviewer.py"


def _mentions_claude_binary(node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    value = node.value
    return value == "claude" or value.endswith("/claude") or value.startswith("claude ")


def test_only_reviewer_module_references_the_claude_binary() -> None:
    offenders: list[str] = []
    for module in sorted(SRC.rglob("*.py")):
        if module.name == ALLOWED_MODULE:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if _mentions_claude_binary(node):
                offenders.append(f"{module.relative_to(SRC)}:{getattr(node, 'lineno', '?')}")

    assert offenders == []
