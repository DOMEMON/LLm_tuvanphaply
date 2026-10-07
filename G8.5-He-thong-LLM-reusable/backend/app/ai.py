import httpx
from fastapi import Request

class AIClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

def get_ai(request: Request) -> AIClient:
    return request.app.state.ai
