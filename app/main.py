"""FastAPI application entry point.

Run with: uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import init_db
from app.routes import agent, integrations, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="FastAPI AI Task Agent", lifespan=lifespan)

app.include_router(tasks.router)
app.include_router(integrations.router)
app.include_router(agent.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
