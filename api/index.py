import base64
import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import get_db, init_db, is_sqlite
from models import (
    Booking,
    BookingsListResponse,
    ErrorResponse,
    NewBooking,
    ResetRequest,
    SlotWithAvailability,
    SlotsResponse,
)

load_dotenv()

API_KEY = os.getenv("API_KEY", "koya-secret-key-2026").strip()
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def parse_dt(val) -> datetime:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    if isinstance(val, str):
        cleaned = val.strip().replace(" ", "T")
        if not cleaned.endswith("Z") and "+" not in cleaned and "-" not in cleaned[10:]:
            cleaned += "Z"
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    raise ValueError(f"Cannot parse datetime from {val}")


def format_iso(val) -> str:
    dt = parse_dt(val)
    return dt.isoformat().replace("+00:00", "Z")


def encode_cursor(created_at: datetime, booking_id: str) -> str:
    raw = f"{created_at.isoformat()}|{booking_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor_str: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode()).decode()
        dt_str, booking_id = raw.split("|", 1)
        return parse_dt(dt_str), booking_id
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Clinic booking API",
    version="1.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)


# Custom exception handlers matching {"error": "<message>"} spec
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    msg = "Invalid request payload"
    if errors:
        first = errors[0]
        field = " -> ".join(str(loc) for loc in first.get("loc", []))
        msg = f"Validation error at {field}: {first.get('msg', 'invalid')}"
    return JSONResponse(status_code=400, content={"error": msg})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": msg},
        headers=exc.headers,
    )


# Middleware: Authentication & Public Path Exemption
@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    path = request.url.path
    # Public routes
    if (
        path == "/"
        or path == "/health"
        or path == "/docs"
        or path == "/openapi.json"
        or path == "/redoc"
        or path.startswith("/static")
        or path.startswith("/favicon")
    ):
        return await call_next(request)

    key = request.headers.get("X-API-Key")
    if not key or key.strip() != API_KEY:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "Invalid or missing API key"},
        )

    return await call_next(request)


# Mount static assets if folder exists
static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", include_in_schema=False)
async def serve_index():
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse(
        {"status": "ok", "message": "Clinic Booking API is running"}
    )


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok"}


# Level 1: POST /reset
@app.post(
    "/reset",
    summary="Level 1 · Replace all slots",
    responses={200: {"description": "Done"}},
)
async def reset_slots(payload: ResetRequest):
    with get_db() as conn:
        conn.execute(text("DELETE FROM bookings;"))
        conn.execute(text("DELETE FROM slots;"))
        conn.execute(text("DELETE FROM idempotency_keys;"))
        conn.execute(text("DELETE FROM booking_attempts;"))

        for s in payload.slots:
            conn.execute(
                text(
                    """
                    INSERT INTO slots (id, provider, start_time, end_time, is_booked)
                    VALUES (:id, :provider, :start_time, :end_time, FALSE);
                    """
                ),
                {
                    "id": s.id,
                    "provider": s.provider,
                    "start_time": s.start,
                    "end_time": s.end,
                },
            )
    return {"status": "ok"}


# Level 1: GET /slots
@app.get(
    "/slots",
    summary="Level 1 · List slots",
    response_model=SlotsResponse,
)
async def list_slots(provider: Optional[str] = Query(None)):
    query = """
        SELECT id, provider, start_time, end_time, is_booked
        FROM slots
    """
    params = {}
    if provider is not None:
        query += " WHERE provider = :provider"
        params["provider"] = provider

    query += " ORDER BY start_time ASC, id ASC;"

    with get_db() as conn:
        rows = conn.execute(text(query), params).mappings().fetchall()

    slots_out = [
        SlotWithAvailability(
            id=r["id"],
            provider=r["provider"],
            start=parse_dt(r["start_time"]),
            end=parse_dt(r["end_time"]),
            available=not bool(r["is_booked"]),
        )
        for r in rows
    ]
    return SlotsResponse(slots=slots_out)


# Level 1-5: POST /bookings
@app.post(
    "/bookings",
    status_code=status.HTTP_201_CREATED,
    summary="Book a slot (levels 1–5)",
    response_model=Booking,
    responses={
        201: {"model": Booking},
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def create_booking(
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # Read raw body bytes for idempotency hashing
    raw_body = await request.body()
    try:
        body_json = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    # Validate required fields
    slot_id = str(body_json.get("slot_id", "")).strip()
    name = str(body_json.get("name", "")).strip()
    email_raw = str(body_json.get("email", "")).strip()

    if not slot_id or not name or not email_raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Fields slot_id, name, and email are required and cannot be empty",
        )

    if not EMAIL_REGEX.match(email_raw):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email address format",
        )

    email = email_raw.lower()

    # Level 5: Distributed Rate Limiting (Serverless Safe)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=60)

    with get_db() as conn:
        attempts = conn.execute(
            text(
                """
                SELECT attempted_at 
                FROM booking_attempts 
                WHERE email = :email AND attempted_at >= :window_start 
                ORDER BY attempted_at ASC
                """
            ),
            {"email": email, "window_start": window_start},
        ).fetchall()

        if len(attempts) >= 5:
            # 6th or higher attempt within the 1-minute window
            conn.execute(
                text(
                    """
                    INSERT INTO booking_attempts (email, attempted_at)
                    VALUES (:email, :attempted_at)
                    """
                ),
                {"email": email, "attempted_at": now},
            )
            oldest_ts = parse_dt(attempts[0][0])
            retry_after = max(1, int(60 - (now - oldest_ts).total_seconds()))
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
                content={"error": "Too many booking attempts for this email"},
            )

        # Record this attempt (failed attempts included)
        conn.execute(
            text(
                """
                INSERT INTO booking_attempts (email, attempted_at)
                VALUES (:email, :attempted_at)
                """
            ),
            {"email": email, "attempted_at": now},
        )

    # Level 4: Idempotency Key check
    request_hash = hashlib.sha256(raw_body).hexdigest()
    if idempotency_key:
        idempotency_key = idempotency_key.strip()
        with get_db() as conn:
            existing = conn.execute(
                text(
                    """
                    SELECT request_hash, status_code, response_body
                    FROM idempotency_keys
                    WHERE key = :key
                    """
                ),
                {"key": idempotency_key},
            ).mappings().first()

            if existing:
                if existing["request_hash"] == request_hash:
                    # Return exact cached response
                    return Response(
                        content=existing["response_body"],
                        status_code=existing["status_code"],
                        media_type="application/json",
                    )
                else:
                    return JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={"error": "Idempotency-Key reused with different body"},
                    )

    # Level 2 & 3: Atomic Slot Locking and Booking Creation
    booking_id = f"b_{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(timezone.utc)

    with get_db() as conn:
        # Check slot existence and start time
        slot_row = conn.execute(
            text("SELECT id, start_time, is_booked FROM slots WHERE id = :id"),
            {"id": slot_id},
        ).mappings().first()

        if not slot_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Slot not found",
            )

        slot_start = parse_dt(slot_row["start_time"])
        if slot_start <= now:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Slot has already started",
            )

        # Atomic conditional update
        updated_slot = conn.execute(
            text(
                """
                UPDATE slots
                SET is_booked = TRUE
                WHERE id = :id AND is_booked = FALSE
                RETURNING id;
                """
            ),
            {"id": slot_id},
        ).scalar()

        if not updated_slot:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Slot already booked",
            )

        try:
            conn.execute(
                text(
                    """
                    INSERT INTO bookings (id, slot_id, patient_name, patient_email, status, created_at)
                    VALUES (:id, :slot_id, :name, :email, 'confirmed', :created_at);
                    """
                ),
                {
                    "id": booking_id,
                    "slot_id": slot_id,
                    "name": name,
                    "email": email,
                    "created_at": created_at,
                },
            )
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Slot already booked",
            )

        booking_obj = Booking(
            id=booking_id,
            slot_id=slot_id,
            name=name,
            email=email,
            status="confirmed",
            created_at=created_at,
        )
        response_json = booking_obj.model_dump_json()

        # Save idempotency record inside the same transaction
        if idempotency_key:
            conn.execute(
                text(
                    """
                    INSERT INTO idempotency_keys (key, request_hash, status_code, response_body, created_at)
                    VALUES (:key, :request_hash, :status_code, :response_body, :created_at)
                    ON CONFLICT (key) DO NOTHING;
                    """
                ),
                {
                    "key": idempotency_key,
                    "request_hash": request_hash,
                    "status_code": 201,
                    "response_body": response_json,
                    "created_at": created_at,
                },
            )

    return Response(
        content=response_json,
        status_code=status.HTTP_201_CREATED,
        media_type="application/json",
    )


# Level 4: GET /bookings
@app.get(
    "/bookings",
    summary="Level 4 · List a person's bookings",
    response_model=BookingsListResponse,
    responses={
        200: {"model": BookingsListResponse},
        400: {"model": ErrorResponse},
    },
)
async def list_bookings(
    email: Optional[str] = Query(None),
    limit: Optional[int] = Query(10),
    cursor: Optional[str] = Query(None),
):
    if email is None or not email.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email parameter is required",
        )

    if limit is None or limit < 1 or limit > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid limit. Must be between 1 and 50",
        )

    norm_email = email.strip().lower()

    base_query = """
        SELECT id, slot_id, patient_name, patient_email, status, created_at
        FROM bookings
        WHERE LOWER(patient_email) = :email
    """
    params = {"email": norm_email, "fetch_limit": limit + 1}

    if cursor:
        c_time, c_id = decode_cursor(cursor)
        base_query += " AND (created_at > :c_time OR (created_at = :c_time AND id > :c_id))"
        params["c_time"] = c_time
        params["c_id"] = c_id

    base_query += " ORDER BY created_at ASC, id ASC LIMIT :fetch_limit;"

    with get_db() as conn:
        rows = conn.execute(text(base_query), params).mappings().fetchall()

    has_more = len(rows) > limit
    returned_rows = rows[:limit] if has_more else rows

    bookings_out = [
        Booking(
            id=r["id"],
            slot_id=r["slot_id"],
            name=r["patient_name"],
            email=r["patient_email"],
            status=r["status"],
            created_at=parse_dt(r["created_at"]),
        )
        for r in returned_rows
    ]

    next_cursor = None
    if has_more and returned_rows:
        last = returned_rows[-1]
        next_cursor = encode_cursor(parse_dt(last["created_at"]), last["id"])

    return BookingsListResponse(bookings=bookings_out, next_cursor=next_cursor)


# Level 1: GET /bookings/{id}
@app.get(
    "/bookings/{id}",
    summary="Level 1 · Get a booking",
    response_model=Booking,
    responses={
        200: {"model": Booking},
        404: {"model": ErrorResponse},
    },
)
async def get_booking(id: str):
    with get_db() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, slot_id, patient_name, patient_email, status, created_at
                FROM bookings
                WHERE id = :id
                """
            ),
            {"id": id},
        ).mappings().first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No booking with that id",
        )

    return Booking(
        id=row["id"],
        slot_id=row["slot_id"],
        name=row["patient_name"],
        email=row["patient_email"],
        status=row["status"],
        created_at=parse_dt(row["created_at"]),
    )


# Level 3: DELETE /bookings/{id}
@app.delete(
    "/bookings/{id}",
    summary="Level 3 · Cancel a booking",
    response_model=Booking,
    responses={
        200: {"model": Booking},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def cancel_booking(id: str):
    with get_db() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, slot_id, patient_name, patient_email, status, created_at
                FROM bookings
                WHERE id = :id
                """
            ),
            {"id": id},
        ).mappings().first()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No booking with that id",
            )

        if row["status"] == "cancelled":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Already cancelled",
            )

        slot_id = row["slot_id"]

        # Cancel booking
        conn.execute(
            text("UPDATE bookings SET status = 'cancelled' WHERE id = :id;"),
            {"id": id},
        )

        # Free the slot
        conn.execute(
            text("UPDATE slots SET is_booked = FALSE WHERE id = :slot_id;"),
            {"slot_id": slot_id},
        )

        booking_obj = Booking(
            id=row["id"],
            slot_id=slot_id,
            name=row["patient_name"],
            email=row["patient_email"],
            status="cancelled",
            created_at=parse_dt(row["created_at"]),
        )

    return booking_obj
