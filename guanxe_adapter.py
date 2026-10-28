"""Focused, bounded Guanxe PDP enrichment from already downloaded HTML.

No fetching, cross-product scanning, or alteration of generic price/brand/image logic.
"""

import re
import unicodedata
from urllib.parse import urlsplit, urljoin

from bs4 import Tag

MAX_SPECS = 200
MAX_SPEC_FIELD = 1000
MAX_DESC = 5000
_REF_RE = re.compile(r'^\s*Ref\s*:\s*([A-Za-z0-9][A-Za-z0-9._/-]{0,63})\s*$', re.I)
_EAN_RE = re.compile(r'^\s*EAN\s*:?\s*(\d{8,14})\s*$', re.I)
_PRODUCT_PATH = re.compile(r'^/es/[^/]+/(\d{3,10})-[^/]+\.html$', re.I)
_POLICY = re.compile(r'^(?:guanxe ofrece a sus clientes|en dicho plazo aceptamos devoluciones|cambios y devoluciones|derecho de devoluci[oó]n|pol[ií]tica de devoluciones|informaci[oó]n sobre env[ií]os|t[eé]rminos y condiciones|roturas o defectos de origen|para realizar una devoluci[oó]n)', re.I)
_MARKETING = re.compile(r'(?:disponible en canarias|env[ií]o r[aá]pido|garant[ií]a oficial|vendedor recomendado|marcas 100% originales|compra online miles de productos|entrega r[aá]pida)', re.I)
_OUT = re.compile(r'(?:temporalmente agotado|sin stock|agotado|no disponible|fuera de stock)', re.I)
_IN = re.compile(r'\b(?:en stock|stock disponible|disponible para compra|disponibilidad inmediata)\b', re.I)
_LIMITED = re.compile(r'(?:[uú]ltimos art[ií]culos en inventario|pocas unidades)', re.I)
_BAD_SECTION = re.compile(r'(?:related|recommend|upsell|crosssell|carrito|cart|footer|newsletter|devoluci[oó]n|returns|shipping|envios|preguntas|questions|accessori|opiniones|reviews|comparador)', re.I)
_SELECTORS = ('#description', '#tab-description', '#more_info_sheets', '#product-description',
              '.product-description', '.product-description-container',
              '[itemprop="description"]', '#product-details', '#tab-product-details',
              '.product-features', '.data-sheet')


def _clean(value, length=MAX_SPEC_FIELD):
    if not value:
        return None
    value = ' '.join(str(value).split())
    return value[:length] if value else None


def _norm(value):
    norm = unicodedata.normalize('NFKD', value.casefold())
    return ''.join(c for c in norm if not unicodedata.combining(c)).strip()


def is_guanxe_product(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and (parts.hostname or '').lower() in
                {'guanxe.com', 'www.guanxe.com'} and bool(_PRODUCT_PATH.fullmatch(parts.path)))
    except (ValueError, TypeError):
        return False


def _section_is_unrelated(node, stop=None):
    cur = node
    while isinstance(cur, Tag):
        label = ' '.join([str(cur.get('id', '')), ' '.join(cur.get('class', []))])
        if _BAD_SECTION.search(label):
            return True
        if cur is stop:
            break
        cur = cur.parent
    return False


def _summary(heading):
    """Use only the explicit product-detail region around the selected H1."""
    if not isinstance(heading, Tag):
        return None
    for parent in heading.parents:
        if not isinstance(parent, Tag) or parent.name in {'body', 'html'}:
            break
        label = ' '.join([str(parent.get('id', '')), ' '.join(parent.get('class', []))]).lower()
        if re.search(r'product[-_ ]?(?:information|info|detail|container|summary)|product-page|^product$', label):
            return parent
    # Only nearby siblings of H1; never scan unrelated recommendations or the entire body.
    parent = heading.parent
    return parent if isinstance(parent, Tag) else None


def _visible_ref(summary, heading):
    if not summary:
        return None, False
    # In a full product container, only inspect the header region up to the description tabs.
    refs = set()
    for node in summary.find_all(['span', 'div', 'p', 'small', 'strong', 'label', 'li'], limit=400):
        if _section_is_unrelated(node, summary):
            continue
        if node.find(['span', 'div', 'p', 'small', 'strong', 'label', 'li']):
            continue
        text = node.get_text(' ', strip=True)
        if not text or len(text) > 90:
            continue
        m = _REF_RE.fullmatch(text)
        if m:
            refs.add(m.group(1))
        elif _norm(text).strip(': ') == 'ref':
            sibling = node.find_next_sibling()
            if sibling and len(sibling.get_text(' ', strip=True)) <= 70:
                value = sibling.get_text(' ', strip=True)
                if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,63}', value):
                    refs.add(value)
    if len(refs) == 1:
        return next(iter(refs)), False
    return None, len(refs) > 1


def trusted_reference(soup):
    heading = soup.find('h1')
    if heading is None:
        return None
    return _visible_ref(_summary(heading), heading)[0]


def _relevant_sections(soup):
    sections = []
    for selector in _SELECTORS:
        for node in soup.select(selector)[:8]:
            if _section_is_unrelated(node, node):
                continue
            if any(parent is node for selected in sections for parent in selected.parents):
                # Existing section is nested inside the new parent; retain the smaller one.
                continue
            if any(node is selected or node in selected.parents for selected in sections):
                continue
            sections.append(node)
    return sections[:12]


def same_guanxe_identity(url, record_url):
    """Allow an explicitly identified PDP across Guanxe category-path changes."""
    if not record_url:
        return False
    try:
        left, right = urlsplit(url), urlsplit(urljoin(url, record_url))
        a, b = _PRODUCT_PATH.fullmatch(left.path), _PRODUCT_PATH.fullmatch(right.path)
        return bool(a and b and a.group(1) == b.group(1)
                    and left.hostname == right.hostname
                    and right.scheme in {'http', 'https'}
                    and right.username is None and right.password is None
                    and right.port in (None, 80, 443)
                    and not right.query and not right.fragment)
    except (ValueError, TypeError):
        return False


def _canonical(soup, url):
    source = urlsplit(url)
    for node in soup.select('link[rel="canonical"][href]')[:2]:
        candidate = urljoin(url, node.get('href', ''))
        try:
            target = urlsplit(candidate)
            src_match = _PRODUCT_PATH.fullmatch(source.path)
            target_match = _PRODUCT_PATH.fullmatch(target.path)
            if (target.scheme == 'https' and target.hostname == source.hostname
                    and not target.query and not target.fragment and src_match and target_match
                    and target_match.group(1) == src_match.group(1)):
                return candidate
        except (ValueError, TypeError):
            pass
    return url


def _collect_specifications(sections):
    result, seen = [], set()
    truncated = False
    def add(group, label, value):
        nonlocal truncated
        label, value = _clean(label), _clean(value)
        if not label or not value or len(label) > 85 or label.lower() in {'especificación','detalle'}:
            return
        if _POLICY.search(label) or _MARKETING.search(label):
            return
        key = (_norm(label).rstrip(':'), _norm(value))
        if key in seen:
            return
        if len(result) >= MAX_SPECS:
            truncated = True
            return
        seen.add(key)
        result.append({'group':_clean(group) or 'Technical specifications', 'label':label.rstrip(':'), 'value':value})
    for section in sections:
        if _section_is_unrelated(section, section):
            continue
        title = section.get('id', '')
        default_group = 'Ficha técnica' if ('detail' in title.lower() or 'feature' in title.lower()) else 'Technical specifications'
        for table in section.find_all('table', limit=35):
            if _section_is_unrelated(table, section):
                continue
            previous = table.find_previous(['h2','h3','h4'])
            group = _clean(previous.get_text(' ', strip=True)) if previous and previous in section.descendants else default_group
            for row in table.find_all('tr', limit=MAX_SPECS+1):
                cells = row.find_all(['th','td'], recursive=False)
                if len(cells) != 2:
                    continue
                add(group, cells[0].get_text(' ', strip=True), cells[1].get_text(' ', strip=True))
        for dl in section.find_all('dl', limit=40):
            if _section_is_unrelated(dl, section):
                continue
            for dt in dl.find_all('dt', recursive=False):
                dd = dt.find_next_sibling('dd')
                if dd:
                    add(default_group, dt.get_text(' ', strip=True), dd.get_text(' ', strip=True))
        # Product-specific nested/listed technical fields: avoid prose-global regular expressions.
        for list_node in section.find_all(['ul','ol'], limit=60):
            if _section_is_unrelated(list_node, section):
                continue
            heading = list_node.find_previous(['h2','h3','h4','h5'])
            section_heading = _clean(heading.get_text(' ', strip=True)) if heading and heading in section.descendants else None
            # Do not parse unrelated bullets like 'Sonido JBL Pure Bass' as specs.
            technical = bool(section_heading and re.search(r'especificaciones|caracter[ií]sticas|ficha t[eé]cnica', section_heading, re.I))
            if not technical:
                # Nested feature lists have their own group label parent, and are also explicitly under product description.
                technical = bool(list_node.find_parent('li') and list_node.find_parent('li') in section.descendants)
            if not technical:
                continue
            parent_li = list_node.find_parent('li')
            group = default_group
            if parent_li and parent_li in section.descendants:
                prefix = [str(child) if isinstance(child, str) else child.get_text(' ', strip=True)
                          for child in parent_li.contents
                          if not (isinstance(child, Tag) and child.name in {'ul', 'ol'})]
                group = (_clean(' '.join(prefix)) or default_group).rstrip(':')
            elif section_heading:
                group = section_heading.rstrip(':')
            for li in list_node.find_all('li', recursive=False, limit=MAX_SPECS+1):
                # Category list items can contain nested lists; individual leaf entries only.
                if li.find(['ul','ol']):
                    continue
                text = _clean(li.get_text(' ', strip=True))
                if text and ':' in text:
                    label,value=text.split(':',1)
                    add(group,label,value)
        # PrestaShop feature rows: div with 'name' and 'value' immediate children.
        for row in section.select('.data-sheet tr, .product-features .feature, .product-features > dl')[:MAX_SPECS]:
            if row.name == 'tr':
                continue
            label_node = row.select_one('.name, dt')
            value_node = row.select_one('.value, dd')
            if label_node and value_node:
                add('Ficha técnica', label_node.get_text(' ', strip=True), value_node.get_text(' ', strip=True))
        # A product description can explicitly show EAN independently of a SKU/Ref.
        for leaf in section.find_all(['p', 'span'], limit=200):
            if not leaf.find(True):
                match = _EAN_RE.fullmatch(leaf.get_text(' ', strip=True))
                if match:
                    add('Identifiers', 'EAN', match.group(1))
    return result, truncated


def _clean_description(sections):
    for section in sections:
        if _section_is_unrelated(section, section):
            continue
        label = _norm(str(section.get('id', ''))+' '+' '.join(section.get('class', [])))
        if 'detail' in label or 'feature' in label or 'ficha' in label:
            continue
        paragraphs = []
        count = 0
        for node in section.find_all(['h2','h3','h4','p'], limit=180):
            if node.find(['p','h2','h3','h4']) or node.find_parent(['table','li']):
                continue
            if _section_is_unrelated(node, section):
                continue
            text = _clean(node.get_text(' ', strip=True), MAX_DESC+1)
            if not text:
                continue
            if _POLICY.search(text):
                break
            if _MARKETING.search(text):
                continue
            if re.match(r'^(?:caracter[ií]sticas|especificaciones|ficha t[eé]cnica)\s*:', text, re.I):
                break
            # A lone EAN label belongs in the specifications, not narrative.
            if _EAN_RE.fullmatch(text):
                continue
            if len(text) < 18 and node.name == 'p':
                continue
            paragraphs.append(text)
            count += len(text) + 1
            if count >= MAX_DESC:
                break
        if paragraphs:
            description = ' '.join(paragraphs)
            # Cut on a complete sentence or whole paragraph rather than boilerplate/truncation.
            if len(description) > MAX_DESC:
                final = []
                for item in paragraphs:
                    if len(' '.join(final+[item])) > MAX_DESC:
                        break
                    final.append(item)
                description = ' '.join(final) or paragraphs[0][:MAX_DESC]
            if description:
                return description
    return None


def _availability(summary, heading):
    if not summary:
        return []
    evidence, seen = [], set()
    # Only short visible leaf nodes from product summary, excluding recommendations.
    for node in summary.find_all(['p','span','div','small','strong','li'], limit=600):
        if node.find(['p','span','div','small','strong','li']) or _section_is_unrelated(node, summary):
            continue
        text = _clean(node.get_text(' ', strip=True))
        if not text or len(text)>180 or _MARKETING.search(text):
            continue
        if not (_OUT.search(text) or _LIMITED.search(text) or _IN.search(text)):
            continue
        if text not in seen:
            seen.add(text)
            evidence.append({'type':'visible_stock_text','text':text,'source':'product-page text'})
            if len(evidence)==20:
                break
    return evidence


def enrich_guanxe_product(url, soup, response):
    """Add only Guanxe-specific evidence; preserve generic name/brand/prices/image.

    `response` is the original product-mode five-key record, already validated by
    the generic identity and pricing logic. No new requests are made.
    """
    if not is_guanxe_product(url) or not isinstance(response.get('product'), dict):
        return response
    heading = soup.find('h1')
    if heading is None:
        return response
    summary = _summary(heading)
    reference, ambiguous = _visible_ref(summary, heading)
    product = response['product']
    warnings = response['warnings']
    if ambiguous:
        if 'identity_conflict' not in warnings:
            warnings.append('identity_conflict')
        response['extraction_status'] = 'partial'
    if reference:
        # Product reference outranks an inferred URL ID or EAN; never overwrite a
        # conflicting explicit structured sku without reporting identity ambiguity.
        existing_sku = product.get('sku')
        if existing_sku and existing_sku != reference:
            if 'identity_conflict' not in warnings:
                warnings.append('identity_conflict')
            response['extraction_status'] = 'partial'
        else:
            product['sku'] = reference
    product['url'] = _canonical(soup, url)
    sections = _relevant_sections(soup)
    specs, truncated = _collect_specifications(sections)
    if specs:
        product['specifications'] = specs
    if truncated and 'specifications_truncated' not in warnings:
        warnings.append('specifications_truncated')
    description = _clean_description(sections)
    if description:
        product['description'] = description
        # Generic 'field_truncated' can have come from unbounded marketing metadata.
        # Retain this warning when an unrelated field is still oversized/ambiguous;
        # remove only when original product name is within its limit.
        if 'field_truncated' in warnings and len(heading.get_text(' ',strip=True))<=1000:
            warnings.remove('field_truncated')
    product['variant'] = None  # Never infer chosen variants from title or generic size/color.
    availability = _availability(summary, heading)
    if availability:
        product['availability_evidence'] = availability
        positive = any(_IN.search(x['text']) for x in availability)
        negative = any(_OUT.search(x['text']) for x in availability)
        if positive and negative and 'availability_conflict' not in warnings:
            warnings.append('availability_conflict')
    return response