// Results belong to their task row. Retain expanded rows during list refreshes.
export function runHistory({watch,stopWatch,escape,statuses,flash}) {
  const lists=new WeakMap();
  return function render(root,rows,emptyHTML) {
    const previous=lists.get(root)||new Map(),current=new Map();
    for(const run of rows) {
      let entry=previous.get(run.id);
      if(!entry) {
        const card=document.createElement('article');card.className='run-entry';card.dataset.runEntry=run.id;
        const header=document.createElement('div');header.className='run-row';
        const panel=document.createElement('div');panel.className='run-details';panel.hidden=true;
        panel.id=root.id+'-result-'+run.id;panel.setAttribute('role','region');
        card.append(header,panel);entry={card,header,panel};
      }
      const {header,panel}=entry;
      panel.setAttribute('aria-label',run.name+'的结果');
      header.innerHTML=`<div class="detail"><b>${escape(run.name)}</b><div class="muted">${new Date(run.created*1000).toLocaleString()}</div></div><span class="badge status-${escape(run.status)}">${statuses[run.status]||escape(run.status)}</span><button data-run="${escape(run.id)}" class="small run-toggle" aria-expanded="${!panel.hidden}" aria-controls="${escape(panel.id)}"><span class="run-chevron" aria-hidden="true"></span><span>${panel.hidden?'查看结果':'收起结果'}</span></button>`;
      const button=header.querySelector('[data-run]');
      button.onclick=async()=>{
        panel.hidden=!panel.hidden;
        button.setAttribute('aria-expanded',String(!panel.hidden));
        button.lastElementChild.textContent=panel.hidden?'查看结果':'收起结果';
        if(panel.hidden){stopWatch(panel);return;}
        panel.textContent='正在加载结果…';
        try {await watch(run.id,panel);}
        catch(error){if(panel.isConnected&&!panel.hidden){panel.textContent='加载失败，请收起后重试。';flash(error.message);}}
      };
      current.set(run.id,entry);
    }
    for(const [id,entry] of previous)if(!current.has(id))stopWatch(entry.panel);
    lists.set(root,current);
    root.replaceChildren(...[...current.values()].map(entry=>entry.card));
    if(!rows.length)root.innerHTML=emptyHTML;
  };
}
