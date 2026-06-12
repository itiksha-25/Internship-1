import pyodbc
import fitz
import cv2
import pytesseract
import numpy as np
import re
import pandas as pd
import os
from pathlib import Path

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Users\intern.it\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
)

FORBIDDEN_PATTERNS = re.compile(r"\b(cest|cgst|sgst|r\/?off|less)\b", re.IGNORECASE)

# ---------------- SQL CONNECTION ----------------
conn = pyodbc.connect(
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=192.168.2.41;"
    "DATABASE=test_sql;"
    "UID=intern.it;"
    "PWD=Int@321;"
)
cursor = conn.cursor()

# ---------------- HELPERS ----------------
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
    text = re.sub(r"\b\d{8}\b", " ", text)
    text = re.sub(r"\b\d+\.\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -")


# ---------------- SQL SEARCH ----------------
def find_from_sql(desc):
    desc = fix_ocr(desc)
    desc = re.sub(r"-", " ", desc)
    desc = re.sub(r",", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    if not has_min_alphabets(desc, 3):
        return "NOT FOUND", "NOT FOUND", "NOT FOUND"

    words = desc.split()

    for i in range(len(words), 0, -1):
        reduced = " ".join(words[:i])
        if len(reduced.replace(" ", "")) < 3:
            continue

        query = """
        SELECT TOP 1 [*Name], [*Short Name], [RlProductSKUId]
        FROM Vw_ProductMaster
        WHERE bot_product_name LIKE ?
        """
        cursor.execute(query, ('%' + reduced + '%',))
        row = cursor.fetchone()

        if row:
            return row[0], row[1], row[2]

    return "NOT FOUND", "NOT FOUND", "NOT FOUND"


# ---------------- OCR METHOD ----------------
def extract_column_method_1(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    ocr = pytesseract.image_to_data(
        gray,
        output_type=pytesseract.Output.DICT,
        config="--psm 6"
    )

    lines_dict = {}

    for i in range(len(ocr["text"])):
        word = str(ocr["text"][i]).strip()
        if not word:
            continue

        key = (
            ocr["block_num"][i],
            ocr["par_num"][i],
            ocr["line_num"][i]
        )

        lines_dict.setdefault(key, []).append(word)

    sr_items = []
    seen_sr_1 = False

    for key in lines_dict:
        words = lines_dict[key]

        line_text = " ".join(words)
        line_text = fix_ocr(line_text).strip()

        if not line_text:
            continue

        if FORBIDDEN_PATTERNS.search(line_text):
            continue

        match = re.match(r"^\s*(\d+)\s*\|?\s*(.+)", line_text)
        if not match:
            continue

        sr = int(match.group(1))
        desc = clean_description(match.group(2))

        # ✅ ADDED RULE
        desc = re.sub(r"^[^A-Za-z|]+", "", desc)

        if sr == 0:
            continue

        if sr == 1:
            if seen_sr_1:
                continue
            seen_sr_1 = True

        if sr > 50:
            continue

        sr_items.append({
            "sr": sr,
            "desc": desc
        })

    return sr_items


# ---------------- FALLBACK OCR ----------------
# def extract_column_method_2(img):
#     gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
#     text = pytesseract.image_to_string(gray)

#     items = []
#     expected_sr = 1

#     for line in text.split("\n"):
#         line = line.strip()
#         if not line:
#             continue

#         if FORBIDDEN_PATTERNS.search(line):
#             break

#         match = re.match(r"^(\d+)\s+(.*)", line)
#         if not match:
#             continue

#         sr = int(match.group(1))

#         if sr != expected_sr:
#             continue

#         desc = clean_description(match.group(2))

#         # ✅ ADDED RULE
#         desc = re.sub(r"^[^A-Za-z|]+", "", desc)

#         items.append({
#             "sr": sr,
#             "desc": desc
#         })

#         expected_sr += 1

#     return items


def extract_from_image(img):
    items = extract_column_method_1(img)
    # if not items:
    #     items = extract_column_method_2(img)
    return items


# ---------------- PDF PROCESS ----------------
def extract_from_pdf(pdf_path):
    print("📄 PROCESSING:", os.path.basename(pdf_path))
    output = []
    doc = fitz.open(pdf_path)

    for page_no in range(len(doc)):

        zoom = 2
        mat = fitz.Matrix(zoom, zoom)
        pix = doc[page_no].get_pixmap(matrix=mat)

        img = np.frombuffer(pix.tobytes(), dtype=np.uint8)
        img = cv2.imdecode(img, cv2.IMREAD_COLOR)

        h, w = img.shape[:2]

        base_items = extract_from_image(img)

        for item in base_items:
            name, short_name, sku = find_from_sql(item["desc"])

            output.append({
                "File Name": os.path.basename(pdf_path),
                "Page": page_no + 1,
                "Sr": item["sr"],
                "desc": item["desc"],
                "c_name": name,
                "Short Name": short_name,
                "sku": sku
            })

    return output


# ---------------- MAIN ----------------
if __name__ == "__main__":
    folder_path = r"C:\Users\intern.it\Desktop\RPA DATA\SS Dec Month-2025\Aaryan Enterprises"

    pdf_files = list(Path(folder_path).rglob("*.pdf"))
    print(f"📂 Total PDFs found: {len(pdf_files)}")

    all_results = []

    for pdf in pdf_files:
        try:
            all_results.extend(extract_from_pdf(str(pdf)))
        except Exception as e:
            print(f"❌ Error in {pdf.name}: {e}")

    df = pd.DataFrame(all_results)
    output_file = r"C:\Users\intern.it\Desktop\table.xlsx"

    df.to_excel(output_file, index=False)

    print("\n✅ Saved Excel:", output_file)

    conn.close()
