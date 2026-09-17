from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=2, max_length=40)
    city: str = "Vilnius"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SignupOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ResendSignupOtpRequest(BaseModel):
    email: EmailStr


class RequestPasswordResetRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(min_length=8)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    city_id: UUID | None = None
    university_or_work: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    location_precision: str
    hide_from_nearby: bool
    restrict_messages: str
    email_verified: bool = True

    class Config:
        from_attributes = True


class PublicUserOut(BaseModel):
    """The non-sensitive portion of a member profile visible to signed-in users."""
    id: UUID
    display_name: str
    university_or_work: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    # One of: self | none | pending_sent | pending_received | accepted —
    # drives the Add Friend / Pending / Friends button on the frontend's
    # profile sheet. Computed by the same Friendship rows friends.py and
    # events.py already treat as the single source of truth; this isn't
    # a second friendship system, just exposing existing state here too.
    friendship_status: str = "none"
    interests: list[str] = []

    class Config:
        from_attributes = True


class ReportProblemCreate(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class ReportSubmit(BaseModel):
    """
    Body for the target-scoped report endpoints (POST /users/{id}/report,
    POST /events/{id}/report) — target_type/target_id are NOT fields
    here on purpose: they come from the URL (validated server-side
    against a real row of that type before app.reports.create_report is
    ever called), never from client-supplied body fields, which is what
    keeps a reporter from manipulating target_id to report an unrelated
    or nonexistent object. reason is a short free-text label (matches
    Report.reason's existing shape — the frontend offers a fixed set of
    suggested reasons, the backend doesn't hard-enforce an enum, same
    latitude report_event already had); details is optional elaboration.
    """
    reason: str = Field(min_length=1, max_length=100)
    details: str | None = Field(default=None, max_length=1000)


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=40)
    university_or_work: str | None = None
    bio: str | None = Field(default=None, max_length=280)
    avatar_url: str | None = None
    location_precision: Literal["approximate", "exact_to_participants"] | None = None
    hide_from_nearby: bool | None = None
    restrict_messages: Literal["everyone", "friends_only"] | None = None
    interests: list[str] | None = None


AccessMode = Literal["public", "approval", "friends", "invite_only", "private"]
GuestPolicy = Literal["none", "one", "two", "host_approval"]
LocationReveal = Literal["after_approval", "confirmed_attendees", "immediate"]

GUEST_POLICY_MAX = {"none": 0, "one": 1, "two": 2, "host_approval": 2}


class EventCreate(BaseModel):
    title: str
    category: str
    description: str | None = None
    latitude: float
    longitude: float
    location_label: str | None = None
    approx_location_label: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    capacity: int = Field(gt=0)
    access_mode: AccessMode = "public"
    guest_policy: GuestPolicy = "none"
    location_reveal: LocationReveal = "immediate"
    chat_enabled: bool = True
    tags: list[str] = []
    cover_image_url: str | None = None


class EventUpdate(BaseModel):
    """
    All fields optional — PATCH semantics, only supplied fields change.
    Deliberately mirrors EventCreate's field set (spec: "use the
    existing create-event UI patterns") rather than being a separate
    shape the frontend has to reconcile.
    """
    title: str | None = None
    category: str | None = None
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None
    approx_location_label: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    capacity: int | None = Field(default=None, gt=0)
    access_mode: AccessMode | None = None
    guest_policy: GuestPolicy | None = None
    location_reveal: LocationReveal | None = None
    chat_enabled: bool | None = None
    cover_image_url: str | None = None


class EventOut(BaseModel):
    id: UUID
    title: str
    category: str
    description: str | None
    distance_km: float | None = None
    starts_at: datetime
    capacity: int
    occupancy: int          # participants + guests currently "going" — see README
    access_mode: AccessMode
    guest_policy: GuestPolicy
    is_full: bool
    host_name: str | None = None
    cover_image_url: str | None = None
    status: str = "active"

    class Config:
        from_attributes = True


class JoinRequestCreate(BaseModel):
    guest_count_requested: int = 0


class JoinRequestOut(BaseModel):
    id: UUID
    user_id: UUID
    status: str
    requested_at: datetime

    class Config:
        from_attributes = True


class GuestInviteCreate(BaseModel):
    guest_name: str | None = None


class GuestInviteOut(BaseModel):
    id: UUID
    token: str
    status: str

    class Config:
        from_attributes = True


class WaitlistOut(BaseModel):
    position: int | None = None
    status: str
    offer_expires_at: datetime | None = None


class CheckInCreate(BaseModel):
    participant_id: UUID | None = None  # host-only manual override
    token: str | None = None            # attendee self check-in via a scanned QR token
    method: Literal["qr", "manual"] = "qr"


class CheckInTokenOut(BaseModel):
    token: str
    expires_at: datetime


class AttendanceOut(BaseModel):
    capacity: int
    occupancy: int
    checked_in: int
    not_checked_in: int
    participants: list[dict]
    guests: list[dict]
    pending_requests: list[dict]
    waitlist: list[dict]


class ReportCreate(BaseModel):
    target_type: Literal["user", "event", "post", "message"]
    target_id: UUID
    reason: str
    details: str | None = None


class BanCreate(BaseModel):
    user_id: UUID
    reason: str | None = None


class RatingCreate(BaseModel):
    tags: list[Literal["reliable_host", "matched_description", "well_organized", "would_join_again"]]


class ImFreeCreate(BaseModel):
    when_window: str = Field(pattern="^(now|tonight|tomorrow|this_weekend)$")
    looking_for: str
    radius_km: float | None = None
    latitude: float
    longitude: float


class SpontaneousPostCreate(BaseModel):
    body: str = Field(min_length=1, max_length=280)
    latitude: float
    longitude: float
    expires_in_minutes: int = Field(default=60, ge=10, le=720)


class ChatMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=1000)
