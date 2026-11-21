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


@app.command("backfill-google-ids")
def backfill_google_ids_cmd(limit: Limit = None, destination: Destination = None) -> None:
    """Fill hotels.google_entity_id / google_kg_id from stored google_detail_url (no Apify call)."""
    from hotel_study.google_ids import backfill_google_ids

    typer.echo(backfill_google_ids(limit, destination))


@app.command("google-ids-export")
def google_ids_export(path: Path = Path("output/google_ids_missing.csv")) -> None:
    """Write final-sample hotels without a Google entity ID to a CSV for manual lookup."""
    from hotel_study.google_ids import export_missing

    typer.echo(f"{export_missing(path)} hotels written to {path}")


@app.command("google-ids-import")
def google_ids_import(csv: Path) -> None:
    """Read google_travel_url per hotel from the CSV and store the entity ID."""
    from hotel_study.google_ids import import_urls

    typer.echo(import_urls(csv))


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


@app.command("pilot-export")
def pilot_export_cmd() -> None:
    """Fill the pilot_sample table (randomised pilot_id) and write pilot_sample.csv and pilot_coding.csv."""
    from hotel_study.pilot import pilot_export

    typer.echo(pilot_export())


@app.command("report")
def report_cmd(eligibility_test: Annotated[bool, typer.Option("--eligibility-test", help="Eligibility-test counts report (read-only)")] = False,
               limit: Limit = None, destination: Destination = None) -> None:
    """Plain: PRD 12 funnel + acceptance checks (output/funnel.csv). --eligibility-test: eligibility counts report."""
    from hotel_study import report

    typer.echo((report.report_eligibility_test if eligibility_test else report.report_funnel)(limit, destination))


@app.command("draft-labels")
def draft_labels_cmd(limit: Limit = None) -> None:
    """Draft LLM pass on a seeded random sample to ESTIMATE label frequencies (output/draft_*.csv; not for selection)."""
    from hotel_study.draft_labels import draft_labels

    typer.echo(draft_labels(limit))


@app.command("assign-sample-roles")
def assign_sample_roles_cmd(force: Annotated[bool, typer.Option("--force", help="Redraw even if roles exist")] = False,
                            dry_run: Annotated[bool, typer.Option("--dry-run", help="Draw and print, write nothing")] = False) -> None:
    """Assign training (30) and gold (200 = 70 dev_set + 130 test_set) roles to selected non-pilot reviews, seed 42."""
    from hotel_study.sample_roles import assign_sample_roles

    try:
        typer.echo(assign_sample_roles(force, dry_run))
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("assign-supp-rel")
def assign_supp_rel_cmd(force: Annotated[bool, typer.Option("--force", help="Redraw even if a supplement exists")] = False,
                        dry_run: Annotated[bool, typer.Option("--dry-run", help="Draw and print, write nothing")] = False) -> None:
    """Keyword oversample for reliability: 100 reviews (25 supp_rel_dev, 75 supp_rel_test) from config/rel_keywords.yaml."""
    from hotel_study.supp_rel import assign_supp_rel

    try:
        typer.echo(assign_supp_rel(force, dry_run))
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("backup-study-data")
def backup_study_data_cmd() -> None:
    """Export coders' splits, submissions, agreed phrases, profiles (no emails) and sample roles to output/backup/study_<date>/."""
    from hotel_study.backup import backup_study_data

    typer.echo(backup_study_data())


@app.command("set-profile")
def set_profile_cmd(email: str, role: Annotated[str, typer.Option(help="lead | researcher | expert | coder")],
                    name: Annotated[str, typer.Option(help="Display name (pseudonym)")]) -> None:
    """Create or change the profile (role and display name) of an existing Supabase user, found by email."""
    from hotel_study.db import connect

    if role not in ("lead", "researcher", "expert", "coder"):
        raise typer.BadParameter("role must be lead, researcher, expert or coder")
    with connect() as conn:
        row = conn.execute("SELECT id FROM auth.users WHERE lower(email) = lower(%s)", (email,)).fetchone()
        if not row:
            typer.echo("No such user in Supabase Auth.", err=True)
            raise typer.Exit(1)
        conn.execute("INSERT INTO profiles (user_id, role, display_name) VALUES (%s, %s, %s) "
                     "ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, display_name = EXCLUDED.display_name",
                     (row[0], role, name))
        conn.commit()
    typer.echo(f"{name}: {role}")


@app.command("plan-dev-rounds")
def plan_dev_rounds_cmd(rounds: Annotated[int, typer.Option(help="How many fresh rounds to prepare")] = 1,
                        force: Annotated[bool, typer.Option("--force", help="Redraw reserves not yet used")] = False) -> None:
    """Pre-draw the reserve of never-used reviews for the next fresh dev round(s) (70 + 25 supplement, seed 42 + round)."""
    from hotel_study.dev_rounds import plan_rounds

    typer.echo(plan_rounds(rounds, force))
