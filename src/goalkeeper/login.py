"""Login-only loopback callback; no task launcher or model inference."""
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import AuthBroker
from .paths import PRIVATE
from .secrets import redact


def app():
    broker = AuthBroker(PRIVATE)

    @asynccontextmanager
    async def lifespan(application):
        yield
        await broker.close()

    application = FastAPI(lifespan=lifespan)

    @application.get("/")
    async def home(request: Request):
        connected = broker.status()["connected"]
        text = "授权完成，可以返回当前会话。" if connected else "请选择用于四组评测的同一个 ChatGPT 账户。"
        response = HTMLResponse("<meta charset='utf-8'><p>" + text + "</p><a href='/auth/start'>授权订阅调用</a>")
        if not request.cookies.get("simple_auth_session"):
            response.set_cookie("simple_auth_session", secrets.token_urlsafe(32), httponly=True, samesite="lax")
        return response

    @application.get("/auth/start")
    async def start(request: Request):
        session = request.cookies.get("simple_auth_session")
        if not session:
            return RedirectResponse("/")
        return RedirectResponse(broker.begin(f"http://127.0.0.1:{request.url.port}/auth/callback", session))

    @application.get("/auth/callback")
    async def callback(request: Request):
        try:
            await broker.callback(dict(request.query_params), request.cookies.get("simple_auth_session", ""))
        except Exception as error:
            broker.last_error = str(redact(str(error)))
            return HTMLResponse("授权未完成，请返回会话查看安全状态。", status_code=400)
        return RedirectResponse("/?authorized=1", 303)

    @application.get("/status")
    async def status():
        value = broker.status()
        return {key: value[key] for key in ("connected", "plan_usage_enabled", "pending", "error")}

    return application


def serve(port):
    import uvicorn
    uvicorn.run(app(), host="127.0.0.1", port=port, access_log=False)
