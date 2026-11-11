"""고장(breakdown)과 예방정비(PM) — 03_tool_availability.md.

한 phase에 묶은 이유: 정지 상태 관리, 중단·재개, 상태 시간 집계를 공유한다.
따로 만들면 같은 기구를 두 번 만들게 된다.

핵심 규칙:
* R1 고장은 tool마다 독립된 사이클. area는 분포만 제공한다.
* R2 TTF 시계는 자유 주행한다. busy/idle, PM, 다른 고장에 영향받지 않는다.
* R3 첫 TTF만 FOA에서, 이후는 MTTF에서 뽑는다.
* R4 PM 스트림은 tool마다, 스트림마다 독립.
* R5 time 기반 PM의 다음 대기는 `MTBPM - 이번 실제 TTR`.
* R6 counter 기반은 스트림마다 자기 카운터.
* R7 첫 발생은 `FOA x k / N`으로 분산.
* R13 Delay 위치 toolgroup에는 정지가 없다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .logistics import sample_uniform

if TYPE_CHECKING:  # pragma: no cover
    from .model_data import BreakdownSpec, PMStreamSpec
    from .tool import Tool


def interrupt_if_working(tool: "Tool") -> None:
    """이 장비에서 진행 중인 작업을 중단시킨다.

    장비 하나에 작업이 여러 개일 수 있다 (Phase 05의 capacity 2). 단수라고
    못박지 않는다. 중단된 쪽은 남은 시간을 기억했다가 복구 후 이어서 한다 (R9).

    복구를 기다리는 중인 프로세스도 여기서 다시 interrupt 될 수 있다. 그쪽은
    안전하게 흡수하도록 되어 있다 (R12).
    """
    for proc in list(tool.current_processes):
        if proc.is_alive:
            proc.interrupt("tool down")


def first_occurrence(foa: float, index: int, num_tools: int) -> float:
    """tool k(0-based)의 첫 발생 시점 (R7).

    같은 toolgroup의 장비들이 한꺼번에 멈추지 않도록 균등 분산시킨다.
    시간 기반이면 분 단위, counter 기반이면 wafer 수에 같은 식을 쓴다.
    """
    return foa * (index + 1) / max(num_tools, 1)


# ---------------------------------------------------------------------------
# 고장
# ---------------------------------------------------------------------------


def breakdown_process(env, tool: "Tool", spec: "BreakdownSpec", rng: Any):
    """tool 하나의 고장 사이클.

    TTF 시계를 멈추지 않는 것이 중요하다. 수리 중이나 PM 중에 멈추면 area별
    정지 비율이 해석값 `MTTR/(MTTF+MTTR)`보다 낮게 나온다 (R2).
    """
    # 첫 TTF만 FOA 분포. 이후는 MTTF (R3).
    yield env.timeout(rng.expovariate(1.0 / spec.foa))

    while True:
        tool.set_down()
        tool.down_events += 1
        event_id = tool.kpi.begin_downtime(tool, "breakdown", spec.event_name) if tool.kpi else None
        interrupt_if_working(tool)

        ttr = rng.expovariate(1.0 / spec.mttr)
        yield env.timeout(ttr)
        tool.down_time += ttr
        if event_id is not None:
            tool.kpi.end_downtime(event_id)
        tool.set_up()

        yield env.timeout(rng.expovariate(1.0 / spec.mttf))


# ---------------------------------------------------------------------------
# PM
# ---------------------------------------------------------------------------


def pm_time_based(env, tool: "Tool", spec: "PMStreamSpec",
                  first: float, rng: Any):
    """달력 주기 PM.

    MTBPM은 PM 수행시간을 **포함한** 주기다. 그래서 PM이 끝난 뒤 다음까지
    기다리는 시간은 `MTBPM - 이번 실제 TTR`이다 (R5). 그냥 MTBPM을 기다리면
    PM 비율이 해석값보다 높게 나온다.
    """
    yield env.timeout(first)

    while True:
        tool.set_down()
        tool.pm_events_count += 1
        event_id = tool.kpi.begin_downtime(tool, "pm", spec.event_name) if tool.kpi else None
        interrupt_if_working(tool)

        ttr = sample_uniform(spec.ttr_mean, spec.ttr_offset, rng)
        yield env.timeout(ttr)
        tool.pm_time += ttr
        if event_id is not None:
            tool.kpi.end_downtime(event_id)
        tool.set_up()

        yield env.timeout(max(spec.mtbpm - ttr, 0.0))


def pm_counter_based(env, tool: "Tool", spec: "PMStreamSpec",
                     first_threshold: float, rng: Any):
    """처리 wafer 수 기반 PM.

    스트림마다 자기 카운터를 갖는다 (R6). 이 스트림의 PM은 이 스트림의
    카운터만 0으로 되돌린다. 세 스트림(WK/MN/QT)이 카운터를 공유하면 서로를
    리셋해서 함께 발화하거나 아예 발화하지 않는다.

    polling 하지 않는다. `Tool.add_wafers`가 임계 도달 시 이벤트를 발화한다.
    """
    name = spec.event_name
    tool.pm_counters[name] = 0.0
    tool.pm_thresholds[name] = first_threshold
    tool.pm_events[name] = env.event()

    while True:
        if tool.pm_counters[name] < tool.pm_thresholds[name]:
            yield tool.pm_events[name]

        tool.set_down()
        tool.pm_events_count += 1
        event_id = tool.kpi.begin_downtime(tool, "pm", spec.event_name) if tool.kpi else None
        interrupt_if_working(tool)

        ttr = sample_uniform(spec.ttr_mean, spec.ttr_offset, rng)
        yield env.timeout(ttr)
        tool.pm_time += ttr
        if event_id is not None:
            tool.kpi.end_downtime(event_id)
        tool.set_up()

        # 자기 카운터만 되돌린다. 첫 주기 이후 임계는 MTBPM이다.
        tool.pm_counters[name] = 0.0
        tool.pm_thresholds[name] = spec.mtbpm
        tool.pm_events[name] = env.event()


# ---------------------------------------------------------------------------
# 기동
# ---------------------------------------------------------------------------


def start_downtime(env, fab, rng: Any) -> dict[str, int]:
    """모든 장비에 고장/PM 프로세스를 건다.

    Delay 위치 toolgroup은 제외한다 (R13). 실제 장비 913대 기준으로
    고장 913개 + PM 2,602개 = 3,515개 프로세스가 생긴다.
    """
    counts = {"breakdown": 0, "pm_time": 0, "pm_counter": 0}

    for tg in fab.toolgroups.values():
        if tg.spec.is_delay:
            continue
        bd = fab.data.breakdowns.get(tg.spec.area)
        streams = fab.data.pm_streams.get(tg.name, ())
        n = len(tg.tools)

        for k, tool in enumerate(tg.tools):
            if bd is not None:
                env.process(breakdown_process(env, tool, bd, rng))
                counts["breakdown"] += 1
            for spec in streams:
                first = first_occurrence(spec.foa, k, n)
                if spec.counter_based:
                    env.process(pm_counter_based(env, tool, spec, first, rng))
                    counts["pm_counter"] += 1
                else:
                    env.process(pm_time_based(env, tool, spec, first, rng))
                    counts["pm_time"] += 1

    return counts
