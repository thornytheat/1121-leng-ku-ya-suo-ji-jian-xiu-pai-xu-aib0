"""告警基准采用策略的服务入口与逐行校验。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..robust_alarm.contracts import AlarmMeasure, InvalidRow as AlarmInvalidRow, parse as parse_measure
from .adoption import adopt_references, plan_adoption
from .algorithm import calculate as fit_references
from .contracts import DutyPoint, InvalidRow, parse as parse_duty


def validate_history(rows: Iterable[Mapping[str, Any]]) -> tuple[list[DutyPoint], list[dict]]:
    accepted: list[DutyPoint] = []
    rejected: list[dict] = []
    for line, raw in enumerate(rows, 1):
        try:
            accepted.append(parse_duty(raw))
        except InvalidRow as issue:
            rejected.append({'line': line, 'field': issue.field, 'code': issue.code})
    return accepted, rejected


def validate_patrol(rows: Iterable[Mapping[str, Any]]) -> tuple[list[AlarmMeasure], list[dict]]:
    accepted: list[AlarmMeasure] = []
    rejected: list[dict] = []
    for line, raw in enumerate(rows, 1):
        try:
            accepted.append(parse_measure(raw))
        except AlarmInvalidRow as issue:
            rejected.append({'line': line, 'field': issue.field, 'code': issue.code})
    return accepted, rejected


def evaluate_adoption(
    history_rows: Iterable[Mapping[str, Any]],
    patrol_rows: Iterable[Mapping[str, Any]],
    *,
    handoff_cutoff: int,
    candidate_cutoff: int,
    lookback: int = 360,
    group_min: int = 5,
    late_trained=(),
    field_recovered=(),
    duty='当班巡查',
    drift_limit: float = 1.8,
    overlap_cutoff: float = .25,
    alarm_options=None,
    episode_options=None,
) -> dict:
    """从原始历史与巡查记录出发，给出基准采用证据和本班实际基准。

    旧基准只读取 ``handoff_cutoff`` 之前的样本；候选基准在
    ``candidate_cutoff`` 上重训，可纳入交班后才到齐的迟到样本。两套基准在
    同一批已校验巡查读数上回放，逐机输出采用结论。
    """
    history, history_errors = validate_history(history_rows)
    patrol, patrol_errors = validate_patrol(patrol_rows)

    previous = fit_references(
        history, cutoff=handoff_cutoff, lookback=lookback, group_min=group_min
    )
    candidate = fit_references(
        history, cutoff=candidate_cutoff, lookback=lookback, group_min=group_min
    )
    plan = plan_adoption(
        previous,
        candidate,
        patrol,
        late_trained=late_trained,
        field_recovered=field_recovered,
        duty=duty,
        drift_limit=drift_limit,
        overlap_cutoff=overlap_cutoff,
        alarm_options=alarm_options,
        episode_options=episode_options,
    )
    adopted, plan = adopt_references(previous, candidate, patrol, plan=plan)
    return {
        'handoff_cutoff': handoff_cutoff,
        'candidate_cutoff': candidate_cutoff,
        'decisions': plan['decisions'],
        'adopted_references': adopted,
        'scored_under_previous': plan['scored_under_previous'],
        'scored_under_candidate': plan['scored_under_candidate'],
        'active_episodes_under_previous': plan['active_episodes_under_previous'],
        'active_episodes_under_candidate': plan['active_episodes_under_candidate'],
        'history_accepted_count': len(history),
        'patrol_accepted_count': len(patrol),
        'history_rejected_count': len(history_errors),
        'patrol_rejected_count': len(patrol_errors),
        'errors': history_errors + [
            {'line': item['line'], 'field': item['field'], 'code': item['code'], 'source': 'patrol'}
            for item in patrol_errors
        ],
    }
