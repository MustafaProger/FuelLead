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
    _upgrade_discovery_cursor_schema(models.DiscoveryCursor)


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
