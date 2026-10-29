"""Offline snapshots of Guanxe's observed PDP structure, never production HTTP tests.

These reduced fixtures reflect observed Ref, product-description, feature and stock
labels; full live HTML must still be verified before approval.
"""
import json
from time import perf_counter

import pytest

from product_extraction import extract_product_content
from guanxe_adapter import is_guanxe_product

JBL = 'https://guanxe.com/es/inicio/1019091-jbl-tune-527bt-auriculares-inalambricos-de-diadema-negros-1200130020902.html'
JBL_CANON = JBL.replace('/es/inicio/', '/es/auriculares/')
S24 = 'https://guanxe.com/es/smartphones/712264-samsung-galaxy-s24-ultra-12gb-512gb-sm-s928b-ds.html'
FLIP = 'https://guanxe.com/es/smartphones/1047586-samsung-galaxy-z-flip-7-fe-5g-8gb-256gb-negro-sm-761b-8806097428367.html'


def page(url, name, brand, price, summary, details, *, description=None, canonical=None, sku=None):
    product = {'@type':'Product','name':name,'url':url,'brand':{'name':brand},
               'image':'https://cdn.guanxe.com/product.jpg',
               'offers':{'price':str(price),'priceCurrency':'EUR'},
               'description':description or 'Generic metadata description'}
    if sku:product['sku']=sku
    return f'''<html><head><link rel="canonical" href="{canonical or url}">
    <meta property="product:price:amount" content="{price}">
    <meta property="product:price:currency" content="EUR">
    <script type="application/ld+json">{json.dumps(product)}</script></head>
    <body><main><div class="product-container">
      <div class="product-information"><h1>{name}</h1>
      <p class="product-reference">{summary}</p><p>Este producto está temporalmente agotado</p>
      <span>Advertencia: ¡Últimos artículos en inventario!</span>
      <p>Disponible en Canarias - Envío rápido - Garantía Oficial</p>
      </div></div>
    <div id="description">{details}</div>
    <aside class="related-products"><p>Ref: 999999</p><table><tr><td>RAM</td><td>999 GB</td></tr></table>
    <p>Stock disponible</p></aside></main></body></html>'''


def test_jbl_reference_table_description_and_canonical():
    html = page(JBL,'JBL Tune 527BT Auriculares Inalámbricos de Diadema - Negros','JBL',54.99,
                'Ref: 30458', '''<p>EAN 1200130020902</p>
                <p>Disfruta del sonido JBL Pure Bass con auriculares inalámbricos JBL Tune 527BT.</p>
                <h3>Especificaciones Técnicas:</h3><ul><li>Tamaño del controlador: 33 mm</li></ul>
                <table><thead><tr><th>Especificación</th><th>Detalle</th></tr></thead>
                <tbody><tr><td>Tamaño del controlador</td><td>33 mm</td></tr>
                <tr><td>Versión de Bluetooth</td><td>5.3</td></tr>
                <tr><td>Autonomía</td><td>Hasta 57 horas</td></tr>
                <tr><td>Peso</td><td>157 gramos</td></tr></tbody></table>
                <h3>Cambios y devoluciones</h3><p>GUANXE ofrece a sus clientes la posibilidad de ejercer su derecho de devolución.</p>''',
                canonical=JBL_CANON)
    t=perf_counter();result=extract_product_content(JBL,html);delta=perf_counter()-t
    assert delta < 1
    assert set(result)=={'mode','extraction_status','source_url','product','warnings'}
    assert result['extraction_status']=='ok', result
    p=result['product']
    assert p['sku']=='30458' and p['url']==JBL_CANON
    assert p['brand']=='JBL' and p['price']==54.99 and p['currency']=='EUR'
    assert p['variant'] is None
    assert len([x for x in p['specifications'] if x['label']=='Tamaño del controlador'])==1
    assert ('Versión de Bluetooth','5.3') in {(x['label'],x['value']) for x in p['specifications']}
    assert ('EAN','1200130020902') in {(x['label'],x['value']) for x in p['specifications']}
    assert 'devolución' not in p['description']
    assert all('999' not in str(x) for x in p['specifications'])
    assert all('Disponible en Canarias' not in x['text'] for x in p['availability_evidence'])
    assert any('temporalmente agotado' in x['text'] for x in p['availability_evidence'])


def test_s24_reference_ficha_technical_specifications():
    html = page(S24,'SAMSUNG Galaxy S24 Ultra 12GB 512GB SM-S928B/DS','Samsung',1049.99,
                'Ref: SM-S928B/DS', '''<p>Un diseño de referencia con ADN Galaxy.</p>
                <p>El Galaxy S24 Ultra presenta el diseño icónico y una batería de 5000 mAh.</p>
                <div id="product-details"><table class="data-sheet">
                <tr><th>Batería</th><td>5000mAh</td></tr>
                <tr><th>Capacidad</th><td>512GB</td></tr>
                <tr><th>RAM</th><td>12GB</td></tr></table></div>
                <h3>Cambios y devoluciones</h3><p>GUANXE ofrece a sus clientes la posibilidad de ejercer su derecho de devolución.</p>''')
    result=extract_product_content(S24,html)
    assert result['extraction_status']=='ok',result
    p=result['product']
    assert p['sku']=='SM-S928B/DS'
    assert p['price']==1049.99 and p['brand']=='Samsung'
    assert {(x['label'],x['value']) for x in p['specifications']} >= {('RAM','12GB'),('Batería','5000mAh'),('Capacidad','512GB')}
    assert 'devolución' not in p['description']
    assert any('temporalmente agotado' in x['text'] for x in p['availability_evidence'])


@pytest.mark.parametrize('url,product_name,row_count', [
    (JBL, 'JBL Tune 527BT', 16),
    (S24, 'Samsung Galaxy S24 Ultra', 4),
])
def test_category_heading_does_not_become_specification_group(url, product_name, row_count):
    rows = ''.join(f'<tr><td>Dato {i}</td><td>Valor {i}</td></tr>' for i in range(row_count))
    html = page(url, product_name, 'Brand', 54.99, 'Ref: 30458',
                f'<h3>Herramientas para coche</h3><table>{rows}</table>')
    result = extract_product_content(url, html)
    specs = result['product']['specifications']
    assert len(specs) == row_count
    assert {item['group'] for item in specs} == {'Technical specifications'}
    assert {(item['label'], item['value']) for item in specs} == {
        (f'Dato {i}', f'Valor {i}') for i in range(row_count)
    }


def test_out_of_stock_and_low_stock_preserve_both_and_warn_conflict():
    result = extract_product_content(JBL, page(
        JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
        '<p>Auriculares JBL con sonido de calidad.</p>'))
    evidence = result['product']['availability_evidence']
    assert any('temporalmente agotado' in item['text'] for item in evidence)
    assert any('Últimos artículos en inventario' in item['text'] for item in evidence)
    assert 'availability_conflict' in result['warnings']


def test_low_stock_alone_is_not_a_conflict_or_availability_confirmation():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>Auriculares JBL con sonido de calidad.</p>')
    html = html.replace('<p>Este producto está temporalmente agotado</p>', '')
    result = extract_product_content(JBL, html)
    assert any('Últimos artículos en inventario' in item['text']
               for item in result['product']['availability_evidence'])
    assert 'availability_conflict' not in result['warnings']
    assert 'availability' not in result['product']


def test_description_deduplicates_text_and_drops_search_promotion_and_navigation():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458', '''
        <p>Auriculares JBL con sonido Pure Bass y batería de larga duración.</p>
        <p>Auriculares JBL con sonido Pure Bass y batería de larga duración.</p>
        <p>Resultados de búsqueda de productos relacionados con JBL.</p>
        <nav><p>Inicio Auriculares Ofertas</p></nav>
        <p>Disponible en Canarias, envío rápido y garantía oficial.</p>''')
    result = extract_product_content(JBL, html)
    description = result['product']['description']
    assert description == 'Auriculares JBL con sonido Pure Bass y batería de larga duración.'
    assert 'Resultados de búsqueda' not in description
    assert 'Ofertas' not in description


def test_category_heading_is_not_added_to_description():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458', '''
        <h3>Herramientas para coche</h3>
        <p>Auriculares JBL con sonido Pure Bass y batería de larga duración.</p>''')
    result = extract_product_content(JBL, html)
    assert result['product']['description'] == (
        'Auriculares JBL con sonido Pure Bass y batería de larga duración.'
    )


def test_metadata_description_removes_repeated_seo_sentences():
    repeated = 'Compra online miles de productos. '
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>Más información</p>', description=(
                    'Auriculares JBL con sonido Pure Bass. ' + repeated * 3
                    + 'Resultados de búsqueda de productos relacionados.'))
    result = extract_product_content(JBL, html)
    assert result['product']['description'] == 'Auriculares JBL con sonido Pure Bass.'


def test_untruncated_trusted_description_recovers_text_after_5000_chars():
    boilerplate = 'Resultados de búsqueda de productos relacionados con JBL. '
    useful = 'Los auriculares JBL Tune 527BT ofrecen sonido Pure Bass y batería duradera.'
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>Más información</p>',
                description=(boilerplate * 105) + useful)
    result = extract_product_content(JBL, html)
    assert result['product']['description'] == useful
    assert 'field_truncated' not in result['warnings']


def test_trusted_description_recovers_text_after_10000_chars():
    boilerplate = 'Resultados de búsqueda de productos relacionados con JBL. '
    useful = 'Los JBL Tune 527BT ofrecen sonido Pure Bass y batería duradera.'
    source = (boilerplate * 200) + useful
    assert 10000 < source.index(useful) < 20000
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>Más información</p>', description=source)
    result = extract_product_content(JBL, html)
    assert result['product']['description'] == useful
    assert 'field_truncated' not in result['warnings']


def test_source_description_cap_keeps_truncation_warning_and_drops_seo_only_text():
    boilerplate = 'Resultados de búsqueda de productos relacionados con JBL. '
    useful_beyond_cap = 'Los auriculares ofrecen sonido JBL Pure Bass.'
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>Más información</p>',
                description=(boilerplate * 500) + useful_beyond_cap)
    result = extract_product_content(JBL, html)
    assert result['product']['description'] is None
    assert 'field_truncated' in result['warnings']


def test_single_description_paragraph_deduplicates_repeated_sentences():
    sentence = 'Auriculares JBL con sonido Pure Bass.'
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                f'<p>{sentence} {sentence} {sentence}</p>')
    result = extract_product_content(JBL, html)
    assert result['product']['description'] == sentence


def test_flip_generic_or_url_identifier_is_not_merchant_ref():
    html = page(FLIP, 'SAMSUNG Galaxy Z Flip 7 Fe 5G', 'Samsung', 899,
                '', '<p>Smartphone plegable Samsung con pantalla AMOLED.</p>',
                sku='4040102798')
    html = html.replace('<p class="product-reference"></p>',
                        '<p>EAN 8806097428367</p><p>Article number 4040102798</p>')
    result = extract_product_content(FLIP, html)
    assert result['product']['sku'] is None
    assert '1019091' not in str(result['product']['sku'])


def test_flip_nested_technical_list_and_no_false_variant():
    tech='''<h2>Samsung Galaxy Z Flip7 FE 5G</h2>
        <p>Un smartphone plegable con pantalla AMOLED.</p><h3>Características:</h3>
        <ul><li><strong>Exhibición:</strong><ul><li>Diagonal de la pantalla: 17 cm (6.7")</li>
        <li>Resolución de la pantalla: 2640 x 1080 Pixeles</li></ul></li>
        <li><strong>Procesador:</strong><ul><li>Modelo del procesador: 2400</li></ul></li>
        <li><strong>Medios de almacenaje:</strong><ul><li>Capacidad de RAM: 8 GB</li>
        <li>Capacidad de almacenamiento interno: 256 GB</li></ul></li></ul>
        <p>GUANXE ofrece a sus clientes la posibilidad de ejercer su derecho de devolución.</p>'''
    html=page(FLIP,'SAMSUNG Galaxy Z Flip 7 Fe 5G 8GB 256GB Negro (SM-761B)','Samsung',899,
              'Ref: 4040102798',tech,description='Promoción '+('palabras ' * 1200))
    result=extract_product_content(FLIP,html)
    assert result['extraction_status']=='ok',result
    p=result['product']
    assert p['sku']=='4040102798' and p['price']==899 and p['variant'] is None
    specs={(x['group'],x['label'],x['value']) for x in p['specifications']}
    assert ('Medios de almacenaje','Capacidad de RAM','8 GB') in specs
    assert ('Exhibición','Diagonal de la pantalla','17 cm (6.7")') in specs
    assert 'field_truncated' not in result['warnings']
    assert len(p['description'])<5000 and 'devolución' not in p['description']


def test_conflicting_visible_refs_do_not_leak_related_product():
    html=page(JBL,'JBL Tune 527BT','JBL',54.99,'Ref: 30458',
              '<p>Descripción buena y relevante para auriculares.</p>')
    html=html.replace('<p class="product-reference">Ref: 30458</p>',
        '<p class="product-reference">Ref: 30458</p><p>Ref: 99999</p>')
    result=extract_product_content(JBL,html)
    assert result['extraction_status']=='partial'
    assert result['product']['sku'] is None
    assert 'identity_conflict' in result['warnings']


def test_merchant_ref_in_trusted_adjacent_sibling_is_accepted():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>JBL Tune 527BT wireless headphones.</p>')
    html = html.replace(
        '<p class="product-reference">Ref: 30458</p>',
        '<p class="product-reference"><span>Ref:</span><span>30458</span></p>',
    )
    result = extract_product_content(JBL, html)
    assert result['product']['sku'] == '30458'
    assert result['extraction_status'] == 'ok'


def test_merchant_ref_value_in_unrelated_sibling_is_rejected():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>JBL Tune 527BT wireless headphones.</p>')
    html = html.replace(
        '<p class="product-reference">Ref: 30458</p>',
        '<p class="product-reference"><span>Ref:</span>'
        '<span class="related-products">99999</span></p>',
    )
    result = extract_product_content(JBL, html)
    assert result['product']['sku'] is None
    assert result['extraction_status'] == 'ok'


def test_guanxe_sku_requires_explicit_merchant_ref():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, '',
                '<p>Auriculares JBL Tune 527BT con sonido inalámbrico.</p>')
    html = html.replace('<p class="product-reference"></p>',
                        '<p>Article number 12345678</p>')
    result = extract_product_content(JBL, html)
    assert result['extraction_status'] == 'ok'
    assert result['product']['sku'] is None
    # The URL contains Guanxe's internal ID and an EAN suffix; neither is a SKU.
    assert '1019091-' in JBL and '1200130020902' in JBL


def test_guanxe_url_internal_id_and_ean_are_not_sku():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, '',
                '<p>Auriculares JBL Tune 527BT con sonido inalámbrico.</p>')
    result = extract_product_content(JBL, html)
    assert result['extraction_status'] == 'ok'
    assert result['product']['sku'] is None


def test_valid_ref_replaces_eight_digit_internal_url_id_without_conflict():
    url = JBL.replace('/1019091-', '/10190918-')
    html = page(url, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>Auriculares JBL Tune 527BT con sonido inalámbrico.</p>')
    result = extract_product_content(url, html)
    assert result['product']['sku'] == '30458'
    assert result['product']['url'] == url
    assert result['extraction_status'] == 'ok'
    assert 'identity_conflict' not in result['warnings']


def test_eight_digit_guanxe_url_id_preserves_associated_jsonld_fields():
    url = 'https://guanxe.com/es/inicio/12345678-jbl-tune-527bt.html'
    canonical = 'https://guanxe.com/es/auriculares/12345678-jbl-tune-527bt.html'
    html = page(
        url, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
        '<p>JBL Tune 527BT wireless headphones with Pure Bass sound.</p>',
        canonical=canonical,
    )
    html = html.replace('<meta property="product:price:amount" content="54.99">', '')
    html = html.replace('<meta property="product:price:currency" content="EUR">', '')
    assert '"sku"' not in html.split('</script>', 1)[0]

    result = extract_product_content(url, html)
    product = result['product']
    assert set(result) == {'mode', 'extraction_status', 'source_url', 'product', 'warnings'}
    assert result['extraction_status'] == 'ok'
    assert result['source_url'] == url
    assert product['sku'] == '30458'
    assert product['brand'] == 'JBL'
    assert product['image_url'] == 'https://cdn.guanxe.com/product.jpg'
    assert product['price'] == 54.99
    assert product['currency'] == 'EUR'
    assert product['url'] == canonical
    assert 'identity_conflict' not in result['warnings']


def test_jsonld_for_different_guanxe_product_is_not_associated():
    url = 'https://guanxe.com/es/inicio/12345678-jbl-tune-527bt.html'
    other_url = 'https://guanxe.com/es/auriculares/87654321-other-product.html'
    html = page(
        url, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
        '<p>JBL Tune 527BT wireless headphones.</p>',
        description='UNRELATED SAMSUNG DESCRIPTION ' + ('unrelated text. ' * 500),
    )
    html = html.replace('"url": "' + url + '"', '"url": "' + other_url + '"')
    html = html.replace('<meta property="product:price:amount" content="54.99">', '')
    html = html.replace('<meta property="product:price:currency" content="EUR">', '')

    result = extract_product_content(url, html)
    product = result['product']
    assert product['sku'] == '30458'
    assert product['brand'] is None
    assert product['image_url'] is None
    assert product['price'] is None
    assert product['currency'] is None
    assert result['extraction_status'] == 'partial'
    assert 'UNRELATED SAMSUNG DESCRIPTION' not in (product['description'] or '')


def test_related_product_ref_and_article_number_do_not_become_sku():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, '',
                '<p>Auriculares JBL Tune 527BT con sonido inalámbrico.</p>')
    html = html.replace(
        '</div></div>\n    <div id="description">',
        '<aside class="related-products"><p>Ref: 99999</p><p>Article number 12345678</p></aside>'
        '</div></div>\n    <div id="description">',
    )
    result = extract_product_content(JBL, html)
    assert result['product']['sku'] is None


def test_nested_related_description_specs_and_stock_are_excluded():
    html = page(
        JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
        '''<p>JBL Tune 527BT wireless headphones with Pure Bass sound.</p>
        <h3>Especificaciones Técnicas</h3>
        <table><tr><th>Bluetooth</th><td>5.3</td></tr></table>''',
    )
    html = html.replace(
        '<span>Advertencia: ¡Últimos artículos en inventario!</span>',
        '<span>Advertencia: ¡Últimos artículos en inventario!</span>'
        '<div class="related-products"><p>Ref: 99999</p><p>Article number 99999999</p>'
        '<p>En stock en tienda de producto recomendado</p></div>',
    )
    html = html.replace(
        '</body>',
        '''<aside class="related-products"><div id="description">
        <p>Recommended unrelated product description that must not replace JBL details.</p>
        <h3>Especificaciones Técnicas</h3>
        <table><tr><th>RAM</th><td>999 GB</td></tr></table>
        <p>Stock disponible para el producto recomendado</p>
        </div></aside></body>''',
    )
    result = extract_product_content(JBL, html)
    product = result['product']
    assert product['sku'] == '30458'
    assert 'wireless headphones' in product['description']
    assert 'Recommended unrelated' not in product['description']
    assert ('Bluetooth', '5.3') in {(x['label'], x['value']) for x in product['specifications']}
    assert ('RAM', '999 GB') not in {(x['label'], x['value']) for x in product['specifications']}
    assert all('recommended' not in item['text'].casefold() for item in product['availability_evidence'])


def test_trusted_ref_wins_over_generic_article_number_and_marks_conflict():
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>JBL Tune 527BT wireless headphones.</p>')
    html = html.replace(
        '<p class="product-reference">Ref: 30458</p>',
        '<p class="product-reference">Ref: 30458</p><p>Article number 12345678</p>',
    )
    result = extract_product_content(JBL, html)
    assert result['product']['sku'] == '30458'
    assert result['extraction_status'] == 'partial'
    assert 'identity_conflict' in result['warnings']


def test_only_real_product_pages_are_enriched():
    assert not is_guanxe_product('https://evilguanxe.com/es/inicio/123456-anything.html')
    assert not is_guanxe_product('http://guanxe.com/es/inicio/123456-anything.html')
    assert not is_guanxe_product('https://guanxe.com/es/search?q=JBL')
    assert is_guanxe_product(JBL)


def test_jsonld_canonical_category_change_keeps_brand_and_price():
    html=page(JBL,'JBL Tune 527BT','JBL',54.99,'Ref: 30458',
              '<p>JBL Pure Bass con batería de 57 horas.</p>',canonical=JBL_CANON)
    # Metadata Product.url is already the canonical category path, although the
    # fetched URL keeps the historic category path.
    html=html.replace('"url": "'+JBL+'"', '"url": "'+JBL_CANON+'"')
    result=extract_product_content(JBL,html)
    assert result['extraction_status']=='ok', result
    assert result['product']['brand']=='JBL'
    assert result['product']['price']==54.99
    assert result['product']['sku']=='30458'
    assert result['product']['url']==JBL_CANON
    assert 'identity_conflict' not in result['warnings']


def test_explicit_ref_without_jsonld_or_ikea_article_number_is_product():
    html=f'''<html><head><meta property="product:price:amount" content="54.99">
      <meta property="product:price:currency" content="EUR"></head><body><main>
      <div class="product-information"><h1>JBL Tune 527BT</h1><p>Ref: 30458</p>
      </div><div id="description"><p>Auriculares inalámbricos JBL Pure Bass.</p></div>
      </main></body></html>'''
    result=extract_product_content(JBL,html)
    assert result['extraction_status']=='ok', result
    assert result['product']['sku']=='30458'
    assert result['product']['price']==54.99


def test_canonical_wrong_id_is_not_trusted():
    other=JBL_CANON.replace('1019091-', '9999999-')
    html=page(JBL,'JBL Tune 527BT','JBL',54.99,'Ref: 30458',
              '<p>Auriculares JBL Tune 527BT.</p>',canonical=other)
    result=extract_product_content(JBL,html)
    assert result['product']['url']==JBL


@pytest.mark.parametrize('canonical', [
    'https://review-user@guanxe.com/es/auriculares/1019091-jbl-tune-527bt.html',
    'https://guanxe.com:8443/es/auriculares/1019091-jbl-tune-527bt.html',
    'https://evil.example/es/auriculares/1019091-jbl-tune-527bt.html',
    'http://guanxe.com/es/auriculares/1019091-jbl-tune-527bt.html',
])
def test_invalid_canonical_authority_falls_back_to_fetched_url(canonical):
    html = page(JBL, 'JBL Tune 527BT', 'JBL', 54.99, 'Ref: 30458',
                '<p>JBL headphones with wireless audio and a long battery life.</p>',
                canonical=canonical)
    result = extract_product_content(JBL, html)
    assert result['product']['url'] == JBL


def test_non_guanxe_generic_product_is_unchanged_by_enrichment():
    from guanxe_adapter import enrich_guanxe_product
    from bs4 import BeautifulSoup
    html='<html><body><h1>Chair</h1><p>Ref: 888</p></body></html>'
    original={'mode':'product','source_url':'https://shop.example/pd/chair-12345678',
              'extraction_status':'ok', 'product':{'sku':'12345678','variant':'Blue'},'warnings':[]}
    result=enrich_guanxe_product(original['source_url'],BeautifulSoup(html,'html.parser'),original)
    assert result is original and result['product']['sku']=='12345678'


def test_recommended_stock_does_not_fill_missing_summary_stock():
    html=page(JBL,'JBL Tune 527BT','JBL',54.99,'Ref: 30458',
              '<p>Auriculares JBL Pure Bass con batería duradera.</p>')
    html=html.replace('<p>Este producto está temporalmente agotado</p>', '')
    html=html.replace('<span>Advertencia: ¡Últimos artículos en inventario!</span>', '')
    result=extract_product_content(JBL,html)
    assert result['product']['availability_evidence']==[]


def test_policy_table_not_collected_as_product_specs():
    html=page(JBL,'JBL Tune 527BT','JBL',54.99,'Ref: 30458',
              '''<h3>Especificaciones Técnicas:</h3>
              <table><tr><td>Bluetooth</td><td>5.3</td></tr></table>
              <p>GUANXE ofrece a sus clientes la posibilidad de ejercer su derecho de devolución.</p>
              <table><tr><td>Plazo de devolución</td><td>14 días</td></tr></table>''')
    result=extract_product_content(JBL,html)
    values={(x['label'],x['value']) for x in result['product']['specifications']}
    assert ('Bluetooth','5.3') in values
    assert ('Plazo de devolución','14 días') not in values
