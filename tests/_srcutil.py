"""
Assert what a module DOES, not what it says about itself.

This exists because the same mistake has now been made five times in this repo: a
check greps a module's source for a string it must not contain, the module's own
comments explain at length why it does not contain it, and the check reports the
explanation as the defect. engine/excursions.py names last_swing_low in order to
say it never reads it. engine/score_live_outcomes.py writes `limit=200000` in
order to say that limit does not work.

A test over source text cannot tell a prohibition from its own rationale.
Stripping comments and docstrings leaves only what executes.
"""

import ast
from pathlib import Path


def executable_source(path) -> str:
    """Module source with every comment and docstring removed.

    Comments go because ast.parse discards them. Docstrings have to be removed
    deliberately -- they are real string expressions and survive a round trip.
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def normalised(path) -> str:
    """executable_source with quotes and spacing flattened, so a check matches on
    content rather than on how ast.unparse chose to format it."""
    return executable_source(path).replace('"', "'").replace(" ", "")
