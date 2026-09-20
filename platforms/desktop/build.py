"""Run on each target OS: uv run --extra desktop python platforms/desktop/build.py."""

import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
os.chdir(root)
command = [
    sys.executable,
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name",
    "EasyAgent",
    "--distpath",
    ".eah/build/desktop",
    "--workpath",
    ".eah/build/pyinstaller",
    "--specpath",
    ".eah/build",
    "--add-data",
    str(root / "LICENSE") + os.pathsep + "licenses",
    "--add-data",
    str(root / "LICENSES/Apache-2.0.txt") + os.pathsep + "licenses",
    "--add-data",
    str(root / "LICENSING.md") + os.pathsep + "licenses",
    "--add-data",
    str(root / "NOTICE") + os.pathsep + "licenses",
    "--collect-all",
    "easyagent",
    "--collect-all",
    "easyagent_client",
    "--collect-all",
    "easyagent_app",
    "--collect-all",
    "playwright",
    "--collect-all",
    "examples",
    "--collect-all",
    "quickjs",
    "--collect-all",
    "wasmtime",
    "--collect-all",
    "rfc3987_syntax",
    "--collect-all",
    "lark",
    "--collect-submodules",
    "mcp.client",
    "--collect-submodules",
    "mcp.server",
    "--collect-submodules",
    "mcp.shared",
    "--collect-submodules",
    "cryptography",
    "--hidden-import",
    "uvicorn.logging",
    "--hidden-import",
    "uvicorn.loops.auto",
    "--hidden-import",
    "uvicorn.protocols.http.auto",
    "--hidden-import",
    "uvicorn.lifespan.on",
    "platforms/desktop/launcher.py",
]
if sys.platform == "darwin":
    command.insert(3, "--windowed")
subprocess.run(command, check=True)
print(root / ".eah/build/desktop")
