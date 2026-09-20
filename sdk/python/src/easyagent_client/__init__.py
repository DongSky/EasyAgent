# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0

"""Standalone HTTP and extension SDK; does not import the EasyAgent server."""

from .client import HubClient
from .extension_sdk import Extension, ExtensionResponse
from .workflows import RunHandle, RunResult, RunStopped, WorkflowHandle
from .sync import Client

__all__ = ["Client", "HubClient", "Extension", "ExtensionResponse", "RunHandle", "RunResult", "RunStopped", "WorkflowHandle"]
__version__ = "0.1.0"
