import json
from pathlib import Path

import pytest

from product_extraction import extract_product_content


FIXTURES = Path(__file__).parent / "fixtures"
BAGGEBO_URL = "https://www.islas.ikea.es/tenerife/es/pd/baggebo-libreria-blanco-art-20436713"
BILLY_URL = "https://www.islas.ikea.es/tenerife/es/pd/billy-oxberg-libreria-puerta-panel-vdr-blanco-spr-19581536"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_baggebo_main_product_data_specs_and_availability():
    result = extract_product_content(BAGGEBO_URL, fixture("baggebo_product.html"))

    assert result["mode"] == "product"
    assert result["source_url"] == BAGGEBO_URL
    assert result["extraction_status"] == "ok"
    product = result["product"]
    assert product["name"] == "BAGGEBO librería, blanco, 50x25x160 cm"
    assert product["sku"] == "20436713"
    assert isinstance(product["sku"], str)
    assert product["price"] == 29
    assert isinstance(product["price"], (int, float))
    assert product["currency"] == "EUR"
    assert product["url"] == BAGGEBO_URL
    assert product["image_url"] == "https://www.islas.ikea.es/assets/baggebo-main.jpg"
    assert product["details_markdown"] is None
    assert "| Ancho: | 50 cm |" in product["technical_details_markdown"]
    assert "| Fondo: | 25 cm |" in product["technical_details_markdown"]
    assert "| Altura: | 160 cm |" in product["technical_details_markdown"]
    assert "| Paquetes: | 1 paquete |" in product["technical_details_markdown"]
    assert "| Volumen por paquete: | 31,1 l |" in product["technical_details_markdown"]
    assert "description" not in product and "specifications" not in product
    assert product["availability_evidence"] == [{
        "type": "visible_stock_text",
        "text": "Stock disponible en IKEA La Laguna, ventas online y ventas telefónicas",
        "source": "product-page text",
    }]
    assert "structured_data_invalid" in result["warnings"]
    assert all("raw_html" not in key for key in result)


def test_billy_composite_stays_one_product_and_keeps_repeated_package_specs():
    result = extract_product_content(BILLY_URL, fixture("billy_oxberg_product.html"))

    assert result["extraction_status"] == "ok"
    product = result["product"]
    assert product["sku"] == "19581536"
    assert product["price"] == 94
    assert product["currency"] == "EUR"
    assert product["name"].startswith("BILLY / OXBERG")
    assert product["details_markdown"] is None
    technical = product["technical_details_markdown"]
    assert "| Ancho: | 40 cm |" in technical
    assert "| Altura: | 202 cm |" in technical
    assert "| Paquetes: | 1 paquete |" in technical
    assert "| Largo: | 211 cm |" in technical
    assert "| Largo: | 206 cm |" in technical
    assert "| Peso Neto: | 24,22 kg |" in technical
    assert "description" not in product and "specifications" not in product


def test_original_price_is_separate_from_current_price():
    html = f"""<html><head><meta property='product:price:currency' content='EUR'></head><body>
    <main><article class='product-detail'><h1>Desk D</h1><p>Article number 12345678</p>
    <div class='price'><del class='oldPrice'>79,99 €</del><span class='currentPrice'>59,50 €</span></div>
    </article></main></body></html>"""
    result = extract_product_content("https://shop.example/pd/desk-12345678", html)
    assert result["extraction_status"] == "ok"
    assert result["product"]["price"] == 59.5
    assert result["product"]["original_price"] == 79.99
    assert result["product"]["currency"] == "EUR"


def test_original_price_marker_on_ancestor_stays_distinct_from_current_price():
    html = """<html><head><meta property='product:price:currency' content='EUR'></head><body>
    <main><article class='product-detail'><h1>Chair</h1><p>Article number 12345678</p>
      <div class='originalPrice'><span class='price'>100 €</span></div>
      <span class='currentPrice'>80 €</span>
    </article></main></body></html>"""
    result = extract_product_content("https://shop.example/pd/chair-12345678", html)
    assert result["extraction_status"] == "ok"
    assert result["product"]["price"] == 80
    assert result["product"]["original_price"] == 100
    assert "price_conflict" not in result["warnings"]


def _recommendation_only_pdp():
    return """<html><body><main><article class='product-detail'>
    <h1>Target item</h1><p>Article number 12345678</p>
    <section class='related-products upsell-products'>
      <h2>Recommended items</h2>
      <span class='currentPrice'>999 €</span>
      <table><tr><th>Composition</th><td>Wrong product material</td></tr>
        <tr><th>Color</th><td>Wrong product color</td></tr></table>
      <p>Stock disponible en tienda recomendada</p>
    </section>
    </article></main></body></html>"""


def test_recommendation_price_does_not_fill_missing_main_product_price():
    result = extract_product_content("https://shop.example/pd/target-12345678", _recommendation_only_pdp())
    assert result["extraction_status"] == "partial"
    assert result["product"]["price"] is None
    assert result["product"]["currency"] is None


def test_recommendation_specifications_are_not_assigned_to_main_product():
    result = extract_product_content("https://shop.example/pd/target-12345678", _recommendation_only_pdp())
    assert result["product"]["details_markdown"] is None
    assert result["product"]["technical_details_markdown"] is None


def test_recommendation_availability_is_not_assigned_to_main_product():
    result = extract_product_content("https://shop.example/pd/target-12345678", _recommendation_only_pdp())
    assert result["product"]["availability_evidence"] == []


def test_nonstandard_labels_are_preserved_in_explicit_product_spec_table():
    html = """<html><body><main><article class='product-detail'>
    <h1>Fabric chair</h1><p>Article number 12345678</p>
    </article>
    <section class='product-specifications'><h2>Specifications</h2><table>
      <tr><th>Composición</th><td>80 % algodón, 20 % lino</td></tr>
      <tr><th>Color</th><td>Azul oscuro</td></tr>
      <tr><th>Tipo de tejido</th><td>Terciopelo</td></tr>
      <tr><th>Color</th><td>Azul oscuro (detalle)</td></tr>
    </table></section></main></body></html>"""
    result = extract_product_content("https://shop.example/pd/fabric-chair-12345678", html)
    markdown = result["product"]["technical_details_markdown"]
    assert "## Specifications" in markdown
    assert "| Composición | 80 % algodón, 20 % lino |" in markdown
    assert "| Color | Azul oscuro |" in markdown
    assert "| Tipo de tejido | Terciopelo |" in markdown
    assert "| Color | Azul oscuro (detalle) |" in markdown


def test_http_jsonld_url_matches_https_page_only_with_same_sku_host_and_path():
    https_url = "https://shop.example/pd/table-12345678"
    html = f"""<html><body><main><h1>Table</h1></main>
    <script type='application/ld+json'>{json.dumps({
        "@type": "Product", "name": "Table", "sku": "12345678",
        "url": "http://shop.example/pd/table-12345678", "brand": "Maker",
        "offers": {"price": "24.50", "priceCurrency": "EUR"}
    })}</script></body></html>"""
    result = extract_product_content(https_url, html)
    assert result["extraction_status"] == "ok"
    assert result["product"]["url"] == https_url
    assert result["product"]["brand"] == "Maker"
    assert result["product"]["price"] == 24.5


def test_http_jsonld_url_with_different_host_remains_conflicting():
    https_url = "https://shop.example/pd/table-12345678"
    html = f"""<html><body><main><h1>Table</h1></main>
    <script type='application/ld+json'>{json.dumps({
        "@type": "Product", "name": "Wrong host product", "sku": "12345678",
        "url": "http://other.example/pd/table-12345678", "brand": "Wrong brand",
        "offers": {"price": "999", "priceCurrency": "USD"}
    })}</script></body></html>"""
    result = extract_product_content(https_url, html)
    assert result["extraction_status"] == "partial"
    assert result["product"]["name"] == "Table"
    assert result["product"]["brand"] is None
    assert result["product"]["price"] is None
    assert "identity_conflict" in result["warnings"]


@pytest.mark.parametrize(("value", "expected"), [("1.234,50", 1234.5), ("24,99", 24.99), ("24.99", 24.99), ("29", 29)])
def test_price_number_locale_parsing(value, expected):
    html = f"""<html><head><meta property='product:price:amount' content='{value}'><meta property='product:price:currency' content='GBP'></head>
    <body><main><h1>Item</h1><p>Article number 12345678</p></main></body></html>"""
    result = extract_product_content("https://shop.example/pd/item-12345678", html)
    assert result["product"]["price"] == expected
    assert result["product"]["currency"] == "GBP"


def test_valid_jsonld_is_used_only_when_identity_and_url_match():
    url = "https://shop.example/pd/table-12345678"
    html = f"""<html><body><main><h1>Table</h1></main>
    <script type='application/ld+json'>{json.dumps({
        "@context": "https://schema.org", "@type": "Product", "name": "Table",
        "sku": "12345678", "brand": {"@type": "Brand", "name": "Maker"},
        "url": url, "image": "/table.jpg", "offers": {"price": "24.50", "priceCurrency": "EUR"}
    })}</script></body></html>"""
    result = extract_product_content(url, html)
    assert result["extraction_status"] == "ok"
    assert result["product"]["brand"] == "Maker"
    assert result["product"]["price"] == 24.5
    assert result["product"]["image_url"] == "https://shop.example/table.jpg"


def test_conflicting_jsonld_url_does_not_contaminate_product_fields():
    url = "https://shop.example/pd/table-12345678"
    html = f"""<html><head><meta property='product:price:amount' content='24'><meta property='product:price:currency' content='EUR'></head>
    <body><main><h1>Table</h1><p>Article number 12345678</p></main>
    <script type='application/ld+json'>{json.dumps({"@type": "Product", "name": "Wrong product", "sku": "12345678", "url": "https://shop.example/pd/other-12345678", "brand": "Wrong brand", "offers": {"price": 999, "priceCurrency": "USD"}})}</script>
    </body></html>"""
    result = extract_product_content(url, html)
    assert result["extraction_status"] == "partial"
    assert result["product"]["name"] == "Table"
    assert result["product"]["brand"] is None
    assert result["product"]["price"] == 24
    assert result["product"]["currency"] == "EUR"
    assert "identity_conflict" in result["warnings"]


def test_malformed_embedded_url_is_ignored_without_crashing():
    url = "https://shop.example/pd/table-12345678"
    html = f"""<html><head><meta property='product:price:amount' content='24'><meta property='product:price:currency' content='EUR'></head>
    <body><main><h1>Table</h1><p>Article number 12345678</p></main>
    <script type='application/ld+json'>{json.dumps({"@type": "Product", "sku": "12345678", "url": "https://[invalid", "name": "Wrong"})}</script>
    </body></html>"""
    result = extract_product_content(url, html)
    assert result["extraction_status"] == "partial"
    assert result["product"]["name"] == "Table"
    assert "identity_conflict" in result["warnings"]


def test_conflicting_page_local_item_records_are_not_combined():
    html = """<html><head><meta property='product:price:amount' content='24'><meta property='product:price:currency' content='EUR'></head>
    <body><main><article class='product-detail'><h1>Table</h1><p>Article number 12345678</p></article></main>
    <script>var itemGtag={"item_name":"Table","item_id":12345678,"price":"24"};
      var itemGtag={"item_name":"Other table","item_id":12345678,"price":"999"};</script></body></html>"""
    result = extract_product_content("https://shop.example/pd/table-12345678", html)
    assert result["extraction_status"] == "partial"
    assert result["product"]["name"] == "Table"
    assert result["product"]["price"] == 24
    assert "identity_conflict" in result["warnings"]


def test_ambiguous_product_recommendations_do_not_create_a_product():
    html = """<html><body><h1>Search results</h1><section class='recommendations'>
    <article><h2>Recommended Desk</h2><a href='/pd/desk-12345678'>Desk</a><span>24 EUR</span></article>
    </section></body></html>"""
    result = extract_product_content("https://shop.example/search?q=desk", html)
    assert result == {
        "mode": "product",
        "source_url": "https://shop.example/search?q=desk",
        "extraction_status": "no_product",
        "product": None,
        "warnings": [],
    }


@pytest.mark.parametrize(("html", "content_status"), [("<html><body> </body></html>", "empty"), ("<html><body>blocked</body></html>", "blocked")])
def test_unusable_content_has_null_product(html, content_status):
    result = extract_product_content("https://shop.example/pd/item-12345678", html, content_status)
    assert result["extraction_status"] == "unusable"
    assert result["product"] is None


def test_missing_optional_fields_do_not_make_product_partial():
    html = "<html><body><main><h1>Simple product</h1><p>Article number 12345678</p></main></body></html>"
    result = extract_product_content("https://shop.example/pd/simple-12345678", html)
    assert result["extraction_status"] == "partial"
    product = result["product"]
    assert product["sku"] == "12345678"
    assert product["price"] is None
    assert product["currency"] is None
    assert product["image_url"] is None
    assert product["original_price"] is None
    assert product["details_markdown"] is None
    assert product["technical_details_markdown"] is None
    assert product["availability_evidence"] == []


def test_long_technical_markdown_is_bounded_and_warns_when_truncated():
    rows = "".join(f"<tr><th>Característica {index}</th><td>{'valor técnico ' * 8}{index}</td></tr>" for index in range(1500))
    html = f"""<html><head><meta property='product:price:amount' content='1'><meta property='product:price:currency' content='EUR'></head><body><main><h1>Item</h1><p>Article number 12345678</p>
    <div id='product-details'><h2>Ficha técnica</h2><table>{rows}</table></div></main></body></html>"""
    result = extract_product_content("https://shop.example/pd/item-12345678", html)
    assert result["product"]["technical_details_markdown"]
    assert len(result["product"]["technical_details_markdown"]) <= 40_000
    assert "technical_details_markdown_truncated" in result["warnings"]
