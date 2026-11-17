import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=9, decimal_places=3, allow_inf_nan=False)]
Label = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f]+$")]
Currency = Literal["EUR", "USD", "GBP", "TRY", "CAD", "AUD"]
cent = Decimal("0.01")


@dataclass(frozen=True)
class ReviewRules:
    require_customer: bool = False
    currencies: frozenset[str] = frozenset()


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: Label | None = None
    quantity: Quantity | None = None
    unit_price: Money | None = None
    amount: Money | None = None


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    supplier: Label | None = None
    customer: Label | None = None
    document_type: Literal["invoice"] | None = None
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


def extract_invoice(text: str, *, observed_fields: set[str] | None = None) -> Invoice:
    labels = {
        "supplier": "supplier",
        "vendor": "supplier",
        "seller": "supplier",
        "billed by": "supplier",
        "customer": "customer",
        "bill to": "customer",
        "buyer": "customer",
        "invoice number": "invoice_number",
        "invoice no": "invoice_number",
        "invoice #": "invoice_number",
        "invoice id": "invoice_number",
        "invoice date": "invoice_date",
        "issued on": "invoice_date",
        "issue date": "invoice_date",
        "currency": "currency",
        "subtotal": "subtotal",
        "net amount": "subtotal",
        "tax": "tax",
        "vat": "tax",
        "total": "total",
        "amount due": "total",
        "balance due": "total",
    }
    raw: dict[str, str | None] = {}
    items: list[LineItem] = []
    in_items = False
    for line in text.splitlines():
        line = line.strip()
        if line.casefold().rstrip(":") in {"items", "line items", "details"}:
            in_items = True
            continue
        if in_items and ("|" in line or "\t" in line) and len(items) < 60:
            cells = [cell.strip() or None for cell in re.split(r"\||\t", line)]
            if len(cells) == 4:
                if cells[0] and cells[0].casefold() == "description":
                    continue
                cells[1:] = [_decimal_text(cell) for cell in cells[1:]]
                fields = dict(zip(("description", "quantity", "unit_price", "amount"), cells, strict=True))
                items.append(LineItem.model_validate(_with_invalid_fields_dropped(LineItem, fields)))
            continue
        pair = re.split(r"[:=\t]", line, maxsplit=1)
        field = labels.get(pair[0].strip().casefold().rstrip(".")) if len(pair) == 2 else None
        if field:
            if observed_fields is not None:
                observed_fields.add(field)
            candidate = pair[1].strip() or None
            if field in {"subtotal", "tax", "total"}:
                candidate = _decimal_text(candidate)
            elif field == "currency" and candidate:
                candidate = candidate.upper()
            elif field == "invoice_date" and candidate:
                for layout in ("%Y/%m/%d", "%d.%m.%Y"):
                    try:
                        candidate = datetime.strptime(candidate, layout).date().isoformat()
                        break
                    except ValueError:
                        continue
            raw[field] = candidate if field not in raw or candidate == raw[field] else None
    if "invoice_number" in raw or "invoice_date" in raw:
        raw["document_type"] = "invoice"
    return Invoice.model_validate({**_with_invalid_fields_dropped(Invoice, raw), "items": items})


def _decimal_text(value: str | None) -> str | None:
    if value and re.fullmatch(r"\d+(?:\.\d{3})*,\d{1,2}", value):
        return value.replace(".", "").replace(",", ".")
    if value and re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{1,2}", value):
        return value.replace(",", "")
    return value


def _with_invalid_fields_dropped(model: type[BaseModel], raw: dict[str, str | None]) -> dict[str, str | None]:
    try:
        model.model_validate(raw)
    except ValidationError as error:
        invalid = {entry["loc"][0] for entry in error.errors()}
        return {key: value for key, value in raw.items() if key not in invalid}
    return raw


def review_issues(invoice: Invoice, rules: ReviewRules = ReviewRules()) -> list[str]:
    issues = [
        f"Missing {field.replace('_', ' ')}."
        for field in ("supplier", "invoice_number", "invoice_date", "currency", "subtotal", "tax", "total")
        if getattr(invoice, field) is None
    ]
    if rules.require_customer and invoice.customer is None:
        issues.append("Missing customer.")
    if invoice.currency and rules.currencies and invoice.currency not in rules.currencies:
        issues.append(f"Currency {invoice.currency} is excluded by the configured business rule.")
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
