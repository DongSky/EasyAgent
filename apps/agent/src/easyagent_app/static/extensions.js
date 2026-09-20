import {schemaForm} from './schema-form.js';

export function extensionStudio({api,escape,flash,load,showTab,watch,token,download}) {
  const nav=document.createElement('button');nav.dataset.tab='extensions';nav.textContent='扩展中心';document.querySelector('.nav').append(nav);
  const page=document.createElement('section');page.id='extensions';page.className='section';
  page.innerHTML='<div class="intro"><h1>扩展能力</h1></div><div class="actions"><button id="installExtension">导入扩展包</button><button id="demoExtension">安装报价示例</button><button id="refreshExtensions">刷新</button><input type="file" id="extensionFile" accept=".json" hidden></div><div id="extensionRows"></div><dialog id="extensionInstallDialog"><h2>安装扩展</h2><div id="extensionPreview"></div><form id="extensionPermissionForm"><div id="extensionGrants"></div><p id="extensionInstallStatus"></p><div class="actions"><button type="submit" class="primary">安装并启用</button><button type="button" id="cancelExtensionInstall">取消</button></div></form></dialog>';
  document.querySelector('main').append(page);
  const $=id=>document.getElementById(id);let selectedConversation=null;window.addEventListener('eah:conversation',e=>{selectedConversation=e.detail.id;});let packageData=null,viewController=new AbortController();
  const guard=fn=>async e=>{try{await fn(e)}catch(err){flash(err.message)}};
  nav.onclick=guard(async()=>{showTab('extensions');await refresh();});
  async function preview(p){
    packageData=p;const result=await api('/v1/extensions/preview','POST',{package:p});
    $('extensionPreview').innerHTML=`<h3>${escape(result.manifest.title)} · v${result.revision}</h3><p>${escape(result.manifest.description)}</p><p>${result.manifest.tools.length} 个工具 · ${result.manifest.commands.length} 个命令 · ${result.manifest.hooks.length} 个执行钩子</p><p class="muted">${result.signed?'已验证发布者签名':'未签名；摘要用于检查文件完整性'}</p>`;
    $('extensionGrants').innerHTML=result.manifest.permissions.map(p=>`<label><input type="checkbox" data-permission="${escape(p)}" required> ${p==='trusted_process'?'信任此源码在本机进程执行（可访问启动账户的系统资源）':escape(({network:'访问网络',filesystem:'访问文件',notifications:'发送通知',ui:'展示界面',commands:'注册操作',state:'保存扩展状态'})[p]||p)}</label>`).join('');
    $('extensionInstallStatus').textContent=result.missing.filter(x=>!x.startsWith('permission:')&&!x.startsWith('trusted process')).join('\n');
    $('extensionInstallDialog').showModal();
  }
  $('installExtension').onclick=()=>$('extensionFile').click();
  $('extensionFile').onchange=guard(async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;if(f.size>1700000)throw Error('扩展包超过大小限制');await preview(JSON.parse(await f.text()));});
  $('demoExtension').onclick=guard(async()=>preview(await api('/v1/extensions/examples/guide')));
  $('cancelExtensionInstall').onclick=()=>$('extensionInstallDialog').close();
  $('extensionPermissionForm').onsubmit=guard(async e=>{
    e.preventDefault();const grants=[...page.querySelectorAll('[data-permission]:checked')].map(x=>x.dataset.permission);
    await api('/v1/extensions/install','POST',{package:packageData,grants,trust_digest:grants.includes('trusted_process')?packageData.digest:null});
    $('extensionInstallDialog').close();await load();await refresh();flash('扩展已启用，可在画布或自动构建中使用。');
  });
  $('refreshExtensions').onclick=guard(refresh);
  async function refresh(){
    viewController.abort();viewController=new AbortController();document.querySelectorAll('[data-extension-frame]').forEach(f=>f.remove());
    const rows=await api('/v1/extensions');const groups=new Map();rows.forEach(r=>{if(!groups.has(r.id))groups.set(r.id,[]);groups.get(r.id).push(r);});
    $('extensionRows').replaceChildren();
    for(const [id,versions] of groups){
      const selected=versions.find(r=>r.active)||versions.at(-1),m=selected.manifest;
      const card=document.createElement('article');card.className='panel';card.style.marginTop='18px';
      card.innerHTML=`<h2>${escape(m.title)}</h2><p>${escape(m.description)}</p><p class="muted">${escape(id)} · ${selected.active?'已启用':'已停用'} · v${selected.revision}</p><details class="extension-controls"><summary>版本与分享</summary><div class="actions"><select aria-label="扩展版本">${versions.map(v=>`<option value="${v.revision}" ${v.revision===selected.revision?'selected':''}>版本 ${v.revision}</option>`).join('')}</select><button data-enable>启用所选版本</button><button data-disable ${selected.active?'':'disabled'}>停用</button><button data-export>导出源码包</button></div></details><div data-settings></div><div data-views></div><details class="technical-details" data-diagnostics><summary>查看扩展状态与事件</summary><div data-diagnostics-body></div></details>`;
      card.querySelector('[data-enable]').onclick=guard(async()=>{await api(`/v1/extensions/${id}/activate`,'POST',{revision:Number(card.querySelector('select').value)});await load();await refresh();});
      card.querySelector('[data-disable]').onclick=guard(async()=>{await api(`/v1/extensions/${id}/disable`,'POST',{});await load();await refresh();});
      card.querySelector('[data-export]').onclick=guard(async()=>{const url=`/v1/extensions/${id}/package?revision=${card.querySelector('select').value}`;const r=await fetch(url,{headers:token()?{Authorization:'Bearer '+token()}: {}});if(!r.ok)throw Error('下载失败');download(token()?await r.blob():url,id+'.eah-extension.json');});
      if(selected.active&&Object.keys(m.settings_schema.properties||{}).length){const form=schemaForm(m.settings_schema,(await api(`/v1/extensions/${id}/settings`)).value),button=document.createElement('button');button.textContent='保存设置';form.form.append(button);form.form.onsubmit=guard(async e=>{e.preventDefault();await api(`/v1/extensions/${id}/settings`,'PUT',{value:form.values()});flash('设置已保存');});card.querySelector('[data-settings]').append(form.form);}
      if(selected.active&&Object.keys(m.flags_schema.properties||{}).length){const fields=schemaForm(m.flags_schema,(await api(`/v1/extensions/${id}/flags`)).value),button=document.createElement('button');button.textContent='保存扩展选项';fields.form.append(button);fields.form.onsubmit=guard(async e=>{e.preventDefault();await api(`/v1/extensions/${id}/flags`,'PUT',{value:fields.values()});flash('扩展选项已保存');});card.querySelector('[data-settings]').append(fields.form);}
      if(selected.active)for(const action of m.commands){
        const area=document.createElement('div'),title=document.createElement('h3');title.textContent=action.title||action.spec.name;area.append(title);
        const form=schemaForm(action.spec.input_schema),button=document.createElement('button'),result=document.createElement('div');button.textContent='执行';form.form.append(button);area.append(form.form,result);
        form.form.onsubmit=guard(async e=>{e.preventDefault();const run=await api('/v1/extensions/commands/'+action.spec.name,'POST',{input:form.values(),conversation_id:selectedConversation});await watch(run.run_id,result);});card.querySelector('[data-views]').append(area);
        if(action.shortcut)window.addEventListener('keydown',e=>{if(document.querySelector('#extensions.active') && e.altKey && e.key.toLowerCase()===action.shortcut.toLowerCase()){e.preventDefault();form.form.requestSubmit();}},{signal:viewController.signal});
      }
      if(selected.active)for(const view of m.views.filter(v=>v.entrypoint)){
        const frame=document.createElement('iframe');frame.title=view.title;frame.setAttribute('sandbox','allow-scripts');frame.style.cssText='width:100%;height:280px;border:0';
        const r=await fetch(`/v1/extensions/${id}/views/${view.id}`,{headers:token()?{Authorization:'Bearer '+token()}: {}});if(!r.ok)throw Error('扩展视图加载失败');
        frame.dataset.extensionFrame='true';frame.srcdoc=`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'">`+await r.text();
        const listener=guard(async e=>{if(e.source!==frame.contentWindow)return;if(e.data?.type==='eah.editor' && view.placement==='editor'){window.dispatchEvent(new CustomEvent('eah:editor',{detail:{text:e.data.text}}));return;}if(e.data?.type!=='eah.command')return;if(e.data.command!==view.command)throw Error('视图未声明此命令');const run=await api('/v1/extensions/commands/'+view.command,'POST',{input:e.data.input||{},conversation_id:selectedConversation});frame.contentWindow.postMessage({type:'eah.accepted',requestId:e.data.requestId,runId:run.run_id},'*');});
        window.addEventListener('message',listener,{signal:viewController.signal});(document.querySelector(`[data-extension-slot="${view.placement}"]`)||card.querySelector('[data-views]')).append(frame);
        window.addEventListener('eah:message',e=>{if(view.placement==='message')frame.contentWindow?.postMessage({type:'eah.message',...e.detail},'*');},{signal:viewController.signal});
      }
      card.querySelector('[data-diagnostics]').ontoggle=guard(async()=>{if(!card.querySelector('[data-diagnostics]').open)return;const events=await api(`/v1/extensions/${id}/events`),state=selected.active?await api(`/v1/extensions/${id}/state`):null;card.querySelector('[data-diagnostics-body]').innerHTML='<pre class=output>'+escape(JSON.stringify({state,events},null,2))+'</pre>';});
      $('extensionRows').append(card);
    }
    if(!rows.length){const p=document.createElement('p');p.textContent='暂无扩展';$('extensionRows').append(p);}
  }
  window.addEventListener('eah:extension-package',e=>preview(e.detail).catch(err=>flash(err.message)));
  return {refresh};
}
