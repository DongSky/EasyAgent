export function skillStudio({ api, escape, flash, load, showTab, download }) {
  const nav = document.createElement('button');
  nav.dataset.tab = 'skills';
  nav.textContent = '使用技巧';
  document.querySelector('.nav').append(nav);
  const page = document.createElement('section');
  page.id = 'skills';
  page.className = 'section';
  page.innerHTML = `<div class="intro"><h1>使用技巧</h1><p class="muted">在对话中输入 /技巧名称 调用；自动构建也可选用。</p></div><div class="actions"><button id="skillAuthor">生成技巧</button><button id="skillImport">导入技巧包</button><button id="skillFolder">导入文件夹</button><button id="skillSource">从 GitHub 添加</button><button id="skillRefresh">刷新</button><input id="skillFile" type="file" accept=".json,.md" hidden><input id="skillDirectory" type="file" webkitdirectory multiple hidden></div><label>查找技巧<input id="skillSearch" type="search" placeholder="搬家、旅行、通知"></label><div id="skillRows"></div><h2>示例技巧</h2><div id="skillLibrary" class="library-grid"></div><dialog id="skillPreview"><div class="dialog-heading"><h2>安装使用技巧</h2><button data-close>关闭 ×</button></div><div data-detail></div><p class="muted">安装会提供做事说明，不会授予工具权限或自动执行脚本。</p><div class="actions"><button id="skillConfirm" class="primary">安装并启用</button></div></dialog><dialog id="skillSourceDialog"><div class="dialog-heading"><h2>从 GitHub 添加</h2><button data-close>关闭 ×</button></div><form id="skillSourceForm"><label>仓库<input name="repository" value="NousResearch/hermes-agent" required placeholder="owner/repository"></label><label>分支、标签或提交<input name="ref" value="HEAD" required></label><label>技巧目录（留空浏览）<input name="path" placeholder="skills/example"></label><button class="primary" type="submit">读取并预览</button><p data-source-status></p><div data-source-results></div></form></dialog>`;
  document.querySelector('main').append(page);
  const $ = (id) => document.getElementById(id);
  let rows = [],
    candidate = null;
  const guard = (fn) => async (e) => {
    try {
      await fn(e);
    } catch (err) {
      flash(err.message);
    }
  };
  page
    .querySelectorAll('[data-close]')
    .forEach((b) => (b.onclick = () => b.closest('dialog').close()));
  nav.onclick = guard(async () => {
    showTab('skills');
    await refresh();
  });
  async function preview(packageData, readOnly = false) {
    const detail = await api('/v1/skill-packages/preview', 'POST', packageData);
    const current = rows.filter((r) => r.name === detail.name);
    candidate = {
      package: packageData,
      expected_revision: Math.max(0, ...current.map((r) => r.revision)),
    };
    $('skillPreview').querySelector('[data-detail]').innerHTML =
      `<h3>${escape(detail.name)}</h3><p>${escape(detail.description)}</p><p>${detail.files.length} 个文件 · ${candidate.expected_revision ? '更新已安装技巧' : '新技巧'}</p><p class="muted">来源：${escape(JSON.stringify(detail.source))}</p><details><summary>检查正文与文件</summary><pre class="output">${escape(packageData.files['SKILL.md'])}</pre>${detail.files.map((f) => `<p>${escape(f)}</p>`).join('')}</details>`;
    $('skillConfirm').hidden = readOnly;
    $('skillPreview').querySelector('h2').textContent = readOnly ? '查看使用技巧' : '安装使用技巧';
    $('skillPreview').showModal();
  }
  function draw() {
    const q = $('skillSearch').value.toLowerCase();
    const groups = new Map();
    rows.forEach((r) => {
      if (!groups.has(r.name)) groups.set(r.name, []);
      groups.get(r.name).push(r);
    });
    $('skillRows').replaceChildren();
    for (const [name, versions] of groups) {
      const latest = versions.at(-1),
        active = versions.find((r) => r.active),
        chosen = active || latest;
      if (!`${name} ${chosen.description}`.toLowerCase().includes(q)) continue;
      const card = document.createElement('article');
      card.className = 'panel';
      card.style.marginTop = '14px';
      card.innerHTML = `<h3>${escape(name)}</h3><p>${escape(chosen.description)}</p><p class="muted">${active ? '已启用 · 版本 ' + active.revision : '已停用'} · 对话输入 /${escape(name)}</p><div class="actions"><select aria-label="技巧版本">${versions.map((r) => `<option value="${r.revision}" ${r === chosen ? 'selected' : ''}>版本 ${r.revision}</option>`).join('')}</select><button data-view>查看内容</button><button data-export>导出</button><button data-enable>启用此版本</button><button data-disable ${active ? '' : 'disabled'}>停用</button>${latest.source.repository ? '<button data-update>检查源仓库更新</button>' : ''}</div>`;
      const rev = () => Number(card.querySelector('select').value);
      card.querySelector('[data-enable]').onclick = guard(async () => {
        await api(`/v1/skill-packages/${name}/activate`, 'POST', { revision: rev() });
        await refresh();
        await load();
      });
      card.querySelector('[data-disable]').onclick = guard(async () => {
        await api(`/v1/skill-packages/${name}/activate`, 'POST', { revision: null });
        await refresh();
        await load();
      });
      card.querySelector('[data-view]').onclick = guard(async () => {
        const r = await api(`/v1/skill-packages/${name}?revision=${rev()}`);
        await preview(r.package, true);
      });
      card.querySelector('[data-export]').onclick = guard(async () => {
        const r = await api(`/v1/skill-packages/${name}?revision=${rev()}`);
        download(
          new Blob([JSON.stringify(r.package, null, 2)], { type: 'application/json' }),
          name + '.eah-skill.json'
        );
      });
      card.querySelector('[data-update]')?.addEventListener(
        'click',
        guard(async () => {
          const r = await api('/v1/skill-packages/source', 'POST', {
            repository: latest.source.repository,
            path: latest.source.path,
            ref: 'HEAD',
          });
          if (r.digest === latest.digest) {
            flash('当前版本与源仓库一致');
            return;
          }
          await preview(r.package);
        })
      );
      $('skillRows').append(card);
    }
  }
  async function refresh() {
    rows = await api('/v1/skill-packages');
    draw();
    const builtin = await api('/v1/skill-library');
    $('skillLibrary').innerHTML = builtin
      .map(
        (b, i) =>
          `<button class="template" data-skill-example="${i}"><h3>${escape(b.title)}</h3><p>查看步骤并添加</p></button>`
      )
      .join('');
    $('skillLibrary')
      .querySelectorAll('button')
      .forEach((b) => (b.onclick = guard(() => preview(builtin[+b.dataset.skillExample].package))));
  }
  const author = document.createElement('dialog');
  author.innerHTML =
    '<div class="dialog-heading"><h2>生成技巧</h2><button type=button data-close>关闭 ×</button></div><form><label>描述步骤或粘贴参考材料<textarea name=requirement required placeholder="例如：比较搬家报价时，要分别核对基础费用、楼层费和取消条件…"></textarea></label><button class=primary>生成并预览</button><p data-status></p></form>';
  page.append(author);
  author.querySelector('[data-close]').onclick = () => author.close();
  $('skillAuthor').onclick = () => author.showModal();
  author.querySelector('form').onsubmit = guard(async (e) => {
    e.preventDefault();
    const button = e.target.querySelector('button'),
      status = e.target.querySelector('[data-status]');
    button.disabled = true;
    try {
      status.textContent = '正在整理步骤…';
      const { id } = await api('/v1/skill-drafts', 'POST', {
        requirement: e.target.elements.requirement.value,
      });
      const deadline = Date.now() + 150000;
      while (Date.now() < deadline) {
        const r = await api('/v1/skill-drafts/' + id);
        if (r.status === 'ready') {
          author.close();
          await preview(r.package);
          return;
        }
        if (['failed', 'cancelled'].includes(r.status)) throw Error(r.errors.join('；'));
        await new Promise((r) => setTimeout(r, 500));
      }
      throw Error('生成仍在继续，请查看任务记录');
    } catch (err) {
      status.textContent = err.message;
      throw err;
    } finally {
      button.disabled = false;
    }
  });
  $('skillConfirm').onclick = guard(async () => {
    await api('/v1/skill-packages/install', 'POST', candidate);
    $('skillPreview').close();
    await refresh();
    await load();
    flash('技巧已安装');
  });
  $('skillRefresh').onclick = guard(refresh);
  $('skillSearch').oninput = draw;
  $('skillImport').onclick = () => $('skillFile').click();
  $('skillFolder').onclick = () => $('skillDirectory').click();
  $('skillFile').onchange = guard(async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (file.size > 1500000) throw Error('文件超过 1.5 MB');
    await preview(
      file.name.endsWith('.md')
        ? { files: { 'SKILL.md': await file.text() } }
        : JSON.parse(await file.text())
    );
  });
  $('skillDirectory').onchange = guard(async (e) => {
    const files = [...e.target.files];
    e.target.value = '';
    if (files.length > 80 || files.reduce((n, f) => n + f.size, 0) > 1500000)
      throw Error('文件夹过大');
    const entries = {};
    for (const f of files)
      entries[f.webkitRelativePath.split('/').slice(1).join('/')] = await f.text();
    await preview({ files: entries });
  });
  $('skillSource').onclick = () => $('skillSourceDialog').showModal();
  $('skillSourceForm').onsubmit = guard(async (e) => {
    e.preventDefault();
    const form = e.target,
      status = form.querySelector('[data-source-status]');
    status.textContent = '正在读取来源…';
    try {
      const r = await api(
        '/v1/skill-packages/source',
        'POST',
        Object.fromEntries(new FormData(form))
      );
      if (r.package) {
        $('skillSourceDialog').close();
        await preview(r.package);
      } else {
        form.elements.ref.value = r.ref;
        form.querySelector('[data-source-results]').innerHTML = r.skills
          .map((p) => `<button type="button" data-path="${escape(p)}">${escape(p)}</button>`)
          .join('');
        form.querySelectorAll('[data-path]').forEach(
          (b) =>
            (b.onclick = () => {
              form.elements.path.value = b.dataset.path;
              form.requestSubmit();
            })
        );
      }
      status.textContent = '已固定来源提交，请选择技巧。';
    } catch (err) {
      status.textContent = err.message;
      throw err;
    }
  });
}
