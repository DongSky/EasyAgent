// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

import { HubClient } from "./index.js";
const client = new HubClient(process.env.EAH_URL, { token: process.env.EAH_TOKEN });
const created = await client.submit({name: "JavaScript SDK demo", steps: [
  {id: "hello", target: "core.echo", input: {language: "JavaScript", message: "hello"}},
]});
const run = await client.wait(created.id);
if (run.status !== "succeeded") throw new Error(JSON.stringify(run));
console.log(JSON.stringify({language: "JavaScript", id: run.id, output: run.steps[0].output}));
const paused = await client.wait((await client.submit({name: "JS human input", limits: {tool_calls: 0}, steps: [
  {id: "question", kind: "input", input: {schema: {type: "object", properties: {date: {type: "string"}}, required: ["date"]}}},
  {id: "answer", kind: "transform", depends_on: ["question"], input: {date: {$ref: "question.date"}}},
]})).id);
if (paused.status !== "waiting_input") throw new Error("expected human input pause");
await client.respond(paused.input_requests[0].id, {date: "2026-10-26"});
const resumed = await client.wait(paused.id);
if (resumed.status !== "succeeded" || resumed.steps[1].output.date !== "2026-10-26") throw new Error("input resume failed");
