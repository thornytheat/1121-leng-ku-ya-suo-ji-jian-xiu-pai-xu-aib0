"""历史工况。"""
from .service import evaluate

__all__ = ["evaluate"]

from .service import compare_references
__all__ += ["compare_references"]

from .adoption import (
    ADOPT_FIRST,
    ADOPT_NEW,
    KEEP_LATE,
    KEEP_MASKED,
    KEEP_NO_BASELINE,
    KEEP_NOT_READY,
    KEEP_STABLE,
    KEEP_UNVERIFIABLE,
    active_confirmed_episodes,
    adopt_references,
    masking_evidence,
    plan_adoption,
    score_patrol_batch,
)
__all__ += [
    "ADOPT_FIRST",
    "ADOPT_NEW",
    "KEEP_LATE",
    "KEEP_MASKED",
    "KEEP_NO_BASELINE",
    "KEEP_NOT_READY",
    "KEEP_STABLE",
    "KEEP_UNVERIFIABLE",
    "active_confirmed_episodes",
    "adopt_references",
    "masking_evidence",
    "plan_adoption",
    "score_patrol_batch",
]

from .adoption_service import evaluate_adoption
__all__ += ["evaluate_adoption"]
