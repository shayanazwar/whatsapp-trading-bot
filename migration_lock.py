from __future__ import annotations

import subprocess
import sys

from sqlalchemy import create_engine

from app.config import get_settings

# A stable application-specific PostgreSQL advisory lock. It serializes Alembic startup
# across the web service and the two worker services when Render starts them together.
_LOCK_KEY = 7_214_026_110_03


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        if engine.dialect.name != "postgresql":
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
            return 0
        with engine.connect() as connection:
            connection.exec_driver_sql(f"SELECT pg_advisory_lock({_LOCK_KEY})")
            connection.commit()
            try:
                subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
            finally:
                connection.exec_driver_sql(f"SELECT pg_advisory_unlock({_LOCK_KEY})")
                connection.commit()
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
