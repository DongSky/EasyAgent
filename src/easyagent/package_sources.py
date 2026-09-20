"""Operator-selected package catalogs with digest-pinned downloads and install previews."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import httpx
from pydantic import Field

from .contracts import Contract
from .http_tools import HTTPTool
from .skill_packages import inspect_package
from .extension_contracts import ExtensionPackage


class Source(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,40}$")
    title: str = Field(min_length=1, max_length=120)
    url: str


class CatalogItem(Contract):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,100}$")
    title: str
    description: str = ""
    kind: Literal["skill", "extension"]
    url: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SourceCatalog(Contract):
    schema_version: Literal[1] = 1
    items: list[CatalogItem] = Field(max_length=1000)


async def fetch(url):
    HTTPTool(name="catalog.fetch", description="Read package catalog", url=url)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            if r.status_code != 200:
                raise ValueError("catalog requires a direct 200 response")
            chunks = []
            size = 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > 1_800_000:
                    raise ValueError("catalog/package exceeds byte limit")
                chunks.append(chunk)
    return b"".join(chunks)


class PackageSources:
    def __init__(self, hub):
        self.hub = hub

    def list(self):
        return [r["value"] for r in self.hub.store.memory_search("package-sources", limit=200)]

    def save(self, body):
        source = Source.model_validate(body)
        HTTPTool(name="catalog.fetch", description="Read package catalog", url=source.url)
        self.hub.store.memory_put("package-sources", source.id, source.model_dump(), "operator")
        return source.model_dump()

    async def catalog(self, name):
        source = next((s for s in self.list() if s["id"] == name), None)
        if source is None:
            raise KeyError(name)
        catalog = SourceCatalog.model_validate_json(await fetch(source["url"]))
        if len({i.id for i in catalog.items}) != len(catalog.items):
            raise ValueError("duplicate catalog IDs")
        # Only operator-managed catalog entries authorize later download locations.
        self.hub.store.memory_put("package-catalogs", name, catalog.model_dump(), "catalog")
        return catalog.model_dump()

    async def preview(self, name, identifier):
        catalogs = self.hub.store.memory_search("package-catalogs", limit=200)
        catalog = next((r["value"] for r in catalogs if r["key"] == name), None)
        if catalog is None:
            raise KeyError(name)
        item = next((i for i in catalog["items"] if i["id"] == identifier), None)
        if item is None:
            raise KeyError(identifier)
        raw = await fetch(item["url"])
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("package digest mismatch; refresh catalog")
        package = json.loads(raw)
        if item["kind"] == "skill":
            report = inspect_package(package)
        else:
            p = ExtensionPackage.model_validate(package)
            report = self.hub.extensions.preview({"package": p})
        return {"item": item, "package": package, "preview": report}


def install_package_sources(app, hub):
    sources = PackageSources(hub)

    @app.get("/v1/package-sources")
    async def listing():
        return sources.list()

    @app.post("/v1/package-sources")
    async def save(body: Source):
        return sources.save(body)

    @app.get("/v1/package-sources/{name}/catalog")
    async def catalog(name: str):
        return await sources.catalog(name)

    @app.get("/v1/package-sources/{name}/items/{identifier}")
    async def preview(name: str, identifier: str):
        return await sources.preview(name, identifier)
