from pathlib import Path
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


@app.command("load-hotels")
def load_hotels_cmd(csv: Path, limit: Limit = None, destination: Destination = None) -> None:
    """Load the hotel CSV into the hotels table (safe to rerun)."""
    from hotel_study.hotels import load_hotels

    typer.echo(load_hotels(csv, limit, destination))


@app.command("google-stars")
def google_stars(limit: Limit = None, destination: Destination = None) -> None:
    """Look up Google star rating for pre-screened hotels."""
    from hotel_study.google import run_google_stars

    typer.echo(run_google_stars(limit, destination))


@app.command("compare-stars")
def compare_stars_cmd(limit: Limit = None, destination: Destination = None,
                      overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace manual_checks.csv even if it holds manual entries")] = False) -> None:
    """Compare Google and TripAdvisor star classes; write output/*.csv."""
    from hotel_study.stars import compare_stars

    try:
        typer.echo(compare_stars(limit, destination, overwrite))
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("finalise-stars")
def finalise_stars_cmd(csv: Path, limit: Limit = None, destination: Destination = None) -> None:
    """Apply the filled manual_checks.csv and set final_star_rating / in_sample_frame."""
    from hotel_study.stars import OpenManualChecks, finalise_stars

    try:
        typer.echo(finalise_stars(csv, limit, destination))
    except OpenManualChecks as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("order-cells")
def order_cells_cmd(limit: Annotated[int | None, typer.Option("--limit", help="Order at most N cells (test runs)")] = None,
                    destination: Destination = None) -> None:
    """Draw the seeded random hotel order within each destination x star cell (safe to rerun until reviews exist)."""
    from hotel_study.cells import order_cells

    try:
        typer.echo(order_cells(limit, destination))
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("scrape-reviews")
def scrape_reviews_cmd(pass_: Annotated[int, typer.Option("--pass", help="1 (main pass; --limit = at most N cells) or 2")] = 1,
                       eligibility_test: Annotated[bool, typer.Option("--eligibility-test", help="Scrape only the first hotels of each cell, no page cap")] = False,
                       limit: Limit = None, destination: Destination = None) -> None:
    """Scrape TripAdvisor reviews (resumable; reruns never duplicate reviews)."""
    from hotel_study.reviews import scrape_reviews

    try:
        typer.echo(scrape_reviews(str(pass_), eligibility_test, limit, destination))
    except (RuntimeError, NotImplementedError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("select-reviews")
def select_reviews_cmd(limit: Limit = None, destination: Destination = None) -> None:
    """Pick the newest reviews_per_hotel eligible reviews per final-sample hotel (safe to rerun)."""
    from hotel_study.selection import select_reviews

    typer.echo(select_reviews(limit, destination))


@app.command("pilot-sample")
def pilot_sample_cmd(force: Annotated[bool, typer.Option("--force", help="Redraw even if a pilot sample exists")] = False,
                     limit: Limit = None, destination: Destination = None) -> None:
    """Draw the stratified pilot sample from selected reviews and export output/pilot_100.csv."""
    from hotel_study.pilot import pilot_sample

    try:
        typer.echo(pilot_sample(force, limit, destination))
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("report")
def report_cmd(eligibility_test: Annotated[bool, typer.Option("--eligibility-test", help="Eligibility-test counts report (read-only)")] = False,
               limit: Limit = None, destination: Destination = None) -> None:
    """Plain: PRD 12 funnel + acceptance checks (output/funnel.csv). --eligibility-test: eligibility counts report."""
    from hotel_study import report

    typer.echo((report.report_eligibility_test if eligibility_test else report.report_funnel)(limit, destination))
