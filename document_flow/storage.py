from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, create_engine, func, or_, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .invoice import Invoice
from .parser import DecodedDocument


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer)
    invoice: Mapped[dict] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON)
    audit: Mapped[list] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[str] = mapped_column(String(40), index=True)

    def view(self) -> dict:
        return {
            "id": self.id, "filename": self.filename, "status": self.status, "version": self.version,
            "invoice": self.invoice, "warnings": self.warnings, "audit": self.audit,
            "note": self.note, "created_at": self.created_at,
        }


class DocumentSource(Base):
    __tablename__ = "document_sources"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    method: Mapped[str] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(Text)


def database(url: str) -> Engine:
    path = make_url(url)
    if path.get_backend_name() == "sqlite" and path.database and path.database != ":memory:":
        Path(path.database).resolve().parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, pool_pre_ping=True)


def audit_entry(action: str) -> dict[str, str]:
    return {"action": action, "at": datetime.now(UTC).isoformat()}


def find_digest(session: Session, digest: str) -> Document | None:
    return session.scalar(select(Document).where(Document.digest == digest))


def list_recent(session: Session, query: str) -> list[Document]:
    candidates = select(Document).order_by(Document.created_at.desc(), Document.id).limit(100)
    if query:
        columns = [Document.filename, *(Document.invoice[field].as_string()
                    for field in ("supplier", "customer", "invoice_number", "document_type"))]
        candidates = candidates.where(or_(*(func.lower(column).contains(query.lower(), autoescape=True)
                                           for column in columns)))
    return list(session.scalars(candidates))


def save_upload(
    session: Session, digest: str, filename: str, invoice: Invoice, note: str | None,
    source: DecodedDocument, warnings: list[str],
) -> tuple[Document, bool]:
    uploaded = audit_entry("Extracted")
    document = Document(
        id=str(uuid4()), digest=digest, filename=filename, status="needs_review", version=1,
        invoice=invoice.model_dump(mode="json"), warnings=warnings,
        audit=[uploaded], created_at=uploaded["at"], note=note,
    )
    session.add(document)
    try:
        session.flush()
        session.add(DocumentSource(document_id=document.id, method=source.method, text=source.text))
        session.commit()
        return document, False
    except IntegrityError:
        session.rollback()
        existing = find_digest(session, digest)
        if existing is None:
            raise
        return existing, True


def save_review(session: Session, document: Document, invoice: Invoice, version: int) -> Document | None:
    changed = session.execute(
        update(Document).where(
            Document.id == document.id, Document.version == version, Document.status == "needs_review",
        ).values(
            version=version + 1, status="validated", invoice=invoice.model_dump(mode="json"),
            warnings=[], audit=[*document.audit, audit_entry("Validated")],
        ).returning(Document)
    ).scalar_one_or_none()
    session.commit()
    return changed
