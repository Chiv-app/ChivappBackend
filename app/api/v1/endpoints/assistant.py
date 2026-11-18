from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session, joinedload

from app.api import deps
from app.core.config import settings
from app.core.limiter import limiter
from app.models.booking_link import BookingLink
from app.models.musician_assistant import (
    AssistantConversation,
    MusicianAssistantConfig,
    MusicianWhatsAppChannel,
)
from app.models.musician_profile import MusicianProfile
from app.models.user import User, UserRole
from app.schemas.assistant import (
    EscalationRulesItem,
    AssistantChatTurnOut,
    AssistantChatTurnRequest,
    AssistantConversationOut,
    AssistantConversationStatusUpdate,
    AssistantMessageItem,
    AssistantUsageOut,
    MusicianAssistantConfigOut,
    MusicianAssistantConfigUpdate,
    WhatsAppChannelConnectRequest,
    WhatsAppChannelOut,
    WhatsAppChannelSettingsUpdate,
)
from app.services.assistant_usage import (
    AssistantQuotaExceeded,
    claim_whatsapp_message,
    get_user_usage,
)
from app.services.assistant_training import get_escalation_rules, record_version
from app.services.musician_assistant import (
    decrypt_whatsapp_token,
    encrypt_whatsapp_token,
    get_or_create_assistant_config,
    get_or_create_whatsapp_channel,
    process_assistant_turn,
)
from app.services.musician_profiles import list_user_profiles, resolve_active_profile
from app.services.uniqueness import normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Musician AI Assistant"])

# El asistente solo responde a mensajes entrantes del cliente (nunca escribe primero ni usa plantillas),
# así que cada respuesta cae dentro de la ventana de servicio de WhatsApp.
AUDIO_UNAVAILABLE_REPLY = (
    "Por ahora no puedo escuchar audios. ¿Me escribes los datos de tu evento "
    "(fecha, hora, lugar y tipo de evento)? Con eso te cotizo enseguida."
)
# Tope de un audio descargado de Meta (las notas de voz de WhatsApp pesan mucho menos).
MAX_AUDIO_BYTES = 5 * 1024 * 1024


def _assert_musician_user(user: User) -> None:
    if user.role != UserRole.musician:
        raise HTTPException(
            status_code=403,
            detail="Solo los músicos pueden configurar su Asistente IA.",
        )


def _resolve_target_profile(
    db: Session,
    user: User,
    profile_id: UUID | None = None,
) -> MusicianProfile:
    if profile_id is not None:
        profiles = list_user_profiles(db, user)
        for p in profiles:
            if p.id == profile_id:
                return p
        raise HTTPException(status_code=404, detail="Perfil de músico no encontrado.")
    profile = resolve_active_profile(db, user)
    if not profile:
        raise HTTPException(status_code=404, detail="Aún no tienes un perfil de músico creado.")
    return profile


def _serialize_config_out(
    profile: MusicianProfile,
    config: MusicianAssistantConfig,
) -> MusicianAssistantConfigOut:
    return MusicianAssistantConfigOut(
        profile_id=profile.id,
        stage_name=profile.stage_name,
        profile_type=profile.profile_type or "solo",
        base_price_per_hour=float(profile.price_per_hour) if profile.price_per_hour is not None else None,
        base_price_per_event=float(profile.price_per_event) if profile.price_per_event is not None else None,
        location_city=profile.location_city,
        ai_engine_active=True,
        enabled=bool(config.enabled),
        greeting_message=config.greeting_message,
        packages=list(config.packages or []),
        additional_hour_price=(
            float(config.additional_hour_price)
            if config.additional_hour_price is not None
            else None
        ),
        zone_surcharges=list(config.zone_surcharges or []),
        per_km_rate=float(config.per_km_rate) if config.per_km_rate is not None else None,
        night_surcharge_amount=float(config.night_surcharge_amount or 0.0),
        night_surcharge_start_hour=int(
            config.night_surcharge_start_hour if config.night_surcharge_start_hour is not None else 23
        ),
        special_date_surcharge_percent=float(config.special_date_surcharge_percent or 0.0),
        max_discount_amount=float(config.max_discount_amount or 0.0),
        max_discount_percent=float(config.max_discount_percent or 0.0),
        custom_instructions=config.custom_instructions,
        tone=config.tone or "cercano",
        voice_examples=[str(e) for e in (config.voice_examples or [])],
        escalation_rules=EscalationRulesItem(**get_escalation_rules(config)),
    )


def _serialize_whatsapp_channel(channel: MusicianWhatsAppChannel) -> WhatsAppChannelOut:
    base_api = (getattr(settings, "OAUTH_REDIRECT_BASE_URL", None) or "https://chiv.app/api/v1").rstrip("/")
    webhook_url = f"{base_api}/whatsapp/webhook"
    verify_token = (
        channel.webhook_verify_token
        or getattr(settings, "META_WHATSAPP_VERIFY_TOKEN", "chivapp-whatsapp-verify")
    )
    meta_app_id = (
        (getattr(settings, "META_WHATSAPP_APP_ID", None) or "").strip()
        or (getattr(settings, "FACEBOOK_APP_ID", None) or "").strip()
        or None
    )
    return WhatsAppChannelOut(
        connected=bool(channel.phone_number_id),
        phone_number_id=channel.phone_number_id,
        waba_id=channel.waba_id,
        display_phone_number=channel.display_phone_number,
        has_access_token=bool(channel.access_token_encrypted),
        is_active=bool(channel.is_active),
        auto_pause_on_human_reply=bool(channel.auto_pause_on_human_reply),
        ignore_ensemble_members=bool(channel.ignore_ensemble_members),
        ignore_personal_chats=bool(channel.ignore_personal_chats),
        ignored_phones=list(channel.ignored_phones or []),
        webhook_url=webhook_url,
        verify_token=verify_token,
        meta_app_id=meta_app_id,
        connected_at=channel.connected_at,
    )


@router.get("/assistant/config", response_model=MusicianAssistantConfigOut)
def get_assistant_config(
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    config = get_or_create_assistant_config(db, profile)
    return _serialize_config_out(profile, config)


@router.put("/assistant/config", response_model=MusicianAssistantConfigOut)
def update_assistant_config(
    payload: MusicianAssistantConfigUpdate,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    config = get_or_create_assistant_config(db, profile)

    config.enabled = payload.enabled
    config.greeting_message = (payload.greeting_message or "").strip() or None
    config.packages = [pkg.model_dump() for pkg in payload.packages]
    config.additional_hour_price = payload.additional_hour_price
    config.zone_surcharges = [z.model_dump() for z in payload.zone_surcharges]
    config.per_km_rate = payload.per_km_rate
    config.night_surcharge_amount = payload.night_surcharge_amount
    config.night_surcharge_start_hour = payload.night_surcharge_start_hour
    config.special_date_surcharge_percent = payload.special_date_surcharge_percent
    config.max_discount_amount = payload.max_discount_amount
    config.max_discount_percent = payload.max_discount_percent
    config.custom_instructions = (payload.custom_instructions or "").strip() or None
    # Los campos de entrenamiento nuevos son opcionales: si la pantalla no los envía, quedan como estaban.
    if payload.tone is not None:
        config.tone = payload.tone
    if payload.voice_examples is not None:
        config.voice_examples = [e.strip()[:300] for e in payload.voice_examples if e and e.strip()][:10]
    if payload.escalation_rules is not None:
        config.escalation_rules = payload.escalation_rules.model_dump()

    record_version(db, profile, config)
    db.commit()
    db.refresh(config)
    return _serialize_config_out(profile, config)


@router.get("/assistant/usage", response_model=AssistantUsageOut)
def get_assistant_usage(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Cuánto del cupo mensual de IA lleva usado el músico."""
    _assert_musician_user(current_user)
    usage = get_user_usage(db, current_user.id)
    return AssistantUsageOut(
        ai_calls_used=usage.ai_calls_used,
        ai_calls_limit=usage.ai_calls_limit,
        audio_used=usage.audio_used,
        audio_limit=usage.audio_limit,
    )


@router.post("/assistant/simulate", response_model=AssistantChatTurnOut)
@limiter.limit(settings.RATE_LIMIT_ASSISTANT)
def simulate_assistant_turn(
    request: Request,
    payload: AssistantChatTurnRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    try:
        res = process_assistant_turn(
            db,
            user=current_user,
            channel_type="simulator",
            message_text=payload.message,
            audio_base64=payload.audio_base64,
            audio_mime_type=payload.audio_mime_type or "audio/webm",
            conversation_id=payload.conversation_id,
            requested_profile_id=payload.profile_id,
            client_phone=payload.client_phone,
            client_name=payload.client_name,
        )
    except AssistantQuotaExceeded:
        raise HTTPException(
            status_code=429,
            detail="Llegaste al límite mensual de audios del asistente. Escribe el mensaje o inténtalo el próximo mes.",
        )
    conv: AssistantConversation = res["conversation"]
    profile: MusicianProfile = res["profile"]
    link: BookingLink | None = res.get("booking_link")
    if link is None and conv.last_booking_link_id:
        link = db.get(BookingLink, conv.last_booking_link_id)

    frontend_base = (getattr(settings, "FRONTEND_URL", None) or "https://chiv.app").rstrip("/")
    link_url = f"{frontend_base}/pay/{link.token}" if link else None

    return AssistantChatTurnOut(
        conversation_id=conv.id,
        profile_id=profile.id if profile else None,
        profile_name=profile.stage_name if profile else None,
        status=conv.status,
        escalation_reason=conv.escalation_reason,
        reply=res.get("reply"),
        extracted_data=dict(conv.extracted_data or {}),
        quote_breakdown=res.get("quote"),
        booking_link_token=link.token if link else None,
        booking_link_url=link_url,
        messages=[AssistantMessageItem(**m) for m in (conv.messages or [])],
    )


@router.get("/assistant/whatsapp", response_model=WhatsAppChannelOut)
def get_whatsapp_channel(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    channel = get_or_create_whatsapp_channel(db, current_user)
    return _serialize_whatsapp_channel(channel)


@router.post("/assistant/whatsapp/connect", response_model=WhatsAppChannelOut)
def connect_whatsapp_channel(
    payload: WhatsAppChannelConnectRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    clean_phone_id = payload.phone_number_id.strip()
    existing = (
        db.query(MusicianWhatsAppChannel)
        .filter(
            MusicianWhatsAppChannel.phone_number_id == clean_phone_id,
            MusicianWhatsAppChannel.user_id != current_user.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Ese Phone Number ID de WhatsApp ya está vinculado a otra cuenta.",
        )

    channel = get_or_create_whatsapp_channel(db, current_user)
    channel.phone_number_id = clean_phone_id
    channel.waba_id = (payload.waba_id or "").strip() or None
    channel.display_phone_number = (payload.display_phone_number or "").strip() or None
    if payload.access_token and payload.access_token.strip():
        channel.access_token_encrypted = encrypt_whatsapp_token(payload.access_token.strip())
    channel.connected_at = datetime.utcnow()
    channel.is_active = True

    db.commit()
    db.refresh(channel)
    return _serialize_whatsapp_channel(channel)


@router.delete("/assistant/whatsapp", response_model=WhatsAppChannelOut)
def disconnect_whatsapp_channel(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    channel = get_or_create_whatsapp_channel(db, current_user)
    channel.phone_number_id = None
    channel.waba_id = None
    channel.display_phone_number = None
    channel.access_token_encrypted = None
    channel.connected_at = None
    db.commit()
    db.refresh(channel)
    return _serialize_whatsapp_channel(channel)


@router.patch("/assistant/whatsapp/settings", response_model=WhatsAppChannelOut)
def update_whatsapp_channel_settings(
    payload: WhatsAppChannelSettingsUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    channel = get_or_create_whatsapp_channel(db, current_user)
    if payload.is_active is not None:
        channel.is_active = payload.is_active
    if payload.auto_pause_on_human_reply is not None:
        channel.auto_pause_on_human_reply = payload.auto_pause_on_human_reply
    if payload.ignore_ensemble_members is not None:
        channel.ignore_ensemble_members = payload.ignore_ensemble_members
    if payload.ignore_personal_chats is not None:
        channel.ignore_personal_chats = payload.ignore_personal_chats
    if payload.ignored_phones is not None:
        cleaned = []
        for p in payload.ignored_phones:
            np = normalize_phone(p) or (p or "").strip()
            if np and np not in cleaned:
                cleaned.append(np)
        channel.ignored_phones = cleaned[:100]

    db.commit()
    db.refresh(channel)
    return _serialize_whatsapp_channel(channel)


@router.get("/assistant/conversations", response_model=list[AssistantConversationOut])
def list_assistant_conversations(
    channel: str | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    query = (
        db.query(AssistantConversation)
        .options(
            joinedload(AssistantConversation.profile),
            joinedload(AssistantConversation.last_booking_link),
        )
        .filter(AssistantConversation.user_id == current_user.id)
    )
    if channel:
        query = query.filter(AssistantConversation.channel == channel)
    rows = query.order_by(AssistantConversation.last_message_at.desc()).limit(50).all()
    return [
        AssistantConversationOut(
            id=row.id,
            profile_id=row.profile_id,
            profile_name=row.profile.stage_name if row.profile else None,
            channel=row.channel,
            client_phone=row.client_phone,
            client_name=row.client_name,
            status=row.status,
            escalation_reason=row.escalation_reason,
            extracted_data=dict(row.extracted_data or {}),
            messages=list(row.messages or []),
            last_booking_link_token=(
                row.last_booking_link.token if row.last_booking_link else None
            ),
            last_message_at=row.last_message_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.patch("/assistant/conversations/{conversation_id}/status", response_model=AssistantConversationOut)
def update_assistant_conversation_status(
    conversation_id: UUID,
    payload: AssistantConversationStatusUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    conv = (
        db.query(AssistantConversation)
        .options(
            joinedload(AssistantConversation.profile),
            joinedload(AssistantConversation.last_booking_link),
        )
        .filter(
            AssistantConversation.id == conversation_id,
            AssistantConversation.user_id == current_user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversación no encontrada.")

    conv.status = payload.status
    db.commit()
    db.refresh(conv)
    return AssistantConversationOut(
        id=conv.id,
        profile_id=conv.profile_id,
        profile_name=conv.profile.stage_name if conv.profile else None,
        channel=conv.channel,
        client_phone=conv.client_phone,
        client_name=conv.client_name,
        status=conv.status,
        escalation_reason=conv.escalation_reason,
        extracted_data=dict(conv.extracted_data or {}),
        messages=list(conv.messages or []),
        last_booking_link_token=(
            conv.last_booking_link.token if conv.last_booking_link else None
        ),
        last_message_at=conv.last_message_at,
        created_at=conv.created_at,
    )


# --- Meta WhatsApp Cloud API Official Webhook ---


@router.get("/whatsapp/webhook")
def verify_meta_whatsapp_webhook(
    request: Request,
    db: Session = Depends(deps.get_db),
):
    """Endpoint de verificación oficial de Meta WhatsApp Cloud API (hub.challenge)."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode != "subscribe" or not token or challenge is None:
        raise HTTPException(status_code=403, detail="Parámetros de verificación inválidos.")

    global_token = (getattr(settings, "META_WHATSAPP_VERIFY_TOKEN", None) or "").strip()
    matched = bool(global_token and hmac.compare_digest(token, global_token))
    if not matched:
        channel = (
            db.query(MusicianWhatsAppChannel)
            .filter(MusicianWhatsAppChannel.webhook_verify_token == token)
            .first()
        )
        matched = channel is not None

    if not matched:
        raise HTTPException(status_code=403, detail="Token de verificación de WhatsApp inválido.")

    return Response(content=str(challenge), media_type="text/plain")


def _download_whatsapp_audio_base64(
    media_id: str,
    access_token: str | None,
) -> tuple[str | None, str]:
    if not media_id or not access_token:
        return None, "audio/ogg"
    version = getattr(settings, "META_GRAPH_API_VERSION", "v21.0") or "v21.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=15.0) as client:
            meta_resp = client.get(
                f"https://graph.facebook.com/{version}/{media_id}",
                headers=headers,
            )
            if meta_resp.status_code >= 400:
                return None, "audio/ogg"
            info = meta_resp.json()
            media_url = info.get("url")
            mime_type = info.get("mime_type") or "audio/ogg"
            if not media_url:
                return None, mime_type
            audio_resp = client.get(media_url, headers=headers)
            if audio_resp.status_code >= 400 or len(audio_resp.content) > MAX_AUDIO_BYTES:
                return None, mime_type
            return base64.b64encode(audio_resp.content).decode("ascii"), mime_type
    except Exception as exc:
        logger.warning("No se pudo descargar audio de WhatsApp media_id=%s: %s", media_id, exc)
        return None, "audio/ogg"


def _send_whatsapp_cloud_message(
    *,
    phone_number_id: str,
    access_token: str | None,
    to_phone: str,
    text: str,
) -> bool:
    if not phone_number_id or not access_token or not to_phone or not text:
        return False
    version = getattr(settings, "META_GRAPH_API_VERSION", "v21.0") or "v21.0"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"https://graph.facebook.com/{version}/{phone_number_id}/messages",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to_phone,
                    "type": "text",
                    "text": {"preview_url": True, "body": text},
                },
            )
            if resp.status_code >= 400:
                # Meta explica el motivo en el cuerpo (token vencido, destinatario no permitido, ventana de 24 h...).
                logger.warning(
                    "WhatsApp Cloud API rechazó el envío (phone_number_id=%s, HTTP %s): %s",
                    phone_number_id,
                    resp.status_code,
                    (resp.text or "")[:300],
                )
                return False
            return True
    except Exception as exc:
        logger.warning("Fallo enviando mensaje a WhatsApp Cloud API: %s", exc)
        return False


@router.post("/whatsapp/webhook")
async def receive_meta_whatsapp_webhook(
    request: Request,
    db: Session = Depends(deps.get_db),
):
    """Recibe mensajes entrantes y ecos de coexistencia desde WhatsApp Cloud API (Meta Oficial)."""
    raw_body = await request.body()
    # La firma es obligatoria: sin el secreto de la app de Meta no se puede saber de quién viene el mensaje,
    # y cada mensaje aceptado puede costar una llamada al modelo. Sin secreto configurado se rechaza todo.
    app_secret = (getattr(settings, "META_WHATSAPP_APP_SECRET", None) or "").strip()
    if not app_secret:
        logger.error("META_WHATSAPP_APP_SECRET no está configurado: se rechaza el webhook de WhatsApp.")
        raise HTTPException(status_code=503, detail="Webhook de WhatsApp no configurado.")
    sig_header = request.headers.get("X-Hub-Signature-256") or ""
    expected_sig = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig_header.encode("utf-8"), expected_sig.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Firma de webhook de Meta inválida.")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido.")

    processed_results: list[dict[str, Any]] = []
    logger.info("Webhook de WhatsApp recibido y firmado correctamente (%d entradas).", len(payload.get("entry") or []))

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            if not phone_number_id:
                continue

            channel = (
                db.query(MusicianWhatsAppChannel)
                .filter(MusicianWhatsAppChannel.phone_number_id == phone_number_id)
                .first()
            )
            if not channel:
                logger.warning(
                    "Webhook de WhatsApp para un número sin canal vinculado en Chivapp (phone_number_id=%s).",
                    phone_number_id,
                )
                continue

            musician_user = db.get(User, channel.user_id)
            if not musician_user:
                continue

            access_token = decrypt_whatsapp_token(channel.access_token_encrypted)

            # 1. Human takeover: si el propio músico escribió desde su celular (smb_message_echoes o from_me)
            echoes = value.get("smb_message_echoes") or []
            for echo in echoes:
                recipient_phone = normalize_phone(echo.get("to")) or echo.get("to")
                if recipient_phone and channel.auto_pause_on_human_reply:
                    conv = (
                        db.query(AssistantConversation)
                        .filter(
                            AssistantConversation.user_id == musician_user.id,
                            AssistantConversation.channel == "whatsapp",
                            AssistantConversation.client_phone == recipient_phone,
                        )
                        .order_by(AssistantConversation.updated_at.desc())
                        .first()
                    )
                    if conv:
                        conv.status = "paused_by_musician"
                        db.commit()
                        processed_results.append(
                            {"type": "human_takeover", "client_phone": recipient_phone, "status": conv.status}
                        )

            # 2. Mensajes entrantes de clientes
            contacts_map: dict[str, str] = {}
            for c in value.get("contacts") or []:
                wa_id = str(c.get("wa_id") or "").strip()
                prof_name = ((c.get("profile") or {}).get("name") or "").strip()
                if wa_id and prof_name:
                    contacts_map[wa_id] = prof_name

            for msg in value.get("messages") or []:
                from_phone = str(msg.get("from") or "").strip()
                if not from_phone:
                    continue
                # Meta reenvía el mismo mensaje si tarda la respuesta: se procesa una sola vez.
                if not claim_whatsapp_message(db, str(msg.get("id") or "")):
                    continue

                # Si el mensaje viene marcado como enviado por el mismo número del negocio, pausar bot
                if msg.get("from_me") is True:
                    target_phone = normalize_phone(msg.get("to") or from_phone) or from_phone
                    if channel.auto_pause_on_human_reply:
                        conv = (
                            db.query(AssistantConversation)
                            .filter(
                                AssistantConversation.user_id == musician_user.id,
                                AssistantConversation.channel == "whatsapp",
                                AssistantConversation.client_phone == target_phone,
                            )
                            .order_by(AssistantConversation.updated_at.desc())
                            .first()
                        )
                        if conv:
                            conv.status = "paused_by_musician"
                            db.commit()
                            processed_results.append(
                                {"type": "human_takeover", "client_phone": target_phone, "status": conv.status}
                            )
                    continue

                msg_type = msg.get("type")
                text_body: str | None = None
                audio_b64: str | None = None
                audio_mime = "audio/ogg"

                if msg_type == "text":
                    text_body = ((msg.get("text") or {}).get("body") or "").strip()
                elif msg_type in ("audio", "voice"):
                    audio_obj = msg.get(msg_type) or {}
                    text_body = (audio_obj.get("transcript") or "").strip() or None
                    audio_b64 = audio_obj.get("data_base64")
                    if not audio_b64 and audio_obj.get("id"):
                        audio_b64, audio_mime = _download_whatsapp_audio_base64(
                            str(audio_obj["id"]),
                            access_token,
                        )
                    if not text_body and not audio_b64:
                        text_body = "Audio de consulta de evento"
                else:
                    continue

                try:
                    turn_res = process_assistant_turn(
                        db,
                        user=musician_user,
                        channel_type="whatsapp",
                        message_text=text_body,
                        audio_base64=audio_b64,
                        audio_mime_type=audio_mime,
                        client_phone=from_phone,
                        client_name=contacts_map.get(from_phone) or "Cliente WhatsApp",
                    )
                except AssistantQuotaExceeded:
                    # Sin cupo para transcribir: se pide al cliente que escriba (no cuesta modelo).
                    if access_token:
                        _send_whatsapp_cloud_message(
                            phone_number_id=phone_number_id,
                            access_token=access_token,
                            to_phone=from_phone,
                            text=AUDIO_UNAVAILABLE_REPLY,
                        )
                    processed_results.append({"type": "audio_quota_exceeded", "client_phone": from_phone})
                    continue
                except Exception:
                    # Un mensaje con error no debe tumbar el webhook: Meta lo reintentaría sin parar.
                    logger.exception("Falló el procesamiento de un mensaje de WhatsApp.")
                    db.rollback()
                    continue
                reply_text = turn_res.get("reply")
                conversation_obj = turn_res["conversation"]
                sent_ok: bool | None = None
                if reply_text and access_token:
                    sent_ok = _send_whatsapp_cloud_message(
                        phone_number_id=phone_number_id,
                        access_token=access_token,
                        to_phone=from_phone,
                        text=reply_text,
                    )
                # Sin contenido del mensaje ni teléfono del cliente: solo lo necesario para saber qué pasó.
                logger.info(
                    "WhatsApp entrante procesado: phone_number_id=%s tipo=%s estado_conversacion=%s motivo=%s respuesta=%s "
                    "token=%s enviada=%s",
                    phone_number_id,
                    msg_type,
                    conversation_obj.status,
                    getattr(conversation_obj, "escalation_reason", None),
                    "sí" if reply_text else "no",
                    "sí" if access_token else "no",
                    sent_ok,
                )
                link_obj = turn_res.get("booking_link")
                processed_results.append(
                    {
                        "type": "message_processed",
                        "conversation_id": str(turn_res["conversation"].id),
                        "status": turn_res["conversation"].status,
                        "reply": reply_text,
                        "booking_link_token": link_obj.token if link_obj else None,
                    }
                )

    return {"status": "ok", "processed": processed_results}
