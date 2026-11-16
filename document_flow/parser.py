from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .ocr import OcrError

MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 8
MAX_TEXT = 24_000


class ParseError(ValueError):
    pass


class OcrEngine(Protocol):
    def read_pages(self, pdf: bytes, pages: list[int]) -> dict[int, str]: ...


@dataclass(frozen=True)
class DecodedDocument:
    text: str
    method: str


def decode_document(body: bytes, media_type: str) -> str:
    return decode_source(body, media_type).text


def decode_source(body: bytes, media_type: str, ocr: OcrEngine | None = None) -> DecodedDocument:
    method = "text"
    if media_type == "text/plain":
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ParseError("Text documents must use UTF-8.") from error
    elif media_type == "application/pdf":
        method = "digital_pdf"
        if not body.startswith(b"%PDF-"):
            raise ParseError("The file is not a PDF.")
        try:
            reader = PdfReader(BytesIO(body), strict=True)
            if reader.is_encrypted:
                raise ParseError("Encrypted PDFs are not supported.")
            if len(reader.pages) > MAX_PAGES:
                raise ParseError(f"PDFs may contain at most {MAX_PAGES} pages.")
            parts = []
            length = 0
            for page in reader.pages:
                part = page.extract_text() or ""
                length += len(part)
                if length > MAX_TEXT:
                    raise ParseError("Extracted content is too long.")
                parts.append(part)
        except (PdfReadError, RecursionError, ValueError, TypeError, KeyError, OSError) as error:
            if isinstance(error, ParseError):
                raise
            raise ParseError("This PDF could not be read.") from error
        missing = [page for page, part in enumerate(parts, start=1) if not part.strip()]
        if missing and ocr is not None:
            try:
                recognized = ocr.read_pages(body, missing)
            except OcrError as error:
                raise ParseError(str(error)) from error
            for page in missing:
                parts[page - 1] = recognized.get(page, "")
            method = "ocr_pdf"
        elif missing:
            method = "digital_pdf_partial"
        text = "\n".join(parts)
    else:
        raise ParseError("Upload a PDF or UTF-8 text invoice.")
    if len(text) > MAX_TEXT:
        raise ParseError("Extracted content is too long.")
    if not text.strip() or "\x00" in text:
        raise ParseError("The invoice must contain selectable text.")
    return DecodedDocument(text, method)
