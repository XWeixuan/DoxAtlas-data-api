from __future__ import annotations

from importlib.resources import files
from typing import Any, Iterable
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cn_hk_collector.settings import get_database_url

RAW_MEDIA_COLUMNS = (
    "task_id",
    "market",
    "ticker",
    "published_at",
    "source_type",
    "channel",
    "source_name",
    "title",
    "summary",
    "content",
    "url",
    "origin_keyword_type",
    "is_content_relevant",
    "content_relevance_reason",
    "duplicate_count",
    "duplicate_urls",
    "content_hash",
    "simhash",
)

RAW_SOCIAL_COLUMNS = (
    "task_id",
    "market",
    "ticker",
    "published_at",
    "source_type",
    "channel",
    "source_name",
    "title",
    "summary",
    "content",
    "url",
    "origin_keyword_type",
    "is_content_relevant",
    "content_relevance_reason",
)

RAW_TABLE_COLUMNS = {
    "raw_media": RAW_MEDIA_COLUMNS,
    "raw_social": RAW_SOCIAL_COLUMNS,
}


class TaskNotFoundError(Exception):
    """Raised when a crawler task id does not exist."""


class TaskNotReadyError(Exception):
    """Raised when a crawler task has not succeeded yet."""


class TaskAlreadyPulledError(Exception):
    """Raised when a crawler task result was already pulled once."""


def connect(database_url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(get_database_url(database_url), row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    schema = files("cn_hk_collector").joinpath("schema.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for statement in schema.split(";"):
            cleaned = statement.strip()
            if cleaned:
                cur.execute(cleaned)
    conn.commit()


def _coerce_value(column: str, value: Any) -> Any:
    if column == "duplicate_count":
        return int(value or 1)
    if column == "duplicate_urls":
        if isinstance(value, (list, dict)):
            return Jsonb(value)
        return Jsonb([])
    if column == "origin_keyword_type":
        return value or "Base"
    if column == "source_type":
        return value
    if column == "published_at" and not value:
        return None
    return value


def _row_values(record: dict[str, Any], columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(_coerce_value(column, record.get(column)) for column in columns)


def upsert_raw_records(
    conn: psycopg.Connection,
    table_name: str,
    records: Iterable[dict[str, Any]],
    *,
    market: str,
    ticker: str,
    task_id: str | None = None,
) -> int:
    if table_name not in RAW_TABLE_COLUMNS:
        raise ValueError(f"Unsupported raw table: {table_name}")

    prepared_by_url: dict[str, dict[str, Any]] = {}
    for record in records:
        url = str(record.get("url") or "").strip()
        if not url:
            continue
        item = dict(record)
        item["market"] = market
        item["ticker"] = ticker
        if task_id:
            item["task_id"] = task_id
        item["source_type"] = item.get("source_type") or ("media" if table_name == "raw_media" else "social")
        prepared_by_url[url] = item

    prepared = list(prepared_by_url.values())
    if not prepared:
        return 0

    columns = RAW_TABLE_COLUMNS[table_name]
    column_sql = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    update_columns = [column for column in columns if column not in {"market", "ticker", "url"}]
    update_sql = ", ".join([f"{column} = EXCLUDED.{column}" for column in update_columns] + ["updated_at = now()"])
    query = f"""
        INSERT INTO {table_name} ({column_sql})
        VALUES ({placeholders})
        ON CONFLICT (market, ticker, url)
        DO UPDATE SET {update_sql}
    """

    with conn.cursor() as cur:
        cur.executemany(query, [_row_values(record, columns) for record in prepared])
    conn.commit()
    return len(prepared)


def count_rows(conn: psycopg.Connection, table_name: str, *, market: str | None = None, ticker: str | None = None) -> int:
    if table_name not in {"raw_media", "raw_social", "ticker_entities"}:
        raise ValueError(f"Unsupported table: {table_name}")
    where = []
    params: list[Any] = []
    if market:
        where.append("market = %s")
        params.append(market)
    if ticker:
        where.append("ticker = %s")
        params.append(ticker)
    suffix = f" WHERE {' AND '.join(where)}" if where else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS count FROM {table_name}{suffix}", params)
        row = cur.fetchone() or {"count": 0}
    return int(row["count"])


def create_crawler_task(
    conn: psycopg.Connection,
    *,
    market: str,
    ticker: str,
    lookback_days: int,
    window_start: str | None = None,
    window_end: str | None = None,
    collect_media: bool = True,
    collect_social: bool = True,
    task_id: str | None = None,
) -> dict[str, Any]:
    task_id = task_id or str(uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawler_tasks (
                task_id, market, ticker, lookback_days, window_start, window_end,
                collect_media, collect_social
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (task_id, market, ticker, lookback_days, window_start, window_end, collect_media, collect_social),
        )
        task = cur.fetchone()
    conn.commit()
    return dict(task)


def get_crawler_task(conn: psycopg.Connection, task_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM crawler_tasks WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def mark_crawler_task_running(conn: psycopg.Connection, task_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawler_tasks
            SET status = 'running', started_at = now(), updated_at = now(), error = NULL
            WHERE task_id = %s
            """,
            (task_id,),
        )
    conn.commit()


def mark_crawler_task_succeeded(conn: psycopg.Connection, result: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawler_tasks
            SET status = 'succeeded',
                media_fetched = %s,
                media_written = %s,
                social_fetched = %s,
                social_written = %s,
                media_relevant = %s,
                media_irrelevant = %s,
                error = NULL,
                finished_at = now(),
                updated_at = now()
            WHERE task_id = %s
            """,
            (
                int(getattr(result, "media_fetched", 0) or 0),
                int(getattr(result, "media_written", 0) or 0),
                int(getattr(result, "social_fetched", 0) or 0),
                int(getattr(result, "social_written", 0) or 0),
                int(getattr(result, "media_relevant", 0) or 0),
                int(getattr(result, "media_irrelevant", 0) or 0),
                getattr(result, "task_id"),
            ),
        )
    conn.commit()


def mark_crawler_task_failed(conn: psycopg.Connection, task_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawler_tasks
            SET status = 'failed',
                error = %s,
                finished_at = now(),
                updated_at = now()
            WHERE task_id = %s
            """,
            (error[:4000], task_id),
        )
    conn.commit()


def _fetch_task_records(conn: psycopg.Connection, table_name: str, task_id: str) -> list[dict[str, Any]]:
    if table_name not in RAW_TABLE_COLUMNS:
        raise ValueError(f"Unsupported raw table: {table_name}")
    columns = ", ".join(RAW_TABLE_COLUMNS[table_name])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {columns}
            FROM {table_name}
            WHERE task_id = %s
            ORDER BY published_at DESC NULLS LAST, created_at DESC
            """,
            (task_id,),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def pull_crawler_task_once(conn: psycopg.Connection, task_id: str) -> dict[str, Any]:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM crawler_tasks WHERE task_id = %s FOR UPDATE", (task_id,))
            task = cur.fetchone()
        if not task:
            raise TaskNotFoundError(task_id)
        task = dict(task)
        if task.get("status") != "succeeded":
            raise TaskNotReadyError(task_id)
        if task.get("pulled_at"):
            raise TaskAlreadyPulledError(task_id)

        media_records = _fetch_task_records(conn, "raw_media", task_id)
        social_records = _fetch_task_records(conn, "raw_social", task_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crawler_tasks
                SET pulled_at = now(), updated_at = now()
                WHERE task_id = %s
                RETURNING *
                """,
                (task_id,),
            )
            pulled_task = dict(cur.fetchone())
    return {
        "task": pulled_task,
        "raw_media": media_records,
        "raw_social": social_records,
    }
