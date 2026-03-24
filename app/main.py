import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.presentation.middlewares.request_id import RequestIDMiddleware
from app.presentation.routers import auth, documents, users
from app.presentation.routers.integration import router as integration_router
from app.presentation.routers.integration import submit_router

logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(documents.router, prefix=settings.api_prefix)
app.include_router(users.router, prefix=settings.api_prefix)
# Integration: launch, mapped-signers, signing-progress
app.include_router(integration_router, prefix=settings.api_prefix)
# Submit: POST /api/documents/{id}/submit  (lives alongside existing document endpoints)
app.include_router(submit_router, prefix=settings.api_prefix)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
