# 0.1 public preview notes

[简体中文](PREVIEW_RELEASE.md) | **English**

This version is ready for a first public source preview for personal use, workflow building, and feedback. It remains `0.1.0 preview`; this is not a stable production release or a claim that packages have been published to PyPI or another platform.

## Start here

- [Keyword search → prompt writing → image and source notes → joined delivery](GETTING_STARTED.en.md): a practical workflow with multiple inputs and parallel branches.
- The Python SDK and CLI execute locally. Install the HTTP server, independent Agent App, and remote clients as needed.
- A node is one operation; a workflow stores a step graph. Explicit `Subflow` creates a child run. Approval, retries, artifacts, and recovery remain recorded.

`examples/first-workflow.json` remains a credential-free installation check. Redundant cleaning/greeting toy scripts were removed; the development tutorial now uses the search-to-image case. Regression fixtures remain in test and acceptance code and do not populate normal workspaces automatically.

## Verification and limits

On September 20, 2026, the full local integration suite passed **165 tests**, covering multiple inputs, branch joins, node/subworkflow boundaries, and pinned execution after archival and restart. The Python SDK, App, and HTTP client were built and installed separately. Cross-platform CI is configured; this local record does not stand in for results on each platform.

This round verified external protocols with local HTTP fixtures without new paid model calls. Previous live image/video results are documented in [media acceptance](MEDIA_ACCEPTANCE.md). Desktop installers were not rebuilt in this round; use the source installation path first.

`Module.forward` supports multiple inputs and parallel dependencies. Native Python `if` on symbolic values is not implemented; conditional routing uses `Step.when`, and automatic joins of mutually exclusive branches need further design. Distributed high availability, strong multitenant isolation, and a complete mobile runtime are outside this preview's commitments.

## Keep workspaces clean

Use separate databases for experiments and acceptance runs. Existing test definitions can be [archived](NODE_LIBRARY.md#清理本地实验条目), removing them from discovery and automatic planning while retaining history and pinned references. The repository ignores `.eah/`, databases, environment secrets, and build caches. Local backups and cleanup manifests stay outside distribution packages.
