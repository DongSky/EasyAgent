import {stepStatus} from './run-status.js?v=20260921-operator-8';
import {retryPanel,bindRetry} from './run-retry.js?v=20260921-operator-8';
import {modelChoices} from './model-choice.js';
import {toolLabels} from './ui-labels.js';
import {formatChat} from './chat-format.js';
import {uploadMedia,bindMedia,clearMedia,canPreview} from './media-preview.js';

export function workspaceChat({api,escape,flash,showTab,renderRun,stopWatch,loadWorkflow,token,download,statuses}) {
  const nav=document.createElement('button');nav.dataset.tab='conversations';nav.textContent='对话办事';document.querySelector('.nav').append(nav);
  const page=document.createElement('section');page.id='conversations';page.className='section chat-workspace';
  page.innerHTML=`<div class="chat-page-heading"><div><h1>对话办事</h1></div><button class="text-button" data-go="agent-chat">高级对话 ↗</button></div>
  <div class="chat-layout"><aside class="chat-history"><button class="chat-new" data-new>＋ 新对话</button><label class="chat-search"><span class="sr-only">搜索对话</span><input type="search" data-search placeholder="搜索对话"></label><div data-history></div></aside>
  <article class="chat-room"><header class="chat-room-heading"><div><span class="chat-presence"></span><strong data-title>新对话</strong></div><div class="chat-mobile-tools"><button class="small" data-history-toggle aria-expanded="false">历史</button><button class="small" data-new-mobile>＋ 新对话</button></div></header>
  <div class="chat-scroll" data-scroll><div data-welcome class="chat-welcome"><div class="chat-starters"><button data-starter="帮我整理这份通知，列出要做的事和截止日期。"><span>▤</span><b>整理材料</b><small>通知、文档、会议记录</small></button><button data-starter="帮我找到合适的已有流程，处理我上传的图片。"><span>◇</span><b>使用已有流程</b></button><button data-starter="帮我创建一个可以重复使用的流程：" data-create-starter><span>⌘</span><b>创建流程</b></button></div></div><div data-timeline></div></div>
  <form class="chat-composer" data-compose><div class="chat-composer-top"><label><span class="sr-only">如何处理这条消息</span><select data-destination><option value="auto">✦ 自动安排</option><option value="create">＋ 创建新流程</option><option value="chat">仅对话</option></select></label><label class="chat-model-choice">模型<select data-model aria-label="处理模型"><option value="auto">自动选择已连接模型</option></select></label><label>执行方式<select data-execution aria-label="执行方式"><option value="automatic">自动执行</option><option value="confirm">逐项确认</option></select></label><span data-context>优先复用已有流程</span></div><div class="chat-attachments" data-files></div><label class="sr-only" for="workspaceMessage">你的需求</label><textarea id="workspaceMessage" data-text rows="3" placeholder="输入需求，或拖入附件"></textarea><div class="chat-composer-bottom"><button type="button" data-attach class="chat-attach" aria-label="添加图片、视频、音频或文档">＋ <span>添加附件</span></button><input type="file" multiple hidden data-file-input accept="image/*,audio/*,video/*,.pdf,.docx,.txt,.md,.csv,.json,.yaml,.yml"><span class="chat-compose-hint">Enter 发送 · Shift + Enter 换行</span><button type="button" data-stop class="small" hidden>停止本轮</button><button class="chat-send" data-send aria-label="发送需求">发送 <span aria-hidden="true">↑</span></button></div><p data-error class="chat-inline-error" role="alert" hidden></p></form><p class="chat-footnote" data-queue>自动执行已连接工具、Python 和媒体生成，可随时停止；服务无回执时，生图可能重新提交。</p><div class="chat-drop-zone" data-drop hidden>松开添加附件<span>图片 · 视频 · 音频 · 文档</span></div></article></div>`;
  document.querySelector('main').append(page);
  const $=s=>page.querySelector(s),guard=fn=>async e=>{try{await fn(e)}catch(error){$('[data-error]').textContent=error.message;$('[data-error]').hidden=false;flash(error.message);}};
  const floating=document.createElement('button');floating.className='chat-launcher';floating.innerHTML='<span aria-hidden="true">✦</span> 对话办事';floating.setAttribute('aria-label','打开对话办事入口');floating.onclick=()=>nav.click();document.body.append(floating);
  let selected=null,polling=false,sending=false,uploading=0,files=[],catalog=[],historyRows=[],signature='',searchTimer=null,modelList=[],defaultModel=null,chosenModel=localStorage.getItem('easyagent.workspaceModel')||'auto',modelBusy=false,turnBusy=false,workingTurn=null;
  const noiseKinds=new Set(['run.status']);
  const activityLabels={'tool.started':'调用','tool.succeeded':'完成','tool.failed_observed':'失败并反馈给助手','tool.input_rejected':'参数被拒绝，助手将修正','tool.unknown_requested':'请求了不存在的工具','tool.approval_required':'等待你确认','tool.authorized':'已按自动执行授权','model.started':'思考中','definition.saved':'已保存定义','run.created':'启动子任务','agent.recovering':'恢复上下文','context.compacted':'压缩历史','context.compaction_failed':'整理上下文失败，改用其他方式继续','input.requested':'等待你补充信息','session.steered':'已接收补充要求'};
  function activityHTML(events){
    const rows=[];
    for(const ev of events){const p=ev.payload||{},kind=ev.kind;let text=null;
      if(noiseKinds.has(kind))continue;
      if(kind==='tool.started')text='调用 '+(toolLabels[p.tool]||p.tool);
      else if(kind==='tool.succeeded')text='✓ '+(toolLabels[p.tool]||p.tool);
      else if(kind==='tool.failed_observed'||kind==='tool.input_rejected')text='✗ '+(toolLabels[p.tool]||p.tool)+' · '+(kind==='tool.failed_observed'?'失败（'+(p.code||'')+'），已反馈给助手修正':'参数无效，助手将修正');
      else if(kind==='tool.unknown_requested')text='✗ 请求了不存在的工具 '+p.tool+'，已告知助手';
      else if(kind==='tool.approval_required')text='Ⅱ 等待你确认 '+(toolLabels[p.tool]||p.tool);
      else if(kind==='definition.saved')text='💾 已保存'+({workflow:'流程',api:'接口节点',node:'节点',component:'组件'}[p.kind]||p.kind)+' '+p.id+' v'+p.revision;
      else if(kind==='model.started')text='… 思考中';
      else if(kind==='agent.recovering')text='↻ '+(p.reason==='context_overflow'?'上下文已满，已压缩后继续':'输出被截断，已要求分步继续');
      else if(kind==='input.requested')text='Ⅱ 等待你补充：'+(p.prompt||'');
      else if(kind==='context.compaction_failed')text='! '+(activityLabels[kind]||kind);
      else if(kind==='session.steered')text='✦ 已接收你的补充要求';
      if(text)rows.push({text,kind,time:ev.created});}
    const shown=rows.slice(-40);
    return shown.map(r=>`<div class="chat-activity-row kind-${escape(r.kind.replace(/\./g,'-'))}"><small>${new Date(r.time*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}</small><span>${escape(r.text)}</span></div>`).join('')+(rows.length>40?`<p class="muted">仅显示最近 40 条，共 ${rows.length} 条</p>`:'');
  }
  async function updateActivity(entry,run){
    const slot=entry.root.querySelector('[data-activity]');
    if(!run?.spec?.metadata?.operator){slot.hidden=true;return;}
    if(entry.eventRun!==run.id){entry.eventRun=run.id;entry.events=[];entry.eventCursor=0;}
    const fresh=await api(`/v1/runs/${run.id}/events?after=${entry.eventCursor}`);
    for(const ev of fresh){entry.eventCursor=Math.max(entry.eventCursor,ev.id||0);entry.events.push(ev);}
    if(!fresh.length&&slot.dataset.rendered===String(entry.eventCursor))return;
    slot.dataset.rendered=String(entry.eventCursor);slot.hidden=false;
    const running=['queued','running'].includes(run.status);
    slot.innerHTML=`<details ${running||!slot.dataset.userClosed?'open':''}><summary>活动记录 · ${entry.events.filter(e=>e.kind==='tool.succeeded').length} 次工具调用${running?' · 进行中':''}</summary><div class="chat-activity-list">${activityHTML(entry.events)}</div></details>`;
    slot.querySelector('details').ontoggle=e=>{slot.dataset.userClosed=e.target.open?'':'1';};
    if(running){const list=slot.querySelector('.chat-activity-list');list.scrollTop=list.scrollHeight;}
  }
  function drawModel(){modelChoices($('[data-model]'),modelList,chosenModel,defaultModel);$('[data-model]').disabled=turnBusy||sending||modelBusy;}
  async function loadModels(){const data=await api('/v1/studio/connections');modelList=data.connections;defaultModel=data.default_model;drawModel();}
  $('[data-model]').onchange=guard(async()=>{
    const value=$('[data-model]').value,id=selected;modelBusy=true;drawFiles();drawModel();
    try{if(id)await api('/v1/conversations/'+id,'PATCH',{model:value});
      if(id===selected){chosenModel=value;localStorage.setItem('easyagent.workspaceModel',value);signature='';}
    }finally{modelBusy=false;drawModel();drawFiles();}
  });
  $('[data-execution]').onchange=()=>{$('[data-queue]').textContent=$('[data-execution]').value==='automatic'?'自动执行已连接工具、Python 和媒体生成，可随时停止；服务无回执时，生图可能重新提交。':'外部写操作会逐项请求确认。';};
  window.addEventListener('eah:connections-changed',()=>loadModels().catch(e=>flash(e.message)));
  const drafts=new Map(),cards=new Map(),runCache=new Map();
  const size=n=>n<1e6?Math.ceil(n/1000)+' KB':(n/1e6).toFixed(1)+' MB';
  const glyph=type=>({image:'▧',audio:'♫',video:'▷',document:'▤'})[type]||'▤';
  const nearBottom=()=>{const el=$('[data-scroll]');return el.scrollHeight-el.clientHeight-el.scrollTop<100;};
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
  reducedMotion.addEventListener('change',()=>{if(reducedMotion.matches)page.querySelectorAll('.just-completed').forEach(node=>node.classList.remove('just-completed'));});
  function scrollEnd(force=false){if(force)$('[data-scroll]').scrollTo({top:$('[data-scroll]').scrollHeight,behavior:reducedMotion.matches?'auto':'smooth'});}
  function closeHistory(){$('.chat-history').classList.remove('mobile-open');$('[data-history-toggle]').setAttribute('aria-expanded','false');}
  $('[data-history-toggle]').onclick=()=>{const opened=$('.chat-history').classList.toggle('mobile-open');$('[data-history-toggle]').setAttribute('aria-expanded',String(opened));};
  page.addEventListener('keydown',e=>{if(e.key==='Escape')closeHistory();});
  function clearCards(){for(const entry of cards.values()){stopWatch(entry.details);entry.root.querySelectorAll('[data-child-run]').forEach(stopWatch);clearMedia(entry.media);}cards.clear();runCache.clear();$('[data-timeline]').replaceChildren();}
  function persistDraft(){drafts.set(selected||'new',{text:$('[data-text]').value,files,destination:$('[data-destination]').value,execution:$('[data-execution]').value});}
  function restoreDraft(){const draft=drafts.get(selected||'new')||{text:'',files:[],destination:'auto'};$('[data-text]').value=draft.text;files=draft.files;$('[data-destination]').value=draft.destination;$('[data-execution]').value=draft.execution||'automatic';drawFiles();}
  function drawFiles(){
    $('[data-files]').innerHTML=files.map((f,i)=>`<div class="chat-file ${f.error?'has-error':''}"><span>${glyph(f.kind||f.type?.split('/')[0])}</span><div><b>${escape(f.name)}</b><small>${f.error?'上传失败 · 移除后可重新添加':f.id?size(f.size):'正在上传…'}</small></div><button type="button" data-remove="${i}" aria-label="移除 ${escape(f.name)}">×</button></div>`).join('');
    $('[data-files]').querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{files.splice(Number(b.dataset.remove),1);drawFiles();});
    $('[data-send]').disabled=!!uploading||sending||modelBusy;$('[data-model]').disabled=turnBusy||sending||modelBusy;
  }
  async function attach(incoming){
    if(files.length+incoming.length>8)throw Error('每条消息最多添加 8 个附件。');
    for(const file of incoming){
      if(file.size>50_000_000)throw Error('单个附件不能超过 50 MB。');
      const group=files,entry={name:file.name,size:file.size,type:file.type};group.push(entry);uploading++;drawFiles();
      try{Object.assign(entry,await uploadMedia(file,token(),50_000_000));}
      catch(error){entry.error=error.message;flash(error.message);}
      finally{uploading--;drawFiles();}
    }
    $('[data-text]').focus();
  }
  $('[data-attach]').onclick=()=>$('[data-file-input]').click();
  $('[data-file-input]').onchange=guard(async e=>{await attach([...e.target.files]);e.target.value='';});
  $('[data-text]').addEventListener('paste',guard(async e=>{const list=[...(e.clipboardData?.files||[])];if(list.length){e.preventDefault();await attach(list);}}));
  let dragDepth=0;
  page.addEventListener('dragenter',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();dragDepth++;$('[data-drop]').hidden=false;}});
  page.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});
  page.addEventListener('dragleave',()=>{if(--dragDepth<=0)$('[data-drop]').hidden=true;});
  page.addEventListener('drop',guard(async e=>{if(!e.dataTransfer?.files.length)return;e.preventDefault();dragDepth=0;$('[data-drop]').hidden=true;await attach([...e.dataTransfer.files]);}));
  async function choose(id){closeHistory();persistDraft();selected=id;signature='';turnBusy=false;if(!id)chosenModel=localStorage.getItem('easyagent.workspaceModel')||'auto';drawModel();clearCards();restoreDraft();localStorage.setItem('easyagent.workspaceConversation',id||'');$('[data-title]').textContent='新对话';$('[data-welcome]').hidden=!!id;drawHistory();if(id){await api('/v1/conversations/'+id+'/resume-connections','POST',{});await refresh();}}
  const newChat=guard(async()=>{await choose(null);$('[data-text]').focus();});$('[data-new]').onclick=$('[data-new-mobile]').onclick=newChat;
  function drawHistory(){const query=$('[data-search]').value.trim().toLowerCase();$('[data-history]').innerHTML=historyRows.filter(r=>r.title.toLowerCase().includes(query)).map(r=>`<button data-thread="${escape(r.id)}" class="chat-history-item ${r.id===selected?'active':''}"><span>${escape(r.title)}</span><small>${r.active_run?'● 正在处理':new Date(r.created*1000).toLocaleDateString()}</small></button>`).join('')||(query?'<p class="empty-hint">没有匹配的对话</p>':'<p class="empty-hint">暂无对话</p>');$('[data-history]').querySelectorAll('[data-thread]').forEach(b=>b.onclick=guard(()=>choose(b.dataset.thread)));}
  async function listing(){historyRows=(await api('/v1/conversations')).filter(r=>r.workspace);drawHistory();}
  async function loadCatalog(){catalog=await api('/v1/conversations/workflow-catalog');const select=$('[data-destination]'),value=select.value;select.innerHTML='<option value="auto">✦ 自动安排</option><option value="create">＋ 创建新流程</option><option value="chat">仅对话</option><optgroup label="指定已保存的流程">'+catalog.map(c=>`<option value="${escape(c.key)}">${escape(c.title)} · v${c.revision}</option>`).join('')+'</optgroup>';if([...select.options].some(o=>o.value===value))select.value=value;}
  $('[data-search]').oninput=()=>{drawHistory();clearTimeout(searchTimer);if($('[data-search]').value.trim())searchTimer=setTimeout(guard(async()=>{const query=$('[data-search]').value.trim();const matches=await api('/v1/conversations/search?query='+encodeURIComponent(query));if(query!==$('[data-search]').value.trim())return;const ids=new Set(matches.map(m=>m.conversation));const rows=historyRows.filter(r=>ids.has(r.id)&&!r.title.toLowerCase().includes(query.toLowerCase()));for(const row of rows){const b=document.createElement('button');b.className='chat-history-item';b.textContent=row.title;b.onclick=guard(()=>choose(row.id));$('[data-history]').append(b);}}),300);};
  $('[data-destination]').onchange=()=>{$('[data-context]').textContent=$('[data-destination]').value==='auto'?'优先复用已有流程':$('[data-destination]').value==='create'?'生成并保存流程':$('[data-destination]').value==='chat'?'不执行工作流':'使用指定流程的固定版本';};
  page.querySelectorAll('[data-starter]').forEach(b=>b.onclick=()=>{$('[data-text]').value=b.dataset.starter;if(b.hasAttribute('data-create-starter'))$('[data-destination]').value='create';$('[data-destination]').dispatchEvent(new Event('change'));$('[data-text]').focus();});
  async function send(event){
    event?.preventDefault();if(sending||uploading||modelBusy)return;
    if(files.some(f=>f.error||!f.id))throw Error('请移除上传失败的附件，或重新上传。');
    const text=$('[data-text]').value.trim();if(!text&&!files.length)return;
    const destination=$('[data-destination]').value,attachments=files.map(f=>f.id);
    sending=true;drawFiles();$('[data-error]').hidden=true;
    try{
      if(!selected){const c=await api('/v1/conversations','POST',{workspace:true,model:chosenModel,title:(text||files[0]?.name||'新的对话').slice(0,60)});selected=c.id;localStorage.setItem('easyagent.workspaceConversation',selected);}
      const id=selected;
      // Retain the key on transport failure so retry cannot duplicate a model call or run.
      const payload={text,attachments,execution:$('[data-execution]').value,intent:['auto','create','chat'].includes(destination)?destination:'workflow',...(!['auto','create','chat'].includes(destination)?{workflow:destination}:{})};
      if(workingTurn&&destination==='auto')payload.mode='steer';
      const fingerprint=JSON.stringify({id,...payload});if(send.fingerprint!==fingerprint){send.fingerprint=fingerprint;send.key=crypto.randomUUID();}
      await api(`/v1/conversations/${id}/messages`,'POST',{...payload,idempotency_key:send.key});
      if(id===selected){$('[data-text]').value='';files=[];drafts.delete('new');drafts.delete(id);drawFiles();await refresh();scrollEnd(true);}send.fingerprint=null;await listing();
    }finally{sending=false;drawFiles();}
  }
  $('[data-compose]').onsubmit=guard(send);
  $('[data-text]').onkeydown=guard(async e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();await send();}});
  $('[data-stop]').onclick=guard(async()=>{await api(`/v1/conversations/${selected}/interrupt`,'POST',{});await refresh();});
  function graphHTML(run){
    const steps=run.steps,byId=new Map(steps.map(s=>[s.id,s])),levels=new Map();
    function depth(s){if(levels.has(s.id))return levels.get(s.id);const d=Math.max(-1,...s.spec.depends_on.map(id=>depth(byId.get(id))))+1;levels.set(s.id,d);return d;}
    steps.forEach(depth);const vertical=innerWidth<=760,columns=new Map(),positions=new Map();for(const s of steps){const d=levels.get(s.id),row=(columns.get(d)||[]);positions.set(s.id,{x:(vertical?row.length:d)*224+10,y:(vertical?d:row.length)*91+12});row.push(s);columns.set(d,row);}
    const breadth=Math.max(...[...columns.values()].map(a=>a.length)),width=Math.max(240,(vertical?breadth:columns.size)*224),height=(vertical?columns.size:breadth)*91+12;
    const lines=steps.flatMap(s=>s.spec.depends_on.map(id=>{
      const a=positions.get(id),b=positions.get(s.id);
      const d=vertical?`M${a.x+96},${a.y+63} C${a.x+96},${a.y+78} ${b.x+96},${b.y-15} ${b.x+96},${b.y}`:`M${a.x+192},${a.y+31} C${a.x+212},${a.y+31} ${b.x-20},${b.y+31} ${b.x},${b.y+31}`;
      return `<g class="chat-edge" data-from="${escape(id)}" data-to="${escape(s.id)}"><path class="chat-edge-track" d="${d}"/><path class="chat-edge-flow" d="${d}"/></g>`;
    })).join('');
    const labels=run.spec.metadata.step_labels||{},kinds={model:'模型处理',agent:'智能处理',artifact:'保存结果',transform:'整理数据',input:'补充信息',approval:'确认操作',retrieve:'检索资料',subworkflow:'子流程',foreach:'批量处理'};
    return `<div class="chat-dag-scroll" tabindex="0" aria-label="工作流实时执行图"><div class="chat-dag" style="width:${width}px;height:${height}px"><svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${lines}</svg>${steps.map(s=>{const p=positions.get(s.id);return `<button class="chat-node" style="left:${p.x}px;top:${p.y}px" data-node="${escape(s.id)}"><span class="chat-node-indicator" aria-hidden="true"></span><div><b>${escape(labels[s.id]||toolLabels[s.spec.target]||kinds[s.spec.kind]||s.spec.target||s.id)}</b><small></small></div></button>`;}).join('')}</div></div>`;
  }
  function updateGraph(entry,run){
    const graph=entry.root.querySelector('[data-graph]');
    // Keep existing nodes and SVG paths while states change: preserve focus,
    // scrolling and animation timelines across the 800 ms polling interval.
    const key=JSON.stringify([run.id,innerWidth<=760,run.steps.map(s=>[s.id,s.spec.depends_on])]);
    const rebuilt=key!==entry.lastGraph;
    if(rebuilt){
      entry.lastGraph=key;graph.innerHTML=graphHTML(run);
      graph.querySelectorAll('[data-node]').forEach(button=>{
        button.onclick=()=>{
          entry.details.hidden=false;
          const toggle=entry.root.querySelector('[data-details-toggle]');
          toggle.textContent='收起步骤与结果';toggle.setAttribute('aria-expanded','true');
          renderRun(entry.run,entry.details);
          entry.details.scrollIntoView({block:'nearest',behavior:reducedMotion.matches?'auto':'smooth'});
        };
        button.addEventListener('animationend',e=>{if(e.animationName==='chatNodeComplete')button.classList.remove('just-completed');});
      });
    }
    const terminal=['succeeded','failed','cancelled'].includes(run.status);
    const byId=new Map(run.steps.map(s=>[s.id,s]));
    const statusOf=step=>terminal&&step.status==='running'?run.status:step.status;
    const icons={running:'◌',retrying:'↻',succeeded:'✓',failed:'!',waiting_approval:'Ⅱ',waiting_input:'Ⅱ',needs_attention:'!',waiting_remote:'…',waiting_children:'…',cancelled:'−',skipped:'−'};
    graph.querySelectorAll('[data-node]').forEach(button=>{
      const step=byId.get(button.dataset.node),status=statusOf(step),previous=button.dataset.status;
      const label=stepStatus(run,{...step,status},statuses);
      button.querySelector('small').textContent=label;
      button.setAttribute('aria-label',button.querySelector('b').textContent+' · '+label);
      if(previous===status)return;
      button.dataset.status=status;
      button.className='chat-node state-'+status;
      button.querySelector('.chat-node-indicator').textContent=icons[status]||'·';
      if(!rebuilt&&previous&&status==='succeeded'&&!reducedMotion.matches)button.classList.add('just-completed');
    });
    graph.querySelectorAll('.chat-edge').forEach(edge=>{
      const from=statusOf(byId.get(edge.dataset.from)),to=statusOf(byId.get(edge.dataset.to));
      let status='pending';
      if(['cancelled','skipped'].includes(to))status='inactive';
      else if(to==='failed')status='failed';
      else if(['waiting_input','waiting_approval','needs_attention'].includes(to))status='waiting';
      else if(from==='succeeded'&&to==='running'&&!terminal)status='running';
      else if(from==='succeeded'&&to==='succeeded')status='succeeded';
      if(edge.dataset.status!==status){edge.dataset.status=status;edge.setAttribute('class','chat-edge state-'+status);}
    });
  }
  function ensureCard(turn){
    let entry=cards.get(turn.id);if(entry)return entry;
    const root=document.createElement('article');root.className='chat-turn';root.dataset.turn=turn.id;
    root.innerHTML='<div class="chat-user-message"><small>你</small><div data-user></div><div class="chat-sent-files" data-sent-files></div></div><div class="chat-task-card"><div data-card-heading></div><div data-graph></div><div data-children></div><div class="chat-activity" data-activity hidden></div><div data-choices class="chat-choices"></div><div class="chat-result-message" data-reply></div><div data-setup></div><div data-retry></div><div class="chat-result-media" data-media></div><div class="chat-task-actions"><button class="text-button" data-details-toggle aria-expanded="false">展开步骤与结果</button><button class="text-button" data-edit hidden>在画布中打开 ↗</button></div><div class="chat-task-details" data-details hidden></div></div>';
    entry={root,details:root.querySelector('[data-details]'),media:root.querySelector('[data-media]'),lastDetail:'',lastMedia:'',lastGraph:'',events:[],eventCursor:0,eventRun:null};cards.set(turn.id,entry);$('[data-timeline]').append(root);
    root.querySelector('[data-details-toggle]').onclick=guard(async e=>{entry.details.hidden=!entry.details.hidden;e.currentTarget.setAttribute('aria-expanded',String(!entry.details.hidden));e.currentTarget.textContent=entry.details.hidden?'展开步骤与结果':'收起步骤与结果';if(entry.details.hidden)stopWatch(entry.details);else if(entry.run)renderRun(entry.run,entry.details);});
    return entry;
  }
  async function paint(c){
    const history=historyRows.find(r=>r.id===c.id);if(history&&history.active_run!==c.active_run){history.active_run=c.active_run;drawHistory();}
    chosenModel=c.model;turnBusy=!!c.active_run||c.turns.some(t=>!['succeeded','failed','cancelled','waiting_connections','steered'].includes(t.status));drawModel();
    workingTurn=c.turns.find(t=>['starting','running'].includes(t.status)&&t.task?.phase?.startsWith('working'))||null;
    $('[data-send]').textContent=workingTurn?'补充要求 ↑':'发送 ↑';$('[data-text]').placeholder=workingTurn?'任务正在自主处理；发送的内容会补充给它':'输入需求，或拖入附件';
    const pinned=nearBottom();$('[data-title]').textContent=c.title;$('[data-welcome]').hidden=c.turns.length>0;$('[data-stop]').hidden=!c.turns.some(t=>!['succeeded','failed','cancelled'].includes(t.status));
    for(const turn of c.turns){
      const state=turn.task;if(!state)continue;const entry=ensureCard(turn),root=entry.root,find=s=>root.querySelector(s);
      find('[data-user]').textContent=turn.text;
      const attachments=state.attachments||[];find('[data-sent-files]').innerHTML=attachments.map(a=>`<button data-file="${escape(a.id)}">${glyph(a.kind)} ${escape(a.name)}</button>`).join('');find('[data-sent-files]').querySelectorAll('button').forEach(b=>b.onclick=guard(()=>downloadArtifact(attachments.find(a=>a.id===b.dataset.file))));
      let run=null;if(state.run_id){run=runCache.get(state.run_id);if(!run||!['succeeded','failed','cancelled'].includes(run.status)||['starting','running'].includes(turn.status)){run=await api('/v1/runs/'+state.run_id);runCache.set(run.id,run);}if(c.id!==selected)return;entry.run=run;}
      const labels={queued:'已收到',routing:'正在匹配合适的流程',building:'正在创建新流程',working:'正在自主处理',executing:'正在执行',completed:'处理完成',waiting_connections:'已保存 · 等待连接模型或服务',superseded:'已合并到后续消息',steered:'已补充到当前任务',clarification:'需要补充一点信息',answered:'回复',failed:'处理遇到问题',cancelled:'已停止'};
      const title=state.selected?.title||labels[state.phase]||'正在安排';
      const finished=run?.steps.filter(s=>['succeeded','skipped'].includes(s.status)).length||0;
      const active=run?['queued','running'].includes(run.status):['queued','routing','building','working','executing'].includes(state.phase);
      const retryWaiting=run?.steps.some(s=>s.status==='retrying')&&!run.steps.some(s=>s.status==='running');
      const label=retryWaiting?'等待自动重试':active&&state.phase==='failed'?'正在继续处理':run&&['executing','working'].includes(state.phase)?(state.phase==='working'&&['queued','running'].includes(run.status)?'正在自主处理':statuses[run.status]||run.status):labels[state.phase]||'正在安排';
      const heading=`<div class="chat-task-heading"><span class="chat-task-mark ${active?'is-working':''}">${active?'✦':state.phase==='completed'?'✓':state.phase==='failed'?'!':'✦'}</span><div><b>${escape(title)}</b><p>${escape(label)}${state.selected?' · 固定版本 v'+state.selected.revision:''}</p></div>${run&&['executing','completed'].includes(state.phase)?`<span class="chat-step-count">${finished}/${run.steps.length}</span>`:''}</div>${state.reason?`<p class="chat-route-reason">${escape(state.reason)}</p>`:''}`;
      if(heading!==entry.lastHeading){entry.lastHeading=heading;find('[data-card-heading]').innerHTML=heading;}
      if(run){updateGraph(entry,run);await updateActivity(entry,run);if(c.id!==selected)return;
        const children=find('[data-children]'),childKey=JSON.stringify(run.children||[]);
        if(entry.childKey!==childKey){entry.childKey=childKey;children.querySelectorAll('[data-child-run]').forEach(stopWatch);children.innerHTML=(run.children||[]).length?'<p class="muted">流程执行与协作任务</p>'+(run.children||[]).map(child=>`<details data-child="${escape(child.id)}"><summary>${child.kind==='agent'?'协作助手':'工作流'} · ${escape(child.name)} · ${escape(statuses[child.status]||child.status)}</summary><div data-child-run></div></details>`).join(''):'';
          children.querySelectorAll('[data-child]').forEach(detail=>detail.ontoggle=guard(async()=>{const body=detail.querySelector('[data-child-run]');if(detail.open){const child=await api('/v1/runs/'+detail.dataset.child);renderRun(child,body);}else stopWatch(body);}));
        }
      }
      const message=c.messages.filter(m=>m.turn_id===turn.id&&m.role==='assistant').at(-1);find('[data-reply]').innerHTML=formatChat(message?.content||(['clarification','failed','waiting_connections','superseded','steered'].includes(state.phase)?state.message:''),escape);
      find('[data-choices]').innerHTML=(state.choices||[]).map(choice=>`<button data-choice="${escape(choice.key)}">使用 ${escape(choice.title)} →</button>`).join('');find('[data-choices]').querySelectorAll('button').forEach(b=>b.onclick=guard(async()=>{await loadCatalog();$('[data-destination]').value=b.dataset.choice;if(!$('[data-destination]').value)throw Error('流程版本已更新，请在列表重新选择。');$('[data-text]').value=turn.text;files=[...attachments];drawFiles();await send();}));
      const setup=find('[data-setup]');
      setup.innerHTML=state.phase==='waiting_connections'?`<div class="notice"><ul>${(state.required_connections||[]).map(r=>`<li><b>${escape(r.title)}</b><p>${escape(r.reason)}</p></li>`).join('')}</ul><div class="actions"><button data-setup-connect>去连接模型或服务</button><button data-setup-resume ${state.can_resume?'':'disabled'}>识别连接并继续</button></div><p class="muted">需求和附件已保留。连接后返回本对话会继续检查，也可以发送补充说明。</p></div>${state.planned_steps?.length?'<h3>步骤草稿 · 待接入</h3><ol>'+state.planned_steps.map(s=>'<li><b>'+escape(s.title)+'</b><p>'+escape(s.description)+'</p><small>等待：'+escape(s.depends_on.map(id=>state.planned_steps.find(step=>step.id===id)?.title||id).join('、')||'无前置步骤')+'</small></li>').join('')+'</ol>':''}${state.blueprint?'<details><summary>查看已保存的流程草稿（待验证）</summary><ol>'+state.blueprint.steps.map(s=>'<li>'+escape(state.blueprint.metadata?.step_labels?.[s.id]||s.id)+'</li>').join('')+'</ol></details>':''}`:'';
      if(state.phase==='waiting_connections'){
        setup.querySelector('[data-setup-connect]').onclick=()=>document.querySelector('[data-tab="connections"]').click();
        setup.querySelector('[data-setup-resume]').onclick=guard(async()=>{await api('/v1/conversations/'+c.id+'/resume-connections?turn_id='+encodeURIComponent(turn.id),'POST',{});signature='';await refresh();});
      }
      const retryRoot=find('[data-retry]'),retryKey=JSON.stringify([run?.id,run?.updated,run?.status,run?.retry]);
      if(entry.retryKey!==retryKey){entry.retryKey=retryKey;retryRoot.innerHTML=run?retryPanel(run,escape):'';if(run)bindRetry(retryRoot,run,{api,onRetry:async()=>{runCache.delete(run.id);signature='';await refresh();}});}
      find('[data-edit]').hidden=!state.selected;find('[data-edit]').onclick=guard(async()=>{const saved=await api(`/v1/studio/workflows/${encodeURIComponent(state.selected.id)}?revision=${state.selected.revision}`);loadWorkflow(saved.workflow,saved);showTab('workflow');});
      find('[data-details-toggle]').hidden=!run;
      if(run){
        const wait=['waiting_approval','waiting_input','needs_attention'].includes(run.status),detailKey=JSON.stringify([run.status,run.approvals,run.input_requests,run.reconciliations,run.steps.map(s=>[s.id,s.status,s.attempts,s.ready_at,s.error]),run.children]);
        if(wait&&entry.lastDetail!==detailKey){entry.details.hidden=false;find('[data-details-toggle]').setAttribute('aria-expanded','true');find('[data-details-toggle]').textContent='收起步骤与结果';}
        if(!entry.details.hidden&&entry.lastDetail!==detailKey){renderRun(run,entry.details);entry.lastDetail=detailKey;}
        if(run.status==='succeeded'&&state.phase==='completed'&&entry.lastMedia!==run.id){
          const found=new Map();function visit(v){if(!v||typeof v!=='object')return;if(v.id&&v.digest&&v.media_type&&!attachments.some(a=>a.id===v.id))found.set(v.id,v);Object.values(v).forEach(visit);}run.steps.forEach(s=>visit(s.output));
          // Operator final answers contain text; its artifacts live in tool receipts and child runs.
          // Collect actual stored artifacts across the run tree so these deliverables are downloadable.
          const result=await api('/v1/runs/'+encodeURIComponent(run.id)+'/result');result.artifacts.forEach(visit);if(c.id!==selected)return;entry.lastMedia=run.id;
          clearMedia(entry.media);entry.media.innerHTML=[...found.values()].map(a=>`<div class="chat-result-file"><span>${glyph(a.media_type.split('/')[0])}</span><b>${escape(a.name)}</b><button class="small" data-download="${a.id}">下载</button>${canPreview(a.media_type)?`<button class="small" data-media-preview="${a.id}" data-media-type="${escape(a.media_type)}" data-filename="${escape(a.name)}">预览</button>`:''}</div>`).join('');entry.media.querySelectorAll('[data-download]').forEach(b=>b.onclick=guard(()=>downloadArtifact(found.get(b.dataset.download))));bindMedia(entry.media,token(),guard);
        }
      }
    }
    $('[data-queue]').textContent=c.turns.some(t=>['queued','starting'].includes(t.status))?'后续消息排队中':$('[data-execution]').value==='automatic'?'自动执行已连接工具、Python 和媒体生成，可随时停止；服务无回执时，生图可能重新提交。':'外部写操作会逐项请求确认。';if(pinned)scrollEnd(true);
  }
  async function downloadArtifact(a){const r=await fetch('/v1/artifacts/'+encodeURIComponent(a.id)+'/content',{headers:token()?{Authorization:'Bearer '+token()}:{}});if(!r.ok)throw Error('文件读取失败');download(await r.blob(),a.name);}
  async function refresh(){if(!selected||polling)return;polling=true;const id=selected;try{const c=await api('/v1/conversations/'+id);if(id!==selected)return;const next=JSON.stringify(c);if(next!==signature||c.active_run){signature=next;window.dispatchEvent(new CustomEvent('eah:conversation',{detail:{id:c.id}}));window.dispatchEvent(new CustomEvent('eah:message',{detail:{conversation:c.id,messages:c.messages}}));await paint(c);}}finally{polling=false;}}
  window.addEventListener('eah:run-retried',e=>{
    const run=e.detail.run;
    if(run)runCache.set(run.id,run);else runCache.delete(e.detail.id);
    for(const entry of cards.values()){
      entry.lastDetail='';entry.retryKey='';
      if(run&&entry.run?.id===run.id){
        entry.run=run;updateGraph(entry,run);entry.lastHeading='';
        const mark=entry.root.querySelector('.chat-task-mark');mark.textContent='✦';mark.classList.toggle('is-working',['queued','running'].includes(run.status));
        entry.root.querySelector('.chat-task-heading p').textContent='已提交重试，正在继续处理';
        entry.root.querySelector('[data-retry]').replaceChildren();
      }
    }
    signature='';refresh().catch(e=>flash(e.message));
  });
  nav.onclick=guard(async()=>{showTab('conversations');await Promise.all([listing(),loadCatalog(),loadModels()]);if(!selected){const cached=localStorage.getItem('easyagent.workspaceConversation');if(historyRows.some(r=>r.id===cached))await choose(cached);}if(selected)await api('/v1/conversations/'+selected+'/resume-connections','POST',{});signature='';await refresh();});
  document.addEventListener('eah:route',e=>{floating.hidden=e.detail.id==='conversations';if(e.detail.id!=='conversations')for(const entry of cards.values())stopWatch(entry.details);});
  document.addEventListener('eah:chat-workflow',guard(async e=>{await nav.onclick();await choose(null);$('[data-destination]').value=e.detail.key;$('[data-destination]').dispatchEvent(new Event('change'));$('[data-text]').focus();}));
  setInterval(()=>{if(page.classList.contains('active')&&!document.hidden)refresh().catch(e=>flash(e.message));},800);
  window.addEventListener('resize',()=>{signature='';if(page.classList.contains('active'))refresh().catch(e=>flash(e.message));});
  restoreDraft();
}
