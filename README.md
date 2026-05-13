# scanBrain

AI-powered POS item recognition backend. Upload a product image and get back structured item details (name, brand, category, size, confidence) via Claude Vision. Built with FastAPI, Supabase, AWS S3, and JWT authentication.

---

## Features

- **AI Recognition** — sends product images to Claude Vision API and returns structured JSON
- **Dual Auth** — supports both JWT Bearer tokens and API keys on protected endpoints
- **Rate Limiting** — 30 requests per minute per user (configurable)
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
│   │   ├── auth.py              # /auth — register, login, API key generation
│   │   └── scan.py              # /scan — image upload and recognition
│   ├── services/
│   │   ├── vision.py            # Claude Vision API integration
│   │   └── storage.py           # S3 upload + Supabase metadata persistence
│   ├── middleware/
│   │   └── security.py          # JWT + API key verification, rate limiter, request logger
│   └── models/
│       └── schemas.py           # Shared Pydantic request/response models
├── storage/
│   └── images/                  # Local scratch space (not committed)
├── .env                         # Secrets (not committed — see .env section below)
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

### Auth — `/auth`
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/auth/register` | None | Create a new user account |
| POST | `/auth/login` | None | Exchange email + password for a JWT |
| POST | `/auth/api-keys` | JWT | Generate a long-lived API key |

### Scan — `/scan`
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/scan/` | JWT or API Key | Upload a product image and get item details |

Full interactive docs at **`/docs`** once the server is running.

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd VisionCore
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy `.env` and fill in real values (see the table below):

```bash
cp .env .env.local   # optional: keep .env as a clean template
```

At minimum you need:
- `ANTHROPIC_API_KEY` — get from [console.anthropic.com](https://console.anthropic.com)
- `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` — from your Supabase project settings
- `AWS_*` + `S3_BUCKET_NAME` — from your AWS IAM credentials and S3 console
- `JWT_SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`

### 4. Create Supabase tables

Run this SQL in your Supabase SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE users (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email           TEXT UNIQUE NOT NULL,
  password_hash   TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE api_keys (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  key_hash        TEXT NOT NULL UNIQUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_used_at    TIMESTAMPTZ
);

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

CREATE INDEX idx_api_keys_user_id       ON api_keys(user_id);
CREATE INDEX idx_api_keys_key_hash      ON api_keys(key_hash);
CREATE INDEX idx_image_metadata_store   ON image_metadata(store_id);
CREATE INDEX idx_image_metadata_time    ON image_metadata(timestamp DESC);
```

### 5. Create the S3 bucket

1. Go to the [AWS S3 Console](https://s3.console.aws.amazon.com)
2. Create a bucket named to match your `S3_BUCKET_NAME` env var
3. Attach a bucket policy for public reads (or use presigned URLs for private buckets)
4. Make sure your IAM user has `s3:PutObject` on that bucket

### 6. Run the development server

```bash
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
| `SUPABASE_SERVICE_KEY` | Yes | — | Supabase service role key |
| `SUPABASE_IMAGE_TABLE` | No | `image_metadata` | Table name for scan records |
| `AWS_ACCESS_KEY_ID` | Yes | — | AWS IAM access key |
| `AWS_SECRET_ACCESS_KEY` | Yes | — | AWS IAM secret key |
| `AWS_REGION` | No | `us-east-1` | S3 bucket region |
| `S3_BUCKET_NAME` | Yes | — | Target S3 bucket |
| `S3_PREFIX` | No | `scans` | Key prefix inside the bucket |
| `S3_ENDPOINT_URL` | No | — | Override for MinIO / R2 / LocalStack |
| `S3_PUBLIC_URL_BASE` | No | — | CDN / CloudFront base URL |
| `S3_ACL` | No | — | Set to `public-read` for legacy buckets only |
| `JWT_SECRET_KEY` | Yes | — | Secret for signing JWTs |
| `JWT_ALGORITHM` | No | `HS256` | JWT signing algorithm |
| `JWT_EXPIRE_MINUTES` | No | `60` | JWT lifetime in minutes |
| `RATE_LIMIT` | No | `30/minute` | Max requests per user per window |
| `MAX_IMAGE_BYTES` | No | `10485760` | Upload size limit (10 MB) |
| `SCAN_CACHE_MAX_SIZE` | No | `1000` | Max entries in the recognition cache |

---

## Example Requests

**Register**
```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "cashier@store.com", "password": "securepass123"}'
```

**Login**
```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "cashier@store.com", "password": "securepass123"}'
```

**Scan an item**
```bash
curl -X POST http://localhost:8000/scan/ \
  -H "Authorization: Bearer <your-jwt>" \
  -H "X-Store-Id: store-001" \
  -F "image=@/path/to/product.jpg"
```

**Generate an API key**
```bash
curl -X POST http://localhost:8000/auth/api-keys \
  -H "Authorization: Bearer <your-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name": "POS Terminal 3"}'
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API framework | FastAPI + Uvicorn |
| AI Vision | Anthropic Claude (`claude-sonnet-4-20250514`) |
| Database | Supabase (PostgreSQL) |
| Object storage | AWS S3 |
| Auth | JWT (`python-jose`) + bcrypt (`passlib`) |
| Rate limiting | slowapi |
| Validation | Pydantic v2 |
