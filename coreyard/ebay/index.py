"""The public comparable index: fetch a query's result page, parse it into candidates.

This is the one place CoreYard reaches outside itself for pricing evidence, and it is
deliberately small. Everything above it — which query to ask, which candidates agree with
the part, what the agreed price is — belongs to :mod:`coreyard.ebay.comps`, so swapping the
evidence source is a change to this module and nothing else.

That seam matters because the current source is a public HTML index, parsed with a regex
against someone else's markup. It works, it needs no key, and it costs nothing, which is
why it is the default. It is also the most fragile thing in the channel: the markup can
change without notice and every installation breaks at once. ``COMPS_INDEX_URL`` names the
source so a site can point at its own mirror, and a provider backed by a marketplace's own
API would implement :func:`fetch` and :func:`parse_page` and leave the rest untouched.

Pages are cached on disk by query, and a cached page is never re-fetched. A pricing run is
therefore resumable and re-runnable at no cost, which is what lets the nightly job bank
partial work without a budget to exhaust.
"""

from __future__ import annotations

import html
import math
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

from coreyard.config import _get

# A page smaller than this is an error page, a challenge, or a truncated response — never
# a result set worth parsing or worth keeping in the cache.
MIN_PAGE_BYTES = 10_000

USER_AGENT = "Mozilla/5.0 CoreYard/1.0"
DEFAULT_INDEX_URL = "https://picclick.com/?q="

# Candidates that are not one working instance of the part being priced. Multiples ("set",
# "pair") price several items at once; condition words ("new", "remanufactured") describe a
# different market; services ("repair", "service") are not parts at all.
#
# Some terms here are wheel-specific ("hubcap", "trim ring", "flywheel"), left from when
# this rule served the wheel workflow alone. They are inert for other part types — a tail
# lamp listing does not say "flywheel" — so they are kept verbatim rather than trimmed in a
# structural change, because narrowing them would move prices. The right home for a
# per-type exclusion is a :class:`coreyard.ebay.comps.Rule`.
EXCLUDED = re.compile(
    r"\b(set|pair|[234]\s*(?:pc|piece|wheels?|rims?)|"
    r"wheel\s*(?:and|&)\s*tire|tires?|tyres?|center\s*cap|hubcap|wheel\s*cover|"
    r"steering|flywheel|simulator|trim\s*ring|skin|insert|repair|service|replica|"
    r"aftermarket|reconditioned|remanufactured|refinished|refurbished|new|"
    r"road\s*ready|rtx)\b", re.I,
)

PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)")
ITEM_RE = re.compile(
    r'<li id="item-(?P<item>\d+)">.*?'
    r'<h3 title="(?P<title>.*?)".*?</h3>.*?'
    r'<div class="price"><strong>(?P<price>.*?)</strong>', re.S,
)


def index_url(query: str) -> str:
    """The URL a query is asked at. Read at call time so tests never need the environment."""
    return str(_get("COMPS_INDEX_URL", DEFAULT_INDEX_URL)) + quote_plus(query)


def parse_page(path: Path) -> list[dict]:
    """Candidate listings from a cached result page, as ``{item, title, price}``."""
    if not path.is_file() or path.stat().st_size < MIN_PAGE_BYTES:
        return []
    raw = path.read_text(errors="ignore")
    result = []
    for match in ITEM_RE.finditer(raw):
        title = html.unescape(re.sub(r"<[^>]+>", " ", match.group("title")))
        prices = PRICE_RE.findall(html.unescape(match.group("price")))
        if prices:
            result.append({
                "item": match.group("item"), "title": title.strip(),
                "price": float(prices[-1].replace(",", "")),
            })
    return result


def fetch(query: str, destination: Path, session, delay: float = 1.5) -> bool:
    """Cache one query's result page, returning whether a usable page is on disk.

    A page already cached is never requested again, so re-running a priced batch costs
    nothing and an interrupted run resumes where it stopped.
    """
    if destination.is_file() and destination.stat().st_size >= MIN_PAGE_BYTES:
        return True
    response = session.get(index_url(query),
                           headers={"User-Agent": USER_AGENT}, timeout=45)
    time.sleep(delay)
    if response.status_code != 200 or len(response.content) < MIN_PAGE_BYTES:
        return False
    destination.write_bytes(response.content)
    return True


def percentile(values: list[float], position: float) -> float:
    """Linear-interpolated percentile of a sorted list. ``position`` is a fraction."""
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * position
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)
