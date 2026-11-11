import pytest

from hotel_study.config import get_secret, require_main_pass_ready


def test_require_main_pass_ready_raises_when_ceiling_none():
    with pytest.raises(RuntimeError, match="ceiling"):
        require_main_pass_ready({"ceiling": None, "end_page": 5})


def test_require_main_pass_ready_raises_when_end_page_none():
    with pytest.raises(RuntimeError, match="end_page"):
        require_main_pass_ready({"ceiling": 30, "end_page": None})


def test_require_main_pass_ready_ok_when_both_set():
    require_main_pass_ready({"ceiling": 30, "end_page": 5})


def test_get_secret_raises_when_missing(monkeypatch):
    monkeypatch.setenv("NOPE_SECRET", "")
    with pytest.raises(RuntimeError, match="NOPE_SECRET"):
        get_secret("NOPE_SECRET")
