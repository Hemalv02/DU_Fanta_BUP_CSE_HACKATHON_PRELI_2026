"""API v1 routers."""

from app.api.v1 import health, optimize

routers = (health.router, optimize.router)

__all__ = ["health", "optimize", "routers"]
