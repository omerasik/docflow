"""Verify the local demo API, including real OCR and corrected exports."""

import argparse
import json
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8012")
    args = parser.parse_args()
    origin = args.url.rstrip("/")
    if urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The smoke test only accepts a local demo server.")

    def call(path: str, data: bytes | None = None, **headers) -> bytes:
        request = Request(origin + "/api/" + path, data=data,
                          headers={"Authorization": "Bearer docflow-local-demo", **headers})
        with urlopen(request, timeout=60) as response:
            return response.read()

    for attempt in range(30):
        try:
            call("health")
            break
        except URLError:
            if attempt == 29:
                raise
            time.sleep(1)
    scan = Path(__file__).resolve().parent.parent / "samples" / "scanned-invoice.pdf"
    document = json.loads(call("documents", scan.read_bytes(), **{
        "Content-Type": "application/pdf", "X-Filename": "scanned-invoice.pdf",
    }))
    path = "documents/" + document["id"]
    assert json.loads(call(path + "/source"))["method"] == "ocr_pdf"
    assert document["invoice"]["supplier"] == "Amber Field Studio"
    assert len(document["invoice"]["items"]) == 2
    if document["status"] == "needs_review":
        assert document["warnings"] == ["Subtotal plus tax is 350.90 and differs from total."]
        document["invoice"]["total"] = "350.90"
        result = json.loads(call(path + "/review", json.dumps({
            "version": document["version"], "invoice": document["invoice"],
        }).encode(), **{"Content-Type": "application/json"}))
        assert result["status"] == "validated"
    assert json.loads(call(path + "/export"))["total"] == "350.90"
    assert b"Design consultation" in call(path + "/export?format=csv")
    print("PASS: native OCR upload, source preview, decimal correction, validation and JSON/CSV exports")


if __name__ == "__main__":
    main()
