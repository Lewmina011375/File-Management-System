import os
import re
import logging
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
from PyPDF2 import PdfReader
import io

# Accurate PDF reading: PyMuPDF → pypdfium2 (PDFium/Chrome) → pdfplumber → PyPDF2 → OCR
try:
    import fitz as _pymupdf
    _HAS_PYMUPDF = True
except ImportError:
    _pymupdf = None
    _HAS_PYMUPDF = False
try:
    import pypdfium2 as _pdfium
    _HAS_PYPDFIUM2 = True
except ImportError:
    _pdfium = None
    _HAS_PYPDFIUM2 = False
try:
    import cv2 as _cv2
    import numpy as _np
    _HAS_OPENCV = True
except ImportError:
    _cv2 = _np = None
    _HAS_OPENCV = False
import pytesseract

# If extracted text is below this many characters (e.g. scanned/image-only PDF), we try OCR.
MIN_PDF_TEXT_CHARS = 25

_tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.isfile(_tesseract_path):
    pytesseract.pytesseract.tesseract_cmd = _tesseract_path

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'txt', 'pdf', 'xlsx', 'csv'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


def clear_uploads_folder():
    """Delete all files in the uploads folder (automatically clean uploads)."""
    folder = app.config['UPLOAD_FOLDER']
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as e:
                logging.warning("Failed to remove upload %s: %s", path, e)


# Clear uploads on startup so each run starts with an empty uploads folder
clear_uploads_folder()


def _read_pdf_via_ocr(path):
    """When PDF has no or minimal text layer (scanned/image PDF), render pages to images and OCR.
    Returns (full_text, list_of_page_texts) or (None, None) on failure."""
    if not os.path.isfile(path):
        logging.warning("PDF OCR skipped: file not found %s", path)
        return None, None
    page_texts = []
    if _HAS_PYMUPDF and _pymupdf and _HAS_OPENCV and _cv2 is not None and _np is not None:
        try:
            doc = _pymupdf.open(path)
            try:
                for page in doc:
                    pix = page.get_pixmap(dpi=150)
                    if not pix.samples or pix.width < 10 or pix.height < 10:
                        page_texts.append("")
                        continue
                    img = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(pix.height, pix.width, pix.n)
                    if pix.n == 1:
                        gray = img.squeeze()
                    else:
                        if pix.n == 4:
                            img = _cv2.cvtColor(img, _cv2.COLOR_RGBA2BGR)
                        else:
                            img = _cv2.cvtColor(img, _cv2.COLOR_RGB2BGR)
                        gray = _cv2.cvtColor(img, _cv2.COLOR_BGR2GRAY)
                    try:
                        text = pytesseract.image_to_string(gray)
                    except Exception as te:
                        logging.warning("Tesseract OCR failed (is Tesseract installed?): %s", te)
                        text = ""
                    page_texts.append(text or "")
            finally:
                doc.close()
            if page_texts:
                full = (" ".join(page_texts)).strip()
                if full:
                    logging.info("PDF read via OCR (PyMuPDF render): %s chars from %s", len(full), path)
                return full, page_texts
        except Exception as e:
            logging.warning("PDF OCR via PyMuPDF render failed %s: %s", path, e)
    if _HAS_OPENCV and _cv2 is not None:
        try:
            from pdf2image import convert_from_path
            images = convert_from_path(path)
        except Exception as e:
            logging.warning("pdf2image failed for OCR fallback %s: %s (install Poppler?)", path, e)
            return None, None
        page_texts = []
        for pil_img in images or []:
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    pil_img.save(f.name, "PNG")
                    img = _cv2.imread(f.name)
                    try:
                        os.remove(f.name)
                    except OSError:
                        pass
                if img is not None:
                    gray = _cv2.cvtColor(img, _cv2.COLOR_BGR2GRAY)
                    try:
                        page_texts.append(pytesseract.image_to_string(gray) or "")
                    except Exception:
                        page_texts.append("")
                else:
                    page_texts.append("")
            except Exception:
                page_texts.append("")
        if page_texts:
            full = (" ".join(page_texts)).strip()
            if full:
                logging.info("PDF read via OCR (pdf2image): %s chars from %s", len(full), path)
            return full, page_texts
    return None, None


def _read_pdf_via_pypdfium2(path):
    """Extract text using pypdfium2 (PDFium engine, same as Chrome). Very accurate for most PDFs."""
    if not _HAS_PYPDFIUM2 or _pdfium is None:
        return None, None
    try:
        pdf = _pdfium.PdfDocument(path)
        try:
            n = len(pdf)
            parts = []
            for i in range(n):
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    t = (textpage.get_text_bounded() or "").strip()
                    parts.append(t)
                except Exception:
                    parts.append("")
            text = " ".join(parts).strip()
            return text if text else None, parts if parts else None
        finally:
            pdf.close()
    except Exception as e:
        logging.debug("pypdfium2 extraction failed %s: %s", path, e)
        return None, None


def _read_pdf_via_pdfplumber(path):
    """Try pdfplumber for text extraction (layout-aware; good for tables)."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            parts = []
            for page in pdf.pages:
                t = (page.extract_text() or "").strip()
                parts.append(t)
            text = " ".join(parts).strip()
            return text if text else None, parts if parts else None
    except Exception as e:
        logging.debug("pdfplumber text extraction failed %s: %s", path, e)
        return None, None


def read_pdf_all(path):
    """Read all text from PDF. Accurate chain: PyMuPDF → pypdfium2 (PDFium) → pdfplumber → PyPDF2 → OCR."""
    if not path or (isinstance(path, str) and not os.path.isfile(path)):
        logging.warning("read_pdf_all: path not a file %s", path)
        return ""
    text = ""
    if _HAS_PYMUPDF and _pymupdf:
        try:
            doc = _pymupdf.open(path)
            try:
                for page in doc:
                    text += (page.get_text("text") or "") + " "
                text = (text or "").strip()
            finally:
                doc.close()
        except Exception as e:
            logging.warning("PyMuPDF open failed %s: %s", path, e)
    if not text and _HAS_PYPDFIUM2:
        full, _ = _read_pdf_via_pypdfium2(path)
        if full:
            text = full
    if not text:
        try:
            reader = PdfReader(path)
            for page in reader.pages:
                part = page.extract_text()
                text += (part if part is not None else "") + " "
            text = (text or "").strip()
        except Exception as e:
            logging.warning("PyPDF2 read failed %s: %s", path, e)
    if not text:
        full, _ = _read_pdf_via_pdfplumber(path)
        if full:
            text = full
    if len(text) < MIN_PDF_TEXT_CHARS:
        full, _ = _read_pdf_via_ocr(path)
        if full:
            return full
    return text or ""


def read_pdf_pages(path):
    """Read PDF and return a list of page texts. Chain: PyMuPDF → pypdfium2 → pdfplumber → PyPDF2 → OCR."""
    if not path or (isinstance(path, str) and not os.path.isfile(path)):
        logging.warning("read_pdf_pages: path not a file %s", path)
        return [""]
    pages = []
    if _HAS_PYMUPDF and _pymupdf:
        try:
            doc = _pymupdf.open(path)
            try:
                pages = [page.get_text("text") or "" for page in doc]
            finally:
                doc.close()
        except Exception as e:
            logging.warning("PyMuPDF open failed %s: %s", path, e)
    if not pages or not any((p or "").strip() for p in pages):
        full, page_list = _read_pdf_via_pypdfium2(path)
        if full and page_list:
            pages = page_list
    if not pages or not any((p or "").strip() for p in pages):
        try:
            reader = PdfReader(path)
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as e:
            logging.warning("PyPDF2 read failed %s: %s", path, e)
    if not pages or not any((p or "").strip() for p in pages):
        full, page_list = _read_pdf_via_pdfplumber(path)
        if full and page_list:
            pages = page_list
    total_chars = sum(len(p or "") for p in pages)
    empty_pages = sum(1 for p in pages if not (p or "").strip())
    if total_chars < MIN_PDF_TEXT_CHARS or (len(pages) > 1 and empty_pages > len(pages) // 2):
        _, ocr_pages = _read_pdf_via_ocr(path)
        if ocr_pages:
            return ocr_pages
    return pages if pages else [""]


# Valid pocket names we extract from taping report after "Item:" (Requirement 1)
# Canonical form for display (with H).
VALID_POCKET_NAMES = frozenset([
    'GPFL50D-30H', 'GPFL50D-40H', 'GPFL50D-50H', 'GPFL50D-70H', 'GPFL50D-95H', 'GPFL50D-125H',
])
POCKET_NAME_ORDER = ('GPFL50D-30H', 'GPFL50D-40H', 'GPFL50D-50H', 'GPFL50D-70H', 'GPFL50D-95H', 'GPFL50D-125H')


def _normalize_item_to_canonical_pocket(raw_value):
    """If raw_value (after 'Item:') is a known pocket variant, return canonical name (e.g. GPFL50D-95H); else None.
    PDF may show GPFL50D_95, GPFL50D-95, GPFL50D_95H, GPFL50D-95H etc."""
    if not raw_value:
        return None
    raw_value = raw_value.strip().rstrip('.,;')
    normalized = raw_value.replace('_', '-').upper()
    # Exact match (with H)
    if normalized in (v.upper() for v in VALID_POCKET_NAMES):
        return next(v for v in VALID_POCKET_NAMES if v.upper() == normalized)
    # With underscore: GPFL50D_95H -> GPFL50D-95H
    if normalized in (v.upper().replace('-', '_') for v in VALID_POCKET_NAMES):
        return next(v for v in VALID_POCKET_NAMES if v.upper().replace('-', '_') == normalized)
    # Without trailing H: GPFL50D-95, GPFL50D_95 -> GPFL50D-95H
    for suffix in ('30', '40', '50', '70', '95', '125'):
        if normalized == 'GPFL50D-' + suffix or normalized == 'GPFL50D_' + suffix:
            return 'GPFL50D-' + suffix + 'H'
    return None


def get_pocket_data_from_taping(filepath):
    """Extract pocket details from the entire taping report (Requirement 1 & 2).
    Requirement 1: Find "Item:" and if the value after it is one of GPFL50D-30H, GPFL50D-40H, GPFL50D-50H,
    GPFL50D-70H, GPFL50D-95H, GPFL50D-125H (or variants like GPFL50D_50, GPFL50D-95), extract → Pocket Name.
    Requirement 2: For that pocket section, find "count" and extract the integer immediately after
    (e.g. count 4 → 4, Count: 004 → 4) → Tape Layout Count.
    Returns a list of dicts: [ {tape_layout, pocket_count}, ... ].
    """
    try:
        pages = read_pdf_pages(filepath)
    except Exception as e:
        logging.warning("Failed to read taping PDF %s: %s", filepath, e)
        return []
    if not pages:
        return []
    # Go through the entire taping report (all pages)
    search_pages = pages
    seen = set()
    result_by_name = {}  # canonical_key -> (display_name, count_int_as_str)
    for page_text in search_pages:
        # Find all "Item: <value>" and accept valid pocket names or variants (e.g. GPFL50D_95, GPFL50D-95)
        item_matches = list(re.finditer(r'Item:\s*(\S+)', page_text, re.IGNORECASE))
        items = []
        for m in item_matches:
            raw = m.group(1).strip()
            canonical = _normalize_item_to_canonical_pocket(raw)
            if canonical:
                items.append(canonical)
        # Find all "count" / "Count:" and integer after it on this page; pair by order
        count_matches = re.findall(r'Count:\s*(\d+)', page_text, re.IGNORECASE)
        if not count_matches:
            count_matches = re.findall(r'\bcount\s+(\d+)', page_text, re.IGNORECASE)
        for i, pocket_name in enumerate(items):
            count_str = count_matches[i] if i < len(count_matches) else ''
            # Normalize to integer display: "004" → "4"
            if count_str:
                try:
                    count_str = str(int(count_str))
                except (ValueError, TypeError):
                    pass
            key = pocket_name.upper()
            if key not in seen:
                seen.add(key)
                result_by_name[key] = (pocket_name, count_str)
    # Stable order: 30H, 40H, 70H, 95H, 125H
    result = []
    ordered_keys = {n.upper() for n in POCKET_NAME_ORDER}
    for name in POCKET_NAME_ORDER:
        key = name.upper()
        if key in result_by_name:
            display_name, count_str = result_by_name[key]
            result.append({'tape_layout': display_name, 'pocket_count': count_str})
    for key, (display_name, count_str) in result_by_name.items():
        if key not in ordered_keys:
            result.append({'tape_layout': display_name, 'pocket_count': count_str})
    return result


def get_cunningham_from_taping(filepath_or_content):
    """Extract Cunningham availability from the ReinS page of the taping report.

    STEP 1 – Isolate ReinS page
      - Read all pages.
      - Find the page that contains the exact word 'ReinS' (word-boundary match).
      - Only that page is processed; other pages are ignored.

    STEP 2 – Search inside ReinS page (case-insensitive)
      - Regex: \\b(Cunham|Cunno)\\b

    STEP 3 – Output
      - Match found  → return matched keyword ('Cunham' or 'Cunno') with original casing.
      - No match     → return None (UI will show '-').
      - Multiple     → first occurrence only.
    """

    def _find_reins_page(pages):
        """Return the text of the first page that contains the exact word 'ReinS'."""
        if not pages:
            return None
        pattern = re.compile(r"\bReinS\b")
        for page_text in pages:
            if page_text and pattern.search(page_text):
                return page_text
        return None

    def _extract_from_page(text):
        """Search only within the ReinS page for Cunham or Cunno."""
        if not text:
            return None
        m = re.search(r"\b(Cunham|Cunno)\b", text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    # File path case: use real pages from PDF
    if filepath_or_content and os.path.isfile(filepath_or_content):
        try:
            pages = read_pdf_pages(filepath_or_content)
        except Exception:
            pages = []
        reins_page = _find_reins_page(pages)
        if not reins_page:
            return None
        return _extract_from_page(reins_page)

    # Raw content case (already a single page of text)
    content = filepath_or_content if isinstance(filepath_or_content, str) and not os.path.isfile(filepath_or_content) else ""
    if not content:
        return None
    # Treat content as one logical page; require ReinS to be present
    if not re.search(r"\bReinS\b", content):
        return None
    return _extract_from_page(content)


# STEP 1 — Work Ticket: exact phrases; first occurrence in reading order; no modification/normalization.
# Populate Exterior Tape row, Work Ticket column with exact original phrase as it appears.
EXTERIOR_TAPE_WORKTICKET_PHRASES = [
    "Raw Black",
    "Raw poly Black",
    "Raw No nonwoven Genoa leech",
    "Raw nonwoven",
    "Raw No nonwoven",
    "Black Taffeta",
    "Black Taffeta Surface",
    "White Taffeta",
    "White Taffeta Surface",
    "Grey Taffeta",
    "Grey Taffeta Surface",
    "White Exterior (0505)",
    "White Exterior (1010)",
    "Black Exterior (0505)",
    "Black Exterior (1010)",
    "Grey exterior (0505)",
    "Grey Exterior (1010)",
    "RAW Grey (No Tint)",
    "RAW UHMPE",
    "Raw yellow",
    "Gold Taffeta",
    "Endurance Yellow",
    "Raw poly",
]


def get_exterior_tape_from_workticket(text):
    """Extract Exterior Tape from Work Ticket — STEP 1.

    Scan entire Work Ticket for first occurrence (in reading order) of any phrase in
    EXTERIOR_TAPE_WORKTICKET_PHRASES. Return the complete phrase exactly as it appears
    (no modification, normalization, trim, or partial extraction). Only extraction; no matching logic.
    """
    if not text:
        return None
    first_start = None
    first_match_text = None
    for phrase in EXTERIOR_TAPE_WORKTICKET_PHRASES:
        # Allow flexible whitespace so we find the phrase; return exact span from document
        pattern = r"\s+".join(re.escape(p) for p in phrase.split())
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            start = m.start()
            if first_start is None or start < first_start:
                first_start = start
                first_match_text = m.group(0)
    return first_match_text


def get_exterior_tape_item_from_taping(filepath_or_content):
    """Extract first integer from Item column — STEP 2–4 (Taping Report).

    First page only. Locate the table that contains ALL column headers: Tape Type, Item, Length, Count, Mass.
    Ignore any other tables. Within that table, Item column, scan top to bottom; return the first integer
    (as string). No validation, matching, or business logic.
    """
    if not filepath_or_content:
        return None

    if not os.path.isfile(filepath_or_content):
        text = str(filepath_or_content)
        # Text path: same as before
        m = re.search(
            r"Tape\s*Type\s+Item\s+Length\s+Count\s+Mass(.*)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return None
        tail = m.group(1)
        for line in tail.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.search(r"Tape\s*Type\s+Item\s+Length\s+Count\s+Mass", stripped, re.IGNORECASE):
                continue
            num_match = re.search(r"\b(\d+)\b", stripped)
            if num_match:
                return num_match.group(1)
        return None

    # File path: try pdfplumber table extraction first (Item column by name), then text fallback
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None

    if pdfplumber:
        try:
            with pdfplumber.open(filepath_or_content) as pdf:
                if not pdf.pages:
                    return None
                page = pdf.pages[0]
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        if not table or not table[0]:
                            continue
                        # STEP 2: Use only the table that contains ALL column headers: Tape Type, Item, Length, Count, Mass.
                        header_cells = [str(c).replace("\n", " ").strip().lower() for c in (table[0] or []) if c is not None]
                        header_text = " ".join(header_cells)
                        if not all(
                            x in header_text
                            for x in ("tape", "item", "length", "count", "mass")
                        ):
                            continue
                        item_col_idx = None
                        for j, cell in enumerate(table[0]):
                            if cell is None:
                                continue
                            val = (str(cell).replace("\n", " ").strip() or "").strip()
                            if val and "item" in val.lower():
                                item_col_idx = j
                                break
                        if item_col_idx is None:
                            continue
                        # STEP 3: From Item column, first integer value
                        for row in table[1:]:
                            if not row or item_col_idx >= len(row):
                                continue
                            cell = row[item_col_idx]
                            if cell is None:
                                continue
                            raw = str(cell).replace("\n", " ").strip()
                            num_match = re.search(r"\b(\d+)\b", raw)
                            if num_match:
                                return num_match.group(1)
        except Exception as e:
            logging.warning("Taping report table extraction for exterior tape Item failed: %s", e)

    # Fallback: text from first page
    try:
        pages = read_pdf_pages(filepath_or_content)
        text = pages[0] if pages else ""
    except Exception as e:
        logging.warning("Failed to read taping PDF for exterior tape %s: %s", filepath_or_content, e)
        return None
    if not text:
        return None
    m = re.search(
        r"Tape\s*Type\s+Item\s+Length\s+Count\s+Mass(.*)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    tail = m.group(1)
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r"Tape\s*Type\s+Item\s+Length\s+Count\s+Mass", stripped, re.IGNORECASE):
            continue
        num_match = re.search(r"\b(\d+)\b", stripped)
        if num_match:
            return num_match.group(1)
    return None


# Valid OE Number prefixes (tape report, work ticket, text file)
OE_PREFIXES = ('OSP', 'OUS', 'OUK', 'OHK', 'OFR', 'ONZ', 'ONL', 'OAU', 'OCA', 'ODE', 'OIT', 'OCR', 'ODK', 'OSE')

def get_oe(text):
    """Extract OE Number; only matches if it starts with a valid prefix (OSP, OUS, OUK, ...).
    Format: PREFIX + 4-10 digits + '-' + 3 digits (e.g. OUK4024191-002)."""
    if not text:
        return None
    prefix_group = '|'.join(re.escape(p) for p in OE_PREFIXES)
    pattern = rf"\b((?:{prefix_group})\d{{4,10}}-\d{{3}})\b"
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1) if match else None

def get_dpi(text, pattern):
    """Extract DPI value"""
    match = re.search(pattern, text)
    if not match:
        return None
    val = match.group(1)
    return float(val.replace(",", ""))

def get_tier_before(text):
    """Extract Tier: 3-digit value immediately before RAW, ENDURANCE, OCEAN, or ENDURANCE EDGE"""
    match = re.search(r"(\d{3})\s*(RAW|ENDURANCE EDGE|ENDURANCE|OCEAN)", text, re.IGNORECASE)
    return match.group(1) if match else None

def get_tier_after(text):
    """Extract Tier: 3-digit value immediately after RAW, ENDURANCE, OCEAN, or ENDURANCE EDGE"""
    match = re.search(r"(RAW|ENDURANCE EDGE|ENDURANCE|OCEAN)\s*(\d{3})", text, re.IGNORECASE)
    return match.group(2) if match else None

def get_tier_before_taping(text):
    """Extract Tier for taping report only: 3-digit value immediately before STD, ENDURANCE, OCEAN, or RAW"""
    match = re.search(r"(\d{3})\s*(STD|ENDURANCE\s*EDGE|ENDURANCE|OCEAN|RAW)", text, re.IGNORECASE)
    return match.group(1) if match else None

# Endurance Edge always uses Tier 360 (text file, taping report, work ticket)
TIER_ENDURANCE_EDGE = 360

def is_endurance_edge(text):
    """True if content mentions Endurance Edge (flexible spacing)."""
    return bool(re.search(r"endurance\s*edge", text or "", re.IGNORECASE))

def get_msm_lengths_from_txt(text):
    """Extract decimal values from the Length column in the Msm's section (Msm#0, Msm#1, ...).
    Returns a list of floats in order, or empty list if section not found."""
    if not text:
        return []
    lines = text.splitlines()
    in_msms = False
    lengths = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\s*Msm's\s*:", stripped, re.I):
            in_msms = True
            continue
        if in_msms:
            if not stripped:
                break
            if re.match(r"^\s*Msm#\d+\s*:", stripped, re.I):
                nums = re.findall(r"([0-9]+\.[0-9]+)", stripped)
                if nums:
                    try:
                        lengths.append(float(nums[-1]))  # Length = last column
                    except ValueError:
                        pass
            else:
                break
    return lengths


def get_measurements_txt(text):
    """Extract measurements from text file using Msm line"""
    lines = text.split("\n")
    msm_line = next((l for l in lines if re.match(r"^\s*Msm\b", l, re.I)), "")
    msm_vals = re.findall(r"[0-9]+\.[0-9]+", msm_line)
    head_line = next((l for l in lines if re.match(r"^\s*Head", l, re.I)), "")
    head_val = re.search(r"[0-9]+\.[0-9]+", head_line)
    head_val = head_val.group(0) if head_val else None
    return {
        "Luff": float(msm_vals[0]) if len(msm_vals) > 0 else None,
        "Leech": float(msm_vals[1]) if len(msm_vals) > 1 else None,
        "Foot": float(msm_vals[2]) if len(msm_vals) > 2 else None,
        "LP": float(msm_vals[3]) if len(msm_vals) > 3 else None,
        "Head": float(head_val) if head_val else None
    }

def get_measurements_wt(text):
    """Extract measurements from work ticket (supports OFR and other formats)."""
    def extract(key):
        # Try: "Key" or "Key:" followed by optional spaces/newlines and number (dot or comma decimal)
        for pattern in [
            rf"{key}\s*:\s*([0-9]+[.,][0-9]+)",   # Key: 123.45 or Key: 123,45
            rf"{key}\s+([0-9]+[.,][0-9]+)",       # Key 123.45 or Key  123,45
            rf"{key}\s*:\s*([0-9]+)\s*\.\s*([0-9]+)",  # Key: 123 . 45 (split by space)
        ]:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                if len(match.groups()) == 2:
                    num_str = f"{match.group(1)}.{match.group(2)}"
                else:
                    num_str = match.group(1).replace(",", ".")
                try:
                    return float(num_str)
                except ValueError:
                    continue
        return None
    return {
        "Head": extract("Head"),
        "Luff": extract("Luff"),
        "Leech": extract("Leech"),
        "Foot": extract("Foot"),
        "LP": extract("LP")
    }


def get_workticket_finished_molded_table(text):
    """Extract Finished and Molded values from the work ticket 'HEADSAIL SIZING' table.

    Expected (extracted) layout per row resembles:
        Head 0,100 0,000 0,100
        Luff 27,600 0,040 27,560
    i.e. Finished, Reductions, Molded.

    Returns:
        { "Head": {"Finished": 0.1, "Molded": 0.1}, ... }
    """
    if not text:
        return {}

    # Narrow search to the HEADSAIL SIZING section if present.
    section_text = text
    m = re.search(r"HEADSAIL\s+SIZING", text, re.IGNORECASE)
    if m:
        section_text = text[m.start():]

    def _to_float(s):
        if s is None:
            return None
        try:
            return float(str(s).replace(",", "."))
        except (ValueError, TypeError):
            return None

    result = {}
    keys = ["Head", "Luff", "Leech", "Foot", "LP"]
    for key in keys:
        # key + Finished + Reductions + Molded
        match = re.search(
            rf"\b{key}\b\s+([0-9]+[.,][0-9]+)\s+([0-9]+[.,][0-9]+)\s+([0-9]+[.,][0-9]+)",
            section_text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            finished = _to_float(match.group(1))
            molded = _to_float(match.group(3))
            if finished is not None or molded is not None:
                result[key] = {"Finished": finished, "Molded": molded}
    return result


def get_strips_count_from_txt_line(text):
    """Extract Trim Stripes count from a line like 'Trim Stripes Count = X' in the text file.
    Returns the number X (int) or None if not found."""
    if not text:
        return None
    m = re.search(r"Trim\s*Stripes\s*Count\s*=\s*(\d+)", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            pass
    return None


def get_strips_count_from_txt(text):
    """Trim Stripes Count from text file: first try line 'Trim Stripes Count = X'; else count
    how many times the word 'SS' appears as a mark in the Marks section (e.g. SS2/8, SS3/8, SS4/8, SS5/8, SS6/8).
    Each such line counts as one. Returns count (int) or None."""
    if not text:
        return None
    from_line = get_strips_count_from_txt_line(text)
    if from_line is not None:
        return from_line
    lines = text.splitlines()
    in_marks = False
    count = 0
    for l in lines:
        stripped = l.strip()
        if re.match(r"Mark?s?\s*:", stripped, re.IGNORECASE):
            in_marks = True
            continue
        if in_marks:
            if not stripped:
                break
            # Count every line that contains SS as a mark (e.g. SS2/8 :, SS3/8 :, SS4/8 :); do not break on other marks (LuM02, LeM02, etc.)
            if re.search(r"\bSS\d*/?\d*\b", stripped, re.IGNORECASE) or re.match(r"^SS\d*/?\d*\s*:", stripped, re.IGNORECASE):
                count += 1
    return count if count > 0 else None


def get_reefs_count_from_txt(text):
    """Count how many times 'Reef' + integer (Reef1, Reef2, Reef3, etc.) appears in the text file.
    Returns that count for display in the Text File column, Reefs Count row."""
    if not text:
        return None
    # Find all "Reef" followed by integer (Reef1, Reef 1, Reef2, Reef3, etc.)
    matches = re.findall(r"\bReef\s*(\d+)\b", text, re.IGNORECASE)
    if not matches:
        return None
    # Count unique reef numbers (Reef1, Reef2, Reef3 -> 3)
    unique = set(int(m) for m in matches)
    return len(unique)


def get_cunningham_from_txt(text):
    """Search text file for keyword 'Cungh'. Returns 'Cungh' if found; otherwise None."""
    if not text:
        return None
    m = re.search(r"\b(Cungh)\b", text, re.IGNORECASE)
    return m.group(1) if m else None


def _get_trim_stripes_data_rows(text):
    """Return list of Trim Stripes table data rows (GENOA GRAPHICS / MAINSAIL GRAPHICS).
    Explicitly ignores: (1) the 'Trim Stripes' title row, (2) the '#' header row (column headers).
    Only rows that look like data (e.g. start with 1, 2, 3, 4 or # 1, # 2) are returned. Rows can vary by work ticket."""
    if not text:
        return []
    text_lower = text.lower()
    has_trim_stripes = 'trim' in text_lower and 'stripes' in text_lower
    has_graphics = ('mainsail' in text_lower or 'genoa' in text_lower) and 'graphics' in text_lower
    if not (has_trim_stripes or has_graphics):
        return []

    batten_row = re.compile(
        r"bat#|batten|short\s+batten|full\s+length\s+batten|luff\s+|leech\s+|girth\s+|reinforcement|pocket\s*\d",
        re.IGNORECASE
    )
    # Ignore: Trim Stripes title row only
    trim_stripes_title = re.compile(r"^trim\s*stripes\s*$", re.IGNORECASE)
    # Ignore: # header row (#, Primary Stripe, Width, Secondary Stripe)
    header_row = re.compile(
        r"^[\s#]*(#\s*)?(primary\s*stripe|width|secondary\s*stripe)",
        re.IGNORECASE
    )
    data_row = re.compile(r"^\s*#?\s*\d{1,2}\b")  # e.g. "1", "2", "# 3", "4"
    lines = text.splitlines()
    trim_stripes_header = re.compile(r"trim\s*stripes", re.IGNORECASE) if has_trim_stripes else None
    fallback_header = re.compile(r"genoa\s*graphics|mainsail\s*graphics|#\s*primary\s*stripe", re.IGNORECASE)
    in_section = False
    data_rows = []
    for l in lines:
        stripped = l.strip()
        # Ignore Trim Stripes title row
        if trim_stripes_title.match(stripped):
            in_section = True
            data_rows = []
            continue
        if trim_stripes_header and trim_stripes_header.search(stripped):
            in_section = True
            data_rows = []
            continue
        if not in_section and (not has_trim_stripes) and fallback_header.search(stripped):
            in_section = True
            data_rows = []
            continue
        if in_section:
            if not stripped:
                if data_rows:
                    break
                continue
            if batten_row.search(stripped):
                break
            # Ignore # row (column headers: #, Primary Stripe, Width, Secondary Stripe)
            if re.match(r"^\s*#\s*$", stripped):
                continue
            if re.match(r"^\s*#\s*Primary\s+Stripe", stripped, re.IGNORECASE):
                continue
            if header_row.search(stripped) or (
                re.search(r"primary\s*stripe|secondary\s*stripe", stripped, re.IGNORECASE)
                and re.match(r"^[#A-Za-z]", stripped)
            ):
                continue
            if data_row.match(stripped):
                data_rows.append(stripped)
    return data_rows


def get_strips_count_from_workticket_trim_section(text):
    """Locate the section titled 'Trim Stripes' in the work ticket; inside that table find the
    column with header '#' and return the number in the last row of that column. Returns int or None."""
    if not text:
        return None
    lines = text.splitlines()
    in_trim_stripes = False
    after_hash_header = False
    last_hash_value = None
    trim_stripes_header = re.compile(r"^\s*Trim\s*Stripes\s*:?\s*$", re.IGNORECASE)
    hash_header = re.compile(r"^\s*#\s*($|\s|Primary|Width|Secondary)", re.IGNORECASE)
    data_row = re.compile(r"^\s*#?\s*(\d{1,2})\b")
    for line in lines:
        stripped = line.strip()
        if trim_stripes_header.match(stripped):
            in_trim_stripes = True
            after_hash_header = False
            last_hash_value = None
            continue
        if not in_trim_stripes:
            continue
        if not stripped:
            if last_hash_value is not None:
                return last_hash_value
            continue
        if re.search(r"^\s*#\s*$", stripped) or hash_header.search(stripped):
            after_hash_header = True
            continue
        if after_hash_header:
            m = data_row.match(stripped)
            if m:
                try:
                    last_hash_value = int(m.group(1))
                except (ValueError, TypeError):
                    pass
            else:
                if last_hash_value is not None and re.match(r"^[A-Za-z]", stripped):
                    return last_hash_value
    return last_hash_value


def _get_trim_stripes_last_row_numbers(text):
    """For each Graphics section (DOWNWIND, GENOA, MAINSAIL), find the Trim Stripes table and
    return the value in the last row of the # column. Returns list of ints (one per section).
    Work ticket strip count = sum of these (so it matches 'last row of #' per section)."""
    if not text:
        return []
    text_lower = text.lower()
    has_graphics = (
        ('mainsail' in text_lower or 'genoa' in text_lower or 'downwind' in text_lower)
        and 'graphics' in text_lower
    )
    if not has_graphics:
        return []
    sections = ["DOWNWIND GRAPHICS", "GENOA GRAPHICS", "MAINSAIL GRAPHICS"]
    header_row = re.compile(
        r"^[\s#]*(#\s*)?(primary\s*stripe|width|secondary\s*stripe)",
        re.IGNORECASE
    )
    data_row = re.compile(r"^\s*#?\s*(\d{1,2})\b")
    lines = text.splitlines()
    batten_row = re.compile(
        r"bat#|batten|short\s+batten|full\s+length\s+batten|luff\s+|leech\s+|girth\s+|reinforcement|pocket\s*\d",
        re.IGNORECASE
    )
    other_table_header = re.compile(
        r"^\s*#\s*.*\b(Type|Item|Location|Vertical\s+Distance|Distance\s+Back)\b|^\s*Windows\s*$",
        re.IGNORECASE
    )
    last_numbers = []
    in_section = False
    after_header = False
    last_hash_value = None
    for line in lines:
        stripped = line.strip()
        for sec in sections:
            if sec in stripped:
                if last_hash_value is not None:
                    last_numbers.append(last_hash_value)
                in_section = True
                after_header = False
                last_hash_value = None
                break
        if not in_section:
            continue
        if not stripped:
            if last_hash_value is not None:
                last_numbers.append(last_hash_value)
                last_hash_value = None
            after_header = False
            continue
        if batten_row.search(stripped):
            if last_hash_value is not None:
                last_numbers.append(last_hash_value)
            break
        if other_table_header.search(stripped) or re.search(r"\bWindows\b", stripped, re.IGNORECASE):
            if last_hash_value is not None:
                last_numbers.append(last_hash_value)
                last_hash_value = None
            after_header = False
            continue
        if re.match(r"^\s*#\s*$", stripped) or re.match(r"^\s*#\s*Primary\s+Stripe", stripped, re.IGNORECASE):
            after_header = True
            continue
        if header_row.search(stripped):
            after_header = True
            continue
        if after_header:
            m = data_row.match(stripped)
            if m:
                try:
                    last_hash_value = int(m.group(1))
                except ValueError:
                    pass
    if last_hash_value is not None:
        last_numbers.append(last_hash_value)
    return last_numbers


def get_trim_stripes_primary_stripe_cell_count(text):
    """Count cells in the 'primary Stripe' column of the Trim Stripes table in Mainsail Graphics /
    Genoa Graphics / Downwind Graphics. Finds each section, locates the Trim Stripes table
    (header with 'primary Stripe'), counts data rows in that column. Returns sum of counts or None."""
    if not text:
        return None
    text_lower = text.lower()
    if not (
        ("mainsail" in text_lower or "genoa" in text_lower or "downwind" in text_lower)
        and "graphics" in text_lower
        and ("trim" in text_lower and "stripe" in text_lower)
    ):
        return None
    sections = ["DOWNWIND GRAPHICS", "GENOA GRAPHICS", "MAINSAIL GRAPHICS"]
    header_row = re.compile(
        r"^[\s#]*(#\s*)?(primary\s*stripe|width|secondary\s*stripe)",
        re.IGNORECASE
    )
    data_row = re.compile(r"^\s*#?\s*\d{1,2}\b")
    trim_stripes_title = re.compile(r"trim\s*stripes", re.IGNORECASE)
    batten_row = re.compile(
        r"bat#|batten|short\s+batten|full\s+length\s+batten|luff\s+|leech\s+|girth\s+|reinforcement|pocket\s*\d",
        re.IGNORECASE
    )
    other_table = re.compile(
        r"\b(Type|Item|Location|Vertical\s+Distance|Distance\s+Back|Windows)\b",
        re.IGNORECASE
    )
    lines = text.splitlines()
    total = 0
    in_section = False
    after_header = False
    count_in_section = 0
    for line in lines:
        stripped = line.strip()
        for sec in sections:
            if sec in stripped:
                if after_header:
                    total += count_in_section
                in_section = True
                after_header = False
                count_in_section = 0
                break
        if not in_section:
            continue
        if not stripped:
            if after_header:
                total += count_in_section
            after_header = False
            count_in_section = 0
            continue
        if batten_row.search(stripped):
            if after_header:
                total += count_in_section
            break
        if other_table.search(stripped):
            if after_header:
                total += count_in_section
            after_header = False
            count_in_section = 0
            continue
        if trim_stripes_title.search(stripped):
            after_header = False
            count_in_section = 0
            continue
        if re.match(r"^\s*#\s*$", stripped) or re.match(r"^\s*#\s*Primary\s+Stripe", stripped, re.IGNORECASE):
            after_header = True
            continue
        if header_row.search(stripped):
            after_header = True
            continue
        if after_header and data_row.match(stripped):
            count_in_section += 1
    if after_header:
        total += count_in_section
    return total if total > 0 else None


def get_trim_stripes_primary_stripe_cell_count_pdf(filepath):
    """Work ticket PDF: go to Mainsail/Genoa/Downwind Graphics; find table with 'Trim stripes';
    select 'Primary Stripe' column and count cells. Show in Trim Stripes Count row, Work Ticket column."""
    if not os.path.isfile(filepath):
        return None
    total = _trim_stripes_primary_stripe_count_via_pdfplumber(filepath)
    if total is not None and total > 0:
        return total
    total = _trim_stripes_primary_stripe_count_via_text(filepath)
    return total


def _trim_stripes_primary_stripe_count_via_pdfplumber(filepath):
    """Use pdfplumber tables: pages with Graphics + Trim/Stripe, tables with Primary Stripe column."""
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(filepath) as pdf:
            total = 0
            for page in pdf.pages:
                page_text = (page.extract_text() or "").lower()
                if "graphics" not in page_text:
                    continue
                if "trim" not in page_text and "stripe" not in page_text:
                    continue
                tables = page.extract_tables()
                if not tables:
                    tables = page.extract_tables(table_settings={"vertical_strategy": "text"}) or []
                for table in tables or []:
                    if not table or len(table) < 2:
                        continue
                    primary_stripe_col_idx = None
                    header_row_idx = None
                    for ri, row in enumerate(table):
                        if not row:
                            continue
                        for j, cell in enumerate(row or []):
                            if cell is None:
                                continue
                            val = (str(cell).replace("\n", " ").replace("\r", " ").strip() or "").lower()
                            if "primary" in val and "stripe" in val:
                                primary_stripe_col_idx = j
                                header_row_idx = ri
                                break
                        if primary_stripe_col_idx is not None:
                            break
                    if primary_stripe_col_idx is None or header_row_idx is None:
                        continue
                    count = 0
                    for ri in range(header_row_idx + 1, len(table)):
                        row = table[ri]
                        if not row or primary_stripe_col_idx >= len(row):
                            continue
                        cell = row[primary_stripe_col_idx]
                        val = (str(cell).replace("\n", " ").strip() or "").strip() if cell else ""
                        if val and val.lower().replace("  ", " ").strip() in ("primary stripe", "primary stripe "):
                            continue
                        count += 1
                    if count > 0:
                        total += count
            return total if total > 0 else None
    except Exception as e:
        logging.debug("Trim stripes pdfplumber failed %s: %s", filepath, e)
        return None


def _trim_stripes_primary_stripe_count_via_text(filepath):
    """Fallback: get full PDF text and run text-based Primary Stripe column counter."""
    try:
        content = read_pdf_all(filepath)
        if content:
            return get_trim_stripes_primary_stripe_cell_count(content)
    except Exception:
        pass
    return None


def get_strips_count_from_workticket(text):
    """Work ticket strip count = sum of (last row # in the # column) for each Graphics section
    (DOWNWIND GRAPHICS, GENOA GRAPHICS, MAINSAIL GRAPHICS). Uses the value in the last row of
    the Trim Stripes table under each section, not the row count."""
    last_numbers = _get_trim_stripes_last_row_numbers(text)
    if not last_numbers:
        data_rows = _get_trim_stripes_data_rows(text)
        count = len(data_rows)
        return count if count > 0 else None
    return sum(last_numbers)


def get_strips_count_from_workticket_ocr(pdf_path):
    """Extract strip counts from work ticket PDF using OCR (pdf2image + OpenCV + Tesseract).
    Finds sections DOWNWIND GRAPHICS, GENOA GRAPHICS, MAINSAIL GRAPHICS; after each section's
    header row (#, Primary...), counts data rows (lines starting with a digit). Returns total
    strip count across all sections, or None on error or if OCR deps unavailable."""
    try:
        from pdf2image import convert_from_path
        import cv2
    except ImportError:
        return None
    try:
        images = convert_from_path(pdf_path)
    except Exception as e:
        logging.warning("pdf2image failed for %s: %s", pdf_path, e)
        return None
    if not images:
        return None
    all_text = []
    for i, pil_img in enumerate(images):
        img_path = os.path.join(app.config['UPLOAD_FOLDER'], f"_ocr_page_{i}.png")
        try:
            pil_img.save(img_path, "PNG")
            img = cv2.imread(img_path)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            text = pytesseract.image_to_string(gray)
            all_text.append(text or "")
        except Exception as e:
            logging.warning("OCR failed for page %s of %s: %s", i, pdf_path, e)
        finally:
            try:
                if os.path.isfile(img_path):
                    os.remove(img_path)
            except OSError:
                pass
    full_text = "\n".join(all_text)
    lines = full_text.split("\n")
    sections = ["DOWNWIND GRAPHICS", "GENOA GRAPHICS", "MAINSAIL GRAPHICS"]
    other_table_keywords = re.compile(
        r"\bWindows\b|\bType\b.*\bItem\b|\bLocation\b|\bVertical\s*Distance\b|\bDistance\s*Back\b",
        re.IGNORECASE
    )
    last_numbers = []
    current_section = None
    after_header = False
    last_hash_value = None
    for line in lines:
        line_stripped = line.strip()
        for sec in sections:
            if sec in line_stripped:
                if last_hash_value is not None:
                    last_numbers.append(last_hash_value)
                current_section = sec
                after_header = False
                last_hash_value = None
                break
        if not line_stripped:
            if last_hash_value is not None:
                last_numbers.append(last_hash_value)
                last_hash_value = None
            after_header = False
            continue
        if other_table_keywords.search(line_stripped):
            if last_hash_value is not None:
                last_numbers.append(last_hash_value)
                last_hash_value = None
            after_header = False
            continue
        if re.search(r"\bWindows\b|\bType\b|\bLocation\b|\bVertical\s*Distance\b|\bDistance\s*Back\b", line_stripped, re.IGNORECASE):
            if last_hash_value is not None:
                last_numbers.append(last_hash_value)
                last_hash_value = None
            after_header = False
            continue
        if "#" in line_stripped and "Primary" in line_stripped:
            after_header = True
            continue
        if after_header and len(line_stripped) > 0 and line_stripped[0].isdigit():
            first_num = re.match(r"^\s*#?\s*(\d{1,2})\b", line_stripped)
            if first_num:
                try:
                    last_hash_value = int(first_num.group(1))
                except ValueError:
                    pass
    if last_hash_value is not None:
        last_numbers.append(last_hash_value)
    total = sum(last_numbers)
    return total if total > 0 else None


def get_strips_count_from_dataframe(df):
    """Work ticket strip count from Excel/CSV = value in the last row of the # column (Trim Stripes).
    Uses '#' column: data rows are rows where # is 1–2 digit number; returns that last value (or sum if multiple blocks)."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    cols_lower = [str(c).strip().lower() for c in df.columns]
    sharp_col = None
    for i, c in enumerate(df.columns):
        if str(c).strip() == '#':
            sharp_col = df.columns[i]
            break
    if sharp_col is not None:
        vals = df[sharp_col].astype(str).str.strip()
        numeric = vals[vals.str.match(r"^\d{1,2}$", na=False)]
        if len(numeric) > 0:
            try:
                return int(numeric.iloc[-1])
            except (ValueError, TypeError):
                pass
    primary_stripe_col = None
    for i, c in enumerate(cols_lower):
        if c == 'primary stripe' or c == 'primary_stripe':
            primary_stripe_col = df.columns[i]
            break
    if primary_stripe_col is not None:
        vals = df[primary_stripe_col].dropna().astype(str).str.strip()
        header_lower = 'primary stripe'
        data_vals = vals[(vals != '') & (vals.str.lower() != header_lower)]
        if len(data_vals) > 0:
            return len(data_vals)
    return None


def get_width_cells_count_from_workticket(text):
    """Count total Width cells in Trim Stripes table (GENOA GRAPHICS or MAINSAIL GRAPHICS).
    After Trim Stripes / graphics header, finds Width column then counts lines with e.g. '75 mm', '50 mm'.
    Returns len() of those items (int) or None."""
    if not text:
        return None
    text_lower = text.lower()
    if "genoa graphics" not in text_lower and "mainsail graphic" not in text_lower and "mainsail graphics" not in text_lower:
        return None
    lines = text.splitlines()
    in_graphics_section = False
    width_section_found = False
    width_count = 0
    for line in lines:
        stripped = line.strip()
        if re.search(r"trim\s*stripes|genoa\s*graphics|mainsail\s*graphics", stripped, re.IGNORECASE):
            in_graphics_section = True
            width_section_found = False
            width_count = 0
            continue
        if not in_graphics_section:
            continue
        if stripped == "":
            if width_count > 0:
                break
            continue
        if "width" in stripped.lower() and not re.search(r"\b\d+\s*mm\b", stripped.lower()):
            width_section_found = True
            continue
        if width_section_found and re.search(r"\b\d+\s*mm\b", stripped.lower()):
            width_count += 1
    return width_count if width_count > 0 else None


def get_width_cells_count_from_dataframe(df):
    """Count rows in Trim Stripes where Width column contains 'mm' (e.g. '75 mm', '50 mm'). For Excel/CSV work tickets."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    cols_lower = [str(c).strip().lower() for c in df.columns]
    width_col = None
    for i, c in enumerate(cols_lower):
        if c == 'width':
            width_col = df.columns[i]
            break
    if width_col is None:
        return None
    count = df[width_col].astype(str).str.contains(r'\d+\s*mm', case=False, na=False).sum()
    return int(count) if count > 0 else None


def _get_extras_section(text):
    """Return the text of the EXTRAS section (case-insensitive) or empty string."""
    if not text:
        return ''
    lines = text.splitlines()
    in_extras = False
    parts = []
    for l in lines:
        stripped = l.strip()
        if re.match(r"^\s*EXTRAS\s*$", stripped, re.IGNORECASE):
            in_extras = True
            continue
        if in_extras:
            if not stripped:
                if parts:
                    break
                continue
            if re.match(r"^[A-Z][A-Z\s]+$", stripped) and len(stripped) > 3 and stripped.upper() != 'EXTRAS':
                break
            parts.append(stripped)
    return ' '.join(parts)


def get_trim_stripes_count_from_extras(text):
    """Extract Trim Stripes count from Work Ticket EXTRAS section (e.g. '3 X Trim Stripes',
    '3 * Trim Stripes', '3 X Vinyl Trim Stripes', '3 × Dacron Trim Stripes').
    Returns the number when present, else None. This is the authoritative count when present."""
    if not text:
        return None
    extras = _get_extras_section(text)
    if not extras:
        return None
    # Allow optional word(s) between the multiplier (X, × or *) and Trim (e.g. "3 X Vinyl Trim Stripes")
    m = re.search(r"(\d+)\s*[×xX*]\s+(?:\w+\s+)*Trim\s+Stripes", extras, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            pass
    return None


def get_reefs_count_from_workticket(text):
    """Extract reef count from Work Ticket EXTRAS section.
    Three keywords: N× Standard reef, N× 3Di IBF reef, N× Reef (N can vary).
    Extract the number before each × and sum them. Display in Work Ticket column, Reefs Count row.
    If only the words (Standard reef, 3Di IBF reef, Reef) appear without any number, show 1."""
    if not text:
        return None
    extras = _get_extras_section(text)
    if not extras:
        return None
    total = 0
    # 1. Extract number before × Standard reef
    for m in re.finditer(r"(\d+)\s*[×xX]\s*Standard\s+reef", extras, re.IGNORECASE):
        total += int(m.group(1))
    # 2. Extract number before × 3Di IBF reef (also matches 3D IBF reef)
    for m in re.finditer(r"(\d+)\s*[×xX]\s*3D[i]?\s+IBF\s+reef", extras, re.IGNORECASE):
        total += int(m.group(1))
    # 3. Extract number before × Reef (standalone, not part of Standard reef or 3Di IBF reef)
    for m in re.finditer(r"(\d+)\s*[×xX]\s*Reef\b", extras, re.IGNORECASE):
        total += int(m.group(1))
    # 4. If only words without numbers: Standard reef, 3Di IBF reef, Reef → show 1
    has_reef_keywords = (
        re.search(r"\bStandard\s+reef\b", extras, re.IGNORECASE) or
        re.search(r"\b3D[i]?\s+IBF\s+reef\b", extras, re.IGNORECASE) or
        re.search(r"\bReef\b", extras, re.IGNORECASE)
    )
    if total == 0 and has_reef_keywords:
        return 1
    return total if total > 0 else None


def get_spreader_from_txt(text):
    """Find only the key word 'Spread' in text file (e.g. 'Spread:' or 'Spread '). Returns 'Spread' for Text File column."""
    if not text:
        return None
    if re.search(r"\bSpread\b", text, re.IGNORECASE):
        return 'Spread'
    return None


def get_spreader_from_workticket(text):
    """If 'Spreader Patches' appears in EXTRAS section (case-insensitive), return 'Spreader Patches'; else None.
    Ignore 'Spreader Patches Location' and 'Mainsail Tip Spreader Patches'."""
    extras = _get_extras_section(text)
    # Match "Spreader Patches" but not when followed by " Location" and not when preceded by "Mainsail Tip "
    if re.search(r"(?<!Mainsail Tip )\bSpreader\s+Patches\b(?!\s+Location)", extras, re.IGNORECASE):
        return 'Spreader Patches'
    return None


def get_cunningham_from_workticket(text):
    """In Extras section only, search for 'Cunningham'. Returns 'Cunningham' if found; otherwise None."""
    extras = _get_extras_section(text)
    if not extras:
        return None
    m = re.search(r"\b(Cunningham)\b", extras, re.IGNORECASE)
    return m.group(1) if m else None


def get_tt_windows_from_workticket(text):
    """If 'TT Windows' appears in EXTRAS section of the work ticket, return the phrase (e.g. '2 X TT Windows' or '2 × TT Windows').
    For display in Work Ticket column, TT Windows row. No comparison with text file or taping report.
    Prefer EXTRAS section; if not found there, search full work ticket text so table layouts are still picked up."""
    # Match "N X TT Windows" or "TT Windows" (case-insensitive); support ×, x, X
    pattern = re.compile(r"(\d+\s*[×xX]\s*TT\s+Windows|TT\s+Windows)", re.IGNORECASE)
    extras = _get_extras_section(text)
    if extras:
        m = pattern.search(extras)
        if m:
            return _normalize_tt_windows_display(m.group(1).strip())
    # Fallback: search full work ticket text (e.g. when EXTRAS boundary misses table cells)
    if text:
        m = pattern.search(text)
        if m:
            return _normalize_tt_windows_display(m.group(1).strip())
    return None


def _normalize_tt_windows_display(phrase):
    """Normalize '2 X TT Windows' to '2 × TT Windows' for consistent display."""
    if not phrase:
        return phrase
    return re.sub(r"\s+[xX]\s+", " × ", phrase, count=1)


def get_spreader_from_dataframe(df):
    """If any cell contains 'Spreader Patches' but not 'Spreader Patches Location' or 'Mainsail Tip Spreader Patches', return 'Spreader Patches'; else None."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    for c in df.columns:
        for v in df[c].dropna().astype(str):
            if re.search(r"(?<!Mainsail Tip )\bSpreader\s+Patches\b(?!\s+Location)", str(v), re.IGNORECASE):
                return 'Spreader Patches'
    return None


def get_stanchion_from_workticket(text):
    """If 'Stanchion Patches' appears in EXTRAS (or anywhere in work ticket), return 'Stanchion Patches' for Work Ticket column; else None."""
    # Match "Stanchion Patches" or "Stanchion-Patches" (flexible for PDF extraction)
    pattern = re.compile(r"\bStanchion[- ]+Patches\b", re.IGNORECASE)
    extras = _get_extras_section(text)
    if extras and pattern.search(extras):
        return "Stanchion Patches"
    if text and pattern.search(text):
        return "Stanchion Patches"
    return None


def get_stanchion_from_dataframe(df):
    """If any cell contains 'Stanchion Patches' (case-insensitive), return 'Stanchion Patches' for Work Ticket column; else None."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    for c in df.columns:
        for v in df[c].dropna().astype(str):
            val = str(v).strip()
            if re.search(r"\bStanchion[- ]+Patches\b", val, re.IGNORECASE):
                return "Stanchion Patches"
    return None


def get_cunningham_from_dataframe(df):
    """If any cell contains 'Cunningham' (work ticket Excel/CSV), return 'Cunningham'; else None."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    for c in df.columns:
        for v in df[c].dropna().astype(str):
            if re.search(r"\bCunningham\b", str(v), re.IGNORECASE):
                return "Cunningham"
    return None


def get_helix_from_workticket(text):
    """If 'Helix' appears in EXTRAS section, return the full phrase for display (e.g. 'HELIX Structured Luff'); else None."""
    # Prefer full phrase "HELIX Structured Luff" for Work Ticket column
    pattern_full = re.compile(r"(HELIX\s+Structured\s+Luff)", re.IGNORECASE)
    extras = _get_extras_section(text)
    if extras:
        m = pattern_full.search(extras)
        if m:
            return m.group(1).strip()
        if re.search(r"\bhelix\b", extras, re.IGNORECASE):
            return 'Helix'
    # Fallback: search full work ticket text (e.g. when EXTRAS boundary misses the cell)
    if text:
        m = pattern_full.search(text)
        if m:
            return m.group(1).strip()
        if re.search(r"\bhelix\b", text, re.IGNORECASE):
            return 'Helix'
    return None


def get_helix_from_taping(filepath):
    """If 'Helix' appears on the first page of taping report (e.g. '3di Jib 870 L HELIX 2022'), return 'Helix'; else None."""
    try:
        pages = read_pdf_pages(filepath)
        if not pages:
            return None
        first_page = pages[0]
        if re.search(r"\bhelix\b", first_page, re.IGNORECASE):
            return 'Helix'
    except Exception:
        pass
    return None


def get_helix_from_dataframe(df):
    """If any cell contains 'Helix' (case-insensitive), return that cell's value for display (e.g. 'HELIX Structured Luff'); else None."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    for c in df.columns:
        for v in df[c].dropna().astype(str):
            val = str(v).strip()
            if re.search(r"\bhelix\b", val, re.IGNORECASE):
                return val
    return None


def get_tt_windows_from_dataframe(df):
    """If any cell contains 'TT Windows' (case-insensitive), return that phrase for display; else None. For Excel/CSV work tickets."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    for c in df.columns:
        for v in df[c].dropna().astype(str):
            m = re.search(r"(\d+\s*[×xX]\s*TT\s+Windows|TT\s+Windows)", str(v), re.IGNORECASE)
            if m:
                return _normalize_tt_windows_display(m.group(1).strip())
    return None


def get_batten_from_text(text):
    """Detect `Short` / `Full` / `Full/Short` in a text file content."""
    if not text:
        return None
    lower = text.lower()
    has_short = bool(re.search(r"\bshort\b", lower))
    has_full = bool(re.search(r"\bfull\b", lower))
    if has_full and has_short:
        return 'Full/Short'
    if has_full:
        return 'Full'
    if has_short:
        return 'Short'
    return None


def get_batten_types_from_text(text):
    """Extract batten types from text file reading downward-to-upward (bottom of file first).
    Rule 1: Bat#Integer → type = Full | Leech | Short | Roller (from same line).
    Rule 2: FBa#Integer or FBat#Integer → type = Flutter.
    Returns list of {"batten_no": row_index, "type": str} in bottom-to-top order for Text File column.
    """
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    results = []
    # Read from downward to upward = process lines in reverse order (bottom of file first)
    for line in reversed(lines):
        lower = line.lower()
        # Rule 2: Flutter (FBa#N or FBat#N)
        m_flutter = re.search(r"\b(FBa|FBat)#?\s*(\d+)\b", line, re.IGNORECASE)
        if m_flutter:
            results.append({"batten_no": len(results) + 1, "type": "Flutter"})
            continue
        # Rule 1: Standard (Bat#N or BatN) → Full, Leech, Short, Roller
        m_bat = re.search(r"\bBat#?\s*(\d+)\b", line, re.IGNORECASE)
        if not m_bat:
            continue
        if re.search(r"full\s+length\s+batten|\bfull\s+length\b|\bfull\b", lower):
            btype = "Full"
        elif re.search(r"\bshort\s+batten\b|\bshort\b", lower):
            btype = "Short"
        elif re.search(r"\bleech\b", lower):
            btype = "Leech"
        elif re.search(r"\broller\b", lower):
            btype = "Roller"
        else:
            btype = None
        if btype:
            results.append({"batten_no": len(results) + 1, "type": btype})
    # results are in downward-to-upward order (first = from bottom of file = row 1 in UI)
    return results


def get_batten_list_from_text(text):
    """Extract batten types reading downward-to-upward (bottom to top of file).
    Bat#Integer → Full/Leech/Short/Roller; FBa#Integer or FBat#Integer → Flutter.
    Returns list of type strings in that order for the Text File column in UI.
    """
    typed = get_batten_types_from_text(text)
    return [item["type"] for item in typed]


def get_batten_from_workticket(text):
    """Detect keywords in work ticket that indicate batten type.
    Returns one of: 'full', 'vertical full', 'vertical', 'leech', 'roller', 'flutter' or None.
    """
    if not text:
        return None
    lower = text.lower()
    # check more specific phrases first
    if re.search(r"vertical\s*full", lower):
        return 'vertical full'
    for kw in ('full', 'leech', 'roller', 'vertical', 'flutter'):
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            return kw
    return None


def get_batten_list_from_workticket(text):
    """Extract batten types from the work ticket in top->down order.
    Scan for table-like lines in the BATTENS & POCKETS section containing known keywords.
    Returns list in the order found (top-to-bottom).
    """
    if not text:
        return []

    # Split into raw lines (preserve spacing for table detection)
    raw_lines = [l.rstrip() for l in text.splitlines()]

    # Find the header for the Battens & Pockets block (if possible)
    start_idx = None
    for i, l in enumerate(raw_lines):
        if re.search(r"battens\s*&\s*pockets|battens\s+pockets", l, re.IGNORECASE):
            start_idx = i + 1
            break

    # If not found, return empty
    if start_idx is None:
        return []

    # Known section headers that mark the end of BATTENS & POCKETS (must be exact or nearly exact section names)
    section_headers = ['Batten Bellows', 'Batten Hollows', 'MAINSAIL GRAPHICS', 'MAINSAIL EDGES', 'NOTES', 'Page']
    
    results = []
    first_data_row = True
    # Scan lines after the header
    for l in raw_lines[start_idx:]:
        stripped = l.strip()
        
        # Stop on blank line (end of table)
        if not stripped:
            break
        
        # Stop if we hit a known section header that marks end of table
        if any(re.search(rf"^{re.escape(header)}", stripped, re.IGNORECASE) for header in section_headers):
            break
        
        # Skip the column header row (contains "Type", "Length", "Batten", "Pocket", etc.)
        if re.search(r"\b(Type|Length|Batten|Pocket|Chafe|Style|Slide|Hardware|Closures|Reinforcements|End\s+Caps)\b", stripped):
            first_data_row = True
            continue

        # Split table-like lines on 2+ spaces or tabs to get columns
        parts = re.split(r"\s{2,}|\t", stripped)
        if not parts:
            continue

        # Candidate is the first column (Type column)
        candidate = parts[0].strip()
        if not candidate:
            continue

        lower_candidate = candidate.lower()

        # Look for batten keywords in the first column
        found = None
        if re.search(r"vertical\s*full", lower_candidate):
            found = 'Vertical Full'
        else:
            for kw in ('full', 'leech', 'roller', 'vertical', 'flutter', 'short'):
                if re.search(rf"\b{re.escape(kw)}\b", lower_candidate):
                    found = kw.capitalize() if kw != 'short' else 'Short'
                    break

        if found:
            results.append(found)

    return results


def get_batten_lengths_from_txt(text):
    """Extract batten lengths (meters) from the text file in downward-to-upward order.
    - Flutter (FBa# / FBat#): use decimal from Girth column (last decimal on line).
    - Full/Leech/Short/Roller (Bat#): use decimal from Length column (last decimal on line).
    Returns list in same order as get_batten_types_from_text (bottom of file = index 0).
    """
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lengths = []
    for line in reversed(lines):  # downward to upward (bottom of file first)
        nums = re.findall(r"([0-9]+\.[0-9]+)", line)
        if not nums:
            continue
        # Flutter: FBa# or FBat# → Girth column = last decimal on line
        if re.search(r"\b(FBa|FBat)#?\s*\d+", line, re.IGNORECASE):
            try:
                lengths.append(float(nums[-1]))  # Girth
            except ValueError:
                continue
            continue
        # Standard (Bat#): Length column = last decimal (only on batten table lines with type)
        if re.search(r"\bBat#?\s*\d+", line, re.IGNORECASE) and re.search(
            r"Short\s+Batten|Full\s+Length\s+Batten|\b(Full|Leech|Short|Roller)\b", line, re.IGNORECASE
        ):
            try:
                lengths.append(float(nums[-1]))  # Length
            except ValueError:
                continue
    # We collected bottom-to-top; first element = bottom of file = row 1 in UI
    return lengths


def get_batten_lengths_from_workticket(text):
    """Extract batten lengths (millimetres) from the work ticket Length column.
    Returns list in table order (upward to downward = top->down).
    Only one length per batten row: we add length only when the batten Type
    (Full, Leech, Roller, Vertical, Flutter, Short) is on the SAME line as the mm value,
    so we do not double-count when each batten has FRONT/REAR or two lines in the PDF.
    """
    if not text:
        return []
    raw_lines = [l.rstrip() for l in text.splitlines()]
    start_idx = None
    for i, l in enumerate(raw_lines):
        if re.search(r"battens\s*&\s*pockets|battens\s+pockets", l, re.IGNORECASE):
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    search_lines = raw_lines[start_idx:]
    lengths = []
    for i, l in enumerate(search_lines):
        if not l.strip():
            if lengths:
                break
            continue
        stripped = l.strip()
        if re.search(r"\b(Type|Length|Batten|Pocket|Chafe|Style|Slide|Hardware|Closures|Reinforcements|End\s+Caps)\b", stripped):
            continue

        mm_match = re.search(r"(\d{3,4})\s*mm", l, re.IGNORECASE)
        type_on_this_line = re.search(r"\b(full|leech|roller|vertical|flutter|short)\b", l, re.IGNORECASE)
        if mm_match and type_on_this_line:
            try:
                val = int(mm_match.group(1))
                lengths.append(val if val != 0 else None)
            except ValueError:
                lengths.append(None)
        elif mm_match:
            continue
        elif type_on_this_line:
            # Type on this line, mm may be on next line; if no mm found, Length is " - " (append None)
            found = False
            for j in range(i + 1, min(i + 3, len(search_lines))):
                m2 = re.search(r"(\d{3,4})\s*mm", search_lines[j], re.IGNORECASE)
                if m2:
                    try:
                        v = int(m2.group(1))
                        lengths.append(v if v != 0 else None)
                        found = True
                        break
                    except ValueError:
                        pass
            if not found:
                lengths.append(None)
    return lengths


# Exact pocket sizes to detect from Work Ticket Pocket column (regex word-boundary only)
POCKET_EXACT_MM = (40, 50, 70, 95, 125)
_RE_EXACT_MM = {mm: re.compile(r'\b' + re.escape(str(mm)) + r'mm\b', re.IGNORECASE) for mm in POCKET_EXACT_MM}

# Debug mode: print each Pocket row and count increments (set to False for production)
POCKET_TABLE_DEBUG = True


def get_pocket_counts_from_pdf_tables(filepath):
    """Extract and count pocket sizes from Work Ticket PDF using pdfplumber.extract_tables() only.
    Finds table with 'Pocket' column (case-insensitive), cleans rows (skip header/repeated header/empty),
    counts exact 40/70/95/125mm once per row. Uses run-based counting: consecutive rows with same
    Pocket value = one logical row (avoids duplicate count for FRONT/REAR).
    Returns (result_dict, pocket_column_found). result_dict = {"40mm": {"found": bool, "count": int}, ...}."""
    try:
        import pdfplumber
    except ImportError:
        logging.warning("pdfplumber not available for table extraction")
        return ({}, False)

    pocket_column_found = False
    pocket_col_idx = None
    pocket_values = []  # list of cleaned Pocket cell values, one per data row (after skip header/empty)
    processed_rows = set()  # (page_no, table_idx, row_idx) to avoid counting same row twice

    try:
        with pdfplumber.open(filepath) as pdf:
            for page_no, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                if not tables:
                    continue
                for table_idx, table in enumerate(tables):
                    if not table or not table[0]:
                        continue
                    # Find header row and column index where header is "Pocket"
                    header_row_idx = None
                    col_idx = None
                    for row_idx, row in enumerate(table):
                        if not row:
                            continue
                        for j, cell in enumerate(row):
                            if cell is None:
                                continue
                            val = (str(cell).replace('\n', ' ').strip() or '').strip()
                            if val.lower() == 'pocket':
                                header_row_idx = row_idx
                                col_idx = j
                                pocket_column_found = True
                                break
                        if col_idx is not None:
                            break
                    if col_idx is None:
                        continue
                    # Collect data rows: skip header, skip repeated "Pocket" or empty (one table only)
                    for row_idx, row in enumerate(table):
                        if row_idx <= header_row_idx:
                            continue
                        key = (page_no, table_idx, row_idx)
                        if key in processed_rows:
                            continue
                        if col_idx >= len(row):
                            continue
                        cell = row[col_idx]
                        if cell is None:
                            continue
                        raw = str(cell).replace('\n', ' ').replace('\r', ' ').strip()
                        if not raw or raw.lower() == 'pocket':
                            continue
                        processed_rows.add(key)
                        pocket_values.append(raw)
                        if POCKET_TABLE_DEBUG:
                            print("[Pocket table DEBUG] Pocket row: %r" % raw)
                    # Use only the first table that has Pocket column (avoid duplicate from next page)
                    break
                if pocket_column_found:
                    break
    except Exception as e:
        logging.warning("get_pocket_counts_from_pdf_tables failed for %s: %s", filepath, e)
        return ({}, pocket_column_found)

    # Count once per row only (strict regex; no duplicate counting)
    result = {}
    for mm in POCKET_EXACT_MM:
        pattern = _RE_EXACT_MM[mm]
        count = 0
        for v in pocket_values:
            if v and pattern.search(v):
                count += 1
                if POCKET_TABLE_DEBUG:
                    print("[Pocket table DEBUG] Increment %smm (row value: %r)" % (mm, v[:50]))
        result["%smm" % mm] = {"found": count > 0, "count": count}
        if POCKET_TABLE_DEBUG:
            print("[Pocket table DEBUG] Final count %smm = %s" % (mm, count))

    return (result, pocket_column_found)


def _pocket_column_index(lines, start_idx):
    """Find the column index of 'Pocket' in the BATTENS & POCKETS table header.
    Header row is the first row that contains 'Pocket' (and typically Type, Length, Batten, etc.).
    Returns (header_row_index, pocket_col_index) or (None, None)."""
    for i in range(start_idx, min(start_idx + 10, len(lines))):
        line = lines[i].strip()
        if not line:
            continue
        parts = re.split(r"\s{2,}|\t", line)
        for j, part in enumerate(parts):
            if re.search(r'\bPocket\b', part, re.IGNORECASE):
                return i, j
    return None, None


def get_pocket_list_from_workticket(text):
    """Extract Pocket column values only from the work ticket BATTENS & POCKETS table.
    Step 1: Locate table with column header 'Pocket', extract values strictly under that column.
    Returns one pocket value per batten row (same order, top->down). Values like '901008 (125mm)', '901006 (70mm)'."""
    if not text:
        return []
    raw_lines = [l.rstrip() for l in text.splitlines()]
    start_idx = None
    for i, l in enumerate(raw_lines):
        if re.search(r"battens\s*&\s*pockets|battens\s+pockets", l, re.IGNORECASE):
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    search_lines = raw_lines[start_idx:]
    header_row_idx, pocket_col_idx = _pocket_column_index(raw_lines, start_idx)
    if header_row_idx is not None and pocket_col_idx is not None:
        pockets = []
        for i in range(header_row_idx + 1, len(raw_lines)):
            line = raw_lines[i].strip()
            if not line:
                if pockets:
                    break
                continue
            if re.search(r"\b(Type|Length|Batten|Pocket|Chafe|Style|Slide|Hardware|Closures|Reinforcements|End\s+Caps)\b", line, re.IGNORECASE):
                continue
            parts = re.split(r"\s{2,}|\t", line)
            if pocket_col_idx < len(parts):
                cell = (parts[pocket_col_idx] or "").strip()
                if cell and (cell.upper() != 'LDX75' and not cell.upper().startswith('LDX')):
                    pockets.append(cell)
                else:
                    pockets.append("")
            else:
                pockets.append("")
        if pockets:
            return pockets

    # Fallback: row-based extraction (one pocket value per batten-type row)
    pockets = []
    for i, l in enumerate(search_lines):
        if not l.strip():
            if pockets:
                break
            continue
        if re.search(r"\b(Type|Length|Batten|Pocket|Chafe|Style|Slide|Hardware|Closures|Reinforcements|End\s+Caps)\b", l.strip(), re.IGNORECASE):
            continue
        type_on_line = re.search(r"\b(full|leech|roller|vertical|flutter|short)\b", l, re.IGNORECASE)
        if not type_on_line:
            continue
        pocket_val = None
        m = re.search(r"(\d{5,6}\s*\(\d+\s*mm\))", l, re.IGNORECASE)
        if m:
            pocket_val = m.group(1).strip()
        if not pocket_val:
            m = re.search(r"([A-Z]{2,}\d+[A-Z0-9]*(?:\s+\d+\s*mm)?)", l, re.IGNORECASE)
            if m:
                pocket_val = m.group(1).strip()
        if not pocket_val:
            for j in range(i + 1, min(i + 3, len(search_lines))):
                m2 = re.search(r"(\d{5,6}\s*\(\d+\s*mm\)|[A-Z]{2,}\d+[A-Z0-9]*)", search_lines[j], re.IGNORECASE)
                if m2:
                    pocket_val = m2.group(1).strip()
                    break
        if pocket_val and (pocket_val.upper() == 'LDX75' or pocket_val.upper().startswith('LDX')):
            pocket_val = ''
        pockets.append(pocket_val or "")
    return pockets


# Pocket Types: mm values we care about (from work ticket Length or Pocket column)
POCKET_MM_VALUES = (30, 40, 70, 95, 125)


def get_length_column_pocket_mm_from_workticket(text):
    """Extract 40/70/95/125 mm from the work ticket BATTENS & POCKETS table (Length column).
    Returns list of mm values in row order. Only exact length: 70mm matches, 170mm does not."""
    if not text:
        return []
    raw_lines = [l.rstrip() for l in text.splitlines()]
    start_idx = None
    for i, l in enumerate(raw_lines):
        if re.search(r"battens\s*&\s*pockets|battens\s+pockets", l, re.IGNORECASE):
            start_idx = i + 1
            break
    if start_idx is None:
        return []
    search_lines = raw_lines[start_idx:]
    result = []
    for i, l in enumerate(search_lines):
        if not l.strip():
            if result:
                break
            continue
        if re.search(r"\b(Type|Length|Batten|Pocket|Chafe|Style|Slide|Hardware|Closures|Reinforcements|End\s+Caps)\b", l.strip(), re.IGNORECASE):
            continue
        if not re.search(r"\b(full|leech|roller|vertical|flutter|short)\b", l, re.IGNORECASE):
            continue
        mm_val = None
        for mm in POCKET_MM_VALUES:
            if re.search(r"\b%s\s*mm\b" % mm, l, re.IGNORECASE):
                mm_val = mm
                break
        if mm_val is None:
            for j in range(i + 1, min(i + 3, len(search_lines))):
                for mm in POCKET_MM_VALUES:
                    if re.search(r"\b%s\s*mm\b" % mm, search_lines[j], re.IGNORECASE):
                        mm_val = mm
                        break
                if mm_val is not None:
                    break
        if mm_val is not None:
            result.append(mm_val)
    return result


def _mm_from_pocket_string(s):
    """Extract mm (40, 70, 95, 125) from a pocket string using exact regex only.
    Matches \\b40mm\\b, \\b70mm\\b, \\b95mm\\b, \\b125mm\\b. Does NOT match 140mm, 240mm, 95mmx2, 70mmH."""
    if not s or not isinstance(s, str):
        return None
    for mm in POCKET_EXACT_MM:
        if _RE_EXACT_MM[mm].search(s):
            return mm
    return None


def get_pocket_mm_from_workticket_pocket_list(wt_pocket_list):
    """Return set of mm values (40, 70, 95, 125) that appear in the work ticket Pocket column."""
    if not wt_pocket_list:
        return set()
    out = set()
    for p in wt_pocket_list:
        mm = _mm_from_pocket_string(p)
        if mm is not None:
            out.add(mm)
    return out


def count_workticket_pocket_by_mm(wt_pocket_list, mm):
    """Count how many times exact size (e.g. 40mm) appears in the work ticket Pocket column.
    Uses regex \\b{mm}mm\\b only: matches 40mm, 70mm, 95mm, 125mm; does NOT match 140mm, 95mmx2, 70mmH."""
    if not wt_pocket_list or mm is None:
        return 0
    if mm not in POCKET_EXACT_MM:
        return 0
    pattern = _RE_EXACT_MM[mm]
    return sum(1 for p in wt_pocket_list if p and pattern.search(str(p)))


def count_length_column_by_mm(length_mm_list, mm):
    """Count how many times mm appears in the Length-column mm list."""
    if length_mm_list is None or mm is None:
        return None
    return sum(1 for m in length_mm_list if m == mm)


# Fixed pocket codes for Work Ticket Count table (901005, 901006, 901007, 901008)
VALID_POCKET_CODES = ('901005', '901006', '901007', '901008')
VALID_POCKET_LABELS = {'901005': '901005 (40mm)', '901006': '901006 (70mm)', '901007': '901007 (95mm)', '901008': '901008 (125mm)'}


def count_wt_pocket_codes(wt_pocket_list):
    """Count exact occurrences of 901005, 901006, 901007, 901008 in Work Ticket Pocket column."""
    counts = {code: 0 for code in VALID_POCKET_CODES}
    for p in (wt_pocket_list or []):
        if not p or not isinstance(p, str):
            continue
        s = (p or '').strip()
        if not s:
            continue
        for code in VALID_POCKET_CODES:
            if re.match(r'^%s\b' % re.escape(code), s, re.IGNORECASE):
                counts[code] += 1
                break
    return [{"pocket_type": VALID_POCKET_LABELS[code], "count": counts[code]} for code in VALID_POCKET_CODES]


# Work Ticket Count table: count (40mm), (50mm), (70mm), (95mm), (125mm) in Pocket column
POCKET_MM_FOR_COUNT = (40, 50, 70, 95, 125)


def count_wt_pocket_by_mm(wt_pocket_list):
    """Count how many times each 40mm, 70mm, 95mm, 125mm appears in the work ticket Pocket column.
    Returns list of {pocket_type: '40mm'|'70mm'|'95mm'|'125mm', count: int} for UI Work Ticket Count column."""
    return [
        {"pocket_type": "%smm" % mm, "count": count_workticket_pocket_by_mm(wt_pocket_list, mm) or 0}
        for mm in POCKET_MM_FOR_COUNT
    ]


class FileProcessor:
    def __init__(self):
        self.data = {
            'txt_file': {},
            'taping_report': {},
            'work_ticket': {},
            # store uploaded filenames so UI can persist across pages
            'file_names': {'txt': None, 'taping': None, 'ticket': None}
        }
    
    def parse_txt_file(self, filepath):
        """Parse text file content using improved extraction methods"""
        data = {}
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as file:
                content = file.read()
            
            # Extract OE Number from content, then fallback to filename (e.g. OUK4024191-002.txt)
            oe = get_oe(content)
            if not oe:
                oe = get_oe(os.path.basename(filepath))
            if oe:
                data['OE Number'] = oe
            
            # Extract Tier (try after format first, then before)
            tier = get_tier_after(content)
            if not tier:
                tier = get_tier_before(content)
            if tier:
                try:
                    data['Tier'] = int(tier)
                except (ValueError, TypeError):
                    pass
            # Endurance Edge = 360 always (for check report)
            if is_endurance_edge(content):
                data['Tier'] = TIER_ENDURANCE_EDGE
            
            # Extract measurements using improved method
            measurements = get_measurements_txt(content)
            if measurements['Head'] is not None:
                data['Head'] = measurements['Head']
            if measurements['Luff'] is not None:
                data['Luff'] = measurements['Luff']
            if measurements['Leech'] is not None:
                data['Leech'] = measurements['Leech']
            if measurements['Foot'] is not None:
                data['Foot'] = measurements['Foot']
            if measurements['LP'] is not None:
                data['LP'] = measurements['LP']
            # Extract Length column from Msm's section (Msm#0, Msm#1, ...) for msm row Text File column.
            # If only the single "Msm" row exists (no Msm's / Msm#N block), do not extract or show values.
            msm_lengths = get_msm_lengths_from_txt(content)
            if msm_lengths:
                data['msm'] = ", ".join(str(v) for v in msm_lengths)
            
            # Extract Batten Type from txt file (Short / Full / Full/Short)
            batten_txt = get_batten_from_text(content)
            if batten_txt:
                data['Batten Type'] = batten_txt
            # Extract batten list in downward-to-upward order for Text File column
            batten_types = get_batten_types_from_text(content)
            if batten_types:
                data['Batten_Types_From_Text'] = batten_types
                data['Batten_List'] = [item["type"] for item in batten_types]
            # Extract batten lengths (file order top->down)
            batten_lengths = get_batten_lengths_from_txt(content)
            if batten_lengths:
                data['Batten_Lengths_m'] = batten_lengths
            strips_count = get_strips_count_from_txt(content)
            if strips_count is not None:
                data['Strips_Count'] = strips_count
            reefs_count = get_reefs_count_from_txt(content)
            if reefs_count is not None:
                data['Reefs_Count'] = reefs_count
            cunningham_txt = get_cunningham_from_txt(content)
            if cunningham_txt is not None:
                data['Cunningham'] = cunningham_txt
            spreader_txt = get_spreader_from_txt(content)
            if spreader_txt is not None:
                data['Spreader_Patches'] = spreader_txt

        except Exception as e:
            logging.warning("Error parsing txt file %s: %s", filepath, e)
        return data
    
    def parse_pdf_report(self, filepath, is_work_ticket=False):
        """Parse PDF report content using improved extraction methods"""
        data = {}
        try:
            content = read_pdf_all(filepath)
            
            # Extract OE Number from content, then fallback to filename (e.g. WorkTicket_OUK4024191-002.pdf)
            oe = get_oe(content)
            if not oe:
                oe = get_oe(os.path.basename(filepath))
            if oe:
                data['OE Number'] = oe
            
            # Extract DPI with different patterns for taping report vs work ticket
            if is_work_ticket:
                dpi = get_dpi(content, r"(\d+[\,\d]*)\s*Dpi")
            else:
                dpi = get_dpi(content, r"(\d+)\s*DPI")
            if dpi is not None:
                data['DPI'] = dpi
            
            # Extract Tier (taping report: 3-digit before STD/ENDURANCE/OCEAN/RAW; work ticket: after then before)
            if is_work_ticket:
                tier = get_tier_after(content)
                if not tier:
                    tier = get_tier_before(content)
            else:
                tier = get_tier_before_taping(content)
            if tier:
                try:
                    data['Tier'] = int(tier)
                except (ValueError, TypeError):
                    pass
            # Endurance Edge = 360 always in work ticket only (for check report)
            if is_work_ticket and is_endurance_edge(content):
                data['Tier'] = TIER_ENDURANCE_EDGE
            
            # For taping report only: extract pocket data (Item → Tape Layout, Count → Pocket count) per header
            # Uses page-by-page read; skips first page; looks for GPFL50D_50, _70, _95, _125
            if not is_work_ticket:
                pocket_list = get_pocket_data_from_taping(filepath)
                if pocket_list:
                    data['Pocket_List'] = pocket_list
            
            # For work tickets, extract measurements
            if is_work_ticket:
                measurements = get_measurements_wt(content)
                fm_table = get_workticket_finished_molded_table(content)
                if fm_table:
                    data['Measurements_Finished_Molded'] = fm_table
                    # Use Finished as the canonical work-ticket measurement (for difference/status logic)
                    for k in ['Head', 'Luff', 'Leech', 'Foot', 'LP']:
                        fin = fm_table.get(k, {}).get('Finished')
                        if fin is not None:
                            data[k] = fin
                if measurements['Head'] is not None:
                    data['Head'] = measurements['Head']
                if measurements['Luff'] is not None:
                    data['Luff'] = measurements['Luff']
                if measurements['Leech'] is not None:
                    data['Leech'] = measurements['Leech']
                if measurements['Foot'] is not None:
                    data['Foot'] = measurements['Foot']
                if measurements['LP'] is not None:
                    data['LP'] = measurements['LP']
                
                # Extract Batten Type from work ticket (keywords: full, leech, roller, vertical, flutter, vertical full)
                batten_wt = get_batten_from_workticket(content)
                if batten_wt:
                    data['Batten Type'] = batten_wt
                # Extract batten list (top->down) for per-batten comparison
                batten_list_wt = get_batten_list_from_workticket(content)
                if batten_list_wt:
                    data['Batten_List'] = batten_list_wt
                # Extract batten lengths in mm (work ticket table order top->down)
                batten_lengths_wt = get_batten_lengths_from_workticket(content)
                if batten_lengths_wt:
                    data['Batten_Lengths_mm'] = batten_lengths_wt
                # Extract Pocket column via pdfplumber tables (row-based, no duplicate count)
                pocket_counts_dict, table_pocket_found = get_pocket_counts_from_pdf_tables(filepath)
                if pocket_counts_dict:
                    data['Pocket_Counts_By_Size'] = pocket_counts_dict
                if table_pocket_found:
                    data['Pocket_Column_Found'] = True
                # Fallback: text-based Pocket list for other logic if table path didn't find column
                if not table_pocket_found:
                    data['Pocket_Column_Found'] = get_pocket_column_found(content)
                pocket_list_wt = get_pocket_list_from_workticket(content)
                if pocket_list_wt:
                    data['Pocket_List'] = pocket_list_wt
                # Length column: 40/70/95/125 mm for Pocket Types (exact match only)
                length_mm_list = get_length_column_pocket_mm_from_workticket(content)
                if length_mm_list:
                    data['Length_Column_Pocket_Mm'] = length_mm_list
                # Trim Stripes Count: prefer EXTRAS line (e.g. "3 X Trim Stripes") when present; else
                # count cells in "primary Stripe" column in Trim Stripes table (Mainsail/Genoa/Downwind Graphics)
                strips_count_wt = get_trim_stripes_count_from_extras(content)
                if strips_count_wt is None:
                    strips_count_wt = get_trim_stripes_primary_stripe_cell_count_pdf(filepath)
                if strips_count_wt is None:
                    strips_count_wt = get_trim_stripes_primary_stripe_cell_count(content)
                if strips_count_wt is None:
                    strips_count_wt = get_strips_count_from_workticket_trim_section(content)
                if strips_count_wt is None:
                    strips_count_wt = get_strips_count_from_workticket_ocr(filepath)
                if strips_count_wt is None:
                    strips_count_wt = get_strips_count_from_workticket(content)
                width_cells_wt = get_width_cells_count_from_workticket(content)
                if strips_count_wt is not None:
                    data['Strips_Count'] = strips_count_wt
                elif width_cells_wt is not None:
                    data['Strips_Count'] = width_cells_wt
                if width_cells_wt is not None:
                    data['Width_Cells_Count'] = width_cells_wt
                # Spreader Patches: only if "Spreader" appears in EXTRAS section (case-insensitive)
                spreader_wt = get_spreader_from_workticket(content)
                if spreader_wt is not None:
                    data['Spreader_Patches'] = spreader_wt
                cunningham_wt = get_cunningham_from_workticket(content)
                if cunningham_wt is not None:
                    data['Cunningham'] = cunningham_wt
                # Helix Structure: from EXTRAS (e.g. HELIX Structured Luff)
                helix_wt = get_helix_from_workticket(content)
                if helix_wt is not None:
                    data['Helix_Structure'] = helix_wt
                # Reef count from EXTRAS: N× Standard reef, N× 3Di IBF reef, N× Reef; or plain words → 1
                reefs_wt = get_reefs_count_from_workticket(content)
                if reefs_wt is not None:
                    data['Reefs_Count'] = reefs_wt
                # TT Windows: from EXTRAS (e.g. "2 X TT Windows"); show in Work Ticket column only
                tt_windows_wt = get_tt_windows_from_workticket(content)
                if tt_windows_wt is not None:
                    data['TT_Windows'] = tt_windows_wt
                # Stanchion Patches: from EXTRAS (exact phrase, preserve case e.g. "Stanchion Patches")
                stanchion_wt = get_stanchion_from_workticket(content)
                if stanchion_wt is not None:
                    data['Stachion_Patches'] = stanchion_wt
                # Exterior Tape: exact phrase from Work Ticket (first match from EXTERIOR_TAPE_WORKTICKET_PHRASES)
                exterior_tape_wt = get_exterior_tape_from_workticket(content)
                if exterior_tape_wt is not None:
                    data['Exterior_Tape'] = exterior_tape_wt

            else:
                # Taping report: Cunham or Cunno on ReinS page
                cunningham_tape = get_cunningham_from_taping(filepath)
                if cunningham_tape is not None:
                    data['Cunningham'] = cunningham_tape
                # Helix Structure: on first page (e.g. product line with HELIX)
                helix_tape = get_helix_from_taping(filepath)
                if helix_tape is not None:
                    data['Helix_Structure'] = helix_tape
                # Exterior Tape: first integer from Item column on first page of taping report
                exterior_tape_item = get_exterior_tape_item_from_taping(filepath)
                if exterior_tape_item is not None:
                    data['Exterior_Tape'] = exterior_tape_item

        except Exception as e:
            logging.warning("Error parsing PDF report %s: %s", filepath, e)
        return data
    
    def compare_files(self, tolerance=0.01):
        """Compare data from all three sources with improved accuracy"""
        results = []
        criteria = ['OE Number', 'DPI', 'Tier', 'Head', 'Luff', 'Leech', 'Foot', 'LP', 'msm']
        wt_finished_molded = (self.data.get('work_ticket', {}) or {}).get('Measurements_Finished_Molded') or {}
        
        for criterion in criteria:
            wt_val = self.data['work_ticket'].get(criterion, '-')
            display_name = 'Msm' if criterion == 'msm' else criterion
            row = {
                'Criteria': display_name,
                'Text_File': self.data['txt_file'].get(criterion, '-'),
                'Taping_Report': self.data['taping_report'].get(criterion, '-'),
                # Keep original field for comparison logic
                'Work_Ticket': wt_val,
                # UI: Work Ticket split into 3 sub-columns
                'Work_Ticket_Other': '-',
                'Work_Ticket_Finished': '-',
                'Work_Ticket_Molded': '-',
            }

            # OE/DPI/Tier/msm go under "Other"; measurement rows go under "Finished"
            if criterion in ['OE Number', 'DPI', 'Tier', 'msm']:
                row['Work_Ticket_Other'] = wt_val if wt_val != '-' else '-'
            elif criterion in ['Head', 'Luff', 'Leech', 'Foot', 'LP']:
                fm = wt_finished_molded.get(criterion) or {}
                finished = fm.get('Finished')
                molded = fm.get('Molded')
                row['Work_Ticket_Finished'] = finished if finished is not None else (wt_val if wt_val != '-' else '-')
                row['Work_Ticket_Molded'] = molded if molded is not None else '-'
            
            if criterion == 'OE Number':
                values = [row['Text_File'], row['Taping_Report'], row['Work_Ticket']]
                non_dash_values = [v for v in values if v != '-']
                row['Difference'] = '-'
                if len(non_dash_values) >= 2:
                    row['Status'] = '✓' if len(set(non_dash_values)) == 1 else '✗'
                elif len(non_dash_values) == 1:
                    row['Status'] = '?'
                else:
                    row['Status'] = '?'
            elif criterion in ['Head', 'Luff', 'Leech', 'Foot', 'LP']:
                # Compare TXT with Finished first; show difference (Finished - TXT) in DIFFERENCE.
                # If mismatch, compare TXT with Molded and show difference (Molded - TXT) in DIFFERENCE.
                txt_val_raw = row['Text_File'] if row['Text_File'] != '-' else None
                finished_raw = row.get('Work_Ticket_Finished')
                molded_raw = row.get('Work_Ticket_Molded')

                def _to_float(v):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None

                t = _to_float(txt_val_raw)
                f = _to_float(finished_raw)
                m = _to_float(molded_raw)

                if t is None or (f is None and m is None):
                    row['Difference'] = '-'
                    row['Status'] = '?'
                else:
                    used_diff = None
                    status = '✗'

                    # 1) Match with Finished: show difference between Text File and Finished
                    if f is not None:
                        diff_f = round(f - t, 3)
                        row['Difference'] = f'{diff_f:.3f}'
                        if abs(diff_f) <= tolerance:
                            status = '✓'
                            used_diff = diff_f
                        else:
                            # 2) Mismatch: match with Molded and show difference between Text File and Molded
                            if m is not None:
                                diff_m = round(m - t, 3)
                                used_diff = diff_m
                                row['Difference'] = f'{diff_m:.3f}'
                                status = '✓' if abs(diff_m) <= tolerance else '✗'
                            else:
                                used_diff = diff_f
                                status = '✗'
                    else:
                        # No Finished: use Molded for difference
                        if m is not None:
                            diff_m = round(m - t, 3)
                            used_diff = diff_m
                            row['Difference'] = f'{diff_m:.3f}'
                            status = '✓' if abs(diff_m) <= tolerance else '✗'
                        else:
                            row['Difference'] = '-'
                            status = '?'

                    if used_diff is None:
                        row['Difference'] = '-'
                        row['Status'] = '?'
                    else:
                        row['Status'] = status
            elif criterion == 'msm':
                values = [row['Text_File'], row['Taping_Report'], row['Work_Ticket']]
                non_dash = [v for v in values if v != '-']
                row['Difference'] = '-'
                if len(non_dash) >= 2:
                    row['Status'] = '✓' if len(set(non_dash)) == 1 else '✗'
                elif len(non_dash) == 1:
                    row['Status'] = '?'
                else:
                    row['Status'] = '?'
            else:
                txt_val = row['Text_File'] if row['Text_File'] != '-' else None
                tap_val = row['Taping_Report'] if row['Taping_Report'] != '-' else None
                wk_val = row['Work_Ticket'] if row['Work_Ticket'] != '-' else None
                available_values = [(v, _) for v, _ in [(txt_val, 'Text_File'), (tap_val, 'Taping_Report'), (wk_val, 'Work_Ticket')] if v is not None]
                row['Difference'] = '-'
                if len(available_values) == 0 or len(available_values) == 1:
                    row['Status'] = '?'
                else:
                    try:
                        vals = [float(v) for v, _ in available_values]
                        row['Status'] = '✓' if all(abs(v - vals[0]) <= tolerance for v in vals) else '✗'
                    except (TypeError, ValueError):
                        row['Status'] = '?'
            
            results.append(row)
        # Insert per-batten comparison rows (Batten 1, Batten 2, ...)
        txt_batts = self.data['txt_file'].get('Batten_List', [])
        wt_batts = self.data['work_ticket'].get('Batten_List', [])
        max_batts = max(len(txt_batts), len(wt_batts))
        for i in range(max_batts):
            idx = i + 1
            txt_val = txt_batts[i] if i < len(txt_batts) else None
            wt_val = wt_batts[i] if i < len(wt_batts) else None
            row = {
                'Criteria': f'Batten {idx}',
                'Text_File': txt_val if txt_val is not None else '-',
                'Taping_Report': '-',
                'Work_Ticket': wt_val if wt_val is not None else '-',
                'Work_Ticket_Other': wt_val if wt_val is not None else '-',
                'Work_Ticket_Finished': '-',
                'Work_Ticket_Molded': '-',
                'Difference': '-'
            }
            # determine status using same mapping rules
            if txt_val is None or wt_val is None:
                row['Status'] = '?'
            else:
                t = str(txt_val).lower()
                w = str(wt_val).lower()
                txt_options = set()
                if 'full' in t:
                    txt_options.add('full')
                if 'short' in t:
                    txt_options.add('short')
                if 'flutter' in t:
                    txt_options.add('flutter')
                # allowed mapping for work-ticket keywords (text file type can match any in list)
                allowed = {
                    'full': ['full'],
                    'vertical full': ['full', 'short'],
                    'vertical': ['short'],
                    'leech': ['short'],
                    'roller': ['short'],
                    'flutter': ['short', 'flutter']  # Flutter vs Flutter → ✓; Short vs Flutter → ✓
                }
                # find allowed set
                allowed_set = None
                for key in allowed.keys():
                    if key in w:
                        allowed_set = set(allowed[key])
                        break
                if allowed_set is None:
                    allowed_set = {w}
                row['Status'] = '✓' if txt_options & allowed_set else '✗'
            results.append(row)
        
        return results

    def generate_batten_mapping(self):
        """Generate a mapping between work ticket (top->down) and text file (bottom->up).
        Returns a pandas DataFrame with columns:
         - WorkTicket_Row (1-based), WorkTicket_Length_mm
         - TextFile_Batten (1-based), TextFile_Length_mm
         - Match (True/False)
        """
        wt_lengths = self.data.get('work_ticket', {}).get('Batten_Lengths_mm', []) or []
        txt_lengths_m = self.data.get('txt_file', {}).get('Batten_Lengths_m', []) or []
        txt_batten_list = self.data.get('txt_file', {}).get('Batten_List', []) or []
        wt_batten_list = self.data.get('work_ticket', {}).get('Batten_List', []) or []

        # Text file lengths are already in downward-to-upward order (Flutter=Girth, Bat#=Length)
        txt_types = list(txt_batten_list)
        wt_types = list(wt_batten_list)

        # Match with 1 mm tolerance (rounding between m and mm)
        TOLERANCE_MM = 1
        max_len = max(len(wt_lengths), len(txt_lengths_m))
        rows = []
        for i in range(max_len):
            wt_len = wt_lengths[i] if i < len(wt_lengths) else None
            txt_len_m = txt_lengths_m[i] if i < len(txt_lengths_m) else None
            txt_len_mm = int(round(txt_len_m * 1000)) if txt_len_m is not None else None
            txt_type = txt_types[i] if i < len(txt_types) else ''
            wt_type = wt_types[i] if i < len(wt_types) else ''
            match = None
            if wt_len is None or txt_len_mm is None:
                match = False
            else:
                match = abs(wt_len - txt_len_mm) <= TOLERANCE_MM
            rows.append({
                'WorkTicket_Row': i+1,
                'WorkTicket_Length_mm': wt_len if wt_len is not None else '',
                'WorkTicket_Type': wt_type,
                'TextFile_Batten': i+1,
                'TextFile_Length_mm': txt_len_mm if txt_len_mm is not None else '',
                'TextFile_Type': txt_type,
                'Match': match
            })
        df = pd.DataFrame(rows)
        return df

processor = FileProcessor()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/battens-pocket')
def battens_pocket():
    return render_template('battens_pocket.html')

@app.route('/other-criteria')
def other_criteria():
    return render_template('other_criteria.html')


@app.route('/other_criteria_json')
def other_criteria_json():
    """Return data for Other special criteria table (Strips count, Spreader Patches, etc.).
    Spreader Patches: Text file = 'Sprd' keyword (e.g. Sprd1, Sprd2); Work Ticket = 'Spreader Patches' in EXTRAS only (ignore Spreader Patches Location, Mainsail Tip Spreader Patches)."""
    txt_data = processor.data.get('txt_file', {}) or {}
    taping_data = processor.data.get('taping_report', {}) or {}
    wt_data = processor.data.get('work_ticket', {}) or {}
    def _has_value(v):
        return v is not None and str(v).strip() != ''

    strips_txt = txt_data.get('Strips_Count')
    strips_tape = taping_data.get('Strips_Count')
    strips_wt = wt_data.get('Strips_Count')
    width_cells_wt = wt_data.get('Width_Cells_Count')  # Count of cells with "mm" in Width column under Trim Stripes (fallback)
    # Strips count row: Work Ticket column should show strips_wt (cells under the '#' column).
    # width_cells_wt remains available as a fallback when strips_wt couldn't be determined.
    strips_any = _has_value(strips_txt) or _has_value(strips_tape) or _has_value(strips_wt)
    strips_status = '—'
    if strips_any:
        # compare text file value to the work ticket strips count
        strips_status = '✓' if (strips_txt is not None and strips_wt is not None and str(strips_txt) == str(strips_wt)) else '✗'
    else:
        strips_status = '-'

    # Exterior Tape: extraction and UI placement only (no matching logic until criteria provided).
    # Work Ticket column: exact phrase from EXTERIOR_TAPE_WORKTICKET_PHRASES found in Work Ticket (first occurrence).
    # Tape Layout column: first integer from Item column on first page of Taping Report table.
    exterior_tape_txt = txt_data.get('Exterior_Tape')
    exterior_tape_tape = taping_data.get('Exterior_Tape')
    exterior_tape_wt = wt_data.get('Exterior_Tape')

    # Do NOT perform matching or comparison for Exterior Tape until matching criteria are provided.
    exterior_tape_status = '-'

    # Reefs Count: Work Ticket from EXTRAS (N× Standard reef, N× 3Di IBF reef, N× Reef); TXT from Reef1, Reef2… Compare: match → ✓, mismatch → ✗.
    reefs_txt = txt_data.get('Reefs_Count')
    reefs_tape = taping_data.get('Reefs_Count')
    reefs_wt = wt_data.get('Reefs_Count')
    reefs_status = '-'
    if reefs_txt is not None and reefs_wt is not None:
        reefs_status = '✓' if int(reefs_txt) == int(reefs_wt) else '✗'
    elif reefs_txt is not None or reefs_wt is not None:
        reefs_status = '✗'

    # Spreader Patches: text file = keyword "Spread"; work ticket = "Spreader Patches" in EXTRAS only (ignore Spreader Patches Location, Mainsail Tip Spreader Patches). Status ✓ only when BOTH have it; if only one has it → ✗; neither → '-'.
    spreader_txt = txt_data.get('Spreader_Patches')
    spreader_tape = taping_data.get('Spreader_Patches')
    spreader_ticket = wt_data.get('Spreader_Patches')
    spreader_any = _has_value(spreader_txt) or _has_value(spreader_ticket)
    spreader_status = '-'
    if spreader_any:
        spreader_status = '✓' if (_has_value(spreader_txt) and _has_value(spreader_ticket)) else '✗'

    helix_txt = txt_data.get('Helix_Structure')
    helix_tape = taping_data.get('Helix_Structure')
    helix_ticket = wt_data.get('Helix_Structure')
    helix_any = _has_value(helix_txt) or _has_value(helix_tape) or _has_value(helix_ticket)
    # Helix must be in BOTH work ticket AND taping report; otherwise show ✗
    helix_status = '-'
    if helix_any:
        helix_status = '✓' if (_has_value(helix_ticket) and _has_value(helix_tape)) else '✗'

    cunningham_txt = txt_data.get('Cunningham')
    cunningham_tape = taping_data.get('Cunningham')
    cunningham_ticket = wt_data.get('Cunningham')
    has_cun_txt = _has_value(cunningham_txt)
    has_cun_tape = _has_value(cunningham_tape)
    has_cun_ticket = _has_value(cunningham_ticket)
    if has_cun_txt and has_cun_tape and has_cun_ticket:
        cunningham_status = '✓'
    elif has_cun_txt or has_cun_tape or has_cun_ticket:
        cunningham_status = '✗'
    else:
        cunningham_status = '-'

    roller_txt = txt_data.get('Roller_Reefing')
    roller_tape = taping_data.get('Roller_Reefing')
    roller_ticket = wt_data.get('Roller_Reefing')
    roller_any = _has_value(roller_txt) or _has_value(roller_tape) or _has_value(roller_ticket)
    roller_status = '-'
    if roller_any:
        roller_status = '✓'

    tt_windows_txt = txt_data.get('TT_Windows')
    tt_windows_tape = taping_data.get('TT_Windows')
    tt_windows_ticket = wt_data.get('TT_Windows')
    # TT Windows: no comparison with text file or taping report; only show work ticket value, status ✓ when work ticket has value
    tt_windows_status = '✓' if _has_value(tt_windows_ticket) else '-'

    stachion_txt = txt_data.get('Stachion_Patches')
    stachion_tape = taping_data.get('Stachion_Patches')
    stachion_ticket = wt_data.get('Stachion_Patches')
    # Stachion Patches: no comparison with text file or taping report; only show work ticket value, status ✓ when work ticket has value
    stachion_status = '✓' if _has_value(stachion_ticket) else '-'

    return jsonify({
        'exterior_tape_txt': exterior_tape_txt,
        'exterior_tape_tape': exterior_tape_tape,
        'exterior_tape_wt': exterior_tape_wt,
        'exterior_tape_status': exterior_tape_status,
        'strips_count_txt': strips_txt,
        'strips_count_tape': strips_tape,
        'strips_count_wt': strips_wt,
        'width_cells_wt': width_cells_wt,
        'strips_status': strips_status,
        'reefs_count_txt': reefs_txt,
        'reefs_count_tape': reefs_tape,
        'reefs_count_wt': reefs_wt,
        'reefs_status': reefs_status,
        'spreader_txt': spreader_txt,
        'spreader_tape': spreader_tape,
        'spreader_ticket': spreader_ticket,
        'spreader_status': spreader_status,
        'helix_txt': helix_txt,
        'helix_tape': helix_tape,
        'helix_ticket': helix_ticket,
        'helix_status': helix_status,
        'cunningham_txt': cunningham_txt,
        'cunningham_tape': cunningham_tape,
        'cunningham_ticket': cunningham_ticket,
        'cunningham_status': cunningham_status,
        'roller_txt': roller_txt,
        'roller_tape': roller_tape,
        'roller_ticket': roller_ticket,
        'roller_status': roller_status,
        'tt_windows_txt': tt_windows_txt,
        'tt_windows_tape': tt_windows_tape,
        'tt_windows_ticket': tt_windows_ticket,
        'tt_windows_status': tt_windows_status,
        'stachion_txt': stachion_txt,
        'stachion_tape': stachion_tape,
        'stachion_ticket': stachion_ticket,
        'stachion_status': stachion_status,
    })


FILE_TYPE_TO_DATA_KEY = {'txt': 'txt_file', 'taping': 'taping_report', 'ticket': 'work_ticket'}

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    file_type = request.form.get('file_type')
    
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file_type not in FILE_TYPE_TO_DATA_KEY:
        return jsonify({'error': 'Invalid or missing file_type. Use: txt, taping, or ticket'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400
    
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    key = file_type
    old_name = processor.data.get('file_names', {}).get(key)
    if old_name and old_name != filename:
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], old_name))
        except OSError as e:
            logging.warning("Failed to remove previous upload %s: %s", old_name, e)
    file.save(filepath)
    
    try:
        if file_type == 'txt':
            processor.data['txt_file'] = processor.parse_txt_file(filepath)
            processor.data['file_names']['txt'] = filename
        elif file_type == 'taping':
            processor.data['taping_report'] = processor.parse_pdf_report(filepath, is_work_ticket=False)
            processor.data['file_names']['taping'] = filename
        elif file_type == 'ticket':
            ext = filename.rsplit('.', 1)[-1].lower()
            df = None
            if ext == 'xlsx':
                try:
                    df = pd.read_excel(filepath)
                except Exception as e:
                    logging.warning("Failed to parse work ticket Excel: %s", e)
                    processor.data['work_ticket'] = {}
            elif ext == 'csv':
                try:
                    df = pd.read_csv(filepath)
                except Exception as e:
                    logging.warning("Failed to parse work ticket CSV: %s", e)
                    processor.data['work_ticket'] = {}
            if df is not None:
                strips = get_strips_count_from_dataframe(df)
                width_cells = get_width_cells_count_from_dataframe(df)
                spreader_wt = get_spreader_from_dataframe(df)
                cunningham_wt = get_cunningham_from_dataframe(df)
                helix_wt = get_helix_from_dataframe(df)
                tt_windows_wt = get_tt_windows_from_dataframe(df)
                stanchion_wt = get_stanchion_from_dataframe(df)
                wt_data = {}
                oe_from_filename = get_oe(filename)
                if oe_from_filename:
                    wt_data['OE Number'] = oe_from_filename
                if strips is not None:
                    wt_data['Strips_Count'] = strips
                if width_cells is not None:
                    wt_data['Width_Cells_Count'] = width_cells
                if spreader_wt is not None:
                    wt_data['Spreader_Patches'] = spreader_wt
                if cunningham_wt is not None:
                    wt_data['Cunningham'] = cunningham_wt
                if helix_wt is not None:
                    wt_data['Helix_Structure'] = helix_wt
                if tt_windows_wt is not None:
                    wt_data['TT_Windows'] = tt_windows_wt
                if stanchion_wt is not None:
                    wt_data['Stachion_Patches'] = stanchion_wt
                # Exterior Tape: exact phrase search in work ticket text (first occurrence)
                wt_text = " ".join(df.astype(str).values.flatten())
                exterior_tape_wt = get_exterior_tape_from_workticket(wt_text)
                if exterior_tape_wt is not None:
                    wt_data['Exterior_Tape'] = exterior_tape_wt
                processor.data['work_ticket'] = wt_data
            elif ext not in ('xlsx', 'csv'):
                processor.data['work_ticket'] = processor.parse_pdf_report(filepath, is_work_ticket=True)
            processor.data['file_names']['ticket'] = filename
        
        data_key = FILE_TYPE_TO_DATA_KEY[file_type]
        return jsonify({
            'success': True,
            'filename': filename,
            'data': processor.data[data_key]
        })
    except Exception as e:
        logging.exception("Upload processing failed for %s", filename)
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except OSError:
            pass
        return jsonify({'error': 'Failed to process file. See server log.'}), 500

@app.route('/delete_file', methods=['POST'])
def delete_file():
    """Clear uploaded file data for a specific file type"""
    data = request.get_json()
    file_type = data.get('file_type') if data else None
    
    if file_type == 'txt':
        # remove parsed data and stored filename
        filename = processor.data.get('file_names', {}).get('txt')
        processor.data['txt_file'] = {}
        processor.data['file_names']['txt'] = None
        if filename:
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            except OSError as e:
                logging.warning("Failed to delete file %s: %s", filename, e)
    elif file_type == 'taping':
        filename = processor.data.get('file_names', {}).get('taping')
        processor.data['taping_report'] = {}
        processor.data['file_names']['taping'] = None
        if filename:
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            except OSError as e:
                logging.warning("Failed to delete file %s: %s", filename, e)
    elif file_type == 'ticket':
        filename = processor.data.get('file_names', {}).get('ticket')
        processor.data['work_ticket'] = {}
        processor.data['file_names']['ticket'] = None
        if filename:
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            except OSError as e:
                logging.warning("Failed to delete file %s: %s", filename, e)
    else:
        return jsonify({'error': 'Invalid file type'}), 400
    
    return jsonify({'success': True})

@app.route('/run_checking', methods=['POST'])
def run_checking():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid or missing JSON body'}), 400
    try:
        raw = data.get('tolerance', 0.01)
        tolerance = float(raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'tolerance must be a number'}), 400
    if not (0 <= tolerance <= 1):
        return jsonify({'error': 'tolerance must be between 0 and 1'}), 400
    results = processor.compare_files(tolerance)
    return jsonify(results)


@app.route('/uploaded_status', methods=['GET'])
def uploaded_status():
    """Return which files are currently uploaded and parsed (used to persist UI state)."""
    file_names = processor.data.get('file_names', {'txt': None, 'taping': None, 'ticket': None})
    parsed = {
        'txt_file': processor.data.get('txt_file', {}),
        'taping_report': processor.data.get('taping_report', {}),
        'work_ticket': processor.data.get('work_ticket', {})
    }
    return jsonify({'files': file_names, 'parsed': parsed})

@app.route('/download_report')
def download_report():
    # Create Excel report
    results = processor.compare_files()
    df = pd.DataFrame(results)
    
    # Use OE Number in filename if available (from any source)
    oe = (
        processor.data['txt_file'].get('OE Number') or
        processor.data['taping_report'].get('OE Number') or
        processor.data['work_ticket'].get('OE Number')
    )
    if oe and oe != '-':
        download_name = f"{oe}_comparison_report.xlsx"
    else:
        download_name = 'comparison_report.xlsx'
    
    # Create Excel file in memory
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Results', index=False)
    
    output.seek(0)
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=download_name
    )


@app.route('/export_batten_mapping')
def export_batten_mapping():
    """Create a CSV mapping of battens (work ticket top->down → text file bottom->up)
    and save it under the `uploads/` folder as `batten_mapping.csv`, then return it.
    """
    df = processor.generate_batten_mapping()
    # Save to uploads
    out_path = os.path.join(app.config['UPLOAD_FOLDER'], 'batten_mapping.csv')
    try:
        df.to_csv(out_path, index=False)
    except OSError as e:
        logging.warning("Failed to save batten_mapping.csv to %s: %s", out_path, e)

    # Return CSV to user
    csv_buf = io.BytesIO()
    csv_buf.write(df.to_csv(index=False).encode('utf-8'))
    csv_buf.seek(0)
    return send_file(
        csv_buf,
        mimetype='text/csv',
        as_attachment=True,
        download_name='batten_mapping.csv'
    )


@app.route('/batten_mapping_json')
def batten_mapping_json():
    """Return the batten mapping as JSON (list of rows) for the UI to render.
    Each row contains: WorkTicket_Row, WorkTicket_Length_mm, TextFile_Batten, TextFile_Length_mm, Match
    """
    df = processor.generate_batten_mapping()
    # convert DataFrame to list of dicts
    rows = df.to_dict(orient='records')
    return jsonify(rows)


def _tape_layout_to_mm(tape_layout):
    """Return mm (30, 40, 50, 70, 95, 125) if tape_layout is GPFL50D-XXH; else None.
    Only exact match: -70H matches, -170H and -270H do NOT match 70."""
    if not tape_layout:
        return None
    t = tape_layout.strip().upper().replace('_', '-')
    for mm in (125, 95, 70, 50, 40, 30):
        if t.endswith('-%sH' % mm) or re.match(r'^.*-\s*%s\s*H\s*$' % mm, t):
            return mm
    return None


@app.route('/pocket_types_json')
def pocket_types_json():
    """Pocket Types: WT/WT Count from Work Ticket Pocket column (table-based when available).
    Returns JSON: { "40mm": {"found": bool, "count": int}, ... } and pocket_rows for table render.
    If Pocket column not found in work ticket PDF, returns error for UI."""
    taping_list = processor.data.get('taping_report', {}).get('Pocket_List', []) or []
    wt_data = processor.data.get('work_ticket', {}) or {}
    wt_pocket_list = wt_data.get('Pocket_List', []) or []
    wt_length_mm_list = wt_data.get('Length_Column_Pocket_Mm', []) or []
    pocket_counts_by_size = wt_data.get('Pocket_Counts_By_Size')  # table-based counts (no duplicate)

    if wt_data.get('Pocket_Column_Found') is False:
        return jsonify({
            'error': 'Pocket column not found',
            'pocket_rows': [],
            '40mm': {'found': False, 'count': 0},
            '50mm': {'found': False, 'count': 0},
            '70mm': {'found': False, 'count': 0},
            '95mm': {'found': False, 'count': 0},
            '125mm': {'found': False, 'count': 0},
        })

    # Prefer table-based counts (row-based, no duplicate) when available
    if pocket_counts_by_size:
        counts_json = {
            '40mm': pocket_counts_by_size.get('40mm', {'found': False, 'count': 0}),
            '50mm': pocket_counts_by_size.get('50mm', {'found': False, 'count': 0}),
            '70mm': pocket_counts_by_size.get('70mm', {'found': False, 'count': 0}),
            '95mm': pocket_counts_by_size.get('95mm', {'found': False, 'count': 0}),
            '125mm': pocket_counts_by_size.get('125mm', {'found': False, 'count': 0}),
        }
    else:
        counts_json = {}
        for mm in POCKET_EXACT_MM:
            c = count_workticket_pocket_by_mm(wt_pocket_list, mm)
            counts_json["%smm" % mm] = {"found": c > 0, "count": c}

    use_length_for_status = bool(wt_length_mm_list)
    rows = []
    for taping in taping_list:
        tape_layout = (taping.get('tape_layout') or '').strip()
        pocket_count = (taping.get('pocket_count') or '').strip()
        mm = _tape_layout_to_mm(tape_layout)
        if pocket_counts_by_size and mm is not None and mm in POCKET_EXACT_MM:
            info = counts_json.get("%smm" % mm, {"found": False, "count": 0})
            work_ticket_count = info["count"]
        else:
            work_ticket_count = count_workticket_pocket_by_mm(wt_pocket_list, mm) if mm is not None else 0
        status_count = count_length_column_by_mm(wt_length_mm_list, mm) if use_length_for_status and mm is not None else work_ticket_count
        if status_count is not None and pocket_count != '':
            try:
                status = '✓' if int(pocket_count) == status_count else '✗'
            except (ValueError, TypeError):
                status = '✗'
        else:
            status = '-'
        wt_display = "%smm" % mm if (work_ticket_count and work_ticket_count > 0) else "-"
        wt_count_display = work_ticket_count if work_ticket_count is not None else 0
        if mm is not None and mm in POCKET_EXACT_MM:
            pass
        else:
            wt_display = "-"
            wt_count_display = 0
        rows.append({
            'tape_layout': tape_layout,
            'pocket_count': pocket_count,
            'wt': wt_display,
            'work_ticket_count': wt_count_display,
            'status': status,
        })

    # When no taping data, show 40/70/95/125 mm rows from Work Ticket only (table-based or list-based)
    if not rows:
        for mm in POCKET_EXACT_MM:
            info = counts_json.get("%smm" % mm, {"found": False, "count": 0})
            wt_display = "%smm" % mm if info["found"] else "-"
            rows.append({
                'tape_layout': '%smm' % mm,
                'pocket_count': '',
                'wt': wt_display,
                'work_ticket_count': info["count"],
                'status': '-',
            })

    out = {
        'pocket_rows': rows,
        '40mm': counts_json.get('40mm', {'found': False, 'count': 0}),
        '50mm': counts_json.get('50mm', {'found': False, 'count': 0}),
        '70mm': counts_json.get('70mm', {'found': False, 'count': 0}),
        '95mm': counts_json.get('95mm', {'found': False, 'count': 0}),
        '125mm': counts_json.get('125mm', {'found': False, 'count': 0}),
    }
    return jsonify(out)

def get_lan_ip():
    """Get this machine's LAN IP for access from other devices."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


if __name__ == '__main__':
    # host='0.0.0.0' allows access from other devices on your LAN
    lan_ip = get_lan_ip()
    print('-' * 50)
    print('Northsail running.')
    print('  On this PC:     http://127.0.0.1:5000')
    print('  From other PC: http://{}:5000'.format(lan_ip))
    print('-' * 50)
    app.run(host='0.0.0.0', port=5000, debug=True)

