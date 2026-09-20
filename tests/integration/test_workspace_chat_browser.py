"""Browser-to-runtime acceptance for attachment dispatch and the live workflow view."""
import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from playwright.async_api import async_playwright, expect

from conftest import live_server
from easyagent.contracts import ToolSpec
from easyagent.models import HTTPProvider


async def test_nested_wait_labels_show_the_actual_blocker(api):
    from easyagent.contracts import ModelResult
    url, hub = api

    class Router:
        async def generate(self, request, model):
            return ModelResult(data={'action': 'use', 'candidate': 'nested@1', 'confidence': 1,
                                     'message': '执行已保存的流程。', 'inputs': {}})

    async def uncertain(args, ctx):
        raise RuntimeError('fixture: request sent, result unknown')

    hub.models.register('router', Router(), 'fixture', ['decision'])
    hub.tools.register(ToolSpec(name='fixture.write', effect='write', idempotent=False), uncertain)
    hub.development.save_workflow('nested', {'name': '嵌套状态', 'metadata': {'step_labels': {
        'edit': '逐张编辑原图', 'save': '保存关联清单'}}, 'steps': [
        {'id': 'edit', 'kind': 'foreach', 'input': {'items': [1]}, 'body': {'name': '单张处理', 'steps': [
            {'id': 'submit', 'target': 'fixture.write'}]}},
        {'id': 'save', 'kind': 'transform', 'depends_on': ['edit']}]})
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(url+'/#conversations')
            await page.locator('#conversations [data-execution]').select_option('confirm')
            await page.locator('#workspaceMessage').fill('运行嵌套流程')
            await page.locator('#conversations [data-send]').click()
            await expect(page.locator('[data-node="edit"] small')).to_have_text('子流程等待确认执行', timeout=15000)
            await expect(page.locator('[data-node="save"] small')).to_have_text('等待前置步骤：逐张编辑原图')
            await expect(page.locator('.chat-task-details')).to_contain_text('子流程等待确认执行')
            await page.locator('[data-approve]').click()
            await expect(page.locator('[data-node="edit"] small')).to_have_text('子流程结果不明，需核验', timeout=15000)
            await expect(page.locator('.chat-task-details')).to_contain_text('子流程结果不明，需核验')
            await page.reload()
            await expect(page.locator('[data-node="edit"] small')).to_have_text('子流程结果不明，需核验', timeout=15000)
        finally:
            await browser.close()


async def test_chat_upload_dispatch_graph_restore_canvas_and_mobile(api, tmp_path):
    url, hub = api
    remote = FastAPI()
    release = asyncio.Event()

    @remote.post('/chat/completions')
    async def model(request: Request):
        body = await request.json()
        context = json.loads(body['messages'][-1]['content'])
        choice = {'action': 'use', 'candidate': 'notice@1', 'confidence': .99, 'message': '这份通知可以交给已保存的材料整理流程。',
                  'inputs': {'reference_artifact': context['attachments'][0]['id']}}
        return {'choices': [{'message': {'content': json.dumps(choice, ensure_ascii=False)}}]}

    async def organize(args, ctx):
        await release.wait()
        return {'text': '## 待办事项\n\n- [ ] **提交回执**：' + args['text'] + '\n\n<img src=x onerror="window.chatInjected=true">'}

    hub.tools.register(ToolSpec(name='fixture.organize'), organize)
    hub.development.save_workflow('notice', {
        'name': '通知与材料整理', 'inputs': {'message': ''},
        'metadata': {'step_labels': {'read': '读取通知', 'organize': '整理行动清单', 'save': '保存清单'}},
        'steps': [{'id': 'read', 'target': 'attachments.read', 'input': {'artifact_id': {'$ref': '$input.reference_artifact'}}},
                  {'id': 'organize', 'target': 'fixture.organize', 'depends_on': ['read'], 'input': {'text': {'$ref': 'read.text'}}},
                  {'id': 'save', 'kind': 'artifact', 'depends_on': ['organize'], 'input': {'name': '行动清单.txt', 'content': {'$ref': 'organize.text'}}}]
    })
    async with live_server(remote) as endpoint, async_playwright() as playwright:
        hub.models.register('ui-protocol-fixture', HTTPProvider(endpoint, ''), 'fixture', ['decision', 'chat'])
        browser = await playwright.chromium.launch()
        evidence = Path(os.environ.get('EAH_UI_EVIDENCE', str(tmp_path)))
        evidence.mkdir(parents=True, exist_ok=True)
        context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='no-preference',
                                            record_video_dir=str(evidence/'video') if os.environ.get('EAH_UI_EVIDENCE') else None)
        page = await context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(url+'/#conversations')
            await expect(page.locator('#conversations')).to_be_visible()
            await expect(page.locator('#conversations [data-text]')).to_be_visible()
            await page.locator('#conversations [data-file-input]').set_input_files({'name': '学校通知.txt', 'mimeType': 'text/plain', 'buffer': '周五下午三点前提交家长回执，活动当天带水杯。'.encode()})
            await expect(page.locator('#conversations .chat-file small')).not_to_have_text('正在上传…')
            await page.locator('#workspaceMessage').fill('请整理这份学校通知，保存一份行动清单。')
            await page.locator('#conversations [data-send]').click()
            await expect(page.locator('.chat-task-heading')).to_contain_text('通知与材料整理', timeout=20000)
            await expect(page.locator('.chat-node')).to_have_count(3)
            await expect(page.locator('.chat-node.state-running')).to_have_count(1)
            flow = page.locator('.chat-edge.state-running .chat-edge-flow')
            await expect(flow).to_have_count(1)
            # Observe actual movement, not just the existence of an animation class.
            before = await flow.evaluate('(el)=>getComputedStyle(el).strokeDashoffset')
            await page.wait_for_function('(old)=>getComputedStyle(document.querySelector(".chat-edge.state-running .chat-edge-flow")).strokeDashoffset!==old', arg=before)
            await page.locator('.chat-node.state-running').evaluate('(el)=>window.activeNode=el')
            await page.screenshot(path=str(evidence/'chat_running.png'), full_page=True)
            await page.emulate_media(reduced_motion='reduce')
            await expect(flow).to_have_css('animation-name', 'none')
            await expect(page.locator('.chat-node.state-running .chat-node-indicator')).to_have_css('animation-name', 'none')
            assert await page.locator('.chat-node.state-running').evaluate('(el)=>getComputedStyle(el,"::after").animationName') == 'none'
            await page.emulate_media(reduced_motion='no-preference')
            # Polling must preserve the current node and its animation timeline.
            await page.wait_for_function('document.querySelector(".chat-node.state-running").getAnimations({subtree:true}).some(a=>a.currentTime>1000)')
            release.set()
            await expect(page.locator('.chat-task-heading')).to_contain_text('处理完成', timeout=20000)
            await expect(page.locator('.chat-node.state-succeeded')).to_have_count(3)
            assert await page.locator('[data-node="organize"]').evaluate('(el)=>el===window.activeNode')
            await expect(page.locator('.chat-edge.state-running')).to_have_count(0)
            await expect(page.locator('.chat-edge.state-succeeded')).to_have_count(2)
            await expect(page.locator('.chat-node.just-completed')).to_have_count(0)
            assert await page.locator('.chat-dag').evaluate('(el)=>el.getAnimations({subtree:true}).filter(a=>a.effect.getTiming().iterations===Infinity).length') == 0
            await expect(page.locator('.chat-history-item.active')).not_to_contain_text('正在处理')
            await expect(page.locator('.chat-result-message h4')).to_have_text('待办事项')
            await expect(page.locator('.chat-result-message strong')).to_have_text('提交回执')
            assert await page.locator('.chat-result-message img').count() == 0
            assert await page.evaluate('window.chatInjected === undefined')
            await expect(page.locator('.chat-result-file').filter(has_text='行动清单.txt')).to_be_visible()
            await page.reload()
            await expect(page.locator('.chat-task-heading')).to_contain_text('处理完成', timeout=15000)
            await page.locator('[data-details-toggle]').click()
            await expect(page.locator('.chat-task-details')).to_contain_text('周五下午三点')
            await page.locator('[data-details-toggle]').click()
            await page.screenshot(path=str(evidence/'chat_desktop.png'), full_page=True)
            await page.locator('[data-edit]').click()
            await expect(page.locator('#workflow')).to_be_visible()
            await expect(page.locator('#workflowName')).to_have_value('通知与材料整理')
            await expect(page.locator('.chat-launcher')).to_be_visible()
            await page.locator('.chat-launcher').click()
            await expect(page.locator('.chat-task-heading')).to_contain_text('处理完成')
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.locator('[data-history-toggle]').click()
            await expect(page.locator('.chat-history.mobile-open')).to_be_visible()
            await page.locator('.chat-history-item').first.click()
            await expect(page.locator('.chat-history.mobile-open')).to_have_count(0)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(evidence/'chat_mobile.png'), full_page=True)
            assert not errors, errors
        finally:
            release.set()
            await context.close()
            await browser.close()
