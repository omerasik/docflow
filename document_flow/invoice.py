from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=9, decimal_places=3, allow_inf_nan=False)]
Label = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f]+$")]
Currency = Literal["EUR", "USD", "GBP", "TRY", "CAD", "AUD"]
cent = Decimal("0.01")


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: Label | None = None
    quantity: Quantity | None = None
    unit_price: Money | None = None
    amount: Money | None = None


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    supplier: Label | None = None
    invoice_number: Label | None = None
    invoice_date: date | None = None
    currency: Currency | None = None
    subtotal: Money | None = None
    tax: Money | None = None
    total: Money | None = None
    items: list[LineItem] = Field(default_factory=list, max_length=60)


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=2_147_483_646)
    invoice: Invoice


def extract_invoice(text: str) -> Invoice:
    labels = {
        "supplier": "supplier",
        "invoice number": "invoice_number",
        "invoice date": "invoice_date",
        "currency": "currency",
        "subtotal": "subtotal",
        "tax": "tax",
        "total": "total",
    }
    raw: dict[str, str | None] = {}
    items: list[LineItem] = []
    in_items = False
    for line in text.splitlines():
        line = line.strip()
        if line.casefold() == "items:":
            in_items = True
            continue
        if in_items and "|" in line and len(items) < 60:
            cells = [cell.strip() or None for cell in line.split("|")]
            if len(cells) == 4:
                fields = dict(zip(("description", "quantity", "unit_price", "amount"), cells, strict=True))
                items.append(LineItem.model_validate(_with_invalid_fields_dropped(LineItem, fields)))
            continue
        key, separator, value = line.partition(":")
        field = labels.get(key.strip().casefold()) if separator else None
        if field:
            candidate = value.strip() or None
            raw[field] = candidate if field not in raw or candidate == raw[field] else None
    return Invoice.model_validate({**_with_invalid_fields_dropped(Invoice, raw), "items": items})


def _with_invalid_fields_dropped(model: type[BaseModel], raw: dict[str, str | None]) -> dict[str, str | None]:
    try:
        model.model_validate(raw)
    except ValidationError as error:
        invalid = {entry["loc"][0] for entry in error.errors()}
        return {key: value for key, value in raw.items() if key not in invalid}
    return raw


def review_issues(invoice: Invoice) -> list[str]:
    issues = [
        f"Missing {field.replace('_', ' ')}."
        for field in ("supplier", "invoice_number", "invoice_date", "currency", "subtotal", "tax", "total")
        if getattr(invoice, field) is None
    ]
    if not invoice.items:
        issues.append("At least one line item is required.")
    for index, item in enumerate(invoice.items, start=1):
        if any(value is None for value in (item.description, item.quantity, item.unit_price, item.amount)):
            issues.append(f"Line {index} has missing fields.")
        if item.quantity is not None and item.unit_price is not None and item.amount is not None:
            amount = (item.quantity * item.unit_price).quantize(cent, rounding=ROUND_HALF_UP)
            if amount != item.amount:
                issues.append(f"Line {index} amount should equal quantity × unit price ({amount}).")

    amounts = [item.amount for item in invoice.items]
    if invoice.items and all(amount is not None for amount in amounts):
        line_sum = sum((amount for amount in amounts if amount is not None), Decimal(0))
        if invoice.subtotal is not None and line_sum != invoice.subtotal:
            issues.append(f"Line amounts sum to {line_sum:.2f} and differ from subtotal.")
    if invoice.subtotal is not None and invoice.tax is not None and invoice.total is not None:
        expected = invoice.subtotal + invoice.tax
        if expected != invoice.total:
            issues.append(f"Subtotal plus tax is {expected:.2f} and differs from total.")
    return issues
