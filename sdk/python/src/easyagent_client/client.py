# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

import httpx


class HubClient:
    @classmethod
    def from_env(cls):
        return cls(os.environ.get('EAH_URL', 'http://127.0.0.1:8765'), os.environ.get('EAH_TOKEN', ''))

    def workflow(self, identifier, *, revision=None):
        from .workflows import WorkflowHandle
        return WorkflowHandle(self, identifier, revision)

    def run_handle(self, identifier):
        from .workflows import RunHandle
        return RunHandle(self, identifier)

    async def upload_file(self, filename):
        from .workflows import upload_file
        return await upload_file(self, filename)

    def __init__(self, base_url="http://127.0.0.1:8765", token="", timeout=30):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": "Bearer " + token} if token else {},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def request(self, method, path, body=None, *, idempotency_key=None):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        response = await self.http.request(method, path, json=body, headers=headers)
        response.raise_for_status()
        return response.json()

    async def skills(self):
        return await self.request('GET', '/v1/skills')

    async def install_skill(self, package, expected_revision=0):
        return await self.request('POST', '/v1/skill-packages/install', {'package':package,'expected_revision':expected_revision})

    async def skill_source(self, repository, path='', ref='HEAD'):
        return await self.request('POST', '/v1/skill-packages/source', {'repository':repository,'path':path,'ref':ref})

    async def create_goal(self, objective, workflow, model, **options):
        return await self.request('POST','/v1/goals',{'objective':objective,'workflow':workflow,'model':model,**options})

    async def goal(self, identifier):
        return await self.request('GET','/v1/goals/'+quote(identifier,safe=''))

    async def control_goal(self, identifier, action, feedback=''):
        return await self.request('POST','/v1/goals/'+quote(identifier,safe='')+'/control',{'action':action,'feedback':feedback})

    async def extensions(self):
        return await self.request("GET", "/v1/extensions")

    async def install_extension(self, package, *, grants=None, trust_digest=None, preview=False):
        return await self.request(
            "POST",
            "/v1/extensions/" + ("preview" if preview else "install"),
            {"package": package, "grants": grants or [], "trust_digest": trust_digest},
        )

    async def extension_command(self, name, arguments=None, *, conversation_id=None):
        return await self.request(
            "POST",
            "/v1/extensions/commands/" + quote(name, safe=""),
            {"input": arguments or {}, "conversation_id": conversation_id},
        )

    async def create_conversation(self, model, title="新对话", **agent):
        return await self.request(
            "POST", "/v1/conversations", {"model": model, "title": title, "agent": {"prompt": "", **agent}}
        )

    async def send_message(self, conversation, text, *, mode="follow_up", idempotency_key=None):
        body = {"text": text, "mode": mode}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return await self.request(
            "POST", "/v1/conversations/" + quote(conversation, safe="") + "/messages", body
        )

    async def library(self):
        return await self.request("GET", "/v1/library")

    async def export_workflow(self, workflow):
        if hasattr(workflow, "model_dump"):
            workflow = workflow.model_dump()
        return await self.request("POST", "/v1/workflow-packages/export", {"workflow": workflow})

    async def import_workflow(self, package, *, preview=False, **bindings):
        return await self.request(
            "POST",
            "/v1/workflow-packages/" + ("preview" if preview else "import"),
            {"package": package, **bindings},
        )

    async def export_component(self, component_id, revision=None):
        suffix = f"?revision={revision}" if revision is not None else ""
        return await self.request("GET", "/v1/library/" + quote(component_id, safe="") + "/package" + suffix)

    async def import_component(self, package, credential_bindings=None, *, preview=False):
        return await self.request(
            "POST",
            "/v1/library/packages/" + ("preview" if preview else "import"),
            {"package": package, "credential_bindings": credential_bindings or {}},
        )

    async def instantiate(self, component_id, *, revision=None, step_id="component", inputs=None):
        return await self.request(
            "POST",
            "/v1/library/" + quote(component_id, safe="") + "/instantiate",
            {"revision": revision, "step_id": step_id, "input": inputs},
        )

    async def submit(self, workflow, idempotency_key=None):
        if hasattr(workflow, "model_dump"):
            workflow = workflow.model_dump()
        return await self.request("POST", "/v1/runs", workflow, idempotency_key=idempotency_key)

    async def run(self, run_id):
        return await self.request("GET", "/v1/runs/" + quote(run_id, safe=""))

    async def cancel(self, run_id):
        return await self.request("POST", "/v1/runs/" + quote(run_id, safe="") + "/cancel")

    async def approve(self, invocation_id, approved=True):
        return await self.request(
            "POST", "/v1/approvals/" + quote(invocation_id, safe=""), {"approved": approved}
        )

    async def respond(self, request_id, values):
        return await self.request("POST", "/v1/inputs/" + quote(request_id, safe=""), values)

    async def events(self, run_id, after=0):
        return await self.request("GET", "/v1/runs/" + quote(run_id, safe="") + f"/events?after={after}")

    async def wait(self, run_id, timeout=30):
        async with asyncio.timeout(timeout):
            while True:
                run = await self.run(run_id)
                if run["status"] in (
                    "succeeded",
                    "failed",
                    "cancelled",
                    "waiting_approval",
                    "waiting_input",
                    "needs_attention",
                ):
                    return run
                await asyncio.sleep(0.1)
