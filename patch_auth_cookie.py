import sys
import re
sys.path.append('.')

path = 'app/api/v1/endpoints/auth.py'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

# I notice that _set_oauth_pending_cookie sets samesite='lax', but doesn't set domain.
# Also it's accessed via frontend proxy. The cookies might be dropped if the domain is not specified and it crosses subdomains.
# Oh, we had a bug with _set_auth_cookie where we added domain=_get_cookie_domain().
# We should probably do the same for _set_oauth_pending_cookie.

text = text.replace('def _set_oauth_pending_cookie(response: Response, token: str) -> None:\n    response.set_cookie(\n        key=OAUTH_PENDING_COOKIE,\n        value=token,\n        httponly=True,\n        secure=_is_secure_cookie(),\n        samesite="lax",\n        path="/",\n        max_age=30 * 60,\n    )', 'def _set_oauth_pending_cookie(response: Response, token: str) -> None:\n    response.set_cookie(\n        key=OAUTH_PENDING_COOKIE,\n        value=token,\n        httponly=True,\n        secure=_is_secure_cookie(),\n        samesite="lax",\n        domain=_get_cookie_domain(),\n        path="/",\n        max_age=30 * 60,\n    )')

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)
