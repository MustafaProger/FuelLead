from collections.abc import Generator

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
    _upgrade_company_status_schema(
        models.Company,
        models.ALL_STATUSES,
        models.REMOVED_COMPANY_STATUSES,
    )
    _upgrade_discovery_cursor_schema(models.DiscoveryCursor)


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
