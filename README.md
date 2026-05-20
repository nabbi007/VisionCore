# scanBrain

AI-powered POS item recognition backend. Upload a product image and get back structured item details (name, brand, category, size, confidence) via Claude Vision. Built with FastAPI, Supabase, AWS S3, and a shared service key for service-to-service authentication.

---

## Features

- **AI Recognition** — sends product images to Claude Vision API and returns structured JSON
- **Service Auth** — shared secret key (`X-Service-Key` header) for service-to-service security
- **Rate Limiting** — 30 requests per minute per IP (configurable)
- **Scan Cache** — in-memory LRU cache avoids redundant Claude calls for identical images
- **S3 Storage** — uploads every scanned image to AWS S3 (or any S3-compatible service)
- **Audit Log** — saves scan metadata (store ID, LLM response, training flags) to Supabase

---

## Project Structure

```
VisionCore/
├── app/
│   ├── main.py                  # FastAPI app, middleware wiring, route registration
│   ├── routes/
│   │   └── scan.py              # /scan — image upload and recognition
│   ├── services/
│   │   ├── vision.py            # Claude Vision API integration
│   │   └── storage.py           # S3 upload + Supabase metadata persistence
│   ├── middleware/
│   │   └── security.py          # Service key verification, rate limiter, request logger
│   └── models/
│       └── schemas.py           # Shared Pydantic request/response models
├── storage/
│   └── images/                  # Local scratch space (not committed)
├── .env                         # Secrets (not committed)
├── .gitignore
├── requirements.txt
└── README.md
```

---

## API Endpoints

### Health
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | Liveness check |

### Scan — `/scan`
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/scan/` | X-Service-Key | Upload a product image and get item details |

Full interactive docs at **`/docs`** once the server is running.

---

## Setup

### 1. Clone and create a virtual environment

```powershell
git clone <your-repo-url>
cd VisionCore

python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure environment variables

Fill in the `.env` file with your real values (see the table below):

```powershell
# Generate a strong service secret key
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Paste the output into `SERVICE_SECRET_KEY` in `.env`. Set the same value in your base POS app — every request must include it as the `X-Service-Key` header.

### 4. Create the Supabase table

Run this SQL in your Supabase project → **SQL Editor**:

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE image_metadata (
  image_id            UUID PRIMARY KEY,
  timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  store_id            TEXT,
  llm_response        JSONB,
  confirmed_by_user   BOOLEAN NOT NULL DEFAULT FALSE,
  used_for_training   BOOLEAN NOT NULL DEFAULT FALSE,
  s3_url              TEXT NOT NULL,
  s3_key              TEXT NOT NULL
);

CREATE INDEX idx_image_metadata_store ON image_metadata(store_id);
CREATE INDEX idx_image_metadata_time  ON image_metadata(timestamp DESC);
```

Get your Supabase credentials:
- `SUPABASE_URL` — your project URL, e.g. `https://xxxx.supabase.co`
- `SUPABASE_SERVICE_KEY` — **Project Settings → API → service_role key** (not the anon key)

### 5. Create the S3 bucket

1. Go to the [AWS S3 Console](https://s3.console.aws.amazon.com)
2. Create a bucket named to match your `S3_BUCKET_NAME` env var
3. Make sure your IAM user has `s3:PutObject` on that bucket

### 6. Run the development server

```powershell
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/docs** for the interactive Swagger UI.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APP_ENV` | No | `development` | `development`, `staging`, or `production` |
| `LOG_LEVEL` | No | `INFO` | Python logging level |
| `ANTHROPIC_API_KEY` | Yes | — | Claude Vision API key |
| `VISION_MODEL` | No | `claude-sonnet-4-20250514` | Claude model ID |
| `VISION_MAX_TOKENS` | No | `512` | Max tokens for vision response |
| `SUPABASE_URL` | Yes | — | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Yes | — | Supabase **service_role** key |
| `SUPABASE_IMAGE_TABLE` | No | `image_metadata` | Table name for scan records |
| `AWS_ACCESS_KEY_ID` | Yes | — | AWS IAM access key |
| `AWS_SECRET_ACCESS_KEY` | Yes | — | AWS IAM secret key |
| `AWS_REGION` | No | `us-east-1` | S3 bucket region |
| `S3_BUCKET_NAME` | Yes | — | Target S3 bucket |
| `S3_PREFIX` | No | `scans` | Key prefix inside the bucket |
| `S3_ENDPOINT_URL` | No | — | Override for MinIO / R2 / LocalStack |
| `S3_PUBLIC_URL_BASE` | No | — | CDN / CloudFront base URL |
| `S3_ACL` | No | — | Set to `public-read` for legacy buckets only |
| `SERVICE_SECRET_KEY` | Yes | — | Shared secret with the base POS app |
| `RATE_LIMIT` | No | `30/minute` | Max requests per IP per window |
| `MAX_IMAGE_BYTES` | No | `10485760` | Upload size limit (10 MB) |
| `SCAN_CACHE_MAX_SIZE` | No | `1000` | Max entries in the recognition cache |

---

## Example Requests

**Health check**
```bash
curl http://localhost:8000/health
```

**Scan an item**
```bash
curl -X POST http://localhost:8000/scan/ \
  -H "X-Service-Key: <your-service-secret-key>" \
  -H "X-Store-Id: store-001" \
  -F "image=@/path/to/product.jpg"
```

**Example response**
```json
{
  "details": {
    "item_name": "Coca-Cola",
    "brand": "Coca-Cola",
    "category": "beverage",
    "size": "500ml",
    "confidence": 0.97
  },
  "image_id": "a1b2c3d4-...",
  "image_url": "https://your-bucket.s3.amazonaws.com/scans/a1b2c3d4-.jpeg",
  "cached": false
}
```

---

## How the base POS app calls this service

Every request from your base app must include the shared secret header:

```python
import httpx

response = httpx.post(
    "http://<visioncore-host>/scan/",
    headers={"X-Service-Key": SERVICE_SECRET_KEY, "X-Store-Id": store_id},
    files={"image": image_bytes},
)
```

The `X-Store-Id` header is optional — pass it to tag the scan record with which terminal or store triggered it.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API framework | FastAPI + Uvicorn |
| AI Vision | Anthropic Claude (`claude-sonnet-4-20250514`) |
| Database | Supabase (PostgreSQL) |
| Object storage | AWS S3 |
| Auth | Shared service key (`X-Service-Key`) |
| Rate limiting | slowapi |
| Validation | Pydantic v2 |
