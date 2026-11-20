import hashlib
import re
from datetime import date

from hotel_study.config import load_config

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


_CLOSING_LINE = re.compile(r"^(?:(?:very|kindly|warmest|warm|best|kind|with|sincere|yours)\s+){0,3}"
                           r"(?:regards?|sincerely(?:\s+yours)?|sincerity|best wishes|salam(?:\s+\w+){0,2}|hormat(?:\s+\w+){0,2})\b(?:\s*[,\u2013-]\s*[A-Z].{0,70})?[\s,.]*$", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_HANDLE = re.compile(r"\b[A-Za-z]+\d{4,}\b|\b\d+[A-Za-z]+\d*[A-Za-z]*\b(?=\s*[,\n])")  # TripAdvisor handles such as Nomad19077435611, patrak2024
_TITLE = re.compile(r"\b(manager|director|direktur|executive|officer|supervisor|asisten|assistant|head of|chef|concierge|front office|"
                    r"guest relations?|reservations?|marketing|sales|owner|cluster|duty|gm|hr|coordinator|koordinator|team|tim)\b", re.I)
_HON_WORDS = {"mr", "mrs", "ms", "miss", "s", "bapak", "bpk", "ibu", "pak", "bu", "mba", "mbak", "mas", "kak", "sdr", "sdri"}
_GENERIC = {"valued", "value", "guest", "guests", "customer", "sir", "madam", "all", "team", "tamu", "yang", "terhormat", "our", "para", "traveller",
            "traveler", "beloved", "dearest", "esteemed", "honoured", "honored", "loyal", "terima", "kasih", "makasih", "and", "dan", "atau", "or", "the", "of", "from", "dari", "kami", "anda", "you", "there"}
_HONORIFIC = re.compile(r"\b(Mr|Mrs|Ms|Miss|Bapak|Bpk|Ibu|Pak|Bu|Mba|Mbak|Mas|Kak|Sdr|Sdri)\.?[ \t]+([A-Z][\w'\u2019-]+(?:[ \t]+[A-Z][\w'\u2019-]+){0,2})")
_NOT_NAMES = {"Terima", "Makasih", "Atau", "Dan", "Yang", "Kami", "Kita", "Anda", "Saya", "Selamat", "Semoga", "Mohon", "Kunjungan", "Dear",
              "Valued", "Guest", "Tamu", "Hotel", "Thank", "We", "Our", "The", "Untuk", "Dari", "Di", "Kepada", "Pak", "Bu", "Ibu", "Bapak"}
_GREET = r"^(\s*(?:Dear|Hi|Hello|Halo|Hai|Kepada|Yth\.?|Terhormat)[ \t]+)"
_GREETING_PHRASE = re.compile(_GREET + r"([^\s,]+(?:[ \t]+[^\s,]+){0,2})(?=[ \t]*(?:,|\n|$))")  # name or handle ends at a comma, line end or text end
_GREETING_TOKEN = re.compile(_GREET + r"([^\s,]+(?:[ \t]+[^\s,]+)?)")  # no terminator: look at two tokens, scrub only the first non-honorific one


def _honorific_name(m):
    first = m.group(2).split()[0]
    return m.group(0) if first in _NOT_NAMES else f"{m.group(1)} [name]"


def _greeting_name(m, phrase=True):
    """'Dear patrak2024,' / 'Dear Mr. Smith,' -> the handle or name becomes [name]; 'Dear Valued Guest,' stays.
    Without a comma or line end, only one token is replaced so the body of the reply is never touched."""
    toks = re.split(r"([\s/.]+)", m.group(2))  # keep separators
    words = [(i, t) for i, t in enumerate(toks) if t and not re.fullmatch(r"[\s/.]+", t)]
    lead = 0
    while lead < len(words) and words[lead][1].lower() in _HON_WORDS:
        lead += 1
    rest = words[lead:] if phrase else words[lead:lead + 1]
    if not rest or all(w.lower() in _GENERIC for _, w in rest):
        return m.group(0)
    start = words[lead][0]
    end = (words[-1][0] + 1) if phrase else (start + 1)
    return m.group(1) + "".join(toks[:start]) + "[name]" + "".join(toks[end:])


def _scrub_once(text):
    """Remove the signature block (closing phrase, name, job title) and names after honorifics or 'Dear'. Heuristic: reviewed on samples, not exact."""
    if not text:
        return text
    lines = text.rstrip().split("\n")
    filled = [i for i, l in enumerate(lines) if l.strip()]
    cut = None
    for i in filled[-4:]:  # a closing phrase within the last four lines starts the signature block
        tail = [l for l in lines[i + 1:] if l.strip()]
        # a signature block is short; an opening salutation ('Salam hangat dari X!') is followed by the body
        if len(lines[i].strip()) <= 100 and _CLOSING_LINE.match(lines[i].strip(" \t,.!:;-")) and all(len(l.strip()) <= 70 for l in tail):
            cut = i
            break
    if cut is None:  # no closing phrase: drop a trailing run of short lines if one of them is a job title
        j = len(lines)
        while j > 0 and (not lines[j - 1].strip() or (len(lines[j - 1].strip()) <= 60 and not lines[j - 1].rstrip().endswith((".", "!", "?", "\u3002", "\uff01", "\uff1f")))):
            j -= 1
        block = lines[j:]
        if j < len(lines) and len([l for l in block if l.strip()]) <= 4 and any(_TITLE.search(l) for l in block):  # signatures are at most four lines
            cut = j
    if cut is not None:
        text = "\n".join(lines[:cut]).rstrip()
    text = _HANDLE.sub("[name]", _EMAIL.sub("[email]", text))
    if _GREETING_PHRASE.match(text):
        text = _GREETING_PHRASE.sub(_greeting_name, text, count=1)
    else:
        text = _GREETING_TOKEN.sub(lambda m: _greeting_name(m, phrase=False), text, count=1)
    return _HONORIFIC.sub(_honorific_name, text)


def scrub_owner_response(text):
    """Remove signature blocks (closing phrase, name, job title), names/usernames in greetings and after honorifics, and emails.
    Repeated until stable (a long signature can take two passes). Heuristic: reviewed on samples, not exact."""
    for _ in range(3):
        new = _scrub_once(text)
        if new == text:
            break
        text = new
    return text


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
            return scrub_owner_response("\n".join(lines[:i]).rstrip())
    return scrub_owner_response(text)


def _non_negative(v):
    """The actor sends -1 for an unknown count; the table only allows >= 0, so store NULL."""
    return v if isinstance(v, int) and v >= 0 else None


def flatten_review(item: dict, salt: str, labels: dict | None = None) -> dict:
    """Map one raw Apify item to PRD section 4 reviews columns. PII is never copied."""
    user, owner = item.get("userProfile") or {}, item.get("ownerResponse") or {}
    trip = item.get("tripInfo") or {}
    labels = labels or load_config()["subrating_labels"]  # column suffix -> accepted labels (en, id)
    lookup = {l.strip().lower(): k for k, ls in labels.items() for l in ls}
    sub, other = {}, {}
    for a in item.get("additionalRatings") or []:
        label, val = a.get("ratingLabel"), a.get("rating", a.get("value"))
        if label and label.strip().lower() in lookup:
            sub["subrating_" + lookup[label.strip().lower()]] = val
        elif label:
            other[label] = val
    row = {
        "review_id": item.get("id"), "hotel_id": (item.get("location") or {}).get("locationId"),
        "created_date": _date(item.get("createdDate")), "published_date": _date(item.get("publishedDate")),
        "publish_platform": item.get("publishPlatform"), "rating": item.get("rating"),
        "language": item.get("language"), "original_language": item.get("originalLanguage"),
        "translation_type": item.get("translationType"), "title": item.get("title"), "text": item.get("text"),
        "url": item.get("url"), "helpful_votes": _non_negative(item.get("helpfulVotes")), "mcid": item.get("mcid"),
        "trip_info_stay_date": _date(trip.get("stayDate")), "trip_info_trip_type": trip.get("tripType"),
        **{f"subrating_{k}": None for k in ("value", "rooms", "location", "cleanliness", "service", "sleep_quality")},
        **sub, "subratings_other": other or None,
        "photo_count": len(item.get("photos") or []),
        "user_id_hash": hash_user_id(user["userId"], salt) if user.get("userId") else None,
        "user_hometown": user.get("hometown"),
        "user_contribution_count": _non_negative((user.get("contributionCounts") or {}).get("sumAllUgc")),
        "owner_response_id": owner.get("id"), "owner_response_published_date": _date(owner.get("publishedDate")),
        "owner_response_language": owner.get("language"),
        "owner_response_text": _clean_owner_text(owner.get("text"), [owner.get("responder"), owner.get("username")]),
        "owner_response_connection_to_subject": owner.get("connectionToSubject"),
        "input_page_number": (item.get("inputSource") or {}).get("pageNumber"),
    }
    return row
