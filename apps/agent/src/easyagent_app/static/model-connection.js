export function modelConnection({api,$,load,flash}) {
  const dialog=$('connectionDialog'),form=$('connectionForm');let editing=null,busy=false;
  function close(){dialog.close();if(!busy)form.elements.api_key.value='';}
  function open(connection=null){
    if(busy){flash('连接请求仍在处理，请稍候');return;}
    editing=connection?.alias||null;form.reset();form.elements.alias.readOnly=!!editing;
    $('connectionTitle').textContent=editing?'编辑模型连接':'连接模型';
    $('connectionKeyHint').textContent=editing?'留空保留原密钥；修改服务地址时请重新填写。':'本地模型可以留空。';
    $('connectionResult').textContent='';$('connectionModels').replaceChildren();
    if(connection){for(const field of ['alias','base_url','dialect','model'])form.elements[field].value=connection[field];
      form.querySelectorAll('[name=capabilities]').forEach(c=>c.checked=connection.capabilities.includes(c.value));}
    dialog.showModal();
  }
  $('connectBtn').onclick=()=>open();
  window.addEventListener('eah:edit-model',e=>open(e.detail));
  $('closeConnection').onclick=close;$('closeConnectionFooter').onclick=close;
  dialog.addEventListener('close',()=>{if(!busy)form.elements.api_key.value='';});
  function body(){const data=new FormData(form);return {...Object.fromEntries(data),capabilities:data.getAll('capabilities')};}
  function lock(value){busy=value;form.querySelectorAll('input,select,button').forEach(el=>{if(el.id!=='closeConnectionFooter')el.disabled=value;});}
  $('discoverConnectionModels').onclick=async()=>{
    if(busy)return;const data=body();data.alias=data.alias||'catalog';
    lock(true);$('connectionResult').textContent='正在读取服务的模型列表…';
    try{const result=await api('/v1/studio/connections/discover'+(editing?'?existing_alias='+encodeURIComponent(editing):''),'POST',data);
      $('connectionModels').replaceChildren(...result.models.map(m=>new Option(m,m)));
      $('connectionResult').textContent=result.models.length?`已读取 ${result.models.length} 个模型，点击模型名称输入框选择，也可手动填写。`:'服务未返回模型，请手动填写模型 ID。';
    }catch(error){$('connectionResult').textContent=error.message;}
    finally{lock(false);if(!dialog.open)form.elements.api_key.value='';}
  };
  form.onsubmit=async event=>{
    event.preventDefault();if(busy)return;
    const data=body(),test=event.submitter?.id!=='saveConnectionOnly';
    lock(true);$('connectionResult').textContent=test?'正在测试连接…可关闭窗口。':'正在保存…';
    try{
      const path=editing?'/v1/studio/connections/'+encodeURIComponent(editing)+(test?'?test=true':''):'/v1/studio/connections'+(test?'/test-and-save':'');
      await api(path,editing?'PUT':'POST',data);form.elements.api_key.value='';
      close();flash(test?'模型连接已保存，测试通过':'模型连接已保存');
      window.dispatchEvent(new Event('eah:connections-changed'));await load();
    }catch(error){$('connectionResult').textContent='未完成：'+error.message;if(!dialog.open)flash(error.message);}
    finally{lock(false);if(!dialog.open)form.elements.api_key.value='';}
  };
}
