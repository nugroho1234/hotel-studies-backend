import hashlib
import re
from datetime import date

SUBRATINGS = {"value", "rooms", "location", "cleanliness", "service", "sleep quality"}
CLOSINGS = {"best regards", "warm regards", "kind regards", "regards", "sincerely", "salam",
            "hormat kami", "terima kasih"}


def hash_user_id(user_id: str, salt: str) -> str:
    return hashlib.sha256((salt + str(user_id)).encode()).hexdigest()


def _date(s) -> date | None:
    """Date part only of an ISO string; tolerates 'YYYY-MM' (day 1). None if unparsable."""
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(s))
    try:
        return date(int(m[1]), int(m[2]), int(m[3] or 1)) if m else None
    except ValueError:
        return None


def in_window(published_date: str, start, end) -> bool:
    d = _date(published_date)
    return d is not None and _date(start) <= d <= _date(end)


def _clean_owner_text(text, own_names):
    # shortcut: unknown names mentioned mid-text are not caught; upgrade if a name audit finds leaks.
    if not text:
        return text
    for n in own_names:
        if isinstance(n, str) and len(n.strip()) >= 3:
            text = re.sub(rf"\b{re.escape(n.strip())}\b", "", text, flags=re.I)
    lines = text.rstrip().split("\n")
    filled = [i for i, l in enumerate(lines) if l.strip()]
    for i in filled[-3:]:  # the closing phrase must be the whole line, not a sentence starting with it
        if lines[i].strip(" \t,.!:;-").lower() in CLOSINGS:
            return "\n".join(lines[:i]).rstrip()
    return text


def flatten_review(item: dict, salt: str) -> dict:
    """Map one raw Apify item to PRD section 4 reviews columns. PII is never copied."""
    user, owner = item.get("userProfile") or {}, item.get("ownerResponse") or {}
    trip = item.get("tripInfo") or {}
    sub, other = {}, {}
    for a in item.get("additionalRatings") or []:
        label, val = a.get("ratingLabel"), a.get("rating", a.get("value"))
        if label and label.strip().lower() in SUBRATINGS:
            sub["subrating_" + label.strip().lower().replace(" ", "_")] = val
        elif label:
            other[label] = val
    row = {
        "review_id": item.get("id"), "hotel_id": (item.get("location") or {}).get("locationId"),
        "created_date": _date(item.get("createdDate")), "published_date": _date(item.get("publishedDate")),
        "publish_platform": item.get("publishPlatform"), "rating": item.get("rating"),
        "language": item.get("language"), "original_language": item.get("originalLanguage"),
        "translation_type": item.get("translationType"), "title": item.get("title"), "text": item.get("text"),
        "url": item.get("url"), "helpful_votes": item.get("helpfulVotes"), "mcid": item.get("mcid"),
        "trip_info_stay_date": _date(trip.get("stayDate")), "trip_info_trip_type": trip.get("tripType"),
        **{f"subrating_{k}": None for k in ("value", "rooms", "location", "cleanliness", "service", "sleep_quality")},
        **sub, "subratings_other": other or None,
        "photo_count": len(item.get("photos") or []),
        "user_id_hash": hash_user_id(user["userId"], salt) if user.get("userId") else None,
        "user_hometown": user.get("hometown"),
        "user_contribution_count": (user.get("contributionCounts") or {}).get("sumAllUgc"),
        "owner_response_id": owner.get("id"), "owner_response_published_date": _date(owner.get("publishedDate")),
        "owner_response_language": owner.get("language"),
        "owner_response_text": _clean_owner_text(owner.get("text"), [owner.get("responder"), owner.get("username")]),
        "owner_response_connection_to_subject": owner.get("connectionToSubject"),
        "input_page_number": (item.get("inputSource") or {}).get("pageNumber"),
    }
    return row
