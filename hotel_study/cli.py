from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True)

# Shared option set: use as `limit: Limit = None, destination: Destination = None`.
Limit = Annotated[int | None, typer.Option("--limit", help="Process at most N items (test runs)")]
Destination = Annotated[str | None, typer.Option("--destination", help="Only this destination")]


def same_destination(a: str, b: str | None) -> bool:
    """Case-insensitive destination match; no filter (None) matches all."""
    return b is None or a.strip().lower() == b.strip().lower()


@app.callback()
def main() -> None:
    """Hotel study pipeline."""


@app.command("init-db")
def init_db() -> None:
    """Create the tables if missing (safe to rerun)."""
    from hotel_study.db import apply_schema, connect

    with connect() as conn:
        apply_schema(conn)
    typer.echo("Schema applied.")
