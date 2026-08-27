"""The README must call the library that exists.

Every published example was wrong at v1.0.0 -- `ToolRegistry(audit=...)`,
`ToolSpec(schema=, risk_tier=)`, `TierGate(threshold=)` and a `record_trace`
missing its first argument -- and nothing caught it, because documentation is
the one part of the repository no test was pointed at. It is also the first
code a reader copies.

This walks every fenced python block in README.md, finds each call to a name
the package exports, and binds it against the real signature. Renaming a
public parameter now fails CI in the same commit rather than in someone's
terminal.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

import harness

README = Path(__file__).resolve().parents[1] / "README.md"
BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
EXPORTS = set(harness.__all__)


def documented_calls() -> list[tuple[str, int, ast.Call]]:
    found: list[tuple[str, int, ast.Call]] = []
    for block in BLOCK.findall(README.read_text(encoding="utf-8")):
        # Blocks are illustrative and may use `await` at top level, so they are
        # parsed rather than executed.
        tree = ast.parse(block, mode="exec", type_comments=False)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in EXPORTS
            ):
                found.append((node.func.id, node.lineno, node))
    return found


def _ids(calls: list[tuple[str, int, ast.Call]]) -> list[str]:
    return [f"{name}@L{line}" for name, line, _ in calls]


CALLS = documented_calls()


def test_readme_contains_examples() -> None:
    """A README that stopped documenting the API would otherwise pass silently."""
    assert CALLS, "no calls to exported names found in README.md"


@pytest.mark.parametrize("name,lineno,node", CALLS, ids=_ids(CALLS))
def test_documented_call_matches_signature(name: str, lineno: int, node: ast.Call) -> None:
    target = getattr(harness, name)
    if not callable(target):
        pytest.skip(f"{name} is not callable")
    signature = inspect.signature(target)
    positional = [object()] * len(node.args)
    keywords = {kw.arg: object() for kw in node.keywords if kw.arg is not None}
    try:
        # Strict bind, not bind_partial: a README shows complete calls, and
        # `record_trace(ctx, result)` -- one positional short of the real
        # signature -- is exactly the kind of error a partial bind waves through.
        signature.bind(*positional, **keywords)
    except TypeError as exc:
        pytest.fail(
            f"README documents {name}(...) at block line {lineno} but the real "
            f"signature is {name}{signature} -- {exc}"
        )
