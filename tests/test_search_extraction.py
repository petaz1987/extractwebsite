from pathlib import Path

import pytest

from search_extraction import MAX_SEARCH_PRODUCTS, _currency_from_text, _parse_numeric_price, extract_search_content


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_ikea_cards_bind_each_product_fields_by_sku_and_use_current_displayed_price():
    html = (FIXTURE_DIR / "ikea_search_cards.html").read_text(encoding="utf-8")

    result = extract_search_content("https://shop.example/ten/es/search?q=items", html)

    assert result["mode"] == "search"
    assert result["source_url"] == "https://shop.example/ten/es/search?q=items"
    assert result["extraction_status"] == "ok"
    assert result["product_count"] == 2
    assert result["truncated"] is False
    assert result["products"] == [
        {
            "name": "ITEM ONE",
            "sku": "101",
            "price": 24.9,
            "currency": "EUR",
            "variant": "White, 60x30 cm",
            "url": "https://shop.example/ten/es/pd/item-one",
            "image_url": "https://shop.example/assets/one.jpg",
        },
        {
            "name": "ITEM TWO",
            "sku": "202",
            "price": 59,
            "currency": "EUR",
            "variant": "Oak, 80x40 cm",
            "url": "https://shop.example/ten/es/pd/item-two",
            "image_url": "https://images.example.test/two.jpg",
        },
    ]


def test_missing_optional_product_fields_are_null():
    html = '''<div class="itemBlock" id="art_303_x">
      <a data-item="303" href="/ten/es/pd/item-three"><h3>ITEM THREE</h3></a>
      <div data-id="itemFacts-303">Blue</div>
    </div>'''

    result = extract_search_content("https://shop.example/ten/es/search", html)

    assert result["extraction_status"] == "ok"
    assert result["products"] == [{
        "name": "ITEM THREE",
        "sku": "303",
        "price": None,
        "currency": None,
        "variant": "Blue",
        "url": "https://shop.example/ten/es/pd/item-three",
        "image_url": None,
    }]


def test_unrelated_names_and_links_do_not_create_products():
    html = '''<h1>BAGGEBO</h1><span>24 €</span><a href="/pd/item">View item</a>'''

    result = extract_search_content("https://shop.example/search", html)

    assert result["extraction_status"] == "no_products"
    assert result["products"] == []
    assert result["product_count"] == 0
    assert result["truncated"] is False


def test_jysk_search_shell_without_product_records_is_no_products():
    result = extract_search_content(
        "https://jysk.es/search?query=BREIDTINDEN",
        "<html><body><div id='search-shell'>Search results are loading</div></body></html>",
    )

    assert result["extraction_status"] == "no_products"
    assert result["products"] == []


def test_ikea_baggebo_reference_skus_and_prices_are_extracted_from_bound_cards():
    expected = {
        "30621480": (24, "verde vivo, 60x30x80 cm", "baggebo-estanteria-verde-vivo-art-30621480", "980/1498087_PE1006161_S3.jpg"),
        "40553616": (59, "blanco, 78x30x92 cm", "baggebo-aparador-blanco-art-40553616", "417/1241722_PE919974_S3.jpg"),
        "50481172": (19, "metal/blanco, 60x25x116 cm", "baggebo-estanteria-blanco-art-50481172", "815/0981562_PE815396_S3.jpg"),
        "60481204": (29, "blanco, 50x30x80 cm", "baggebo-armario-con-puerta-blanco-art-60481204", "167/1016757_PE830615_S3.jpg"),
        "20436713": (29, "blanco, 50x25x160 cm", "baggebo-libreria-blanco-art-20436713", "815/0981552_PE815388_S3.jpg"),
        "80502998": (39, "metal/blanco, 34x30x116 cm", "baggebo-armario-con-puertas-vidrio-blanco-art-80502998", "815/0981557_PE815393_S3.jpg"),
    }
    cards = []
    records = []
    for sku, (price, variant, slug, image) in expected.items():
        cards.append(
            f'''<div class="itemBlock" id="art_{sku}_x">
              <a data-item="{sku}" href="/ten/es/pd/{slug}"><h3>BAGGEBO</h3></a>
              <div data-id="itemFacts-{sku}">{variant}</div>
              <div class="itemPriceBox" data-id="{sku}"><div class="itemPrice itemNormalPrice">
                <span data-price="{price}">{price} €</span>
              </div></div>
            </div>'''
        )
        records.append(
            f'<script>var item = {{"partnumber":"{sku}","name":"BAGGEBO",'
            f'"valid_design":"{variant}","current_price":{price},'
            f'"url":"/ten/es/pd/{slug}","default_image":"/assets/images/{image}"}};</script>'
        )

    html = "<html><body>" + "".join(cards) + "".join(reversed(records)) + "</body></html>"
    result = extract_search_content("https://shop.example/ten/es/search?q=BAGGEBO", html)

    assert result["extraction_status"] == "ok"
    assert result["product_count"] == 6
    assert result["truncated"] is False
    assert {
        product["sku"]: (product["price"], product["variant"], product["url"].rsplit("/", 1)[-1], product["image_url"].rsplit("/assets/images/", 1)[-1])
        for product in result["products"]
    } == {
        sku: (price, variant, slug, image)
        for sku, (price, variant, slug, image) in expected.items()
    }


def test_unusable_content_returns_unusable_without_product_records():
    result = extract_search_content("https://shop.example/search", "<div>challenge</div>", content_status="blocked")

    assert result["extraction_status"] == "unusable"
    assert result["products"] == []
    assert result["truncated"] is False


def test_failed_identifiable_card_makes_successful_extraction_partial():
    html = '''
    <div class="itemBlock" id="art_101_x">
      <a data-item="101" href="/pd/one"><h3>One</h3></a>
      <div class="itemPriceBox" data-id="101"><div class="itemNormalPrice"><span data-price="1">1 €</span></div></div>
    </div>
    <div class="itemBlock" id="art_202_x">
      <a data-item="202" href="javascript:alert(1)"><h3>Two</h3></a>
    </div>
    '''

    result = extract_search_content("https://shop.example/search", html)

    assert result["extraction_status"] == "partial"
    assert result["product_count"] == 1
    assert result["truncated"] is False


def test_output_limit_marks_partial_and_truncated_only_when_more_products_remain():
    cards = []
    for sku in range(1, MAX_SEARCH_PRODUCTS + 2):
        cards.append(
            f'''<div class="itemBlock" id="art_{sku}_x">
            <a data-item="{sku}" href="/pd/{sku}"><h3>ITEM {sku}</h3></a></div>'''
        )

    result = extract_search_content("https://shop.example/search", "".join(cards))

    assert result["extraction_status"] == "partial"
    assert result["product_count"] == MAX_SEARCH_PRODUCTS
    assert result["truncated"] is True


def test_exactly_output_limit_is_not_reported_as_truncated():
    cards = []
    for sku in range(1, MAX_SEARCH_PRODUCTS + 1):
        cards.append(
            f'''<div class="itemBlock" id="art_{sku}_x">
            <a data-item="{sku}" href="/pd/{sku}"><h3>ITEM {sku}</h3></a></div>'''
        )

    result = extract_search_content("https://shop.example/search", "".join(cards))

    assert result["extraction_status"] == "ok"
    assert result["product_count"] == MAX_SEARCH_PRODUCTS
    assert result["truncated"] is False


@pytest.mark.parametrize(("raw", "source", "expected"), [
    ("24,90 €", "text", 24.9),
    ("1.234,00 €", "text", 1234),
    ("1.234 €", "text", 1234),
    ("2.500 €", "text", 2500),
    ("1,234.56", "text", 1234.56),
    ("24.90", "machine", 24.9),
    ("1.234", "machine", 1.234),
])
def test_localized_and_machine_price_formats(raw, source, expected):
    assert _parse_numeric_price(raw, source=source) == expected


def _card(sku, name="ITEM", href=None, price_markup="", variant="Variant"):
    href = href or f"/pd/{sku}"
    return f'''<div class="itemBlock" id="art_{sku}_x">
      <a data-item="{sku}" href="{href}"><h3>{name}</h3></a>
      <div data-id="itemFacts-{sku}">{variant}</div>{price_markup}
    </div>'''


def test_duplicate_cards_are_deduplicated_by_sku_and_pdp():
    card = _card("101", price_markup='<div class="itemPriceBox" data-id="101"><div class="itemNormalPrice"><span data-price="24">24 €</span></div></div>')

    result = extract_search_content("https://shop.example/search", card + card)

    assert result["extraction_status"] == "ok"
    assert result["product_count"] == 1
    assert result["products"][0]["sku"] == "101"
    assert result["truncated"] is False


def test_twenty_one_duplicate_cards_do_not_trigger_output_truncation():
    card = _card("101")

    result = extract_search_content("https://shop.example/search", card * 21)

    assert result["extraction_status"] == "ok"
    assert result["product_count"] == 1
    assert result["truncated"] is False


def test_distinct_variants_are_not_merged_and_conflicts_are_not_combined():
    distinct = extract_search_content(
        "https://shop.example/search",
        _card("101", name="ITEM", href="/pd/item-white", variant="White")
        + _card("102", name="ITEM", href="/pd/item-blue", variant="Blue"),
    )
    assert distinct["product_count"] == 2
    assert [item["variant"] for item in distinct["products"]] == ["White", "Blue"]

    same_sku_distinct_pages = extract_search_content(
        "https://shop.example/search",
        _card("101", name="ITEM", href="/pd/item-white", variant="White")
        + _card("101", name="ITEM", href="/pd/item-blue", variant="Blue"),
    )
    assert same_sku_distinct_pages["product_count"] == 2
    assert [item["url"] for item in same_sku_distinct_pages["products"]] == [
        "https://shop.example/pd/item-white",
        "https://shop.example/pd/item-blue",
    ]

    conflicted = extract_search_content(
        "https://shop.example/search",
        _card("101", name="First", variant="White") + _card("101", name="Second", variant="Blue"),
    )
    assert conflicted["extraction_status"] == "partial"
    assert conflicted["product_count"] == 1
    assert conflicted["products"][0]["name"] == "First"
    assert conflicted["products"][0]["variant"] == "White"


def test_matching_price_box_is_preferred_to_unassociated_box():
    html = '''<div class="itemBlock" id="art_101_x">
      <a data-item="101" href="/pd/101"><h3>ITEM</h3></a>
      <div class="itemPriceBox"><div class="itemNormalPrice"><span data-price="9">9 €</span></div></div>
      <div class="itemPriceBox" data-id="101"><div class="itemNormalPrice"><span data-price="24">24 €</span></div></div>
    </div>'''

    result = extract_search_content("https://shop.example/search", html)

    assert result["products"][0]["price"] == 24


def test_card_record_pdp_mismatch_uses_card_fields_and_marks_partial():
    html = '''<div class="itemBlock" id="art_101_x">
      <a data-item="101" href="/pd/card-product"><h3>CARD PRODUCT</h3></a>
      <div data-id="itemFacts-101">Card variant</div>
      <div class="itemPriceBox" data-id="101"><div class="itemNormalPrice"><span data-price="24">24 €</span></div></div>
      <img src="/images/card-product.jpg" alt="CARD PRODUCT">
    </div>
    <script>var item = {"partnumber":"101","name":"WRONG RECORD PRODUCT","valid_design":"Wrong variant","current_price":99,"currency":"USD","url":"/pd/different-product","default_image":"/images/wrong-product.jpg"};</script>'''

    result = extract_search_content("https://shop.example/search", html)

    assert result["extraction_status"] == "partial"
    assert result["product_count"] == 1
    assert result["products"][0] == {
        "name": "CARD PRODUCT",
        "sku": "101",
        "price": 24,
        "currency": "EUR",
        "variant": "Card variant",
        "url": "https://shop.example/pd/card-product",
        "image_url": "https://shop.example/images/card-product.jpg",
    }


@pytest.mark.parametrize(("value", "expected"), [
    ("24 IVA incluido", None),
    ("24 NOW", None),
    ("24 €", "EUR"),
    ("24 GBP", "GBP"),
    ("24 USD", "USD"),
])
def test_currency_text_accepts_supported_symbols_and_codes_only(value, expected):
    assert _currency_from_text(value) == expected


def test_ambiguous_unassociated_price_boxes_do_not_supply_a_price():
    html = '''<div class="itemBlock" id="art_101_x">
      <a data-item="101" href="/pd/101"><h3>ITEM</h3></a>
      <div class="itemPriceBox"><div class="itemNormalPrice"><span data-price="9">9 €</span></div></div>
      <div class="itemPriceBox"><div class="itemNormalPrice"><span data-price="24">24 €</span></div></div>
    </div>'''

    result = extract_search_content("https://shop.example/search", html)

    assert result["products"][0]["price"] is None
    assert result["products"][0]["currency"] is None


def test_embedded_record_currency_is_fallback_when_display_has_no_currency():
    html = '''<div class="itemBlock" id="art_101_x">
      <a data-item="101" href="/pd/101"><h3>ITEM</h3></a>
      <div class="itemPriceBox" data-id="101"><div class="itemNormalPrice"><span data-price="24">24</span></div></div>
    </div>
    <script>var item = {"partnumber":"101","name":"ITEM","currency":"USD","url":"/pd/101"};</script>'''

    result = extract_search_content("https://shop.example/search", html)

    assert result["products"][0]["price"] == 24
    assert result["products"][0]["currency"] == "USD"


def test_complete_embedded_record_is_usable_without_visible_cards():
    html = '''<html><head><title>Search results</title></head><body>
      <script>var item = {"partnumber":"101","name":"ITEM","current_price":24.9,"currency":"EUR","url":"/pd/101","default_image":"/101.jpg"};</script>
    </body></html>'''

    result = extract_search_content("https://shop.example/search", html, content_status="empty")

    assert result["extraction_status"] == "ok"
    assert result["product_count"] == 1
    assert result["products"][0]["sku"] == "101"
    assert result["products"][0]["price"] == 24.9


def test_empty_content_without_complete_embedded_records_is_unusable():
    result = extract_search_content(
        "https://shop.example/search",
        "<html><head><title>Empty</title></head><body></body></html>",
        content_status="empty",
    )

    assert result["extraction_status"] == "unusable"
    assert result["products"] == []
