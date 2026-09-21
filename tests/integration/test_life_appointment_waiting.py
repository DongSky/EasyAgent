"""Daily-life butler: "book the appointment and tell me when the outside party confirms".

The user asks the butler to request an appointment with an outside party (a clinic). The clinic
only hands back a *pending* reference. Until the clinic itself confirms, the butler must park the
task and tell the user nothing more: it must not invent a time, must not run the recording step,
and must keep the pending request intact across a process restart. Only once the fixture clinic
reports the confirmation does the task resume and record the confirmed time together with the
receipt it came from.
"""
import asyncio
import json

from easyagent.contracts import ToolSpec
from easyagent.runtime import Hub
from easyagent.tools import WaitingRemote

REQUEST = "clinic.request_appointment"
CONFIRMATION = "clinic.appointment_status"
RECORD = "home.record_appointment"
PENDING_REFERENCE = "APPT-4471"
CONFIRMED_TIME = "2026-09-24T10:30:00+08:00"
RECEIPT = "clinic-sms-7741"


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
            "additionalProperties": False}


async def until(hub, run_id, predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while True:
            run = hub.store.run(run_id)
            if predicate(run):
                return run
            await asyncio.sleep(.01)


def install_clinic(hub, state):
    """Fixture outside party. It accepts the request, then stays pending until the test flips the flag."""

    async def request_appointment(args, ctx):
        state["requests"].append(dict(args))
        return {"reference": PENDING_REFERENCE, "status": "pending", "source": "fixture-clinic-desk"}

    async def appointment_status(args, ctx):
        state["polls"] += 1
        if args["reference"] != PENDING_REFERENCE:
            raise ValueError("unknown appointment reference")
        if not state["confirmed"]:
            # Checkpointed polling: release the worker, consume no attempt, resume at delay.
            raise WaitingRemote(.02)
        return {"status": "confirmed", "time": CONFIRMED_TIME, "reference": PENDING_REFERENCE,
                "receipt": RECEIPT, "source": "fixture-clinic-sms"}

    async def record_appointment(args, ctx):
        state["recorded"].append(dict(args))
        return {"recorded": True, "confirmed_time": args["time"],
                "evidence": {"reference": args["reference"], "receipt": args["receipt"]}}

    hub.tools.register(ToolSpec(name=REQUEST, description="Synthetic fixture: ask the clinic for an appointment",
                                input_schema=schema({"subject": {"type": "string", "minLength": 1}}),
                                effect="write"), request_appointment)
    hub.tools.register(ToolSpec(name=CONFIRMATION, description="Synthetic fixture: poll the clinic for the confirmed time",
                                input_schema=schema({"reference": {"type": "string", "minLength": 1}}),
                                effect="read"), appointment_status)
    hub.tools.register(ToolSpec(name=RECORD, description="Synthetic fixture: keep the confirmed appointment at home",
                                input_schema=schema({"time": {"type": "string", "minLength": 1},
                                                     "reference": {"type": "string", "minLength": 1},
                                                     "receipt": {"type": "string", "minLength": 1}}),
                                effect="local"), record_appointment)


def appointment_workflow():
    return {"name": "clinic appointment", "steps": [
        {"id": "request", "kind": "tool", "target": REQUEST, "input": {"subject": "dental cleaning"}},
        {"id": "wait", "kind": "tool", "target": CONFIRMATION, "depends_on": ["request"],
         "input": {"reference": {"$ref": "request.reference"}}},
        {"id": "record", "kind": "tool", "target": RECORD, "depends_on": ["wait"],
         "input": {"time": {"$ref": "wait.time"}, "reference": {"$ref": "wait.reference"},
                   "receipt": {"$ref": "wait.receipt"}}},
    ]}


async def park_on_third_party(hub, run_id):
    """Approve the outbound request, then wait until the polling step parks on the clinic."""
    pending = await hub.wait(run_id)
    assert pending["status"] == "waiting_approval", pending
    for approval in pending["approvals"]:
        hub.tools.approve(hub.store, approval["id"], True)
    return await until(hub, run_id, lambda run: run["steps"][1]["status"] == "waiting_remote")


async def test_pending_appointment_waits_and_records_only_real_confirmation(hub):
    state = {"requests": [], "recorded": [], "confirmed": False, "polls": 0}
    install_clinic(hub, state)
    run_id = hub.submit(appointment_workflow())

    run = await park_on_third_party(hub, run_id)

    # The clinic only acknowledged the request; nothing is confirmed and nothing was recorded.
    assert state["requests"] == [{"subject": "dental cleaning"}]
    assert state["confirmed"] is False and state["recorded"] == []
    assert run["status"] not in ("succeeded", "failed", "cancelled", "needs_attention")
    steps = {step["id"]: step for step in run["steps"]}
    assert steps["request"]["status"] == "succeeded"
    assert steps["wait"]["status"] == "waiting_remote" and steps["wait"]["output"] is None
    assert steps["record"]["status"] == "queued" and steps["record"]["output"] is None
    kinds = [event["kind"] for event in hub.store.events(run_id)]
    assert "tool.approval_required" in kinds and "step.waiting_remote" in kinds
    # Neither the confirmed time nor the receipt exists in any durable record while pending.
    assert CONFIRMED_TIME not in json.dumps(run) and RECEIPT not in json.dumps(run)

    state["confirmed"] = True
    complete = await hub.wait(run_id)
    assert complete["status"] == "succeeded", complete
    assert state["polls"] >= 2  # polled while pending, then again once the clinic confirmed
    steps = {step["id"]: step for step in complete["steps"]}
    assert steps["wait"]["output"] == {"status": "confirmed", "time": CONFIRMED_TIME,
                                       "reference": PENDING_REFERENCE, "receipt": RECEIPT,
                                       "source": "fixture-clinic-sms"}
    assert steps["record"]["output"] == {"recorded": True, "confirmed_time": CONFIRMED_TIME,
                                         "evidence": {"reference": PENDING_REFERENCE, "receipt": RECEIPT}}
    assert state["recorded"] == [{"time": CONFIRMED_TIME, "reference": PENDING_REFERENCE, "receipt": RECEIPT}]
    # Waiting, polling and approving never sent the request to the clinic a second time.
    assert state["requests"] == [{"subject": "dental cleaning"}]


async def test_restart_mid_wait_preserves_pending_appointment_and_can_finish(tmp_path):
    state = {"requests": [], "recorded": [], "confirmed": False, "polls": 0}
    database = tmp_path / "appointment.db"
    first = Hub(database, poll_seconds=.01, lease_seconds=10)
    await first.start()
    try:
        install_clinic(first, state)
        run_id = first.submit(appointment_workflow())
        parked = await park_on_third_party(first, run_id)
        assert parked["steps"][1]["status"] == "waiting_remote"
        assert parked["steps"][1]["output"] is None and state["recorded"] == []
    finally:
        await first.stop()

    # A fresh process over the same file still sees the parked request, not a confirmation.
    second = Hub(database, poll_seconds=.01, lease_seconds=10)
    resumed = second.store.run(run_id)
    assert resumed["status"] not in ("succeeded", "failed", "cancelled")
    assert resumed["steps"][1]["status"] == "waiting_remote" and resumed["steps"][1]["output"] is None
    assert resumed["steps"][2]["status"] == "queued" and state["recorded"] == []

    install_clinic(second, state)
    state["confirmed"] = True
    await second.start()
    try:
        complete = await second.wait(run_id)
    finally:
        await second.stop()
    assert complete["status"] == "succeeded", complete
    assert complete["steps"][2]["output"]["confirmed_time"] == CONFIRMED_TIME
    assert complete["steps"][2]["output"]["evidence"] == {"reference": PENDING_REFERENCE, "receipt": RECEIPT}
    assert state["recorded"] == [{"time": CONFIRMED_TIME, "reference": PENDING_REFERENCE, "receipt": RECEIPT}]
    # The restart re-drove the workflow but never re-sent the already-approved outbound request.
    assert state["requests"] == [{"subject": "dental cleaning"}]
