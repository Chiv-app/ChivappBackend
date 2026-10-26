from app.services.uniqueness import (
    normalize_document_number,
    normalize_email,
    normalize_phone,
    normalize_stage_name,
    normalize_username,
    slugify,
)


def test_normalize_email_lowercases_and_strips():
    assert normalize_email("  User@Example.COM  ") == "user@example.com"


def test_normalize_email_empty_returns_none():
    assert normalize_email("") is None
    assert normalize_email(None) is None


def test_normalize_phone_strips_spaces_and_dashes():
    assert normalize_phone("+51 999-888-777") == "999888777"
    assert normalize_phone("51 999 888 777") == "999888777"
    assert normalize_phone("+34 600-111-222") == "+34600111222"


def test_normalize_phone_plus_only_kept_at_start():
    assert normalize_phone("99+9888") == "999888"


def test_normalize_phone_empty_returns_none():
    assert normalize_phone("   ") is None
    assert normalize_phone(None) is None


def test_normalize_document_number_uppercases_and_removes_spaces():
    assert normalize_document_number(" ab 123 cd ") == "AB123CD"


def test_normalize_stage_name_collapses_whitespace():
    assert normalize_stage_name("  Los   Mariachis   Reales  ") == "Los Mariachis Reales"


def test_normalize_stage_name_empty_returns_none():
    assert normalize_stage_name("   ") is None


def test_slugify_basic():
    assert slugify("Los Mariachis Reales") == "los-mariachis-reales"


def test_slugify_strips_accents_and_symbols():
    assert slugify("Mariachi Águila Dorado!!") == "mariachi-aguila-dorado"


def test_slugify_falls_back_to_musico_when_empty():
    assert slugify("¡¡¡!!!") == "musico"


def test_normalize_username():
    assert normalize_username("Mariachi Gallo") == "mariachi-gallo"
    assert normalize_username("  mariachi-sol_99  ") == "mariachi-sol_99"
    assert normalize_username("¡¡Mariachi Óliver!!") == "mariachi-oliver"
    assert normalize_username("") is None
    assert normalize_username(None) is None

