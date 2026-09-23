from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import settings
from app.core.hashing import hash_password, validate_password_strength, verify_password
from app.core.limiter import limiter
from app.core.jwt import (
    create_access_token,
    create_oauth_pending_token,
    decode_access_token,
)
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.oauth_account import OAuthAccount, OAuthProvider
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.schemas.auth import (
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
    notify_leader_member_joined,
)
from app.services.oauth import (
    _provider_or_400,
    build_authorize_url,
    create_oauth_state,
    exchange_code_for_profile,
    oauth_configured,
    parse_oauth_state,
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
    token = create_access_token(subject=str(user.id), role=user.role.value)
    _set_auth_cookie(response, token)
    _clear_oauth_pending_cookie(response)
    return token


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
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        return None
    try:
        return db.get(User, UUID(str(payload["sub"])))
    except ValueError:
        return None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


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

    token = _issue_login(response, user)
    db.commit()
    return {"access_token": token}


@router.post("/logout")
def logout(response: Response):
    _clear_auth_cookie(response)
    _clear_oauth_pending_cookie(response)
    return {"message": "Logout exitoso"}


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
def preview_password_setup(token: str, db: Session = Depends(deps.get_db)):
    member = find_password_setup_member(db, token)
    user = db.get(User, member.member_user_id)
    leader = db.get(User, member.leader_user_id)
    if not user:
        raise HTTPException(404, "Cuenta no encontrada")

    # Si ya tiene contraseña, activar el integrante al abrir el enlace.
    if user.password_hash:
        activate_member_after_password(db, member)
        mark_email_verified(user)
        notify_leader_member_joined(db, member)
        db.commit()

    return PasswordSetupPreviewOut(
        email=user.email,
        fullname=member.fullname or user.fullname,
        specialties=list(member.specialties or []),
        leader_name=leader.fullname if leader else None,
        requires_password=not bool(user.password_hash),
    )


@router.post("/set-password", response_model=UserOut)
def set_password(
    payload: SetPasswordRequest,
    response: Response,
    request: Request,
    db: Session = Depends(deps.get_db),
):
    """Crea contraseña vía token de invitación o sesión autenticada sin password."""
    password = payload.password.strip()
    if len(password) < 8:
        raise HTTPException(400, "La contraseña debe tener al menos 8 caracteres")

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

    if user.password_hash and not token:
        raise HTTPException(400, "Tu cuenta ya tiene contraseña")

    if not user.password_hash:
        user.password_hash = hash_password(password)

    user.updated_at = datetime.utcnow()

    if member:
        activate_member_after_password(db, member)
        mark_email_verified(user)
        notify_leader_member_joined(db, member)
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
            notify_leader_member_joined(db, pending)

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

    link_user_id = None
    if intent == "link":
        user = _optional_user(request, db)
        if not user:
            raise HTTPException(401, "Debes iniciar sesión para vincular una cuenta")
        link_user_id = str(user.id)

    state = create_oauth_state(intent, link_user_id)
    url = build_authorize_url(provider, intent=intent, state=state)
    return RedirectResponse(url=url, status_code=302)


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
    if error:
        return _frontend_redirect("/login", {"oauth_error": error})
    if not code or not state:
        return _frontend_redirect("/login", {"oauth_error": "missing_code"})

    try:
        intent, link_user_id = parse_oauth_state(state)
        profile = await exchange_code_for_profile(provider, code)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "oauth_failed"
        return _frontend_redirect("/login", {"oauth_error": detail})

    if intent == "link":
        if not link_user_id:
            return _frontend_redirect("/login", {"oauth_error": "link_session"})
        user = db.get(User, UUID(link_user_id))
        if not user:
            return _frontend_redirect("/login", {"oauth_error": "user_not_found"})
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
        redirect = _frontend_redirect(_post_login_path(user))
        _issue_login(redirect, user)
        db.commit()
        return redirect

    user_by_email = None
    if profile.email:
        user_by_email = db.query(User).filter(User.email == profile.email).first()

    if user_by_email:
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
    access = _issue_login(response, user)
    db.commit()
    return {"access_token": access}


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
        token = _issue_login(response, user)
        db.commit()
        return GoogleAuthResponse(
            status="logged_in",
            access_token=token,
            redirect_url=_post_login_path(user),
            role=user.role.value,
        )

    # 2. Existing user by email
    user_by_email = None
    if profile.email:
        user_by_email = db.query(User).filter(User.email == profile.email).first()

    if user_by_email:
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
        token = _issue_login(response, user_by_email)
        db.commit()
        return GoogleAuthResponse(
            status="logged_in",
            access_token=token,
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
def preview_password_reset(token: str, db: Session = Depends(deps.get_db)):
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

    user.password_hash = hash_password(payload.password.strip())
    user.updated_at = datetime.utcnow()
    clear_password_reset(user)
    _issue_login(response, user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
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
    db.commit()
    db.refresh(current_user)
    return {"message": "Contraseña actualizada exitosamente"}





