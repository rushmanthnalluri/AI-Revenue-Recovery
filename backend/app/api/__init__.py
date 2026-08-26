"""API package. Routers live in app.api.v1 (auto-discovered by app.main)."""

from fastapi.responses import JSONResponse


def not_implemented(feature: str) -> JSONResponse:
    """Standard stub response for endpoints owned by later feature agents."""
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "code": "not_implemented",
                "message": f"{feature} is not implemented yet (foundation scaffold).",
            }
        },
    )
