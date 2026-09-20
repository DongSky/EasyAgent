# Local Python SDK and CLI

[简体中文](SDK_GUIDE.zh-CN.md) | **English**

Start with [keyword research to image generation](GETTING_STARTED.en.md); use this page as the API and advanced usage reference.

Use `easyagent` in local scripts and `easyagent_client` for remote calls.
The browser application is a third package, `easyagent-app`, connected through the same HTTP API.

## Install

Registry publication is not assumed. From the repository root:

```sh
uv sync --locked
uv run python examples/getting_started/media/research_image.py --export research-image.workflow.json
```

The base install does not need FastAPI, frontend assets, MCP, a browser, JavaScript or WASM engines.
Choose extras `server`, `mcp`, `code`, `browser`, or `documents` as needed; `app` includes all application dependencies.
`uv build --all-packages` creates three independent wheels. Install the local SDK wheel together with the `easyagent-client` wheel.

## Multiple inputs, branches and joins

See the [research-to-image tutorial](GETTING_STARTED.en.md) and [complete source](../examples/getting_started/media/research_image.py). `Module.forward` parameters are workflow inputs. Nodes accept multiple parameters, return objects or lists, and feed multiple consumers. References determine dependencies; independent steps may run concurrently.

```python
class Picture(Module):
    def forward(self, keywords: str, reference_image: str, style: str):
        sources = evidence(self.search({"query": keywords}))
        prompt = self.writer(drawing_brief(sources, keywords, style))
        image = self.draw(prompt, reference_image, "1024x1024")
        notes = self.save_sources(source_notes(sources, keywords))
        return deliver(image, notes, prompt)
```

This excerpt illustrates wiring; the tutorial supplies constructors and helpers. Notes do not wait for the image; `deliver` waits for every producer. `Call("tool")(some_node(...))` accepts a whole symbolic dictionary that resolves to an object at runtime. `Artifact("sources.md", media_type="text/markdown")(content)` saves a run-owned artifact.

`Sequential` passes one result to the next module. It does not guess how to unpack dictionaries or tuples. Wire multiple arguments explicitly in `Module.forward`; incompatible chains fail during graph construction.

Parallel branching differs from conditional routing. Python `if` over symbolic results is unsupported. Durable conditions use `Step.when` / `Workflow`; automatic merging of mutually exclusive branches is not part of the shorthand API. Source-code line order is not an execution dependency.

## Define, compose, execute

```python
from easyagent import Agent, Sequential, node

@node
def clean(text: str) -> str:
    return text.strip()

flow = Sequential(clean, Agent("mock"))
print(flow("  hello  "))
flow.export("flow.json")
```

This runs offline. `mock` is an explicit echo fixture, not a language model.
Replace it with your model name and set `OPENAI_API_KEY` plus optional `OPENAI_BASE_URL` to use the existing Chat Completions adapter.
`Agent()` reads `EAH_MODEL` and fails clearly if missing. Configure other protocols, fallbacks and pricing through `Runtime(config="config.json")` or `runtime.hub.models.register(...)`.

`@node` and `@tool` derive input/output schemas from annotations and accept sync or async functions. Unannotated values accept any JSON.
The default is local computation. Declare external writes with `@node(effect="write")`: these require approval and are not assumed idempotent.
Functions are trusted local code, not a sandbox. Sync functions run in threads; cancellation cannot forcibly stop their side effects. Use process extensions when stronger isolation is needed.

## Nodes and subworkflows

`@node` defines one scheduled operation containing arbitrary ordinary Python code. `Node(function)` is its explicit constructor. `@tool` retains the same execution behavior; both work in `Agent(tools=[...])`. Retries and approvals apply to the whole node.

`Sequential` and `Module.forward` expand steps in the same graph. `Subflow(module)` explicitly creates a child run and returns that module's result. `Subflow("saved.id", revision=1)({...})` returns a saved workflow's named output dictionary. Call ordinary Python helpers inside nodes and compose SDK modules outside node handlers.

```python
from easyagent import Agent, Sequential, Subflow

review = Sequential(Agent("mock"), Agent("mock"))
flow = Sequential(Subflow(review), Agent("mock"))
print(flow("hello"))
```

The parent has two steps and the child has two. Approvals, budgets, cancellation and recovery continue to apply across the parent and child runs.

Library node templates use `NodeDefinition` and instantiate to one Step. Saved workflows retain their type, even with one step, and instantiate as subworkflow calls. See the [beginner guide](GETTING_STARTED.en.md) for a complete example.

## Custom structures

```python
from easyagent import Agent, Module

class Review(Module):
    def __init__(self):
        self.draft = Agent("mock", instructions="Draft a reply")
        self.check = Agent("mock", instructions="Check for omissions")

    def forward(self, prompt: str):
        draft = self.draft(prompt)
        return {"draft": draft, "review": self.check(draft)}

print(Review()("Plan a move"))
```

`forward()` runs during graph construction. Module calls add nodes without executing models or tools. References infer dependencies; independent steps can run concurrently.
Index values with `value["image"]["id"]`. Unknown values cannot drive Python `if`, iteration, string formatting or arithmetic during construction. Put data operations in tools, or use low-level `Workflow` for `when`, `foreach` and goal loops. The API borrows callable modules and composition, not all of PyTorch's dynamic graph semantics.

`Agent` returns text, or structured data with `response_schema`.
`Model` makes one model call and returns its full `ModelResult`.
`Call("api.node")({"argument": value})` reuses a registered API/tool node.
`Subflow("saved.id", revision=1)({...})` calls a saved workflow and returns named outputs. Execution pins its version.

## Lifecycle, records and async calls

```python
from easyagent import Agent, Runtime

agent = Agent("mock")
with Runtime(".eah/project.db", key="operation-1") as runtime:
    result = runtime.run(agent, "hello")
    print(result.id, result.value, result.outputs)
```

Without an explicit Runtime, each call uses `.eah/local.db`, configurable with `EAH_DATABASE`.
Reuse a context for multiple calls. A `key` identifies one operation: reusing it with different workflow/input data raises a conflict. Omit it for unrelated operations, or use separate keyed sessions.
`runtime.hub` retains provider, extension, Skills, budget, event and scheduling APIs.

```python
async with Runtime(".eah/project.db") as runtime:
    result = await runtime.arun(agent, "hello")
    value = await agent.acall("another message")
```

Place this in an existing coroutine. Synchronous calls never nest event loops.
`RunStopped` exposes `run_id`, `status` and `state`; approval/input waits and failures are not success.
After approval, call `runtime.resume(id)` or `await runtime.aresume(id)`.
Timeout preserves the record. Leaving a local Runtime stops its workers; reopen the same database with the original code to continue. Use a continuously running Hub/service for unattended background work.

## CLI

```sh
easyagent run examples/getting_started/media/research_image.py:flow --input @inputs.json --config research-image.config.json --database .eah/project.db
easyagent run flow.json --input @inputs.json --database .eah/project.db
easyagent export examples/getting_started/media/research_image.py:flow --output flow.json
easyagent inspect RUN_ID --database .eah/project.db
easyagent events RUN_ID --database .eah/project.db
easyagent approve INVOCATION_ID --yes --database .eah/project.db
easyagent resume RUN_ID --source examples/getting_started/media/research_image.py:flow --config research-image.config.json --database .eah/project.db
```

`inputs.json` contains `keywords`, a `reference_image` artifact ID uploaded to the same database, `style`, and `size`. The [tutorial script](GETTING_STARTED.en.md) handles reference uploads for you. Exporting needs no credentials. Graphs containing Python nodes need their original code to execute or resume: use the `.py:flow` entry above. `easyagent run flow.json` is for workflows whose tools are already registered.

`--input -` reads a JSON object from stdin. stdout contains JSON; diagnostics use stderr.
Exit codes: `0` success, `1` execution failed/cancelled, `2` input/config/transport error, `3` human action needed, `4` timeout.
`--key` supplies an idempotency key; `--timeout` is in seconds. `python -m easyagent` is equivalent to `easyagent`.
Loading a `.py` entry executes trusted Python code, like running a script.

Exported JSON does not contain function source. It retains fingerprints; resuming requires the original registered code. Missing or changed functions are rejected.
Fingerprints detect function-source changes; they do not lock transitive Python dependencies or global state. Use [extension packages](EXTENSIONS.md) to distribute executable code.

## Remote calls and media demos

```python
from pathlib import Path
from easyagent_client import Client

with Client() as client:
    generate = client.workflow("media.expression_video", key="animation-1", timeout=1900)
    result = generate(reference_image=Path("reference.png"))
    result.download("image", "expression.png")
    result.download("video", "animation.mp4")
```

Install the [example workflows](../examples/getting_started/media/README.en.md) first.
`Client()` reads `EAH_URL` and `EAH_TOKEN`. `Path` inputs upload automatically; ordinary strings remain unchanged.
Async callers use `await client.workflow(id).run(inputs, key=..., timeout=...)`; `start()` and `result()` remain available.
JavaScript uses `.run(inputs, {key, timeoutMs})`; `file(path)` uploads Node files, and browsers accept `File`/`Blob`.
Rust uses `.workflow(id).timeout(Duration::from_secs(1900)).run(&inputs, Some(key)).await?`.
JavaScript/Rust remain independent HTTP clients, not implementations of the full local Python engine. Rust `core/` remains an embeddable execution subset.

```sh
easyagent run media.expression_video --url http://127.0.0.1:8765 --input @inputs.json --key animation-1 --timeout 1900
```

Here `reference_image` in JSON is an uploaded artifact ID. A remote wait timeout does not stop the server-side task.

## Run backend and frontend separately

```sh
uv run --extra server easyagent serve --port 8765
uv run --package easyagent-app easyagent-app --backend http://127.0.0.1:8765 --port 8766
```

Use a second terminal for the second command. `serve` provides only APIs; the app installs independently or can use your own static host and same-origin proxy.
Enable the example API with `serve --with-life`. `uv run --extra app easyagent studio` explicitly combines the packages, as does the desktop build.
See the [app package](../apps/agent/README.md) for deployment and proxy constraints.

The local Python runtime/SDK and App retain AGPL licensing. Independent HTTP/extension SDKs retain Apache-2.0. See [license scopes](../LICENSING.md).

Default local sessions only execute submitted or explicitly resumed run trees. They do not claim other scripts’ queued tasks or start workspace scheduling, maintenance or message delivery. An explicitly supplied `Runtime(hub=...)` retains that Hub’s execution scope.
