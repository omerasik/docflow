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
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home docflow && mkdir data && chown docflow:docflow data
COPY document_flow/ document_flow/
COPY --from=frontend /build/web/dist/ web/dist/
USER docflow
EXPOSE 8012
CMD ["python", "-m", "uvicorn", "document_flow.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8012"]
