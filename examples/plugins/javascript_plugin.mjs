// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

import { createInterface } from "node:readline";
const reader = createInterface({ input: process.stdin });
for await (const line of reader) {
  const request = JSON.parse(line);
  const { a, b } = request.params.arguments;
  console.log(JSON.stringify({ id: request.id, result: { value: a + b, language: "JavaScript" } }));
  break;
}
reader.close();
