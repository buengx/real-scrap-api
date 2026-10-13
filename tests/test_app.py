from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
import runpy
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 20), ("1", 1), ("20", 20), ("101", 101), ("001", 1), (" 3 ", 3)],
)
def test_parse_search_max_pages_accepts_valid_configuration(value, expected):
    assert app_module.parse_search_max_pages(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "0", "-1", "1.5", "abc", "2e1"])
def test_parse_search_max_pages_rejects_invalid_configuration(value):
    with pytest.raises(
        RuntimeError,
        match="Invalid SEARCH_MAX_PAGES configuration: must be an integer >= 1",
    ):
        app_module.parse_search_max_pages(value)


@pytest.mark.parametrize(("value", "expected"), [(None, 20), ("1", 1), ("37", 37)])
def test_import_applies_search_page_limit(monkeypatch, tmp_path, value, expected):
    if value is None:
        monkeypatch.delenv("SEARCH_MAX_PAGES", raising=False)
    else:
        monkeypatch.setenv("SEARCH_MAX_PAGES", value)
    database_path = tmp_path / "unused.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))

    # Execute in a fresh namespace so import-time configuration is exercised
    # without changing the module used by the other tests.
    imported = runpy.run_path(app_module.__file__)

    assert imported["SEARCH_MAX_PAGES"] == expected
    request_class = imported["SearchRequest"]
    assert request_class(listingType="buy", maxPages=expected).maxPages == expected
    with pytest.raises(ValidationError, match=f"maxPages must be between 1 and {expected}"):
        request_class(listingType="buy", maxPages=expected + 1)
    assert not database_path.exists()


@pytest.mark.parametrize("value", ["", "0", "-1", "not-an-integer", "1.5"])
def test_import_rejects_invalid_search_page_limit(monkeypatch, tmp_path, value):
    monkeypatch.setenv("SEARCH_MAX_PAGES", value)
    database_path = tmp_path / "unused.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))

    with pytest.raises(
        RuntimeError,
        match="Invalid SEARCH_MAX_PAGES configuration: must be an integer >= 1",
    ):
        runpy.run_path(app_module.__file__)

    assert not database_path.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("3000", "3000"), (" 0800 ", "0800"), ([" 0800 "], ["0800"])],
)
def test_search_request_normalizes_postcode(value, expected):
    request = app_module.SearchRequest(listingType="buy", postcode=value)
    assert request.postcode == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must contain at least one postcode"),
        ([None], "must contain postcode strings only"),
        (["3000", None], "must contain postcode strings only"),
        ([None, "3000"], "must contain postcode strings only"),
        ([1234], "must be a string"),
        (["3000", 1234], "must be a string"),
        ([["3000"]], "must be a string"),
        ([{}], "must be a string"),
        ([""], "must not be empty"),
        (["   "], "must not be empty"),
        (["123"], "must be exactly 4 digits"),
        (["30000"], "must be exactly 4 digits"),
        (["30a0"], "must be exactly 4 digits"),
        (["3000", "3001"], "multiple postcodes are not supported"),
        (["3000", "3000"], "multiple postcodes are not supported"),
        (1234, "must be a string or an array of strings"),
        ({"postcode": "3000"}, "must be a string or an array of strings"),
    ],
)
def test_search_request_rejects_invalid_postcode(value, message):
    with pytest.raises(ValidationError) as exc_info:
        app_module.SearchRequest(listingType="buy", postcode=value)

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("postcode",)
    assert message in error["msg"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (" house ", "house"), ([" house ", " apartment "], ["house", "apartment"])],
)
def test_search_request_normalizes_property_type(value, expected):
    request = app_module.SearchRequest(listingType="buy", propertyType=value)
    assert request.propertyType == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must contain at least one property type"),
        ([None], "must contain property type strings only"),
        (["house", None], "must contain property type strings only"),
        ([None, "house"], "must contain property type strings only"),
        ([123], "must be a string"),
        (["house", 123], "must be a string"),
        ([["house"]], "must be a string"),
        ([{}], "must be a string"),
        ([""], "must not be empty"),
        (["house", "   "], "must not be empty"),
        (123, "must be a string or an array of strings"),
        ({"type": "house"}, "must be a string or an array of strings"),
    ],
)
def test_search_request_rejects_invalid_property_type(value, message):
    with pytest.raises(ValidationError) as exc_info:
        app_module.SearchRequest(listingType="buy", propertyType=value)

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("propertyType",)
    assert message in error["msg"]


@pytest.mark.parametrize(
    ("prices", "expected_min", "expected_max"),
    [
        ({}, None, None),
        ({"minPrice": None, "maxPrice": None}, None, None),
        ({"maxPrice": 0.0}, None, 0),
        ({"minPrice": 0.0, "maxPrice": 0.0}, 0, 0),
        ({"minPrice": 500000.0, "maxPrice": 900000.0}, 500000, 900000),
        ({"minPrice": 999999999.0, "maxPrice": 999999999.0}, 999999999, 999999999),
        ({"maxPrice": 999999999.0}, None, 999999999),
    ],
)
def test_search_request_normalizes_price_bounds_to_integers(prices, expected_min, expected_max):
    request = app_module.SearchRequest(listingType="buy", **prices)

    assert request.minPrice == expected_min
    assert request.maxPrice == expected_max
    # Equality alone would also accept floats; the PR promises integer bounds.
    if expected_min is not None:
        assert type(request.minPrice) is int
    if expected_max is not None:
        assert type(request.maxPrice) is int


@pytest.mark.parametrize("field", ["minPrice", "maxPrice"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (0.5, "must be a whole number"),
        (999999999.5, "must be a whole number"),
        (1000000000, "must be less than or equal to 999999999"),
        (-1, "greater than or equal to 0"),
    ],
)
def test_search_request_rejects_invalid_price_bound(field, value, message):
    prices = {"maxPrice": 999999999, field: value}
    with pytest.raises(ValidationError, match=message):
        app_module.SearchRequest(listingType="buy", **prices)


@pytest.mark.parametrize("field", ["minPrice", "maxPrice"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_search_request_rejects_non_finite_price_bound(field, value):
    prices = {"maxPrice": 999999999, field: value}
    with pytest.raises(ValidationError):
        app_module.SearchRequest(listingType="buy", **prices)


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"minPrice": 0}, "maxPrice is required when minPrice is provided"),
        ({"minPrice": 0, "maxPrice": None}, "maxPrice is required when minPrice is provided"),
        ({"minPrice": 2.0, "maxPrice": 1.0}, "minPrice cannot be greater than maxPrice"),
        ({"minBedrooms": 2, "maxBedrooms": 1}, "minBedrooms cannot be greater than maxBedrooms"),
        ({"suburb": "Darwin", "postcode": "0800"}, "state is required"),
        ({"suburb": "Darwin", "postcode": ["0800"]}, "state is required"),
    ],
)
def test_search_request_preserves_range_and_location_validation(filters, message):
    with pytest.raises(ValidationError, match=message):
        app_module.SearchRequest(listingType="buy", **filters)
