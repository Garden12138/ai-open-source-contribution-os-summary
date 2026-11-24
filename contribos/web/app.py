"""FastAPI factory for the loopback-only local service."""

import fcntl
import os
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
from contribos.desktop_mode import DesktopMode, DesktopNotifications, SHELL_PATHS, router as desktop_router
from contribos.sandbox.host import safe_text
from contribos.security import (
    CSRF_COOKIE, CSRF_HEADER, SAFE_METHODS, SESSION_COOKIE,
    LocalAuth, SecurityError, load_or_create_local_token, valid_origin,
)
from contribos.tasks.store import TaskError, TaskStore
from contribos.tasks.supervisor import TaskSupervisor
from contribos.publish.service import Publisher
from contribos.review.service import ReviewService
from contribos.web.routes_publish import router as publish_router
from contribos.web.routes_review import router as review_router
from contribos.web.routes_tasks import router as task_router
from contribos.web.routes_settings import router as settings_router
from contribos.web.demo import demo_supervisor

STATIC = Path(__file__).with_name("static")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'"
)


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(settings: Settings | None = None, *, auth: LocalAuth | None = None,
               demo: bool = False, desktop: DesktopMode | None = None,
               supervisor_factory: Callable[[Settings, TaskStore], TaskSupervisor] = TaskSupervisor) -> FastAPI:
    settings = settings or Settings()
    origin = server_origin(settings)  # Validate before filesystem access or startup.
    local_token = load_or_create_local_token(settings.local_token_path) if auth is None else None
    auth = auth or LocalAuth(local_token)

    def clean(text: str) -> str:
        if local_token:
            text = text.replace(local_token, '[REDACTED]')
        if desktop:
            text = text.replace(desktop.shell_token, '[REDACTED]').replace(desktop.startup_code, '[REDACTED]')
        return safe_text(text)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.home.mkdir(parents=True, exist_ok=True)
        fd = os.open(settings.home / 'backend.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('A backend is already using this data directory.') from None
            database = connect(settings.database_path)
            try:
                migrate(database)
                app.state.db = database
                store = TaskStore(database, clean)
                settings.tool_paths.update(store.setting('tool_paths', {}))
                supervisor = demo_supervisor(settings, store) if demo else supervisor_factory(settings, store)
                app.state.supervisor = supervisor
                if not demo:
                    app.state.publisher = Publisher(supervisor)
                    app.state.publisher.recover()
                    app.state.reviews = ReviewService(supervisor)
                    app.state.reviews.recover()
                if desktop:
                    app.state.desktop_notifications = DesktopNotifications(supervisor.events)
                    supervisor.events.on_event = app.state.desktop_notifications.on_task_event
                    auth.issue_startup_code(desktop.startup_code)
                try:
                    supervisor.start()
                    yield
                finally:
                    await supervisor.shutdown()
            finally:
                database.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.settings = settings
    app.state.demo = demo

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
            if desktop and path.rstrip('/') in SHELL_PATHS:
                if not desktop.authorized(request.headers.get("authorization", "")):
                    return error_response(401, "shell_authentication_required", "A desktop shell token is required.")
                if request.method not in SAFE_METHODS:
                    return error_response(405, "method_not_allowed", "This endpoint is read-only.")
            elif is_api and not is_health:
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

    @app.exception_handler(TaskError)
    async def task_error(request: Request, exc: TaskError) -> JSONResponse:
        return error_response(exc.status, clean(exc.code), clean(exc.message))

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/doctor")
    def doctor_endpoint() -> dict[str, list[dict[str, str]]]:
        from contribos.cli import doctor_report

        if demo:
            return {"checks": [{"name": "demo", "status": "ok", "detail": "Offline simulation."}]}
        return {"checks": app.state.supervisor.store.sanitize(doctor_report(settings))}

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

    app.include_router(task_router)
    app.include_router(publish_router)
    app.include_router(review_router)
    app.include_router(settings_router)
    if desktop:
        app.include_router(desktop_router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
