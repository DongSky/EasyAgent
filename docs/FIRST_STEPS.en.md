# Run and understand your first workflow

[简体中文](FIRST_STEPS.zh-CN.md) · [Source reading route](CODE_MAP.en.md) · [SDK reference](SDK_GUIDE.en.md)

This example connects two ordinary Python functions: trim whitespace, then count words. It needs no model, API key, browser, or running server.

## 1. Run it

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from the repository root. Installing dependencies initially requires network access; the example itself runs offline.

```sh
uv sync --locked
uv run python examples/getting_started/first_steps.py
```

Expected output:

```text
{'text': 'Hello EasyAgent', 'words': 2}
```

Open the [complete source](../examples/getting_started/first_steps.py) and start at `main()`:

1. `Sequential(clean, count_words)` connects the first node's output to the second node's input.
2. `Runtime(...)` manages the runtime and local database; leaving `with` closes it.
3. `runtime.run(...)` executes the workflow; `result.value` holds its final value.
4. `workflow.export(...)` saves the graph for inspection without rerunning the functions.

`@node` declares an ordinary Python function as a node. Its body can contain normal conditions, loops, and helper calculations.

## 2. Change it and check the result

Replace `"  Hello EasyAgent  "` with `"  Learn workflows step by step  "` and run again. The result should contain `words: 5`.

History lives in `.eah/first-steps.db`; the exported graph is `.eah/first-steps.workflow.json`. The graph references locally registered functions without embedding their source. Sharing executable code with another machine requires an extension package, not just this JSON file.

## 3. Know the boundaries

- A **node** is one operation with inputs and outputs. Computation inside the function executes and retries together.
- A **workflow** connects nodes and their dependencies. Here, trimming precedes counting.
- An **agent** is a node for model judgment, tool selection, or iteration. This deterministic example needs no agent.
- **Runtime / Hub**: `Runtime` is the scripting interface; it uses `Hub` to execute and persist work. They share one execution engine.

Next, read [research to image generation](GETTING_STARTED.en.md) for multiple inputs, parallel branches, and model services; the [source route](CODE_MAP.en.md) to change the framework; or the [SDK reference](SDK_GUIDE.en.md) for parameters.
