# Web Extractor API

This is a simple Flask app that extracts structured content from a website.

## Usage

Deploy on Render, then call:

```
GET /extract?url=https://example.com
```

## Output
- `title`
- `meta_description`
- `main_text` (first 100k chars)
- `links` (text and href)
- `raw_html` (truncated)