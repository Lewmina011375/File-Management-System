import os
import re
import logging
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
from PyPDF2 import PdfReader
import io
import pytesseract

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


def read_pdf_all(path):
    """Read all text from PDF file"""
    reader = PdfReader(path)
    text = ""
    for page in reader.pages:
        part = page.extract_text()
        text += (part if part is not None else "") + " "
    return text


def read_pdf_pages(path):
    """Read PDF and return a list of page texts (one string per page)."""
    reader = PdfReader(path)
    return [page.extract_text() or "" for page in reader.pages]


def get_pocket_data_from_taping(filepath):
    """Extract Tape Layout (word after Item:) and Pocket count (number after Count:) from taping report.
    Only considers pages with headers GPFL50D_30, GPFL50D_40, GPFL50D_50, GPFL50D_70, GPFL50D_95, GPFL50D_125.
    Tape layout must be from the taping report: only accept Item: values that contain GPFL50D (so we do not
    pick up unrelated codes like LDX75 from elsewhere on the page).
    Returns a list of dicts: [ {tape_layout, pocket_count}, ... ] starting from Pocket 1.
    """
    try:
        pages = read_pdf_pages(filepath)
    except Exception as e:
        logging.warning("Failed to read taping PDF %s: %s", filepath, e)
        return []
    if len(pages) < 2:
        return []
    search_pages = pages[1:]
    target_headers = [
        'GPFL50D_30', 'GPFL50D_40', 'GPFL50D_50',
        'GPFL50D_70', 'GPFL50D_95', 'GPFL50D_125'
    ]
    result = []
    for header in target_headers:
        tape_layout = ''
        pocket_count = ''
        for page_text in search_pages:
            if header in page_text:
                # Item: may appear multiple times on a page; only accept value that matches taping tape layout (GPFL50D)
                for item_m in re.finditer(r'Item:\s*(\S+)', page_text, re.IGNORECASE):
                    candidate = item_m.group(1).strip()
                    if 'GPFL50D' in candidate.upper():
                        tape_layout = candidate
                        break
                count_m = re.search(r'Count:\s*(\d+)', page_text, re.IGNORECASE)
                if count_m:
                    pocket_count = count_m.group(1).strip()
                break
        # Only include pockets that have data so numbering starts at Pocket 1 (no empty rows at start)
        if tape_layout or pocket_count:
            result.append({'tape_layout': tape_layout, 'pocket_count': pocket_count})
    return result


def get_cunningham_from_taping(content):
    """Detect Cunningham in taping report: look for 'cun' in tape names under Resin (e.g. ReinS, Resin).
    Tapes appear as 'Tapes: Cunham' or similar. Returns the tape name containing 'cun' or 'Yes'; else None."""
    if not content:
        return None
    content_lower = content.lower()
    if 'resin' not in content_lower and 'reins' not in content_lower:
        return None
    for m in re.finditer(r"Tapes:\s*(\S+)", content, re.IGNORECASE):
        tape_name = (m.group(1) or '').strip()
        if tape_name and re.search(r"cun", tape_name, re.IGNORECASE):
            return tape_name
    if re.search(r"Tapes:\s*[A-Za-z]*cun[A-Za-z]*", content, re.IGNORECASE):
        return 'Yes'
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

def get_measurements_txt(text):
    """Extract measurements from text file using Msm line"""
    lines = text.split("\n")
    msm_line = next((l for l in lines if re.match(r"^\s*Msm", l, re.I)), "")
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


def get_strips_count_from_txt(text):
    """Count how many times the word SS appears under the Marks section in the text file.
    Marks has lines like SS2/8 :, SS4/8 :, SS6/8 :, SS7/8 :. Each such line is one SS occurrence.
    Returns the count (int) or None."""
    if not text:
        return None
    lines = text.splitlines()
    in_marks = False
    count = 0
    for l in lines:
        stripped = l.strip()
        if re.match(r"Marks\s*:", stripped, re.IGNORECASE):
            in_marks = True
            continue
        if in_marks:
            if not stripped:
                break
            # Count lines that start with SS (e.g. SS2/8, SS4/8, SS6/8, SS7/8)
            if re.match(r"^SS\d*/?\d*", stripped, re.IGNORECASE):
                count += 1
            elif re.match(r"^[A-Za-z]", stripped) and not stripped.upper().startswith("SS"):
                break
    return count if count > 0 else None


def get_cunningham_from_txt(text):
    """Detect Cunningham in text file: look for a word containing 'cun' in the Marks section
    (e.g. 'Cungh :' or 'Cunningham :'). Returns the label (e.g. 'Cungh') or 'Yes' if found, else None."""
    if not text:
        return None
    lines = text.splitlines()
    in_marks = False
    for l in lines:
        stripped = l.strip()
        if re.match(r"Marks\s*:", stripped, re.IGNORECASE):
            in_marks = True
            continue
        if in_marks:
            if not stripped:
                break
            if re.match(r"^SS\d*/?\d*", stripped, re.IGNORECASE):
                continue
            # Word containing 'cun' followed by optional spaces and colon (e.g. Cungh :, Cunningham :)
            m = re.search(r"([A-Za-z]*cun[A-Za-z]*)\s*:", stripped, re.IGNORECASE)
            if m:
                return m.group(1).strip() or 'Yes'
            if re.match(r"^[A-Za-z]", stripped) and not stripped.upper().startswith("SS"):
                break
    return None


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


def get_spreader_from_txt(text):
    """Find keyword 'Spread' in text file (e.g. 'Spread:', 'Sprd1 :', 'Sprd2 :'). Prefer 'Spread' then Sprd labels."""
    if not text:
        return None
    # Match the word "Spread" (e.g. "Spread:" on its own line)
    if re.search(r"\bSpread\b", text, re.IGNORECASE):
        return 'Spread'
    # Fallback: Sprd as label (e.g. "Sprd1 :", "Sprd2 :")
    m = re.search(r"\b(Sprd\d*)\s*:", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if re.search(r"\bSprd\b", text, re.IGNORECASE):
        return 'Yes'
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
    """If a word containing 'cun' (e.g. Cunningham) appears in EXTRAS section, return that word or 'Yes'; else None."""
    extras = _get_extras_section(text)
    m = re.search(r"\b([A-Za-z]*cun[A-Za-z]*)\b", extras, re.IGNORECASE)
    if m:
        return m.group(1).strip() or 'Yes'
    return None


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


def get_cunningham_from_dataframe(df):
    """If any cell contains a word with 'cun' (e.g. Cunningham) (case-insensitive), return that word or 'Yes'; else None."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    for c in df.columns:
        for v in df[c].dropna().astype(str):
            m = re.search(r"\b([A-Za-z]*cun[A-Za-z]*)\b", str(v), re.IGNORECASE)
            if m:
                return m.group(1).strip() or 'Yes'
    return None


def get_helix_from_workticket(text):
    """If 'Helix' appears in EXTRAS section (e.g. 'HELIX Structured Luff'), return 'Helix'; else None."""
    extras = _get_extras_section(text)
    if re.search(r"\bhelix\b", extras, re.IGNORECASE):
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
    """If any cell contains 'Helix' (case-insensitive), return 'Helix'; else None. For Excel/CSV work tickets."""
    if df is None or df.empty:
        return None
    df = pd.DataFrame(df)
    for c in df.columns:
        s = df[c].astype(str).str.strip()
        if s.str.contains(r'helix', case=False, na=False).any():
            return 'Helix'
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


def get_batten_list_from_text(text):
    """Extract batten types from the text file in file order and return them bottom->top.
    Example lines in text file end with 'Short Batten' or 'Full Length Batten'.
    We collect lines that contain 'batten' or 'bat#' and detect 'short' or 'full'.
    Return list ordered bottom-to-top (reverse of file appearance).
    """
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    matches = []
    for l in lines:
        if re.search(r"bat(?:ten)?\b|bat#", l, re.IGNORECASE):
            lower = l.lower()
            if 'short' in lower:
                matches.append('Short')
            elif 'full' in lower:
                # treat 'full length' or 'full' as Full
                matches.append('Full')
            else:
                # fallback: try to find known keywords
                for kw in ('leech', 'vertical', 'roller', 'flutter'):
                    if kw in lower:
                        matches.append(kw.capitalize())
                        break
    # file top->bottom, we need bottom->top
    return list(reversed(matches))


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
    """Extract batten lengths (meters) from the text file Length column only.
    Returns list in file order (top->down). For matching with work ticket, caller
    reverses this list so that Batten 1 = bottom of column (read downward to upward).
    Only lines that are clearly from the batten table (contain Bat# and Short/Full Length Batten)
    are used, so we do not pick up numbers from Marks, Msm, or other tables.
    """
    if not text:
        return []
    lengths = []
    for l in text.splitlines():
        # Must be a batten table row: has Bat# and ends with "Short Batten" or "Full Length Batten"
        if not re.search(r"Bat#?\s*\d|Batts?\s*:", l, re.IGNORECASE):
            continue
        if not re.search(r"Short\s+Batten|Full\s+Length\s+Batten", l, re.IGNORECASE):
            continue
        nums = re.findall(r"([0-9]+\.[0-9]+)", l)
        if nums:
            # Length column is the last float on the line (after Luff, Leech, Girth)
            try:
                lengths.append(float(nums[-1]))
            except ValueError:
                continue
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


def get_pocket_list_from_workticket(text):
    """Extract Pocket column values from the work ticket BATTENS & POCKETS table.
    Returns one pocket value per batten row (same order as batten lengths, top->down).
    Pocket values are codes like '901008 (125mm)', '901006 (70mm)', 'BPF63BK', etc.
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
        # Pocket column: prefer "901008 (125mm)" or "901006 (70mm)" (code + size); else "BPF63BK" style
        pocket_val = None
        # First try code with (Nmm) so we don't take Batten column (e.g. CT31)
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
        # Do not show non-pocket codes (e.g. LDX75 is a product/spec code, not a pocket)
        if pocket_val and (pocket_val.upper() == 'LDX75' or pocket_val.upper().startswith('LDX')):
            pocket_val = ''
        pockets.append(pocket_val or "")
    return pockets


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
            
            # Extract Batten Type from txt file (Short / Full / Full/Short)
            batten_txt = get_batten_from_text(content)
            if batten_txt:
                data['Batten Type'] = batten_txt
            # Extract batten list (bottom->top) for per-batten comparison
            batten_list = get_batten_list_from_text(content)
            if batten_list:
                data['Batten_List'] = batten_list
            # Extract batten lengths (file order top->down)
            batten_lengths = get_batten_lengths_from_txt(content)
            if batten_lengths:
                data['Batten_Lengths_m'] = batten_lengths
            strips_count = get_strips_count_from_txt(content)
            if strips_count is not None:
                data['Strips_Count'] = strips_count
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
                # Extract Pocket column per batten row for Pocket Types table
                pocket_list_wt = get_pocket_list_from_workticket(content)
                if pocket_list_wt:
                    data['Pocket_List'] = pocket_list_wt
                # Strips count: try OCR first (DOWNWIND/GENOA/MAINSAIL GRAPHICS), then text-based, then width fallback
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
                # Cunningham: only if word containing 'cun' (e.g. Cunningham) appears in EXTRAS
                cunningham_wt = get_cunningham_from_workticket(content)
                if cunningham_wt is not None:
                    data['Cunningham'] = cunningham_wt
                # Helix Structure: from EXTRAS (e.g. HELIX Structured Luff)
                helix_wt = get_helix_from_workticket(content)
                if helix_wt is not None:
                    data['Helix_Structure'] = helix_wt

            else:
                # Taping report: Cunningham from Resin / Tapes containing 'cun'
                cunningham_tape = get_cunningham_from_taping(content)
                if cunningham_tape is not None:
                    data['Cunningham'] = cunningham_tape
                # Helix Structure: on first page (e.g. product line with HELIX)
                helix_tape = get_helix_from_taping(filepath)
                if helix_tape is not None:
                    data['Helix_Structure'] = helix_tape

        except Exception as e:
            logging.warning("Error parsing PDF report %s: %s", filepath, e)
        return data
    
    def compare_files(self, tolerance=0.01):
        """Compare data from all three sources with improved accuracy"""
        results = []
        criteria = ['OE Number', 'DPI', 'Tier', 'Head', 'Luff', 'Leech', 'Foot', 'LP']
        
        for criterion in criteria:
            row = {
                'Criteria': criterion,
                'Text_File': self.data['txt_file'].get(criterion, '-'),
                'Taping_Report': self.data['taping_report'].get(criterion, '-'),
                'Work_Ticket': self.data['work_ticket'].get(criterion, '-')
            }
            
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
                txt_val = row['Text_File'] if row['Text_File'] != '-' else None
                wk_val = row['Work_Ticket'] if row['Work_Ticket'] != '-' else None
                if txt_val is None or wk_val is None:
                    row['Difference'] = 'INCOMPLETE'
                    row['Status'] = '?'
                else:
                    try:
                        t, w = float(txt_val), float(wk_val)
                        diff = round(w - t, 3)
                        row['Difference'] = f'{diff:.3f}'
                        row['Status'] = '✓' if abs(diff) <= tolerance else '✗'
                    except (TypeError, ValueError):
                        row['Difference'] = '-'
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
                # allowed mapping for work-ticket keywords
                allowed = {
                    'full': ['full'],
                    'vertical full': ['full', 'short'],
                    'vertical': ['short'],
                    'leech': ['short'],
                    'roller': ['short'],
                    'flutter': ['short']
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

        # Text file: Length column read downward-to-upward, so reverse file order
        txt_lengths_m_rev = list(reversed(txt_lengths_m))
        # Batten_List is already bottom->top for text; work ticket list is top->down
        txt_types = list(txt_batten_list)
        wt_types = list(wt_batten_list)

        # Match with 1 mm tolerance (rounding between m and mm)
        TOLERANCE_MM = 1
        max_len = max(len(wt_lengths), len(txt_lengths_m_rev))
        rows = []
        for i in range(max_len):
            wt_len = wt_lengths[i] if i < len(wt_lengths) else None
            txt_len_m = txt_lengths_m_rev[i] if i < len(txt_lengths_m_rev) else None
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

    # Spreader Patches: text file = Sprd (e.g. Sprd1, Sprd2); work ticket = "Spreader Patches" in EXTRAS only (exclude Location / Mainsail Tip)
    spreader_txt = txt_data.get('Spreader_Patches')
    spreader_tape = taping_data.get('Spreader_Patches')
    spreader_ticket = wt_data.get('Spreader_Patches')
    spreader_any = _has_value(spreader_txt) or _has_value(spreader_tape) or _has_value(spreader_ticket)
    spreader_status = '-'
    if spreader_any:
        spreader_status = '✓' if _has_value(spreader_ticket) else '✗'

    # Cunningham, Helix, Roller: status '-' when no value in any column
    cunningham_txt = txt_data.get('Cunningham')
    cunningham_tape = taping_data.get('Cunningham')
    cunningham_ticket = wt_data.get('Cunningham')
    cunningham_any = _has_value(cunningham_txt) or _has_value(cunningham_tape) or _has_value(cunningham_ticket)
    cunningham_status = '-'
    if cunningham_any:
        cunningham_status = '✓' if _has_value(cunningham_ticket) else '✗'

    helix_txt = txt_data.get('Helix_Structure')
    helix_tape = taping_data.get('Helix_Structure')
    helix_ticket = wt_data.get('Helix_Structure')
    helix_any = _has_value(helix_txt) or _has_value(helix_tape) or _has_value(helix_ticket)
    # Helix must be in BOTH work ticket AND taping report; otherwise show ✗
    helix_status = '-'
    if helix_any:
        helix_status = '✓' if (_has_value(helix_ticket) and _has_value(helix_tape)) else '✗'

    roller_txt = txt_data.get('Roller_Reefing')
    roller_tape = taping_data.get('Roller_Reefing')
    roller_ticket = wt_data.get('Roller_Reefing')
    roller_any = _has_value(roller_txt) or _has_value(roller_tape) or _has_value(roller_ticket)
    roller_status = '-'
    if roller_any:
        roller_status = '✓'

    return jsonify({
        'strips_count_txt': strips_txt,
        'strips_count_tape': strips_tape,
        'strips_count_wt': strips_wt,
        'width_cells_wt': width_cells_wt,
        'strips_status': strips_status,
        'spreader_txt': spreader_txt,
        'spreader_tape': spreader_tape,
        'spreader_ticket': spreader_ticket,
        'spreader_status': spreader_status,
        'cunningham_txt': cunningham_txt,
        'cunningham_tape': cunningham_tape,
        'cunningham_ticket': cunningham_ticket,
        'cunningham_status': cunningham_status,
        'helix_txt': helix_txt,
        'helix_tape': helix_tape,
        'helix_ticket': helix_ticket,
        'helix_status': helix_status,
        'roller_txt': roller_txt,
        'roller_tape': roller_tape,
        'roller_ticket': roller_ticket,
        'roller_status': roller_status,
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


@app.route('/pocket_types_json')
def pocket_types_json():
    """Return pocket data for the Pocket Types table (Pocket Name and Tape Layout Count from taping report only)."""
    taping_list = processor.data.get('taping_report', {}).get('Pocket_List', []) or []
    rows = []
    for taping in taping_list:
        tape_layout = (taping.get('tape_layout') or '').strip()
        pocket_count = (taping.get('pocket_count') or '').strip()
        rows.append({
            'tape_layout': tape_layout,
            'pocket_count': pocket_count
        })
    return jsonify(rows)

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

