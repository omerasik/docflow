import os
from collections.abc import Iterator
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

from document_flow.app import create_app
from document_flow.invoice import Invoice, review_issues
from document_flow.model import ReviewModel
from document_flow.parser import MAX_BYTES

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
