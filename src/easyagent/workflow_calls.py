"""Invoke saved workflows through a stable business-input/output contract."""
from __future__ import annotations

import hashlib
import uuid

from fastapi import Header, Query
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import Field

from .contracts import Contract, Workflow
from .results import run_result
from .store import Conflict, encode
from .workspace_chat import input_contract


class WorkflowCall(Contract):
    inputs: dict = Field(default_factory=dict)
    revision: int | None = Field(default=None, ge=0)


def install_workflow_calls(app, hub):
    with hub.store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS workflow_calls(key TEXT PRIMARY KEY,request TEXT NOT NULL,spec TEXT NOT NULL)')

    @app.get('/v1/workflows/{identifier}')
    async def get_workflow(identifier: str, revision: int | None = Query(default=None, ge=0)):
        return hub.development.get('workflow', identifier, revision)

    @app.put('/v1/workflows/{identifier}')
    async def save_workflow(identifier: str, workflow: Workflow, expected_revision: int = Query(default=0, ge=0)):
        return hub.development.save_workflow(identifier, workflow, expected_revision)

    @app.post('/v1/workflows/{identifier}/runs', status_code=201)
    async def start_workflow(identifier: str, body: WorkflowCall,
                             idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
        key = idempotency_key or uuid.uuid4().hex
        request = encode({'workflow': identifier, **body.model_dump()})
        with hub.store.connect() as db:
            old = db.execute('SELECT request,spec FROM workflow_calls WHERE key=?', (key,)).fetchone()
        if not old:
            saved = hub.development.get('workflow', identifier, body.revision)
            flow = saved['workflow']
            flow['inputs'] = {**flow.get('inputs', {}), **body.inputs}
            Draft202012Validator(input_contract(flow), format_checker=FormatChecker()).validate(flow['inputs'])
            flow.setdefault('metadata', {})['workflow_source'] = {'id': identifier, 'revision': saved['revision']}
            spec = encode(hub.prepare(flow).model_dump())
            # Persist the exact version before submission. Retries after a crash,
            # concurrent request or later workflow edit use this same snapshot.
            with hub.store.transaction() as db:
                db.execute('INSERT OR IGNORE INTO workflow_calls VALUES(?,?,?)', (key, request, spec))
                old = db.execute('SELECT request,spec FROM workflow_calls WHERE key=?', (key,)).fetchone()
        if old['request'] != request:
            raise Conflict('idempotency key was used with different workflow inputs or revision')
        workflow = Workflow.model_validate_json(old['spec'])
        internal_key = 'workflow-call:' + hashlib.sha256(key.encode()).hexdigest()
        run_id = hub.store.submit(workflow, internal_key)
        return {'id': run_id, 'status': hub.store.run(run_id)['status'], 'workflow_id': identifier,
                'revision': workflow.metadata['workflow_source']['revision']}

    @app.get('/v1/runs/{run_id}/result')
    async def result(run_id: str):
        return run_result(hub, run_id)
