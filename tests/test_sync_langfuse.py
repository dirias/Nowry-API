"""Tests for Phase 11 Sync Script — SY-01, SY-02.

Wave 0 RED stubs: these tests import scripts.sync_langfuse, which does not yet
exist. They fail with ModuleNotFoundError for 'scripts.sync_langfuse' (correct
RED state) until Task 2 delivers the script. Task 2 then makes them pass GREEN.

Test isolation strategy mirrors test_model_config.py / test_langfuse_client.py:
- sys.modules.setdefault() guard prevents groq/google.generativeai/langfuse
  SDK import errors on the Python 3.9 test runner
- monkeypatch for env vars; patch() for the Langfuse constructor and the
  model_config singleton client attributes
- each test imports scripts.sync_langfuse inside the test body (not at module
  level) so the ImportError during RED is contained to each test individually,
  matching the test_prompt_manager.py pattern for not-yet-existing modules
"""

import sys
from unittest.mock import MagicMock

# Prevent SDK import errors on Python 3.9 test runner
sys.modules.setdefault("groq", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())
sys.modules.setdefault("langfuse", MagicMock())

import importlib
import inspect
import pytest
from unittest.mock import patch, MagicMock

from app.core.prompt_manager import _FALLBACKS


def test_missing_credentials_exits_nonzero(monkeypatch):
    """11-01-01 / SY-01: missing LANGFUSE_SECRET_KEY/LANGFUSE_PUBLIC_KEY exits 1
    BEFORE any Langfuse client construction or API call (T-11-01 fail-fast)."""
    from scripts import sync_langfuse

    # delenv AFTER import: sync_langfuse's module-level load_dotenv(dotenv_path=...)
    # (11-03 gap fix) populates os.environ from app/.env at import time, which
    # would otherwise re-supply real credentials and defeat this test. Deleting
    # after import (but before main()) ensures _validate_credentials() sees
    # unset vars regardless of whether this is the first import in the session.
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)

    fake_langfuse_ctor = MagicMock()
    with patch("scripts.sync_langfuse.Langfuse", fake_langfuse_ctor, create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main([])

    assert exc_info.value.code == 1
    assert fake_langfuse_ctor.call_count == 0


def test_load_dotenv_called_before_prompt_manager_import():
    """Gap-fix regression (UAT Test 1 & 2): load_dotenv(dotenv_path=...) pointing
    at app/.env must run BEFORE `from app.core.prompt_manager import _FALLBACKS` --
    otherwise (a) _validate_credentials() can never see app/.env's keys (UAT Test 2,
    blocker), and (b) app.core.langfuse_client's module-level singleton (transitively
    imported via prompt_manager) sees unset env vars and prints a spurious 'Langfuse
    disabled' warning even on --help (UAT Test 1, minor)."""
    from scripts import sync_langfuse

    source = inspect.getsource(sync_langfuse)

    load_dotenv_snippet = 'load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / "app" / ".env")'
    import_snippet = "from app.core.prompt_manager import _FALLBACKS"

    assert load_dotenv_snippet in source
    assert import_snippet in source

    load_dotenv_index = source.index(load_dotenv_snippet)
    import_index = source.index(import_snippet)

    assert load_dotenv_index < import_index, (
        "load_dotenv(dotenv_path=...) must appear BEFORE the "
        "app.core.prompt_manager import so env vars are loaded before "
        "the transitively-imported langfuse_client singleton reads os.environ"
    )


def test_compare_before_push_skips_unchanged(monkeypatch):
    """11-01-02 / SY-01, SY-02: identical content is detected as unchanged —
    zero create_prompt calls, returns 'unchanged' (D-05 compare-before-push)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    from scripts import sync_langfuse

    local_content = _FALLBACKS["nowry-book-expand"]

    fake_client = MagicMock()
    fake_current = MagicMock()
    fake_current.prompt = local_content
    fake_client.get_prompt.return_value = fake_current

    result = sync_langfuse.compare_and_push_prompt(
        fake_client, "nowry-book-expand", local_content, dry_run=False
    )

    assert fake_client.create_prompt.call_count == 0
    assert result == "unchanged"


def test_compare_before_push_pushes_changed(monkeypatch):
    """11-01-03 / SY-01, SY-02: differing content triggers exactly one
    create_prompt call labeled 'production', returns 'pushed' (D-05)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    from scripts import sync_langfuse

    local_content = _FALLBACKS["nowry-book-expand"]
    name = "nowry-book-expand"

    fake_client = MagicMock()
    fake_current = MagicMock()
    fake_current.prompt = "OLD CONTENT THAT DIFFERS"
    fake_client.get_prompt.return_value = fake_current

    result = sync_langfuse.compare_and_push_prompt(
        fake_client, name, local_content, dry_run=False
    )

    assert fake_client.create_prompt.call_count == 1
    _, kwargs = fake_client.create_prompt.call_args
    assert kwargs["labels"] == ["production"]
    assert kwargs["name"] == name
    assert kwargs["prompt"] == local_content
    assert result == "pushed"


def test_model_config_derived_from_live_wiring(monkeypatch):
    """11-02-01 / SY-02: nowry-model-config values are derived live from
    model_config.py's singleton client attributes — never hardcoded, never
    read from subscription_plans.AGENT_MODELS (D-07 / Pitfall 4 guard)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    fake_groq = MagicMock()
    fake_groq.model = "llama-3.3-70b-versatile"
    fake_flash = MagicMock()
    fake_flash._model_id = "models/gemini-flash-latest"
    fake_pro = MagicMock()
    fake_pro._model_id = "models/gemini-pro-latest"

    import app.core.model_config as model_config

    with patch.object(model_config, "_groq_client", fake_groq), \
         patch.object(model_config, "_gemini_flash_client", fake_flash), \
         patch.object(model_config, "_gemini_pro_client", fake_pro):
        from scripts import sync_langfuse
        result = sync_langfuse.derive_model_config_dict()

    assert result == {
        "free": "llama-3.3-70b-versatile",
        "plus": "models/gemini-flash-latest",
        "pro": "models/gemini-pro-latest",
    }

    source = inspect.getsource(sync_langfuse.derive_model_config_dict)
    assert "subscription_plans" not in source
    assert "AGENT_MODELS" not in source


# ---------------------------------------------------------------------------
# Wave 2 (Plan 02) tests: cache-gating, idempotent re-run, dry-run no-writes
# ---------------------------------------------------------------------------

import json
from datetime import datetime


def _write_tmp_cache(tmp_path):
    """Write an isolated langfuse_cache.json copy with the real 8-prompt
    shape so each cache-gating/regeneration test doesn't repeat the JSON
    construction. Returns the Path to the written file."""
    cache_path = tmp_path / "langfuse_cache.json"
    cache_data = {
        "version": 1,
        "updated_at": None,
        "prompts": {name: content for name, content in _FALLBACKS.items()},
        "model_config": {},
    }
    cache_path.write_text(json.dumps(cache_data, indent=2))
    return cache_path


def _patch_model_config(monkeypatch):
    """Patch model_config singleton attributes so derive_model_config_dict()
    returns a deterministic, known dict across these tests."""
    fake_groq = MagicMock()
    fake_groq.model = "llama-3.3-70b-versatile"
    fake_flash = MagicMock()
    fake_flash._model_id = "models/gemini-flash-latest"
    fake_pro = MagicMock()
    fake_pro._model_id = "models/gemini-pro-latest"

    import app.core.model_config as model_config

    monkeypatch.setattr(model_config, "_groq_client", fake_groq)
    monkeypatch.setattr(model_config, "_gemini_flash_client", fake_flash)
    monkeypatch.setattr(model_config, "_gemini_pro_client", fake_pro)

    return {
        "free": "llama-3.3-70b-versatile",
        "plus": "models/gemini-flash-latest",
        "pro": "models/gemini-pro-latest",
    }


def test_cache_not_written_on_partial_failure(monkeypatch, tmp_path):
    """11-03-01 / SY-04, T-11-02: a single failed push leaves
    langfuse_cache.json byte-for-byte untouched and exits non-zero (D-10
    all-or-nothing cache-regeneration gate)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    expected_model_config = _patch_model_config(monkeypatch)
    cache_path = _write_tmp_cache(tmp_path)
    original_bytes = cache_path.read_bytes()

    from scripts import sync_langfuse

    monkeypatch.setattr(sync_langfuse, "CACHE_PATH", cache_path)

    FAILING_NAME = "nowry-quiz-from-deck"

    def fake_get_prompt(name, type="text"):
        if name == MODEL_CONFIG_NAME_FOR_TEST:
            return MagicMock(prompt="Model configuration", config=expected_model_config)
        if name == FAILING_NAME:
            raise Exception("network error fetching prompt")
        return MagicMock(prompt=_FALLBACKS[name], config=None)

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = fake_get_prompt

    with patch("scripts.sync_langfuse.Langfuse", MagicMock(return_value=fake_client), create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main([])

    assert exc_info.value.code != 0
    assert cache_path.read_bytes() == original_bytes


# Module-level constant mirrored here (avoid importing sync_langfuse at
# module load time — keeps RED-state import errors contained per-test).
MODEL_CONFIG_NAME_FOR_TEST = "nowry-model-config"


def test_cache_regeneration_shape(monkeypatch, tmp_path):
    """11-03-02 / SY-04: a fully successful run regenerates langfuse_cache.json
    with version, ISO-8601 updated_at, all 8 prompts, and a non-empty
    model_config matching prewarm()'s expected shape."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    expected_model_config = _patch_model_config(monkeypatch)
    cache_path = _write_tmp_cache(tmp_path)

    from scripts import sync_langfuse

    monkeypatch.setattr(sync_langfuse, "CACHE_PATH", cache_path)

    def fake_get_prompt(name, type="text"):
        if name == MODEL_CONFIG_NAME_FOR_TEST:
            return MagicMock(prompt="Model configuration", config=expected_model_config)
        return MagicMock(prompt=_FALLBACKS[name], config=None)

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = fake_get_prompt

    with patch("scripts.sync_langfuse.Langfuse", MagicMock(return_value=fake_client), create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main([])

    assert exc_info.value.code == 0

    data = json.loads(cache_path.read_text())
    assert data["version"] == 1
    assert data["updated_at"] is not None
    # Must parse as ISO-8601 (tolerate trailing 'Z')
    datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
    assert len(data["prompts"]) == 8
    for name in _FALLBACKS:
        assert name in data["prompts"]
    assert isinstance(data["model_config"], dict)
    assert data["model_config"] == expected_model_config


def test_idempotent_rerun_creates_no_versions(monkeypatch, tmp_path):
    """11-03-03 / SY-04: when remote content for all 8 prompts AND the model
    config already matches local, zero create_prompt calls occur — the
    SY-04 contract made testable end-to-end."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    expected_model_config = _patch_model_config(monkeypatch)
    cache_path = _write_tmp_cache(tmp_path)

    from scripts import sync_langfuse

    monkeypatch.setattr(sync_langfuse, "CACHE_PATH", cache_path)

    def fake_get_prompt(name, type="text"):
        if name == MODEL_CONFIG_NAME_FOR_TEST:
            return MagicMock(prompt="Model configuration", config=expected_model_config)
        return MagicMock(prompt=_FALLBACKS[name], config=None)

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = fake_get_prompt

    with patch("scripts.sync_langfuse.Langfuse", MagicMock(return_value=fake_client), create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main([])

    assert exc_info.value.code == 0
    assert fake_client.create_prompt.call_count == 0


def test_dry_run_makes_no_writes(monkeypatch, tmp_path, capsys):
    """11-04-01 / SY-01..04: --dry-run prefixes every status line with
    [DRY RUN], makes zero create_prompt calls and zero cache writes, and
    prints a differently-worded tally (would push / cache NOT regenerated)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    expected_model_config = _patch_model_config(monkeypatch)
    cache_path = _write_tmp_cache(tmp_path)
    original_bytes = cache_path.read_bytes()

    from scripts import sync_langfuse

    monkeypatch.setattr(sync_langfuse, "CACHE_PATH", cache_path)

    DIFFERENT_NAMES = {"nowry-cards-magic", "nowry-quiz-magic"}

    def fake_get_prompt(name, type="text"):
        if name == MODEL_CONFIG_NAME_FOR_TEST:
            return MagicMock(prompt="Model configuration", config=expected_model_config)
        if name in DIFFERENT_NAMES:
            return MagicMock(prompt="DIFFERENT CONTENT THAT DOES NOT MATCH LOCAL", config=None)
        return MagicMock(prompt=_FALLBACKS[name], config=None)

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = fake_get_prompt

    with patch("scripts.sync_langfuse.Langfuse", MagicMock(return_value=fake_client), create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main(["--dry-run"])

    assert exc_info.value.code == 0
    assert fake_client.create_prompt.call_count == 0
    assert cache_path.read_bytes() == original_bytes

    out = capsys.readouterr().out
    dry_run_lines = [line for line in out.splitlines() if "[DRY RUN]" in line]
    assert len(dry_run_lines) >= 2
    tally_lines = [line for line in out.splitlines() if "cache NOT regenerated" in line]
    assert len(tally_lines) == 1


# ---------------------------------------------------------------------------
# Gap-fix regression tests: first-sync bootstrap when get_prompt 404s
# (LangfuseNotFoundError) -- a 404 on the existence check must NOT be
# treated as a fatal error; it means the prompt/config simply hasn't been
# pushed to this Langfuse project yet and create_prompt() should run.
# ---------------------------------------------------------------------------

def _not_found_error(name):
    """Build a stand-in for the langfuse SDK's NotFoundError: any Exception
    with `.status_code == 404` set, matching the real SDK's ApiError base
    class shape (see _is_not_found_error's docstring for why duck-typing on
    status_code is used instead of isinstance against the real SDK class)."""
    exc = Exception(
        f"headers: {{}}, status_code: 404, body: {{'message': \"Prompt not found: '{name}' "
        f"with label 'production'\", 'error': 'LangfuseNotFoundError'}}"
    )
    exc.status_code = 404
    return exc


def test_compare_before_push_not_found_bootstraps_new_prompt(monkeypatch):
    """Gap-fix regression: a 404 'not found' on get_prompt (first-time sync
    -- the prompt has never been pushed to this Langfuse project) must NOT
    be reported as 'error'. compare_and_push_prompt should fall through to
    create_prompt (labeled 'production') and return 'pushed'."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    from scripts import sync_langfuse

    name = "nowry-cards-magic"
    local_content = _FALLBACKS[name]

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = _not_found_error(name)

    result = sync_langfuse.compare_and_push_prompt(
        fake_client, name, local_content, dry_run=False
    )

    assert result == "pushed"
    assert fake_client.create_prompt.call_count == 1
    _, kwargs = fake_client.create_prompt.call_args
    assert kwargs["name"] == name
    assert kwargs["prompt"] == local_content
    assert kwargs["labels"] == ["production"]


def test_compare_before_push_not_found_dry_run_does_not_push(monkeypatch):
    """Gap-fix regression: in --dry-run, a 404 'not found' is still reported
    as 'pushed' (so the dry-run tally reflects the bootstrap that WOULD
    happen) but create_prompt must NOT actually be called."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    from scripts import sync_langfuse

    name = "nowry-cards-magic"
    local_content = _FALLBACKS[name]

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = _not_found_error(name)

    result = sync_langfuse.compare_and_push_prompt(
        fake_client, name, local_content, dry_run=True
    )

    assert result == "pushed"
    assert fake_client.create_prompt.call_count == 0


def test_compare_before_push_genuine_error_still_errors(monkeypatch):
    """A non-404 exception (network error, 401, 5xx, etc.) from get_prompt
    must still be reported as 'error' and must NOT trigger create_prompt --
    only a 404 'not found' is treated as a bootstrap case."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    from scripts import sync_langfuse

    name = "nowry-cards-magic"
    local_content = _FALLBACKS[name]

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = Exception("network error: connection refused")

    result = sync_langfuse.compare_and_push_prompt(
        fake_client, name, local_content, dry_run=False
    )

    assert result == "error"
    assert fake_client.create_prompt.call_count == 0


def test_model_config_not_found_bootstraps_new_config(monkeypatch, tmp_path):
    """Gap-fix regression (nowry-model-config block in main()): a 404 'not
    found' on get_prompt(MODEL_CONFIG_NAME) is the same first-sync bootstrap
    case -- main() must push the derived model config via create_prompt,
    NOT increment failed_count, and (since this is the only prompt and it's
    the success path) regenerate the cache."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    expected_model_config = _patch_model_config(monkeypatch)
    cache_path = _write_tmp_cache(tmp_path)

    from scripts import sync_langfuse

    monkeypatch.setattr(sync_langfuse, "CACHE_PATH", cache_path)

    def fake_get_prompt(name, type="text"):
        if name == MODEL_CONFIG_NAME_FOR_TEST:
            raise _not_found_error(name)
        return MagicMock(prompt=_FALLBACKS[name], config=None)

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = fake_get_prompt

    with patch("scripts.sync_langfuse.Langfuse", MagicMock(return_value=fake_client), create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main([])

    assert exc_info.value.code == 0

    create_calls = [
        call for call in fake_client.create_prompt.call_args_list
        if call.kwargs.get("name") == MODEL_CONFIG_NAME_FOR_TEST
    ]
    assert len(create_calls) == 1
    assert create_calls[0].kwargs["config"] == expected_model_config
    assert create_calls[0].kwargs["labels"] == ["production"]

    data = json.loads(cache_path.read_text())
    assert data["model_config"] == expected_model_config


def test_dry_run_summary_reports_failures(monkeypatch, tmp_path, capsys):
    """Gap-fix regression: a GENUINE (non-404) fetch error during --dry-run
    must surface in the dry-run summary's failed count and cause a non-zero
    exit -- dry-run must never silently report 'Would push: 0, unchanged: 0'
    while hiding real fetch failures."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    expected_model_config = _patch_model_config(monkeypatch)
    cache_path = _write_tmp_cache(tmp_path)

    from scripts import sync_langfuse

    monkeypatch.setattr(sync_langfuse, "CACHE_PATH", cache_path)

    FAILING_NAME = "nowry-quiz-from-deck"

    def fake_get_prompt(name, type="text"):
        if name == MODEL_CONFIG_NAME_FOR_TEST:
            return MagicMock(prompt="Model configuration", config=expected_model_config)
        if name == FAILING_NAME:
            raise Exception("network error: connection refused")
        return MagicMock(prompt=_FALLBACKS[name], config=None)

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = fake_get_prompt

    with patch("scripts.sync_langfuse.Langfuse", MagicMock(return_value=fake_client), create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main(["--dry-run"])

    assert exc_info.value.code != 0
    assert fake_client.create_prompt.call_count == 0

    out = capsys.readouterr().out
    tally_lines = [line for line in out.splitlines() if "Would push" in line]
    assert len(tally_lines) == 1
    assert "failed: 1" in tally_lines[0]


def test_dry_run_first_sync_all_not_found_reports_pushed(monkeypatch, tmp_path, capsys):
    """End-to-end regression for the originally reported bug: on a fresh
    Langfuse project where NONE of the 9 sync targets exist yet, --dry-run
    must report them all as 'would push' (bootstrap), with zero failures
    and exit 0 -- NOT 'Would push: 0, unchanged: 0' while every fetch 404s."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    _patch_model_config(monkeypatch)
    cache_path = _write_tmp_cache(tmp_path)

    from scripts import sync_langfuse

    monkeypatch.setattr(sync_langfuse, "CACHE_PATH", cache_path)

    def fake_get_prompt(name, type="text"):
        raise _not_found_error(name)

    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = fake_get_prompt

    with patch("scripts.sync_langfuse.Langfuse", MagicMock(return_value=fake_client), create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main(["--dry-run"])

    assert exc_info.value.code == 0
    assert fake_client.create_prompt.call_count == 0

    out = capsys.readouterr().out
    tally_lines = [line for line in out.splitlines() if "Would push" in line]
    assert len(tally_lines) == 1
    assert f"Would push: {len(_FALLBACKS)}" in tally_lines[0]
    assert "failed: 0" in tally_lines[0]
