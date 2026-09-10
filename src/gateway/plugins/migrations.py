"""Run a plugin's Alembic chain against its own version table.

Otari's chain stamps ``alembic_version``; a plugin's stamps
``alembic_version_<name>``. Keeping them apart is what lets the two chains be
upgraded independently and what keeps the core ``env.py``'s foreign-history
check (``alembic/env.py``) from mistaking a plugin's head for another
application's.
"""

import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, pool

from gateway.core.database import to_sync_url
from gateway.log_config import logger

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import MetaData

    from gateway.plugins.registry import LoadedPlugin


def _alembic_config(database_url: str, script_location: "Path") -> Config:
    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(script_location))
    alembic_cfg.set_main_option("sqlalchemy.url", to_sync_url(database_url))
    alembic_cfg.attributes["configure_logger"] = False
    return alembic_cfg


def run_plugin_migrations(database_url: str, plugins: Iterable["LoadedPlugin"], revision: str = "head") -> None:
    """Upgrade every migration directory the loaded plugins registered."""
    for plugin in plugins:
        if plugin.status != "loaded":
            continue
        for script_location in plugin.migrations:
            logger.info("Running migrations for plugin %s from %s", plugin.name, script_location)
            command.upgrade(_alembic_config(database_url, script_location), revision)


def run_plugin_env(context: Any, target_metadata: "MetaData | None", plugin_name: str) -> None:
    """The body of a plugin's ``env.py``.

    A plugin's ``alembic/env.py`` is two lines::

        from gateway.plugins.migrations import run_plugin_env
        run_plugin_env(context, Base.metadata, "agent-gates")

    It reads the database URL the gateway set (or ``OTARI_DATABASE_URL`` for a
    bare ``alembic`` run, for autogenerate), stamps the plugin's own version
    table, and renders in batch mode so an ``ALTER`` works on SQLite too.
    """
    config = context.config
    database_url = config.get_main_option("sqlalchemy.url") or os.getenv("OTARI_DATABASE_URL")
    if not database_url:
        msg = (
            f"No database URL to migrate plugin {plugin_name!r}. Set OTARI_DATABASE_URL, or go through `otari migrate`."
        )
        raise RuntimeError(msg)
    version_table = f"alembic_version_{plugin_name.replace('-', '_')}"
    url = to_sync_url(database_url)

    if context.is_offline_mode():
        context.configure(
            url=url,
            target_metadata=target_metadata,
            version_table=version_table,
            literal_binds=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                version_table=version_table,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
