import re

_ID = re.compile(r"-d(\d+)-")
_STAR = re.compile(r"(\d)(?:\.\d)?-star")


def parse_hotel_id(url: str | None) -> int | None:
    m = _ID.search(url or "")
    return int(m.group(1)) if m else None


def parse_star_label(label: str | None) -> int | None:
    m = _STAR.search(label or "")
    return int(m.group(1)) if m else None
