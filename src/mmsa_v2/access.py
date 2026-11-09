"""Local operator and candidate entry points for the V2 competition workbench."""

import hmac
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse


PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = PROJECT / "suit-demo" / "local-data" / "v2"
COOKIE = "mmsa_v2_operator"


def data_root():
    return Path(os.environ.get("MMSA_V2_DATA_DIR", DEFAULT_DATA)).resolve()


def operator_key(root=None):
    folder = Path(root) if root is not None else data_root()
    folder.mkdir(parents=True, exist_ok=True)
    key_path = folder / ".operator-key"
    if not key_path.exists():
        # Publish a complete key, even when STDIO and the launcher start together.
        pending = folder / (".operator-" + secrets.token_hex(12) + ".tmp")
        try:
            with pending.open("x", encoding="ascii") as handle:
                os.chmod(pending, 0o600)
                handle.write(secrets.token_urlsafe(48))
            try:
                os.link(pending, key_path)
            except FileExistsError:
                pass
        finally:
            pending.unlink(missing_ok=True)
    value = key_path.read_text(encoding="ascii")
    if len(value) != 64 or not value.isascii():
        raise RuntimeError("本机 V2 凭据损坏，请用本机启动器检查工作目录。")
    return value


def install_access(app, root):
    local_hosts = {"localhost", "127.0.0.1", "::1"}

    def authorized(token):
        return bool(token and token.isascii() and hmac.compare_digest(token, operator_key(root)))

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        local_test = host == "testserver" and request.client and request.client.host == "testclient"
        if host not in local_hosts and not local_test:
            return JSONResponse({"detail": "仅支持本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).netloc != request.url.netloc or urlsplit(origin).scheme != request.url.scheme):
            return JSONResponse({"detail": "不接受跨来源请求"}, status_code=403)
        path = request.url.path
        if path.startswith("/v2/api/") and path != "/v2/api/status" and not path.startswith("/v2/api/c/"):
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not authorized(bearer or request.cookies.get(COOKIE, "")):
                return JSONResponse({"detail": "请使用本机工作台启动器登录"}, status_code=401)
        result = await call_next(request)
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["X-Content-Type-Options"] = "nosniff"
        if path.startswith("/v2/api/") or path == "/v2/auth":
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.post("/v2/auth", include_in_schema=False)
    def operator_login(request: Request):
        value = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not authorized(value):
            return JSONResponse({"detail": "本机登录凭据无效"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, value, httponly=True, samesite="strict", path="/v2")
        return response
