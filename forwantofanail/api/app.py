from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from forwantofanail.api.routes import router

app = FastAPI(title="For Want of a Nail API", version="0.1.1")
app.include_router(router)


@app.get("/dev/dashboard", include_in_schema=False)
def dev_dashboard():
    dashboard_path = Path(__file__).resolve().parents[1] / "web" / "static" / "dev_dashboard.html"
    return FileResponse(dashboard_path)


@app.get("/player/dashboard", include_in_schema=False)
def player_dashboard():
    dashboard_path = Path(__file__).resolve().parents[1] / "web" / "static" / "player_dashboard.html"
    return FileResponse(dashboard_path)
