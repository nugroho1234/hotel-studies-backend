import re

from rapidfuzz import fuzz

from hotel_study.parsing import parse_star_label

_DROP = {"hotel", "by", "the"}


def normalise_name(s: str) -> str:
    return " ".join(w for w in re.sub(r"[^\w\s]", " ", s.lower()).split() if w not in _DROP)


def match_score(a: str, b: str) -> float:
    return fuzz.token_set_ratio(normalise_name(a), normalise_name(b))


def classify_match(score: float, cfg: dict) -> str:
    """cfg is config name_match: {accept, uncertain}."""
    return "matched" if score >= cfg["accept"] else "uncertain" if score >= cfg["uncertain"] else "none"


def build_query(hotel_name: str, destination: str) -> str:
    city = destination.strip().title()
    return hotel_name if city.lower() in hotel_name.lower() else f"{hotel_name} {city}"


def pick_best(hotel_name: str, results: list[dict], cfg: dict) -> dict:
    """Best-scoring result as google_* columns; below the uncertain threshold only the score is kept."""
    scored = [(match_score(hotel_name, r.get("name") or ""), r) for r in results]
    if not scored:
        return {"google_name": None, "google_detail_url": None, "google_star_rating": None,
                "google_star_class_label": None, "google_match_score": 0}
    score, r = max(scored, key=lambda x: x[0])
    keep = classify_match(score, cfg) != "none"
    stars = r.get("star_rating") or parse_star_label(r.get("star_class"))
    return {"google_name": r.get("name") if keep else None,
            "google_detail_url": r.get("detail_url") if keep else None,
            "google_star_rating": stars if keep else None,
            "google_star_class_label": r.get("star_class") if keep else None,
            "google_match_score": score}
