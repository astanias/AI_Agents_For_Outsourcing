import calendar as month_calendar
import hmac
import json
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.api.auth import authenticate_google_code, create_password_user_account
from app.api.meetings import update_meeting as api_update_meeting
from app.api.recommendations import generate_meeting_time_recommendations
from app.api.deps import get_db
from app.core.avatar import AVATAR_COLOR_OPTIONS, avatar_color_hex, normalize_avatar_color_id
from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models import PasswordCredential, User
from app.schemas.auth import RegisterRequest, UpdateProfileRequest
from app.schemas.meetings import MeetingUpdate
from app.schemas.travel import LocationSuggestion
from app.schemas.recommendations import MeetingRecommendationRequest
from app.services.notifications import (
    get_or_create_notification_preferences,
    notify_meeting_cancelled,
    notify_meeting_invite,
    notify_meeting_updated,
    update_notification_preferences,
)
from app.services.travel import autocomplete_locations, get_travel_warning_service


CSRF_SESSION_KEY = "_csrf_token"
GOOGLE_OAUTH_STATE_SESSION_KEY = "_google_oauth_state"


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def validate_web_csrf(request: Request) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if not settings.csrf_protection_enabled:
        return

    expected = request.session.get(CSRF_SESSION_KEY)
    form = await request.form()
    submitted = form.get("csrf_token")
    if not isinstance(expected, str) or not isinstance(submitted, str):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    if not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


router = APIRouter(tags=["web"], dependencies=[Depends(validate_web_csrf)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["csrf_token"] = csrf_token
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DAY_OPTIONS = [
    (0, "Sunday"),
    (1, "Monday"),
    (2, "Tuesday"),
    (3, "Wednesday"),
    (4, "Thursday"),
    (5, "Friday"),
    (6, "Saturday"),
]
DAY_NAME_BY_INDEX = {day: name for day, name in DAY_OPTIONS}
DAY_SHORT_NAME_BY_INDEX = {day: name[:3] for day, name in DAY_OPTIONS}
WARNING_SEVERITY_ORDER = {"critical": 0, "caution": 1, "info": 2}
ORIGIN_SOURCE_LABELS = {
    "previous_meeting": "From previous meeting",
    "user_default": "From your default location",
    "org_default": "From organization default",
    "unknown": "Origin unresolved",
}
CALENDAR_COLOR_TOKENS = ("sky", "amber", "lime", "coral", "violet")
QUIET_HOURS_OPTIONS = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)]


def _push_flash(request: Request, category: str, msg: str) -> None:
    flashes = request.session.get("_flashes", [])
    flashes.append({"category": category, "message": msg})
    request.session["_flashes"] = flashes


def _pop_flashes(request: Request) -> list[dict[str, str]]:
    flashes = request.session.get("_flashes", [])
    request.session["_flashes"] = []
    return flashes


def _app_base_url(request: Request) -> str:
    configured = (settings.app_base_url or "").strip().rstrip("/")
    if configured:
        if configured.startswith(("http://", "https://")):
            return configured
        return f"https://{configured}"
    return str(request.base_url).rstrip("/")


def _google_redirect_uri(request: Request) -> str:
    return f"{_app_base_url(request)}/web/auth/google/callback"


def _google_authorization_url(*, state: str, redirect_uri: str) -> str:
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def _show_google_oauth_ui() -> bool:
    return not settings.is_deployed_like


def _show_microsoft_oauth_ui() -> bool:
    return not settings.is_deployed_like


def _current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def _app_shell_context(user: User, *, active_page: str) -> dict[str, str]:
    return {
        "email": user.email,
        "active_page": active_page,
        "current_user_name": _display_name_for_user(
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
        ),
        "current_user_email": user.email,
        "current_user_initials": _initials_for_user(
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
        ),
        "current_user_avatar_color_hex": avatar_color_hex(user.avatar_color),
    }


def _parse_datetime_local(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_optional_float(raw: str) -> float | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_optional_date(raw: str) -> date | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_optional_month(raw: str) -> date | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return datetime.strptime(f"{value}-01", "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_optional_time_value(value: Any, *, fallback: str = "") -> str:
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return fallback
        return cleaned[:5]
    return fallback


def _build_location_form_state(
    *,
    location: str,
    location_raw: str,
    location_latitude: str,
    location_longitude: str,
) -> dict[str, str]:
    return {
        "location": location.strip(),
        "location_raw": location_raw.strip(),
        "location_latitude": location_latitude.strip(),
        "location_longitude": location_longitude.strip(),
    }


def _build_group_meeting_form_state(*, invitees: str = "") -> dict[str, str]:
    now = datetime.now(timezone.utc)
    start_dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_dt = start_dt + timedelta(hours=1)
    return {
        "title": "",
        "meeting_type": "in_person",
        **_build_location_form_state(
            location="",
            location_raw="",
            location_latitude="",
            location_longitude="",
        ),
        "start_time": _format_datetime_local_value(start_dt),
        "end_time": _format_datetime_local_value(end_dt),
        "invitees": invitees,
    }


def _resolve_submitted_location(
    *,
    location: str,
    location_raw: str,
    location_latitude: str,
    location_longitude: str,
) -> dict[str, object]:
    display_text = location.strip()
    raw_text = location_raw.strip() or display_text
    latitude = _parse_optional_float(location_latitude)
    longitude = _parse_optional_float(location_longitude)
    coordinates_present = latitude is not None and longitude is not None

    return {
        "location": display_text or raw_text or None,
        "location_raw": raw_text or display_text or None,
        "location_latitude": latitude if coordinates_present else None,
        "location_longitude": longitude if coordinates_present else None,
        "location_is_resolved": coordinates_present,
    }


def _parse_invitee_emails(raw: str) -> tuple[list[str], list[str]]:
    parts = [p.strip().lower() for p in re.split(r"[,;\n]+", raw) if p.strip()]
    seen: set[str] = set()
    valid: list[str] = []
    invalid: list[str] = []

    for email in parts:
        if email in seen:
            continue
        seen.add(email)
        if EMAIL_RE.match(email):
            valid.append(email)
        else:
            invalid.append(email)
    return valid, invalid


def _normalize_meeting_type(raw: str, *, fallback: str = "in_person") -> str:
    value = raw.strip().lower()
    if value in {"in_person", "virtual"}:
        return value
    return fallback


def _get_or_create_personal_calendar(db: Session, user: User) -> int:
    existing_id = db.execute(
        text(
            """
            SELECT id
            FROM calendars
            WHERE owner_type = 'user' AND owner_id = :user_id
            ORDER BY id
            LIMIT 1
            """
        ),
        {"user_id": user.id},
    ).scalar_one_or_none()
    if existing_id is not None:
        return int(existing_id)

    return int(
        db.execute(
            text(
                """
                INSERT INTO calendars (name, owner_type, owner_id)
                VALUES (:name, 'user', :user_id)
                RETURNING id
                """
            ),
            {"name": f"{user.email} calendar", "user_id": user.id},
        ).scalar_one()
    )


def _list_meetings(
    db: Session,
    *,
    user: User,
    q: str,
    status: str,
    mine: bool,
    attendee_statuses: tuple[str, ...] = ("invited", "accepted", "maybe"),
):
    sql = """
        SELECT
            m.id,
            m.title,
            m.description,
            COALESCE(u.email, 'group-calendar') AS organizer_email,
            COALESCE(m.meeting_type, 'in_person') AS meeting_type,
            m.start_time,
            m.end_time,
            m.location,
            m.location_latitude,
            m.location_longitude,
            CASE
                WHEN m.created_by = :current_user_id THEN TRUE
                WHEN c.owner_type = 'user' AND c.owner_id = :current_user_id THEN TRUE
                ELSE FALSE
            END AS is_organizer,
            CASE
                WHEN m.end_time < NOW() THEN 'completed'
                ELSE 'scheduled'
            END AS status,
            (
                EXISTS (
                    SELECT 1
                    FROM calendars own_c
                    WHERE own_c.id = m.calendar_id
                      AND own_c.owner_type = 'user'
                      AND own_c.owner_id = :current_user_id
                )
                OR EXISTS (
                    SELECT 1
                    FROM meeting_attendees own_ma
                    WHERE own_ma.meeting_id = m.id
                      AND own_ma.user_id = :current_user_id
                      AND own_ma.status IN :attendee_statuses
                )
            ) AS is_relevant_to_user
        FROM meetings m
        JOIN calendars c ON c.id = m.calendar_id
        LEFT JOIN users u ON c.owner_type = 'user' AND c.owner_id = u.id
        WHERE COALESCE(m.status, 'confirmed') <> 'cancelled'
          AND (
            m.created_by = :current_user_id
            OR (c.owner_type = 'user' AND c.owner_id = :current_user_id)
            OR EXISTS (
                SELECT 1
                FROM meeting_attendees visible_ma
                WHERE visible_ma.meeting_id = m.id
                  AND visible_ma.user_id = :current_user_id
                  AND visible_ma.status IN :attendee_statuses
            )
          )
    """
    params: dict[str, object] = {"current_user_id": user.id, "attendee_statuses": attendee_statuses}

    if q:
        sql += " AND (m.title ILIKE :q OR COALESCE(m.location, '') ILIKE :q OR COALESCE(u.email, '') ILIKE :q)"
        params["q"] = f"%{q}%"

    if status in {"scheduled", "completed"}:
        if status == "completed":
            sql += " AND m.end_time < NOW()"
        if status == "scheduled":
            sql += " AND m.end_time >= NOW()"

    if mine:
        sql += " AND COALESCE(u.email, '') = :email"
        params["email"] = user.email

    sql += " ORDER BY m.start_time ASC"
    query = text(sql).bindparams(bindparam("attendee_statuses", expanding=True))
    return db.execute(query, params).mappings().all()


def _coerce_datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _format_time_label(value: datetime | None) -> str:
    if value is None:
        return "--"
    return value.strftime("%I:%M %p").lstrip("0")


def _format_datetime_local_value(value: datetime | None) -> str:
    if value is None:
        return ""
    normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M")


def _format_day_label(value: date) -> str:
    return value.strftime("%A, %B ") + str(value.day) + value.strftime(", %Y")


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


def _build_meetings_query_string(*, q: str, status: str, mine: bool, day_value: str) -> str:
    params: dict[str, str] = {}
    if q:
        params["q"] = q
    if status:
        params["status"] = status
    if mine:
        params["mine"] = "1"
    if day_value:
        params["day"] = day_value
    return urlencode(params)


def _shift_month(month_value: date, offset: int) -> date:
    shifted = month_value.replace(day=1)
    month_index = shifted.month - 1 + offset
    year = shifted.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _pick_calendar_color_token(meeting: dict[str, Any]) -> str:
    severity = str(meeting.get("primary_severity") or "none")
    if severity == "critical":
        return "rose"
    if severity == "caution":
        return "amber"
    if severity == "info":
        return "sky"

    meeting_id = int(meeting.get("id") or 0)
    return CALENDAR_COLOR_TOKENS[meeting_id % len(CALENDAR_COLOR_TOKENS)]


def _build_agenda_item(meeting: dict[str, Any]) -> dict[str, Any]:
    start_dt = _coerce_datetime_value(meeting.get("start_time"))
    end_dt = _coerce_datetime_value(meeting.get("end_time"))
    warnings = [dict(item) for item in meeting.get("travel_warnings") or []]
    primary_warning = min(
        warnings,
        key=lambda warning: WARNING_SEVERITY_ORDER.get(str(warning.get("severity")), 99),
        default=None,
    )

    travel_snapshot = next(
        (
            warning
            for warning in warnings
            if warning.get("travel_minutes") is not None
            or warning.get("distance_miles") is not None
            or warning.get("distance_km") is not None
            or warning.get("origin_source") not in {None, "", "unknown"}
        ),
        None,
    )

    if travel_snapshot:
        travel_badges: list[str] = []
        if travel_snapshot.get("travel_minutes") is not None:
            travel_badges.append(f"{travel_snapshot['travel_minutes']} min travel")
        if travel_snapshot.get("distance_miles") is not None:
            travel_badges.append(f"{travel_snapshot['distance_miles']:.1f} mi")
        elif travel_snapshot.get("distance_km") is not None:
            travel_badges.append(f"{travel_snapshot['distance_km']:.1f} km")
        origin_source = str(travel_snapshot.get("origin_source") or "unknown")
        if origin_source in ORIGIN_SOURCE_LABELS:
            travel_badges.append(ORIGIN_SOURCE_LABELS[origin_source])
        travel_summary = " · ".join(travel_badges)
    elif meeting.get("location"):
        travel_summary = "No travel warning recorded."
        travel_badges = []
    else:
        travel_summary = "Travel info unavailable without a location."
        travel_badges = []

    has_actionable_warning = any(
        warning.get("severity") in {"critical", "caution"} for warning in warnings
    )

    return {
        **meeting,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "day_iso": start_dt.date().isoformat() if start_dt else "",
        "time_range_label": f"{_format_time_label(start_dt)} - {_format_time_label(end_dt)}",
        "start_time_label": _format_time_label(start_dt),
        "end_time_label": _format_time_label(end_dt),
        "start_input_value": _format_datetime_local_value(start_dt),
        "end_input_value": _format_datetime_local_value(end_dt),
        "location_label": meeting.get("location") or "No location provided",
        "primary_warning": primary_warning,
        "primary_severity": primary_warning.get("severity", "none") if primary_warning else "none",
        "travel_summary": travel_summary,
        "travel_badges": travel_badges,
        "has_actionable_warning": has_actionable_warning,
        "status_label": str(meeting.get("status") or "scheduled").capitalize(),
    }


def _build_agenda_context(
    meetings: list[dict[str, Any]],
    *,
    selected_day_raw: str,
    q: str,
    status: str,
    mine: bool,
) -> dict[str, Any]:
    agenda_items = [_build_agenda_item(dict(meeting)) for meeting in meetings]
    requested_day = _parse_optional_date(selected_day_raw)
    if requested_day is None:
        requested_day = next(
            (item["start_dt"].date() for item in agenda_items if item.get("start_dt") is not None),
            datetime.now(timezone.utc).date(),
        )

    selected_day_iso = requested_day.isoformat()
    selected_meetings = [item for item in agenda_items if item.get("day_iso") == selected_day_iso]
    warning_count = sum(1 for item in selected_meetings if item["has_actionable_warning"])
    info_count = sum(
        1
        for item in selected_meetings
        if any(warning.get("severity") == "info" for warning in item.get("travel_warnings") or [])
    )

    return {
        "selected_day": selected_day_iso,
        "selected_day_label": _format_day_label(requested_day),
        "meeting_count": len(selected_meetings),
        "warning_count": warning_count,
        "info_count": info_count,
        "meeting_count_label": _pluralize(len(selected_meetings), "meeting"),
        "warning_count_label": _pluralize(warning_count, "travel warning"),
        "info_count_label": _pluralize(info_count, "routing note"),
        "meetings": selected_meetings,
        "is_empty": len(selected_meetings) == 0,
        "prev_query": _build_meetings_query_string(
            q=q,
            status=status,
            mine=mine,
            day_value=(requested_day - timedelta(days=1)).isoformat(),
        ),
        "next_query": _build_meetings_query_string(
            q=q,
            status=status,
            mine=mine,
            day_value=(requested_day + timedelta(days=1)).isoformat(),
        ),
        "today_query": _build_meetings_query_string(
            q=q,
            status=status,
            mine=mine,
            day_value=datetime.now(timezone.utc).date().isoformat(),
        ),
    }


def _build_calendar_context(meetings: list[dict[str, Any]], *, selected_month_raw: str) -> dict[str, Any]:
    agenda_items = [_build_agenda_item(dict(meeting)) for meeting in meetings]
    requested_month = _parse_optional_month(selected_month_raw)
    if requested_month is None:
        requested_month = datetime.now(timezone.utc).date().replace(day=1)

    month_start = requested_month.replace(day=1)
    month_grid = month_calendar.Calendar(firstweekday=6).monthdatescalendar(month_start.year, month_start.month)
    visible_start = month_grid[0][0]
    visible_end = month_grid[-1][-1]
    today = datetime.now(timezone.utc).date()

    meetings_by_day: dict[str, list[dict[str, Any]]] = {}
    month_groups: dict[str, list[dict[str, Any]]] = {}

    for item in agenda_items:
        start_dt = item.get("start_dt")
        if start_dt is None:
            continue

        meeting_day = start_dt.date()
        if not (visible_start <= meeting_day <= visible_end):
            continue

        calendar_item = {
            **item,
            "color_token": _pick_calendar_color_token(item),
            "detail_url": f"/meetings/{item['id']}",
            "date_label": _format_day_label(meeting_day),
            "warning_message": item["primary_warning"]["message"]
            if item.get("primary_warning")
            else "No active travel warning for this meeting.",
        }
        meetings_by_day.setdefault(meeting_day.isoformat(), []).append(calendar_item)

        if meeting_day.month == month_start.month and meeting_day.year == month_start.year:
            month_groups.setdefault(meeting_day.isoformat(), []).append(calendar_item)

    for day_items in meetings_by_day.values():
        day_items.sort(key=lambda item: item["start_dt"] or datetime.max.replace(tzinfo=timezone.utc))
    for day_items in month_groups.values():
        day_items.sort(key=lambda item: item["start_dt"] or datetime.max.replace(tzinfo=timezone.utc))

    weeks: list[list[dict[str, Any]]] = []
    month_days: list[dict[str, Any]] = []
    for week in month_grid:
        week_cells: list[dict[str, Any]] = []
        for day_value in week:
            day_iso = day_value.isoformat()
            day_meetings = meetings_by_day.get(day_iso, [])
            is_current_month = day_value.month == month_start.month
            day_context = {
                "date_iso": day_iso,
                "day_number": day_value.day,
                "weekday_short": day_value.strftime("%a"),
                "is_current_month": is_current_month,
                "is_today": day_value == today,
                "meetings": day_meetings[:3],
                "all_meetings": day_meetings,
                "meeting_count": len(day_meetings),
                "more_count": max(0, len(day_meetings) - 3),
            }
            week_cells.append(day_context)
            if is_current_month:
                month_days.append(day_context)
        weeks.append(week_cells)

    grouped_meetings = [
        {"label": _format_day_label(_parse_optional_date(day_iso) or month_start), "meetings": month_groups[day_iso]}
        for day_iso in sorted(month_groups.keys())
    ]

    month_meeting_count = sum(len(items) for items in month_groups.values())
    return {
        "selected_month": month_start.strftime("%Y-%m"),
        "month_label": month_start.strftime("%B %Y"),
        "meeting_count_label": _pluralize(month_meeting_count, "meeting"),
        "weeks": weeks,
        "month_days": month_days,
        "grouped_meetings": grouped_meetings,
        "is_empty": month_meeting_count == 0,
        "prev_query": urlencode({"month": _shift_month(month_start, -1).strftime("%Y-%m")}),
        "next_query": urlencode({"month": _shift_month(month_start, 1).strftime("%Y-%m")}),
        "today_query": urlencode({"month": datetime.now(timezone.utc).date().replace(day=1).strftime("%Y-%m")}),
    }


def _format_travel_warning_flash(warning: dict[str, Any]) -> str:
    origin = warning.get("origin_location") or "origin"
    destination = warning.get("destination_location") or "meeting"
    travel_minutes = warning.get("travel_minutes")
    available_minutes = warning.get("available_minutes")

    detail = f"{warning['message']} {origin} -> {destination}."
    if travel_minutes is not None and available_minutes is not None:
        detail += f" Estimated travel: {travel_minutes} min; available gap: {available_minutes} min."
    return detail


def _load_meetings_with_travel_context(
    db: Session,
    *,
    user: User,
    q: str,
    status: str,
    mine: bool,
    attendee_statuses: tuple[str, ...] = ("invited", "accepted", "maybe"),
) -> list[dict[str, Any]]:
    rows = _list_meetings(
        db,
        user=user,
        q=q,
        status=status,
        mine=mine,
        attendee_statuses=attendee_statuses,
    )
    fallback_rows: list[dict[str, Any]] = []
    for row in rows:
        meeting = dict(row)
        meeting["travel_warnings"] = []
        fallback_rows.append(meeting)

    try:
        meetings = get_travel_warning_service().enrich_meetings(db, user=user, meetings=rows, persist=True)
        db.commit()
        return meetings
    except Exception:
        db.rollback()
        return fallback_rows


def _display_name_for_user(*, first_name: Any, last_name: Any, email: str) -> str:
    parts = [str(value).strip() for value in (first_name, last_name) if str(value or "").strip()]
    if parts:
        return " ".join(parts)
    return email.split("@", 1)[0]


def _initials_for_user(*, first_name: Any, last_name: Any, email: str) -> str:
    name_parts = [str(value).strip() for value in (first_name, last_name) if str(value or "").strip()]
    if name_parts:
        return "".join(part[0].upper() for part in name_parts[:2])

    handle = email.split("@", 1)[0]
    letters = "".join(char for char in handle if char.isalnum())
    return (letters[:2] or "U").upper()


def _build_invitee_suggestion(row: dict[str, Any], *, kind: str) -> dict[str, Any]:
    email = str(row["email"]).strip().lower()
    invite_count = int(row.get("invite_count") or 0)
    display_name = _display_name_for_user(
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        email=email,
    )

    if kind == "frequent":
        reason = "Usually invite"
        detail = f"Invited {invite_count} time{'s' if invite_count != 1 else ''}"
    else:
        reason = "Handle match"
        detail = display_name

    return {
        "id": int(row["id"]),
        "email": email,
        "display_name": display_name,
        "handle": email.split("@", 1)[0],
        "initials": _initials_for_user(
            first_name=row.get("first_name"),
            last_name=row.get("last_name"),
            email=email,
        ),
        "kind": kind,
        "reason": reason,
        "detail": detail,
        "invite_count": invite_count,
    }


def _frequent_invitee_suggestions(db: Session, *, current_user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                u.id,
                u.first_name,
                u.last_name,
                u.email,
                COUNT(*)::int AS invite_count,
                MAX(m.start_time) AS last_invited_at
            FROM meetings m
            JOIN calendars c ON c.id = m.calendar_id
            JOIN meeting_attendees ma ON ma.meeting_id = m.id
            JOIN users u ON u.id = ma.user_id
            WHERE c.owner_type = 'user'
              AND c.owner_id = :current_user_id
              AND u.id <> :current_user_id
              AND u.is_active = true
              AND ma.status IN ('invited', 'accepted')
              AND EXISTS (
                  SELECT 1
                  FROM group_memberships current_gm
                  JOIN group_memberships candidate_gm
                    ON candidate_gm.group_id = current_gm.group_id
                   AND candidate_gm.user_id = u.id
                  WHERE current_gm.user_id = :current_user_id
              )
            GROUP BY u.id, u.first_name, u.last_name, u.email
            ORDER BY COUNT(*) DESC, MAX(m.start_time) DESC, u.email ASC
            LIMIT :limit
            """
        ),
        {"current_user_id": current_user_id, "limit": limit},
    ).mappings()
    return [_build_invitee_suggestion(dict(row), kind="frequent") for row in rows]


def _matching_invitee_suggestions(
    db: Session,
    *,
    current_user_id: int,
    query: str,
    limit: int = 6,
) -> list[dict[str, Any]]:
    query_norm = query.strip().lower()
    if not query_norm:
        return []

    rows = db.execute(
        text(
            """
            WITH invite_history AS (
                SELECT
                    ma.user_id,
                    COUNT(*)::int AS invite_count,
                    MAX(m.start_time) AS last_invited_at
                FROM meetings m
                JOIN calendars c ON c.id = m.calendar_id
                JOIN meeting_attendees ma ON ma.meeting_id = m.id
                WHERE c.owner_type = 'user'
                  AND c.owner_id = :current_user_id
                  AND ma.user_id <> :current_user_id
                  AND ma.status IN ('invited', 'accepted')
                GROUP BY ma.user_id
            )
            SELECT
                u.id,
                u.first_name,
                u.last_name,
                u.email,
                COALESCE(invite_history.invite_count, 0) AS invite_count,
                CASE
                    WHEN LOWER(u.email) = :query THEN 0
                    WHEN LOWER(split_part(u.email, '@', 1)) = :query THEN 1
                    WHEN LOWER(u.email) LIKE :prefix THEN 2
                    WHEN LOWER(split_part(u.email, '@', 1)) LIKE :prefix THEN 3
                    WHEN LOWER(u.first_name) LIKE :prefix THEN 4
                    WHEN LOWER(u.last_name) LIKE :prefix THEN 5
                    WHEN LOWER(u.first_name || ' ' || u.last_name) LIKE :prefix THEN 6
                    ELSE 7
                END AS match_rank
            FROM users u
            LEFT JOIN invite_history ON invite_history.user_id = u.id
            WHERE u.is_active = true
              AND u.id <> :current_user_id
              AND EXISTS (
                  SELECT 1
                  FROM group_memberships current_gm
                  JOIN group_memberships candidate_gm
                    ON candidate_gm.group_id = current_gm.group_id
                   AND candidate_gm.user_id = u.id
                  WHERE current_gm.user_id = :current_user_id
              )
              AND (
                  LOWER(u.email) LIKE :contains
                  OR LOWER(split_part(u.email, '@', 1)) LIKE :contains
                  OR LOWER(u.first_name) LIKE :contains
                  OR LOWER(u.last_name) LIKE :contains
                  OR LOWER(u.first_name || ' ' || u.last_name) LIKE :contains
              )
            ORDER BY
                match_rank ASC,
                COALESCE(invite_history.invite_count, 0) DESC,
                LOWER(u.first_name) ASC,
                LOWER(u.last_name) ASC,
                LOWER(u.email) ASC
            LIMIT :limit
            """
        ),
        {
            "current_user_id": current_user_id,
            "query": query_norm,
            "prefix": f"{query_norm}%",
            "contains": f"%{query_norm}%",
            "limit": limit,
        },
    ).mappings()
    return [_build_invitee_suggestion(dict(row), kind="match") for row in rows]


def _overlap_conflict_count(
    db: Session,
    *,
    user_id: int,
    slot_start: datetime,
    slot_end: datetime,
    exclude_meeting_id: int | None = None,
) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM meetings m
                WHERE m.start_time < :slot_end
                  AND m.end_time > :slot_start
                  AND (:exclude_meeting_id IS NULL OR m.id <> :exclude_meeting_id)
                  AND (
                    EXISTS (
                        SELECT 1
                        FROM calendars c
                        WHERE c.id = m.calendar_id
                          AND c.owner_type = 'user'
                          AND c.owner_id = :user_id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM meeting_attendees ma
                        WHERE ma.meeting_id = m.id
                          AND ma.user_id = :user_id
                          AND ma.status IN ('invited', 'accepted')
                    )
                  )
                """
            ),
            {
                "slot_start": slot_start,
                "slot_end": slot_end,
                "user_id": user_id,
                "exclude_meeting_id": exclude_meeting_id,
            },
        ).scalar_one()
    )


def _preferred_slot_info(db: Session, *, user_id: int, slot_start: datetime, slot_end: datetime) -> tuple[bool, bool]:
    has_preferences = bool(
        db.execute(
            text("SELECT EXISTS (SELECT 1 FROM time_slot_preferences WHERE user_id = :user_id)"),
            {"user_id": user_id},
        ).scalar_one()
    )
    if not has_preferences:
        return False, False

    day_of_week = slot_start.isoweekday() % 7  # Sunday=0 ... Saturday=6
    start_time = slot_start.time().replace(tzinfo=None)
    end_time = slot_end.time().replace(tzinfo=None)
    within_preference = bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM time_slot_preferences
                    WHERE user_id = :user_id
                      AND day_of_week = :day_of_week
                      AND start_time <= :slot_start_time
                      AND end_time >= :slot_end_time
                )
                """
            ),
            {
                "user_id": user_id,
                "day_of_week": day_of_week,
                "slot_start_time": start_time,
                "slot_end_time": end_time,
            },
        ).scalar_one()
    )
    return True, within_preference


def _availability_summary(
    db: Session,
    *,
    user_id: int,
    slot_start: datetime,
    slot_end: datetime,
    exclude_meeting_id: int | None = None,
) -> tuple[str, int]:
    conflict_count = _overlap_conflict_count(
        db,
        user_id=user_id,
        slot_start=slot_start,
        slot_end=slot_end,
        exclude_meeting_id=exclude_meeting_id,
    )
    if conflict_count > 0:
        return "Busy (has overlapping meetings)", conflict_count

    has_preferences, within_preference = _preferred_slot_info(
        db, user_id=user_id, slot_start=slot_start, slot_end=slot_end
    )
    if has_preferences and not within_preference:
        return "Outside preferred availability", 0
    if has_preferences and within_preference:
        return "Available (within preferred slot)", 0
    return "Available (no preferences set)", 0


def _build_availability_preview(
    db: Session, *, emails: list[str], slot_start: datetime, slot_end: datetime
) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for email in emails:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            preview.append(
                {
                    "email": email,
                    "status": "User not found",
                    "conflicts": 0,
                    "exists": False,
                }
            )
            continue

        status, conflicts = _availability_summary(
            db,
            user_id=user.id,
            slot_start=slot_start,
            slot_end=slot_end,
        )
        preview.append(
            {
                "email": email,
                "status": status,
                "conflicts": conflicts,
                "exists": True,
            }
        )
    return preview


def _load_user_preferences(db: Session, user_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT id, day_of_week, start_time, end_time
            FROM time_slot_preferences
            WHERE user_id = :user_id
            ORDER BY day_of_week ASC, start_time ASC
            """
        ),
        {"user_id": user_id},
    ).mappings()
    preferences: list[dict[str, Any]] = []
    for row in rows:
        day_idx = int(row["day_of_week"])
        start_time = row["start_time"]
        end_time = row["end_time"]
        preferences.append(
            {
                "id": int(row["id"]),
                "day_of_week": day_idx,
                "day_name": DAY_NAME_BY_INDEX.get(day_idx, f"Day {day_idx}"),
                "start_time": start_time.strftime("%H:%M"),
                "end_time": end_time.strftime("%H:%M"),
            }
        )
    return preferences


def _preferences_to_selected_cells(preferences: list[dict[str, Any]]) -> list[dict[str, int]]:
    selected_cells: list[dict[str, int]] = []
    for preference in preferences:
        day_idx = int(preference["day_of_week"])
        start_time = _parse_time_value(preference["start_time"])
        end_time = _parse_time_value(preference["end_time"])
        start_minutes = start_time.hour * 60 + start_time.minute
        end_minutes = end_time.hour * 60 + end_time.minute
        for minute_value in range(start_minutes, end_minutes, 15):
            selected_cells.append({"day_of_week": day_idx, "start_minutes": minute_value})
    # Sort consistently: by day first, then by minute
    return sorted(selected_cells, key=lambda cell: (cell["day_of_week"], cell["start_minutes"]))


def _parse_selected_cells(raw: str) -> list[tuple[int, int]]:
    value = raw.strip()
    if not value:
        return []

    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []

    selected_cells: list[tuple[int, int]] = []
    if not isinstance(payload, list):
        return selected_cells

    for item in payload:
        day_value: Any = None
        minute_value: Any = None
        if isinstance(item, dict):
            day_value = item.get("day_of_week", item.get("day"))
            minute_value = item.get("start_minutes", item.get("minute"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            day_value, minute_value = item[0], item[1]
        elif isinstance(item, str) and ":" in item:
            day_part, minute_part = item.split(":", 1)
            day_value, minute_value = day_part, minute_part

        try:
            day_idx = int(day_value)
            minute_idx = int(minute_value)
        except (TypeError, ValueError):
            continue

        if day_idx not in DAY_NAME_BY_INDEX:
            continue
        if minute_idx % 15 != 0:
            continue
        if minute_idx < 7 * 60 or minute_idx >= 23 * 60:
            continue
        selected_cells.append((day_idx, minute_idx))

    return selected_cells


def _build_availability_calendar(
    preferences: list[dict[str, Any]],
    *,
    selected_cells: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    selected_set = set(selected_cells or [])
    if not selected_set:
        selected_set = {
            (cell["day_of_week"], cell["start_minutes"])
            for cell in _preferences_to_selected_cells(preferences)
        }

    preference_windows: dict[int, list[tuple[int, int]]] = {day_idx: [] for day_idx, _ in DAY_OPTIONS}
    for preference in preferences:
        day_idx = int(preference["day_of_week"])
        start_time = _parse_time_value(preference["start_time"])
        end_time = _parse_time_value(preference["end_time"])
        start_minutes = start_time.hour * 60 + start_time.minute
        end_minutes = end_time.hour * 60 + end_time.minute
        preference_windows[day_idx].append((start_minutes, end_minutes))

    rows: list[dict[str, Any]] = []
    for minute_value in range(7 * 60, 23 * 60, 15):
        slot_end = minute_value + 15
        is_hour = minute_value % 60 == 0
        hour_value = (minute_value // 60) % 12 or 12
        period_value = "AM" if minute_value < 12 * 60 else "PM"
        row_cells: list[dict[str, Any]] = []

        for day_idx, _day_name in DAY_OPTIONS:
            is_available = any(
                window_start <= minute_value and window_end >= slot_end
                for window_start, window_end in preference_windows[day_idx]
            )
            row_cells.append(
                {
                    "day_of_week": day_idx,
                    "start_minutes": minute_value,
                    "end_minutes": slot_end,
                    "is_available": is_available,
                    "is_selected": (day_idx, minute_value) in selected_set,
                }
            )

        rows.append(
            {
                "label": f"{hour_value}:00 {period_value}" if is_hour else "",
                "is_hour": is_hour,
                "start_minutes": minute_value,
                "end_minutes": slot_end,
                "cells": row_cells,
            }
        )

    return {
        "rows": rows,
        "selected_cells": [
            {"day_of_week": day_idx, "start_minutes": minute_value}
            for day_idx, minute_value in sorted(selected_set, key=lambda value: (value[0], value[1]))
        ],
        "selected_count": len(selected_set),
    }


def _availability_context(
    db: Session,
    *,
    user_id: int,
    form_data: dict[str, Any] | None = None,
    next_path: str,
) -> dict[str, Any]:
    preferences = _load_user_preferences(db, user_id)
    selected_cells = _parse_selected_cells(str((form_data or {}).get("selected_cells", "")))
    return {
        "preferences": preferences,
        "day_options": DAY_OPTIONS,
        "day_short_names": DAY_SHORT_NAME_BY_INDEX,
        "availability_form_data": form_data or {"selected_cells": ""},
        "availability_calendar": _build_availability_calendar(preferences, selected_cells=selected_cells),
        "availability_selected_cells_json": json.dumps(
            [
                {"day_of_week": day_idx, "start_minutes": minute}
                for day_idx, minute in (selected_cells or [])
            ]
            or _preferences_to_selected_cells(preferences)
        ),
        "availability_next": next_path,
    }


def _default_notification_form(preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    prefs = preferences or {}
    return {
        "email": bool(prefs.get("email", True)),
        "in_app": bool(prefs.get("in_app", True)),
        "meeting_reminders": bool(prefs.get("meeting_reminders", True)),
        "group_activity": bool(prefs.get("group_activity", True)),
        "weekly_digest": bool(prefs.get("weekly_digest", False)),
        "digest_frequency": str(prefs.get("digest_frequency") or "weekly"),
        "quiet_hours_enabled": bool(prefs.get("quiet_hours_enabled", False)),
        "quiet_hours_start": _format_optional_time_value(prefs.get("quiet_hours_start"), fallback="22:00"),
        "quiet_hours_end": _format_optional_time_value(prefs.get("quiet_hours_end"), fallback="07:00"),
    }


def _default_profile_form(user: User) -> dict[str, str]:
    return {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "avatar_color": normalize_avatar_color_id(user.avatar_color),
        "current_password": "",
        "new_password": "",
        "confirm_password": "",
    }


def _normalize_next_path(raw: str, *, default: str) -> str:
    value = raw.strip()
    allowed = {"/availability", "/settings"}
    if value in allowed:
        return value
    return default


def _calendar_redirect_url(month_raw: str) -> str:
    month_value = month_raw.strip()
    if _parse_optional_month(month_value) is not None:
        return f"/calendar?month={month_value}"
    return "/calendar"


def _load_user_groups(db: Session, *, user_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                g.id,
                g.name,
                g.description,
                gm.role,
                (
                    SELECT COUNT(*)
                    FROM group_memberships gm_count
                    WHERE gm_count.group_id = g.id
                ) AS member_count
            FROM groups g
            JOIN group_memberships gm ON gm.group_id = g.id
            WHERE gm.user_id = :user_id
            ORDER BY LOWER(g.name) ASC, g.id ASC
            """
        ),
        {"user_id": user_id},
    ).mappings().all()

    groups: list[dict[str, Any]] = []
    for row in rows:
        group = dict(row)
        group["token"] = _format_group_token(int(group["id"]))
        group["role_label"] = _group_role_label(str(group["role"]))
        group["can_manage"] = _can_manage_group_role(str(group["role"]))
        groups.append(group)
    return groups


def _format_group_token(group_id: int) -> str:
    return f"{group_id:09d}"


def _parse_group_token(raw: str) -> int | None:
    token = re.sub(r"\D", "", raw.strip())
    if len(token) != 9:
        return None
    return int(token)


def _group_role_label(role: str) -> str:
    normalized = role.strip().lower()
    if normalized == "admin":
        return "Manager"
    if normalized == "owner":
        return "Owner"
    return "Member"


def _can_manage_group_role(role: str) -> bool:
    return role.strip().lower() in {"owner", "admin"}


def _normalize_group_member_role(raw: str, *, fallback: str = "member") -> str:
    value = raw.strip().lower()
    if value in {"manager", "admin"}:
        return "admin"
    if value == "member":
        return "member"
    return fallback


def _group_detail_url(group_id: int, *, month: str = "", member_id: int | None = None) -> str:
    params: dict[str, str] = {}
    month_value = month.strip()
    if month_value:
        params["month"] = month_value
    if member_id is not None:
        params["member_id"] = str(member_id)

    base_url = f"/groups/{group_id}"
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params)}"


def _load_group_membership(db: Session, *, user_id: int, group_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT gm.user_id, gm.group_id, gm.role, g.name, g.description
            FROM group_memberships gm
            JOIN groups g ON g.id = gm.group_id
            WHERE gm.user_id = :user_id
              AND gm.group_id = :group_id
            """
        ),
        {"user_id": user_id, "group_id": group_id},
    ).mappings().one_or_none()
    if row is None:
        return None

    membership = dict(row)
    membership["role_label"] = _group_role_label(str(membership["role"]))
    membership["can_manage"] = _can_manage_group_role(str(membership["role"]))
    return membership


def _load_group_summary(db: Session, *, group_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT
                g.id,
                g.name,
                g.description,
                (
                    SELECT COUNT(*)
                    FROM group_memberships gm
                    WHERE gm.group_id = g.id
                ) AS member_count,
                (
                    SELECT COUNT(*)
                    FROM group_memberships gm
                    WHERE gm.group_id = g.id
                      AND gm.role IN ('owner', 'admin')
                ) AS manager_count
            FROM groups g
            WHERE g.id = :group_id
            """
        ),
        {"group_id": group_id},
    ).mappings().one_or_none()
    if row is None:
        return None

    group = dict(row)
    group["token"] = _format_group_token(int(group["id"]))
    return group


def _load_group_roster(db: Session, *, group_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                u.id,
                u.email,
                u.first_name,
                u.last_name,
                gm.role,
                (
                    SELECT COUNT(DISTINCT m.id)
                    FROM meetings m
                    WHERE COALESCE(m.status, 'confirmed') <> 'cancelled'
                      AND m.end_time >= NOW()
                      AND (
                          m.created_by = u.id
                          OR EXISTS (
                              SELECT 1
                              FROM meeting_attendees ma
                              WHERE ma.meeting_id = m.id
                                AND ma.user_id = u.id
                          )
                      )
                ) AS upcoming_meeting_count,
                (
                    SELECT COUNT(*)
                    FROM time_slot_preferences tsp
                    WHERE tsp.user_id = u.id
                ) AS preference_count
            FROM group_memberships gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = :group_id
            ORDER BY
                CASE gm.role
                    WHEN 'owner' THEN 0
                    WHEN 'admin' THEN 1
                    ELSE 2
                END,
                LOWER(u.email) ASC
            """
        ),
        {"group_id": group_id},
    ).mappings().all()

    roster: list[dict[str, Any]] = []
    for row in rows:
        member = dict(row)
        member["display_name"] = _display_name_for_user(
            first_name=member.get("first_name"),
            last_name=member.get("last_name"),
            email=str(member["email"]),
        )
        member["initials"] = _initials_for_user(
            first_name=member.get("first_name"),
            last_name=member.get("last_name"),
            email=str(member["email"]),
        )
        member["role_label"] = _group_role_label(str(member["role"]))
        member["can_manage"] = _can_manage_group_role(str(member["role"]))
        roster.append(member)
    return roster


def _load_group_member(db: Session, *, group_id: int, member_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT u.id, u.email, u.first_name, u.last_name, gm.role
            FROM group_memberships gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = :group_id
              AND gm.user_id = :member_id
            """
        ),
        {"group_id": group_id, "member_id": member_id},
    ).mappings().one_or_none()
    if row is None:
        return None

    member = dict(row)
    member["display_name"] = _display_name_for_user(
        first_name=member.get("first_name"),
        last_name=member.get("last_name"),
        email=str(member["email"]),
    )
    member["initials"] = _initials_for_user(
        first_name=member.get("first_name"),
        last_name=member.get("last_name"),
        email=str(member["email"]),
    )
    member["role_label"] = _group_role_label(str(member["role"]))
    return member


def _load_group_upcoming_meetings(db: Session, *, group_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT DISTINCT
                m.id,
                m.title,
                m.description,
                m.location,
                COALESCE(m.meeting_type, 'in_person') AS meeting_type,
                m.start_time,
                m.end_time,
                COALESCE(m.status, 'confirmed') AS status,
                creator.email AS organizer_email
            FROM meetings m
            JOIN meeting_attendees ma ON ma.meeting_id = m.id
            JOIN group_memberships gm ON gm.user_id = ma.user_id
            LEFT JOIN users creator ON creator.id = m.created_by
            WHERE gm.group_id = :group_id
              AND COALESCE(m.status, 'confirmed') <> 'cancelled'
              AND m.end_time >= NOW()
            ORDER BY m.start_time ASC, m.id ASC
            """
        ),
        {"group_id": group_id},
    ).mappings().all()

    meetings: list[dict[str, Any]] = []
    for row in rows:
        meeting = dict(row)
        meeting["travel_warnings"] = []
        meetings.append(meeting)
    return meetings


def _load_member_upcoming_meetings(db: Session, *, user_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT DISTINCT
                m.id,
                m.title,
                m.description,
                m.location,
                COALESCE(m.meeting_type, 'in_person') AS meeting_type,
                m.start_time,
                m.end_time,
                COALESCE(m.status, 'confirmed') AS status,
                creator.email AS organizer_email,
                CASE WHEN m.created_by = :user_id THEN TRUE ELSE FALSE END AS is_organizer
            FROM meetings m
            LEFT JOIN users creator ON creator.id = m.created_by
            WHERE COALESCE(m.status, 'confirmed') <> 'cancelled'
              AND m.end_time >= NOW()
              AND (
                  m.created_by = :user_id
                  OR EXISTS (
                      SELECT 1
                      FROM meeting_attendees ma
                      WHERE ma.meeting_id = m.id
                        AND ma.user_id = :user_id
                  )
              )
            ORDER BY m.start_time ASC, m.id ASC
            """
        ),
        {"user_id": user_id},
    ).mappings().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        start_dt = _coerce_datetime_value(item.get("start_time"))
        end_dt = _coerce_datetime_value(item.get("end_time"))
        item.update(
            {
                "day_label": _format_day_label(start_dt.date()) if start_dt else "",
                "time_range_label": f"{_format_time_label(start_dt)} - {_format_time_label(end_dt)}",
                "location_label": (
                    item.get("location")
                    or ("Link to be shared" if item.get("meeting_type") == "virtual" else "Location TBD")
                ),
                "role_label": "Organizer" if item.get("is_organizer") else "Participant",
            }
        )
        items.append(item)
    return items


def _build_member_availability_grid(db: Session, *, user_id: int) -> dict[str, Any]:
    preferences = _load_user_preferences(db, user_id)
    minutes_by_day: dict[int, list[tuple[int, int]]] = {day_idx: [] for day_idx, _ in DAY_OPTIONS}

    for item in preferences:
        start_hours, start_minutes = [int(part) for part in str(item["start_time"]).split(":")[:2]]
        end_hours, end_minutes = [int(part) for part in str(item["end_time"]).split(":")[:2]]
        minutes_by_day[int(item["day_of_week"])].append(
            (start_hours * 60 + start_minutes, end_hours * 60 + end_minutes)
        )

    rows: list[dict[str, Any]] = []
    start_minute = 7 * 60
    end_minute = 22 * 60
    for minute_value in range(start_minute, end_minute, 30):
        slot_start = minute_value
        slot_end = minute_value + 30
        display_hour = (slot_start // 60) % 12 or 12
        display_minutes = slot_start % 60
        display_period = "AM" if slot_start < 12 * 60 else "PM"
        rows.append(
            {
                "label": f"{display_hour}:{display_minutes:02d} {display_period}",
                "cells": [
                    {
                        "day_of_week": day_idx,
                        "is_available": any(
                            window_start <= slot_start and window_end >= slot_end
                            for window_start, window_end in minutes_by_day[day_idx]
                        ),
                    }
                    for day_idx, _day_name in DAY_OPTIONS
                ],
            }
        )

    active_days = [
        DAY_SHORT_NAME_BY_INDEX[day_idx]
        for day_idx, windows in minutes_by_day.items()
        if windows
    ]
    return {
        "rows": rows,
        "has_preferences": bool(preferences),
        "active_days_label": ", ".join(active_days) if active_days else "",
    }


def _build_group_availability_grid(db: Session, *, group_id: int) -> dict[str, Any]:
    rows = db.execute(
        text(
            """
            SELECT
                u.id AS user_id,
                u.email,
                u.first_name,
                u.last_name,
                gm.role,
                tsp.day_of_week,
                tsp.start_time::text AS start_time,
                tsp.end_time::text AS end_time
            FROM group_memberships gm
            JOIN users u ON u.id = gm.user_id
            LEFT JOIN time_slot_preferences tsp ON tsp.user_id = u.id
            WHERE gm.group_id = :group_id
            ORDER BY
                CASE gm.role
                    WHEN 'owner' THEN 0
                    WHEN 'admin' THEN 1
                    ELSE 2
                END,
                LOWER(u.email) ASC,
                tsp.day_of_week ASC,
                tsp.start_time ASC
            """
        ),
        {"group_id": group_id},
    ).mappings().all()

    members_by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        user_id = int(row["user_id"])
        member = members_by_id.setdefault(
            user_id,
            {
                "id": user_id,
                "email": row["email"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "role": row["role"],
                "display_name": _display_name_for_user(
                    first_name=row.get("first_name"),
                    last_name=row.get("last_name"),
                    email=str(row["email"]),
                ),
                "initials": _initials_for_user(
                    first_name=row.get("first_name"),
                    last_name=row.get("last_name"),
                    email=str(row["email"]),
                ),
                "slots": [],
            },
        )
        if row["day_of_week"] is not None:
            member["slots"].append(
                {
                    "day_of_week": int(row["day_of_week"]),
                    "start_time": str(row["start_time"]),
                    "end_time": str(row["end_time"]),
                }
            )

    availability_by_day: dict[int, list[tuple[int, int, int]]] = {day_idx: [] for day_idx, _ in DAY_OPTIONS}
    all_member_ids = [int(member["id"]) for member in members_by_id.values()]
    member_name_by_id = {int(member["id"]): str(member["display_name"]) for member in members_by_id.values()}
    for member in members_by_id.values():
        for slot in member["slots"]:
            start_hours, start_minutes = [int(part) for part in slot["start_time"].split(":")[:2]]
            end_hours, end_minutes = [int(part) for part in slot["end_time"].split(":")[:2]]
            availability_by_day[int(slot["day_of_week"])].append(
                (start_hours * 60 + start_minutes, end_hours * 60 + end_minutes, int(member["id"]))
            )

    rows_out: list[dict[str, Any]] = []
    start_minute = 7 * 60
    end_minute = 23 * 60
    total_members = len(members_by_id)

    for minute_value in range(start_minute, end_minute, 15):
        slot_start = minute_value
        slot_end = minute_value + 15
        display_hour = (slot_start // 60) % 12 or 12
        display_minutes = slot_start % 60
        display_period = "AM" if slot_start < 12 * 60 else "PM"
        is_hour = display_minutes == 0
        row_cells: list[dict[str, Any]] = []

        for day_idx, _day_name in DAY_OPTIONS:
            available_member_ids: set[int] = set()
            for window_start, window_end, member_id in availability_by_day[day_idx]:
                if window_start < slot_end and window_end > slot_start:
                    available_member_ids.add(member_id)

            available_count = len(available_member_ids)
            unavailable_member_ids = [member_id for member_id in all_member_ids if member_id not in available_member_ids]
            status = "none"
            if available_count == total_members and total_members > 0:
                status = "full"
            elif available_count > 0:
                status = "partial"

            row_cells.append(
                {
                    "day_of_week": day_idx,
                    "available_count": available_count,
                    "total_count": total_members,
                    "status": status,
                    "unavailable_count": len(unavailable_member_ids),
                    "unavailable_members": [member_name_by_id[member_id] for member_id in unavailable_member_ids],
                }
            )

        rows_out.append(
            {
                "label": f"{display_hour}:{display_minutes:02d} {display_period}" if is_hour else "",
                "is_hour": is_hour,
                "start_minutes": slot_start,
                "end_minutes": slot_end,
                "cells": row_cells,
            }
        )

    return {
        "rows": rows_out,
        "members": list(members_by_id.values()),
        "has_preferences": bool(members_by_id),
    }


def _has_owned_groups(db: Session, *, user_id: int) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM group_memberships
                    WHERE user_id = :user_id
                      AND role IN ('owner', 'admin')
                )
                """
            ),
            {"user_id": user_id},
        ).scalar_one()
    )


def _normalize_meetings_overview_scope(raw: str, *, has_owned_groups: bool) -> str:
    scope = raw.strip().lower()
    if has_owned_groups and scope == "group":
        return "group"
    return "mine"


def _meetings_overview_redirect_url(scope: str) -> str:
    normalized_scope = scope.strip().lower()
    if normalized_scope == "group":
        return "/meetings/overview?scope=group"
    return "/meetings/overview"


def _meeting_has_owned_group_member(db: Session, *, meeting_id: int, user_id: int) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM meeting_attendees ma
                    JOIN group_memberships gm_member ON gm_member.user_id = ma.user_id
                    JOIN group_memberships gm_owner ON gm_owner.group_id = gm_member.group_id
                    WHERE ma.meeting_id = :meeting_id
                      AND gm_owner.user_id = :user_id
                      AND gm_owner.role IN ('owner', 'admin')
                )
                """
            ),
            {"meeting_id": meeting_id, "user_id": user_id},
        ).scalar_one()
    )


def _load_meeting_action_context(db: Session, *, meeting_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT
                m.id,
                m.title,
                m.created_by,
                COALESCE(m.status, 'confirmed') AS status,
                CASE
                    WHEN c.owner_type = 'user' THEN c.owner_id
                    ELSE NULL
                END AS calendar_owner_id
            FROM meetings m
            JOIN calendars c ON c.id = m.calendar_id
            WHERE m.id = :meeting_id
            """
        ),
        {"meeting_id": meeting_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _meeting_organizer_user_id(meeting_context: dict[str, Any]) -> int | None:
    organizer_id = meeting_context.get("created_by") or meeting_context.get("calendar_owner_id")
    return int(organizer_id) if organizer_id is not None else None


def _user_can_manage_overview_meeting(
    db: Session,
    *,
    meeting_id: int,
    user: User,
    meeting_context: dict[str, Any] | None = None,
) -> bool:
    context = meeting_context or _load_meeting_action_context(db, meeting_id=meeting_id)
    if context is None:
        return False

    organizer_user_id = _meeting_organizer_user_id(context)
    if organizer_user_id == user.id:
        return True
    return _meeting_has_owned_group_member(db, meeting_id=meeting_id, user_id=user.id)


def _user_can_add_people_to_meeting(
    db: Session,
    *,
    meeting_id: int,
    user: User,
    meeting_context: dict[str, Any] | None = None,
) -> bool:
    if _user_can_manage_overview_meeting(
        db,
        meeting_id=meeting_id,
        user=user,
        meeting_context=meeting_context,
    ):
        return True

    return bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM meeting_attendees
                    WHERE meeting_id = :meeting_id
                      AND user_id = :user_id
                )
                """
            ),
            {"meeting_id": meeting_id, "user_id": user.id},
        ).scalar_one()
    )


def _load_meeting_attendees(db: Session, *, meeting_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                ma.user_id,
                u.email,
                u.first_name,
                u.last_name,
                ma.status
            FROM meeting_attendees ma
            JOIN users u ON u.id = ma.user_id
            WHERE ma.meeting_id = :meeting_id
            ORDER BY
                CASE ma.status
                    WHEN 'accepted' THEN 0
                    WHEN 'maybe' THEN 1
                    WHEN 'invited' THEN 2
                    ELSE 3
                END,
                u.email ASC
            """
        ),
        {"meeting_id": meeting_id},
    ).mappings().all()

    attendees: list[dict[str, Any]] = []
    for row in rows:
        attendee = dict(row)
        attendee["display_name"] = _display_name_for_user(
            first_name=attendee.get("first_name"),
            last_name=attendee.get("last_name"),
            email=str(attendee["email"]),
        )
        attendee["initials"] = _initials_for_user(
            first_name=attendee.get("first_name"),
            last_name=attendee.get("last_name"),
            email=str(attendee["email"]),
        )
        attendee["status_label"] = str(attendee.get("status") or "invited").capitalize()
        attendees.append(attendee)
    return attendees


def _load_meetings_overview_items(
    db: Session,
    *,
    user: User,
    scope: str,
) -> list[dict[str, Any]]:
    direct_clause = """
        (
            m.created_by = :user_id
            OR EXISTS (
                SELECT 1
                FROM meeting_attendees own_ma
                WHERE own_ma.meeting_id = m.id
                  AND own_ma.user_id = :user_id
            )
            OR (c.owner_type = 'user' AND c.owner_id = :user_id)
        )
    """
    group_clause = """
        EXISTS (
            SELECT 1
            FROM meeting_attendees group_ma
            JOIN group_memberships gm_member ON gm_member.user_id = group_ma.user_id
            JOIN group_memberships gm_owner ON gm_owner.group_id = gm_member.group_id
            WHERE group_ma.meeting_id = m.id
              AND gm_owner.user_id = :user_id
              AND gm_owner.role IN ('owner', 'admin')
        )
    """
    visibility_clause = direct_clause if scope == "mine" else f"({direct_clause} OR {group_clause})"

    rows = db.execute(
        text(
            f"""
            SELECT
                m.id,
                m.title,
                m.description,
                m.location,
                COALESCE(m.meeting_type, 'in_person') AS meeting_type,
                COALESCE(m.status, 'confirmed') AS status,
                m.start_time,
                m.end_time,
                m.created_by,
                creator.email AS organizer_email,
                creator.first_name AS organizer_first_name,
                creator.last_name AS organizer_last_name,
                CASE
                    WHEN m.created_by = :user_id THEN TRUE
                    WHEN c.owner_type = 'user' AND c.owner_id = :user_id THEN TRUE
                    ELSE FALSE
                END AS is_direct_owner,
                EXISTS (
                    SELECT 1
                    FROM meeting_attendees own_ma
                    WHERE own_ma.meeting_id = m.id
                      AND own_ma.user_id = :user_id
                ) AS is_participant,
                {group_clause} AS is_owned_group_meeting
            FROM meetings m
            JOIN calendars c ON c.id = m.calendar_id
            LEFT JOIN users creator ON creator.id = m.created_by
            WHERE m.end_time >= NOW()
              AND COALESCE(m.status, 'confirmed') <> 'cancelled'
              AND {visibility_clause}
            ORDER BY m.start_time ASC, m.id ASC
            """
        ),
        {"user_id": user.id},
    ).mappings().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        meeting_context = {
            "id": item["id"],
            "title": item["title"],
            "created_by": item.get("created_by"),
            "calendar_owner_id": user.id if item.get("is_direct_owner") else None,
        }
        attendees = _load_meeting_attendees(db, meeting_id=int(item["id"]))
        start_dt = _coerce_datetime_value(item.get("start_time"))
        end_dt = _coerce_datetime_value(item.get("end_time"))
        organizer_email = str(item.get("organizer_email") or user.email)

        accepted_count = sum(1 for attendee in attendees if attendee.get("status") == "accepted")
        maybe_count = sum(1 for attendee in attendees if attendee.get("status") == "maybe")
        invited_count = sum(1 for attendee in attendees if attendee.get("status") == "invited")
        declined_count = sum(1 for attendee in attendees if attendee.get("status") == "declined")

        item.update(
            {
                "attendees": attendees,
                "attendee_count": len(attendees),
                "accepted_count": accepted_count,
                "maybe_count": maybe_count,
                "invited_count": invited_count,
                "declined_count": declined_count,
                "start_input_value": _format_datetime_local_value(start_dt),
                "end_input_value": _format_datetime_local_value(end_dt),
                "day_label": _format_day_label(start_dt.date()) if start_dt else "",
                "time_range_label": f"{_format_time_label(start_dt)} - {_format_time_label(end_dt)}",
                "organizer_label": _display_name_for_user(
                    first_name=item.get("organizer_first_name"),
                    last_name=item.get("organizer_last_name"),
                    email=organizer_email,
                ),
                "location_label": (
                    item.get("location")
                    or ("Link to be shared" if item.get("meeting_type") == "virtual" else "Location TBD")
                ),
                "current_user_id": user.id,
                "current_user_status": next(
                    (a["status"] for a in attendees if a["user_id"] == user.id),
                    None,
                ),
                "is_group_visible_only": bool(item.get("is_owned_group_meeting")) and not (
                    bool(item.get("is_direct_owner")) or bool(item.get("is_participant"))
                ),
            }
        )
        item["can_manage"] = _user_can_manage_overview_meeting(
            db,
            meeting_id=int(item["id"]),
            user=user,
            meeting_context=meeting_context,
        )
        item["can_add_people"] = _user_can_add_people_to_meeting(
            db,
            meeting_id=int(item["id"]),
            user=user,
            meeting_context=meeting_context,
        )
        items.append(item)
    return items


def _parse_time_value(raw: str) -> time:
    parsed = time.fromisoformat(raw.strip())
    if parsed.second != 0 or parsed.microsecond != 0 or parsed.minute not in {0, 15, 30, 45}:
        raise ValueError("Availability times must use 15-minute increments.")
    return parsed.replace(second=0, microsecond=0)


def _parse_day_values(raw_values: list[str]) -> list[int]:
    seen: set[int] = set()
    parsed_days: list[int] = []

    for raw in raw_values:
        value = raw.strip()
        if not value:
            continue

        day_value = int(value)
        if day_value < 0 or day_value > 6:
            raise ValueError("Day of week must be between 0 and 6.")
        if day_value in seen:
            continue

        seen.add(day_value)
        parsed_days.append(day_value)

    return parsed_days


def _render_availability_page(
    request: Request,
    *,
    db: Session,
    user: User,
    form_data: dict[str, Any] | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="availability.html",
        context={
            **_app_shell_context(user, active_page="availability"),
            "messages": _pop_flashes(request),
            **_availability_context(db, user_id=user.id, form_data=form_data, next_path="/availability"),
        },
    )


def _render_signup_page(
    request: Request,
    *,
    form_data: dict[str, str] | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="signup.html",
        context={
            "messages": _pop_flashes(request),
            "form_data": form_data
            or {
                "first_name": "",
                "last_name": "",
                "email": "",
                "phone": "",
            },
        },
    )


def _render_meetings_page(
    request: Request,
    *,
    db: Session,
    user: User,
    q: str,
    status: str,
    mine: bool,
    selected_day: str = "",
    create_form: dict[str, str] | None = None,
    availability_preview: list[dict[str, Any]] | None = None,
    recommendation_form: dict[str, str] | None = None,
    meeting_recommendations: list[dict[str, Any]] | None = None,
    unresolved_recommendation_emails: list[str] | None = None,
    unresolved_recommendation_user_ids: list[int] | None = None,
):
    create_form_value = {
        "title": "",
        "meeting_type": "in_person",
        "location": "",
        "location_raw": "",
        "location_latitude": "",
        "location_longitude": "",
        "start_time": "",
        "end_time": "",
        "invitees": "",
    }
    if create_form:
        create_form_value.update(create_form)
    recommendation_form_value = {
        "window_start": create_form_value["start_time"],
        "window_end": create_form_value["end_time"],
        "duration_minutes": "60",
        "slot_interval_minutes": "30",
        "max_results": "5",
    }
    if recommendation_form:
        recommendation_form_value.update(recommendation_form)
    meetings = _load_meetings_with_travel_context(db, user=user, q=q, status=status, mine=mine)

    return templates.TemplateResponse(
        request=request,
        name="meetings.html",
        context={
            **_app_shell_context(user, active_page="meeting_scheduler"),
            "meetings": meetings,
            "agenda": _build_agenda_context(
                meetings,
                selected_day_raw=selected_day,
                q=q,
                status=status,
                mine=mine,
            ),
            "q": q,
            "status": status,
            "mine": mine,
            "selected_day": selected_day,
            "messages": _pop_flashes(request),
            "create_form": create_form_value,
            "availability_preview": availability_preview or [],
            "recommendation_form": recommendation_form_value,
            "meeting_recommendations": meeting_recommendations or [],
            "unresolved_recommendation_emails": unresolved_recommendation_emails or [],
            "unresolved_recommendation_user_ids": unresolved_recommendation_user_ids or [],
        },
    )


def _render_meetings_overview_page(
    request: Request,
    *,
    db: Session,
    user: User,
    scope: str = "",
):
    has_owned_groups = _has_owned_groups(db, user_id=user.id)
    normalized_scope = _normalize_meetings_overview_scope(scope, has_owned_groups=has_owned_groups)
    overview_meetings = _load_meetings_overview_items(db, user=user, scope=normalized_scope)
    return templates.TemplateResponse(
        request=request,
        name="meetings_overview.html",
        context={
            **_app_shell_context(user, active_page="meetings_overview"),
            "messages": _pop_flashes(request),
            "overview_scope": normalized_scope,
            "has_owned_groups": has_owned_groups,
            "overview_meetings": overview_meetings,
            "overview_meeting_count": len(overview_meetings),
        },
    )


def _render_calendar_page(
    request: Request,
    *,
    db: Session,
    user: User,
    selected_month: str = "",
):
    meetings = _load_meetings_with_travel_context(
        db,
        user=user,
        q="",
        status="",
        mine=False,
        attendee_statuses=("accepted", "maybe"),
    )
    return templates.TemplateResponse(
        request=request,
        name="calendar.html",
        context={
            **_app_shell_context(user, active_page="calendar"),
            "messages": _pop_flashes(request),
            "calendar_view": _build_calendar_context(meetings, selected_month_raw=selected_month),
        },
    )


def _render_settings_page(
    request: Request,
    *,
    db: Session,
    user: User,
    profile_form: dict[str, str] | None = None,
    notification_form: dict[str, Any] | None = None,
    availability_form_data: dict[str, Any] | None = None,
):
    preferences = get_or_create_notification_preferences(user.id, db)
    profile_form_value = profile_form or _default_profile_form(user)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            **_app_shell_context(user, active_page="settings"),
            "messages": _pop_flashes(request),
            "profile_form": profile_form_value,
            "avatar_color_options": AVATAR_COLOR_OPTIONS,
            "profile_avatar_color_hex": avatar_color_hex(profile_form_value.get("avatar_color")),
            "notification_form": notification_form or _default_notification_form(preferences),
            "quiet_hours_options": QUIET_HOURS_OPTIONS,
            **_availability_context(db, user_id=user.id, form_data=availability_form_data, next_path="/settings"),
        },
    )


def _render_assistant_page(
    request: Request,
    *,
    user: User,
):
    return templates.TemplateResponse(
        request=request,
        name="assistant.html",
        context={
            **_app_shell_context(user, active_page="assistant"),
            "messages": _pop_flashes(request),
        },
    )


def _render_groups_page(
    request: Request,
    *,
    db: Session,
    user: User,
    create_form: dict[str, str] | None = None,
    join_form: dict[str, str] | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="groups.html",
        context={
            **_app_shell_context(user, active_page="groups"),
            "messages": _pop_flashes(request),
            "groups": _load_user_groups(db, user_id=user.id),
            "group_create_form": create_form or {"name": "", "description": ""},
            "group_join_form": join_form or {"token": ""},
        },
    )


def _render_group_detail_page(
    request: Request,
    *,
    db: Session,
    user: User,
    group_id: int,
    month: str = "",
    member_id: int | None = None,
    invite_form: dict[str, str] | None = None,
):
    membership = _load_group_membership(db, user_id=user.id, group_id=group_id)
    if membership is None:
        _push_flash(request, "error", "Group not found.")
        return RedirectResponse(url="/groups", status_code=303)

    group = _load_group_summary(db, group_id=group_id)
    if group is None:
        _push_flash(request, "error", "Group not found.")
        return RedirectResponse(url="/groups", status_code=303)

    roster = _load_group_roster(db, group_id=group_id)
    roster_invitees = ", ".join(member["email"] for member in roster)
    selected_member = None
    if membership["can_manage"] and member_id is not None:
        selected_member = _load_group_member(db, group_id=group_id, member_id=member_id)
        if selected_member is None:
            _push_flash(request, "warning", "That group member could not be found.")

    selected_member_meetings = (
        _load_member_upcoming_meetings(db, user_id=int(selected_member["id"]))
        if selected_member is not None
        else []
    )
    selected_member_availability = (
        _build_member_availability_grid(db, user_id=int(selected_member["id"]))
        if selected_member is not None
        else {"rows": [], "has_preferences": False, "active_days_label": ""}
    )

    group_meeting_grid = _build_group_availability_grid(db, group_id=group_id)

    return templates.TemplateResponse(
        request=request,
        name="group_detail.html",
        context={
            **_app_shell_context(user, active_page="groups"),
            "messages": _pop_flashes(request),
            "group": group,
            "group_membership": membership,
            "group_can_manage": membership["can_manage"],
            "group_roster": roster,
            "group_invite_form": invite_form or {"invitees": "", "role": "member"},
            "group_meeting_form": _build_group_meeting_form_state(invitees=roster_invitees),
            "group_calendar_view": _build_calendar_context(
                _load_group_upcoming_meetings(db, group_id=group_id),
                selected_month_raw=month,
            ),
            "group_meeting_grid": group_meeting_grid,
            "selected_member": selected_member,
            "selected_member_meetings": selected_member_meetings,
            "selected_member_availability": selected_member_availability,
            "day_options": DAY_OPTIONS,
        },
    )


@router.get("/", name="web_index")
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "messages": _pop_flashes(request),
            "show_google_oauth": _show_google_oauth_ui(),
            "show_microsoft_oauth": _show_microsoft_oauth_ui(),
        },
    )


@router.get("/signup", name="web_signup_page")
def signup_page(request: Request):
    return _render_signup_page(request)


@router.post("/signup", name="web_signup")
def signup(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    form_data = {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "email": email.strip(),
        "phone": phone.strip(),
    }

    if password != confirm_password:
        _push_flash(request, "error", "Passwords do not match.")
        return _render_signup_page(request, form_data=form_data)

    try:
        payload = RegisterRequest(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email.strip().lower(),
            phone=phone.strip() or None,
            password=password,
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Use valid signup details."
        _push_flash(request, "error", str(first_error))
        return _render_signup_page(request, form_data=form_data)

    try:
        user = create_password_user_account(db, payload=payload)
    except HTTPException as exc:
        _push_flash(request, "error", str(exc.detail))
        return _render_signup_page(request, form_data=form_data)

    request.session["user_id"] = user.id
    _push_flash(request, "success", f"Account created. Signed in as {user.email}")
    return RedirectResponse(url="/meetings", status_code=303)


@router.post("/login", name="web_login")
def web_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email_norm = email.strip().lower()
    user = db.execute(select(User).where(User.email == email_norm)).scalar_one_or_none()

    if user is None or not user.is_active:
        _push_flash(request, "error", "Invalid credentials.")
        return RedirectResponse(url="/", status_code=303)

    cred = db.get(PasswordCredential, user.id)
    if cred is None or not verify_password(password, cred.password_hash):
        _push_flash(request, "error", "Invalid credentials.")
        return RedirectResponse(url="/", status_code=303)

    request.session["user_id"] = user.id
    _push_flash(request, "success", f"Signed in as {user.email}")
    return RedirectResponse(url="/meetings", status_code=303)


@router.get("/locations/autocomplete", name="web_locations_autocomplete")
def locations_autocomplete(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        return JSONResponse(status_code=401, content={"suggestions": [], "status": "unauthorized"})

    query = q.strip()
    if len(query) < 3:
        return {"suggestions": [], "status": "idle"}

    suggestions = [
        LocationSuggestion(label=item.label, latitude=item.latitude, longitude=item.longitude).model_dump(mode="python")
        for item in autocomplete_locations(query, size=5)
    ]
    return {"suggestions": suggestions, "status": "ok"}


@router.get("/invitees/suggestions", name="web_invitee_suggestions")
def invitee_suggestions(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        return JSONResponse(status_code=401, content={"frequent": [], "matches": [], "status": "unauthorized"})

    query = q.strip()
    frequent = _frequent_invitee_suggestions(db, current_user_id=user.id)
    matches = _matching_invitee_suggestions(db, current_user_id=user.id, query=query)
    return {"frequent": frequent, "matches": matches, "status": "ok"}


@router.get("/dashboard", name="web_dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    now = datetime.now(timezone.utc)
    week_end = now + timedelta(days=7)

    all_upcoming = _load_member_upcoming_meetings(db, user_id=user.id)
    all_upcoming = _load_member_upcoming_meetings(db, user_id=user.id)
    meetings_this_week = sum(
        1 for m in all_upcoming
        if m.get("start_time") and _coerce_datetime_value(m["start_time"]) and
        now <= _coerce_datetime_value(m["start_time"]) <= week_end
    )

    pending_rsvps_rows = db.execute(
        text("""
            SELECT m.id, m.title, m.start_time, m.end_time, creator.email AS organizer_email
            FROM meeting_attendees ma
            JOIN meetings m ON m.id = ma.meeting_id
            LEFT JOIN users creator ON creator.id = m.created_by
            WHERE ma.user_id = :user_id
              AND ma.status = 'invited'
              AND m.end_time >= NOW()
              AND COALESCE(m.status, 'confirmed') <> 'cancelled'
            ORDER BY m.start_time ASC
        """),
        {"user_id": user.id},
    ).mappings().all()

    pending_rsvps = []
    for row in pending_rsvps_rows:
        item = dict(row)
        start_dt = _coerce_datetime_value(item.get("start_time"))
        end_dt = _coerce_datetime_value(item.get("end_time"))
        item["day_label"] = _format_day_label(start_dt.date()) if start_dt else ""
        item["time_range_label"] = f"{_format_time_label(start_dt)} - {_format_time_label(end_dt)}"
        pending_rsvps.append(item)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **_app_shell_context(user, active_page="dashboard"),
            "messages": _pop_flashes(request),
            "upcoming_meetings": all_upcoming[:5],
            "upcoming_meetings_total": len(all_upcoming),
            "meetings_this_week": meetings_this_week,
            "pending_rsvps": pending_rsvps,
        },
    )


@router.get("/availability", name="web_availability")
def availability_page(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    return _render_availability_page(request, db=db, user=user)


@router.get("/calendar", name="web_calendar")
def calendar_page(request: Request, db: Session = Depends(get_db), month: str = ""):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    return _render_calendar_page(request, db=db, user=user, selected_month=month.strip())


@router.get("/assistant", name="web_assistant")
def assistant_page(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    return _render_assistant_page(request, user=user)


@router.post("/calendar/meetings/update", name="web_calendar_meeting_update")
def calendar_meeting_update(
    request: Request,
    meeting_id: int = Form(...),
    month: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    location: str = Form(""),
    meeting_type: str = Form("in_person"),
    start_time: str = Form(""),
    end_time: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    redirect_url = _calendar_redirect_url(month)

    try:
        payload = MeetingUpdate(
            title=title.strip(),
            description=description.strip() or None,
            location=location.strip() or None,
            meeting_type=meeting_type.strip() or None,
            start_time=_parse_datetime_local(start_time),
            end_time=_parse_datetime_local(end_time),
        )
        api_update_meeting(meeting_id=meeting_id, payload=payload, current_user=user, db=db)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Use valid meeting details."
        _push_flash(request, "error", str(first_error))
        return RedirectResponse(url=redirect_url, status_code=303)
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            message = detail.get("message") or "Unable to update the meeting."
        else:
            message = str(detail)
        _push_flash(request, "error", message)
        return RedirectResponse(url=redirect_url, status_code=303)

    _push_flash(request, "success", "Meeting updated. Attendees have been asked to reconfirm.")
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("/settings", name="web_settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    return _render_settings_page(request, db=db, user=user)


@router.post("/settings/profile", name="web_settings_profile")
def settings_profile(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    avatar_color: str = Form("blue"),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    profile_form = {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "email": email.strip(),
        "avatar_color": normalize_avatar_color_id(avatar_color),
        "current_password": "",
        "new_password": "",
        "confirm_password": "",
    }

    changing_password = any(value.strip() for value in (current_password, new_password, confirm_password))
    if changing_password:
        if not new_password.strip():
            _push_flash(request, "error", "Enter a new password to change your password.")
            return _render_settings_page(request, db=db, user=user, profile_form=profile_form)
        if new_password != confirm_password:
            _push_flash(request, "error", "New passwords do not match.")
            return _render_settings_page(request, db=db, user=user, profile_form=profile_form)

    try:
        payload = UpdateProfileRequest(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email.strip().lower(),
            avatar_color=profile_form["avatar_color"],
            current_password=current_password.strip() or None,
            new_password=new_password.strip() or None,
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Use valid profile details."
        _push_flash(request, "error", str(first_error))
        return _render_settings_page(request, db=db, user=user, profile_form=profile_form)

    if not payload.first_name or not payload.last_name or not payload.email:
        _push_flash(request, "error", "First name, last name, and email are required.")
        return _render_settings_page(request, db=db, user=user, profile_form=profile_form)

    if payload.new_password:
        if not payload.current_password:
            _push_flash(request, "error", "Current password is required to set a new password.")
            return _render_settings_page(request, db=db, user=user, profile_form=profile_form)
        cred = db.get(PasswordCredential, user.id)
        if cred is None or not verify_password(payload.current_password, cred.password_hash):
            _push_flash(request, "error", "Current password is incorrect.")
            return _render_settings_page(request, db=db, user=user, profile_form=profile_form)
        cred.password_hash = hash_password(payload.new_password)

    email_norm = payload.email.strip().lower()
    existing = db.execute(select(User).where(User.email == email_norm)).scalar_one_or_none()
    if existing is not None and existing.id != user.id:
        _push_flash(request, "error", "Email already in use.")
        return _render_settings_page(request, db=db, user=user, profile_form=profile_form)

    user.first_name = payload.first_name.strip()
    user.last_name = payload.last_name.strip()
    user.email = email_norm
    user.avatar_color = normalize_avatar_color_id(payload.avatar_color)
    db.commit()
    db.refresh(user)

    _push_flash(request, "success", "Profile updated.")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/notifications", name="web_settings_notifications")
def settings_notifications(
    request: Request,
    email_enabled: str | None = Form(None),
    in_app_enabled: str | None = Form(None),
    meeting_reminders_enabled: str | None = Form(None),
    group_activity_enabled: str | None = Form(None),
    weekly_digest_enabled: str | None = Form(None),
    digest_frequency: str = Form("weekly"),
    quiet_hours_enabled: str | None = Form(None),
    quiet_hours_start: str = Form("22:00"),
    quiet_hours_end: str = Form("07:00"),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    notification_form = {
        "email": email_enabled is not None,
        "in_app": in_app_enabled is not None,
        "meeting_reminders": meeting_reminders_enabled is not None,
        "group_activity": group_activity_enabled is not None,
        "weekly_digest": weekly_digest_enabled is not None,
        "digest_frequency": digest_frequency.strip().lower() or "weekly",
        "quiet_hours_enabled": quiet_hours_enabled is not None,
        "quiet_hours_start": quiet_hours_start.strip() or "22:00",
        "quiet_hours_end": quiet_hours_end.strip() or "07:00",
    }

    if notification_form["digest_frequency"] not in {"daily", "weekly"}:
        _push_flash(request, "error", "Choose a valid digest frequency.")
        return _render_settings_page(request, db=db, user=user, notification_form=notification_form)

    quiet_start: time | None = None
    quiet_end: time | None = None
    if notification_form["quiet_hours_enabled"]:
        try:
            quiet_start = _parse_time_value(notification_form["quiet_hours_start"])
            quiet_end = _parse_time_value(notification_form["quiet_hours_end"])
        except Exception:
            _push_flash(request, "error", "Use valid quiet-hour times.")
            return _render_settings_page(request, db=db, user=user, notification_form=notification_form)

    update_notification_preferences(
        user.id,
        {
            "email": notification_form["email"],
            "in_app": notification_form["in_app"],
            "meeting_reminders": notification_form["meeting_reminders"],
            "group_activity": notification_form["group_activity"],
            "weekly_digest": notification_form["weekly_digest"],
            "digest_frequency": notification_form["digest_frequency"],
            "quiet_hours_enabled": notification_form["quiet_hours_enabled"],
            "quiet_hours_start": quiet_start,
            "quiet_hours_end": quiet_end,
        },
        db,
    )

    _push_flash(request, "success", "Notification preferences updated.")
    return RedirectResponse(url="/settings", status_code=303)


@router.get("/groups", name="web_groups")
def groups_page(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    return _render_groups_page(request, db=db, user=user)


@router.get("/groups/{group_id}", name="web_group_detail")
def group_detail_page(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
    month: str = "",
    member_id: int | None = None,
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    return _render_group_detail_page(
        request,
        db=db,
        user=user,
        group_id=group_id,
        month=month.strip(),
        member_id=member_id,
    )


@router.post("/groups/create", name="web_groups_create")
def groups_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    create_form = {"name": name.strip(), "description": description.strip()}
    if not create_form["name"]:
        _push_flash(request, "error", "Group name is required.")
        return _render_groups_page(request, db=db, user=user, create_form=create_form)

    group_row = db.execute(
        text(
            """
            INSERT INTO groups (name, description)
            VALUES (:name, :description)
            RETURNING id, name
            """
        ),
        {
            "name": create_form["name"],
            "description": create_form["description"] or None,
        },
    ).mappings().one()

    db.execute(
        text(
            """
            INSERT INTO group_memberships (user_id, group_id, role)
            VALUES (:user_id, :group_id, 'owner')
            """
        ),
        {"user_id": user.id, "group_id": group_row["id"]},
    )
    db.commit()

    _push_flash(
        request,
        "success",
        f"Created {group_row['name']}. Share token {_format_group_token(int(group_row['id']))} to invite others.",
    )
    return RedirectResponse(url="/groups", status_code=303)


@router.post("/groups/{group_id}/invite", name="web_groups_invite")
def groups_invite(
    group_id: int,
    request: Request,
    invitees: str = Form(""),
    role: str = Form("member"),
    month: str = Form(""),
    member_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    membership = _load_group_membership(db, user_id=user.id, group_id=group_id)
    redirect_member_id = int(member_id) if member_id.strip().isdigit() else None
    redirect_url = _group_detail_url(group_id, month=month.strip(), member_id=redirect_member_id)
    normalized_role = _normalize_group_member_role(role)
    invite_form = {"invitees": invitees.strip(), "role": normalized_role}

    if membership is None:
        _push_flash(request, "error", "Group not found.")
        return RedirectResponse(url="/groups", status_code=303)
    if not membership["can_manage"]:
        _push_flash(request, "error", "Only owners and managers can invite people to this group.")
        return RedirectResponse(url=redirect_url, status_code=303)

    emails, invalid_emails = _parse_invitee_emails(invitees)
    if not emails:
        _push_flash(request, "error", "Add at least one valid email to invite.")
        return _render_group_detail_page(
            request,
            db=db,
            user=user,
            group_id=group_id,
            month=month.strip(),
            member_id=redirect_member_id,
            invite_form=invite_form,
        )

    missing_users: list[str] = []
    already_in_group: list[str] = []
    updated_users: list[str] = []
    added_users: list[str] = []

    for email in emails:
        invitee = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if invitee is None:
            missing_users.append(email)
            continue

        existing_membership = db.execute(
            text(
                """
                SELECT role
                FROM group_memberships
                WHERE group_id = :group_id
                  AND user_id = :user_id
                """
            ),
            {"group_id": group_id, "user_id": invitee.id},
        ).mappings().one_or_none()

        if existing_membership is None:
            db.execute(
                text(
                    """
                    INSERT INTO group_memberships (user_id, group_id, role)
                    VALUES (:user_id, :group_id, :role)
                    """
                ),
                {"user_id": invitee.id, "group_id": group_id, "role": normalized_role},
            )
            added_users.append(email)
            continue

        existing_role = str(existing_membership["role"])
        if invitee.id == user.id or existing_role == "owner" or existing_role == normalized_role:
            already_in_group.append(email)
            continue

        db.execute(
            text(
                """
                UPDATE group_memberships
                SET role = :role
                WHERE group_id = :group_id
                  AND user_id = :user_id
                """
            ),
            {"role": normalized_role, "group_id": group_id, "user_id": invitee.id},
        )
        updated_users.append(email)

    db.commit()

    invited_role_label = "manager" if normalized_role == "admin" else "member"
    if added_users:
        _push_flash(request, "success", f"Added {len(added_users)} {invited_role_label}(s) to the group.")
    if updated_users:
        _push_flash(request, "success", f"Updated {len(updated_users)} existing group member(s) to {invited_role_label}.")
    if already_in_group:
        _push_flash(request, "warning", f"Already in the group: {', '.join(already_in_group)}.")
    if missing_users:
        _push_flash(request, "warning", f"Not registered yet: {', '.join(missing_users)}.")
    if invalid_emails:
        _push_flash(request, "warning", f"Ignored invalid emails: {', '.join(invalid_emails)}.")

    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/groups/join-token", name="web_groups_join")
def groups_join(
    request: Request,
    token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    join_form = {"token": token.strip()}
    group_id = _parse_group_token(token)
    if group_id is None:
        _push_flash(request, "error", "Enter a valid 9-digit group token.")
        return _render_groups_page(request, db=db, user=user, join_form=join_form)

    existing_group = db.execute(
        text("SELECT id, name FROM groups WHERE id = :group_id"),
        {"group_id": group_id},
    ).mappings().one_or_none()
    if existing_group is None:
        _push_flash(request, "error", "Group not found.")
        return _render_groups_page(request, db=db, user=user, join_form=join_form)

    try:
        db.execute(
            text(
                """
                INSERT INTO group_memberships (user_id, group_id, role)
                VALUES (:user_id, :group_id, 'member')
                """
            ),
            {"user_id": user.id, "group_id": group_id},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        _push_flash(request, "error", "You are already a member of this group.")
        return _render_groups_page(request, db=db, user=user, join_form=join_form)

    _push_flash(request, "success", f"You joined {existing_group['name']}.")
    return RedirectResponse(url="/groups", status_code=303)


@router.post("/groups/{group_id}/members/{target_user_id}/remove", name="web_groups_remove_member")
def groups_remove_member(
    group_id: int,
    target_user_id: int,
    request: Request,
    month: str = Form(""),
    member_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    membership = _load_group_membership(db, user_id=user.id, group_id=group_id)
    redirect_member_id = int(member_id) if member_id.strip().isdigit() else None
    if redirect_member_id == target_user_id:
        redirect_member_id = None
    redirect_url = _group_detail_url(group_id, month=month.strip(), member_id=redirect_member_id)

    if membership is None:
        _push_flash(request, "error", "Group not found.")
        return RedirectResponse(url="/groups", status_code=303)
    if not membership["can_manage"]:
        _push_flash(request, "error", "Only owners and managers can remove people from this group.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if target_user_id == user.id:
        _push_flash(request, "error", "Use a separate leave-group flow later instead of removing yourself here.")
        return RedirectResponse(url=redirect_url, status_code=303)

    target_membership = db.execute(
        text(
            """
            SELECT gm.role, u.email
            FROM group_memberships gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = :group_id
              AND gm.user_id = :user_id
            """
        ),
        {"group_id": group_id, "user_id": target_user_id},
    ).mappings().one_or_none()

    if target_membership is None:
        _push_flash(request, "error", "Group member not found.")
        return RedirectResponse(url=redirect_url, status_code=303)

    if str(target_membership["role"]) == "owner":
        owner_count = int(
            db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM group_memberships
                    WHERE group_id = :group_id
                      AND role = 'owner'
                    """
                ),
                {"group_id": group_id},
            ).scalar_one()
        )
        if owner_count <= 1:
            _push_flash(request, "error", "The last owner cannot be removed from the group.")
            return RedirectResponse(url=redirect_url, status_code=303)

    db.execute(
        text(
            """
            DELETE FROM group_memberships
            WHERE group_id = :group_id
              AND user_id = :user_id
            """
        ),
        {"group_id": group_id, "user_id": target_user_id},
    )
    db.commit()

    _push_flash(request, "success", f"Removed {target_membership['email']} from the group.")
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/groups/{group_id}/members/{target_user_id}/role", name="web_groups_update_member_role")
def groups_update_member_role(
    group_id: int,
    target_user_id: int,
    request: Request,
    role: str = Form("member"),
    month: str = Form(""),
    member_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    membership = _load_group_membership(db, user_id=user.id, group_id=group_id)
    redirect_member_id = int(member_id) if member_id.strip().isdigit() else None
    redirect_url = _group_detail_url(group_id, month=month.strip(), member_id=redirect_member_id)
    normalized_role = _normalize_group_member_role(role)

    if membership is None:
        _push_flash(request, "error", "Group not found.")
        return RedirectResponse(url="/groups", status_code=303)
    if not membership["can_manage"]:
        _push_flash(request, "error", "Only owners and managers can change group roles.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if target_user_id == user.id:
        _push_flash(request, "error", "Change your own role later through a dedicated team-settings flow.")
        return RedirectResponse(url=redirect_url, status_code=303)

    target_membership = db.execute(
        text(
            """
            SELECT gm.role, u.email
            FROM group_memberships gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = :group_id
              AND gm.user_id = :user_id
            """
        ),
        {"group_id": group_id, "user_id": target_user_id},
    ).mappings().one_or_none()

    if target_membership is None:
        _push_flash(request, "error", "Group member not found.")
        return RedirectResponse(url=redirect_url, status_code=303)

    existing_role = str(target_membership["role"])
    if existing_role == "owner":
        _push_flash(request, "warning", "Owner roles stay fixed in this view.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if existing_role == normalized_role:
        _push_flash(request, "warning", f"{target_membership['email']} is already set as {_group_role_label(normalized_role).lower()}.")
        return RedirectResponse(url=redirect_url, status_code=303)

    db.execute(
        text(
            """
            UPDATE group_memberships
            SET role = :role
            WHERE group_id = :group_id
              AND user_id = :user_id
            """
        ),
        {"role": normalized_role, "group_id": group_id, "user_id": target_user_id},
    )
    db.commit()

    _push_flash(request, "success", f"{target_membership['email']} is now a {_group_role_label(normalized_role).lower()}.")
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/availability/add", name="web_availability_add")
def availability_add(
    request: Request,
    selected_cells: str = Form(""),
    day_of_week: list[str] = Form([]),
    start_time: str = Form(""),
    end_time: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    form_data = {
        "selected_cells": selected_cells.strip(),
        "selected_days": [value.strip() for value in day_of_week if value.strip()],
        "start_time": start_time.strip(),
        "end_time": end_time.strip(),
    }
    selected_cells_raw = selected_cells.strip()
    is_painted_submission = selected_cells_raw.startswith("[") and selected_cells_raw.endswith("]")
    next_path = _normalize_next_path(next, default="/availability")

    try:
        parsed_selected_cells = _parse_selected_cells(selected_cells) if is_painted_submission else []
        if is_painted_submission:
            grouped_windows: dict[int, list[int]] = {day_idx: [] for day_idx, _ in DAY_OPTIONS}
            for day_idx, minute_value in parsed_selected_cells:
                grouped_windows[day_idx].append(minute_value)

            selection_windows: list[tuple[int, time, time]] = []
            for day_idx, minute_values in grouped_windows.items():
                if not minute_values:
                    continue
                minute_values = sorted(set(minute_values))
                window_start = minute_values[0]
                window_previous = minute_values[0]
                for minute_value in minute_values[1:]:
                    if minute_value == window_previous + 15:
                        window_previous = minute_value
                        continue
                    selection_windows.append(
                        (
                            day_idx,
                            time(hour=window_start // 60, minute=window_start % 60),
                            time(hour=(window_previous + 15) // 60, minute=(window_previous + 15) % 60),
                        )
                    )
                    window_start = minute_value
                    window_previous = minute_value
                selection_windows.append(
                    (
                        day_idx,
                        time(hour=window_start // 60, minute=window_start % 60),
                        time(hour=(window_previous + 15) // 60, minute=(window_previous + 15) % 60),
                    )
                )
            day_values = [day_idx for day_idx, _start, _end in selection_windows]
            start_value = selection_windows[0][1] if selection_windows else None
            end_value = selection_windows[0][2] if selection_windows else None
        else:
            day_values = _parse_day_values(day_of_week)
            start_value = _parse_time_value(start_time)
            end_value = _parse_time_value(end_time)
    except Exception:
        _push_flash(request, "error", "Use valid day/start/end values.")
        if next_path == "/settings":
            return _render_settings_page(request, db=db, user=user, availability_form_data=form_data)
        return _render_availability_page(request, db=db, user=user, form_data=form_data)

    if not is_painted_submission and not day_values:
        _push_flash(request, "error", "Pick at least one day.")
        if next_path == "/settings":
            return _render_settings_page(request, db=db, user=user, availability_form_data=form_data)
        return _render_availability_page(request, db=db, user=user, form_data=form_data)

    if not is_painted_submission and end_value <= start_value:
        _push_flash(request, "error", "End time must be after start time.")
        if next_path == "/settings":
            return _render_settings_page(request, db=db, user=user, availability_form_data=form_data)
        return _render_availability_page(request, db=db, user=user, form_data=form_data)

    inserted_days: list[str] = []
    overlapping_days: list[str] = []
    if is_painted_submission:
        db.execute(
            text(
                """
                DELETE FROM time_slot_preferences
                WHERE user_id = :user_id
                """
            ),
            {"user_id": user.id},
        )

        for day_value, start_value, end_value in selection_windows:
            db.execute(
                text(
                    """
                    INSERT INTO time_slot_preferences (user_id, day_of_week, start_time, end_time)
                    VALUES (:user_id, :day_of_week, :start_time, :end_time)
                    """
                ),
                {
                    "user_id": user.id,
                    "day_of_week": day_value,
                    "start_time": start_value,
                    "end_time": end_value,
                },
            )
            inserted_days.append(DAY_SHORT_NAME_BY_INDEX[day_value])
    else:
        for day_value in day_values:
            overlaps_existing = bool(
                db.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM time_slot_preferences
                            WHERE user_id = :user_id
                              AND day_of_week = :day_of_week
                              AND start_time < :end_time
                              AND end_time > :start_time
                        )
                        """
                    ),
                    {
                        "user_id": user.id,
                        "day_of_week": day_value,
                        "start_time": start_value,
                        "end_time": end_value,
                    },
                ).scalar_one()
            )
            if overlaps_existing:
                overlapping_days.append(DAY_SHORT_NAME_BY_INDEX[day_value])
                continue

            db.execute(
                text(
                    """
                    INSERT INTO time_slot_preferences (user_id, day_of_week, start_time, end_time)
                    VALUES (:user_id, :day_of_week, :start_time, :end_time)
                    """
                ),
                {
                    "user_id": user.id,
                    "day_of_week": day_value,
                    "start_time": start_value,
                    "end_time": end_value,
                },
            )
            inserted_days.append(DAY_SHORT_NAME_BY_INDEX[day_value])

    db.commit()

    if is_painted_submission:
        return RedirectResponse(url=next_path, status_code=303)

    if not inserted_days:
        if next_path == "/settings":
            return _render_settings_page(request, db=db, user=user, availability_form_data=form_data)
        return _render_availability_page(request, db=db, user=user, form_data=form_data)

    return RedirectResponse(url=next_path, status_code=303)


@router.post("/availability/delete", name="web_availability_delete")
def availability_delete(
    request: Request,
    preference_id: int = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    next_path = _normalize_next_path(next, default="/availability")

    deleted = db.execute(
        text(
            """
            DELETE FROM time_slot_preferences
            WHERE id = :preference_id
              AND user_id = :user_id
            """
        ),
        {"preference_id": preference_id, "user_id": user.id},
    ).rowcount
    db.commit()

    if deleted:
        _push_flash(request, "success", "Availability preference removed.")
    else:
        _push_flash(request, "error", "Preference not found.")
    return RedirectResponse(url=next_path, status_code=303)


@router.get("/meetings", name="web_meetings")
def meetings(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    status: str = "",
    mine: str = "",
    day: str = "",
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    return _render_meetings_page(
        request,
        db=db,
        user=user,
        q=q.strip(),
        status=status.strip().lower(),
        mine=mine.strip() == "1",
        selected_day=day.strip(),
    )


@router.get("/meetings/overview", name="web_meetings_overview")
def meetings_overview(
    request: Request,
    db: Session = Depends(get_db),
    scope: str = "",
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)
    return _render_meetings_overview_page(request, db=db, user=user, scope=scope)


@router.post("/meetings/availability", name="web_meetings_availability")
def meetings_availability(
    request: Request,
    title: str = Form(""),
    meeting_type: str = Form("in_person"),
    location: str = Form(""),
    location_raw: str = Form(""),
    location_latitude: str = Form(""),
    location_longitude: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    invitees: str = Form(""),
    recommendation_window_start: str = Form(""),
    recommendation_window_end: str = Form(""),
    recommendation_duration_minutes: str = Form("60"),
    recommendation_slot_interval_minutes: str = Form("30"),
    recommendation_max_results: str = Form("5"),
    q: str = Form(""),
    status: str = Form(""),
    mine: str = Form(""),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    q_norm = q.strip()
    status_norm = status.strip().lower()
    mine_enabled = mine.strip() == "1"
    selected_day = day.strip()
    create_form = {
        "title": title.strip(),
        "meeting_type": _normalize_meeting_type(meeting_type),
        **_build_location_form_state(
            location=location,
            location_raw=location_raw,
            location_latitude=location_latitude,
            location_longitude=location_longitude,
        ),
        "start_time": start_time.strip(),
        "end_time": end_time.strip(),
        "invitees": invitees.strip(),
    }
    recommendation_form = {
        "window_start": recommendation_window_start.strip() or start_time.strip(),
        "window_end": recommendation_window_end.strip() or end_time.strip(),
        "duration_minutes": recommendation_duration_minutes.strip() or "60",
        "slot_interval_minutes": recommendation_slot_interval_minutes.strip() or "30",
        "max_results": recommendation_max_results.strip() or "5",
    }

    try:
        start_dt = _parse_datetime_local(start_time)
        end_dt = _parse_datetime_local(end_time)
    except Exception:
        _push_flash(request, "error", "Use valid start/end date-time values to check availability.")
        return _render_meetings_page(
            request,
            db=db,
            user=user,
            q=q_norm,
            status=status_norm,
            mine=mine_enabled,
            selected_day=selected_day,
            create_form=create_form,
            recommendation_form=recommendation_form,
        )

    if end_dt <= start_dt:
        _push_flash(request, "error", "End time must be after start time.")
        return _render_meetings_page(
            request,
            db=db,
            user=user,
            q=q_norm,
            status=status_norm,
            mine=mine_enabled,
            selected_day=selected_day,
            create_form=create_form,
            recommendation_form=recommendation_form,
        )

    emails, invalid_emails = _parse_invitee_emails(invitees)
    if invalid_emails:
        _push_flash(request, "error", f"Ignored invalid emails: {', '.join(invalid_emails)}")
    if not emails:
        _push_flash(request, "error", "Add at least one invitee email.")
        return _render_meetings_page(
            request,
            db=db,
            user=user,
            q=q_norm,
            status=status_norm,
            mine=mine_enabled,
            selected_day=selected_day,
            create_form=create_form,
            recommendation_form=recommendation_form,
        )

    preview = _build_availability_preview(db, emails=emails, slot_start=start_dt, slot_end=end_dt)

    recommendations: list[dict[str, Any]] = []
    unresolved_user_ids: list[int] = []
    unresolved_emails: list[str] = []
    try:
        recommendation_payload = MeetingRecommendationRequest(
            attendee_emails=emails,
            window_start=_parse_datetime_local(recommendation_form["window_start"]),
            window_end=_parse_datetime_local(recommendation_form["window_end"]),
            duration_minutes=int(recommendation_form["duration_minutes"]),
            slot_interval_minutes=int(recommendation_form["slot_interval_minutes"]),
            max_results=int(recommendation_form["max_results"]),
            include_current_user=True,
        )
        recommendation_response = generate_meeting_time_recommendations(
            payload=recommendation_payload,
            db=db,
            current_user=user,
        )
        recommendations = [rec.model_dump(mode="python") for rec in recommendation_response.recommendations]
        unresolved_user_ids = recommendation_response.unresolved_user_ids
        unresolved_emails = recommendation_response.unresolved_emails
        _push_flash(request, "success", "Availability preview and recommendations updated.")
    except HTTPException as exc:
        _push_flash(request, "error", f"Recommendations unavailable: {exc.detail}")
    except Exception:
        _push_flash(request, "error", "Recommendations unavailable due to invalid recommendation settings.")

    return _render_meetings_page(
        request,
        db=db,
        user=user,
        q=q_norm,
        status=status_norm,
        mine=mine_enabled,
        selected_day=selected_day,
        create_form=create_form,
        availability_preview=preview,
        recommendation_form=recommendation_form,
        meeting_recommendations=recommendations,
        unresolved_recommendation_emails=unresolved_emails,
        unresolved_recommendation_user_ids=unresolved_user_ids,
    )


@router.post("/meetings/create", name="web_meetings_create")
def meetings_create(
    request: Request,
    title: str = Form(""),
    meeting_type: str = Form("in_person"),
    location: str = Form(""),
    location_raw: str = Form(""),
    location_latitude: str = Form(""),
    location_longitude: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    invitees: str = Form(""),
    q: str = Form(""),
    status: str = Form(""),
    mine: str = Form(""),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    q_norm = q.strip()
    status_norm = status.strip().lower()
    mine_enabled = mine.strip() == "1"
    selected_day = day.strip()
    normalized_meeting_type = _normalize_meeting_type(meeting_type)
    create_form = {
        "title": title.strip(),
        "meeting_type": normalized_meeting_type,
        **_build_location_form_state(
            location=location,
            location_raw=location_raw,
            location_latitude=location_latitude,
            location_longitude=location_longitude,
        ),
        "start_time": start_time.strip(),
        "end_time": end_time.strip(),
        "invitees": invitees.strip(),
    }

    if not title.strip():
        _push_flash(request, "error", "Meeting title is required.")
        return _render_meetings_page(
            request,
            db=db,
            user=user,
            q=q_norm,
            status=status_norm,
            mine=mine_enabled,
            selected_day=selected_day,
            create_form=create_form,
        )

    try:
        start_dt = _parse_datetime_local(start_time)
        end_dt = _parse_datetime_local(end_time)
    except Exception:
        _push_flash(request, "error", "Use valid start/end date-time values.")
        return _render_meetings_page(
            request,
            db=db,
            user=user,
            q=q_norm,
            status=status_norm,
            mine=mine_enabled,
            selected_day=selected_day,
            create_form=create_form,
        )

    if end_dt <= start_dt:
        _push_flash(request, "error", "End time must be after start time.")
        return _render_meetings_page(
            request,
            db=db,
            user=user,
            q=q_norm,
            status=status_norm,
            mine=mine_enabled,
            selected_day=selected_day,
            create_form=create_form,
        )

    emails, invalid_emails = _parse_invitee_emails(invitees)
    if invalid_emails:
        _push_flash(request, "error", f"Ignored invalid emails: {', '.join(invalid_emails)}")

    resolved_location = _resolve_submitted_location(
        location=location,
        location_raw=location_raw,
        location_latitude=location_latitude,
        location_longitude=location_longitude,
    )
    calendar_id = _get_or_create_personal_calendar(db, user)
    meeting_id = int(
        db.execute(
            text(
                """
                INSERT INTO meetings (
                    calendar_id,
                    title,
                    created_by,
                    meeting_type,
                    location,
                    location_raw,
                    location_latitude,
                    location_longitude,
                    start_time,
                    end_time,
                    capacity,
                    setup_minutes,
                    cleanup_minutes
                )
                VALUES (
                    :calendar_id,
                    :title,
                    :created_by,
                    :meeting_type,
                    :location,
                    :location_raw,
                    :location_latitude,
                    :location_longitude,
                    :start_time,
                    :end_time,
                    NULL,
                    0,
                    0
                )
                RETURNING id
                """
            ),
            {
                "calendar_id": calendar_id,
                "title": title.strip(),
                "created_by": user.id,
                "meeting_type": normalized_meeting_type,
                "location": resolved_location["location"],
                "location_raw": resolved_location["location_raw"],
                "location_latitude": resolved_location["location_latitude"],
                "location_longitude": resolved_location["location_longitude"],
                "start_time": start_dt,
                "end_time": end_dt,
            },
        ).scalar_one()
    )

    db.execute(
        text(
            """
            INSERT INTO meeting_attendees (meeting_id, user_id, status)
            VALUES (:meeting_id, :user_id, 'accepted')
            ON CONFLICT (meeting_id, user_id) DO NOTHING
            """
        ),
        {"meeting_id": meeting_id, "user_id": user.id},
    )

    invited_count = 0
    missing_users: list[str] = []
    added_user_ids: list[int] = []
    for email in emails:
        invitee = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if invitee is None:
            missing_users.append(email)
            continue

        status_value = "accepted" if invitee.id == user.id else "invited"
        db.execute(
            text(
                """
                INSERT INTO meeting_attendees (meeting_id, user_id, status)
                VALUES (:meeting_id, :user_id, :status)
                ON CONFLICT (meeting_id, user_id) DO NOTHING
                """
            ),
            {"meeting_id": meeting_id, "user_id": invitee.id, "status": status_value},
        )
        if invitee.id != user.id:
            invited_count += 1
            added_user_ids.append(invitee.id)

    travel_warnings = get_travel_warning_service().evaluate_meeting(
        db,
        user=user,
        meeting={
            "id": meeting_id,
            "title": title.strip(),
            "start_time": start_dt,
            "end_time": end_dt,
            "location": resolved_location["location"],
            "location_latitude": resolved_location["location_latitude"],
            "location_longitude": resolved_location["location_longitude"],
            "is_relevant_to_user": True,
        },
        persist=True,
    )
    db.commit()
    if added_user_ids:
        notify_meeting_invite(meeting_id, db, attendee_user_ids=added_user_ids)

    summary = f"Meeting created. Invited {invited_count} user(s)."
    if missing_users:
        summary += f" Not found: {', '.join(missing_users)}."
    _push_flash(request, "success", summary)
    first_actionable_warning = next(
        (warning for warning in travel_warnings if warning.severity in {"critical", "caution"}),
        None,
    )
    if first_actionable_warning is not None:
        _push_flash(
            request,
            "error" if first_actionable_warning.severity == "critical" else "warning",
            _format_travel_warning_flash(first_actionable_warning.model_dump(mode="python")),
        )
    return RedirectResponse(url=f"/meetings/{meeting_id}", status_code=303)


@router.post("/meetings/overview/invitees", name="web_meetings_overview_invitees")
def meetings_overview_invitees(
    request: Request,
    meeting_id: int = Form(...),
    invitees: str = Form(""),
    scope: str = Form("mine"),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    redirect_url = _meetings_overview_redirect_url(scope)
    meeting_context = _load_meeting_action_context(db, meeting_id=meeting_id)
    if meeting_context is None:
        _push_flash(request, "error", "Meeting not found.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if meeting_context["status"] == "cancelled":
        _push_flash(request, "error", "Cancelled meetings cannot be updated.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if not _user_can_add_people_to_meeting(
        db,
        meeting_id=meeting_id,
        user=user,
        meeting_context=meeting_context,
    ):
        _push_flash(request, "error", "You do not have permission to add people to this meeting.")
        return RedirectResponse(url=redirect_url, status_code=303)

    emails, invalid_emails = _parse_invitee_emails(invitees)
    if not emails:
        _push_flash(request, "error", "Add at least one valid email to invite.")
        return RedirectResponse(url=redirect_url, status_code=303)

    existing_attendee_ids = {
        int(row["user_id"])
        for row in db.execute(
            text(
                """
                SELECT user_id
                FROM meeting_attendees
                WHERE meeting_id = :meeting_id
                """
            ),
            {"meeting_id": meeting_id},
        ).mappings().all()
    }
    organizer_user_id = _meeting_organizer_user_id(meeting_context)
    missing_users: list[str] = []
    already_present: list[str] = []
    added_user_ids: list[int] = []

    for email in emails:
        invitee = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if invitee is None:
            missing_users.append(email)
            continue
        if invitee.id in existing_attendee_ids:
            already_present.append(email)
            continue

        attendee_status = "accepted" if organizer_user_id is not None and invitee.id == organizer_user_id else "invited"
        db.execute(
            text(
                """
                INSERT INTO meeting_attendees (meeting_id, user_id, status)
                VALUES (:meeting_id, :user_id, :status)
                """
            ),
            {"meeting_id": meeting_id, "user_id": invitee.id, "status": attendee_status},
        )
        existing_attendee_ids.add(invitee.id)
        if attendee_status != "accepted":
            added_user_ids.append(invitee.id)

    db.commit()

    if added_user_ids:
        notify_meeting_invite(meeting_id, db, attendee_user_ids=added_user_ids)
        _push_flash(request, "success", f"Added {len(added_user_ids)} attendee(s) to {meeting_context['title']}.")
    elif not missing_users and not already_present and not invalid_emails:
        _push_flash(request, "warning", "No new attendees were added.")

    if already_present:
        _push_flash(request, "warning", f"Already on the meeting: {', '.join(already_present)}.")
    if missing_users:
        _push_flash(request, "warning", f"Not registered yet: {', '.join(missing_users)}.")
    if invalid_emails:
        _push_flash(request, "warning", f"Ignored invalid emails: {', '.join(invalid_emails)}.")

    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/meetings/overview/reschedule", name="web_meetings_overview_reschedule")
def meetings_overview_reschedule(
    request: Request,
    meeting_id: int = Form(...),
    start_time: str = Form(""),
    end_time: str = Form(""),
    scope: str = Form("mine"),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    redirect_url = _meetings_overview_redirect_url(scope)
    meeting_context = _load_meeting_action_context(db, meeting_id=meeting_id)
    if meeting_context is None:
        _push_flash(request, "error", "Meeting not found.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if not _user_can_manage_overview_meeting(
        db,
        meeting_id=meeting_id,
        user=user,
        meeting_context=meeting_context,
    ):
        _push_flash(request, "error", "You do not have permission to reschedule this meeting.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if meeting_context["status"] == "cancelled":
        _push_flash(request, "error", "Cancelled meetings cannot be rescheduled.")
        return RedirectResponse(url=redirect_url, status_code=303)

    try:
        payload = MeetingUpdate(
            start_time=_parse_datetime_local(start_time),
            end_time=_parse_datetime_local(end_time),
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Use valid meeting times."
        _push_flash(request, "error", str(first_error))
        return RedirectResponse(url=redirect_url, status_code=303)
    except Exception:
        _push_flash(request, "error", "Use valid start and end times.")
        return RedirectResponse(url=redirect_url, status_code=303)

    db.execute(
        text(
            """
            UPDATE meetings
            SET start_time = :start_time,
                end_time = :end_time
            WHERE id = :meeting_id
            """
        ),
        {
            "meeting_id": meeting_id,
            "start_time": payload.start_time,
            "end_time": payload.end_time,
        },
    )

    organizer_user_id = _meeting_organizer_user_id(meeting_context)
    if organizer_user_id is None:
        db.execute(
            text(
                """
                UPDATE meeting_attendees
                SET status = 'invited'
                WHERE meeting_id = :meeting_id
                """
            ),
            {"meeting_id": meeting_id},
        )
    else:
        db.execute(
            text(
                """
                UPDATE meeting_attendees
                SET status = 'invited'
                WHERE meeting_id = :meeting_id
                  AND user_id <> :organizer_user_id
                """
            ),
            {"meeting_id": meeting_id, "organizer_user_id": organizer_user_id},
        )

    db.commit()
    notify_meeting_updated(meeting_id, db)
    _push_flash(request, "success", "Meeting rescheduled. Attendees have been asked to reconfirm.")
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/meetings/overview/cancel", name="web_meetings_overview_cancel")
def meetings_overview_cancel(
    request: Request,
    meeting_id: int = Form(...),
    scope: str = Form("mine"),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    redirect_url = _meetings_overview_redirect_url(scope)
    meeting_context = _load_meeting_action_context(db, meeting_id=meeting_id)
    if meeting_context is None:
        _push_flash(request, "error", "Meeting not found.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if not _user_can_manage_overview_meeting(
        db,
        meeting_id=meeting_id,
        user=user,
        meeting_context=meeting_context,
    ):
        _push_flash(request, "error", "You do not have permission to cancel this meeting.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if meeting_context["status"] == "cancelled":
        _push_flash(request, "warning", "This meeting is already cancelled.")
        return RedirectResponse(url=redirect_url, status_code=303)

    db.execute(
        text(
            """
            UPDATE meetings
            SET status = 'cancelled'
            WHERE id = :meeting_id
            """
        ),
        {"meeting_id": meeting_id},
    )
    db.commit()
    notify_meeting_cancelled(meeting_id, db)
    _push_flash(request, "success", "Meeting cancelled.")
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("/meetings/{meeting_id}", name="web_meeting_detail")
def meeting_detail(meeting_id: int, request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    row = db.execute(
        text(
            """
            SELECT
                m.id,
                m.title,
                COALESCE(u.email, 'group-calendar') AS organizer_email,
                m.start_time,
                m.end_time,
                m.location,
                CASE
                    WHEN m.end_time < NOW() THEN 'completed'
                    ELSE 'scheduled'
                END AS status
            FROM meetings m
            JOIN calendars c ON c.id = m.calendar_id
            LEFT JOIN users u ON c.owner_type = 'user' AND c.owner_id = u.id
            WHERE m.id = :meeting_id
              AND (
                  m.created_by = :current_user_id
                  OR (c.owner_type = 'user' AND c.owner_id = :current_user_id)
                  OR EXISTS (
                      SELECT 1
                      FROM meeting_attendees ma_access
                      WHERE ma_access.meeting_id = m.id
                        AND ma_access.user_id = :current_user_id
                  )
              )
            """
        ),
        {"meeting_id": meeting_id, "current_user_id": user.id},
    ).mappings().one_or_none()

    if row is None:
        _push_flash(request, "error", "Meeting not found.")
        return RedirectResponse(url="/meetings", status_code=303)

    start_dt = row["start_time"]
    end_dt = row["end_time"]
    if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
        _push_flash(request, "error", "Meeting date data is invalid.")
        return RedirectResponse(url="/meetings", status_code=303)

    attendees_raw = db.execute(
        text(
            """
            SELECT u.id, u.email, ma.status
            FROM meeting_attendees ma
            JOIN users u ON u.id = ma.user_id
            WHERE ma.meeting_id = :meeting_id
            ORDER BY u.email
            """
        ),
        {"meeting_id": meeting_id},
    ).mappings()

    attendees: list[dict[str, Any]] = []
    for attendee in attendees_raw:
        availability_status, conflicts = _availability_summary(
            db,
            user_id=int(attendee["id"]),
            slot_start=start_dt,
            slot_end=end_dt,
            exclude_meeting_id=meeting_id,
        )
        attendees.append(
            {
                "email": attendee["email"],
                "invite_status": attendee["status"],
                "availability_status": availability_status,
                "conflicts": conflicts,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="meeting_detail.html",
        context={
            **_app_shell_context(user, active_page="meetings_overview"),
            "meeting": row,
            "attendees": attendees,
            "messages": _pop_flashes(request),
        },
    )
@router.post("/meetings/overview/rsvp", name="web_meetings_overview_rsvp")
def meetings_overview_rsvp(
    request: Request,
    meeting_id: int = Form(...),
    status: str = Form(...),
    scope: str = Form("mine"),
    db: Session = Depends(get_db),
):
    user = _current_user(request, db)
    if user is None:
        _push_flash(request, "error", "Please sign in first.")
        return RedirectResponse(url="/", status_code=303)

    valid_statuses = {"accepted", "declined", "maybe"}
    if status not in valid_statuses:
        _push_flash(request, "error", "Invalid RSVP status.")
        return RedirectResponse(url=_meetings_overview_redirect_url(scope), status_code=303)

    result = db.execute(
        text("""
            UPDATE meeting_attendees
            SET status = :status
            WHERE meeting_id = :meeting_id AND user_id = :user_id
        """),
        {"status": status, "meeting_id": meeting_id, "user_id": user.id},
    )
    db.commit()

    if result.rowcount == 0:
        _push_flash(request, "error", "You are not an attendee of this meeting.")
    else:
        _push_flash(request, "success", f"RSVP updated to {status}.")

    return RedirectResponse(url=_meetings_overview_redirect_url(scope), status_code=303)

@router.post("/logout", name="web_logout")
def logout(request: Request):
    request.session.clear()
    _push_flash(request, "success", "Signed out.")
    return RedirectResponse(url="/", status_code=303)


@router.get("/web/auth/google", name="web_auth_google")
def auth_google(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        _push_flash(request, "error", "Google login is not configured yet.")
        return RedirectResponse(url="/", status_code=303)

    state = secrets.token_urlsafe(32)
    request.session[GOOGLE_OAUTH_STATE_SESSION_KEY] = state
    return RedirectResponse(
        url=_google_authorization_url(state=state, redirect_uri=_google_redirect_uri(request)),
        status_code=303,
    )


@router.get("/web/auth/google/callback", name="web_auth_google_callback")
def auth_google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    expected_state = request.session.pop(GOOGLE_OAUTH_STATE_SESSION_KEY, None)
    if error:
        _push_flash(request, "error", "Google sign-in was cancelled.")
        return RedirectResponse(url="/", status_code=303)

    if not code or not state or not isinstance(expected_state, str) or not hmac.compare_digest(expected_state, state):
        _push_flash(request, "error", "Google sign-in could not be verified. Please try again.")
        return RedirectResponse(url="/", status_code=303)

    try:
        user = authenticate_google_code(
            db,
            code=code,
            code_verifier=None,
            redirect_uri=_google_redirect_uri(request),
        )
    except HTTPException as exc:
        _push_flash(request, "error", str(exc.detail))
        return RedirectResponse(url="/", status_code=303)

    request.session["user_id"] = user.id
    _push_flash(request, "success", f"Signed in as {user.email}")
    return RedirectResponse(url="/meetings", status_code=303)


@router.get("/web/auth/microsoft", name="web_auth_microsoft")
def auth_microsoft(request: Request):
    _push_flash(request, "error", "Microsoft OAuth flow is not wired yet.")
    return RedirectResponse(url="/", status_code=303)
