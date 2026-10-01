export function apiSetup({ api, $, escape, download, load, flash }) {
  const config = { http_tools: [], search: [] };
  const run = (fn) => async (event) => {
    event?.preventDefault();
    $('apiSetupResult').textContent = '正在处理…';
    try {
      await fn(event);
    } catch (error) {
      $('apiSetupResult').textContent = error.message;
    }
  };
  for (const id of ['openApiSetup', 'openApiFromCanvas', 'globalApiSetup'])
    $(id) && ($(id).onclick = () => $('apiDialog').showModal());
  $('closeApiSetup').onclick = () => $('apiDialog').close();
  document.querySelectorAll('[data-api-mode]').forEach(
    (b) =>
      (b.onclick = () => {
        document
          .querySelectorAll('[data-api-panel]')
          .forEach((p) => (p.hidden = p.dataset.apiPanel !== b.dataset.apiMode));
        document
          .querySelectorAll('[data-api-mode]')
          .forEach((p) => p.classList.toggle('primary', p === b));
        $('apiSetupResult').textContent = '';
      })
  );
  function values(form) {
    const body = Object.fromEntries(new FormData(form));
    for (const key of ['api_key_env', 'server_url', 'body_parameter', 'effect'])
      if (!body[key]) delete body[key];
    return body;
  }
  async function connected(result, form) {
    form.elements.api_key.value = '';
    for (const key of ['http_tools', 'search'])
      for (const entry of result.config?.[key] || []) {
        config[key] = config[key].filter((x) => x.name !== entry.name);
        config[key].push(entry);
      }
    $('exportApiConfig').disabled = false;
    await load();
    window.dispatchEvent(new Event('eah:connections-changed'));
    $('apiSetupResult').textContent = result.message;
    flash('API 已接入');
  }
  $('tinyfishForm').onsubmit = run(async (e) =>
    connected(await api('/v1/studio/search/tinyfish', 'POST', values(e.target)), e.target)
  );
  $('apiAuth').onchange = () => {
    $('apiForm').elements.auth_header.value = ['bearer', 'basic'].includes($('apiAuth').value)
      ? 'Authorization'
      : $('apiAuth').value === 'query'
        ? 'api_key'
        : 'X-API-Key';
  };
  $('apiForm').onsubmit = run(async (e) => {
    const body = values(e.target),
      auth = $('apiAuth').value;
    for (const key of ['input_schema', 'output_schema', 'parameter_locations', 'headers'])
      body[key] = JSON.parse(body[key]);
    body.idempotent = e.target.elements.idempotent.checked;
    body.timeout_seconds = Number(body.timeout_seconds);
    body.auth_location = auth === 'query' ? 'query' : 'header';
    body.auth_prefix = auth === 'bearer' ? 'Bearer ' : auth === 'basic' ? 'Basic ' : '';
    if (auth === 'none') {
      body.api_key = '';
      delete body.api_key_env;
    }
    await connected(await api('/v1/studio/apis', 'POST', body), e.target);
  });
  function openapiBody() {
    const body = values($('openapiForm'));
    body.document = JSON.parse(body.document);
    return body;
  }
  const invalidate = () => {
    $('openapiOperations').replaceChildren();
    $('importOpenapi').disabled = true;
  };
  $('openapiForm').elements.document.oninput = invalidate;
  $('openapiForm').elements.server_url.oninput = invalidate;
  $('openapiForm').elements.prefix.oninput = invalidate;
  $('openapiFile').onchange = run(async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (file.size > 1500000) throw Error('文档请控制在 1.5 MB 内');
    $('openapiForm').elements.document.value = await file.text();
    invalidate();
    $('apiSetupResult').textContent = '文档已载入，请预览操作';
  });
  $('previewOpenapi').onclick = run(async () => {
    const result = await api('/v1/studio/apis/openapi/preview', 'POST', openapiBody());
    $('openapiOperations').innerHTML = result.operations
      .map(
        (op) =>
          `<label class="check"><input type="checkbox" value="${escape(op.id)}"><span>${escape(op.method + ' · ' + op.name)}${op.needs_credential ? ' · 需要凭证' : ''}<small style="display:block">${escape(op.description)}</small></span></label>`
      )
      .join('');
    $('openapiOperations').onchange = () => {
      $('importOpenapi').disabled = !$('openapiOperations').querySelector('input:checked');
    };
    $('apiSetupResult').textContent = result.unsupported.length
      ? '以下操作需通过手动配置或插件接入：\n' +
        result.unsupported.map((x) => x.operation_id + ': ' + x.error).join('\n')
      : '预览完成，请选择要接入的操作。';
  });
  $('openapiForm').onsubmit = run(async (e) => {
    const body = openapiBody();
    body.operations = [...$('openapiOperations').querySelectorAll('input:checked')].map(
      (x) => x.value
    );
    await connected(await api('/v1/studio/apis/openapi', 'POST', body), e.target);
  });
  $('exportApiConfig').onclick = () =>
    download(
      new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' }),
      'api-connections.json'
    );
}
