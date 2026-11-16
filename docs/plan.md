# Implementation plan

1. Define the bounded upload contract and deterministic invoice schema.
2. Implement PDF and UTF-8 extraction, exact money checks, versioned review and export.
3. Serve a review queue and editable invoice in React, with an optional model-generated review note.
4. Verify the workflow with synthetic PDF and text invoices, a browser export, and CI against PostgreSQL.
