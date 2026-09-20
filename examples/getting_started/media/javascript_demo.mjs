// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0
import { join } from "node:path";
import { HubClient, RunStopped, file } from "../../../sdk/javascript/index.js";

const [reference, output = "output/media-sdk/javascript"] =
  process.argv.slice(2);
if (!reference)
  throw new Error(
    "Usage: node javascript_demo.mjs reference.png [output-directory]",
  );
const client = HubClient.fromEnv(),
  key = process.env.EAH_DEMO_KEY || "media-demo";
try {
  const result = await client
    .workflow("media.expression_video")
    .run({ reference_image: file(reference) }, { key, timeoutMs: 1900000 });
  await result.download("image", join(output, "expression.png"));
  await result.download("video", join(output, "animation.mp4"));
  console.log(
    JSON.stringify({ language: "JavaScript", run_id: result.id, output }),
  );
} catch (error) {
  if (!(error instanceof RunStopped)) throw error;
  console.error(error.message);
  process.exitCode = 2;
}
