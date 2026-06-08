"""
Sync Langfuse Script

Pushes all 8 prompts (from app.core.prompt_manager._FALLBACKS) and the
tier->model mapping (derived live from app.core.model_config) to Langfuse
with the `production` label, then regenerates app/config/langfuse_cache.json.

Idempotent via compare-before-push (D-05): fetches the current production
version, compares to the codebase value, and only pushes when different.

Usage (run from Nowry-API/):
    python scripts/sync_langfuse.py              # push for real
    python scripts/sync_langfuse.py --dry-run    # preview only, writes nothing
"""
import os
import sys
import argparse
import logging

from dotenv import load_dotenv

from app.core.prompt_manager import _FALLBACKS

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)

PRODUCTION_LABEL = "production"
MODEL_CONFIG_NAME = "nowry-model-config"
DEFAULT_LANGFUSE_URL = "https://cloud.langfuse.com"

# Hoisted to module level (guarded by try/except) so that
# patch("scripts.sync_langfuse.Langfuse", ..., create=True) resolves to a
# module attribute the tests can replace before _build_client() runs. If the
# real langfuse SDK is unavailable at import time (e.g. test runners that stub
# it via sys.modules), this stays None and _build_client() raises a clear
# error rather than an AttributeError on a missing name.
try:
    from langfuse import Langfuse
except ImportError:  # pragma: no cover - exercised only when SDK truly absent
    Langfuse = None


def _validate_credentials():
    """Fail fast (exit 1) if LANGFUSE_SECRET_KEY/LANGFUSE_PUBLIC_KEY are missing.
    Mirrors check_models.py's fail-fast pattern -- runs BEFORE any Langfuse
    client construction or API call (Pitfall 5: never spend time on setup
    work before validating credentials). T-11-01: never print/log the secret
    values themselves -- only their presence/absence (boolean state)."""
    secret = os.getenv("LANGFUSE_SECRET_KEY")
    public = os.getenv("LANGFUSE_PUBLIC_KEY")
    if not secret or not public:
        print("ERROR: LANGFUSE_SECRET_KEY or LANGFUSE_PUBLIC_KEY not set in .env")
        sys.exit(1)
    return secret, public


def _validate_base_url():
    """T-9-03 mitigation reuse: reject non-https LANGFUSE_BASE_URL, fall back
    to https://cloud.langfuse.com with a WARNING (mirrors langfuse_client.py
    lines 38-49 -- never connect to a plaintext endpoint -- T-11-02)."""
    raw = os.getenv("LANGFUSE_BASE_URL", DEFAULT_LANGFUSE_URL)
    if not raw.startswith("https://"):
        logger.warning(
            "LANGFUSE_BASE_URL '%s' does not start with https:// -- falling back to '%s'.",
            raw, DEFAULT_LANGFUSE_URL,
        )
        return DEFAULT_LANGFUSE_URL
    return raw


def _build_client(secret, public, base_url):
    """Construct a Langfuse client directly from env vars -- standalone-script
    pattern that avoids importing app.core.langfuse_client (which triggers
    FastAPI-adjacent module init, per 11-RESEARCH.md Pattern 2). `Langfuse` is
    resolved from this module's namespace so tests can patch
    scripts.sync_langfuse.Langfuse with a fake constructor."""
    if Langfuse is None:
        print("ERROR: langfuse SDK is not installed -- cannot build client.")
        sys.exit(1)
    return Langfuse(secret_key=secret, public_key=public, base_url=base_url)


def compare_and_push_prompt(client, name, local_content, dry_run=False):
    """Compare-before-push for a single prompt (D-05, makes SY-04 true by
    construction). Returns one of: "pushed", "unchanged", "error".
    NEVER calls create_prompt when dry_run=True or content is unchanged --
    this is what makes re-runs idempotent and dry-run side-effect-free.
    T-11-03: only the caught exception's string representation is surfaced
    (no raw tracebacks) -- the Langfuse SDK's str(exc) does not leak credentials."""
    try:
        current = client.get_prompt(name, type="text")
        remote_content = current.prompt
    except Exception as exc:
        print(f"ERROR fetching {name}: {exc}")
        return "error"

    if remote_content.strip() == local_content.strip():
        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}{name}: unchanged -- skipped")
        return "unchanged"

    if dry_run:
        print(f"[DRY RUN] {name}: content changed -- would push new version")
        return "pushed"

    try:
        client.create_prompt(
            name=name,
            type="text",
            prompt=local_content,
            labels=[PRODUCTION_LABEL],
        )
        print(f"{name}: content changed -- pushing new version")
        return "pushed"
    except Exception as exc:
        print(f"ERROR pushing {name}: {exc}")
        return "error"


def derive_model_config_dict():
    """Derive {free, plus, pro} -> model identifier from model_config.py's
    LIVE client wiring (D-07). Reads singleton attributes directly --
    NEVER hardcodes values and NEVER reads the Smart Pet agent's separate
    tier->model map (a different system -- see Pitfall 4 in 11-RESEARCH.md).
    This guarantees nowry-model-config can never drift from what the
    running app actually does."""
    import app.core.model_config as model_config

    result = {}
    if model_config._groq_client is not None:
        result["free"] = model_config._groq_client.model
    if model_config._gemini_flash_client is not None:
        result["plus"] = model_config._gemini_flash_client._model_id
    if model_config._gemini_pro_client is not None:
        result["pro"] = model_config._gemini_pro_client._model_id
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync prompts and model config to Langfuse.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to Langfuse or the local cache.")
    args = parser.parse_args(argv)

    secret, public = _validate_credentials()
    base_url = _validate_base_url()
    client = _build_client(secret, public, base_url)

    # NOTE: prompt loop, model-config push, cache regeneration, final tally,
    # and exit-code wiring are completed in Plan 02 (11-02-PLAN.md). This
    # function and the helpers above are the stable contract Plan 02 builds on.
    return client, args


if __name__ == "__main__":
    main()
