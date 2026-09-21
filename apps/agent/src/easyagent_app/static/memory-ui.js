export function memoryUI({ api, escape, flash }) {
  const $ = (id) => document.getElementById(id),
    panel = $('memoryResults').parentElement;
  const actions = document.createElement('div');
  actions.className = 'actions';
  actions.style.marginTop = '15px';
  actions.innerHTML =
    '<button id="mergeMemory" disabled>合并所选内容</button><button id="memoryHistory">查看合并历史</button>';
  panel.append(actions);
  const history = document.createElement('div');
  history.id = 'memoryHistoryResults';
  panel.append(history);
  const dialog = document.createElement('dialog');
  dialog.innerHTML =
    '<div class="dialog-heading"><h2>合并偏好</h2><button type="button" data-close>关闭 ×</button></div><p class="muted">将所选内容整理成一条，原文和来源保留在合并历史中。</p><div data-sources></div><form><label>合并后的名称<input name="destination" required maxlength="160"></label><label>整理后的内容<textarea name="value" required rows="5"></textarea></label><label>过期时间（可选）<input name="expires" type="datetime-local"></label><div class="actions"><button class="primary">保存合并</button><button type="button" data-cancel>取消</button></div></form>';
  document.body.append(dialog);
  const guard = (fn) => async (e) => {
    try {
      await fn(e);
    } catch (err) {
      flash(err.message);
    }
  };
  let rows = [],
    namespace = '',
    mergeSources = [],
    mergeNamespace = '';
  const display = (value) => (typeof value === 'string' ? value : JSON.stringify(value, null, 2));
  async function find() {
    namespace = $('memoryGroup').value.trim();
    if (!namespace) throw Error('填写要查看的分组');
    rows = await api(
      '/v1/memory/' + encodeURIComponent(namespace) + '?include_digest=true&limit=100'
    );
    $('memoryResults').innerHTML =
      rows
        .map(
          (r, i) =>
            `<div class="notice"><label class="check"><input type="checkbox" data-memory="${i}"><b>${escape(r.key)}</b></label><p>${escape(display(r.value))}</p><small>来源：${escape(r.source.startsWith('operator; archives:') ? '用户整理 · 原文见合并历史' : r.source)}</small></div>`
        )
        .join('') || '<p class="empty-hint">该分组暂无内容</p>';
    $('mergeMemory').disabled = true;
    $('memoryResults').onchange = () => {
      $('mergeMemory').disabled = !$('memoryResults').querySelector(':checked');
    };
    history.replaceChildren();
  }
  $('findMemory').onclick = guard(find);
  $('memoryGroup').oninput = () => {
    $('mergeMemory').disabled = true;
    $('memoryResults').replaceChildren();
    history.replaceChildren();
  };
  $('mergeMemory').onclick = guard(() => {
    mergeSources = [...$('memoryResults').querySelectorAll(':checked')].map(
      (el) => rows[Number(el.dataset.memory)]
    );
    mergeNamespace = namespace;
    if (!mergeSources.length) return;
    dialog.querySelector('[data-sources]').innerHTML = mergeSources
      .map((r) => `<p><b>${escape(r.key)}</b>：${escape(display(r.value))}</p>`)
      .join('');
    const form = dialog.querySelector('form');
    form.reset();
    form.elements.destination.value = mergeSources[0].key;
    form.elements.value.value = mergeSources.map((r) => display(r.value)).join('\n');
    dialog.showModal();
  });
  dialog.querySelector('[data-close]').onclick = dialog.querySelector('[data-cancel]').onclick =
    () => dialog.close();
  dialog.querySelector('form').onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await api('/v1/memory/' + encodeURIComponent(mergeNamespace) + '/merge', 'POST', {
      destination: f.get('destination'),
      value: f.get('value'),
      sources: Object.fromEntries(mergeSources.map((r) => [r.key, r.digest])),
      expires: f.get('expires') ? new Date(f.get('expires')).getTime() / 1000 : null,
    });
    dialog.close();
    await find();
    flash('偏好已合并，原文已保留在历史中');
  });
  $('memoryHistory').onclick = guard(async () => {
    const group = $('memoryGroup').value.trim();
    if (!group) throw Error('请填写分组');
    const rows = await api('/v1/memory/' + encodeURIComponent(group) + '/history');
    history.innerHTML =
      '<h3 style="margin-top:20px">合并前的原文</h3>' +
      (rows
        .map(
          (r) =>
            `<div class="notice"><b>${escape(r.key)}</b><p>${escape(display(r.value))}</p><small>来源：${escape(r.source)} · ${new Date(r.archived * 1000).toLocaleString()}<br>合并到：${escape(r.reason.replace(/^merge:/, ''))}</small></div>`
        )
        .join('') || '<p class="empty-hint">暂无合并历史</p>');
  });
}
