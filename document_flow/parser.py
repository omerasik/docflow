from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 8
MAX_TEXT = 24_000


class ParseError(ValueError):
    pass


def decode_document(body: bytes, media_type: str) -> str:
    if media_type == "text/plain":
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ParseError("Text documents must use UTF-8.") from error
    elif media_type == "application/pdf":
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
            text = "\n".join(parts)
        except (PdfReadError, RecursionError, ValueError, TypeError, KeyError, OSError) as error:
            if isinstance(error, ParseError):
                raise
            raise ParseError("This PDF could not be read.") from error
    else:
        raise ParseError("Upload a PDF or UTF-8 text invoice.")
    if len(text) > MAX_TEXT:
        raise ParseError("Extracted content is too long.")
    if not text.strip() or "\x00" in text:
        raise ParseError("The invoice must contain selectable text.")
    return text
