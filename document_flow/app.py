import csv
import hashlib
import hmac
import io
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .invoice import Correction, extract_invoice, review_issues
from .model import ReviewModel
from .parser import MAX_BYTES, ParseError, decode_document
from .storage import Base, Document, database, find_digest, save_review, save_upload

DEMO_TOKEN = "docflow-local-demo"


def create_app(
    *, url: str, token: str, demo: bool = False, model: ReviewModel | None = None,
    web_path: Path | None = None,
) -> FastAPI:
    if not demo and len(token) < 32:
        raise ValueError("Set OPERATOR_TOKEN to a random token of at least 32 characters.")
    engine = database(url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        Base.metadata.create_all(engine)
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="DocFlow", lifespan=lifespan)

    def authorize(request: Request) -> None:
        supplied = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
            raise HTTPException(401, "Operator access required.")

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @app.get("/api/config")
    def demo_status() -> dict[str, bool]:
        return {"demo_access": demo and token == DEMO_TOKEN}

    @api.get("/documents")
    def list_documents() -> list[dict]:
        with Session(engine) as session:
            documents = session.scalars(select(Document).order_by(Document.created_at.desc()).limit(100))
            return [document.view() for document in documents]

    def ingest(data: bytes, filename: str, media_type: str) -> JSONResponse:
        digest = hashlib.sha256(data).hexdigest()
        with Session(engine) as session:
            existing = find_digest(session, digest)
            if existing:
                return JSONResponse({**existing.view(), "duplicate": True}, status_code=200)
        try:
            extracted = extract_invoice(decode_document(data, media_type))
        except ParseError as error:
            raise HTTPException(422, str(error)) from error
        note = model.suggest(extracted) if model is not None and review_issues(extracted) else None
        with Session(engine) as session:
            document, duplicate = save_upload(session, digest, filename, extracted, note)
            return JSONResponse({**document.view(), "duplicate": duplicate}, status_code=200 if duplicate else 201)

    @api.post("/documents")
    async def upload(request: Request) -> JSONResponse:
        kind = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if kind not in {"application/pdf", "text/plain"}:
            raise HTTPException(415, "Upload a PDF or UTF-8 text invoice.")
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
            issues = review_issues(correction.invoice)
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
        fields = ["supplier", "invoice_number", "invoice_date", "currency", "subtotal", "tax", "total"]
        line_fields = ["description", "quantity", "unit_price", "amount"]
        writer.writerow([*fields, *line_fields])
        for item in invoice["items"]:
            writer.writerow([_csv_cell(invoice[key]) for key in fields] + [_csv_cell(item[key]) for key in line_fields])
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


def _csv_cell(value: str) -> str:
    return "'" + value if re.match(r"^[\s\ufeff]*[=+@-]", value) else value


def from_env() -> FastAPI:
    demo = os.getenv("DEMO_MODE") == "true"
    token = os.getenv("OPERATOR_TOKEN") or (DEMO_TOKEN if demo else "")
    key = os.getenv("MODEL_API_KEY")
    model = None
    if key:
        endpoint = os.getenv("MODEL_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        name = os.getenv("MODEL_NAME")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not name:
            raise ValueError("Set MODEL_NAME and use an HTTPS model endpoint without URL credentials.")
        model = ReviewModel(endpoint, key, name)
    return create_app(
        url=os.getenv("DATABASE_URL", "sqlite:///data/docflow.db"), token=token, demo=demo, model=model,
        web_path=Path(__file__).resolve().parent.parent / "web" / "dist",
    )
