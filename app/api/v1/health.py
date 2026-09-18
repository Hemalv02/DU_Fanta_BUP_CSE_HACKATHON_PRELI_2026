"""GET /health — judge readiness endpoint (Section 6.2)."""

from fastapi import APIRouter

from app.schemas.response import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return HTTP 200 with {"status": "ok"} when the service is ready."""
    return HealthResponse(status="ok")
