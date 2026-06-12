import fitz
import cv2
import pytesseract
import numpy as np
import re
from pathlib import Path
from collections import defaultdict
import difflib
import pandas as pd
import traceback


pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Users\intern.it\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
)

# ---------------- KEYWORDS ----------------
TAX_KEYWORDS = ["LESS", "ROFF", "TOTAL", "ROUND OFF", "ROUND O F F"]
PER_ALLOWED = {"PKT","CTN", "NOS", "BOX", "BAG", "PCS", "PC", "KG", "LTR", "ML", "GM"}

FORBIDDEN_PATTERNS = re.compile(r"\b(cest|cgst|sgst|r\/?off|less)\b",re.IGNORECASE)

import pyodbc

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=192.168.2.41;"
    "DATABASE=test_sql;"
    "UID=intern.it;"
    "PWD=Int@321;"
)

cursor = conn.cursor()

final_rows = []

keyword = ["HSN"]

gst_pat = r'\b\d{2}[A-Z]{5}\d{4}[A-Z]\dZ[A-Z0-9]\b'
invoice_pat = r'(invoice\s*(no|number|num|#)\.?\s*[:\-]?\s*)(.*)'

strict_date_pat = r'\b\d{1,2}[\/\-\.]\d{1,2}[\/\-\.](?:2\d{3}|2\d)\b'
loose_date_pat = r'\b\d{1,2}(?:st|nd|rd|th)?[\/\-\s\.]?[A-Za-z]{3,9}[\/\-\s\.]?(?:2\d{3}|2\d)\b'

# NEW KEYWORDS
bill_keywords = ["bill to", "billed to", "billing address", "party name", "to,", 'party', "m/s"]
ship_keywords = ["ship to", "shipping address", "consignee"]
save_path = Path(r'C:\Users\intern.it\Desktop')



def safe_bounds(col):
    if not col:
        return None
    left = col.get("left")
    right = col.get("right")

    if left is None:
        return None

    if right is None:
        right = 10**9   # safe fallback instead of None

    return {"left": left, "right": right}

def has_min_alphabets(text, min_count=3):
    return len(re.findall(r"[A-Za-z]", text)) >= min_count

def fix_ocr(text):
    text = text.replace("ILTR", "1LTR")
    text = text.replace("I 12", "112")
    text = text.replace("lLtr", "1Ltr")
    text = re.sub(r"(?<=-)I\b", "1", text)
    text = re.sub(r"\bI(?=\d)", "1", text)
    return text

def clean_description(text):

    text = text.replace("\\", " ")
    text = text.replace(",", " ")

    # ---------------- REMOVE LEADING JUNK ----------------
    text = re.sub(r"^\s*\d+(\.\d+)?\s*", "", text)     # 2, 2.5 etc
    text = re.sub(r"^\s*\d+[a-zA-Z]+\s*", "", text)    # 2g, 12kg, 180ml etc
    text = re.sub(r"^\s*[-:/]+\s*", "", text)

    # ---------------- REMOVE LONG NUMBERS ----------------
    text = re.sub(r"\b\d{8}\b", " ", text)
    text = re.sub(r"\b\d+\.\d+\b", " ", text)

    # ---------------- OCR NORMALIZATION (IMPORTANT ADDITION) ----------------
    # PSP150GR → PSP 150 GR
    text = re.sub(r"([A-Za-z]+)(\d+)", r"\1 \2", text)
    text = re.sub(r"(\d+)([A-Za-z]+)", r"\1 \2", text)

    # ---------------- CLEAN SPACING ----------------
    text = re.sub(r"\s+", " ", text).strip()

    # ---------------- DROP SINGLE-CHAR FIRST WORD ----------------
    words = text.split()

    if words and len(words[0]) == 1:
        words = words[1:]

    return " ".join(words)

def find_from_sql(desc):
    desc = fix_ocr(desc)
    desc = re.sub(r"-", " ", desc)
    desc = re.sub(r",", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    if not has_min_alphabets(desc, 3):
        return "NOT FOUND", "NOT FOUND"

    words = desc.split()

    for i in range(len(words), 0, -1):
        reduced = " ".join(words[:i])
        if len(reduced.replace(" ", "")) < 3:
            continue

        query = """
        SELECT TOP 1 [*Name],  [RlProductSKUId]
        FROM Vw_ProductMaster
        WHERE bot_product_name LIKE ?
        """
        cursor.execute(query, ('%' + reduced + '%',))
        row = cursor.fetchone()

        if row:
            return row[0], row[1]

    return "NOT FOUND", "NOT FOUND"



def extract_date(text):

    text = re.sub(r"\s+", " ", text)

    ignore_patterns = [
        r'printed\s+on',
        r'print\s+date',
        r'generated\s+on',
        r'created\s+on'
    ]

    cleaned = text

    # remove known noise lines safely
    for pat in ignore_patterns:
        cleaned = re.sub(
            rf"{pat}.{{0,40}}?(?:{strict_date_pat}|{loose_date_pat})",
            "",
            cleaned,
            flags=re.I
        )

    # ONLY real invoice date anchors (strict)
    anchors = [
        r"invoice\s*date",
        r"inv\.?\s*date",
        r"bill\s*date",
        r"dated"
    ]

    for line in cleaned.splitlines():

        low = line.lower()

        if any(a in low for a in anchors):

            m = re.search(strict_date_pat, line) or re.search(loose_date_pat, line)

            if m:
                return m.group()

    # fallback: scan nearby context lines
    for line in cleaned.splitlines():

        m = re.search(strict_date_pat, line) or re.search(loose_date_pat, line)

        if m and not re.search(r"total|amount|gst|invoice\s*no", line, re.I):
            return m.group()

    return "not found"

def extract_name(text, keywords, folder_name):

    business_words = {
        "enterprises", "traders", "distributors", "pharma",
        "industries", "corporation", "company", "suppliers",
        "wholesale", "retail", "agency", "logistics"
    }

    normalized_folder = re.sub(r'[^a-z0-9]', '', folder_name.lower())
    lines = text.split("\n")

    for kw in keywords:

        for i, line in enumerate(lines):

            if re.search(kw, line, re.I):

                for next_line in lines[i+1:i+10]:

                    clean = next_line.strip()

                    clean = re.sub(
                        r"(gstin|gst|invoice|date|total|amount|state|phone|mobile|email)",
                        "",
                        clean,
                        flags=re.I
                    )

                    clean = re.sub(r"\b\d{4,}\b", "", clean)
                    clean = re.sub(r"[^A-Za-z0-9\s\.,&/-]", "", clean).strip()

                    if len(clean) < 3:
                        continue

                    words = clean.lower().split()

                    # strong address rejection
                    address_flags = {
                        "road", "street", "lane", "nagar", "colony",
                        "floor", "building", "near", "opp", "behind",
                        "pin", "district"
                    }

                    if any(w in address_flags for w in words):
                        continue

                    # must look like business OR have proper capitalization structure
                    valid_structure = (
                        any(w in business_words for w in words)
                        or sum(1 for w in words if w[0].isupper()) >= 2
                    )

                    if not valid_structure:
                        continue

                    normalized_clean = re.sub(r'[^a-z0-9]', '', clean.lower())

                    if normalized_clean == normalized_folder:
                        continue

                    return clean.title()

    return "not found"

def detect_pattern(text):
    return "".join(["D" if c.isdigit() else "A" if c.isalpha() else c for c in text])

def is_valid_value(text):
    text = text.strip()
    return bool(text) and not text.isalpha()

def clean_text(v):
    if isinstance(v, str):
        return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', v)
    return v


all_rows = []

def detect_table_lines(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    thresh = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        15, 10
    )

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1))

    detected = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=2)

    contours, _ = cv2.findContours(
        detected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    lines = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)

        if w > img.shape[1] * 0.4:  # only strong horizontal rules
            lines.append((x, y, w, h))

    return sorted(lines, key=lambda x: x[1])

def is_float_number(txt):
    txt = txt.strip().replace(",", "")

    # must contain digits
    if not re.search(r"\d", txt):
        return False

    if txt.count(".") > 1:
        parts = txt.split(".")
        txt = "".join(parts[:-1]) + "." + parts[-1]

    return bool(re.fullmatch(r"\d+\.\d+", txt))

def is_hsn_code(txt):
    txt = txt.strip()

    # match 6–8 digit numbers (safe for GST invoices)
    return bool(re.fullmatch(r"\d{6,8}", txt))

def expand_bounds(col, pad=15):
    if not col:
        return None
    return {
        "left": col["left"] - pad,
        "right": col["right"] + pad if col["right"] else None
    }

def is_header_word(txt):
    txt = norm(txt)

    targets = [
        "shipped", "quantity", "qty", "amount",
        "rate", "hsn", "per", "disc"
    ]

    for t in targets:
        if t in txt:
            return True

    return False

def detect_vertical_lines(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV, 15, 10
    )

    # vertical kernel (important part)
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))

    detect_lines = cv2.morphologyEx(
        thresh, cv2.MORPH_OPEN, vertical_kernel, iterations=2
    )

    contours, _ = cv2.findContours(
        detect_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    lines = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if h > 50:  # filter noise
            lines.append((x, y, w, h))

    return lines

def detect_horizontal_bounds(img, ocr):

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    thresh = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        15,
        10
    )

    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (40, 1)
    )

    detect_lines = cv2.morphologyEx(
        thresh,
        cv2.MORPH_OPEN,
        horizontal_kernel,
        iterations=2
    )

    contours, _ = cv2.findContours(
        detect_lines,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    h_lines = []

    for c in contours:

        x, y, w, h = cv2.boundingRect(c)

        if w > 80:
            h_lines.append((x, y, w, h))

    if not h_lines:
        return 0, img.shape[0]

    # ---------------- FIND HEADER Y ----------------

    header_keywords = {
        "quantity",
        "qty",
        "amount",
        "rate",
        "hsn",
        "per",
        "disc%",
        "disc"
    }

    header_y = None

    for i in range(len(ocr["text"])):

        text = str(ocr["text"][i]).strip().lower()

        if text in header_keywords:

            y = ocr["top"][i]

            if header_y is None:
                header_y = y
            else:
                header_y = min(header_y, y)

    # fallback
    if header_y is None:
        top = min(y for _, y, _, _ in h_lines)
        bottom = max(y + h for _, y, w, h in h_lines)
        return top, bottom

    # ---------------- FIND CLOSEST LINE ABOVE HEADER ----------------

    lines_above = []

    for x, y, w, h in h_lines:

        if y <= header_y:
            lines_above.append(y)

    if lines_above:
        top = max(lines_above)
    else:
        top = min(y for _, y, _, _ in h_lines)

    # ---------------- FIND TABLE END ----------------

    lines_below = []

    for x, y, w, h in h_lines:

        if y > header_y:
            lines_below.append(y + h)

    if lines_below:
        bottom = max(lines_below)
    else:
        bottom = img.shape[0]

    return top, bottom

def find_vertical_borders(x, lines, img_width, margin=80):
    left = None
    right = None

    # nearest line on left side
    for lx, ly, lw, lh in lines:
        if lx < x and (x - lx) < margin:
            if left is None or lx > left:
                left = lx

    # nearest line on right side
    for lx, ly, lw, lh in lines:
        if lx > x and (lx - x) < margin:
            if right is None or lx < right:
                right = lx

    # fallback if no line found
    if left is None:
        left = 0
    if right is None:
        right = img_width - 1

    return left, right

def norm(text):
    return re.sub(r"[^A-Z]", "", text.upper())

# ---------------- OCR ----------------
def get_ocr(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    return pytesseract.image_to_data(
        gray,
        output_type=pytesseract.Output.DICT,
        config="--psm 6"
    )

# ---------------- ROW GROUP ----------------
def get_row(y, tol=20):
    return int(y // tol)

# ---------------- PROCESS PAGE ----------------
def process_page(img):

    ocr = get_ocr(img)
    h, w = img.shape[:2]

    lines = detect_vertical_lines(img)

    amount_col = safe_bounds(expand_bounds(find_amount_column(ocr, lines)))
    discount_col = safe_bounds(expand_bounds(find_discount_column(ocr, lines)))
    per_col = safe_bounds(expand_bounds(find_per_column(ocr, lines)))
    qty_col = safe_bounds(expand_bounds(find_qty_column(ocr, lines)))
    rate_col = safe_bounds(expand_bounds(find_rate_column(ocr, lines)))
    desc_col = safe_bounds(expand_bounds(find_description_column(ocr, lines)))

    all_cols = [amount_col, discount_col, per_col, qty_col, rate_col]
    first_col_x = get_first_column_left(all_cols)

    header_keywords = {
        "quantity", "qty", "amount", "rate", "hsn", "per",
        "disc%", "disc", "description", "m.r.p", "of goods",
        "gst", "sr no", "no.", "ne", "per", "disc%"
    }

    # ---------------- TABLE END ----------------
    tax_ys = []

    total_ys = []

    for i in range(len(ocr["text"])):

        txt = str(ocr["text"][i]).strip().upper()

        # existing tax-based detection
        for t in TAX_KEYWORDS:
            if t in txt:
                tax_ys.append(ocr["top"][i])

        # NEW: detect TOTAL words for fallback
        if "TOTAL" in txt:
            total_ys.append(ocr["top"][i])

    # ---------------- BLUE LINE LOGIC ----------------

    h_end = None

    # 1. primary: tax keywords
    if tax_ys:
        h_end = min(tax_ys) - 2

    # 2. fallback: TOTAL keyword proximity
    if h_end is None and total_ys:

        # pick the lowest TOTAL near bottom of table
        h_end = max(total_ys) + 10   # small padding below TOTAL row

    # 3. final fallback: full page with safety constraint
    if h_end is None:
        h_end = h

    # ---------------- NEW SAFETY RULE ----------------
    # blue line cannot be in top 15% of page
    min_blue_y = int(h * 0.20)

    if h_end < min_blue_y:
        h_end = h  # push it down to bottom fallback
    
    # ---------------- FIND HEADER ROW ----------------
    row_hits = {}

    for i in range(len(ocr["text"])):

        txt = str(ocr["text"][i]).strip().lower()

        if any(k in txt for k in header_keywords) or "shipped" in txt:

            y = ocr["top"][i]
            row = get_row(y, tol=20)

            row_hits[row] = row_hits.get(row, 0) + 1

    header_y = 0

    if row_hits:

        best_row = max(row_hits, key=row_hits.get)

        ys = [
            ocr["top"][i]
            for i in range(len(ocr["text"]))
            if get_row(ocr["top"][i], 20) == best_row
            and any(k in str(ocr["text"][i]).lower() for k in header_keywords)
        ]

        header_y = min(ys) if ys else 0

    # ---------------- FALLBACK ONLY IF WRONG ----------------

    if header_y >= h_end:
        h_lines = detect_table_lines(img)

        best_line = None
        best_score = float("inf")

        min_gap_from_blue = 80   # 👈 IMPORTANT: prevents red line hugging blue line

        for x, y, w, h in h_lines:

            # must be ABOVE blue line
            if y >= h_end:
                continue

            # must be a THICK / strong line
            if w < img.shape[1] * 0.5:
                continue

            # ❗ reject lines too close to blue line
            if (h_end - y) < min_gap_from_blue:
                continue

            # score = how close it is to blue line BUT still valid
            score = abs(h_end - y)

            # optional extra: prefer lines that are also closer to header zone
            score += abs(header_y - y) * 0.3

            if score < best_score:
                best_score = score
                best_line = y

        if best_line is not None:
            header_y = best_line

    

    # ---------------- VALID TABLE WORDS ----------------
    valid_indices = []

    for i in range(len(ocr["text"])):

        y = ocr["top"][i]

        if header_y <= y <= h_end:
            valid_indices.append(i)

    # ---------------- COLUMN BOUNDS ----------------
    column_bounds = sorted([x for x, y, w, h in lines])

    def get_col(x):
        for i in range(len(column_bounds) - 1):
            if column_bounds[i] <= x <= column_bounds[i + 1]:
                return i
        return None

    def is_number(txt):
        txt = txt.strip()
        return bool(re.fullmatch(r"[0-9,.]+", txt))

    # ---------------- STORAGE ----------------
    text_boxes = []
    col_has_red = {}

    col_starts = []

    for c in [discount_col, per_col, rate_col, qty_col]:
        if c and c.get("left") is not None:
            col_starts.append(c["left"])

    if col_starts:
        drx = min(col_starts)
    else:
        drx = img.shape[1]

    # ---------------- PASS 1 (CLEAN PRIORITY FIX) ----------------
    for i in valid_indices:

        text = str(ocr["text"][i]).strip()
        if not text:
            continue

        x = ocr["left"][i]
        y = ocr["top"][i]
        bw = ocr["width"][i]
        bh = ocr["height"][i]

        col_id = get_col(x)
        if col_id is None:
            continue

        is_header = any(k in text.lower() for k in header_keywords)

        color = (0, 0, 0)

        # ---------------- HSN ----------------
        if is_hsn_code(text):
            color = (255, 255, 0)

        elif is_header:
            color = (0, 0, 255)
            col_has_red[col_id] = True

        else:
            
            # ---------------- DESCRIPTION COLUMN LOGIC ----------------
            if desc_col and desc_col.get("left") is not None:
                dx = desc_col["left"]
            else:
                dx = 0  # fallback = leftmost column

            if dx <= x <= drx:
                color = (0, 255, 255)

            # ---------------- DISCOUNT ----------------
            if discount_col and discount_col["right"] is not None:
                lx, rx = discount_col["left"], discount_col["right"]

                if lx <= x <= rx:
                    txt_clean = txt.strip()

                    # must contain digit + one %
                    if re.search(r"\d", txt_clean) and "%" in txt_clean:
                        # ensure % is not completely isolated noise
                        if re.search(r"\d+\s*%", txt_clean):
                            color = (0, 255, 0)

            # ---------------- PER ----------------
            if per_col and per_col["right"] is not None:
                px, prx = per_col["left"], per_col["right"]
                if px <= x <= prx:
                    if (text.upper().strip() in PER_ALLOWED and not re.search(r"\d", text)):
                        color = (255, 0, 255)

            # ---------------- AMOUNT ----------------
            if amount_col:
                ax = amount_col["left"]
                arx = amount_col["right"]

                if ax is not None and arx is not None:
                    if ax - 5 <= x <= arx + 5:
                        print("AMOUNT CANDIDATE:", repr(text))
                        if color == (0, 0, 0) and is_float_number(text):
                            print("PASSED")
                            color = (128, 0, 128)

            # ---------------- QUANTITY ----------------
            if qty_col:
                qx = qty_col["left"]
                qrx = qty_col["right"]

                if qx is not None and qrx is not None:

                    if qx - 10 <= x <= qrx + 10:

                        txt_clean = text.strip()

                        # ❌ skip bracketed OCR like (20.00 pcs)
                        if txt_clean.startswith("(") or txt_clean.endswith(")"):
                            continue

                        txt_clean = txt_clean.upper()
                        per_clean = re.sub(r"[^A-Z]", "", txt_clean)

                        # ---------------- 1. EXACT PER MATCH ----------------
                        if per_clean in PER_ALLOWED:
                            color = (42, 42, 165)

                        # ---------------- 2. FUZZY PER MATCH ----------------
                        elif any(per_clean.startswith(p) or p.startswith(per_clean) for p in PER_ALLOWED):
                            color = (42, 42, 165)

                        # ---------------- 3. QTY FALLBACK (BLACK ONLY) ----------------
                        elif color == (0, 0, 0):
                            # numeric-like check (qty should usually be number or number+text)
                            if re.search(r"\d", text):
                                color = (42, 42, 165)

            # ---------------- RATE COLUMN (FLOAT VALUES ONLY) ----------------
            if rate_col:
                rx = rate_col["left"]
                rrx = rate_col["right"]

                if rx is not None and rrx is not None:
                    if rx <= x <= rrx:
                        print("RATE CANDIDATE:", repr(text))

                        if is_float_number(text) and color == (0, 0, 0):
                            print("PASSED")
                            color = (0,165,255)


        # ALWAYS STORE
        text_boxes.append((x, y, bw, bh, text, color))

        amount_values = []

        if amount_col:
            for x, y, w, h, text, color in text_boxes:
                if amount_col["left"] <= x <= amount_col["right"]:

                    text_clean = text.replace(",", "").strip()

                    if re.fullmatch(r"\d+(\.\d{1,2})?", text_clean):
                        amount_values.append(text_clean)

    rows = defaultdict(list)

    # ---------------- STEP 1: BUILD ROWS ----------------
    for x, y, w, h, text, color in text_boxes:
        if amount_col and amount_col["left"] <= x <= amount_col["right"]:

            row_id = get_row(y)

            text_clean = text.replace(",", "").strip()

            try:
                num = float(text_clean)
                rows[row_id].append((y, num))   # store y also for better stop precision
            except:
                continue


    # ---------------- STEP 2: FLATTEN ORDERED VALUES ----------------
    values = []

    for row_id in sorted(rows.keys()):
        for y, num in rows[row_id]:
            values.append((row_id, y, num))


    # ---------------- STEP 3: RUNNING SUM + STOP ----------------
    running_sum = 0
    stop_y = None
    flag = 0

    for row_id, y, num in values:

        # STOP CONDITION (your rule)
        if abs(num - running_sum) < 0.01:
            stop_y = y   # exact row Y (IMPORTANT FIX)
            flag = 1
            break

        running_sum += num


    # ---------------- STEP 4: APPLY CUT ----------------
    if stop_y is not None:
        h_end = min(h_end, stop_y)


    # ---------------- STEP 5: FILTER DRAW BOXES ----------------
    filtered_boxes = [
        (x, y, w, h, text, color)
        for (x, y, w, h, text, color) in text_boxes
        if stop_y is None or y <= stop_y
    ]
    
    if flag ==1:
        filtered_boxes.pop()

    return (
        img,
        filtered_boxes,
        [],
        lines,
        header_y,
        h_end,
        per_col,
        discount_col,
        qty_col,
        desc_col,
        first_col_x,
        rate_col,
        amount_col
    )
   
# ---------------- DRAW ----------------
def draw(img, text_boxes, header_boxes, lines, h_start, h_end):

    debug = img.copy()

    # draw ALL text
    for x, y, w, h, text, color in text_boxes:

        cv2.putText(
            debug,
            text,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2
        )

    # vertical lines
    for x, y, w, h in lines:
        cv2.line(debug, (x, h_start), (x, h_end), (255, 255, 0), 2)

    # RED header line
    cv2.line(debug, (0, h_start), (debug.shape[1], h_start), (0, 0, 255), 2)

    # BLUE bottom line
    cv2.line(debug, (0, h_end), (debug.shape[1], h_end), (255, 0, 0), 2)

    h, w = debug.shape[:2]

    scale = 0.5  # change this to 0.3 if you want even smaller

    debug_small = cv2.resize(
        debug,
        (int(w * scale), int(h * scale))
    )

    cv2.imshow("TABLE", debug_small)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

# ---------------- PDF ----------------
def process_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    pdf_name = Path(pdf_path).name

    for page_no, page in enumerate(doc, start=1):

        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img = np.frombuffer(pix.tobytes(), dtype=np.uint8)
        img = cv2.imdecode(img, cv2.IMREAD_COLOR)

        h, w = img.shape[:2]
        img = cv2.resize(img, (1200, int(h * (1200 / w))))

        img, text_boxes, header_boxes, lines, h_start, h_end, per_col, discount_col, qty_col, desc_col, first_col_x, rate_col, amount_col = process_page(img)

        extract_rows(
            text_boxes,
            pdf_name,
            page_no,
            per_col=per_col,
            discount_col=discount_col,
            qty_col=qty_col,
            desc_col=desc_col,
            first_col_x=first_col_x,
            rate_col = rate_col,
            amount_col=amount_col
        )

        draw(img, text_boxes, header_boxes, lines, h_start, h_end)

def extract_rows(
    text_boxes,
    pdf_name,
    page_no,
    per_col=None,
    discount_col=None,
    qty_col=None,
    desc_col=None,
    first_col_x=None,
    rate_col=None,
    amount_col=None
):

    rows = defaultdict(dict)
    row_words = defaultdict(list)

    ROW_TOL = 18  # adjust if needed (VERY IMPORTANT)

    # -----------------------------
    # STEP 1: ASSIGN ROWS CLEANLY
    # -----------------------------
    for x, y, w, h, text, color in text_boxes:

        txt = text.replace(",", "").strip()
        if not txt:
            continue

        row_id = get_row(y, tol=ROW_TOL)
        row_words[row_id].append((x, txt, color))

        # ---------------- HSN ----------------
        if is_hsn_code(txt):
            rows[row_id]["hsn"] = txt

        # ---------------- PER ----------------
        if per_col and per_col["left"] <= x <= per_col["right"]:
            if txt.upper().strip() in PER_ALLOWED:
                rows[row_id]["per"] = txt

        # ---------------- DISCOUNT ----------------
        if discount_col and discount_col["left"] <= x <= discount_col["right"]:
            if re.fullmatch(r"\d+(?:\.\d+)?%", txt.strip()):
                if txt not in {"18%", "9%", "5%"}:
                    rows[row_id]["disc"] = txt

        # ---------------- QTY ----------------
        clean_txt = txt.strip()

        if qty_col and qty_col["left"] <= x <= qty_col["right"]:

            # ❌ skip anything inside parentheses
            if clean_txt.startswith("(") or clean_txt.endswith(")"):
                pass
            else:
                if color == (42, 42, 165) or re.search(r"\d", clean_txt):
                    rows[row_id]["qty"] = clean_txt

        # ---------------- DESCRIPTION ----------------
        if color == (0, 255, 255):
            rows[row_id]["desc"] = rows[row_id].get("desc", "") + " " + txt

        # ---------------- RATE ----------------
        rate_assigned = False

        # 1. PRIMARY: column-based
        if not rate_assigned and color == (0, 165, 255):
            if is_float_number(txt):
                rows[row_id]["rate"] = float(txt.replace(",", ""))
                rate_assigned = True
        # 2. FALLBACK: COLOR-based
        elif rate_col and rate_col["left"] <= x <= rate_col["right"]:
            if is_float_number(txt):
                rows[row_id]["rate"] = float(txt.replace(",", ""))
                rate_assigned = True


        # ---------------- AMOUNT ----------------
        amount_assigned = False

        # 1. PRIMARY: column-based
        if not amount_assigned and color == (128, 0, 128):
            if is_float_number(txt):
                rows[row_id]["amount"] = float(txt.replace(",", ""))
                amount_assigned = True

        # 2. FALLBACK: COLOR-based
        elif amount_col and amount_col["left"] <= x <= amount_col["right"]:
            if is_float_number(txt):
                rows[row_id]["amount"] = float(txt.replace(",", ""))
                amount_assigned = True

    # ---------------------------------------
    # STEP 2: FALLBACK PER / DISC (SAFE)
    # ---------------------------------------
    for row_id, words in row_words.items():

        # PER fallback
        if "per" not in rows[row_id]:
            for x, txt, _ in sorted(words, key=lambda v: v[0]):
                if txt.upper().strip() in PER_ALLOWED:
                    rows[row_id]["per"] = txt
                    break

        # DISC fallback
        if "disc" not in rows[row_id]:
            for x, txt, _ in sorted(words, key=lambda v: v[0]):
                if re.fullmatch(r"\d+(?:\.\d+)?%", txt):
                    if txt not in {"18%", "9%", "5%"}:
                        rows[row_id]["disc"] = txt
                        break

    # ---------------------------------------
    # STEP 3: DESCRIPTION FALLBACK
    # ---------------------------------------
    for row_id, words in row_words.items():

        if "desc" in rows[row_id]:
            continue

        desc_words = []

        for x, txt, _ in sorted(words, key=lambda v: v[0]):

            if is_hsn_code(txt):
                continue
            if is_float_number(txt):
                continue
            if txt.upper() in PER_ALLOWED:
                continue

            desc_words.append(txt)

        if desc_words:
            rows[row_id]["desc"] = " ".join(desc_words)

    # ---------------------------------------
    # STEP 3: RATE FALLBACK
    # ---------------------------------------
    for row_id, words in row_words.items():

        # skip if rate already detected
        if rows[row_id].get("rate") is not None:
            continue

        candidates = []

        for x, txt, _ in words:

            txt_clean = txt.replace(",", "").strip()

            # ignore percentages
            if "%" in txt_clean:
                continue

            # must be float
            if not is_float_number(txt_clean):
                continue

            try:
                val = float(txt_clean)
                candidates.append((x, val))
            except:
                continue

        if candidates:
            # rightmost float in the row
            candidates.sort(key=lambda v: v[0])

            rows[row_id]["rate"] = candidates[-1][1]

    # ---------------------------------------
    # STEP 4: FINAL VALIDATION + OUTPUT
    # ---------------------------------------
    sr_no = 1

    for row_id, data in rows.items():

        if not data:
            continue

        # HARD VALIDATION (kept but safer)
        if not data.get("hsn"):
            continue

        # optional: allow missing amount but prefer it
        non_empty = [v for v in [
            data.get("qty"),
            data.get("per"),
            data.get("disc"),
            data.get("rate"),
            data.get("amount"),
        ] if v not in [None, "", 0]]

        if len(non_empty) < 1:
            continue

        raw_desc = data.get("desc", "")

        match = re.match(r"^\s*(\d+)\s*\|?\s*(.+)", raw_desc)
        cleaned_desc = match.group(2) if match else raw_desc

        cleaned_desc = fix_ocr(clean_description(cleaned_desc))

        c_name, sku = find_from_sql(cleaned_desc)

        rows[row_id]["c_name"] = c_name
        rows[row_id]["sku"] = sku
        rows[row_id]["desc"] = cleaned_desc

        all_rows.append({
            "file": pdf_name,
            "page": page_no,
            "row_id": row_id,
            "hsn": data.get("hsn"),
            "qty": data.get("qty"),
            "per": data.get("per"),
            "disc": data.get("disc"),
            "Sr": sr_no,
            "c_name": c_name,
            "sku": sku,
            "rate": data.get("rate"),
            "amount": data.get("amount"),
            "desc": rows[row_id].get("desc")
        })

        sr_no += 1


def find_discount_column(ocr, lines, margin=80):
    targets = {"disc", "disc%", "disc.%", "discount"}

    disc_x = None

    for i in range(len(ocr["text"])):
        txt = str(ocr["text"][i]).strip().lower()

        if any(t in txt for t in targets):
            disc_x = ocr["left"][i]
            break

    if disc_x is None:
        return None

    left = None
    right = None

    for x, y, w, h in lines:
        if x < disc_x and (disc_x - x) < margin:
            left = x if left is None else max(left, x)

        if x > disc_x and (x - disc_x) < margin:
            right = x if right is None else min(right, x)

    return {
        "left": left if left is not None else 0,
        "right": right
    }

def find_per_column(ocr, lines, margin=80):
    targets = {"per", "per.", "per-"}

    per_x = None

    for i in range(len(ocr["text"])):
        txt = str(ocr["text"][i]).strip().lower()

        if txt in targets:
            per_x = ocr["left"][i]
            break

    if per_x is None:
        return None

    left = None
    right = None

    for x, y, w, h in lines:
        if x < per_x and (per_x - x) < margin:
            left = x if left is None else max(left, x)

        if x > per_x and (x - per_x) < margin:
            right = x if right is None else min(right, x)

    return {
        "left": left if left is not None else 0,
        "right": right if right is not None else 10**6
    }

def find_amount_column(ocr, lines, margin=120):
    texts = ocr["text"]
    lefts = ocr["left"]

    candidates = []

    # -----------------------------
    # STEP 1: collect float-like values from right side
    # -----------------------------
    for i in range(len(texts)):
        txt = str(texts[i]).strip()

        if not txt:
            continue

        # normalize
        txt_clean = txt.replace(",", "").strip()

        # must be float-like
        if not re.fullmatch(r"\d+\.\d{1,2}", txt_clean):
            continue

        x = lefts[i]

        # prefer right side of page (important change)
        candidates.append((x, float(txt_clean)))

    if not candidates:
        return None

    # -----------------------------
    # STEP 2: pick rightmost cluster
    # -----------------------------
    candidates.sort(key=lambda x: x[0], reverse=True)

    # take top-right region cluster
    rightmost_x = candidates[0][0]

    cluster = [c for c in candidates if abs(c[0] - rightmost_x) < margin]

    if not cluster:
        cluster = [candidates[0]]

    xs = [c[0] for c in cluster]

    # -----------------------------
    # STEP 3: derive column bounds
    # -----------------------------
    left = min(xs) 
    right = max(xs) + margin

    return {
        "left": max(0, left),
        "right": right
    }

def find_qty_column(ocr, lines, margin=80):
    targets = {"quantity", "qty", "qty.", "shipped"}

    qty_x = None

    # better anchor: match full word, not partial chaos
    for i in range(len(ocr["text"])):
        txt = str(ocr["text"][i]).strip().lower()

        if txt in targets or any(t == txt for t in targets):
            qty_x = ocr["left"][i]
            break

    if qty_x is None:
        return None

    left = None
    right = None

    # strict boundary detection
    for x, y, w, h in lines:

        if x < qty_x and (qty_x - x) < margin:
            left = x if left is None else max(left, x)

        if x > qty_x and (x - qty_x) < margin:
            right = x if right is None else min(right, x)

    # ❗ CRITICAL FIX: DO NOT use infinite fallback
    if left is None and right is None:
        return None

    # safe fallback: shrink instead of expanding
    if left is None:
        left = qty_x - margin
    if right is None:
        right = qty_x + margin

    return {
        "left": left if left is not None else 0,
        "right": right if right is not None else 10**6
    }

def find_rate_column(ocr, lines):

    headers = {
        "rate",
        "mrp",
        "price",
        "unit price"
    }

    for i, txt in enumerate(ocr["text"]):

        txt = str(txt).lower().strip()

        if txt in headers:

            x = ocr["left"][i]

            return {
                "left": x - 60,
                "right": x + 60
            }

    return None

def find_description_column(ocr, lines, margin=120):
    targets = {
        "description", "particulars", "item name", "product name",
        "details", "goods", "narration", "description of goods"
    }

    desc_x = None

    # Try to detect header normally
    for i in range(len(ocr["text"])):
        txt = str(ocr["text"][i]).strip().lower()
        if any(t == txt or t in txt for t in targets):
            desc_x = ocr["left"][i]
            break

    # ---------------- FALLBACK ----------------
    if desc_x is None:
        # If no description header found, take the leftmost column as description
        column_x_positions = sorted([x for x, y, w, h in lines])
        if column_x_positions:
            # Take first vertical line as end of description column
            left = 0
            right = column_x_positions[0]
        else:
            # If no lines detected, fallback full page width
            left, right = 0, 10**6

        return {"left": left, "right": right}

    # ---------------- NORMAL BOUNDS ----------------
    left = None
    right = None

    for x, y, w, h in lines:
        if x < desc_x and (desc_x - x) < margin:
            left = x if left is None else max(left, x)
        if x > desc_x and (x - desc_x) < margin:
            right = x if right is None else min(right, x)

    return {
        "left": left if left is not None else 0,
        "right": right if right is not None else 10**6
    }

def get_first_column_left(cols):
    cols = [c for c in cols if c and c.get("left") is not None]
    if not cols:
        return None
    return min(cols, key=lambda c: c["left"])["left"]

# ---------------- MAIN ----------------
if __name__ == "__main__":

    folder = r"C:\Users\intern.it\Desktop\RPA DATA\SS Dec Month-2025\Aaryan Enterprises"
    output_file = r"C:\Users\intern.it\Desktop\tables.xlsx"

    pdfs = list(Path(folder).rglob("*.pdf"))

    print("PDFs:", len(pdfs))

    final_rows = []

    for pdf in pdfs:

        name = pdf.parent.name

        try:
            doc = fitz.open(pdf)
            print("📄 PROCESSING:", pdf.name)

            gst_pos = []
            invoice_number = None
            invoice_date = "not found"
            bill_to = "not found"
            ship_to = "not found"

            # ---------------- INVOICE LEVEL EXTRACTION ----------------
            for page in doc:

                text = page.get_text("text")

                if bill_to == "not found":
                    bill_to = extract_name(text, bill_keywords, name)

                if ship_to == "not found":
                    ship_to = extract_name(text, ship_keywords, name)

                if invoice_date == "not found":
                    d = extract_date(text)
                    if d != "not found":
                        invoice_date = d

                blocks = page.get_text("blocks")
                texts = [b[4] for b in blocks]

                for b in blocks:

                    t = b[4]

                    for m in re.findall(gst_pat, t):
                        gst_pos.append((m, b[1]))

                    if invoice_number is None:
                        inv = re.search(invoice_pat, t, re.I)

                        if inv:
                            raw = inv.group(3).strip()

                            cleaned = [
                                w for w in raw.split()
                                if w.lower() not in [
                                    "e-way", "eway", "bill",
                                    "ref.", "no.", "invoice",
                                    "date", "&", "-"
                                ]
                            ]

                            if cleaned:
                                invoice_number = cleaned[0]

                if invoice_number is None:

                    for idx, text in enumerate(texts):

                        if re.search(
                            r'(Invoice|Invoice e-Way Bill|Bill|Invoice No & Date|Inv\.)\s*(No|Number|#|No.:)?',
                            text,
                            re.I
                        ):

                            for j in range(idx + 1, min(idx + 6, len(texts))):

                                candidate = texts[j]

                                match = re.search(r'\bPPL\/[^\s]+\b', candidate)

                                if match:
                                    invoice_number = match.group(0)
                                    break

                        if invoice_number:
                            break

            # ---------------- GST LOGIC ----------------
            if not gst_pos:
                ss_gst = "not found"
                dist_gst = "not found"
            else:
                gst_pos.sort(key=lambda x: x[1])

                unique = []
                for g, _ in gst_pos:
                    if g not in unique:
                        unique.append(g)

                ss_gst = unique[0]
                dist_gst = unique[1] if len(unique) > 1 else "not found"

            doc.close()

            # ---------------- TABLE DATA (FROM OCR PIPELINE) ----------------
            process_pdf(str(pdf))  # fills all_rows

            for r in all_rows:

                final_rows.append({
                    "file name": clean_text(pdf.name),
                    "customer name": clean_text(name),

                    "bill to": clean_text(bill_to),
                    "ship to": clean_text(ship_to),

                    "invoice no": clean_text(invoice_number) if invoice_number else "not found",
                    "ss gst": ss_gst,
                    "dist gst": dist_gst,
                    "date": invoice_date,

                    "Sr no.": r.get("Sr"),
                    "correct name": r.get("c_name"),
                    "desc": r.get("desc"),
                    "rlproductcode": r.get("sku"),
                    "qty": r.get("qty"),
                    "rate": r.get("rate"),
                    "amount": r.get("amount"),
                    "per": r.get("per"),
                    "disc": r.get("disc"),
                    "hsn": r.get("hsn")
                })

            # IMPORTANT: reset per file
            all_rows.clear()

        except Exception as e:
            print(f"Error reading {pdf}: {e}")
            traceback.print_exc()

    df = pd.DataFrame(final_rows)

    df = df.dropna(how="all")
    df.to_excel(output_file, index=False)
    print("✅ Excel saved:", output_file)
