import {canPreview,clearMedia,bindMedia,uploadMedia} from './media-preview.js';
import {goalResult} from './goals.js?v=20260920-nodes-1';
import {toolLabel} from './ui-labels.js';
import { graphEditor } from './graph-editor.js';
import {runHistory} from './run-history.js';
import {stepStatus} from './run-status.js';

let shell=null;
let token="", tools=[], models=[], skills=[], nodes=[], editingWorkflow=null;
const $=id=>typeof id==='string'?document.getElementById(id):id, escape=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function flash(text){$('flash').textContent=text;$('flash').style.display='block';setTimeout(()=>$('flash').style.display='none',6000)}
async function api(path,method='GET',body){const r=await fetch(path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:body===undefined?undefined:JSON.stringify(body)});if(!r.ok){let d;try{d=await r.json()}catch{throw Error(`服务请求失败（HTTP ${r.status}），请稍后重试`)}throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail))}return r.json()}
function guard(fn){return async e=>{try{await fn(e)}catch(err){flash(err.message)}}}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=guard(async()=>{showTab(b.dataset.tab);if(b.dataset.tab==='runs')await listRuns();if(b.dataset.tab==='workflow'){await loadBase();drawGraph();}if(['knowledge','automation','evolution'].includes(b.dataset.tab))await loadAdvanced()}));
$('authBtn').onclick=guard(async()=>{const value=prompt('填写 EAH_TOKEN（仅保存在当前页面内存）');if(value!==null){token=value;await load();flash('凭证已更新')}});
import { modelConnection } from './model-connection.js?v=20260920-pending-1';
modelConnection({api,$,load,flash});
async function loadBase(){
  const selectedTool=$('newTool').value;
  [tools,models,skills]=await Promise.all([api('/v1/tools'),api('/v1/models'),api('/v1/skills')]);
  $('newTool').innerHTML=tools.map(t=>`<option value="${escape(t.name)}">${escape(toolLabel(t))}</option>`).join('');
  if([...$('newTool').options].some(o=>o.value===selectedTool))$('newTool').value=selectedTool;else if(!nodes.length)$('newTool').value='core.echo';
  $('capabilityList').innerHTML=tools.map(t=>`<div class="saved-row"><div><b>${escape(t.name)}</b><div class="muted">${escape(t.description)}</div></div><span class="badge">${t.effect==='write'?'需确认':t.effect==='local'?'本地开发':'读取'}</span></div>`).join('');
  await noCode.refresh(models);
  await library.refresh();
  await listWorkflows();if(!nodes.length)addNode('tool');renderNodes();
}
const statuses={waiting_remote:'等待外部任务',waiting_input:'需要补充信息',waiting_children:'等待子流程',queued:'等待处理',running:'正在处理',waiting_approval:'需要你确认',needs_attention:'需要核验外部结果',succeeded:'已完成',failed:'处理失败',cancelled:'已取消',skipped:'已跳过',retrying:'准备重试'};
const activeWatches=new WeakMap();
function stopWatch(target){const root=$(target);activeWatches.delete(root);clearMedia(root);}
async function watch(id,target){
  const root=$(target),ticket={};
  const unavailable=()=>!root.isConnected||(root.classList.contains('run-details')&&root.hidden);
  if(unavailable())return;
  activeWatches.set(root,ticket);
  for(let i=0;i<600;i++){
    if(activeWatches.get(root)!==ticket||unavailable())return;
    let r;
    try{r=await api('/v1/runs/'+id);}
    catch(error){if(activeWatches.get(root)!==ticket||unavailable())return;throw error;}
    if(activeWatches.get(root)!==ticket||unavailable())return;
    renderRun(r,root);
    if(['succeeded','failed','cancelled','waiting_approval','waiting_input','needs_attention'].includes(r.status))return;
    await new Promise(resolve=>setTimeout(resolve,300));
  }
  if(activeWatches.get(root)===ticket&&!unavailable())flash('任务仍在运行，可到运行记录继续查看');
}
function artifactButtons(output){
  const found=new Map();
  function visit(value){if(!value||typeof value!=='object')return;if(value.id&&value.digest&&value.media_type)found.set(value.id,value);for(const v of Object.values(value))visit(v);}
  visit(output);
  return [...found.values()].map(a=>`${canPreview(a.media_type)?`<button class="small" data-media-preview="${escape(a.id)}" data-media-type="${escape(a.media_type)}" data-filename="${escape(a.name)}">预览${a.media_type.startsWith("image/")?"图片":a.media_type.startsWith("video/")?"视频":"音频"}</button>`:""}<button class="small" data-artifact="${escape(a.id)}" data-filename="${escape(a.name)}">下载 ${escape(a.name)}</button>`).join(' ');
}
function outputPanel(step){
  if(step.output===null)return '';
  const text=escape(step.output.text||JSON.stringify(step.spec?.kind==='goal'?(step.output.outputs||{reason:step.output.reason}):step.output,null,2));
  const hasMedia=value=>value&&typeof value==='object'&&(canPreview(value.media_type)||Object.values(value).some(hasMedia));
  const content=`<pre class="output">${text}</pre>`;
  return hasMedia(step.output)?`<details><summary>查看接口回执</summary>${content}</details>`:content;
}
function errorPanel(error){
  if(!error)return '';
  const full=String(error),brief=full.replace(/\s+/g,' ').trim();
  return `<div class="run-error"><p class="run-error-summary">${escape(brief.length>180?brief.slice(0,180)+'…':brief)}</p>${full.length>180||full.includes('\n')?`<details class="run-diagnostic"><summary>查看完整错误</summary><pre class="output run-scroll" tabindex="0" role="region" aria-label="完整错误">${escape(full)}</pre></details>`:''}</div>`;
}
function approvalPanel(a){
  const parameters=`<details class="run-diagnostic"><summary>查看调用参数</summary><pre class="output run-scroll" tabindex="0" role="region" aria-label="调用参数">${escape(JSON.stringify(a.arguments,null,2))}</pre></details>`;
  const action=a.status==='approval'
    ? `<button data-approve="${a.id}" class="small primary">确认执行</button> <button data-deny="${a.id}" class="small">拒绝</button>`
    : `<details class="run-reconcile"><summary>核验外部结果并继续</summary><p>先核对外部实际结果，再填写回执并恢复。</p>${inputForm({id:'reconcile-'+a.id,prompt:'结果',schema:tools.find(t=>t.name===a.tool)?.output_schema||{type:'object'}})}<label>核验依据或回执<textarea data-receipt="${a.id}" required></textarea></label><button data-reconcile="${a.id}">提交核验结果</button></details>`;
  return `<div class="notice run-action"><b>${escape(a.tool)}</b>${parameters}${action}</div>`;
}
function renderRun(r,target){clearMedia($(target));$(target).innerHTML=`<div class="panel"><div class="actions"><span class="badge">${statuses[r.status]||escape(r.status)}</span><span class="muted">${escape(r.id.slice(0,8))}</span></div>${r.children?.length?'<p class=muted>子运行：'+r.children.map(c=>'<a href=# data-child='+c.id+'>'+escape(c.name)+' · '+(statuses[c.status]||c.status)+'</a>').join(' / ')+'</p>':''}${r.steps.map(s=>`<div style="margin-top:14px"><b>${escape(r.spec?.metadata?.step_labels?.[s.id]||(s.spec?.kind==='tool'?toolLabel(tools.find(t=>t.name===s.spec.target)):kindNames[s.spec?.kind])||s.id)}</b> · ${escape(stepStatus(r,s,statuses))}${outputPanel(s)}${artifactButtons(s.output)}${errorPanel(s.error)}</div>`).join('')}${(r.input_requests||[]).map(q=>`<div class=notice><b>${escape(q.prompt)}</b>${inputForm(q)}<button data-input-send="${q.id}">提交补充信息</button></div>`).join('')}${r.approvals.map(approvalPanel).join('')}${!['succeeded','failed','cancelled'].includes(r.status)?'<button data-cancel class="small">停止运行</button>':''}<details style="margin-top:15px"><summary>查看事件记录</summary><pre class="output events">加载中</pre></details></div>`;$(target).querySelectorAll('[data-artifact]').forEach(b=>b.onclick=guard(async()=>{const response=await fetch('/v1/artifacts/'+encodeURIComponent(b.dataset.artifact)+'/content',{headers:token?{Authorization:'Bearer '+token}:{}});if(!response.ok)throw Error('产物下载失败');download(token?await response.blob():'/v1/artifacts/'+encodeURIComponent(b.dataset.artifact)+'/content',b.dataset.filename)}));$(target).querySelectorAll('[data-approve],[data-deny]').forEach(b=>b.onclick=guard(async()=>{await api('/v1/approvals/'+(b.dataset.approve||b.dataset.deny),'POST',{approved:!!b.dataset.approve});await watch(r.id,target)}));$(target).querySelector('[data-cancel]')?.addEventListener('click',guard(async()=>{await api(`/v1/runs/${r.id}/cancel`,'POST');await watch(r.id,target)}));$(target).querySelectorAll('[data-child]').forEach(b=>b.onclick=guard(async e=>{e.preventDefault();await watch(b.dataset.child,target)}));$(target).querySelectorAll('[data-input-send]').forEach(b=>b.onclick=guard(async()=>{await api('/v1/inputs/'+b.dataset.inputSend,'POST',inputValues($(target),b.dataset.inputSend));await watch(r.id,target)}));$(target).querySelectorAll('[data-reconcile]').forEach(b=>b.onclick=guard(async()=>{const receipt=$(target).querySelector('[data-receipt="'+b.dataset.reconcile+'"]').value;if(!receipt.trim())throw Error('请填写核验依据');await api('/v1/reconciliations/'+b.dataset.reconcile,'POST',{output:inputValues($(target),'reconcile-'+b.dataset.reconcile),receipt});await watch(r.id,target)}));bindMedia($(target),token,guard);goalResult({run:r,root:$(target),api,escape,guard,watch,loadWorkflow,showTab});const details=$(target).querySelector('.events').closest('details');details.ontoggle=guard(async()=>{if(details.open)details.querySelector('.events').textContent=JSON.stringify(await api(`/v1/runs/${r.id}/events`),null,2)})}
let runRows=[];
const runFilters=document.createElement('div');runFilters.className='run-filters';runFilters.innerHTML='<input id="runSearch" type="search" aria-label="查找任务" placeholder="查找任务名称"><select id="runStatus" aria-label="筛选任务状态"><option value="all">全部状态</option><option value="attention">需要我处理</option><option value="active">正在执行</option><option value="succeeded">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select>';$('refreshRuns').before(runFilters);runFilters.append($('refreshRuns'));
const renderRunHistory=runHistory({watch,stopWatch,escape,statuses,flash});
function displayRuns(){
  const query=$('runSearch').value.trim().toLocaleLowerCase(),status=$('runStatus').value;
  const rows=runRows.filter(r=>r.name.toLocaleLowerCase().includes(query)&&(status==='all'||status==='attention'&&['waiting_approval','waiting_input','needs_attention'].includes(r.status)||status==='active'&&['queued','running','waiting_remote','waiting_children'].includes(r.status)||r.status===status));
  const filtered=runRows.length||query||status!=='all';
  renderRunHistory($('runList'),rows,'<div class="empty-state"><h3>'+(filtered?'没有符合条件的任务':'还没有任务记录')+'</h3></div>');
}
let runRequest=0,runDebounce;
async function listRuns(){const ticket=++runRequest,filter=$('runStatus').value,params=new URLSearchParams({query:$('runSearch').value.trim(),limit:'100'});const selected=filter==='attention'?['waiting_approval','waiting_input','needs_attention']:filter==='active'?['queued','running','waiting_remote','waiting_children']:filter==='all'?[]:[filter];selected.forEach(status=>params.append('status',status));const rows=await api('/v1/runs?'+params);if(ticket!==runRequest)return;runRows=rows;displayRuns();}
$('refreshRuns').onclick=guard(listRuns);$('runSearch').maxLength=160;$('runSearch').oninput=()=>{runRequest++;clearTimeout(runDebounce);runDebounce=setTimeout(()=>listRuns().catch(e=>flash(e.message)),250);};$('runStatus').onchange=guard(listRuns);
const kindNames={goal:'目标检查与自动调整',tool:'调用工具',model:'模型处理',agent:'自主处理',retrieve:'检索资料',transform:'整理数据',artifact:'保存结果',input:'补充信息',approval:'请你确认',foreach:'批量处理',subworkflow:'复用流程'};
const defaultInputs={tool:{text:'你好，EasyAgent'},model:{prompt:'请整理这段文字'},agent:{prompt:'请完成这项任务',instructions:'保留来源，使用已授权工具',tools:[]},retrieve:{namespace:'home',query:'搬家',mode:'lexical'},transform:{value:'可填写数据或 $ref 引用'},artifact:{name:'result.txt',content:'这里填写内容或引用'},input:{prompt:'请补充日期',schema:{type:'object',properties:{date:{type:'string'}},required:['date'],additionalProperties:false}},approval:{action:'请确认本次安排'},foreach:{items:['第一项','第二项']},subworkflow:{message:'子流程输入'}};
function addNode(kind){let n=1;while(nodes.some(x=>x.id==='step'+n))n++;const node={id:'step'+n,kind,target:kind==='tool'?($('newTool').value||'core.echo'):['model','agent'].includes(kind)?'mock':'',input:structuredClone(defaultInputs[kind]),depends_on:[]};if(['foreach','subworkflow'].includes(kind))node.body={name:'子流程',steps:[{id:'output',kind:'transform',input:{value:{$ref:kind==='foreach'?'$input.item':'$input'}}}]};if(kind==='tool'&&node.target!=='core.echo'){node.input={};const schema=tools.find(t=>t.name===node.target)?.input_schema||{};for(const [key,field] of Object.entries(schema.properties||{})){if(field.default!==undefined)node.input[key]=field.default;}}nodes.push(node);renderNodes()}
function syncNodes(){document.querySelectorAll('.node').forEach((el,i)=>{nodes[i].id=el.querySelector('[name=id]').value;const selectedTarget=el.querySelector('[name=target]')?.value||'';if(nodes[i].target!==selectedTarget)delete nodes[i].tool_revision;nodes[i].target=selectedTarget;nodes[i].depends_on=el.querySelector('[name=depends]').value.split(',').map(s=>s.trim()).filter(Boolean);nodes[i].input=JSON.parse(el.querySelector('[name=input]').value);const source=el.querySelector('[name=condition]').value.trim();if(source)nodes[i].when={source,equals:JSON.parse(el.querySelector('[name=equals]').value)};else delete nodes[i].when;nodes[i].requires_approval=el.querySelector('[name=approval]').checked;nodes[i].max_attempts=Number(el.querySelector('[name=attempts]').value);nodes[i].timeout_seconds=Number(el.querySelector('[name=timeout]').value);const scheduled=el.querySelector('[name=not_before]').value;if(scheduled)nodes[i].not_before=new Date(scheduled).getTime()/1000;else delete nodes[i].not_before;const body=el.querySelector('[name=body]');if(body)nodes[i].body=JSON.parse(body.value);const compensate=el.querySelector('[name=compensate]').value.trim();if(compensate)nodes[i].compensate=JSON.parse(compensate);else delete nodes[i].compensate})}
function renderNodes(){$('flowPreview').innerHTML=nodes.map(n=>`<span>${escape(n.id)}${n.depends_on.length?' ← '+escape(n.depends_on.join(', ')):''}</span>`).join('');drawGraph();$('nodes').innerHTML=nodes.map((n,i)=>`<div class="node"><header><b>${i+1} · ${kindNames[n.kind]||escape(n.kind)}</b><div><button class="small" data-up="${i}" ${i===0?'disabled':''}>上移</button> <button class="small" data-remove="${i}">删除</button></div></header><div class="node-grid"><label>步骤名称<input name="id" value="${escape(n.id)}" pattern="[a-zA-Z][a-zA-Z0-9_-]*"></label>${['tool','model','agent'].includes(n.kind)?`<label>${n.kind==='tool'?'选择工具':'选择模型'}<select name="target">${(n.kind==='tool'?tools.map(t=>t.name):models.map(m=>m.alias)).map(a=>`<option ${a===n.target?'selected':''}>${escape(a)}</option>`).join('')}</select></label>`:'<p class="muted">'+kindNames[n.kind]+'</p>'}</div><label>等待哪些步骤（多个用逗号分隔）<input name="depends" value="${escape(n.depends_on.join(','))}" placeholder="无依赖时可并行"></label><label>输入参数 / 数据映射（JSON）<textarea name="input">${escape(JSON.stringify(n.input,null,2))}</textarea></label>${['foreach','subworkflow'].includes(n.kind)?`<label>复用已保存流程<select data-body-select="${i}"><option value="">独立副本 · 直接编辑下方定义</option>${n.workflow_ref?`<option value="__pinned__" selected>${escape(n.workflow_ref.id)} · 固定版本 ${n.workflow_ref.revision}</option>`:''}${savedWorkflowRows.map(w=>`<option value="${escape(w.id)}">${escape(w.workflow.name)} · 版本 ${w.revision||0}</option>`).join('')}</select></label><label>${n.workflow_ref?'所引用版本的内容（只读；切换为独立副本可编辑）':'子流程定义'}<textarea name="body" rows="7" ${n.workflow_ref?'readonly':''}>${escape(JSON.stringify(n.body,null,2))}</textarea></label>`:''}<details><summary>条件、重试、时间与补偿</summary><div class="node-grid"><label>条件来源<input name="condition" placeholder="step1.data.category" value="${escape(n.when?.source||'')}"></label><label>等于（JSON 值）<input name="equals" value="${escape(JSON.stringify(n.when?.equals??true))}"></label><label>最多尝试次数<input name="attempts" type="number" min="1" max="10" value="${n.max_attempts||3}"></label><label>超时（秒）<input name="timeout" type="number" min="1" max="3600" value="${n.timeout_seconds||60}"></label></div><label>最早执行时间<input name="not_before" type="datetime-local" value="${n.not_before?localDate(n.not_before):''}"></label><label class="check"><input type="checkbox" name="approval" ${n.requires_approval?'checked':''}>这一步执行前让我确认</label><label>失败补偿（可选 JSON 工具调用）<textarea name="compensate" placeholder='{"target":"tool.name","input":{}}'>${escape(n.compensate?JSON.stringify(n.compensate,null,2):'')}</textarea></label></details></div>`).join('');$('nodes').querySelectorAll('[data-remove]').forEach(b=>b.onclick=guard(()=>{syncNodes();nodes.splice(+b.dataset.remove,1);renderNodes()}));$('nodes').querySelectorAll('[data-up]').forEach(b=>b.onclick=guard(()=>{syncNodes();const i=+b.dataset.up;[nodes[i-1],nodes[i]]=[nodes[i],nodes[i-1]];renderNodes()}));$('nodes').querySelectorAll('[data-body-select]').forEach(b=>b.onchange=guard(()=>{syncNodes();if(b.value==='__pinned__')return;const node=nodes[+b.dataset.bodySelect];if(b.value){const saved=savedWorkflowRows.find(w=>w.id===b.value);node.workflow_ref={id:saved.id,revision:saved.revision||0};node.body=structuredClone(saved.workflow);}else delete node.workflow_ref;renderNodes()}))}
function localDate(seconds){const d=new Date(seconds*1000);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}
function currentDefinition(){return {name:$('workflowName').value,steps:structuredClone(nodes),inputs:JSON.parse($('workflowInputs').value),metadata:JSON.parse($('workflowMetadata').value),limits:JSON.parse($('workflowLimits').value)}}
const graph=graphEditor({root:$('graphEditor'),upload:file=>uploadMedia(file,token),read:currentDefinition,tools:()=>tools,models:()=>models,flash,write:w=>{nodes=w.steps;$('workflowMetadata').value=JSON.stringify(w.metadata||{},null,2);renderNodes();}});
function drawGraph(){graph.render(currentDefinition())}
$('addTool').onclick=guard(()=>{graph.flush();syncNodes();addNode('tool')});$('addModel').onclick=guard(()=>{graph.flush();syncNodes();addNode('model')});function workflow(){graph.flush();syncNodes();renderNodes();return currentDefinition()}
function loadWorkflow(w,saved=null){editingWorkflow=saved;graph.reset();nodes=structuredClone(w.steps).map(s=>({kind:'tool',input:{},depends_on:[],...s}));$('workflowName').value=w.name;for(const [key,id] of [['inputs','workflowInputs'],['metadata','workflowMetadata'],['limits','workflowLimits']])$(id).value=JSON.stringify(w[key]||{},null,2);renderNodes()}
$('runWorkflow').onclick=guard(async()=>{const r=await api('/v1/runs','POST',workflow());await watch(r.id,'workflowResult')});$('saveWorkflow').onclick=guard(async()=>{const saved=editingWorkflow?await api('/v1/studio/workflows/'+encodeURIComponent(editingWorkflow.id)+'?expected_revision='+editingWorkflow.revision,'PUT',workflow()):await api('/v1/studio/workflows','POST',workflow());editingWorkflow={id:saved.id,revision:saved.revision};await listWorkflows();shell?.refresh().catch(()=>{});flash('流程已保存 · 版本 '+saved.revision)});function download(blob,name){const a=document.createElement('a'),url=typeof blob==='string'?blob:URL.createObjectURL(blob);a.href=url;a.download=name;a.style.display='none';document.body.append(a);a.click();a.remove();if(typeof blob!=='string')setTimeout(()=>URL.revokeObjectURL(url),5000)}$('exportWorkflow').onclick=guard(()=>download(new Blob([JSON.stringify(workflow(),null,2)],{type:'application/json'}),'workflow.json'));$('importBtn').onclick=()=>$('importFile').click();$('importFile').onchange=guard(async e=>{const data=JSON.parse(await e.target.files[0].text());if(data.format==='easyagent.workflow-package.v1'){await sharing.open(data);return;}if(!Array.isArray(data.steps))throw Error('请导入包含 steps 的流程配置');const saved=await api('/v1/studio/workflows','POST',data);loadWorkflow(saved.workflow,saved);await listWorkflows();flash('流程已导入')});async function listWorkflows(){const rows=await api('/v1/studio/workflows');savedWorkflowRows=rows;$('savedWorkflows').innerHTML=rows.map(r=>`<div class="saved-row"><b>${escape(r.workflow.name)}</b><span class="badge">版本 ${r.revision||0}</span><button class="small" data-use-chat="${r.id}@${r.revision||0}">在对话中使用</button><button class="small" data-workflow="${r.id}">继续编辑</button><button class="small" data-reuse="${r.id}">用作子流程</button><button class="small" data-export-saved="${r.id}" data-revision="${r.revision||0}">导出此版本</button><button class="small" data-share-saved="${r.id}" data-revision="${r.revision||0}">分享完整流程</button></div>`).join('')||'<div class=empty-state><h3>暂无流程</h3><button data-go=workflow>新建流程</button><button data-go=workflow data-import-shortcut>导入已有流程</button></div>';$('savedWorkflows').querySelectorAll('[data-use-chat]').forEach(b=>b.onclick=()=>document.dispatchEvent(new CustomEvent('eah:chat-workflow',{detail:{key:b.dataset.useChat}})));$('savedWorkflows').querySelectorAll('[data-share-saved]').forEach(b=>b.onclick=guard(()=>sharing.exportSaved(b.dataset.shareSaved,Number(b.dataset.revision))));$('savedWorkflows').querySelectorAll('[data-workflow]').forEach(b=>b.onclick=()=>{const saved=rows.find(r=>r.id===b.dataset.workflow);loadWorkflow(saved.workflow,saved);showTab('workflow')});$('savedWorkflows').querySelectorAll('[data-reuse]').forEach(b=>b.onclick=()=>{const saved=rows.find(r=>r.id===b.dataset.reuse);loadWorkflow({name:('复用 · '+saved.workflow.name).slice(0,160),inputs:structuredClone(saved.workflow.inputs||{}),steps:[{id:'reuse',kind:'subworkflow',workflow_ref:{id:saved.id,revision:saved.revision||0},body:structuredClone(saved.workflow),input:Object.fromEntries(Object.keys(saved.workflow.inputs||{}).map(k=>[k,{$ref:'$input.'+k}]))}]});showTab('workflow');flash('已创建流程，引用固定版本。')});$('savedWorkflows').querySelectorAll('[data-export-saved]').forEach(b=>b.onclick=guard(async()=>{const url='/v1/studio/workflows/'+encodeURIComponent(b.dataset.exportSaved)+'/workflow.json?revision='+b.dataset.revision;if(!token){download(url,'workflow-v'+b.dataset.revision+'.json');return;}const response=await fetch(url,{headers:{Authorization:'Bearer '+token}});if(!response.ok)throw Error('流程导出失败');download(await response.blob(),'workflow-v'+b.dataset.revision+'.json')}))}


import { advanced } from './studio-advanced.js?v=20260920-nodes-1';
let savedWorkflowRows=[];
const advancedState={workflows:[]};
const refreshAdvanced=advanced({api,$,guard,flash,models:()=>models,load:()=>loadAdvanced(),state:()=>advancedState});
async function loadAdvanced(){await refreshAdvanced();savedWorkflowRows=advancedState.workflows;}
async function load(){const selected=$('newTool').value;await loadBase();await loadAdvanced();if([...$('newTool').options].some(o=>o.value===selected))$('newTool').value=selected;renderNodes();}
$('addAdvanced').onclick=guard(()=>{graph.flush();syncNodes();addNode($('nodeKind').value)});
import { noCodeBuilder } from './no-code.js?v=20260920-pending-1';
import { workflowSharing } from './workflow-sharing.js?v=20260920-nodes-1';
const sharing=workflowSharing({api,$,escape,download,token:()=>token,flash,loadWorkflow,reload:loadBase,showTab});
$('shareWorkflow').onclick=guard(()=>sharing.share(workflow()));
const noCode=noCodeBuilder({api,$,escape,download,watch,loadWorkflow,showTab,listWorkflows,flash,token:()=>token,shareWorkflow:sharing.share});
import { nodeLibrary } from './node-library.js?v=20260920-media-1';
function addComponent(step){graph.flush();syncNodes();let n=1;while(nodes.some(x=>x.id==='component'+n))n++;step.id='component'+n;nodes.push(step);renderNodes();$('graphEditor').scrollIntoView({behavior:'smooth',block:'start'});}
const library=nodeLibrary({api,$,escape,flash,guard,download,reload:loadBase,add:addComponent,token:()=>token});
import { modelCatalog } from './model-catalog.js';
modelCatalog({api,$,escape,reload:loadBase,add:step=>{showTab('workflow');addComponent(step);},flash});
import {conversationStudio} from './conversations.js?v=20260920-nodes-1';
conversationStudio({api,escape,flash,showTab,watch});
import {extensionStudio} from './extensions.js?v=20260920-nodes-1';
extensionStudio({api,escape,flash,load,showTab,watch,token:()=>token,download});
import {connectionStudio} from './connections.js?v=20260920-pending-1';
connectionStudio({api,escape,flash,showTab,load});
import {learningStudio} from './learning.js?v=20260920-nodes-1';
learningStudio({api,escape,flash,showTab,load});
import {operationsStudio} from './operations.js?v=20260920-nodes-1';
operationsStudio({api,escape,flash,showTab,token:()=>token,download});
import {skillStudio} from './skills.js?v=20260920-nodes-1';
skillStudio({api,escape,flash,load,showTab,download});
import {backendSettings} from './backend-settings.js?v=20260920-local-1';
backendSettings({api,escape,flash});
import {voiceStudio} from './voice.js';
voiceStudio({api,escape,flash,token:()=>token});
import {workspaceChat} from './workspace-chat.js?v=20260920-upload-1';
workspaceChat({api,escape,flash,showTab,renderRun,stopWatch,loadWorkflow,token:()=>token,download,statuses});
import {workspaceUI} from './workspace-ui.js?v=20260920-nodes-1';
shell=workspaceUI({api,escape,flash,showTab,renderRunHistory,listWorkflows});
shell.select(location.hash.slice(1)||'home');
load().then(()=>{shell.start();shell.refresh().catch(e=>flash(e.message));}).catch(e=>{shell.start();flash(e.message)});

function inputForm(q){
  const props=q.schema.properties;
  if(q.schema.type!=='object'||!props)return `<label>补充内容（JSON）<textarea data-input-body="${q.id}"></textarea></label>`;
  return Object.entries(props).map(([key,v])=>{const type=Array.isArray(v.type)?v.type.find(t=>t!=='null'):v.type,req=(q.schema.required||[]).includes(key),attrs=`data-question="${q.id}" data-field="${escape(key)}" data-value-type="${escape(type||'string')}" ${req?'required':''}`;let field;
    if(v.enum)field=`<select ${attrs} data-enum>${v.enum.map(value=>`<option value="${escape(JSON.stringify(value))}">${escape(value)}</option>`).join('')}</select>`;
    else if(type==='boolean')field=`<select ${attrs}><option value="true">是</option><option value="false">否</option></select>`;
    else if(['object','array'].includes(type))field=`<textarea ${attrs} placeholder="JSON"></textarea>`;
    else field=`<input ${attrs} type="${['number','integer'].includes(type)?'number':'text'}" ${v.format==='date-time'?'placeholder="2026-09-19T09:00:00+08:00"':''}>`;
    return `<label>${escape(v.title||key)}${req?' *':''}${field}${v.description?'<small>'+escape(v.description)+'</small>':''}</label>`;
  }).join('');
}
function inputValues(root,id){const raw=root.querySelector('[data-input-body="'+id+'"]');if(raw)return JSON.parse(raw.value);const values={};for(const el of root.querySelectorAll('[data-question="'+id+'"]')){if(!el.reportValidity())throw Error('请填写所需信息');if(el.value===''&&!el.required)continue;values[el.dataset.field]=el.hasAttribute('data-enum')||['boolean','object','array'].includes(el.dataset.valueType)?JSON.parse(el.value):['number','integer'].includes(el.dataset.valueType)?Number(el.value):el.value;}return values;}

function showTab(id){if(shell){shell.select(id);if(id==='workflow')requestAnimationFrame(drawGraph);return;}document.querySelectorAll('main > .section').forEach(x=>x.classList.toggle('active',x.id===id));}
$('validateWorkflow').onclick=guard(async()=>{await api('/v1/studio/workflows/validate','POST',workflow());flash('流程结构、API 和必填参数检查通过')});
$('nodes').addEventListener('change',guard(()=>{syncNodes();drawGraph()}));

import { apiSetup } from './api-setup.js?v=20260920-nodes-1';
apiSetup({api, $, escape, download, load, flash});
