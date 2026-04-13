from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")


@router.get("/items")
async def list_items() -> list:
    return []


@router.get("/items/{item_id}")
async def get_item(item_id: str) -> dict:
    return {"id": item_id}


@router.post("/items")
async def create_item(body: dict) -> dict:
    return body


@router.delete("/items/{item_id}")
async def delete_item(item_id: str) -> dict:
    return {"deleted": item_id}
