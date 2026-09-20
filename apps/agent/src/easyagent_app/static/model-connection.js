export function modelConnection({api,$,load,flash}) {
  const dialog=$('connectionDialog');let attempt=0;
  function close(){dialog.close();}
  $('connectBtn').onclick=()=>{dialog.showModal();};
  $('closeConnection').onclick=close;$('closeConnectionFooter').onclick=close;
  dialog.addEventListener('cancel',()=>{ /* Native Escape closes independently of request status. */ });
  $('connectionForm').onsubmit=async event=>{
    event.preventDefault();const current=++attempt,button=$('saveConnection'),form=event.target;
    button.disabled=true;button.textContent='正在测试…';$('connectionResult').textContent='正在测试连接…可关闭窗口。';
    try{
      const data=new FormData(form),body=Object.fromEntries(data);body.capabilities=data.getAll('capabilities');
      await api('/v1/studio/connections/test-and-save','POST',body);form.elements.api_key.value='';
      if(current!==attempt)return;
      // Close immediately on success; unrelated page refresh errors must not trap the dialog.
      close();flash('模型连接已保存，测试通过');
      await load();window.dispatchEvent(new Event('eah:connections-changed'));
    }catch(error){$('connectionResult').textContent='未完成：'+error.message;}
    finally{button.disabled=false;button.textContent='保存并测试';}
  };
}
