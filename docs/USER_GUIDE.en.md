# EasyAgent User Guide

[中文](USER_GUIDE.zh-CN.md) · **English** · [Project overview](../README.en.md) · [Developer guide](DEVELOPER_GUIDE.en.md) · [All documentation](README.en.md)

For version 0.1.0 development preview; updated September 20, 2026. This guide is for Studio users, assistant builders, and users of the Life Assistant example. Run commands from the repository root. Examples use port 8765; substitute your actual port. Chinese labels in parentheses match the current UI.

## Contents

- [Install and start](#install-and-start)
- [Understand the workspace](#understand-the-workspace)
- [Connect models and search](#connect-models-and-search)
- [Create an assistant with natural language](#create-an-assistant-with-natural-language)
- [Connect nodes on the canvas](#connect-nodes-on-the-canvas)
- [Run from code](#run-from-code)
- [Inspect tasks and arrange notifications](#inspect-tasks-and-arrange-notifications)
- [Conversations, knowledge, and memory](#conversations-knowledge-and-memory)
- [Install skills and extensions](#install-skills-and-extensions)
- [Images, video, and audio](#images-video-and-audio)
- [Automatic repair and learning](#automatic-repair-and-learning)
- [Reuse, export, and share](#reuse-export-and-share)
- [Life Assistant](#life-assistant)
- [Preserve data and use other platforms](#preserve-data-and-use-other-platforms)
- [Troubleshooting](#troubleshooting)

For scripts and terminals, use the [local SDK and CLI](SDK_GUIDE.en.md) without a browser. See the [independent App](../apps/agent/README.md) for separate frontend/backend deployment.

## Install and start

### Start from source

You need Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and a browser that can reach a local service. Python 3.12 is recommended. Node.js/Rust are only required for their development paths and the full integration suite.

```sh
uv sync --locked --extra app --python 3.12
uv run --extra app easyagent studio
```

Open [Studio](http://127.0.0.1:8765/). Leave the terminal running; Ctrl+C stops the server. Restart with the same database path to recover saved settings and history. Closing the browser does not stop the Python Hub, but it stops that browser from receiving system notifications.

Optional startup parameters:

```sh
uv run --extra app easyagent studio --port 8770 --database .eah/my-project.db --no-browser
```

Now open `http://127.0.0.1:8770/` manually. Different databases are independent workspaces; choosing another database does not copy connection keys. `serve` exposes the same API/Studio without opening a browser; `life` additionally mounts the example application.

An environment with a local macOS build can launch `.eah/build/desktop/EasyAgent.app`. This is a build output, not a file guaranteed to accompany every checkout, and it is not a notarized public installer. Platform validation limits appear later in this guide.

### First workflow without a key

Keep the server terminal open and run in a second terminal:

```sh
uv run easyagent run examples/first-workflow.json --url http://127.0.0.1:8765
```

Expect two successful steps and a `hello.json` artifact. In **Tasks** (`任务`), locate the run, expand **View results** (`查看结果`), and download the file. The example uses an echo tool: no network service or paid model is called, and language understanding is not tested. Images returned by the default `mock` provider are placeholders.

## Understand the workspace

- **Chat workspace** (`对话办事`): submit requests and attachments, select or create workflows automatically, and follow progress and results in the conversation.
- **My assistants** (`我的助手`): describe requirements, use the canvas, and manage saved assistants/workflows.
- **Tasks** (`任务`): runs, pending approvals, errors, and schedules. Each execution has its own record.
- **Knowledge** (`资料`): retrievable materials and confirmed preferences/facts.
- **Settings** (`设置`): models/services, Skills, extensions, learning, diagnostics/backups, and developer interfaces.

An **assistant** saves an intended purpose and processing method. A **workflow** contains executable steps. A **run/task** is one execution with particular input. A **node/tool** supplies a capability. An **artifact** is a generated downloadable file. Saving does not execute a workflow; successful execution does not necessarily mean an external notification has arrived.

The **User guide** (`使用指南`) dialog gives a brief introduction. `/docs` is the API reference; this file is the complete operational guide.

## Connect models and search

### Connect your model

1. Open **Settings → Models & Services → Connect model** (`设置 → 模型与服务 → 连接模型`).
2. Enter a recognizable connection name, endpoint, exact model ID, protocol, and your own API key.
3. Select Chat Completions, Responses, or Messages according to the provider's interface. A model name alone does not establish protocol support.
4. Save and test. A successful connection becomes available to assistants; correct configuration errors if the test fails.

Use the API base URL documented by the provider. Do not accidentally enter a complete `/chat/completions` request path as the base URL. Copying somebody else's model ID does not make your endpoint compatible.

Model keys saved in Settings are encrypted in the local credential store and restored on restart. They are not filled back into forms or included in workflow exports. A passing connection test proves one request, not all parameter combinations, output quality, or account capacity.

### Manage and select models

Each model added in the UI has **Edit** and **Delete** controls. A blank API key preserves the saved credential when editing; changing the endpoint requires entering the key again. Connection aliases are stable workflow references. Models supplied by configuration files or extensions must be changed at their source.

Use **Read available models** to fetch IDs from the service, or enter an ID manually. Listing does not generate content. **Save only** skips the potentially billable test. Select a default in settings, or select a particular model above the chat composer and in the assistant builder. Selection persists; wait for queued and active turns to finish before switching an existing conversation.

Deleting a connection removes its encrypted key and disables derived image adapters. History remains, while existing references require a replacement or a rebuilt workflow. Changing a default or conversation model does not rewrite model steps in saved workflows.

### Create first, connect later

You can submit a request with attachments or create an assistant before configuring any model. EasyAgent saves it and asks for a language model with structured output. Once a planner is available, missing execution services are listed separately, with a non-executable workflow blueprint when the steps can be planned.

Connect the requested service and return to the original conversation. The agent detects inventory changes, recompiles against actual interfaces, validates, and continues with the original attachments. Adding a connection alone does not run old tasks, unchanged connections do not repeatedly trigger model calls, and stopped tasks remain stopped. Saved assistants offer **Continue after connecting**. Waiting tasks and drafts survive restart; external actions retain normal approvals.

### Configure TinyFish search

In **Web search** (`联网搜索`), enter your own TinyFish API key and choose **Save connection** or **Save and test**. You can replace a key and test the current connection. Leaving the key blank preserves a previously saved settings key; it does not delete it.

The default tool is `search.tinyfish`. A saved user connection takes precedence over a demo/startup configuration with the same name. Advanced options can select an environment variable or custom gateway. Search returns links and snippets; their accuracy and relevance still require checking.

### Add another API or MCP service

**Add search / API** (`添加搜索 / API`) accepts manually configured HTTP interfaces and OpenAPI 3 JSON documents. Preview operations before choosing which to import. Keep keys in credential fields or aliases. Common JSON interfaces work directly; complex media, streaming, or custom signing may need native nodes or an extension.

**Notifications & Calendar** (`通知与日历`) manages outbound channels. **MCP service** (`MCP 服务`) connects a remote MCP service, handles its authorization, refreshes capabilities, and lets you choose which tools to expose. Discovering tools does not grant permission automatically. The no-code builder selects from connected, authorized capabilities; it does not create third-party accounts for you.

## Create an assistant with natural language

### Example: turn a school notice into actions

Connect a real language model, then open **My assistants → Create assistant** (`我的助手 → 创建助手`). Enter a name and a request such as:

> Turn the school notice I provide into an action list. For each action, include the task, owner, deadline, supplies, and supporting source text. Mark missing information as “needs confirmation”; do not invent it. Save a downloadable checklist.

1. Generate the workflow and wait for compilation to finish.
2. Inspect the steps, dependencies, and required inputs. Open the full definition or move to the canvas if needed.
3. Supply a sample notice and run it.
4. Inspect every step, any requests for information, and the final artifact.
5. Save the assistant when the output is useful; reuse it with new material later.

Check whether all actions from the source are included, dates have evidence, unknown fields remain unknown, and the file really downloads. The final sentence “done” is not sufficient evidence.

Compilation generates and validates a plan; it does not send messages or make bookings. Missing capabilities require additional setup and another build. Rebuild after changing requirements so you do not run a stale plan. Preview, execution, and export use the same saved workflow.

Your model must support the structured output needed for building. The built-in mock cannot compile natural-language requests. Compilation and execution have separate run records; closing a dialog does not cancel either task.

## Connect nodes on the canvas

Use the canvas when you know the sequence of operations.

1. Open the canvas from **My assistants** and name the workflow.
2. Add steps from tools/the node library, then fill in their parameter forms.
3. Drag an upstream output port to a downstream input port. A data connection creates a field reference and an execution dependency; an execution-only dependency does not copy all data.
4. Validate the workflow and fix missing inputs, incompatible types, or cycles.
5. Save and run, then inspect results step by step.

For a quick start, import `examples/first-workflow.json`, inspect the echo-to-artifact connection, change the initial input, and save/run your own copy.

The canvas supports moving, zooming, panning, auto-layout, undo/redo, and disconnecting ports. Independent steps may run in parallel. Conditions currently compare a field for equality; `foreach` iterates a bounded array, not an unlimited `while` loop. Common nodes include tool, model, agent, transform, retrieval, artifact, iteration, subworkflow, approval, and input. Goal repair uses a separate goal controller.

Complex parameters, retries, compensation, and subworkflows have advanced settings. Saving a generated graph from the canvas does not automatically overwrite the originating assistant's generated version.

## Run from code

With a Hub running, execute the repository examples:

```sh
uv run python examples/getting_started/python_client.py
node examples/getting_started/javascript_client.mjs
cargo run --locked --manifest-path sdk/rust/Cargo.toml --example demo
```

The second and third commands require Node.js 20+ and Rust/Cargo respectively. They connect to `http://127.0.0.1:8765` by default. Set `EAH_URL` for another Hub:

```sh
# macOS / Linux shell
export EAH_URL=http://127.0.0.1:8770
```

```powershell
# Windows PowerShell
$env:EAH_URL = "http://127.0.0.1:8770"
```

SDK wait methods return when approval, input, or reconciliation is needed; they do not approve actions for you. `easyagent run` does not read `EAH_URL`; use `--url`. See the [developer guide](DEVELOPER_GUIDE.en.md) for the programming interfaces.

## Inspect tasks and arrange notifications

### Handle run states

**View results** (`查看结果`) expands details directly below that record; click again to collapse. Filter or refresh the list as needed. Preview/download artifacts in the expanded section.

- **Queued / running**: the service is processing; inspect steps/events to see whether it is waiting for a remote result.
- **Waiting for approval**: review the specific tool, destination, and arguments before approving or denying.
- **Waiting for input**: answer the requested fields to resume the same run.
- **Needs reconciliation**: an external action may have happened without a reliable local receipt. Check the external result before recording evidence, so it is not accidentally repeated.
- **Succeeded**: workflow execution completed; external notification delivery still has its own record.
- **Failed / cancelled**: inspect the failing step. Cancellation does not undo external actions already accepted.

### Schedules

Save and manually verify the workflow before creating a schedule under **Tasks**. Choose a daily, weekday, or weekly template and confirm the time zone. The Hub must be running. Missed triggers are coalesced into a catch-up run rather than replaying every missed occurrence.

Scheduled execution does not bypass write approvals. Unattended external actions require an explicit deployment policy. Durable scheduling handles long waits without holding one model request open.

### System notifications

In **Settings → Models & Services → Notifications & Calendar**, select **System notification (current device)** (`系统通知（当前设备）`):

1. Click **Allow system notifications** (`允许系统通知`) and grant browser permission.
2. Keep the default names or enter your own, then save.
3. Use **Save and send test notification** or **Send test notification** on the saved connection.

No URL or API key is needed. Assistants and canvas nodes can use the saved capability with notification text and an optional title. Workflow calls still require write approval.

The receiver is **the device running the bound browser**, not the Hub server. Keep Studio open. Closing the page or suspending a phone leaves messages queued until the page returns. Changing browsers/ports or clearing site data requires rebinding. Existing workflows pinned to an older connection revision retain that older receiver.

Delivery records distinguish waiting, failure, uncertainty, and **submitted to the system**. The latter means the browser accepted the request, not that a banner appeared or somebody read it. OS focus/do-not-disturb settings can suppress banners. Use a compatible system browser if an embedded browser lacks support. Native iOS notification bridges and background Push are not currently provided.

### Messaging and calendars

Webhook, Telegram, Slack, Discord, Feishu, and CalDAV use your service endpoints, credentials, and recipients. Enable safe retries only when the receiving service actually supports idempotent deduplication. Deliveries use a durable queue and separate receipts. Writing a CalDAV event does not provide complete two-way calendar synchronization.

## Conversations, knowledge, and memory

Open **Chat workspace** (`对话办事`), also available from the floating button on other pages. Start a conversation, describe your request, and use **Add attachment** (`添加附件`), drag and drop, or paste an image. Connect your model and required services in Settings first; you do not need to select tools for each request.

The default **Automatic** (`自动安排`) mode matches a saved workflow and runs its fixed revision. When no suitable workflow exists, it generates, saves, and runs a new one. Unclear requests and missing required fields lead to clarification. The menu also lets you select a workflow, **Create workflow** (`创建新流程`), or **Chat only** (`仅对话`). Upload success does not mean every model can interpret the file.

Each request shows the actual execution graph, including running, completed, failed, and waiting steps. Click a node to expand details, handle approvals or missing inputs, and download files in place. **Open on canvas** (`在画布中打开`) lets you edit, export, or share the workflow. Stopping does not undo completed external actions; messages sent during execution are queued in order.

History and search appear on the left; on phones, tap **History** (`历史`). Reloading or restarting the same workspace preserves material, runs, and results. Each message accepts up to 8 attachments, 50 MB per file and 100 MB combined; direct model attachments are limited to 12 MB combined. Text, text-based PDFs, and DOCX files support extraction. Scanned documents need OCR; audio/video require a compatible model or processing API. PDF extraction covers at most the first 100 pages and 60,000 characters; split longer material.

**Advanced chat** (`高级对话`) keeps model/tool selection, steering, branches, and source summaries. Follow-up messages in the workflow workspace do not rewrite running steps. Summaries preserve originals and do not replace permanent memory. See [chat workspace and attachments](CONVERSATION_WORKSPACE.md) for details (in Chinese).

Under **Knowledge** (`资料`), add retrievable material and select its namespace. Retrieval supports lexical, vector, and hybrid modes; vector search needs a compatible embedding model. Check citations. A missed match is not proof that a fact does not exist.

**Remembered preferences** (`记住的偏好`) stores confirmed habits and facts. Namespaces bound access. Updates/merges check revisions or original values. Execution checkpoints resume runs; they are not a knowledge base.

## Install skills and extensions

### Skills: how to do something

In **Settings → Skills** (`设置 → 使用技巧`), choose built-in skills, import Markdown/JSON/a folder, or fetch from GitHub. Preview descriptions, files, and a pinned source revision before installing. You can also describe a method, generate a candidate, and review/install it.

A skill contains `SKILL.md` and optional reference text. Start a conversation message with `/skill-name`; the automatic builder can also discover installed skills. Inspect, export, disable, roll back, and check source updates from the same area.

Installing a skill does not execute its scripts or grant terminal/network/tool access. Text from Hermes-style skills can be reused, but platform-specific tools and dependencies still need adaptation.

### Extensions: actual capabilities

In **Settings → Extensions** (`设置 → 扩展`), import a source package or configure a trusted catalog. Review contributions, settings, permissions, and versions before installation. Contributions appear as capabilities, library nodes, or extension views; versions can be updated, activated, and exported.

Pure JavaScript/WASM extensions perform restricted computation. Python/Node/Rust process extensions require explicit trust in the exact source package and a compatible runtime, and have the host account's privileges. A signature/hash does not establish source safety. The installer does not run arbitrary dependency-install scripts.

Local browser/terminal execution is disabled by default. Configure allowed origins, working directories, and limits before enabling it. Those restrictions are not an OS sandbox.

## Images, video, and audio

The node library includes five [default media nodes](DEFAULT_MEDIA_NODES.md): image generation, reference-image editing, video submission, durable video waiting, and reference-image upload. Select a saved service connection and its model ID to bind them; no schema authoring is required. Binding performs no generation or upload, and each service must support the displayed protocol. Video submission also configures its separate waiting node.

1. Choose the exact model and operation in the model/API catalog; check its protocol and parameters.
2. Bind your service credentials and add the operation to the node library/canvas. Synchronizing the catalog does not start generation.
3. Enter the prompt and other fields. Reference-file inputs accept uploaded images or artifacts from the same Hub.
4. Preview/download outputs in task details. Asynchronous video usually needs submission, status waiting/polling, and result collection; cancel explicitly through the remote API when necessary.

Model names do not determine request shapes. Text, image editing, audio uploads, and video polling use different fields. The separate raw attachment upload accepts files up to 50 MB; ordinary JSON requests still have a 2 MB limit. External media APIs have their own request/response limits, so this does not imply support for large videos throughout the pipeline. Remote result URLs may expire. Re-upload referenced media when moving to another Hub.

Voice settings select transcription and speech models. Advanced-chat recordings are limited to 60 seconds and 1 MB. Review transcribed text before sending it; replies can be synthesized and played. This is not full-duplex realtime voice. Microphone permissions and actual paid voice accounts still need testing in your environment.

Live acceptance generated a chibi expression with `gpt-image-2.5-flare`, then animated it with `doubao-seedance-2-5-260628` into a roughly four-second, 720×720 video. This service required uploading the reference to obtain an HTTPS URL and handling the initial `pending` status. The output still contained an audio track despite requesting no audio. See the [media acceptance record](MEDIA_ACCEPTANCE.md); test other services and models separately.

## Automatic repair and learning

When creating an assistant, enable **Check results and repair or add steps** (`自动检查结果，修复或补充步骤后继续`), choose a revision limit, and decide whether to allow new pure-computation code.

The controller executes the plan, checks outputs, proposes necessary revisions, and validates the next plan. Versions, reasons, and child executions remain recorded. Give feedback, pause future iterations, or resume from task details. Reusable plans are saved as `goal_<run_id>.plan` and can be edited/shared.

The repair model cannot expand the goal's allowed capabilities or budgets. Repeated plans, exhausted budgets, or inability to finish stop with an incomplete result. Changed external writes are not blindly replayed. Semantic evaluators can be wrong; developers should add deterministic checks for critical values, fields, and receipts.

**Learning** (`经验改进`) is a separate path: generate a skill/policy candidate from a run, execute evaluations, then have a person approve publication or rollback. Automatic reflection is off by default and has a daily allowance. Repair does not automatically install arbitrary plugins or train a model.

## Reuse, export, and share

- **Workflow JSON** keeps steps, input, layout, and references; the receiver still needs the relevant models, tools, and data.
- **Assistant ZIP** includes the workflow, inputs, dependency notes, and Python/JS/Rust clients; it is not a standalone server installer.
- **Node/component package** reuses pinned HTTP capabilities or subworkflows. Preview and bind recipient credentials before importing.
- **Extension source package** contains code and declared permissions/dependencies, not the credential vault or run history.
- **Complete workflow package** carries the workflow snapshot, layout, fixed definitions, and packageable source extensions. MCP/connectors, knowledge, media inputs, and keys require recipient setup/rebinding.

Choose **Share complete workflow** in the canvas, saved workflows, or generated result. The recipient previews dependencies, supplies models/credentials, explicitly installs required extensions, and imports an independent copy. Import does not execute it. Personal information entered literally in prompts, parameters, or skills can still be included; inspect content before sharing.

Within one Hub, **Use as subworkflow** reuses a saved revision. Updating a component does not silently alter submitted runs. Select a newer revision explicitly and save again.

## Life Assistant

Run `uv run --extra app easyagent life` and open `/life`. The same `--port` and `--database` options apply.

1. Choose moving, travel, school notice, or repair; enter the main date and create a draft.
2. Paste original material. A model can propose tasks; confirm dates and sources before accepting them.
3. Assign owners and record who or what is blocking progress.
4. Adjust unfinished tasks when dates change; retain completed tasks and history.
5. Record costs, references, and completion evidence before closing tasks. Unfinished dependencies prevent premature completion.

Synthetic templates work without a key and are not described as model reasoning. The application's local reminders are primarily its pending-action lists. Adding a system notification connection does not automatically wire every life task to it; a workflow must explicitly call the capability.

Family responsibilities can be recorded, but multiple family accounts are not synchronized. Automatic mailbox integration, OCR, payments, calls, vendor bookings, and real concierge fulfillment are not implemented.

## Preserve data and use other platforms

Source deployments default to `.eah/hub.db`. The adjacent `.secrets.key` is needed to decrypt credentials. Protect both. Copying only a SQLite main file can omit WAL data. Knowledge, prompts, and outputs may also contain personal information.

Use **Settings → Diagnostics & Backup** or the consistent encrypted CLI backup. First supply `EAH_BACKUP_PASSWORD` through your own secure mechanism, with at least 12 characters:

```sh
uv run easyagent backup .eah/my-backup.eah --database .eah/hub.db
uv run easyagent restore .eah/my-backup.eah --database .eah/restored.db
uv run --extra app easyagent studio --port 8770 --database .eah/restored.db
```

Restore requires a new database path that does not already exist. Backups contain the database and credential decryption key; they do not automatically collect external plugin source, toolchains, environment variables, or remote files. Preserve those deployment dependencies separately. Diagnose with `uv run easyagent doctor --database .eah/hub.db`.

Phones can connect to your own HTTPS Hub. The Hub handles the full Python engine, trusted code, and long-running background work. The embedded mobile Rust core implements only a declarative execution subset. An iOS simulator offline flow has been tested; Android APK and physical-device background validation remain pending. Docker is not required, and permanent mobile background execution is not promised.

Remote deployment requires server-side `EAH_TOKEN`, an HTTPS entry point, and access control. This is a single-user token, not a tenant/account system. The browser keeps the Hub access token in page memory, so a reload can require entering it again.

## Troubleshooting

**Page unavailable or port occupied.** Check that the server terminal is running and the address matches its port. Choose another port/database. Do not stop unrelated services for this tutorial.

**401/403, 404, or unavailable model.** Check the key/account permissions, endpoint/protocol, then the exact model ID and account catalog. A passing test does not establish support for every model-specific parameter.

**Search still uses an old key.** Inspect the credential source in Web search. A saved settings key overrides startup environment configuration with the same name. Replace and test it. If explicitly using an environment variable, restart the process that reads it after changing the variable.

**No generated workflow or empty export.** Use a real model, check the build run, ensure requirements have not changed since building, and connect necessary APIs. Unbuilt/stale assistants reject export; a written requirement is not an executable plan.

**Task waits for approval/input.** Expand the run and respond to its actual request. This is durable suspension, not necessarily a hang. SDK wait methods also return these states.

**Output misses the goal.** Inspect intermediate input/output and the final file. Give specific missing items and checkable acceptance criteria. Goal repair can help, but budget exhaustion must still be treated as failure.

**No system notification banner.** Check browser support/permission, OS notification/focus settings, an open page, and the bound receiving browser. `queued` is not delivered; `submitted` is not visually confirmed. Reconcile uncertainty before repeating a notification.

**Media 503 or no video.** Keep the submission receipt and check for a remote task ID. Without an ID, do not claim generation succeeded. Avoid repeating writes whose outcomes are unknown. Local cancellation does not guarantee cancellation of the provider's billable task.

**An imported workflow does not run.** Resolve model, API credential, MCP, notification, extension, knowledge, and media dependencies in the preview. Artifact IDs and filesystem paths from another Hub do not automatically become local files.

**Dependency installation fails.** Try the recommended Python 3.12 and check uv, connectivity, platform wheels, and native build tools. The complete suite also needs Node, Cargo, and Chromium. Platforms without device validation do not have a guaranteed one-click setup.

Continue with the [developer guide](DEVELOPER_GUIDE.en.md) to build capabilities. The [latest delivery record](NEXT_DELIVERY.md) lists evidence and pending work; older stage-specific test counts are historical, not the current total.
