// Preserve an unavailable explicit selection instead of silently using another service.
export function modelChoices(select, models, value='auto', defaultModel=null) {
  select.replaceChildren(new Option(defaultModel?'默认模型 · '+defaultModel:'自动选择已连接模型','auto'));
  for(const m of models.filter(m=>m.alias!=='mock'&&m.capabilities.includes('decision')))
    select.add(new Option(m.alias+' · '+m.model,m.alias));
  if(value!=='auto'&&![...select.options].some(o=>o.value===value)) {
    const missing=new Option(value+'（连接已不可用，请重新选择）',value);missing.disabled=true;select.add(missing);
  }
  select.value=value;
}
