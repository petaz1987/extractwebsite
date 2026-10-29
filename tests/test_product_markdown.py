from product_extraction import extract_product_content


URL = "https://shop.example/pd/product-12345678"


def _page(content, *, extra=""):
    return f"""<html><head>
      <meta property='product:price:amount' content='24.99'>
      <meta property='product:price:currency' content='EUR'>
    </head><body><main><article class='product-detail'>
      <h1>Example product</h1><p>Article number 12345678</p>{content}
    </article>{extra}</main></body></html>"""


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


def test_related_hidden_content_is_excluded_but_trusted_inactive_tab_is_extracted():
    html = _page("""
      <a role='tab' href='#description-panel'>Más información</a>
      <button role='tab' aria-controls='technical-panel' aria-selected='false'>Ficha técnica</button>
      <section id='description-panel'><p>Visible product description.</p></section>
      <section id='technical-panel' hidden aria-hidden='true'>
        <h2>Ficha técnica</h2><table><tr><th>Alto</th><td>40 cm</td></tr></table>
      </section>
    """, extra="""
      <section hidden aria-hidden='true'><p>Unrelated hidden page content.</p></section>
      <aside class='related-products'><section hidden id='other-specs'>
        <h2>Ficha técnica</h2><p>Unrelated hidden product spec.</p>
      </section></aside>
    """)
    product = extract_product_content(URL, html)["product"]
    assert "Visible product description." in product["details_markdown"]
    assert "| Alto | 40 cm |" in product["technical_details_markdown"]
    assert "Unrelated hidden" not in str(product)
    assert "Unrelated hidden product" not in str(product)


def test_br_preserves_line_boundaries_in_paragraphs_and_technical_list_values():
    html = _page("""
      <a role='tab' href='#description'>Más información</a>
      <div id='description'><p>First line<br>Second line</p></div>
      <a role='tab' href='#product-details'>Ficha técnica</a>
      <div id='product-details'><h2>Conectividad</h2>
        <ul><li><strong>Interfaces:</strong> Wi-Fi 6<br>Bluetooth 5.3<br>USB-C</li></ul>
      </div>
    """)
    product = extract_product_content(URL, html)["product"]
    assert "First line  \nSecond line" in product["details_markdown"]
    assert "Wi-Fi 6  \nBluetooth 5.3  \nUSB-C" in product["technical_details_markdown"]


def test_markdown_links_allow_only_safe_schemes_case_insensitively():
    html = _page("""
      <a role='tab' href='#description'>Más información</a>
      <div id='description'><p>
        <a href='JaVaScRiPt:alert(1)'>script</a>
        <a href='vbscript:msgbox(1)'>vbscript</a>
        <a href='data:text/html,unsafe'>data</a>
        <a href='https://example.test/product'>secure</a>
        <a href='/product/manual'>relative</a>
        <a href='mailto:support@example.test'>email</a>
      </p></div>
    """)
    markdown = extract_product_content(URL, html)["product"]["details_markdown"]
    assert "[secure](https://example.test/product)" in markdown
    assert "[relative](/product/manual)" in markdown
    assert "[email](mailto:support@example.test)" in markdown
    assert "javascript:" not in markdown.casefold()
    assert "vbscript:" not in markdown.casefold()
    assert "data:text" not in markdown.casefold()


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
      <aside class='related-products'><div id='other-description'>
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


def test_untabbed_fallback_never_scans_a_second_product_outside_main_context():
    html = _page("", extra="""
      <article class='product-detail secondary-product'>
        <h1>Other product</h1><section class='product-specifications'>
          <h2>Información del artículo</h2>
          <table><tr><th>Color</th><td>Unrelated red item</td></tr></table>
        </section>
      </article>
    """)
    html = html.replace(
        "</article>",
        "<section><h2>Product description</h2><p>Main product content.</p></section></article>",
        1,
    )
    product = extract_product_content(URL, html)["product"]
    assert "Main product content." in product["details_markdown"]
    assert product["technical_details_markdown"] is None
    assert "Unrelated red item" not in str(product)


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


def test_other_product_tabs_and_duplicate_target_ids_do_not_contaminate_main_product():
    html = _page("""
      <a role='tab' href='#main-details'>Más información</a>
      <section id='main-details'><p>Main product narrative.</p></section>
      <article class='product-card'><h2>Other item</h2>
        <button role='tab' aria-controls='other-tech'>Ficha técnica</button>
        <section id='other-tech'><table><tr><th>Other RAM</th><td>999 GB</td></tr></table></section>
      </article>
      <aside class='recommendations'><div id='duplicate'>wrong</div></aside>
      <div id='duplicate'>also wrong</div>
    """)
    product = extract_product_content(URL, html)["product"]
    assert product["details_markdown"] == "Main product narrative."
    assert product["technical_details_markdown"] is None
    assert "Other RAM" not in str(product)


def test_div_product_card_tabs_cannot_contaminate_selected_product():
    html = _page("""
      <a role='tab' href='#main-details'>Más información</a>
      <section id='main-details'><p>Main product narrative.</p></section>
    """, extra="""
      <div class='product-card'><h2>Other item</h2>
        <button role='tab' aria-controls='other-tech'>Ficha técnica</button>
        <section id='other-tech'><table><tr><th>Other RAM</th><td>999 GB</td></tr></table></section>
      </div>
    """)
    product = extract_product_content(URL, html)["product"]
    assert product["details_markdown"] == "Main product narrative."
    assert product["technical_details_markdown"] is None
    assert "Other RAM" not in str(product)


def test_nested_product_information_wrapper_shares_product_tab_ownership():
    html = """<html><head>
      <meta property='product:price:amount' content='24.99'>
      <meta property='product:price:currency' content='EUR'></head><body><main>
      <div class='product-detail'><div class='product-information'><h1>Camera</h1>
        <p>Article number 12345678</p></div>
        <a href='#tech'>Ficha técnica</a>
        <section id='tech'>Camera sensor 24 MP</section>
      </div></main></body></html>"""
    product = extract_product_content(URL, html)["product"]
    assert product["technical_details_markdown"] == "Camera sensor 24 MP"


def test_product_card_item_with_tab_cannot_contaminate_selected_product():
    html = _page("<div id='description'><p>Correct.</p></div>", extra="""
      <div class='product-card-item'><h2>Other item</h2>
        <button role='tab' aria-controls='other-tech'>Ficha técnica</button>
        <section id='other-tech'><table><tr><th>Other RAM</th><td>999 GB</td></tr></table></section>
      </div>
    """)
    product = extract_product_content(URL, html)["product"]
    assert product["details_markdown"] == "Correct."
    assert product["technical_details_markdown"] is None
    assert "Other RAM" not in str(product)


def test_both_panel_nesting_directions_keep_every_selected_section():
    html = _page("""
      <a role='tab' href='#outer-tech'>Ficha técnica</a>
      <a role='tab' href='#inner-details'>Más información</a>
      <section id='outer-tech'><h2>Ficha técnica</h2><p>RAM 12 GB</p>
        <div id='inner-details'><p>Product narrative.</p></div><p>Batería 5000 mAh</p>
      </section>
    """)
    product = extract_product_content(URL, html)["product"]
    assert "Product narrative." in product["details_markdown"]
    assert "RAM 12 GB" in product["technical_details_markdown"]
    assert "Batería 5000 mAh" in product["technical_details_markdown"]

    reverse = _page("""
      <a role='tab' href='#outer-details'>Más información</a>
      <a role='tab' href='#inner-tech'>Ficha técnica</a>
      <section id='outer-details'><p>Product narrative.</p>
        <div id='inner-tech'><table><tr><th>RAM</th><td>12 GB</td></tr></table></div>
      </section>
    """)
    product = extract_product_content(URL, reverse)["product"]
    assert "Product narrative." in product["details_markdown"]
    assert "| RAM | 12 GB |" in product["technical_details_markdown"]


def test_heading_led_sections_work_in_plain_div_and_hidden_tab_wrapper():
    html = _page("""
      <div><h2>Información del artículo</h2>
        <table><tr><th>Altura</th><td>160 cm</td></tr></table>
      </div>
      <button role='tab' aria-controls='hidden-tech'>Ficha técnica</button>
      <div hidden><section id='hidden-tech'><p>Conectividad Wi-Fi 6</p></section></div>
    """)
    product = extract_product_content(URL, html)["product"]
    assert "| Altura | 160 cm |" in product["technical_details_markdown"]
    assert "Conectividad Wi-Fi 6" in product["technical_details_markdown"]


def test_direct_heading_sibling_sections_work_in_article_and_product_div():
    article = _page("""
      <h2>Ficha técnica</h2>
      <p>Valores principales</p>
      <table><tr><th>RAM</th><td>12 GB</td></tr></table>
      <h2>Recomendaciones</h2><p>Other product content</p>
    """)
    product = extract_product_content(URL, article)["product"]
    assert "## Ficha técnica" in product["technical_details_markdown"]
    assert "Valores principales" in product["technical_details_markdown"]
    assert "| RAM | 12 GB |" in product["technical_details_markdown"]
    assert "Other product content" not in product["technical_details_markdown"]

    product_div = """<html><head>
      <meta property='product:price:amount' content='24.99'>
      <meta property='product:price:currency' content='EUR'></head><body><main>
      <div class='product-detail'><h1>Example product</h1><p>Article number 12345678</p>
        <h2>Ficha técnica</h2><ul><li>Peso: 5 kg</li></ul>
        <h2>Cambios y devoluciones</h2><p>Policy text</p>
      </div></main></body></html>"""
    product = extract_product_content(URL, product_div)["product"]
    assert "## Ficha técnica" in product["technical_details_markdown"]
    assert "Peso: 5 kg" in product["technical_details_markdown"]
    assert "Policy text" not in product["technical_details_markdown"]


def test_deep_inline_and_malformed_href_are_bounded_and_nonfatal():
    deep = "<span>" * 400 + "Deep text" + "</span>" * 400
    html = _page(f"<div id='description'><p>{deep}</p></div>")
    result = extract_product_content(URL, html)
    assert result["product"]["details_markdown"] is None
    assert "details_markdown_truncated" in result["warnings"]
    malformed = extract_product_content(
        URL, _page("<div id='description'><p>Text <a href='https://['>manual</a></p></div>")
    )
    assert "Text manual" in malformed["product"]["details_markdown"]


def test_wrapped_nested_lists_and_rowspan_colspan_associations_are_preserved():
    html = _page("""
      <div id='description'><ul><li>Connectivity<div><ul><li>Wi-Fi 6</li><li>Bluetooth 5.3</li></ul></div></li></ul></div>
      <div id='product-details'><table><tr><th>Group</th><th>Label</th><th>Value</th></tr>
        <tr><td rowspan='2'>Display</td><td>Width</td><td>60 cm</td></tr>
        <tr><td colspan='2'>Height: 40 cm</td></tr></table></div>
    """)
    product = extract_product_content(URL, html)["product"]
    assert "Connectivity" in product["details_markdown"]
    assert "  - Wi-Fi 6" in product["details_markdown"]
    assert "  - Bluetooth 5.3" in product["details_markdown"]
    assert "| Display | Height: 40 cm | Height: 40 cm |" in product["technical_details_markdown"]


def test_markdown_delimiters_in_plain_source_and_link_labels_are_escaped():
    html = _page("""
      <div id='description'><p>literal [label](javascript:alert(1)) and *stars*</p>
        <a href='https://safe.example'>Manual](javascript:alert%281%29) [safe</a></div>
    """)
    markdown = extract_product_content(URL, html)["product"]["details_markdown"]
    assert r"\[label\]\(javascript:alert\(1\)\)" in markdown
    assert r"\*stars\*" in markdown
    assert "javascript:alert%281%29) [safe](https://safe.example)" not in markdown


def test_heading_list_and_html_like_source_text_are_rendered_literally():
    html = _page("""
      <div id='description'><p># Not a heading</p><p>- Not a list item</p>
        <p>&lt;img src=x onerror=alert(1)&gt;</p>
        <p>literal [link](https://evil.example) and \\ backslash</p></div>
    """)
    markdown = extract_product_content(URL, html)["product"]["details_markdown"]
    assert r"\# Not a heading" in markdown
    assert r"\- Not a list item" in markdown
    assert r"\<img src=x onerror=alert\(1\)>" in markdown
    assert r"\[link\]\(https://evil.example\)" in markdown


def test_paragraph_ordered_and_plus_prefixes_remain_literal_prose():
    html = _page("""
      <div id='description'><p>1. This is prose, not a list.</p>
        <p>+ This is prose, not a list.</p></div>
    """)
    markdown = extract_product_content(URL, html)["product"]["details_markdown"]
    assert markdown == r"1\. This is prose, not a list." + "\n\n" + r"\+ This is prose, not a list."


def test_html_comments_are_not_rendered():
    html = _page("<div id='description'><p>Visible product content</p><!-- hidden merchant note --></div>")
    markdown = extract_product_content(URL, html)["product"]["details_markdown"]
    assert markdown == "Visible product content"
    assert "hidden merchant note" not in markdown


def test_duplicate_ids_remain_unresolved_but_overlapping_selectors_do_not_drop_data():
    duplicate = _page("""
      <button role='tab' aria-controls='same-target'>Ficha técnica</button>
      <section id='same-target'><p>First ambiguous target</p></section>
      <section id='same-target'><p>Second ambiguous target</p></section>
    """)
    result = extract_product_content(URL, duplicate)
    assert result["product"]["technical_details_markdown"] is None
    assert "technical_details_extraction_failed" in result["warnings"]

    overlap = _page("""
      <a role='tab' href='#all-details'>Más información</a>
      <section id='all-details'><p>Before technical panel</p>
        <section id='product-details'><p>RAM 12 GB</p></section>
        <p>After technical panel</p></section>
      <a role='tab' href='#product-details'>Ficha técnica</a>
    """)
    product = extract_product_content(URL, overlap)["product"]
    assert "Before technical panel" in product["details_markdown"]
    assert "After technical panel" in product["details_markdown"]
    assert "RAM 12 GB" in product["technical_details_markdown"]


def test_identical_package_sections_are_preserved_in_source_order():
    package = "<section><h2>Paquetes</h2><table><tr><th>Peso</th><td>10 kg</td></tr></table></section>"
    html = _page(package + package)
    markdown = extract_product_content(URL, html)["product"]["technical_details_markdown"]
    assert markdown.count("## Paquetes") == 2
    assert markdown.index("## Paquetes") < markdown.rindex("## Paquetes")


def test_navigation_category_heading_is_excluded_and_product_headings_survive():
    html = _page("""
      <section><nav><h2>Herramientas para coche</h2></nav>
        <h2>Ficha técnica</h2><p>Medidas del producto</p>
      </section>
    """)
    markdown = extract_product_content(URL, html)["product"]["technical_details_markdown"]
    assert "Herramientas para coche" not in markdown
    assert "## Ficha técnica" in markdown
    assert "Medidas del producto" in markdown
