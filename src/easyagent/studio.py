from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Literal

import httpx
from fastapi import HTTPException, Response
from pydantic import Field

from .contracts import AgentConfig, Contract, ModelRequest, RunLimits, Step, Workflow
from .models import HTTPProvider, ProviderError
from .assistant_builder import start_build, build_status, compiled_workflow


class Assistant(Contract):
    construction: Literal["manual", "automatic"] = "manual"
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=12000)
    model: str = "mock"
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    memory_namespaces: list[str] = Field(default_factory=list)
    strategy: Literal["react", "plan_execute"] = "react"
    max_turns: int = Field(default=8, ge=1, le=64)
    max_tool_calls: int = Field(default=16, ge=0, le=128)
    context_chars: int = Field(default=64000, ge=4000, le=500000)
    policy: str | None = None
    capability: Literal["chat", "image", "embedding"] = "chat"
    limits: RunLimits = Field(default_factory=RunLimits)


class AssistantRun(Contract):
    auto_repair: bool = False
    allow_code: bool = False
    max_revisions: int = Field(default=3, ge=0, le=12)
    message: str = Field(min_length=1, max_length=32000)


class Connection(Contract):
    alias: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,60}$")
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    dialect: Literal["chat", "responses", "anthropic"] = "chat"
    api_key: str = ""
    capabilities: list[str] = Field(default_factory=lambda: ["chat", "decision"])


TEMPLATES = [
    {"id": "organizer", "name": "事项整理助手", "description": "从通知提取待办事项和截止日期",
     "purpose": "整理用户提供的材料，列出事项、负责人、截止日期与需要确认的问题。缺失信息应明确标注，不要编造。", "tools": []},
    {"id": "writer", "name": "写作助手", "description": "按主题和读者生成文稿",
     "purpose": "根据用户的目的和读者写出简洁清晰的中文文本。先给出可用稿，再列出需要确认的事实。", "tools": []},
    {"id": "memory", "name": "资料查询助手", "description": "检索已保存的资料，附来源",
     "purpose": "从已保存的资料中寻找与本次问题相关的信息，回答时标注来源。没有相关记录时明确说明，不编造。",
     "tools": ["memory.search"]},
]


def install_studio(app, hub):
    if getattr(app.state, "studio_api_installed", False):
        return
    app.state.studio_api_installed = True
    from .authoring import install_authoring
    from .http_tools import install_http_tools
    install_authoring(app, hub)
    install_http_tools(app, hub)
    from .node_library import install_node_library
    install_node_library(app, hub)
    from .model_catalog import install_model_catalog
    install_model_catalog(app, hub)
    from .component_packages import install_component_packages
    install_component_packages(app, hub)
    from .workflow_packages import install_workflow_packages
    install_workflow_packages(app, hub)

    @app.get("/v1/studio/templates")
    async def templates():
        return TEMPLATES

    def validate_assistant(body):
        if body.construction == "automatic":
            if any((body.tools, body.skills, body.knowledge, body.memory_namespaces, body.policy)):
                raise ValueError("自动构建由系统配置能力，无需手动选择工具或策略")
            if body.model != "auto" and body.model not in hub.models.bindings:
                raise ValueError("请连接模型，或使用自动选择")
            return
        if body.model not in hub.models.bindings:
            raise ValueError("请先连接所选模型")
        needed = "decision" if body.capability == "chat" and body.strategy == "plan_execute" else body.capability
        if needed not in hub.models.bindings[body.model].capabilities:
            raise ValueError("所选模型不支持这个助手的能力：" + needed)
        for tool in body.tools:
            hub.tools.spec(tool)
        for skill in body.skills:
            hub.skills.load(skill)
        if body.policy:
            policy = hub.evolution.active(body.policy)
            if not set(body.tools).issubset(policy["body"]["tools"]):
                raise PermissionError("所选工具超出已批准策略的权限")

    @app.post("/v1/studio/assistants", status_code=201)
    async def save_assistant(body: Assistant):
        validate_assistant(body)
        identifier = uuid.uuid4().hex
        hub.store.memory_put("studio-assistants", identifier, body.model_dump(), "studio")
        return {"id": identifier, **body.model_dump()}

    @app.get("/v1/studio/assistants")
    async def assistants():
        return [{"id": row["key"], **row["value"]} for row in hub.store.memory_search("studio-assistants", limit=1000)]

    @app.put("/v1/studio/assistants/{identifier}")
    async def update_assistant(identifier: str, body: Assistant):
        old = get_assistant(identifier)
        validate_assistant(body)
        hub.store.memory_put("studio-assistant-history", identifier + ":" + uuid.uuid4().hex, old, "studio-version")
        hub.store.memory_put("studio-assistants", identifier, body.model_dump(), "studio")
        return {"id": identifier, **body.model_dump()}

    def get_assistant(identifier):
        rows = hub.store.memory_search("studio-assistants", limit=1000)
        for row in rows:
            if row["key"] == identifier:
                return row["value"]
        raise KeyError(identifier)

    @app.post("/v1/studio/assistants/{identifier}/run")
    async def run_assistant(identifier: str, body: AssistantRun):
        assistant = get_assistant(identifier)
        if assistant.get("construction") == "automatic":
            workflow = compiled_workflow(hub, identifier, assistant)
            workflow.inputs["message"] = body.message
            if body.auto_repair:
                from .assistant_builder import stored
                plan = stored(hub, 'studio-assistant-plans', identifier)
                return hub.goals.create({'objective':assistant['purpose'], 'workflow':workflow, 'model':plan['model'],
                    'allowed_tools':[t['name'] for t in hub.available_tools() if not t['name'].startswith(('development.','code.','agents.','skills.save'))],
                    'allowed_models':[m['alias'] for m in hub.models.catalog() if m['alias']!='mock'],
                    'max_revisions':body.max_revisions,'allow_code':body.allow_code,'limits':assistant['limits']})
            return {"id": hub.submit(workflow)}
        config = AgentConfig(prompt=body.message, instructions=assistant["purpose"],
                             **{k: v for k, v in assistant.items() if k not in ("name", "purpose", "model", "capability", "limits", "construction")})
        workflow = Workflow(name=assistant["name"], limits=assistant.get("limits", {}), steps=[Step(id="answer", kind="agent", target=assistant["model"],
                                                              input=config.model_dump())])
        if assistant.get("capability", "chat") != "chat":
            workflow.steps = [Step(id="answer", kind="model", target=assistant["model"], input={
                "capability": assistant["capability"], "prompt": assistant["purpose"] + "\n" + body.message})]
        return {"id": hub.submit(workflow)}

    @app.post("/v1/studio/assistants/{identifier}/build", status_code=202)
    async def build_assistant(identifier: str):
        return start_build(hub, identifier, get_assistant(identifier))

    @app.get("/v1/studio/assistants/{identifier}/workflow")
    async def assistant_workflow(identifier: str):
        assistant = get_assistant(identifier)
        if assistant.get("construction") != "automatic":
            return {"status": "legacy", "message": "这个助手来自旧版单 Agent 配置。请按需求重新生成可见工作流。"}
        return build_status(hub, identifier, assistant)

    @app.get("/v1/studio/assistants/{identifier}/workflow.json")
    async def download_workflow(identifier: str):
        assistant = get_assistant(identifier)
        workflow = compiled_workflow(hub, identifier, assistant)
        return Response(json.dumps(workflow.model_dump(), ensure_ascii=False, indent=2),
            media_type="application/json", headers={"Content-Disposition": 'attachment; filename="workflow.json"'})

    @app.post("/v1/studio/workflows", status_code=201)
    async def save_workflow(workflow: Workflow):
        identifier = uuid.uuid4().hex
        return hub.development.save_workflow(identifier, workflow)

    @app.get("/v1/studio/workflows")
    async def workflows():
        return hub.development.workflows()

    @app.get("/v1/studio/workflows/{identifier}")
    async def saved_workflow(identifier: str, revision: int | None = None):
        return hub.development.get("workflow", identifier, revision)

    @app.get("/v1/studio/workflows/{identifier}/revisions")
    async def workflow_revisions(identifier: str):
        return hub.development.list_versions("workflow", identifier)

    @app.get("/v1/studio/workflows/{identifier}/workflow.json")
    async def saved_workflow_download(identifier: str, revision: int | None = None):
        saved = hub.development.get("workflow", identifier, revision)
        return Response(json.dumps(saved["workflow"], ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="workflow-v{saved["revision"]}.json"'})

    @app.put("/v1/studio/workflows/{identifier}")
    async def update_workflow(identifier: str, workflow: Workflow, expected_revision: int):
        hub.development.get("workflow", identifier)
        return hub.development.save_workflow(identifier, workflow, expected_revision)

    def connection_provider(body: Connection):
        from urllib.parse import urlparse
        parsed = urlparse(body.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("模型地址必须是有效的 HTTP(S) 地址，不能在地址中包含密码")
        if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("远程模型请使用 HTTPS")
        if not set(body.capabilities) <= {"chat", "decision", "image", "embedding"} or not body.capabilities:
            raise ValueError("未知模型能力")
        if body.dialect == "anthropic" and set(body.capabilities) - {"chat", "decision"}:
            raise ValueError("Anthropic Messages 适配器仅支持 chat 和 decision")
        if body.alias in hub.models.bindings:
            raise ValueError("这个连接名称已存在，请填写另一个名称")
        return HTTPProvider(body.base_url, body.api_key, body.dialect)

    async def test_provider(provider, model, capabilities):
        capability = next(c for c in ("chat", "decision", "embedding", "image") if c in capabilities)
        try:
            result = await provider.generate(ModelRequest(model=model, capability=capability,
                prompt="A simple green circle.", max_output_tokens=32), model)
        except ProviderError as exc:
            raise HTTPException(502, f"模型服务返回 HTTP {exc.status}，请检查服务状态、模型名称和访问凭证。") from exc
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "连接测试超时，请检查模型服务是否正常。") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "无法连接模型服务，请检查服务地址和网络。") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise HTTPException(502, "模型返回格式不符合所选接口，请检查接口类型和模型名称。") from exc
        return {"text": result.text or f"{capability} 返回 {len(result.images or result.embeddings)} 项结果", "usage": result.usage}

    @app.post("/v1/studio/connections")
    async def connection(body: Connection):
        provider = connection_provider(body)
        hub.connections.save_model(body,provider)
        return {"alias": body.alias, "message": "连接已保存，凭证加密存储，重启后自动恢复。"}

    @app.post("/v1/studio/connections/test-and-save")
    async def test_and_save_connection(body: Connection):
        provider = connection_provider(body)
        result = await test_provider(provider, body.model, body.capabilities)
        hub.connections.save_model(body,provider)
        return {"alias": body.alias, **result}

    @app.post("/v1/studio/connections/{alias}/test")
    async def test_connection(alias: str):
        binding = hub.models.bindings[alias]
        return await test_provider(binding.provider, binding.model, binding.capabilities)

    @app.get("/v1/studio/assistants/{identifier}/export")
    async def export(identifier: str):
        assistant = get_assistant(identifier)
        if assistant.get("construction") == "automatic":
            workflow = compiled_workflow(hub, identifier, assistant)
        else:
            # Legacy API clients retain their explicitly configured Agent workflow.
            config = AgentConfig(prompt="",
                instructions=assistant["purpose"], **{k: v for k, v in assistant.items()
                if k not in ("name", "purpose", "model", "capability", "limits", "construction")})
            workflow = Workflow(name=assistant["name"], inputs={"message": ""}, limits=assistant.get("limits", {}),
                steps=[Step(id="answer", kind="agent", target=assistant["model"],
                    input={**config.model_dump(), "prompt": {"$ref": "$input.message"}})])
            if assistant.get("capability", "chat") != "chat":
                workflow.steps = [Step(id="answer", kind="model", target=assistant["model"], input={
                    "capability": assistant["capability"], "prompt": {"$ref": "$input.message"}})]
        from .project_export import project_files
        files = project_files(hub, assistant, workflow)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="my-assistant.zip"'})
