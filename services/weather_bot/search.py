from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel


logger = logging.getLogger(__name__)


class SearchResult(BaseModel):
    title: str
    url: str = ""
    content: str = ""


class TavilySearchClient:
    def __init__(self, api_key: str | None = None, timeout: float = 12.0):
        self.api_key = api_key
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, max_results: int = 4) -> list[SearchResult]:
        if not self.enabled:
            return []
        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post("https://api.tavily.com/search", json=payload)
            if response.status_code >= 400:
                logger.warning("Tavily search HTTP %s: %s", response.status_code, response.text[:300])
                return []
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Tavily search failed: %s", exc)
            return []
        return _parse_search_results(body)


def _parse_search_results(body: dict[str, Any]) -> list[SearchResult]:
    results = []
    for item in body.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        url = str(item.get("url") or "").strip()
        if title or content:
            results.append(SearchResult(title=title or url or "搜索结果", url=url, content=content))
    return results
