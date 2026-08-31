"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.database import close_db, init_db
from app.core.exceptions import (
    DomainError,
    HTTPException,
    domain_error_handler,
    generic_exception_handler,
    http_exception_handler,
)
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    configure_logging()
    logger.info("Starting JumpTo API", environment=settings.environment)
    await init_db()
    yield
    # Shutdown
    logger.info("Shutting down JumpTo API")
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="JumpTo API",
        description="YouTube Keyword Timestamp Finder API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Exception handlers
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    # Include routers
    app.include_router(api_router)

    # Health check endpoints
    @app.get("/")
    async def root_health() -> dict:
        return {"status": "healthy"}

    @app.get("/health")
    async def health_check() -> dict:
        return {"status": "healthy"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.is_development)
