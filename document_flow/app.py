import csv
import hashlib
import hmac
import io
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import unquote, urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .extraction import InvoiceModel
from .invoice import Correction, Currency, Invoice, ReviewRules, extract_invoice, review_issues
from .model import ReviewModel
from .ocr import TesseractOcr
from .parser import MAX_BYTES, MAX_TEXT, DecodedDocument, OcrEngine, ParseError, decode_source
from .storage import (
    Base,
    Document,
    DocumentSource,
    OriginalSource,
    backfill_identities,
    backfill_retention,
    database,
    delete_document,
    duplicate_candidates,
    expire_sources,
    find_digest,
    list_recent,
    purge_source,
    save_review,
    save_upload,
)

DEMO_TOKEN = "docflow-local-demo"


def create_app(
    *, url: str, token: str, demo: bool = False, model: ReviewModel | None = None,
    web_path: Path | None = None, ocr: OcrEngine | None = None, rules: ReviewRules = ReviewRules(),
    extractor: InvoiceModel | None = None, retention_days: int = 7,
) -> FastAPI:
    if not demo and len(token) < 32:
        raise ValueError("Set OPERATOR_TOKEN to a random token of at least 32 characters.")
    if not 1 <= retention_days <= 365:
        raise ValueError("SOURCE_RETENTION_DAYS must be between 1 and 365.")
    engine = database(url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        Base.metadata.create_all(engine)
        try:
            backfill_identities(engine)
            backfill_retention(engine, retention_days)
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="DocFlow", lifespan=lifespan)

    def authorize(request: Request) -> None:
        supplied = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
            raise HTTPException(401, "Operator access required.")

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    def demo_status() -> dict[str, bool]:
        return {"demo_access": demo and token == DEMO_TOKEN}

    @api.get("/documents")
    def list_documents(q: str = Query(default="", max_length=120)) -> list[dict]:
        with Session(engine) as session:
            expire_sources(session)
            return [document.view() for document in list_recent(session, q.strip())]

    @api.get("/documents/{document_id}/source")
    def preview(document_id: UUID) -> dict:
        with Session(engine) as session:
            expire_sources(session)
            doc_id = str(document_id)
            source = session.get(DocumentSource, doc_id)
            original = session.get(OriginalSource, doc_id)
            if source is None and session.get(Document, doc_id) is None:
                raise HTTPException(404, "Document not found.")
            if original is not None and original.expires_at <= datetime.now(UTC).isoformat():
                source = None
                original = None
            return {"method": source.method if source else None, "text": source.text if source else None,
                    "original_available": original is not None and original.content is not None,
                    "expires_at": original.expires_at if original else None,
                    "duplicate_candidates": duplicate_candidates(session, doc_id)}

    @api.get("/documents/{document_id}/original")
    def original_document(document_id: UUID) -> Response:
        with Session(engine) as session:
            expire_sources(session)
            original = session.get(OriginalSource, str(document_id))
            if original is None or original.content is None or original.expires_at <= datetime.now(UTC).isoformat():
                raise HTTPException(410, "The original is unavailable or its retention period expired.")
            extension = {"application/pdf": "pdf", "application/json": "json", "text/plain": "txt"}[original.media_type]
            return Response(original.content, media_type=original.media_type, headers={
                "Content-Disposition": f'attachment; filename="source-{document_id}.{extension}"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
            })

    @api.delete("/documents/{document_id}/source")
    def remove_source(document_id: UUID, version: int = Query(ge=1, le=2_147_483_646)) -> dict:
        with Session(engine) as session:
            document = session.get(Document, str(document_id))
            if document is None:
                raise HTTPException(404, "Document not found.")
            changed = purge_source(session, document, version, "Source removed by operator")
            if changed is None:
                raise HTTPException(409, "The document changed. Reload before removing its source.")
            return changed.view()

    @api.delete("/documents/{document_id}")
    def remove_document(document_id: UUID, version: int = Query(ge=1, le=2_147_483_646)) -> Response:
        with Session(engine) as session:
            if not delete_document(session, str(document_id), version):
                raise HTTPException(409, "The document changed or was already deleted. Reload the queue.")
        return Response(status_code=204)

    def ingest(data: bytes, filename: str, media_type: str) -> JSONResponse:
        digest = hashlib.sha256(data).hexdigest()
        with Session(engine) as session:
            expire_sources(session)
            existing = find_digest(session, digest)
            if existing:
                return JSONResponse({**existing.view(), "duplicate": True}, status_code=200)
        try:
            if media_type == "application/json":
                if len(data) > MAX_TEXT:
                    raise ParseError("Structured invoice is too large.")
                extracted = Invoice.model_validate_json(data)
                source = DecodedDocument(data.decode("utf-8"), "structured")
            else:
                source = decode_source(data, media_type, ocr)
                observed: set[str] = set()
                extracted = extract_invoice(source.text, observed_fields=observed)
                if extractor is not None:
                    extracted = extractor.fill_missing(source.text, extracted, observed)
        except ParseError as error:
            raise HTTPException(422, str(error)) from error
        except (ValidationError, UnicodeDecodeError) as error:
            raise HTTPException(422, "The JSON document does not match the invoice schema.") from error
        warnings = review_issues(extracted, rules)
        if source.method == "digital_pdf_partial":
            warnings.append("Some pages have no readable text. Enable OCR or verify the missing pages separately.")
        note = model.suggest(extracted) if model is not None and warnings else None
        with Session(engine) as session:
            document, duplicate = save_upload(session, digest, filename, extracted, note, source, warnings,
                                              original=data, media_type=media_type, retention_days=retention_days)
            return JSONResponse({**document.view(), "duplicate": duplicate}, status_code=200 if duplicate else 201)

    @api.post("/documents")
    async def upload(request: Request) -> JSONResponse:
        kind = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if kind not in {"application/pdf", "text/plain", "application/json"}:
            raise HTTPException(415, "Upload a PDF, UTF-8 text or structured JSON invoice.")
        filename = unquote(request.headers.get("X-Filename", "")).replace("\\", "/").rsplit("/", 1)[-1][:150]
        if not filename or not filename.isprintable():
            raise HTTPException(422, "Supply a valid filename.")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Invoices must be 2 MiB or smaller.")
            data.extend(chunk)
        return await run_in_threadpool(ingest, bytes(data), filename, kind)

    @api.post("/documents/{document_id}/review")
    def review(document_id: UUID, correction: Correction) -> dict:
        with Session(engine) as session:
            document = session.get(Document, str(document_id))
            if document is None:
                raise HTTPException(404, "Document not found.")
            if document.status != "needs_review" or document.version != correction.version:
                raise HTTPException(409, "This review has changed. Reload the document.")
            issues = review_issues(correction.invoice, rules)
            if issues:
                raise HTTPException(422, {"issues": issues})
            changed = save_review(session, document, correction.invoice, correction.version)
            if changed is None:
                raise HTTPException(409, "This review has changed. Reload the document.")
            return changed.view()

    @api.get("/documents/{document_id}/export")
    def export(document_id: UUID, format: Literal["json", "csv"] = "json") -> Response:
        with Session(engine) as session:
            document = session.get(Document, str(document_id))
            if document is None:
                raise HTTPException(404, "Document not found.")
            if document.status != "validated":
                raise HTTPException(409, "Validate the document before exporting.")
            invoice = document.invoice
        headers = {"Content-Disposition": f'attachment; filename="invoice-{document_id}.{format}"'}
        if format == "json":
            return JSONResponse(invoice, headers=headers)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        fields = ["document_type", "supplier", "customer", "invoice_number", "invoice_date", "currency",
                  "subtotal", "tax", "total"]
        line_fields = ["description", "quantity", "unit_price", "amount"]
        writer.writerow([*fields, *line_fields])
        for item in invoice["items"]:
            writer.writerow([_csv_cell(invoice.get(key)) for key in fields] + [_csv_cell(item[key]) for key in line_fields])
        return Response(stream.getvalue(), media_type="text/csv", headers=headers)

    @app.middleware("http")
    async def response_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(api)
    if web_path and web_path.is_dir():
        app.mount("/", StaticFiles(directory=web_path, html=True), name="web")
    return app


def _csv_cell(value: str | None) -> str:
    if value is None:
        return ""
    return "'" + value if re.match(r"^[\s\ufeff]*[=+@-]", value) else value


def from_env() -> FastAPI:
    demo = os.getenv("DEMO_MODE") == "true"
    token = os.getenv("OPERATOR_TOKEN") or (DEMO_TOKEN if demo else "")
    key = os.getenv("MODEL_API_KEY")
    model = None
    extractor = None
    if key:
        endpoint = os.getenv("MODEL_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        name = os.getenv("MODEL_NAME")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not name:
            raise ValueError("Set MODEL_NAME and use an HTTPS model endpoint without URL credentials.")
        model = ReviewModel(endpoint, key, name)
        if os.getenv("MODEL_EXTRACTION_ENABLED") == "true":
            extractor = InvoiceModel(endpoint, key, name)
    elif os.getenv("MODEL_EXTRACTION_ENABLED") == "true":
        raise ValueError("Model extraction requires MODEL_API_KEY and MODEL_NAME.")
    ocr = None
    if os.getenv("OCR_ENABLED") == "true":
        ocr = TesseractOcr.configure(
            os.getenv("OCR_RENDERER", "pdftoppm"), os.getenv("OCR_RECOGNIZER", "tesseract"),
            os.getenv("OCR_LANGUAGE", "eng"),
        )
    currencies = frozenset(os.getenv("INVOICE_CURRENCIES", "").upper().replace(",", " ").split())
    if not currencies.issubset(get_args(Currency)):
        raise ValueError("INVOICE_CURRENCIES includes an unsupported currency code.")
    return create_app(
        url=os.getenv("DATABASE_URL", "sqlite:///data/docflow.db"), token=token, demo=demo, model=model,
        ocr=ocr, rules=ReviewRules(os.getenv("REQUIRE_CUSTOMER") == "true", currencies),
        extractor=extractor,
        retention_days=int(os.getenv("SOURCE_RETENTION_DAYS", "7")),
        web_path=Path(__file__).resolve().parent.parent / "web" / "dist",
    )
