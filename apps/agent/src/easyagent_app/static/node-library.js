export function nodeLibrary({api,$,escape,flash,guard,download,reload,add,token}) {
  let rows=[],selected=null,packageData=null,sources=[];
  const descriptions={
    'library.typesafe.evaluate':'对任意内容做分类、布尔判断或评分，保留概率和原始响应。',
    'library.runway.image':'从文字描述生成图片。提交后用等待节点获取结果。',
    'library.runway.video':'从文字描述生成 2–10 秒视频。提交后用等待节点获取结果。',
    'library.runway.wait':'持续查询生成结果；等待期间释放执行资源，支持重启恢复。',
    'library.runway.cancel':'取消进行中的任务，或删除已完成任务及结果。',
    'library.elevenlabs.speech':'把文字转换为 MP3 并保存为可下载文件。需要声音 ID。'
  };
  const mediaFields=document.createElement('div');mediaFields.hidden=true;
  mediaFields.innerHTML='<label>使用已保存的服务连接<select name="connection" id="libraryMediaConnection"></select></label><label>该服务提供的模型 ID<input name="model" id="libraryMediaModel" placeholder="填写服务提供的准确模型 ID"></label><p class="muted">节点已内置；连接只绑定地址和凭证，不会生成媒体或上传文件。</p><button type="button" id="libraryAddConnection">添加模型服务连接</button>';
  $('libraryConnectionForm').prepend(mediaFields);
  const credentialLabel=$('libraryConnectionForm').elements.api_key.closest('label');
  $('libraryAddConnection').onclick=()=>{$('libraryConnection').close();document.querySelector('[data-tab="connections"]').click();$('connectBtn').click();};
  async function connect(row){
    selected=row;mediaFields.hidden=!row.builtin_media;credentialLabel.hidden=!!row.builtin_media;
    $('libraryConnectionTitle').textContent='连接 · '+row.title;$('libraryConnectionResult').textContent='';$('libraryConnectionForm').reset();
    if(row.builtin_media){
      const data=await api('/v1/studio/connections'),connections=data.connections.filter(m=>m.managed&&m.dialect!=='anthropic');
      $('libraryMediaConnection').replaceChildren(...connections.map(m=>new Option(m.alias+' · '+m.model,m.alias)));
      const usesModel=Object.hasOwn(row.defaults||row.manifest?.defaults||{},'model');
      $('libraryMediaModel').closest('label').hidden=!usesModel;$('libraryMediaModel').required=usesModel;$('libraryMediaConnection').required=true;
      function suggest(){const selected=connections.find(m=>m.alias===$('libraryMediaConnection').value);$('libraryMediaModel').value=usesModel?(row.id.includes('image_')&&selected?.capabilities.includes('image')?selected.model:(row.defaults||row.manifest?.defaults).model):'';}
      $('libraryMediaConnection').onchange=suggest;suggest();
      $('libraryConnectionInfo').textContent=(row.protocol||'媒体协议')+'：所选服务必须提供此协议。凭证沿用加密保存的连接。'+(connections.length?'':'请先添加一个服务连接，再回来绑定节点。');
    }else{
      $('libraryMediaConnection').required=false;$('libraryMediaModel').required=false;
      $('libraryConnectionInfo').textContent='密钥仅保留在服务端内存。也可预先设置 '+row.credential_ref+'，重启后自动读取。';
    }
    $('libraryConnection').showModal();
  }
  const componentType=r=>r.component_type||(r.manifest?.source.kind==='workflow'?'subworkflow':'node');
  const typeLabel=r=>componentType(r)==='subworkflow'?'子工作流':'节点';
  function render(){
    const query=$('librarySearch').value.toLowerCase();
    $('nodeLibraryList').innerHTML=rows.filter(r=>!$('libraryType').value||componentType(r)===$('libraryType').value).filter(r=>(r.title+r.id+r.category).toLowerCase().includes(query)).map(r=>`<article class="library-card"><b>${escape(r.title)}</b><small>${typeLabel(r)} · ${escape(r.category)} · ${r.builtin_media?(r.available?'默认内置 · 已连接':'默认内置 · 待连接'):r.builtin?'内置节点':r.available?'已连接 · v'+r.manifest.revision:r.installed?'需要配置凭证':'待连接'}</small><p>${escape(descriptions[r.id]||r.description)}</p><small>${r.imported?'由文件导入 · 尚未在本机验证':r.manifest?.validation==='live_verified'?'真实服务已验证':r.manifest?.validation==='protocol_integration'?'协议集成测试通过 · 真实任务需单独验证':'可查看定义与验证范围'}</small><div class="actions"><button class="small ${r.available?'primary':''}" data-library-id="${escape(r.id)}">${r.available?'加入画布':r.builtin_media?'选择服务连接':r.installed?'查看连接要求':'连接服务'}</button>${r.builtin_media&&r.installed?`<button class="small" data-media-configure="${escape(r.id)}">修改连接</button>`:''}${r.docs?.[0]&&/^https?:\/\//.test(r.docs[0])?`<a href="${escape(r.docs[0])}" target="_blank" rel="noreferrer">接口文档 ↗</a>`:''}<button class="small" data-library-detail="${escape(r.id)}">定义</button>${r.installed||r.builtin?`<button class="small" data-package="${escape(r.id)}">导出组件包</button>`:''}</div></article>`).join('')||'<p class="muted">没有匹配的节点或子工作流。</p>';
    $('nodeLibraryList').querySelectorAll('[data-library-id]').forEach(b=>b.onclick=guard(async()=>{
      const row=rows.find(r=>r.id===b.dataset.libraryId);
      if(row.available){const result=await api('/v1/library/'+encodeURIComponent(row.id)+'/instantiate','POST',{revision:row.manifest?.revision});add(result.step);flash(typeLabel(row)+'已加入画布，可填写参数');return;}
      if(row.builtin_media){await connect(row);return;}
      if(row.installed&&!row.credential_ref){flash(row.resolution.candidates.flatMap(c=>c.reasons).join('；')+'。请在服务端配置这些凭证。');return;}
      await connect(row);
    }));
    $('nodeLibraryList').querySelectorAll('[data-media-configure]').forEach(b=>b.onclick=guard(()=>connect(rows.find(r=>r.id===b.dataset.mediaConfigure))));
    $('nodeLibraryList').querySelectorAll('[data-library-detail]').forEach(b=>b.onclick=()=>{
      const row=rows.find(r=>r.id===b.dataset.libraryDetail);
      download(new Blob([JSON.stringify(row.manifest||row.definition||row,null,2)],{type:'application/json'}),row.id+'.json');
    });
    $('nodeLibraryList').querySelectorAll('[data-package]').forEach(b=>b.onclick=guard(async()=>{
      const row=rows.find(r=>r.id===b.dataset.package);
      if(row.builtin)await api('/v1/library/'+encodeURIComponent(row.id)+'/instantiate','POST',{});
      const url='/v1/library/'+encodeURIComponent(row.id)+'/package'+(row.manifest?'?revision='+row.manifest.revision:'');
      const body=await api(url);
      download(token()?new Blob([JSON.stringify(body,null,2)],{type:'application/json'}):url,row.id+'.eah-component.json');
      flash('已导出'+typeLabel(row)+'及固定版本依赖；接收端需配置凭证。');
    }));
  }
  $('librarySearch').oninput=render;
  $('libraryType').onchange=render;
  $('closeLibraryConnection').onclick=()=>$('libraryConnection').close();
  $('libraryConnectionForm').onsubmit=async e=>{
    e.preventDefault();const button=e.submitter||e.target.querySelector('button:not([type=button])');button.disabled=true;
    try{await api('/v1/library/'+encodeURIComponent(selected.id)+'/install','POST',selected.builtin_media?{connection:$('libraryMediaConnection').value,model:$('libraryMediaModel').value||null}:{api_key:new FormData(e.target).get('api_key')||''});e.target.reset();$('libraryConnection').close();await reload();window.dispatchEvent(new Event('eah:connections-changed'));flash(selected.builtin_media?'默认媒体节点已连接；可加入画布或返回对话继续':'节点已加入库；配套流程列在“子工作流”中');}
    catch(error){$('libraryConnectionResult').textContent=error.message;}
    finally{button.disabled=false;}
  };
  $('importComponent').onclick=()=>$('componentFile').click();
  $('componentFile').onchange=guard(async e=>{
    const file=e.target.files[0];e.target.value='';if(!file)return;
    if(file.size>5_000_000)throw Error('组件包不能超过 5 MB');
    packageData=JSON.parse(await file.text());
    const preview=await api('/v1/library/packages/preview','POST',{package:packageData});
    $('componentPreview').innerHTML=`<p><b>${escape(packageData.root)} · v${escape(packageData.root_revision)}</b></p><p>${preview.definition_count} 个定义 · ${preview.components.length} 个组件</p><pre class="output">${escape(packageData.definitions.filter(r=>r.kind==='component').map(r=>`${r.body.title}\n权限：${{read:'读取服务',write:'修改外部数据',local:'本机处理与保存'}[r.body.effect]||r.body.effect}\n服务：${r.body.requirements.network_origins.join('、')||'本地'}\n能力：${r.body.requirements.capabilities.join('、')}`).join('\n\n'))}</pre><p class="muted">SHA-256 用于完整性校验，文件未签名，发布者身份未验证。请确认上面的权限与服务地址。</p>`;
    $('componentBindings').innerHTML=preview.credential_refs.map(ref=>`<label>本机凭证变量：${escape(ref)}<input data-credential="${escape(ref)}" value="${escape(ref)}" required pattern="[A-Za-z_][A-Za-z0-9_]*"></label>`).join('');
    $('componentImportResult').textContent='填写服务端环境变量名称，不填写密钥值。';$('componentImportDialog').showModal();
  });
  $('closeComponentImport').onclick=()=>$('componentImportDialog').close();
  $('componentImportForm').onsubmit=async e=>{
    e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
    try{const bindings=Object.fromEntries([...$('componentBindings').querySelectorAll('[data-credential]')].map(i=>[i.dataset.credential,i.value.trim()]));await api('/v1/library/packages/import','POST',{package:packageData,credential_bindings:bindings});$('componentImportDialog').close();await reload();flash('已导入能力库，可按节点或子工作流筛选；尚未执行。');}
    catch(error){$('componentImportResult').textContent=error.message;}
    finally{button.disabled=false;}
  };
  function fillSource(){
    const row=sources[Number($('componentSource').value)];if(!row)return;
    $('componentSourceType').textContent='保存为：'+typeLabel(row)+(componentType(row)==='subworkflow'?'（保留内部步骤和子运行）':'（一个执行步骤）');
    const form=$('componentPublishForm');form.elements.id.value=('shared.'+row.id).slice(0,101);
    form.elements.title.value=row.workflow?.name||row.id;form.elements.description.value=row.definition?.description||row.workflow?.name||row.id;
  }
  $('publishLibrary').onclick=guard(async()=>{
    sources=await api('/v1/library/sources');
    $('componentSource').innerHTML=sources.map((r,i)=>`<option value="${i}">${escape(r.workflow?.name||r.id)} · ${typeLabel(r)} · v${r.revision}</option>`).join('');
    $('componentPublishResult').textContent=sources.length?'':'先接入 API、保存节点或保存工作流。';fillSource();$('componentPublishDialog').showModal();
  });
  $('componentSource').onchange=fillSource;
  $('closeComponentPublish').onclick=()=>$('componentPublishDialog').close();
  $('componentPublishForm').onsubmit=async e=>{
    e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
    try{const source=sources[Number($('componentSource').value)];if(!source)throw Error('请先保存来源定义');const values=Object.fromEntries(new FormData(e.target));await api('/v1/library/publish','POST',{...values,kind:source.kind,source_id:source.id,source_revision:source.revision,defaults:source.workflow?.inputs||source.definition?.defaults||{},input_schema:source.workflow?.metadata?.component_input_schema||source.workflow?.metadata?.input_schema||source.definition?.input_schema||{type:'object'}});$('componentPublishDialog').close();await reload();flash(typeLabel(source)+'已加入能力库');}
    catch(error){$('componentPublishResult').textContent=error.message;}
    finally{button.disabled=false;}
  };
  return {async refresh(){rows=(await api('/v1/library')).sort((a,b)=>Number(!!b.builtin_media)-Number(!!a.builtin_media));render();}};
}
