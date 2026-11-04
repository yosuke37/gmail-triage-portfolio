FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir --no-compile -r requirements.lock
COPY triage ./triage
COPY dashboard.py ./
COPY deploy/ocr_smoke.py ./deploy/ocr_smoke.py
COPY data/sample_emails.json ./data/sample_emails.json
COPY data/attachment_demo.json ./data/attachment_demo.json
COPY reports/proposals-dev.json reports/proposals-holdout.json ./reports/
RUN pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home triage \
    && mkdir -p /app/output /app/output/oauth \
    && chown -R 10001:10001 /app/output
USER 10001:10001
CMD ["python", "-m", "triage.runtime", "api"]
