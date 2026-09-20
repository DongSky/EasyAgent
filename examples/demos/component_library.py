"""Opt-in live demo: two classifications, export, clean import, restart and reuse.

Usage: uv run python -m examples.demos.component_library --live --base-url http://127.0.0.1:8766
Reads TYPESAFE_API_KEY only in the fresh receiving Hub; writes no secret values.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from easyagent.client import HubClient
from easyagent.component_packages import import_package
from easyagent.runtime import Hub

CASES = [
    {'name': '售后工单分流', 'expected': 'support', 'input': {
        'state': '上周买的风扇无法启动，需要退货或维修，不咨询新产品购买。',
        'instructions': '判断用户是寻求售后处理还是售前购买咨询。',
        'criteria': {'support': '已购产品的故障、退换或维修', 'sales': '购买新产品的咨询'},
        'filename': 'support-receipt.json'}},
    {'name': '设备故障优先级', 'expected': 'urgent', 'input': {
        'state': '机房主电源已经断开，全部业务中断，需要立即排障。',
        'instructions': '判断设备事件的处理优先级。',
        'criteria': {'urgent': '业务中断需要立即处理', 'normal': '不影响业务的日常维护'},
        'filename': 'priority-receipt.json'}}]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def verify(run, expected):
    assert run['status'] == 'succeeded', run['status']
    result = run['steps'][0]['output']['results'][0]
    assert result['decision']['choice'] == expected, result['decision']
    return result


async def main(args):
    if not args.live:
        raise SystemExit('Pass --live to call the real classification API (potentially billable).')
    if not os.environ.get('TYPESAFE_API_KEY'):
        raise SystemExit('Set TYPESAFE_API_KEY in the server and this demo environment.')
    out = Path(args.output or '.eah/live-acceptance/library-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    out.mkdir(parents=True, exist_ok=False)
    report = {'real_service': 'TypeSafe Jev', 'cases': [], 'media_generation_tested': False}
    async with HubClient(args.base_url, timeout=120) as client:
        for case in CASES:
            instance = await client.instantiate('library.decision.classify_receipt', inputs=case['input'])
            created = await client.submit({'name': '节点库真实验收 · '+case['name'], 'steps': [instance['step']]})
            run = await client.wait(created['id'], timeout=120)
            result = verify(run, case['expected'])
            write(out/(case['expected']+'-run.json'), run)
            report['cases'].append({'task': case['name'], 'run':run['id'], 'decision':result['decision']})
        package = await client.export_component('library.decision.classify_receipt')
        write(out/'classification.eah-component.json', package)
        catalog = await client.request('POST', '/v1/studio/model-catalog/discover', {})
        report['catalog'] = {k:catalog[k] for k in ('model_count','operation_count','missing_protocol_count','connected')}
        write(out/'model-catalog.json', catalog)
    # This receiving installation starts with an empty database and a different credential alias.
    os.environ['EAH_LIBRARY_DEMO_KEY'] = os.environ['TYPESAFE_API_KEY']
    for iteration in range(2):
        hub = Hub(out/'receiving.db', poll_seconds=.02)
        await hub.start()
        try:
            if iteration == 0:
                imported = import_package(hub, {'package':package, 'credential_bindings': {'TYPESAFE_API_KEY':'EAH_LIBRARY_DEMO_KEY'}})
                assert imported['execution_started'] is False
                report['import'] = imported
            instance = hub.library.instantiate('library.decision.classify_receipt', {'input':CASES[iteration]['input']})
            run = await hub.wait(hub.submit({'name':'Clean receiving Hub' if not iteration else 'Restored receiving Hub',
                                            'steps':[instance['step']]}), timeout=120)
            result = verify(run, CASES[iteration]['expected'])
            artifact = result['receipt']['results'][0]['receipt']
            info, content = hub.artifacts.get(artifact['id'])
            (out/('imported-' + info['name'])).write_bytes(content)
            write(out/f'receiving-{iteration}.json', run)
            report['restart_passed' if iteration else 'clean_import_passed'] = True
        finally:
            await hub.stop()
    write(out/'report.json',report)
    print(json.dumps({'evidence':str(out.resolve()), **report}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--output')
    asyncio.run(main(parser.parse_args()))
