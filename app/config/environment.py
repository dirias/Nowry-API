# app/config/environment.py
"""Single source of truth for "are we running in production" checks.

Prior to this module, `app/config/auth_config.py` read `ENV` and
`app/main.py` read `ENVIRONMENT` — two different env vars gating the same
decision. If an operator set one but not the other, cookies could end up
non-secure in production while Sentry believed it was in production (or vice
versa).

`ENV` is the canonical variable name: it is the one actually set in the
project's deploy configs (`Nowry-API/docker-compose.yml` and
`Nowry-API/app/docker-compose.yml` both set `ENV=local`); `ENVIRONMENT` was
not set anywhere in deploy config. All environment-gated decisions must go
through `get_environment()` / `is_production()` below rather than reading
`ENV` or `ENVIRONMENT` directly.
"""
import os

_PRODUCTION = "production"
_DEFAULT_ENVIRONMENT = "development"


def get_environment() -> str:
    """Return the raw environment name from the `ENV` env var.

    Defaults to "development" when unset.
    """
    return os.getenv("ENV", _DEFAULT_ENVIRONMENT).strip().lower()


def is_production() -> bool:
    """Return True when the app is running in the production environment."""
    return get_environment() == _PRODUCTION
