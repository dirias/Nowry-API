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
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

# This script lives in Nowry-API/scripts/ but imports the app.* package that
# lives in Nowry-API/. Unlike root-level standalone scripts (check_models.py,
# fix_all_public_books.py) where `python script.py` naturally puts Nowry-API/
# on sys.path[0], a script in a subdirectory only gets scripts/ added --
# `app` would not be importable without this. Prepend the repo root (parent
# of this file's directory) so `python scripts/sync_langfuse.py` works from
# Nowry-API/ exactly as documented (D-03), matching pytest's rootdir-based
# resolution that already makes `from app.core...` work in the test suite.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv

from app.core.prompt_manager import _FALLBACKS

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)

PRODUCTION_LABEL = "production"
MODEL_CONFIG_NAME = "nowry-model-config"
DEFAULT_LANGFUSE_URL = "https://cloud.langfuse.com"

# Full-rewrite target for regenerate_cache() -- mirrors prompt_manager.prewarm()'s
# write-through path (Path(__file__).parent.parent / "config" / "langfuse_cache.json"
# from app/core/, which resolves to the same app/config/ directory when computed
# relative to this script's location in scripts/).
CACHE_PATH = Path(__file__).resolve().parent.parent / "app" / "config" / "langfuse_cache.json"

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
    running app actually does.

    T-11-06 mitigation: the attribute access below reaches into private,
    implementation-detail attributes (`.model`, `._model_id`) on singleton
    client objects. Wrapped in try/except so a future client refactor that
    renames/removes these surfaces a clean error message (matching this
    script's established clean-error-reporting pattern) instead of a raw
    AttributeError traceback mid-run."""
    import app.core.model_config as model_config

    result = {}
    try:
        if model_config._groq_client is not None:
            result["free"] = model_config._groq_client.model
        if model_config._gemini_flash_client is not None:
            result["plus"] = model_config._gemini_flash_client._model_id
        if model_config._gemini_pro_client is not None:
            result["pro"] = model_config._gemini_pro_client._model_id
    except AttributeError as exc:
        print(f"ERROR: could not derive model config from live client wiring: {exc}")
        sys.exit(1)
    return result


def regenerate_cache(prompts_dict, model_config_dict, cache_path):
    """Full rewrite of langfuse_cache.json -- mirrors prompt_manager.prewarm()'s
    write-through shape (lines 141-157) so prewarm() reads it identically.
    Sets updated_at to an ISO-8601 UTC timestamp (this script OWNS that field --
    Phase 10 explicitly left it None with a comment naming this phase).

    T-11-02 mitigation (D-10): this is the ONLY call site of regenerate_cache()
    in the entire module, reached exclusively from the `if failed_count == 0`
    branch in main() -- structurally impossible to write a half-synced cache.

    T-11-07 mitigation (CR-02): unlike prewarm()'s write-through (which
    tolerates a failed cache write by logging a warning and continuing on
    its in-memory fallbacks), a crash here happens AFTER prompts have
    already been pushed to Langfuse production -- so we (a) wrap the
    read/write in error handling that fails loudly with a clear message
    instead of an uncaught traceback, and (b) write atomically via a
    temp-file-then-rename so a mid-write crash (Ctrl-C, OOM, disk full)
    can never leave langfuse_cache.json as a zero-byte or partial-JSON
    file -- the existing cache is only ever replaced by `os.replace`,
    which is atomic on POSIX."""
    try:
        with open(cache_path) as f:
            cache_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read {cache_path} for regeneration: {exc}")
        sys.exit(1)

    cache_data["prompts"] = dict(prompts_dict)
    cache_data["model_config"] = dict(model_config_dict)
    cache_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(cache_data, f, indent=2)
        os.replace(tmp_path, cache_path)  # atomic on POSIX -- no partial-write window
    except OSError as exc:
        print(f"ERROR: failed to write regenerated cache to {cache_path}: {exc}")
        sys.exit(1)


def _config_dicts_equal(a, b):
    """Order-independent structural comparison via sorted-key JSON serialization
    (Pitfall 6 -- avoid naive `==` on dicts with possible key-order/nesting
    differences causing false 'changed' positives)."""
    return json.dumps(a or {}, sort_keys=True) == json.dumps(b or {}, sort_keys=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync prompts and model config to Langfuse.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to Langfuse or the local cache.")
    args = parser.parse_args(argv)
    dry_run = args.dry_run

    secret, public = _validate_credentials()
    base_url = _validate_base_url()
    client = _build_client(secret, public, base_url)

    pushed_count = 0
    unchanged_count = 0
    failed_count = 0
    synced_prompts = {}

    # --- 8 prompts (D-05 compare-before-push, reusing Plan 01's core) ---
    for name, local_content in _FALLBACKS.items():
        status = compare_and_push_prompt(client, name, local_content, dry_run=dry_run)
        synced_prompts[name] = local_content
        if status == "pushed":
            pushed_count += 1
        elif status == "unchanged":
            unchanged_count += 1
        else:
            failed_count += 1

    # --- nowry-model-config (D-07 derivation + D-05 compare-before-push) ---
    local_model_config = derive_model_config_dict()

    # T-11-06 mitigation (CR-01): a partial/empty derived config must never be
    # trusted as a push/cache candidate -- it would silently overwrite a correct
    # production `nowry-model-config` and get baked into the cold-start cache.
    # Fail loudly and refuse to push or regenerate the cache if any tier is
    # missing (e.g. GROQ_API_KEY / GEMINI_API_KEY absent from this script's env).
    EXPECTED_TIERS = {"free", "plus", "pro"}
    missing_tiers = EXPECTED_TIERS - local_model_config.keys()
    if missing_tiers:
        print(
            f"ERROR: derived model config is missing tier(s) {sorted(missing_tiers)} -- "
            f"refusing to push or cache an incomplete config. Check that "
            f"GROQ_API_KEY/GEMINI_API_KEY are set in this script's environment."
        )
        sys.exit(1)

    model_config_status = "unchanged"
    try:
        current_cfg = client.get_prompt(MODEL_CONFIG_NAME, type="text")
        remote_model_config = getattr(current_cfg, "config", None)
    except Exception as exc:
        print(f"ERROR fetching {MODEL_CONFIG_NAME}: {exc}")
        remote_model_config = None
        failed_count += 1
        model_config_status = "error"

    if model_config_status != "error":
        if _config_dicts_equal(remote_model_config, local_model_config):
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"{prefix}{MODEL_CONFIG_NAME}: unchanged -- skipped")
            model_config_status = "unchanged"
        else:
            if dry_run:
                print(f"[DRY RUN] {MODEL_CONFIG_NAME}: content changed -- would push new version")
                model_config_status = "changed"
            else:
                try:
                    client.create_prompt(
                        name=MODEL_CONFIG_NAME,
                        type="text",
                        prompt="Model configuration",
                        config=local_model_config,
                        labels=[PRODUCTION_LABEL],
                    )
                    print(f"{MODEL_CONFIG_NAME}: content changed -- pushing new version")
                    model_config_status = "changed"
                except Exception as exc:
                    print(f"ERROR pushing {MODEL_CONFIG_NAME}: {exc}")
                    failed_count += 1
                    model_config_status = "error"

    # --- dry-run: report and exit, never write anything (T-11-05 mitigation) ---
    if dry_run:
        cfg_word = "would update" if model_config_status == "changed" else "unchanged"
        print(
            f"[DRY RUN] Would push: {pushed_count}, unchanged: {unchanged_count}, "
            f"model config {cfg_word}, cache NOT regenerated"
        )
        sys.exit(0)

    # --- gate cache regeneration on FULL success (D-10 / T-11-02) ---
    if failed_count == 0:
        regenerate_cache(synced_prompts, local_model_config, CACHE_PATH)
        cfg_word = "updated" if model_config_status == "changed" else "unchanged"
        print(
            f"Done: {pushed_count} pushed, {unchanged_count} unchanged, "
            f"model config {cfg_word}, cache regenerated -> app/config/langfuse_cache.json"
        )
        sys.exit(0)
    else:
        print(f"ERROR: {failed_count} push(es) failed. Cache NOT regenerated. Re-run to retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
