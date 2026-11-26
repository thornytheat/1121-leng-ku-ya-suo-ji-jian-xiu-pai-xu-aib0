"""告警基准采用策略。

历史工况页面在交班时为每台机组定下本班实际采用的告警基准。旧基准（交班
前训练）与候选新基准（纳入最新样本后重训）必须用同一批巡查读数回放评分和
持续告警，再逐机决定：

* 候选基准在交班时点训练量不足 -> 保留旧基准；迟到样本在交班后才补足训练量
  时同样保留旧基准（``LATE_TRAINING_VOLUME``）。
* 工况确已漂移且换新不会让现场尚未确认恢复的持续告警消失 -> 采用新基准。
* 新基准会盖住仍未解除的持续告警 -> 保留旧基准（``UNRESOLVED_ALARM_MASKED``）。
* 同批其他机组待复核不影响已证实漂移且不丢告警的机组照常换新。

本班下游的评分与持续告警统一使用 :func:`adopt_references` 返回的实际基准。
"""
from __future__ import annotations

from collections import defaultdict

from ..robust_alarm.algorithm import calculate as score_readings
from .reference_comparison import compare_references

# 逐机采用结论
ADOPT_NEW = 'ADOPT_NEW_BASELINE'
ADOPT_FIRST = 'ADOPT_FIRST_BASELINE'
KEEP_STABLE = 'NO_DRIFT_DETECTED'
KEEP_MASKED = 'UNRESOLVED_ALARM_MASKED'
KEEP_LATE = 'LATE_TRAINING_VOLUME'
KEEP_NOT_READY = 'NEW_BASELINE_NOT_READY'
KEEP_UNVERIFIABLE = 'UNVERIFIABLE_DRIFT'
KEEP_NO_BASELINE = 'NO_USABLE_BASELINE'

KEEP_REASONS = frozenset({
    KEEP_STABLE, KEEP_MASKED, KEEP_LATE, KEEP_NOT_READY, KEEP_UNVERIFIABLE, KEEP_NO_BASELINE,
})

# compare_references 中表示工况确已漂移、可以据此换新的标记
_DRIFT_STATUS = 'REFERENCE_DRIFT'


def _fit_index(records):
    return {row['unit']: row for row in records}


def _fit_ready(fit):
    return bool(fit) and fit.get('pressure') is not None and fit.get('vibration') is not None


def score_patrol_batch(patrol_rows, references, *, duty='当班巡查', **alarm_options):
    """用同一批巡查读数在指定基准下回放稳健异常评分。"""
    scored = score_readings(patrol_rows, references=references, **alarm_options)
    # 持续告警事件按 duty 分段，同一批巡查读数统一打上本班 duty 标签。
    return [dict(row, duty=duty) for row in scored]


def active_confirmed_episodes(scored_rows, **episode_options):
    """交班时点仍然开启且已达到持续门槛的告警事件，按机组汇总。

    只有持续告警事件（episode ledger 中以 ``ACTIVE_AT_HANDOFF`` 收口、且
    ``confirmed`` 为真）才算现场尚未确认恢复的告警。返回的每个事件附带
    ``alarm_minutes``，取自该批读数里真实触发告警的观测分钟（不按区间补点）。
    """
    from ..persistence_filter.episode_ledger import extract_episodes

    alarm_minutes = defaultdict(list)
    for row in scored_rows:
        if row.get('eligible') and row.get('alarm'):
            alarm_minutes[row['unit']].append(row['minute'])

    open_episodes = {}
    for episode in extract_episodes(scored_rows, **episode_options):
        if episode['end_reason'] == 'ACTIVE_AT_HANDOFF' and episode['confirmed']:
            minutes = sorted(
                minute
                for minute in alarm_minutes.get(episode['unit'], [])
                if episode['start'] <= minute <= episode['end']
            )
            open_episodes.setdefault(episode['unit'], []).append(
                {**episode, 'alarm_minutes': minutes}
            )
    return open_episodes


def _alarm_keys(episodes):
    return {
        (episode['unit'], minute)
        for episode in episodes
        for minute in episode['alarm_minutes']
    }


def masking_evidence(old_scored, new_scored, field_recovered, **episode_options):
    """比较同一批读数在两套基准下的持续告警，找出被新基准盖住的未解除告警。

    现场已确认恢复的机组（``field_recovered``）不再受保护，其旧告警允许随
    新基准消失。
    """
    recovered = set(field_recovered)
    old_episodes = active_confirmed_episodes(old_scored, **episode_options)
    new_episodes = active_confirmed_episodes(new_scored, **episode_options)
    new_alarm_keys = _alarm_keys(
        [episode for unit_episodes in new_episodes.values() for episode in unit_episodes]
    )
    evidence = {}
    for unit, episodes in old_episodes.items():
        if unit in recovered:
            evidence[unit] = {
                'active_under_old': True,
                'active_under_new': unit in new_episodes,
                'protected': False,
                'masked_alarm_readings': [],
                'field_recovered': True,
            }
            continue
        lost = sorted(
            minute
            for episode in episodes
            for minute in episode['alarm_minutes']
            if (unit, minute) not in new_alarm_keys
        )
        evidence[unit] = {
            'active_under_old': True,
            'active_under_new': unit in new_episodes,
            'protected': True,
            'masked_alarm_readings': lost,
            'field_recovered': False,
        }
    return evidence


def _unit_summary(unit, fit, scored_rows, episodes_by_unit):
    eligible = [row for row in scored_rows if row['unit'] == unit and row.get('score') is not None]
    peak = max((row['score'] for row in eligible), default=None)
    episodes = episodes_by_unit.get(unit, [])
    return {
        'fit_id': fit['fit_id'] if fit else None,
        'basis': fit['basis'] if fit else None,
        'eligible_points': len(eligible),
        'peak_score': peak,
        'active_confirmed_alarms': len(episodes),
        'alarm_readings': sorted(minute for episode in episodes for minute in episode['alarm_minutes']),
    }


def plan_adoption(
    previous,
    candidate,
    patrol_rows,
    *,
    late_trained=(),
    field_recovered=(),
    duty='当班巡查',
    drift_limit=1.8,
    overlap_cutoff=.25,
    alarm_options=None,
    episode_options=None,
):
    """逐机决定本班采用哪一套基准，并用同一批巡查读数给出对照证据。

    ``previous``/``candidate`` 为 :mod:`duty_reference` 产出的基准记录；
    ``patrol_rows`` 为本班同一批巡查读数（``AlarmMeasure`` 可解析的映射或
    dataclass）。``late_trained`` 中的机组表示其候选基准的训练量是在交班之后
    才由迟到样本补足的，本班一律沿用旧基准。
    """
    alarm_options = dict(alarm_options or {})
    episode_options = dict(episode_options or {})
    old_fits, new_fits = _fit_index(previous), _fit_index(candidate)
    late_units = frozenset(late_trained)
    drift_rows = {
        row['unit']: row
        for row in compare_references(
            previous, candidate, drift_limit=drift_limit, overlap_cutoff=overlap_cutoff
        )
    }

    # 同一批巡查读数分别在旧、新基准下回放，保证可比。
    old_scored = score_patrol_batch(patrol_rows, previous, duty=duty, **alarm_options)
    new_scored = score_patrol_batch(patrol_rows, candidate, duty=duty, **alarm_options)
    old_episodes = active_confirmed_episodes(old_scored, **episode_options)
    new_episodes = active_confirmed_episodes(new_scored, **episode_options)
    masking = masking_evidence(
        old_scored, new_scored, field_recovered, **episode_options
    )

    decisions = []
    for unit in sorted(old_fits.keys() | new_fits.keys()):
        old_fit, new_fit = old_fits.get(unit), new_fits.get(unit)
        drift = drift_rows.get(unit)
        new_ready = _fit_ready(new_fit)
        old_ready = _fit_ready(old_fit)

        if unit in late_units and old_ready:
            # 候选基准的训练量在交班后才由迟到样本补足：本班仍用原基准，
            # 即使此刻重训已经达标。该判断先于漂移与告警保护。
            reason = KEEP_LATE
        elif not new_ready:
            reason = KEEP_NOT_READY if old_ready else KEEP_NO_BASELINE
        elif not old_ready:
            # 首次拿到可用基准：没有旧基准需要保护，直接采用。
            reason = ADOPT_FIRST
        elif drift is None:
            reason = KEEP_UNVERIFIABLE
        elif drift['status'] != _DRIFT_STATUS:
            # 工况未证实漂移（含 REFERENCE_STABLE / 队列变更 / 样本不足等）。
            reason = KEEP_STABLE
        else:
            masked = masking.get(unit)
            if masked is not None and masked['protected'] and masked['masked_alarm_readings']:
                reason = KEEP_MASKED
            else:
                reason = ADOPT_NEW

        if reason == KEEP_NO_BASELINE:
            # 两套基准都不可用：本班没有可采用的基准，下游读数走 NO_REFERENCE。
            adopted_fit = None
        elif reason in KEEP_REASONS:
            adopted_fit = old_fit
        else:
            adopted_fit = new_fit
        decisions.append({
            'unit': unit,
            'decision': 'KEEP_OLD' if reason in KEEP_REASONS else 'USE_NEW',
            'reason': reason,
            'drift_status': drift['status'] if drift else None,
            'late_training': unit in late_units,
            'field_recovered': unit in field_recovered,
            'previous_fit_id': old_fit['fit_id'] if old_fit else None,
            'candidate_fit_id': new_fit['fit_id'] if new_fit else None,
            'adopted_fit_id': adopted_fit['fit_id'] if adopted_fit else None,
            'masking': masking.get(unit, {
                'active_under_old': False,
                'active_under_new': False,
                'protected': False,
                'masked_alarm_readings': [],
                'field_recovered': unit in field_recovered,
            }),
            'under_previous': _unit_summary(unit, old_fit, old_scored, old_episodes),
            'under_candidate': _unit_summary(unit, new_fit, new_scored, new_episodes),
        })

    return {
        'decisions': decisions,
        'scored_under_previous': old_scored,
        'scored_under_candidate': new_scored,
        'active_episodes_under_previous': [
            episode for episodes in old_episodes.values() for episode in episodes
        ],
        'active_episodes_under_candidate': [
            episode for episodes in new_episodes.values() for episode in episodes
        ],
    }


def adopt_references(previous, candidate, patrol_rows, plan=None, **options):
    """返回本班每台机组实际采用的基准记录（供下游评分与持续告警使用）。

    可传入已算好的 ``plan`` 复用回放结果，避免重复计算。
    """
    if plan is None:
        plan = plan_adoption(previous, candidate, patrol_rows, **options)
    chosen = {}
    for decision in plan['decisions']:
        source = candidate if decision['decision'] == 'USE_NEW' else previous
        fit = next((row for row in source if row['unit'] == decision['unit']), None)
        if fit is not None:
            chosen[decision['unit']] = fit
    units = sorted(chosen)
    return [chosen[unit] for unit in units], plan
