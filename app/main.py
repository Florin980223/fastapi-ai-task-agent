"""FastAPI application entry point.

Run with: uvicorn app.main:app --reload
"""

from fastapi import FastAPI

from app.routes import agent, integrations, tasks

app = FastAPI(title="FastAPI AI Task Agent")

app.include_router(tasks.router)
app.include_router(integrations.router)
app.include_router(agent.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
