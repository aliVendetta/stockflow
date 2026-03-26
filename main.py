"""
Stockflow - Inventory Manager
FastAPI + SQLite (SQLAlchemy) backend
Invoice <-> Inventory 1-to-many relationship, fully persistent
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List, Generator
import uuid, json, base64, os, re, io
from datetime import datetime

# -- Load .env ----------------------------------------------------------------
from dotenv import load_dotenv
load_dotenv()

# -- OpenAI (httpx pin fix) ---------------------------------------------------
import httpx
import openai as _openai

def _get_openai() -> _openai.OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY is not set in .env")
    return _openai.OpenAI(api_key=api_key, http_client=httpx.Client())


# =============================================================================
# DATABASE SETUP  (SQLAlchemy + SQLite)
# =============================================================================
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    DateTime, ForeignKey, JSON, Text, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./stockflow.db")

# connect_args only needed for SQLite (allows multi-thread use in FastAPI)
connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# =============================================================================
# ORM MODELS
# =============================================================================

class InvoiceORM(Base):
    __tablename__ = "invoices"

    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, nullable=True)

    invoice_number  = Column(String, nullable=True, index=True)
    invoice_date    = Column(String, nullable=True)
    date_ordered    = Column(String, nullable=True)
    delivery_date   = Column(String, nullable=True)
    sale_type       = Column(String, nullable=True)
    payment_terms   = Column(String, nullable=True)
    clerk_name      = Column(String, nullable=True)
    terminal        = Column(String, nullable=True)

    # Contact blobs stored as JSON columns
    sold_to         = Column(JSON, nullable=True)   # customer
    ship_to         = Column(JSON, nullable=True)   # ship-to address
    shipper         = Column(JSON, nullable=True)   # vendor/supplier

    subtotal        = Column(Float,   nullable=True)
    total           = Column(Float,   nullable=True)
    total_units     = Column(Integer, nullable=True)
    notes           = Column(Text,    nullable=True)

    # 1-to-many: one invoice has many items
    items = relationship("InventoryItemORM", back_populates="invoice",
                         cascade="save-update, merge")


class InventoryItemORM(Base):
    __tablename__ = "inventory_items"

    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, nullable=True)

    invoice_id      = Column(String, ForeignKey("invoices.id", ondelete="SET NULL"),
                             nullable=True, index=True)

    name            = Column(String,  nullable=False)
    sku             = Column(String,  nullable=True, index=True)
    barcode         = Column(String,  nullable=True, index=True)
    category        = Column(String,  nullable=True, index=True)
    size            = Column(String,  nullable=True)
    unit            = Column(String,  nullable=True)
    units_per_case  = Column(Integer, nullable=True)
    quantity        = Column(Float,   nullable=True)
    price           = Column(Float,   nullable=True)
    total           = Column(Float,   nullable=True)
    description     = Column(Text,    nullable=True)

    invoice = relationship("InvoiceORM", back_populates="items")


# Create all tables on startup
Base.metadata.create_all(bind=engine)


# =============================================================================
# DB SESSION DEPENDENCY
# =============================================================================

def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =============================================================================
# SERIALISATION HELPERS
# =============================================================================

def _fmt_dt(dt) -> Optional[str]:
    return dt.isoformat() if dt else None

def _invoice_to_dict(inv: InvoiceORM, include_items: bool = False) -> dict:
    d = {
        "id":             inv.id,
        "created_at":     _fmt_dt(inv.created_at),
        "updated_at":     _fmt_dt(inv.updated_at),
        "invoice_number": inv.invoice_number,
        "invoice_date":   inv.invoice_date,
        "date_ordered":   inv.date_ordered,
        "delivery_date":  inv.delivery_date,
        "sale_type":      inv.sale_type,
        "payment_terms":  inv.payment_terms,
        "clerk_name":     inv.clerk_name,
        "terminal":       inv.terminal,
        "sold_to":        inv.sold_to,
        "ship_to":        inv.ship_to,
        "shipper":        inv.shipper,
        "subtotal":       inv.subtotal,
        "total":          inv.total,
        "total_units":    inv.total_units,
        "notes":          inv.notes,
        "item_count":     len(inv.items),
    }
    if include_items:
        d["items"] = [_item_to_dict(i) for i in inv.items]
    return d

def _invoice_ref(inv: Optional[InvoiceORM]) -> Optional[dict]:
    if not inv:
        return None
    return {
        "id":             inv.id,
        "invoice_number": inv.invoice_number,
        "invoice_date":   inv.invoice_date,
        "sold_to_name":   (inv.sold_to or {}).get("name"),
    }

def _item_to_dict(item: InventoryItemORM) -> dict:
    return {
        "id":             item.id,
        "created_at":     _fmt_dt(item.created_at),
        "updated_at":     _fmt_dt(item.updated_at),
        "invoice_id":     item.invoice_id,
        "invoice_ref":    _invoice_ref(item.invoice),
        "name":           item.name,
        "sku":            item.sku,
        "barcode":        item.barcode,
        "category":       item.category,
        "size":           item.size,
        "unit":           item.unit,
        "units_per_case": item.units_per_case,
        "quantity":       item.quantity,
        "price":          item.price,
        "total":          item.total,
        "description":    item.description,
    }


# =============================================================================
# PYDANTIC SCHEMAS
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
    invoice_number: Optional[str]         = None
    invoice_date:   Optional[str]         = None
    date_ordered:   Optional[str]         = None
    delivery_date:  Optional[str]         = None
    sale_type:      Optional[str]         = None
    payment_terms:  Optional[str]         = None
    clerk_name:     Optional[str]         = None
    terminal:       Optional[str]         = None
    sold_to:        Optional[ContactInfo] = None
    ship_to:        Optional[ContactInfo] = None
    shipper:        Optional[ContactInfo] = None
    subtotal:       Optional[float]       = None
    total:          Optional[float]       = None
    total_units:    Optional[int]         = None
    notes:          Optional[str]         = None

class InvoiceUpdate(InvoiceCreate):
    pass  # same fields, all optional

class InventoryItemCreate(BaseModel):
    invoice_id:     Optional[str]   = None
    name:           str
    sku:            Optional[str]   = None
    barcode:        Optional[str]   = None
    category:       Optional[str]   = None
    size:           Optional[str]   = None
    unit:           Optional[str]   = None
    units_per_case: Optional[int]   = None
    quantity:       Optional[float] = 0
    price:          Optional[float] = None
    total:          Optional[float] = None
    description:    Optional[str]   = None

class InventoryItemUpdate(BaseModel):
    invoice_id:     Optional[str]   = None
    name:           Optional[str]   = None
    sku:            Optional[str]   = None
    barcode:        Optional[str]   = None
    category:       Optional[str]   = None
    size:           Optional[str]   = None
    unit:           Optional[str]   = None
    units_per_case: Optional[int]   = None
    quantity:       Optional[float] = None
    price:          Optional[float] = None
    total:          Optional[float] = None
    description:    Optional[str]   = None


# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(title="Stockflow API", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# INVOICE CRUD
# =============================================================================

@app.get("/api/invoices", response_model=List[dict])
def list_invoices(db: Session = Depends(get_db)):
    invoices = db.query(InvoiceORM).order_by(InvoiceORM.created_at.desc()).all()
    return [_invoice_to_dict(inv) for inv in invoices]


@app.post("/api/invoices", response_model=dict, status_code=201)
def create_invoice(body: InvoiceCreate, db: Session = Depends(get_db)):
    inv = InvoiceORM(
        id             = str(uuid.uuid4()),
        invoice_number = body.invoice_number,
        invoice_date   = body.invoice_date,
        date_ordered   = body.date_ordered,
        delivery_date  = body.delivery_date,
        sale_type      = body.sale_type,
        payment_terms  = body.payment_terms,
        clerk_name     = body.clerk_name,
        terminal       = body.terminal,
        sold_to        = body.sold_to.model_dump() if body.sold_to else None,
        ship_to        = body.ship_to.model_dump() if body.ship_to else None,
        shipper        = body.shipper.model_dump() if body.shipper else None,
        subtotal       = body.subtotal,
        total          = body.total,
        total_units    = body.total_units,
        notes          = body.notes,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return _invoice_to_dict(inv)


@app.get("/api/invoices/{inv_id}", response_model=dict)
def get_invoice(inv_id: str, db: Session = Depends(get_db)):
    inv = db.query(InvoiceORM).filter(InvoiceORM.id == inv_id).first()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return _invoice_to_dict(inv, include_items=True)


@app.put("/api/invoices/{inv_id}", response_model=dict)
def update_invoice(inv_id: str, body: InvoiceUpdate, db: Session = Depends(get_db)):
    inv = db.query(InvoiceORM).filter(InvoiceORM.id == inv_id).first()
    if not inv:
        raise HTTPException(404, "Invoice not found")

    fields = body.model_dump(exclude_none=True)
    for key in ["sold_to", "ship_to", "shipper"]:
        if key in fields and isinstance(fields[key], dict):
            pass  # already a dict from pydantic
    for key, val in fields.items():
        if key in ("sold_to", "ship_to", "shipper") and hasattr(val, "model_dump"):
            val = val.model_dump()
        setattr(inv, key, val)
    inv.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(inv)
    return _invoice_to_dict(inv, include_items=True)


@app.delete("/api/invoices/{inv_id}")
def delete_invoice(inv_id: str, delete_items: bool = False, db: Session = Depends(get_db)):
    inv = db.query(InvoiceORM).filter(InvoiceORM.id == inv_id).first()
    if not inv:
        raise HTTPException(404, "Invoice not found")

    item_count = len(inv.items)
    if delete_items:
        for item in inv.items:
            db.delete(item)
        db.delete(inv)
        db.commit()
        return {"message": f"Invoice and {item_count} items deleted"}
    else:
        # Unlink items (FK set to NULL)
        for item in inv.items:
            item.invoice_id = None
        db.delete(inv)
        db.commit()
        return {"message": f"Invoice deleted, {item_count} items kept (unlinked)"}


# =============================================================================
# INVENTORY CRUD
# =============================================================================

@app.get("/api/inventory", response_model=List[dict])
def list_inventory(invoice_id: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(InventoryItemORM)
    if invoice_id:
        q = q.filter(InventoryItemORM.invoice_id == invoice_id)
    items = q.order_by(InventoryItemORM.created_at.desc()).all()
    return [_item_to_dict(i) for i in items]


@app.post("/api/inventory", response_model=dict, status_code=201)
def add_item(body: InventoryItemCreate, db: Session = Depends(get_db)):
    if body.invoice_id:
        inv = db.query(InvoiceORM).filter(InvoiceORM.id == body.invoice_id).first()
        if not inv:
            raise HTTPException(400, "Invoice not found")

    item = InventoryItemORM(
        id             = str(uuid.uuid4()),
        invoice_id     = body.invoice_id,
        name           = body.name,
        sku            = body.sku,
        barcode        = body.barcode,
        category       = body.category,
        size           = body.size,
        unit           = body.unit,
        units_per_case = body.units_per_case,
        quantity       = body.quantity,
        price          = body.price,
        total          = body.total,
        description    = body.description,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _item_to_dict(item)


@app.put("/api/inventory/{item_id}", response_model=dict)
def update_item(item_id: str, body: InventoryItemUpdate, db: Session = Depends(get_db)):
    item = db.query(InventoryItemORM).filter(InventoryItemORM.id == item_id).first()
    if not item:
        raise HTTPException(404, "Item not found")

    for key, val in body.model_dump(exclude_none=True).items():
        setattr(item, key, val)
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _item_to_dict(item)


@app.delete("/api/inventory/{item_id}")
def delete_item(item_id: str, db: Session = Depends(get_db)):
    item = db.query(InventoryItemORM).filter(InventoryItemORM.id == item_id).first()
    if not item:
        raise HTTPException(404, "Item not found")
    db.delete(item)
    db.commit()
    return {"message": "Deleted"}


# =============================================================================
# AI EXTRACTION PROMPT
# =============================================================================

EXTRACT_PROMPT = """
You are an expert invoice data extractor. Extract ALL data from this invoice/estimate document.

Return a single JSON object with exactly two keys: "invoice" and "items".

"invoice" fields (use null if not found — NEVER invent or guess):
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

"items" — one object per product line item (extract EVERY line, no skipping):
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
1. Extract EVERY single line item — no skipping
2. NEVER hallucinate or invent data not in the document
3. Return ONLY raw valid JSON — no markdown, no explanation, no fences
4. All numbers must be actual numbers, not strings
5. Use null for any missing field
6. "sold_to" is the customer; "shipper" is the vendor/supplier at the top
"""


# =============================================================================
# FILE -> TEXT HELPERS
# =============================================================================

def _pdf_to_text(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(500, "pypdf not installed. Run: pip install pypdf")
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = [f"--- Page {i+1} ---\n{(p.extract_text() or '')}"
             for i, p in enumerate(reader.pages)]
    return "\n\n".join(pages)


def _spreadsheet_to_text(file_bytes: bytes, filename: str) -> str:
    if filename.lower().endswith(".csv"):
        return file_bytes.decode("utf-8", errors="replace")
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(500, "openpyxl not installed. Run: pip install openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    rows = ["\t".join(str(c) if c is not None else "" for c in row)
            for row in wb.active.iter_rows(values_only=True)]
    return "\n".join(rows)


def _call_gpt(file_bytes: bytes, filename: str) -> str:
    client = _get_openai()
    fname  = filename.lower()

    if any(fname.endswith(e) for e in [".png", ".jpg", ".jpeg", ".webp", ".gif"]):
        ext_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".webp": "image/webp", ".gif": "image/gif"}
        media_type = next(v for k, v in ext_map.items() if fname.endswith(k))
        b64 = base64.standard_b64encode(file_bytes).decode()
        messages = [{"role": "user", "content": [
            {"type": "text", "text": EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}", "detail": "high"}},
        ]}]

    elif fname.endswith(".pdf"):
        text = _pdf_to_text(file_bytes)
        messages = [{"role": "user", "content": f"{EXTRACT_PROMPT}\n\nDocument text:\n{text}"}]

    elif any(fname.endswith(e) for e in [".xlsx", ".xls", ".csv"]):
        text = _spreadsheet_to_text(file_bytes, filename)
        messages = [{"role": "user", "content": f"{EXTRACT_PROMPT}\n\nDocument content:\n{text}"}]

    else:
        raise HTTPException(400, f"Unsupported file type: {filename}")

    resp = client.chat.completions.create(model="gpt-4o", messages=messages, max_tokens=4096)
    return resp.choices[0].message.content.strip()


# =============================================================================
# UPLOAD ENDPOINT
# =============================================================================

@app.post("/api/invoices/upload", response_model=dict)
async def upload_invoice(file: UploadFile = File(...), db: Session = Depends(get_db)):
    file_bytes = await file.read()
    raw = _call_gpt(file_bytes, file.filename)

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$",        "", raw, flags=re.MULTILINE).strip()

    try:
        data         = json.loads(raw)
        invoice_data = data.get("invoice") or {}
        items_data   = data.get("items")   or []
    except Exception as exc:
        raise HTTPException(500, f"AI parse error: {exc}. Raw: {raw[:500]}")

    def _contact(d) -> Optional[dict]:
        if not d:
            return None
        return {k: d.get(k) for k in
                ["name", "address", "city", "state", "zip_code",
                 "phone", "phone2", "email", "tax_number", "tob_number"]}

    # -- Persist invoice -------------------------------------------------------
    inv = InvoiceORM(
        id             = str(uuid.uuid4()),
        invoice_number = invoice_data.get("invoice_number"),
        invoice_date   = invoice_data.get("invoice_date"),
        date_ordered   = invoice_data.get("date_ordered"),
        delivery_date  = invoice_data.get("delivery_date"),
        sale_type      = invoice_data.get("sale_type"),
        payment_terms  = invoice_data.get("payment_terms"),
        clerk_name     = invoice_data.get("clerk_name"),
        terminal       = invoice_data.get("terminal"),
        sold_to        = _contact(invoice_data.get("sold_to")),
        ship_to        = _contact(invoice_data.get("ship_to")),
        shipper        = _contact(invoice_data.get("shipper")),
        subtotal       = invoice_data.get("subtotal"),
        total          = invoice_data.get("total"),
        total_units    = invoice_data.get("total_units"),
        notes          = invoice_data.get("notes"),
    )
    db.add(inv)
    db.flush()   # get inv.id before adding children

    # -- Persist line items ----------------------------------------------------
    added = []
    for d in items_data:
        if not d.get("name"):
            continue
        item = InventoryItemORM(
            id             = str(uuid.uuid4()),
            invoice_id     = inv.id,
            name           = d.get("name"),
            sku            = d.get("sku"),
            barcode        = d.get("barcode"),
            category       = d.get("category"),
            size           = d.get("size"),
            unit           = d.get("unit"),
            units_per_case = d.get("units_per_case"),
            quantity       = d.get("quantity"),
            price          = d.get("price"),
            total          = d.get("total"),
            description    = d.get("description"),
        )
        db.add(item)
        added.append(item)

    db.commit()
    db.refresh(inv)

    return {
        "message":     f"Invoice {inv.invoice_number or '?'} saved with {len(added)} items",
        "invoice":     _invoice_to_dict(inv),
        "items_added": len(added),
        "items":       [_item_to_dict(i) for i in added],
    }


# =============================================================================
# STATS
# =============================================================================

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
    total_val   = db.query(func.sum(InventoryItemORM.total)).scalar()    or 0
    total_units = db.query(func.sum(InventoryItemORM.quantity)).scalar() or 0
    total_skus  = db.query(func.count(InventoryItemORM.id)).scalar()     or 0
    total_invs  = db.query(func.count(InvoiceORM.id)).scalar()           or 0
    cats        = db.query(InventoryItemORM.category)\
                    .filter(InventoryItemORM.category.isnot(None))\
                    .distinct().count()
    return {
        "total_skus":     total_skus,
        "total_invoices": total_invs,
        "total_units":    total_units,
        "total_value":    round(float(total_val), 2),
        "categories":     cats,
    }


# =============================================================================
# CONFIG
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