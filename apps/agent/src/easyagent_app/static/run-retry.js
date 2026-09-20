export function retryPanel(run,escape){
  if(run.status!=='failed'&&!run.retry?.verification_failure)return '';
  const retry=run.retry;
  if(!retry?.allowed)return `<p class="muted">${escape(retry?.reason||'请先查看失败原因。')}</p>`;
  return `<div class="notice run-retry"><p>已保留 ${retry.preserved_steps} 个完成步骤及已有附件，从失败处继续。</p>${retry.build_budget_upgrade?'<p class="muted">继续时会移除旧版系统添加的构建次数和总输出限制，保留已有草稿与执行记录。</p>':''}<div class="actions"><button data-retry-run>从失败处重试</button>${retry.longer_wait?'<button data-retry-run="longer">延长等待重试</button>':''}</div>${retry.longer_wait?'<p class="muted">延长等待：模型响应最多等待 300 秒；本次步骤至少预留 600 秒。仍受任务总预算限制。</p>':''}<p data-retry-error role="alert" hidden></p></div>`;
}

export function bindRetry(root,run,{api,onRetry}){
  root.querySelectorAll('[data-retry-run]').forEach(button=>button.onclick=async()=>{
    if(button.disabled)return;
    const buttons=[...root.querySelectorAll('[data-retry-run]')];buttons.forEach(b=>b.disabled=true);
    const label=button.textContent;
    button.textContent='正在提交重试…';button.setAttribute('aria-busy','true');
    const progress=document.createElement('p');progress.className='retry-request-status';progress.setAttribute('role','status');
    progress.innerHTML='<span class="retry-request-spinner" aria-hidden="true"></span>正在提交重试请求…';
    button.closest('.run-retry').append(progress);
    const error=root.querySelector('[data-retry-error]');if(error)error.hidden=true;
    let accepted=false;
    try{
      const resumed=await api('/v1/runs/'+run.id+'/retry','POST',{expected_updated:run.updated,longer_wait:button.dataset.retryRun==='longer'});
      accepted=true;button.textContent='已提交重试';progress.textContent='重试已提交，正在更新进度…';
      window.dispatchEvent(new CustomEvent('eah:run-retried',{detail:{id:run.id,run:resumed}}));
      await onRetry();
    }catch(e){
      if(error){error.textContent=accepted?'重试已提交，但进度刷新失败，请刷新页面查看。':e.message;error.hidden=false;}
      if(!accepted){buttons.forEach(b=>b.disabled=false);button.textContent=label;}
    }finally{button.removeAttribute('aria-busy');progress.remove();}
  });
}

export function retryProgress(step,escape){
  const info=step.retry_state;
  if(!info?.error||!['retrying','running','failed','queued'].includes(step.status))return '';
  const attempt=step.status==='running'?Math.max(1,step.attempts-(info.base_attempts||0)):info.attempt;
  let text=step.status==='retrying'?`第 ${attempt} / ${info.max_attempts} 次尝试失败，将于 ${new Date(step.ready_at*1000).toLocaleTimeString()} 自动重试。`:
    step.status==='running'?`正在进行第 ${attempt} / ${step.spec.max_attempts} 次尝试。`:
    step.status==='failed'?`本轮已尝试 ${attempt} 次。${info.error.retryable?'自动重试已结束，可手动继续。':'请先检查错误原因或配置。'}`:'正在从失败处继续。';
  return `<p class="muted" role="status">${escape(text)}</p>`;
}

export function readableError(step){
  if(!step.error)return '';
  if(step.retry_state?.error?.category&&step.retry_state.error.category!=='execution')return step.retry_state.error.message;
  if(/^ReadTimeout:/.test(step.error))return '等待远程服务返回数据超时。已完成结果保留，可从失败处重试，或延长等待。';
  if(step.error==='ValueError: provider did not complete the response')return '模型服务已响应，但没有返回完整结果。旧记录未保存具体结束原因；请检查模型服务或调整任务后重试。';
  return step.error;
}
