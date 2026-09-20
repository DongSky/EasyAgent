import copy
import json
import os

import httpx
import pytest
from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.api import create_app
from easyagent.component_packages import export_package, import_package
from easyagent.components import digest
from easyagent.runtime import Hub
from easyagent.studio import install_studio
from test_interop import subprocess_output


def rehash(package):
    for row in package['definitions']:
        row['digest'] = digest(row['body'])
    package['digest'] = digest({k:v for k,v in package.items() if k != 'digest'})
    return package


async def test_export_import_dependency_bundle_across_clean_hubs(api, tmp_path, monkeypatch):
    url, source = api
    requests = []
    remote = FastAPI()
    @remote.post('/classify')
    async def classify(request: Request):
        requests.append(request.headers['authorization'])
        body = await request.json()
        assert body['questions']['result']['criteria'] == {'yes':'需要处理', 'no':'无需处理'}
        return {'model':'fixture', 'usage':{}, 'answers':{'result':{
            'type':'choice','choice':'yes','confidence':.9,'probabilities':{'yes':.9,'no':.1}}}}
    monkeypatch.setenv('TYPESAFE_API_KEY','source-secret')
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        source.library.install('library.typesafe.evaluate', {'endpoint':origin+'/classify'})
        response = await client.get('/v1/library/library.decision.classify_receipt/package')
        assert response.status_code == 200, response.text
        package = response.json()
        assert 'source-secret' not in response.text
        assert {r['kind'] for r in package['definitions']} == {'api','workflow','component'}
        target = Hub(tmp_path/'clean.db', poll_seconds=.01)
        app = create_app(target)
        install_studio(app,target)
        monkeypatch.delenv('TYPESAFE_API_KEY')
        monkeypatch.setenv('MY_DECISION_KEY','destination-secret')
        request = {'package':package,'credential_bindings':{'TYPESAFE_API_KEY':'MY_DECISION_KEY'}}
        async with live_server(app) as target_url, httpx.AsyncClient(base_url=target_url) as destination:
            preview = await destination.post('/v1/library/packages/preview',json=request)
            assert preview.status_code == 200, preview.text
            assert preview.json()['execution_started'] is False and not requests
            # Use a real JS client: JSON.parse/stringify changes integral floats to integers.
            package_path = tmp_path/'component.json'
            package_path.write_text(json.dumps(package))
            code = '''import {readFile} from 'node:fs/promises';
import {HubClient} from './sdk/javascript/index.js';
const client=new HubClient(process.env.EAH_URL);
const packageData=JSON.parse(await readFile(process.env.EAH_PACKAGE_FILE,'utf8'));
console.log(JSON.stringify(await client.importComponent(packageData,{TYPESAFE_API_KEY:'MY_DECISION_KEY'})));
'''
            result = json.loads(await subprocess_output(['node','--input-type=module','-e',code],
                {**os.environ,'EAH_URL':target_url,'EAH_PACKAGE_FILE':str(package_path)}))
            assert result['imported'] and result['execution_started'] is False
            imported = await destination.post('/v1/library/packages/import',json=request)
            assert imported.status_code == 201, imported.text
            # Idempotent import does not add new versions or start API calls.
            again = await destination.post('/v1/library/packages/import',json=request)
            assert again.status_code == 201 and not requests
            instance = target.library.instantiate('library.decision.classify_receipt', {'input': {
                'state':'设备故障', 'instructions':'是否需要处理', 'criteria':{'yes':'需要处理','no':'无需处理'}, 'filename':'result.json'}})
            run = await target.wait(target.submit({'name':'Portable consumer', 'steps':[instance['step']]}))
            assert run['status'] == 'succeeded', run
            assert run['steps'][0]['output']['results'][0]['decision']['choice'] == 'yes'
            assert requests == ['Bearer destination-secret']
            assert all(r.get('imported') for r in target.library.catalog() if r['installed'])
            # A package cannot silently rebind an existing installed revision.
            bad = await destination.post('/v1/library/packages/import',json={**request,'credential_bindings':{'TYPESAFE_API_KEY':'ANOTHER_KEY'}})
            assert bad.status_code == 409
            assert len(target.development.list_versions('api')) == 1
        restored = Hub(tmp_path/'clean.db', poll_seconds=.01)
        await restored.start()
        try:
            second = await restored.wait(restored.submit({'name':'After restart', 'steps':[instance['step']]}))
            assert second['status'] == 'succeeded' and requests[-1] == 'Bearer destination-secret'
        finally:
            await restored.stop()


async def test_bundle_tampering_incomplete_dependencies_and_no_partial_import(hub, tmp_path):
    hub.library.install_local()
    package = export_package(hub, 'library.json.receipt').model_dump()
    clean = Hub(tmp_path/'reject.db')
    for mutation in ['digest','body','dependency','permissions']:
        bad = copy.deepcopy(package)
        if mutation == 'digest':
            bad['digest'] = '0'*64
        elif mutation == 'body':
            bad['definitions'][0]['body']['name'] = 'corrupted'
        elif mutation == 'dependency':
            bad['definitions'] = [r for r in bad['definitions'] if r['kind'] != 'node']
            rehash(bad)
        else:
            for row in bad['definitions']:
                if row['kind'] == 'component':
                    row['body']['requirements']['capabilities'] = []
            rehash(bad)
        with pytest.raises(ValueError):
            import_package(clean, {'package':bad})
        assert not clean.development.list_versions('component')
        assert not clean.development.list_versions('workflow')
    import_package(clean, {'package':package})
    assert clean.store.memory_search('component-imports')[0]['value']['unsigned'] is True


async def test_legacy_manifest_upgrade_preserves_executable_versions(tmp_path):
    import hashlib
    from easyagent.store import encode
    source = Hub(tmp_path/'legacy.db')
    saved = source.development.save_api({'name':'legacy.api', 'description':'legacy read',
        'url':'http://127.0.0.1:1/', 'timeout_seconds':2.0})
    manifest = source.library.publish({'id':'legacy.component', 'kind':'api', 'source_id':'legacy.api',
        'source_revision':saved['revision'], 'title':'Legacy', 'description':'Pre-release digest'})
    raw = manifest.model_dump()
    raw['source']['digest'] = hashlib.sha256(encode(saved['definition']).encode()).hexdigest()
    assert raw['source']['digest'] != manifest.source.digest
    with source.store.transaction() as db:
        db.execute("UPDATE definition_versions SET body=? WHERE kind='component' AND id=?", (encode(raw),manifest.id))
    restored = Hub(tmp_path/'legacy.db')
    assert restored.library.get(manifest.id).revision == 2
    assert restored.library.get(manifest.id).source.revision == 1
    assert restored.library.get(manifest.id,1).source.digest == raw['source']['digest']
    with pytest.raises(ValueError,match='legacy'):
        export_package(restored,manifest.id,1)
    package = export_package(restored,manifest.id).model_dump()
    script = 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>process.stdout.write(JSON.stringify(JSON.parse(s))))'
    # Use the same JS representation through the already-tested SDK transport above.
    import asyncio
    proc = await asyncio.create_subprocess_exec('node','-e',script,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
    output, _ = await proc.communicate(json.dumps(package).encode())
    assert proc.returncode == 0
    clean = Hub(tmp_path/'clean-legacy.db')
    assert import_package(clean,{'package':json.loads(output)})['imported']
    again = Hub(tmp_path/'legacy.db')
    assert again.library.get(manifest.id).revision == 2
