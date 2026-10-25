#!/usr/bin/env python3
"""
Optimized Czech Government Auction Scraper
==========================================

This scraper directly calls the API endpoint to get all auction data at once,
eliminating the need for browser automation and complex pagination logic.

The API provides comprehensive auction data including:
- All auction details (title, description, price context, pickup place, dates)
- Knock-down flag (Priklepnuto)
- Auction status and metadata

This optimized version:
- Uses only HTTP requests (no Playwright)
- One API call for the live auctions, one more for the ended ones
  (auctionStatus 'ended' / 'all')
- Applies filters after fetching data
- Much faster and more reliable
"""

import asyncio
import os
import re
import unicodedata
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from apify import Actor
from bs4 import BeautifulSoup

try:
    from .czech_cities import CZECH_CITIES
except ImportError:
    from czech_cities import CZECH_CITIES

try:
    from zoneinfo import ZoneInfo
    _PRAGUE_TZ = ZoneInfo("Europe/Prague")
except Exception:  # no tz database in the image: fall back to the EU rule below
    _PRAGUE_TZ = None


# Bounded exponential backoff for transient fetch failures (timeouts, resets,
# 429/5xx). A single flaky response used to abort an entire category scrape.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def _request_with_retry(client, method, url, attempts=3, **kwargs):
    """Issue a request, retrying transient failures with exponential backoff.

    Returns the final response without raising for status (callers keep their
    own raise_for_status()); re-raises the last transport error if the
    connection itself keeps failing.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            if attempt == attempts:
                raise
            reason = repr(e)
        else:
            if response.status_code not in _RETRYABLE_STATUS or attempt == attempts:
                return response
            reason = f"HTTP {response.status_code}"
        delay = 2.0 * (2 ** (attempt - 1))
        Actor.log.warning(
            f"Transient fetch failure ({reason}); retry {attempt}/{attempts - 1} "
            f"in {delay:.0f}s: {url}"
        )
        await asyncio.sleep(delay)


# --- API query ------------------------------------------------------------------
# Predmet_drazby attributes to request. All of them exist on the server model
# (POST /api/v01/as/descriptions/Classes ["Predmet_drazby"]); the March outage came
# from 'Stav' and 'Poznamka', which do not. A known attribute the anonymous session
# may not read is simply left out of the rows, and so is any null value.
_SELECT_FIELDS = (
    # the SPA's own list query
    "Nazev", "Hlavni_miniatura.ID", "ID", "Akt_hod_prihozu", "Mesto_prevzeti",
    "Aukce.Mesto_prevzeti", "Rozdilnost_prevzeti", "Datum_od_kdy", "Datum_do_kdy",
    # knock-down flag, the item's own description, price context, auction kind and
    # the free-text pickup / viewing places (location fallback)
    "Priklepnuto", "Popis", "Zjistena_cena", "Nejnizsi_podani", "Minimalni_prihoz",
    "Vyse_jistoty", "Aukce.Kod", "Aukce.Druh_dr.Nazev", "Aukce.Misto_vyzvednuti",
    "Aukce.Info_prohlidka",
)


def _api_filter(expression: str) -> str:
    """The data API expects its filter URL-encoded once more on top of httpx's encoding."""
    return urllib.parse.quote(expression, safe="().")


# Published, not stopped auctions whose bidding has not closed yet (what the site
# lists), and the same auctions once the closing time has passed: the API keeps
# serving about three months of them (437 on 2026-10-25, one 200 response).
_LIVE_FILTER = _api_filter(
    "ID > 0 AND Soubor = NULL AND Aukce.Zverejneno = TRUE AND datum_do_kdy > DB.GetDateTime() "
    "and Aukce.Zastaveno <> true and Aukce.Zastav_insolvence <> true")
_ENDED_FILTER = _api_filter(
    "ID > 0 AND Soubor = NULL AND Aukce.Zverejneno = TRUE AND datum_do_kdy < DB.GetDateTime() "
    "and Aukce.Zastaveno <> true and Aukce.Zastav_insolvence <> true")

_AUCTION_STATUSES = ("active", "ended", "all")
_ENDED_MARK = "_gfr_ended"  # set on rows that came from the ended-auctions query
_LIVE_STATUSES = ("upcoming", "running")


# --- value helpers -----------------------------------------------------------------
def _flag(value: Any) -> bool:
    """An API Boolean; missing or null (the API omits nulls) counts as False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "ano")
    return False


def _num(value: Any) -> Optional[float]:
    """An API Money/number value as float, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("\u00a0", "").replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    return None


def _czk(value: Optional[float]) -> str:
    """2120172.0 -> '2 120 172 Kč' (Czech grouping; decimals only when present)."""
    if value is None:
        return ""
    if float(value).is_integer():
        text = f"{value:,.0f}".replace(",", " ")
    else:
        text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} Kč"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _one_line(value: Any) -> str:
    """Free text squeezed onto one 'Label: value' line of full_description."""
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


# --- dates -------------------------------------------------------------------------
# The API's DateTime values carry no offset ("2026-11-24T09:45:00.0000000"). They are
# Europe/Prague wall-clock times: the SPA renders offset-less values as-is (its
# DateUtils.fromXsdDateTime), and auction start/end hours have the same distribution
# in summer (CEST) and winter (CET) — UTC values would shift by an hour.
_API_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def _last_sunday(year: int, month: int) -> date:
    """Last Sunday of March or October (both have 31 days)."""
    day = date(year, month, 31)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _prague_utc_offset(local: datetime) -> timedelta:
    """UTC offset of a Prague wall-clock time under the EU summer-time rule; the hour
    that does not exist (March) or repeats (October) resolves like zoneinfo's fold=0."""
    start = datetime.combine(_last_sunday(local.year, 3), time(3))
    end = datetime.combine(_last_sunday(local.year, 10), time(3))
    return timedelta(hours=2) if start <= local.replace(tzinfo=None) < end else timedelta(hours=1)


def _parse_api_datetime(value: Any) -> Optional[datetime]:
    """API DateTime -> timezone-aware datetime (Europe/Prague when no offset), or None."""
    if not isinstance(value, str):
        return None
    m = _API_DATETIME_RE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, tz = m.groups()
    try:
        naive = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError:
        return None
    if tz:  # an explicit offset is already unambiguous: keep it
        if tz == "Z":
            return naive.replace(tzinfo=timezone.utc)
        digits = tz[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return naive.replace(tzinfo=timezone(offset if tz[0] == "+" else -offset))
    if _PRAGUE_TZ is not None:
        return naive.replace(tzinfo=_PRAGUE_TZ)
    return naive.replace(tzinfo=timezone(_prague_utc_offset(naive)))


def _iso_datetime(value: Any) -> str:
    """API DateTime -> ISO 8601 with its UTC offset, e.g. '2026-11-24T09:45:00+01:00';
    '' when missing, the raw text (max 100 chars, the DB column) when unparseable."""
    parsed = _parse_api_datetime(value)
    if parsed is not None:
        return parsed.isoformat(timespec="seconds")
    if value is None:
        return ""
    return str(value).strip()[:100]


# --- location ------------------------------------------------------------------------
# Coordinates come only from the town gazetteer in czech_cities.py, matched on whole
# names: a place that is not in it gets None rather than another town's coordinates
# (the old substring/district fallbacks could do that).
def _fold(text: str) -> str:
    """Lower case without diacritics, one output char per input char."""
    return "".join(unicodedata.normalize("NFD", ch)[0].lower()[:1] for ch in text)


# Okres (district) names the gazetteer maps to the city centre although they are the
# rural ring around the city: a village there must not get the city's coordinates.
_NOT_A_TOWN = ("praha-vychod", "praha-zapad", "plzen-jih", "plzen-sever", "brno-venkov")
_TOWNS: Dict[str, Tuple[float, float]] = {}
for _name, _coords in CZECH_CITIES.items():
    _key = re.sub(r"\s+", " ", _fold(_name)).strip()
    if _key not in _NOT_A_TOWN:
        _TOWNS.setdefault(_key, _coords)
_TOWN_KEYS = sorted(_TOWNS, key=len, reverse=True)  # longest name first
# Statutory cities whose districts are written "City-District" / "City District"
# ("Brno-Královo Pole", "Ostrava Poruba", "Pardubice II"): still the same city.
_DISTRICT_CITIES = {
    "praha", "brno", "ostrava", "plzen", "usti nad labem", "liberec", "olomouc",
    "pardubice", "hradec kralove", "ceske budejovice", "zlin", "opava", "karvina",
    "havirov", "kladno", "jihlava", "karlovy vary",
}
# Words that continue a place name: "Hranice u Aše", "Staré Město pod Sněžníkem".
_NAME_CONNECTORS = {"u", "nad", "pod", "na", "v", "ve", "pri"}
_ROMAN_RE = re.compile(r"^[IVX]+\.?$")
_PSC_RE = re.compile(r"(?<!\d)[1-7]\d{2} ?\d{2}(?!\d)")
_PART_MARKER_RE = re.compile(
    r"^(?:k\.\s*ú\.|kú\b|katastrální\s+území|obec\b|SSHR\b|sklad\s+SSHR\b)[\s,:-]*", re.IGNORECASE)
# "obec X" / "v obci X" / "obec a okres X" in a real-estate text names the municipality
_OBEC_RE = re.compile(r"\b(?:obec\s+a\s+okres|[Vv]\s+obci|[Oo]bec)\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])")
_PRAHA_RE = re.compile(r"\bhl(?:avní)?\.?\s*m(?:ěsto|\.)?\s*Praha\b", re.IGNORECASE)


def _name_ends_here(key: str, rest: str) -> bool:
    """Does a gazetteer name matched at the start of a text end where `rest` starts?"""
    if not rest:
        return True
    first = rest[0]
    if first in "-–":  # "Brno-Královo Pole" (district) vs "Sedlec-Prčice" (other town)
        return key in _DISTRICT_CITIES
    if first.isalpha():  # mid-word: "Břeclavská" is not "Břeclav"
        return False
    if not first.isspace():  # punctuation or a digit ("Praha4")
        return True
    nxt = rest.lstrip()
    if not nxt or nxt[0] in "-–" or not nxt[0].isalpha():  # " - Střekov", " 4", " (..."
        return True
    word = re.match(r"[^\s,.;:()/*-]+", nxt).group(0)
    if _ROMAN_RE.match(word):  # "Pardubice II"
        return True
    if word[0].isupper():  # "Nová Ves", "Staré Město" — unless a city district
        return key in _DISTRICT_CITIES
    return _fold(word) not in _NAME_CONNECTORS


def _town_at(text: str) -> Optional[Tuple[str, Tuple[float, float]]]:
    """The gazetteer town that `text` starts with, as (name as written, (lat, lng))."""
    text = re.sub(r"\s+", " ", text).strip()
    folded = _fold(text)
    compact = re.sub(r"[\s\-–]+", "-", folded)
    for blocked in _NOT_A_TOWN:  # "Praha-východ", "Brno - venkov": a district, not the city
        if compact.startswith(blocked) and not compact[len(blocked):len(blocked) + 1].isalpha():
            return None
    for key in _TOWN_KEYS:
        if folded.startswith(key) and _name_ends_here(key, text[len(key):]):
            return text[:len(key)], _TOWNS[key]
    return None


def _town_in_place_field(value: Any) -> Optional[Tuple[str, Tuple[float, float]]]:
    """Town named by a pickup-place field such as 'Štěpánská 28, Praha 1',
    'Vlkov, Osová Bitýška, okr. Žďár nad Sázavou', 'SSHR Polerady', 'k.ú. Jince',
    'Břeclavská 1100, 691 42 Valtice.' — comma parts left to right."""
    if not isinstance(value, str):
        return None
    # "Podolí (Uh. Hradiště)": the bracket is a district hint, not the place
    for part in re.sub(r"\([^)]*\)", " ", value).split(","):
        part = part.strip(" .;:\t\n")
        if not part or re.match(r"(?i)^okr(?:es\b|\.)", part):  # a district is not a place
            continue
        part = _PSC_RE.sub(" ", part, count=1) if _PSC_RE.match(part) else part
        part = _PART_MARKER_RE.sub("", part.strip())
        found = _town_at(part)
        if found:
            return found
    return None


def _town_after_psc(value: Any) -> Optional[Tuple[str, Tuple[float, float]]]:
    """Town after a postcode in free text ('..., 400 21 Ústí nad Labem, 9:00 ...')."""
    if not isinstance(value, str):
        return None
    for m in _PSC_RE.finditer(value):
        found = _town_at(value[m.end():].lstrip(" ,"))
        if found:
            return found
    return None


def _resolve_place(api_data: Dict[str, Any]) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
    """Coordinates of an auction item and the gazetteer town they belong to.

    1. Real estate: the municipality its own text names ("katastrální území Královo
       Pole ... obec Brno"). If that municipality is not in the gazetteer, stop: the
       pickup town of a property may be a homonym or the tax office.
    2. The item's pickup town (Mesto_prevzeti), then the auction's.
    3. A postcode + town in the auction's pickup or viewing text.
    """
    title = _text(api_data.get("Nazev"))
    popis = _text(api_data.get("Popis"))
    kind = _fold(_text(api_data.get("Aukce.Druh_dr.Nazev")))
    is_real_estate = "nemovit" in kind or (
        not kind and re.search(r"katastráln|k\.\s?ú\.", f"{title} {popis}", re.IGNORECASE))
    if is_real_estate:
        text = _PRAHA_RE.sub("Praha", f"{title}\n{popis}")
        m = _OBEC_RE.search(text)
        if m:
            found = _town_at(text[m.end():])
            return (found[1], found[0]) if found else (None, None)
    for field in ("Mesto_prevzeti", "Aukce.Mesto_prevzeti"):
        found = _town_in_place_field(api_data.get(field))
        if found:
            return found[1], found[0]
    for field in ("Aukce.Misto_vyzvednuti", "Aukce.Info_prohlidka"):
        found = _town_after_psc(api_data.get(field))
        if found:
            return found[1], found[0]
    return None, None


class _FailRunOnCrash:
    """Marks the actor_runs row 'failed' if the run dies with an unhandled error.

    Historically a terminal status was written only on the happy path, so a crash
    (or an Apify hard-timeout kill) left the row 'running' forever and the web app
    could not tell a dead run from a live one — sold-detection then mistreated the
    run's listings. Used as `async with Actor, _FailRunOnCrash():` so it exits
    before Actor; the exception still propagates and fails the platform run too.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                # db_manager is only imported inside main() in this scraper, so
                # resolve it lazily here as well.
                try:
                    from .database import db_manager
                except ImportError:
                    from database import db_manager
                Actor.log.error(f"Run failed with unhandled error: {exc!r}")
                db_manager.update_actor_run_status("failed", 0)
                db_manager.close_pool()
            except Exception as finalize_error:
                Actor.log.error(f"Could not mark run failed: {finalize_error}")
        return False


class OptimizedGovernmentAuctionScraper:
    """Optimized scraper that uses browser for session establishment and HTTP for API calls."""
    
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.api_url = "https://drazby.fs.gov.cz/api/v02/as/data/Predmet_drazby"
        # Why this run is incomplete (session, fetch or parse failed, or the API
        # served a partial set); main() marks such a run 'failed', not 'completed'.
        self.incomplete_reasons: List[str] = []
        
    @staticmethod
    def normalise_status(auction_status: Any) -> str:
        """'active' (default) | 'ended' | 'all'; anything else falls back to 'active'."""
        status = str(auction_status or "active").strip().lower()
        if status not in _AUCTION_STATUSES:
            Actor.log.warning(f"Unknown auctionStatus {auction_status!r}; using 'active'")
            return "active"
        return status

    async def scrape_all_auctions(
        self,
        max_listings: int = 0,
        auction_status: str = "active",
        price_min: int = 0,
        price_max: int = 0,
        location: Optional[str] = None,
        search_query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Scrape all auctions from the API and apply filters.
        
        Args:
            max_listings: Maximum number of listings to return (0 = all)
            auction_status: "active" = live auctions not knocked down; "ended" =
                auctions whose bidding has closed (knocked down or not); "all" = both
            price_min: Minimum price filter in CZK
            price_max: Maximum price filter in CZK
            location: Location filter (city/region)
            search_query: Search term to filter titles/descriptions
            
        Returns:
            List of auction dictionaries
        """
        status = self.normalise_status(auction_status)
        try:
            # Step 1: Establish session via auth API
            Actor.log.info("Establishing session with the API...")
            session_ok = await self._establish_session()

            if not session_ok:
                Actor.log.error("Failed to establish session")
                self.incomplete_reasons.append("could not establish an API session")
                return []

            # Step 2: Live auctions for 'active'/'all', ended ones for 'ended'/'all'
            all_auctions: List[Dict[str, Any]] = []
            if status in ("active", "all"):
                Actor.log.info("Fetching live auction data from API...")
                all_auctions.extend(await self._fetch_auctions("live"))
            if status in ("ended", "all"):
                Actor.log.info("Fetching ended auction data from API...")
                all_auctions.extend(await self._fetch_auctions("ended"))
            
            if not all_auctions:
                Actor.log.warning("⚠️ No auction data received from API")
                return []
            
            Actor.log.info(f"✅ Successfully fetched {len(all_auctions)} total auctions from API")
            
            # Step 3: Apply filters
            Actor.log.info("🔍 Applying filters to auction data...")
            filtered_auctions = self._apply_filters(
                all_auctions, status, price_min, price_max, location, search_query
            )
            
            Actor.log.info(f"📊 After filtering: {len(filtered_auctions)} auctions match criteria")
            
            # Step 4: Limit results if requested
            if max_listings > 0 and len(filtered_auctions) > max_listings:
                filtered_auctions = filtered_auctions[:max_listings]
                Actor.log.info(f"✂️ Limited to {max_listings} auctions as requested")
            
            # Step 5: Convert to standardized format
            Actor.log.info("🔄 Converting to standardized format...")
            standardized_auctions = []
            for auction_data in filtered_auctions:
                standardized = self._convert_to_standard_format(auction_data)
                if standardized:
                    standardized_auctions.append(standardized)
            
            Actor.log.info(f"🎯 Final result: {len(standardized_auctions)} auctions ready for storage")
            return standardized_auctions
            
        except Exception as e:
            Actor.log.error(f"❌ Error in scrape_all_auctions: {e}")
            self.incomplete_reasons.append(f"scrape_all_auctions: {e!r}")
            return []
    
    async def _establish_session(self) -> bool:
        """Establish a session via the site's auth API (no browser needed)."""
        try:
            # Step 1: Auth check — creates a server-side session
            resp = await self.client.post(
                "https://drazby.fs.gov.cz/api/v01/as/auth/check",
                params={"winAuth": "true", "sso": "true"},
            )
            Actor.log.info(f"Auth check: {resp.status_code}")

            # Step 2: Login as public/anonymous user
            resp = await self.client.get(
                "https://drazby.fs.gov.cz/api/v01/as/owmanager/Login",
                params={"language": "cs-cz"},
            )
            Actor.log.info(f"Login: {resp.status_code}")

            return resp.status_code == 200
        except Exception as e:
            Actor.log.error(f"Failed to establish session: {e}")
            return False
    
    async def _fetch_auctions(self, which: str) -> List[Dict[str, Any]]:
        """Fetch the live (`which='live'`) or the ended (`'ended'`) auctions in one call,
        with the SPA's list filter (the ended query flips its closing-time test)."""
        ended = which == "ended"
        label = "ended-auction API" if ended else "auction API"
        try:
            params = {
                "select": ",".join(_SELECT_FIELDS),
                "filter": _ENDED_FILTER if ended else _LIVE_FILTER,
                "orderBy": "DESC Datum_do_kdy" if ended else "DESC Aukce.Datum_zverejneni",
            }
            response = await _request_with_retry(self.client, "GET", self.api_url, params=params, timeout=60.0)

            if response.status_code not in (200, 206):
                Actor.log.error(f"{label} returned {response.status_code}: {response.text[:500]}")
                self.incomplete_reasons.append(f"{label}: HTTP {response.status_code}")
                return []

            if response.status_code == 206:
                # 206 Partial Content: the API served only a range of the matching
                # rows (it accepts 'Range: rows=...'), so the rest would be missed.
                content_range = (response.headers.get('content-range')
                                 or response.headers.get('x-content-range') or 'n/a')
                Actor.log.warning(f"{label} returned 206 Partial Content — the auction list is incomplete")
                self.incomplete_reasons.append(
                    f"{label} returned 206 Partial Content (Content-Range: {content_range})"
                )

            data = response.json()

            if isinstance(data, list):
                Actor.log.info(f"{label} returned {len(data)} auction records")
                if ended:
                    for row in data:
                        if isinstance(row, dict):
                            row[_ENDED_MARK] = True
                return data
            else:
                Actor.log.warning(f"Unexpected API response format: {type(data)}")
                self.incomplete_reasons.append(
                    f"{label}: unexpected response format ({type(data).__name__})"
                )
                return []

        except Exception as e:
            Actor.log.error(f"Failed to fetch auctions from {label}: {e}")
            self.incomplete_reasons.append(f"{label}: {e!r}")
            return []
    
    def _apply_filters(
        self,
        auctions: List[Dict[str, Any]],
        auction_status: str,
        price_min: int,
        price_max: int,
        location: Optional[str],
        search_query: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Apply filters to the auction data."""
        filtered = auctions.copy()
        
        # Filter by auction status (a missing Priklepnuto means not knocked down)
        if auction_status == "active":
            filtered = [a for a in filtered if not a.get(_ENDED_MARK) and not _flag(a.get('Priklepnuto'))]
            Actor.log.info(f"🟢 Filtered to active auctions: {len(filtered)} remaining")
        elif auction_status == "ended":
            filtered = [a for a in filtered if a.get(_ENDED_MARK) or _flag(a.get('Priklepnuto'))]
            Actor.log.info(f"🔴 Filtered to ended auctions: {len(filtered)} remaining")
        # "all" means no status filtering
        
        # Filter by price range
        if price_min > 0 or price_max > 0:
            price_filtered = []
            for auction in filtered:
                price = auction.get('Akt_hod_prihozu', 0)
                if isinstance(price, (int, float)):
                    if price_min > 0 and price < price_min:
                        continue
                    if price_max > 0 and price > price_max:
                        continue
                    price_filtered.append(auction)
            filtered = price_filtered
            Actor.log.info(f"💰 Price filtered: {len(filtered)} remaining")
        
        # Filter by location
        if location and location.strip():
            location_lower = location.lower().strip()
            location_filtered = []
            for auction in filtered:
                auction_location = (_text(auction.get('Mesto_prevzeti'))
                                    or _text(auction.get('Aukce.Mesto_prevzeti'))).lower()
                if location_lower in auction_location:
                    location_filtered.append(auction)
            filtered = location_filtered
            Actor.log.info(f"📍 Location filtered: {len(filtered)} remaining")
        
        # Filter by search query
        if search_query and search_query.strip():
            query_lower = search_query.lower().strip()
            search_filtered = []
            for auction in filtered:
                title = _text(auction.get('Nazev')).lower()
                description = _text(auction.get('Popis')).lower()
                if query_lower in title or query_lower in description:
                    search_filtered.append(auction)
            filtered = search_filtered
            Actor.log.info(f"🔍 Search filtered: {len(filtered)} remaining")
        
        return filtered
    
    def _convert_to_standard_format(self, api_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert API data to standardized listing format."""
        auction_id = api_data.get('ID') if isinstance(api_data, dict) else None
        try:
            # Extract auction ID
            if auction_id in (None, ''):
                Actor.log.warning("⚠️ Skipping an auction record without ID")
                return None
            auction_id = str(auction_id)
            
            # Build auction URL
            auction_url = f"https://drazby.fs.gov.cz/client/main?formName=predmet&selectedID={auction_id}"
            
            title = _text(api_data.get('Nazev'))
            # The item's own description (Popis); before it was selected the
            # description was a copy of the title.
            popis = _text(api_data.get('Popis'))
            # price = current bid (Akt_hod_prihozu), which equals the minimum bid
            # until someone bids.
            price = _num(api_data.get('Akt_hod_prihozu'))
            price = price if price is not None else 0.0
            appraisal = _num(api_data.get('Zjistena_cena'))
            minimum_bid = _num(api_data.get('Nejnizsi_podani'))
            minimum_increment = _num(api_data.get('Minimalni_prihoz'))
            deposit = _num(api_data.get('Vyse_jistoty'))
            kind = _text(api_data.get('Aukce.Druh_dr.Nazev'))
            # Location: use Mesto_prevzeti, fall back to Aukce.Mesto_prevzeti
            location = _text(api_data.get('Mesto_prevzeti')) or _text(api_data.get('Aukce.Mesto_prevzeti'))
            coordinates, geocoded_place = _resolve_place(api_data)

            # Dates: Prague wall-clock time in the API -> ISO 8601 with offset
            date_from = _iso_datetime(api_data.get('Datum_od_kdy'))
            date_to = _iso_datetime(api_data.get('Datum_do_kdy'))
            starts = _parse_api_datetime(api_data.get('Datum_od_kdy'))

            is_knocked_down = _flag(api_data.get('Priklepnuto'))
            if api_data.get(_ENDED_MARK):
                auction_status = 'ended'
            elif starts is not None and starts > datetime.now(timezone.utc):
                auction_status = 'upcoming'
            else:
                auction_status = 'running'

            # No image URL: the site's own thumbnail URL
            # (client/thumbnail_images/Priloha_predmetu/{Hlavni_miniatura.ID}) only
            # serves the image inside a drazby session; without its cookies it
            # redirects to the NIA login, so the map's <img> would be broken.
            image_url = ''

            # Full description: the item's description, then the auction facts as
            # "Label: value" lines (the web app's normalizer reads "Konec dražby").
            facts = [
                ("Druh dražby", kind),
                ("Zjištěná cena", _czk(appraisal)),
                ("Nejnižší podání", _czk(minimum_bid)),
                ("Minimální příhoz", _czk(minimum_increment)),
                ("Dražební jistota", _czk(deposit)),
                ("Začátek dražby", date_from),
                ("Konec dražby", date_to),
                ("Místo převzetí", _one_line(location)),
                ("Místo vyzvednutí", _one_line(api_data.get('Aukce.Misto_vyzvednuti'))),
                ("Prohlídka", _one_line(api_data.get('Aukce.Info_prohlidka'))),
                ("Číslo dražby", _one_line(api_data.get('Aukce.Kod'))),
            ]
            fact_lines = "\n".join(f"{label}: {value}" for label, value in facts if value)
            full_description = "\n\n".join(part for part in (popis or title, fact_lines) if part)
            
            # Build images array (a plain list — database.py JSON-encodes it)
            images = [image_url] if image_url else []

            return {
                'id': auction_id,
                'title': title,
                'url': auction_url,
                'category': 'government_auction',
                'price': price,
                'price_text': f"{price:,.0f} Kč" if price > 0 else '',
                'description': popis or title,
                'full_description': full_description,
                'location': location,
                'views': 0,
                'date': date_from,
                'is_top': False,
                'image_url': image_url,
                'contact_name': '',
                'phone': '',
                'coordinates_lat': coordinates[0] if coordinates else None,
                'coordinates_lng': coordinates[1] if coordinates else None,
                'images': images,
                'similar_listings': [],
                'scraped_at': datetime.now().isoformat(),
                'is_knocked_down': is_knocked_down,
                'auction_status': auction_status,
                'date_from': date_from,
                'date_to': date_to,
                # dataset-only extras (database.py stores the columns above)
                'appraised_price': appraisal,
                'minimum_bid': minimum_bid,
                'minimum_increment': minimum_increment,
                'deposit': deposit,
                'auction_kind': kind,
                'geocoded_place': geocoded_place,
            }
            
        except Exception as e:
            # A dropped auction would look sold to the web app, so the run is incomplete.
            Actor.log.warning(f"⚠️ Error converting auction data: {e}")
            self.incomplete_reasons.append(f"could not convert auction {auction_id}: {e!r}")
            return None


async def main():
    """Main function to run the optimized scraper."""
    # Initialize the Actor
    async with Actor, _FailRunOnCrash():
        Actor.log.info("🚀 Starting Optimized Czech Government Auction Scraper")
        Actor.log.info("=" * 60)
        
        # Get input parameters
        input_data = await Actor.get_input() or {}
        
        max_listings = input_data.get('maxListings') or 0
        auction_status = OptimizedGovernmentAuctionScraper.normalise_status(input_data.get('auctionStatus'))
        price_min = input_data.get('priceMin') or 0
        price_max = input_data.get('priceMax') or 0
        location = input_data.get('location') or ''
        search_query = input_data.get('searchQuery') or ''
        
        Actor.log.info(f"📋 Configuration:")
        Actor.log.info(f"   Max listings: {max_listings if max_listings > 0 else 'unlimited'}")
        Actor.log.info(f"   Auction status: {auction_status}")
        Actor.log.info(f"   Price range: {price_min}-{price_max if price_max > 0 else 'unlimited'} CZK")
        Actor.log.info(f"   Location filter: {location if location else 'none'}")
        Actor.log.info(f"   Search query: {search_query if search_query else 'none'}")
        
        # Initialize database connection
        db_manager_available = False
        try:
            from .database import db_manager

            # Get scraper name from environment or use default
            scraper_name = os.environ.get('SCRAPER_NAME', 'gfr')
            db_manager.scraper_name = scraper_name

            db_manager.initialize_pool()

            # Create actor run record
            actor_run_id = os.environ.get('APIFY_ACTOR_RUN_ID') or os.environ.get('ACTOR_RUN_ID', 'local-run')
            actor_run_start = datetime.now()
            db_manager.set_actor_run_info(actor_run_id, actor_run_start)
            
            db_manager.create_actor_run(
                categories=['government_auction'],  # Single category since API provides all
                max_listings=max_listings,
                search_query=search_query,
                location=location,
                price_min=price_min,
                price_max=price_max
            )
            
            Actor.log.info("✅ Database connection established and actor run created")
            db_manager_available = True
            
        except Exception as e:
            Actor.log.error(f"❌ Failed to initialize database: {e}")
            Actor.log.warning("⚠️ Continuing without database integration - data will be stored in Apify dataset only")
            db_manager_available = False
        
        # Create HTTP client with proper headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'cs,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            scraper = OptimizedGovernmentAuctionScraper(client)

            # Scrape all auctions (no browser needed — uses HTTP auth flow)
            all_auctions = await scraper.scrape_all_auctions(
                max_listings=max_listings,
                auction_status=auction_status,
                price_min=price_min,
                price_max=price_max,
                location=location,
                search_query=search_query
            )
            
            incomplete_reasons = list(scraper.incomplete_reasons)
            saved = 0  # rows actually inserted into the database
            # The map's database holds live auctions only (upcoming or running, not
            # knocked down); ended ones go to the Apify dataset alone.
            db_auctions = [
                auction for auction in all_auctions
                if auction.get('auction_status') in _LIVE_STATUSES and not auction.get('is_knocked_down', True)
            ]

            # Save data to both Apify dataset and database
            if all_auctions:
                # Save to Apify dataset (all auctions)
                await Actor.push_data(all_auctions)
                Actor.log.info(f"💾 Saved {len(all_auctions)} auctions to Apify dataset")
                
                # Save to database if available (only live auctions)
                if db_manager_available:
                    try:
                        Actor.log.info(f"🔍 Filtering for database: {len(db_auctions)} live auctions out of {len(all_auctions)} total")
                        
                        if db_auctions:
                            Actor.log.info("💾 Saving live auctions to database...")
                            db_manager.insert_listings(db_auctions)
                            saved = len(db_auctions)
                            Actor.log.info(f"✅ Saved {len(db_auctions)} live listings to database")
                        else:
                            Actor.log.info("ℹ️ No live auctions to save to database")
                    except Exception as e:
                        Actor.log.error(f"❌ Failed to save listings to database: {e}")
                        # Try to refresh the connection pool and retry once
                        try:
                            Actor.log.info("🔄 Attempting to refresh connection pool and retry database operation")
                            db_manager.refresh_pool()
                            if db_auctions:
                                db_manager.insert_listings(db_auctions)
                                saved = len(db_auctions)
                                Actor.log.info(f"✅ Successfully saved {len(db_auctions)} live listings to database after retry")
                        except Exception as retry_e:
                            Actor.log.error(f"❌ Failed to save listings to database even after retry: {retry_e}")
                            incomplete_reasons.append(f"database insert failed: {retry_e!r}")
            else:
                Actor.log.warning("⚠️ No auctions found matching the specified criteria")

            if auction_status == 'ended' and db_manager_available:
                Actor.log.warning("auctionStatus 'ended' fills the Apify dataset only — the database keeps "
                                  "live auctions — so the database run is closed as 'failed' (sold-detection ignores it)")
            
            # Zero auctions almost always means upstream breakage (API change,
            # auth/session failure) rather than an empty market — mark the run
            # 'failed' so sold-detection ignores it instead of flagging every
            # previously-seen listing as sold. The same goes for a run that stopped
            # early (session/fetch failure, a 206 partial list, a failed insert):
            # sold-detection would flag every listing it missed as sold.
            run_status = 'completed' if saved and not incomplete_reasons else 'failed'
            for reason in incomplete_reasons:
                Actor.log.error(f"Run incomplete — {reason}")

            # Update actor run status in database
            if db_manager_available:
                try:
                    db_manager.update_actor_run_status(run_status, saved)
                    Actor.log.info("✅ Updated actor run status in database")
                except Exception as e:
                    Actor.log.error(f"❌ Failed to update actor run status: {e}")

            if incomplete_reasons:
                more = f" (+{len(incomplete_reasons) - 1} more)" if len(incomplete_reasons) > 1 else ""
                try:
                    await Actor.set_status_message(f"Stopped early: {incomplete_reasons[0]}{more}")
                except Exception as e:
                    Actor.log.warning(f"Could not set the run's status message: {e}")
            
            # Final summary
            Actor.log.info("=" * 60)
            if run_status == 'completed':
                Actor.log.info("🎉 SCRAPING COMPLETED SUCCESSFULLY")
            else:
                Actor.log.warning("⚠️ SCRAPING FINISHED — run status: failed")
            Actor.log.info(f"📊 Total auctions scraped: {len(all_auctions)}")
            if saved:
                Actor.log.info(f"💾 Data saved to: Apify dataset ({len(all_auctions)} auctions) and database ({saved} live auctions)")
            else:
                Actor.log.info(f"💾 Data saved to: {'Apify dataset only' if all_auctions else 'nowhere'}")
            Actor.log.info("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
