from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.avatar import normalize_avatar_color_id
from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models import AuthIdentity, PasswordCredential, RefreshToken, User
from app.schemas.auth import (
    GoogleExchangeRequest,
    LoginRequest,
    MeResponse,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
)


router = APIRouter(prefix="/auth", tags=["auth"])


def create_password_user_account(db: Session, *, payload: RegisterRequest) -> User:
    email_norm = payload.email.strip().lower()

    user = User(
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        email=email_norm,
        phone=payload.phone.strip() if payload.phone else None,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already in use")

    db.add(
        PasswordCredential(
            user_id=user.id,
            password_hash=hash_password(payload.password),
        )
    )
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider="password",
            provider_subject=email_norm,
            email=email_norm,
            email_verified=False,
        )
    )
    db.commit()
    db.refresh(user)
    return user


def _set_refresh_cookie(resp: Response, refresh_token: str) -> None:
    resp.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/auth",
        max_age=int(timedelta(days=settings.refresh_token_expire_days).total_seconds()),
    )


def _clear_refresh_cookie(resp: Response) -> None:
    resp.delete_cookie(
        key="refresh_token",
        domain=settings.cookie_domain,
        path="/auth",
    )


def _issue_tokens(db: Session, *, user: User, request: Request, response: Response) -> TokenResponse:
    access = create_access_token(user_id=user.id)

    refresh_plain = generate_refresh_token()
    refresh_hash = hash_refresh_token(refresh_plain)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=settings.refresh_token_expire_days)
    rt = RefreshToken(
        user_id=user.id,
        token_hash=refresh_hash,
        expires_at=expires,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    db.add(rt)
    db.commit()
    _set_refresh_cookie(response, refresh_plain)
    return TokenResponse(access_token=access)


@router.post("/register", response_model=TokenResponse)
def register(payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user = create_password_user_account(db, payload=payload)
    return _issue_tokens(db, user=user, request=request, response=response)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    email_norm = payload.email.strip().lower()

    user = db.execute(select(User).where(User.email == email_norm)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    cred = db.get(PasswordCredential, user.id)
    if cred is None or not verify_password(payload.password, cred.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return _issue_tokens(db, user=user, request=request, response=response)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    raw = request.cookies.get("refresh_token")
    if not raw:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    token_hash = hash_refresh_token(raw)
    rt = db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).scalar_one_or_none()
    if rt is None or rt.revoked_at is not None:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    now = datetime.now(timezone.utc)
    if rt.expires_at <= now:
        rt.revoked_at = now
        db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Refresh token expired")

    user = db.get(User, rt.user_id)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="User not found")

    # Rotate refresh token
    new_plain = generate_refresh_token()
    new_hash = hash_refresh_token(new_plain)
    rt.revoked_at = now
    rt.replaced_by_token_hash = new_hash
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=now + timedelta(days=settings.refresh_token_expire_days),
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()

    _set_refresh_cookie(response, new_plain)
    return TokenResponse(access_token=create_access_token(user_id=user.id))


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    raw = request.cookies.get("refresh_token")
    if raw:
        token_hash = hash_refresh_token(raw)
        rt = db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).scalar_one_or_none()
        if rt is not None and rt.revoked_at is None:
            rt.revoked_at = datetime.now(timezone.utc)
            db.commit()
    _clear_refresh_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)):
    return MeResponse(
        id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        phone=user.phone,
        avatar_color=normalize_avatar_color_id(user.avatar_color),
    )


@router.patch("/me", response_model=MeResponse)
def update_me(
    payload: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.new_password:
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="Current password is required to set a new password")
        cred = db.get(PasswordCredential, current_user.id)
        if cred is None or not verify_password(payload.current_password, cred.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        cred.password_hash = hash_password(payload.new_password)

    if payload.first_name is not None:
        first_name = payload.first_name.strip()
        if not first_name:
            raise HTTPException(status_code=422, detail="First name cannot be empty")
        current_user.first_name = first_name

    if payload.last_name is not None:
        last_name = payload.last_name.strip()
        if not last_name:
            raise HTTPException(status_code=422, detail="Last name cannot be empty")
        current_user.last_name = last_name

    if payload.email is not None:
        email_norm = payload.email.strip().lower()
        if not email_norm:
            raise HTTPException(status_code=422, detail="Email cannot be empty")
        if email_norm != current_user.email:
            existing = db.execute(select(User).where(User.email == email_norm)).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(status_code=409, detail="Email already in use")
            current_user.email = email_norm

    if payload.avatar_color is not None:
        current_user.avatar_color = normalize_avatar_color_id(payload.avatar_color)

    db.commit()
    db.refresh(current_user)
    return MeResponse(
        id=current_user.id,
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        email=current_user.email,
        phone=current_user.phone,
        avatar_color=normalize_avatar_color_id(current_user.avatar_color),
    )


def _google_exchange_code(*, code: str, code_verifier: str | None, redirect_uri: str) -> dict:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth not configured")

    data = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data=data,
        timeout=15,
    )
    if token_resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Google exchange failed")
    return token_resp.json()


def _verify_google_id_token(id_token: str) -> dict:
    try:
        req = google_requests.Request()
        claims = google_id_token.verify_oauth2_token(id_token, req, settings.google_client_id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid Google token")
    if not claims.get("email"):
        raise HTTPException(status_code=401, detail="Google account missing email")
    if claims.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="Google email not verified")
    return claims


def authenticate_google_code(
    db: Session,
    *,
    code: str,
    code_verifier: str | None,
    redirect_uri: str,
) -> User:
    tokens = _google_exchange_code(code=code, code_verifier=code_verifier, redirect_uri=redirect_uri)
    claims = _verify_google_id_token(tokens.get("id_token"))
    return get_or_create_google_user(db, claims=claims)


def get_or_create_google_user(db: Session, *, claims: dict) -> User:
    sub = str(claims["sub"])
    email = str(claims["email"]).strip().lower()
    given = (claims.get("given_name") or "").strip() or "Google"
    family = (claims.get("family_name") or "").strip() or "User"

    identity = db.execute(
        select(AuthIdentity).where(AuthIdentity.provider == "google", AuthIdentity.provider_subject == sub)
    ).scalar_one_or_none()

    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found")
        return user

    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        if not existing.is_active:
            raise HTTPException(status_code=401, detail="User not found")
        db.add(
            AuthIdentity(
                user_id=existing.id,
                provider="google",
                provider_subject=sub,
                email=email,
                email_verified=True,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="Google account already linked")
        db.refresh(existing)
        return existing

    user = User(first_name=given, last_name=family, email=email)
    db.add(user)
    db.flush()
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider="google",
            provider_subject=sub,
            email=email,
            email_verified=True,
        )
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/google/exchange", response_model=TokenResponse)
def google_exchange(payload: GoogleExchangeRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user = authenticate_google_code(
        db,
        code=payload.code,
        code_verifier=payload.code_verifier,
        redirect_uri=payload.redirect_uri,
    )
    return _issue_tokens(db, user=user, request=request, response=response)


@router.post("/link/google", response_model=TokenResponse)
def link_google(
    payload: GoogleExchangeRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tokens = _google_exchange_code(code=payload.code, code_verifier=payload.code_verifier, redirect_uri=payload.redirect_uri)
    claims = _verify_google_id_token(tokens.get("id_token"))

    sub = str(claims["sub"])
    email = str(claims["email"]).strip().lower()

    existing = db.execute(
        select(AuthIdentity).where(AuthIdentity.provider == "google", AuthIdentity.provider_subject == sub)
    ).scalar_one_or_none()
    if existing is not None and existing.user_id != current_user.id:
        raise HTTPException(status_code=409, detail="Google account already linked to another user")

    if existing is None:
        db.add(
            AuthIdentity(
                user_id=current_user.id,
                provider="google",
                provider_subject=sub,
                email=email,
                email_verified=True,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="Google account already linked")

    return _issue_tokens(db, user=current_user, request=request, response=response)
