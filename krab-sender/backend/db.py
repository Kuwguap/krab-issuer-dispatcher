from datetime import datetime, timezone
from pathlib import Path
import os

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    inspect,
    String,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker


DB_PATH = Path(__file__).resolve().parent.parent / "krab_sender.db"

# Prefer DATABASE_URL (e.g. Supabase Postgres) if provided; otherwise fall back to local SQLite.
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    # If using plain "postgresql://" DSN (e.g. from Supabase), tell SQLAlchemy
    # to use the modern psycopg3 driver by default.
    if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

    engine = create_engine(DATABASE_URL)
else:
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


class TransactionORM(Base):
    """
    Database representation of a Transmission event.

    Mirrors the logical Transaction model and the roadmap schema.
    """

    __tablename__ = "transactions"

    # Internal numeric primary key for convenience.
    pk = Column(Integer, primary_key=True, autoincrement=True)

    # Business identifier (UUID) from bot.models.Transaction.id
    id = Column(String, unique=True, nullable=False, index=True)

    telegram_name = Column(String, nullable=False)
    telegram_handle = Column(String, nullable=True)
    filename = Column(String, nullable=False)
    client_details = Column(String, nullable=False)
    recipient_name = Column(String, nullable=True)
    recipient_email = Column(String, nullable=True)
    issuer_group = Column(String, nullable=True, index=True)

    # Krab Issuer lead reference (8-char prefix from PDF filename); nullable for legacy rows.
    reference_id = Column(String, nullable=True, index=True)

    # Stored in UTC, always timezone-aware on the Python side.
    timestamp_utc = Column(DateTime(timezone=True), nullable=False, index=True)

    delivery_status = Column(String, nullable=False, default="PENDING", index=True)


class RecipientORM(Base):
    """
    Database representation of an email recipient.

    Admin can add recipients (name + email), and users select by name.
    """

    __tablename__ = "recipients"

    pk = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    created_at_utc = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


def init_db() -> None:
    """
    Initialize database and create tables if they don't exist.
    """
    Base.metadata.create_all(bind=engine)

    # Lightweight runtime migration for older databases.
    inspector = inspect(engine)
    transaction_columns = {col["name"] for col in inspector.get_columns("transactions")}
    alter_statements = []
    if "recipient_name" not in transaction_columns:
        alter_statements.append("ALTER TABLE transactions ADD COLUMN recipient_name VARCHAR")
    if "recipient_email" not in transaction_columns:
        alter_statements.append("ALTER TABLE transactions ADD COLUMN recipient_email VARCHAR")
    if "issuer_group" not in transaction_columns:
        alter_statements.append("ALTER TABLE transactions ADD COLUMN issuer_group VARCHAR")
    if "reference_id" not in transaction_columns:
        alter_statements.append("ALTER TABLE transactions ADD COLUMN reference_id VARCHAR")

    if alter_statements:
        with engine.begin() as conn:
            for stmt in alter_statements:
                conn.execute(text(stmt))


