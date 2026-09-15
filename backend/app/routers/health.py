from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
