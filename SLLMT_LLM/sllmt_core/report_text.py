"""도구가 쓰는 답 문장 (11-09, D-91 글 설명 개선 A). I/O 없이 도구 결과 dict 만 받는다.

T-v2 판정에서 틀린 설명의 대부분은 LLM 이 숫자를 옮기다 틀린 것이었다 (CI 13.85 → 1.385, 기준 CI 대신 차이 CI, regular 값을
'전체'로, area 와 툴그룹을 섞음, 전체 순위를 area 순위로). 그래서 숫자가 들어가는 문장은 도구가 직접 만들고 LLM 은 그대로 붙인다.
지표 이름에는 범위를 붙인다 (regular·전체·hot+superhot, WIP 는 모든 lot 유형). 숫자는 도구 결과 값 그대로 (다시 반올림하지 않음).

11-10 (사용자와 응답 형식 개선): ② 결론은 '질문한 지표 줄 + 기본 결론 4줄(전체 처리량·WIP·regular CT·납기 준수 regular)'.
지표 줄마다 변화율(%)과 개수 환산(측정 기간 동안 완료 lot·납기 넘긴 lot 수)을 붙이고, 질문이 '몇 개'면 '개수 먼저',
'몇 %'면 '비율 먼저' 줄을 쓴다. 표는 핵심 표(6행)와 세부 표(전체). 주의는 사용자가 알아야 할 것만, 기술 설명은 '분석 메모'.
조회(가동률) 답은 표로.
"""
from __future__ import annotations

import math
import re

BLOCK_KEY = "답에 그대로 쓸 문장"
DEFAULT_ROWS = ("처리량 전체 (lot/주)", "WIP (lot)", "CT (일)", "납기 준수 regular (%)")      # ② 결론 기본 4줄
CORE_ROWS = ("처리량 전체 (lot/주)", "처리량 (lot/주)", "WIP (lot)", "CT (일)", "납기 준수 regular (%)",
             "납기 준수 hot만 (%)")                                                          # 핵심 표
SHORT_VERDICT = (("유의하게 증가", "유의하게 증가"), ("유의하게 감소", "유의하게 감소"), ("CI 없음", "CI 없음 (짝 1개)"))


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def num(x) -> str:
    """도구 값 그대로 (정수면 소수점 없이). 값이 없으면 '없음'."""
    if not _ok(x):
        return "없음"
    x = float(x)
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.6f}".rstrip("0").rstrip(".")


def signed(x) -> str:
    return "없음" if not _ok(x) else ("+" if float(x) > 0 else "") + num(x).replace("-", "−")


def split_key(key: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)\s*\(([^()]*)\)$", key)
    return (m.group(1), m.group(2)) if m else (key, "")


def display_name(key: str) -> tuple[str, str, str]:
    """도구 지표 키 → (범위를 밝힌 이름, 값 단위, 차이 단위)."""
    base, unit = split_key(key)
    unit = {"lot, 창 전체 합계": "lot"}.get(unit, unit)
    diff_unit = "%p" if unit == "%" else unit
    if base == "WIP":
        name = "WIP (모든 lot 유형)"
    elif base == "처리량 전체":
        name = "전체 처리량 (모든 lot 유형)"
    elif base in ("처리량", "CT"):
        name = f"regular {base}"
    elif re.fullmatch(r"(처리량|CT) (A_long|B_mid|C_short)", base):
        m, g = base.split(" ")
        name = f"regular {m} {g}"
    else:
        name = base.replace("hot+superhot", "hot+superhot(둘 합친 값)")
    return name, unit, diff_unit


def _u(unit: str) -> str:
    return "" if not unit else (unit if unit in ("%", "%p") else f" {unit}")


def verdict(v: str | None) -> str:
    v = str(v or "")
    for k, out in SHORT_VERDICT:
        if v.startswith(k):
            return out
    return "유의한 차이 확인 못함 ('차이가 없다'는 뜻이 아님)" if v else "판정 없음"


def _parts(key: str, row: dict) -> tuple[str, str, str, str]:
    """(이름, '기준 → 시나리오', '차이 ± CI', 판정)."""
    name, u, du = display_name(key)
    b, s, d, c = row.get("비교 대상"), row.get("시나리오"), row.get("차이"), row.get("차이 95% CI 반폭")
    vals = (f"{num(b)}{_u(u)} → " if _ok(b) else "비교 대상 값 없음 → ") + f"{num(s)}{_u(u)}"
    diff = f"차이 {signed(d)}{_u(du)} ±{num(c)}{_u(du)}" if _ok(d) else ""
    return name, vals, diff, verdict(row.get("판정"))


def row_line(key: str, row: dict, extra: str | None = None) -> str:
    """기본 줄: '이름: 기준 → 시나리오, 차이 ± CI (환산) — 판정'."""
    name, vals, diff, v = _parts(key, row)
    return f"{name}: {vals}{', ' + diff if diff else ''}{f' ({extra})' if extra else ''} — {v}"


# -- 변화율·개수 환산 (11-10) ---------------------------------------------------------------------------------------
def _pct_txt(d, b) -> str | None:
    """기준 대비 변화율. 기준이 0 이면 없음."""
    if not (_ok(d) and _ok(b)) or abs(float(b)) < 1e-9:
        return None
    p = round(float(d) / float(b) * 100, 1)
    return "0%" if p == 0 else f"{p:+.1f}%".replace("-", "−")


def _cnt_txt(x) -> str | None:
    if not _ok(x):
        return None
    n = int(math.copysign(math.floor(abs(float(x)) + 0.5), float(x)))   # 반올림 (0.5 는 0 에서 먼 쪽)
    return f"{n:+d}".replace("-", "−") if n else "0"


def _throughput_key(rate_key: str) -> str:
    """'납기 준수 regular A_long (%)' → '처리량 A_long (lot/주)' (그 lot 들의 완료 수를 셀 처리량 행)."""
    who = split_key(rate_key)[0][len("납기 준수 "):]
    if who == "regular":
        return "처리량 (lot/주)"
    return f"처리량 {who[len('regular '):] if who.startswith('regular ') else who} (lot/주)"


def _late_diff(rate: dict, thr: dict | None, weeks) -> float | None:
    """측정 기간 동안 납기를 넘긴 lot 수의 차이 (seed 평균 처리량 × 주 수 × (1 − 납기 준수율), 약)."""
    vals = [rate.get("비교 대상"), rate.get("시나리오"), (thr or {}).get("비교 대상"), (thr or {}).get("시나리오"), weeks]
    if not all(_ok(v) for v in vals):
        return None
    rb, rs, tb, ts, w = map(float, vals)
    return ts * w * (100 - rs) / 100 - tb * w * (100 - rb) / 100


def conversions(key: str, row: dict, rows: dict, weeks) -> dict:
    """{'count': 개수 문구 또는 None, 'pct': 비율 문구 또는 None}. 단위별로 뜻이 있는 것만."""
    base, unit = split_key(key)
    d, b = row.get("차이"), row.get("비교 대상")
    w = num(weeks) if _ok(weeks) else None
    if unit == "lot/주":
        cnt = _cnt_txt(float(d) * float(weeks)) if _ok(d) and _ok(weeks) else None
        return {"count": f"측정 {w}주 동안 완료 lot 약 {cnt}개" if cnt is not None else None, "pct": _pct_txt(d, b)}
    if base == "WIP" and unit == "lot":
        cnt = _cnt_txt(d)
        return {"count": f"공장 안 lot 약 {cnt}개" if cnt is not None else None, "pct": _pct_txt(d, b)}
    if unit == "일":
        return {"count": None, "pct": _pct_txt(d, b)}
    if unit == "%" and base.startswith("납기 준수"):
        cnt = _cnt_txt(_late_diff(row, rows.get(_throughput_key(key)), weeks))
        return {"count": f"납기 넘긴 lot 측정 {w}주 동안 약 {cnt}개" if cnt is not None else None,
                "pct": f"납기 준수율 {signed(d)}%p" if _ok(d) else None}
    return {"count": None, "pct": None}


def _extra(key: str, cv: dict) -> str | None:
    """기본 줄 괄호 안: 처리량은 '변화율, 개수', WIP·CT 는 변화율, 납기는 개수."""
    unit = split_key(key)[1]
    if unit == "lot/주":
        got = [x for x in (cv["pct"], cv["count"]) if x]
    elif unit == "%":
        got = [cv["count"]] if cv["count"] else []
    else:
        got = [cv["pct"]] if cv["pct"] else []
    return ", ".join(got) or None


def lead_line(key: str, row: dict, cv: dict, first: str) -> str:
    """first='count' 면 개수 먼저, 'pct' 면 비율 먼저. 그 뒤에 '기준 → 시나리오 (차이 ± CI) — 판정'."""
    name, vals, diff, v = _parts(key, row)
    _, unit, du = display_name(key)
    native = f"{signed(row.get('차이'))}{_u(du)}" if _ok(row.get("차이")) else None
    a, b = (cv["count"], cv["pct"]) if first == "count" else (cv["pct"], cv["count"])
    a, b = a or native, b or (native if (a or native) != native else None)
    lead = f"{a} ({b})" if a and b else (a or "")
    return f"{name}: {lead}, {vals}{f' ({diff})' if diff else ''} — {v}" if lead else row_line(key, row)


def _table(rows: dict) -> str:
    head = "| 지표 | 비교 대상 | 시나리오 | 차이 ± 95% CI | 판정 |\n|---|---:|---:|---:|---|"
    lines = [head]
    for k, r in rows.items():
        if not isinstance(r, dict) or "시나리오" not in r:
            continue
        name, u, du = display_name(k)
        d = f"{signed(r.get('차이'))} ±{num(r.get('차이 95% CI 반폭'))}{_u(du)}" if _ok(r.get("차이")) else ""
        lines.append(f"| {name} ({u}) | {num(r.get('비교 대상')) if _ok(r.get('비교 대상')) else ''} | {num(r.get('시나리오'))} | {d} | "
                     f"{verdict(r.get('판정')).split(' (')[0]} |")
    return "\n".join(lines)


def report_sentences(res: dict, changes: list | None = None) -> dict:
    """get_report 결과 → 답에 그대로 쓸 문장 (조건·바꾼 것·기본 결론·지표별 결론·개수 먼저·비율 먼저·사건 결론·병목 결론·
    핵심 표·세부 표·주의·분석 메모)."""
    kpi = res.get("kpi") or {}
    lot = res.get("lot 유형별·제품군별 (짝 비교)") or {}
    lot = lot if isinstance(lot, dict) else {}
    w0, w1 = (res.get("측정 창 (주)") or [None, None])
    weeks = (w1 - w0) if _ok(w0) and _ok(w1) else None
    seeds = res.get("seeds") or []
    cond = (f"측정 {num(w0)}~{num(w1)}주 ({num(weeks) if _ok(weeks) else '?'}주간), seed {', '.join(map(str, seeds))} "
            f"(짝 {res.get('짝지은 seed 수')}개). 비교 대상: {res.get('비교 대상')}. 차이 = 시나리오 − 비교 대상 (같은 seed 끼리), "
            "± 는 95% 신뢰구간 반폭")
    rows = {k: r for t in (kpi, lot) for k, r in t.items() if isinstance(r, dict) and "시나리오" in r}
    conv = {k: conversions(k, r, rows, weeks) for k, r in rows.items()}
    by_name = {display_name(k)[0]: row_line(k, r, _extra(k, conv[k])) for k, r in rows.items()}
    base_concl = [by_name[display_name(k)[0]] for k in DEFAULT_ROWS if k in rows]
    count_first = {display_name(k)[0]: lead_line(k, rows[k], conv[k], "count") for k in CORE_ROWS if k in rows}
    ratio_first = {display_name(k)[0]: lead_line(k, rows[k], conv[k], "pct") for k in CORE_ROWS if k in rows}
    event, bottleneck = [], []
    concl = event                                                      # 아래 주별 궤적 줄은 '사건 결론'
    tj = res.get("주별 궤적") or {}
    if tj:
        concl.append(f"평균 WIP 차이 곡선은 {num(tj.get('평균 곡선이 가장 높은 주'))}주에 가장 높음 (약 {num(tj.get('평균 곡선 최고값 (lot)'))} lot). "
                     f"짝마다 최고의 평균은 {num(tj.get('WIP 차이 최고 (lot, 짝마다 최댓값의 평균)'))} lot "
                     f"(±{num(tj.get('최고점 95% CI 반폭'))}) — 두 값은 다른 집계")
        band = next((v for k, v in tj.items() if k.startswith("WIP 회복 판정") and isinstance(v, dict)), None)
        if band:
            ent = band.get("회복 진입 주")
            line = f"WIP 회복 판정: {band.get('상태')}"
            if _ok(ent):
                line += f" — 회복 진입 {num(ent)}주 (장비 복귀 {num(band.get('장비 복귀(사건 끝) 주'))}주 뒤 {num(band.get('복귀 후 경과 주'))}주)"
            if _ok(band.get("허용 폭 (lot)")):
                line += (f". 기준: 평균 WIP 차이가 기준 WIP {num(band.get('기준 WIP 참조값 (lot, 비교 대상의 측정 창 평균)'))} lot 의 "
                         f"{band.get('허용 비율')}(±{num(band.get('허용 폭 (lot)'))} lot) 안에 {band.get('최소 유지 주')}주 이상, 관측 끝까지 재이탈 없음 "
                         "(평균 곡선 기준, 납기·생산 회복과는 다름)")
            concl.append(line)
            sens = band.get("허용 폭 민감도 (참고, 3주 유지)")
            if band.get("허용 폭에 따라 결론이 갈림") and isinstance(sens, dict):
                concl.append("허용 폭에 따라 회복 결론이 갈림: " + ", ".join(f"{k} {v}" for k, v in sens.items()))
        cd, cc = tj.get("창 끝 누적 완료 regular lot 차이"), tj.get("누적 완료 차이 95% CI 반폭")
        if _ok(cd):
            sig = "유의" if _ok(cc) and abs(float(cd)) > float(cc) else "유의한 차이 확인 못함"
            concl.append(f"창 끝 누적 완료 regular lot 차이: {signed(cd)} lot ±{num(cc)} lot — {sig}")
    concl = bottleneck                                                 # 아래 가동률 줄은 '병목 결론'
    ch = res.get("가동률이 가장 많이 바뀐 툴그룹") or []
    if ch:
        concl.append("가동률이 가장 많이 바뀐 툴그룹: " + "; ".join(
            f"{x.get('툴그룹')} {num(x.get('비교 대상 가동률 (%)'))}% → {num(x.get('시나리오 가동률 (%)'))}% "
            f"(차이 {signed(x.get('차이 (%p)'))}%p ±{num(x.get('차이 95% CI 반폭 (%p)'))}%p)" for x in ch[:3]))
    top = res.get("가동률 상위 5 (시나리오)") or []
    if top:
        n = len(seeds)
        concl.append("가동률 상위 5 (시나리오, 툴그룹별 값): " + ", ".join(
            f"{x.get('툴그룹')} {num(x.get('가동률 (%)'))}%" + (f" (상위 5에 든 seed {x.get('상위 5에 든 seed 수')}/{n})"
                                                            if x.get("상위 5에 든 seed 수") is not None else "") for x in top))
        base_top = [x.get("툴그룹") for x in res.get("가동률 상위 5 (비교 대상)") or []]
        if base_top:
            same = [x.get("툴그룹") for x in top][:1] == base_top[:1]
            concl.append(f"가동률 1위: 비교 대상 {base_top[0]}, 시나리오 {top[0].get('툴그룹')} — " + ("같음" if same else "바뀜"))
    # 주의 = 사용자가 결과를 읽을 때 알아야 할 것, 분석 메모 = 정상상태·warm-up 같은 기술 설명 (11-10)
    notes, memo = [], []
    ss = res.get("정상상태") or {}
    event_type = str((res.get("기간") or {}).get("실험 유형", "")).startswith("사건형")
    if ss.get("설명"):
        memo.append(ss["설명"])
    if ss.get("WIP 계속 증가") and not event_type:
        notes.append("측정 기간 동안 WIP 가 계속 늘어 안정된 상태로 보기 어려움. 수치는 측정 기간 길이에 따라 달라질 수 있음")
    if ss.get("warm-up 부족 가능성") and not event_type:
        notes.append("실행 초반의 불안정한 구간이 측정에 섞였을 수 있음 (분석 메모 참고)")
    if event_type and "시작 상태" in str(res.get("비교 대상")):
        notes.append("이 결과는 그 시작 상태 하나에 대한 답")
    notes.append("'유의한 차이 확인 못함'은 CI 가 0 을 포함한다는 뜻이고 '차이가 없다·그대로다'는 뜻이 아님")
    for n in res.get("notes") or []:
        if any(w in n for w in ("seed 가 1개", "짝지은 seed", "측정 창을", "warm-up")):
            notes.append(n)
    out = {"조건": cond, "기본 결론": base_concl, "지표별 결론": by_name, "개수 먼저": count_first, "비율 먼저": ratio_first,
           **({"사건 결론": event} if event else {}), **({"병목 결론": bottleneck} if bottleneck else {}),
           "핵심 표": _table({k: rows[k] for k in CORE_ROWS if k in rows}), "세부 표": _table(rows),
           "주의": notes, **({"분석 메모": memo} if memo else {}),
           "쓰는 법": ("줄은 고치지 않고 그대로 붙인다 (숫자·이름·판정을 바꾸거나 다시 계산하지 않음). 직접 쓰는 문장의 숫자는 "
                     "도구 줄 값 그대로만 (한 줄 답). "
                     "② 결론은 질문한 지표의 줄(지표별 결론, 질문이 '몇 개'면 개수 먼저, '몇 %'면 비율 먼저)을 먼저, 이어서 기본 결론. "
                     "③ 은 핵심 표 (세부 표는 사용자가 '자세히' 요청할 때만). 분석 메모는 답 맨 끝")}
    if changes:
        out = {"바꾼 것": list(changes), **out}
    return out


def kpi_sentences(res: dict) -> dict:
    """get_kpi 결과 → 답에 그대로 쓸 문장."""
    w0, w1 = (res.get("측정 창 (주)") or [None, None])
    lines = []
    for k, v in (res.get("kpi") or {}).items():
        if not isinstance(v, dict):
            continue
        name, u, _ = display_name(k)
        ci = f" (95% CI ±{num(v.get('95% CI 반폭'))}{_u(u)})" if _ok(v.get("95% CI 반폭")) else ""
        extra = f", seed별 {[v2 for v2 in (v.get('seed별') or [])]}" if k.startswith("측정 창 완료") else ""
        lines.append(f"{name}: 평균 {num(v.get('평균'))}{_u(u)}{ci}{extra}")
    return {"조건": f"{res.get('대상')}, 측정 {num(w0)}~{num(w1)}주, seed {', '.join(map(str, res.get('seeds') or []))} 평균",
            "값": lines, "주의": [n for n in res.get("notes") or [] if "30개 미만" in n or "해당 없음" in n or "창 전체 합계" in n],
            "쓰는 법": "값 줄은 고치지 않고 그대로 붙인다. 평균의 CI 는 그 값의 불확실성이지 변화량이 아님"}


RANK_RE = re.compile(r"(\d+) / (\d+)")
TOOLGROUP_COLUMNS = ("가동률 = 쓸 수 있는 시간(작업 + 유휴) 중 실제 작업한 비율 · 가용률 = 전체 시간 중 고장·PM 이 아닌 시간(작업 + 유휴)의 비율 · "
                     "고장·PM = 전체 시간 중 고장·PM 시간의 비중 · 평균 대기 lot = 그 툴그룹 앞에서 기다리는 lot 수의 평균 · "
                     "± = 95% 신뢰구간 반폭")


def toolgroup_sentences(res: dict) -> dict:
    """get_toolgroup_stats 결과 → 답에 그대로 쓸 표 (11-10: 한 줄에 7개 값을 늘어놓던 것을 표로).
    순위는 '전체 N개 툴그룹 중 k위'의 k (모집단은 열 이름에), area 안 순위는 따로 열."""
    w0, w1 = (res.get("측정 창 (주)") or [None, None])
    rows = res.get("rows") or []
    cmp_ = any(_ok(x.get("가동률 차이 (%p)")) for x in rows)
    total = next((m.group(2) for x in rows if (m := RANK_RE.fullmatch(str(x.get("가동률 순위") or "")))), None)
    area_rank = any(RANK_RE.fullmatch(str(x.get("area 안 가동률 순위") or "")) for x in rows)
    cols = [f"가동률 순위 (전체 {total}개 중)" if total else "가동률 순위", "툴그룹", "area", "장비 수"]
    cols += ["area 안 순위"] if area_rank else []
    cols += (["가동률 (비교 대상 → 시나리오)", "가동률 차이 ± 95% CI", "평균 대기 lot (비교 대상 → 시나리오)"] if cmp_
             else ["가동률 ± 95% CI", "평균 대기 lot"])
    cols += ["가용률", "고장", "PM"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for x in rows:
        m = RANK_RE.fullmatch(str(x.get("가동률 순위") or ""))
        cells = [f"{m.group(1)}위" if m else str(x.get("가동률 순위") or ""), str(x.get("툴그룹")), str(x.get("area")),
                 f"{x.get('장비 수')}대"]
        if area_rank:
            ar = RANK_RE.fullmatch(str(x.get("area 안 가동률 순위") or ""))
            cells.append(f"{ar.group(1)}위 / {ar.group(2)}개" if ar else "")
        if cmp_:
            cells += [f"{num(x.get('비교 대상 가동률 (%)'))}% → {num(x.get('가동률 (%)'))}%",
                      f"{signed(x.get('가동률 차이 (%p)'))}%p ±{num(x.get('차이 95% CI 반폭 (%p)'))}%p",
                      f"{num(x.get('비교 대상 대기 lot'))} → {num(x.get('대기 lot'))}"]
        else:
            cells += [f"{num(x.get('가동률 (%)'))}% ±{num(x.get('가동률 95% CI 반폭 (%p)'))}%p", num(x.get("대기 lot"))]
        cells += [f"{num(x.get('가용률 (%)'))}%", f"{num(x.get('고장 (%)'))}%", f"{num(x.get('PM (%)'))}%"]
        lines.append("| " + " | ".join(cells) + " |")
    out = {"조건": f"{res.get('대상')}, 측정 {num(w0)}~{num(w1)}주, seed {', '.join(map(str, res.get('seeds') or []))} 평균, "
                   f"정렬 {res.get('정렬')}, 보인 행 {res.get('보인 행 / 해당 툴그룹')}",
           "표": "\n".join(lines), "열 설명": TOOLGROUP_COLUMNS,
           "쓰는 법": "조건 줄과 표를 고치지 않고 그대로 붙이고, 표 아래에 열 설명을 한 줄로 붙인다. 순위는 전체 툴그룹 중 순위다 — "
                    "area 안 순위는 'area 안 순위' 열만 쓰고 '전체 중 k위'를 area 안 순위로 바꾸지 않는다"}
    if res.get("area 평균"):
        out["area 평균"] = res["area 평균"]
    return out
