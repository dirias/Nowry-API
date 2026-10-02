"""
GTM-010 — motion is held (ADR-040).

The animation endpoint refuses with `animation_disabled` unless the operator
switches it on, and an evolution no longer marks the animation for
regeneration while it is off. The code stays, so the revisit is a variable.
"""
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def _flag_module():
    """Read the flag reader without importing the agent router (it needs the models package)."""
    import importlib.util, types
    source = (APP / "routers" / "agent.py").read_text()
    start = source.index("def companion_animation_enabled()")
    end = source.index("\n\n", start)
    module = types.ModuleType("flag")
    exec("import os\n" + source[start:end], module.__dict__)
    return module


def test_flag_is_off_unless_switched_on(monkeypatch):
    flag = _flag_module()
    monkeypatch.delenv("COMPANION_ANIMATION_ENABLED", raising=False)
    assert flag.companion_animation_enabled() is False
    monkeypatch.setenv("COMPANION_ANIMATION_ENABLED", "true")
    assert flag.companion_animation_enabled() is True
    monkeypatch.setenv("COMPANION_ANIMATION_ENABLED", "no")
    assert flag.companion_animation_enabled() is False


def test_endpoint_refuses_before_reading_the_user():
    source = (APP / "routers" / "agent.py").read_text()
    body = source[source.index("async def generate_animation(") :]
    refusal = body.index('raise HTTPException(status_code=403, detail="animation_disabled")')
    user_read = body.index("users_collection.find_one")
    assert refusal < user_read


def test_evolution_marks_animation_only_while_enabled():
    source = (APP / "routers" / "agent.py").read_text()
    block = source[source.index('"preferences.pet.avatar_regen_pending": True,') :][:400]
    assert "if companion_animation_enabled() else {}" in block
