"""Generate an image-only invoice; requires the development dependencies."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas


def scanned_pdf() -> bytes:
    source = Path(__file__).with_name("invoice.txt").read_text(encoding="utf-8")
    image = Image.new("RGB", (1600, 1200), "white")
    ImageDraw.Draw(image).multiline_text(
        (40, 35), source, fill="black", font=ImageFont.load_default(size=44), spacing=20,
    )
    target = BytesIO()
    pdf = Canvas(target)
    pdf.drawImage(ImageReader(image), 35, 330, width=525, height=394)
    pdf.save()
    return target.getvalue()


if __name__ == "__main__":
    destination = Path(__file__).with_name("scanned-invoice.pdf")
    destination.write_bytes(scanned_pdf())
    print(destination)
