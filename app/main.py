import logging
import time
import uuid

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.api.assistant import router as assistant_router
from app.api.auth import router as auth_router
from app.api.availability import router as availability_router
from app.api.calendar import router as calendar_router
from app.api.deps import get_current_user, get_db
from app.api.meetings import router as meetings_router
from app.api.notifications import router as notifications_router
from app.api.recommendations import router as recommendations_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.bootstrap import ensure_runtime_schema
from app.models import User
from app.schemas.groups import (
    CreateGroupRequest,
    GroupAvailabilityResponse,
    GroupMemberResponse,
    GroupResponse,
    JoinGroupRequest,
)
from app.services.notifications import start_notification_scheduler, stop_notification_scheduler
from app.web.routes import router as web_router


groups_router = APIRouter(prefix="/groups", tags=["groups"])
api_groups_router = APIRouter(prefix="/api/groups", tags=["groups"])


configure_logging(settings.log_level)
logger = logging.getLogger("app")


def _allowed_origins() -> list[str]:
    origins: set[str] = set()
    if not settings.is_production:
        origins.update(
            {
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "http://localhost:5174",
                "http://127.0.0.1:5174",
            }
        )
    if settings.frontend_origin:
        origins.add(settings.frontend_origin)
    return sorted(origins)


def create_app() -> FastAPI:
    settings.validate_runtime()
    api = FastAPI(title="AI Agents API")

    @api.on_event("startup")
    def bootstrap_runtime_schema() -> None:
        ensure_runtime_schema()

    @api.on_event("startup")
    async def startup_notification_scheduler() -> None:
        start_notification_scheduler(api)

    @api.on_event("shutdown")
    async def shutdown_notification_scheduler() -> None:
        await stop_notification_scheduler(api)

    @api.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0

        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s -> %s %.1fms request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response

    @api.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
        logger.exception(
            "Unhandled error on %s %s request_id=%s",
            request.method,
            request.url.path,
            request_id,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    api.add_middleware(
        SessionMiddleware,
        secret_key=settings.jwt_secret,
        same_site=settings.cookie_samesite,
        https_only=settings.cookie_secure,
        domain=settings.cookie_domain,
    )

    api.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api.mount("/static", StaticFiles(directory="app/static"), name="static")
    api.include_router(web_router)
    api.include_router(assistant_router)
    api.include_router(auth_router)
    api.include_router(recommendations_router)
    api.include_router(api_groups_router)
    api.include_router(groups_router)
    api.include_router(availability_router)
    api.include_router(calendar_router)
    api.include_router(meetings_router)
    api.include_router(notifications_router)
    return api


@api_groups_router.get("/")
@groups_router.get("/")
def get_user_groups(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Fetch all groups the current logged-in user belongs to."""

    query = text(
        """
        SELECT g.id, g.name, g.description, gm.role
        FROM groups g
        JOIN group_memberships gm ON g.id = gm.group_id
        WHERE gm.user_id = :user_id
        """
    )

    result = db.execute(query, {"user_id": current_user.id}).mappings().all()
    return [dict(row) for row in result]


def _require_group_membership(db: Session, *, user_id: int, group_id: int) -> str:
    membership = db.execute(
        text(
            """
            SELECT role
            FROM group_memberships
            WHERE user_id = :user_id AND group_id = :group_id
            """
        ),
        {"user_id": user_id, "group_id": group_id},
    ).mappings().one_or_none()

    if membership is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return str(membership["role"])


@api_groups_router.get("/{group_id}", response_model=GroupResponse)
def get_group_detail(
    group_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = _require_group_membership(db, user_id=current_user.id, group_id=group_id)

    group = db.execute(
        text("SELECT id, name, description FROM groups WHERE id = :group_id"),
        {"group_id": group_id},
    ).mappings().one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    return GroupResponse(
        id=group["id"],
        name=group["name"],
        description=group["description"],
        role=role,
    )


@api_groups_router.get("/{group_id}/members", response_model=list[GroupMemberResponse])
def get_group_members(
    group_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_group_membership(db, user_id=current_user.id, group_id=group_id)

    rows = db.execute(
        text(
            """
            SELECT
                u.id,
                u.email,
                u.first_name,
                u.last_name,
                gm.role
            FROM group_memberships gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = :group_id
            ORDER BY
                CASE gm.role
                    WHEN 'owner' THEN 0
                    WHEN 'admin' THEN 1
                    ELSE 2
                END,
                u.email ASC
            """
        ),
        {"group_id": group_id},
    ).mappings().all()

    return [GroupMemberResponse(**dict(row)) for row in rows]


@api_groups_router.get("/{group_id}/availability", response_model=list[GroupAvailabilityResponse])
def get_group_member_availability(
    group_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_group_membership(db, user_id=current_user.id, group_id=group_id)

    rows = db.execute(
        text(
            """
            SELECT
                tsp.id,
                tsp.user_id,
                u.email,
                u.first_name,
                u.last_name,
                tsp.day_of_week,
                tsp.start_time::text AS start_time,
                tsp.end_time::text AS end_time
            FROM time_slot_preferences tsp
            JOIN group_memberships gm
              ON gm.user_id = tsp.user_id
             AND gm.group_id = :group_id
            JOIN users u ON u.id = tsp.user_id
            ORDER BY tsp.day_of_week ASC, tsp.start_time ASC, u.email ASC
            """
        ),
        {"group_id": group_id},
    ).mappings().all()

    return [GroupAvailabilityResponse(**dict(row)) for row in rows]


def _group_id_from_invite_code(invite_code: str) -> int | None:
    token = invite_code.strip()
    if not token:
        return None

    # Minimal invite format support backed by current schema:
    # numeric code ("12") or prefixed code ("GRP-12").
    token_upper = token.upper()
    if token_upper.startswith("GRP-"):
        token = token[4:]

    if token.isdigit():
        return int(token)

    return None


@api_groups_router.post("/", response_model=GroupResponse)
@groups_router.post("/", response_model=GroupResponse)
def create_group(
    payload: CreateGroupRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    group_name = payload.name.strip()
    if not group_name:
        raise HTTPException(status_code=422, detail="Group name cannot be empty")

    description = payload.description.strip() if payload.description else None
    if description == "":
        description = None

    group_row = db.execute(
        text(
            """
            INSERT INTO groups (name, description)
            VALUES (:name, :description)
            RETURNING id, name, description
            """
        ),
        {"name": group_name, "description": description},
    ).mappings().one()

    db.execute(
        text(
            """
            INSERT INTO group_memberships (user_id, group_id, role)
            VALUES (:user_id, :group_id, 'owner')
            """
        ),
        {"user_id": current_user.id, "group_id": group_row["id"]},
    )
    db.commit()

    return GroupResponse(
        id=group_row["id"],
        name=group_row["name"],
        description=group_row["description"],
        role="owner",
    )


@api_groups_router.post("/join", response_model=GroupResponse)
@groups_router.post("/join", response_model=GroupResponse)
def join_group(
    payload: JoinGroupRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    group_id = payload.groupId
    if group_id is None and payload.inviteCode:
        group_id = _group_id_from_invite_code(payload.inviteCode)

    if group_id is None:
        raise HTTPException(status_code=422, detail="Provide a valid groupId or inviteCode")

    existing_group = db.execute(
        text("SELECT id, name, description FROM groups WHERE id = :group_id"),
        {"group_id": group_id},
    ).mappings().one_or_none()

    if existing_group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    try:
        db.execute(
            text(
                """
                INSERT INTO group_memberships (user_id, group_id, role)
                VALUES (:user_id, :group_id, 'member')
                """
            ),
            {"user_id": current_user.id, "group_id": group_id},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="You are already a member of this group")

    return GroupResponse(
        id=existing_group["id"],
        name=existing_group["name"],
        description=existing_group["description"],
        role="member",
    )


app = create_app()


@app.get("/healthz", tags=["health"])
def healthcheck():
    return {"status": "ok"}
