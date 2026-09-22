from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_database() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _run_postgresql_migrations()
    _upgrade_company_status_schema(
        models.Company,
        models.ALL_STATUSES,
        models.REMOVED_COMPANY_STATUSES,
    )
    _upgrade_discovery_cursor_schema(models.DiscoveryCursor)
    _upgrade_search_run_schema()
    _upgrade_imap_uidvalidity_schema()
    _upgrade_sqlite_sender_providers()
    _upgrade_email_content_schema()


def _upgrade_email_content_schema() -> None:
    """Add content fields without changing saved text, campaigns or deliveries."""
    additions = {
        "email_templates": {
            "body_format": "VARCHAR(10) NOT NULL DEFAULT 'text'",
            "html_template": "TEXT NOT NULL DEFAULT ''",
            "attachment_ids": "JSON NOT NULL DEFAULT '[]'",
        },
        "outreach_campaigns": {"attachment_ids": "JSON NOT NULL DEFAULT '[]'"},
        "outreach_deliveries": {"html_body": "TEXT"},
    }
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(701337, 20260921)")
        tables = set(inspect(connection).get_table_names())
        for table, fields in additions.items():
            if table not in tables:
                continue
            columns = {c["name"] for c in inspect(connection).get_columns(table)}
            for name, definition in fields.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _upgrade_sqlite_sender_providers() -> None:
    """Expand the provider check, retaining all columns, indexes and child FKs."""
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy.schema import CreateTable
    from app.models import SenderAccount

    with engine.connect() as connection:
        checks = inspect(connection).get_check_constraints("sender_accounts")
        provider_check = next((c for c in checks if c.get("name") == "ck_sender_accounts_provider"), None)
        if provider_check and all(p in provider_check["sqltext"] for p in ("gmail_smtp", "yandex_smtp")):
            return
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            # Explicit BEGIN keeps DDL and the data copy atomic in sqlite3.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            indexes = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE tbl_name='sender_accounts' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL"
            ).scalars().all()
            ddl = str(CreateTable(SenderAccount.__table__).compile(connection))
            connection.exec_driver_sql(ddl.replace("CREATE TABLE sender_accounts", "CREATE TABLE sender_accounts_upgrade", 1))
            quote = connection.dialect.identifier_preparer.quote
            columns = ", ".join(quote(c["name"]) for c in inspect(connection).get_columns("sender_accounts"))
            connection.exec_driver_sql(f"INSERT INTO sender_accounts_upgrade ({columns}) SELECT {columns} FROM sender_accounts")
            connection.exec_driver_sql("DROP TABLE sender_accounts")
            connection.exec_driver_sql("ALTER TABLE sender_accounts_upgrade RENAME TO sender_accounts")
            for statement in indexes:
                connection.exec_driver_sql(statement)
            if connection.exec_driver_sql("PRAGMA foreign_key_check(sender_accounts)").all():
                raise RuntimeError("Sender provider migration failed foreign-key validation")
            connection.commit()
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.exec_driver_sql(f"PRAGMA foreign_keys={int(foreign_keys)}")
            connection.commit()


def _upgrade_imap_uidvalidity_schema() -> None:
    """Keep existing local SQLite mailboxes usable after the additive upgrade."""
    if engine.dialect.name == "postgresql":
        return  # The versioned migration is serialized with backend/worker startup.
    with engine.begin() as connection:
        inspector = inspect(connection)
        if "sender_accounts" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("sender_accounts")}
        if "imap_uidvalidity" not in columns:
            connection.exec_driver_sql("ALTER TABLE sender_accounts ADD COLUMN imap_uidvalidity BIGINT")


def _upgrade_search_run_schema() -> None:
    """Preserve historical batch runs while adding observable full searches."""
    additions = {
        "search_scope": "VARCHAR(20) NOT NULL DEFAULT 'batch'",
        "cancel_requested": "BOOLEAN NOT NULL DEFAULT FALSE",
        "active_provider": "VARCHAR(20)",
        "search_requests": "INTEGER NOT NULL DEFAULT 0",
        "company_requests": "INTEGER NOT NULL DEFAULT 0",
        "progress_message": "TEXT",
        "provider_results": "JSON NOT NULL DEFAULT '{}'",
        "skipped_known": "INTEGER NOT NULL DEFAULT 0",
        "skipped_region": "INTEGER NOT NULL DEFAULT 0",
        "skipped_unknown_region": "INTEGER NOT NULL DEFAULT 0",
    }
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(701337, 20260904)")
        columns = {column["name"] for column in inspect(connection).get_columns("search_runs")}
        for name, definition in additions.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE search_runs ADD COLUMN {name} {definition}")


def _run_postgresql_migrations() -> None:
    """Apply the idempotent Mail.ru scheduler migration on PostgreSQL.

    SQLite test databases are created directly from current metadata. Production
    PostgreSQL keeps an explicit migration ledger and upgrades existing Gmail-era
    campaign/delivery rows in place.
    """
    if engine.dialect.name != "postgresql":
        return
    migration_path = (
        Path(__file__).resolve().parent
        / "migrations"
        / "20260903_mailru_scheduler.sql"
    )
    migration_sql = migration_path.read_text(encoding="utf-8")
    with engine.begin() as connection:
        # Backend and the standalone IMAP process can start together. A
        # transaction-scoped advisory lock serializes their schema upgrade.
        connection.exec_driver_sql(
            "SELECT pg_advisory_xact_lock(701337, 20260903)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(100) PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = connection.execute(
            text(
                "SELECT 1 FROM schema_migrations "
                "WHERE version = '20260903_mailru_scheduler'"
            )
        ).scalar()
        if not applied:
            connection.exec_driver_sql(migration_sql)
        health_version = "20260910_mail_health"
        if not connection.execute(text("SELECT 1 FROM schema_migrations WHERE version = :version"), {"version": health_version}).scalar():
            connection.exec_driver_sql(migration_path.with_name(f"{health_version}.sql").read_text(encoding="utf-8"))
        uidvalidity_version = "20260911_imap_uidvalidity"
        if not connection.execute(text("SELECT 1 FROM schema_migrations WHERE version = :version"), {"version": uidvalidity_version}).scalar():
            connection.exec_driver_sql(migration_path.with_name(f"{uidvalidity_version}.sql").read_text(encoding="utf-8"))
        providers_version = "20260914_mail_providers"
        if not connection.execute(text("SELECT 1 FROM schema_migrations WHERE version = :version"), {"version": providers_version}).scalar():
            connection.exec_driver_sql(migration_path.with_name(f"{providers_version}.sql").read_text(encoding="utf-8"))


def _upgrade_company_status_schema(company_model, statuses, removed_statuses) -> None:
    """Replace legacy lead statuses without losing existing companies.

    FuelLead has no versioned migration runner yet. PostgreSQL can replace the
    named check constraint in place; SQLite needs a table rebuild because it
    cannot drop check constraints. Removed status values are folded into
    ``new`` so the simplified pipeline has no inaccessible rows; delivery
    errors remain visible as ``error``.
    """
    with engine.connect() as connection:
        inspector = inspect(connection)
        if "companies" not in inspector.get_table_names():
            return
        checks = inspector.get_check_constraints("companies")
        status_check = next(
            (item for item in checks if item.get("name") == "ck_companies_status"),
            None,
        )
        definition = str((status_check or {}).get("sqltext") or "").lower()
        is_current = all(status in definition for status in statuses) and not any(
            status in definition for status in removed_statuses
        )
        dialect = connection.dialect.name

    if is_current:
        return
    if dialect == "sqlite":
        _rebuild_sqlite_companies(company_model, removed_statuses)
        return

    allowed_values = ",".join(f"'{status}'" for status in statuses)
    removed_values = ",".join(f"'{status}'" for status in removed_statuses)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE companies SET status = 'new' "
                f"WHERE status IN ({removed_values})"
            )
        )
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE companies "
                    "DROP CONSTRAINT IF EXISTS ck_companies_status"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE companies ADD CONSTRAINT ck_companies_status "
                    f"CHECK (status IN ({allowed_values}))"
                )
            )


def _rebuild_sqlite_companies(company_model, removed_statuses) -> None:
    """Rebuild only the SQLite companies table while preserving child FKs."""
    removed_values = ",".join(f"'{status}'" for status in removed_statuses)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()
        try:
            with connection.begin():
                indexes = inspect(connection).get_indexes("companies")
                connection.execute(
                    text(
                        f"UPDATE companies SET status = 'new' "
                        f"WHERE status IN ({removed_values})"
                    )
                )
                connection.execute(
                    text("ALTER TABLE companies RENAME TO companies_legacy")
                )
                quote = connection.dialect.identifier_preparer.quote
                for index in indexes:
                    if index.get("name"):
                        connection.execute(
                            text(f"DROP INDEX IF EXISTS {quote(index['name'])}")
                        )
                company_model.__table__.create(bind=connection, checkfirst=False)
                columns = ", ".join(
                    quote(column.name) for column in company_model.__table__.columns
                )
                connection.execute(
                    text(
                        f"INSERT INTO companies ({columns}) "
                        f"SELECT {columns} FROM companies_legacy"
                    )
                )
                connection.execute(text("DROP TABLE companies_legacy"))
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def _upgrade_discovery_cursor_schema(cursor_model) -> None:
    """Add provider-scoped cursors to databases created before combined discovery.

    FuelLead currently has no versioned migration system. The old table used
    (okved_code, region_code) as its unique key, so PostgreSQL can be upgraded
    in place while SQLite needs a small table rebuild to remove that constraint.
    Existing rows belong to the historical Checko search and keep their place.
    """
    with engine.begin() as connection:
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("discovery_cursors")}
        constraints = {
            constraint.get("name")
            for constraint in inspector.get_unique_constraints("discovery_cursors")
        }
        has_provider = "provider" in columns
        has_provider_constraint = "uq_discovery_cursor_provider_query" in constraints

        if has_provider and has_provider_constraint:
            return

        if connection.dialect.name == "sqlite":
            connection.execute(
                text("ALTER TABLE discovery_cursors RENAME TO discovery_cursors_legacy")
            )
            cursor_model.__table__.create(bind=connection, checkfirst=False)
            provider_expression = "provider" if has_provider else "'checko'"
            connection.execute(
                text(
                    """
                    INSERT INTO discovery_cursors
                        (id, provider, okved_code, region_code, next_page,
                         next_record_index, page_size, completed_cycles, updated_at)
                    SELECT id, {provider_expression}, okved_code, region_code, next_page,
                           next_record_index, page_size, completed_cycles, updated_at
                    FROM discovery_cursors_legacy
                    """.format(provider_expression=provider_expression)
                )
            )
            connection.execute(text("DROP TABLE discovery_cursors_legacy"))
            return

        if not has_provider:
            connection.execute(
                text("ALTER TABLE discovery_cursors ADD COLUMN provider VARCHAR(20)")
            )
            connection.execute(
                text("UPDATE discovery_cursors SET provider = 'checko' WHERE provider IS NULL")
            )

        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE discovery_cursors "
                    "DROP CONSTRAINT IF EXISTS uq_discovery_cursor_query"
                )
            )
            if not has_provider_constraint:
                connection.execute(
                    text(
                        "ALTER TABLE discovery_cursors ADD CONSTRAINT "
                        "uq_discovery_cursor_provider_query UNIQUE "
                        "(provider, okved_code, region_code)"
                    )
                )
