# DocFlow

DocFlow turns digital PDFs, scanned PDFs, UTF-8 invoices and structured JSON into reviewable records. Operators compare extracted values with source text, correct discrepancies, search invoice metadata and export verified records.

![DocFlow review screen showing a total mismatch in a synthetic invoice](docs/dashboard.png)

## Run locally

Use Python 3.12, Node.js 22.14+ and npm. Build the frontend before starting the backend so FastAPI can serve both on the same origin.

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
npm --prefix web ci
npm --prefix web run build
Copy-Item .env.example .env
.venv/Scripts/python -m uvicorn document_flow.app:from_env --factory --env-file .env --host 127.0.0.1 --port 8012
```

On Linux and macOS, use `.venv/bin/python` and `cp .env.example .env`. For frontend development, run the backend and `npm --prefix web run dev`. The loopback development server at port 5173 rebuilds changed assets and proxies the API to port 8012; refresh the browser after edits. Production assets are built with esbuild and strict TypeScript checking.

Open [http://127.0.0.1:8012](http://127.0.0.1:8012) and choose **Load sample**. The invoice has a deliberate total error. Set **Total** to `350.90`, select **Validate invoice**, and download either export. The same [sample file](samples/invoice.txt) can be uploaded manually.

For a real OCR demonstration, install Poppler and Tesseract with English data, set `OCR_ENABLED=true`, and restart the backend. On Windows, use the installer linked by the [Tesseract installation documentation](https://tesseract-ocr.github.io/tessdoc/Installation.html) and configure `OCR_RENDERER` and `OCR_RECOGNIZER` when the executables are not on PATH. Run `python samples/generate_scan.py` with the development dependencies, then upload `samples/scanned-invoice.pdf`. This file has an image only, with no selectable text. Compare the OCR values, correct **Total** from `349.90` to `350.90`, validate and export from the browser.

## API

| Endpoint | Behavior |
| --- | --- |
| GET `/api/documents?q=…` | Searches filename, supplier, customer, invoice number and document type. Results are capped at 100. |
| POST `/api/documents` | Receives up to 2 MiB of PDF, UTF-8 text or structured invoice JSON with a percent-encoded `X-Filename` header. A byte-identical replay returns the existing record. |
| GET `/api/documents/{id}/source` | Returns source text, extraction method and potential duplicates sharing the supplier and invoice number. |
| POST `/api/documents/{id}/review` | Receives `{"version": 1, "invoice": {…}}` with corrected fields. Rejects stale versions and inconsistent invoices. |
| GET `/api/documents/{id}/export?format=csv` | Downloads the validated record. Use `format=json` or omit the format for JSON. |

The document endpoints require an operator bearer token. The local demo uses `docflow-local-demo`. API schemas are available at `/docs`.

## Configuration and deployment

| Setting | Behavior |
| --- | --- |
| `DEMO_MODE` | Enables the default loopback demo credential and sample button when set to `true`. |
| `OPERATOR_TOKEN` | Set a random token of at least 32 characters and disable demo mode for deployment. |
| `DATABASE_URL` | Defaults to `sqlite:///data/docflow.db`. Use `postgresql+psycopg://…` for PostgreSQL. |
| `MODEL_API_KEY`, `MODEL_NAME`, `MODEL_ENDPOINT` | Optional Chat Completions integration for short notes about existing validation issues. With no key, there are zero model calls. |
| `MODEL_EXTRACTION_ENABLED` | Opt in to sending full extracted source text to the configured model for suggestions on fields the deterministic parser did not observe. |
| `OCR_ENABLED`, `OCR_LANGUAGE` | Enable Poppler and Tesseract processing on PDF pages without text. Defaults to the English language pack when enabled. |
| `OCR_RENDERER`, `OCR_RECOGNIZER` | Optional paths to the `pdftoppm` and `tesseract` executables. |
| `REQUIRE_CUSTOMER`, `INVOICE_CURRENCIES` | Require a customer field or restrict validation to a comma-separated subset of supported currencies. |

To run with Compose, add a URL-safe random `DOCFLOW_DB_PASSWORD` to `.env` and run `docker compose up --build`. The app is exposed on loopback. For external access, use TLS and request limits at the reverse proxy.

With a local demo server and OCR enabled, `python scripts/smoke.py` uploads the generated scan and verifies correction plus both export formats. CI runs the same check inside the built Docker image on an isolated loopback port. The script accepts loopback destinations only and uses the public demo token.

## Verification

```sh
python -m pytest -q
python -m ruff check document_flow tests
npm --prefix web run build
python -m pip_audit -r requirements-dev.txt
npm --prefix web audit --audit-level=moderate
```

The default tests use temporary SQLite databases and generate synthetic digital and image-only PDFs in memory. Tests compare expected fields and validation results over three layouts. The real OCR test reads a complete generated scan, including two line items and the expected total mismatch. Install Poppler and the Tesseract English language pack to run it locally. GitHub Actions installs both tools and requires the OCR test while running against PostgreSQL 17. Set `DOCFLOW_TEST_DATABASE_URL` to a dedicated PostgreSQL test database with permission to create schemas to run the same cases on a server.

## Scope and original work

Deterministic extraction supports labeled fields, common English aliases, decimal separators and pipe- or tab-delimited line items. JSON accepts the invoice schema used for export. The preview displays extracted text. Uploaded originals are discarded after processing, so keep the original available to check poor scans or unsupported layouts. The application supports one operator role and six currencies. Model extraction requires an authorized configuration and has been tested with synthetic responses. [Architecture](docs/architecture.md).

Byte-identical uploads return the existing record. Distinct uploads with the same normalized supplier and invoice number remain separately reviewable. The preview lists up to ten matches for the operator to inspect.

Original work includes the bounded upload API, missing-field handling, exact decimal checks, replay deduplication, versioned review, audit history and safe exports. [Third-party notices](THIRD_PARTY_NOTICES.md) · [MIT license](LICENSE).
