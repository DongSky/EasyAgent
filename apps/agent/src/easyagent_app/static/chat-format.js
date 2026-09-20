// Deliberately limited message formatting: escaped text only, with no raw HTML,
// remote images, link execution, or model-controlled attributes.
export function formatChat(text, escape) {
  const inline=value=>value.split(/(`[^`\n]+`)/g).map(part=>part.startsWith('`')&&part.endsWith('`')
    ? '<code>'+escape(part.slice(1,-1))+'</code>'
    : escape(part).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')).join('');
  const out=[];let list=null,paragraph=[],code=null;
  const flush=()=>{if(paragraph.length){out.push('<p>'+paragraph.map(inline).join('<br>')+'</p>');paragraph=[];}};
  const closeList=()=>{if(list){out.push('</'+list+'>');list=null;}};
  for(const line of String(text||'').split('\n')){
    if(line.trim().startsWith('```')){
      flush();closeList();if(code===null)code=[];else{out.push('<pre><code>'+escape(code.join('\n'))+'</code></pre>');code=null;}continue;
    }
    if(code!==null){code.push(line);continue;}
    if(!line.trim()){flush();closeList();continue;}
    const heading=line.match(/^#{1,6}\s+(.+)$/),item=line.match(/^\s*(?:([-*])|\d+[.)])\s+(.+)$/);
    if(heading){flush();closeList();out.push('<h4>'+inline(heading[1])+'</h4>');continue;}
    if(item){
      flush();const kind=item[1]?'ul':'ol';if(list!==kind){closeList();list=kind;out.push('<'+kind+'>');}
      const check=item[2].match(/^\[([ xX])\]\s+(.*)$/);
      out.push(check?'<li class="chat-check"><span aria-label="'+(check[1]===' '?'待办':'已完成')+'">'+(check[1]===' '?'☐':'☑')+'</span><div>'+inline(check[2])+'</div></li>':'<li>'+inline(item[2])+'</li>');continue;
    }
    closeList();paragraph.push(line);
  }
  flush();closeList();if(code!==null)out.push('<pre><code>'+escape(code.join('\n'))+'</code></pre>');return out.join('');
}
