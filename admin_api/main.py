from fastapi import FastAPI
from .routers.health import router as health_router
from .routers.sessions import router as sessions_router
from .routers.players import router as players_router
from .routers.ready import router as ready_router
from .routers.audit import router as audit_router
from .routers.outbox import router as outbox_router
from .routers.admin import router as admin_router

app = FastAPI(title="Internal Admin API", version="0.2.0")

app.include_router(admin_router)
app.include_router(health_router)
app.include_router(sessions_router)
app.include_router(players_router)
app.include_router(ready_router)
app.include_router(audit_router)
app.include_router(outbox_router)