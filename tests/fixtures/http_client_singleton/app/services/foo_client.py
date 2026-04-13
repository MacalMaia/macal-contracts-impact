import httpx

from app.core.config import settings


class FooClient:
    def __init__(self) -> None:
        self.base_url = settings.MACAL_USERS_API_URL
        self._client: httpx.AsyncClient | None = None

    async def get_thing(self, thing_id: str) -> dict:
        client = httpx.AsyncClient(base_url=self.base_url)
        response = await client.get(f"/api/v1/things/{thing_id}")
        return response.json()

    async def create_thing(self, payload: dict) -> dict:
        client = httpx.AsyncClient(base_url=self.base_url)
        response = await client.post("/api/v1/things", json=payload)
        return response.json()
