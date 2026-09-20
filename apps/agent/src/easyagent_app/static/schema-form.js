// All entry points consume the same JSON Schema. Common nested inputs stay visual.
export function schemaForm(schema, value = {}) {
  const form = document.createElement('form');
  const definitions = schema;
  function resolve(raw) {
    if (!raw.$ref) return raw;
    if (!raw.$ref.startsWith('#/')) throw Error('表单只支持包内 Schema 引用');
    const target = raw.$ref.slice(2).split('/').reduce((v,k)=>v?.[k.replace(/~1/g,'/').replace(/~0/g,'~')], definitions);
    if (!target) throw Error('表单引用不存在');
    return {...target,...Object.fromEntries(Object.entries(raw).filter(([k])=>k!=='$ref'))};
  }
  function field(raw, initial, required, title, depth=0) {
    if(depth>10) throw Error('表单嵌套过深，请简化扩展输入');
    let spec=resolve(raw);
    if(spec.anyOf?.length===2 && spec.anyOf.some(s=>s.type==='null')) spec={...spec,...spec.anyOf.find(s=>s.type!=='null')};
    const wrap=document.createElement('fieldset');wrap.style.cssText='border:0;padding:8px 0;margin:0';
    const legend=document.createElement('legend');legend.textContent=spec.title||title;wrap.append(legend);
    if(spec.description){const hint=document.createElement('small');hint.textContent=spec.description;wrap.append(hint);}
    let enabled=null;
    if(!required && ['object','array'].includes(spec.type)){
      const label=document.createElement('label');enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=initial!==undefined||spec.default!==undefined;
      label.append(enabled,document.createTextNode('填写此项'));wrap.append(label);
    }
    initial=initial??spec.default;
    let read;
    if(spec.type==='object' && spec.properties){
      const children=Object.entries(spec.properties).map(([key,child])=>{
        const item=field(child,initial?.[key],(spec.required||[]).includes(key),key,depth+1);wrap.append(item.element);return [key,item];
      });
      read=()=>Object.fromEntries(children.map(([key,item])=>[key,item.read()]).filter(([,v])=>v!==undefined));
    } else if(spec.type==='array' && spec.items && !Array.isArray(spec.items)) {
      const rows=document.createElement('div'),add=document.createElement('button');add.type='button';add.textContent='添加一项';wrap.append(rows,add);
      const items=[];
      function append(v){
        const row=document.createElement('div'),item=field(spec.items,v,true,'项目',depth+1),remove=document.createElement('button');
        remove.type='button';remove.textContent='移除此项';row.append(item.element,remove);rows.append(row);const entry={row,item};items.push(entry);
        remove.onclick=()=>{items.splice(items.indexOf(entry),1);row.remove();};
      }
      (initial||[]).forEach(append);add.onclick=()=>{append(undefined);if(enabled)enabled.checked=true;};
      read=()=>{if(items.length<(spec.minItems||0) || items.length>(spec.maxItems??Infinity))throw Error((spec.title||title)+'的项目数量不符合要求');return items.map(x=>x.item.read());};
    } else {
      const type=Array.isArray(spec.type)?spec.type.find(t=>t!=='null'):spec.type;
      const complex=['object','array'].includes(type)||spec.oneOf||spec.anyOf;
      const input=document.createElement(spec.enum?'select':complex?'textarea':'input');
      input.setAttribute('aria-label',spec.title||title);input.name=title;
      if(spec.enum){
        if(!required){const blank=document.createElement('option');blank.value='';blank.textContent='不填写';input.append(blank);}
        for(const v of spec.enum){const option=document.createElement('option');option.value=JSON.stringify(v);option.textContent=String(v);input.append(option);}
        if(initial!==undefined)input.value=JSON.stringify(initial);
      }else{
        if(input.tagName==='INPUT')input.type=type==='boolean'?'checkbox':['number','integer'].includes(type)?'number':spec.format==='password'?'password':'text';
        if(type==='boolean')input.checked=initial??false;
        else input.value=initial===undefined?'':complex?JSON.stringify(initial,null,2):initial;
        if(spec.minimum!==undefined)input.min=spec.minimum;if(spec.maximum!==undefined)input.max=spec.maximum;
        if(spec.minLength!==undefined)input.minLength=spec.minLength;if(spec.maxLength!==undefined)input.maxLength=spec.maxLength;
        if(spec.pattern)input.pattern=spec.pattern;
        if(type==='number')input.step='any';if(type==='integer')input.step='1';
        if(complex){const hint=document.createElement('small');hint.textContent='此扩展使用自由结构；高级模式填写 JSON。';wrap.append(hint);}
      }
      // JSON Schema requires a boolean value, not a true value.
      input.required=required&&type!=='boolean';wrap.append(input);
      read=()=>{
        if(!input.reportValidity())throw Error('请检查 '+(spec.title||title));
        if(input.value===''&&!required&&type!=='boolean')return undefined;
        if(spec.enum)return JSON.parse(input.value);
        if(type==='boolean')return input.checked;
        if(['number','integer'].includes(type))return Number(input.value);
        if(complex){try{return JSON.parse(input.value);}catch{throw Error((spec.title||title)+'需要有效 JSON');}}
        return input.value;
      };
    }
    if(enabled){const previous=read;read=()=>enabled.checked?previous():undefined;}
    return {element:wrap,read};
  }
  const fields=Object.entries(schema.properties||{}).map(([key,spec])=>{
    const item=field(spec,value[key],(schema.required||[]).includes(key),key);form.append(item.element);return [key,item];
  });
  form.noValidate=true; // Validate only enabled optional groups through values().
  return {form,values:()=>Object.fromEntries(fields.map(([key,item])=>[key,item.read()]).filter(([,v])=>v!==undefined))};
}
