# EasyAgent documentation

[简体中文](README.md) | **English**

## Start here

- **First code example**: [offline workflow](FIRST_STEPS.en.md), without API keys.
- **Changing the framework**: [source reading route](CODE_MAP.en.md), from a message to tool results.

- **Learn from a real task**: [keyword research to image generation](GETTING_STARTED.en.md), covering multiple inputs, branches, joins and terminal usage.
- **Look up SDK usage**: [local SDK and CLI](SDK_GUIDE.en.md).

- [Project overview](../README.en.md): learn what EasyAgent does and run your first workflow.
- [User guide](USER_GUIDE.en.md): connect models, create assistants, inspect results, and handle failures.
- [Developer guide](DEVELOPER_GUIDE.en.md): build tools, extensions, and applications, or connect your own services.
- Code examples: [Python](../examples/getting_started/python_client.py), [JavaScript](../examples/getting_started/javascript_client.mjs), [Rust](../sdk/rust/examples/demo.rs), and [a custom tool](../examples/getting_started/embedded_tool.py).
- [Images and video from three languages](../examples/getting_started/media/README.en.md): install reusable workflows, upload a reference image, and download both results from one call.

## Topic references

The user and developer guides above are available in English. The detailed topic references below are currently in Chinese; code, schemas, and configuration files retain their original identifiers.

### Building assistants and applications

- [Ways to build](DEVELOPMENT_MODES.md)
- [Studio interface](UI_REDESIGN.md)
- [Chat workspace and attachments](CONVERSATION_WORKSPACE.md)
- [Life Assistant](LIFE_ASSISTANT.md)
- [Node library](NODE_LIBRARY.md)
- [Component reuse and code execution](REUSE_AND_CODE_EXECUTION.md)
- [Importing and exporting node packages](COMPONENT_PACKAGES.md)
- [Sharing complete workflows](WORKFLOW_SHARING.md)

### Interfaces and extensions

- [Architecture](ARCHITECTURE.md) and [API contracts](INTERFACES.md)
- [Adding tools and models](EXTENDING.md)
- [Extensions](EXTENSIONS.md) and [service backends](BACKENDS.md)
- [Skills](SKILLS.md)
- [Adding nodes and workflows during execution](RUNTIME_DEVELOPMENT.md)
- [Automatic checks and repair](GOALS.md)
- [Search and custom APIs](SEARCH_AND_APIS.md)
- [Model and media APIs](MODEL_API_COMPATIBILITY.md)
- [Conversations, notifications, and service connections](CONVERSATIONS_AND_CONNECTIONS.md)
- Interface definitions: [JSON Schema](contracts/) and [TypeScript types](../sdk/javascript/contracts.d.ts).

### Deployment and maintenance

- [Permissions, backups, and recovery](OPERATIONS.md)
- [Android and iOS](MOBILE_RUNTIME_STRATEGY.md)
- [Integration test configuration](../.github/workflows/integration.yml)
- [Application build configuration](../.github/workflows/releases.yml)
- [License scopes and commercial use](../LICENSING.md)

## Plans and test records

The [latest development and acceptance record](NEXT_DELIVERY.md) tracks current progress. The files below preserve earlier designs and test results. Feature status in older documents describes the project at the date of that record.

- Development plans: [original plan](PLAN.md), [extension roadmap](EXTENSION_ROADMAP.md), and [feature review](COMPLETENESS.md).
- Design references: [open-source projects](OPEN_SOURCE_REVIEW.md) and [models and research approaches](MODELS_AND_RESEARCH.md).
- Project comparisons: [early Pi/Hermes comparison](PI_HERMES_GAP_BASELINE.md) and [later gap review](PI_HERMES_GAP_ANALYSIS.md).
- Integration tests: [historical records](VALIDATION.md), [construction methods](CONSTRUCTION_ACCEPTANCE.md), and [task scenarios](TEST_SCENARIOS.md).
- Focused checks: [extensions](EXTENSION_ACCEPTANCE.md), [live workflows](LIVE_WORKFLOW_ACCEPTANCE.md), and [images and video](MEDIA_ACCEPTANCE.md).

## Maintaining the docs

Update the relevant Chinese and English guides when changing a feature. Document the prerequisites and working directory for example commands. Validate examples with a separate database, and keep keys and personal file paths out of the docs. Preserve dates on historical test records and add new results separately.

- [Python 本地 SDK 与 CLI](SDK_GUIDE.zh-CN.md) · [Local SDK and CLI](SDK_GUIDE.en.md)
- [SDK / App 包边界](SDK_AND_APP.md)
