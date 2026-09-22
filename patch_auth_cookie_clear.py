import sys
sys.path.append('.')

path = 'app/api/v1/endpoints/auth.py'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('def _clear_oauth_pending_cookie(response: Response) -> None:\n    response.delete_cookie(\n        key=OAUTH_PENDING_COOKIE,\n        httponly=True,\n        samesite="lax",\n        secure=_is_secure_cookie(),\n        path="/",\n    )', 'def _clear_oauth_pending_cookie(response: Response) -> None:\n    response.delete_cookie(\n        key=OAUTH_PENDING_COOKIE,\n        httponly=True,\n        samesite="lax",\n        secure=_is_secure_cookie(),\n        domain=_get_cookie_domain(),\n        path="/",\n    )')

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)
