"""FastAPI factory for the loopback-only local service."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from contribos.config import Settings, server_origin
from contribos.db import connect, migrate
from contribos.security import (
    CSRF_COOKIE, CSRF_HEADER, SAFE_METHODS, SESSION_COOKIE,
    LocalAuth, SecurityError, load_or_create_local_token, valid_origin,
)

STATIC = Path(__file__).with_name("static")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'"
)


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(settings: Settings | None = None, *, auth: LocalAuth | None = None) -> FastAPI:
    settings = settings or Settings()
    origin = server_origin(settings)  # Validate before filesystem access or startup.
    auth = auth or LocalAuth(load_or_create_local_token(settings.local_token_path))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = connect(settings.database_path)
        try:
            migrate(database)
            app.state.db = database
            yield
        finally:
            database.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.settings = settings

    @app.middleware("http")
    async def local_security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        async def dispatch() -> Response:
            # A fixed authority prevents DNS rebinding and Host-derived Origin checks.
            if not valid_origin("http://" + request.headers.get("host", ""), origin):
                return error_response(400, "invalid_host", "Use the configured local server address.")
            path = request.url.path
            is_api = path == "/api" or path.startswith("/api/")
            is_health = path == "/api/health" and request.method in {"GET", "HEAD"}
            if is_api and not is_health:
                authorization = request.headers.get("authorization", "")
                scheme, _, token = authorization.partition(" ")
                local_token = scheme.lower() == "bearer" and auth.valid_local_token(token)
                session_cookie = request.cookies.get(SESSION_COOKIE, "")
                if (authorization and not local_token) or not (local_token or auth.valid_session(session_cookie)):
                    return error_response(401, "authentication_required", "A local session or token is required.")
                if request.method not in SAFE_METHODS:
                    browser_origin = request.headers.get("origin")
                    # Only explicit bearer clients without Origin bypass browser CSRF.
                    if not (local_token and browser_origin is None):
                        if not valid_origin(browser_origin or "", origin):
                            return error_response(403, "origin_rejected", "A matching Origin is required.")
                        if not auth.valid_csrf(
                            session_cookie, request.cookies.get(CSRF_COOKIE, ""),
                            request.headers.get(CSRF_HEADER, ""),
                        ):
                            return error_response(403, "csrf_rejected", "A matching CSRF token is required.")
            try:
                return await call_next(request)
            except Exception:
                return error_response(500, "internal_error", "The request could not be completed.")

        response = await dispatch()
        response.headers.update({
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        })
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(exc.status_code, f"http_{exc.status_code}", "The request could not be completed.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "invalid_request", "The request parameters are invalid.")

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/doctor")
    def doctor_endpoint() -> dict[str, list[dict[str, str]]]:
        from contribos.cli import doctor_report

        return {"checks": doctor_report()}

    @app.get("/desktop/bootstrap")
    async def bootstrap(code: str = "") -> Response:
        try:
            cookies = auth.exchange_startup_code(code)
        except SecurityError:
            return error_response(401, "invalid_startup_code", "The login link is invalid, expired or already used.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, cookies.session, httponly=True, samesite="strict", path="/")
        response.set_cookie(CSRF_COOKIE, cookies.csrf, httponly=False, samesite="strict", path="/")
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
