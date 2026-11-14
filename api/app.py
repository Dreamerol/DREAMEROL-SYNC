
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.auth import WebAuthManager
from api.routes import router
from auth_broker.client import DEFAULT_BROKER_URL


load_dotenv()

app = FastAPI(
    title="DREAMEROL FORK SYNC",
    docs_url=None,
    redoc_url=None,
)

app.state.auth_manager = WebAuthManager(
    os.getenv("BROKER_URL", DEFAULT_BROKER_URL)
)


@app.middleware("http")
async def add_api_no_store_header(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(router)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app.mount(
    "/static",
    StaticFiles(directory=WEB_DIR),
    name="static",
)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")

