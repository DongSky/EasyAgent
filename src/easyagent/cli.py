from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path

from .client import HubClient


def main():
    parser = argparse.ArgumentParser(
        prog="easyagent", description="EasyAgent — create, run and inspect durable assistants"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "studio", "life"):
        server = sub.add_parser(name)
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", type=int, default=8765)
        server.add_argument("--database", default=".eah/hub.db")
        server.add_argument("--config")
        server.add_argument("--no-browser", action="store_true")
        server.add_argument("--with-life", action="store_true", help="enable Life Assistant example API")
    submit = sub.add_parser("run")
    submit.add_argument("workflow")
    submit.add_argument("--input", default="{}", help="JSON object, @file.json or - for stdin")
    submit.add_argument("--key", help="idempotency key; reuse to avoid duplicate work")
    submit.add_argument("--source", help="file.py:flow registering tools for an exported workflow")
    commands_with_runtime = [submit]
    for name in ("inspect", "events", "resume"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
        if name == "resume":
            command.add_argument("--source", help="original file.py:flow registering Python tools")
        commands_with_runtime.append(command)
    approve = sub.add_parser("approve")
    approve.add_argument("invocation_id")
    decision = approve.add_mutually_exclusive_group(required=True)
    decision.add_argument("--yes", action="store_true")
    decision.add_argument("--deny", action="store_true")
    commands_with_runtime.append(approve)
    for command in commands_with_runtime:
        command.add_argument("--url", help="HTTP backend; omit to run locally")
        command.add_argument("--database", default=os.environ.get("EAH_DATABASE", ".eah/local.db"))
        command.add_argument("--config")
        command.add_argument("--timeout", type=float, default=600)
    export = sub.add_parser("export")
    export.add_argument("source")
    export.add_argument("--output")
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--url", default="http://127.0.0.1:8765")
    extension = sub.add_parser("extension")
    commands = extension.add_subparsers(dest="extension_command", required=True)
    init = commands.add_parser("init")
    init.add_argument("directory")
    init.add_argument("--id", required=True)
    init.add_argument("--language", choices=["javascript", "python", "node", "rust"], default="javascript")
    pack = commands.add_parser("package")
    pack.add_argument("directory")
    pack.add_argument("--output", required=True)
    install = commands.add_parser("install")
    install.add_argument("file")
    install.add_argument("--url", default="http://127.0.0.1:8765")
    install.add_argument("--grant", action="append", default=[])
    install.add_argument("--trust-digest")
    skill = sub.add_parser("skill")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    for action in ("list", "install", "source", "package"):
        skill_parser = skill_commands.add_parser(action)
        skill_parser.add_argument("--url", default="http://127.0.0.1:8765")
        if action == "install":
            skill_parser.add_argument("source")
            skill_parser.add_argument("--expected-revision", type=int, default=0)
        elif action == "source":
            skill_parser.add_argument("repository")
            skill_parser.add_argument("--path", default="")
            skill_parser.add_argument("--ref", default="HEAD")
        elif action == "package":
            skill_parser.add_argument("directory")
            skill_parser.add_argument("--output", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--database", default=".eah/hub.db")
    for action in ("backup", "restore"):
        p = sub.add_parser(action)
        p.add_argument("file")
        p.add_argument("--database", default=".eah/hub.db")
    args = parser.parse_args()
    token = os.environ.get("EAH_TOKEN", "")
    if args.command in ("run", "inspect", "events", "resume", "approve", "export"):
        from .run_cli import command
        from easyagent_client.workflows import RunStopped
        from pydantic import ValidationError
        from jsonschema import ValidationError as SchemaError
        from contextlib import redirect_stdout
        try:
            if getattr(args, "timeout", 1) <= 0:
                raise ValueError("timeout must be positive")
            with redirect_stdout(sys.stderr):
                result = command(args, token)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except RunStopped as exc:
            print(json.dumps(exc.state, ensure_ascii=False, indent=2))
            raise SystemExit(3 if exc.status in ("waiting_approval", "waiting_input", "needs_attention") else 1)
        except TimeoutError as exc:
            print(json.dumps({"status": "timeout", "id": getattr(exc, "run_id", None)}))
            print(str(exc), file=sys.stderr)
            raise SystemExit(4)
        except Exception as exc:
            print("Invalid workflow or inputs" if isinstance(exc, (ValidationError, SchemaError)) else str(exc), file=sys.stderr)
            raise SystemExit(2)
        return
    if args.command in ("doctor", "backup", "restore"):
        from .runtime import Hub
        from .operations import diagnostics, backup, restore

        if args.command == "doctor":
            result = diagnostics(Hub(args.database))
        else:
            password = os.environ.get("EAH_BACKUP_PASSWORD", "")
            result = (
                backup(args.database, args.file, password)
                if args.command == "backup"
                else restore(args.file, args.database, password)
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "skill":
        from .skill_cli import command

        print(json.dumps(asyncio.run(command(args, token)), ensure_ascii=False, indent=2))
    elif args.command == "extension":
        from .extension_cli import scaffold, package_directory

        if args.extension_command == "init":
            print(json.dumps(scaffold(args.directory, args.id, args.language)))
        elif args.extension_command == "package":
            package = package_directory(args.directory)
            Path(args.output).write_text(package.model_dump_json(indent=2), encoding="utf-8")
            print(json.dumps({"output": args.output, "digest": package.digest}))
        else:

            async def install():
                async with HubClient(args.url, token) as client:
                    print(
                        json.dumps(
                            await client.install_extension(
                                json.loads(Path(args.file).read_text(encoding="utf-8")),
                                grants=args.grant,
                                trust_digest=args.trust_digest,
                            )
                        )
                    )

            asyncio.run(install())
    elif args.command == "mcp":
        from .mcp_bridge import make_mcp_server

        make_mcp_server(HubClient(args.url, token)).run(transport="stdio")
    else:
        try:
            import uvicorn
            from .api import create_app
        except ModuleNotFoundError:
            parser.error('HTTP service requires: pip install "easyagent[server]"')
        from .config import configure
        from .runtime import Hub
        if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
            parser.error("set EAH_TOKEN before binding to a non-loopback interface")

        async def serve():
            hub = Hub(args.database)
            await configure(hub, args.config)
            app = create_app(hub, token=token)
            from .studio import install_studio

            install_studio(app, hub)
            if args.command in ("studio", "life"):
                try:
                    from easyagent_app import mount_app
                except ModuleNotFoundError:
                    parser.error('Browser application requires: pip install "easyagent[app]"')
                mount_app(app)
            if args.command == "life" or args.with_life:
                from examples.life_assistant.app import install_life

                install_life(app, hub)
            if args.command in ("studio", "life") and not args.no_browser:
                asyncio.get_running_loop().call_later(
                    1,
                    webbrowser.open,
                    f"http://{args.host}:{args.port}/" + ("life" if args.command == "life" else ""),
                )
            await uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port)).serve()

        asyncio.run(serve())


if __name__ == "__main__":
    main()
