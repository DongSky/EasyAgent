// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

import {WorkflowHandle,RunHandle,upload,uploadFile} from './workflows.js';
export {WorkflowHandle,RunHandle,RunResult,RunStopped,file} from './workflows.js';

export class HubClient {
  static fromEnv(){return new HubClient(process.env.EAH_URL||'http://127.0.0.1:8765',{token:process.env.EAH_TOKEN||''});}
  workflow(id,{revision}={}){return new WorkflowHandle(this,id,revision);}
  runHandle(id){return new RunHandle(this,id);}
  upload(data,options){return upload(this,data,options);}
  uploadFile(filename){return uploadFile(this,filename);}
  constructor(baseUrl = "http://127.0.0.1:8765", { token = "", timeoutMs = 30000 } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.token = token;
    this.timeoutMs = timeoutMs;
  }
  async request(method, path, body, idempotencyKey, timeoutMs=this.timeoutMs) {
    const headers = { "Content-Type": "application/json" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const response = await fetch(`${this.baseUrl}${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(Math.ceil(timeoutMs)),
    });
    if (!response.ok) throw new Error(`Hub HTTP ${response.status}: ${await response.text()}`);
    return response.json();
  }
  skills() { return this.request('GET','/v1/skills'); }
  installSkill(packageData, expectedRevision=0) { return this.request('POST','/v1/skill-packages/install',{package:packageData,expected_revision:expectedRevision}); }
  skillSource(repository, path='', ref='HEAD') { return this.request('POST','/v1/skill-packages/source',{repository,path,ref}); }
  createGoal(objective, workflow, model, options={}) { return this.request('POST','/v1/goals',{objective,workflow,model,...options}); }
  goal(id) { return this.request('GET',`/v1/goals/${encodeURIComponent(id)}`); }
  controlGoal(id, action, feedback='') { return this.request('POST',`/v1/goals/${encodeURIComponent(id)}/control`,{action,feedback}); }
  extensions() { return this.request('GET','/v1/extensions'); }
  installExtension(packageData, options={}) { const {preview=false,...grants}=options; return this.request('POST',`/v1/extensions/${preview?'preview':'install'}`,{package:packageData,...grants}); }
  extensionCommand(name, input={}, {conversation_id=null}={}) { return this.request('POST',`/v1/extensions/commands/${encodeURIComponent(name)}`,{input,conversation_id}); }
  createConversation(model, {title='新对话',...agent}={}) { return this.request('POST','/v1/conversations',{model,title,agent:{prompt:'',...agent}}); }
  sendMessage(id,text,options={}) { return this.request('POST',`/v1/conversations/${encodeURIComponent(id)}/messages`,{text,...options}); }
  library() { return this.request("GET", "/v1/library"); }
  exportWorkflow(workflow) { return this.request('POST', '/v1/workflow-packages/export', {workflow}); }
  importWorkflow(packageData, bindings = {}, {preview = false} = {}) { return this.request('POST', `/v1/workflow-packages/${preview?'preview':'import'}`, {package:packageData, ...bindings}); }
  exportComponent(id, revision) { return this.request("GET", `/v1/library/${encodeURIComponent(id)}/package${revision===undefined?'':'?revision='+revision}`); }
  importComponent(packageData, credentialBindings = {}, {preview = false} = {}) { return this.request("POST", `/v1/library/packages/${preview?'preview':'import'}`, {package:packageData, credential_bindings:credentialBindings}); }
  instantiate(id, options = {}) { return this.request("POST", `/v1/library/${encodeURIComponent(id)}/instantiate`, options); }
  submit(workflow, key) { return this.request("POST", "/v1/runs", workflow, key); }
  run(id) { return this.request("GET", `/v1/runs/${encodeURIComponent(id)}`); }
  cancel(id) { return this.request("POST", `/v1/runs/${encodeURIComponent(id)}/cancel`); }
  approve(id, approved = true) { return this.request("POST", `/v1/approvals/${encodeURIComponent(id)}`, { approved }); }
  respond(id, values) { return this.request("POST", `/v1/inputs/${encodeURIComponent(id)}`, values); }
  events(id, after = 0) { return this.request("GET", `/v1/runs/${encodeURIComponent(id)}/events?after=${after}`); }
  async wait(id, timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const run = await this.run(id);
      if (["succeeded", "failed", "cancelled", "waiting_approval", "waiting_input", "needs_attention"].includes(run.status)) return run;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error("Timed out waiting for run");
  }
}
