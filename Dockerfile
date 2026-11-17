FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY web/package.json web/package-lock.json web/
RUN npm --prefix web ci
COPY web/ web/
COPY samples/ samples/
RUN npm --prefix web run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip==26.3 \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home docflow && mkdir data && chown docflow:docflow data
COPY document_flow/ document_flow/
COPY --from=frontend /build/web/dist/ web/dist/
USER docflow
EXPOSE 8012
HEALTHCHECK CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8012/api/health', timeout=3)"]
CMD ["python", "-m", "uvicorn", "document_flow.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8012"]
