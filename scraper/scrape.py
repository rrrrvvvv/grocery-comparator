"""
Scrape Flipp's public flyer search API for the items in items.json
and emit web/prices.json with current best prices, store-by-store details,
unit prices, and rolling history-derived 'good price' thresholds.

Designed to run on GitHub Actions on a daily cron. Reads POSTAL_CODE from env.

The Flipp 'backflipp' search endpoint is undocumented but stable; it powers
the public flipp.com flyer search and has been used by community scrapers
for years. See README for diagnostic / fallback notes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPER_DIR = REPO_ROOT / "scraper"
# GitHub Pages classic only serves from / (root) or /docs, so we publish
# the static site (index.html, prices.json, icons, sw.js, manifest) under docs/.
WEB_DIR = REPO_ROOT / "docs"

ITEMS_PATH = SCRAPER_DIR / "items.json"
HISTORY_PATH = SCRAPER_DIR / "price_history.json"
PRICES_PATH = WEB_DIR / "prices.json"
RAW_DUMP_PATH = SCRAPER_DIR / "last_raw_response.json"

FLIPP_SEARCH_URL = "https://backflipp.wishabi.com/flipp/items/search"
DEFAULT_POSTAL = "V5K0A1"  # Vancouver fallback if env var missing
USER_AGENT = "GroceryComparator/1.0 (personal-use; contact via repo issues)"

# Per-item history points to keep
HISTORY_KEEP_DAYS = 90
# Bumped: merchant allowlist support added.


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------

@dataclass
class Offer:
    merchant: str
    name: str
    price: float | None
    pre_price_text: str | None
    post_price_text: str | None
    sale_story: str | None
    valid_from: str | None
    valid_to: str | None
    image_url: str | None
    flyer_item_id: int | str | None
    raw: dict | None = None  # not serialized to web/prices.json

    def to_public(self) -> dict:
        return {
            "merchant": self.merchant,
            "name": self.name,
            "price": self.price,
            "pre_price_text": self.pre_price_text,
            "post_price_text": self.post_price_text,
            "sale_story": self.sale_story,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "image_url": self.image_url,
        }


@dataclass
class ItemResult:
    id: str
    name: str
    type: str
    query: str
    unit_size: dict | None
    offers: list[Offer]
    category: str | None = None
    best_price: float | None = None
    best_unit_price: float | None = None
    median_price: float | None = None
    history: list[dict] = field(default_factory=list)  # [{date, best_price}]
    thresholds: dict = field(default_factory=dict)     # {good, fair}
    user_good_price: float | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _matches_filters(name: str, filters: dict | None) -> bool:
    """Apply optional must_include / any_of / exclude filters from items.json."""
    if not filters:
        return True
    n = _normalize(name)
    must = [_normalize(t) for t in filters.get("must_include", [])]
    if any(t and t not in n for t in must):
        return False
    any_of = [_normalize(t) for t in filters.get("any_of", [])]
    if any_of and not any(t in n for t in any_of):
        return False
    excl = [_normalize(t) for t in filters.get("exclude", [])]
    if any(t and t in n for t in excl):
        return False
    return True


def _merchant_allowed(merchant: str,
                      allowlist: list[str] | None,
                      blocklist: list[str] | None) -> bool:
    """Apply the global merchant allowlist + blocklist from items.json.
    Substring match, case-insensitive."""
    if not merchant:
        return False
    m = _normalize(merchant)
    if blocklist:
        for b in blocklist:
            bn = _normalize(b)
            if bn and bn in m:
                return False
    if allowlist:
        return any(_normalize(a) in m for a in allowlist if a)
    return True  # no allowlist = allow anything (just blocklist)


def _parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        # Some sale_story fields say things like "2 / $5" — handled separately.
        return None


def _unit_price(price: float | None, unit_size: dict | None,
                offer_name: str = "") -> float | None:
    """Compute $ per (unit_size.unit). Best-effort; returns None if not derivable."""
    if price is None or not unit_size:
        return None
    try:
        size_val = float(unit_size["value"])
    except (KeyError, ValueError, TypeError):
        return None
    if size_val <= 0:
        return None
    # Try to detect a different size in the offer name for sanity (e.g. listed
    # 500 g but our target is 1 kg). For v1 we just trust the user's unit_size.
    return round(price / size_val, 4)


# ---------------------------------------------------------------------------
# Flipp client
# ---------------------------------------------------------------------------

def search_flipp(query: str, postal_code: str, *, locale: str = "en-ca",
                 session: requests.Session | None = None,
                 timeout: int = 20) -> list[dict]:
    """Hit Flipp's flyer search endpoint and return the raw items list."""
    s = session or requests
    params = {"locale": locale, "postal_code": postal_code, "q": query}
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    resp = s.get(FLIPP_SEARCH_URL, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return []
    items = data.get("items") or data.get("ecom_items") or []
    if not isinstance(items, list):
        return []
    return items


def offer_from_raw(raw: dict) -> Offer:
    return Offer(
        merchant=raw.get("merchant_name") or raw.get("merchant") or "",
        name=raw.get("name") or raw.get("display_name") or "",
        price=_parse_price(raw.get("current_price") or raw.get("price")),
        pre_price_text=raw.get("pre_price_text"),
        post_price_text=raw.get("post_price_text"),
        sale_story=raw.get("sale_story"),
        valid_from=raw.get("valid_from"),
        valid_to=raw.get("valid_to"),
        image_url=raw.get("clipping_image_url") or raw.get("image_url"),
        flyer_item_id=raw.get("flyer_item_id") or raw.get("id"),
        raw=raw,
    )


def offer_is_active(o: Offer, now: datetime | None = None) -> bool:
    """Drop expired flyer items."""
    if not o.valid_to:
        return True  # assume active when missing
    now = now or datetime.now(timezone.utc)
    try:
        # Flipp returns ISO 8601; tolerate trailing Z
        end = datetime.fromisoformat(o.valid_to.replace("Z", "+00:00"))
        return end >= now
    except Exception:
        return True


# ---------------------------------------------------------------------------
# history & thresholds
# ---------------------------------------------------------------------------

def load_history() -> dict[str, list[dict]]:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def trim_history(points: list[dict]) -> list[dict]:
    if not points:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_KEEP_DAYS)
    out = []
    for p in points:
        try:
            d = datetime.fromisoformat(p["date"].replace("Z", "+00:00"))
            if d >= cutoff:
                out.append(p)
        except Exception:
            continue
    return out


def compute_thresholds(history_points: list[dict]) -> dict:
    """
    Derive 'good' and 'fair' price thresholds from the rolling daily-best history.

    Convention:
      - GOOD  : <= 25th percentile of recent best-prices (a real deal)
      - FAIR  : <= median   (typical sale price)
      - else  : BAD         (above the typical sale price)
    """
    prices = sorted(p["best_price"] for p in history_points if p.get("best_price"))
    if not prices:
        return {}
    n = len(prices)

    def pct(p):
        if n == 1:
            return prices[0]
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return prices[f] + (prices[c] - prices[f]) * (k - f)

    return {
        "good": round(pct(0.25), 2),
        "fair": round(pct(0.50), 2),
        "min_seen": round(prices[0], 2),
        "max_seen": round(prices[-1], 2),
        "samples": n,
    }


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

def process_item(item: dict, postal_code: str,
                 history: dict[str, list[dict]],
                 session: requests.Session,
                 merchant_allowlist: list[str] | None = None,
                 merchant_blocklist: list[str] | None = None) -> ItemResult:
    item_id = item["id"]
    res = ItemResult(
        id=item_id,
        name=item.get("name", item_id),
        type=item.get("type", "generic"),
        query=item.get("query", item.get("name", "")),
        unit_size=item.get("unit_size"),
        offers=[],
        category=item.get("category"),
        user_good_price=item.get("good_price"),
    )

    try:
        raw_items = search_flipp(res.query, postal_code, session=session)
    except requests.HTTPError as e:
        res.notes.append(f"http_error: {e.response.status_code} {e.response.reason}")
        raw_items = []
    except Exception as e:  # network, json, etc.
        res.notes.append(f"error: {type(e).__name__}: {e}")
        raw_items = []

    filters = item.get("match")
    offers: list[Offer] = []
    for r in raw_items:
        try:
            o = offer_from_raw(r)
        except Exception:
            continue
        if not o.price or not o.merchant or not o.name:
            continue
        if not offer_is_active(o):
            continue
        if not _merchant_allowed(o.merchant, merchant_allowlist, merchant_blocklist):
            continue
        if not _matches_filters(o.name, filters):
            continue
        offers.append(o)

    # de-dupe per merchant: keep the cheapest
    by_merch: dict[str, Offer] = {}
    for o in offers:
        cur = by_merch.get(o.merchant)
        if cur is None or (o.price is not None and (cur.price is None or o.price < cur.price)):
            by_merch[o.merchant] = o
    offers = sorted(by_merch.values(), key=lambda x: (x.price if x.price is not None else 1e9))
    res.offers = offers

    if offers:
        prices = [o.price for o in offers if o.price is not None]
        res.best_price = min(prices) if prices else None
        if prices:
            sp = sorted(prices)
            res.median_price = sp[len(sp) // 2]
        if res.best_price is not None and res.unit_size:
            res.best_unit_price = _unit_price(res.best_price, res.unit_size)

    # Update history
    h_points = trim_history(history.get(item_id, []))
    if res.best_price is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        # one point per day; replace if today already there
        h_points = [p for p in h_points if p.get("date", "")[:10] != today]
        h_points.append({
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "best_price": res.best_price,
        })
    history[item_id] = h_points
    res.history = h_points
    res.thresholds = compute_thresholds(h_points)

    return res


def to_public(res: ItemResult) -> dict:
    out = {
        "id": res.id,
        "category": res.category,
        "name": res.name,
        "type": res.type,
        "query": res.query,
        "unit_size": res.unit_size,
        "best_price": res.best_price,
        "best_unit_price": res.best_unit_price,
        "median_price": res.median_price,
        "thresholds": res.thresholds,
        "user_good_price": res.user_good_price,
        "offers": [o.to_public() for o in res.offers],
        "history": [{"date": p["date"][:10], "best_price": p["best_price"]} for p in res.history],
        "notes": res.notes,
    }
    return out


def main() -> int:
    postal = os.environ.get("POSTAL_CODE", DEFAULT_POSTAL).replace(" ", "").upper()
    items_doc = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    items = items_doc.get("items", [])
    if not items:
        print("[scrape] no items configured in items.json")
        return 1

    merchant_allowlist = items_doc.get("merchants") or []
    merchant_blocklist = items_doc.get("merchants_exclude") or []

    history = load_history()
    session = requests.Session()
    results: list[ItemResult] = []

    print(f"[scrape] postal={postal}  items={len(items)}  "
          f"allowlist={len(merchant_allowlist)} merchants  "
          f"blocklist={len(merchant_blocklist)}")
    for i, item in enumerate(items, 1):
        print(f"[scrape]  ({i}/{len(items)}) {item.get('name', item.get('id'))}", flush=True)
        res = process_item(item, postal, history, session,
                           merchant_allowlist=merchant_allowlist,
                           merchant_blocklist=merchant_blocklist)
        if res.notes:
            for n in res.notes:
                print(f"           note: {n}")
        if res.best_price is not None:
            print(f"           best ${res.best_price:.2f} across {len(res.offers)} merchants")
        else:
            print(f"           no offers found")
        results.append(res)
        time.sleep(0.6)  # be polite

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "postal_code": postal,
        "items": [to_public(r) for r in results],
    }
    PRICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRICES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    print(f"[scrape] wrote {PRICES_PATH.relative_to(REPO_ROOT)} ({len(results)} items)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
