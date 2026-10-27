"""Bounded, deterministic product extraction for the search-mode PoC."""

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag


MAX_SEARCH_PRODUCTS = 20
_SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CARD_SKU_RE = re.compile(r"^art_([A-Za-z0-9][A-Za-z0-9._-]*?)(?:_|$)")
_FACTS_SKU_RE = re.compile(r"^itemFacts-(.+)$")
_OLD_PRICE_MARKERS = ("oldprice", "originalprice", "previousprice", "regularprice", "strike", "wasprice")
_CURRENT_PRICE_CLASSES = {
    "itemcurrentprice",
    "itemfamilyprice",
    "itemnormalprice",
    "itemofferprice",
    "currentprice",
    "pricecurrent",
    "priceoffer",
}
_CURRENCY_CODES = {"€": "EUR", "£": "GBP", "¥": "JPY"}
_SUPPORTED_CURRENCY_CODES = {
    "AUD", "CAD", "CHF", "CNY", "DKK", "EUR", "GBP", "HKD", "INR", "JPY",
    "KRW", "MXN", "NOK", "NZD", "PLN", "SEK", "SGD", "THB", "TRY", "TWD",
    "USD", "ZAR",
}


def _nonempty_string(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _sku(value):
    value = _nonempty_string(value)
    return value if value and _SKU_RE.fullmatch(value) else None


def _resolved_http_url(base_url, value):
    value = _nonempty_string(value)
    if not value:
        return None
    try:
        resolved = urljoin(base_url, value)
        parts = urlsplit(resolved)
    except (TypeError, ValueError):
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return resolved


def _extract_embedded_items(soup):
    """Read observed IKEA `var item = {...}` records by their explicit partnumber."""
    records = {}
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue
        for match in re.finditer(r"\bvar\s+item\s*=\s*", text):
            start = match.end()
            while start < len(text) and text[start].isspace():
                start += 1
            try:
                record, _ = json.JSONDecoder().raw_decode(text, start)
            except (json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue
            partnumber = _sku(record.get("partnumber"))
            if partnumber:
                records.setdefault(partnumber, []).append(record)
    return records


def _card_identifiers(card):
    identifiers = set()
    card_id = card.get("id", "")
    match = _CARD_SKU_RE.match(card_id) if isinstance(card_id, str) else None
    if match:
        value = _sku(match.group(1))
        if value:
            identifiers.add(value)

    for facts in card.select('[data-id^="itemFacts-"]'):
        match = _FACTS_SKU_RE.match(str(facts.get("data-id", "")))
        if match:
            value = _sku(match.group(1))
            if value:
                identifiers.add(value)

    for anchor in card.select('a[data-item][href*="/pd/"]'):
        value = _sku(anchor.get("data-item"))
        if value:
            identifiers.add(value)
    return identifiers


def _price_classes(node, stop):
    classes = set()
    current = node
    while isinstance(current, Tag):
        values = current.get("class", [])
        if isinstance(values, str):
            values = values.split()
        classes.update(re.sub(r"[^a-z0-9]", "", value.casefold()) for value in values)
        if current is stop:
            break
        current = current.parent
    return classes


def _is_old_price(classes):
    return any(
        value in {"old", "original", "previous", "was"}
        or any(marker in value for marker in _OLD_PRICE_MARKERS)
        for value in classes
    )


def _parse_numeric_price(value, *, source="text"):
    if not isinstance(value, str):
        value = str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
    value = value.replace("\u00a0", " ").strip()
    match = re.search(r"[-+]?[0-9][0-9.,\s]*", value)
    if not match:
        return None
    numeric = re.sub(r"\s", "", match.group(0))
    comma = numeric.rfind(",")
    dot = numeric.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_mark = "," if comma > dot else "."
        grouping_mark = "." if decimal_mark == "," else ","
        numeric = numeric.replace(grouping_mark, "").replace(decimal_mark, ".")
    elif comma >= 0 or dot >= 0:
        separator = "," if comma >= 0 else "."
        parts = numeric.split(separator)
        grouped = len(parts) > 1 and 1 <= len(parts[0].lstrip("+-")) <= 3 and all(
            len(part) == 3 and part.isdigit() for part in parts[1:]
        )
        if source == "machine":
            if separator == "," and grouped:
                numeric = "".join(parts)
            else:
                numeric = numeric.replace(separator, ".")
        elif grouped:
            # In Spanish display text a single separator followed by three digits is
            # interpreted as a thousands separator; decimal amounts conventionally
            # have one or two fractional digits (for example, 24,90 €).
            numeric = "".join(parts)
        else:
            numeric = numeric.replace(separator, ".")
    try:
        amount = Decimal(numeric)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _current_price_box(card, sku):
    boxes = card.select(".itemPriceBox")
    explicitly_associated = [box for box in boxes if box.get("data-id") is not None]
    matching = [box for box in explicitly_associated if _sku(box.get("data-id")) == sku]
    if len(matching) == 1:
        return matching[0]
    if matching or explicitly_associated:
        return None
    return boxes[0] if len(boxes) == 1 else None


def _currency_from_text(value):
    value = str(value or "")
    for symbol, code in _CURRENCY_CODES.items():
        if symbol in value:
            return code
    return next(
        (match.group(0) for match in re.finditer(r"\b[A-Z]{3}\b", value.upper())
         if match.group(0) in _SUPPORTED_CURRENCY_CODES),
        None,
    )


def _selected_price_text(node, price_box):
    current = node
    while isinstance(current, Tag) and current is not price_box:
        if _price_classes(current, current) & _CURRENT_PRICE_CLASSES:
            return current.get_text(" ", strip=True)
        current = current.parent
    return node.get_text(" ", strip=True)


def _displayed_price(card, sku, record):
    box = _current_price_box(card, sku)
    selected = None
    price = None
    currency_text = ""
    if box is not None:
        candidates = []
        all_candidates = []
        for node in box.select("[data-price]"):
            classes = _price_classes(node, box)
            if node.name in {"del", "s", "strike"} or _is_old_price(classes):
                continue
            all_candidates.append(node)
            if classes & _CURRENT_PRICE_CLASSES:
                candidates.append(node)
        if len(candidates) == 1:
            selected = candidates[0]
        elif len(candidates) > 1:
            exact = [node for node in candidates if "itemofferprice" in _price_classes(node, box)]
            if len(exact) == 1:
                selected = exact[0]
        elif len(all_candidates) == 1:
            selected = all_candidates[0]

        if selected is not None:
            price = _parse_numeric_price(str(selected.get("data-price", "")), source="machine")
            currency_text = _selected_price_text(selected, box)
            if price is None:
                price = _parse_numeric_price(currency_text, source="text")
        else:
            visible_current = []
            for node in box.select(
                ".itemCurrentPrice, .itemFamilyPrice, .itemNormalPrice, .itemOfferPrice, "
                ".currentPrice, .priceCurrent, .priceOffer"
            ):
                if node.name in {"del", "s", "strike"} or _is_old_price(_price_classes(node, box)):
                    continue
                visible_current.append(node)
            if len(visible_current) == 1:
                selected = visible_current[0]
                price = _parse_numeric_price(selected.get_text(" ", strip=True), source="text")
                currency_text = selected.get_text(" ", strip=True)

    if price is None and isinstance(record, dict):
        price = _parse_numeric_price(record.get("current_price"), source="machine")
        currency_text = str(record.get("currency", ""))

    currency = _currency_from_text(currency_text)
    if currency is None and isinstance(record, dict):
        record_currency = _nonempty_string(record.get("currency"))
        currency = _currency_from_text(record_currency)
    return price, currency


def _card_text_value(card, selectors):
    for selector in selectors:
        node = card.select_one(selector)
        if node:
            value = " ".join(node.stripped_strings)
            if value:
                return value
    return None


def _record_signature(record):
    return json.dumps(
        {
            key: record.get(key)
            for key in ("partnumber", "name", "current_price", "currency", "valid_design", "variant", "url", "default_image")
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _product_from_card(card, final_url, records):
    identifiers = _card_identifiers(card)
    if len(identifiers) != 1:
        return None, False
    sku = next(iter(identifiers))
    matching_records = records.get(sku, [])
    link_urls = {
        _resolved_http_url(final_url, anchor.get("href"))
        for anchor in card.select('a[data-item][href*="/pd/"]')
        if _sku(anchor.get("data-item")) == sku
    }
    link_urls.discard(None)
    if len(link_urls) > 1:
        return None, False
    card_url = next(iter(link_urls), None)

    record = None
    association_conflict = False
    if len(matching_records) == 1:
        candidate = matching_records[0]
        record_url = _resolved_http_url(final_url, candidate.get("url"))
        if card_url and candidate.get("url") and record_url != card_url:
            association_conflict = True
        else:
            record = candidate
    elif matching_records:
        signatures = {_record_signature(candidate) for candidate in matching_records}
        if len(signatures) > 1:
            association_conflict = True
        elif card_url:
            matching = [
                candidate for candidate in matching_records
                if not candidate.get("url") or _resolved_http_url(final_url, candidate.get("url")) == card_url
            ]
            if len(matching) == len(matching_records):
                record = matching[0]
            else:
                association_conflict = True
        else:
            record = matching_records[0]

    product_url = card_url
    if product_url is None and record:
        product_url = _resolved_http_url(final_url, record.get("url"))
    if not product_url:
        return None, association_conflict

    name = None
    if record:
        name = _nonempty_string(record.get("name"))
    if not name:
        name = _card_text_value(card, ("[data-testid='product-title']", ".itemName", ".itemTitle", "h2", "h3", "h4"))
    if not name:
        matching_anchors = [
            anchor for anchor in card.select('a[data-item][href*="/pd/"]')
            if _sku(anchor.get("data-item")) == sku
        ]
        if matching_anchors:
            name = " ".join(matching_anchors[0].stripped_strings) or None

    price, currency = _displayed_price(card, sku, record)
    variant = None
    if record:
        variant = _nonempty_string(record.get("valid_design")) or _nonempty_string(record.get("variant"))
    if not variant:
        facts = card.select_one(f'[data-id="itemFacts-{sku}"]')
        variant = " ".join(facts.stripped_strings) if facts else None
        variant = variant or None

    image_url = None
    if record:
        image_url = _resolved_http_url(final_url, record.get("default_image"))
    if not image_url:
        for image in card.select("img"):
            raw_image = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
            image_url = _resolved_http_url(final_url, raw_image)
            if image_url:
                break

    return {
        "name": name,
        "sku": sku,
        "price": price,
        "currency": currency,
        "variant": variant,
        "url": product_url,
        "image_url": image_url,
    }, association_conflict


def _product_from_record(record, final_url):
    """Fallback for a complete explicit item record when cards are absent."""
    sku = _sku(record.get("partnumber"))
    product_url = _resolved_http_url(final_url, record.get("url"))
    if not sku or not product_url:
        return None
    price = _parse_numeric_price(record.get("current_price"), source="machine")
    currency = _currency_from_text(record.get("currency"))
    return {
        "name": _nonempty_string(record.get("name")),
        "sku": sku,
        "price": price,
        "currency": currency,
        "variant": _nonempty_string(record.get("valid_design")) or _nonempty_string(record.get("variant")),
        "url": product_url,
        "image_url": _resolved_http_url(final_url, record.get("default_image")),
    }


def _deduplicate_products(products):
    unique = []
    by_identity = {}
    conflicting = False
    for product in products:
        identity = (product["sku"], product["url"])
        existing_index = by_identity.get(identity)
        if existing_index is None:
            by_identity[identity] = len(unique)
            unique.append(product)
        elif unique[existing_index] != product:
            # Keep the first complete record intact. Never fill its missing fields
            # from a conflicting card with the same explicit identity.
            conflicting = True
    return unique, conflicting


def _complete_embedded_record(record):
    return bool(
        _sku(record.get("partnumber"))
        and _nonempty_string(record.get("name"))
        and _nonempty_string(record.get("url"))
    )


def _response(final_url, status, products=None, truncated=False):
    products = products or []
    return {
        "mode": "search",
        "source_url": final_url,
        "extraction_status": status,
        "product_count": len(products),
        "products": products,
        "truncated": bool(truncated),
    }


def extract_search_content(final_url, html, content_status="ok"):
    """Extract product records from already-fetched IKEA search/category HTML."""
    if content_status == "blocked" or not isinstance(html, str) or not html.strip():
        return _response(final_url, "unusable")

    soup = BeautifulSoup(html, "html.parser")
    records = _extract_embedded_items(soup)
    cards = soup.select('div.itemBlock[id^="art_"]')
    if content_status == "empty" and not any(
        _complete_embedded_record(record)
        for record_list in records.values()
        for record in record_list
    ):
        return _response(final_url, "unusable")

    products = []
    unreliable = False
    association_conflict_found = False

    if cards:
        for card in cards:
            product, association_conflict = _product_from_card(card, final_url, records)
            unreliable = unreliable or association_conflict
            association_conflict_found = association_conflict_found or association_conflict
            if product is None:
                unreliable = True
            else:
                products.append(product)
    else:
        for record_list in records.values():
            for record in record_list:
                product = _product_from_record(record, final_url)
                if product is None:
                    unreliable = True
                else:
                    products.append(product)

    products, conflicting_duplicates = _deduplicate_products(products)
    unreliable = unreliable or conflicting_duplicates
    if not products:
        return _response(final_url, "partial" if association_conflict_found else "no_products")

    truncated = len(products) > MAX_SEARCH_PRODUCTS
    products = products[:MAX_SEARCH_PRODUCTS]
    status = "partial" if truncated or unreliable else "ok"
    return _response(final_url, status, products, truncated)
