"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application


def _apply_migrations_best_effort() -> None:
    """
    Apply pending migrations at process start.

    This repository runs against a shared canonical SQLite file. If the DB file is
    restored/seeded without Django migrations being applied, create/update endpoints
    can fail with 500s due to missing columns. Running migrations here ensures the
    schema is aligned with the code (no-op when already up-to-date).

    We intentionally swallow errors to avoid preventing the app from starting in
    environments where migrations are managed externally; in that case, the error
    will surface normally during DB access and can be addressed operationally.
    """
    try:
        from django.core.management import call_command

        call_command("migrate", interactive=False, verbosity=0)
    except Exception:
        # Best effort only; avoid crashing the WSGI app at import-time.
        pass


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

_apply_migrations_best_effort()
application = get_wsgi_application()
