from __future__ import annotations

import json
import time
import uuid
from datetime import date, timedelta
from typing import Literal

from jsonschema import validate
from pydantic import Field

from easyagent.contracts import Contract, ToolSpec
from easyagent.store import Conflict, encode


TEMPLATES = {
    "moving": {"label": "搬家", "description": "退租、报价、搬运、交接、押金", "tasks": [
        ("lease", "核对租约与退租期限", -30, [], "租约、退租通知及房东确认"),
        ("quotes", "收集搬家公司报价", -21, ["lease"], "服务范围、报价、取消条款"),
        ("booking", "确认搬家公司和日期", -14, ["quotes"], "预约确认信息"),
        ("internet", "预约宽带迁移", -10, [], "服务商工单和上门时间"),
        ("elevator", "预约电梯和物业通行", -7, ["booking"], "物业预约确认"),
        ("packing", "打包并记录物品", -2, [], "箱单与贵重物品清单"),
        ("handover", "房屋交接并记录读数", 0, ["lease", "packing"], "交接记录、照片和钥匙回执"),
        ("deposit", "跟进押金返还", 7, ["handover"], "到账记录或房东确认") ]},
    "travel": {"label": "旅行", "description": "证件、预订、取消期限、行李", "tasks": [
        ("documents", "核对证件与入境材料", -30, [], "证件有效期和材料清单"),
        ("bookings", "汇总交通与住宿预订", -14, ["documents"], "预订编号与确认邮件"),
        ("deadlines", "记录免费取消和改签期限", -10, ["bookings"], "各预订的取消政策"),
        ("schedule", "检查行程时间冲突", -7, ["bookings"], "交通衔接与到达时间"),
        ("packing", "准备行李与离家事项", -2, [], "行李和离家检查清单"),
        ("checkin", "确认出发安排", -1, ["schedule", "packing"], "值机或车次确认") ]},
    "school": {"label": "学校通知", "description": "报名回执、物品、接送安排", "tasks": [
        ("notice", "核对通知和报名截止日", -7, [], "学校原始通知"),
        ("reply", "提交报名或回执", -5, ["notice"], "学校接收确认"),
        ("transport", "确认家人接送分工", -3, ["reply"], "接送人和时间确认"),
        ("materials", "准备活动所需物品", -1, ["notice"], "物品清单"),
        ("attend", "确认活动完成与接回", 0, ["transport", "materials"], "接回确认") ]},
    "repair": {"label": "家电维修", "description": "保修、报价、预约、维修结果", "tasks": [
        ("warranty", "找到购买记录与保修信息", -7, [], "发票、型号与保修条款"),
        ("symptoms", "整理故障现象", -6, [], "故障说明和照片链接"),
        ("options", "确认维修选项与费用", -4, ["warranty", "symptoms"], "报价和服务范围"),
        ("appointment", "确认师傅上门时间", -2, ["options"], "预约和联系方式"),
        ("visit", "记录上门维修结果", 0, ["appointment"], "维修工单与收据"),
        ("verify", "确认恢复正常使用", 2, ["visit"], "试用结果") ]},
}


class PlanRequest(Contract):
    kind: Literal["moving", "travel", "school", "repair"]
    title: str = Field(min_length=1, max_length=120)
    anchor: date


class ConfirmPlan(Contract):
    run_id: str


class Reschedule(Contract):
    anchor: date


class TaskEdit(Contract):
    owner: str | None = Field(default=None, max_length=100)
    state: Literal["todo", "waiting", "blocked", "done"] | None = None
    waiting_on: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=4000)
    evidence: str | None = Field(default=None, max_length=4000)
    cost_cents: int | None = Field(default=None, ge=0, le=100000000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    due: date | None = None
    expected_version: int | None = Field(default=None, ge=1)


class Resource(Contract):
    kind: Literal["document", "contact", "note", "expense"]
    title: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=20000)


class Extract(Contract):
    text: str = Field(min_length=1, max_length=32000)
    model: str = "template"


class ExtractConfirm(Contract):
    run_id: str


class LifeStore:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS life_projects(
                  id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,anchor TEXT NOT NULL,
                  plan_run TEXT NOT NULL UNIQUE,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS life_tasks(
                  id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES life_projects(id),
                  task_key TEXT NOT NULL,title TEXT NOT NULL,due TEXT,offset_days INTEGER,
                  dependencies TEXT NOT NULL,owner TEXT NOT NULL DEFAULT '我',state TEXT NOT NULL DEFAULT 'todo',
                  waiting_on TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',
                  evidence_hint TEXT NOT NULL DEFAULT '',cost_cents INTEGER NOT NULL DEFAULT 0,
                  currency TEXT NOT NULL DEFAULT 'CNY',version INTEGER NOT NULL DEFAULT 1,
                  UNIQUE(project_id,task_key));
                CREATE TABLE IF NOT EXISTS life_resources(
                  id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES life_projects(id),kind TEXT NOT NULL,
                  title TEXT NOT NULL,value TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS life_changes(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT NOT NULL,action TEXT NOT NULL,
                  payload TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS life_imports(
                  project_id TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(project_id,run_id));
            """)

    def log(self, db, identifier, action, payload):
        db.execute("INSERT INTO life_changes(project_id,action,payload,created) VALUES(?,?,?,?)", (identifier, action, encode(payload), time.time()))

    def create(self, run_id):
        run = self.store.run(run_id)
        if run["status"] != "succeeded" or run["steps"][0]["spec"]["target"] != "life.plan_template":
            raise ValueError("请先生成并完成计划草稿")
        plan = run["steps"][0]["output"]
        anchor = date.fromisoformat(plan["anchor"])
        with self.store.transaction() as db:
            old = db.execute("SELECT id FROM life_projects WHERE plan_run=?", (run_id,)).fetchone()
            if old:
                identifier = old[0]
            else:
                identifier = uuid.uuid4().hex
                db.execute("INSERT INTO life_projects VALUES(?,?,?,?,?,?)", (identifier, plan["title"], plan["kind"], plan["anchor"], run_id, time.time()))
                keys = {task["key"]: uuid.uuid4().hex for task in plan["tasks"]}
                for task in plan["tasks"]:
                    db.execute("""INSERT INTO life_tasks(id,project_id,task_key,title,due,offset_days,dependencies,evidence_hint)
                        VALUES(?,?,?,?,?,?,?,?)""", (keys[task["key"]], identifier, task["key"], task["title"],
                        (anchor + timedelta(days=task["offset"])).isoformat(), task["offset"],
                        encode([keys[key] for key in task["dependencies"]]), task["evidence_hint"]))
                self.log(db, identifier, "project.confirmed", {"plan_run": run_id})
        return self.get(identifier)

    def get(self, identifier):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM life_projects WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise KeyError(identifier)
            tasks = [dict(t) | {"dependencies": json.loads(t["dependencies"])} for t in db.execute("SELECT * FROM life_tasks WHERE project_id=? ORDER BY due,rowid", (identifier,))]
            states = {t["id"]: t["state"] for t in tasks}
            titles = {t["id"]: t["title"] for t in tasks}
            for task in tasks:
                task["blocked_by"] = [titles[k] for k in task["dependencies"] if states[k] != "done"]
                task["display_state"] = "blocked" if task["blocked_by"] and task["state"] == "todo" else task["state"]
            resources = [dict(r) for r in db.execute("SELECT * FROM life_resources WHERE project_id=? ORDER BY created DESC", (identifier,))]
            changes = [dict(r) | {"payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM life_changes WHERE project_id=? ORDER BY id DESC LIMIT 100", (identifier,))]
        return dict(row) | {"tasks": tasks, "resources": resources, "changes": changes}

    def list(self):
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM life_projects ORDER BY created DESC")]
        return [self.get(identifier) for identifier in ids]

    def edit_task(self, identifier, body):
        updates = body.model_dump(exclude_none=True, mode="json")
        if "due" in body.model_fields_set and body.due is None:
            updates["due"] = None
        expected = updates.pop("expected_version", None)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM life_tasks WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise KeyError(identifier)
            if expected is not None and row["version"] != expected:
                raise Conflict("事项已被更新，请刷新后重试")
            state = updates.get("state", row["state"])
            if state == "done":
                evidence = updates.get("evidence", row["evidence"])
                if not evidence.strip():
                    raise ValueError("办结前请填写完成凭证或确认记录")
                for dep in json.loads(row["dependencies"]):
                    if db.execute("SELECT state FROM life_tasks WHERE id=?", (dep,)).fetchone()[0] != "done":
                        raise Conflict("前置事项还未完成，请先处理依赖")
            if row["state"] == "done" and state != "done":
                for other in db.execute("SELECT state,dependencies FROM life_tasks WHERE project_id=?", (row["project_id"],)):
                    if identifier in json.loads(other["dependencies"]) and other["state"] == "done":
                        raise Conflict("请先重新打开依赖此事项的已完成任务")
            if state == "waiting" and not updates.get("waiting_on", row["waiting_on"]).strip():
                raise ValueError("请填写正在等待谁")
            if state == "blocked" and not updates.get("note", row["note"]).strip():
                raise ValueError("请说明卡在哪里")
            if "due" in updates:
                anchor = date.fromisoformat(db.execute("SELECT anchor FROM life_projects WHERE id=?", (row["project_id"],)).fetchone()[0])
                updates["offset_days"] = (date.fromisoformat(updates["due"]) - anchor).days if updates["due"] else None
            if updates:
                fields = ",".join(k + "=?" for k in updates)
                db.execute(f"UPDATE life_tasks SET {fields},version=version+1 WHERE id=?", (*updates.values(), identifier))
                self.log(db, row["project_id"], "task.updated", {"task_id": identifier, **updates})
            project_id = row["project_id"]
        return self.get(project_id)

    def reschedule(self, identifier, anchor):
        with self.store.transaction() as db:
            row = db.execute("SELECT anchor FROM life_projects WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise KeyError(identifier)
            before = row[0]
            db.execute("UPDATE life_projects SET anchor=? WHERE id=?", (anchor.isoformat(), identifier))
            tasks = db.execute("SELECT id,offset_days FROM life_tasks WHERE project_id=? AND state!='done' AND offset_days IS NOT NULL", (identifier,)).fetchall()
            for task in tasks:
                due = (anchor + timedelta(days=task["offset_days"])).isoformat()
                db.execute("UPDATE life_tasks SET due=?,version=version+1 WHERE id=?", (due, task["id"]))
            self.log(db, identifier, "project.rescheduled", {"before": before, "after": anchor.isoformat(), "changed_tasks": len(tasks)})
        return self.get(identifier)

    def add_resource(self, identifier, body):
        self.get(identifier)
        with self.store.transaction() as db:
            db.execute("INSERT INTO life_resources VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, identifier, body.kind, body.title, body.value, time.time()))
            self.log(db, identifier, "resource.added", {"title": body.title, "kind": body.kind})
        return self.get(identifier)

    def import_extract(self, identifier, run_id):
        project = self.get(identifier)
        run = self.store.run(run_id)
        if run["status"] != "succeeded" or run["spec"]["metadata"].get("life_project") != identifier:
            raise ValueError("请先完成这个项目的材料整理")
        output = run["steps"][0]["output"]
        data = output["data"] if run["steps"][0]["spec"]["kind"] == "model" else output
        validate({"tasks": data["tasks"]}, EXTRACTION_SCHEMA)
        step = run["steps"][0]["spec"]
        material = step["input"]["messages"][-1]["content"] if step["kind"] == "model" else step["input"]["text"]
        # Validate every candidate before any database mutation; keep unknown dates unknown.
        for task in data["tasks"]:
            if not task["title"].strip() or not task["source"].strip():
                raise ValueError("事项标题和原文来源不能为空")
            if task["source"] not in material:
                raise ValueError("提取事项的来源必须逐字引用原始材料，请重新整理或手动核对")
            if task.get("due") is not None:
                date.fromisoformat(task["due"])
        anchor = date.fromisoformat(project["anchor"])
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM life_imports WHERE project_id=? AND run_id=?", (identifier, run_id)).fetchone():
                return project
            for index, task in enumerate(data["tasks"]):
                due = date.fromisoformat(task["due"]) if task.get("due") else None
                db.execute("""INSERT INTO life_tasks(id,project_id,task_key,title,due,offset_days,dependencies,owner,note,evidence_hint)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, identifier, run_id+":"+str(index), task["title"], due.isoformat() if due else None,
                    (due-anchor).days if due else None, "[]", task.get("owner") or "待分工", task["source"], "原始材料对应的完成确认"))
            db.execute("INSERT INTO life_imports VALUES(?,?)", (identifier, run_id))
            self.log(db, identifier, "material.confirmed", {"run_id": run_id, "tasks": len(data["tasks"])})
        return self.get(identifier)


EXTRACTION_SCHEMA = {"type": "object", "properties": {"tasks": {"type": "array", "maxItems": 30, "items": {
    "type": "object", "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 200},
    "due": {"type": ["string", "null"]}, "owner": {"type": "string"}, "source": {"type": "string", "minLength": 1}},
    "required": ["title", "due", "owner", "source"], "additionalProperties": False}}}, "required": ["tasks"], "additionalProperties": False}


def install_life(app, hub):
    life = LifeStore(hub)

    async def plan_template(args, context):
        request = PlanRequest.model_validate(args)
        return {**request.model_dump(mode="json"), "mode": "template", "tasks": [
            {"key": key, "title": title, "offset": offset, "dependencies": deps, "evidence_hint": evidence}
            for key, title, offset, deps, evidence in TEMPLATES[request.kind]["tasks"]]}

    async def capture_material(args, context):
        # With no model configured, preserve the source as one explicit manual-review task.
        return {"mode": "manual", "tasks": [{"title": "核对并拆解这份材料", "due": None, "owner": "待分工", "source": args["text"]}]}

    hub.tools.register(ToolSpec(name="life.plan_template", description="Generate an explicit life-project template; not an LLM inference",
                               input_schema=PlanRequest.model_json_schema()), plan_template)
    hub.tools.register(ToolSpec(name="life.capture_material", description="Preserve raw material for manual review",
        input_schema={"type": "object", "required": ["text"], "properties": {"text": {"type": "string", "maxLength": 32000}}}), capture_material)

    @app.get("/v1/life/templates")
    async def templates():
        return [{"kind": k, "label": v["label"], "description": v["description"], "tasks": len(v["tasks"])} for k, v in TEMPLATES.items()]

    @app.post("/v1/life/plans", status_code=201)
    async def plan(body: PlanRequest):
        return {"id": hub.submit({"name": "整理计划：" + body.title, "steps": [{"id": "plan", "target": "life.plan_template", "input": body.model_dump(mode="json")} ]})}

    @app.post("/v1/life/projects", status_code=201)
    async def create(body: ConfirmPlan):
        return life.create(body.run_id)

    @app.get("/v1/life/projects")
    async def projects():
        return life.list()

    @app.get("/v1/life/projects/{identifier}")
    async def get(identifier: str):
        return life.get(identifier)

    @app.patch("/v1/life/projects/{identifier}/date")
    async def reschedule(identifier: str, body: Reschedule):
        return life.reschedule(identifier, body.anchor)

    @app.patch("/v1/life/tasks/{identifier}")
    async def edit(identifier: str, body: TaskEdit):
        return life.edit_task(identifier, body)

    @app.post("/v1/life/projects/{identifier}/resources")
    async def resource(identifier: str, body: Resource):
        return life.add_resource(identifier, body)

    @app.post("/v1/life/projects/{identifier}/extract")
    async def extract(identifier: str, body: Extract):
        project = life.get(identifier)
        if body.model == "template":
            step = {"id": "extract", "target": "life.capture_material", "input": {"text": body.text}}
        else:
            if body.model == "mock":
                raise ValueError("请选择真实模型，或使用原文待核对模式")
            step = {"id": "extract", "kind": "model", "target": body.model, "input": {"capability": "decision",
                "messages": [{"role": "system", "content": "从材料提取可执行事项。每项保留原文 source，不能编造日期或承诺；缺失日期用 null、负责人用待分工。日期为 YYYY-MM-DD。参考项目主要日期：" + project["anchor"]},
                             {"role": "user", "content": body.text}], "response_schema": EXTRACTION_SCHEMA}}
        run_id = hub.submit({"name": "整理材料：" + project["title"], "metadata": {"life_project": identifier}, "steps": [step]})
        life.add_resource(identifier, Resource(kind="note", title="待整理的原始材料", value=body.text))
        return {"id": run_id}

    @app.post("/v1/life/projects/{identifier}/extract/confirm")
    async def confirm_extract(identifier: str, body: ExtractConfirm):
        return life.import_extract(identifier, body.run_id)

    @app.get("/v1/life/reminders")
    async def reminders(today: date | None = None):
        day = (today or date.today()).isoformat()
        return [{"project": p["title"], **t} for p in life.list() for t in p["tasks"] if t["state"] != "done" and t["due"] and t["due"] <= day]

    return life
