"""Offline snapshots of Guanxe's observed PDP structure, never production HTTP tests.

These reduced fixtures reflect observed Ref, product-description, feature and stock
labels; full live HTML must still be verified before approval.
"""
import json
from time import perf_counter

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


def test_non_guanxe_generic_product_is_unchanged_by_enrichment():
    from guanxe_adapter import enrich_guanxe_product
    from bs4 import BeautifulSoup
    html='<html><body><h1>Chair</h1><p>Ref: 888</p></body></html>'
    original={'mode':'product','source_url':'https://shop.example/pd/chair-12345678',
              'extraction_status':'ok', 'product':{'sku':'12345678','variant':'Blue'},'warnings':[]}
    result=enrich_guanxe_product(original['source_url'],BeautifulSoup(html,'html.parser'),original)
    assert result is original and result['product']['sku']=='12345678'