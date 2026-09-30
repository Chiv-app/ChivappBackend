from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

LOGO_CID = "chivapp-logo"
LOGO_FILENAME = "logo-chivappv2.png"
LOGO_PUBLIC_URL = "https://chiv.app/logo-chivappv2.png"

ASSET_PATH = Path(__file__).resolve().parent / "assets" / "logo-chivappv2.png"


@lru_cache(maxsize=1)
def get_logo_base64() -> str:
    """Retorna el contenido en base64 del logo optimizado para correos."""
    if ASSET_PATH.exists():
        return base64.b64encode(ASSET_PATH.read_bytes()).decode("ascii")

    alt_frontend = Path(__file__).resolve().parents[4] / "Frontend" / "public" / "logo-chivappv2.png"
    if alt_frontend.exists():
        return base64.b64encode(alt_frontend.read_bytes()).decode("ascii")

    return ""
