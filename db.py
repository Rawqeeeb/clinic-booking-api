import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool, StaticPool

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    db_path = Path(__file__).resolve().parent / "clinic.db"
    DATABASE_URL = f"sqlite:///{db_path.as_posix()}"
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

is_sqlite = DATABASE_URL.startswith("sqlite")

if is_sqlite:
    if ":memory:" in DATABASE_URL:
        engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_pre_ping=True,
        )

    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
else:
    engine = create_engine(
        DATABASE_URL,
        poolclass=NullPool,
        pool_pre_ping=True,
    )


@contextmanager
def get_db():
    with engine.begin() as conn:
        yield conn


def init_db():
    with get_db() as conn:
        # Slots table
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS slots (
                    id VARCHAR(255) PRIMARY KEY,
                    provider TEXT NOT NULL,
                    start_time TIMESTAMPTZ NOT NULL,
                    end_time TIMESTAMPTZ NOT NULL,
                    is_booked BOOLEAN NOT NULL DEFAULT FALSE
                );
                """
            )
        )

        # Bookings table
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS bookings (
                    id VARCHAR(255) PRIMARY KEY,
                    slot_id VARCHAR(255) NOT NULL,
                    patient_name TEXT NOT NULL,
                    patient_email TEXT NOT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'confirmed',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        )

        # Partial unique index for concurrency safety (Zero double-booking)
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_booking 
                ON bookings (slot_id) 
                WHERE status = 'confirmed';
                """
            )
        )

        # Idempotency table
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key VARCHAR(255) PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    status_code INT NOT NULL,
                    response_body TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        )

        # Distributed rate limits / attempts table
        id_col = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite else "SERIAL PRIMARY KEY"
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS booking_attempts (
                    id {id_col},
                    email TEXT NOT NULL,
                    attempted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_booking_attempts_email_time 
                ON booking_attempts (email, attempted_at);
                """
            )
        )

    seed_slots_if_empty()


def seed_slots_if_empty():
    with get_db() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM slots")).scalar()
        if count == 0:
            now = datetime.now(timezone.utc)
            base_date = (now + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            providers = ["Dr Ade", "Dr Okafor", "Dr Smith"]
            slots_to_insert = []

            for day_offset in range(3):
                day = base_date + timedelta(days=day_offset)
                for i, prov in enumerate(providers):
                    for slot_idx in range(2):
                        slot_start = day + timedelta(hours=i * 2 + slot_idx)
                        slot_end = slot_start + timedelta(minutes=30)
                        slot_id = f"slot_{day.strftime('%Y%m%d')}_{i}_{slot_idx}"
                        slots_to_insert.append(
                            {
                                "id": slot_id,
                                "provider": prov,
                                "start_time": slot_start,
                                "end_time": slot_end,
                                "is_booked": False,
                            }
                        )

            for s in slots_to_insert:
                conn.execute(
                    text(
                        """
                        INSERT INTO slots (id, provider, start_time, end_time, is_booked)
                        VALUES (:id, :provider, :start_time, :end_time, :is_booked)
                        ON CONFLICT (id) DO NOTHING;
                        """
                    ),
                    s,
                )


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
