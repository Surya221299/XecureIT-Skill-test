import re
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
import unicodedata

MAX_PAGES = 12
MAX_TEXT = 200_000

PATTERNS = [
    ("EMAIL", .99, re.compile(r"(?i)\b[\w.+-]+[ \t]*@[ \t]*[\w.-]+[ \t]*\.[ \t]*[a-z]{2,}\b")),
    ("NPWP", .98, re.compile(r"(?i)\b(?:\d{2}\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}|(?<=NPWP: )\d{15,16})\b")),
    ("NIK", .96, re.compile(r"(?i)\b(?:NIK|KTP)\s*[:#-]?\s*(\d{16})\b")),
    ("KARTU", .96, re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")),
    ("NIK", .70, re.compile(r"(?<!\d)\d{16}(?!\d)")),
    ("PHONE", .92, re.compile(r"(?<!\w)(?:\+62|0062|0)[ -]?[2-9](?:[ .-]?\d){7,12}(?!\d)")),
    ("NAMA", .82, re.compile(
        r"(?im)^\s*(?:nama(?: lengkap)?|name)\s*[:=-]\s*([^\n\d@]{2,100})"
    )),
]
CATEGORIES = {
    "financial": ("invoice", "faktur", "rekening", "saldo", "pajak", "transfer", "total"),
    "legal": ("kontrak", "perjanjian", "pasal", "pihak", "hukum", "agreement"),
    "hr": ("karyawan", "pegawai", "rekrutmen", "gaji", "cuti", "employment"),
    "technical": ("api", "server", "deployment", "spesifikasi", "database", "arsitektur"),
}


class ProcessingError(Exception):
    pass


def luhn(value):
    digits = [int(char) for char in value if char.isdecimal()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
        total += digit - 9 if digit > 9 else digit
    return total % 10 == 0


def redact(text):
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n")
    text = "".join(char for char in text if char in "\n\t" or not unicodedata.category(char).startswith("C"))
    spans = []
    for kind, confidence, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(1 if pattern.groups else 0)
            if kind == "KARTU" and not luhn(text[start:end]):
                continue
            spans.append((start, end, kind, confidence))

    # Merge overlaps so that a second detection cannot leave a partial value exposed.
    merged = []
    for start, end, kind, confidence in sorted(spans):
        if merged and merged[-1][1] > start:
            left, right, prior_kind, prior_confidence = merged[-1]
            label = (kind, confidence) if confidence > prior_confidence else (prior_kind, prior_confidence)
            merged[-1] = (left, max(right, end), *label)
        else:
            merged.append((start, end, kind, confidence))
    counts = Counter()
    scores = {}
    cursor = 0
    chunks = []
    for start, end, kind, confidence in merged:
        chunks.extend((text[cursor:start], f"[{kind}]"))
        cursor = end
        counts[kind] += 1
        scores[kind] = max(scores.get(kind, 0), confidence)
    chunks.append(text[cursor:])
    return "".join(chunks).strip(), [(kind, scores[kind], count) for kind, count in counts.items()]


def classify(text, findings):
    words = set(re.findall(r"\w+", text.lower()))
    scores = {kind: len(words.intersection(terms)) for kind, terms in CATEGORIES.items()}
    category = max(scores, key=scores.get)
    if not scores[category]:
        category = "personal"
    kinds = {kind for kind, _, _ in findings}
    if kinds & {"NIK", "NPWP", "KARTU"}:
        return category, "high"
    return category, "medium" if kinds or category in {"legal", "hr", "financial"} else "low"


def command(arguments):
    try:
        return subprocess.run(arguments, check=True, capture_output=True, timeout=12).stdout
    except subprocess.TimeoutExpired:
        raise ProcessingError("OCR_TIMEOUT") from None
    except (subprocess.CalledProcessError, OSError):
        raise ProcessingError("DOCUMENT_UNREADABLE") from None


def extract(data, mime):
    with TemporaryDirectory() as temp:
        source = Path(temp) / "source"
        source.write_bytes(data)
        if mime != "application/pdf":
            pages = [source]
        else:
            info = command(["pdfinfo", str(source)])
            match = re.search(rb"(?m)^Pages:\s+(\d+)", info)
            if not match or not 1 <= int(match[1]) <= MAX_PAGES:
                raise ProcessingError("PDF_PAGE_LIMIT")
            pages = []
            prefix = Path(temp) / "page"
            command(["pdftoppm", "-f", "1", "-l", match[1].decode(), "-scale-to", "2200",
                     "-png", str(source), str(prefix)])
            pages = sorted(Path(temp).glob("page-*.png"))
        text = "\n".join(command(["tesseract", str(page), "stdout", "-l", "ind+eng"]).decode(
            "utf-8", errors="replace"
        ) for page in pages)
        if not text.strip():
            raise ProcessingError("NO_TEXT_FOUND")
        if len(text) > MAX_TEXT:
            raise ProcessingError("TEXT_LIMIT")
        return text
