// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0
const LIMIT = 50_000_000;
class InputFile { constructor(path) { this.path = path; } }
export const file = (path) => new InputFile(path);
async function prepareInputs(client, value) {
  if (value instanceof InputFile) return (await client.uploadFile(value.path)).id;
  if (typeof Blob !== 'undefined' && value instanceof Blob)
    return (await client.upload(value, {name:value.name || 'attachment',mediaType:value.type || 'application/octet-stream'})).id;
  if (Array.isArray(value)) return Promise.all(value.map(v => prepareInputs(client, v)));
  if (value && typeof value === 'object')
    return Object.fromEntries(await Promise.all(Object.entries(value).map(async ([k,v]) => [k, await prepareInputs(client,v)])));
  return value;
}
export class RunStopped extends Error {
  constructor(state) {
    super(
      `Run ${state.id}: ${state.status}. Inspect state, supply approval/input, then resume this run ID.`,
    );
    this.name = "RunStopped";
    this.state = state;
    this.runId = state.id;
    this.status = state.status;
  }
}
export class RunResult {
  constructor(client, state) {
    this.client = client;
    this.state = state;
    this.id = state.id;
    this.outputs = state.outputs;
    this.artifacts = state.artifacts;
  }
  async download(output, destination) {
    const value = this.outputs[output];
    let url,
      headers = {};
    if (value && typeof value === "object" && value.id && value.digest) {
      url = `${this.client.baseUrl}/v1/artifacts/${encodeURIComponent(value.id)}/content`;
      if (this.client.token)
        headers.Authorization = `Bearer ${this.client.token}`;
    } else if (typeof value === "string") {
      url = new URL(value);
      if (
        !["http:", "https:"].includes(url.protocol) ||
        url.username ||
        url.password ||
        url.hash ||
        (url.protocol === "http:" &&
          !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))
      )
        throw new Error(
          "Media URL must use HTTPS (or loopback HTTP), without embedded credentials",
        );
    } else
      throw new Error(
        "Named output must be an artifact descriptor or a media URL",
      );
    const response = await fetch(url, {
      headers,
      credentials: "omit",
      redirect: "error",
      signal: AbortSignal.timeout(120000),
    });
    if (!response.ok) throw new Error(`Media download HTTP ${response.status}`);
    const reader = response.body.getReader(),
      chunks = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > LIMIT) throw new Error("Download exceeds 50 MB limit");
        chunks.push(value);
      }
    } finally {
      await reader.cancel();
    }
    const blob = new Blob(chunks, {
      type: response.headers.get("content-type") || "application/octet-stream",
    });
    if (destination === undefined) return blob;
    const fs = await import("node:fs/promises"),
      path = await import("node:path");
    const target = path.resolve(destination),
      temporary = target + "." + crypto.randomUUID() + ".part";
    await fs.mkdir(path.dirname(target), { recursive: true });
    try {
      await fs.writeFile(temporary, new Uint8Array(await blob.arrayBuffer()), {
        flag: "wx",
      });
      await fs.rename(temporary, target);
      return target;
    } finally {
      await fs.rm(temporary, { force: true });
    }
  }
}
export class RunHandle {
  constructor(client, id) {
    this.client = client;
    this.id = id;
  }
  cancel() {
    return this.client.cancel(this.id);
  }
  async result({ timeoutMs = 600000, pollIntervalMs = 1000 } = {}) {
    if (timeoutMs <= 0 || pollIntervalMs <= 0)
      throw new Error("Timeout and polling interval must be positive");
    const deadline = performance.now() + timeoutMs;
    try {
      while (performance.now() < deadline) {
        const state = await this.client.request(
          "GET",
          `/v1/runs/${encodeURIComponent(this.id)}/result`,
          undefined,
          undefined,
          Math.max(
            1,
            Math.min(this.client.timeoutMs, deadline - performance.now()),
          ),
        );
        if (state.status === "succeeded")
          return new RunResult(this.client, state);
        if (
          [
            "failed",
            "cancelled",
            "waiting_approval",
            "waiting_input",
            "needs_attention",
          ].includes(state.status)
        )
          throw new RunStopped(state);
        await new Promise((resolve) =>
          setTimeout(
            resolve,
            Math.max(1, Math.min(pollIntervalMs, deadline - performance.now())),
          ),
        );
      }
    } catch (error) {
      if (error.name !== "TimeoutError") throw error;
    }
    const error = new Error(
      `Run ${this.id} timed out locally; resume it instead of resubmitting. The run was not cancelled.`,
    );
    error.name = "RunTimeout";
    error.runId = this.id;
    throw error;
  }
}
export class WorkflowHandle {
  constructor(client, id, revision) {
    this.client = client;
    this.id = id;
    this.revision = revision;
  }
  async run(inputs = {}, {key, ...wait} = {}) {
    const job = await this.start(await prepareInputs(this.client, inputs), {key});
    return job.result(wait);
  }
  async start(inputs = {}, { key } = {}) {
    const created = await this.client.request(
      "POST",
      `/v1/workflows/${encodeURIComponent(this.id)}/runs`,
      { inputs, revision: this.revision ?? null },
      key,
    );
    return new RunHandle(this.client, created.id);
  }
  definition() {
    return this.client.request(
      "GET",
      `/v1/workflows/${encodeURIComponent(this.id)}${this.revision === undefined ? "" : "?revision=" + this.revision}`,
    );
  }
}
export async function upload(
  client,
  data,
  { name = "attachment", mediaType = "application/octet-stream" } = {},
) {
  if ((data.size ?? data.byteLength) > LIMIT)
    throw new Error("File exceeds 50 MB upload limit");
  const headers = { "Content-Type": mediaType };
  if (client.token) headers.Authorization = `Bearer ${client.token}`;
  const response = await fetch(
    `${client.baseUrl}/v1/artifacts/upload?name=${encodeURIComponent(name)}`,
    {
      method: "POST",
      headers,
      body: data,
      signal: AbortSignal.timeout(client.timeoutMs),
    },
  );
  if (!response.ok) throw new Error(`Upload HTTP ${response.status}`);
  return response.json();
}
export async function uploadFile(client, filename) {
  const fs = await import("node:fs/promises"),
    path = await import("node:path");
  const file = await fs.open(filename, "r");
  let content;
  try {
    if ((await file.stat()).size > LIMIT)
      throw new Error("File exceeds 50 MB upload limit");
    content = await file.readFile();
  } finally {
    await file.close();
  }
  const mediaType =
    {
      ".png": "image/png",
      ".jpg": "image/jpeg",
      ".jpeg": "image/jpeg",
      ".webp": "image/webp",
      ".mp4": "video/mp4",
      ".wav": "audio/wav",
      ".mp3": "audio/mpeg",
      ".pdf": "application/pdf",
      ".txt": "text/plain",
    }[path.extname(filename).toLowerCase()] || "application/octet-stream";
  return upload(client, content, { name: path.basename(filename), mediaType });
}
