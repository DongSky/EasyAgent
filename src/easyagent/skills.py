from __future__ import annotations

import re
from pathlib import Path

import yaml


class SkillRegistry:
    def __init__(self):
        self.entries = {}
        self.extension_entries = {}
        self.managed = {}

    def discover(self, root):
        root = Path(root).resolve()
        for path in sorted(root.glob("*/SKILL.md")):
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or path.is_symlink() or path.parent.is_symlink():
                raise ValueError("skills must stay inside their configured root")
            if path.stat().st_size > 128_000:
                raise ValueError("SKILL.md exceeds 128 KB")
            text = path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) != 3 or parts[0].strip():
                raise ValueError(f"{path}: YAML frontmatter required")
            metadata = yaml.safe_load(parts[1])
            if not isinstance(metadata, dict):
                raise ValueError("skill metadata must be a mapping")
            name, description = metadata.get("name", ""), metadata.get("description", "")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or len(name) > 64
                or name != path.parent.name
            ):
                raise ValueError("skill name must match its directory and use lowercase kebab-case")
            if not isinstance(description, str) or not 1 <= len(description) <= 1024:
                raise ValueError("skill description must have 1-1024 characters")
            if name in self.entries or name in self.managed:
                raise ValueError("duplicate skill name: " + name)
            self.entries[name] = {
                "name": name,
                "description": description,
                "path": resolved,
                "allowed_tools": metadata.get("allowed-tools", ""),
            }

    def catalog(self):
        return (
            [{k: v for k, v in entry.items() if k != "path"} for entry in self.entries.values()]
            + [
                {"name": owner + "." + name, "description": body[:160], "allowed_tools": ""}
                for owner, entries in self.extension_entries.items()
                for name, body in entries.items()
            ]
            + [self.describe(row) for row in self.managed.values()]
        )

    def describe(self, row):
        from .skill_packages import inspect_package

        return inspect_package(row["package"]) | {"revision": row["revision"]}

    def snapshot(self, name):
        if name in self.managed:
            return dict(self.managed[name]["package"]["files"])
        if name in self.entries:
            root = self.entries[name]["path"].parent
            files = {}
            for path in root.rglob("*"):
                if path.is_file():
                    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                        raise ValueError("skill resources must stay in their root")
                    if path.stat().st_size > 128_000 or len(files) >= 80:
                        raise ValueError("skill resources exceed limits")
                    files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
            if sum(len(v.encode()) for v in files.values()) > 1_500_000:
                raise ValueError("skill resources exceed total limit")
            return files
        return {"SKILL.md": self.load(name)}

    def load(self, name):
        if name in self.managed:
            return self.managed[name]["package"]["files"]["SKILL.md"].split("---", 2)[2].strip()
        owner, _, leaf = name.partition(".")
        if owner in self.extension_entries and leaf in self.extension_entries[owner]:
            return self.extension_entries[owner][leaf]
        path = self.entries[name]["path"]
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 128_000:
            raise ValueError("skill file changed outside accepted boundaries")
        return path.read_text(encoding="utf-8").split("---", 2)[2].strip()

    def resource(self, name, relative):
        if name in self.managed:
            return self.managed[name]["package"]["files"][relative]
        root = self.entries[name]["path"].parent.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.stat().st_size > 128_000:
            raise ValueError("invalid skill resource path or size")
        return target.read_text(encoding="utf-8")
