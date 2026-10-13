import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Literal
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, quote_plus

import httpx
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

DATABASE_PATH = os.getenv("DATABASE_PATH", "./realestate.db")
API_KEY = os.getenv("API_KEY", "")
REAL_ESTATE_DOMAIN = "realestate.com.au"
DEFAULT_SEARCH_MAX_PAGES = 20
MAX_SEARCH_PRICE = 999_999_999
SEARCH_MAX_PAGES = DEFAULT_SEARCH_MAX_PAGES
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)

app = FastAPI(title="Real Estate AU Scraper API", version="1.0.0")


def db_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def init_db():
    connection = db_connection()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            address TEXT NOT NULL,
            price_text TEXT,
            price_min INTEGER,
            price_max INTEGER,
            bedrooms INTEGER,
            bathrooms INTEGER,
            cars INTEGER,
            land_area_m2 REAL,
            building_area_m2 REAL,
            property_type TEXT,
            listing_type TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            raw_card_html TEXT
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_listings_address ON listings(address)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price_min, price_max)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_listings_bedrooms ON listings(bedrooms)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_listings_area ON listings(land_area_m2)")
    connection.commit()
    connection.close()


@app.on_event("startup")
def startup():
    global SEARCH_MAX_PAGES
    SEARCH_MAX_PAGES = parse_search_max_pages(os.getenv("SEARCH_MAX_PAGES"))
    init_db()


def parse_search_max_pages(value: Optional[str]) -> int:
    if value is None:
        return DEFAULT_SEARCH_MAX_PAGES
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError("Invalid SEARCH_MAX_PAGES configuration: must be an integer >= 1") from exc
    if parsed < 1:
        raise RuntimeError("Invalid SEARCH_MAX_PAGES configuration: must be an integer >= 1")
    return parsed


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_number(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.search(r"([\d,.]+)", value)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_integer(value: Optional[str]) -> Optional[int]:
    number = parse_number(value)
    return int(number) if number is not None else None


def parse_price(price_text: Optional[str]):
    if not price_text:
        return None, None

    values = re.findall(r"\$?\s*([\d,.]+)\s*([mk])?", price_text.lower())
    numbers = []
    for number_text, suffix in values:
        number = float(number_text.replace(",", ""))
        if suffix == "m":
            number *= 1_000_000
        elif suffix == "k":
            number *= 1_000
        numbers.append(int(number))

    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def extract_aria_value(card, pattern: str) -> Optional[str]:
    for element in card.select("[aria-label]"):
        aria_label = element.get("aria-label", "")
        if re.search(pattern, aria_label, flags=re.IGNORECASE):
            return aria_label
    return None


def extract_listing_id(url: str) -> str:
    match = re.search(r"-(\d+)(?:[/?#]|$)", url)
    if match:
        return match.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def detect_listing_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if "/buy" in path:
        return "buy"
    if "/rent" in path:
        return "rent"
    if "/sold" in path:
        return "sold"
    return "unknown"


def parse_card(card, page_url: str):
    link = card.select_one("a[href*='/property-']")
    address_element = card.select_one(".residential-card__address-heading a")
    price_element = card.select_one(".property-price")

    if not link or not address_element:
        return None

    url = urljoin(page_url, link.get("href"))
    address = address_element.get_text(" ", strip=True)
    price_text = price_element.get_text(" ", strip=True) if price_element else None

    bedroom_label = extract_aria_value(card, r"\bbedroom")
    bathroom_label = extract_aria_value(card, r"\bbathroom")
    car_label = extract_aria_value(card, r"\bcar space")
    land_label = extract_aria_value(card, r"\bland size")
    building_label = extract_aria_value(card, r"\bbuilding size|\bfloor area")

    property_type = None
    primary_list = card.select_one("ul[aria-label]")
    if primary_list:
        text = primary_list.get_text(" ", strip=True)
        known_types = [
            "house", "unit", "apartment", "townhouse", "villa", "land",
            "acreage", "retirement", "block of units",
        ]
        for known_type in known_types:
            if re.search(rf"\b{re.escape(known_type)}\b", text, re.IGNORECASE):
                property_type = known_type
                break

    price_min, price_max = parse_price(price_text)
    return {
        "listing_id": extract_listing_id(url),
        "url": url,
        "address": address,
        "price_text": price_text,
        "price_min": price_min,
        "price_max": price_max,
        "bedrooms": parse_integer(bedroom_label),
        "bathrooms": parse_integer(bathroom_label),
        "cars": parse_integer(car_label),
        "land_area_m2": parse_number(land_label),
        "building_area_m2": parse_number(building_label),
        "property_type": property_type,
        "listing_type": detect_listing_type(page_url),
        "raw_card_html": str(card),
    }


def parse_page(html: str, page_url: str):
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div.residential-card__content")
    if not cards:
        cards = soup.select("[data-testid*='residential-card']")

    listings = []
    for card in cards:
        listing = parse_card(card, page_url)
        if listing:
            listings.append(listing)
    return listings


def save_listings(listings):
    if not listings:
        return 0

    connection = db_connection()
    now = utc_now()
    saved = 0

    for listing in listings:
        connection.execute(
            """
            INSERT INTO listings (
                listing_id, url, address, price_text, price_min, price_max,
                bedrooms, bathrooms, cars, land_area_m2, building_area_m2,
                property_type, listing_type, first_seen, last_seen, raw_card_html
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                url = excluded.url,
                address = excluded.address,
                price_text = excluded.price_text,
                price_min = excluded.price_min,
                price_max = excluded.price_max,
                bedrooms = excluded.bedrooms,
                bathrooms = excluded.bathrooms,
                cars = excluded.cars,
                land_area_m2 = excluded.land_area_m2,
                building_area_m2 = excluded.building_area_m2,
                property_type = excluded.property_type,
                listing_type = excluded.listing_type,
                last_seen = excluded.last_seen,
                raw_card_html = excluded.raw_card_html
            """,
            (
                listing["listing_id"], listing["url"], listing["address"],
                listing["price_text"], listing["price_min"], listing["price_max"],
                listing["bedrooms"], listing["bathrooms"], listing["cars"],
                listing["land_area_m2"], listing["building_area_m2"],
                listing["property_type"], listing["listing_type"], now, now,
                listing["raw_card_html"],
            ),
        )
        saved += 1

    connection.commit()
    connection.close()
    return saved


def validate_target_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Only HTTP and HTTPS URLs are allowed")

    hostname = (parsed.hostname or "").lower()
    if hostname != REAL_ESTATE_DOMAIN and not hostname.endswith("." + REAL_ESTATE_DOMAIN):
        raise HTTPException(status_code=400, detail="Only realestate.com.au URLs are allowed")


def build_page_url(url: str, page_number: int) -> str:
    if page_number == 1:
        return url
    if re.search(r"/list-\d+", url):
        return re.sub(r"/list-\d+", f"/list-{page_number}", url)
    return url.rstrip("/") + f"/list-{page_number}"


def scrape_url(url: str, max_pages: int = 1):
    validate_target_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    all_listings = []
    seen_ids = set()
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
        for page_number in range(1, max_pages + 1):
            page_url = build_page_url(url, page_number)
            response = client.get(page_url)
            response.raise_for_status()
            listings = parse_page(response.text, str(response.url))
            if not listings:
                break

            new_count = 0
            for listing in listings:
                if listing["listing_id"] not in seen_ids:
                    seen_ids.add(listing["listing_id"])
                    all_listings.append(listing)
                    new_count += 1
            if new_count == 0:
                break

    return save_listings(all_listings)


class ScrapeRequest(BaseModel):
    url: str
    max_pages: int = 1


SUPPORTED_PROPERTY_TYPES = {
    "house": "house",
    "townhouse": "townhouse",
    "villa": "villa",
    "land": "land",
    "acreage": "acreage",
    "retirement": "retirement",
    "apartment": "apartment-unit+apartment",
    "unit": "apartment-unit+apartment",
    "flat": "apartment-unit+apartment",
    "apartment-unit+apartment": "apartment-unit+apartment",
}


def _clean_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string")
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


def _normalize_price_bound(field_name: str, value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    if not float(value).is_integer():
        raise ValueError(f"{field_name} must be a whole number")
    normalized_value = int(value)
    if normalized_value > MAX_SEARCH_PRICE:
        raise ValueError(f"{field_name} must be less than or equal to {MAX_SEARCH_PRICE}")
    return normalized_value


class SearchRequest(BaseModel):
    suburb: Optional[str] = None
    state: Optional[str] = None
    postcode: Optional[str | list[str]] = None
    listingType: Literal["buy", "rent"]
    minPrice: Optional[float] = Field(default=None, ge=0)
    maxPrice: Optional[float] = Field(default=None, ge=0)
    minBedrooms: Optional[int] = Field(default=None, ge=0)
    maxBedrooms: Optional[int] = Field(default=None, ge=0)
    minBathrooms: Optional[int] = Field(default=None, ge=0)
    minCarSpaces: Optional[int] = Field(default=None, ge=0)
    propertyType: Optional[str | list[str]] = None
    maxPages: int = Field(default=1, ge=1)

    @field_validator("suburb", "state", mode="before")
    @classmethod
    def validate_text_fields(cls, value):
        return _clean_optional_text(value)

    @field_validator("postcode", mode="before")
    @classmethod
    def validate_postcode(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = _clean_optional_text(value)
            if not re.fullmatch(r"\d{4}", cleaned):
                raise ValueError("must be exactly 4 digits")
            return cleaned
        if isinstance(value, list):
            cleaned = []
            for item in value:
                cleaned_item = _clean_optional_text(item)
                if cleaned_item is None:
                    raise ValueError("must contain postcode strings only")
                if not re.fullmatch(r"\d{4}", cleaned_item):
                    raise ValueError("must be exactly 4 digits")
                cleaned.append(cleaned_item)
            if not cleaned:
                raise ValueError("must contain at least one postcode")
            if len(cleaned) > 1:
                raise ValueError(
                    "multiple postcodes are not supported by /search URL builder; provide a single postcode"
                )
            return cleaned
        raise ValueError("must be a string or an array of strings")

    @field_validator("propertyType", mode="before")
    @classmethod
    def validate_property_type(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            return _clean_optional_text(value)
        if isinstance(value, list):
            cleaned = [_clean_optional_text(item) for item in value]
            if not cleaned:
                raise ValueError("must contain at least one property type")
            if any(item is None for item in cleaned):
                raise ValueError("must contain property type strings only")
            return cleaned
        raise ValueError("must be a string or an array of strings")

    @model_validator(mode="after")
    def validate_ranges(self):
        min_price = _normalize_price_bound("minPrice", self.minPrice)
        max_price = _normalize_price_bound("maxPrice", self.maxPrice)
        if self.minPrice is not None and self.maxPrice is not None and self.minPrice > self.maxPrice:
            raise ValueError("minPrice cannot be greater than maxPrice")
        if self.minBedrooms is not None and self.maxBedrooms is not None and self.minBedrooms > self.maxBedrooms:
            raise ValueError("minBedrooms cannot be greater than maxBedrooms")
        if min_price is not None and max_price is None:
            raise ValueError("maxPrice is required when minPrice is provided")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("minPrice cannot be greater than maxPrice")
        if self.postcode and self.suburb and not self.state:
            raise ValueError("state is required when suburb and postcode are both provided")
        if self.maxPages > SEARCH_MAX_PAGES:
            raise ValueError(f"maxPages must be between 1 and {SEARCH_MAX_PAGES}")
        return self


def _build_location_segment(suburb: Optional[str], state: Optional[str], postcode: Optional[str]) -> Optional[str]:
    suburb_part = quote_plus(suburb.lower()) if suburb else None
    state_part = quote_plus(state.lower()) if state else None

    if suburb_part and state_part and postcode:
        return f"{suburb_part},+{state_part}+{postcode}"
    if suburb_part and state_part:
        return f"{suburb_part},+{state_part}"
    if suburb_part:
        return suburb_part
    if state_part and postcode:
        return f"{state_part}+{postcode}"
    if state_part:
        return state_part
    if postcode:
        return postcode
    return None


def _normalize_property_types(property_type: Optional[str | list[str]]) -> Optional[list[str]]:
    if property_type is None:
        return None
    requested_types = [property_type] if isinstance(property_type, str) else property_type
    slugs = []
    for requested_type in requested_types:
        key = requested_type.strip().lower()
        slug = SUPPORTED_PROPERTY_TYPES.get(key)
        if not slug:
            supported = ", ".join(sorted(SUPPORTED_PROPERTY_TYPES.keys()))
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported propertyType '{requested_type}'. Supported values: {supported}",
            )
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def build_search_url(request: SearchRequest) -> str:
    property_type_slugs = _normalize_property_types(request.propertyType)
    postcode = request.postcode[0] if isinstance(request.postcode, list) else request.postcode
    location = _build_location_segment(request.suburb, request.state, postcode)

    path_parts = []
    if property_type_slugs:
        path_parts.append("property-" + "-".join(property_type_slugs))
    if request.minBedrooms is not None and request.minBedrooms > 0:
        label = "bedroom" if request.minBedrooms == 1 else "bedrooms"
        path_parts.append(f"with-{request.minBedrooms}-{label}")
    if request.maxPrice is not None:
        min_price = _normalize_price_bound("minPrice", request.minPrice) or 0
        max_price = _normalize_price_bound("maxPrice", request.maxPrice)
        if max_price is None:
            raise HTTPException(status_code=422, detail="maxPrice is required when minPrice is provided")
        path_parts.append(f"between-{min_price}-{max_price}")
    if location:
        path_parts.append(f"in-{location}")

    query_parameters = {"source": "refinement"}
    if request.maxBedrooms is not None and request.maxBedrooms > 0:
        query_parameters["maxBeds"] = str(request.maxBedrooms)
    if request.minBathrooms is not None and request.minBathrooms > 0:
        query_parameters["numBaths"] = str(request.minBathrooms)
    if request.minCarSpaces is not None and request.minCarSpaces > 0:
        query_parameters["numParkingSpaces"] = str(request.minCarSpaces)

    if path_parts:
        path = f"/{request.listingType}/{'-'.join(path_parts)}/list-1"
    else:
        path = f"/{request.listingType}/list-1"

    url = urlunparse(
        (
            "https",
            f"www.{REAL_ESTATE_DOMAIN}",
            path,
            "",
            urlencode(query_parameters),
            "",
        )
    )
    validate_target_url(url)
    if urlparse(url).scheme != "https":
        raise HTTPException(status_code=400, detail="Generated search URL must be HTTPS")
    return url


@app.get("/health")
def health():
    return {"status": "ok", "database": DATABASE_PATH}


@app.post("/scrape", dependencies=[Depends(require_api_key)])
def scrape(request: ScrapeRequest, background_tasks: BackgroundTasks):
    if request.max_pages < 1 or request.max_pages > 100:
        raise HTTPException(status_code=400, detail="max_pages must be between 1 and 100")
    validate_target_url(request.url)
    background_tasks.add_task(scrape_url, request.url, request.max_pages)
    return {"status": "queued", "url": request.url, "max_pages": request.max_pages}


@app.post("/search", dependencies=[Depends(require_api_key)])
def search(request: SearchRequest, background_tasks: BackgroundTasks):
    generated_url = build_search_url(request)
    min_price = _normalize_price_bound("minPrice", request.minPrice)
    max_price = _normalize_price_bound("maxPrice", request.maxPrice)
    background_tasks.add_task(scrape_url, generated_url, request.maxPages)
    return {
        "status": "queued",
        "url": generated_url,
        "max_pages": request.maxPages,
        "listing_type": request.listingType,
        "filters": {
            "suburb": request.suburb,
            "state": request.state,
            "postcode": request.postcode,
            "minPrice": min_price,
            "maxPrice": max_price,
            "minBedrooms": request.minBedrooms,
            "maxBedrooms": request.maxBedrooms,
            "minBathrooms": request.minBathrooms,
            "minCarSpaces": request.minCarSpaces,
            "propertyType": request.propertyType,
        },
    }


@app.get("/listings", dependencies=[Depends(require_api_key)])
def listings(
    address: Optional[str] = Query(default=None),
    min_price: Optional[int] = Query(default=None),
    max_price: Optional[int] = Query(default=None),
    min_bedrooms: Optional[int] = Query(default=None),
    min_bathrooms: Optional[int] = Query(default=None),
    min_cars: Optional[int] = Query(default=None),
    min_area_m2: Optional[float] = Query(default=None),
    property_type: Optional[str] = Query(default=None),
    listing_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    conditions = []
    parameters = []

    if address:
        conditions.append("address LIKE ?")
        parameters.append(f"%{address}%")
    if min_price is not None:
        conditions.append("(price_max IS NULL OR price_max >= ?)")
        parameters.append(min_price)
    if max_price is not None:
        conditions.append("(price_min IS NULL OR price_min <= ?)")
        parameters.append(max_price)
    if min_bedrooms is not None:
        conditions.append("bedrooms >= ?")
        parameters.append(min_bedrooms)
    if min_bathrooms is not None:
        conditions.append("bathrooms >= ?")
        parameters.append(min_bathrooms)
    if min_cars is not None:
        conditions.append("cars >= ?")
        parameters.append(min_cars)
    if min_area_m2 is not None:
        conditions.append("land_area_m2 >= ?")
        parameters.append(min_area_m2)
    if property_type:
        conditions.append("property_type = ?")
        parameters.append(property_type)
    if listing_type:
        conditions.append("listing_type = ?")
        parameters.append(listing_type)

    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
    connection = db_connection()
    rows = connection.execute(
        f"""
        SELECT listing_id, url, address, price_text, price_min, price_max,
               bedrooms, bathrooms, cars, land_area_m2, building_area_m2,
               property_type, listing_type, first_seen, last_seen
        FROM listings
        {where_sql}
        ORDER BY last_seen DESC
        LIMIT ? OFFSET ?
        """,
        parameters + [limit, offset],
    ).fetchall()
    connection.close()

    return {
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in rows],
    }


@app.get("/listings/{listing_id}", dependencies=[Depends(require_api_key)])
def get_listing(listing_id: str):
    connection = db_connection()
    row = connection.execute(
        """
        SELECT listing_id, url, address, price_text, price_min, price_max,
               bedrooms, bathrooms, cars, land_area_m2, building_area_m2,
               property_type, listing_type, first_seen, last_seen, raw_card_html
        FROM listings
        WHERE listing_id = ?
        """,
        (listing_id,),
    ).fetchone()
    connection.close()

    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    return dict(row)
