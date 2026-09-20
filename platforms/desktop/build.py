"""Run on each target OS: uv run --extra desktop python platforms/desktop/build.py."""

import os
from pathlib import Path
import subprocess
import sys
import tomllib

root = Path(__file__).resolve().parents[2]
os.chdir(root)
version = tomllib.loads((root / 'pyproject.toml').read_text(encoding="utf-8"))['project']['version']
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
    command[3:3] = ["--windowed", "--osx-bundle-identifier", "ai.easyagent.desktop"]
logs = root / '.eah/build/logs'
logs.mkdir(parents=True, exist_ok=True)
with (logs / 'pyinstaller.log').open('w', encoding='utf-8') as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
if result.returncode:
    print((logs / 'pyinstaller.log').read_text(encoding='utf-8')[-16000:])
    result.check_returncode()
if sys.platform == 'darwin':
    import plistlib

    info = root / '.eah/build/desktop/EasyAgent.app/Contents/Info.plist'
    with info.open('rb') as stream:
        metadata = plistlib.load(stream)
    metadata.update(CFBundleShortVersionString=version, CFBundleVersion=version)
    with info.open('wb') as stream:
        plistlib.dump(metadata, stream)
    # Modifying Info.plist invalidates PyInstaller's ad-hoc signature; re-sign locally.
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(info.parents[1])], check=True)
print(root / ".eah/build/desktop")
