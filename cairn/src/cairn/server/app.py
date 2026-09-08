from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.routers import agents, export, hints, intents, orchestration, projects, settings

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="Cairn CTF",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

from cairn.server.routers import logs

app.include_router(logs.router)
app.include_router(settings.router)
app.include_router(agents.router)
from cairn.server.routers import extensions
app.include_router(extensions.router)
from cairn.server.routers import attachments
app.include_router(attachments.router)
app.include_router(projects.router)
app.include_router(orchestration.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
