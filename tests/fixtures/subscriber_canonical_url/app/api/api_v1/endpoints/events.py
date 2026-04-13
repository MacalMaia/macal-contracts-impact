from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")


@router.post("/events/foo.bar-baz")
async def handle_foo_bar_baz(body: dict) -> dict:
    return {"ok": True}


@router.post("/events/another.topic")
async def handle_another_topic(body: dict) -> dict:
    return {"ok": True}
