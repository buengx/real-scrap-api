from fastapi.testclient import TestClient
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(app_module, "API_KEY", "test-api-key")
    app_module.init_db()
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def scrape_calls(monkeypatch):
    calls = []

    def fake_scrape_url(url, max_pages=1):
        calls.append((url, max_pages))
        return 0

    monkeypatch.setattr(app_module, "scrape_url", fake_scrape_url)
    return calls


def auth_headers():
    return {"X-API-Key": "test-api-key"}


def test_build_search_url_minimal():
    request = app_module.SearchRequest(listingType="buy")
    url = app_module.build_search_url(request)
    assert url == "https://www.realestate.com.au/buy/list-1?source=refinement"


def test_build_search_url_full_request():
    request = app_module.SearchRequest(
        suburb="South Yarra",
        state="VIC",
        postcode="3141",
        listingType="buy",
        minPrice=500000,
        maxPrice=900000,
        minBedrooms=2,
        maxBedrooms=4,
        minBathrooms=2,
        minCarSpaces=1,
        propertyType=["house", "apartment"],
        maxPages=3,
    )
    url = app_module.build_search_url(request)
    assert (
        url
        == "https://www.realestate.com.au/buy/"
        "property-house-apartment-unit+apartment-with-2-bedrooms-between-500000-900000-in-south+yarra,+vic+3141/"
        "list-1?source=refinement&maxBeds=4&numBaths=2&numParkingSpaces=1"
    )


def test_build_search_url_max_price_without_min_price():
    request = app_module.SearchRequest(listingType="rent", maxPrice=800)
    url = app_module.build_search_url(request)
    assert "between-0-800" in url


def test_search_rejects_contradictory_ranges(client):
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "minPrice": 900000, "maxPrice": 500000},
    )
    assert response.status_code == 422
    assert "minPrice cannot be greater than maxPrice" in response.text


def test_search_rejects_min_price_without_max_price(client):
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "minPrice": 500000},
    )
    assert response.status_code == 422
    assert "maxPrice is required when minPrice is provided" in response.text


def test_search_rejects_invalid_values_and_types(client):
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "minBathrooms": -1},
    )
    assert response.status_code == 422

    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "suburb": "   "},
    )
    assert response.status_code == 422

    # Non-string for string fields (suburb, state)
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "suburb": 123},
    )
    assert response.status_code == 422

    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "state": ["VIC"]},
    )
    assert response.status_code == 422

    # Invalid list items for postcode or propertyType
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "postcode": [None]},
    )
    assert response.status_code == 422

    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "propertyType": [123]},
    )
    assert response.status_code == 422

    # Fractional or out-of-range prices
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "maxPrice": 500000.50},
    )
    assert response.status_code == 422

    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "maxPrice": 1_000_000_000},
    )
    assert response.status_code == 422


def test_search_rejects_multiple_postcodes(client):
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "postcode": ["3000", "3001"]},
    )
    assert response.status_code == 422
    assert "multiple postcodes are not supported" in response.text


def test_search_rejects_unsupported_listing_type(client):
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "sold"},
    )
    assert response.status_code == 422


def test_search_rejects_unsupported_property_type(client):
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={"listingType": "buy", "propertyType": "warehouse"},
    )
    assert response.status_code == 422
    assert "Unsupported propertyType 'warehouse'" in response.text


def test_search_requires_api_key(client):
    response = client.post("/search", json={"listingType": "buy"})
    assert response.status_code == 401


def test_search_queues_scrape(client, scrape_calls):
    response = client.post(
        "/search",
        headers=auth_headers(),
        json={
            "suburb": "Mernda",
            "state": "VIC",
            "postcode": "3754",
            "listingType": "buy",
            "propertyType": "house",
            "maxPages": 2,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["max_pages"] == 2
    assert payload["url"].startswith("https://www.realestate.com.au/buy/")
    assert scrape_calls == [(payload["url"], 2)]


def test_existing_scrape_endpoint_still_works(client, scrape_calls):
    response = client.post(
        "/scrape",
        headers=auth_headers(),
        json={"url": "https://www.realestate.com.au/buy/in-moe,+vic+3825/list-1?source=refinement", "max_pages": 1},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert scrape_calls == [("https://www.realestate.com.au/buy/in-moe,+vic+3825/list-1?source=refinement", 1)]


def test_scrape_requires_api_key(client):
    response = client.post("/scrape", json={"url": "https://www.realestate.com.au/buy/list-1", "max_pages": 1})
    assert response.status_code == 401


import random


def test_randomized_search_requests(client, scrape_calls):
    suburbs_and_locations = [
        ("South Yarra", "VIC", "3141"),
        ("Richmond", "VIC", "3121"),
        ("Surry Hills", "NSW", "2010"),
        ("St Agnes", "SA", "5097"),
        ("Brisbane City", "QLD", "4000"),
        (None, None, None),
    ]
    listing_types = ["buy", "rent"]
    property_types = ["house", "apartment", "unit", "townhouse", "villa", "land"]

    random.seed(42)
    for _ in range(10):
        suburb, state, postcode = random.choice(suburbs_and_locations)
        listing_type = random.choice(listing_types)

        # Generate non-contradictory price bounds
        min_price = random.choice([None, 100000, 300000, 500000])
        if min_price is not None:
            max_price = min_price + random.choice([100000, 200000, 500000])
        else:
            max_price = random.choice([None, 500000, 1000000])

        # Generate non-contradictory bedroom bounds
        min_beds = random.choice([None, 1, 2, 3])
        if min_beds is not None:
            max_beds = min_beds + random.choice([0, 1, 2])
        else:
            max_beds = random.choice([None, 2, 4])

        min_baths = random.choice([None, 1, 2])
        min_cars = random.choice([None, 1, 2])

        selected_prop_types = random.sample(property_types, k=random.randint(0, 2))
        prop_type_val = selected_prop_types if selected_prop_types else None

        payload = {
            "listingType": listing_type,
        }
        if suburb and state and postcode:
            payload["suburb"] = suburb
            payload["state"] = state
            payload["postcode"] = postcode
        if min_price is not None:
            payload["minPrice"] = min_price
        if max_price is not None:
            payload["maxPrice"] = max_price
        if min_beds is not None:
            payload["minBedrooms"] = min_beds
        if max_beds is not None:
            payload["maxBedrooms"] = max_beds
        if min_baths is not None:
            payload["minBathrooms"] = min_baths
        if min_cars is not None:
            payload["minCarSpaces"] = min_cars
        if prop_type_val:
            payload["propertyType"] = prop_type_val

        response = client.post("/search", headers=auth_headers(), json=payload)
        assert response.status_code == 200, f"Failed for payload: {payload}, error: {response.text}"
        data = response.json()
        assert data["status"] == "queued"
        assert data["url"].startswith(f"https://www.realestate.com.au/{listing_type}/")
