import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("EXTRACT_API_TOKEN", "test-secret")

import main
import worten_adapter as wa

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://canarias.worten.es/search?query=radiador+de+aceite"
PRODUCT_URL = "https://canarias.worten.es/productos/radiador-aceite-orbegozors2500-8235777"


@pytest.fixture
def client():
    return main.create_app("test-secret").test_client()


@pytest.fixture(autouse=True)
def workspace_tempdir(monkeypatch):
    temp_root = Path(__file__).parent / ".worten-temp"
    temp_root.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    yield
    try:
        temp_root.rmdir()
    except OSError:
        pass


def search_payload():
    return (FIXTURES / "worten_search.json").read_bytes()


def product_html():
    return (FIXTURES / "worten_product.html").read_bytes()


def install_curl(monkeypatch, body, content_type, status=200, returncode=0):
    calls = []
    monkeypatch.setattr(wa, "_curl_executable", lambda deadline: r"C:\tools\curl.exe")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        output_path = args[args.index("--output") + 1]
        Path(output_path).write_bytes(body)
        metrics = {"http_code": status, "content_type": content_type, "size_download": len(body)}
        kwargs["stdout"].write(json.dumps(metrics).encode())
        kwargs["stderr"].write(b"untrusted upstream detail")
        return SimpleNamespace(returncode=returncode, stdout=None, stderr=None)

    monkeypatch.setattr(wa.subprocess, "run", run)
    return calls


def resolver(address="93.184.216.34"):
    return lambda hostname: {address}


def test_search_joins_explicit_ids_and_returns_existing_contract(monkeypatch):
    calls = install_curl(monkeypatch, search_payload(), "application/json")
    result = wa.extract_worten_search(SEARCH_URL, resolver())

    assert result == {
        "mode": "search",
        "source_url": SEARCH_URL,
        "extraction_status": "ok",
        "product_count": 2,
        "products": [
            {"name": "Radiador Aceite ORBEGOZO RS2500", "sku": "8235777", "price": 89.99,
             "currency": None, "variant": None,
             "url": "https://canarias.worten.es/productos/radiador-aceite-orbegozors2500-8235777", "image_url": None},
            {"name": "Lavadora BECKEN Boostwash BWM8812N", "sku": "8240636", "price": 329.99,
             "currency": None, "variant": None,
             "url": "https://canarias.worten.es/productos/lavadora-becken-bwm8812n-8240636", "image_url": None},
        ],
        "truncated": False,
    }
    args = calls[0][0]
    payload = json.loads(calls[0][1]["input"])
    assert payload == {
        "query": "radiador de aceite",
        "params": {"pageNumber": 0, "pageSize": 48, "filters": [], "collapse": True,
                   "favoriteStores": [], "debug": False},
        "hasVariants": True, "debug": False,
    }
    assert args[args.index("--data-binary") + 1] == "@-"
    assert "--retry" in args and args[args.index("--retry") + 1] == "0"
    assert "--max-redirs" in args and args[args.index("--max-redirs") + 1] == "0"
    assert "--max-filesize" in args and args[args.index("--max-filesize") + 1] == str(wa.MAX_BODY_BYTES)
    assert "--resolve" in args and args[args.index("--resolve") + 1] == f"{wa.WORTEN_HOST}:443:93.184.216.34"
    assert args[1] == "--disable"


def test_search_caps_at_twenty_and_marks_partial(monkeypatch):
    data = json.loads(search_payload())
    products, canonicals, offers = [], [], []
    hits = []
    for index in range(23):
        sku = f"82{index:05d}"
        webitem = f"webitem-{sku}"
        offer_id = f"offer-{sku}"
        hits.append({"product": {"id": webitem}})
        products.append({"meta": {"refs": {"webitem_id": webitem, "sku": sku}},
                         "woffer": {"offer_id": offer_id},
                         "properties": {"text": {"name": f"Product {sku}"}}})
        canonicals.append({"id": webitem, "url": f"/productos/product-{sku}"})
        offers.append({"offer_id": offer_id, "source_offer_code": sku, "price": {"final": "12.50"}})
    data["searchResponse"] = {"totalHits": 23, "hits": hits}
    data["detailsResponse"] = {"productsData": {"products": products},
                               "productsCanonicalsData": {"web_items": canonicals},
                               "offersData": {"offers": offers}}
    install_curl(monkeypatch, json.dumps(data).encode(), "application/json")

    result = wa.extract_worten_search(SEARCH_URL, resolver())

    assert result["product_count"] == 20
    assert len(result["products"]) == 20
    assert result["products"][-1]["sku"] == "8200019"
    assert result["truncated"] is True
    assert result["extraction_status"] == "partial"


def test_search_reports_omitted_later_pages_as_truncated(monkeypatch):
    data = json.loads(search_payload())
    data["searchResponse"]["totalHits"] = 50
    install_curl(monkeypatch, json.dumps(data).encode(), "application/json")
    result = wa.extract_worten_search(SEARCH_URL, resolver())
    assert result["truncated"] is True
    assert result["extraction_status"] == "partial"


def test_search_explicit_zero_hits_is_no_products(monkeypatch):
    install_curl(monkeypatch, json.dumps({"searchResponse": {"totalHits": 0, "hits": []}, "detailsResponse": None}).encode(), "application/json")
    assert wa.extract_worten_search(SEARCH_URL, resolver()) == {
        "mode": "search", "source_url": SEARCH_URL, "extraction_status": "no_products",
        "product_count": 0, "products": [], "truncated": False,
    }


@pytest.mark.parametrize("mutate", [
    lambda data: data["searchResponse"]["hits"][0]["product"].update(id="unknown-webitem"),
    lambda data: data["detailsResponse"]["productsCanonicalsData"]["web_items"][0].update(url="/productos-wrong-0000000"),
    lambda data: data["detailsResponse"]["offersData"]["offers"][0].update(source_offer_code="9999999"),
])
def test_search_rejects_unrelated_or_conflicting_associations(monkeypatch, mutate):
    data = json.loads(search_payload())
    mutate(data)
    install_curl(monkeypatch, json.dumps(data).encode(), "application/json")
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(SEARCH_URL, resolver())
    assert exc.value.code == "upstream_invalid_response"


def test_search_rejects_duplicate_offer_ids(monkeypatch):
    data = json.loads(search_payload())
    data["detailsResponse"]["offersData"]["offers"].append(data["detailsResponse"]["offersData"]["offers"][0])
    install_curl(monkeypatch, json.dumps(data).encode(), "application/json")
    with pytest.raises(wa.WortenAdapterError, match="conflicting identifiers"):
        wa.extract_worten_search(SEARCH_URL, resolver())


@pytest.mark.parametrize("url", [
    "https://canarias.worten.es/search?query=chair&page=2",
    "https://canarias.worten.es/search?query=chair&sort=price",
    "https://canarias.worten.es/search?query=chair&filters=color",
    "https://canarias.worten.es/search?query=chair&query=desk",
    "http://canarias.worten.es/search?query=chair",
    "https://canarias.worten.es:444/search?query=chair",
    "https://user@canarias.worten.es/search?query=chair",
    "https://canarias.worten.es/products?query=chair",
])
def test_search_rejects_unsupported_url_and_parameters(url):
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(url, resolver())
    assert exc.value.status == 400


@pytest.mark.parametrize("url", [
    "http://canarias.worten.es/productos/a-123",
    "https://canarias.worten.es:444/productos/a-123",
    "https://other.example/productos/a-123",
    "https://canarias.worten.es/productos/a-123?foo=bar",
    "https://canarias.worten.es/productos/a-123/",
])
def test_product_rejects_unsupported_url(url):
    with pytest.raises(wa.WortenAdapterError):
        wa.extract_worten_product(url, resolver())


def test_standalone_product_extracts_verified_identity_price_and_evidence(monkeypatch):
    install_curl(monkeypatch, product_html(), "text/html; charset=utf-8")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["mode"] == "product"
    assert result["source_url"] == PRODUCT_URL
    assert result["extraction_status"] == "ok"
    product = result["product"]
    assert product["name"] == "Radiador Aceite ORBEGOZO RS2500"
    assert product["sku"] == "8235777"
    assert product["price"] == 89.99
    assert product["original_price"] == 99.99
    assert product["currency"] == "EUR"
    assert product["url"] == PRODUCT_URL
    assert product["brand"] == "ORBEGOZO"
    assert product["description"] == "Radiador de aceite de 2500 W"
    assert product["image_url"] == "https://canarias.worten.es/images/rs2500.jpg"
    assert {item["label"] for item in product["specifications"]} >= {"Potencia", "Color"}
    assert product["availability_evidence"][0]["text"] == "Disponible en tienda sujeto a código postal."


def test_product_identity_survives_generic_parser_seven_digit_heuristic(monkeypatch):
    html = product_html().replace(
        b',"url":"https://canarias.worten.es/productos/radiador-aceite-orbegozors2500-8235777"',
        b"",
    )
    assert "url" not in html.decode("utf-8").split('"offers"', 1)[0]
    generic = wa._extract_product_html(PRODUCT_URL, html.decode("utf-8"), content_status="ok")
    assert generic["extraction_status"] == "no_product"

    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "ok"
    assert result["product"]["name"] == "Radiador Aceite ORBEGOZO RS2500"
    assert result["product"]["sku"] == "8235777"
    assert result["product"]["price"] == 89.99
    assert result["product"]["currency"] == "EUR"
    assert result["product"]["url"] == PRODUCT_URL
    assert len(result["product"]["specifications"]) == 2
    assert result["product"]["availability_evidence"] == [
        {"type": "visible_stock_text", "text": "Disponible en tienda sujeto a código postal.",
         "source": "product-page text"}
    ]


def test_product_without_visible_or_structured_name_is_partial(monkeypatch):
    html = product_html().replace(
        b'"name":"Radiador Aceite ORBEGOZO RS2500"', b'"name":null'
    ).replace(
        b"<h1>Radiador Aceite ORBEGOZO RS2500</h1>", b""
    )
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "partial"
    assert result["product"]["name"] is None
    assert result["product"]["sku"] == "8235777"
    assert result["product"]["price"] == 89.99
    assert result["product"]["currency"] == "EUR"
    assert "name_unverified" in result["warnings"]


def test_product_visible_price_supports_split_span_and_spanish_decimal(monkeypatch):
    html = product_html().replace(
        b"89,99 \xe2\x82\xac",
        b"<span>89</span><span>,99</span><span> \xe2\x82\xac</span>",
    )
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "ok"
    assert result["product"]["price"] == 89.99
    assert result["product"]["currency"] == "EUR"


def test_product_conflicting_visible_price_is_partial_and_warning(monkeypatch):
    html = product_html().replace(b"89,99 \xe2\x82\xac", b"79,99 \xe2\x82\xac")
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "partial"
    assert result["product"]["price"] == 89.99
    assert "price_conflict" in result["warnings"]


def test_recommendation_price_cannot_verify_main_product_price(monkeypatch):
    html = product_html().replace(b"89,99 \xe2\x82\xac", b"")
    html = html.replace(
        b"</body>",
        b'<section class="related-products"><span class="price">89,99 \xe2\x82\xac</span></section></body>',
    )
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "partial"
    assert "visible_price_unverified" in result["warnings"]


def test_product_without_price_currency_is_partial_not_fabricated(monkeypatch):
    html = product_html().replace(b'"priceCurrency":"EUR",', b'')
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "partial"
    assert result["product"]["price"] is None
    assert result["product"]["currency"] is None


def test_product_conflicting_jsonld_offers_are_partial(monkeypatch):
    html = product_html().replace(
        b'"offers":{"@type":"Offer","price":"89.99","priceCurrency":"EUR","availability":"https://schema.org/InStock"}',
        b'"offers":[{"@type":"Offer","price":"89.99","priceCurrency":"EUR"},{"@type":"Offer","price":"79.99","priceCurrency":"EUR"}]',
    )
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "partial"
    assert result["product"]["price"] is None
    assert result["product"]["currency"] is None
    assert "price_ambiguous" in result["warnings"]


def test_product_rejects_wrong_sku_and_conflicting_canonical(monkeypatch):
    html = product_html().replace(b'"sku":"8235777"', b'"sku":"0000000"')
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "no_product"
    assert result["product"] is None

    html = product_html().replace(b"radiador-aceite-orbegozors2500-8235777\">", b"other-product-8235777\">")
    install_curl(monkeypatch, html, "text/html")
    with pytest.raises(wa.WortenAdapterError, match="canonical URL"):
        wa.extract_worten_product(PRODUCT_URL, resolver())


def test_product_multiple_matching_product_records_fail_closed(monkeypatch):
    html = product_html().replace(b"</head>", b'<script type="application/ld+json">{"@type":"Product","sku":"8235777","name":"Other"}</script></head>')
    install_curl(monkeypatch, html, "text/html")
    with pytest.raises(wa.WortenAdapterError, match="ambiguous product identity"):
        wa.extract_worten_product(PRODUCT_URL, resolver())


def test_product_conflicting_structured_sku_or_offer_identity_fails_closed(monkeypatch):
    html = product_html().replace(b'"sku":"8235777"', b'"sku":"8235777","productID":"0000000"')
    install_curl(monkeypatch, html, "text/html")
    with pytest.raises(wa.WortenAdapterError, match="identifiers conflicted"):
        wa.extract_worten_product(PRODUCT_URL, resolver())

    html = product_html().replace(b'"priceCurrency":"EUR"', b'"priceCurrency":"EUR","sku":"0000000"')
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "partial"
    assert result["product"]["price"] is None


def test_private_dns_fails_before_curl(monkeypatch):
    monkeypatch.setattr(wa, "_curl_executable", lambda deadline: pytest.fail("curl must not run"))
    def private(host):
        raise main.ExtractionError("unsafe_url", "The requested URL is not allowed.", 403)
    with pytest.raises(main.ExtractionError) as exc:
        wa.extract_worten_search(SEARCH_URL, private)
    assert exc.value.code == "unsafe_url"


def test_dns_resolution_cannot_hold_the_request_past_deadline():
    def slow_lookup(host):
        import time
        time.sleep(0.15)
        return {"93.184.216.34"}
    started = wa.time.monotonic()
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa._resolve_before_deadline(slow_lookup, started + 0.02)
    assert exc.value.code == "upstream_timeout"
    assert wa.time.monotonic() - started < 0.12


def test_each_request_pins_the_validated_dns_answer(monkeypatch):
    calls = install_curl(monkeypatch, search_payload(), "application/json")
    answers = iter([{"93.184.216.34"}, {"93.184.216.35"}])
    seen = []
    def changing_dns(host):
        seen.append(host)
        return next(answers)
    wa.extract_worten_search(SEARCH_URL, changing_dns)
    wa.extract_worten_search(SEARCH_URL, changing_dns)
    assert seen == [wa.WORTEN_HOST, wa.WORTEN_HOST]
    assert calls[0][0][calls[0][0].index("--resolve") + 1].endswith(":93.184.216.34")
    assert calls[1][0][calls[1][0].index("--resolve") + 1].endswith(":93.184.216.35")


def test_unknown_length_oversized_response_is_rejected(monkeypatch):
    body = b"x" * (wa.MAX_BODY_BYTES + 1)
    install_curl(monkeypatch, body, "application/json")
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(SEARCH_URL, resolver())
    assert exc.value.code == "response_too_large"
    assert exc.value.status == 413


@pytest.mark.parametrize("status,code", [(403, "upstream_access_denied"), (500, "upstream_fetch_failed")])
def test_http_errors_are_safe_and_do_not_leak_upstream_text(monkeypatch, status, code):
    install_curl(monkeypatch, b"denied", "text/plain", status=status)
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(SEARCH_URL, resolver())
    assert exc.value.code == code
    assert "untrusted upstream detail" not in str(exc.value)


def test_malformed_json_and_unexpected_content_type_fail_safely(monkeypatch):
    install_curl(monkeypatch, b"{not json", "application/json")
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(SEARCH_URL, resolver())
    assert exc.value.code == "upstream_invalid_response"

    install_curl(monkeypatch, b"<html>not json</html>", "text/html")
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(SEARCH_URL, resolver())
    assert exc.value.code == "upstream_invalid_response"


def test_200_challenge_maps_to_unusable_without_retry(monkeypatch):
    calls = install_curl(monkeypatch, b"<html><title>Just a moment...</title></html>", "text/html")
    result = wa.extract_worten_search(SEARCH_URL, resolver())
    assert result["extraction_status"] == "unusable"
    assert len(calls) == 1

    calls = install_curl(monkeypatch, b"<html><title>Just a moment...</title></html>", "text/html", status=403)
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(SEARCH_URL, resolver())
    assert exc.value.code == "upstream_access_denied"
    assert len(calls) == 1


def test_empty_successful_responses_map_to_unusable(monkeypatch):
    install_curl(monkeypatch, b"  \n", "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "unusable"
    assert result["product"] is None


def test_malformed_or_missing_product_jsonld_is_no_product(monkeypatch):
    html = b"<html><h1>Product</h1><script type='application/ld+json'>{not json</script></html>"
    install_curl(monkeypatch, html, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "no_product"
    assert result["product"] is None

    malformed = b"<html><h1>Product</h1><script type='application/ld+json'>{\"@type\":\"Product\",\"sku\":\"8235777\"}"
    install_curl(monkeypatch, malformed, "text/html")
    result = wa.extract_worten_product(PRODUCT_URL, resolver())
    assert result["extraction_status"] == "no_product"
    assert result["product"] is None


def test_curl_timeout_is_mapped_safely(monkeypatch):
    monkeypatch.setattr(wa, "_curl_executable", lambda deadline: r"C:\tools\curl.exe")
    calls = []
    def timeout(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("curl", 1)
    monkeypatch.setattr(wa.subprocess, "run", timeout)
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa.extract_worten_search(SEARCH_URL, resolver())
    assert (exc.value.code, exc.value.status) == ("upstream_timeout", 504)
    output_path = Path(calls[0][calls[0].index("--output") + 1])
    assert not output_path.exists()
    assert not output_path.parent.exists()


def test_temporary_artifacts_are_removed_after_failure(monkeypatch):
    calls = install_curl(monkeypatch, b"forbidden", "text/plain", status=403)
    with pytest.raises(wa.WortenAdapterError):
        wa.extract_worten_search(SEARCH_URL, resolver())
    output_path = Path(calls[0][0][calls[0][0].index("--output") + 1])
    assert not output_path.exists()
    assert not output_path.parent.exists()


def test_temporary_artifacts_are_removed_after_success(monkeypatch):
    calls = install_curl(monkeypatch, search_payload(), "application/json")
    wa.extract_worten_search(SEARCH_URL, resolver())
    output_path = Path(calls[0][0][calls[0][0].index("--output") + 1])
    assert not output_path.exists()
    assert not output_path.parent.exists()


def test_worten_routing_keeps_authentication_and_existing_mode_contract(client, monkeypatch):
    monkeypatch.setattr(main, "_resolve_public_addresses", lambda host: {"93.184.216.34"})
    monkeypatch.setattr(main, "extract_worten_search", lambda url, resolver: {
        "mode": "search", "source_url": url, "extraction_status": "no_products",
        "product_count": 0, "products": [], "truncated": False,
    })
    denied = client.get("/extract?url=https%3A%2F%2Fcanarias.worten.es%2Fsearch%3Fquery%3Dchair&mode=search")
    assert denied.status_code == 401
    response = client.get(
        "/extract?url=https%3A%2F%2Fcanarias.worten.es%2Fsearch%3Fquery%3Dchair&mode=search",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200
    assert response.json["source_url"] == "https://canarias.worten.es/search?query=chair"


def test_worten_product_route_requires_bearer_and_dispatches(client, monkeypatch):
    monkeypatch.setattr(main, "_resolve_public_addresses", lambda host: {"93.184.216.34"})
    monkeypatch.setattr(main, "extract_worten_product", lambda url, resolver: {
        "mode": "product", "source_url": url, "extraction_status": "no_product",
        "product": None, "warnings": [],
    })
    encoded = "https%3A%2F%2Fcanarias.worten.es%2Fproductos%2Fitem-8235777"
    assert client.get(f"/extract?url={encoded}&mode=product").status_code == 401
    response = client.get(
        f"/extract?url={encoded}&mode=product",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200
    assert response.json["mode"] == "product"


def test_real_subprocess_with_local_fake_curl_handles_search_and_product(monkeypatch):
    expected_request = {
        "query": "radiador de aceite",
        "params": {"pageNumber": 0, "pageSize": 48, "filters": [], "collapse": True,
                   "favoriteStores": [], "debug": False},
        "hasVariants": True,
        "debug": False,
    }
    script_path = Path(__file__).parent / ".worten-temp" / "fake_curl.py"
    script = '''
import json
import pathlib
import sys

args = sys.argv[1:]
assert args[0] == "--disable"
url = args[args.index("--url") + 1]
output = pathlib.Path(args[args.index("--output") + 1])
if url.endswith("/worten-api/search-products"):
    assert args[args.index("--data-binary") + 1] == "@-"
    received = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    assert received == EXPECTED_REQUEST
    body = bytes.fromhex(SEARCH_RESPONSE_HEX)
    content_type = "application/json"
else:
    assert url == PRODUCT_URL
    assert "--data-binary" not in args
    body = bytes.fromhex(PRODUCT_HTML_HEX)
    content_type = "text/html; charset=utf-8"
output.write_bytes(body)
telemetry = {"http_code": 200, "content_type": content_type, "size_download": len(body)}
sys.stdout.buffer.write(json.dumps(telemetry).encode("utf-8"))
'''.replace("EXPECTED_REQUEST", repr(expected_request))
    script = script.replace("SEARCH_RESPONSE_HEX", repr(search_payload().hex()))
    script = script.replace("PRODUCT_HTML_HEX", repr(product_html().hex()))
    script = script.replace("PRODUCT_URL", repr(PRODUCT_URL))
    script_path.write_text(script, encoding="utf-8")
    monkeypatch.setattr(wa, "_curl_executable", lambda deadline: sys.executable)
    monkeypatch.setattr(wa, "_curl_command_prefix", lambda executable: [executable, str(script_path)])
    try:
        search_result = wa.extract_worten_search(SEARCH_URL, resolver())
        product_result = wa.extract_worten_product(PRODUCT_URL, resolver())
    finally:
        script_path.unlink(missing_ok=True)

    assert search_result["product_count"] == 2
    assert search_result["extraction_status"] == "ok"
    assert product_result["extraction_status"] == "ok"
    assert product_result["product"]["sku"] == "8235777"
    assert product_result["product"]["price"] == 89.99


def test_curl_minimum_version_is_enforced(monkeypatch):
    monkeypatch.setattr(wa.shutil, "which", lambda name: r"C:\tools\curl.exe")
    monkeypatch.setattr(wa.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="curl 8.3.0 libcurl/8.3.0", stderr=""))
    with pytest.raises(wa.WortenAdapterError) as exc:
        wa._curl_executable(wa.time.monotonic() + 5)
    assert exc.value.code == "runtime_unavailable"
