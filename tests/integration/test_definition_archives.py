"""Workspace cleanup must hide experiments without breaking pinned consumers."""
import httpx
import pytest
from fastapi import FastAPI

from conftest import live_server
from easyagent.api import create_app
from easyagent.runtime import Hub
from easyagent.studio import install_studio


async def test_archived_api_and_subworkflow_survive_restart_and_pinned_execution(tmp_path):
    service = FastAPI()
    calls = []

    @service.get('/lookup')
    async def lookup():
        calls.append('lookup')
        return {'result': 'kept'}

    async with live_server(service) as url:
        database = tmp_path / 'workspace.db'
        original = Hub(database)
        original.development.save_api({'name': 'experiment.lookup', 'description': 'Fixture lookup',
                                       'url': url + '/lookup'})
        original.development.save_workflow('experiment.flow', {
            'name': 'Experiment', 'steps': [{'id': 'lookup', 'target': 'experiment.lookup'}],
        })
        original.library.publish({'id': 'experiment.component', 'kind': 'workflow',
            'source_id': 'experiment.flow', 'source_revision': 1, 'title': 'Experiment', 'description': ''})
        original.development.save_workflow('useful.flow', {'name': 'Keep this consumer', 'steps': [
            {'id': 'child', 'kind': 'subworkflow', 'workflow_ref': {'id': 'experiment.flow', 'revision': 1}},
        ]})
        # Already-submitted work also keeps the original API and workflow versions.
        run_id = original.submit(original.development.get('workflow', 'useful.flow')['workflow'])
        for kind, identifier in [('api', 'experiment.lookup'), ('workflow', 'experiment.flow'),
                                  ('component', 'experiment.component')]:
            original.development.set_archived(kind, identifier, reason='Superseded acceptance fixture')
        assert calls == []
        assert 'experiment.lookup' not in {t['name'] for t in original.tools.catalog()}
        assert original.tools.spec('experiment.lookup', 1).name == 'experiment.lookup'
        await original.stop()

        restored = Hub(database, poll_seconds=.01)
        app = create_app(restored, manage_workers=False)
        install_studio(app, restored)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            assert [r['id'] for r in (await client.get('/v1/studio/workflows')).json()] == ['useful.flow']
            assert [r['id'] for r in restored.chat.public_catalog()] == ['useful.flow']
            for endpoint in ['/v1/studio/apis', '/v1/library', '/v1/library/sources', '/v1/tools']:
                response = await client.get(endpoint)
                assert response.status_code == 200
                assert not any((r.get('id') or r.get('name', '')).startswith('experiment.') for r in response.json())
            assert (await client.get('/v1/studio/workflows/experiment.flow/revisions')).json()[0]['revision'] == 1
            assert (await client.get('/v1/library/experiment.component')).status_code == 200
            assert (await client.get('/v1/studio/apis/experiment.lookup?revision=1')).status_code == 200
        await restored.start()
        try:
            result = await restored.wait(run_id)
            assert result['status'] == 'succeeded' and calls == ['lookup']
            assert len(result['children']) == 1
        finally:
            await restored.stop()
        for kind, identifier in [('api', 'experiment.lookup'), ('workflow', 'experiment.flow'),
                                  ('component', 'experiment.component')]:
            restored.development.set_archived(kind, identifier, False)
        assert 'experiment.lookup' in {t['name'] for t in restored.tools.catalog()}
        assert len(restored.development.workflows()) == 2
        assert 'experiment.component' in {r['id'] for r in restored.library.catalog()}


async def test_archive_node_recipe_and_legacy_workflow_without_changing_versions(tmp_path):
    hub = Hub(tmp_path / 'workspace.db')
    hub.library.install_local()
    node_before = hub.development.get('node', 'library.json.receipt')
    hub.store.memory_put('studio-workflows', 'legacy', {'name': 'Legacy', 'steps': [
        {'id': 'echo', 'target': 'core.echo'},
    ]})
    for kind, identifier in [('node', 'library.json.receipt'), ('component', 'library.json.receipt'),
                              ('workflow', 'legacy')]:
        hub.development.set_archived(kind, identifier)
        hub.development.set_archived(kind, identifier)  # Idempotent; no new definition revision.
    assert not hub.development.workflows()
    assert not hub.chat.public_catalog()
    assert not hub.development.list_versions('node', include_archived=False)
    assert hub.development.get('node', 'library.json.receipt') == node_before
    assert hub.development.get('workflow', 'legacy')['revision'] == 0
    assert 'library.json.receipt' not in {r['id'] for r in hub.library.catalog()}
    assert hub.library.instantiate('library.json.receipt')['step']['kind'] == 'artifact'
    hub.development.set_archived('workflow', 'legacy', False)
    assert hub.development.workflows()[0]['id'] == 'legacy'
    with pytest.raises(KeyError):
        hub.development.set_archived('workflow', 'missing')
    with pytest.raises(ValueError):
        hub.development.set_archived('unknown', 'legacy')
    await hub.stop()
