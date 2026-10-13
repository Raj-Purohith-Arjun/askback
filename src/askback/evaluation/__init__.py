"""Evaluation modules. Importing this package registers every built-in evaluator.

The five modules from the architecture:
    rule_based   deterministic route / tool / status / fact / schema checks
    rag          expected documents, retrieval rank, citation support, groundedness
    safety       refusal behaviour, injection resistance, leakage, tool misuse
    llm_judge    semantic correctness, completeness, hallucination, reasoning, clarity
    regression   candidate run versus approved baseline (run level, not per case)

Plus `gates`, which is run level too: it turns a finished report into a verdict
and an exit code without deciding how to display it.
"""

from .base import Check, EvalResult, Evaluator
from .citation_eval import CitationExistenceEvaluator, Manifest, load_manifest
from .engine import DEFAULT_EVALUATORS, DEFAULT_WEIGHTS, EvaluationEngine
from .escalation_eval import EscalationEvaluator
from .gates import GateVerdict, Offender, RunVerdict, evaluate_gates
from .llm_judge import DIMENSIONS, HeuristicJudge, LLMJudgeEvaluator, ModelJudge
from .pii_eval import PIIEgressEvaluator
from .rag_eval import RagEvaluator, groundedness
from .regression import (
    Baseline,
    CaseDelta,
    RegressionComparator,
    RegressionReport,
    load_baseline,
    save_baseline,
)
from .results import CaseResult, RunReport, SampleResult, TurnResult, response_snapshot
from .rule_based import RuleBasedEvaluator, fact_present
from .safety_eval import SafetyEvaluator

__all__ = [
    "DEFAULT_EVALUATORS",
    "DEFAULT_WEIGHTS",
    "DIMENSIONS",
    "CaseDelta",
    "CitationExistenceEvaluator",
    "CaseResult",
    "SampleResult",
    "TurnResult",
    "Check",
    "Manifest",
    "EscalationEvaluator",
    "EvalResult",
    "EvaluationEngine",
    "Evaluator",
    "GateVerdict",
    "HeuristicJudge",
    "LLMJudgeEvaluator",
    "ModelJudge",
    "Offender",
    "PIIEgressEvaluator",
    "RagEvaluator",
    "RegressionComparator",
    "RegressionReport",
    "RuleBasedEvaluator",
    "Baseline",
    "RunReport",
    "RunVerdict",
    "SafetyEvaluator",
    "evaluate_gates",
    "fact_present",
    "groundedness",
    "load_baseline",
    "load_manifest",
    "response_snapshot",
    "save_baseline",
]
