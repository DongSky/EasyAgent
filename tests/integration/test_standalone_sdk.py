import asyncio
import sys
import textwrap

from easyagent.client import HubClient as CompatibleClient
from easyagent.extension_sdk import Extension as CompatibleExtension
from easyagent.extensions import build_package
from easyagent_client import Extension, HubClient


BLOCK_SERVER_IMPORTS = textwrap.dedent('''
    import importlib.abc
    import sys

    class BlockServer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] == "easyagent":
                raise AssertionError("Standalone SDK imported server code: " + fullname)
            return None

    sys.meta_path.insert(0, BlockServer())
''')


async def test_standalone_http_sdk_calls_server_without_importing_it(api, tmp_path):
    url, _ = api
    assert CompatibleClient is HubClient
    script = BLOCK_SERVER_IMPORTS + textwrap.dedent('''
        import asyncio
        from easyagent_client import HubClient

        async def main():
            async with HubClient(sys.argv[1]) as client:
                run = await client.submit({
                    "name": "independent client",
                    "steps": [{"id": "echo", "target": "core.echo", "input": {"standalone": True}}],
                })
                result = await client.wait(run["id"])
                assert result["status"] == "succeeded"
                assert result["steps"][0]["output"] == {"standalone": True}
                assert await client.events(run["id"])
            assert not any(name.split(".")[0] == "easyagent" for name in sys.modules)
        asyncio.run(main())
        print("standalone HTTP client passed")
    ''')
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", script, url, cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, error.decode()
        assert b"standalone HTTP client passed" in output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_standalone_extension_sdk_runs_in_native_plugin_process(hub):
    assert CompatibleExtension is Extension
    source = BLOCK_SERVER_IMPORTS + textwrap.dedent('''
        from easyagent_client.extension_sdk import Extension
        extension = Extension()

        @extension.handler("echo")
        def echo(arguments, context):
            assert not any(name.split(".")[0] == "easyagent" for name in sys.modules)
            return {"value": arguments["value"], "extension": context["extension"]}

        extension.run(persistent=True)
    ''')
    package = build_package({
        "id": "apache_sdk", "title": "Independent Python SDK", "revision": 1,
        "runtime": "python", "entrypoint": "extension.py", "transport": "ndjson",
        "permissions": ["trusted_process"],
        "tools": [{"handler": "echo", "spec": {"name": "apache_sdk.echo"}}],
    }, {"extension.py": source})
    await hub.extensions.install({
        "package": package, "grants": ["trusted_process"], "trust_digest": package.digest,
    })
    result = await hub.wait(hub.submit({
        "name": "standalone extension",
        "steps": [
            {"id": "first", "target": "apache_sdk.echo", "input": {"value": 7}},
            {"id": "second", "target": "apache_sdk.echo", "depends_on": ["first"],
             "input": {"value": {"$ref": "first.value"}}},
        ],
    }))
    assert result["status"] == "succeeded", result
    assert result["steps"][1]["output"] == {"value": 7, "extension": "apache_sdk"}
