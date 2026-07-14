"""
db.py — shared Neon Postgres connector for all KSplit scripts.

Reads DATABASE_URL from C:/KSplit/.env. Import this everywhere; never
hardcode the connection string.

    from db import get_conn, upsert_rows, fetch

Setup (once):
    pip install psycopg[binary] python-dotenv
    # create C:/KSplit/.env containing one line:
    #   DATABASE_URL=postgresql://neondb_owner:PASSWORD@ep-xxxx-pooler...neon.tech/neondb?sslmode=require

Test:
    python db.py      # should print: Connected. Tables: [...]
"""

import os
from contextlib import contextmanager

import psycopg
from psycopg import sql as _sql
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL not set. Create C:/KSplit/.env with your Neon connection string."
    )


@contextmanager
def get_conn():
    """Context-managed connection. Commits on clean exit, rolls back on error."""
    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_rows(table, columns, rows, conflict_cols, update_cols=None, page=1000):
    """Bulk INSERT ... ON CONFLICT DO UPDATE. This is how every scraper writes.

    table         : target table name
    columns       : list of column names, in order
    rows          : list of tuples matching columns
    conflict_cols : the table's primary-key columns
    update_cols   : columns to overwrite on conflict (default: all non-key cols)
    Returns the number of rows written.

    psycopg3: uses executemany with a parameterized VALUES clause (no mogrify).
    """
    if not rows:
        return 0
    if update_cols is None:
        update_cols = [c for c in columns if c not in conflict_cols]

    collist = _sql.SQL(", ").join(_sql.Identifier(c) for c in columns)
    conflict = _sql.SQL(", ").join(_sql.Identifier(c) for c in conflict_cols)
    setclause = _sql.SQL(", ").join(
        _sql.SQL("{c} = EXCLUDED.{c}").format(c=_sql.Identifier(c)) for c in update_cols
    )
    placeholders = _sql.SQL("(") + _sql.SQL(", ").join(
        _sql.Placeholder() for _ in columns
    ) + _sql.SQL(")")

    query = _sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES {ph} "
        "ON CONFLICT ({conflict}) DO UPDATE SET {setc}"
    ).format(
        table=_sql.Identifier(table),
        cols=collist,
        ph=placeholders,
        conflict=conflict,
        setc=setclause,
    )

    written = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), page):
                chunk = rows[i:i + page]
                cur.executemany(query, chunk)
                written += len(chunk)
    return written


def fetch(sql, params=None):
    """Run a SELECT, return list of dict rows."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


if __name__ == "__main__":
    tables = fetch("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
    """)
    print("Connected. Tables:", [t["table_name"] for t in tables])
