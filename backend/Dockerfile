FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app/backend

# Ensure Chromium is present for Playwright Computer Use tasks in Cloud Run.
RUN playwright install chromium

EXPOSE 8080

CMD ["bash", "-lc", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
