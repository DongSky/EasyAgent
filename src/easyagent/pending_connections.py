"""Serializable setup requirements and a private inventory fingerprint for resumable drafts."""
import hashlib
from typing import Literal

from pydantic import Field

from .contracts import Contract
from .store import encode


class ConnectionRequirement(Contract):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,60}$')
    capability: Literal['decision', 'chat', 'image', 'image_edit', 'embedding', 'search',
                        'speech', 'transcription', 'video', 'custom']
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1200)
    model_alias: str | None = None


class PlannedStep(Contract):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1200)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    requires: list[str] = Field(default_factory=list, max_length=12)


class MissingPlanningModel(ValueError):
    pass


def planner_requirement(requested='auto'):
    return ConnectionRequirement(id='planner', capability='decision', title='用于理解需求和编排流程的大语言模型',
        reason='连接支持结构化 JSON 输出的文字模型，并启用“结构化决策”。'
               + (f' 当前指定连接为 {requested}，也可以在对话或助手中改选其他模型。' if requested != 'auto' else ''),
        model_alias=requested if requested != 'auto' else None).model_dump()


def inventory_fingerprint(hub):
    # Only the digest is persisted. Neither ciphertext nor credential values reach model context/UI.
    with hub.store.connect() as db:
        credentials = [tuple(row) for row in db.execute('SELECT name,ciphertext FROM vault ORDER BY name')]
    models = [{**m, 'base_url': getattr(hub.models.bindings[m['alias']].provider, 'base_url', ''),
               'dialect': getattr(hub.models.bindings[m['alias']].provider, 'dialect', '')}
              for m in hub.models.catalog()]
    return hashlib.sha256(encode({'models': sorted(models, key=lambda m: m['alias']),
                                  'tools': sorted(hub.available_tools(), key=lambda t: t['name']),
                                  'credentials': credentials, 'default': hub.connections.default_model()}).encode()).hexdigest()
