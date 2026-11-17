"""Validador de respuestas del modelo: montos, enlaces y fechas con día de la semana."""

from datetime import date

from app.services import assistant_guard as guard


TODAY = date(2026, 11, 16)  # lunes


def test_amounts_must_come_from_the_tools_or_the_musicians_prices():
    ok = guard.check_reply(
        "El show cuesta S/ 450.00 y la hora extra S/ 200.",
        tool_results=[{"total": 450.0}],
        configured_amounts=[200.0],
    )
    bad = guard.check_reply("Te lo dejo en S/ 199.00", tool_results=[{"total": 450.0}])

    assert ok == []
    assert bad == ["monto no respaldado por el sistema: S/ 199.00"]


def test_amount_formats_are_understood():
    assert guard.amounts_in("S/ 1,200.50 y S/450 y S/. 80,5") == [1200.5, 450.0, 80.5]


def test_links_must_be_generated_by_the_system():
    result = {"enlace": "https://chiv.app/pay/abc"}

    assert guard.check_reply("Paga aquí: https://chiv.app/pay/abc.", tool_results=[result]) == []
    assert guard.check_reply("Paga aquí: https://otro.example/x", tool_results=[result]) != []
    assert guard.check_reply("https://chiv.app/pay/abc/", tool_results=[], allowed_urls=["https://chiv.app/pay/abc"]) == []


def test_weekday_that_does_not_match_the_date_is_rejected():
    # 20 de noviembre de 2026 cae viernes
    assert guard.check_reply("Reservamos el viernes 20 de noviembre.", tool_results=[], today=TODAY) == []
    bad = guard.check_reply("Reservamos el sábado 20 de noviembre.", tool_results=[], today=TODAY)

    assert len(bad) == 1 and "sábado 20 de noviembre" in bad[0]


def test_weekday_check_uses_next_year_for_dates_already_past():
    # 5 de enero: ya pasó en 2026, el próximo es el de 2027 (martes)
    assert guard.check_reply("El martes 5 de enero.", tool_results=[], today=TODAY) == []
    assert guard.check_reply("El lunes 5 de enero.", tool_results=[], today=TODAY) != []


def test_dates_without_weekday_or_invalid_dates_are_not_flagged():
    assert guard.check_reply("El 20 de noviembre a las 8 pm. También el sábado 31 de febrero.", tool_results=[], today=TODAY) == []


def test_a_weekday_date_quoted_by_a_tool_can_be_repeated_when_clarifying():
    tool = {"problems": ["El cliente dijo «sábado 20 de noviembre», pero el 20 cae viernes."]}

    quoting = guard.check_reply("Me dices sábado 20 de noviembre, pero cae viernes. ¿Cuál prefieres?", tool_results=[tool], today=TODAY)
    inventing = guard.check_reply("Te esperamos el sábado 20 de noviembre.", tool_results=[], today=TODAY)

    assert quoting == [] and inventing != []
