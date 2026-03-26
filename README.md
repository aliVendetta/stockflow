# Stockflow — Inventory Manager v2

A FastAPI + HTML SPA with full **Invoice ↔ Inventory** 1-to-many relationships.
Supports AI-powered invoice extraction via GPT-4o (PDF, image, Excel, CSV).

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) Set OpenAI API key as env var
export OPENAI_API_KEY=sk-your-key-here

# 3. Run
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# 4. Open browser
http://localhost:8000
```

---

## Data Models

### Invoice
| Field | Type | Description |
|-------|------|-------------|
| invoice_number | string | e.g. "885229" |
| invoice_date | string | "2026-02-03" |
| date_ordered | string | |
| delivery_date | string | |
| sale_type | string | "Invoice" or "Estimate" |
| payment_terms | string | "15 days GM-TOB" |
| clerk_name | string | Sales rep name |
| terminal | string | Terminal # |
| **sold_to** | ContactInfo | Customer details |
| **ship_to** | ContactInfo | Ship-to address |
| **shipper** | ContactInfo | Supplier/vendor details |
| subtotal | float | |
| total | float | Invoice total |
| total_units | integer | |
| notes | string | Terms, disclaimers |

### ContactInfo (sold_to / ship_to / shipper)
| Field | Description |
|-------|-------------|
| name | Company/person name |
| address | Street address |
| city | City |
| state | State/Province |
| zip_code | ZIP/Postal code |
| phone | Primary phone |
| phone2 | Secondary phone |
| email | Email address |
| tax_number | Tax/EIN number |
| tob_number | Tobacco license number |

### Inventory Item
| Field | Description |
|-------|-------------|
| **invoice_id** | FK → Invoice (1 invoice : many items) |
| name | Product name |
| sku | Internal item # |
| barcode | UPC/barcode |
| category | Product category |
| size | e.g. "16 oz" |
| unit | Case / Box / Each / Display |
| units_per_case | Pack quantity |
| quantity | Units ordered |
| price | Price per unit |
| total | Line total |
| description | Notes |

---

## API Endpoints

### Invoices
| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/invoices` | List all invoices (with item_count) |
| POST | `/api/invoices` | Create invoice manually |
| GET | `/api/invoices/{id}` | Get invoice + all linked items |
| PUT | `/api/invoices/{id}` | Update invoice |
| DELETE | `/api/invoices/{id}?delete_items=true` | Delete invoice (optionally cascade) |
| POST | `/api/invoices/upload` | **AI extract**: upload PDF/image/Excel |

### Inventory
| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/inventory` | List all items (with invoice_ref) |
| GET | `/api/inventory?invoice_id=X` | Filter by invoice |
| POST | `/api/inventory` | Add item (with optional invoice_id) |
| PUT | `/api/inventory/{id}` | Update item |
| DELETE | `/api/inventory/{id}` | Delete item |

### Stats
| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/stats` | Dashboard stats summary |

---

## Upload Endpoint

**POST** `/api/invoices/upload`

Form data:
- `file` — PDF, PNG, JPG, WEBP, XLSX, XLS, or CSV
- `openai_api_key` — Your OpenAI API key (or set env var)

Response:
```json
{
  "message": "Invoice 885229 imported with 33 items",
  "invoice": { ...full invoice object... },
  "items_added": 33,
  "items": [ ...all extracted items... ]
}
```

---

## Notes

- **Data is in-memory** — restart clears it. For persistence add SQLite (SQLModel/SQLAlchemy) or PostgreSQL.
- AI uses **GPT-4o vision** to extract both invoice header info AND all line items in one pass.
- Delete invoice offers two options: cascade-delete items, or keep items (they become unlinked).
- The `invoice_ref` field on inventory items includes invoice number, date, and customer name for quick display.
# stockflow
