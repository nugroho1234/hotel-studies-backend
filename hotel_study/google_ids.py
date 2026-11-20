"""Google Hotels entity token and knowledge-graph ID, read from the stored google_detail_url (no Apify call)."""
import base64
import re
from urllib.parse import parse_qs, urlparse

from hotel_study.db import connect
from hotel_study.cli import same_destination

ENTITY_URL = "https://www.google.com/travel/hotels/entity/{}"


def _unwrap_qs(qs: str) -> str | None:
    """travel/search?qs=<base64 protobuf>: the entity token sits in a length-prefixed field (0x32, length, 'Ch...').
    Search-result pages (no hotel opened) carry no token, so this returns None for them."""
    try:
        raw = base64.urlsafe_b64decode(qs + "=" * (-len(qs) % 4))
    except ValueError:
        return None
    for i in range(len(raw) - 3):
        if raw[i] == 0x32 and raw[i + 2:i + 4] == b"Ch" and i + 2 + raw[i + 1] <= len(raw):
            token = raw[i + 2:i + 2 + raw[i + 1]].decode("ascii", "ignore")
            if re.fullmatch(r"Ch[A-Za-z0-9_-]+", token):
                return token
    return None


def extract_entity_id(url: str | None) -> str | None:
    """The token after /entity/, or the one wrapped in a travel/search `qs` value. None for anything else; never raises."""
    if not url or not isinstance(url, str):
        return None
    try:
        u = urlparse(url)
        m = re.search(r"/entity/([A-Za-z0-9_-]+)", u.path)
        token = m.group(1) if m else (parse_qs(u.query).get("qs") or [None])[0]
    except ValueError:
        return None
    if not (token and re.fullmatch(r"[A-Za-z0-9_-]+", token)):
        return None
    return token if m else _unwrap_qs(token)


def extract_kg_id(url: str | None) -> str | None:
    """The knowledge-graph ID (/g/... or /m/...) encoded inside the entity token, or None."""
    token = extract_entity_id(url)
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return None
    m = re.search(rb"/[gm]/[A-Za-z0-9_-]+", raw)
    return m.group(0).decode() if m else None


def entity_url(token: str) -> str:
    return ENTITY_URL.format(token)


def backfill_google_ids(limit: int | None = None, destination: str | None = None) -> dict:
    with connect() as conn:
        rows = [r for r in conn.execute("SELECT hotel_id, google_detail_url, destination FROM hotels WHERE google_detail_url IS NOT NULL ORDER BY hotel_id").fetchall()
                if same_destination(r[2] or "", destination)][:limit]
        n = 0
        for hotel_id, url, _ in rows:
            token = extract_entity_id(url)
            if token:
                conn.execute("UPDATE hotels SET google_entity_id = %s, google_kg_id = %s WHERE hotel_id = %s", (token, extract_kg_id(url), hotel_id))
                n += 1
        conn.commit()
    return {"urls": len(rows), "ids_stored": n}


def export_missing(path) -> int:
    """CSV of final-sample hotels without an entity ID, for the owner to look up by hand (fill google_travel_url)."""
    import csv
    with connect() as conn:
        rows = conn.execute("SELECT hotel_id, destination, final_star_rating, hotel_name, address FROM hotels "
                            "WHERE in_final_sample AND google_entity_id IS NULL ORDER BY destination, final_star_rating, hotel_name").fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hotel_id", "destination", "final_star_rating", "hotel_name", "address", "google_travel_url"])
        w.writerows([*r, ""] for r in rows)
    return len(rows)


def is_no_price(remark) -> bool:
    return isinstance(remark, str) and remark.strip().upper() in {"NO_GOOGLE_PRICE", "NO_GOOGLE_PAGE"}


def import_urls(path) -> dict:
    """Read google_travel_url (or any google.com/travel URL) per hotel_id; store entity ID and KG ID. Bad or blank rows are counted, not errors."""
    import csv
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    done, bad, no_price = 0, [], 0
    with connect() as conn:
        for r in rows:
            url = (r.get("google_travel_url") or "").strip()
            if is_no_price(r.get("remarks")) and not extract_entity_id(url):
                conn.execute("UPDATE hotels SET has_google_price = false WHERE hotel_id = %s", (int(r["hotel_id"]),))
                no_price += 1
                continue
            if not url:
                continue
            token = extract_entity_id(url)
            if not token:
                bad.append(r["hotel_id"])
                continue
            cur = conn.execute("UPDATE hotels SET google_entity_id = %s, google_kg_id = %s WHERE hotel_id = %s",
                               (token, extract_kg_id(url), int(r["hotel_id"])))
            done += cur.rowcount
        conn.commit()
    return {"rows": len(rows), "stored": done, "marked_no_price": no_price, "unreadable_hotel_ids": bad}
