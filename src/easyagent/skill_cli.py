"""Skill packaging/import CLI, preserving the same API and validation as Studio."""

import json
from pathlib import Path
from .skill_packages import SkillPackage, inspect_package


def package_directory(directory):
    root = Path(directory).resolve()
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("skill source cannot contain symlinks")
        if path.is_file():
            if path.stat().st_size > 128000 or len(files) >= 80:
                raise ValueError("skill exceeds file/size limits")
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    package = SkillPackage(files=files, source={"kind": "local"})
    inspect_package(package)
    return package.model_dump()


async def command(args, token):
    from .client import HubClient

    if args.skill_command == "package":
        body = package_directory(args.directory)
        Path(args.output).write_text(json.dumps(body, ensure_ascii=False, indent=2))
        return {"output": args.output}
    async with HubClient(args.url, token, timeout=90) as client:
        if args.skill_command == "list":
            return await client.skills()
        if args.skill_command == "install":
            path = Path(args.source)
            body = (
                package_directory(path)
                if path.is_dir()
                else {"files": {"SKILL.md": path.read_text()}}
                if path.suffix == ".md"
                else json.loads(path.read_text())
            )
            return await client.install_skill(body, args.expected_revision)
        return await client.skill_source(args.repository, args.path, args.ref)
