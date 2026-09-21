"""Integration acceptance for separately installed wheels, outside the checkout."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    wheels = {name: next(args.dist.resolve().glob(name + "-*.whl")) for name in
              ("easyagent", "easyagent_app", "easyagent_client")}
    with zipfile.ZipFile(wheels["easyagent"]) as archive:
        assert not any(name.endswith((".html", ".css")) for name in archive.namelist())
    env = {k: v for k, v in os.environ.items() if k in
           ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "SSL_CERT_FILE", "SSL_CERT_DIR")}
    with tempfile.TemporaryDirectory(prefix="easyagent-packages-") as temp:
        root = Path(temp)
        for name, packages, script in [
            ("local", [wheels["easyagent"], wheels["easyagent_client"]], '''
                import importlib.util
                for name in ('fastapi', 'uvicorn', 'starlette', 'easyagent_app', 'mcp', 'playwright', 'quickjs', 'wasmtime'):
                    assert importlib.util.find_spec(name) is None, name
                from easyagent import Agent, Runtime, Sequential, Subflow, node
                from easyagent.models import HTTPProvider, ModelRegistry, MockProvider
                from easyagent.model_streaming import MODEL_OBSERVER as legacy_observer
                from easyagent.models.streaming import MODEL_OBSERVER
                from easyagent.workspace_chat import WorkspaceChat, Phase
                from easyagent.extensions import ExtensionHost, build_package, validate_package
                assert legacy_observer is MODEL_OBSERVER
                @node
                def clean(text: str) -> str: return text.strip()
                flow = Sequential(clean, Agent('mock'))
                with Runtime('isolated.db') as runtime:
                    assert flow(' isolated ') == 'isolated'
                    assert Subflow(flow)(' nested ') == 'nested'
                flow.export('flow.json')
                print('headless SDK verified')
            '''),
            ("app", [wheels["easyagent_app"]], '''
                import importlib.util
                assert importlib.util.find_spec('easyagent') is None
                assert importlib.util.find_spec('easyagent_client') is None
                from easyagent_app import create_app, ASSETS
                assert (ASSETS/'studio.html').is_file()
                for module in ('requests', 'state', 'views', 'graph', 'activity'):
                    assert (ASSETS/'chat'/f'{module}.js').is_file(), module
                assert len(create_app().routes) >= 3
                print('independent frontend verified')
            '''),
            ("client", [wheels["easyagent_client"]], '''
                import importlib.util
                assert importlib.util.find_spec('easyagent') is None
                assert importlib.util.find_spec('easyagent_app') is None
                from easyagent_client import Client, HubClient
                with Client() as client:
                    assert client.workflow('example').identifier == 'example'
                print('independent HTTP SDK verified')
            '''),
        ]:
            directory = root / name
            subprocess.run(["uv", "venv", "--python", sys.executable, str(directory)], env=env, check=True)
            python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run(["uv", "pip", "install", "--python", str(python), *map(str, packages)], env=env, check=True)
            source = root / (name + ".py")
            source.write_text(textwrap.dedent(script), encoding="utf-8")
            subprocess.run([str(python), "-I", str(source)], cwd=root, env=env, check=True)
            if name == "local":
                subprocess.run([str(python), "-I", "-m", "easyagent.cli", "run",
                                str(Path(__file__).resolve().parents[1] / "examples/first-workflow.json"),
                                "--database", str(root / "cli.db")], cwd=root, env=env, check=True)


if __name__ == "__main__":
    main()
