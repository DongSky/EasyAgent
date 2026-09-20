# Generate an expression image and animation from code

[简体中文](README.md) | **English**

Upload a reference, generate an expression image, and animate it. Python, JavaScript, and Rust call the same saved workflow and download `expression.png` and `animation.mp4`. These examples and all three SDKs use Apache-2.0.

The default models are `gpt-image-2.5-flare` and `doubao-seedance-2-5-260628`. Configure service endpoints, model credentials, and media protocols on the Hub server. Clients need only the Hub URL and optional Hub access token.

## Build a workflow from keywords

For workflow authoring, start with the [research-to-image tutorial](../../../docs/GETTING_STARTED.en.md) and [executable source](research_image.py). Keywords, reference image, style and size feed parallel image-generation and source-note branches, then join. It runs with the local SDK/CLI without Studio. The following sections cover calling saved media workflows remotely from three languages.

## 1. Set up once

Start the current Studio version and connect your service in the model API catalog. Install using that saved connection:

```sh
export EAH_URL=http://127.0.0.1:8766
uv run python examples/getting_started/media/setup.py
```

This saves three workflows without generating media: `media.image`, `media.video`, and `media.expression_video`. The combined workflow pins its two child revisions. All remain editable, reusable, and shareable on the canvas.

Without a saved catalog connection, pass `--base-url https://your-provider.example/v1 --api-key-env OPENAI_API_KEY`. The **Hub server** must resolve that secret reference from its environment or credential connection; do not embed model keys in the clients.

In Windows PowerShell, set the URL with `$env:EAH_URL="http://127.0.0.1:8766"`. Set `EAH_TOKEN` if Hub authentication is enabled. This is the Hub token, not the model API key.

## 2. Run any language

From the repository root, replace the reference path:

```sh
export EAH_DEMO_KEY=my-first-animation
uv run python examples/getting_started/media/python_demo.py path/to/reference.png
node examples/getting_started/media/javascript_demo.mjs path/to/reference.png
cargo run --manifest-path sdk/rust/Cargo.toml --example media -- path/to/reference.png
```

With the same `EAH_DEMO_KEY` and reference file, all three commands get the same run instead of generating three videos. Outputs go to `output/media-sdk/python`, `javascript`, or `rust`; an optional second argument selects another output directory. Use Node.js 20+, Cargo for Rust, and the Python SDK installed by `uv sync` or separately from `./sdk/python`.

The first run may return `waiting_approval`. Open Tasks (`任务`) in Studio, inspect and approve the relevant call, then rerun with the same key and inputs. Completed steps are reused. The examples do not automatically approve external writes.

**Keep the key when continuing the same operation. Use a new key when changing the reference, prompts, or requesting another generation.** Reusing a key with different inputs returns a conflict. All examples default to `media-demo`. A local timeout does not cancel remote generation; do not change keys just to resume.

## 3. Customize the request

All languages follow upload → select workflow → submit business inputs → wait → download. For Python, inside an async function:

```python
async with HubClient.from_env() as client:
    reference = await client.upload_file("reference.png")
    task = await client.workflow("media.expression_video").start(
        {"reference_image": reference["id"], "image_prompt": "画一个开心的 Q 版表情",
         "video_prompt": "角色轻轻挥手，固定镜头", "duration": 4},
        key="happy-wave-01",
    )
    result = await task.result(timeout=1900)
    await result.download("image", "expression.png")
    await result.download("video", "animation.mp4")
```

Call `media.image` and `media.video` independently with `reference_image` and `prompt`; video also defaults to `duration=4`. To add a review or processing stage between them, edit the combined workflow without copying orchestration into three clients.

Pin revisions with Python `workflow(..., revision=1)`, JavaScript `workflow(..., {revision:1})`, or Rust `workflow(...).revision(1)`. Without an explicit revision, the initial submission freezes the current version; retries with the same key retain it.

## Protocol differences and customization

The default uploads an image to obtain an HTTPS reference. Use `--reference-mode inline` for compatible services. For a `data.task_id` receipt, use `--task-id-path result.data.task_id`. Upload operation, file field, reference URL path, video output path, and model names are configurable; see `setup.py --help`. Use `--replace` explicitly to publish new revisions of existing examples.

Declare named results in `metadata.outputs`, such as an `image` reference to an actual artifact. Parents consume child `outputs`; original step outputs remain available in `results`. A model name does not guarantee identical provider parameters. Adapt the node/workflow and verify a new protocol before reuse.

The SDKs retain `request`, `submit`, `run`, `events`, `approve`, `respond`, library, and sharing APIs. `workflow(...).definition()` returns the full definition; `run_handle(id)` / `runHandle(id)` restores a known run. Direct embedding still uses the framework; HTTP SDKs do not require the server package.

Uploads and downloads are limited to 50 MB. Explicit video downloads use an anonymous request that never forwards the Hub token. HTTPS and loopback HTTP are supported; redirects are not followed. Expired result URLs need a fresh accessible URL from the service; the examples do not generate another video. Failed downloads preserve existing destination files.

## What became simpler

The original media acceptance script handled discovery, node creation, receipt variants, stage IDs, polling, and downloads. Node and workflow authors should configure those details once. Application code should use business inputs and named results.

This is a general workflow invocation layer, also usable for documents, search, and notifications. Full definitions, schemas, revisions, and low-level APIs preserve customization; reusing saved composite workflows simplifies calls. Rust still requires explicit types and error propagation, and connecting a new media protocol still requires node configuration.

The next useful improvement is generating workflow-specific types and calling code from Studio input/output schemas, together with template dependency checks. That would reduce spelling mistakes and first-time setup while retaining the public contracts. This iteration does not claim to implement that generator.

Integration tests launch all three language processes against local HTTP media protocol fixtures. They cover the full pipeline, both reference modes, two task-ID shapes, shared idempotency, approval/resume, and downloads. Fixtures do not demonstrate model output quality; see the existing [live media acceptance record](../../../docs/MEDIA_ACCEPTANCE.md) for that evidence.
