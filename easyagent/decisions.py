"""Decision providers: Jev (TypeSafe System One) via the workspace ``jev`` CLI.

Access method (per workspace skill ``typesafe``):
    ``~/workspace/skills/typesafe/bin/jev`` -- a subprocess CLI that performs
    the System One call. Authentication uses the vault surrogate mechanism
    *inside* the CLI; this module never touches a raw key, never sets a secret
    environment variable, and never writes an auth file.

- :class:`DecisionProvider`: ``decide(q: DecisionQuestion) -> Decision``.
- :class:`JevProvider`: subprocess call to the CLI. Any CLI failure, timeout,
  or unavailability raises :class:`ProviderUnavailable` (the caller degrades).
- :class:`FallbackProvider`: Hermes main model (``llm.py`` ModelClient) with a
  strict self-judgment prompt, parsing yes/no/choice/score.
- :class:`MockProvider`: canned decisions for offline smoke tests.
- :func:`make_provider`: ``JEV_ENABLED`` (default 1) and a usable CLI ->
  :class:`JevProvider`, otherwise :class:`FallbackProvider`.
- Four gates: ``risk_gate``, ``gap_triage``, ``completion_score``,
  ``plugin_judge``.

Logging: info level only; question ids and model id may be logged, never the
request body or any credential material.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# --- contracts (canonical home is easyagent/contracts.py; local fallback keeps
# this module importable before that worker lands) ---
try:  # pragma: no cover - depends on sibling worker ordering
    from .contracts import Decision as _ContractDecision
    from .contracts import DecisionQuestion as _ContractDecisionQuestion

    DecisionQuestion = _ContractDecisionQuestion
    Decision = _ContractDecision
    _USING_CONTRACTS = True
except ImportError:  # contracts.py not delivered yet

    @dataclass
    class DecisionQuestion:  # type: ignore[no-redef]
        kind: str  # noul | choice | score
        question: str
        options: list[str] | None = None
        state: dict = field(default_factory=dict)

    @dataclass
    class Decision:  # type: ignore[no-redef]
        answer: str | bool | float
        probability: float
        provider: str

    _USING_CONTRACTS = False


class ProviderUnavailable(Exception):
    """The decision provider cannot serve right now; caller should degrade."""


def _default_cli() -> str:
    return os.environ.get(
        "JEV_CLI", str(Path.home() / "workspace" / "skills" / "typesafe" / "bin" / "jev")
    )


class DecisionProvider:
    def decide(self, q: DecisionQuestion) -> Decision:
        criteria = _default_criteria(q.kind, q.options)
        state = q.state if q.state else q.question
        return self.decide_raw(q.kind, q.question, criteria, state)

    def decide_raw(self, kind: str, instructions: str, criteria, state) -> Decision:
        """Single decision with explicit Jev criteria (noul/choice/score)."""
        return self._single(kind, instructions, criteria, state)

    def _single(self, kind: str, instructions: str, criteria, state) -> Decision:
        raise NotImplementedError

    def decide_many(self, questions: dict[str, DecisionQuestion]) -> dict[str, Decision]:
        return {qid: self.decide(q) for qid, q in questions.items()}

    def decide_many_raw(self, items: dict[str, tuple[str, str, object, object]]) -> dict[str, Decision]:
        """Batch: {qid: (kind, instructions, criteria, state)} in one CLI call."""
        raise NotImplementedError

    @property
    def last_usage(self) -> dict | None:  # for loop.py budget accounting
        return None


def _default_criteria(kind: str, options: list[str] | None):
    if kind == "noul":
        return {
            "yes": "the answer is yes / the statement is true",
            "no": "the answer is no / the statement is false",
        }
    if kind == "choice":
        return {o: o for o in (options or [])}
    if kind == "score":
        return list(_COMPLETION_CRITERIA)
    return {}


# ---------------------------------------------------------------- Jev via CLI

_RISK_CRITERIA = {
    "risky": "could damage the system, data, or the environment; destructive or hard to reverse",
    "safe": "read-only or trivially reversible, no destructive effects",
}
_TRIAGE_CRITERIA = {
    "explore": "try to discover and build the missing capability now",
    "skip": "ignore the gap and proceed without the capability",
    "ask": "ask a human how to handle the gap",
}
_COMPLETION_CRITERIA = [
    "0: not started / total failure",
    "1: barely begun",
    "2: partial progress",
    "3: mostly done, usable result",
    "4: complete, minor polish missing",
    "5: fully complete, verified",
]
_JUDGE_CRITERIA = {
    "promote": "all fixtures pass; code is safe, correct, and matches the manifest",
    "reject": "fixtures fail, or the code is unsafe or incorrect",
}


class JevProvider(DecisionProvider):
    """TypeSafe System One via the workspace ``jev`` CLI (subprocess).

    Auth is handled inside the CLI via the vault surrogate; no key material
    ever appears in this process's environment, argv, files, or logs.
    """

    def __init__(
        self,
        cli: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ):
        self.cli = cli or _default_cli()
        self.model = model or os.environ.get("JEV_MODEL", "jev-latest")
        self.timeout = float(os.environ.get("JEV_TIMEOUT", timeout))
        if not (os.path.isfile(self.cli) and os.access(self.cli, os.X_OK)):
            raise ProviderUnavailable(f"jev CLI not usable: {self.cli}")
        self._last_usage: dict | None = None

    @property
    def last_usage(self) -> dict | None:
        return self._last_usage

    # -- subprocess plumbing -------------------------------------------------
    def _invoke(self, state, questions: dict) -> tuple[dict, dict]:
        """Call the CLI once; return (answers, usage). Never logs the body."""
        qjson = json.dumps(questions, ensure_ascii=False)
        state_json = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        cmd = [self.cli, "--model", self.model, "--timeout", str(self.timeout)]
        tmp_files: list[str] = []
        try:
            if len(state_json) + len(qjson) < 6000:
                cmd += ["--state", state_json, "--questions", qjson]
            else:
                for payload, suffix in ((state_json, ".state"), (qjson, ".questions")):
                    fh = tempfile.NamedTemporaryFile(
                        "w", suffix=suffix + ".json", delete=False, dir="/tmp"
                    )
                    fh.write(payload)
                    fh.close()
                    tmp_files.append(fh.name)
                cmd += ["--state-file", tmp_files[0], "--questions-file", tmp_files[1]]
            log.info(
                "jev decision: model=%s questions=%s", self.model, sorted(questions)
            )
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout + 15
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise ProviderUnavailable(f"jev CLI failed: {type(exc).__name__}") from exc
            if proc.returncode != 0:
                # stderr may contain an HTTP status line only; never echo it fully.
                raise ProviderUnavailable(
                    f"jev CLI exited with status {proc.returncode}"
                )
            try:
                data = json.loads(proc.stdout)
            except ValueError as exc:
                raise ProviderUnavailable("jev CLI returned non-JSON output") from exc
            answers = data.get("answers")
            if not isinstance(answers, dict):
                raise ProviderUnavailable("jev CLI response has no answers map")
            self._last_usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            return answers, self._last_usage or {}
        finally:
            for p in tmp_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # -- question mapping ----------------------------------------------------
    def _single(self, kind: str, instructions: str, criteria, state) -> Decision:
        payload = {"q": {"type": kind, "instructions": instructions, "criteria": criteria}}
        answers, _ = self._invoke(state, payload)
        raw = answers.get("q")
        if not isinstance(raw, dict):
            raise ProviderUnavailable("jev CLI answer missing for question")
        return self._parse_answer(kind, raw)

    def decide_many_raw(self, items: dict[str, tuple[str, str, object, object]]) -> dict[str, Decision]:
        payload = {
            qid: {"type": kind, "instructions": instr, "criteria": crit}
            for qid, (kind, instr, crit, _state) in items.items()
        }
        states = {
            json.dumps(s, sort_keys=True, default=str)
            for (_, _, _, s) in items.values()
        }
        if len(states) == 1:
            state = next(iter(items.values()))[3]
        else:
            state = {qid: s for qid, (_, _, _, s) in items.items()}
        answers, _ = self._invoke(state, payload)
        out = {}
        for qid, (kind, _instr, _crit, _state) in items.items():
            raw = answers.get(qid)
            if not isinstance(raw, dict):
                raise ProviderUnavailable(f"jev CLI answer missing for {qid}")
            out[qid] = self._parse_answer(kind, raw)
        return out

    def decide_many(self, questions: dict[str, DecisionQuestion]) -> dict[str, Decision]:
        items = {
            qid: (
                q.kind,
                q.question,
                _default_criteria(q.kind, q.options),
                q.state if q.state else q.question,
            )
            for qid, q in questions.items()
        }
        return self.decide_many_raw(items)

    @staticmethod
    def _parse_answer(kind: str, raw: dict) -> Decision:
        if kind == "noul":
            p = float(raw.get("noul", 0.5))
            return Decision(answer=(p >= 0.5), probability=p, provider="jev")
        if kind == "choice":
            choice = str(raw.get("choice", ""))
            probs = raw.get("probabilities") or {}
            p = float(probs.get(choice, raw.get("confidence", 0.5)))
            return Decision(answer=choice, probability=p, provider="jev")
        if kind == "score":
            s = float(raw.get("score", 0.0))
            p = float(raw.get("confidence", 0.5))
            return Decision(answer=max(0.0, min(5.0, s)), probability=p, provider="jev")
        raise ProviderUnavailable(f"unknown question kind: {kind}")


# ------------------------------------------------------- Fallback (Hermes LLM)

_FALLBACK_SYSTEM = (
    "You are a strict decision judge. Answer ONLY with the requested format, "
    "no explanation, no extra text."
)


class FallbackProvider(DecisionProvider):
    """Self-judgment via the Hermes main model (llm.py) with a strict prompt."""

    def __init__(self, client=None):
        if client is None:
            from .llm import make_client

            client = make_client()
        self.client = client

    def _ask(self, system_extra: str, user: str) -> str:
        try:
            out = self.client.chat(
                [
                    {"role": "system", "content": _FALLBACK_SYSTEM + " " + system_extra},
                    {"role": "user", "content": user},
                ]
            )
        except Exception as exc:
            raise ProviderUnavailable(f"fallback LLM failed: {type(exc).__name__}") from exc
        return (out.get("content") or "").strip().lower()

    def _single(self, kind: str, instructions: str, criteria, state) -> Decision:
        state_s = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        try:
            if kind == "noul":
                text = self._ask(
                    'Reply with exactly "yes" or "no".',
                    f"Question: {instructions}\nContext: {state_s}\nAnswer (yes/no):",
                )
                yes = text.startswith("yes")
                return Decision(answer=yes, probability=0.8 if yes else 0.2, provider="fallback")
            if kind == "choice":
                options = list(criteria.keys()) if isinstance(criteria, dict) else []
                text = self._ask(
                    f'Reply with exactly one of: {", ".join(options)}.',
                    f"Question: {instructions}\nContext: {state_s}\nAnswer:",
                )
                pick = next((o for o in options if o in text), options[0] if options else "")
                return Decision(answer=pick, probability=0.6, provider="fallback")
            if kind == "score":
                text = self._ask(
                    "Reply with exactly one number from 0 to 5.",
                    f"Question: {instructions}\nContext: {state_s}\nAnswer (0-5):",
                )
                import re as _re

                m = _re.search(r"[0-5](?:\.\d+)?", text)
                s = float(m.group(0)) if m else 0.0
                return Decision(answer=max(0.0, min(5.0, s)), probability=0.6, provider="fallback")
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"fallback parse failed: {type(exc).__name__}") from exc
        raise ProviderUnavailable(f"unknown question kind: {kind}")

    def decide_many_raw(self, items: dict[str, tuple[str, str, object, object]]) -> dict[str, Decision]:
        return {qid: self._single(kind, instr, crit, s) for qid, (kind, instr, crit, s) in items.items()}


# ------------------------------------------------------------------ Mock

class MockProvider(DecisionProvider):
    """Canned decisions for offline smoke tests (no network, no cost)."""

    def _single(self, kind: str, instructions: str, criteria, state) -> Decision:
        if kind == "noul":
            return Decision(answer=True, probability=0.9, provider="mock")
        if kind == "choice":
            opts = list(criteria.keys()) if isinstance(criteria, dict) else ["explore"]
            return Decision(answer=opts[0], probability=0.8, provider="mock")
        if kind == "score":
            return Decision(answer=4.0, probability=0.75, provider="mock")
        raise ProviderUnavailable(f"unknown question kind: {kind}")

    def decide_many_raw(self, items: dict[str, tuple[str, str, object, object]]) -> dict[str, Decision]:
        return {qid: self._single(kind, instr, crit, s) for qid, (kind, instr, crit, s) in items.items()}


# ---------------------------------------------------------------- factory

def make_provider() -> DecisionProvider:
    """JEV_ENABLED (default 1) and a usable ``jev`` CLI -> JevProvider.

    Otherwise FallbackProvider. Callers wrap ``decide()`` in
    ``try/except ProviderUnavailable`` and degrade to the fallback.
    """
    enabled = os.environ.get("JEV_ENABLED", "1") not in ("0", "false", "no", "")
    if enabled:
        try:
            return JevProvider()
        except ProviderUnavailable as exc:
            log.info("jev CLI unavailable (%s); using fallback provider", exc)
    return FallbackProvider()


def _provider_or_default(provider: DecisionProvider | None) -> DecisionProvider:
    return provider if provider is not None else make_provider()


# ------------------------------------------------------------------ gates

RISK_THRESHOLD = float(os.environ.get("EASYAGENT_RISK_THRESHOLD", "0.5"))

_RISK_QUESTION = (
    "Is this tool call risky — could it damage the system, data, or the "
    "environment — and should it require human approval before running?"
)
_TRIAGE_QUESTION = (
    "A capability gap was found: the agent lacks a tool for the need below. "
    "What should be done with this need?"
)
_COMPLETION_QUESTION = (
    "Rate how completely the mission described below was achieved, on a 0-5 "
    "scale where 0 is not started and 5 is fully complete and verified."
)
_JUDGE_QUESTION = (
    "Does this plugin meet the promotion standard — all fixtures pass, code "
    "is safe and correct — and should it be promoted to active?"
)


def risk_gate(tool_name: str, args: dict, provider: DecisionProvider | None = None) -> bool:
    """noul: is this tool call risky? P(risky) >= threshold -> human approval."""
    d = _provider_or_default(provider).decide_raw(
        "noul", _RISK_QUESTION, dict(_RISK_CRITERIA),
        {"tool_name": tool_name, "args": args},
    )
    return bool(d.answer) and d.probability >= RISK_THRESHOLD


def gap_triage(need: str, provider: DecisionProvider | None = None) -> str:
    """choice: explore | skip | ask for a capability gap."""
    d = _provider_or_default(provider).decide_raw(
        "choice", _TRIAGE_QUESTION, dict(_TRIAGE_CRITERIA), {"need": need}
    )
    return str(d.answer)


def completion_score(summary: str, provider: DecisionProvider | None = None) -> float:
    """score: mission completeness on a 0..5 scale."""
    d = _provider_or_default(provider).decide_raw(
        "score", _COMPLETION_QUESTION, list(_COMPLETION_CRITERIA),
        {"summary": summary},
    )
    return float(d.answer)


def plugin_judge(
    name: str, fixture_report: dict, provider: DecisionProvider | None = None
) -> bool:
    """noul: does the plugin meet the promotion standard?"""
    d = _provider_or_default(provider).decide_raw(
        "noul", _JUDGE_QUESTION, dict(_JUDGE_CRITERIA),
        {"plugin": name, "fixture_report": fixture_report},
    )
    return bool(d.answer) and d.probability >= 0.5
