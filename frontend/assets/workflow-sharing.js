export function workflowSharing({
  api,
  $,
  escape,
  download,
  token,
  flash,
  loadWorkflow,
  reload,
  showTab,
}) {
  let packageData = null;
  const dialog = $('workflowShareDialog');
  const field = (name, key, label, control) =>
    `<label>${escape(label)}${control || `<input data-bind="${name}" data-source="${escape(key)}" value="${escape(key)}">`}</label>`;
  function bindings() {
    const body = { package: packageData, allow_development: $('shareAllowDevelopment').checked };
    dialog.querySelectorAll('[data-bind]').forEach((el) => {
      if (el.value) (body[el.dataset.bind] ??= {})[el.dataset.source] = el.value;
    });
    return body;
  }
  function status(result) {
    $('workflowShareStatus').textContent = result.ready
      ? '依赖齐全。导入不执行流程。'
      : result.missing.join('\n');
  }
  async function open(data) {
    packageData = data;
    const [preview, models, tools] = await Promise.all([
      api('/v1/workflow-packages/preview', 'POST', { package: data }),
      api('/v1/models'),
      api('/v1/tools'),
    ]);
    const req = preview.requirements;
    $('workflowShareSummary').innerHTML =
      `<h3>${escape(preview.name)}</h3><p>${preview.node_count} 个步骤（含嵌套子流程） · ${preview.api_count} 个随包 API</p><p class="muted">保留参数、连线、预算、审批与指令快照。密钥、历史运行、产物及知识库内容不随包复制。</p><details><summary>查看服务地址与外部操作</summary><pre class="output">${escape(JSON.stringify({ 服务: req.network_origins, 写操作: req.write_tools, 运行时开发: req.development }, null, 2))}</pre></details>`;
    let html = (req.extension_packages || [])
      .map(
        (p, i) =>
          `<p>${escape(p.manifest.title)} · v${p.manifest.revision} <button type="button" data-bundled="${i}">检查并安装扩展</button></p>`
      )
      .join('');
    for (const [name, caps] of Object.entries(req.models)) {
      const compatible = models.filter((m) => caps.every((c) => m.capabilities.includes(c)));
      html += field(
        'model_bindings',
        name,
        '模型 ' + name,
        `<select data-bind="model_bindings" data-source="${escape(name)}"><option value="">请选择本机模型</option>${compatible.map((m) => `<option value="${escape(m.alias)}" ${m.alias === name ? 'selected' : ''}>${escape(m.alias + ' · ' + m.model)}</option>`).join('')}</select>`
      );
    }
    for (const spec of req.external_tools) {
      const same = tools.find((t) => t.name === spec.name);
      html += field(
        'tool_bindings',
        spec.name,
        '外部工具 ' + spec.name,
        `<select data-bind="tool_bindings" data-source="${escape(spec.name)}"><option value="">请选择已安装工具</option>${tools.map((t) => `<option value="${escape(t.name)}" ${same?.name === t.name ? 'selected' : ''}>${escape(t.name)}</option>`).join('')}</select>`
      );
    }
    for (const ref of req.credentials)
      html += field('credential_bindings', ref, '连接中心凭证名称或环境变量 ' + ref);
    for (const ns of req.knowledge) html += field('knowledge_bindings', ns, '本机知识库对应 ' + ns);
    for (const ns of req.memory) html += field('memory_bindings', ns, '本机记忆分组对应 ' + ns);
    $('workflowShareBindings').innerHTML = html;
    dialog.querySelectorAll('[data-bundled]').forEach(
      (b) =>
        (b.onclick = async () => {
          try {
            await api('/v1/workflow-packages/dependencies', 'POST', bindings());
            window.dispatchEvent(
              new CustomEvent('eah:extension-package', {
                detail: req.extension_packages[Number(b.dataset.bundled)],
              })
            );
          } catch (error) {
            $('workflowShareStatus').textContent = error.message;
          }
        })
    );
    $('shareDevelopmentLabel').hidden = !req.development.length;
    $('shareAllowDevelopment').checked = false;
    status(preview);
    dialog.showModal();
  }
  async function fileDownload(url) {
    const response = await fetch(url, {
      headers: token() ? { Authorization: 'Bearer ' + token() } : {},
    });
    if (!response.ok) {
      const result = await response.json();
      throw Error(
        typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail)
      );
    }
    download(token() ? await response.blob() : url, 'workflow.eah-workflow.json');
    flash('已导出完整工作流包，包含当前输入和指令。接收端可绑定自己的模型与凭证。');
  }
  async function share(workflow) {
    const data = await api('/v1/workflow-packages/export', 'POST', { workflow });
    await fileDownload('/v1/workflow-packages/exports/' + data.digest);
  }
  $('closeWorkflowShare').onclick = () => dialog.close();
  $('checkWorkflowShare').onclick = async () => {
    try {
      status(await api('/v1/workflow-packages/preview', 'POST', bindings()));
    } catch (error) {
      $('workflowShareStatus').textContent = error.message;
    }
  };
  $('workflowShareForm').onsubmit = async (e) => {
    e.preventDefault();
    const button = $('confirmWorkflowShare');
    button.disabled = true;
    try {
      const result = await api('/v1/workflow-packages/import', 'POST', bindings());
      dialog.close();
      await reload();
      loadWorkflow(result.workflow, result);
      showTab('workflow');
      flash('工作流已导入');
    } catch (error) {
      $('workflowShareStatus').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  $('importSharedWorkflow').onclick = () => $('sharedWorkflowFile').click();
  $('sharedWorkflowFile').onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    try {
      if (file.size > 1_700_000) throw Error('分享包应小于 1.7 MB');
      await open(JSON.parse(await file.text()));
    } catch (error) {
      flash(error.message);
    }
  };
  return {
    share,
    open,
    exportSaved: async (id, revision) =>
      fileDownload(
        '/v1/studio/workflows/' + encodeURIComponent(id) + '/package?revision=' + revision
      ),
  };
}
