export function operationsStudio({ api, escape, flash, showTab, token, download }) {
  const nav = document.createElement('button');
  nav.dataset.tab = 'operations';
  nav.textContent = '诊断与维护';
  document.querySelector('.nav').append(nav);
  const page = document.createElement('section');
  page.id = 'operations';
  page.className = 'section';
  page.innerHTML = `<div class="intro"><h1>诊断与备份</h1></div><button data-refresh>刷新诊断</button><div class="diagnostic-grid" data-summary></div><details class="technical-details"><summary>查看完整诊断数据</summary><pre class="output" data-doctor></pre></details><div class="panel"><h2>后台维护</h2><p class="muted">整理与复盘会调用模型，可能产生费用。</p><form data-maintenance><label><input name="auto_compact" type="checkbox">长对话自动整理上下文，保留原始记录</label><label>整理阈值（字符数）<input name="compact_after_chars" type="number" min="4000" max="500000" value="48000"></label><label>自动复盘模型<select name="review_model"></select></label><label>每天最多复盘次数（0 表示关闭）<input name="review_limit_per_day" type="number" min="0" max="100" value="0"></label><p class="muted">仅复盘明确设置了 learn_as 的流程；经验候选不会自动发布。</p><button>保存维护设置</button></form><div data-jobs></div></div><div class="panel" style="margin-top:18px"><h2>加密备份</h2><form data-backup><label>备份密码（至少 12 位，请妥善保管）<input name="password" type="password" minlength="12" required autocomplete="new-password"></label><button>下载加密备份</button></form><p class="muted">包含数据库与凭证解密密钥，使用该密码加密。恢复时使用 easyagent restore 并选择新的数据库路径。</p></div>`;
  document.querySelector('main').append(page);
  const $ = (s) => page.querySelector(s),
    guard = (fn) => async (e) => {
      try {
        await fn(e);
      } catch (err) {
        flash(err.message);
      }
    };
  async function refresh() {
    const [doctor, settings, jobs, models] = await Promise.all([
      api('/v1/operations/doctor'),
      api('/v1/maintenance/settings'),
      api('/v1/maintenance/jobs'),
      api('/v1/models'),
    ]);
    const active = Object.entries(doctor.runs)
      .filter(([s]) => !['succeeded', 'failed', 'cancelled'].includes(s))
      .reduce((n, [, v]) => n + v, 0);
    $('[data-summary]').innerHTML = [
      [
        doctor.database.integrity === 'ok' ? '正常' : '需检查',
        '数据库',
        Math.round(doctor.database.size_bytes / 1024 / 1024) + ' MB',
      ],
      [doctor.workers, '工作进程', ''],
      [active, '未完成任务', '含执行中与等待确认'],
      [
        Array.isArray(doctor.extensions) ? doctor.extensions.length : doctor.extensions,
        '已启用扩展',
        '',
      ],
      [doctor.deliveries.failed || 0, '发送失败', '在模型与服务中查看回执'],
      [
        Math.round(doctor.oldest_active_seconds / 60) + ' 分钟',
        '最久等待',
        '最早排队或执行任务的持续时间',
      ],
    ]
      .map(
        ([value, label, detail]) =>
          `<div class=diagnostic-card><small>${escape(label)}</small><strong>${escape(value)}</strong>${detail ? '<p>' + escape(detail) + '</p>' : ''}</div>`
      )
      .join('');
    $('[data-doctor]').textContent = JSON.stringify(
      {
        数据库: doctor.database.integrity,
        工作进程: doctor.workers,
        运行队列: doctor.runs,
        投递队列: doctor.deliveries,
        最早未完成任务秒数: Math.round(doctor.oldest_active_seconds),
        已启用扩展: doctor.extensions,
        运行环境: doctor.runtimes,
      },
      null,
      2
    );
    $('[name=auto_compact]').checked = settings.auto_compact;
    $('[name=compact_after_chars]').value = settings.compact_after_chars;
    $('[name=review_limit_per_day]').value = settings.review_limit_per_day;
    $('[name=review_model]').innerHTML =
      '<option value="">不选择</option>' +
      models
        .filter((m) => m.capabilities.includes('decision'))
        .map(
          (m) =>
            `<option value="${escape(m.alias)}" ${settings.review_model === m.alias ? 'selected' : ''}>${escape(m.alias)}</option>`
        )
        .join('');
    $('[data-jobs]').innerHTML = jobs
      .slice(0, 10)
      .map(
        (j) =>
          `<p>${escape({ compact: '整理对话', review: '任务复盘' }[j.kind] || j.kind)} · ${escape({ succeeded: '已完成', failed: '未完成', running: '处理中', queued: '排队中' }[j.status] || j.status)} ${escape(j.error || '')}</p>`
      )
      .join('');
  }
  nav.onclick = guard(async () => {
    showTab('operations');
    await refresh();
  });
  $('[data-refresh]').onclick = guard(refresh);
  $('[data-maintenance]').onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await api('/v1/maintenance/settings', 'PUT', {
      auto_compact: f.has('auto_compact'),
      compact_after_chars: Number(f.get('compact_after_chars')),
      review_limit_per_day: Number(f.get('review_limit_per_day')),
      review_model: f.get('review_model') || null,
    });
    flash('后台维护设置已保存');
  });
  $('[data-backup]').onsubmit = guard(async (e) => {
    e.preventDefault();
    const button = e.target.querySelector('button');
    button.disabled = true;
    try {
      const r = await fetch('/v1/operations/backup' + (token() ? '' : '?link=true'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token() ? { Authorization: 'Bearer ' + token() } : {}),
        },
        body: JSON.stringify({ password: new FormData(e.target).get('password') }),
      });
      if (!r.ok) throw Error('备份失败，请查看诊断');
      download(token() ? await r.blob() : (await r.json()).url, 'easyagent-backup.eah');
      e.target.reset();
      flash('加密备份已下载');
    } finally {
      button.disabled = false;
    }
  });
}
