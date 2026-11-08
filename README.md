# Clinic Appointment Booking API & Web UI

Implementation of the 5-level Clinic Appointment Booking specification for Koya Academy Assessment (Task 1).

## Tech Stack
- **Framework:** FastAPI (Python 3.11+)
- **Database:** PostgreSQL (Neon / Supabase) with SQLAlchemy + `psycopg` (serverless compatible with `NullPool`), with local SQLite WAL fallback.
- **Frontend:** Single-page UI with Tailwind CSS + Vanilla JS served at `GET /`.
- **Deployment:** Vercel Serverless (`vercel.json`) or Render.

## Features by Level
- **Level 1 (Core CRUD):** `POST /reset`, `GET /slots`, `POST /bookings`, `GET /bookings/{id}`.
- **Level 2 (Auth & Validation):** `X-API-Key` authentication, strict input validation, formatted error responses (`{"error": "<message>"}`).
- **Level 3 (Concurrency & Cancellation):** Atomic conditional slot locking (`UPDATE ... WHERE is_booked = FALSE RETURNING id`), storage-engine partial unique index (`idx_unique_active_booking`), `DELETE /bookings/{id}` with slot freeing.
- **Level 4 (Idempotency & Pagination):** `Idempotency-Key` header support with request body hashing, cursor-based pagination for `GET /bookings`.
- **Level 5 (Rate Limiting):** Distributed sliding-window rate limiting (5 attempts/email/minute, failed attempts included) returning `429 Too Many Requests` and `Retry-After` header.

## Setup & Testing

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Environment Variables
Copy `.env.example` to `.env`:
```bash
API_KEY=koya-secret-key-2026
# Optional: PostgreSQL URL (defaults to local SQLite if omitted)
# DATABASE_URL=postgresql://user:password@host/neondb?sslmode=require
```

### 3. Run Automated Tests
```bash
pytest -q --tb=short
```

### 4. Run Development Server
```bash
uvicorn api.index:app --reload --port 8000
```
Open `http://localhost:8000` to view the Single-Page UI or `http://localhost:8000/docs` for Swagger UI.
