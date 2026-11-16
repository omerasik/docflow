# Architecture

A React application uses a same-origin FastAPI service. A streamed upload is capped at 2 MiB before parsing in a worker thread. PDF input must be unencrypted and contain selectable text within eight pages. UTF-8 text is also supported. The extractor reads fixed field labels and a delimited item table. Conflicting and invalid fields remain blank for review.

The Pydantic model bounds each field and stores monetary values as decimal strings. The validation service checks line amounts after rounding to cents, the subtotal against line amounts, and the total against subtotal plus tax. A new invoice always requires manual review. SQLAlchemy writes validated values and an audit entry in one transaction, guarded by the expected document version.

PostgreSQL is the server database. The same SQLAlchemy repository runs against SQLite for the single-process demo. A hash of the uploaded bytes prevents duplicate document records. Original file contents are discarded after extraction. Persisted metadata includes the filename, source hash, extracted or corrected fields, state, audit timestamps and any optional review note.

The default review path runs offline. An opt-in model may summarize existing validation issues. It receives those issue strings and cannot modify invoice fields. The operator must validate before an export becomes available. CSV output escapes text that could be interpreted as a spreadsheet formula.
