// Page controller: composer events, conversation selection and the refresh lifecycle.
import { createRunGraph } from './chat/graph.js';
import { createActivityView } from './chat/activity.js';
import { conversationState } from './chat/state.js';
import { pageHTML, cardHTML, setupHTML, turnHeading } from './chat/views.js';
import { readProgress, messagePayload } from './chat/requests.js';
import { retryPanel, bindRetry } from './run-retry.js?v=20260921-operator-8';
import { modelChoices } from './model-choice.js';
import { formatChat } from './chat-format.js';
import { uploadMedia, bindMedia, clearMedia, canPreview } from './media-preview.js';

export function workspaceChat({
  api,
  escape,
  flash,
  showTab,
  renderRun,
  stopWatch,
  loadWorkflow,
  token,
  download,
  statuses,
}) {
  const nav = document.createElement('button');
  nav.dataset.tab = 'conversations';
  nav.textContent = '对话办事';
  document.querySelector('.nav').append(nav);
  const page = document.createElement('section');
  page.id = 'conversations';
  page.className = 'section chat-workspace';
  page.innerHTML = pageHTML;
  document.querySelector('main').append(page);
  const $ = (s) => page.querySelector(s),
    guard = (fn) => async (e) => {
      try {
        await fn(e);
      } catch (error) {
        $('[data-error]').textContent = error.message;
        $('[data-error]').hidden = false;
        flash(error.message);
      }
    };
  const floating = document.createElement('button');
  floating.className = 'chat-launcher';
  floating.innerHTML = '<span aria-hidden="true">✦</span> 对话办事';
  floating.setAttribute('aria-label', '打开对话办事入口');
  floating.onclick = () => nav.click();
  document.body.append(floating);
  let selected = null,
    polling = false,
    sending = false,
    uploading = 0,
    files = [],
    catalog = [],
    historyRows = [],
    signature = '',
    searchTimer = null,
    modelList = [],
    defaultModel = null,
    chosenModel = localStorage.getItem('easyagent.workspaceModel') || 'auto',
    modelBusy = false,
    turnBusy = false,
    workingTurn = null;
  function drawModel() {
    modelChoices($('[data-model]'), modelList, chosenModel, defaultModel);
    $('[data-model]').disabled = turnBusy || sending || modelBusy;
  }
  async function loadModels() {
    const data = await api('/v1/studio/connections');
    modelList = data.connections;
    defaultModel = data.default_model;
    drawModel();
  }
  $('[data-model]').onchange = guard(async () => {
    const value = $('[data-model]').value,
      id = selected;
    modelBusy = true;
    drawFiles();
    drawModel();
    try {
      if (id) await api('/v1/conversations/' + id, 'PATCH', { model: value });
      if (id === selected) {
        chosenModel = value;
        localStorage.setItem('easyagent.workspaceModel', value);
        signature = '';
      }
    } finally {
      modelBusy = false;
      drawModel();
      drawFiles();
    }
  });
  $('[data-execution]').onchange = () => {
    $('[data-queue]').textContent =
      $('[data-execution]').value === 'automatic'
        ? '自动执行已连接工具、Python 和媒体生成，可随时停止；服务无回执时，生图可能重新提交。'
        : '外部写操作会逐项请求确认。';
  };
  window.addEventListener('eah:connections-changed', () =>
    loadModels().catch((e) => flash(e.message))
  );
  const drafts = new Map(),
    cards = new Map(),
    runCache = new Map();
  const size = (n) => (n < 1e6 ? Math.ceil(n / 1000) + ' KB' : (n / 1e6).toFixed(1) + ' MB');
  const glyph = (type) => ({ image: '▧', audio: '♫', video: '▷', document: '▤' })[type] || '▤';
  const nearBottom = () => {
    const el = $('[data-scroll]');
    return el.scrollHeight - el.clientHeight - el.scrollTop < 100;
  };
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const updateGraph = createRunGraph({ escape, statuses, guard, showDetails, reducedMotion });
  const updateActivity = createActivityView({ api, escape });
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches)
      page
        .querySelectorAll('.just-completed')
        .forEach((node) => node.classList.remove('just-completed'));
  });
  function scrollEnd(force = false) {
    if (force)
      $('[data-scroll]').scrollTo({
        top: $('[data-scroll]').scrollHeight,
        behavior: reducedMotion.matches ? 'auto' : 'smooth',
      });
  }
  function closeHistory() {
    $('.chat-history').classList.remove('mobile-open');
    $('[data-history-toggle]').setAttribute('aria-expanded', 'false');
  }
  $('[data-history-toggle]').onclick = () => {
    const opened = $('.chat-history').classList.toggle('mobile-open');
    $('[data-history-toggle]').setAttribute('aria-expanded', String(opened));
  };
  page.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeHistory();
  });
  function clearCards() {
    for (const entry of cards.values()) {
      stopWatch(entry.details);
      entry.root.querySelectorAll('[data-child-run]').forEach(stopWatch);
      clearMedia(entry.media);
    }
    cards.clear();
    runCache.clear();
    $('[data-timeline]').replaceChildren();
  }
  function persistDraft() {
    drafts.set(selected || 'new', {
      text: $('[data-text]').value,
      files,
      destination: $('[data-destination]').value,
      execution: $('[data-execution]').value,
    });
  }
  function restoreDraft() {
    const draft = drafts.get(selected || 'new') || { text: '', files: [], destination: 'auto' };
    $('[data-text]').value = draft.text;
    files = draft.files;
    $('[data-destination]').value = draft.destination;
    $('[data-execution]').value = draft.execution || 'automatic';
    drawFiles();
  }
  function drawFiles() {
    $('[data-files]').innerHTML = files
      .map(
        (f, i) =>
          `<div class="chat-file ${f.error ? 'has-error' : ''}"><span>${glyph(f.kind || f.type?.split('/')[0])}</span><div><b>${escape(f.name)}</b><small>${f.error ? '上传失败 · 移除后可重新添加' : f.id ? size(f.size) : '正在上传…'}</small></div><button type="button" data-remove="${i}" aria-label="移除 ${escape(f.name)}">×</button></div>`
      )
      .join('');
    $('[data-files]')
      .querySelectorAll('[data-remove]')
      .forEach(
        (b) =>
          (b.onclick = () => {
            files.splice(Number(b.dataset.remove), 1);
            drawFiles();
          })
      );
    $('[data-send]').disabled = !!uploading || sending || modelBusy;
    $('[data-model]').disabled = turnBusy || sending || modelBusy;
  }
  async function attach(incoming) {
    if (files.length + incoming.length > 8) throw Error('每条消息最多添加 8 个附件。');
    for (const file of incoming) {
      if (file.size > 50_000_000) throw Error('单个附件不能超过 50 MB。');
      const group = files,
        entry = { name: file.name, size: file.size, type: file.type };
      group.push(entry);
      uploading++;
      drawFiles();
      try {
        Object.assign(entry, await uploadMedia(file, token(), 50_000_000));
      } catch (error) {
        entry.error = error.message;
        flash(error.message);
      } finally {
        uploading--;
        drawFiles();
      }
    }
    $('[data-text]').focus();
  }
  $('[data-attach]').onclick = () => $('[data-file-input]').click();
  $('[data-file-input]').onchange = guard(async (e) => {
    await attach([...e.target.files]);
    e.target.value = '';
  });
  $('[data-text]').addEventListener(
    'paste',
    guard(async (e) => {
      const list = [...(e.clipboardData?.files || [])];
      if (list.length) {
        e.preventDefault();
        await attach(list);
      }
    })
  );
  let dragDepth = 0;
  page.addEventListener('dragenter', (e) => {
    if (e.dataTransfer?.types.includes('Files')) {
      e.preventDefault();
      dragDepth++;
      $('[data-drop]').hidden = false;
    }
  });
  page.addEventListener('dragover', (e) => {
    if (e.dataTransfer?.types.includes('Files')) e.preventDefault();
  });
  page.addEventListener('dragleave', () => {
    if (--dragDepth <= 0) $('[data-drop]').hidden = true;
  });
  page.addEventListener(
    'drop',
    guard(async (e) => {
      if (!e.dataTransfer?.files.length) return;
      e.preventDefault();
      dragDepth = 0;
      $('[data-drop]').hidden = true;
      await attach([...e.dataTransfer.files]);
    })
  );
  async function choose(id) {
    closeHistory();
    persistDraft();
    selected = id;
    signature = '';
    turnBusy = false;
    if (!id) chosenModel = localStorage.getItem('easyagent.workspaceModel') || 'auto';
    drawModel();
    clearCards();
    restoreDraft();
    localStorage.setItem('easyagent.workspaceConversation', id || '');
    $('[data-title]').textContent = '新对话';
    $('[data-welcome]').hidden = !!id;
    drawHistory();
    if (id) {
      await api('/v1/conversations/' + id + '/resume-connections', 'POST', {});
      await refresh();
    }
  }
  const newChat = guard(async () => {
    await choose(null);
    $('[data-text]').focus();
  });
  $('[data-new]').onclick = $('[data-new-mobile]').onclick = newChat;
  function drawHistory() {
    const query = $('[data-search]').value.trim().toLowerCase();
    $('[data-history]').innerHTML =
      historyRows
        .filter((r) => r.title.toLowerCase().includes(query))
        .map(
          (r) =>
            `<button data-thread="${escape(r.id)}" class="chat-history-item ${r.id === selected ? 'active' : ''}"><span>${escape(r.title)}</span><small>${r.active_run ? '● 正在处理' : new Date(r.created * 1000).toLocaleDateString()}</small></button>`
        )
        .join('') ||
      (query ? '<p class="empty-hint">没有匹配的对话</p>' : '<p class="empty-hint">暂无对话</p>');
    $('[data-history]')
      .querySelectorAll('[data-thread]')
      .forEach((b) => (b.onclick = guard(() => choose(b.dataset.thread))));
  }
  async function listing() {
    historyRows = (await api('/v1/conversations')).filter((r) => r.workspace);
    drawHistory();
  }
  async function loadCatalog() {
    catalog = await api('/v1/conversations/workflow-catalog');
    const select = $('[data-destination]'),
      value = select.value;
    select.innerHTML =
      '<option value="auto">✦ 自动安排</option><option value="create">＋ 创建新流程</option><option value="chat">仅对话</option><optgroup label="指定已保存的流程">' +
      catalog
        .map((c) => `<option value="${escape(c.key)}">${escape(c.title)} · v${c.revision}</option>`)
        .join('') +
      '</optgroup>';
    if ([...select.options].some((o) => o.value === value)) select.value = value;
  }
  $('[data-search]').oninput = () => {
    drawHistory();
    clearTimeout(searchTimer);
    if ($('[data-search]').value.trim())
      searchTimer = setTimeout(
        guard(async () => {
          const query = $('[data-search]').value.trim();
          const matches = await api('/v1/conversations/search?query=' + encodeURIComponent(query));
          if (query !== $('[data-search]').value.trim()) return;
          const ids = new Set(matches.map((m) => m.conversation));
          const rows = historyRows.filter(
            (r) => ids.has(r.id) && !r.title.toLowerCase().includes(query.toLowerCase())
          );
          for (const row of rows) {
            const b = document.createElement('button');
            b.className = 'chat-history-item';
            b.textContent = row.title;
            b.onclick = guard(() => choose(row.id));
            $('[data-history]').append(b);
          }
        }),
        300
      );
  };
  $('[data-destination]').onchange = () => {
    $('[data-context]').textContent =
      $('[data-destination]').value === 'auto'
        ? '优先复用已有流程'
        : $('[data-destination]').value === 'create'
          ? '生成并保存流程'
          : $('[data-destination]').value === 'chat'
            ? '不执行工作流'
            : '使用指定流程的固定版本';
  };
  page.querySelectorAll('[data-starter]').forEach(
    (b) =>
      (b.onclick = () => {
        $('[data-text]').value = b.dataset.starter;
        if (b.hasAttribute('data-create-starter')) $('[data-destination]').value = 'create';
        $('[data-destination]').dispatchEvent(new Event('change'));
        $('[data-text]').focus();
      })
  );
  async function send(event) {
    event?.preventDefault();
    if (sending || uploading || modelBusy) return;
    if (files.some((f) => f.error || !f.id)) throw Error('请移除上传失败的附件，或重新上传。');
    const text = $('[data-text]').value.trim();
    if (!text && !files.length) return;
    const destination = $('[data-destination]').value,
      attachments = files.map((f) => f.id);
    sending = true;
    drawFiles();
    $('[data-error]').hidden = true;
    try {
      if (!selected) {
        const c = await api('/v1/conversations', 'POST', {
          workspace: true,
          model: chosenModel,
          title: (text || files[0]?.name || '新的对话').slice(0, 60),
        });
        selected = c.id;
        localStorage.setItem('easyagent.workspaceConversation', selected);
      }
      const id = selected;
      // Retain the key on transport failure so retry cannot duplicate a model call or run.
      const payload = messagePayload(
        text,
        attachments,
        destination,
        $('[data-execution]').value,
        workingTurn
      );
      const fingerprint = JSON.stringify({ id, ...payload });
      if (send.fingerprint !== fingerprint) {
        send.fingerprint = fingerprint;
        send.key = crypto.randomUUID();
      }
      await api(`/v1/conversations/${id}/messages`, 'POST', {
        ...payload,
        idempotency_key: send.key,
      });
      if (id === selected) {
        $('[data-text]').value = '';
        files = [];
        drafts.delete('new');
        drafts.delete(id);
        drawFiles();
        await refresh();
        scrollEnd(true);
      }
      send.fingerprint = null;
      await listing();
    } finally {
      sending = false;
      drawFiles();
    }
  }
  $('[data-compose]').onsubmit = guard(send);
  $('[data-text]').onkeydown = guard(async (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      await send();
    }
  });
  $('[data-stop]').onclick = guard(async () => {
    await api(`/v1/conversations/${selected}/interrupt`, 'POST', {});
    await refresh();
  });
  async function showDetails(entry) {
    const id = entry.run?.id;
    if (!id) return;
    const full = entry.run.progress ? await api('/v1/runs/' + id) : entry.run;
    if (entry.run?.id !== id || entry.details.hidden || !entry.root.isConnected) return;
    renderRun(full, entry.details);
  }
  function ensureCard(turn) {
    let entry = cards.get(turn.id);
    if (entry) return entry;
    const root = document.createElement('article');
    root.className = 'chat-turn';
    root.dataset.turn = turn.id;
    root.innerHTML = cardHTML;
    entry = {
      root,
      details: root.querySelector('[data-details]'),
      media: root.querySelector('[data-media]'),
      lastDetail: '',
      lastMedia: '',
      lastGraph: '',
      events: [],
      eventCursor: 0,
      eventRun: null,
    };
    cards.set(turn.id, entry);
    $('[data-timeline]').append(root);
    root.querySelector('[data-details-toggle]').onclick = guard(async (e) => {
      entry.details.hidden = !entry.details.hidden;
      e.currentTarget.setAttribute('aria-expanded', String(!entry.details.hidden));
      e.currentTarget.textContent = entry.details.hidden ? '展开步骤与结果' : '收起步骤与结果';
      if (entry.details.hidden) stopWatch(entry.details);
      else if (entry.run) await showDetails(entry);
    });
    return entry;
  }
  async function paint(c) {
    const history = historyRows.find((r) => r.id === c.id);
    if (history && history.active_run !== c.active_run) {
      history.active_run = c.active_run;
      drawHistory();
    }
    ({ chosenModel, turnBusy, workingTurn } = conversationState(c));
    drawModel();
    $('[data-send]').textContent = workingTurn ? '补充要求 ↑' : '发送 ↑';
    $('[data-text]').placeholder = workingTurn
      ? '任务正在自主处理；发送的内容会补充给它'
      : '输入需求，或拖入附件';
    const pinned = nearBottom();
    $('[data-title]').textContent = c.title;
    $('[data-welcome]').hidden = c.turns.length > 0;
    $('[data-stop]').hidden = !c.turns.some(
      (t) => !['succeeded', 'failed', 'cancelled'].includes(t.status)
    );
    for (const turn of c.turns) {
      const state = turn.task;
      if (!state) continue;
      const entry = ensureCard(turn),
        root = entry.root,
        find = (s) => root.querySelector(s);
      find('[data-user]').textContent = turn.text;
      const attachments = state.attachments || [];
      find('[data-sent-files]').innerHTML = attachments
        .map(
          (a) => `<button data-file="${escape(a.id)}">${glyph(a.kind)} ${escape(a.name)}</button>`
        )
        .join('');
      find('[data-sent-files]')
        .querySelectorAll('button')
        .forEach(
          (b) =>
            (b.onclick = guard(() =>
              downloadArtifact(attachments.find((a) => a.id === b.dataset.file))
            ))
        );
      let run = null;
      if (state.run_id) {
        run = await readProgress(api, runCache, state.run_id, turn.status);
        if (c.id !== selected) return;
        entry.run = run;
      }
      const heading = turnHeading(state, run, c.phases, statuses, escape);
      if (heading !== entry.lastHeading) {
        entry.lastHeading = heading;
        find('[data-card-heading]').innerHTML = heading;
      }
      if (run) {
        updateGraph(entry, run);
        await updateActivity(entry, run);
        if (c.id !== selected) return;
        const children = find('[data-children]'),
          childKey = JSON.stringify(run.children || []);
        if (entry.childKey !== childKey) {
          entry.childKey = childKey;
          children.querySelectorAll('[data-child-run]').forEach(stopWatch);
          children.innerHTML = (run.children || []).length
            ? '<p class="muted">流程执行与协作任务</p>' +
              (run.children || [])
                .map(
                  (child) =>
                    `<details data-child="${escape(child.id)}"><summary>${child.kind === 'agent' ? '协作助手' : '工作流'} · ${escape(child.name)} · ${escape(statuses[child.status] || child.status)}</summary><div data-child-run></div></details>`
                )
                .join('')
            : '';
          children.querySelectorAll('[data-child]').forEach(
            (detail) =>
              (detail.ontoggle = guard(async () => {
                const body = detail.querySelector('[data-child-run]');
                if (detail.open) {
                  const child = await api('/v1/runs/' + detail.dataset.child);
                  renderRun(child, body);
                } else stopWatch(body);
              }))
          );
        }
      }
      const message = c.messages
        .filter((m) => m.turn_id === turn.id && m.role === 'assistant')
        .at(-1);
      find('[data-reply]').innerHTML = formatChat(
        message?.content ||
          (['clarification', 'failed', 'waiting_connections', 'superseded', 'steered'].includes(
            state.phase
          )
            ? state.message
            : ''),
        escape
      );
      find('[data-choices]').innerHTML = (state.choices || [])
        .map(
          (choice) =>
            `<button data-choice="${escape(choice.key)}">使用 ${escape(choice.title)} →</button>`
        )
        .join('');
      find('[data-choices]')
        .querySelectorAll('button')
        .forEach(
          (b) =>
            (b.onclick = guard(async () => {
              await loadCatalog();
              $('[data-destination]').value = b.dataset.choice;
              if (!$('[data-destination]').value) throw Error('流程版本已更新，请在列表重新选择。');
              $('[data-text]').value = turn.text;
              files = [...attachments];
              drawFiles();
              await send();
            }))
        );
      const setup = find('[data-setup]');
      setup.innerHTML = setupHTML(state, escape);
      if (state.phase === 'waiting_connections') {
        setup.querySelector('[data-setup-connect]').onclick = () =>
          document.querySelector('[data-tab="connections"]').click();
        setup.querySelector('[data-setup-resume]').onclick = guard(async () => {
          await api(
            '/v1/conversations/' +
              c.id +
              '/resume-connections?turn_id=' +
              encodeURIComponent(turn.id),
            'POST',
            {}
          );
          signature = '';
          await refresh();
        });
      }
      const retryRoot = find('[data-retry]'),
        retryKey = JSON.stringify([run?.id, run?.updated, run?.status, run?.retry]);
      if (entry.retryKey !== retryKey) {
        entry.retryKey = retryKey;
        retryRoot.innerHTML = run ? retryPanel(run, escape) : '';
        if (run)
          bindRetry(retryRoot, run, {
            api,
            onRetry: async () => {
              runCache.delete(run.id);
              signature = '';
              await refresh();
            },
          });
      }
      find('[data-edit]').hidden = !state.selected;
      find('[data-edit]').onclick = guard(async () => {
        const saved = await api(
          `/v1/studio/workflows/${encodeURIComponent(state.selected.id)}?revision=${state.selected.revision}`
        );
        loadWorkflow(saved.workflow, saved);
        showTab('workflow');
      });
      find('[data-details-toggle]').hidden = !run;
      if (run) {
        const wait = ['waiting_approval', 'waiting_input', 'needs_attention'].includes(run.status),
          detailKey = JSON.stringify([
            run.status,
            run.approvals,
            run.input_requests,
            run.reconciliations,
            run.steps.map((s) => [s.id, s.status, s.attempts, s.ready_at, s.error, s.build_turns]),
            run.children,
          ]);
        if (wait && entry.lastDetail !== detailKey) {
          entry.details.hidden = false;
          find('[data-details-toggle]').setAttribute('aria-expanded', 'true');
          find('[data-details-toggle]').textContent = '收起步骤与结果';
        }
        if (!entry.details.hidden && entry.lastDetail !== detailKey) {
          await showDetails(entry);
          entry.lastDetail = detailKey;
        }
        if (
          run.status === 'succeeded' &&
          state.phase === 'completed' &&
          entry.lastMedia !== run.id
        ) {
          const found = new Map();
          function visit(v) {
            if (!v || typeof v !== 'object') return;
            if (v.id && v.digest && v.media_type && !attachments.some((a) => a.id === v.id))
              found.set(v.id, v);
            Object.values(v).forEach(visit);
          }
          run.steps.forEach((s) => visit(s.output));
          // Operator final answers contain text; its artifacts live in tool receipts and child runs.
          // Collect actual stored artifacts across the run tree so these deliverables are downloadable.
          const result = await api('/v1/runs/' + encodeURIComponent(run.id) + '/result');
          result.artifacts.forEach(visit);
          if (c.id !== selected) return;
          entry.lastMedia = run.id;
          clearMedia(entry.media);
          entry.media.innerHTML = [...found.values()]
            .map(
              (a) =>
                `<div class="chat-result-file"><span>${glyph(a.media_type.split('/')[0])}</span><b>${escape(a.name)}</b><button class="small" data-download="${a.id}">下载</button>${canPreview(a.media_type) ? `<button class="small" data-media-preview="${a.id}" data-media-type="${escape(a.media_type)}" data-filename="${escape(a.name)}">预览</button>` : ''}</div>`
            )
            .join('');
          entry.media
            .querySelectorAll('[data-download]')
            .forEach(
              (b) => (b.onclick = guard(() => downloadArtifact(found.get(b.dataset.download))))
            );
          bindMedia(entry.media, token(), guard);
        }
      }
    }
    $('[data-queue]').textContent = c.turns.some((t) => ['queued', 'starting'].includes(t.status))
      ? '后续消息排队中'
      : $('[data-execution]').value === 'automatic'
        ? '自动执行已连接工具、Python 和媒体生成，可随时停止；服务无回执时，生图可能重新提交。'
        : '外部写操作会逐项请求确认。';
    if (pinned) scrollEnd(true);
  }
  async function downloadArtifact(a) {
    const r = await fetch('/v1/artifacts/' + encodeURIComponent(a.id) + '/content', {
      headers: token() ? { Authorization: 'Bearer ' + token() } : {},
    });
    if (!r.ok) throw Error('文件读取失败');
    download(await r.blob(), a.name);
  }
  async function refresh() {
    if (!selected || polling) return;
    polling = true;
    const id = selected;
    try {
      const c = await api('/v1/conversations/' + id);
      if (id !== selected) return;
      const next = JSON.stringify(c);
      if (next !== signature || c.active_run) {
        signature = next;
        window.dispatchEvent(new CustomEvent('eah:conversation', { detail: { id: c.id } }));
        window.dispatchEvent(
          new CustomEvent('eah:message', { detail: { conversation: c.id, messages: c.messages } })
        );
        await paint(c);
      }
    } finally {
      polling = false;
    }
  }
  window.addEventListener('eah:run-retried', (e) => {
    const run = e.detail.run;
    if (run) runCache.set(run.id, run);
    else runCache.delete(e.detail.id);
    for (const entry of cards.values()) {
      entry.lastDetail = '';
      entry.retryKey = '';
      if (run && entry.run?.id === run.id) {
        entry.run = run;
        updateGraph(entry, run);
        entry.lastHeading = '';
        const mark = entry.root.querySelector('.chat-task-mark');
        mark.textContent = '✦';
        mark.classList.toggle('is-working', ['queued', 'running'].includes(run.status));
        entry.root.querySelector('.chat-task-heading p').textContent = '已提交重试，正在继续处理';
        entry.root.querySelector('[data-retry]').replaceChildren();
      }
    }
    signature = '';
    refresh().catch((e) => flash(e.message));
  });
  nav.onclick = guard(async () => {
    showTab('conversations');
    await Promise.all([listing(), loadCatalog(), loadModels()]);
    if (!selected) {
      const cached = localStorage.getItem('easyagent.workspaceConversation');
      if (historyRows.some((r) => r.id === cached)) await choose(cached);
    }
    if (selected) await api('/v1/conversations/' + selected + '/resume-connections', 'POST', {});
    signature = '';
    await refresh();
  });
  document.addEventListener('eah:route', (e) => {
    floating.hidden = e.detail.id === 'conversations';
    if (e.detail.id !== 'conversations')
      for (const entry of cards.values()) stopWatch(entry.details);
  });
  document.addEventListener(
    'eah:chat-workflow',
    guard(async (e) => {
      await nav.onclick();
      await choose(null);
      $('[data-destination]').value = e.detail.key;
      $('[data-destination]').dispatchEvent(new Event('change'));
      $('[data-text]').focus();
    })
  );
  setInterval(() => {
    if (page.classList.contains('active') && !document.hidden)
      refresh().catch((e) => flash(e.message));
  }, 800);
  window.addEventListener('resize', () => {
    signature = '';
    if (page.classList.contains('active')) refresh().catch((e) => flash(e.message));
  });
  restoreDraft();
}
