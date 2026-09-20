"""Exports the exact saved workflow and runnable clients, never provider credentials."""
import json

from .contracts import Workflow


def project_files(hub, assistant, workflow):
    data = workflow.model_dump()
    tools, models, skill_names, namespaces = set(), set(), set(), set()
    labels = workflow.metadata.get("step_labels", {})
    def collect(flow):
        flow = Workflow.model_validate(flow).model_dump()
        for s in flow["steps"]:
            if s["kind"] == "tool":
                tools.add(s["target"])
            if s["kind"] in ("model", "agent"):
                models.add(s["target"])
                tools.update(s["input"].get("tools", []))
                skill_names.update(s["input"].get("skills", []))
                namespaces.update(s["input"].get("knowledge", []))
            if s["kind"] == "retrieve" and isinstance(s["input"].get("namespace"), str):
                namespaces.add(s["input"]["namespace"])
            if s.get("compensate"):
                tools.add(s["compensate"]["target"])
            if s.get("body"):
                collect(s["body"])
    collect(data)
    manifest = {"requires_running_hub": True, "tools": [hub.tools.spec(t).model_dump() for t in sorted(tools)],
                "models": [m for m in hub.models.catalog() if m["alias"] in models],
                "skills": [s for s in hub.skills.catalog() if s["name"] in skill_names], "knowledge_namespaces": sorted(namespaces)}
    outline = "\n".join(f"{i+1}. {labels.get(s.id, s.id)} ({s.kind}: {s.target or s.id}) — 前置：{', '.join(s.depends_on) or '无'}" for i, s in enumerate(workflow.steps))
    return {
        "assistant.json": json.dumps(assistant, ensure_ascii=False, indent=2),
        "workflow.json": json.dumps(data, ensure_ascii=False, indent=2),
        "input.json": json.dumps(workflow.inputs, ensure_ascii=False, indent=2),
        "dependencies.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        "WORKFLOW.md": f"# {workflow.name}\n\n{assistant['purpose']}\n\n{outline}\n\n完整节点参数、分支、连线及子流程见 workflow.json。\n",
        "README.md": "# 已生成的助手项目\n\nworkflow.json 是页面预览、实际运行和导出共用的工作流，不会在执行时重新编造计划。\n\n"
            "先启动已配置相同工具和模型的 EasyAgent。编辑 input.json 的 message 为本次材料，再运行以下任一入口。\n\n"
            "Python: pip install -r requirements.txt，然后 python run.py\n\nJavaScript: Node.js 20+，node run.mjs\n\nRust: cargo run\n\n"
            "EAH_URL 默认 http://127.0.0.1:8765；EAH_TOKEN 在服务开启认证时设置。\n\n"
            "客户端等待结果，遇到补充信息或审批会输出暂停状态，请在 Studio 运行记录中处理。\n\n"
            "dependencies.json 列出需要接入的模型和工具。密钥、连接地址及凭证不包含在导出包内；这不是脱离 Hub 的独立部署镜像。\n",
        "requirements.txt": "httpx>=0.28,<1\n",
        "run.py": '''import asyncio, json, os, sys, httpx
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent
async def main():
    workflow = json.loads((ROOT / 'workflow.json').read_text(encoding='utf-8'))
    workflow['inputs'].update(json.loads((ROOT / 'input.json').read_text(encoding='utf-8')))
    async with httpx.AsyncClient(base_url=os.getenv('EAH_URL','http://127.0.0.1:8765'), headers={'Authorization':'Bearer '+os.environ['EAH_TOKEN']} if os.getenv('EAH_TOKEN') else {}) as client:
        response = await client.post('/v1/runs', json=workflow)
        response.raise_for_status()
        identifier = response.json()['id']
        async with asyncio.timeout(600):
            while True:
                response = await client.get('/v1/runs/'+identifier)
                response.raise_for_status()
                result = response.json()
                if result['status'] in ('succeeded','failed','cancelled','waiting_input','waiting_approval','needs_attention'):
                    print(json.dumps(result,ensure_ascii=False,indent=2))
                    return
                await asyncio.sleep(.3)
asyncio.run(main())
''',
        "run.mjs": '''import {readFile} from 'node:fs/promises';
const load=async name=>JSON.parse(await readFile(new URL(name,import.meta.url),'utf8'));
const workflow=await load('workflow.json');Object.assign(workflow.inputs,await load('input.json'));
const base=(process.env.EAH_URL||'http://127.0.0.1:8765').replace(/\\/$/,'');
const headers={'Content-Type':'application/json',...(process.env.EAH_TOKEN?{'Authorization':'Bearer '+process.env.EAH_TOKEN}:{})};
async function request(path,body){const r=await fetch(base+path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error(await r.text());return r.json()}
const {id}=await request('/v1/runs',workflow);const deadline=Date.now()+600000;
while(true){const result=await request('/v1/runs/'+id);if(['succeeded','failed','cancelled','waiting_input','waiting_approval','needs_attention'].includes(result.status)){console.log(JSON.stringify(result,null,2));break}if(Date.now()>deadline)throw Error('Waiting timed out; inspect the run in Studio');await new Promise(r=>setTimeout(r,300))}
''',
        "Cargo.toml": '[package]\nname="my-assistant"\nversion="0.1.0"\nedition="2021"\n[dependencies]\nreqwest={version="0.12",features=["blocking","json"]}\nserde_json="1"\n',
        "src/main.rs": '''fn main()->Result<(),Box<dyn std::error::Error>> {
    let root=std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut workflow:serde_json::Value=serde_json::from_str(&std::fs::read_to_string(root.join("workflow.json"))?)?;
    let input:serde_json::Value=serde_json::from_str(&std::fs::read_to_string(root.join("input.json"))?)?;
    for (key,value) in input.as_object().ok_or("input must be an object")? {workflow["inputs"][key]=value.clone();}
    let base=std::env::var("EAH_URL").unwrap_or("http://127.0.0.1:8765".into());
    let token=std::env::var("EAH_TOKEN").unwrap_or_default();
    let mut headers=reqwest::header::HeaderMap::new();
    if !token.is_empty() {headers.insert(reqwest::header::AUTHORIZATION,format!("Bearer {}",token).parse()?);}
    let client=reqwest::blocking::Client::builder().default_headers(headers).build()?;
    let created:serde_json::Value=client.post(format!("{}/v1/runs",base.trim_end_matches('/'))).json(&workflow).send()?.error_for_status()?.json()?;
    let id=created["id"].as_str().ok_or("missing run id")?;
    let start=std::time::Instant::now();
    loop {
        let run:serde_json::Value=client.get(format!("{}/v1/runs/{}",base.trim_end_matches('/'),id)).send()?.error_for_status()?.json()?;
        if ["succeeded","failed","cancelled","waiting_input","waiting_approval","needs_attention"].contains(&run["status"].as_str().unwrap_or("")) {println!("{}",serde_json::to_string_pretty(&run)?);break;}
        if start.elapsed().as_secs()>600 {return Err("Waiting timed out; inspect the run in Studio".into());}
        std::thread::sleep(std::time::Duration::from_millis(300));
    }
    Ok(())
}
'''}
