"""
One-time data migrations.

Each module here is a standalone, idempotent script — none are wired into app
startup (``app/main.py`` lifespan only builds indexes). Run one manually from
the ``Nowry-API`` directory, for example:

    .venv/bin/python -m app.migrations.normalize_interests_taxonomy          # dry run
    .venv/bin/python -m app.migrations.normalize_interests_taxonomy --apply  # write

Every migration must be safe to re-run and must page through collections with
bounded ``.to_list(length=N)`` queries.
"""
