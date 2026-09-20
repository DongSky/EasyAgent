# EasyAgent licensing

[简体中文](docs/LICENSING.zh-CN.md) | **English**

Copyright 2026 EasyAgent contributors. Each component is licensed according to the scope below. Existing third-party notices and licenses continue to apply to their respective material.

## Default: AGPL-3.0-only

Except for the Apache-2.0 components listed below, original EasyAgent source code and documentation are licensed under the **GNU Affero General Public License, version 3 only**, in [LICENSE](LICENSE).

This includes the Python local SDK/runtime and optional HTTP server in `src/easyagent/`, the independently packaged frontend in `apps/agent/`, the Life Assistant backend in `examples/life_assistant/`, and the desktop and mobile application hosts in `platforms/`.

The license permits commercial use and charging for services. Distribution of covered software carries source and notice obligations. If you modify the program and let users interact with it remotely over a network, section 13 requires a prominent offer of the corresponding source to those users. The full license text governs the details.

## Apache-2.0 components

The following original EasyAgent components are licensed under [Apache License 2.0](LICENSES/Apache-2.0.txt):

- `sdk/`: standalone Python, JavaScript / TypeScript, and Rust clients, extension SDKs, and their examples.
- `core/`: the independently embeddable Rust execution core and its examples.
- `docs/contracts/`: published JSON Schema interface definitions.
- `examples/getting_started/`, `examples/plugins/`, `examples/extensions/`, and `examples/skills/`: introductory tools, extensions, and Skills.
- `examples/first-workflow.json`, identified by its `.license` sidecar.
- Code snippets in the root Chinese/English READMEs and the Chinese/English user and developer guides. Surrounding documentation follows the default license.

These components may be used in proprietary applications under Apache-2.0, including its license, notice, modification, and patent provisions. Each independently packaged SDK and the Rust core carries its own license and notice files. Package names do not change the scope of these grants.

## Integration boundaries

Install the standalone Python package from `sdk/python/` and import `easyagent_client` when you need only the HTTP client or extension SDK. It depends on `httpx` and does not import the AGPL server. The legacy `easyagent.client` and `easyagent.extension_sdk` imports are compatibility entry points inside the server package.

Independent applications communicating through the HTTP API are generally separate from the server. Whether an extension or an application embedding server code forms a covered combined work depends on how it uses and combines that code. The Apache license on an SDK or example does not remove the obligations of an AGPL dependency; for example, `embedded_tool.py` embeds the AGPL Python runtime.

Workflows, prompts, user-written plugins, user data, credentials, generated media, and other outputs do not become AGPL merely through using EasyAgent. Content that incorporates covered code must be assessed according to that content. This policy grants no additional rights to third-party material, service content, or files supplied by users. In particular, local `output/` and `.eah/` content is outside these source-code grants and is excluded from Python release archives.

## Package metadata and GitHub

The server distribution includes both AGPL server code and Apache example components, so its package metadata uses `AGPL-3.0-only AND Apache-2.0`. The standalone HTTP/extension SDKs and Rust core use `Apache-2.0`. The separate `easyagent-app` distribution uses `AGPL-3.0-only`; moving frontend files does not relicense them. Each component has the specified license; the `AND` expression describes the contents of the distribution rather than offering a choice of licenses.

The root `LICENSE` contains the unmodified standard AGPL text. GitHub can recognize this as the repository's primary license. Its summary does not describe all directory-level scopes, so the READMEs link to both licenses and this scope document. Actual detection is performed by GitHub after the files are pushed. See [GitHub's licensing documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

## Contributions and notices

Unless separately agreed with the maintainers, contributions are submitted under the license assigned to their component. Preserve applicable copyright, license, and notice files when copying or distributing components. Third-party dependencies keep their own terms; this document does not relicense them. Keep the license scope, package metadata, generated SDK headers, and both language versions of the documentation consistent when moving code between components.
