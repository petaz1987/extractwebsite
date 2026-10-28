# Isolated Render Free DEV runtime. Production native-Python service is unchanged.
FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Debian 13 supplies curl >= 8.4 with the distro CA store.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Ship only the runtime files; no local credentials, fixtures or test artifacts.
COPY main.py product_extraction.py search_extraction.py worten_adapter.py ./

EXPOSE 10000

# Log the effective curl version in Render, then preserve the existing app start.
CMD ["sh", "-c", "curl --version && exec python main.py"]
