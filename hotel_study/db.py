import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from hotel_study.config import ROOT, get_secret

SCHEMA = ROOT / "sql" / "schema.sql"
FRONTEND_SCHEMA = ROOT / "sql" / "frontend.sql"  # access layer for the web app; runs after the tables exist


def connect() -> psycopg.Connection:
    return psycopg.connect(get_secret("SUPABASE_DB_URL"))


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.execute(FRONTEND_SCHEMA.read_text())
    conn.commit()


def upsert(conn: psycopg.Connection, table: str, rows: list[dict], key: str | list[str]) -> int:
    """INSERT ... ON CONFLICT DO UPDATE. Does not commit; the caller controls the transaction."""
    if not rows:
        return 0
    keys = [key] if isinstance(key, str) else list(key)
    cols = list(rows[0])
    ident = lambda names: sql.SQL(", ").join(map(sql.Identifier, names))
    sets = [c for c in cols if c not in keys]
    action = (sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
        sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in sets)) if sets else sql.SQL("DO NOTHING")
    q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) {}").format(
        sql.Identifier(table), ident(cols), sql.SQL(", ").join(sql.Placeholder(c) for c in cols), ident(keys), action)
    with conn.cursor() as cur:
        cur.executemany(q, [{c: Jsonb(v) if isinstance(v, dict) else v for c, v in r.items()} for r in rows])
    return len(rows)
