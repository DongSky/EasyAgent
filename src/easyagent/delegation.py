"""Dynamic child agents use the same durable runs, budgets and approval boundaries."""

import json
import time

from .contracts import DelegationGrant, ToolSpec
from .tools import WaitingChildren


class Delegation:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS agent_mailbox(
              id TEXT PRIMARY KEY,parent TEXT NOT NULL,child TEXT NOT NULL,text TEXT NOT NULL,
              delivered INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL)""")
        self.register()

    def grant(self, ctx):
        raw = ctx.job["spec"]["input"].get("delegation")
        if not raw:
            raise PermissionError("dynamic delegation requires an explicit grant")
        return DelegationGrant.model_validate(raw)

    def owned(self, ctx, child):
        self.grant(ctx)
        with self.store.connect() as db:
            if not db.execute(
                "SELECT 1 FROM child_runs WHERE parent_id=? AND child_id=?", (ctx.run_id, child)
            ).fetchone():
                raise PermissionError("child does not belong to this parent run")
        return self.store.run(child)

    def receive(self, job, state):
        with self.store.transaction() as db:
            self.store.assert_owner(db, job)
            rows = db.execute(
                "SELECT * FROM agent_mailbox WHERE child=? AND delivered=0 ORDER BY created,id",
                (job["run_id"],),
            ).fetchall()
            for row in rows:
                state["messages"].append({"role": "user", "content": "协作任务消息：" + row["text"]})
                db.execute("UPDATE agent_mailbox SET delivered=1 WHERE id=?", (row["id"],))
            if rows:
                db.execute(
                    "UPDATE steps SET state=? WHERE run_id=? AND id=?",
                    (json.dumps(state), job["run_id"], job["id"]),
                )
        return bool(rows)

    def register(self):
        async def spawn(args, ctx):
            grant = self.grant(ctx)
            if args["model"] not in grant.models or not set(args.get("tools", [])).issubset(grant.tools):
                raise PermissionError("child capabilities exceed delegation grant")
            if any(n.startswith(("development.", "code.")) for n in args.get("tools", [])):
                raise PermissionError("child cannot acquire code/development authority implicitly")
            child_grant = DelegationGrant(models=[], tools=[], max_depth=grant.max_depth)
            if args.get("delegation"):
                child_grant = DelegationGrant.model_validate(args["delegation"])
                if (
                    not grant.allow_redelegate
                    or not set(child_grant.models).issubset(grant.models)
                    or not set(child_grant.tools).issubset(grant.tools)
                    or child_grant.max_children > grant.max_children
                    or child_grant.max_depth > grant.max_depth
                ):
                    raise PermissionError("re-delegation exceeds parent grant")
            elif any(n.startswith("agents.") and n != "agents.reply" for n in args.get("tools", [])):
                raise PermissionError("nested delegation tools require a child grant")
            slot = int(ctx.invocation_id[:15], 16)
            with self.store.connect() as db:
                old = db.execute(
                    "SELECT child_id FROM child_runs WHERE parent_id=? AND step_id=? AND slot=?",
                    (ctx.run_id, ctx.step_id, slot),
                ).fetchone()
                count = db.execute(
                    "SELECT count(*) FROM child_runs WHERE parent_id=?", (ctx.run_id,)
                ).fetchone()[0]
                parent, depth = ctx.run_id, 0
                while row := db.execute(
                    "SELECT parent_id FROM child_runs WHERE child_id=?", (parent,)
                ).fetchone():
                    parent = row[0]
                    depth += 1
            if old:
                return {"id": old[0]}
            if count >= grant.max_children or depth >= grant.max_depth:
                raise ValueError("delegation budget exhausted")
            child = self.hub.submit(
                {
                    "name": args.get("title", "子任务")[:160],
                    "steps": [
                        {
                            "id": "agent",
                            "kind": "agent",
                            "target": args["model"],
                            "input": {
                                "prompt": args["goal"],
                                "tools": list(dict.fromkeys([*args.get("tools", []), "agents.reply"])),
                                "delegation": child_grant.model_dump(),
                            },
                            "timeout_seconds": None,
                        }
                    ],
                },
                "delegate:" + ctx.invocation_id,
                (ctx.run_id, ctx.step_id, slot),
                parent_job=ctx.job,
            )
            return {"id": child}

        async def status(args, ctx):
            run = self.owned(ctx, args["id"])
            return {"id": run["id"], "status": run["status"], "usage": run["usage"]}

        async def result(args, ctx):
            run = self.owned(ctx, args["id"])
            if run["status"] not in ("succeeded", "failed", "cancelled"):
                raise WaitingChildren()
            return {
                "id": run["id"],
                "status": run["status"],
                "output": run["steps"][0]["output"],
                "error": run["steps"][0]["error"],
                "usage": run["usage"],
            }

        async def send(args, ctx):
            run = self.owned(ctx, args["id"])
            if run["status"] in ("succeeded", "failed", "cancelled"):
                raise ValueError("child has already finished")
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO agent_mailbox VALUES(?,?,?,?,0,?)",
                    (ctx.invocation_id, ctx.run_id, args["id"], args["text"], time.time()),
                )
            return {"queued": True, "id": ctx.invocation_id}

        async def reply(args, ctx):
            with self.store.transaction() as db:
                parent = db.execute(
                    "SELECT parent_id FROM child_runs WHERE child_id=?", (ctx.run_id,)
                ).fetchone()
                if not parent:
                    raise PermissionError("only delegated children can reply to their parent")
                active = db.execute("SELECT status FROM runs WHERE id=?", (parent[0],)).fetchone()[0]
                if active in ("succeeded", "failed", "cancelled"):
                    raise ValueError("parent has finished")
                db.execute(
                    "INSERT OR IGNORE INTO agent_mailbox VALUES(?,?,?,?,0,?)",
                    (ctx.invocation_id, ctx.run_id, parent[0], args["text"], time.time()),
                )
            return {"queued": True, "parent": parent[0]}

        async def cancel(args, ctx):
            self.owned(ctx, args["id"])
            self.store.cancel(args["id"])
            return {"id": args["id"], "status": "cancelled"}

        definitions = {
            "spawn": (
                spawn,
                {
                    "goal": {"type": "string", "maxLength": 64000},
                    "model": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": "string"},
                    "delegation": DelegationGrant.model_json_schema(),
                },
                ["goal", "model"],
            ),
            "send": (
                send,
                {"id": {"type": "string"}, "text": {"type": "string", "maxLength": 32000}},
                ["id", "text"],
            ),
            "reply": (reply, {"text": {"type": "string", "maxLength": 32000}}, ["text"]),
            "status": (status, {"id": {"type": "string"}}, ["id"]),
            "wait": (result, {"id": {"type": "string"}}, ["id"]),
            "cancel": (cancel, {"id": {"type": "string"}}, ["id"]),
        }
        for name, (fn, properties, required) in definitions.items():
            self.hub.tools.register(
                ToolSpec(
                    name="agents." + name,
                    description="动态子 Agent " + name + "；需要父任务明确的 delegation 授权",
                    effect="local",
                    input_schema={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                ),
                fn,
            )
