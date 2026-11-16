from datetime import date, time
from decimal import Decimal
from types import SimpleNamespace

from app.services.musician_assistant import (
    calculate_smart_quote,
    extract_booking_fields_from_text_or_audio,
    _extract_fields_heuristic,
    _build_assistant_reply,
    should_ignore_incoming_message,
    encrypt_whatsapp_token,
    decrypt_whatsapp_token,
)
from app.schemas.booking_link import BookingLinkCreate, BookingLinkPublicOut


def _make_dummy_profile():
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        stage_name="Mariachi América Ayacucho",
        slug="mariachi-america-ayacucho",
        profile_type="group",
        group_size="6-8",
        location_city="Ayacucho",
        details={"other_cities": ["Huamanga", "San Juan Bautista"]},
        price_per_hour=Decimal("350.00"),
        price_per_event=Decimal("400.00"),
        genres=["Mariachi", "Boleros"],
        instruments=["Trompeta", "Violín", "Guitarrón"],
        repertoire=[{"title": "Las Mañanitas"}, {"title": "El Rey"}],
        songs=["Las Mañanitas", "El Rey"],
        showreel_video_url="https://youtu.be/demo123",
        videos=[],
    )


def _make_dummy_config():
    return SimpleNamespace(
        enabled=True,
        greeting_message="¡Hola! Soy el asistente de Mariachi América Ayacucho.",
        packages=[
            {
                "id": "pkg-5",
                "name": "Serenata 5 integrantes (1 hora)",
                "members_count": 5,
                "duration_hours": 1.0,
                "price": 320.0,
                "description": "Show de 8 canciones",
            },
            {
                "id": "pkg-7",
                "name": "Show Completo 7 integrantes (1 hora)",
                "members_count": 7,
                "duration_hours": 1.0,
                "price": 420.0,
                "description": "Show de 12 canciones",
            },
        ],
        additional_hour_price=Decimal("250.00"),
        zone_surcharges=[
            {"zone": "Huamanga", "surcharge": 0.0, "distance_km": 0.0},
            {"zone": "Carmen Alto", "surcharge": 30.0, "distance_km": 5.0},
            {"zone": "Quinua", "surcharge": 80.0, "distance_km": 35.0},
        ],
        per_km_rate=Decimal("2.50"),
        night_surcharge_amount=Decimal("50.00"),
        night_surcharge_start_hour=23,
        special_date_surcharge_percent=Decimal("15.00"),
        max_discount_amount=Decimal("30.00"),
        max_discount_percent=Decimal("5.00"),
        custom_instructions="Si piden en Jesús Nazareno no cobres movilidad.",
    )


def test_smart_quote_package_zone_night_and_discount():
    profile = _make_dummy_profile()
    config = _make_dummy_config()

    # 7 integrantes, 1h (S/ 420) + Carmen Alto (+S/ 30) + noche 23:30 (+S/ 50) = S/ 500
    quote = calculate_smart_quote(
        profile,
        config,
        members_count=7,
        duration_hours=1.0,
        zone_or_city="Carmen Alto",
        address="Jr. Libertad 450, Carmen Alto",
        start_time=time(23, 30),
        requested_discount=False,
    )
    assert quote["out_of_coverage"] is False
    assert quote["base_price"] == 420.0
    assert quote["zone_surcharge"] == 30.0
    assert quote["night_surcharge"] == 50.0
    assert quote["final_price"] == 500.0

    # Con negociación de descuento (máx S/ 30.00 entre 30 fijo y 5% de 500 = 25)
    quote_discounted = calculate_smart_quote(
        profile,
        config,
        members_count=7,
        duration_hours=1.0,
        zone_or_city="Carmen Alto",
        address="Jr. Libertad 450, Carmen Alto",
        start_time=time(23, 30),
        requested_discount=True,
    )
    assert quote_discounted["discount_applied"] == 30.0
    assert quote_discounted["final_price"] == 470.0


def test_smart_quote_out_of_coverage_escalation():
    profile = _make_dummy_profile()
    config = _make_dummy_config()

    quote = calculate_smart_quote(
        profile,
        config,
        members_count=7,
        duration_hours=1.0,
        zone_or_city="Arequipa",
        address="Plaza de Armas de Arequipa",
        start_time=time(20, 0),
    )
    assert quote["out_of_coverage"] is True


def test_extract_booking_fields_from_natural_spanish_text():
    text = (
        "Acabo de cerrar por llamada con soy María López al 987654321, correo maria@test.com, "
        "para una serenata de cumpleaños el 2026-12-20 a las 9pm en Jr. Libertad 450 en Carmen Alto, "
        "con 7 integrantes por S/ 450"
    )
    result = extract_booking_fields_from_text_or_audio(
        text=text,
        audio_base64=None,
    )
    assert result["price"] == 450.0
    assert result["event_date"] == "2026-12-20"
    assert result["start_time"] == "21:00"
    assert result["event_type"] == "Cumpleaños"
    assert result["client_phone"] == "987654321"
    assert result["client_email"] == "maria@test.com"
    assert result["location_address"] is not None
    assert "Libertad 450" in result["location_address"]

    heur = _extract_fields_heuristic(text)
    assert heur["members_count"] == 7


def test_assistant_reply_builder_and_token_encryption():
    profile = _make_dummy_profile()
    config = _make_dummy_config()
    conv = SimpleNamespace(client_name="Carlos Pérez", messages=[{"sender": "client", "text": "Hola"}])
    extracted = {
        "event_date": "2026-12-18",
        "start_time": "20:00",
        "event_type": "Cumpleaños",
        "location_address": "Av. Mariscal Cáceres 820, Huamanga",
        "client_fullname": "Carlos Pérez",
    }
    quote = calculate_smart_quote(
        profile,
        config,
        members_count=7,
        duration_hours=1.0,
        zone_or_city="Huamanga",
        address="Av. Mariscal Cáceres 820",
        start_time=time(20, 0),
    )

    reply = _build_assistant_reply(
        profile=profile,
        config=config,
        conversation=conv,
        user_message="Soy Carlos Pérez, será en Av. Mariscal Cáceres 820, Huamanga",
        extracted=extracted,
        quote=quote,
        availability_ok=True,
        availability_reason=None,
        booking_link_url="https://chivapp.pe/pay/token-demo-123",
    )
    assert "https://chivapp.pe/pay/token-demo-123" in reply
    assert "S/ 420.00" in reply

    enc = encrypt_whatsapp_token("EAAB-secret-meta-token")
    assert enc != "EAAB-secret-meta-token"
    assert decrypt_whatsapp_token(enc) == "EAAB-secret-meta-token"


def test_personal_chat_filtering():
    class DummyQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [SimpleNamespace(fullname="Juan Trompeta", phone="911222333")]

    class DummyDB:
        def query(self, model):
            return DummyQuery()

    user = SimpleNamespace(id="00000000-0000-0000-0000-000000000002")
    channel = SimpleNamespace(
        ignored_phones=["999000111"],
        ignore_ensemble_members=True,
        ignore_personal_chats=True,
    )

    # 1. Integrante de la agrupación -> ignorado
    ignore_member, reason_member = should_ignore_incoming_message(
        DummyDB(),
        user=user,
        channel=channel,
        client_phone="+51 911 222 333",
        message_text="Hola maestro a qué hora ensayamos",
        is_new_conversation=True,
    )
    assert ignore_member is True
    assert "Juan Trompeta" in (reason_member or "")

    # 2. Chat familiar/personal -> ignorado
    ignore_family, _ = should_ignore_incoming_message(
        DummyDB(),
        user=user,
        channel=channel,
        client_phone="955444333",
        message_text="Amor ya llegué a casa, vamos a almorzar con mamá",
        is_new_conversation=True,
    )
    assert ignore_family is True

    # 3. Cliente real pidiendo cotización -> NO ignorado
    ignore_client, _ = should_ignore_incoming_message(
        DummyDB(),
        user=user,
        channel=channel,
        client_phone="955444333",
        message_text="Buenas noches, cuánto cobran por una serenata de cumpleaños para mi mamá este sábado",
        is_new_conversation=True,
    )
    assert ignore_client is False


def test_prefilled_booking_link_schemas():
    payload = BookingLinkCreate(
        price=420.0,
        expiration_hours=48,
        event_date=date(2026, 12, 18),
        start_time=time(20, 0),
        end_time=time(21, 0),
        event_type="Cumpleaños",
        location_address="Av. Mariscal Cáceres 820, Huamanga",
        location_city="Ayacucho",
        client_fullname="Carlos Pérez",
        client_phone="987654321",
        client_email="carlos@example.com",
        source="whatsapp_ai",
    )
    assert payload.price == 420.0
    assert payload.source == "whatsapp_ai"
    assert payload.location_address == "Av. Mariscal Cáceres 820, Huamanga"
    assert BookingLinkPublicOut.model_fields["location_address"] is not None
