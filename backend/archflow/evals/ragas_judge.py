"""RAGAS metrics judged through ArchFlow's LLM gateway (optional and kept out of the lockfile; see ``runner``).

``GatewayLLM`` adapts the gateway to RAGAS's ``BaseRagasLLM`` so the judge uses the same providers, keys,
retries and usage accounting (feature ``eval``) as the product.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from .. import llm

log = logging.getLogger("archflow.evals")
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def available() -> bool:
    try:
        import ragas  # noqa: F401
    except ImportError:
        return False
    return True


def _judge_class():
    from langchain_core.outputs import Generation, LLMResult
    from ragas.llms.base import BaseRagasLLM

    @dataclass
    class GatewayLLM(BaseRagasLLM):
        group: str = "fast"
        org_id: str | None = None

        def generate_text(self, prompt: Any, n: int = 1, temperature: float = 0.01, stop: Any = None,
                          callbacks: Any = None) -> LLMResult:
            text = prompt.to_string() if hasattr(prompt, "to_string") else str(prompt)
            with llm.usage_scope(feature="eval", prompt="ragas.judge"):
                out, _ = llm.complete_tools("", [{"role": "user", "content": text}], [], group=self.group,
                                            org_id=self.org_id, max_tokens=8000)  # thinking models need room
            m = _FENCE.match(out or "")  # RAGAS parses JSON; models often wrap it in a code fence
            out = m.group(1) if m else out
            return LLMResult(generations=[[Generation(text=out) for _ in range(max(1, n))]])

        async def agenerate_text(self, prompt: Any, n: int = 1, temperature: float | None = 0.01, stop: Any = None,
                                 callbacks: Any = None) -> LLMResult:
            return await asyncio.to_thread(self.generate_text, prompt, n, temperature or 0.01, stop, callbacks)

        def is_finished(self, response: LLMResult) -> bool:
            return True

    return GatewayLLM


def judge(group: str = "fast", org_id: str | None = None) -> Any:
    return _judge_class()(group=group, org_id=org_id)


CRITICS = {
    "technically_sound": "Does the response give technically sound software-architecture advice for the "
                         "components it names (no nonsensical pairings such as a CDN in front of a database)?",
    "grounded_in_project": "Does the response refer to the project's existing components and requirement IDs by "
                           "their real names, without presenting anything as already existing that is not in the "
                           "given project data? Proposing new components to add is fine.",
}


async def _score(metrics: dict[str, Any], sample: Any) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for name, metric in metrics.items():
        try:
            value = await metric.single_turn_ascore(sample)
            out[name] = round(float(value), 3) if value is not None else None
        except Exception as exc:  # noqa: BLE001 - one failing metric must not lose the others
            log.warning("ragas metric %s failed: %s", name, exc)
            out[name] = None
    return out


METRICS = ("faithfulness", "answer_accuracy", *CRITICS)


def score(question: str, response: str, contexts: list[str], *, reference: str = "",
          metrics_wanted: list[str] | None = None, judge_llm: Any = None) -> dict[str, float | None]:
    """RAGAS scores (0-1) for one answer. ``metrics_wanted`` (default: all that apply): "faithfulness" (also adds
    response groundedness) against ``contexts``, "answer_accuracy" against ``reference``, and aspect critics
    (``CRITICS``), which see the contexts too."""
    wanted = set(metrics_wanted if metrics_wanted is not None else METRICS)
    from ragas.dataset_schema import SingleTurnSample
    from ragas.metrics import AnswerAccuracy, AspectCritic, Faithfulness, ResponseGroundedness
    from ragas.metrics.base import MetricType

    j = judge_llm or judge()
    metrics: dict[str, Any] = {}
    if contexts and "faithfulness" in wanted:
        metrics["faithfulness"] = Faithfulness(llm=j)
        metrics["groundedness"] = ResponseGroundedness(llm=j)
    if reference and "answer_accuracy" in wanted:
        metrics["answer_accuracy"] = AnswerAccuracy(llm=j)
    for c in (c for c in CRITICS if c in wanted):
        # Critics see the project data too, so "grounded" and "sound for these components" can be judged.
        cols = {"user_input", "response", *(["retrieved_contexts"] if contexts else [])}
        metrics[c] = AspectCritic(name=c, definition=CRITICS.get(c, c), llm=j,
                                  required_columns={MetricType.SINGLE_TURN: cols})
    sample = SingleTurnSample(user_input=question, response=response, retrieved_contexts=contexts or None,
                              reference=reference or None)
    return asyncio.run(_score(metrics, sample))
