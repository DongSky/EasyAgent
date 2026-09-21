"""A parent turns a school notice into dated, owned action items without inventing missing details."""

import json

from easyagent.contracts import ModelResult
from examples.scenarios.world import workflow


NOTICE = (
    "关于收集社会实践报名表的通知\n"
    "\n"
    "各位家长：\n"
    "本学期的社会实践活动已开始报名，请家长在 9 月 25 日前把填好的《社会实践报名表》交给班主任王老师。\n"
    "10 月 12 日上午 8:30 在社区活动中心集合，请给孩子准备水壶和运动鞋。\n"
    "本次活动需要若干家长志愿者，具体人选由家委会商量后另行通知。\n"
    "如有疑问请拨打 12345 咨询。\n"
    "\n"
    "三年级(2)班 家委会\n"
    "9 月 15 日"
)

# The guard against invention is expressed in the contract itself: an item may only omit its date
# and owner when it is explicitly flagged as unconfirmed.
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "due_date": {"type": ["string", "null"]},
                    "owner": {"type": ["string", "null"]},
                    "needs_confirmation": {"type": "boolean"},
                },
                "required": ["title", "due_date", "owner", "needs_confirmation"],
                "additionalProperties": False,
                "allOf": [
                    {
                        "if": {
                            "properties": {"needs_confirmation": {"const": False}},
                            "required": ["needs_confirmation"],
                        },
                        "then": {
                            "properties": {
                                "due_date": {"type": "string"},
                                "owner": {"type": "string", "minLength": 1},
                            }
                        },
                    }
                ],
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

EXTRACTED = {
    "items": [
        {"title": "提交《社会实践报名表》", "due_date": "2026-09-25", "owner": "王老师(班主任)", "needs_confirmation": False},
        {"title": "带孩子参加社会实践活动", "due_date": "2026-10-12", "owner": "家长", "needs_confirmation": False},
        {"title": "安排家长志愿者", "due_date": None, "owner": None, "needs_confirmation": True},
    ]
}


def notice_workflow(model, max_attempts=3):
    return {
        "name": "通知 → 有日期与负责人的行动项",
        "metadata": {"family": "life-butler", "synthetic": True},
        "inputs": {"notice": NOTICE},
        "steps": [
            {"id": "notice", "kind": "artifact", "input": {"name": "notice.txt", "media_type": "text/plain",
                                                           "content": {"$ref": "$input.notice"}}},
            {"id": "extract", "kind": "model", "target": model, "depends_on": ["notice"], "max_attempts": max_attempts,
             "input": {"messages": [
                 {"role": "system", "content": "只依据通知原文整理行动项；通知没有写明的日期或负责人必须留空并标记 needs_confirmation。"},
                 {"role": "user", "content": {"$ref": "$input.notice"}}],
                 "response_schema": SCHEMA}},
            {"id": "echo", "target": "core.echo", "depends_on": ["extract"],
             "input": {"notice": {"$ref": "$input.notice"}, "notice_artifact": {"$ref": "notice.name"},
                       "first_title": {"$ref": "extract.data.items.0.title"}}},
            {"id": "actions", "kind": "artifact", "depends_on": ["extract"],
             "input": {"name": "action_items.json", "media_type": "application/json",
                       "content": {"$ref": "extract.data"}}},
        ],
    }


class Extractor:
    def __init__(self):
        self.requests = []

    async def generate(self, request, model):
        self.requests.append(request)
        return ModelResult(data=EXTRACTED, usage={"mock": True})


async def test_notice_becomes_dated_owned_action_items_and_preserves_the_source(hub):
    extractor = Extractor()
    hub.models.register("notice-extractor", extractor, "fixture-notice-extractor", ["chat"])

    run = await hub.wait(hub.submit(notice_workflow("notice-extractor")))
    assert run["status"] == "succeeded", run
    assert len(extractor.requests) == 1
    request = extractor.requests[0]
    # The extraction saw the real notice text and the declared contract, not a paraphrase.
    assert [m["content"] for m in request.messages if m["role"] == "user"] == [NOTICE]
    assert request.response_schema == SCHEMA
    assert request.capability == "chat"

    artifacts = {a["name"]: a for a in hub.artifacts.list(run["id"])}
    assert set(artifacts) == {"notice.txt", "action_items.json"}

    info, preserved = hub.artifacts.get(artifacts["notice.txt"]["id"])
    assert info["media_type"] == "text/plain"
    assert preserved.decode() == NOTICE
    assert run["spec"]["inputs"]["notice"] == NOTICE

    info, encoded = hub.artifacts.get(artifacts["action_items.json"]["id"])
    assert info["media_type"] == "application/json"
    payload = json.loads(encoded)
    assert [item["title"] for item in payload["items"]] == [
        "提交《社会实践报名表》", "带孩子参加社会实践活动", "安排家长志愿者"]

    for item in payload["items"]:
        assert set(item) == {"title", "due_date", "owner", "needs_confirmation"}
        # Every item either is dated and owned, or says outright that it still needs confirming.
        assert item["needs_confirmation"] is True or (item["due_date"] and item["owner"])
    dated = payload["items"][0]
    assert (dated["due_date"], dated["owner"]) == ("2026-09-25", "王老师(班主任)")
    unconfirmed = payload["items"][2]
    assert unconfirmed["needs_confirmation"] is True and unconfirmed["due_date"] is None and unconfirmed["owner"] is None
    # That blankness is faithful: the notice defers the volunteer roster to a later announcement.
    assert "由家委会商量后另行通知" in NOTICE and "志愿者" in unconfirmed["title"]

    steps = {s["id"]: s for s in run["steps"]}
    assert steps["echo"]["output"]["first_title"] == "提交《社会实践报名表》"
    assert steps["echo"]["output"]["notice"] == NOTICE
    events = hub.store.events(run["id"])
    assert [e["kind"] for e in events].count("artifact.created") == 2

    class Inventing:
        async def generate(self, request, model):
            return ModelResult(data={"items": [
                {"title": "缴纳活动费", "due_date": None, "owner": None, "needs_confirmation": False}]},
                usage={"mock": True})

    hub.models.register("inventing-extractor", Inventing(), "fixture-inventing", ["chat"])
    rejected = await hub.wait(hub.submit(notice_workflow("inventing-extractor", max_attempts=1)))
    assert rejected["status"] == "failed", rejected
    assert "is not of type 'string'" in rejected["steps"][1]["error"], rejected["steps"][1]["error"]
    produced = {a["name"] for a in hub.artifacts.list(rejected["id"])}
    assert produced == {"notice.txt"}, produced


async def test_shipped_life_notice_example_prepares_and_runs_end_to_end(hub):
    definition = workflow("life_notice_action_items")
    prepared = hub.prepare(definition)
    assert [s.id for s in prepared.steps] == ["notice", "echo", "items", "actions"]
    for step in prepared.steps:
        if step.target:
            # A reader can submit the shipped example without registering any fixture tool.
            assert hub.tools.spec(step.target, step.tool_revision) is not None

    run = await hub.wait(hub.submit(definition))
    assert run["status"] == "succeeded", run
    artifacts = {a["name"]: a for a in hub.artifacts.list(run["id"])}
    assert set(artifacts) == {"notice.txt", "action_items.json"}
    _, preserved = hub.artifacts.get(artifacts["notice.txt"]["id"])
    assert preserved.decode() == definition["inputs"]["notice"]
    _, encoded = hub.artifacts.get(artifacts["action_items.json"]["id"])
    payload = json.loads(encoded)
    assert payload["source_notice"] == definition["inputs"]["notice"]
    assert [(i["due_date"], i["owner"], i["needs_confirmation"]) for i in payload["items"]] == [
        ("2026-09-25", "王老师(班主任)", False),
        ("2026-10-12", "家长", False),
        (None, None, True),
    ]
