import {modelTypes} from './ui-labels.js';
export function modelCatalog({api,$,escape,reload,add,flash}) {
  let catalog=null,limit=30;
  const names={openai:'Chat Completions','openai-response':'Responses',anthropic:'Messages',gemini:'Gemini generateContent'};
  function render(){
    if(!catalog)return;
    const query=$('catalogSearch').value.toLowerCase(),type=$('catalogFilter').value;
    const matches=catalog.models.filter(m=>(!type||m.model_type===type)&&(m.id+JSON.stringify(m.operations)).toLowerCase().includes(query));
    $('catalogStatus').textContent=`${catalog.model_count} 个模型 · ${catalog.operation_count} 个接口操作 · ${catalog.missing_protocol_count} 个模型缺少接口说明。${catalog.connected?'已同步当前账户。':'当前为内置目录，请先同步账户。'}`;
    $('modelCatalogList').innerHTML=matches.slice(0,limit).map((m,i)=>`<article class="library-card"><b>${escape(m.id)}</b><small>${escape(modelTypes[m.model_type]||m.model_type||'类型未说明')} · ${m.operations.length?'有协议定义':'待补充接口'}</small>${m.operations.length?`<select aria-label="${escape(m.id)} 的接口" data-route="${i}">${m.operations.map(o=>`<option value="${escape(o.operation_id)}">${escape(names[o.protocol]||o.protocol)} · ${escape(o.method+' '+o.path)}</option>`).join('')}</select>`:'<p>接口信息不足，暂不自动连接。</p>'}<div class="actions">${m.normalized_adapter?`<button class="small" data-connect-model="${i}" ${catalog.connected?'':'disabled'}>连接文字模型</button>`:''}${m.operations.length?`<button class="small" data-native-model="${i}" ${catalog.connected?'':'disabled'}>添加接口节点</button>`:''}</div></article>`).join('')||'<p>没有匹配的模型。</p>';
    $('catalogMore').hidden=matches.length<=limit;
    $('modelCatalogList').querySelectorAll('[data-connect-model],[data-native-model]').forEach(b=>b.onclick=async()=>{
      b.disabled=true;$('catalogResult').textContent='正在接入…';
      try{
        const i=Number(b.dataset.connectModel??b.dataset.nativeModel),model=matches[i];
        if(b.dataset.connectModel!==undefined){const result=await api('/v1/studio/model-catalog/connect','POST',{model:model.id});await reload();$('catalogResult').textContent=`已连接 ${result.model} · ${result.dialect}。尚未调用模型，可在工作流中测试。`;}
        else{const operation=$('modelCatalogList').querySelector(`[data-route="${i}"]`).value;const result=await api('/v1/studio/model-catalog/nodes','POST',{model:model.id,operation_id:operation});await reload();$('modelCatalogDialog').close();add(result.step);flash('接口已加入节点库与画布，请填写请求参数后运行。');}
      }catch(error){$('catalogResult').textContent=error.message;}finally{b.disabled=false;}
    });
  }
  $('modelCatalogBtn').onclick=async()=>{
    $('modelCatalogDialog').showModal();$('catalogResult').textContent='';
    try{catalog=await api('/v1/studio/model-catalog');$('catalogFilter').innerHTML='<option value="">全部类型</option>'+[...new Set(catalog.models.map(m=>m.model_type).filter(Boolean))].sort().map(t=>`<option value="${escape(t)}">${escape(modelTypes[t]||t)}</option>`).join('');limit=30;render();}
    catch(error){$('catalogResult').textContent=error.message;}
  };
  $('closeModelCatalog').onclick=()=>$('modelCatalogDialog').close();
  for(const id of ['catalogSearch','catalogFilter'])$(id).oninput=()=>{limit=30;render();};
  $('catalogMore').onclick=()=>{limit+=30;render();};
  $('catalogDiscoverForm').onsubmit=async e=>{
    e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
    try{const values=Object.fromEntries(new FormData(e.target));if(!values.base_url)delete values.base_url;catalog=await api('/v1/studio/model-catalog/discover','POST',values);render();$('catalogResult').textContent='已同步目录，没有发起模型生成。';}
    catch(error){$('catalogResult').textContent=error.message;}
    finally{button.disabled=false;}
  };
}
