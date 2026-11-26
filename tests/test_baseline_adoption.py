"""告警基准采用策略的单元测试。

策略层用显式构造的基准记录和同一批巡查读数回放，核对逐机采用结论与持续
告警保护；端到端层直接跑 ``sample_data/baseline_adoption_evening.json``，
覆盖七种不同采用结果。
"""
from __future__ import annotations

import json
from pathlib import Path

from chillwatch.duty_reference import (
    ADOPT_FIRST,
    ADOPT_NEW,
    KEEP_LATE,
    KEEP_MASKED,
    KEEP_NO_BASELINE,
    KEEP_NOT_READY,
    KEEP_STABLE,
    active_confirmed_episodes,
    adopt_references,
    evaluate_adoption,
    masking_evidence,
    plan_adoption,
    score_patrol_batch,
)
from chillwatch.duty_reference.adoption_service import validate_history, validate_patrol
from chillwatch.robust_alarm.contracts import parse as parse_measure

FIT_WINDOW = {'window_start': 140, 'window_end': 590}


def channel(center, *, scale=12.0, radius=12):
    return {
        'center': float(center),
        'scale': float(scale),
        'iqr': 0.0,
        'minimum': float(center - radius),
        'maximum': float(center + radius),
    }


def fit(unit, pressure_center, *, basis='INDIVIDUAL_BASELINE', family='screw',
        ready=True, fit_id=None, vibration_center=2.0):
    return {
        'unit': unit,
        'family': family,
        'basis': basis,
        'history_points': 6 if ready else 3,
        'unit_history_points': 6 if ready else 3,
        'pressure': channel(pressure_center) if ready else None,
        'vibration': channel(vibration_center, scale=.2, radius=.2) if ready else None,
        'fit_id': fit_id or f'{unit}-fit-{pressure_center}',
        **FIT_WINDOW,
    }


def patrol(unit, pressures, *, start=550):
    return [
        parse_measure({
            'unit': unit,
            'minute': start + 10 * i,
            'pressure_kpa': float(value),
            'vibration': 2.0,
            'defrost': False,
        })
        for i, value in enumerate(pressures)
    ]


def decision_map(plan):
    return {row['unit']: row for row in plan['decisions']}


def drifting_pair(unit='F1'):
    # 区间不重叠且中心相距 80：compare_references 判 REFERENCE_DRIFT。
    return fit(unit, 800, fit_id=f'{unit}-old'), fit(unit, 880, fit_id=f'{unit}-new')


def test_drift_without_open_alarm_adopts_new_baseline():
    previous, candidate = drifting_pair('F1')
    # 840 落在两套基准之间，两套回放都不触发告警。
    rows = patrol('F1', [840, 841, 840, 842])
    case = decision_map(plan_adoption([previous], [candidate], rows))['F1']
    assert case['reason'] == ADOPT_NEW
    assert case['decision'] == 'USE_NEW'
    assert case['drift_status'] == 'REFERENCE_DRIFT'
    assert case['adopted_fit_id'] == candidate['fit_id']
    assert case['under_previous']['active_confirmed_alarms'] == 0
    assert case['masking']['masked_alarm_readings'] == []


def test_masked_unresolved_persistent_alarm_keeps_old_baseline():
    previous, candidate = drifting_pair('F2')
    # 885 在旧基准（中心 800）连续告警，在新基准（中心 880）回到基线。
    rows = patrol('F2', [885, 886, 885, 887])
    case = decision_map(plan_adoption([previous], [candidate], rows))['F2']
    assert case['reason'] == KEEP_MASKED
    assert case['decision'] == 'KEEP_OLD'
    assert case['adopted_fit_id'] == previous['fit_id']
    assert case['under_previous']['active_confirmed_alarms'] == 1
    assert case['under_candidate']['active_confirmed_alarms'] == 0
    assert case['masking']['protected'] is True
    assert case['masking']['masked_alarm_readings'] == [550, 560, 570, 580]


def test_field_recovered_alarm_is_not_protected():
    previous, candidate = drifting_pair('F7')
    rows = patrol('F7', [885, 886, 885, 887])
    case = decision_map(
        plan_adoption([previous], [candidate], rows, field_recovered=['F7'])
    )['F7']
    assert case['reason'] == ADOPT_NEW
    assert case['masking']['active_under_old'] is True
    assert case['masking']['protected'] is False
    assert case['masking']['field_recovered'] is True
    assert case['masking']['masked_alarm_readings'] == []


def test_late_training_volume_keeps_old_even_when_candidate_is_ready():
    previous, candidate = drifting_pair('F3')
    rows = patrol('F3', [840, 841, 840, 842])
    case = decision_map(
        plan_adoption([previous], [candidate], rows, late_trained=['F3'])
    )['F3']
    assert case['reason'] == KEEP_LATE
    assert case['late_training'] is True
    assert case['adopted_fit_id'] == previous['fit_id']


def test_late_flag_without_old_baseline_does_not_fabricate_one():
    # 旧、新基准都不可用时，迟到标记不能凭空造出一套基准。
    previous = fit('F9', 500, ready=False, fit_id='F9-old')
    candidate = fit('F9', 500, ready=False, fit_id='F9-new')
    rows = patrol('F9', [500, 501, 500, 500])
    case = decision_map(
        plan_adoption([previous], [candidate], rows, late_trained=['F9'])
    )['F9']
    assert case['reason'] == KEEP_NO_BASELINE
    assert case['adopted_fit_id'] is None


def test_insufficient_candidate_keeps_old_baseline():
    previous = fit('F5', 600, fit_id='F5-old')
    candidate = fit('F5', 600, ready=False, fit_id='F5-new')
    rows = patrol('F5', [610, 611, 610, 610])
    case = decision_map(plan_adoption([previous], [candidate], rows))['F5']
    assert case['reason'] == KEEP_NOT_READY
    assert case['adopted_fit_id'] == previous['fit_id']
    assert case['under_candidate']['fit_id'] == candidate['fit_id']
    assert case['under_candidate']['peak_score'] is None


def test_stable_distribution_keeps_old_baseline():
    previous = fit('F4', 900, fit_id='F4-old')
    candidate = fit('F4', 900, fit_id='F4-new')
    rows = patrol('F4', [910, 911, 910, 910])
    case = decision_map(plan_adoption([previous], [candidate], rows))['F4']
    assert case['reason'] == KEEP_STABLE
    assert case['drift_status'] == 'REFERENCE_STABLE'
    assert case['adopted_fit_id'] == previous['fit_id']


def test_first_available_baseline_is_adopted_without_protection():
    previous = fit('F6', 500, ready=False, fit_id='F6-old')
    candidate = fit('F6', 516, fit_id='F6-new')
    rows = patrol('F6', [506, 507, 506, 506])
    case = decision_map(plan_adoption([previous], [candidate], rows))['F6']
    assert case['reason'] == ADOPT_FIRST
    assert case['adopted_fit_id'] == candidate['fit_id']
    assert case['previous_fit_id'] == previous['fit_id']


def test_same_batch_independent_unit_decisions():
    # 同批中 F2 因盖住未解除告警保留旧基准，不影响 F1 照常换新。
    old_f1, new_f1 = drifting_pair('F1')
    old_f2, new_f2 = drifting_pair('F2')
    rows = patrol('F1', [840, 841, 840, 842]) + patrol('F2', [885, 886, 885, 887])
    cases = decision_map(plan_adoption([old_f1, old_f2], [new_f1, new_f2], rows))
    assert cases['F1']['reason'] == ADOPT_NEW
    assert cases['F2']['reason'] == KEEP_MASKED


def test_same_patrol_rows_feed_both_replays():
    previous, candidate = drifting_pair('F1')
    rows = patrol('F1', [885, 886, 885, 887])
    plan = plan_adoption([previous], [candidate], rows)
    identity = [(r.unit, r.minute) for r in rows]
    assert [(r['unit'], r['minute']) for r in plan['scored_under_previous']] == identity
    assert [(r['unit'], r['minute']) for r in plan['scored_under_candidate']] == identity
    old_flags = {r['minute']: r['alarm'] for r in plan['scored_under_previous']}
    new_flags = {r['minute']: r['alarm'] for r in plan['scored_under_candidate']}
    assert all(old_flags.values()) and not any(new_flags.values())


def test_adopted_references_feed_shift_scoring_and_persistence():
    old_f1, new_f1 = drifting_pair('F1')
    old_f2, new_f2 = drifting_pair('F2')
    rows = patrol('F1', [840, 841, 840, 842]) + patrol('F2', [885, 886, 885, 887])
    adopted, plan = adopt_references([old_f1, old_f2], [new_f1, new_f2], rows)
    cases = decision_map(plan)
    adopted_by_unit = {row['unit']: row['fit_id'] for row in adopted}
    assert adopted_by_unit == {unit: cases[unit]['adopted_fit_id'] for unit in cases}
    # 本班评分实际跑在采用基准上：F1 用新 fit，F2 保留旧 fit 且仍持续告警。
    shift = score_patrol_batch(rows, adopted)
    f1 = [row for row in shift if row['unit'] == 'F1']
    f2 = [row for row in shift if row['unit'] == 'F2']
    assert all(row['fit_id'] == new_f1['fit_id'] for row in f1)
    assert all(row['fit_id'] == old_f2['fit_id'] and row['alarm'] for row in f2)
    # 持续告警同样基于采用基准：F2 确认，F1 不确认。
    from chillwatch.persistence_filter import evaluate as persist_evaluate

    persistence = {
        row['unit']: row
        for row in persist_evaluate([dict(row, duty='夜班实测') for row in shift])['records']
    }
    assert persistence['F2']['decision'] == 'CONFIRMED'
    assert persistence['F1']['decision'] == 'MONITOR'


def test_cohort_change_is_not_treated_as_verified_drift():
    previous = fit('F1', 800, basis='INDIVIDUAL_BASELINE', fit_id='F1-old')
    candidate = fit('F1', 880, basis='FAMILY_BASELINE', fit_id='F1-new')
    rows = patrol('F1', [800, 801, 800, 800])
    case = decision_map(plan_adoption([previous], [candidate], rows))['F1']
    assert case['drift_status'] == 'REFERENCE_COHORT_CHANGED'
    assert case['reason'] == KEEP_STABLE


def test_unconfirmed_short_run_is_not_a_protected_alarm():
    previous, candidate = drifting_pair('F1')
    # 只有第一笔在旧基准告警，达不到 min_points=2，不构成未解除持续告警。
    rows = patrol('F1', [885, 840, 840, 840])
    plan = plan_adoption([previous], [candidate], rows)
    assert decision_map(plan)['F1']['reason'] == ADOPT_NEW
    assert active_confirmed_episodes(plan['scored_under_previous']) == {}


def test_masking_evidence_marks_field_recovered_separately():
    previous, candidate = drifting_pair('F2')
    rows = patrol('F2', [885, 886, 885, 887])
    old_scored = score_patrol_batch(rows, [previous])
    new_scored = score_patrol_batch(rows, [candidate])
    protected = masking_evidence(old_scored, new_scored, [])
    recovered = masking_evidence(old_scored, new_scored, ['F2'])
    assert protected['F2']['masked_alarm_readings'] == [550, 560, 570, 580]
    assert recovered['F2']['protected'] is False
    assert recovered['F2']['masked_alarm_readings'] == []


def test_row_validation_isolates_bad_history_and_patrol_lines():
    bad_history = [
        {
            'unit': 'F1', 'family': 'screw', 'minute': 140 + 10 * i,
            'pressure_kpa': 800.0, 'vibration': 2.0, 'defrost': False,
        }
        for i in range(6)
    ] + [{
        'unit': 'F1', 'family': 'screw', 'minute': 200,
        'pressure_kpa': 9999.0, 'vibration': 2.0, 'defrost': False,
    }]
    bad_patrol = [{
        'unit': 'F1', 'minute': 550, 'pressure_kpa': 800.0,
        'vibration': 2.0, 'defrost': False,
    }, {
        'unit': 'F1', 'minute': 560, 'pressure_kpa': 800.0,
        'vibration': 2.0, 'defrost': 'no',
    }]
    accepted_h, rejected_h = validate_history(bad_history)
    accepted_p, rejected_p = validate_patrol(bad_patrol)
    assert len(accepted_h) == 6 and rejected_h[0]['code'] == 'OUT_OF_RANGE'
    assert len(accepted_p) == 1 and rejected_p[0]['code'] == 'EXPECTED_FLAG'


FIXTURE_PATH = Path(__file__).resolve().parents[1] / 'sample_data' / 'baseline_adoption_evening.json'


def test_fixture_covers_every_adoption_outcome():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    result = evaluate_adoption(
        fixture['history'], fixture['patrol'],
        handoff_cutoff=fixture['handoff_cutoff'],
        candidate_cutoff=fixture['candidate_cutoff'],
        lookback=fixture['lookback'],
        group_min=fixture['group_min'],
        late_trained=fixture['late_trained'],
        field_recovered=fixture['field_recovered'],
    )
    reasons = {row['unit']: row['reason'] for row in result['decisions']}
    assert reasons == fixture['expected_reasons']
    assert result['errors'] == []
    # 实际采用基准逐机与采用决定一致。
    adopted_ids = {row['unit']: row['fit_id'] for row in result['adopted_references']}
    for row in result['decisions']:
        assert adopted_ids[row['unit']] == row['adopted_fit_id']


def test_fixture_shift_outputs_use_adopted_baselines():
    from chillwatch.persistence_filter import evaluate as persist_evaluate
    from chillwatch.robust_alarm import evaluate as alarm_evaluate

    fixture = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    result = evaluate_adoption(
        fixture['history'], fixture['patrol'],
        handoff_cutoff=fixture['handoff_cutoff'],
        candidate_cutoff=fixture['candidate_cutoff'],
        lookback=fixture['lookback'],
        group_min=fixture['group_min'],
        late_trained=fixture['late_trained'],
        field_recovered=fixture['field_recovered'],
    )
    # 被盖住告警的 A2 保留旧基准：本班评分仍持续告警；已确认恢复的 A7 换新后消警。
    shift = alarm_evaluate(fixture['patrol'], references=result['adopted_references'])['records']
    persistence = {
        row['unit']: row
        for row in persist_evaluate([dict(row, duty='夜班实测') for row in shift])['records']
    }
    assert persistence['A2']['decision'] == 'CONFIRMED'
    assert persistence['A7']['decision'] == 'MONITOR'
    adopted_fit = {row['unit']: row['fit_id'] for row in result['adopted_references']}
    assert all(row['fit_id'] == adopted_fit['A2'] for row in shift if row['unit'] == 'A2')
