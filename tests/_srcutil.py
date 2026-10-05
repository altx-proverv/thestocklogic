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


def migration(stem: str):
    """The migration whose name ends in `stem`, pending or applied.

    A migration is named PENDING_<stem>.sql while it waits and
    <timestamp>_<stem>.sql once it has been applied, so a test that hardcodes
    either filename breaks the day the other one is true. tests/test_meridian_iv.py
    did exactly that: renaming six applied files on 2026-10-03 failed a suite that
    was testing the SQL's content, which had not changed at all.

    Resolves by suffix so the test follows the file through the rename, and raises
    with both names it looked for rather than returning an empty string -- a
    migration assertion that silently passes over missing SQL is worse than no
    assertion.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "migrations"
    hits = sorted(root.glob(f"PENDING_{stem}.sql")) + \
        sorted(root.glob(f"*_{stem}.sql"))
    if not hits:
        raise FileNotFoundError(
            f"no migration matching PENDING_{stem}.sql or *_{stem}.sql in "
            f"{root}. If it was renamed, rename the stem here too; if it was "
            f"deleted, this assertion has nothing left to check.")
    return hits[0].read_text(encoding="utf-8")
