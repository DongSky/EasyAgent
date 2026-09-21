import { modelChoices } from './model-choice.js';
import { searchSettings } from './search-settings.js?v=20260920-copy-2';
import { systemNotifications } from './system-notifications.js?v=20260920-copy-2';
export function connectionStudio({ api, escape, flash, showTab, load }) {
  function limitLabel(m) {
    const l = m.limits || {};
    if (!m.capabilities.some((c) => ['chat', 'decision'].includes(c))) return '';
    return l.context_window
      ? '上下文：' +
          l.context_window.toLocaleString() +
          ' tokens · ' +
          (l.source === 'provider_error' ? '服务反馈识别' : '服务元数据识别')
      : l.max_input_tokens
        ? '输入上限：' + l.max_input_tokens.toLocaleString() + ' tokens · 自动识别'
        : '上下文上限：服务未提供，超限时自动压缩';
  }
  const nav = document.createElement('button');
  nav.dataset.tab = 'connections';
  nav.textContent = '服务连接';
  document.querySelector('.nav').append(nav);
  const page = document.createElement('section');
  page.id = 'connections';
  page.className = 'section';
  page.innerHTML = `<div class="intro"><h1>模型与服务</h1></div><div class="actions"><button data-refresh>刷新状态</button></div><div class="panel" style="margin:18px 0"><h2>保存访问凭证</h2><form data-secret><label>凭证名称<input name="name" required placeholder="travel_service"></label><label>API Key / Token<input name="value" type="password" required autocomplete="off"></label><button>加密保存</button></form><p data-secrets class="muted"></p></div><div class="panel"><h2>通知与日历</h2><form data-connector><div class="node-grid"><label>连接名称<input name="id" required pattern="[a-z][a-z0-9_]{1,40}" placeholder="home_calendar"></label><label>显示名称<input name="title" required></label><label>服务类型<select name="kind"><option value="system">系统通知（当前设备）</option><option value="webhook">通用通知 Webhook</option><option value="telegram">Telegram</option><option value="caldav">CalDAV 日历</option><option value="slack">Slack 消息</option><option value="discord">Discord Webhook</option><option value="feishu">飞书 Webhook</option></select></label><label>服务地址<input name="url" type="url" required placeholder="https://..."></label><label>访问凭证<select name="credential"></select></label><label data-recipient hidden>收件人 / Chat ID<input name="recipient"></label></div><label><input name="idempotent" type="checkbox">服务支持按 Idempotency-Key 去重，可安全重试</label><button type="submit">保存连接</button></form><div data-connectors></div></div><div class="panel" style="margin-top:18px"><h2>MCP 服务</h2><form data-mcp><label>连接名称<input name="id" required pattern="[a-z][a-z0-9_]{1,40}"></label><label>服务地址<input name="url" type="url" required></label><label><input name="oauth" type="checkbox">使用浏览器登录授权</label><button>连接并发现能力</button></form><div data-mcp-list></div></div><div class="panel" style="margin-top:18px"><h2>投递回执</h2><div data-deliveries></div></div>`;
  document.querySelector('main').append(page);
  const $ = (s) => page.querySelector(s),
    guard = (fn) => async (e) => {
      try {
        await fn(e);
      } catch (err) {
        flash(err.message);
      }
    };
  const notifications = systemNotifications({
    form: $('[data-connector]'),
    api,
    flash,
    refresh: () => refresh(),
  });
  async function refresh() {
    const [secrets, connections, deliveries, mcp] = await Promise.all([
      api('/v1/connections/credentials'),
      api('/v1/connections'),
      api('/v1/connections/deliveries'),
      api('/v1/mcp/connections'),
    ]);
    $('[data-secrets]').textContent = secrets.length
      ? '已保存：' + secrets.map((s) => s.name).join(' · ')
      : '尚未保存凭证。模型连接也可以直接填写密钥。';
    const previousCredential = $('[name=credential]').value;
    $('[name=credential]').innerHTML =
      '<option value="">无需凭证</option>' +
      secrets.map((s) => `<option>${escape(s.name)}</option>`).join('');
    $('[name=credential]').value = previousCredential;
    $('[data-connectors]').innerHTML =
      connections
        .map(
          (c) =>
            `<div class="saved-row"><div><b>${escape(c.title)}</b><div class="muted">${c.kind === 'system' ? '系统通知 · 接收浏览器需保持页面打开' : escape(c.kind) + ' · ' + escape(c.url)}</div></div>${c.kind === 'system' ? '<button type="button" class="small" data-test-system="' + escape(c.id) + '">发送测试通知</button>' : ''}</div>`
        )
        .join('') || '<p class=muted>还没有通知或日历连接。</p>';
    notifications.bindSaved($('[data-connectors]'), connections);
    $('[data-deliveries]').innerHTML =
      deliveries
        .map(
          (d) =>
            `<p><b>${escape(d.connector)}</b> · ${{ queued: d.kind === 'system' ? '等待接收浏览器上线并允许通知' : '等待投递', sending: '正在发送', submitted: '已提交系统（未确认显示或已读）', delivered: '已送达', failed: '投递失败', uncertain: '需核验实际送达' }[d.status] || escape(d.status)} · ${d.attempts} 次尝试${d.error ? ' · ' + escape(d.error) : ''}</p>`
        )
        .join('') || '<p class="muted">暂无投递记录。</p>';
    $('[data-mcp-list]').replaceChildren();
    for (const c of mcp) {
      const card = document.createElement('article');
      card.className = 'panel';
      card.innerHTML = `<h3>${escape(c.id)} · ${escape({ ready: '已连接', connected: '已连接', connecting: '连接中', error: '连接失败', disconnected: '未连接', authorization_required: '等待授权', healthy: '正常', failed: '连接失败' }[c.health.status] || c.health.status)}</h3>${c.error ? '<p>' + escape(c.error) + '</p>' : ''}<button data-reload>重新连接 / 刷新能力</button><div data-auth></div><details><summary>管理助手可用的能力</summary><form data-permissions></form></details>`;
      if (c.authorization_url) {
        const a = document.createElement('a');
        a.href = c.authorization_url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = '打开服务授权页面';
        card.querySelector('[data-auth]').append(a);
      }
      card.querySelector('[data-reload]').onclick = guard(async () => {
        await api(`/v1/mcp/connections/${c.id}/reload`, 'POST', {});
        flash('正在重新连接，稍后点击“刷新状态”查看结果');
      });
      const form = card.querySelector('[data-permissions]');
      for (const t of c.available_tools) {
        const label = document.createElement('label');
        label.textContent = t.name + ' · ' + (t.description || '');
        const select = document.createElement('select');
        select.dataset.tool = t.name;
        [
          ['', '不开放给助手'],
          ['read', '只读（确认该工具无外部副作用）'],
          ['write', '外部操作，逐次确认'],
          ['idempotent', '可幂等重试的外部操作，逐次确认'],
        ].forEach(([v, text]) => select.append(new Option(text, v)));
        const risk = c.profile.permissions[t.name];
        select.value =
          risk?.effect === 'read' ? 'read' : risk?.idempotent ? 'idempotent' : risk ? 'write' : '';
        label.append(select);
        form.append(label);
      }
      if (c.available_tools.length) {
        const button = document.createElement('button');
        button.textContent = '保存能力权限';
        form.append(button);
        form.onsubmit = guard(async (e) => {
          e.preventDefault();
          const permissions = {};
          form.querySelectorAll('[data-tool]').forEach((s) => {
            if (s.value)
              permissions[s.dataset.tool] = {
                effect: s.value === 'read' ? 'read' : 'write',
                idempotent: s.value === 'read' || s.value === 'idempotent',
              };
          });
          await api('/v1/mcp/connections', 'POST', { ...c.profile, permissions });
          flash('连接权限已保存');
          await load();
          await refresh();
        });
      }
      $('[data-mcp-list]').append(card);
    }
  }
  nav.onclick = guard(async () => {
    showTab('connections');
    await refresh();
  });
  $('[data-refresh]').onclick = guard(refresh);
  $('[data-secret]').onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await api('/v1/connections/credentials/' + encodeURIComponent(f.get('name')), 'PUT', {
      value: f.get('value'),
    });
    e.target.reset();
    flash('凭证已加密保存');
    await refresh();
  });
  $('[name=kind]').onchange = (e) => {
    $('[data-recipient]').hidden = !['telegram', 'slack'].includes(e.target.value);
    if (e.target.value === 'telegram') $('[name=url]').value = 'https://api.telegram.org';
    if (e.target.value === 'slack')
      $('[name=url]').value = 'https://slack.com/api/chat.postMessage';
    notifications.update();
  };
  $('[data-connector]').onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    if (f.get('kind') === 'system') {
      const buttons = [...e.target.querySelectorAll('button')];
      buttons.forEach((b) => (b.disabled = true));
      try {
        await notifications.save(!!e.submitter?.hasAttribute('data-test-notification'));
        await load();
        await refresh();
      } finally {
        buttons.forEach((b) => (b.disabled = false));
        notifications.update();
      }
      return;
    }
    await api('/v1/connections', 'POST', {
      id: f.get('id'),
      title: f.get('title'),
      kind: f.get('kind'),
      url: f.get('url'),
      credential: f.get('credential') || null,
      recipient: f.get('recipient') || null,
      idempotent: f.has('idempotent'),
    });
    flash('连接已保存');
    await load();
    await refresh();
  });
  $('[data-mcp]').onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await api('/v1/mcp/connections', 'POST', {
      id: f.get('id'),
      url: f.get('url'),
      oauth: f.has('oauth'),
      redirect_uri: location.origin + '/v1/mcp/oauth/callback',
      permissions: {},
    });
    flash('连接已提交；点击“刷新状态”发现能力后，选择允许助手使用的项目');
    await refresh();
  });
  // Forms refresh only on navigation or explicit action, never while the user is editing.

  const modelArea = document.createElement('div');
  modelArea.className = 'connection-models';
  modelArea.innerHTML =
    '<div class=actions><h2>已连接模型</h2><button data-add-model>添加模型</button></div><label>默认处理模型<select data-default-model></select></label><p class=muted>用于选择“自动”的新任务；每个对话和助手也可以指定模型。</p><div data-models></div>';
  modelArea.querySelector('[data-add-model]').onclick = () =>
    document.getElementById('connectBtn').click();
  page.querySelector('.intro').after(modelArea);
  const grid = document.createElement('div');
  grid.className = 'service-grid';
  page.append(grid);
  for (const formSelector of ['[data-secret]', '[data-connector]', '[data-mcp]']) {
    const form = $(formSelector),
      panel = form.closest('.panel'),
      details = document.createElement('details');
    details.className = 'panel';
    const heading = panel.querySelector('h2');
    details.innerHTML = '<summary>' + heading.textContent + '</summary>';
    heading.remove();
    while (panel.firstChild) details.append(panel.firstChild);
    panel.remove();
    grid.append(details);
  }
  const delivery = $('[data-deliveries]').closest('.panel');
  delivery.querySelector('h2').textContent = '通知送达情况';
  page.append(delivery);
  const gateway = document.createElement('details');
  gateway.className = 'panel';
  gateway.innerHTML = `<summary>接收外部消息</summary><p class="muted">外部应用须按接口文档对消息签名。</p><form data-gateway><label>通道名称<input name="id" required pattern="[a-z][a-z0-9_]{1,40}" placeholder="home_inbox"></label><label>验签凭证<select name="credential" required></select></label><label>处理模型<select name="model" required></select></label><label>助手处理要求<textarea name="instructions" placeholder="描述收到消息后要如何回应"></textarea></label><label>回复发送到<select name="reply_connector"><option value="">只保存在对话中</option></select></label><button>保存消息通道</button></form><div data-gateways></div><a href="/docs#/" target="_blank" rel="noopener">查看接入文档 ↗</a>`;
  grid.append(gateway);
  const search = searchSettings({ page, api, flash, load });
  const baseRefresh = refresh;
  refresh = async function () {
    await Promise.all([baseRefresh(), search.refresh()]);
    const [managed, gateways, secrets, connectors] = await Promise.all([
      api('/v1/studio/connections'),
      api('/v1/gateway/connections'),
      api('/v1/connections/credentials'),
      api('/v1/connections'),
    ]);
    const models = managed.connections;
    modelChoices($('[data-default-model]'), models, managed.default_model || 'auto');
    $('[data-default-model]').onchange = guard(async (e) => {
      await api('/v1/studio/model-default', 'PUT', {
        alias: e.target.value === 'auto' ? null : e.target.value,
      });
      flash('默认模型已更新');
      window.dispatchEvent(new Event('eah:connections-changed'));
      await load();
    });
    $('[data-models]').innerHTML =
      models
        .filter((m) => m.alias !== 'mock')
        .map(
          (m) =>
            `<div class="saved-row" data-model-row="${escape(m.alias)}"><div><b>${escape(m.alias)}</b><div class="muted">${escape(m.model)}</div><small>${m.capabilities.map((c) => ({ chat: '对话', decision: '结构化决策', image: '图片', embedding: '语义检索' })[c] || escape(c)).join(' · ')}</small><div class="muted" data-model-limits>${escape(limitLabel(m))}</div></div>${m.managed ? `<div class="actions"><button class="small" data-edit-model="${escape(m.alias)}">编辑</button><button class="small" data-delete-model="${escape(m.alias)}">删除</button></div>` : '<span class="muted">由配置文件或扩展管理</span>'}</div>`
        )
        .join('') || '<p class="empty-hint">尚未连接模型</p>';
    $('[data-models]')
      .querySelectorAll('[data-edit-model]')
      .forEach(
        (b) =>
          (b.onclick = () =>
            window.dispatchEvent(
              new CustomEvent('eah:edit-model', {
                detail: models.find((m) => m.alias === b.dataset.editModel),
              })
            ))
      );
    $('[data-models]')
      .querySelectorAll('[data-delete-model]')
      .forEach(
        (b) =>
          (b.onclick = guard(async () => {
            const alias = b.dataset.deleteModel;
            if (
              !confirm(
                `删除模型连接“${alias}”？本地凭证和派生图片节点会停用。引用它的对话与工作流需要重新选择模型。`
              )
            )
              return;
            await api('/v1/studio/connections/' + encodeURIComponent(alias), 'DELETE');
            flash('模型连接已删除');
            window.dispatchEvent(new Event('eah:connections-changed'));
            await load();
          }))
      );
    const options = (select, html) => {
      const value = select.value;
      select.innerHTML = html;
      if ([...select.options].some((o) => o.value === value)) select.value = value;
    };
    options(
      gateway.querySelector('[name=credential]'),
      '<option value="">选择已保存的凭证</option>' +
        secrets.map((s) => `<option>${escape(s.name)}</option>`).join('')
    );
    options(
      gateway.querySelector('[name=model]'),
      models
        .filter((m) => m.capabilities.includes('chat'))
        .map((m) => `<option value="${escape(m.alias)}">${escape(m.alias)}</option>`)
        .join('')
    );
    options(
      gateway.querySelector('[name=reply_connector]'),
      '<option value="">只保存在对话中</option>' +
        connectors
          .map((c) => `<option value="${escape(c.id)}">${escape(c.title)}</option>`)
          .join('')
    );
    $('[data-gateways]').innerHTML =
      gateways
        .map(
          (g) =>
            `<div class="saved-row"><div><b>${escape(g.id)}</b><div class="muted">模型：${escape(g.model)} · /v1/gateway/incoming/${escape(g.id)}</div></div><button data-edit-gateway="${escape(g.id)}" class="small">修改</button></div>`
        )
        .join('') || '<p class="empty-hint">尚未添加消息通道。</p>';
    $('[data-gateways]')
      .querySelectorAll('[data-edit-gateway]')
      .forEach(
        (b) =>
          (b.onclick = () => {
            const g = gateways.find((g) => g.id === b.dataset.editGateway),
              f = $('[data-gateway]');
            f.elements.id.value = g.id;
            f.elements.credential.value = g.credential;
            f.elements.model.value = g.model;
            f.elements.instructions.value = g.agent.instructions || '';
            f.elements.reply_connector.value = g.reply_connector || '';
            f.dataset.editing = g.id;
            gateway._original = g;
            f.scrollIntoView({ behavior: 'smooth', block: 'center' });
          })
      );
  };
  window.addEventListener('eah:notification-delivery', () => {
    if (page.classList.contains('active')) refresh().catch(() => {});
  });
  window.addEventListener('eah:connections-changed', () => {
    if (page.classList.contains('active')) refresh().catch((e) => flash(e.message));
  });
  $('[data-refresh]').onclick = guard(refresh);
  $('[data-gateway]').onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target),
      original = e.target.dataset.editing === f.get('id') ? gateway._original : null;
    await api('/v1/gateway/connections', 'POST', {
      id: f.get('id'),
      credential: f.get('credential'),
      model: f.get('model'),
      agent: { ...(original?.agent || { prompt: '' }), instructions: f.get('instructions') },
      reply_connector: f.get('reply_connector') || null,
    });
    flash('消息通道已保存');
    await refresh();
  });
}
