"""Bounded, source-faithful Markdown extraction for product content panels."""

import re
import unicodedata
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag


MAX_PRODUCT_SECTION_LENGTH = 40_000
MAX_PRODUCT_MARKDOWN_TOTAL_LENGTH = 60_000
MAX_PANEL_REFERENCES = 1_200
MAX_RENDERED_NODES = 8_000
MAX_TABLE_ROWS = 1_200
MAX_LIST_DEPTH = 12

_DETAIL_LABELS = (
    "mas informacion", "descripcion", "descripcion del producto", "detalles del producto",
    "product description", "product information", "product details", "caracteristicas",
    "informacion del articulo", "about this product",
)
_TECH_LABELS = (
    "ficha tecnica", "especificaciones", "especificaciones tecnicas", "technical specifications",
    "technical details", "technical data", "product specifications", "datos tecnicos",
    "caracteristicas tecnicas",
)
_DETAIL_PANEL_MARKERS = (
    "description", "productdescription", "tabdescription", "moreinfo", "moreinformation",
    "productinformation", "productdetailsdescription",
)
_TECH_PANEL_MARKERS = (
    "technical", "specification", "datasheet", "productspecifications", "productdetails",
    "productfeatures", "caracteristicastecnicas", "fichatecnica",
)
_UNRELATED_MARKERS = (
    "related", "recommend", "upsell", "crosssell", "productcarousel", "productlist",
    "suggestion", "recentlyviewed", "complementary", "youmaylike", "accessor", "reviews",
    "questions", "newsletter", "footer", "breadcrumb", "navigation", "nav-", "seo", "keywords",
)
_SKIP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "aside", "form", "button", "input",
    "select", "textarea", "svg", "iframe", "template", "canvas",
}
_BLOCK_TAGS = {"address", "blockquote", "div", "dl", "h1", "h2", "h3", "h4", "h5", "h6",
               "hr", "li", "ol", "p", "pre", "section", "table", "ul"}
_POLICY = re.compile(
    r"^(?:guanxe ofrece a sus clientes|en dicho plazo aceptamos devoluciones|cambios y devoluciones|"
    r"derecho de devoluci[oó]n|pol[ií]tica de devoluciones|informaci[oó]n sobre env[ií]os|"
    r"t[eé]rminos y condiciones|roturas o defectos de origen|para realizar una devoluci[oó]n)", re.I,
)
_MARKETING = re.compile(
    r"(?:compra online miles de productos|entrega r[aá]pida desde|marcas 100% originales|"
    r"disponible en canarias|garant[ií]a oficial)", re.I,
)


def _normalize_label(value):
    value = unicodedata.normalize("NFKD", str(value).casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _classify_label(value):
    normalized = _normalize_label(value)
    if any(label in normalized for label in _TECH_LABELS):
        return "technical_details_markdown"
    if any(label in normalized for label in _DETAIL_LABELS):
        return "details_markdown"
    return None


def _panel_kind(node):
    labels = [str(node.get("id", "")), *node.get("class", [])]
    normalized = " ".join(re.sub(r"[^a-z0-9]", "", item.casefold()) for item in labels)
    if any(marker in normalized for marker in _TECH_PANEL_MARKERS):
        return "technical_details_markdown"
    if any(marker in normalized for marker in _DETAIL_PANEL_MARKERS):
        return "details_markdown"
    return None


def _is_unrelated(node):
    current = node
    while isinstance(current, Tag):
        labels = [str(current.get("id", "")), *current.get("class", [])]
        normalized = " ".join(re.sub(r"[^a-z0-9]", "", item.casefold()) for item in labels)
        if current.name in {"nav", "footer", "aside"} or any(marker in normalized for marker in _UNRELATED_MARKERS):
            return True
        if str(current.get("aria-hidden", "")).casefold() == "true" or current.has_attr("hidden"):
            return True
        current = current.parent
    return False


def _fragment_targets(document, control):
    targets = []
    fragment = None
    href = control.get("href")
    if isinstance(href, str) and href.startswith("#") and len(href) > 1:
        fragment = unquote(href[1:])
    if not fragment:
        for attribute in ("aria-controls", "data-target", "data-bs-target"):
            value = control.get(attribute)
            if not isinstance(value, str):
                continue
            refs = value.split() if attribute == "aria-controls" else [item.lstrip("#") for item in value.split()]
            targets.extend(refs[:4])
    else:
        targets.append(fragment)
    result = []
    for target_id in dict.fromkeys(targets):
        matches = document.find_all(id=target_id, limit=2)
        if len(matches) == 1 and isinstance(matches[0], Tag):
            result.append(matches[0])
    return result


def _append_unique(mapping, kind, node):
    items = mapping[kind]
    if not any(existing is node or existing in node.parents or node in existing.parents for existing in items):
        items.append(node)


def _collect_panels(document):
    panels = {"details_markdown": [], "technical_details_markdown": []}
    missing = set()
    controls = document.find_all(
        lambda node: isinstance(node, Tag) and (
            node.get("role") == "tab" or node.has_attr("aria-controls")
            or (node.name == "a" and isinstance(node.get("href"), str) and node["href"].startswith("#"))
            or node.has_attr("data-target") or node.has_attr("data-bs-target")
        ),
        limit=MAX_PANEL_REFERENCES + 1,
    )
    if len(controls) > MAX_PANEL_REFERENCES:
        controls = controls[:MAX_PANEL_REFERENCES]
        missing.add("references_truncated")
    for control in controls:
        if _is_unrelated(control):
            continue
        label = control.get("aria-label") or control.get("title") or control.get_text(" ", strip=True)
        kind = _classify_label(label)
        targets = _fragment_targets(document, control)
        if kind is None:
            kind = next((_panel_kind(target) for target in targets if _panel_kind(target)), None)
        if kind is None:
            continue
        if not targets:
            missing.add(kind)
            continue
        for target in targets:
            if not _is_unrelated(target):
                _append_unique(panels, kind, target)
    return panels, missing


def _fallback_panels(document, context, panels):
    if all(panels.values()):
        return False
    detail_ids = {"description", "tab-description", "more_info_sheets", "product-description"}
    detail_classes = {"product-description", "product-description-container"}
    technical_ids = {
        "product-details", "tab-product-details", "technical-specifications", "technical-details",
    }
    technical_classes = {
        "product-features", "data-sheet", "technical-specifications", "technical-details",
        "product-specifications",
    }
    nodes = document.find_all(True, limit=MAX_RENDERED_NODES + 1)
    scan_truncated = len(nodes) > MAX_RENDERED_NODES
    for node in nodes[:MAX_RENDERED_NODES]:
        node_id = str(node.get("id", "")).casefold()
        classes = node.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        classes = {str(value).casefold() for value in classes}
        is_details = (node_id in detail_ids or bool(classes & detail_classes)
                      or str(node.get("itemprop", "")).casefold() == "description")
        is_technical = node_id in technical_ids or bool(classes & technical_classes)
        if not (is_details or is_technical) or _is_unrelated(node):
            continue
        if is_details:
            _append_unique(panels, "details_markdown", node)
        if is_technical:
            _append_unique(panels, "technical_details_markdown", node)
    # A separate technical panel is only extracted as such when its own
    # target is outside the selected details panel. Embedded specs remain
    # part of the details Markdown.
    if panels["technical_details_markdown"] and panels["details_markdown"]:
        panels["technical_details_markdown"] = [node for node in panels["technical_details_markdown"] if not any(
            node is details or node in details.parents or details in node.parents
            for details in panels["details_markdown"]
        )]
    return scan_truncated


def _inline(node, *, excluded=None):
    excluded = excluded or set()
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or id(node) in excluded or node.name in _SKIP_TAGS or _is_unrelated(node):
        return ""
    if node.name == "br":
        return "  \n"
    if node.name == "img":
        alt = " ".join(str(node.get("alt", "")).split())
        return alt
    content = "".join(_inline(child, excluded=excluded) for child in node.children)
    content = re.sub(r"[ \t\r\f\v]+", " ", content).strip()
    if not content:
        return ""
    if node.name in {"strong", "b"}:
        return f"**{content}**"
    if node.name in {"em", "i"}:
        return f"*{content}*"
    if node.name == "code":
        return f"`{content}`"
    if node.name == "a":
        href = str(node.get("href", "")).strip()
        if href and not href.startswith(("javascript:", "data:")):
            return f"[{content}]({href})"
    return content


class _MarkdownRenderer:
    def __init__(self, excluded):
        self.excluded = {id(node) for node in excluded}
        self.visited = 0
        self.truncated = False

    def _visit(self, node):
        self.visited += 1
        if self.visited > MAX_RENDERED_NODES:
            self.truncated = True
            return False
        return not _is_unrelated(node) and id(node) not in self.excluded

    def _inline(self, node, excluded=None):
        value = _inline(node, excluded=self.excluded | (excluded or set()))
        return " ".join(value.split())

    def _table(self, table):
        rows = table.find_all("tr", limit=MAX_TABLE_ROWS + 1)
        if len(rows) > MAX_TABLE_ROWS:
            rows = rows[:MAX_TABLE_ROWS]
            self.truncated = True
        parsed = []
        for row in rows:
            if not self._visit(row):
                continue
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                cells = row.find_all(["th", "td"])
            if cells:
                parsed.append([self._inline(cell) for cell in cells])
        parsed = [row for row in parsed if any(row)]
        if not parsed:
            return ""
        explicit_header = bool(table.find("thead")) or all(
            cell.name == "th" for cell in parsed and rows[0].find_all(["th", "td"], recursive=False)
        )
        if explicit_header and len(parsed) > 1:
            header, body = parsed[0], parsed[1:]
        elif explicit_header:
            header, body = parsed[0], []
        else:
            width = max(len(row) for row in parsed)
            header = ["Label", "Value"] if width == 2 else [f"Column {i + 1}" for i in range(width)]
            body = parsed
        width = max(len(header), *(len(row) for row in body)) if body else len(header)
        header += [""] * (width - len(header))
        lines = ["| " + " | ".join(self._escape_cell(value) for value in header) + " |",
                 "| " + " | ".join("---" for _ in range(width)) + " |"]
        for row in body:
            row += [""] * (width - len(row))
            lines.append("| " + " | ".join(self._escape_cell(value) for value in row) + " |")
        return "\n".join(lines)

    @staticmethod
    def _escape_cell(value):
        return " ".join(str(value).split()).replace("|", r"\|")

    def _list(self, node, depth=0):
        if depth >= MAX_LIST_DEPTH:
            self.truncated = True
            return ""
        ordered = node.name == "ol"
        start = int(node.get("start", 1)) if str(node.get("start", "1")).isdigit() else 1
        lines = []
        for item_index, item in enumerate(node.find_all("li", recursive=False)):
            if not self._visit(item):
                continue
            prefix = "  " * depth + (f"{start + item_index}. " if ordered else "- ")
            nested = item.find_all(["ul", "ol"], recursive=False)
            label = self._inline(item, excluded={id(child) for child in nested})
            if label:
                lines.append(prefix + label)
            for child in nested:
                nested_markdown = self._list(child, depth + 1)
                if nested_markdown:
                    lines.append(nested_markdown)
        return "\n".join(lines)

    def _definition_list(self, node):
        lines = []
        for term in node.find_all("dt", recursive=False):
            if not self._visit(term):
                continue
            definition = term.find_next_sibling("dd")
            if definition:
                self._visit(definition)
                label, value = self._inline(term), self._inline(definition)
                if label and value:
                    lines.append(f"- **{label}**: {value}")
        return "\n".join(lines)

    def render(self, node, depth=0):
        if depth > 64 or not self._visit(node):
            if depth > 64:
                self.truncated = True
            return ""
        if node.name == "table":
            return self._table(node)
        if node.name in {"ul", "ol"}:
            return self._list(node)
        if node.name == "dl":
            return self._definition_list(node)
        if node.name in {"p", "blockquote", "pre"}:
            text = self._inline(node)
            return text if text and not _POLICY.match(text) and not _MARKETING.search(text) else ""
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = self._inline(node)
            if not text or _POLICY.match(text) or _MARKETING.search(text):
                return ""
            return f"{'#' * int(node.name[1])} {text}"

        blocks, inline = [], []
        for child in node.children:
            if isinstance(child, NavigableString):
                if str(child).strip():
                    inline.append(str(child))
                continue
            if not isinstance(child, Tag) or not self._visit(child):
                continue
            if child.name in {"h1", "h2", "h3", "h4", "h5", "h6", "p"} and _POLICY.match(self._inline(child)):
                break
            if child.name in _BLOCK_TAGS:
                if inline:
                    text = " ".join(" ".join(" ".join(inline).split()).split())
                    if text and not _POLICY.match(text) and not _MARKETING.search(text):
                        blocks.append(text)
                    inline = []
                if child.name in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "pre"}:
                    rendered = self.render_visited(child, depth + 1)
                elif child.name == "li":
                    rendered = self._inline(child)
                else:
                    rendered = self.render_visited(child, depth + 1)
                if rendered:
                    blocks.append(rendered)
            else:
                inline.append(self._inline(child))
        if inline:
            text = " ".join(" ".join(" ".join(inline).split()).split())
            if text and not _POLICY.match(text) and not _MARKETING.search(text):
                blocks.append(text)
        return "\n\n".join(blocks)

    def render_visited(self, node, depth):
        # The parent loop has already counted the node.
        return self._render_children(node, depth)

    def _render_children(self, node, depth):
        if depth > 64:
            self.truncated = True
            return ""
        if node.name == "table":
            return self._table(node)
        if node.name in {"ul", "ol"}:
            return self._list(node)
        if node.name == "dl":
            return self._definition_list(node)
        if node.name in {"p", "blockquote", "pre"}:
            text = self._inline(node)
            return text if text and not _POLICY.match(text) and not _MARKETING.search(text) else ""
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = self._inline(node)
            if not text or _POLICY.match(text) or _MARKETING.search(text):
                return ""
            return f"{'#' * int(node.name[1])} {text}"
        blocks, inline = [], []
        for child in node.children:
            if isinstance(child, NavigableString):
                if str(child).strip():
                    inline.append(str(child))
            elif isinstance(child, Tag) and self._visit(child):
                if child.name in {"h1", "h2", "h3", "h4", "h5", "h6", "p"} and _POLICY.match(self._inline(child)):
                    break
                if child.name in _BLOCK_TAGS:
                    if inline:
                        text = " ".join(" ".join(" ".join(inline).split()).split())
                        if text and not _POLICY.match(text) and not _MARKETING.search(text):
                            blocks.append(text)
                        inline = []
                    rendered = self.render_visited(child, depth + 1)
                    if rendered:
                        blocks.append(rendered)
                else:
                    inline.append(self._inline(child))
        if inline:
            text = " ".join(" ".join(" ".join(inline).split()).split())
            if text and not _POLICY.match(text) and not _MARKETING.search(text):
                blocks.append(text)
        return "\n\n".join(blocks)


def _render_sections(nodes, excluded, limit):
    if not nodes:
        return None, False
    rendered = []
    truncated = False
    for node in nodes:
        renderer = _MarkdownRenderer(excluded)
        content = renderer.render(node)
        truncated = truncated or renderer.truncated
        if content:
            rendered.append(content)
    text = "\n\n".join(dict.fromkeys(rendered)).strip()
    if len(text) > limit:
        truncated = True
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        text = text[:cut].rstrip()
    return text or None, truncated


def extract_product_markdown(document, context):
    """Return product-section Markdown and safe warnings from one parsed page."""
    panels, missing = _collect_panels(document)
    scan_truncated = _fallback_panels(document, context, panels)
    warnings = []
    if "references_truncated" in missing or scan_truncated:
        warnings.append("product_sections_truncated")
    for kind in ("details_markdown", "technical_details_markdown"):
        if kind in missing and not panels[kind]:
            warnings.append(kind.replace("_markdown", "_extraction_failed"))

    details_nodes = panels["details_markdown"]
    technical_nodes = panels["technical_details_markdown"]
    # Nested targets belong to one panel. Keep the content in its outer section
    # rather than duplicating it across the two response fields.
    if any(details in technical.parents or technical in details.parents
           for details in details_nodes for technical in technical_nodes):
        technical_nodes = []

    details, details_truncated = _render_sections(
        details_nodes, technical_nodes, MAX_PRODUCT_SECTION_LENGTH,
    )
    if details_nodes and not details and "details_extraction_failed" not in warnings:
        warnings.append("details_extraction_failed")
    if details_truncated:
        warnings.append("details_markdown_truncated")
    remaining = max(0, MAX_PRODUCT_MARKDOWN_TOTAL_LENGTH - (len(details) if details else 0))
    technical_limit = min(MAX_PRODUCT_SECTION_LENGTH, remaining)
    technical, technical_truncated = _render_sections(technical_nodes, details_nodes, technical_limit)
    if technical_nodes and not technical and "technical_details_extraction_failed" not in warnings:
        warnings.append("technical_details_extraction_failed")
    if technical_truncated:
        warnings.append("technical_details_markdown_truncated")
    return {
        "details_markdown": details,
        "technical_details_markdown": technical,
    }, warnings
