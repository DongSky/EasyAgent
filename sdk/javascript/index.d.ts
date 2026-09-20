// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

import type { ExtensionPackage, AgentConfig } from './contracts.js';
export type { ExtensionManifest, ExtensionPackage, ExtensionRequest, ExtensionResponse, AgentConfig, ModelRequest, ModelResult, ToolSpec, ConversationCreate, ConversationInput, WorkflowCall, NodeDefinition, ComponentPackage } from './contracts.js';
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface WorkflowReference { id: string; revision?: number | null; }
export interface Step {
  id: string; kind?: "tool" | "model" | "agent" | "transform" | "retrieve" | "artifact" | "foreach" | "subworkflow" | "approval" | "input"; target?: string;
  input?: Record<string, Json>; depends_on?: string[]; max_attempts?: number;
  timeout_seconds?: number; not_before?: number; requires_approval?: boolean;
  when?: { source: string; equals: Json }; body?: Workflow; workflow_ref?: WorkflowReference; tool_revision?: number | null;
  compensate?: { target: string; input?: Record<string, Json>; tool_revision?: number };
}
export interface RunLimits { model_calls?: number; tool_calls?: number; output_tokens?: number; cost_usd?: number | null; child_runs?: number; wall_time_seconds?: number; }
export interface Workflow { name: string; steps: Step[]; metadata?: Record<string, Json>; inputs?: Record<string, Json>; limits?: RunLimits; }
export interface Run { id: string; status: string; steps: Array<Record<string, Json>>; approvals: Array<Record<string, Json>>; input_requests: Array<Record<string, Json>>; children: Array<Record<string, Json>>; usage: Record<string, Json>; }
export class HubClient {
  static fromEnv(): HubClient;
  workflow<T extends object = Record<string,Json>>(id: string, options?: {revision?: number}): WorkflowHandle<T>;
  runHandle<T extends object = Record<string,Json>>(id: string): RunHandle<T>;
  upload(data: Blob | Uint8Array | ArrayBuffer, options?: {name?: string; mediaType?: string}): Promise<Artifact>;
  uploadFile(filename: string): Promise<Artifact>;
  constructor(baseUrl?: string, options?: {token?: string; timeoutMs?: number});
  request(method: string, path: string, body?: Json | Workflow, idempotencyKey?: string, timeoutMs?: number): Promise<any>;
  skills(): Promise<unknown[]>;
  installSkill(packageData: object, expectedRevision?: number): Promise<unknown>;
  skillSource(repository: string, path?: string, ref?: string): Promise<unknown>;
  createGoal(objective: string, workflow: object, model: string, options?: object): Promise<{id:string}>;
  goal(id: string): Promise<unknown>;
  controlGoal(id: string, action: 'pause'|'resume'|'feedback', feedback?: string): Promise<unknown>;
  extensions(): Promise<unknown[]>;
  installExtension(packageData: ExtensionPackage, options?: {preview?:boolean,grants?:string[],trust_digest?:string}): Promise<any>;
  extensionCommand(name:string,input?:Record<string,unknown>,options?:{conversation_id?:string}): Promise<{run_id:string}>;
  createConversation(model:string, options?:Partial<AgentConfig> & {title?:string}): Promise<any>;
  sendMessage(id:string,text:string,options?:{mode?:'follow_up'|'steer',idempotency_key?:string}): Promise<any>;
  library(): Promise<Array<Record<string, Json>>>;
  exportWorkflow(workflow: Workflow): Promise<Record<string, Json>>;
  importWorkflow(packageData: Record<string, Json>, bindings?: WorkflowBindings, options?: {preview?: boolean}): Promise<Record<string, Json>>;
  exportComponent(id: string, revision?: number): Promise<Record<string, Json>>;
  importComponent(packageData: Record<string, Json>, credentialBindings?: Record<string, string>, options?: {preview?: boolean}): Promise<Record<string, Json>>;
  instantiate(id: string, options?: {revision?: number; step_id?: string; input?: Record<string, Json>}): Promise<{step: Step; component: ComponentManifest; component_type: "node" | "subworkflow"; resolution: Record<string, Json>}>;
  submit(workflow: Workflow, key?: string): Promise<{id: string; status: string}>;
  run(id: string): Promise<Run>;
  cancel(id: string): Promise<Run>;
  approve(id: string, approved?: boolean): Promise<{approved: boolean}>;
  respond(id: string, values: Record<string, Json>): Promise<{accepted: boolean}>;
  events(id: string, after?: number): Promise<Array<Record<string, Json>>>;
  wait(id: string, timeoutMs?: number): Promise<Run>;
}

export interface Artifact {id:string;name:string;media_type:string;digest:string;size:number;run_id:string|null;created:number;}
export interface WorkflowResult {id:string;status:string;outputs:Record<string,Json>;artifacts:Artifact[];approvals:Record<string,Json>[];input_requests:Record<string,Json>[];errors:Record<string,Json>[];}
export class WorkflowHandle<T extends object = Record<string,Json>> {
  readonly id:string; readonly revision?:number;
  start<I extends object = Record<string,Json>>(inputs?:I,options?:{key?:string}):Promise<RunHandle<T>>;
  run<I extends object = Record<string,unknown>>(inputs?:I,options?:{key?:string;timeoutMs?:number;pollIntervalMs?:number}):Promise<RunResult<T>>;
  definition():Promise<{id:string;revision:number;workflow:Workflow}>;
}
export interface InputFile { readonly path:string; }
export function file(path:string):InputFile;
export class RunHandle<T extends object = Record<string,Json>> {
  readonly id:string;
  result(options?:{timeoutMs?:number;pollIntervalMs?:number}):Promise<RunResult<T>>;
  cancel():Promise<Run>;
}
export class RunResult<T extends object = Record<string,Json>> {
  readonly id:string;readonly outputs:T;readonly artifacts:Artifact[];readonly state:WorkflowResult;
  download(output:keyof T & string):Promise<Blob>;
  download(output:keyof T & string,destination:string):Promise<string>;
}
export class RunStopped extends Error {readonly state:WorkflowResult;readonly runId:string;readonly status:string;}

export type Platform = "macos" | "windows" | "linux" | "android" | "ios";
export interface WorkflowBindings { model_bindings?: Record<string,string>; tool_bindings?: Record<string,string>; credential_bindings?: Record<string,string>; knowledge_bindings?: Record<string,string>; memory_bindings?: Record<string,string>; allow_development?: boolean; workflow_id?: string; }
export interface RuntimeRequirements { platforms: Platform[]; capabilities: string[]; network_origins: string[]; credential_refs: string[]; memory_mb: number; }
export interface RuntimeProfile { id: string; platform: Platform; capabilities: string[]; network_origins: string[]; credential_refs: string[]; memory_mb: number; location: "local" | "remote"; }
export interface ComponentReference { kind: "api" | "node" | "workflow" | "component"; id: string; revision: number; digest: string; }
export interface ComponentManifest { schema_version: 1; id: string; revision: number; title: string; description: string; source: ComponentReference; dependencies: ComponentReference[]; input_schema: Record<string, Json>; output_schema: Record<string, Json>; defaults: Record<string, Json>; requirements: RuntimeRequirements; effect: "read" | "write" | "local"; docs: string[]; validation: "protocol_integration" | "live_verified" | "unverified"; }
export interface CodePackage { id: string; revision: number; language: "python" | "javascript" | "rust" | "wasm"; entrypoint: string; source_digests: Record<string, string>; dependency_lock_digest: string; requirements: RuntimeRequirements; integration_scenarios: string[]; state: "draft" | "built" | "tested" | "published"; }
