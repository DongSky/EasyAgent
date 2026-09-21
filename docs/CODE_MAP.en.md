# Read the code by following one message

[简体中文](CODE_MAP.zh-CN.md) · [Offline first example](FIRST_STEPS.en.md) · [Developer reference](DEVELOPER_GUIDE.en.md)

Run the minimal example first, then follow the request below. Use responsibilities to locate files and execution stages to locate failures.

## Repository entry points

- `examples/getting_started/first_steps.py`: begin here to see ordinary functions become nodes.
- `src/easyagent/`: Python SDK and backend. `contracts.py` defines data, `modules.py` builds graphs, and `local.py` exposes the scripting Runtime.
- `apps/agent/src/easyagent_app/static/`: browser UI. It uses HTTP rather than reading the database or model credentials.
- `sdk/python/`, `sdk/javascript/`, `sdk/rust/`: remote clients of the same backend, without separate execution engines.
- `tests/integration/`: regressions. `scripts/live_agent_acceptance.py`: real-model acceptance through the product UI.
- `docs/`: start with this route and the developer reference. Dated acceptance records describe results at that date.

## Follow a natural-language message

1. **Send from the page**: find `send()` in `workspace-chat.js`. It collects text, attachments, and execution mode and calls `/v1/conversations/{id}/messages`.
2. **Receive and persist**: follow `install_conversations()` and `Conversations.send()` in `sessions.py`, then `WorkspaceChat.send()` in `workspace_chat/controller.py`. The message is stored before asynchronous execution.
3. **Choose a path**: `WorkspaceChat.begin()` matches a pinned saved workflow or starts an operator/compiler. `decide()` handles matching and `bind()` binds business inputs.
4. **Construct work**: `autonomy.py` supplies the operator's tools and instructions; `assistant_builder.py` generates and validates compiled plans. Both submit the same `Workflow` to the same execution engine.
5. **Execute nodes**: `Hub.submit()` in `runtime.py` freezes dependencies; workers claim steps and `execute()` dispatches each node type. `store.py` persists steps, checkpoints, and events.
6. **Call tools from an agent**: `Hub.agent()` enters `execution/agent.py`, which alternates model requests and tool results. `execution/context.py` handles compaction; `execution/model_calls.py` handles each model request. `ToolRegistry.invoke()` in `tools.py` handles validation, authority, approvals, and receipts. Tool recovery and workflow replanning are separate stages.
7. **Display results**: `WorkspaceChat.tick()` advances the conversation. The page polls progress, reads completed results, and displays files. Expanded details load the full execution record.

The scripting path is shorter: `Sequential / Module → Runtime.run → Hub.submit → worker → execute`, without UI or conversation matching.

## Where to make a change

- New computation: start with `@node`; no scheduler change is needed.
- Model tool calls or streaming: inspect `models/http.py` and `models/streaming.py`; run `test_agent_execution_integrity.py`.
- Parallel work, pause, and recovery: inspect the runtime and `store.py`; run `test_runtime.py` and `test_run_retry.py`.
- Natural-language matching and construction: inspect `workspace_chat/controller.py`, `autonomy.py`, and `assistant_builder.py`; run `test_workspace_chat.py` and `test_workflow_planning.py`.
- Chat rendering and downloads: start at `workspace-chat.js`; requests, state, templates, graph and activity live in its sibling `chat/` directory. Run `test_workspace_chat_browser.py`.

## Validate a change

Run related tests first, then the full suite. The full suite needs development/application extras and Chromium:

```sh
uv sync --locked --extra app --extra dev
uv run playwright install chromium
uv run pytest -q
uv run ruff check src apps tests scripts examples
```

Real-service acceptance incurs API usage; offline fixtures cannot prove model quality. See the [agent acceptance record (Chinese)](AGENT_EXECUTION_REPAIR.md) for the entry point and connection requirements.
