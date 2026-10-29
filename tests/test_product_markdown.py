from product_extraction import extract_product_content


URL = "https://shop.example/pd/product-12345678"


def _page(content, *, extra=""):
    return f"""<html><head>
      <meta property='product:price:amount' content='24.99'>
      <meta property='product:price:currency' content='EUR'>
    </head><body><main><article class='product-detail'>
      <h1>Example product</h1><p>Article number 12345678</p>
    </article>
    {content}{extra}</main></body></html>"""


def test_tab_relationships_split_details_from_separate_technical_panel():
    html = _page("""
      <div role='tablist'>
        <a role='tab' href='#details-panel'>Más información</a>
        <button role='tab' aria-controls='technical-panel'>Ficha técnica</button>
      </div>
      <section id='details-panel'>
        <h2>Descripción</h2><p>First paragraph with <strong>emphasis</strong>.</p>
        <h3>Características incluidas</h3>
        <ol><li>First item<ul><li>Nested item</li></ul></li><li>Second item</li></ol>
      </section>
      <section id='technical-panel'><h2>Ficha técnica</h2>
        <table><thead><tr><th>Parámetro</th><th>Valor</th></tr></thead>
        <tbody><tr><td>Material</td><td>Acero inoxidable</td></tr>
        <tr><td>Longitud</td><td>25 cm</td></tr></tbody></table>
      </section>
    """)
    result = extract_product_content(URL, html)
    product = result["product"]

    assert result["extraction_status"] == "ok"
    assert product["details_markdown"] == (
        "## Descripción\n\nFirst paragraph with **emphasis**.\n\n"
        "### Características incluidas\n\n1. First item\n  - Nested item\n2. Second item"
    )
    assert product["technical_details_markdown"] == (
        "## Ficha técnica\n\n| Parámetro | Valor |\n| --- | --- |\n"
        "| Material | Acero inoxidable |\n| Longitud | 25 cm |"
    )
    assert set(product) == {
        "name", "sku", "brand", "price", "original_price", "currency", "variant", "url",
        "image_url", "availability_evidence", "details_markdown", "technical_details_markdown",
    }


def test_embedded_specifications_stay_in_details_when_no_separate_technical_tab_exists():
    html = _page("""
      <a role='tab' href='#description'>Product description</a>
      <div id='description'><p>Main product text.</p>
        <h3>Specifications</h3><table><tr><th>Width</th><td>15 cm</td></tr></table>
      </div>
    """)
    product = extract_product_content(URL, html)["product"]
    assert "Main product text." in product["details_markdown"]
    assert "| Width | 15 cm |" in product["details_markdown"]
    assert product["technical_details_markdown"] is None


def test_nested_related_panels_and_overlapping_duplicate_containers_are_excluded():
    html = _page("""
      <a role='tab' href='#description'>Más información</a>
      <div id='description'><p>Trusted product details.</p>
        <section><p>Nested visible content.</p></section>
      </div>
    """, extra="""
      <aside class='related-products'><div id='description'>
        <p>Unrelated product content.</p><table><tr><th>Price</th><td>999</td></tr></table>
      </div></aside>
      <div class='recommendations'><p>Recommended item details.</p></div>
    """)
    product = extract_product_content(URL, html)["product"]
    markdown = product["details_markdown"]
    assert markdown.count("Trusted product details.") == 1
    assert "Nested visible content." in markdown
    assert "Unrelated product content" not in markdown
    assert "Recommended item details" not in markdown
    assert "999" not in markdown


def test_absent_content_sections_are_null_without_warning():
    result = extract_product_content(URL, _page(""))
    assert result["product"]["details_markdown"] is None
    assert result["product"]["technical_details_markdown"] is None
    assert not any("extraction_failed" in warning for warning in result["warnings"])


def test_associated_but_empty_panel_is_distinguished_from_missing_panel():
    html = _page("<a role='tab' href='#description'>Más información</a><div id='description'></div>")
    result = extract_product_content(URL, html)
    assert result["product"]["details_markdown"] is None
    assert "details_extraction_failed" in result["warnings"]


def test_long_technical_section_is_not_cut_at_legacy_description_limit():
    paragraph = "Long product parameter text remains intact beyond five thousand characters. "
    rows = "".join(f"<tr><th>Parameter {i}</th><td>{paragraph}{i}</td></tr>" for i in range(100))
    html = _page(f"""
      <a role='tab' href='#product-details'>Technical specifications</a>
      <div id='product-details'><table>{rows}</table></div>
    """)
    markdown = extract_product_content(URL, html)["product"]["technical_details_markdown"]
    assert len(markdown) > 5_000
    assert "Parameter 99" in markdown
