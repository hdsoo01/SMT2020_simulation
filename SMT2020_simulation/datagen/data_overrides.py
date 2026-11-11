"""엑셀 파라미터 덮어쓰기 (11-04, D-60). 시뮬레이터 코드(code/)는 고치지 않고 실행 직전 ModelData 만 바꾼다.

시나리오 JSON 의 "overrides":
  {"tools": [{"toolgroup": "Litho_FE_111", "delta": 1, "start_week": 30}],
   "processing_time": [{"toolgroup": "DE_BE_11", "mult": 0.9}, {"area": "Dry_Etch", "mult": 0.9}],
   "pm": [{"toolgroup": "Litho_FE_111", "interval_mult": 1.2, "duration_mult": 0.7}]}

  tools            장비 수 증감. delta > 0 증설, < 0 감축 (남는 장비는 1대 이상).
                   start_week 0(기본): 처음부터 장비 수 자체를 바꾼다.
                   start_week > 0: 증설은 장비를 처음부터 만들되 start_week 까지 정지(OUTAGE)로 두고,
                   감축은 번호가 큰 장비부터 start_week 부터 끝까지 정지시킨다 (downtime_scenarios 의 outage 기구).
                   새 장비는 PM 카운터 0, 고장 이력 없이 시작한다.
  processing_time  가공 시간 배율 (평균·편차·cascading 간격). toolgroup 또는 area. 실행 처음부터 적용.
                   디스패칭 분모(suffix_raw_pt)와 보고용 RPT 도 data_parser 의 같은 함수로 다시 계산한다.
  pm               PM 간격 배율(interval_mult: 시간·wafer 카운터 기준 모두, 첫 발생 시점 포함)과
                   소요 시간 배율(duration_mult: 평균·편차). toolgroup 또는 area. 실행 처음부터 적용.

배율이 모두 1 이거나 overrides 가 없으면 원래 ModelData 를 그대로 돌려준다 (결과가 같다).
장비 수·가공 시간이 바뀌면 같은 seed 라도 난수 소비 순서가 달라져 기준 실행과 경로가 갈라진다.
"""
from __future__ import annotations

import dataclasses
from types import MappingProxyType

from smt2020_sim import data_parser as dp

KEYS = ("tools", "processing_time", "pm")
MULT_RANGE = (0.1, 10.0)


class OverrideError(ValueError):
    def __init__(self, problems: list):
        super().__init__("; ".join(problems))
        self.problems = problems


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _targets(e: dict, data, where: str, problems: list) -> list:
    """toolgroup 또는 area → 장비 있는 툴그룹 목록."""
    if bool(e.get("toolgroup")) == bool(e.get("area")):
        problems.append(f"{where}: toolgroup 과 area 중 하나만 준다")
        return []
    if e.get("toolgroup"):
        s = data.toolgroups.get(e["toolgroup"])
        if s is None or s.is_delay:
            problems.append(f"{where}: 장비가 있는 toolgroup 이 아니다 ({e['toolgroup']!r})")
            return []
        return [e["toolgroup"]]
    tgs = sorted(n for n, s in data.toolgroups.items() if s.area == e["area"] and not s.is_delay)
    if not tgs:
        problems.append(f"{where}: 없는 area ({e['area']!r})")
    return tgs


def _mult(e: dict, key: str, where: str, problems: list) -> float | None:
    v = e.get(key, 1.0)
    if not _num(v) or not MULT_RANGE[0] <= v <= MULT_RANGE[1]:
        problems.append(f"{where}: {key} 는 {MULT_RANGE[0]}~{MULT_RANGE[1]} 사이의 수 ({v!r})")
        return None
    return float(v)


def apply(data, spec: dict):
    """(덮어쓴 ModelData, 추가 outage 이벤트 목록, 요약 문자열 목록). 형식 오류는 OverrideError."""
    ov = spec.get("overrides") or {}
    if not ov:
        return data, [], []
    problems, summary, extra = [], [], []
    for k in sorted(set(ov) - set(KEYS)):
        problems.append(f"overrides: 모르는 항목 {k!r} (가능: {KEYS})")
    end = float(spec["end_week"])

    tgs = dict(data.toolgroups)
    seen_tools = set()
    for k, e in enumerate(ov.get("tools") or []):
        where = f"overrides.tools[{k}]"
        name, delta, s0 = e.get("toolgroup"), e.get("delta"), e.get("start_week", 0)
        s = data.toolgroups.get(name)
        if s is None or s.is_delay:
            problems.append(f"{where}: 장비가 있는 toolgroup 이 아니다 ({name!r})")
            continue
        if name in seen_tools:
            problems.append(f"{where}: 같은 toolgroup 을 두 번 바꿨다 ({name})")
            continue
        seen_tools.add(name)
        if not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
            problems.append(f"{where}: delta 는 0 이 아닌 정수 ({delta!r})")
            continue
        if not _num(s0) or not 0 <= s0 < end:
            problems.append(f"{where}: start_week 는 0 이상 실행 끝({end:g}주) 미만 ({s0!r})")
            continue
        n0 = s.num_tools
        if n0 + delta < 1:
            problems.append(f"{where}: {name} 은 {n0}대라 {-delta}대를 줄이면 남는 장비가 없다")
            continue
        if delta > 0:
            tgs[name] = dataclasses.replace(s, num_tools=n0 + delta)
            if s0 > 0:
                extra.append({"type": "outage", "toolgroup": name, "tools": list(range(n0 + 1, n0 + delta + 1)),
                              "start_week": 0, "duration_weeks": float(s0)})
            summary.append(f"{name} 장비 {n0} → {n0 + delta}대" + (f" (새 장비는 {s0:g}주부터 가동)" if s0 > 0 else ""))
        elif s0 == 0:
            tgs[name] = dataclasses.replace(s, num_tools=n0 + delta)
            summary.append(f"{name} 장비 {n0} → {n0 + delta}대")
        else:
            extra.append({"type": "outage", "toolgroup": name, "tools": list(range(n0 + delta + 1, n0 + 1)),
                          "start_week": float(s0), "duration_weeks": end - float(s0)})
            summary.append(f"{name} 장비 {n0} → {n0 + delta}대 ({s0:g}주부터 {-delta}대 반출)")

    pt = {}
    for k, e in enumerate(ov.get("processing_time") or []):
        where = f"overrides.processing_time[{k}]"
        m = _mult(e, "mult", where, problems)
        targets = _targets(e, data, where, problems)
        for t in targets:
            if t in pt:
                problems.append(f"{where}: {t} 의 가공 시간을 두 번 바꿨다")
            elif m is not None:
                pt[t] = m
        if targets and m is not None and m != 1.0:
            label = e["toolgroup"] if e.get("toolgroup") else f"{e['area']} area (툴그룹 {len(targets)}개)"
            summary.append(f"가공 시간 ×{m:g}: {label}")

    pm = {}
    for k, e in enumerate(ov.get("pm") or []):
        where = f"overrides.pm[{k}]"
        im, dm = _mult(e, "interval_mult", where, problems), _mult(e, "duration_mult", where, problems)
        targets = _targets(e, data, where, problems)
        with_pm = [t for t in targets if data.pm_streams.get(t)]
        if e.get("toolgroup") and targets and not with_pm:
            problems.append(f"{where}: {e['toolgroup']} 에는 PM 이 없다")
        for t in with_pm:
            if t in pm:
                problems.append(f"{where}: {t} 의 PM 을 두 번 바꿨다")
            elif im is not None and dm is not None:
                pm[t] = (im, dm)
        if im is not None and dm is not None and (im != 1.0 or dm != 1.0) and with_pm:
            label = e["toolgroup"] if e.get("toolgroup") else f"{e['area']} area (PM 있는 툴그룹 {len(with_pm)}개)"
            summary.append(f"PM 간격 ×{im:g}, 소요 시간 ×{dm:g}: {label}")
    if problems:
        raise OverrideError(problems)

    changes = {}
    if tgs != dict(data.toolgroups):
        changes["toolgroups"] = MappingProxyType(tgs)
    if any(m != 1.0 for m in pt.values()):
        fab = data.transport.get(("Fab", "Fab"))
        tmean = fab.mean if fab is not None else 0.0
        routes = {}
        for p, r in data.routes.items():
            steps = tuple(
                dataclasses.replace(st, pt_mean=st.pt_mean * pt[st.toolgroup], pt_offset=st.pt_offset * pt[st.toolgroup],
                                    cascade_interval=(st.cascade_interval * pt[st.toolgroup]
                                                      if st.cascade_interval is not None else None))
                if st.toolgroup in pt else st for st in r.steps)
            routes[p] = dataclasses.replace(r, steps=steps, suffix_raw_pt=dp._suffix_raw_pt(steps, tgs),
                                            rpt_paper=dp._rpt_paper(steps, tgs, tmean))
        changes["routes"] = MappingProxyType(routes)
    if any(v != (1.0, 1.0) for v in pm.values()):
        streams = dict(data.pm_streams)
        for t, (im, dm) in pm.items():
            streams[t] = tuple(dataclasses.replace(ps, mtbpm=ps.mtbpm * im, foa=ps.foa * im,
                                                   ttr_mean=ps.ttr_mean * dm, ttr_offset=ps.ttr_offset * dm)
                               for ps in data.pm_streams[t])
        changes["pm_streams"] = MappingProxyType(streams)
    return (dataclasses.replace(data, **changes) if changes else data), extra, summary
