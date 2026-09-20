import {modelChoices} from './model-choice.js';
import {retryPanel,bindRetry,retryProgress} from './run-retry.js?v=20260921-verification-5';
// One source of truth: persist the compiled graph, preview it, run it, export it.
export function noCodeBuilder({api,$,escape,download,watch,loadWorkflow,showTab,listWorkflows,flash,token,shareWorkflow}) {
  let identifier=null,plan=null,savedSignature='',preferredModel='auto',availableModels=[],defaultModel=null,busy=false,epoch=0;
  const signature=()=>JSON.stringify([$('assistantName').value,$('purpose').value,preferredModel]);
  const drawModels=()=>modelChoices($('assistantModel'),availableModels,preferredModel,defaultModel);
  $('assistantModel').onchange=()=>{preferredModel=$('assistantModel').value;invalidate();};
  const report=error=>{flash(error.message);$('savedLabel').textContent=error.message;};
  const guard=fn=>async e=>{e?.preventDefault();try{await fn(e)}catch(error){report(error)}};
  function invalidate(){plan=null;$('runResult').replaceChildren();$('tryBtn').disabled=true;$('savedLabel').textContent='需求已修改，请重新生成';renderStatus({status:'stale',message:'需求已修改，请重新生成。'});}
  $('assistantName').oninput=invalidate;$('purpose').oninput=invalidate;
  $('newAssistant').onclick=()=>{$('runResult').replaceChildren();$('message').value='';epoch++;identifier=null;preferredModel='auto';drawModels();plan=null;$('assistantName').value='我的新助手';$('purpose').value='';$('savedLabel').textContent='';$('tryBtn').disabled=true;renderStatus({status:'not_built'});};
  function dependencies(w){const labels=w.metadata?.step_labels||{};return w.steps.map(s=>`<li><strong>${escape(labels[s.id]||s.id)}</strong><span>${escape(({tool:'调用接口',model:'模型处理',agent:'智能处理',input:'补充信息',approval:'人工确认',artifact:'保存结果',transform:'整理数据',retrieve:'检索资料',foreach:'批量处理',subworkflow:'子流程'})[s.kind]||s.kind)}${s.target?' · '+escape(s.target):''}</span><small>${s.depends_on.length?'等待 '+s.depends_on.map(id=>escape(labels[id]||id)).join('、'):'无前置步骤'}${s.body?' · 子流程含 '+s.body.steps.length+' 个步骤':''}</small></li>`).join('');}
  function diagram(w){
    const labels=w.metadata?.step_labels||{},levels=new Map(),pending=new Map(w.steps.map(s=>[s.id,s]));
    while(pending.size){let added=false;for(const [id,s] of pending)if(s.depends_on.every(d=>levels.has(d))){levels.set(id,s.depends_on.length?Math.max(...s.depends_on.map(d=>levels.get(d)))+1:0);pending.delete(id);added=true}if(!added)break;}
    const counts={},positions={};for(const s of w.steps){const level=levels.get(s.id)||0;positions[s.id]={x:20+level*236,y:24+(counts[level]||0)*100};counts[level]=(counts[level]||0)+1}
    const width=(Math.max(0,...levels.values())+1)*236+20,height=Math.max(1,...Object.values(counts))*100+24;
    const edges=w.steps.flatMap(s=>s.depends_on.map(id=>{const a=positions[id],b=positions[s.id];return `<path d="M ${a.x+200} ${a.y+32} C ${a.x+222} ${a.y+32},${b.x-20} ${b.y+32},${b.x} ${b.y+32}"/>`})).join('');
    const nodes=w.steps.map(s=>{const p=positions[s.id],name=labels[s.id]||s.id;return `<g transform="translate(${p.x},${p.y})"><title>${escape(name+' · '+(s.target||s.kind))}</title><rect width="200" height="64" rx="8"/><text x="12" y="26">${escape(name.length>13?name.slice(0,13)+'…':name)}</text><text class="plan-node-detail" x="12" y="47">${escape((s.target||s.kind).slice(0,25))}</text></g>`}).join('');
    return `<div class="plan-diagram"><svg role="img" aria-label="已生成工作流的步骤和连接" viewBox="0 0 ${width} ${height}" style="min-width:${Math.min(width,1600)}px;height:${height}px">${edges}${nodes}</svg></div>`;
  }
  function renderStatus(result){
    const root=$('assistantPlan');if($('assistantTrial'))$('assistantTrial').classList.toggle('ready',result.status==='ready');if($('trialHint'))$('trialHint').hidden=result.status==='ready';
    if(result.status==='ready'){
      plan=result;$('tryBtn').disabled=signature()!==savedSignature;
      root.innerHTML=`<div class="actions"><h2 style="flex:1">已生成 ${result.workflow.steps.length} 个步骤</h2><span class="badge">已保存</span></div><p>${escape(result.explanation)}</p>${diagram(result.workflow)}<ol class="plan-step-list">${dependencies(result.workflow)}</ol><div class="actions"><button class="primary" id="openPlanCanvas">在画布中编辑</button><button id="exportPlanJson">导出工作流 JSON</button><button id="exportPlanProject">导出客户端项目</button><button id="sharePlanWorkflow">分享完整工作流</button></div><details style="margin-top:16px"><summary>查看完整工作流定义</summary><pre class="output">${escape(JSON.stringify(result.workflow,null,2))}</pre></details>`;
      $('openPlanCanvas').onclick=()=>{loadWorkflow(result.workflow);showTab('workflow')};
      $('exportPlanJson').onclick=guard(()=>exportProject(identifier,'workflow.json'));
      $('exportPlanProject').onclick=guard(()=>exportProject(identifier));
      $('sharePlanWorkflow').onclick=guard(()=>shareWorkflow(result.workflow));
      return;
    }
    plan=null;$('tryBtn').disabled=true;
    const statuses={waiting_connections:'助手已创建 · 等待连接模型或服务',legacy:'旧版助手尚未生成具体流程',not_built:'等待生成工作流',stale:'需求已更改',queued:'正在准备构建',running:'模型正在安排步骤',retrying:'正在重试构建',failed:'工作流构建失败',invalid:'生成的流程未通过检查',clarification:'还缺少完成需求的条件',cancelled:'构建已取消'};
    const detail=result.message||result.explanation||(result.errors||[]).join('\n')||(['queued','running','retrying'].includes(result.status)?'正在生成流程…':'填写需求后点击“生成工作流”。');
    root.innerHTML=`<h2>${escape(statuses[result.status]||result.status)}</h2><p style="white-space:pre-wrap">${escape(detail)}</p>${result.questions?.length?'<ul>'+result.questions.map(q=>'<li>'+escape(q)+'</li>').join('')+'</ul>':''}${result.build_id?'<small class="muted">构建记录 '+escape(result.build_id.slice(0,8))+'</small>':''}`;
    root.insertAdjacentHTML('beforeend',(result.retry_steps||[]).map(step=>retryProgress(step,escape)).join(''));
    if(result.status==='failed'&&result.build_id){
      const slot=document.createElement('div');root.append(slot);const id=identifier,ticket=epoch;
      api('/v1/runs/'+result.build_id).then(run=>{
        if(!root.contains(slot)||ticket!==epoch)return;slot.innerHTML=retryPanel(run,escape);
        bindRetry(slot,run,{api,onRetry:async()=>{busy=true;$('buildAssistant').disabled=true;try{await followBuild(id,ticket);}finally{busy=false;$('buildAssistant').disabled=false;}}});
      }).catch(report);
    }
    if(result.status==='waiting_connections'){
      root.insertAdjacentHTML('beforeend',`<ul>${(result.required_connections||[]).map(r=>`<li><b>${escape(r.title)}</b><p>${escape(r.reason)}</p></li>`).join('')}</ul>${result.planned_steps?.length?'<h3>步骤草稿 · 待接入</h3><ol>'+result.planned_steps.map(s=>'<li><b>'+escape(s.title)+'</b><p>'+escape(s.description)+'</p><small>等待：'+escape(s.depends_on.map(id=>result.planned_steps.find(step=>step.id===id)?.title||id).join('、')||'无前置步骤')+'</small></li>').join('')+'</ol>':''}${result.workflow?'<h3>已保存的流程草稿 · 待接入后验证</h3>'+diagram(result.workflow):''}<div class="actions"><button type="button" id="setupAssistantConnection">去连接模型或服务</button><button type="button" id="resumeAssistantBuild">已连接，继续生成</button></div>`);
      $('setupAssistantConnection').onclick=()=>document.querySelector('[data-tab="connections"]').click();
      $('resumeAssistantBuild').onclick=()=>$('assistantForm').requestSubmit();
    }
  }
  async function followBuild(id,ticket){
    const deadline=Date.now()+1800000;
      while(ticket===epoch){const result=await api('/v1/studio/assistants/'+id+'/workflow');
        if(signature()!==savedSignature){renderStatus({status:'stale',message:'本次生成对应之前的需求，请根据新需求重新生成。'});break;}
        renderStatus(result);if(!['queued','running','retrying'].includes(result.status)){$('savedLabel').textContent=result.status==='ready'?'工作流已生成并保存':result.status==='waiting_connections'?'助手与需求已保存，等待连接':'请查看下面的构建结果';break;}
        if(Date.now()>deadline){$('savedLabel').textContent='构建仍在后台继续，可从任务记录查看进度。';break;}await new Promise(r=>setTimeout(r,500));
      }
  }
  async function exportProject(id,format='export'){
    const path='/v1/studio/assistants/'+id+'/'+format;
    const response=await fetch(path,{headers:token()?{Authorization:'Bearer '+token()}: {}});
    if(!response.ok){const error=await response.json();throw Error(typeof error.detail==='string'?error.detail:JSON.stringify(error.detail))}
    download(token()?await response.blob():path,format==='export'?'my-assistant.zip':'workflow.json');
  }
  async function list(){
    const rows=await api('/v1/studio/assistants');
    $('savedAssistants').innerHTML=rows.length?rows.map(a=>`<div class="saved-row"><div><b>${escape(a.name)}</b><div class="muted">${a.construction==='automatic'?'按需求生成':'旧版配置 · 可重新生成'}</div></div><div class="actions"><button data-load="${a.id}">打开助手</button>${a.construction==='automatic'?`<button data-export="${a.id}">导出项目</button>`:''}</div></div>`).join(''):'<div class="empty-state"><h3>暂无助手</h3><button class="primary" data-go="create">创建助手</button></div>';
    $('savedAssistants').querySelectorAll('[data-load]').forEach(b=>b.onclick=guard(async()=>{epoch++;const a=rows.find(x=>x.id===b.dataset.load);identifier=a.id;$('runResult').replaceChildren();preferredModel=a.model==='mock'?'auto':a.model;drawModels();$('assistantName').value=a.name;$('purpose').value=a.purpose;savedSignature=signature();$('savedLabel').textContent='已载入';showTab('create');renderStatus(await api('/v1/studio/assistants/'+identifier+'/workflow'));}));
    $('savedAssistants').querySelectorAll('[data-export]').forEach(b=>b.onclick=guard(()=>exportProject(b.dataset.export)));
  }
  $('assistantForm').onsubmit=guard(async()=>{
    if(busy||!$('assistantForm').reportValidity())return;
    busy=true;const ticket=++epoch;$('buildAssistant').disabled=true;$('tryBtn').disabled=true;$('savedLabel').textContent='正在生成…';
    try{
      const body={name:$('assistantName').value,purpose:$('purpose').value,model:preferredModel,construction:'automatic'};
      const saved=await api('/v1/studio/assistants'+(identifier?'/'+identifier:''),identifier?'PUT':'POST',body);identifier=saved.id;savedSignature=signature();
      const id=identifier;await api('/v1/studio/assistants/'+id+'/build','POST');
      await followBuild(id,ticket);
      await list();await listWorkflows();
    }catch(error){renderStatus({status:'failed',message:error.message});throw error;}
    finally{busy=false;$('buildAssistant').disabled=false;}
  });
  const repairOptions=document.createElement('div');repairOptions.className='panel';repairOptions.innerHTML='<label><input type=checkbox id=autoRepair> 自动检查结果，修复或补充步骤后继续</label><label><input type=checkbox id=allowGoalCode> 允许生成并测试纯计算节点</label><label>最多修改次数<input type=number id=goalRevisions min=0 max=12 value=3></label><p class=muted>修复沿用当前预算与审批，修改记录可查。</p>';$('tryBtn').before(repairOptions);
  $('tryBtn').onclick=guard(async()=>{
    if(!plan||signature()!==savedSignature)throw Error('请先按当前需求生成工作流');
    if(!$('message').value.trim())throw Error('请填写本次要处理的材料');
    $('tryBtn').disabled=true;try{const run=await api('/v1/studio/assistants/'+identifier+'/run','POST',{message:$('message').value,auto_repair:$('autoRepair').checked,allow_code:$('allowGoalCode').checked,max_revisions:Number($('goalRevisions').value)});await watch(run.id,'runResult');}finally{$('tryBtn').disabled=!plan;}
  });
  $('refreshAssistants').onclick=guard(list);
  return {async refresh(models){
    const managed=await api('/v1/studio/connections');availableModels=models;defaultModel=managed.default_model;drawModels();
    const available=models.filter(m=>m.alias!=='mock'&&m.capabilities.includes('decision'));
    $('automaticModelStatus').textContent=available.length?'可指定构建模型；生成后的各步骤保留各自的模型配置。':'可以先创建助手并保存需求，稍后连接编排模型继续。';
    const templates=await api('/v1/studio/templates');$('templates').innerHTML=templates.map((t,i)=>`<button class="template" data-template="${i}"><h3>${escape(t.name)}</h3><p class="muted">${escape(t.description)}</p></button>`).join('');
    $('templates').querySelectorAll('button').forEach(b=>b.onclick=()=>{epoch++;identifier=null;preferredModel='auto';drawModels();const t=templates[+b.dataset.template];$('assistantName').value=t.name;$('purpose').value=t.purpose;invalidate();renderStatus({status:'not_built'});});
    await list();
  }};
}
