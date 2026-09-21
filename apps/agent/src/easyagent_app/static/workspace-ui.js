import { memoryUI } from './memory-ui.js?v=20260920-copy-2';
// The shell only organizes existing screens. Their API handlers remain the source of truth.
export function workspaceUI({ api, escape, flash, showTab, renderRunHistory, listWorkflows }) {
  const $ = (id) => document.getElementById(id);
  const routes = {
    home: ['我的助手', 'home'],
    create: ['描述需求', 'home'],
    workflow: ['画布编排', 'home'],
    conversations: ['对话办事', 'conversations'],
    'agent-chat': ['高级对话', 'conversations'],
    runs: ['任务记录', 'runs'],
    automation: ['定时与触发', 'runs'],
    knowledge: ['资料库', 'knowledge'],
    memory: ['记住的偏好', 'knowledge'],
    settings: ['设置', 'settings'],
    connections: ['模型与服务', 'settings'],
    extensions: ['扩展能力', 'settings'],
    skills: ['使用技巧', 'settings'],
    learning: ['经验改进', 'settings'],
    evolution: ['策略验证', 'settings'],
    operations: ['诊断与备份', 'settings'],
    code: ['开发接口', 'settings'],
  };
  const groups = {
    conversations: ['对话办事', 'chat'],
    home: ['我的助手', 'home'],
    runs: ['任务', 'tasks'],
    knowledge: ['资料', 'book'],
    settings: ['设置', 'settings'],
  };
  const paths = {
    home: 'M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z',
    chat: 'M21 11.5a8.4 8.4 0 0 1-.9 3.8A8.5 8.5 0 0 1 12.5 20a8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 3.9a8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z',
    tasks: 'M9 6h12M9 12h12M9 18h12M3 6h.01M3 12h.01M3 18h.01',
    book: 'M12 5C8 2 3 3 3 3v17s5-1 9 2c4-3 9-2 9-2V3s-5-1-9 2Zm0 0v17',
    settings: 'M4 7h16M4 17h16M8 4v6M16 14v6',
  };
  const icon = (name) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]}"/></svg>`;
  const makePage = (id, html) => {
    const p = document.createElement('section');
    p.id = id;
    p.className = 'section';
    p.innerHTML = html;
    document.querySelector('main').append(p);
    return p;
  };
  const home = makePage(
    'home',
    `<div class="page-heading"><h1>我的助手</h1><div class="actions"><button class="primary" data-go="create">＋ 创建助手</button><button data-go="workflow">新建流程</button><button class="text-button" data-go="code">开发接口 ↗</button></div></div><div class="home-stats" id="homeStats" aria-live="polite"></div><div class="subhead"><div><h2>已保存</h2></div><label class="search-field"><span class="sr-only">查找助手或流程</span><input id="homeSearch" type="search" placeholder="查找助手或流程"></label></div><div class="collection-tabs" role="group" aria-label="内容类型"><button data-collection="assistants" class="active" aria-pressed="true">助手</button><button data-collection="workflows" aria-pressed="false">流程</button><button id="homeRefresh" class="text-button">刷新</button></div><div id="homeAssistants"></div><div id="homeWorkflows" hidden></div><p id="homeNoMatch" class="empty-hint" hidden>没有匹配的助手或流程</p><div class="subhead"><h2>最近任务</h2><button data-go="runs" class="text-button">查看全部 →</button></div><div id="homeRecent"></div>`
  );
  makePage(
    'settings',
    `<div class="intro"><h1>设置</h1></div><div class="settings-grid">${[
      ['connections', '模型与服务', '模型、搜索、通知、日历', 'settings'],
      ['extensions', '扩展能力', '工具与插件', 'book'],
      ['skills', '使用技巧', 'Skills 安装与管理', 'book'],
      ['learning', '经验改进', '任务复盘与代码验证', 'tasks'],
      ['operations', '诊断与备份', '服务状态、后台维护、数据备份', 'home'],
    ]
      .map(
        ([id, title, desc, ic]) =>
          `<button class="setting-card" data-go="${id}">${icon(ic)}<div><h2>${title}</h2><p>${desc}</p></div><span aria-hidden="true">→</span></button>`
      )
      .join(
        ''
      )}</div><details class="advanced-settings"><summary>开发者选项</summary><div class="settings-grid"><button class="setting-card" data-go="evolution"><div><h2>策略验证</h2><p>评估、发布、回滚</p></div>→</button><button class="setting-card" data-go="code"><div><h2>开发接口</h2><p>Python、JavaScript、Rust SDK 与 API 文档。</p></div>→</button></div><div id="accessControl" class="actions"></div></details>`
  );
  $('accessControl').append($('authBtn'));
  const saved = $('savedAssistants');
  $('refreshAssistants').hidden = true;
  home.append($('refreshAssistants'));
  saved.previousElementSibling?.remove();
  $('homeAssistants').append(saved);
  const wf = $('savedWorkflows');
  wf.previousElementSibling?.remove();
  $('homeWorkflows').append(wf);
  const memory = makePage('memory', '<div class="intro"><h1>记住的偏好</h1></div>');
  const memorySplit = $('memoryForm').closest('.split');
  memorySplit.previousElementSibling?.remove();
  memory.append(memorySplit);
  memoryUI({ api, escape, flash });
  // Keep primary actions beside their content, with technical controls under disclosure.
  const editor = $('workflow'),
    toolbar = document.createElement('div');
  toolbar.className = 'workflow-commandbar';
  const title = document.createElement('label');
  title.className = 'workflow-title';
  title.innerHTML = '<span>流程名称</span>';
  title.append($('workflowName'));
  toolbar.append(title);
  const actions = document.createElement('div');
  actions.className = 'actions';
  ['validateWorkflow', 'saveWorkflow', 'runWorkflow'].forEach((id) => actions.append($(id)));
  toolbar.append(actions);
  editor.querySelector('.intro').after(toolbar);
  const more = document.createElement('details');
  more.className = 'panel compact-panel';
  more.innerHTML = '<summary>导入、导出与分享</summary><div class="actions"></div>';
  ['importBtn', 'importSharedWorkflow', 'exportWorkflow', 'shareWorkflow'].forEach((id) =>
    more.querySelector('.actions').append($(id))
  );
  editor.append(more);
  const add = $('addTool').parentElement;
  add.classList.add('add-step-bar');
  const addTitle = document.createElement('span');
  addTitle.textContent = '添加步骤';
  addTitle.className = 'add-step-title';
  add.prepend(addTitle);
  editor.querySelector('.node-library').open = false;
  const help = $('workflowResult').previousElementSibling;
  if (help) {
    const d = document.createElement('details');
    d.className = 'panel compact-panel';
    d.innerHTML = '<summary>连线使用说明</summary>';
    while (help.childNodes.length) d.append(help.firstChild);
    d.querySelector('h2')?.remove();
    help.replaceWith(d);
  }
  const trial = $('tryBtn').closest('.panel');
  trial.id = 'assistantTrial';
  trial.classList.add('trial-panel');
  trial.querySelector('h2').textContent = '3. 试运行';
  const placeholder = document.createElement('p');
  placeholder.className = 'muted';
  placeholder.id = 'trialHint';
  placeholder.textContent = '请先生成流程。';
  trial.prepend(placeholder);
  trial.before($('assistantPlan'));
  $('assistantPlan').style.marginTop = '0';
  $('assistantForm').querySelector('h2').textContent = '1. 需求';
  $('assistantPlan').querySelector('h2').textContent = '2. 流程';
  $('globalApiSetup').textContent = '添加搜索 / API';
  $('modelCatalogBtn').textContent = '浏览模型目录';
  const services = document.createElement('div');
  services.className = 'service-actions actions';
  ['connectBtn', 'globalApiSetup', 'modelCatalogBtn'].forEach((id) => services.append($(id)));
  $('connections').querySelector('.intro').after(services);
  const nav = document.querySelector('.nav'),
    buttons = new Map([...nav.querySelectorAll('[data-tab]')].map((b) => [b.dataset.tab, b]));
  for (const id of ['home', 'settings', 'memory']) {
    const b = document.createElement('button');
    b.dataset.tab = id;
    b.onclick = () => {
      showTab(id);
      if (id === 'home') refreshHome().catch((e) => flash(e.message));
    };
    buttons.set(id, b);
  }
  const topline = document.querySelector('.topline');
  topline.innerHTML =
    '<div id="breadcrumbs" class="breadcrumbs"></div><div class="actions"><button id="workspaceHelp" class="text-button">使用指南</button><button id="quickCreate" class="small">＋ 创建助手</button></div>';
  const secondary = document.createElement('nav');
  secondary.className = 'secondary-nav';
  secondary.setAttribute('aria-label', '当前栏目');
  topline.after(secondary);
  nav.setAttribute('aria-label', '主导航');
  nav.replaceChildren();
  for (const [id, [name, ic]] of Object.entries(groups)) {
    const b = document.createElement('button');
    b.dataset.group = id;
    b.innerHTML = icon(ic) + `<span>${name}</span>`;
    b.onclick = () => navigate(id);
    nav.append(b);
  }
  for (const [id, b] of [...buttons].sort(
    ([a], [b]) => Object.keys(routes).indexOf(a) - Object.keys(routes).indexOf(b)
  )) {
    b.textContent = routes[id][0];
    b.removeAttribute('class');
    secondary.append(b);
  }
  let current = null,
    historyNavigation = false;
  function select(id) {
    if (!routes[id]) id = 'home';
    if (current !== id) window.scrollTo({ top: 0, behavior: 'instant' });
    current = id;
    const [label, group] = routes[id];
    document
      .querySelectorAll('main > .section')
      .forEach((p) => p.classList.toggle('active', p.id === id));
    nav.querySelectorAll('[data-group]').forEach((b) => {
      const active = b.dataset.group === group;
      b.classList.toggle('active', active);
      if (active) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
    for (const [key, b] of buttons) {
      b.hidden =
        routes[key][1] !== group || (group === 'settings' && key !== id && key !== 'settings');
      b.classList.toggle('active', key === id);
      if (key === id) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    }
    secondary.hidden = group === 'conversations' || id === 'settings' || id === 'home';
    $('breadcrumbs').innerHTML =
      id === group
        ? `<span>${escape(groups[group][0])}</span>`
        : `<button data-back>${escape(groups[group][0])}</button><span aria-hidden="true">/</span><span>${escape(label)}</span>`;
    $('breadcrumbs')
      .querySelector('[data-back]')
      ?.addEventListener('click', () => navigate(group));
    document.title = `${label} · EasyAgent`;
    if (!historyNavigation && location.hash !== '#' + id) history.pushState(null, '', '#' + id);
    document.dispatchEvent(new CustomEvent('eah:route', { detail: { id } }));
  }
  function navigate(id) {
    if (!buttons.has(id)) id = 'home';
    buttons.get(id).click();
  }
  window.addEventListener('popstate', () => {
    historyNavigation = true;
    try {
      navigate(location.hash.slice(1) || 'home');
    } finally {
      historyNavigation = false;
    }
  });
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-go]');
    if (b) {
      if (b.dataset.go === 'create') $('newAssistant').click();
      navigate(b.dataset.go);
      if (b.dataset.runFilter) {
        $('runStatus').value = b.dataset.runFilter;
        $('runStatus').dispatchEvent(new Event('change'));
      }
      if (b.hasAttribute('data-import-shortcut')) $('importBtn').click();
    }
  });
  $('quickCreate').onclick = () => {
    $('newAssistant').click();
    navigate('create');
  };
  const helpDialog = document.createElement('dialog');
  helpDialog.className = 'help-dialog';
  helpDialog.innerHTML = `<div class="dialog-heading"><h2>使用指南</h2><button data-close aria-label="关闭使用指南">关闭 ×</button></div><ol class="help-steps"><li><b>连接一个模型</b><p>在“模型与服务”填写地址和密钥，保存并测试。</p></li><li><b>创建助手</b><p>用文字描述需求，或在画布中连接步骤；开发者可使用三种语言 SDK。</p></li><li><b>试运行，查看结果</b><p>输入材料后运行；待确认或缺少信息时暂停。</p></li></ol><p class="muted">助手、完整流程和扩展包均可导出分享。连接服务使用的密钥由接收者自行配置。</p><div class="actions"><button data-go="connections">连接模型</button><button data-go="create" class="primary">创建助手</button><a href="/docs" target="_blank" rel="noopener">API 文档 ↗</a></div>`;
  document.body.append(helpDialog);
  helpDialog.querySelector('[data-close]').onclick = () => helpDialog.close();
  helpDialog
    .querySelectorAll('[data-go]')
    .forEach((b) => b.addEventListener('click', () => helpDialog.close()));
  $('workspaceHelp').onclick = () => helpDialog.showModal();
  let collection = 'assistants';
  function filter() {
    const query = $('homeSearch').value.toLocaleLowerCase().trim(),
      root = collection === 'assistants' ? saved : wf;
    let count = 0;
    root.querySelectorAll('.saved-row').forEach((row) => {
      row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
      if (!row.hidden) count++;
    });
    $('homeNoMatch').hidden = !query || count > 0;
  }
  home.querySelectorAll('[data-collection]').forEach(
    (b) =>
      (b.onclick = () => {
        collection = b.dataset.collection;
        home.querySelectorAll('[data-collection]').forEach((t) => {
          t.classList.toggle('active', t === b);
          t.setAttribute('aria-pressed', String(t === b));
        });
        $('homeAssistants').hidden = collection !== 'assistants';
        $('homeWorkflows').hidden = collection !== 'workflows';
        filter();
      })
  );
  $('homeSearch').oninput = filter;
  new MutationObserver(filter).observe(saved, { childList: true });
  new MutationObserver(filter).observe(wf, { childList: true });
  let refreshing = false;
  async function refreshHome() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [runs, assistants, workflows, models, metrics, waiting] = await Promise.all([
        api('/v1/runs?limit=5'),
        api('/v1/studio/assistants'),
        api('/v1/studio/workflows'),
        api('/v1/models'),
        api('/v1/metrics'),
        api('/v1/runs?limit=5&status=waiting_approval&status=waiting_input&status=needs_attention'),
      ]);
      const count = (states) => states.reduce((sum, s) => sum + (metrics.runs[s] || 0), 0);
      $('homeStats').innerHTML = [
        [assistants.length + workflows.length, '已保存助手与流程', 'home'],
        [
          count(['waiting_approval', 'waiting_input', 'needs_attention']),
          '需要你处理',
          'runs',
          'attention',
        ],
        [
          count(['running', 'queued', 'waiting_remote', 'waiting_children']),
          '正在执行',
          'runs',
          'active',
        ],
      ]
        .map(
          ([n, label, id, filter]) =>
            `<button data-go="${id}" ${filter ? 'data-run-filter="' + filter + '"' : ''}><strong>${n}</strong><span>${label}</span><span aria-hidden="true">→</span></button>`
        )
        .join('');
      const recent = [...waiting, ...runs.filter((r) => !waiting.some((w) => w.id === r.id))].slice(
        0,
        5
      );
      renderRunHistory(
        $('homeRecent'),
        recent,
        '<div class="empty-state"><h3>暂无任务</h3><button data-go="create">创建助手</button></div>'
      );
      const connected = models.some((m) => m.alias !== 'mock' && m.capabilities.includes('chat'));
      document.querySelector('.aside-note').innerHTML =
        `<span class="local-status"><i class="status-dot"></i>${connected ? '模型已连接' : '尚未连接模型'}</span><button class="text-button" data-go="connections">${connected ? '管理连接 →' : '连接模型 →'}</button>`;
    } finally {
      refreshing = false;
    }
  }
  $('homeRefresh').onclick = () => {
    document.getElementById('refreshAssistants')?.click();
    listWorkflows().catch((e) => flash(e.message));
    refreshHome().catch((e) => flash(e.message));
  };
  return {
    select,
    navigate,
    start() {
      navigate(location.hash.slice(1) || 'home');
    },
    refresh: refreshHome,
  };
}
