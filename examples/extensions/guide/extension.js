// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

function handle(request) {
  const args = request.params;
  if (request.method.startsWith('lifecycle.')) return {result: {ready: true}};
  if (request.method === 'quote') {
    return {result: {total: Math.round(args.price * args.count * 100) / 100, currency: args.currency}};
  }
  if (request.method === 'context') {
    return {result: {patch: {messages: args.value.messages.concat([
      {role: 'system', content: '给出清晰的计算依据，保留货币单位。'}
    ])}}};
  }
  if (request.method === 'counter') {
    const count = (request.context.state.value.count || 0) + 1;
    return {result: {count}, state: {count}};
  }
  throw Error('unknown handler');
}
