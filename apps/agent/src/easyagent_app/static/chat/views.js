// HTML presentation only. Events, requests and stale-response checks belong to the controller.

export const pageHTML = `
<div class="chat-page-heading">
  <div><h1>对话办事</h1></div>
  <button class="text-button" data-go="agent-chat">高级对话 ↗</button>
</div>
<div class="chat-layout">
  <aside class="chat-history">
    <button class="chat-new" data-new>＋ 新对话</button>
    <label class="chat-search">
      <span class="sr-only">搜索对话</span>
      <input type="search" data-search placeholder="搜索对话" />
    </label>
    <div data-history></div>
  </aside>
  <article class="chat-room">
    <header class="chat-room-heading">
      <div>
        <span class="chat-presence"></span>
        <strong data-title>新对话</strong>
      </div>
      <div class="chat-mobile-tools">
        <button class="small" data-history-toggle aria-expanded="false">历史</button>
        <button class="small" data-new-mobile>＋ 新对话</button>
      </div>
    </header>
    <div class="chat-scroll" data-scroll>
      <div data-welcome class="chat-welcome">
        <div class="chat-starters">
          <button data-starter="帮我整理这份通知，列出要做的事和截止日期。">
            <span>▤</span>
            <b>整理材料</b>
            <small>通知、文档、会议记录</small>
          </button>
          <button data-starter="帮我找到合适的已有流程，处理我上传的图片。">
            <span>◇</span>
            <b>使用已有流程</b>
          </button>
          <button data-starter="帮我创建一个可以重复使用的流程：" data-create-starter>
            <span>⌘</span>
            <b>创建流程</b>
          </button>
        </div>
      </div>
      <div data-timeline></div>
    </div>
    <form class="chat-composer" data-compose>
      <div class="chat-composer-top">
        <label>
          <span class="sr-only">如何处理这条消息</span>
          <select data-destination>
            <option value="auto">✦ 自动安排</option>
            <option value="create">＋ 创建新流程</option>
            <option value="chat">仅对话</option>
          </select>
        </label>
        <label class="chat-model-choice">
          模型
          <select data-model aria-label="处理模型">
            <option value="auto">自动选择已连接模型</option>
          </select>
        </label>
        <label>
          执行方式
          <select data-execution aria-label="执行方式">
            <option value="automatic">自动执行</option>
            <option value="confirm">逐项确认</option>
          </select>
        </label>
        <span data-context>优先复用已有流程</span>
      </div>
      <div class="chat-attachments" data-files></div>
      <label class="sr-only" for="workspaceMessage">你的需求</label>
      <textarea
        id="workspaceMessage"
        data-text
        rows="3"
        placeholder="输入需求，或拖入附件"
      ></textarea>
      <div class="chat-composer-bottom">
        <button
          type="button"
          data-attach
          class="chat-attach"
          aria-label="添加图片、视频、音频或文档"
        >
          ＋
          <span>添加附件</span>
        </button>
        <input
          type="file"
          multiple
          hidden
          data-file-input
          accept="image/*,audio/*,video/*,.pdf,.docx,.txt,.md,.csv,.json,.yaml,.yml"
        />
        <span class="chat-compose-hint">Enter 发送 · Shift + Enter 换行</span>
        <button type="button" data-stop class="small" hidden>停止本轮</button>
        <button class="chat-send" data-send aria-label="发送需求">
          发送
          <span aria-hidden="true">↑</span>
        </button>
      </div>
      <p data-error class="chat-inline-error" role="alert" hidden></p>
    </form>
    <p class="chat-footnote" data-queue>
      自动执行已连接工具、Python 和媒体生成，可随时停止；服务无回执时，生图可能重新提交。
    </p>
    <div class="chat-drop-zone" data-drop hidden>
      松开添加附件
      <span>图片 · 视频 · 音频 · 文档</span>
    </div>
  </article>
</div>
`;

export const cardHTML = `
<div class="chat-user-message">
  <small>你</small>
  <div data-user></div>
  <div class="chat-sent-files" data-sent-files></div>
</div>
<div class="chat-task-card">
  <div data-card-heading></div>
  <div data-graph></div>
  <div data-children></div>
  <div class="chat-activity" data-activity hidden></div>
  <div data-choices class="chat-choices"></div>
  <div class="chat-result-message" data-reply></div>
  <div data-setup></div>
  <div data-retry></div>
  <div class="chat-result-media" data-media></div>
  <div class="chat-task-actions">
    <button class="text-button" data-details-toggle aria-expanded="false">展开步骤与结果</button>
    <button class="text-button" data-edit hidden>在画布中打开 ↗</button>
  </div>
  <div class="chat-task-details" data-details hidden></div>
</div>
`;

export function setupHTML(state, escape) {
  if (state.phase !== 'waiting_connections') return '';

  const connections = (state.required_connections || [])
    .map(
      (connection) =>
        `<li><b>${escape(connection.title)}</b><p>${escape(connection.reason)}</p></li>`
    )
    .join('');

  let plannedSteps = '';
  if (state.planned_steps?.length) {
    const items = state.planned_steps
      .map((step) => {
        const dependencies = step.depends_on
          .map((id) => state.planned_steps.find((candidate) => candidate.id === id)?.title || id)
          .join('、');
        return `<li>
        <b>${escape(step.title)}</b><p>${escape(step.description)}</p>
        <small>等待：${escape(dependencies || '无前置步骤')}</small>
      </li>`;
      })
      .join('');
    plannedSteps = `<h3>步骤草稿 · 待接入</h3><ol>${items}</ol>`;
  }

  let blueprint = '';
  if (state.blueprint) {
    const items = state.blueprint.steps
      .map(
        (step) => `<li>${escape(state.blueprint.metadata?.step_labels?.[step.id] || step.id)}</li>`
      )
      .join('');
    blueprint = `<details>
      <summary>查看已保存的流程草稿（待验证）</summary><ol>${items}</ol>
    </details>`;
  }

  return `<div class="notice">
    <ul>${connections}</ul>
    <div class="actions">
      <button data-setup-connect>去连接模型或服务</button>
      <button data-setup-resume ${state.can_resume ? '' : 'disabled'}>识别连接并继续</button>
    </div>
    <p class="muted">需求和附件已保留。连接后返回本对话会继续检查，也可以发送补充说明。</p>
  </div>${plannedSteps}${blueprint}`;
}

export function turnHeading(state, run, phases, statuses, escape) {
  const labels = phases.labels;
  const title = state.selected?.title || labels[state.phase] || '正在安排';
  const finished =
    run?.steps.filter((s) => ['succeeded', 'skipped'].includes(s.status)).length || 0;
  const active = run
    ? ['queued', 'running'].includes(run.status)
    : phases.active.includes(state.phase);
  const retryWaiting =
    run?.steps.some((s) => s.status === 'retrying') &&
    !run.steps.some((s) => s.status === 'running');
  let label = labels[state.phase] || '正在安排';
  if (retryWaiting) {
    label = '等待自动重试';
  } else if (active && state.phase === 'failed') {
    label = '正在继续处理';
  } else if (run && ['executing', 'working'].includes(state.phase)) {
    label =
      state.phase === 'working' && active ? '正在自主处理' : statuses[run.status] || run.status;
  }

  let mark = '✦';
  if (!active && state.phase === 'completed') mark = '✓';
  if (!active && state.phase === 'failed') mark = '!';
  const revision = state.selected ? ` · 固定版本 v${state.selected.revision}` : '';
  const count =
    run && ['executing', 'completed'].includes(state.phase)
      ? `<span class="chat-step-count">${finished}/${run.steps.length}</span>`
      : '';
  const reason = state.reason ? `<p class="chat-route-reason">${escape(state.reason)}</p>` : '';

  return `<div class="chat-task-heading">
    <span class="chat-task-mark ${active ? 'is-working' : ''}">${mark}</span>
    <div><b>${escape(title)}</b><p>${escape(label)}${revision}</p></div>
    ${count}
  </div>${reason}`;
}
