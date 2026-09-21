"""Live acceptance for the autonomous operator, against a real connected model.

Offline integration tests use scripted fixtures and prove runtime behaviour; this script
proves the real end-to-end path: a real model, the real search and media connections
configured in the workspace, real files on disk. It is never run by CI.

    uv run python scripts/live_acceptance.py --model gpt --task all
    uv run python scripts/live_acceptance.py --task terminal --request "写一个脚本统计字符数"

Every run writes a transcript to --output/<timestamp>/ so the evidence outlives the terminal,
and prints where it went. Failures are reported as failures: a task that did not complete is
recorded with its status and the underlying step error, never as a success.

SPDX-FileCopyrightText: 2026 EasyAgent contributors
SPDX-License-Identifier: AGPL-3.0-only
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from easyagent.runtime import Hub  # noqa: E402

# Each task is a realistic multi-step request. They are deliberately different in shape:
# writing a file, aggregating data, searching the web, delegating parallel research, and
# repairing a failed attempt from the observed error.
TASKS = {
    'terminal': (
        '在工作目录里创建一个 Python 脚本，统计下面这段文本的字符数并打印结果，然后告诉我文件路径。\n\n'
        '把复杂的问题拆成可以验证的小步骤，是让自动化可靠的关键。\n'),
    'aggregate': (
        '把下面这份销售数据按客户汇总金额，找出金额最高的客户，把结果写成文件，'
        '并保存成一个以后可以直接重复使用的流程。\n\n'
        'customer,amount\nAlice,120.5\nBob,80\nAlice,79.5\nCarol,45\nBob,20.5\n'),
    'research': (
        '联网查一下「最小可复现示例」为什么对调试很重要，给出你实际读过的来源链接，'
        '并用三句话总结，最后保存成一份 Markdown 文件。'),
    'swarm': (
        '帮我做一份「极简几何 Logo 风格调研」：\n'
        '1. 用联网搜索分别调研三种不同的极简几何方向，三种方向必须由三个并行的子任务各自完成；\n'
        '2. 每个子任务给出该方向的代表特征、适合的品牌气质，以及一段可直接使用的英文图像提示词；\n'
        '3. 然后为每种方向各生成一张真实的 Logo 示例图；\n'
        '4. 最后把三条调研结果与三张图的说明整理成一份 Markdown 报告保存成文件，并把整条流程保存为可复用流程。'),
    'repair': (
        '在工作目录里创建一个 Python 脚本读取 data.csv 并把第二列求和。'
        'data.csv 现在不存在，请先创建一份带表头的示例数据再运行，'
        '如果第一次运行失败，请根据实际报错修正后重跑，最后告诉我求和结果。'),
    # --- software automation, phrased as a person would ask for it ---
    'docs_adapter': (
        '读一下这份接口文档，按照它自己写明的参数和返回格式，给这个接口做一个可以复用的节点，'
        '然后真的调用一次验证它能用。文档地址我会在下一句给出。'),
    'batch_files': (
        '我上传了三份销售明细，请逐份处理，把每份的客户与金额汇总，'
        '再合并成一份总的汇总文件；如果其中某一份格式不对，单独说明是哪一份、哪里不对，'
        '不要把它悄悄跳过。'),
    # --- daily life butler, phrased as a person would ask for it ---
    'notice': (
        '把下面这份学校通知整理成行动清单，每项列出事项、负责人、截止时间、需准备物品和原文依据。'
        '通知里没写的（比如志愿者名单），标成待确认，不要编造。最后保存一份可以下载的清单。\n\n'
        '各位家长：本学期的社会实践活动已开始报名，请家长在 9 月 25 日前把填好的《社会实践报名表》'
        '交给班主任王老师。10 月 12 日上午 8:30 在社区活动中心集合，请给孩子准备水壶和运动鞋。'
        '本次活动需要若干家长志愿者，具体人选由家委会商量后另行通知。'),
    'receipts': (
        '这是我这个月的收据，我计划最多花 100 元。请核对：实际花了多少、还剩多少、有没有超支，'
        '把不能计入的（比如外币的、金额看不清的）单独列出来说明原因，最后存一份明细文件。'),
    'appointment': (
        '帮我跟牙科诊所约一次洗牙，约好之后告诉我时间。注意：诊所只会先给一个待确认的受理号，'
        '真正的时间要等诊所确认，确认之前不要当成约好了，也不要在我的日程里写死时间；'
        '诊所确认后，把时间和它的确认凭据一起记下来。'),
}


def summarise(hub, transcript, run_id):
    run = hub.store.run(run_id)
    transcript.update(
        run_status=run['status'], execution=run['execution'], approvals=len(run['approvals']),
        usage=run['usage'], children=run['children'],
        agent={'turns': (run['steps'][0]['output'] or {}).get('turns'),
               'tool_count': (run['steps'][0]['output'] or {}).get('tool_count'),
               'error': run['steps'][0]['error']},
    )
    with hub.store.connect() as db:
        rows = db.execute(
            'SELECT tool,status,arguments,error FROM invocations WHERE run_id=? ORDER BY rowid', (run_id,)
        ).fetchall()
    transcript['tool_calls'] = [
        {'tool': r['tool'], 'status': r['status'], 'arguments': r['arguments'][:600], 'error': r['error']}
        for r in rows
    ]
    transcript['events'] = [
        {'kind': e['kind'], 'payload': e['payload']}
        for e in hub.store.events(run_id, limit=400)
        if e['kind'].startswith(('tool.', 'definition.', 'media.', 'agent.', 'run.', 'input.', 'context.'))
    ]
    transcript['artifacts'] = hub.artifacts.list(run_id)
    subagents = []
    for child in run['children']:
        child_run = hub.store.run(child['id'])
        with hub.store.connect() as db:
            tools = [r[0] for r in db.execute(
                'SELECT DISTINCT tool FROM invocations WHERE run_id=?', (child['id'],))]
        subagents.append({'id': child['id'], 'name': child['name'], 'status': child_run['status'],
                          'tools': tools})
    transcript['subagents'] = subagents
    return run


async def run_task(hub, name, model, request, timeout, output):
    conversation = await hub.conversations.create(
        {'workspace': True, 'model': model, 'title': '实跑：' + name})
    sent = await hub.conversations.send(
        conversation['id'], {'text': request, 'intent': 'create', 'execution': 'automatic'})
    transcript = {'task': name, 'request': request, 'model': model, 'turn': sent['id']}
    print(f'[{name}] turn {sent["id"]} started', flush=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        await hub.conversations.tick()
        await asyncio.sleep(1)
        turn = hub.conversations.get(conversation['id'])['turns'][-1]
        task = turn.get('task') or {}
        if turn['status'] in ('succeeded', 'failed', 'cancelled') and task.get('phase') not in (
                'building', 'working', 'routing', 'executing'):
            break

    current = hub.conversations.get(conversation['id'])
    turn = current['turns'][-1]
    task = turn.get('task') or {}
    transcript.update(status=turn['status'], phase=task.get('phase'), run_id=task.get('run_id'),
                      selected=task.get('selected'),
                      message=current['messages'][-1]['content'] if current['messages'] else '')
    if task.get('run_id'):
        run = summarise(hub, transcript, task['run_id'])
        if task.get('selected'):
            try:
                transcript['saved_workflow'] = hub.development.get(
                    'workflow', task['selected']['id'], task['selected']['revision'])['workflow']
            except KeyError:
                pass
        transcript['task_memory'] = [r['value'] for r in hub.store.memory_search('tasks', limit=3)]
        if run['status'] != 'succeeded':
            transcript['failure'] = run['steps'][0]['error']
    transcript['user_memory'] = [{'key': r['key'], 'value': r['value']}
                                 for r in hub.store.memory_search('user', limit=10)]
    transcript['skills'] = [s['name'] for s in hub.skills.catalog() if s['name'].startswith('learned-')]
    if task.get('reflection_run'):
        try:
            reflected = await hub.wait(task['reflection_run'], timeout=180)
            transcript['reflection'] = {'status': reflected['status'],
                                        'error': reflected['steps'][0]['error']}
        except TimeoutError:
            transcript['reflection'] = {'status': 'timeout'}

    (output / f'{name}.json').write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding='utf-8')
    verdict = 'succeeded' if transcript.get('run_status') == 'succeeded' else 'FAILED'
    print(f'[{name}] {verdict}', flush=True)
    return transcript


async def main(args):
    output = Path(args.output) / time.strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=True)
    hub = Hub(args.database, poll_seconds=.2)
    await hub.start()
    results = {}
    try:
        catalog = {c['alias']: c for c in hub.connections.model_catalog()['connections']}
        if args.model not in catalog:
            raise SystemExit(f'{args.model} is not connected; connect a model first')
        if 'chat' not in catalog[args.model]['capabilities']:
            raise SystemExit(f'{args.model} has no chat capability; the operator needs tool calling')
        settings = hub.execution.settings()
        print(f'model={args.model} terminal={settings.terminal_enabled} workspace={settings.workspace}',
              flush=True)
        chosen = list(TASKS) if args.task == 'all' else [args.task]
        for name in chosen:
            request = args.request or TASKS[name]
            if name == 'docs_adapter':
                request += '\n\n文档地址：' + args.docs_url
            results[name] = await run_task(hub, name, args.model, request, args.timeout, output)
    finally:
        await hub.stop()

    print(json.dumps({name: {'status': r.get('status'), 'run': r.get('run_status'),
                             'selected': (r.get('selected') or {}).get('key'),
                             'subagents': len(r.get('subagents') or []),
                             'failure': r.get('failure')}
                      for name, r in results.items()}, ensure_ascii=False, indent=2), flush=True)
    print('evidence:', output, flush=True)
    (output / 'summary.json').write_text(
        json.dumps({name: {'status': r.get('status'), 'run_status': r.get('run_status')}
                    for name, r in results.items()}, ensure_ascii=False, indent=2), encoding='utf-8')
    failed = [name for name, r in results.items() if r.get('run_status') != 'succeeded']
    if failed:
        print('did not complete: ' + ', '.join(failed), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='.eah/hub.db',
                        help='workspace database holding the model/service connections')
    parser.add_argument('--model', default='gpt', help='connected model alias to run the tasks with')
    parser.add_argument('--task', default='all', choices=['all', *TASKS], help='which task to run')
    parser.add_argument('--request', default='', help='override the task text (with --task)')
    parser.add_argument('--timeout', type=float, default=1800, help='seconds allowed per task')
    parser.add_argument('--output', default='.eah/live-operator', help='where transcripts are written')
    parser.add_argument('--docs-url', default='', help='documentation URL for the docs_adapter task')
    asyncio.run(main(parser.parse_args()))
