import os
import shutil
from collections.abc import Iterator
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from document_flow.app import create_app
from document_flow.extraction import CandidateInvoice, InvoiceModel
from document_flow.invoice import Invoice, ReviewRules, extract_invoice, review_issues
from document_flow.model import ReviewModel
from document_flow.ocr import TesseractOcr
from document_flow.parser import MAX_BYTES, ParseError, decode_source
from document_flow.storage import Document
from samples.generate_scan import scanned_pdf

token = "document-operator".ljust(35, "x")
auth = {"Authorization": f"Bearer {token}"}
text = """Supplier: Example Studio
Invoice Number: ST-0042
Invoice Date: 2026-11-14
Currency: EUR
Subtotal: 290.00
Tax: 60.90
Total: 349.00
Items:
Consulting | 2 | 95.00 | 190.00
Artwork | 1 | 100.00 | 100.00"""


@pytest.fixture
def client() -> Iterator[TestClient]:
    if server_url := os.getenv("DOCFLOW_TEST_DATABASE_URL"):
        schema = "docflow_test_" + uuid4().hex
        engine = create_engine(server_url)
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
        url = make_url(server_url).update_query_dict({"options": f"-csearch_path={schema}"})
        try:
            with TestClient(create_app(url=url.render_as_string(hide_password=False), token=token)) as session:
                yield session
        finally:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
            engine.dispose()
    else:
        with TemporaryDirectory() as directory:
            url = "sqlite:///" + (Path(directory) / "docs.db").as_posix()
            with TestClient(create_app(url=url, token=token)) as session:
                yield session


def upload(client: TestClient, data: bytes = text.encode(), kind: str = "text/plain") -> httpx.Response:
    return client.post("/api/documents", content=data, headers={
        **auth, "Content-Type": kind, "X-Filename": "sample",
    })


def sample_pdf(data: str) -> bytes:
    target = BytesIO()
    pdf = Canvas(target)
    cursor = pdf.beginText(45, 740)
    for line in data.splitlines():
        cursor.textLine(line)
    pdf.drawText(cursor)
    pdf.save()
    return target.getvalue()


def test_pdf_review_and_export(client: TestClient):
    uploaded = upload(client, sample_pdf(text), "application/pdf")
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()
    assert document["status"] == "needs_review"
    assert document["invoice"]["supplier"] == "Example Studio"
    assert document["warnings"] == ["Subtotal plus tax is 350.90 and differs from total."]
    path = "/api/documents/" + document["id"]
    assert client.get(path + "/export", headers=auth).status_code == 409
    correction = {"version": document["version"], "invoice": document["invoice"]}
    assert client.post(path + "/review", json=correction, headers=auth).status_code == 422
    correction["invoice"]["total"] = "350.90"
    result = client.post(path + "/review", json=correction, headers=auth)
    assert result.status_code == 200, result.text
    assert result.json()["version"] == 2
    assert [entry["action"] for entry in result.json()["audit"]] == ["Extracted", "Validated"]
    assert client.post(path + "/review", json=correction, headers=auth).status_code == 409
    assert client.get(path + "/export", headers=auth).json()["total"] == "350.90"
    csv = client.get(path + "/export?format=csv", headers=auth)
    assert csv.status_code == 200
    assert "Example Studio" in csv.text
    assert "Consulting" in csv.text


def test_replay_missing_fields_and_authorization(client: TestClient):
    assert client.get("/api/config").json() == {"demo_access": False}
    assert client.get("/api/documents").status_code == 401
    doc = upload(client, b"Supplier: First\nSupplier: Second").json()
    assert doc["invoice"]["supplier"] is None
    assert doc["invoice"]["tax"] is None
    assert any("Missing tax" in warning for warning in doc["warnings"])
    replay = upload(client, b"Supplier: First\nSupplier: Second")
    assert replay.status_code == 200
    assert replay.json()["id"] == doc["id"]
    assert replay.json()["duplicate"] is True


def test_rejects_oversized_invalid_and_unsupported_uploads(client: TestClient):
    assert upload(client, b"X" * (MAX_BYTES + 1)).status_code == 413
    assert upload(client, b"not a pdf", "application/pdf").status_code == 422
    assert upload(client, b"\xff").status_code == 422
    assert upload(client, b"whatever", "application/octet-stream").status_code == 415


def test_review_issues_decimal_and_csv_formula_protection(client: TestClient):
    invoice = Invoice.model_validate({
        "supplier": "Sample", "invoice_number": "C-1", "invoice_date": "2026-10-01",
        "currency": "EUR", "subtotal": "1.01", "tax": "0", "total": "1.01",
        "items": [{"description": "Rounding check", "quantity": "0.5", "unit_price": "2.01", "amount": "1.01"}],
    })
    assert review_issues(invoice) == []
    doc = upload(client).json()
    invoice.supplier = "=2+2"
    path = "/api/documents/" + doc["id"]
    assert client.post(path + "/review", headers=auth, json={
        "version": 1, "invoice": invoice.model_dump(mode="json"),
    }).status_code == 200
    assert "'=2+2" in client.get(path + "/export?format=csv", headers=auth).text


def test_review_model_receives_issues_and_fails_without_changing_invoice(monkeypatch: pytest.MonkeyPatch):
    invoice = Invoice(supplier="Synthetic confidential supplier")
    url = "https://model.invalid/classify"
    model = ReviewModel(url, "test-key", "test-model")

    def respond(endpoint: str, **kwargs) -> httpx.Response:
        assert endpoint == url
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 8
        messages = kwargs["json"]["messages"]
        assert "Synthetic confidential supplier" not in messages[1]["content"]
        assert "Missing invoice number" in messages[1]["content"]
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": "Check the invoice number."}}],
        })

    def throttle(endpoint: str, **kwargs) -> httpx.Response:
        return httpx.Response(429, request=httpx.Request("POST", endpoint))

    monkeypatch.setattr(httpx, "post", respond)
    assert model.suggest(invoice) == "Check the invoice number."
    monkeypatch.setattr(httpx, "post", throttle)
    assert model.suggest(invoice) is None
    assert invoice.invoice_number is None


@pytest.mark.parametrize(("source", "expected", "issue_count"), [
    (text, {"supplier": "Example Studio", "customer": None, "invoice_number": "ST-0042", "currency": "EUR",
            "total": "349.00", "invoice_date": "2026-11-14", "document_type": "invoice"}, 1),
    ("Vendor = Leaf Works\nBill To: Riverside\nInvoice No.: L-41\nIssued On: 15.11.2026\n"
     "Currency: eur\nNet Amount: 1.280,00\nVAT: 160,00\nAmount Due: 1.440,00\nLine Items:\n"
     "Description | Qty | Unit price | Amount\nDesign | 2 | 640,00 | 1.280,00",
     {"supplier": "Leaf Works", "customer": "Riverside", "invoice_number": "L-41", "currency": "EUR",
      "total": "1440.00", "invoice_date": "2026-11-15", "document_type": "invoice"}, 0),
    ("Buyer: Northwind\nInvoice ID: J-41\nIssue Date: 2026/11/12\nCurrency: USD\nTax: invalid",
     {"supplier": None, "customer": "Northwind", "invoice_number": "J-41", "currency": "USD",
      "total": None, "invoice_date": "2026-11-12", "document_type": "invoice"}, 5),
])
def test_synthetic_layout_reference_fields(source: str, expected: dict, issue_count: int):
    invoice = extract_invoice(source)
    actual = invoice.model_dump(mode="json")
    assert {field: actual[field] for field in expected} == expected
    assert len(review_issues(invoice)) == issue_count


def test_structured_upload_search_source_and_customer_rules(client: TestClient):
    invoice = extract_invoice(text).model_copy(update={"customer": "Riverside_25"})
    response = upload(client, invoice.model_dump_json().encode(), "application/json")
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["invoice"]["customer"] == "Riverside_25"
    assert upload(client, b'{"unknown":"field"}', "application/json").status_code == 422
    assert review_issues(invoice, ReviewRules(currencies=frozenset({"USD"})))[-2].startswith("Currency EUR")
    for query in ["riverside_25", "eXamPle sTudio", "0042"]:
        assert [doc["id"] for doc in client.get("/api/documents", params={"q": query}, headers=auth).json()] == [item["id"]]
    assert client.get("/api/documents", params={"q": "%"}, headers=auth).json() == []
    source_path = f"/api/documents/{item['id']}/source"
    assert client.get(source_path).status_code == 401
    assert client.get(source_path, headers=auth).json() == {
        "method": "structured", "text": invoice.model_dump_json(), "duplicate_candidates": []
    }


def test_finds_duplicates_across_layouts_and_updates_identity_after_review(client: TestClient):
    original = text.replace("Supplier: Example Studio", "Supplier: Été Studio")
    alternate = text.replace("Supplier: Example Studio", "Vendor: E\u0301TE\u0301  STUDIO")
    first = upload(client, original.encode()).json()
    second = upload(client, alternate.encode()).json()
    assert first["id"] != second["id"]
    path = f"/api/documents/{first['id']}/source"
    assert client.get(path, headers=auth).json()["duplicate_candidates"] == [
        {"id": second["id"], "filename": "sample", "status": "needs_review"}
    ]
    reviewed = {**second["invoice"], "invoice_number": "ST-0080", "total": "350.90"}
    result = client.post(f"/api/documents/{second['id']}/review", headers=auth,
                         json={"version": second["version"], "invoice": reviewed})
    assert result.status_code == 200
    assert client.get(path, headers=auth).json()["duplicate_candidates"] == []


def test_populates_identity_for_legacy_documents():
    with TemporaryDirectory() as directory:
        url = "sqlite:///" + (Path(directory) / "legacy.db").as_posix()
        engine = create_engine(url)
        Document.__table__.create(engine)
        identifier = str(uuid4())
        with Session(engine) as session:
            session.add(Document(id=identifier, digest="a" * 64, filename="legacy.txt", status="needs_review",
                                 version=1, invoice=extract_invoice(text).model_dump(mode="json"), warnings=[],
                                 audit=[], created_at="2026-10-01T12:00:00+00:00"))
            session.commit()
        engine.dispose()
        with TestClient(create_app(url=url, token=token)) as client:
            latest = upload(client).json()
            result = client.get(f"/api/documents/{latest['id']}/source", headers=auth)
            assert result.json()["duplicate_candidates"][0]["id"] == identifier
            assert client.get(f"/api/documents/{identifier}/source", headers=auth).json()["text"] is None


def test_ocr_boundary_for_image_only_document():
    class LocalOcr:
        def read_pages(self, _pdf: bytes, pages: list[int]) -> dict[int, str]:
            assert pages == [1]
            return {1: text}

    pdf = scanned_pdf()
    with pytest.raises(ParseError, match="selectable text"):
        decode_source(pdf, "application/pdf")
    extracted = decode_source(pdf, "application/pdf", LocalOcr())
    assert extracted.method == "ocr_pdf"
    assert extract_invoice(extracted.text).supplier == "Example Studio"


def test_real_ocr_synthetic_scan():
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        if os.getenv("DOCFLOW_REQUIRE_OCR_TEST") == "true":
            pytest.fail("The CI OCR executables are not installed.")
        pytest.skip("pdftoppm and tesseract are required")
    extracted = decode_source(scanned_pdf(), "application/pdf", TesseractOcr.configure("pdftoppm", "tesseract", "eng"))
    invoice = extract_invoice(extracted.text)
    assert extracted.method == "ocr_pdf"
    assert invoice.supplier == "Amber Field Studio"
    assert invoice.invoice_number == "AF-2026-042"
    assert invoice.model_dump(mode="json")["total"] == "349.90"
    assert len(invoice.items) == 2
    assert review_issues(invoice) == ["Subtotal plus tax is 350.90 and differs from total."]


def test_structured_model_never_replaces_confirmed_or_invents_absent_values(monkeypatch: pytest.MonkeyPatch):
    candidate = CandidateInvoice(fields={
        "supplier": "Source Customer", "customer": "Source Customer", "total": "349.00", "invoice_number": "GUESS",
    }, items=[{"description": "Artwork", "quantity": "1", "unit_price": "100.00", "amount": "100.00"}])

    def respond(method: str, endpoint: str, **kwargs):
        assert method == "POST"
        assert kwargs["follow_redirects"] is False
        assert kwargs["json"]["messages"][0]["role"] == "system"
        return nullcontext(httpx.Response(200, request=httpx.Request(method, endpoint), json={
            "choices": [{"finish_reason": "stop", "message": {"content": candidate.model_dump_json()}}]
        }))

    monkeypatch.setattr(httpx, "stream", respond)
    result = InvoiceModel("https://model.invalid", "test-key", "test-model").fill_missing(
        "Source Customer\nAmount 1349.00\nArtwork | 1 | 100.00 | 100.00", Invoice(supplier="Verified Vendor"),
    )
    assert result.supplier == "Verified Vendor"
    assert result.customer == "Source Customer"
    assert result.total is None
    assert result.invoice_number is None
    assert len(result.items) == 1
