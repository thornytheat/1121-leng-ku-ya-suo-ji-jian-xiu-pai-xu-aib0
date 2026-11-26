"""生成告警基准采用样例 ``sample_data/baseline_adoption_evening.json``。

样例覆盖同一批巡查读数下的不同采用结论：

* A1 工况漂移且无未解除持续告警 -> 采用新基准
* A2 工况漂移但新基准会盖住未确认恢复的持续告警 -> 保留旧基准
* A3 候选基准靠交班后的迟到样本才补足训练量 -> 保留旧基准
* A4 工况稳定 -> 保留旧基准
* A5 候选基准训练量不足 -> 保留旧基准
* A6 首次拿到可用基准 -> 采用新基准
* A7 同 A2 但现场已确认恢复 -> 采用新基准

运行：``PYTHONPATH=src python3 -I tools/generate_adoption_fixture.py``
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from chillwatch.duty_reference import evaluate_adoption
from chillwatch.duty_reference.adoption import (
    ADOPT_FIRST,
    ADOPT_NEW,
    KEEP_LATE,
    KEEP_MASKED,
    KEEP_NOT_READY,
    KEEP_STABLE,
)

HANDOFF_CUTOFF = 500
CANDIDATE_CUTOFF = 590
LOOKBACK = 360
GROUP_MIN = 5
VIBRATION = 2.0


def history_point(unit, family, minute, pressure):
    return {
        'unit': unit,
        'family': family,
        'minute': minute,
        'pressure_kpa': float(pressure),
        'vibration': VIBRATION,
        'defrost': False,
    }


def linear_window(unit, family, start_minute, start_pressure, *, count=6, step_minute=10, step_pressure=4):
    return [
        history_point(unit, family, start_minute + i * step_minute, start_pressure + i * step_pressure)
        for i in range(count)
    ]


def patrol(unit, pressures, *, start=550, step=10):
    return [
        {
            'unit': unit,
            'minute': start + i * step,
            'pressure_kpa': float(pressure),
            'vibration': VIBRATION,
            'defrost': False,
        }
        for i, pressure in enumerate(pressures)
    ]


def build_fixture():
    history: list[dict] = []

    # A1：历史窗口中心由 910 漂移到 980；巡查读数落在两套基准的共同安全区，
    # 不产生持续告警，换新不丢任何告警。
    history += linear_window('A1', 'fam-a1', 140, 880)
    history += linear_window('A1', 'fam-a1', 460, 950)

    # A2：旧中心 800、新中心 880；正常负载 885 在旧基准下连续告警，
    # 新基准下全部回到基线 -> 未解除持续告警会被盖住。历史窗口取紧密分布，
    # 尺度落到仪表分辨率下限，让稳定偏移在旧基准上足以触发告警。
    history += linear_window('A2', 'fam-a2', 140, 800, step_pressure=0)
    history += linear_window('A2', 'fam-a2', 460, 880, step_pressure=0)

    # A3：旧窗口 6 点已可用；新窗口交班时点只有 3 点，靠 520–540 的迟到
    # 样本才补足 6 点（late_trained 元数据来自上报截止审计）。
    history += linear_window('A3', 'fam-a3', 140, 700)
    history += linear_window('A3', 'fam-a3', 460, 760, count=3)
    history += linear_window('A3', 'fam-a3', 520, 772, count=3)

    # A4：两个窗口同分布，比较结果 REFERENCE_STABLE。
    history += linear_window('A4', 'fam-a4', 140, 900)
    history += linear_window('A4', 'fam-a4', 460, 900)

    # A5：旧窗口可用；新窗口仅 3 点且无同族可借，候选基准不可用。
    history += linear_window('A5', 'fam-a5', 140, 600)
    history += linear_window('A5', 'fam-a5', 460, 600, count=3)

    # A6：旧窗口只落进 4 点（不足），新窗口 6 点齐备且全部在交班前来齐。
    history += linear_window('A6', 'fam-a6', 470, 500, count=4, step_minute=5)
    history += linear_window('A6', 'fam-a6', 505, 516, count=2, step_minute=5)

    # A7：结构同 A2，但现场已确认恢复，允许新基准消警。
    history += linear_window('A7', 'fam-a7', 140, 800, step_pressure=0)
    history += linear_window('A7', 'fam-a7', 460, 880, step_pressure=0)

    patrol_rows = (
        patrol('A1', [945, 948, 946, 947])
        + patrol('A2', [885, 888, 886, 889])
        + patrol('A3', [770, 771, 770, 772])
        + patrol('A4', [910, 911, 910, 910])
        + patrol('A5', [610, 611, 610, 610])
        + patrol('A6', [506, 507, 506, 506])
        + patrol('A7', [885, 888, 886, 889])
    )

    return {
        'description': '交班告警基准采用对照样例：同一批巡查读数回放新旧基准',
        'handoff_cutoff': HANDOFF_CUTOFF,
        'candidate_cutoff': CANDIDATE_CUTOFF,
        'lookback': LOOKBACK,
        'group_min': GROUP_MIN,
        'late_trained': ['A3'],
        'field_recovered': ['A7'],
        'expected_reasons': {
            'A1': ADOPT_NEW,
            'A2': KEEP_MASKED,
            'A3': KEEP_LATE,
            'A4': KEEP_STABLE,
            'A5': KEEP_NOT_READY,
            'A6': ADOPT_FIRST,
            'A7': ADOPT_NEW,
        },
        'history': history,
        'patrol': patrol_rows,
    }


def main():
    fixture = build_fixture()
    result = evaluate_adoption(
        fixture['history'],
        fixture['patrol'],
        handoff_cutoff=fixture['handoff_cutoff'],
        candidate_cutoff=fixture['candidate_cutoff'],
        lookback=fixture['lookback'],
        group_min=fixture['group_min'],
        late_trained=fixture['late_trained'],
        field_recovered=fixture['field_recovered'],
        duty='夜班实测',
    )
    actual = {row['unit']: row['reason'] for row in result['decisions']}
    expected = fixture['expected_reasons']
    assert actual == expected, f'采用结论与设计不符: {actual} != {expected}'
    for row in result['decisions']:
        print(
            f"{row['unit']} {row['decision']:<7} {row['reason']:<28} "
            f"drift={row['drift_status']} adopted_fit={row['adopted_fit_id']}"
        )
    output = Path(__file__).resolve().parents[1] / 'sample_data' / 'baseline_adoption_evening.json'
    output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'written {output}')


if __name__ == '__main__':
    main()
