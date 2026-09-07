from pathlib import Path
import fitz


def extract_pdf_text(path: Path) -> str:
    with fitz.open(path) as document:
        return "\n".join(page.get_text("text") for page in document[:2]).strip()
