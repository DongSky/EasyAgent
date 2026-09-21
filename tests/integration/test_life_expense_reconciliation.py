"""Daily-life butler: check a shoebox of receipts against the spending plan before the month is gone.

The household hands the butler the receipts (vendor, amount, currency, date) and the budget it
planned to keep, and expects a total it can trust: only what can actually be counted, an explicit
list of what could not be counted and why, and a verdict instead of a quietly wrong number.
"""
import json
from decimal import Decimal, InvalidOperation

from easyagent.contracts import ToolSpec

RECEIPTS = {
    "week": [
        {"receipt_id": "r-001", "vendor": "morning-market", "amount": "4.35", "currency": "CNY", "date": "2026-09-14"},
        {"receipt_id": "r-002", "vendor": "neighbourhood-pharmacy", "amount": "12.35", "currency": "CNY", "date": "2026-09-15"},
        {"receipt_id": "r-003", "vendor": "power-utility", "amount": "31.15", "currency": "CNY", "date": "2026-09-16"},
        {"receipt_id": "r-004", "vendor": "corner-cafe", "amount": "n/a", "currency": "CNY", "date": "2026-09-17"},
        {"receipt_id": "r-005", "vendor": "airport-shop", "amount": "9.99", "currency": "USD", "date": "2026-09-18"},
        {"receipt_id": "r-006", "vendor": "bus-stop-kiosk", "amount": "", "currency": "CNY", "date": "2026-09-19"},
    ],
    # Five amounts add up to exactly 100.00 on paper, but as binary floats they add up to
    # 100.00000000000001: a float comparison against a 100.00 budget calls this overspending.
    "on_budget": [
        {"receipt_id": "r-101", "vendor": "school-canteen", "amount": "3.35", "currency": "CNY", "date": "2026-09-01"},
        {"receipt_id": "r-102", "vendor": "bookstore", "amount": "33.33", "currency": "CNY", "date": "2026-09-02"},
        {"receipt_id": "r-103", "vendor": "bus-pass", "amount": "15.55", "currency": "CNY", "date": "2026-09-03"},
        {"receipt_id": "r-104", "vendor": "chemist", "amount": "11.10", "currency": "CNY", "date": "2026-09-04"},
        {"receipt_id": "r-105", "vendor": "laundry", "amount": "16.66", "currency": "CNY", "date": "2026-09-05"},
        {"receipt_id": "r-106", "vendor": "phone-topup", "amount": "20.01", "currency": "CNY", "date": "2026-09-06"},
    ],
    "empty": [],
}


def plan(set_name, budget):
    """The workflow the butler runs: fetch the receipts, reconcile against the plan, file it."""
    return {
        "name": "reconcile receipts against the spending plan",
        "inputs": {"set": set_name, "budget": budget, "currency": "CNY"},
        "steps": [
            {"id": "fetch", "kind": "tool", "target": "household.receipts", "input": {"set": {"$ref": "$input.set"}}},
            {"id": "reconcile", "kind": "tool", "target": "household.reconcile", "depends_on": ["fetch"],
             "input": {"receipts": {"$ref": "fetch.receipts"}, "budget": {"$ref": "$input.budget"},
                       "currency": {"$ref": "$input.currency"}}},
            {"id": "report", "kind": "artifact", "depends_on": ["reconcile"],
             "input": {"name": "expense-report.json", "content": {"$ref": "reconcile"},
                       "media_type": "application/json"}},
        ],
    }


def line(receipt, amount):
    return {"receipt_id": receipt["receipt_id"], "vendor": receipt["vendor"], "amount": amount,
            "currency": receipt["currency"], "date": receipt["date"],
            "source": "household.receipts:" + receipt["receipt_id"]}


def register(hub, calls):
    async def receipts(args, ctx):
        calls.append(args)
        return {"receipts": [dict(r) for r in RECEIPTS[args["set"]]], "source": "household-receipt-drawer"}

    async def reconcile(args, ctx):
        counted, issues = [], []
        total = Decimal("0")
        for receipt in args["receipts"]:
            raw = receipt.get("amount")
            if receipt.get("currency") != args["currency"]:
                # A foreign-currency receipt cannot be added to this plan; say so, do not convert silently.
                issues.append({"kind": "currency_mismatch", "receipt_id": receipt["receipt_id"],
                               "vendor": receipt["vendor"], "amount": raw, "currency": receipt["currency"],
                               "expected_currency": args["currency"]})
                continue
            try:
                amount = Decimal(raw).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                issues.append({"kind": "unparseable_amount", "receipt_id": receipt["receipt_id"],
                               "vendor": receipt["vendor"], "raw": raw})
                continue
            total += amount
            counted.append(line(receipt, str(amount)))
        budget = Decimal(args["budget"])
        total = total.quantize(Decimal("0.01"))
        remaining = (budget - total).quantize(Decimal("0.01"))
        return {"currency": args["currency"], "budget": str(budget), "total": str(total),
                "remaining": str(remaining), "line_items": counted, "issues": issues,
                "verdict": "over_budget" if total > budget else "on_budget" if total == budget else "under_budget"}

    hub.tools.register(ToolSpec(name="household.receipts", effect="read",
                                description="Synthetic integration fixture: the household receipt drawer",
                                input_schema={"type": "object", "properties": {"set": {"enum": sorted(RECEIPTS)}},
                                              "required": ["set"], "additionalProperties": False}), receipts)
    hub.tools.register(ToolSpec(name="household.reconcile", effect="read",
                                description="Synthetic integration fixture: exact decimal reconciliation of receipts",
                                input_schema={"type": "object", "properties": {
                                    "receipts": {"type": "array"}, "budget": {"type": "string"},
                                    "currency": {"type": "string"}},
                                    "required": ["receipts", "budget", "currency"], "additionalProperties": False}), reconcile)


async def report_of(hub, set_name, budget):
    """Run the plan and return (run, stored reconcile output, artifact metadata, artifact JSON)."""
    run = await hub.wait(hub.submit(plan(set_name, budget)))
    assert run["status"] == "succeeded", run
    artifact = run["steps"][-1]["output"]
    info, content = hub.artifacts.get(artifact["id"])
    return run, run["steps"][1]["output"], info, json.loads(content)


async def test_life_expense_reconciliation_exact_total_verdict_and_unreadable_receipts(hub):
    """A household total this month's receipts against the budget it planned, and must hear about every
    receipt that could not be counted instead of a total that quietly swallows them."""
    calls = []
    register(hub, calls)

    # Over budget, with a foreign-currency receipt and two amounts the butler cannot read.
    run, stored, info, report = await report_of(hub, "week", "40.00")
    assert report == {
        "currency": "CNY", "budget": "40.00", "total": "47.85", "remaining": "-7.85", "verdict": "over_budget",
        "line_items": [line(RECEIPTS["week"][0], "4.35"), line(RECEIPTS["week"][1], "12.35"),
                       line(RECEIPTS["week"][2], "31.15")],
        "issues": [
            {"kind": "unparseable_amount", "receipt_id": "r-004", "vendor": "corner-cafe", "raw": "n/a"},
            {"kind": "currency_mismatch", "receipt_id": "r-005", "vendor": "airport-shop", "amount": "9.99",
             "currency": "USD", "expected_currency": "CNY"},
            {"kind": "unparseable_amount", "receipt_id": "r-006", "vendor": "bus-stop-kiosk", "raw": ""},
        ],
    }
    # The USD receipt is named, not summed: the exact total stays the three CNY receipts and no float
    # representation of them ("47.849999999999994") can pass this string comparison.
    assert report["total"] == "47.85" and Decimal("4.35") + Decimal("12.35") + Decimal("31.15") == Decimal("47.85")
    assert "airport-shop" not in [item["vendor"] for item in report["line_items"]]
    # The artifact is the report the household keeps, named and typed, and the persisted step agrees with it.
    assert (info["name"], info["media_type"]) == ("expense-report.json", "application/json")
    assert stored == report
    assert hub.artifacts.list(run["id"])[0]["id"] == info["id"]

    # Exactly on budget. These six amounts sum to 100.00 exactly in decimal; whatever a float sum
    # happens to give, the verdict must come from decimal arithmetic and land on "on_budget" with
    # nothing left over. Asserting what the float sum "should" be would only test this fixture's data.
    naive = sum(float(r["amount"]) for r in RECEIPTS["on_budget"])
    assert abs(naive - 100.0) < 0.01, "fixture amounts must be near a whole-unit boundary"
    _, _, _, on_budget = await report_of(hub, "on_budget", "100.00")
    assert (on_budget["total"], on_budget["remaining"], on_budget["verdict"]) == ("100.00", "0.00", "on_budget")
    assert on_budget["issues"] == [] and len(on_budget["line_items"]) == 6
    assert on_budget["line_items"][1] == line(RECEIPTS["on_budget"][1], "33.33")

    # No receipts yet is not an error: nothing spent, all of the plan still unspent.
    _, _, _, empty = await report_of(hub, "empty", "40.00")
    assert (empty["total"], empty["remaining"], empty["verdict"]) == ("0.00", "40.00", "under_budget")
    assert empty["line_items"] == [] and empty["issues"] == []

    # Every submit really did fetch its own drawer, and each report was filed once.
    assert calls == [{"set": "week"}, {"set": "on_budget"}, {"set": "empty"}]
    assert [e["payload"]["tool"] for e in hub.store.events(run["id"]) if e["kind"] == "tool.succeeded"] == \
        ["household.receipts", "household.reconcile"]
