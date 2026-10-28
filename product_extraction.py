"""Bounded, deterministic extraction of one product detail page."""

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from guanxe_adapter import is_guanxe_product, same_guanxe_identity, trusted_reference, enrich_guanxe_product


MAX_PRODUCT_NAME_LENGTH = 1000
MAX_PRODUCT_DESCRIPTION_LENGTH = 5000
MAX_PRODUCT_FIELD_LENGTH = 1000
MAX_PRODUCT_URL_LENGTH = 2048
MAX_PRODUCT_SPECIFICATIONS = 200
MAX_PRODUCT_WARNINGS = 16
MAX_PRODUCT_TABLES = 300
MAX_PRODUCT_HEADINGS = 1000
MAX_AVAILABILITY_PARAGRAPHS = 1000
MAX_SCANNED_SCRIPTS = 2000
MAX_JSONLD_SCRIPTS = 20
MAX_JSONLD_SCRIPT_CHARS = 50_000
MAX_EMBEDDED_SCRIPT_CHARS = 100_000
MAX_RECORDS_PER_KIND = 100

_CURRENCY_CODES = {
    "AUD", "CAD", "CHF", "CNY", "DKK", "EUR", "GBP", "HKD", "INR", "JPY",
    "KRW", "MXN", "NOK", "NZD", "PLN", "SEK", "SGD", "THB", "TRY", "TWD",
    "USD", "ZAR",
}
_CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD", "¥": "JPY"}
_OLD_PRICE_MARKERS = ("oldprice", "originalprice", "previousprice", "regularprice", "wasprice")
_CURRENCY_RE = re.compile(r"\b[A-Z]{3}\b")
_PRICE_RE = re.compile(r"(?<![\w])\d[\d\s.,]*\d|(?<![\w])\d(?![\w])")
_ARTICLE_NUMBER_RE = re.compile(r"(?<!\d)(\d{3}\.\d{3}\.\d{2}|\d{8})(?!\d)")
_URL_ARTICLE_RE = re.compile(r"(?:art[-_])?(\d{8})(?:$|[-_/])", re.I)
_ITEM_GTAGS_RE = re.compile(r"\bvar\s+itemGtag\s*=\s*")
_AVAILABILITY_TERMS = (
    "stock disponible", "disponible en", "available in", "in stock", "currently unavailable",
    "agotado", "sin stock", "disponibilidad",
)
_UNRELATED_SECTION_MARKERS = (
    "recommend", "relatedproduct", "relateditem", "upsell", "crosssell", "productcarousel",
    "productlist", "suggestion", "recentlyviewed", "complementaryproduct", "youlike",
)
_SPEC_SECTION_MARKERS = (
    "medida", "dimension", "specification", "articleinformation", "productdetails",
    "caracteristica", "informaciondelarticulo", "informaciondelproducto", "composicion",
    "materiales", "technicaldetails", "productattributes", "features",
)


def _clean(value, limit=None):
    if value is None or isinstance(value, bool):
        return None
    value = " ".join(str(value).split())
    if not value:
        return None
    return value[:limit] if limit else value


def _would_truncate(value, limit):
    if value is None:
        return False
    return len(" ".join(str(value).split())) > limit


def _normalize_article_number(value):
    value = _clean(value)
    if not value:
        return None
    if re.fullmatch(r"\d{3}\.\d{3}\.\d{2}", value):
        return value.replace(".", "")
    if re.fullmatch(r"\d{8}", value):
        return value
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) else None


def _url_sku(url):
    try:
        path = urlsplit(url).path
    except (TypeError, ValueError):
        return None
    match = _URL_ARTICLE_RE.search(path.rsplit("/", 1)[-1])
    return match.group(1) if match else None


def _resolved_http_url(base_url, value):
    value = _clean(value)
    if not value:
        return None
    try:
        resolved = urljoin(base_url, value)
        parts = urlsplit(resolved)
        hostname = parts.hostname
    except (TypeError, ValueError):
        return None
    if parts.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if len(resolved) > MAX_PRODUCT_URL_LENGTH:
        return None
    return resolved


def _same_page_url(first, second, *, allow_http_scheme_match=False):
    try:
        left = urlsplit(first)
        right = urlsplit(second)
        left_host = left.hostname
        right_host = right.hostname
        left_port = left.port
        right_port = right.port
    except (TypeError, ValueError):
        return False
    if left.scheme != right.scheme:
        if not (
            allow_http_scheme_match
            and {left.scheme.lower(), right.scheme.lower()} == {"http", "https"}
            and left_port is None
            and right_port is None
        ):
            return False
    elif (left_port or (443 if left.scheme == "https" else 80)) != (
        right_port or (443 if right.scheme == "https" else 80)
    ):
        return False
    return (
        left_host and right_host
        and left_host.casefold() == right_host.casefold()
        and left.path.rstrip("/") == right.path.rstrip("/")
    )


def _currency(value):
    value = _clean(value)
    if not value:
        return None
    if "C$" in value:
        return "CAD"
    if "A$" in value:
        return "AUD"
    if "US$" in value:
        return "USD"
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in value:
            return code
    for match in _CURRENCY_RE.finditer(value.upper()):
        if match.group(0) in _CURRENCY_CODES:
            return match.group(0)
    return None


def _number(value):
    value = _clean(value)
    if not value:
        return None
    match = _PRICE_RE.search(value.replace("\u00a0", " "))
    if not match:
        return None
    numeric = re.sub(r"\s", "", match.group(0))
    comma, dot = numeric.rfind(","), numeric.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_mark = "," if comma > dot else "."
        grouping_mark = "." if decimal_mark == "," else ","
        numeric = numeric.replace(grouping_mark, "").replace(decimal_mark, ".")
    elif comma >= 0:
        trailing = len(numeric) - comma - 1
        numeric = numeric.replace(",", ".") if trailing in {1, 2} else numeric.replace(",", "")
    elif dot >= 0 and len(numeric) - dot - 1 not in {1, 2}:
        numeric = numeric.replace(".", "")
    try:
        parsed = Decimal(numeric)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("1000000000"):
        return None
    return parsed


def _json_number(value):
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _jsonld_products(soup):
    records = []
    invalid = False
    truncated = False
    scripts = soup.find_all(
        "script", attrs={"type": re.compile(r"^application/ld\+json(?:\s*;|$)", re.I)},
        limit=MAX_JSONLD_SCRIPTS + 1,
    )
    if len(scripts) > MAX_JSONLD_SCRIPTS:
        scripts = scripts[:MAX_JSONLD_SCRIPTS]
        truncated = True
    for script in scripts:
        raw = str(script.string or script.get_text())
        if len(raw) > MAX_JSONLD_SCRIPT_CHARS:
            truncated = True
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            invalid = True
            continue
        stack = [(parsed, 0)]
        visited = 0
        while stack:
            value, depth = stack.pop()
            visited += 1
            if visited > 20_000 or depth > 64:
                truncated = True
                break
            if isinstance(value, dict):
                record_type = value.get("@type", [])
                if isinstance(record_type, str):
                    record_type = [record_type]
                if any(str(item).casefold() == "product" for item in record_type):
                    records.append(value)
                    if len(records) >= MAX_RECORDS_PER_KIND:
                        truncated = True
                        break
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        stack.append((child, depth + 1))
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, (dict, list)):
                        stack.append((child, depth + 1))
    return records, invalid, truncated


def _item_gtag_records(soup):
    records = []
    truncated = False
    scripts = soup.find_all("script", limit=MAX_SCANNED_SCRIPTS + 1)
    if len(scripts) > MAX_SCANNED_SCRIPTS:
        scripts = scripts[:MAX_SCANNED_SCRIPTS]
        truncated = True
    for script in scripts:
        text = str(script.string or script.get_text())
        if not text or len(text) > MAX_EMBEDDED_SCRIPT_CHARS:
            if text:
                truncated = True
            continue
        for match in _ITEM_GTAGS_RE.finditer(text):
            try:
                record, _ = json.JSONDecoder().raw_decode(text, match.end())
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict) and _normalize_article_number(record.get("item_id")):
                records.append(record)
                if len(records) >= MAX_RECORDS_PER_KIND:
                    return records, True
    return records, truncated


def _article_numbers(soup):
    found = []
    for node in soup.find_all(string=re.compile(r"(?:n[uú]mero\s+de\s+art[ií]culo|article\s+(?:number|no\.?))", re.I)):
        parent = node.parent if isinstance(node.parent, Tag) else None
        text = " ".join(parent.stripped_strings) if parent else str(node)
        if not re.search(r"(?:n[uú]mero\s+de\s+art[ií]culo|article\s+(?:number|no\.?))", text, re.I):
            continue
        match = _ARTICLE_NUMBER_RE.search(text)
        if match:
            found.append(_normalize_article_number(match.group(1)))
    return [value for value in found if value]


def _find_main_context(soup):
    heading = soup.find("h1")
    if not heading:
        return None, soup.body or soup
    for parent in heading.parents:
        if not isinstance(parent, Tag):
            continue
        label = " ".join([str(parent.get("id", "")), *parent.get("class", [])]).casefold()
        if parent.name == "article" or any(
            token in label for token in (
                "product-detail", "productdetail", "product_page", "productpage", "product-info",
                "productinfo", "product-content", "productcontent", "pdp", "pip",
            )
        ):
            return heading, parent
    return heading, heading.find_parent("main") or soup.body or soup


def _meta_values(soup, property_name):
    values = []
    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or "").strip().casefold()
        if key == property_name.casefold():
            value = _clean(meta.get("content"))
            if value:
                values.append(value)
    return values


def _normalized_text(value):
    text = unicodedata.normalize("NFKD", str(value).casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def _is_old_price(node, stop=None):
    current = node
    while isinstance(current, Tag):
        if current.name in {"del", "s", "strike"}:
            return True
        classes = current.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        labels = [*classes, str(current.get("id", "")), str(current.get("aria-label", ""))]
        normalized = [re.sub(r"[^a-z0-9]", "", item.casefold()) for item in labels]
        if any(
            value in {"old", "original", "previous", "was"}
            or any(marker in value for marker in _OLD_PRICE_MARKERS)
            for value in normalized
        ):
            return True
        if current is stop:
            break
        current = current.parent
    return False


def _in_unrelated_product_section(node, stop=None):
    current = node
    while isinstance(current, Tag):
        classes = current.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        labels = [str(current.get("id", "")), *classes]
        normalized = " ".join(re.sub(r"[^a-z0-9]", "", item.casefold()) for item in labels)
        if any(marker in normalized for marker in _UNRELATED_SECTION_MARKERS):
            return True
        if current is stop:
            break
        current = current.parent
    return False


def _price_evidence(context):
    current = []
    original = []
    for node in context.find_all(True):
        if _in_unrelated_product_section(node, context):
            continue
        classes = node.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        labels = [*classes, str(node.get("id", "")), str(node.get("itemprop", ""))]
        normalized = [re.sub(r"[^a-z0-9]", "", item.casefold()) for item in labels]
        has_price_child = False
        for child in node.find_all(True):
            child_classes = child.get("class", [])
            if isinstance(child_classes, str):
                child_classes = child_classes.split()
            child_labels = [*child_classes, str(child.get("id", "")), str(child.get("itemprop", ""))]
            if any("price" in re.sub(r"[^a-z0-9]", "", item.casefold()) for item in child_labels):
                has_price_child = True
                break
        if has_price_child:
            continue
        raw = node.get("content") or node.get("data-price")
        if raw is None and node.name not in {"meta", "script", "style"}:
            raw = node.get_text(" ", strip=True)
        amount = _number(raw)
        if amount is None:
            continue
        if _is_old_price(node):
            original.append(amount)
        elif "price" in " ".join(normalized) or node.get("itemprop") == "price":
            current.append(amount)
    return current, original


def _distinct(values):
    output = []
    for value in values:
        if value is not None and value not in output:
            output.append(value)
    return output


def _record_sku(record):
    for key in ("sku", "productID", "mpn", "item_id"):
        value = _normalize_article_number(record.get(key))
        if value:
            return value
    return None


def _record_url(record):
    value = record.get("url") or record.get("@id")
    return _clean(value)


def _brand(record):
    value = record.get("brand") if isinstance(record, dict) else None
    if isinstance(value, dict):
        value = value.get("name")
    elif isinstance(value, list):
        value = value[0].get("name") if value and isinstance(value[0], dict) else (value[0] if value else None)
    return _clean(value, MAX_PRODUCT_FIELD_LENGTH)


def _record_image(record, final_url):
    value = record.get("image") if isinstance(record, dict) else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    elif isinstance(value, list):
        value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get("url") or value.get("contentUrl")
    return _resolved_http_url(final_url, value)


def _record_price(record):
    offers = record.get("offers") if isinstance(record, dict) else None
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None, None
    return _number(offers.get("price") or offers.get("lowPrice")), _currency(offers.get("priceCurrency"))


def _record_signature(record, *, jsonld=False):
    if jsonld:
        price, currency = _record_price(record)
        values = (_record_sku(record), record.get("name"), _record_url(record), price, currency, _brand(record), record.get("image"))
    else:
        values = (
            _normalize_article_number(record.get("item_id")), record.get("item_name"), record.get("price"),
            record.get("item_variant"),
        )
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _records_conflict(records, *, jsonld=False):
    return len({_record_signature(record, jsonld=jsonld) for record in records}) > 1


def _specifications(context, document):
    specifications = []
    document_tables = document.find_all("table", limit=MAX_PRODUCT_TABLES + 1)
    truncated = len(document_tables) > MAX_PRODUCT_TABLES
    document_tables = document_tables[:MAX_PRODUCT_TABLES]
    heading_elements = document.find_all(["h2", "h3", "h4", "h5"], limit=MAX_PRODUCT_HEADINGS + 1)
    if len(heading_elements) > MAX_PRODUCT_HEADINGS:
        truncated = True
        heading_elements = heading_elements[:MAX_PRODUCT_HEADINGS]
    heading_set = set(heading_elements)
    current_heading = None
    heading_by_table = {}
    for event in document.find_all(["h2", "h3", "h4", "h5", "table"], limit=MAX_PRODUCT_HEADINGS + MAX_PRODUCT_TABLES + 1):
        if event.name == "table":
            heading_by_table[event] = current_heading
        elif event in heading_set:
            current_heading = event
    context_tables = set(context.find_all("table", limit=MAX_PRODUCT_TABLES))
    tables = []
    for table in document_tables:
        if _in_unrelated_product_section(table):
            continue
        if table in context_tables:
            tables.append(table)
            continue
        prior_heading = heading_by_table.get(table)
        heading_text = _normalized_text(prior_heading.get_text(" ", strip=True)) if prior_heading else ""
        labels = [re.sub(r"[^a-z0-9]+", "", _normalized_text(cell.get_text(" ", strip=True))) for cell in table.find_all(["th", "td"])]
        package_table = "paquetes" in labels
        section_labels = [
            str(parent.get("id", ""))
            + " "
            + " ".join(parent.get("class", [])) if isinstance(parent.get("class", []), list)
            else str(parent.get("id", "")) + " " + str(parent.get("class", ""))
            for parent in table.parents if isinstance(parent, Tag)
        ]
        explicit_container = any(
            marker in re.sub(r"[^a-z0-9]", "", label.casefold())
            for label in section_labels for marker in _SPEC_SECTION_MARKERS
        )
        spec_section = any(
            marker in re.sub(r"[^a-z0-9]", "", heading_text.casefold())
            for marker in _SPEC_SECTION_MARKERS
        )
        nonempty_rows = 0
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2 and _clean(cells[0].get_text(" ", strip=True)) and _clean(cells[1].get_text(" ", strip=True)):
                nonempty_rows += 1
        # Outside the selected product wrapper, only explicitly identified
        # specification sections or package tables are trusted.
        if package_table or explicit_container or (spec_section and nonempty_rows >= 2):
            tables.append(table)
    for table in tables:
        group = None
        prior_heading = heading_by_table.get(table)
        if prior_heading:
            candidate = _clean(prior_heading.get_text(" ", strip=True), MAX_PRODUCT_FIELD_LENGTH)
            if candidate:
                group = candidate
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = _clean(cells[0].get_text(" ", strip=True), MAX_PRODUCT_FIELD_LENGTH)
            value = _clean(cells[1].get_text(" ", strip=True), MAX_PRODUCT_FIELD_LENGTH)
            if _would_truncate(cells[0].get_text(" ", strip=True), MAX_PRODUCT_FIELD_LENGTH) or _would_truncate(
                cells[1].get_text(" ", strip=True), MAX_PRODUCT_FIELD_LENGTH
            ):
                truncated = True
            normalized_label = _normalized_text(label or "")
            normalized_label = re.sub(r"[^a-z0-9]+", " ", normalized_label).strip()
            if not label or not value:
                continue
            if len(specifications) >= MAX_PRODUCT_SPECIFICATIONS:
                truncated = True
                return specifications, truncated
            specifications.append({"group": group, "label": label, "value": value})
    return specifications, truncated


def _availability_evidence(document):
    evidence = []
    truncated = False
    paragraphs = document.find_all("p", limit=MAX_AVAILABILITY_PARAGRAPHS + 1)
    if len(paragraphs) > MAX_AVAILABILITY_PARAGRAPHS:
        paragraphs = paragraphs[:MAX_AVAILABILITY_PARAGRAPHS]
        truncated = True
    for node in paragraphs:
        if _in_unrelated_product_section(node):
            continue
        raw_text = node.get_text(" ", strip=True)
        text = _clean(raw_text, MAX_PRODUCT_FIELD_LENGTH)
        if not text:
            continue
        normalized = _normalized_text(text)
        if any(term in normalized for term in _AVAILABILITY_TERMS):
            # Skip containers that merely repeat descendant paragraphs/items.
            if node.find(["p", "li"]):
                continue
            if not any(item["text"] == text for item in evidence):
                evidence.append({"type": "visible_stock_text", "text": text, "source": "product-page text"})
            if len(evidence) >= 20:
                truncated = True
                break
            if _would_truncate(raw_text, MAX_PRODUCT_FIELD_LENGTH):
                truncated = True
    return evidence, truncated


def _heading_variant(heading):
    if not heading:
        return None
    parts = [part.strip() for part in heading.split(",") if part.strip()]
    if len(parts) >= 3:
        return _clean(", ".join(parts[-2:]), MAX_PRODUCT_FIELD_LENGTH)
    return None


def _empty_product():
    return {
        "name": None,
        "sku": None,
        "brand": None,
        "price": None,
        "original_price": None,
        "currency": None,
        "variant": None,
        "url": None,
        "image_url": None,
        "description": None,
        "specifications": [],
        "availability_evidence": [],
    }


def _response(final_url, status, product=None, warnings=None):
    return {
        "mode": "product",
        "source_url": final_url,
        "extraction_status": status,
        "product": product,
        "warnings": list(dict.fromkeys((warnings or [])[:MAX_PRODUCT_WARNINGS])),
    }


def extract_product_content(final_url, html, content_status="ok"):
    """Extract one product from already-fetched HTML; never performs network I/O."""
    if content_status in {"blocked", "empty"} or not isinstance(html, str) or not html.strip():
        return _response(final_url, "unusable")

    soup = BeautifulSoup(html, "html.parser")
    heading, context = _find_main_context(soup)
    raw_h1_name = heading.get_text(" ", strip=True) if heading else None
    h1_name = _clean(raw_h1_name, MAX_PRODUCT_NAME_LENGTH)
    url_sku = _url_sku(final_url)
    visible_skus = _article_numbers(context)
    visible_skus = _distinct(visible_skus)

    gtag_records, gtag_truncated = _item_gtag_records(soup)
    jsonld_records, jsonld_invalid, jsonld_truncated = _jsonld_products(soup)
    warning_codes = []
    if _would_truncate(raw_h1_name, MAX_PRODUCT_NAME_LENGTH):
        warning_codes.append("field_truncated")
    if jsonld_invalid:
        warning_codes.append("structured_data_invalid")
    if jsonld_truncated or gtag_truncated:
        warning_codes.append("structured_data_truncated")

    if url_sku and visible_skus and url_sku not in visible_skus:
        # Other displayed article numbers can belong to related items; retain the URL
        # identity only when the page also contains an explicit matching item record.
        explicit_match = any(_normalize_article_number(record.get("item_id")) == url_sku for record in gtag_records)
        if not explicit_match:
            warning_codes.append("identity_conflict")
    if url_sku:
        target_sku = url_sku
    elif len(visible_skus) == 1:
        target_sku = visible_skus[0]
    else:
        target_sku = None

    matching_gtags = [record for record in gtag_records if target_sku and _normalize_article_number(record.get("item_id")) == target_sku]
    matching_jsonld = [
        record for record in jsonld_records
        if target_sku and _record_sku(record) == target_sku
    ]
    if not matching_jsonld and not target_sku:
        matching_jsonld = [record for record in jsonld_records if (
            _same_page_url(_record_url(record), final_url)
            or (is_guanxe_product(final_url) and same_guanxe_identity(final_url, _record_url(record)))
        )]

    def associated(record):
        record_url = _record_url(record)
        resolved_record_url = _resolved_http_url(final_url, record_url) if record_url else None
        return not record_url or (
            resolved_record_url
            and (
                _same_page_url(resolved_record_url, final_url, allow_http_scheme_match=True)
                or (is_guanxe_product(final_url) and same_guanxe_identity(final_url, resolved_record_url))
            )
        )

    if any(not associated(record) for record in matching_jsonld):
        warning_codes.append("identity_conflict")
        matching_jsonld = []
    if any(not associated(record) for record in matching_gtags):
        warning_codes.append("identity_conflict")
        matching_gtags = []
    if _records_conflict(matching_gtags):
        warning_codes.append("identity_conflict")
        matching_gtags = []
    if _records_conflict(matching_jsonld, jsonld=True):
        warning_codes.append("identity_conflict")
        matching_jsonld = []

    if target_sku and visible_skus and target_sku not in visible_skus and not matching_gtags and not matching_jsonld:
        # URL-only SKU is still usable when this is clearly a PDP and an H1 is present.
        if not h1_name or not re.search(r"/(?:pd|product|p)/", urlsplit(final_url).path, re.I):
            warning_codes.append("identity_conflict")
            target_sku = None

    gtag = matching_gtags[0] if matching_gtags else None
    record = matching_jsonld[0] if matching_jsonld else None
    candidate_names = [
        _clean(item.get("name") or item.get("item_name"), MAX_PRODUCT_NAME_LENGTH)
        for item in [*matching_jsonld, *matching_gtags]
    ]
    candidate_names = _distinct(candidate_names)
    name = h1_name or (candidate_names[0] if candidate_names else None)
    if len(candidate_names) > 1 and len({value.casefold() for value in candidate_names}) > 1:
        warning_codes.append("identity_conflict")

    has_pdp_path = bool(re.search(r"/(?:pd|product|p)/", urlsplit(final_url).path, re.I))
    guanxe_ref = trusted_reference(soup) if is_guanxe_product(final_url) else None
    identity_evidence = bool(
        matching_gtags or matching_jsonld or visible_skus
        or (target_sku and has_pdp_path) or guanxe_ref
    )
    if not name or not identity_evidence:
        return _response(final_url, "no_product", warnings=warning_codes)

    product = _empty_product()
    product["name"] = name
    product["sku"] = target_sku
    product["url"] = final_url
    embedded_variant = _clean(
        (gtag or {}).get("item_variant") or (record or {}).get("color") or (record or {}).get("size")
        or (record or {}).get("variant"), MAX_PRODUCT_FIELD_LENGTH
    )
    product["variant"] = _heading_variant(name) or _clean(unquote(embedded_variant or ""), MAX_PRODUCT_FIELD_LENGTH)
    product["brand"] = _brand(record)
    product["image_url"] = _record_image(record, final_url)
    if not product["image_url"]:
        images = _meta_values(soup, "og:image")
        if len(images) == 1:
            product["image_url"] = _resolved_http_url(final_url, images[0])

    descriptions = _meta_values(soup, "description") or _meta_values(soup, "og:description")
    product["description"] = _clean((record or {}).get("description") or (descriptions[0] if descriptions else None), MAX_PRODUCT_DESCRIPTION_LENGTH)
    raw_description = (record or {}).get("description") or (descriptions[0] if descriptions else None)
    if _would_truncate(raw_description, MAX_PRODUCT_DESCRIPTION_LENGTH):
        warning_codes.append("field_truncated")

    current_dom_prices, original_prices = _price_evidence(context)
    gtag_price = _number((gtag or {}).get("price"))
    record_price, record_currency = _record_price(record or {})
    meta_prices = [_number(value) for value in _meta_values(soup, "product:price:amount")]
    meta_prices = [value for value in meta_prices if value is not None]
    visible_unique = _distinct(current_dom_prices)
    metadata_unique = _distinct(meta_prices)

    price_conflict = False
    attributed_prices = _distinct([*([gtag_price] if gtag_price is not None else []), *metadata_unique,
                                    *([record_price] if record_price is not None else [])])
    if len(attributed_prices) > 1:
        price_conflict = True
        warning_codes.append("price_conflict")
    # A matching item record and page-level product price outrank page-wide
    # price nodes, which may also contain recommendations.
    selected_price = gtag_price
    if selected_price is None and len(metadata_unique) == 1:
        selected_price = metadata_unique[0]
    if selected_price is None:
        selected_price = record_price
    if selected_price is None and len(visible_unique) == 1:
        selected_price = visible_unique[0]
    if selected_price is not None and len(visible_unique) == 1 and visible_unique[0] != selected_price:
        price_conflict = True
        warning_codes.append("price_conflict")
    product["price"] = _json_number(selected_price)

    currencies = []
    for node in context.find_all(True):
        if _in_unrelated_product_section(node, context):
            continue
        classes = node.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        labels = " ".join([*classes, str(node.get("id", ""))]).casefold()
        if "price" in labels:
            code = _currency(node.get_text(" ", strip=True))
            if code:
                currencies.append(code)
    metadata_currency = _distinct([_currency(value) for value in _meta_values(soup, "product:price:currency")])
    currencies = _distinct(currencies)
    if len(currencies) > 1:
        warning_codes.append("currency_conflict")
    product["currency"] = (
        currencies[0] if len(currencies) == 1
        else (metadata_currency[0] if len(metadata_currency) == 1
              else (record_currency or _currency((gtag or {}).get("currency"))))
    )
    if len(metadata_currency) > 1:
        warning_codes.append("currency_conflict")

    # Only collect an original price from explicitly old-price marked DOM nodes.
    old_unique = _distinct(original_prices)
    if len(old_unique) == 1:
        product["original_price"] = _json_number(old_unique[0])
    elif len(old_unique) > 1:
        warning_codes.append("original_price_ambiguous")

    specifications, specs_truncated = _specifications(context, soup)
    product["specifications"] = specifications
    if specs_truncated:
        warning_codes.append("specifications_truncated")
    product["availability_evidence"], availability_truncated = _availability_evidence(soup)
    if availability_truncated:
        warning_codes.append("availability_truncated")

    status = "ok"
    if (
        selected_price is None or product["currency"] is None or price_conflict
        or "identity_conflict" in warning_codes or "currency_conflict" in warning_codes
    ):
        status = "partial"
    result = _response(final_url, status, product, warning_codes)
    return enrich_guanxe_product(final_url, soup, result)
