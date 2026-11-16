# Architecture

A React application uses a same-origin FastAPI service. A streamed upload is capped at 2 MiB before parsing in a worker thread. The parser extracts digital text from up to eight unencrypted PDF pages. When OCR is enabled, bounded Poppler and Tesseract subprocesses read pages with no text layer. UTF-8 input goes through the invoice interpreter and structured JSON goes through Pydantic directly. Conflicting and invalid fields remain blank for review.

The Pydantic model bounds each field and stores monetary values as decimal strings. The validation service checks line amounts after rounding to cents, the subtotal against line amounts, and the total against subtotal plus tax. A new invoice always requires manual review. SQLAlchemy writes validated values and an audit entry in one transaction, guarded by the expected document version.

PostgreSQL is the server database. The same SQLAlchemy repository runs against SQLite for the single-process demo. A hash of the uploaded bytes prevents duplicate document records. The record and its extracted source text are inserted in the same transaction. Original files are discarded. The authenticated preview reads the source table separately. Metadata search uses bound text filters over the filename and invoice fields.

The default review path runs offline. An optional review note uses existing validation issues. A separate opt-in extractor receives source text and proposes schema-bound fields and line items. It preserves observed fields and drops suggested values without textual support. A source match cannot establish that a model mapped a value to the correct field, so the operator still validates the record before export. The CSV export escapes text that could be interpreted as a spreadsheet formula.
