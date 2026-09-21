export function searchSettings({ page, api, flash, load }) {
  const card = document.createElement('section');
  card.className = 'panel search-settings';
  card.innerHTML = `<h2>联网搜索</h2><p data-search-status role="status">正在读取连接状态…</p><form data-search-settings>
    <label>TinyFish API Key<input name="api_key" type="password" autocomplete="new-password" placeholder="输入搜索 API Key"></label>
    <p class="muted">密钥加密保存在当前服务，重启后继续生效。保存后不会显示密钥原文。</p>
    <a href="https://agent.tinyfish.ai/api-keys" target="_blank" rel="noreferrer">获取 TinyFish API Key ↗</a>
    <details class="technical-details"><summary>高级连接设置</summary>
      <label>凭证来源<select name="credential_mode"><option value="key">在此填写 API Key</option><option value="environment">使用服务端环境变量</option></select></label>
      <label data-search-env hidden>环境变量名称<input name="api_key_env" value="TINYFISH_API_KEY"></label>
      <label>搜索服务地址<input name="endpoint" type="url" value="https://api.search.tinyfish.ai" required></label>
      <p>密钥会发送到此地址，仅填写你信任的兼容服务。</p>
    </details>
    <div class="actions"><button type="submit" data-save-search>保存连接</button><button type="submit" data-test-search class="primary">保存并测试</button><button type="button" data-test-current>测试当前连接</button></div>
  </form><p data-search-result role="status" aria-live="polite"></p>`;
  page.querySelector('.connection-models').after(card);
  const form = card.querySelector('form'),
    status = card.querySelector('[data-search-status]'),
    result = card.querySelector('[data-search-result]');
  let current = null,
    dirty = false,
    busy = false;
  const mode = () => form.elements.credential_mode.value;
  function fields() {
    form.elements.api_key.disabled = mode() === 'environment';
    form.elements.api_key.required = mode() === 'key' && current?.credential_source !== 'saved';
    form.elements.api_key.placeholder =
      current?.credential_source === 'saved'
        ? '已保存；留空保留，输入新密钥可替换'
        : '输入搜索 API Key';
    card.querySelector('[data-search-env]').hidden = mode() !== 'environment';
    form.elements.api_key_env.required = mode() === 'environment';
    card.querySelector('[data-test-current]').disabled =
      busy || !current?.active || !current?.configured;
  }
  form.oninput = () => {
    dirty = true;
  };
  form.elements.credential_mode.onchange = () => {
    dirty = true;
    fields();
  };
  async function refresh() {
    const connection = await api('/v1/studio/search/tinyfish');
    current = connection;
    status.textContent =
      connection.configured && connection.active
        ? connection.credential_source === 'saved'
          ? '已连接 · 已保存密钥'
          : connection.credential_source === 'environment'
            ? '已连接 · 使用服务端环境变量'
            : '已连接 · 凭证未持久保存'
        : '尚未连接';
    if (!dirty) {
      form.elements.endpoint.value = connection.endpoint;
      form.elements.api_key_env.value = connection.api_key_env || 'TINYFISH_API_KEY';
    }
    fields();
  }
  async function test() {
    result.textContent = '正在测试搜索…';
    const created = await api('/v1/studio/search/tinyfish/test', 'POST', {});
    const deadline = Date.now() + 130000;
    while (Date.now() < deadline) {
      const run = await api('/v1/runs/' + created.id);
      if (run.status === 'succeeded') {
        result.textContent = '测试通过 · 返回 ' + run.steps[0].output.results.length + ' 条结果。';
        return;
      }
      if (['failed', 'cancelled', 'needs_attention'].includes(run.status))
        throw Error(
          '搜索测试失败：' +
            (run.steps[0].error || run.status) +
            '。请检查密钥和服务地址；任务记录中保留了测试详情。'
        );
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    result.textContent = '测试仍在执行，可到任务记录查看；不会重复提交。';
  }
  async function action(fn) {
    if (busy) return;
    busy = true;
    card.querySelectorAll('button').forEach((b) => (b.disabled = true));
    try {
      await fn();
    } catch (error) {
      result.textContent = error.message;
    } finally {
      busy = false;
      card.querySelectorAll('button').forEach((b) => (b.disabled = false));
      fields();
    }
  }
  form.onsubmit = async (event) => {
    event.preventDefault();
    const shouldTest = event.submitter?.hasAttribute('data-test-search');
    await action(async () => {
      const key = mode() === 'key' ? form.elements.api_key.value.trim() : '';
      if (mode() === 'key' && !key && current?.credential_source !== 'saved')
        throw Error('请填写自己的搜索 API Key');
      result.textContent = '正在保存搜索连接…';
      await api('/v1/studio/search/tinyfish', 'POST', {
        api_key: key,
        ...(mode() === 'environment'
          ? { api_key_env: form.elements.api_key_env.value.trim() }
          : {}),
        endpoint: form.elements.endpoint.value.trim(),
        timeout_seconds: current?.timeout_seconds || 60,
      });
      form.elements.api_key.value = '';
      dirty = false;
      await refresh();
      await load();
      window.dispatchEvent(new Event('eah:connections-changed'));
      result.textContent = '搜索连接已保存';
      if (shouldTest) await test();
      else flash('搜索连接已保存');
    });
  };
  card.querySelector('[data-test-current]').onclick = () => action(test);
  return { refresh };
}
