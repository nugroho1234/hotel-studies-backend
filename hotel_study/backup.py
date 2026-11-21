"""Periodic export of the study data that cannot be recreated (coders' splits, sample roles) to local CSVs.
Never exports emails (profiles has none) or review text. Tables that do not exist yet are skipped."""
import csv
from datetime import date
from pathlib import Path

from hotel_study.config import ROOT
from hotel_study.db import connect

TABLES = {
    "human_splits": "SELECT * FROM human_splits ORDER BY split_id",
    "split_submissions": "SELECT * FROM split_submissions ORDER BY review_id, coder",
    "agreed_phrases": "SELECT * FROM agreed_phrases ORDER BY phrase_id",
    "profiles": "SELECT user_id, role, display_name, created_at FROM profiles ORDER BY created_at",
    "set_access": "SELECT * FROM set_access ORDER BY set_name",
    "review_roles": "SELECT review_id, is_selected, selection_rank, is_pilot, is_training, gold_set FROM reviews "
                    "WHERE is_pilot OR is_training OR gold_set IS NOT NULL ORDER BY review_id",
}


def backup_study_data(out_dir: Path | None = None, conn=None) -> str:
    out = (out_dir or ROOT / "output" / "backup") / f"study_{date.today().isoformat()}"
    out.mkdir(parents=True, exist_ok=True)
    own = conn is None
    conn = conn or connect()
    try:
        lines = []
        for name, q in TABLES.items():
            if not conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()[0]:
                lines.append(f"{name}: skipped (table does not exist yet)")
                continue
            cur = conn.execute(q)
            rows = cur.fetchall()
            with open(out / f"{name}.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([c.name for c in cur.description])
                w.writerows(rows)
            lines.append(f"{name}: {len(rows)} rows")
        return f"Backup in {out}\n" + "\n".join(lines)
    finally:
        if own:
            conn.close()
