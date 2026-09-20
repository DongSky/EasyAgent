<div align="center">

# EasyAgent

**Build, run, and share your own AI assistants.**

Start with natural language or a visual canvas. Extend with Python, JavaScript, and Rust.

<p>
  <a href="./README.md">简体中文</a> | <strong>English</strong>
</p>

<p>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/version-0.1.0%20preview-orange" alt="Version: 0.1.0 development preview"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11 or later"></a>
  <a href="#development"><img src="https://img.shields.io/badge/SDK-Python%20%7C%20JavaScript%20%7C%20Rust-6366F1" alt="Python, JavaScript, and Rust SDKs"></a>
</p>

<p>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/Server-AGPL--3.0--only-blue" alt="Server: AGPL-3.0-only"></a>
  <a href="./LICENSING.md"><img src="https://img.shields.io/badge/SDK%20%26%20Rust%20core-Apache--2.0-green" alt="SDKs and Rust core: Apache-2.0"></a>
</p>

<p>
  <a href="#features">Features</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#examples">Examples</a> ·
  <a href="#deployment">Deployment</a> ·
  <a href="#development">Development</a> ·
  <a href="#documentation">Documentation</a>
</p>

</div>

---

## About

EasyAgent is an AI agent framework with a visual workspace. Connect models, search, files, and external APIs to build assistants that carry out tasks such as organizing information, generating content, and planning schedules.

The project is in the **0.1 public preview stage**, ready for exploration and personal workflow development. See the [preview notes](docs/PREVIEW_RELEASE.en.md) for verified behavior and known limits.

Describe what you need to generate a workflow, or connect nodes on a canvas. For more control, edit the workflow configuration, write tools, or use an SDK to build an application. Each approach uses the same workflow format, so you can keep editing, running, and sharing what you build.

Inspect the inputs, results, and errors for every step. Runs can pause for missing information or approval. With goal checking enabled, an assistant can attempt repairs or add steps within the permissions and budget you set.

<a id="features"></a>

## Features

### Choose how you build

- **No code**: describe a task and review the complete generated workflow, or add nodes and connect their inputs and outputs on a canvas.
- **Low code**: edit JSON workflows and node settings, configure HTTP APIs through forms, or import OpenAPI definitions.
- **Code**: call the service through Python, JavaScript / TypeScript, and Rust clients; embed the framework in Python to build tools, extensions, and applications.

### Run tasks and follow through

- **Chat workspace**: send a request with images, audio, video, or documents. Match a saved workflow or create and save a new one, then follow its progress, handle approvals, and download results in the conversation.
- **Workflow orchestration**: conditional branches, parallel steps, loops, subworkflows, and multi-agent collaboration.
- **Run history**: inspect progress, files, and errors; handle approvals and missing input; cancel runs or resume from saved progress.
- **Repair and improvement**: check whether a goal was met, create workflow revisions or code nodes, and evaluate proposed improvements under configured permissions, budgets, and approval rules.
- **Everyday use**: ongoing conversations, document retrieval, memory, schedules, notifications, and backups.

### Connect services and reuse capabilities

- **Models and media**: connect Chat Completions, Responses, Messages, and image, video, and audio APIs.
- **Search and APIs**: configure your own TinyFish search connection, add other HTTP services, and manage their credentials.
- **Extensions, MCP, and Skills**: install tools and reusable instructions; add commands, UI components, and event handlers.
- **Export and sharing**: package nodes, subworkflows, or complete workflows for another workspace. Recipients configure their own service credentials.

Model and parameter support depends on the upstream service. See the [model and media interface reference](docs/MODEL_API_COMPATIBILITY.md) for protocol coverage (in Chinese).

<a id="quick-start"></a>

## Choose an entry point

For **Python scripts or terminal usage**, start with [keyword research to image generation](docs/GETTING_STARTED.en.md); no server is needed. For the **browser canvas**, follow the Studio instructions below. The App is a separate package connected to the same backend runtime.

## Quick start

### 1. Start Studio

Install [Python 3.11+](https://www.python.org/downloads/) (3.12 recommended) and [uv](https://docs.astral.sh/uv/getting-started/installation/). Download the repository, then run these commands from its root directory:

```sh
uv sync --locked --extra app --python 3.12
uv run --extra app easyagent studio
```

Your browser will open [http://127.0.0.1:8765](http://127.0.0.1:8765). Keep the terminal running while you use Studio; press `Ctrl+C` to stop the service. Studio needs neither Node.js nor Rust, and has no separate frontend build step.

### 2. Run your first workflow

Keep the service running and open another terminal in the same project directory:

```sh
uv run easyagent run examples/first-workflow.json --url http://127.0.0.1:8765
```

This example needs no API key. It passes a message through an echo node and saves the output as `hello.json`. When the status is `succeeded`, open Tasks in Studio and expand the run result to download the file.

### 3. Create your own assistant

1. Open Settings → Models & Services. Enter your model endpoint, model name, and API key, then save and test the connection.
2. Open the chat workspace (`对话办事`), attach your material, and describe a task, such as:

   > Turn this school notice into a checklist with dates, owners, and things to prepare. Mark missing information as unconfirmed and save the checklist as a file.

3. Keep Automatic (`自动安排`) selected to reuse a saved workflow or generate and save a new one. Follow the live execution graph and supply missing information or approvals in the conversation. You can also choose Create assistant to plan before running.

For web search, add your own search API key on the same settings page. Model and search usage is billed by the service you connect. See [troubleshooting](docs/USER_GUIDE.en.md#troubleshooting) for installation and connection help. The user guide includes the Chinese labels used in the current interface.

### Recommended APIs to connect

**Local terminal and file handling are built in; no extra API key is needed.** Desktop and loopback Studio create a workspace and enable the terminal on first launch. Agents can run Python scripts or shell commands, import/export durable artifacts, and download public images with actual decoding and dimension checks. Commands still require workflow approval. Saved opt-out settings are preserved; API servers remain opt-in.

Start with these three capabilities to run the complete “keyword search → research and prompt writing → image generation” example. They can come from different services or a single service that supports the required interfaces.

- **Large language model API**: interprets requests, plans workflows, organizes research, and writes prompts. Prefer a model with tool calling and structured JSON output; check for image input support if it needs to understand reference images. In Settings → Models & Services, enter the endpoint, model name, and API key, then select Chat Completions, Responses, or Messages to match the service's actual protocol.
- **Web search API**: retrieves page links and snippets for keywords, providing sources for subsequent generation. The project includes a **TinyFish** search adapter; enter your own API key in Web Search (`联网搜索`) on the same settings page. The adapter does not include service credits. Other search services can be connected through custom HTTP APIs.
- **Image generation / editing API**: creates images from text or edits uploaded originals. For connected image models with a known editing protocol, automatic construction binds the editing node and passes the original artifact without asking you to write schemas. Other interfaces can be discovered from service documentation or configured through the [image tutorial](docs/GETTING_STARTED.en.md). Supported models, dimensions and formats depend on the service.

Add other capabilities as needed: **video generation APIs** for animating images, usually with a task-status endpoint; **speech recognition / synthesis APIs** for voice input and reading aloud; and **embedding APIs** for semantic knowledge retrieval. Configure only what your task needs. Each capability must be supported by the chosen service; connecting a text model does not automatically provide image generation, search, or speech.

Have the API documentation, endpoint, model name where applicable, and API key ready. See [connecting models and search](docs/USER_GUIDE.en.md#connect-models-and-search) for Studio setup and the [developer guide](docs/DEVELOPER_GUIDE.en.md#integrate-models-search-and-custom-apis) for script and CLI configuration.

<a id="examples"></a>

## Examples

### Life Assistant

The first example app helps track moving, travel, school notices, and home repairs. Start from a template, assign owners, set deadlines, and record progress and open questions.

Start it in another terminal:

```sh
uv run --extra app easyagent life --port 8770 --database .eah/life.db
```

Open [http://127.0.0.1:8770/life](http://127.0.0.1:8770/life). This local, single-user app is a starting point for building your own application. [Walkthrough](docs/USER_GUIDE.en.md#life-assistant) · [Source code](examples/life_assistant/)

### More examples

- **Call a workflow**: [Python](examples/getting_started/python_client.py), [JavaScript](examples/getting_started/javascript_client.mjs), and [Rust](sdk/rust/examples/demo.rs).
- **Build a tool**: [embedded Python tool](examples/getting_started/embedded_tool.py), [extensions](examples/extensions/), and [a Skill](examples/skills/careful-planner/).
- **Generate images and video**: [Python / JavaScript / Rust examples](examples/getting_started/media/README.en.md) reuse image and video subworkflows in one call and return named results. For protocol debugging, see the [multimedia workflow](examples/demos/multimedia_workflow.py) and [acceptance record](docs/MEDIA_ACCEPTANCE.md) (in Chinese).
- **Share a workflow**: an [importable example package](examples/workflows/shared-classification.eah-workflow.json) and the [sharing guide](docs/USER_GUIDE.en.md#reuse-export-and-share).

<a id="deployment"></a>

## Deployment and platforms

**Desktop App packages**: the [Desktop packages](.github/workflows/desktop.yml) GitHub Actions workflow produces ZIPs and SHA-256 files for macOS (Apple Silicon / Intel) and 64-bit Windows. Download them from the run’s **Artifacts**, or start **Run workflow** manually. Packages bundle Python and the backend and open Studio in your system browser. See [desktop packaging](platforms/desktop/README.md) for startup instructions and signing status.

By default, the service listens on localhost and stores workspace data in SQLite at `.eah/hub.db`. Restarting with the same database restores saved assistants, connections, and run records.

To choose a port, use a separate workspace, or skip opening the browser:

```sh
uv run --extra app easyagent studio --port 8780 --database .eah/my-project.db --no-browser
```

Then open [http://127.0.0.1:8780](http://127.0.0.1:8780). Point clients to the same port; CLI submissions accept a `--url` option. See the [developer guide](docs/DEVELOPER_GUIDE.en.md#integrate-models-search-and-custom-apis) for configuration files and [operations guidance](docs/DEVELOPER_GUIDE.en.md#permissions-credentials-and-operations) for authentication, backups, and recovery.

**Version 0.1.0 is a development preview** for local, single-user use. The interface is mostly in Chinese. Development and verification have mainly used macOS; Windows and Linux build configurations are included. Android and iOS native hosts and the embedded execution subset are experimental. The full execution service runs on a computer or server; see [packaging and platforms](docs/DEVELOPER_GUIDE.en.md#packaging-and-platforms).

<a id="development"></a>

## Development and extensions

The local Python SDK, HTTP service and browser App install and run separately. JavaScript/Rust use independent HTTP clients; Rust core provides an embeddable execution subset.

### Call a workflow from code

Save this as `hello.py` and run `uv run python hello.py`. No server is required:

```python
from easyagent import Agent, Sequential, tool

@tool
def clean(text: str) -> str:
    return text.strip()

flow = Sequential(clean, Agent("mock"))
print(flow("  Hello, EasyAgent!  "))
flow.export("flow.json")
```

`mock` is an offline echo fixture. Replace it with your model name and set `OPENAI_API_KEY` plus optional `OPENAI_BASE_URL` for real models. See the [SDK and CLI guide](docs/SDK_GUIDE.en.md) for `Module.forward`, async calls, export, approvals and recovery.

Run backend and frontend in separate terminals:

```sh
uv run --extra server easyagent serve --port 8765
uv run --package easyagent-app easyagent-app --backend http://127.0.0.1:8765 --port 8766
```

`serve` only provides APIs. The independent frontend does not import the execution engine. Studio and desktop builds explicitly combine the packages.

### Add your own capabilities

- **Connect an API**: start with [models, search, and custom APIs](docs/DEVELOPER_GUIDE.en.md#integrate-models-search-and-custom-apis) to define inputs, outputs, and credentials.
- **Write an extension**: add tools, commands, event hooks, or UI components with the [extension guide](docs/DEVELOPER_GUIDE.en.md#build-complete-extensions).
- **Reuse instructions**: package instructions and resources as [Skills](docs/DEVELOPER_GUIDE.en.md#develop-skills-and-connect-mcp), or connect an existing MCP service.
- **Build an application**: use the [developer guide](docs/DEVELOPER_GUIDE.en.md) and [API contracts](docs/contracts/) for runs, approvals, events, and artifacts.

### Contributing

Bug fixes, examples, service adapters, and documentation improvements are welcome. For bug reports, include your environment, reproduction steps, expected results, and error details with API keys removed.

The full integration test environment also needs Node.js 20+ and Rust/Cargo:

```sh
uv sync --locked --extra app --extra dev --python 3.12
uv run --extra app playwright install chromium
uv run --extra app --extra dev pytest -q tests/integration
```

Implementation code lives in `src/easyagent/`, clients in `sdk/`, and application and tool examples in `examples/`. See the [code map](docs/DEVELOPER_GUIDE.en.md#source-map-and-architecture) for more detail.

<a id="documentation"></a>

## Documentation

- [User guide](docs/USER_GUIDE.en.md): connect services, run assistants, inspect results, and handle failures.
- [Developer guide](docs/DEVELOPER_GUIDE.en.md): SDKs, workflows, extensions, MCP, Skills, and platform integration.
- [API reference](http://127.0.0.1:8765/docs): inspect and try endpoints after starting the local service.
- [All documentation](docs/README.en.md): topic guides, development plans, and test records.

## License

EasyAgent assigns licenses by component:

- **Server, Studio, and Life Assistant**: [AGPL-3.0-only](LICENSE). Commercial use is allowed; modified versions offered over a network must provide corresponding source to users as required by the license.
- **Standalone SDKs, Rust core, public API definitions, and designated introductory examples**: [Apache-2.0](LICENSES/Apache-2.0.txt), for integration into your own applications, including proprietary products.

See [license scopes](LICENSING.md) for the complete directory mapping and guidance on plugins and generated content. For only the Python client or extension SDK, install [sdk/python](sdk/python/README.md) separately.
