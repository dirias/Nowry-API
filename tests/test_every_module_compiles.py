"""
Every module under app/ compiles (GTM-003 follow-up).

A duplicate argument name is a SyntaxError the *compiler* raises, not the
parser: `ast.parse` accepts it and only an import trips over it. One such
file took the dev API down on 2026-10-03. This compiles every module the way
an import would, so the suite catches it before a deploy does.
"""
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
MODULES = sorted(p for p in APP.rglob("*.py") if p.is_file() and "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(APP)))
def test_module_compiles(path: Path):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
