import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


class OcrError(ValueError):
    pass


@dataclass(frozen=True)
class TesseractOcr:
    renderer: str
    recognizer: str
    language: str = "eng"

    @classmethod
    def configure(cls, renderer: str, recognizer: str, language: str) -> "TesseractOcr":
        render_path = shutil.which(renderer)
        recognize_path = shutil.which(recognizer)
        if not render_path or not recognize_path:
            raise ValueError("OCR requires pdftoppm and Tesseract on PATH or configured executable paths.")
        if not re.fullmatch(r"[a-z]{3}(?:\+[a-z]{3}){0,2}", language):
            raise ValueError("Set OCR_LANGUAGE to one to three Tesseract language codes.")
        return cls(render_path, recognize_path, language)

    def read_pages(self, pdf: bytes, pages: list[int]) -> dict[int, str]:
        deadline = time.monotonic() + 40
        found: dict[int, str] = {}
        with TemporaryDirectory(prefix="docflow-ocr-") as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(pdf)
            prefix = str(Path(directory) / "page")
            for page in pages:
                self._run([
                    self.renderer, "-f", str(page), "-l", str(page), "-singlefile", "-gray",
                    "-scale-to", "2400", str(source), prefix,
                ], deadline)
                result = self._run([self.recognizer, f"{prefix}.pgm", "stdout", "-l", self.language, "--psm", "6"], deadline)
                found[page] = result.decode("utf-8", errors="replace")
        return found

    @staticmethod
    def _run(args: list[str], deadline: float) -> bytes:
        timeout = min(12, deadline - time.monotonic())
        if timeout <= 0:
            raise OcrError("OCR reached its processing deadline.")
        try:
            result = subprocess.run(args, check=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as error:
            raise OcrError("OCR reached its processing deadline.") from error
        except (OSError, subprocess.CalledProcessError) as error:
            raise OcrError("This PDF could not be processed by OCR.") from error
        return result.stdout
