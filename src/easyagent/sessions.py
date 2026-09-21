"""Durable conversation turns over the existing leased workflow runtime."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Literal

from pydantic import Field, model_validator

from .contracts import AgentConfig, Contract
from .store import Conflict, encode


class ConversationCreate(Contract):
    title: str = Field(default="新对话", min_length=1, max_length=160)
    model: str = "auto"
    workspace: bool = False
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig(prompt=""))


class ConversationSettings(Contract):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    model: str | None = None
    instructions: str | None = Field(default=None, max_length=32000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32768)


class ConversationInput(Contract):
    text: str = Field(default="", max_length=100000)
    attachments: list[str] = Field(default_factory=list, max_length=8)
    intent: Literal["auto", "create", "workflow", "chat"] = "auto"
    execution: Literal['confirm', 'automatic'] = 'confirm'
    workflow: str | None = Field(default=None, max_length=160)
    mode: Literal["follow_up", "steer"] = "follow_up"
    idempotency_key: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=160)

    @model_validator(mode="after")
    def material(self):
        if not self.text.strip() and not self.attachments:
            raise ValueError("请输入需求或添加附件")
        if self.intent == "workflow" and not self.workflow:
            raise ValueError("请选择需要运行的流程")
        return self


class ConversationFork(Contract):
    message_id: int | None = Field(default=None, ge=1)
    title: str = "分支对话"


class ConversationCompact(Contract):
    keep_last: int = Field(default=6, ge=2, le=100)
    instructions: str = Field(
        default="保留用户目标、约束、已确认事实、未解决问题和证据来源。", max_length=4000
    )


class Conversations:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        self.lock = asyncio.Lock()
        with self.store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY,title TEXT NOT NULL,model TEXT NOT NULL,agent TEXT NOT NULL,
                parent TEXT,active_run TEXT,summary TEXT,summary_until INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS conversation_turns(
                id TEXT PRIMARY KEY,conversation TEXT NOT NULL,text TEXT NOT NULL,mode TEXT NOT NULL,
                status TEXT NOT NULL,run_id TEXT,workflow TEXT,key TEXT NOT NULL,created REAL NOT NULL,
                UNIQUE(conversation,key));
              CREATE TABLE IF NOT EXISTS conversation_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,conversation TEXT NOT NULL,turn_id TEXT,
                role TEXT NOT NULL,content TEXT NOT NULL,created REAL NOT NULL);
              CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search USING fts5(conversation UNINDEXED,message_id UNINDEXED,content);
            """)

    def append(self, db, conversation, turn, role, content):
        cursor = db.execute(
            "INSERT INTO conversation_messages(conversation,turn_id,role,content,created) VALUES(?,?,?,?,?)",
            (conversation, turn, role, content, time.time()),
        )
        db.execute("INSERT INTO conversation_search VALUES(?,?,?)", (conversation, cursor.lastrowid, content))
        return cursor.lastrowid

    async def create(self, options, parent=None):
        body = ConversationCreate.model_validate(options)
        if body.workspace and body.model != 'auto':
            from .assistant_builder import select_model
            select_model(self.hub, body.model)
        if not body.workspace:
            self.hub.prepare({
                "name": body.title,
                "steps": [{"id": "agent", "kind": "agent", "target": body.model, "input": body.agent.model_dump()}],
            })
        identifier = uuid.uuid4().hex
        await self.hub.extensions.dispatch("session.start", {"id": identifier, "title": body.title})
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,NULL,NULL,0,?)",
                (identifier, body.title, body.model, body.agent.model_dump_json(), parent, time.time()),
            )
            if body.workspace:
                db.execute("INSERT INTO memory VALUES('conversation-workspace',?,?,?,?)", (identifier, encode({"enabled": True}), "user", time.time()))
        return self.get(identifier)

    async def configure(self, identifier, options):
        body = ConversationSettings.model_validate(options)
        current = self.get(identifier)
        if current["active_run"]:
            raise Conflict("finish or interrupt the active turn before changing conversation settings")
        model = body.model or current["model"]
        if model not in self.hub.models.bindings and not (model == "auto" and current.get("workspace")):
            raise ValueError("unknown model")
        if current.get('workspace') and model != 'auto':
            from .assistant_builder import select_model
            select_model(self.hub, model)
        agent = dict(current["agent"])
        for name in ("instructions", "max_output_tokens"):
            if getattr(body, name) is not None:
                agent[name] = getattr(body, name)
        if model != current["model"]:
            await self.hub.extensions.dispatch(
                "model.select", {"conversation": identifier, "previous": current["model"], "model": model}
            )
        with self.store.transaction() as db:
            if db.execute("SELECT active_run FROM conversations WHERE id=?", (identifier,)).fetchone()[0]:
                raise Conflict("conversation started while configuring")
            if db.execute("SELECT 1 FROM conversation_turns WHERE conversation=? "
                          "AND status IN ('queued','starting','running') LIMIT 1", (identifier,)).fetchone():
                raise Conflict("请先完成或停止待处理消息，再切换模型")
            db.execute(
                "UPDATE conversations SET title=?,model=?,agent=? WHERE id=?",
                (body.title or current["title"], model, encode(agent), identifier),
            )
        await self.hub.extensions.dispatch(
            "session.info_changed",
            {"id": identifier, "title": body.title or current["title"], "model": model},
        )
        return self.get(identifier)

    def get(self, identifier):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise KeyError(identifier)
            messages = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM conversation_messages WHERE conversation=? ORDER BY id", (identifier,)
                )
            ]
            turns = [
                dict(r)
                for r in db.execute(
                    "SELECT id,text,mode,status,run_id,created FROM conversation_turns WHERE conversation=? ORDER BY created,id",
                    (identifier,),
                )
            ]
        result = dict(row) | {"agent": json.loads(row["agent"]), "messages": messages, "turns": turns}
        return self.hub.chat.enrich(result) if hasattr(self.hub, "chat") else result

    def list(self):
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,title,model,parent,active_run,created,EXISTS(SELECT 1 FROM memory WHERE namespace='conversation-workspace' AND key=conversations.id) AS workspace FROM conversations ORDER BY created DESC LIMIT 200"
                )
            ]

    async def send(self, identifier, options):
        body = ConversationInput.model_validate(options)
        conversation = self.get(identifier)
        if conversation.get("workspace"):
            return await self.hub.chat.send(conversation, body)
        if body.attachments or body.workflow or body.intent != "auto":
            raise ValueError("请在对话办事入口提交附件或选择工作流")
        transformed = await self.hub.extensions.dispatch(
            "session.input", {"id": identifier, "text": body.text, "mode": body.mode}
        )
        body.text = transformed["text"]
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT * FROM conversation_turns WHERE conversation=? AND key=?",
                (identifier, body.idempotency_key),
            ).fetchone()
            if old:
                if old["text"] != body.text or old["mode"] != body.mode:
                    raise Conflict("message key already used with different input")
                return dict(old)
            turn = uuid.uuid4().hex
            db.execute(
                "INSERT INTO conversation_turns VALUES(?,?,?,?,'queued',NULL,NULL,?,?)",
                (turn, identifier, body.text, body.mode, body.idempotency_key, time.time()),
            )
        await self.tick()
        return next(t for t in self.get(conversation["id"])["turns"] if t["id"] == turn)

    async def tick(self):
        async with self.lock:
            # History pagination must never prevent an older active task from advancing.
            with self.store.connect() as db:
                active = db.execute("SELECT DISTINCT c.id FROM conversations c JOIN conversation_turns t ON t.conversation=c.id WHERE t.status IN ('queued','starting','running') ORDER BY c.created").fetchall()
            for item in active:
                conversation = self.get(item["id"])
                if conversation.get("workspace"):
                    await self.hub.chat.tick(conversation)
                    continue
                active = conversation["active_run"]
                if active:
                    run = self.store.run(active)
                    if run["status"] not in ("succeeded", "failed", "cancelled"):
                        continue
                    with self.store.transaction() as db:
                        current = db.execute(
                            "SELECT active_run FROM conversations WHERE id=?", (item["id"],)
                        ).fetchone()[0]
                        if current != active:
                            continue
                        turn = db.execute(
                            "SELECT id FROM conversation_turns WHERE run_id=? AND status='running'", (active,)
                        ).fetchone()
                        if turn and run["status"] == "succeeded":
                            self.append(
                                db,
                                item["id"],
                                turn[0],
                                "assistant",
                                run["steps"][0]["output"].get("text", ""),
                            )
                        db.execute(
                            "UPDATE conversation_turns SET status=? WHERE run_id=?", (run["status"], active)
                        )
                        db.execute("UPDATE conversations SET active_run=NULL WHERE id=?", (item["id"],))
                    await self.hub.extensions.dispatch(
                        "turn.end", {"conversation": item["id"], "run_id": active, "status": run["status"]}
                    )
                    conversation = self.get(item["id"])
                with self.store.transaction() as db:
                    # A second process may have dispatched after our initial snapshot.
                    if db.execute(
                        "SELECT active_run FROM conversations WHERE id=?", (item["id"],)
                    ).fetchone()[0]:
                        continue
                    row = db.execute(
                        "SELECT * FROM conversation_turns WHERE conversation=? AND status IN ('queued','starting') ORDER BY created,id LIMIT 1",
                        (item["id"],),
                    ).fetchone()
                    if not row:
                        continue
                    if row["status"] == "queued":
                        agent = dict(conversation["agent"])
                        agent["prompt"] = row["text"]
                        words = row['text'].split()
                        names = {s['name'] for s in self.hub.skills.catalog()}
                        selected = []
                        while words and words[0].startswith('/') and words[0][1:] in names and len(selected) < 5:
                            selected.append(words.pop(0)[1:])
                        if selected:
                            agent['skills'] = list(dict.fromkeys(agent.get('skills', []) + selected))
                            agent['tools'] = list(dict.fromkeys(agent.get('tools', []) + ['skills.read']))

                        workflow = self.hub.prepare(
                            {
                                "name": conversation["title"],
                                "metadata": {"conversation": item["id"], "turn": row["id"]},
                                "steps": [
                                    {
                                        "id": "agent",
                                        "kind": "agent",
                                        "target": conversation["model"],
                                        "timeout_seconds": None,
                                        "input": agent,
                                    }
                                ],
                            }
                        ).model_dump()
                        db.execute(
                            "UPDATE conversation_turns SET status='starting',workflow=? WHERE id=?",
                            (encode(workflow), row["id"]),
                        )
                        self.append(db, item["id"], row["id"], "user", row["text"])
                    else:
                        workflow = json.loads(row["workflow"])
                run_id = self.hub.submit(workflow, "conversation:" + row["id"])
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE conversation_turns SET status='running',run_id=? WHERE id=?",
                        (run_id, row["id"]),
                    )
                    db.execute("UPDATE conversations SET active_run=? WHERE id=?", (run_id, item["id"]))
                await self.hub.extensions.dispatch(
                    "turn.start", {"conversation": item["id"], "run_id": run_id}
                )

    def context(self, job):
        metadata = self.store.run(job["run_id"])["spec"]["metadata"]
        if "conversation" not in metadata:
            return None
        conversation = self.get(metadata["conversation"])
        result = []
        if conversation["summary"]:
            result.append(
                {"role": "user", "content": "历史摘要（原始记录可检索）：" + conversation["summary"]}
            )
        result += [
            {"role": m["role"], "content": m["content"]}
            for m in conversation["messages"]
            if m["id"] > conversation["summary_until"]
        ]
        return result

    def steer(self, job, state):
        metadata = self.store.run(job["run_id"])["spec"]["metadata"]
        if "conversation" not in metadata:
            return False
        with self.store.transaction() as db:
            self.store.assert_owner(db, job)
            rows = db.execute(
                "SELECT * FROM conversation_turns WHERE conversation=? AND mode='steer' AND status='queued' ORDER BY created,id",
                (metadata["conversation"],),
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                message = {"role": "user", "content": row["text"]}
                state["messages"].append(message)
                state.setdefault("pinned_requests", []).append(encode(message))
                self.append(db, metadata["conversation"], row["id"], "user", row["text"])
                db.execute(
                    "UPDATE conversation_turns SET status='steered',run_id=? WHERE id=?",
                    (job["run_id"], row["id"]),
                )
            db.execute(
                "UPDATE steps SET state=? WHERE run_id=? AND id=?", (encode(state), job["run_id"], job["id"])
            )
            self.store.event(db, job["run_id"], "session.steered", {"turns": [r["id"] for r in rows]})
        return True

    async def fork(self, identifier, options):
        body = ConversationFork.model_validate(options)
        source = self.get(identifier)
        await self.hub.extensions.dispatch(
            "session.before_fork", {"id": identifier, "message_id": body.message_id}
        )
        if body.message_id is not None and not any(m["id"] == body.message_id for m in source["messages"]):
            raise ValueError("fork message does not belong to the conversation")
        destination = await self.create(
            {"title": body.title, "model": source["model"], "agent": source["agent"], "workspace": source.get("workspace", False)}, parent=identifier
        )
        with self.store.transaction() as db:
            for message in source["messages"]:
                if body.message_id is None or message["id"] <= body.message_id:
                    self.append(db, destination["id"], None, message["role"], message["content"])
        await self.hub.extensions.dispatch("session.fork", {"source": identifier, "id": destination["id"]})
        return self.get(destination["id"])

    def search(self, query, identifier=None):
        if not query.strip() or len(query) > 500:
            raise ValueError("search query must be between 1 and 500 characters")
        term = '"' + query.replace('"', '""') + '"'
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT conversation,message_id,snippet(conversation_search,2,'','',' … ',40) AS excerpt FROM conversation_search WHERE conversation_search MATCH ? AND (? IS NULL OR conversation=?) LIMIT 30",
                    (term, identifier, identifier),
                )
            ]

    async def compact(self, identifier, options):
        body = ConversationCompact.model_validate(options)
        conversation = self.get(identifier)
        if conversation["active_run"]:
            raise Conflict("finish or interrupt the active turn before compacting")
        messages = [
            m for m in conversation["messages"][: -body.keep_last] if m["id"] > conversation["summary_until"]
        ]
        if not messages:
            return {"compacted": False, "reason": "short conversation"}
        await self.hub.extensions.dispatch(
            "session.before_compact", {"id": identifier, "count": len(messages)}
        )
        run_id = self.hub.submit(
            {
                "name": "会话摘要",
                "steps": [
                    {
                        "id": "summary",
                        "kind": "model",
                        "target": conversation["model"],
                        "input": {
                            "prompt": body.instructions
                            + "\n此前摘要："
                            + (conversation["summary"] or "无")
                            + "\n"
                            + encode(
                                [
                                    {"id": m["id"], "role": m["role"], "content": m["content"]}
                                    for m in messages
                                ]
                            ),
                            "max_output_tokens": 2048,
                        },
                    }
                ],
            }
        )
        run = await self.hub.wait(run_id, timeout=180)
        if run["status"] != "succeeded":
            raise ValueError("summary run failed: " + run_id)
        summary = run["steps"][0]["output"]["text"]
        with self.store.transaction() as db:
            active = db.execute("SELECT active_run FROM conversations WHERE id=?", (identifier,)).fetchone()[
                0
            ]
            if active:
                raise Conflict("conversation changed while summarizing")
            db.execute(
                "UPDATE conversations SET summary=?,summary_until=? WHERE id=?",
                (summary, messages[-1]["id"], identifier),
            )
        await self.hub.extensions.dispatch(
            "session.compact", {"id": identifier, "run_id": run_id, "through": messages[-1]["id"]}
        )
        return {
            "compacted": True,
            "summary": summary,
            "through": messages[-1]["id"],
            "run_id": run_id,
            "original_messages_retained": True,
        }

    async def interrupt(self, identifier):
        conversation = self.get(identifier)
        if conversation.get("workspace"):
            result = await self.hub.chat.interrupt(identifier)
            await self.hub.extensions.dispatch("session.shutdown", {"id": identifier})
            return result
        if conversation["active_run"]:
            self.store.cancel(conversation["active_run"])
        await self.hub.extensions.dispatch("session.shutdown", {"id": identifier})
        return {"interrupted": bool(conversation["active_run"])}


def install_conversations(app, hub):
    sessions = hub.conversations

    @app.get("/v1/conversations")
    async def listing():
        return sessions.list()

    @app.post("/v1/conversations", status_code=201)
    async def create(body: ConversationCreate):
        return await sessions.create(body)

    @app.get("/v1/conversations/search")
    async def search(query: str, conversation: str | None = None):
        return sessions.search(query, conversation)

    @app.get("/v1/conversations/workflow-catalog")
    async def workflow_catalog():
        return hub.chat.public_catalog()

    @app.get("/v1/conversations/{identifier}")
    async def get(identifier: str):
        await sessions.tick()
        return sessions.get(identifier)

    @app.patch("/v1/conversations/{identifier}")
    async def configure(identifier: str, body: ConversationSettings):
        return await sessions.configure(identifier, body)

    @app.post("/v1/conversations/{identifier}/messages", status_code=202)
    async def send(identifier: str, body: ConversationInput):
        return await sessions.send(identifier, body)

    @app.post("/v1/conversations/{identifier}/fork", status_code=201)
    async def fork(identifier: str, body: ConversationFork):
        return await sessions.fork(identifier, body)

    @app.post("/v1/conversations/{identifier}/compact")
    async def compact(identifier: str, body: ConversationCompact):
        return await sessions.compact(identifier, body)

    @app.post("/v1/conversations/{identifier}/interrupt")
    async def interrupt(identifier: str):
        return await sessions.interrupt(identifier)

    @app.post("/v1/conversations/{identifier}/resume-connections")
    async def resume_connections(identifier: str, turn_id: str | None = None):
        return await hub.chat.resume_connections(identifier, turn_id)
