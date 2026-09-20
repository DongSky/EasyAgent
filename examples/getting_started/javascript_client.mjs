// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

// No npm installation or model key is needed. Start the Hub first.
import {readFile} from 'node:fs/promises';
import {HubClient} from '../../sdk/javascript/index.js';

const workflow = JSON.parse(await readFile(new URL('../first-workflow.json', import.meta.url), 'utf8'));
const client = new HubClient(process.env.EAH_URL || 'http://127.0.0.1:8765', {
  token: process.env.EAH_TOKEN || '',
});
const created = await client.submit(workflow);
const run = await client.wait(created.id);
console.log(JSON.stringify(run, null, 2));
if (run.status !== 'succeeded') {
  throw Error('Inspect the run status, approvals and input_requests before continuing.');
}
