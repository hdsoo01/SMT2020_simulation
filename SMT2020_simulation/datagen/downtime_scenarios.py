"""다운타임 시나리오 — 켜고 끄기: 시나리오 JSON 의 "downtime" 목록. 비우거나 생략하면 원래 모델 그대로다.

  {"type": "outage", "toolgroup": "Litho_FE_111", "n_tools": 2, "start_week": 30, "duration_weeks": 3}
      장비 일부를 지정 기간 정지시킨다 (장기 고장, 장비 반출). 기본은 번호가 낮은 장비부터 n_tools대.
      "tools": [1, 2] 처럼 장비 번호(1부터)를 직접 줄 수도 있다. downtime 기록에는 name "OUTAGE"로 남는다.

  {"type": "breakdown_scale", "area": "Dielectric", "mttr": 1.5, "mttf": 1.0, "start_week": 30, "end_week": 60}
      area 또는 "toolgroup" 의 고장 수리 시간(MTTR)·고장 간격(MTTF) 배율 (장비 노후화·개선).
      배율은 그 시점에 새로 뽑는 TTR/TTF 에 적용된다. end_week 를 생략하면 끝까지.

구현: 시뮬레이터 코드는 고치지 않는다.
  - outage 는 기존 정지 기구(Tool.set_down/set_up, KPI begin/end_downtime)를 그대로 쓰는 별도 프로세스다.
  - breakdown_scale 은 실행 중에만 smt2020_sim.downtime.breakdown_process 를 같은 동작의 배율 판으로
    바꿔 끼우고, 실행이 끝나면 되돌린다. 배율이 1이면 원래 함수와 난수 소비·결과가 같다 (테스트로 확인).
"""
from __future__ import annotations

import math

from smt2020_sim import downtime as _dt

WEEK = 10080.0
TYPES = ("outage", "breakdown_scale")


def validate(events: list, data, end_week: float) -> list:
    """형식 검사. 문제 문자열 목록을 돌려준다 (비면 정상)."""
    probs = []
    areas = {s.area for s in data.toolgroups.values()}
    for k, e in enumerate(events):
        t = e.get("type")
        if t not in TYPES:
            probs.append(f"downtime[{k}]: type 은 {TYPES} 중 하나여야 한다 ({t!r})")
            continue
        if t == "outage":
            tg = data.toolgroups.get(e.get("toolgroup"))
            if tg is None or tg.is_delay:
                probs.append(f"downtime[{k}]: 장비가 있는 toolgroup 이 아니다 ({e.get('toolgroup')!r})")
                continue
            idx = outage_tool_indices(e, tg.num_tools)
            if not idx or max(idx) >= tg.num_tools or min(idx) < 0:
                probs.append(f"downtime[{k}]: 장비 번호/대수가 범위 밖 (장비 {tg.num_tools}대)")
            if e.get("duration_weeks", 0) <= 0:
                probs.append(f"downtime[{k}]: duration_weeks 가 0 이하")
        else:
            if ("area" in e) == ("toolgroup" in e):
                probs.append(f"downtime[{k}]: area 와 toolgroup 중 하나만 준다")
            elif "area" in e and e["area"] not in areas:
                probs.append(f"downtime[{k}]: 없는 area {e['area']!r}")
            elif "toolgroup" in e and e["toolgroup"] not in data.toolgroups:
                probs.append(f"downtime[{k}]: 없는 toolgroup {e['toolgroup']!r}")
            for key in ("mttr", "mttf"):
                if e.get(key, 1.0) <= 0:
                    probs.append(f"downtime[{k}]: {key} 배율은 양수")
        if not 0 <= e.get("start_week", -1) < end_week:
            probs.append(f"downtime[{k}]: start_week 가 실행 기간 밖")
    return probs


def outage_tool_indices(e: dict, num_tools: int) -> list:
    if "tools" in e:
        return sorted(int(i) - 1 for i in e["tools"])
    return list(range(int(e.get("n_tools", 0))))


def affected(e: dict, data) -> list:
    """이 이벤트가 바꾸는 toolgroup 이름 목록."""
    if e["type"] == "outage":
        return [e["toolgroup"]]
    if "toolgroup" in e:
        return [e["toolgroup"]]
    return [n for n, s in data.toolgroups.items() if s.area == e["area"] and not s.is_delay]


def _scaled_breakdown_process(scales: list):
    """smt2020_sim.downtime.breakdown_process 와 같은 동작, TTR/TTF 평균에 시점별 배율만 곱한다."""
    def factor(kind, tool, now):
        f = 1.0
        for s in scales:
            if s["start"] <= now < s["end"] and tool.group.name in s["toolgroups"]:
                f *= s[kind]
        return f

    def breakdown_process(env, tool, spec, rng):
        yield env.timeout(rng.expovariate(1.0 / spec.foa))
        while True:
            tool.set_down()
            tool.down_events += 1
            event_id = tool.kpi.begin_downtime(tool, "breakdown", spec.event_name) if tool.kpi else None
            _dt.interrupt_if_working(tool)
            ttr = rng.expovariate(1.0 / (spec.mttr * factor("mttr", tool, env.now)))
            yield env.timeout(ttr)
            tool.down_time += ttr
            if event_id is not None:
                tool.kpi.end_downtime(event_id)
            tool.set_up()
            yield env.timeout(rng.expovariate(1.0 / (spec.mttf * factor("mttf", tool, env.now))))
    return breakdown_process


def _outage(env, fab, tg_name: str, idx: list, start: float, duration: float):
    yield env.timeout(start)
    tools = [fab.toolgroups[tg_name].tools[i] for i in idx]
    ids = []
    for t in tools:                                   # downtime.breakdown_process 와 같은 순서
        t.set_down()
        t.down_events += 1
        ids.append(t.kpi.begin_downtime(t, "breakdown", "OUTAGE") if t.kpi else None)
        _dt.interrupt_if_working(t)
    yield env.timeout(duration)
    for t, eid in zip(tools, ids):
        t.down_time += duration
        if eid is not None:
            t.kpi.end_downtime(eid)
        t.set_up()


class Installed:
    """install() 결과. 실행이 끝나면 uninstall() 로 원래 함수를 되돌린다."""

    def __init__(self, original):
        self._original = original

    def uninstall(self) -> None:
        if self._original is not None:
            _dt.breakdown_process = self._original
            self._original = None


def install(env, fab, events: list, data) -> Installed:
    """fab.start() **전에** 부른다 (고장 프로세스가 start 에서 만들어지므로)."""
    scales = [{"toolgroups": set(affected(e, data)), "mttr": float(e.get("mttr", 1.0)),
               "mttf": float(e.get("mttf", 1.0)), "start": e["start_week"] * WEEK,
               "end": e["end_week"] * WEEK if e.get("end_week") is not None else math.inf}
              for e in events if e["type"] == "breakdown_scale"]
    original = None
    if scales:
        original = _dt.breakdown_process
        _dt.breakdown_process = _scaled_breakdown_process(scales)
    for e in events:
        if e["type"] == "outage":
            n = data.toolgroups[e["toolgroup"]].num_tools
            env.process(_outage(env, fab, e["toolgroup"], outage_tool_indices(e, n),
                                e["start_week"] * WEEK, e["duration_weeks"] * WEEK))
    return Installed(original)
