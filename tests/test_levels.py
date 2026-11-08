import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from httpx import ASGITransport, AsyncClient

from api.index import app, API_KEY
from db import init_db

AUTH_HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def setup_database():
    init_db()


def make_future_iso(hours_ahead: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return dt.isoformat().replace("+00:00", "Z")


def make_past_iso(hours_behind: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_behind)
    return dt.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_level_1_core_crud():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Reset slots
        reset_payload = {
            "slots": [
                {
                    "id": "s_test_1",
                    "provider": "Dr Ade",
                    "start": make_future_iso(2),
                    "end": make_future_iso(3),
                },
                {
                    "id": "s_test_2",
                    "provider": "Dr Ade",
                    "start": make_future_iso(4),
                    "end": make_future_iso(5),
                },
            ]
        }
        res = await client.post("/reset", json=reset_payload, headers=AUTH_HEADERS)
        assert res.status_code == 200

        # 2. List slots
        res = await client.get("/slots", headers=AUTH_HEADERS)
        assert res.status_code == 200
        data = res.json()
        assert "slots" in data
        assert len(data["slots"]) == 2
        slot1 = next(s for s in data["slots"] if s["id"] == "s_test_1")
        assert slot1["available"] is True

        # 3. Book a slot
        booking_payload = {
            "slot_id": "s_test_1",
            "name": "Ada Obi",
            "email": "ada@example.com",
        }
        res = await client.post("/bookings", json=booking_payload, headers=AUTH_HEADERS)
        assert res.status_code == 201
        booking = res.json()
        booking_id = booking["id"]
        assert booking["status"] == "confirmed"
        assert booking["slot_id"] == "s_test_1"
        assert booking["email"] == "ada@example.com"

        # 4. Fetch the booking
        res = await client.get(f"/bookings/{booking_id}", headers=AUTH_HEADERS)
        assert res.status_code == 200
        fetched = res.json()
        assert fetched["id"] == booking_id
        assert fetched["name"] == "Ada Obi"

        # 5. Verify slot availability updated
        res = await client.get("/slots", headers=AUTH_HEADERS)
        data = res.json()
        slot1_after = next(s for s in data["slots"] if s["id"] == "s_test_1")
        assert slot1_after["available"] is False


@pytest.mark.asyncio
async def test_level_2_auth_and_validation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Missing API Key
        res = await client.get("/slots")
        assert res.status_code == 401
        assert "error" in res.json()

        # 2. Wrong API Key
        res = await client.get("/slots", headers={"X-API-Key": "wrong-key"})
        assert res.status_code == 401
        assert "error" in res.json()

        # Public endpoints accessible without key
        res = await client.get("/health")
        assert res.status_code == 200

        # Reset with past and future slot
        await client.post(
            "/reset",
            json={
                "slots": [
                    {
                        "id": "s_past",
                        "provider": "Dr Past",
                        "start": make_past_iso(2),
                        "end": make_past_iso(1),
                    },
                    {
                        "id": "s_future",
                        "provider": "Dr Future",
                        "start": make_future_iso(10),
                        "end": make_future_iso(11),
                    },
                ]
            },
            headers=AUTH_HEADERS,
        )

        # 3. Missing/Empty fields -> 400
        res = await client.post(
            "/bookings",
            json={"slot_id": "", "name": "Test", "email": "test@example.com"},
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 400
        assert "error" in res.json()

        # 4. Invalid email format -> 400
        res = await client.post(
            "/bookings",
            json={
                "slot_id": "s_future",
                "name": "Test",
                "email": "not-an-email",
            },
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 400
        assert "error" in res.json()

        # 5. Non-existent slot -> 404
        res = await client.post(
            "/bookings",
            json={
                "slot_id": "non_existent_slot",
                "name": "Test",
                "email": "valid@example.com",
            },
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 404
        assert "error" in res.json()

        # 6. Slot has already started -> 400
        res = await client.post(
            "/bookings",
            json={
                "slot_id": "s_past",
                "name": "Past User",
                "email": "past@example.com",
            },
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 400
        assert "error" in res.json()

        # 7. Space trimming and lowercasing email
        res = await client.post(
            "/bookings",
            json={
                "slot_id": "s_future",
                "name": "  Trimmed Name  ",
                "email": "  TRIMMED@EXAMPLE.COM  ",
            },
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "Trimmed Name"
        assert data["email"] == "trimmed@example.com"


@pytest.mark.asyncio
async def test_level_3_concurrency_and_cancellation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Reset slots
        await client.post(
            "/reset",
            json={
                "slots": [
                    {
                        "id": "s_race",
                        "provider": "Dr Race",
                        "start": make_future_iso(12),
                        "end": make_future_iso(13),
                    }
                ]
            },
            headers=AUTH_HEADERS,
        )

        # 10 simultaneous booking attempts for the same slot with distinct emails
        tasks = [
            client.post(
                "/bookings",
                json={
                    "slot_id": "s_race",
                    "name": f"User {i}",
                    "email": f"race_user_{i}@example.com",
                },
                headers=AUTH_HEADERS,
            )
            for i in range(10)
        ]

        responses = await asyncio.gather(*tasks)
        status_codes = [r.status_code for r in responses]

        # Exactly 1 success (201) and 9 conflicts (409)
        assert status_codes.count(201) == 1
        assert status_codes.count(409) == 9

        # Find the winning booking ID
        winning_res = next(r for r in responses if r.status_code == 201)
        booking_id = winning_res.json()["id"]

        # Cancel the booking -> 200
        res = await client.delete(f"/bookings/{booking_id}", headers=AUTH_HEADERS)
        assert res.status_code == 200
        assert res.json()["status"] == "cancelled"

        # Cancel again -> 409 already cancelled
        res = await client.delete(f"/bookings/{booking_id}", headers=AUTH_HEADERS)
        assert res.status_code == 409
        assert "error" in res.json()

        # Slot can be booked again
        res = await client.post(
            "/bookings",
            json={
                "slot_id": "s_race",
                "name": "New Person",
                "email": "new_person@example.com",
            },
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 201
        assert res.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_level_4_idempotency_and_pagination():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Reset slots
        slots = [
            {
                "id": f"s_p_{i}",
                "provider": "Dr Pagination",
                "start": make_future_iso(20 + i),
                "end": make_future_iso(21 + i),
            }
            for i in range(6)
        ]
        await client.post("/reset", json={"slots": slots}, headers=AUTH_HEADERS)

        # 1. Idempotency Key test
        body = {
            "slot_id": "s_p_0",
            "name": "Same Person",
            "email": "same@example.com",
        }
        res1 = await client.post(
            "/bookings",
            json=body,
            headers={**AUTH_HEADERS, "Idempotency-Key": "idemp-key-xyz"},
        )
        assert res1.status_code == 201
        b1 = res1.json()

        # Same key with same body -> identical response, no second booking
        res2 = await client.post(
            "/bookings",
            json=body,
            headers={**AUTH_HEADERS, "Idempotency-Key": "idemp-key-xyz"},
        )
        assert res2.status_code == 201
        b2 = res2.json()
        assert b1["id"] == b2["id"]

        # Same key with DIFFERENT body -> 422
        res3 = await client.post(
            "/bookings",
            json={**body, "name": "Different Person"},
            headers={**AUTH_HEADERS, "Idempotency-Key": "idemp-key-xyz"},
        )
        assert res3.status_code == 422
        assert "error" in res3.json()

        # 2. Pagination test
        paged_email = "patient_paged@example.com"
        for i in range(1, 6):
            await client.post(
                "/bookings",
                json={
                    "slot_id": f"s_p_{i}",
                    "name": f"Paged User {i}",
                    "email": paged_email,
                },
                headers=AUTH_HEADERS,
            )

        # Invalid limit < 1 or > 50 -> 400
        res = await client.get(
            f"/bookings?email={paged_email}&limit=0", headers=AUTH_HEADERS
        )
        assert res.status_code == 400

        res = await client.get(
            f"/bookings?email={paged_email}&limit=100", headers=AUTH_HEADERS
        )
        assert res.status_code == 400

        # Page 1: limit 2
        res_p1 = await client.get(
            f"/bookings?email={paged_email}&limit=2", headers=AUTH_HEADERS
        )
        assert res_p1.status_code == 200
        p1_data = res_p1.json()
        assert len(p1_data["bookings"]) == 2
        assert p1_data["next_cursor"] is not None

        # Page 2: limit 2 using cursor
        res_p2 = await client.get(
            f"/bookings?email={paged_email}&limit=2&cursor={p1_data['next_cursor']}",
            headers=AUTH_HEADERS,
        )
        assert res_p2.status_code == 200
        p2_data = res_p2.json()
        assert len(p2_data["bookings"]) == 2
        assert p2_data["next_cursor"] is not None

        # Page 3: final item
        res_p3 = await client.get(
            f"/bookings?email={paged_email}&limit=2&cursor={p2_data['next_cursor']}",
            headers=AUTH_HEADERS,
        )
        assert res_p3.status_code == 200
        p3_data = res_p3.json()
        assert len(p3_data["bookings"]) == 1
        assert p3_data["next_cursor"] is None

        # Email match is case-insensitive
        res_case = await client.get(
            f"/bookings?email={paged_email.upper()}&limit=10", headers=AUTH_HEADERS
        )
        assert res_case.status_code == 200
        assert len(res_case.json()["bookings"]) == 5


@pytest.mark.asyncio
async def test_level_5_rate_limiting():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        target_email = "spam_test@example.com"

        # Send 5 attempts (even invalid slots count toward attempts)
        for i in range(5):
            res = await client.post(
                "/bookings",
                json={
                    "slot_id": f"non_existent_{i}",
                    "name": "Spammer",
                    "email": target_email,
                },
                headers=AUTH_HEADERS,
            )
            # Should be 404 because slot does not exist, but attempt is recorded
            assert res.status_code == 404

        # 6th attempt must be 429 Too Many Requests
        res_6 = await client.post(
            "/bookings",
            json={
                "slot_id": "non_existent_6",
                "name": "Spammer",
                "email": target_email,
            },
            headers=AUTH_HEADERS,
        )
        assert res_6.status_code == 429
        assert "Retry-After" in res_6.headers
        assert int(res_6.headers["Retry-After"]) >= 1
        assert "error" in res_6.json()

        # Different email is NOT blocked
        res_other = await client.post(
            "/bookings",
            json={
                "slot_id": "non_existent_other",
                "name": "Normal User",
                "email": "normal_user@example.com",
            },
            headers=AUTH_HEADERS,
        )
        # 404 because slot doesn't exist, NOT 429
        assert res_other.status_code == 404
