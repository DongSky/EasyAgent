"""Local synthetic API for browser demos. Never contacts TinyFish or other providers."""
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="EasyAgent synthetic API fixture", version="1.0")


@app.get("/search", operation_id="search", description="Synthetic TinyFish-compatible search; not real web results")
async def search(query: str, page: int = 0, x_api_key: str = Header(default="")):
    if x_api_key != "synthetic-demo-key":
        raise HTTPException(401, "Use the documented synthetic demo key")
    return {"query": query, "results": [{"position": 1, "site_name": "example.org", "title": "合成演示资料",
        "snippet": "本地集成验收用固定结果，不代表真实搜索。", "url": "https://example.org/synthetic-source"}], "total_results": 1, "page": page}


class Lookup(BaseModel):
    query: str


@app.post("/lookup", operation_id="lookup", description="Look up synthetic material; safe POST read")
async def lookup(body: Lookup):
    return {"text": "合成 API 回执：" + body.query, "source": "local-fixture"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8771)
