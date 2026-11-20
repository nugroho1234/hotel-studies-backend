import pytest

from hotel_study.config import get_secret, load_config, require_flag, require_main_pass_ready


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


def test_require_flag_raises_when_false():
    with pytest.raises(RuntimeError, match="allow_extended_collection"):
        require_flag({"allow_extended_collection": False}, "allow_extended_collection")


def test_require_flag_raises_when_missing():
    with pytest.raises(RuntimeError, match="allow_price_collection"):
        require_flag({}, "allow_price_collection")


def test_require_flag_ok_when_true():
    require_flag({"allow_price_collection": True}, "allow_price_collection")


def test_default_config_has_all_flags_false():
    cfg = load_config()
    for k in ("allow_extended_collection", "allow_price_collection", "run_rating_feasibility"):
        assert cfg[k] is False
