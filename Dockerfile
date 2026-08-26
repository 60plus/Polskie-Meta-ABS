FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py audioteka_scraper.py audioteka_app.py ./

ENV PYTHONUNBUFFERED=1

EXPOSE 3000 3001

CMD ["sh", "-c", "uvicorn scraper:app --host 0.0.0.0 --port 3000 & uvicorn audioteka_app:app --host 0.0.0.0 --port 3001 & wait"]
