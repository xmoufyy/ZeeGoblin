"""Programmatic Alembic upgrade with an automatic pre-migration backup."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from config.settings import PROJECT_ROOT, sqlite_path, sync_database_url

log = logging.getLogger(__name__)


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", sync_database_url(database_url))
    config.attributes["configure_logger"] = False
    return config


def _current_revision(sync_url: str) -> str | None:
    engine = create_engine(sync_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def backup_sqlite(db_file: Path, backup_dir: Path, label: str) -> Path:
    """Consistent online backup via SQLite's backup API (safe even in WAL mode)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{db_file.stem}-{stamp}-{label}.db"
    source = sqlite3.connect(db_file)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def upgrade_to_head(database_url: str, backup_dir: Path | None = None) -> None:
    config = alembic_config(database_url)
    sync_url = sync_database_url(database_url)
    db_file = sqlite_path(database_url)
    if db_file is not None:
        db_file.parent.mkdir(parents=True, exist_ok=True)

    head = ScriptDirectory.from_config(config).get_current_head()
    current = _current_revision(sync_url)
    if current == head:
        log.info("Database schema is up to date (revision %s)", head)
        return

    if db_file is not None and db_file.exists() and current is not None and backup_dir:
        target = backup_sqlite(db_file, backup_dir, f"pre-{head}")
        log.info("Backed up database to %s before migrating", target)

    log.info("Migrating database from %s to %s", current or "empty", head)
    command.upgrade(config, "head")
