"""Generic TypeSafe HTTP registration + durable decision + evidence/artifact checks."""
import copy

from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.http_tools import register_http_tool
from examples.demos.live_workflow import register_evidence_check, typesafe_definition


async def test_typesafe_decision_and_citation_gate_in_a_real_workflow(hub, monkeypatch):
    sources = [
        {"url": "https://www.gov.hk/en/address", "snippet": "Synthetic source: notify government departments after changing address."},
        {"url": "https://www.ird.gov.hk/eng/address", "snippet": "Synthetic source: update the correspondence address kept by the tax department."}]
    report = {"title": "合成验收", "items": [{"action": "核对地址通知", "url": s["url"], "evidence": s["snippet"]} for s in sources],
              "limitations": "Synthetic integration fixture only; not real government guidance."}
    remote, calls = FastAPI(), []
    @remote.post('/v1/systemone')
    async def evaluate(request: Request):
        assert request.headers['Authorization'] == 'Bearer synthetic-typesafe-key'
        body = await request.json()
        assert body['model'] == 'jev-latest'
        assert body['questions']['review']['type'] == 'choice'
        assert body['state']['sources'] == sources
        calls.append(body)
        return {'model': 'jev-test-fixture', 'answers': {'review': {
            'type': 'choice', 'choice': 'ready_for_review', 'probabilities': {'ready_for_review': .9, 'needs_revision': .1},
            'confidence': .8}}, 'usage': {'input_tokens': 100, 'output_tokens': 25}}
    monkeypatch.setenv('TYPESAFE_API_KEY', 'synthetic-typesafe-key')
    async with live_server(remote) as origin:
        register_http_tool(hub, typesafe_definition(origin + '/v1/systemone'))
        register_evidence_check(hub)
        workflow = {'name': 'typed decision through configured HTTP API', 'steps': [
            {'id': 'review', 'target': 'decision.typesafe', 'input': {
                'model': 'jev-latest', 'state': {'sources': sources, 'report': report}, 'questions': {'review': {
                    'type': 'choice', 'instructions': 'Evaluate the report',
                    'criteria': {'ready_for_review': 'Grounded', 'needs_revision': 'Unsupported'}}}}},
            {'id': 'verify', 'target': 'evidence.verify_report', 'depends_on': ['review'], 'input': {
                'sources': sources, 'report': report, 'review': {'$ref': 'review'}}},
            {'id': 'save', 'kind': 'artifact', 'depends_on': ['verify'], 'input': {
                'name': 'report.md', 'content': {'$ref': 'verify.text'}, 'media_type': 'text/markdown'}}]}
        run = await hub.wait(hub.submit(workflow))
        assert run['status'] == 'succeeded' and len(calls) == 1
        _, content = hub.artifacts.get(run['steps'][-1]['output']['id'])
        assert sources[0]['url'].encode() in content and b'synthetic-typesafe-key' not in content
        # A model approving the draft cannot bypass deterministic source verification.
        forged = copy.deepcopy(workflow)
        forged['steps'][1]['input']['report']['items'][0]['evidence'] = 'This unsupported quotation never appeared in a search snippet.'
        rejected = await hub.wait(hub.submit(forged))
        assert rejected['status'] == 'failed' and rejected['steps'][-1]['status'] == 'skipped'
        assert not hub.artifacts.list(rejected['id'])
