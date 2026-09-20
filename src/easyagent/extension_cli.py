"""Scaffold portable extension projects; package only declared source files."""

import json
from pathlib import Path
from .extensions import build_package


def scaffold(directory, name, language):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("extension directory must be empty")
    manifest = {
        "id": name,
        "revision": 1,
        "title": name,
        "runtime": language,
        "tools": [
            {"handler": "echo", "spec": {"name": name + ".echo", "description": "Return supplied data"}}
        ],
    }
    if language == "javascript":
        files = {
            "extension.js": "function handle(r) { return {result: r.method.startsWith('lifecycle.') ? {} : r.params}; }\n"
        }
    elif language == "python":
        files = {
            "main.py": "import json, sys\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'result':{} if r['method'].startswith('lifecycle.') else r['params']}))\n"
        }
        manifest["entrypoint"] = "main.py"
    elif language == "node":
        files = {
            "extension.mjs": "import {readFileSync} from 'node:fs';\nconst r=JSON.parse(readFileSync(0,'utf8'));\nconsole.log(JSON.stringify({result:r.method.startsWith('lifecycle.')?{}:r.params}));\n"
        }
        manifest["entrypoint"] = "extension.mjs"
    else:
        files = {
            "Cargo.toml": f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n[dependencies]\nserde_json = "1"\n',
            "src/main.rs": """use std::io::{self,Read};
fn main(){let mut s=String::new();io::stdin().read_to_string(&mut s).unwrap();
let r:serde_json::Value=serde_json::from_str(&s).unwrap();
let result=if r["method"].as_str().unwrap().starts_with("lifecycle."){serde_json::json!({})}else{r["params"].clone()};
println!("{}",serde_json::json!({"result":result}));}
""",
        }
        manifest["entrypoint"] = "Cargo.toml"
        manifest["timeout_seconds"] = 120
    if language != "javascript":
        manifest["permissions"] = ["trusted_process"]
    # Validate before creating files.
    build_package(manifest, files)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (root / "sources.json").write_text(json.dumps(list(files), indent=2), encoding="utf-8")
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return {"directory": str(root), "next": "eah extension package " + str(root)}


def package_directory(directory):
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    names = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    if manifest.get("runtime") == "rust":
        import subprocess

        if not (root / "Cargo.lock").exists():
            subprocess.run(
                [
                    "cargo",
                    "generate-lockfile",
                    "--offline",
                    "--manifest-path",
                    str(root / manifest["entrypoint"]),
                ],
                check=True,
                capture_output=True,
            )
        if "Cargo.lock" not in names:
            names.append("Cargo.lock")
    files = {}
    for name in names:
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError("source path escapes project")
        files[name] = path.read_text(encoding="utf-8")
    from .components import digest

    manifest["lock"] = {name: digest(value) for name, value in files.items()}
    return build_package(manifest, files)
