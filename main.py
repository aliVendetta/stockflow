from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import uuid, json, base64, os, re, io
from datetime import datetime

# -- Load .env ----------------------------------------------------------------
from dotenv import load_dotenv
load_dotenv()

# -- OpenAI with httpx fix ----------------------------------------------------
# Passing an explicit http_client avoids the 'proxies' TypeError that appears
# when httpx >= 0.28 is installed alongside some openai SDK builds.
import httpx
import openai as _openai

def _get_client() -> _openai.OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY is not set. Add it to your .env file."
        )
    return _openai.OpenAI(
        api_key=api_key,
        http_client=httpx.Client(),
    )


app = FastAPI(title="Stockflow API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# IN-MEMORY STORES
# =============================================================================
invoices_db: dict[str, dict] = {}
inventory_db: dict[str, dict] = {}


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class ContactInfo(BaseModel):
    name:       Optional[str] = None
    address:    Optional[str] = None
    city:       Optional[str] = None
    state:      Optional[str] = None
    zip_code:   Optional[str] = None
    phone:      Optional[str] = None
    phone2:     Optional[str] = None
    email:      Optional[str] = None
    tax_number: Optional[str] = None
    tob_number: Optional[str] = None

class InvoiceCreate(BaseModel):
    invoice_number:  Optional[str]   = None
    invoice_date:    Optional[str]   = None
    date_ordered:    Optional[str]   = None
    delivery_date:   Optional[str]   = None
    sale_type:       Optional[str]   = None
    payment_terms:   Optional[str]   = None
    clerk_name:      Optional[str]   = None
    terminal:        Optional[str]   = None
    sold_to:         Optional[ContactInfo] = None
    ship_to:         Optional[ContactInfo] = None
    shipper:         Optional[ContactInfo] = None
    subtotal:        Optional[float] = None
    total:           Optional[float] = None
    total_units:     Optional[int]   = None
    notes:           Optional[str]   = None

class InvoiceUpdate(BaseModel):
    invoice_number:  Optional[str]   = None
    invoice_date:    Optional[str]   = None
    date_ordered:    Optional[str]   = None
    delivery_date:   Optional[str]   = None
    sale_type:       Optional[str]   = None
    payment_terms:   Optional[str]   = None
    clerk_name:      Optional[str]   = None
    terminal:        Optional[str]   = None
    sold_to:         Optional[ContactInfo] = None
    ship_to:         Optional[ContactInfo] = None
    shipper:         Optional[ContactInfo] = None
    subtotal:        Optional[float] = None
    total:           Optional[float] = None
    total_units:     Optional[int]   = None
    notes:           Optional[str]   = None

class InventoryItemCreate(BaseModel):
    invoice_id:      Optional[str]   = None
    name:            str
    sku:             Optional[str]   = None
    barcode:         Optional[str]   = None
    category:        Optional[str]   = None
    size:            Optional[str]   = None
    unit:            Optional[str]   = None
    units_per_case:  Optional[int]   = None
    quantity:        Optional[float] = 0
    price:           Optional[float] = None
    total:           Optional[float] = None
    description:     Optional[str]   = None

class InventoryItemUpdate(BaseModel):
    invoice_id:      Optional[str]   = None
    name:            Optional[str]   = None
    sku:             Optional[str]   = None
    barcode:         Optional[str]   = None
    category:        Optional[str]   = None
    size:            Optional[str]   = None
    unit:            Optional[str]   = None
    units_per_case:  Optional[int]   = None
    quantity:        Optional[float] = None
    price:           Optional[float] = None
    total:           Optional[float] = None
    description:     Optional[str]   = None


# =============================================================================
# INVOICE CRUD
# =============================================================================

@app.get("/api/invoices", response_model=List[dict])
def list_invoices():
    result = []
    for inv in invoices_db.values():
        items = [i for i in inventory_db.values() if i.get("invoice_id") == inv["id"]]
        result.append({**inv, "item_count": len(items)})
    return sorted(result, key=lambda x: x.get("created_at", ""), reverse=True)

@app.post("/api/invoices", response_model=dict, status_code=201)
def create_invoice(invoice: InvoiceCreate):
    inv_id = str(uuid.uuid4())
    record = {
        "id": inv_id,
        "created_at": datetime.utcnow().isoformat(),
        **invoice.model_dump(),
    }
    invoices_db[inv_id] = record
    return {**record, "item_count": 0}

@app.get("/api/invoices/{inv_id}", response_model=dict)
def get_invoice(inv_id: str):
    if inv_id not in invoices_db:
        raise HTTPException(404, "Invoice not found")
    inv = invoices_db[inv_id]
    items = [i for i in inventory_db.values() if i.get("invoice_id") == inv_id]
    return {**inv, "items": items, "item_count": len(items)}

@app.put("/api/invoices/{inv_id}", response_model=dict)
def update_invoice(inv_id: str, updates: InvoiceUpdate):
    if inv_id not in invoices_db:
        raise HTTPException(404, "Invoice not found")
    rec = invoices_db[inv_id]
    for field, val in updates.model_dump(exclude_none=True).items():
        rec[field] = val
    rec["updated_at"] = datetime.utcnow().isoformat()
    items = [i for i in inventory_db.values() if i.get("invoice_id") == inv_id]
    return {**rec, "item_count": len(items)}

@app.delete("/api/invoices/{inv_id}")
def delete_invoice(inv_id: str, delete_items: bool = False):
    if inv_id not in invoices_db:
        raise HTTPException(404, "Invoice not found")
    del invoices_db[inv_id]
    if delete_items:
        to_remove = [k for k, v in inventory_db.items() if v.get("invoice_id") == inv_id]
        for k in to_remove:
            del inventory_db[k]
        return {"message": f"Invoice and {len(to_remove)} items deleted"}
    for item in inventory_db.values():
        if item.get("invoice_id") == inv_id:
            item["invoice_id"] = None
    return {"message": "Invoice deleted, items kept (unlinked)"}


# =============================================================================
# INVENTORY CRUD
# =============================================================================

def _enrich(item: dict) -> dict:
    inv_id = item.get("invoice_id")
    if inv_id and inv_id in invoices_db:
        inv = invoices_db[inv_id]
        item["invoice_ref"] = {
            "id":             inv["id"],
            "invoice_number": inv.get("invoice_number"),
            "invoice_date":   inv.get("invoice_date"),
            "sold_to_name":   (inv.get("sold_to") or {}).get("name"),
        }
    else:
        item["invoice_ref"] = None
    return item

@app.get("/api/inventory", response_model=List[dict])
def list_inventory(invoice_id: Optional[str] = None):
    items = list(inventory_db.values())
    if invoice_id:
        items = [i for i in items if i.get("invoice_id") == invoice_id]
    return [_enrich(dict(i)) for i in items]

@app.post("/api/inventory", response_model=dict, status_code=201)
def add_inventory_item(item: InventoryItemCreate):
    if item.invoice_id and item.invoice_id not in invoices_db:
        raise HTTPException(400, "Invoice not found")
    item_id = str(uuid.uuid4())
    record = {"id": item_id, "created_at": datetime.utcnow().isoformat(), **item.model_dump()}
    inventory_db[item_id] = record
    return _enrich(dict(record))

@app.put("/api/inventory/{item_id}", response_model=dict)
def update_inventory_item(item_id: str, updates: InventoryItemUpdate):
    if item_id not in inventory_db:
        raise HTTPException(404, "Item not found")
    rec = inventory_db[item_id]
    for field, val in updates.model_dump(exclude_none=True).items():
        rec[field] = val
    rec["updated_at"] = datetime.utcnow().isoformat()
    return _enrich(dict(rec))

@app.delete("/api/inventory/{item_id}")
def delete_inventory_item(item_id: str):
    if item_id not in inventory_db:
        raise HTTPException(404, "Item not found")
    del inventory_db[item_id]
    return {"message": "Deleted"}


# =============================================================================
# AI EXTRACTION PROMPT
# =============================================================================

EXTRACT_PROMPT = """
You are an expert invoice data extractor. Extract ALL data from this invoice/estimate document.

Return a single JSON object with exactly two keys: "invoice" and "items".

"invoice" fields (use null if not present — NEVER invent or guess):
{
  "invoice_number": string,
  "invoice_date": string,
  "date_ordered": string,
  "delivery_date": string,
  "sale_type": string,
  "payment_terms": string,
  "clerk_name": string,
  "terminal": string,
  "sold_to": {
    "name": string, "address": string, "city": string, "state": string,
    "zip_code": string, "phone": string, "phone2": string,
    "email": string, "tax_number": string, "tob_number": string
  },
  "ship_to": {
    "name": string, "address": string, "city": string, "state": string,
    "zip_code": string, "phone": string, "phone2": string,
    "email": string, "tax_number": string, "tob_number": string
  },
  "shipper": {
    "name": string, "address": string, "city": string, "state": string,
    "zip_code": string, "phone": string, "phone2": string,
    "email": string, "tax_number": string, "tob_number": string
  },
  "subtotal": number,
  "total": number,
  "total_units": integer,
  "notes": string
}

"items" — one object per product line item (extract EVERY line):
{
  "name": string (required),
  "sku": string,
  "barcode": string,
  "category": string,
  "size": string,
  "unit": string (Case/Box/Each/Display),
  "units_per_case": integer,
  "quantity": number,
  "price": number,
  "total": number,
  "description": string
}

ABSOLUTE RULES:
1. Extract EVERY single line item - no skipping
2. NEVER hallucinate or invent data not in the document
3. Return ONLY raw valid JSON - no markdown, no explanation, no fences
4. All numbers must be actual numbers, not strings
5. Use null for any missing field
6. "sold_to" is the customer; "shipper" is the vendor/supplier at the top of the doc
"""


# =============================================================================
# FILE -> TEXT HELPERS
# =============================================================================

def _pdf_to_text(file_bytes: bytes) -> str:
    """Extract all text from a PDF using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(500, "pypdf is not installed. Run: pip install pypdf")
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            pages.append(f"--- Page {i + 1} ---\n{text}")
        return "\n\n".join(pages)
    except Exception as exc:
        raise HTTPException(500, f"PDF read error: {exc}")


def _spreadsheet_to_text(file_bytes: bytes, filename: str) -> str:
    if filename.lower().endswith(".csv"):
        return file_bytes.decode("utf-8", errors="replace")
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(500, "openpyxl is not installed. Run: pip install openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = ["\t".join(str(c) if c is not None else "" for c in row)
            for row in ws.iter_rows(values_only=True)]
    return "\n".join(rows)


def _call_gpt(file_bytes: bytes, filename: str) -> str:
    client = _get_client()
    fname = filename.lower()

    # Images: send as base64 vision input
    if any(fname.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]):
        ext_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".webp": "image/webp", ".gif": "image/gif"}
        media_type = next(v for k, v in ext_map.items() if fname.endswith(k))
        b64 = base64.standard_b64encode(file_bytes).decode()
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": EXTRACT_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:{media_type};base64,{b64}",
                    "detail": "high",
                }},
            ],
        }]

    # PDFs: extract text, send as text prompt
    elif fname.endswith(".pdf"):
        text = _pdf_to_text(file_bytes)
        messages = [{
            "role": "user",
            "content": f"{EXTRACT_PROMPT}\n\nDocument text:\n{text}",
        }]

    # Spreadsheets
    elif any(fname.endswith(ext) for ext in [".xlsx", ".xls", ".csv"]):
        text = _spreadsheet_to_text(file_bytes, filename)
        messages = [{
            "role": "user",
            "content": f"{EXTRACT_PROMPT}\n\nDocument content:\n{text}",
        }]

    else:
        raise HTTPException(400, f"Unsupported file type: {filename}")

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=4096,
    )
    return resp.choices[0].message.content.strip()


# =============================================================================
# UPLOAD ENDPOINT — key comes from .env only
# =============================================================================

@app.post("/api/invoices/upload", response_model=dict)
async def upload_invoice(file: UploadFile = File(...)):
    """
    Upload an invoice PDF, image, or spreadsheet.
    OpenAI API key is read from OPENAI_API_KEY in .env — not from the client.
    """
    file_bytes = await file.read()
    raw = _call_gpt(file_bytes, file.filename)

    # Strip accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE).strip()

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object at the top level")
        invoice_data = data.get("invoice") or {}
        items_data   = data.get("items")   or []
    except Exception as exc:
        raise HTTPException(500, f"AI parse error: {exc}. Raw snippet: {raw[:500]}")

    def _contact(d):
        if not d:
            return None
        return {k: d.get(k) for k in
                ["name", "address", "city", "state", "zip_code",
                 "phone", "phone2", "email", "tax_number", "tob_number"]}

    inv_id = str(uuid.uuid4())
    invoice_record = {
        "id":             inv_id,
        "created_at":     datetime.utcnow().isoformat(),
        "invoice_number": invoice_data.get("invoice_number"),
        "invoice_date":   invoice_data.get("invoice_date"),
        "date_ordered":   invoice_data.get("date_ordered"),
        "delivery_date":  invoice_data.get("delivery_date"),
        "sale_type":      invoice_data.get("sale_type"),
        "payment_terms":  invoice_data.get("payment_terms"),
        "clerk_name":     invoice_data.get("clerk_name"),
        "terminal":       invoice_data.get("terminal"),
        "sold_to":        _contact(invoice_data.get("sold_to")),
        "ship_to":        _contact(invoice_data.get("ship_to")),
        "shipper":        _contact(invoice_data.get("shipper")),
        "subtotal":       invoice_data.get("subtotal"),
        "total":          invoice_data.get("total"),
        "total_units":    invoice_data.get("total_units"),
        "notes":          invoice_data.get("notes"),
    }
    invoices_db[inv_id] = invoice_record

    added_items = []
    for item_data in items_data:
        if not item_data.get("name"):
            continue
        item_id = str(uuid.uuid4())
        record = {
            "id":             item_id,
            "created_at":     datetime.utcnow().isoformat(),
            "invoice_id":     inv_id,
            "name":           item_data.get("name"),
            "sku":            item_data.get("sku"),
            "barcode":        item_data.get("barcode"),
            "category":       item_data.get("category"),
            "size":           item_data.get("size"),
            "unit":           item_data.get("unit"),
            "units_per_case": item_data.get("units_per_case"),
            "quantity":       item_data.get("quantity"),
            "price":          item_data.get("price"),
            "total":          item_data.get("total"),
            "description":    item_data.get("description"),
        }
        inventory_db[item_id] = record
        added_items.append(_enrich(dict(record)))

    return {
        "message":     f"Invoice {invoice_record.get('invoice_number', '?')} imported with {len(added_items)} items",
        "invoice":     {**invoice_record, "item_count": len(added_items)},
        "items_added": len(added_items),
        "items":       added_items,
    }


# =============================================================================
# STATS
# =============================================================================

@app.get("/api/stats")
def get_stats():
    items = list(inventory_db.values())
    total_val   = sum(i.get("total")    or 0 for i in items)
    total_units = sum(i.get("quantity") or 0 for i in items)
    cats = len(set(i.get("category") for i in items if i.get("category")))
    return {
        "total_skus":     len(items),
        "total_invoices": len(invoices_db),
        "total_units":    total_units,
        "total_value":    round(total_val, 2),
        "categories":     cats,
    }


# =============================================================================
# CONFIG ENDPOINT — lets frontend show key status without exposing the key
# =============================================================================

@app.get("/api/config")
def get_config():
    return {"openai_key_configured": bool(os.getenv("OPENAI_API_KEY"))}


# =============================================================================
# STATIC / SPA
# =============================================================================
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
@app.get("/inventory")
@app.get("/invoices")
@app.get("/invoices/{path:path}")
def spa(_path: str = ""):
    return FileResponse("static/index.html")
