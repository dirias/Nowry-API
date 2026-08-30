"""Shared dependency-stub helper for the test suite (DEBT-003 / DEBT-007).

Several test modules stub heavy optional dependencies in `sys.modules` so the
routers can be imported without credentials or a full install. They all used to
do it unconditionally, guarded only by "is this key absent?" — which is not the
same question as "is this package missing?".

That distinction is the whole bug. A `MagicMock` is not a package, so once
`google.generativeai` was replaced by one, `google.generativeai.types` could no
longer resolve *through* it — in the project venv, where the real package is
installed. Because these stubs run at module-import time during collection, one
test file stubbing a package it did not need broke `app.main` for every file
collected after it, and a collection error aborts the whole run rather than
failing one file.

So the rule is: try the real module first, and stub only what genuinely is not
importable. A real package serves its own submodules; a stub is a fallback, not
a default.
"""
import importlib
import sys
from unittest.mock import MagicMock


def stub_if_missing(*module_names: str) -> None:
    """Register a `MagicMock` for each name that cannot actually be imported.

    Never replaces a module already present in `sys.modules` — another test
    file may have installed a purpose-built stub of its own at collection time,
    and that one knows more about what it needs than this does.
    """
    for module_name in module_names:
        if module_name in sys.modules:
            continue
        try:
            importlib.import_module(module_name)
        except Exception:
            sys.modules.setdefault(module_name, MagicMock())
