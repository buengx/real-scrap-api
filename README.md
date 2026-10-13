# real-scrap-api

A small FastAPI service that fetches realestate.com.au search result pages, extracts listing-card data, and stores it in SQLite for later searching.

## What it does

- Fetches a realestate.com.au search URL.
- Supports multiple `/list-N` pages.
- Extracts listing data from the rendered HTML cards.
- Stores and upserts listings in `realestate.db`.
- Provides HTTP endpoints for scraping and searching saved listings.
- Keeps the raw listing-card HTML for inspection/debugging.

The parser uses semantic markup from the cards, especially `aria-label` values. It does not depend on the generated CSS class names for bedrooms, bathrooms, cars, or land size.

## Current extracted fields

Each listing can contain:

- `listing_id`
- `url`
- `address`
- `price_text`
- `price_min`
- `price_max`
- `bedrooms`
- `bathrooms`
- `cars`
- `land_area_m2`
- `building_area_m2`
- `property_type`
- `listing_type`
- `first_seen`
- `last_seen`

Example source card:

```html
<ul aria-label="Unit with 1,679.5m² land size with 1 bedroom 1 bathroom 1 car space">
```

The API extracts `1` bedroom, `1` bathroom, `1` car, `1679.5` square metres, and `unit` from this structure.

## Install

Python 3.10 or newer is recommended.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Configure

Set an API key before exposing the service over the internet:

```bash
export API_KEY="replace-this-with-a-long-random-secret"
export DATABASE_PATH="./realestate.db"
```

Or copy `.env.example` and export the values yourself. The application does not automatically load `.env` files.

## Run

```bash
. .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

The service listens on port `8000`.

## Endpoints

### `GET /health`

Health check. This endpoint does not require the API key.

```bash
curl http://localhost:8000/health
```

Response:

```json
{
  "status": "ok",
  "database": "./realestate.db"
}
```

### `POST /scrape`

Queues a scrape using the URL supplied in the JSON body. The request returns immediately; the scrape runs as a FastAPI background task.

```bash
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: replace-this-with-a-long-random-secret" \
  -d '{
    "url": "https://www.realestate.com.au/buy/size-1000/list-1?source=refinement&sourcePage=rea%3Abuy%3Asearch+result+-+list&sourceElement=search-box-search",
    "max_pages": 10
  }'
```

Response:

```json
{
  "status": "queued",
  "url": "https://www.realestate.com.au/buy/size-1000/list-1?source=refinement&sourcePage=rea%3Abuy%3Asearch+result+-+list&sourceElement=search-box-search",
  "max_pages": 10
}
```

`max_pages` must be between `1` and `100`. If the URL contains `/list-1`, the scraper changes it to `/list-2`, `/list-3`, and so on. It stops when a page has no listings or no new listing IDs.

Only `realestate.com.au` URLs are accepted.

### `GET /listings`

Searches the saved SQLite data. All query parameters are optional.

Available filters:

| Parameter | Meaning |
| --- | --- |
| `address` | Case-insensitive substring match in the address |
| `min_price` | Minimum acceptable price |
| `max_price` | Maximum acceptable price |
| `min_bedrooms` | Minimum bedrooms |
| `min_bathrooms` | Minimum bathrooms |
| `min_cars` | Minimum car spaces |
| `min_area_m2` | Minimum land area |
| `property_type` | Exact type, such as `house` or `unit` |
| `listing_type` | Exact type: `buy`, `rent`, `sold`, or `unknown` |
| `limit` | Number of results, default `100`, maximum `1000` |
| `offset` | Pagination offset, default `0` |

Example: listings with at least 1000 m², at least 2 bedrooms, and a price up to $1,000,000:

```bash
curl "http://localhost:8000/listings?min_area_m2=1000&min_bedrooms=2&max_price=1000000&limit=100" \
  -H "X-API-Key: replace-this-with-a-long-random-secret"
```

Example: Brunswick listings:

```bash
curl "http://localhost:8000/listings?address=Brunswick" \
  -H "X-API-Key: replace-this-with-a-long-random-secret"
```

Response:

```json
{
  "count": 1,
  "limit": 100,
  "offset": 0,
  "results": [
    {
      "listing_id": "151643624",
      "url": "https://www.realestate.com.au/property-unit-vic-brunswick-east-151643624",
      "address": "104/15 BRUNSWICK ROAD, Brunswick East",
      "price_text": "$450,000 - $485,000",
      "price_min": 450000,
      "price_max": 485000,
      "bedrooms": 1,
      "bathrooms": 1,
      "cars": 1,
      "land_area_m2": 1679.5,
      "building_area_m2": null,
      "property_type": "unit",
      "listing_type": "buy",
      "first_seen": "2026-10-13T00:00:00+00:00",
      "last_seen": "2026-10-13T00:00:00+00:00"
    }
  ]
}
```

### `GET /listings/{listing_id}`

Returns one saved listing, including the raw source card HTML.

```bash
curl http://localhost:8000/listings/151643624 \
  -H "X-API-Key: replace-this-with-a-long-random-secret"
```

Response:

```json
{
  "listing_id": "151643624",
  "url": "https://www.realestate.com.au/property-unit-vic-brunswick-east-151643624",
  "address": "104/15 BRUNSWICK ROAD, Brunswick East",
  "price_text": "$450,000 - $485,000",
  "price_min": 450000,
  "price_max": 485000,
  "bedrooms": 1,
  "bathrooms": 1,
  "cars": 1,
  "land_area_m2": 1679.5,
  "building_area_m2": null,
  "property_type": "unit",
  "listing_type": "buy",
  "first_seen": "2026-10-13T00:00:00+00:00",
  "last_seen": "2026-10-13T00:00:00+00:00",
  "raw_card_html": "<div class=\"residential-card__content\">...</div>"
}
```

## Response and price rules

- `price_text` preserves the displayed value, such as `$450,000 - $485,000`.
- `price_min` and `price_max` are numeric values suitable for filtering.
- `$450,000 - $485,000` becomes `450000` and `485000`.
- `$1.2m` becomes `1200000`.
- A single price is used for both `price_min` and `price_max`.
- Unknown values are returned as `null`.
- `land_area_m2` is the land size from the card. It is not necessarily building/floor area.
- `building_area_m2` stays `null` unless the card contains a building-size or floor-area label.
- Existing listings are updated by `listing_id`; they are not duplicated.
- `first_seen` is retained, while `last_seen` changes each time the listing is scraped.

## Errors

Typical HTTP responses:

- `400`: invalid URL, invalid `max_pages`, or invalid request input.
- `401`: missing or incorrect `X-API-Key` when `API_KEY` is configured.
- `404`: requested listing ID is not stored.
- `502`/`5xx`: upstream fetch or server failure.

Example unauthorized response:

```json
{
  "detail": "Invalid API key"
}
```

## Database

SQLite is used because it is easy to deploy and the database is a normal file. The default file is:

```text
./realestate.db
```

The database uses WAL mode and indexes common search fields. Back it up by copying the database file while the service is stopped, or use SQLite's backup tooling.

## Current limitations

- The scraper uses normal HTTP requests. realestate.com.au may return a bot challenge instead of listing HTML.
- There is no persistent job queue yet. FastAPI background tasks run inside the API process.
- There is no automatic retry schedule.
- Listing detail pages are not fetched; data currently comes from search result cards.
- Do not expose this publicly without setting `API_KEY` and placing the service behind HTTPS/reverse proxy protection.

If normal HTTP requests begin returning challenge pages, add a Playwright worker later and feed its resulting HTML into the same `parse_page()` function. Do not add OCR, screen-reader automation, or an LLM unless the site stops exposing the card data in HTML.
