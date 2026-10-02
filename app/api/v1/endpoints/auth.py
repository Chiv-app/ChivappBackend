import hmac
import secrets
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import settings
from app.core.hashing import hash_password, validate_password_strength, verify_password
from app.core.limiter import get_client_ip, limiter
from app.core.jwt import (
    create_access_token,
    create_oauth_pending_token,
    create_signed_state,
    decode_access_token,
    decode_session_token,
    decode_signed_state,
    session_version,
)
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.oauth_account import OAuthAccount, OAuthProvider
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.schemas.auth import (
    GuestRegisterRequest,
    ChangePasswordRequest,
    GoogleAuthResponse,
    GoogleCredentialRequest,
    LoginRequest,
    OAuthAccountOut,
    OAuthAccountsOut,
    OAuthCompleteRequest,
    OAuthPendingOut,
    PasswordSetupPreviewOut,
    RegisterRequest,
    SetPasswordRequest,
    TokenOut,
)
from app.schemas.email import (
    EmailVerificationResult,
    ForgotPasswordRequest,
    PasswordResetPreviewOut,
    ResetPasswordRequest,
)
from app.schemas.user import UserOut
from app.services.email.auth_emails import (
    clear_password_reset,
    find_password_reset_user,
    mark_email_verified,
    send_email_verification,
    send_password_reset_email,
    send_welcome_email,
    verify_email_token,
)
from app.services.ensemble_members import (
    activate_member_after_password,
    find_password_setup_member,
)
from app.services.oauth import (
    _provider_or_400,
    build_authorize_url,
    exchange_code_for_profile,
    oauth_configured,
    verify_google_id_token,
)
from app.services.uniqueness import (
    assert_email_unique,
    assert_musician_slug_unique,
    assert_phone_unique,
    assert_stage_name_unique,
    assert_username_unique,
    next_available_musician_slug,
    next_available_stage_name,
    slugify,
)

router = APIRouter(prefix="/auth", tags=["Auth"])

OAUTH_PENDING_COOKIE = "oauth_pending"
OAUTH_STATE_COOKIE = "oauth_state"


def _is_secure_cookie() -> bool:
    return settings.FRONTEND_URL.startswith("https://")


def _get_cookie_domain() -> str | None:
    from urllib.parse import urlparse
    host = urlparse(settings.FRONTEND_URL).hostname
    if not host or host in ('localhost', '127.0.0.1'):
        return None
    parts = host.split('.')
    if len(parts) >= 2:
        return f".{parts[-2]}.{parts[-1]}"
    return host

def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=_is_secure_cookie(),
        samesite="lax",
        path="/",
        domain=_get_cookie_domain(),
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=_is_secure_cookie(),
        samesite="lax",
        path="/",
        domain=_get_cookie_domain(),
    )


def _set_oauth_pending_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=OAUTH_PENDING_COOKIE,
        value=token,
        httponly=True,
        secure=_is_secure_cookie(),
        samesite="lax",
        domain=_get_cookie_domain(),
        path="/",
        max_age=30 * 60,
    )


def _clear_oauth_pending_cookie(response: Response) -> None:
    response.delete_cookie(
        key=OAUTH_PENDING_COOKIE,
        httponly=True,
        samesite="lax",
        secure=_is_secure_cookie(),
        domain=_get_cookie_domain(),
        path="/",
    )


def _frontend_redirect(path: str, params: dict | None = None) -> RedirectResponse:
    base = settings.FRONTEND_URL.rstrip("/")
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=302)


def _post_login_path(user: User) -> str:
    if user.role == UserRole.admin:
        return "/admin"
    # Músico y contratista aterrizan en el landing público.
    if user.role in (UserRole.musician, UserRole.contractor):
        return "/"
    return "/"


def _issue_login(response: Response, user: User) -> str:
    user.last_login_at = datetime.utcnow()
    token = create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0)
    _set_auth_cookie(response, token)
    _clear_oauth_pending_cookie(response)
    return token


def _revoke_sessions(user: User) -> None:
    """Invalida todas las sesiones activas del usuario (incluida la actual:
    quien llame debe emitir una cookie nueva con _issue_login si corresponde)."""
    user.token_version = (user.token_version or 0) + 1


def _assert_strong_password(password: str) -> None:
    try:
        validate_password_strength(password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _link_oauth_account(
    db: Session,
    *,
    user: User,
    provider: OAuthProvider,
    provider_user_id: str,
    email: str | None,
) -> None:
    if user.role == UserRole.contractor:
        user.is_verified = True

    existing = (
        db.query(OAuthAccount)
        .filter(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
        .first()
    )
    if existing and existing.user_id != user.id:
        raise HTTPException(
            400,
            "Esta cuenta social ya esta vinculada a otro usuario.",
        )
    if existing:
        existing.email = email
        return

    same_provider = (
        db.query(OAuthAccount)
        .filter(
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider == provider,
        )
        .first()
    )
    if same_provider:
        same_provider.provider_user_id = provider_user_id
        same_provider.email = email
        return

    db.add(
        OAuthAccount(
            user_id=user.id,
            provider=provider,
            provider_user_id=provider_user_id,
            email=email,
        )
    )


def _create_role_profile(db: Session, user: User, oauth_verified: bool = False) -> None:
    if user.role == UserRole.musician:
        stage_name = next_available_stage_name(db, user.fullname) if user.fullname else None
        slug = user.username or (next_available_musician_slug(db, stage_name) if stage_name else None)
        db.add(
            MusicianProfile(
                user_id=user.id,
                stage_name=stage_name,
                slug=slug,
                status=ProfileStatus.draft,
                availability_type=AvailabilityType.both,
            )
        )
    elif user.role == UserRole.contractor:
        db.add(
            ContractorProfile(
                user_id=user.id,
                status=ProfileStatus.published if oauth_verified else ProfileStatus.draft,
            )
        )


def _optional_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_session_token(token)
    if not payload or not payload.get("sub"):
        return None
    try:
        user = db.get(User, UUID(str(payload["sub"])))
    except ValueError:
        return None
    if not user or getattr(user, "is_active", True) is False:
        return None
    if session_version(payload) != (user.token_version or 0):
        return None
    return user


def _client_ip(request: Request) -> str:
    return get_client_ip(request)


@router.post("/register", response_model=UserOut, status_code=201)
@limiter.limit(settings.RATE_LIMIT_REGISTER)
def register_user(payload: RegisterRequest, request: Request, db: Session = Depends(deps.get_db)):
    email = assert_email_unique(db, payload.email)
    if not email:
        raise HTTPException(400, "El correo electrónico es obligatorio")
    phone = assert_phone_unique(db, payload.phone)

    if payload.role not in (UserRole.contractor.value, UserRole.musician.value):
        raise HTTPException(400, "Rol inválido")

    if not payload.accepted_terms:
        raise HTTPException(400, "Debes aceptar los Términos y Condiciones")

    _assert_strong_password(payload.password)

    normalized_username: str | None = None
    if payload.username and payload.username.strip():
        normalized_username = assert_username_unique(db, payload.username)

    cleaned_fullname: str | None = (
        payload.fullname.strip() if payload.fullname and payload.fullname.strip() else None
    )
    if payload.role == UserRole.musician.value and cleaned_fullname:
        assert_stage_name_unique(db, cleaned_fullname)

    user = User(
        email=email,
        username=normalized_username,
        fullname=cleaned_fullname,
        password_hash=hash_password(payload.password),
        role=UserRole(payload.role),
        phone=phone,
        terms_accepted_at=datetime.utcnow(),
        terms_accepted_ip=_client_ip(request),
    )
    db.add(user)
    db.flush()
    _create_role_profile(db, user)
    send_welcome_email(db, user)
    send_email_verification(db, user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
@limiter.limit(settings.RATE_LIMIT_LOGIN)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(deps.get_db)):
    from sqlalchemy import func

    email = (payload.email or "").strip().lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()

    if user and not user.password_hash:
        raise HTTPException(
            status_code=403,
            detail="No se puede utilizar este correo porque ya está en uso.",
        )

    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    if getattr(user, "is_active", True) is False:
        raise HTTPException(status_code=403, detail="Tu cuenta está desactivada")

    _issue_login(response, user)
    db.commit()
    # El token viaja solo en la cookie httponly; no se expone a JavaScript.
    return {"message": "Sesión iniciada"}


@router.post("/logout")
def logout(response: Response):
    _clear_auth_cookie(response)
    _clear_oauth_pending_cookie(response)
    return {"message": "Logout exitoso"}


@router.post("/logout-all")
def logout_all(
    response: Response,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Cierra la sesión en todos los dispositivos."""
    _revoke_sessions(current_user)
    db.commit()
    _clear_auth_cookie(response)
    return {"message": "Se cerró la sesión en todos los dispositivos"}


@router.get("/me", response_model=UserOut)
def get_me(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    is_member = (
        db.query(EnsembleMember.id)
        .filter(EnsembleMember.member_user_id == current_user.id)
        .first()
        is not None
    )
    base = UserOut.model_validate(current_user)
    return base.model_copy(update={"is_ensemble_member": is_member})


@router.get("/password-setup/{token}", response_model=PasswordSetupPreviewOut)
@limiter.limit(settings.RATE_LIMIT_PUBLIC)
def preview_password_setup(token: str, request: Request, db: Session = Depends(deps.get_db)):
    member = find_password_setup_member(db, token)
    user = db.get(User, member.member_user_id)
    leader = db.get(User, member.leader_user_id)
    if not user:
        raise HTTPException(404, "Cuenta no encontrada")

    # Si ya tiene contraseña, activar el integrante al abrir el enlace.
    if user.password_hash:
        activate_member_after_password(db, member)
        mark_email_verified(user)
        db.commit()

    return PasswordSetupPreviewOut(
        email=user.email,
        fullname=member.fullname or user.fullname,
        specialties=list(member.specialties or []),
        leader_name=leader.fullname if leader else None,
        requires_password=not bool(user.password_hash),
    )


@router.post("/set-password", response_model=UserOut)
@limiter.limit(settings.RATE_LIMIT_PASSWORD_RESET)
def set_password(
    payload: SetPasswordRequest,
    response: Response,
    request: Request,
    db: Session = Depends(deps.get_db),
):
    """Crea contraseña vía token de invitación o sesión autenticada sin password."""
    password = payload.password.strip()
    _assert_strong_password(password)

    user: User | None = None
    member = None
    token = (payload.token or "").strip() or None

    if token:
        member = find_password_setup_member(db, token)
        user = db.get(User, member.member_user_id)
    else:
        user = _optional_user(request, db)
        if not user:
            raise HTTPException(
                401,
                "Debes iniciar sesión o usar el enlace de invitación para crear tu contraseña",
            )

    if not user:
        raise HTTPException(404, "Cuenta no encontrada")

    if user.password_hash:
        # Un enlace de invitación nunca inicia sesión en una cuenta con contraseña.
        raise HTTPException(400, "Tu cuenta ya tiene contraseña. Inicia sesión con ella.")

    user.password_hash = hash_password(password)
    _revoke_sessions(user)

    user.updated_at = datetime.utcnow()

    if member:
        activate_member_after_password(db, member)
        mark_email_verified(user)
    else:
        pending_members = (
            db.query(EnsembleMember)
            .filter(
                EnsembleMember.member_user_id == user.id,
                EnsembleMember.status == EnsembleMemberStatus.invited,
            )
            .all()
        )
        for pending in pending_members:
            activate_member_after_password(db, pending)

    _issue_login(response, user)
    db.commit()
    db.refresh(user)
    is_member = (
        db.query(EnsembleMember.id)
        .filter(EnsembleMember.member_user_id == user.id)
        .first()
        is not None
    )
    return UserOut.model_validate(user).model_copy(
        update={"is_ensemble_member": is_member}
    )


@router.get("/oauth/{provider}/start")
def oauth_start(
    provider: str,
    request: Request,
    intent: str = Query(default="login", pattern="^(login|link)$"),
    db: Session = Depends(deps.get_db),
):
    try:
        p = _provider_or_400(provider)
        if not oauth_configured(p):
            return _frontend_redirect(
                "/login", {"oauth_error": f"{p.value}_not_configured"}
            )
    except HTTPException:
        return _frontend_redirect("/login", {"oauth_error": "unsupported_provider"})

    state_payload = {"intent": intent, "nonce": secrets.token_urlsafe(24)}
    if intent == "link":
        user = _optional_user(request, db)
        if not user:
            raise HTTPException(401, "Debes iniciar sesión para vincular una cuenta")
        state_payload["uid"] = str(user.id)

    # El state va firmado y su nonce queda en una cookie de este navegador: un
    # callback con un state ajeno (CSRF / login forzado) no coincide.
    state = create_signed_state("oauth_state", state_payload)
    url = build_authorize_url(provider, intent=intent, state=state)
    redirect = RedirectResponse(url=url, status_code=302)
    redirect.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state_payload["nonce"],
        httponly=True,
        secure=_is_secure_cookie(),
        samesite="lax",
        path="/",
        domain=_get_cookie_domain(),
        max_age=10 * 60,
    )
    return redirect


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    response: Response,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(deps.get_db),
):
    result = await _oauth_callback(provider, request, code, state, error, db)
    result.delete_cookie(OAUTH_STATE_COOKIE, path="/", domain=_get_cookie_domain())
    return result


def _verified_state(request: Request, state: str | None) -> dict | None:
    payload = decode_signed_state(state, "oauth_state")
    cookie_nonce = request.cookies.get(OAUTH_STATE_COOKIE) or ""
    if not payload or not cookie_nonce:
        return None
    if not hmac.compare_digest(cookie_nonce, str(payload.get("nonce") or "")):
        return None
    if payload.get("intent") not in {"login", "link"}:
        return None
    return payload


def _can_link_by_email(user: User, profile) -> bool:
    """Vincular por email solo si el proveedor verificó el email y la cuenta local
    no es una cuenta con contraseña cuyo email nunca se verificó (evita que alguien
    pre-registre el correo de otra persona y luego herede su login social)."""
    if not profile.email_verified:
        return False
    if user.password_hash and not user.email_verified_at:
        return False
    return True


async def _oauth_callback(provider, request, code, state, error, db):
    if error:
        return _frontend_redirect("/login", {"oauth_error": error})
    if not code or not state:
        return _frontend_redirect("/login", {"oauth_error": "missing_code"})

    state_payload = _verified_state(request, state)
    if not state_payload:
        return _frontend_redirect("/login", {"oauth_error": "invalid_state"})
    intent = state_payload["intent"]
    link_user_id = state_payload.get("uid")

    try:
        profile = await exchange_code_for_profile(provider, code)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "oauth_failed"
        return _frontend_redirect("/login", {"oauth_error": detail})

    if intent == "link":
        # La cuenta a vincular es la de la sesión actual, y debe ser la misma
        # que inició el flujo.
        user = _optional_user(request, db)
        if not user or not link_user_id or str(user.id) != link_user_id:
            return _frontend_redirect("/login", {"oauth_error": "link_session"})
        try:
            _link_oauth_account(
                db,
                user=user,
                provider=profile.provider,
                provider_user_id=profile.provider_user_id,
                email=profile.email,
            )
            if profile.picture_url and not user.profile_picture_url:
                user.profile_picture_url = profile.picture_url
            db.commit()
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else "link_failed"
            path = (
                "/musician/profile"
                if user.role == UserRole.musician
                else "/contractor/profile"
            )
            return _frontend_redirect(path, {"oauth_error": detail})

        path = (
            "/musician/profile"
            if user.role == UserRole.musician
            else "/contractor/profile"
            if user.role == UserRole.contractor
            else "/admin"
        )
        return _frontend_redirect(path, {"linked": profile.provider.value})

    existing_oauth = (
        db.query(OAuthAccount)
        .filter(
            OAuthAccount.provider == profile.provider,
            OAuthAccount.provider_user_id == profile.provider_user_id,
        )
        .first()
    )
    if existing_oauth:
        user = db.get(User, existing_oauth.user_id)
        if not user:
            return _frontend_redirect("/login", {"oauth_error": "user_not_found"})
        if not user.email_verified_at:
            user.email_verified_at = datetime.utcnow()
        if user.role == UserRole.contractor and not user.is_verified:
            user.is_verified = True
        redirect = _frontend_redirect(_post_login_path(user))
        _issue_login(redirect, user)
        db.commit()
        return redirect

    user_by_email = None
    if profile.email:
        user_by_email = db.query(User).filter(User.email == profile.email).first()

    if user_by_email:
        if not _can_link_by_email(user_by_email, profile):
            return _frontend_redirect("/login", {"oauth_error": "email_in_use"})
        _link_oauth_account(
            db,
            user=user_by_email,
            provider=profile.provider,
            provider_user_id=profile.provider_user_id,
            email=profile.email,
        )
        if profile.picture_url and not user_by_email.profile_picture_url:
            user_by_email.profile_picture_url = profile.picture_url
        if not user_by_email.email_verified_at:
            user_by_email.email_verified_at = datetime.utcnow()
        if user_by_email.role == UserRole.contractor and not user_by_email.is_verified:
            user_by_email.is_verified = True
        # Invalida sesiones previas (p. ej. de una cuenta sombra creada por otro).
        _revoke_sessions(user_by_email)
        redirect = _frontend_redirect(_post_login_path(user_by_email))
        _issue_login(redirect, user_by_email)
        db.commit()
        return redirect

    pending = create_oauth_pending_token(
        {
            "provider": profile.provider.value,
            "provider_user_id": profile.provider_user_id,
            "email": profile.email,
            "fullname": profile.fullname,
            "picture_url": profile.picture_url,
        }
    )
    redirect = _frontend_redirect("/complete-role")
    _set_oauth_pending_cookie(redirect, pending)
    return redirect


@router.get("/oauth/pending", response_model=OAuthPendingOut)
def oauth_pending(request: Request):
    token = request.cookies.get(OAUTH_PENDING_COOKIE)
    if not token:
        raise HTTPException(401, "No hay un registro social pendiente")
    payload = decode_access_token(token)
    if not payload or payload.get("typ") != "oauth_pending":
        raise HTTPException(401, "Registro social expirado. Intenta de nuevo.")
    return OAuthPendingOut(
        email=payload.get("email"),
        fullname=payload.get("fullname") or "Usuario",
        provider=payload.get("provider") or "",
        picture_url=payload.get("picture_url"),
    )


@router.post("/oauth/complete", response_model=TokenOut)
def oauth_complete(
    payload: OAuthCompleteRequest,
    request: Request,
    response: Response,
    db: Session = Depends(deps.get_db),
):
    token = request.cookies.get(OAUTH_PENDING_COOKIE)
    if not token:
        raise HTTPException(401, "No hay un registro social pendiente")
    pending = decode_access_token(token)
    if not pending or pending.get("typ") != "oauth_pending":
        raise HTTPException(401, "Registro social expirado. Intenta de nuevo.")

    if payload.role not in (UserRole.contractor.value, UserRole.musician.value):
        raise HTTPException(400, "Rol inválido")

    provider = OAuthProvider(pending["provider"])
    provider_user_id = pending["provider_user_id"]
    email = assert_email_unique(db, pending.get("email"))
    fullname = pending.get("fullname") or "Usuario"

    if not email:
        raise HTTPException(
            400,
            "Tu cuenta social no compartió un email. Usa otro método o habilita el email.",
        )

    phone = assert_phone_unique(db, payload.phone)

    already = (
        db.query(OAuthAccount)
        .filter(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
        .first()
    )
    if already:
        raise HTTPException(400, "Esta cuenta social ya está registrada")

    normalized_username: str | None = None
    if payload.role == UserRole.musician.value:
        candidate = payload.username.strip() if payload.username and payload.username.strip() else slugify(fullname)
        normalized_username = assert_username_unique(db, candidate)
        assert_stage_name_unique(db, fullname)
    elif payload.username and payload.username.strip():
        normalized_username = assert_username_unique(db, payload.username)

    user = User(
        email=email,
        username=normalized_username,
        fullname=fullname,
        password_hash=None,
        role=UserRole(payload.role),
        phone=phone,
        profile_picture_url=pending.get("picture_url"),
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    _create_role_profile(db, user, oauth_verified=True)
    _link_oauth_account(
        db,
        user=user,
        provider=provider,
        provider_user_id=provider_user_id,
        email=email,
    )
    _issue_login(response, user)
    db.commit()
    return {"message": "Sesión iniciada"}


@router.post("/oauth/google/credential", response_model=GoogleAuthResponse)
async def oauth_google_credential(
    payload: GoogleCredentialRequest,
    request: Request,
    response: Response,
    db: Session = Depends(deps.get_db),
):
    profile = await verify_google_id_token(payload.credential)

    if payload.intent == "link":
        user = _optional_user(request, db)
        if not user:
            raise HTTPException(401, "Debes iniciar sesión para vincular tu cuenta")
        _link_oauth_account(
            db,
            user=user,
            provider=profile.provider,
            provider_user_id=profile.provider_user_id,
            email=profile.email,
        )
        if profile.picture_url and not user.profile_picture_url:
            user.profile_picture_url = profile.picture_url
        db.commit()
        return GoogleAuthResponse(status="linked", role=user.role.value)

        # 1. Existing OAuth account
    existing_oauth = (
        db.query(OAuthAccount)
        .filter(
            OAuthAccount.provider == profile.provider,
            OAuthAccount.provider_user_id == profile.provider_user_id,
        )
        .first()
    )
    if existing_oauth:
        user = db.get(User, existing_oauth.user_id)
        if not user:
            raise HTTPException(404, "Usuario no encontrado")
        if not user.email_verified_at:
            user.email_verified_at = datetime.utcnow()
        if user.role == UserRole.contractor and not user.is_verified:
            user.is_verified = True
        _issue_login(response, user)
        db.commit()
        return GoogleAuthResponse(
            status="logged_in",
            redirect_url=_post_login_path(user),
            role=user.role.value,
        )

    # 2. Existing user by email
    user_by_email = None
    if profile.email:
        user_by_email = db.query(User).filter(User.email == profile.email).first()

    if user_by_email:
        if not _can_link_by_email(user_by_email, profile):
            raise HTTPException(
                409,
                "Este correo ya tiene una cuenta. Inicia sesión con tu contraseña y vincula Google desde tu perfil.",
            )
        _link_oauth_account(
            db,
            user=user_by_email,
            provider=profile.provider,
            provider_user_id=profile.provider_user_id,
            email=profile.email,
        )
        if profile.picture_url and not user_by_email.profile_picture_url:
            user_by_email.profile_picture_url = profile.picture_url
        if not user_by_email.email_verified_at:
            user_by_email.email_verified_at = datetime.utcnow()
        _revoke_sessions(user_by_email)
        _issue_login(response, user_by_email)
        db.commit()
        return GoogleAuthResponse(
            status="logged_in",
            redirect_url=_post_login_path(user_by_email),
            role=user_by_email.role.value,
        )

    # 3. New user -> pending role
    pending = create_oauth_pending_token(
        {
            "provider": profile.provider.value,
            "provider_user_id": profile.provider_user_id,
            "email": profile.email,
            "fullname": profile.fullname,
            "picture_url": profile.picture_url,
        }
    )
    _set_oauth_pending_cookie(response, pending)
    return GoogleAuthResponse(
        status="pending_role",
        redirect_url="/complete-role",
    )


@router.get("/oauth/accounts", response_model=OAuthAccountsOut)
def list_oauth_accounts(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    rows = (
        db.query(OAuthAccount)
        .filter(OAuthAccount.user_id == current_user.id)
        .all()
    )
    linked = {row.provider.value: row for row in rows}
    accounts = [
        OAuthAccountOut(
            provider=provider.value,
            email=linked[provider.value].email if provider.value in linked else None,
            linked=provider.value in linked,
        )
        for provider in OAuthProvider
    ]
    return OAuthAccountsOut(
        accounts=accounts,
        has_password=bool(current_user.password_hash),
    )


@router.delete("/oauth/{provider}")
def unlink_oauth_account(
    provider: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    try:
        p = OAuthProvider(provider)
    except ValueError as exc:
        raise HTTPException(400, "Proveedor inválido") from exc

    account = (
        db.query(OAuthAccount)
        .filter(
            OAuthAccount.user_id == current_user.id,
            OAuthAccount.provider == p,
        )
        .first()
    )
    if not account:
        raise HTTPException(404, "Cuenta no vinculada")

    other_count = (
        db.query(OAuthAccount)
        .filter(
            OAuthAccount.user_id == current_user.id,
            OAuthAccount.provider != p,
        )
        .count()
    )
    if not current_user.password_hash and other_count == 0:
        raise HTTPException(
            400,
            "No puedes desvincular tu único método de acceso. Agrega una contraseña u otra cuenta social primero.",
        )

    db.delete(account)
    db.commit()
    return {"message": "Cuenta desvinculada"}


@router.post("/verify-email/{token}", response_model=EmailVerificationResult)
def verify_email(token: str, db: Session = Depends(deps.get_db)):
    try:
        user = verify_email_token(db, token)
    except ValueError as exc:
        code = str(exc)
        if code == "expired":
            raise HTTPException(410, "El enlace de verificación expiró. Solicita uno nuevo.") from exc
        raise HTTPException(400, "Enlace de verificación no válido") from exc
    db.commit()
    return EmailVerificationResult(
        message=f"Correo {user.email} verificado correctamente.",
        email_verified=True,
    )


@router.post("/resend-verification", response_model=EmailVerificationResult)
def resend_verification(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.email_verified_at:
        return EmailVerificationResult(
            message="Tu correo ya está verificado.",
            email_verified=True,
        )
    send_email_verification(db, current_user)
    db.commit()
    return EmailVerificationResult(
        message="Te enviamos un nuevo enlace de verificación.",
        email_verified=False,
    )


@router.post("/forgot-password")
@limiter.limit(settings.RATE_LIMIT_PASSWORD_RESET)
def forgot_password(payload: ForgotPasswordRequest, request: Request, db: Session = Depends(deps.get_db)):
    from sqlalchemy import func

    email = str(payload.email).strip().lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user and user.password_hash and user.is_active:
        send_password_reset_email(db, user)
        db.commit()
    return {
        "message": "Si el correo existe en nuestra plataforma, recibirás instrucciones para restablecer tu contraseña.",
    }


@router.get("/password-reset/{token}", response_model=PasswordResetPreviewOut)
@limiter.limit(settings.RATE_LIMIT_PUBLIC)
def preview_password_reset(token: str, request: Request, db: Session = Depends(deps.get_db)):
    try:
        user = find_password_reset_user(db, token)
    except ValueError as exc:
        code = str(exc)
        if code == "expired":
            raise HTTPException(410, "El enlace expiró. Solicita uno nuevo.") from exc
        raise HTTPException(404, "Enlace no válido") from exc
    return PasswordResetPreviewOut(email=user.email, fullname=user.fullname)


@router.post("/reset-password", response_model=UserOut)
@limiter.limit(settings.RATE_LIMIT_PASSWORD_RESET)
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(deps.get_db),
):
    try:
        user = find_password_reset_user(db, payload.token)
    except ValueError as exc:
        code = str(exc)
        if code == "expired":
            raise HTTPException(410, "El enlace expiró. Solicita uno nuevo.") from exc
        raise HTTPException(404, "Enlace no válido") from exc

    new_password = payload.password.strip()
    _assert_strong_password(new_password)
    user.password_hash = hash_password(new_password)
    user.updated_at = datetime.utcnow()
    clear_password_reset(user)
    _revoke_sessions(user)
    _issue_login(response, user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Permite al usuario autenticado cambiar su contraseña o crearla si no tiene."""
    new_password = payload.new_password.strip()

    if current_user.password_hash:
        if not payload.current_password:
            raise HTTPException(400, "Debes ingresar tu contraseña actual")
        if not verify_password(payload.current_password, current_user.password_hash):
            raise HTTPException(400, "La contraseña actual es incorrecta")
        if payload.current_password == new_password:
            raise HTTPException(400, "La nueva contraseña debe ser diferente a la actual")

    try:
        validate_password_strength(new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    current_user.password_hash = hash_password(new_password)
    current_user.updated_at = datetime.utcnow()
    # Cierra las demás sesiones y mantiene abierta la de este dispositivo.
    _revoke_sessions(current_user)
    _issue_login(response, current_user)
    db.commit()
    db.refresh(current_user)
    return {"message": "Contraseña actualizada exitosamente"}







@router.post("/guest-register", response_model=UserOut, status_code=201)
@limiter.limit(settings.RATE_LIMIT_REGISTER)
def guest_register(
    payload: GuestRegisterRequest,
    response: Response,
    request: Request,
    db: Session = Depends(deps.get_db)
):
    email = payload.email.lower().strip()
    
    # Check if user already exists
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        # Nunca iniciar sesión solo con conocer el email: la cuenta puede ser de
        # otra persona (Google, integrante invitado, invitado previo).
        raise HTTPException(
            status_code=409,
            detail="Este correo ya tiene una cuenta. Inicia sesión para continuar con tu reserva.",
        )

    # Create new guest user (shadow account)
    phone = assert_phone_unique(db, payload.phone) if payload.phone else None
    cleaned_fullname = payload.fullname.strip() if payload.fullname else "Invitado"

    new_user = User(
        email=email,
        password_hash=None, # NO PASSWORD = Shadow Account
        fullname=cleaned_fullname,
        # El checkout sin cuenta es solo para contratistas.
        role=UserRole.contractor,
        phone=phone,
        is_verified=False,
        email_verified_at=None,
        terms_accepted_at=datetime.utcnow(),
    )
    db.add(new_user)
    db.flush()
    db.add(ContractorProfile(user_id=new_user.id))
    db.commit()
    db.refresh(new_user)

    access_token = create_access_token(subject=str(new_user.id), role=new_user.role.value, version=new_user.token_version or 0)
    _set_auth_cookie(response, access_token)

    return new_user

def _safe_redirect_path(path: str | None) -> str:
    """Solo rutas internas relativas (evita open redirect tipo '@evil.com' o '//evil.com')."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


# Los magic links solo se generan dentro de los emails de notificación
# (booking_notifications). No existe endpoint público para pedirlos.
@router.get("/magic-link/login")
def magic_link_login(
    token: str,
    response: Response,
    db: Session = Depends(deps.get_db)
):
    from fastapi.responses import RedirectResponse
    payload = decode_access_token(token)
    if not payload or not payload.get("sub") or not payload.get("magic"):
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/login?error=invalid_magic_link")
        
    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if not user:
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/login?error=user_not_found")
        
    if getattr(user, "is_active", True) is False:
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/login?error=invalid_magic_link")

    # Abrir el enlace prueba que el email es suyo, pero NO aprueba la cuenta:
    # is_verified es la aprobación del admin.
    if user.email_verified_at is None:
        user.email_verified_at = datetime.utcnow()
        db.commit()

    access_token = create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0)

    redirect_to = _safe_redirect_path(payload.get("redirect_to"))
    # Important: Create RedirectResponse first, then set the cookie on it
    # Otherwise the cookie is set on the injected response but dropped by the returned redirect
    redirect_response = RedirectResponse(url=f"{settings.FRONTEND_URL}{redirect_to}")
    _set_auth_cookie(redirect_response, access_token)
    return redirect_response




