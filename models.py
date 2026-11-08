from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, field_serializer


class Slot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    start: datetime
    end: datetime

    @field_serializer("start", "end", when_used="json")
    def serialize_dt(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            return dt.isoformat() + "Z"
        return dt.isoformat().replace("+00:00", "Z")


class SlotWithAvailability(Slot):
    available: bool


class SlotsResponse(BaseModel):
    slots: List[SlotWithAvailability]


class ResetRequest(BaseModel):
    slots: List[Slot]


class NewBooking(BaseModel):
    slot_id: str
    name: str
    email: str


class Booking(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slot_id: str
    name: str
    email: str
    status: str  # "confirmed" or "cancelled"
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def serialize_dt(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            return dt.isoformat() + "Z"
        return dt.isoformat().replace("+00:00", "Z")


class BookingsListResponse(BaseModel):
    bookings: List[Booking]
    next_cursor: Optional[str] = None


class ErrorResponse(BaseModel):
    error: str
