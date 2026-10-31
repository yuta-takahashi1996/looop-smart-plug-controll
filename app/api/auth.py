"""Web UI の認証・CSRF対策。

単一の管理ユーザー（WEB_USERNAME / WEB_PASSWORD_HASH）でログインし、
ログイン状態は署名付きセッションCookie（SESSION_SECRET）で保持する。
状態を変更するリクエスト（POST）はすべてセッションに紐づく CSRF トークンを要求する。
"""

import base64
import hashlib
import hmac
import secrets
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import get_settings
from app.templating import templates

SESSION_USER_KEY = "user"
SESSION_CSRF_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

# scrypt のコストパラメータ（OWASP 推奨の最小値）
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**17, 8, 1
_SCRYPT_MAXMEM = 256 * 1024 * 1024

router = APIRouter()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected_bytes = base64.b64decode(expected)
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected_bytes),
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, expected_bytes)


def get_csrf_token(request: Request) -> str:
    """セッションの CSRF トークンを返す（なければ発行する）。テンプレートから利用する。"""
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    expected = request.session.get(SESSION_CSRF_KEY)
    supplied = request.headers.get(CSRF_HEADER)
    if supplied is None:
        form = await request.form()
        value = form.get(CSRF_FORM_FIELD)
        supplied = value if isinstance(value, str) else None
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token mismatch")


class LoginRequired(Exception):
    """未ログイン。main.py の例外ハンドラーでログイン画面へ誘導する。"""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


def require_login(request: Request) -> str:
    user = request.session.get(SESSION_USER_KEY)
    if not user:
        next_path = request.url.path
        if request.url.query:
            next_path += "?" + request.url.query
        # HTMX の部分更新では、部分テンプレートではなく元の画面へ戻す
        if request.headers.get("HX-Request"):
            current = urlsplit(request.headers.get("HX-Current-URL", "/"))
            next_path = current.path + (f"?{current.query}" if current.query else "")
        raise LoginRequired(next_path)
    return user


def login_redirect_response(request: Request, exc: LoginRequired) -> Response:
    url = f"/login?next={quote(exc.next_path, safe='')}"
    if request.headers.get("HX-Request"):
        # HTMX はリダイレクトを透過的に追跡してしまうため、画面全体の遷移を指示する
        return Response(status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _safe_next(next_path: str | None) -> str:
    """オープンリダイレクト対策として、サイト内の絶対パスのみ許可する。"""
    if not next_path or not next_path.startswith("/") or next_path.startswith(("//", "/\\")):
        return "/"
    return next_path


def _render_login(request: Request, next_path: str, error: str | None, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next_path": next_path,
            "error": error,
            "auth_configured": get_settings().auth_configured,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse, name="login")
def login_form(request: Request, next: str | None = None) -> Response:
    if request.session.get(SESSION_USER_KEY):
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return _render_login(request, _safe_next(next), None)


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    settings = get_settings()
    next_path = _safe_next(next)
    authenticated = (
        settings.auth_configured
        and secrets.compare_digest(username.encode(), settings.web_username.encode())
        and verify_password(password, settings.web_password_hash.get_secret_value())
    )
    if not authenticated:
        return _render_login(
            request,
            next_path,
            "ユーザー名またはパスワードが正しくありません",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # セッション固定攻撃の対策として、ログイン前のセッションを破棄して作り直す
    request.session.clear()
    request.session[SESSION_USER_KEY] = settings.web_username
    get_csrf_token(request)
    return RedirectResponse(next_path, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", name="logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
