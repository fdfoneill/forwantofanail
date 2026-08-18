import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from forwantofanail.api.routes import router
from sqlalchemy import text

from forwantofanail.core.database import get_database_url, get_engine

app_env = os.getenv("APP_ENV", "development").strip().lower()
if app_env == "production":
    missing = [name for name in ("ADMIN_TOKEN", "GAME_PASSWORD", "SESSION_SECRET", "DATABASE_URL", "PUBLIC_ORIGIN") if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required production configuration: {', '.join(missing)}")
    if get_database_url().startswith("sqlite"):
        raise RuntimeError("Production requires PostgreSQL; SQLite is development-only")
    try:
        with get_engine().connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as exc:
        raise RuntimeError("Production database schema is unavailable; run 'alembic upgrade head'") from exc
    if revision != "20260818_0001":
        raise RuntimeError(f"Database revision {revision!r} does not match required revision '20260818_0001'")

app = FastAPI(
    title="For Want of a Nail API",
    version="0.2.0",
    docs_url=None if app_env == "production" else "/docs",
    redoc_url=None if app_env == "production" else "/redoc",
)
app.include_router(router)
static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        cookie_auth = request.cookies.get("fwoan_session") and not request.headers.get("Authorization")
        if cookie_auth:
            expected_origin = os.getenv("PUBLIC_ORIGIN") or f"{request.url.scheme}://{request.url.netloc}"
            if request.headers.get("Origin") != expected_origin.rstrip("/"):
                return JSONResponse(status_code=403, content={"detail": "Invalid request origin"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/dev/dashboard", include_in_schema=False)
def dev_dashboard():
    dashboard_path = static_dir / "dev_dashboard.html"
    return FileResponse(dashboard_path)


@app.get("/player/dashboard", include_in_schema=False)
def player_dashboard():
    dashboard_path = static_dir / "player_dashboard.html"
    return FileResponse(dashboard_path)
