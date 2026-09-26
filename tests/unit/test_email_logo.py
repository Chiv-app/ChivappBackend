import re
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.services.email.client import send_via_brevo
from app.services.email.defaults import EMAIL_TEMPLATE_DEFAULTS
from app.services.email.email_builder import build_email_layout
from app.services.email.logo import (
    LOGO_CID,
    LOGO_FILENAME,
    LOGO_PUBLIC_URL,
    get_logo_base64,
)


def test_logo_base64_returns_valid_string():
    b64 = get_logo_base64()
    assert isinstance(b64, str)
    assert len(b64) > 1000
    assert not b64.startswith("data:")


def test_build_email_layout_uses_cid_and_dimensions():
    html = build_email_layout(
        category_badge="Bienvenido",
        title="Test Title",
        greeting="Hola Músico",
        lead_text="Gracias por unirte a Chivapp.",
    )
    assert f'src="{LOGO_PUBLIC_URL}"' in html
    assert 'alt="Chivapp"' in html
    assert 'width="107"' in html
    assert 'height="34"' in html


def test_email_template_defaults_include_cid_logo():
    for tmpl in EMAIL_TEMPLATE_DEFAULTS:
        assert f"{LOGO_PUBLIC_URL}" in tmpl["html_body"], f"Template {tmpl['slug']} missing public url logo"


def test_send_via_brevo_attaches_inline_logo():
    html_with_logo = f'<img src="cid:{LOGO_CID}" alt="Chivapp" />'

    with patch.object(settings, "EMAIL_ENABLED", True), patch.object(
        settings, "BREVO_API_KEY", "mock-brevo-key"
    ), patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"messageId": "<mock-id@brevo.com>"}
        mock_post.return_value = mock_resp

        result = send_via_brevo(
            to="test@chiv.app",
            subject="Test Subject",
            html=html_with_logo,
            text="Test content",
        )

        assert result == "<mock-id@brevo.com>"
        assert mock_post.called
        call_kwargs = mock_post.call_args[1]
        payload = call_kwargs["json"]

        assert "attachment" in payload
        assert len(payload["attachment"]) == 1
        att = payload["attachment"][0]
        assert att["name"] == LOGO_FILENAME
        assert att["contentId"] == LOGO_CID
        assert len(att["content"]) > 1000


def test_admin_email_preview_replaces_cid_with_public_url():
    sample_html = f'<img src="cid:{LOGO_CID}" alt="Chivapp">'
    preview_html = re.sub(
        rf'src=["\']cid:{LOGO_CID}["\']',
        f'src="{LOGO_PUBLIC_URL}"',
        sample_html,
    )
    assert f"cid:{LOGO_CID}" not in preview_html
    assert LOGO_PUBLIC_URL in preview_html

