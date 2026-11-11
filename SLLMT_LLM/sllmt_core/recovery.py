"""사건형 보고의 WIP 회복 판정 — baseline_band_v1 (11-09, D-89). I/O·DES 없이 주별 궤적 배열과 spec 만 받는다.

팀원 의사결정안(`…\\output\\recovery_decision_20261109\\회복정의_의사결정_팀원전달_20261109.md`)에 Claude 검토 수정점을 더했다.

정의 (도구가 계산하고 LLM 은 설명만 한다)
- 지표: 평균 WIP 차이 = 같은 seed(갈래)끼리 짝지은 (시나리오 − 비교 대상) 주별 WIP 차이의 seed 평균.
  WIP 는 공정 안 lot 수(regular·hot·superhot 모두)의 [w, w+1) 주 시간 평균 (tg_hourly 에 lot 유형 구분이 없다 — 수정점 1).
- 허용 폭: 기준 WIP 참조값(비교 대상의 측정 창 평균 WIP) × 2%. 최소 유지 3주. 둘 다 연구용 잠정값 (산업 표준 아님).
- 회복 진입 주: 사건이 끝난(장비 복귀) 뒤의 완전한 주부터 찾아, 평균 WIP 차이가 허용 폭 안에 들어와 관측 끝까지 다시 벗어나지
  않는 첫 주. 그 주부터 관측 끝까지 3주 이상이면 recovered, 모자라면 insufficient_observation.
- CI 는 쓰지 않는다 (유의하지 않음 ≠ 가까워짐). 최고값 대비 25% 는 '초과 WIP 최고값 대비 75% 해소 시점' 보조 지표로만 남긴다.
- 끝나지 않는 변경(투입 변경, 영구 정지·장비 수, 끝 없는 고장 특성·hot 전환·디스패칭, 처음부터의 가공 시간·PM)이 있으면
  기준 운영과 영구히 다른 운영이라 회복 판단 대상이 아니다 (not_applicable — 수정점 3).
- 허용 폭 민감도(±1%·±2%·±5%, 3주)를 함께 돌려준다. 저장된 정지 실험 3건이 ±2% 에서 모두 미회복, ±5% 에서 2건 회복으로
  결론이 허용 폭에 따라 뒤집혔다 (수정점 2). 주 판정은 ±2% 그대로이고 민감도는 참고로만 쓴다 (사후에 폭을 고르지 않는다).

이전 정의: 11-09 D-87(최고의 25% 이하 + CI 하한 ≤ 0, 3주) → 이 판으로 바꿈. 그 전: whatif_report 의 처음 25% 이하 주.
"""
from __future__ import annotations

import math

METHOD = "baseline_band_v1"
TOL_RATIO = 0.02
HOLD_WEEKS = 3
SENS_RATIOS = (0.01, 0.02, 0.05)
AUX_FRAC = 0.25

BAND_KEY = "WIP 회복 판정 (baseline_band_v1)"
STATUS_KEY = "상태"
ENTRY_KEY = "회복 진입 주"
AFTER_KEY = "복귀 후 경과 주"
AUX_KEY = "보조: 초과 WIP 최고값 대비 75% 해소 시점 (최고의 25% 이하로 처음 내려간 주, 지속·재상승은 안 봄)"
CAUTION_KEY = "회복 표현 주의"

STATUS_TEXT = {
    "recovered": "관측기간 내 WIP 회복 확인",
    "not_recovered": "관측 종료까지 WIP 회복 확인 못함",
    "insufficient_observation": "허용 범위 진입 후 유지 확인 기간 부족",
    "no_mean_excursion": "평균 WIP 차이의 허용 범위 이탈이 관측되지 않음",
    "not_applicable": "끝나는 사건이 아니라 회복 판단 대상 아님",
    "no_reference": "기준 WIP 참조값이 없어 판단 못함 (짝 비교 불완전)",
}
DEFINITION = (f"장비 복귀(사건 끝) 뒤, 평균 WIP 차이(시나리오 − 비교 대상, 모든 lot 유형)가 기준 WIP 의 ±{100 * TOL_RATIO:g}% 안에 "
              f"들어와 최소 {HOLD_WEEKS}주 유지되고 관측 종료까지 다시 벗어나지 않는 첫 주. 운영상 허용 범위에 대한 기술적 판정이며 "
              "통계적 동등성 입증이 아님. ±2%·3주는 연구용 잠정값")
CAUTION = ("회복은 'WIP 회복 판정'의 상태·진입 주·정의를 그대로 쓴다. 회복 진입 주가 없으면 상태 문구 그대로(예: 'N주(관측 종료)까지 "
           "WIP 회복 확인 못함 — 그 뒤는 이번 결과로 알 수 없음') 쓰고 주를 추정하지 않는다. 평균 곡선 기준이라 seed 마다 회복했다는 "
           "뜻이 아니고, 납기·누적 생산 손실이 회복됐다는 뜻도 아니다. '보조: 75% 해소 시점'을 회복이라고 부르지 않는다. "
           "허용 폭 민감도에서 결론이 갈리면 그 사실을 한 줄로 밝힌다")


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _neutral(ph: dict) -> bool:
    if ph.get("group_wspw"):
        return False
    vals = list((ph.get("group_mult") or {}).values()) + list((ph.get("product_mult") or {}).values()) \
        + list((ph.get("lot_type_mult") or {}).values())
    return all(abs(float(v) - 1.0) < 1e-9 for v in vals)


def event_end(spec: dict) -> tuple[float | None, list]:
    """(끝나는 사건들의 마지막 끝 주, 끝나지 않는 변경 목록). 끝나는 사건이 없으면 첫 값은 None."""
    ends, perm = [], []
    if any(not _neutral(ph) for ph in spec.get("phases") or []):
        perm.append("투입 변경")
    for d in spec.get("downtime") or []:
        if d.get("type") == "outage":
            ends.append(float(d["start_week"]) + float(d["duration_weeks"]))
        elif d.get("end_week") is not None:
            ends.append(float(d["end_week"]))
        else:
            perm.append("고장 특성 (끝 없음)")
    for h in spec.get("hot_conversion") or []:
        if h.get("end_week") is not None:
            ends.append(float(h["end_week"]))
        else:
            perm.append("hot 전환 (끝 없음)")
    ov = spec.get("overrides") or {}
    if ov.get("tools"):
        perm.append("장비 수 (영구 정지·증감)")
    if ov.get("processing_time"):
        perm.append("가공 시간 (처음부터)")
    if ov.get("pm"):
        perm.append("PM (처음부터)")
    for d in spec.get("dispatch") or []:
        if d.get("end_week") is not None:
            ends.append(float(d["end_week"]))
        else:
            perm.append("디스패칭 (끝 없음)")
    return (max(ends) if ends else None), perm


def first_below_frac(weeks: list, wip: list, frac: float = AUX_FRAC):
    """보조 지표: 평균 곡선 최고점 뒤 처음으로 최고의 frac 이하가 된 주 (이전 whatif_report recovery_week 와 같은 계산)."""
    idx = [i for i in range(len(weeks)) if _ok(wip[i])]
    if not idx:
        return None
    i_pk = max(idx, key=lambda i: wip[i])
    pk = float(wip[i_pk])
    if pk <= 0:
        return None
    return next((weeks[i] for i in range(i_pk + 1, len(weeks)) if _ok(wip[i]) and wip[i] <= frac * pk), None)


def band_recovery(weeks: list, wip: list, ref_wip, return_week, ratio: float = TOL_RATIO, hold: int = HOLD_WEEKS) -> dict:
    """주별 평균 WIP 차이 → 회복 판정. weeks[i] 는 [weeks[i], weeks[i]+1) 구간의 시작 주."""
    n = len(weeks)
    out = {"status": None, "entry_week": None, "weeks_after_return": None, "ratio": ratio, "hold": hold,
           "return_week": return_week, "end_week": (float(weeks[-1]) + 1.0) if n else None,
           "ref_wip": ref_wip, "tolerance_lot": None}
    if return_week is None:
        out["status"] = "not_applicable"
        return out
    if not _ok(ref_wip) or ref_wip <= 0 or n == 0:
        out["status"] = "no_reference"
        return out
    tol = float(ref_wip) * ratio
    out["tolerance_lot"] = tol
    inb = [_ok(x) and abs(float(x)) <= tol for x in wip]             # 값이 없으면 범위 밖으로 본다 (보수적)
    if all(inb):
        out["status"] = "no_mean_excursion"
        return out
    first = math.ceil(float(return_week) - 1e-9)                      # 복귀 시각을 걸친 주는 세지 않는다
    start = next((i for i, w in enumerate(weeks) if float(w) >= first), n)
    for i in range(start, n):
        if all(inb[i:]):
            out["entry_week"] = float(weeks[i])
            out["weeks_after_return"] = float(weeks[i]) - float(return_week)
            out["status"] = "recovered" if n - i >= hold else "insufficient_observation"
            return out
    out["status"] = "not_recovered" if start < n else "insufficient_observation"
    return out


def _g(x) -> str:
    return f"{float(x):g}"


def report_fields(weeks: list, wip: list, ref_wip, spec: dict, event_start=None) -> dict:
    """get_report '주별 궤적'에 넣을 항목 (한국어 키·문장)."""
    ret, perm = event_end(spec)
    if perm:
        ret = None
    main = band_recovery(weeks, wip, ref_wip, ret)
    st = main["status"]
    end = main["end_week"]
    band = {STATUS_KEY: STATUS_TEXT[st] + (f" ({_g(end)}주 관측 종료, 그 뒤는 이번 결과로 알 수 없음)"
                                           if st in ("not_recovered", "insufficient_observation") and end is not None else ""),
            ENTRY_KEY: main["entry_week"], AFTER_KEY: main["weeks_after_return"],
            "사건 시작 주": event_start, "장비 복귀(사건 끝) 주": ret, "관측 종료 주": end,
            "기준 WIP 참조값 (lot, 비교 대상의 측정 창 평균)": None if not _ok(ref_wip) else round(float(ref_wip), 1),
            "허용 비율": f"±{100 * TOL_RATIO:g}%",
            "허용 폭 (lot)": None if main["tolerance_lot"] is None else round(main["tolerance_lot"], 1),
            "최소 유지 주": HOLD_WEEKS,
            "지표 범위": "WIP = 공정 안 lot 수(regular·hot·superhot 모두)의 주 시간 평균, 같은 seed 끼리 짝지은 차이의 seed 평균 곡선",
            "정의": DEFINITION}
    if perm:
        band["끝나지 않는 변경"] = perm
    if st not in ("not_applicable", "no_reference"):
        sens = {}
        for r in SENS_RATIOS:
            x = band_recovery(weeks, wip, ref_wip, ret, ratio=r)
            sens[f"±{100 * r:g}%"] = STATUS_TEXT[x["status"]] + (f" ({_g(x['entry_week'])}주)" if x["entry_week"] is not None else "")
        band["허용 폭 민감도 (참고, 3주 유지)"] = sens
        band["허용 폭에 따라 결론이 갈림"] = len({v.split(" (")[0] for v in sens.values()}) > 1
    return {BAND_KEY: band, AUX_KEY: first_below_frac(weeks, wip) or "없음", CAUTION_KEY: CAUTION}
