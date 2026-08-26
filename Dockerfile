FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/*

COPY scraper.py audioteka_provider.py lubimyczytac_provider.py lubimyczytac_patch.py lubimyczytac_search_patch.py nginx.conf ./

ENV PYTHONUNBUFFERED=1

EXPOSE 3000 3001 3002

CMD ["sh", "-c", "uvicorn scraper:app --host 127.0.0.1 --port 8000 & uvicorn audioteka_provider:app --host 127.0.0.1 --port 8001 & uvicorn lubimyczytac_search_patch:app --host 127.0.0.1 --port 8002 & nginx -c /app/nginx.conf -g 'daemon off;' & wait"]
