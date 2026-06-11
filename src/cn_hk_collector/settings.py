from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def get_database_url(value: str | None = None) -> str:
    database_url = str(value or os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required, for example postgresql://postgres:postgres@127.0.0.1:5432/doxatlas_collector")
    return database_url
