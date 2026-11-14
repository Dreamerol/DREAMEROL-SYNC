
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
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

