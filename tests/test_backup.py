import csv

from hotel_study.backup import backup_study_data
from hotel_study.db import connect


def test_backup_writes_roles_and_skips_missing_tables(tmp_path):
    with connect() as conn:
        msg = backup_study_data(tmp_path, conn)
    assert "review_roles:" in msg
    rows = list(csv.reader(open(next(tmp_path.glob("study_*")) / "review_roles.csv")))
    assert rows[0] == ["review_id", "is_selected", "selection_rank", "is_pilot", "is_training", "gold_set"]
    assert len(rows) - 1 == 100 + 30 + 200 + 100  # pilot + training + gold + supplement
