"""Rebuild public model/protocol snapshot from a downloaded documentation bundle.

Input: models.json, pricing.json and spec-*.json (data.platform, data.yamlContent).
No credentials or service responses are copied. Documentation links use --docs-base.
"""

import argparse
import copy
import json
import pathlib
import re
from datetime import date

import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-bundle", type=pathlib.Path, required=True)
parser.add_argument("--docs-base", required=True)
parser.add_argument("--retrieved", default=date.today().isoformat())
parser.add_argument(
    "--output", type=pathlib.Path, default=pathlib.Path("src/easyagent/data/model_protocols.json")
)
args = parser.parse_args()


# YAML 1.2 integer parsing: preserve aspect ratios such as 16:9 as strings.
class Loader(yaml.SafeLoader):
    pass


Loader.yaml_implicit_resolvers = copy.deepcopy(Loader.yaml_implicit_resolvers)
for ch, rules in Loader.yaml_implicit_resolvers.items():
    Loader.yaml_implicit_resolvers[ch] = [
        (tag, rx) for tag, rx in rules if tag not in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:bool")
    ]
Loader.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*|0x[0-9a-fA-F]+)$"), list("-+0123456789")
)
Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF")
)
root = args.source_bundle
pricing = json.loads((root / "pricing.json").read_text())
operations = []
for file in sorted(root.glob("spec-*.json")):
    source = json.loads(file.read_text())["data"]
    family = source["platform"]
    if family in ("system", "playground", "v1-models"):
        continue
    doc = yaml.load(source["yamlContent"], Loader=Loader)

    def resolve(v, chain=()):
        if isinstance(v, list):
            return [resolve(x, chain) for x in v]
        if not isinstance(v, dict):
            return v
        if "$ref" in v:
            ref = v["$ref"]
            if not ref.startswith("#/") or ref in chain:
                return {}
            target = doc
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                return {}
            return resolve(target, (*chain, ref))
        return {k: resolve(x, chain) for k, x in v.items()}

    def lean(v, mapping=False):
        if isinstance(v, list):
            return [lean(x) for x in v]
        if not isinstance(v, dict):
            return v
        return {
            k: lean(x, k in ("properties", "$defs", "patternProperties"))
            for k, x in v.items()
            if mapping
            or (
                not str(k).startswith("x-")
                and k not in ("description", "example", "examples", "title", "$schema")
            )
        }

    for path, item in doc.get("paths", {}).items():
        for method, raw in item.items():
            if method not in ["get", "post", "put", "delete", "patch"]:
                continue
            if "/account/" in path:
                continue
            op = resolve(raw)
            content = op.get("requestBody", {}).get("content", {})
            encoding = (
                "multipart"
                if "multipart/form-data" in content and content["multipart/form-data"].get("schema")
                else "json"
            )
            media = "multipart/form-data" if encoding == "multipart" else "application/json"
            schema = lean(content.get(media, {}).get("schema", {"type": "object"}))
            params = [
                lean(resolve(p))
                for p in item.get("parameters", []) + raw.get("parameters", [])
                if isinstance(p, dict)
            ]
            params = [
                p
                for p in params
                if str(p.get("name", "")).lower()
                not in (
                    "key",
                    "api_key",
                    "api-key",
                    "apikey",
                    "authorization",
                    "x-api-key",
                    "x-goog-api-key",
                    "content-type",
                    "host",
                    "accept",
                )
            ]
            op_id = family + "." + method + "." + re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
            operations.append(
                {
                    "id": op_id,
                    "family": family,
                    "method": method.upper(),
                    "path": path,
                    "title": raw.get("summary", op_id),
                    "request_encoding": encoding,
                    "request_schema": schema,
                    "parameters": params,
                    "source": args.docs_base.rstrip("/") + "/" + family,
                }
            )
models = [
    {k: d[k] for k in ("id", "model_type", "supported_endpoint_types") if k in d}
    for d in json.loads((root / "models.json").read_text())["data"]
]
result = {
    "schema_version": 1,
    "retrieved": args.retrieved,
    "models": models,
    "endpoint_types": pricing["supported_endpoint"],
    "operations": operations,
}
args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
print(
    "bundled",
    len(models),
    "models",
    len(operations),
    "operations",
    len(pricing["supported_endpoint"]),
    "endpoint labels",
)
print("bytes", args.output.stat().st_size)
