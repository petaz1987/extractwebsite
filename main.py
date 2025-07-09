from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

app = Flask(__name__)

def extract_web_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        title = soup.title.string if soup.title else ""
        meta_desc_tag = soup.find("meta", attrs={"name": "description"})
        meta_description = meta_desc_tag["content"] if meta_desc_tag else ""

        main_text = soup.get_text(separator="\n", strip=True)
        links = [
            {"text": a.get_text(strip=True), "href": a.get("href")}
            for a in soup.find_all("a", href=True)
        ]

        return {
            "url": url,
            "title": title,
            "meta_description": meta_description,
            "main_text": main_text[:100000],  # truncate to 100k chars if too long
            "links": links,
            "raw_html": html[:100000]  # truncate if too long
        }
    except Exception as e:
        return {"error": str(e)}

@app.route("/extract", methods=["GET"])
def extract():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing 'url' query parameter"}), 400
    result = extract_web_content(url)
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)