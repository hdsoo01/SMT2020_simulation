"""디스패치 판단 로그 — full 수집 전용 계측.

배정이 확정되기 **직전**의 상태로 "무엇을 골랐고, 장비별 조건을 빼면 1순위였던
lot은 누구이며, 그 lot이 왜 안 뽑혔나"를 한 행으로 남긴다. full 이력은 결과
(lot·장비·배치 타임라인)를 전부 남기지만 판단 근거(후보 순위, 필터 상태)는
남기지 않아서, 규칙이 지켜졌는지 사후에 증명할 수 없었다.

**결과를 바꾸지 않는다.** 여기서 부르는 것은 부수효과가 없는 판정 함수뿐이다
(`pair_rank`, `_minrun_candidates`, `select_batch_group`, `Tool.can_accept`,
`critical_ratio` — 07 R10). `select_lot`은 부르지 않는다: 안에서 MINRUN 계수를
올리므로 스냅샷 카운터가 바뀐다. 난수·이벤트·대기열도 건드리지 않는다.

결정 지점은 셋이고 `path`로 구분한다:
  arrival  lot이 도착하자마자 빈 장비를 잡는 빠른 경로 (`_acquire_tool`)
  server   toolgroup 배정 서버 (`_try_dispatch`)
  batch    배치 형성 (`_try_form_batch`)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .dispatching import _minrun_candidates, pair_rank, pair_setup_rank3, select_batch_group

if TYPE_CHECKING:  # pragma: no cover
    from .dispatching import DispatchContext
    from .lot import Lot
    from .tool import Tool, ToolGroup

# 표 정의는 여기 하나다. collector는 이 순서로 열을 만들고 값을 넣는다.
# visit_id: lot 행은 고른 lot의 방문, batch 행은 대표 구성원의 방문이다. batch_id는
# 배치 프로세스가 시작될 때(`_run_batch` -> `begin_batch`) 붙으므로 여기서는 알 수
# 없다 — 같은 시각에 다른 toolgroup의 배치가 끼어들 수 있어 미리 추정하지 않는다.
# full 이력의 `visits.batch_id`로 이어 붙인다.
COLUMNS = (
    ("decision_id", "INTEGER PRIMARY KEY"), ("path", "TEXT"), ("time_min", "REAL"),
    ("toolgroup", "TEXT"), ("station", "TEXT"), ("visit_id", "INTEGER"),
    ("product", "TEXT"), ("step_index", "INTEGER"),
    ("priority", "INTEGER"), ("setup_cost", "REAL"), ("rank3", "REAL"),
    ("queue_len", "INTEGER"), ("free_tools", "INTEGER"), ("minrun", "TEXT"),
    ("n_better", "INTEGER"), ("top_visit_id", "INTEGER"), ("top_priority", "INTEGER"),
    ("top_rank3", "REAL"), ("top_reason", "TEXT"), ("members", "INTEGER"),
    ("wafers", "INTEGER"), ("groups_eligible", "INTEGER"), ("short_groups", "INTEGER"),
    ("short_max_priority", "INTEGER"), ("reordered", "INTEGER"),
)

# top_reason 값. 장비별 거절 사유는 `|`로 합쳐진다 (예: "gate|ltl").
#   top                    고른 lot이 1순위다
#   setup_cost             1순위는 받을 장비가 있었지만 2순위(setup 시간)에서 졌다
#   tie                    1순위와 같은 키 — 장비 index 동점 처리
#   better_pair_available  1순위가 더 나은 쌍으로 받을 수 있었다. server에서 0이어야
#                          한다. arrival에서는 같은 시각에 풀린 장비와의 순서 차이다
#   minrun / down / full / gate / setup_conflict / ltl / reserved
#                          빈 장비마다 1순위를 거절한 이유 (`can_accept` 순서 그대로)
#   unclassified           `can_accept`는 거절했는데 이름을 못 붙였다 — 조건이 늘어
#                          여기와 어긋났다는 뜻이다. 0이어야 한다
#   no_tool                (lot) 빈 장비가 없다 / (batch) 1위 그룹이 장비를 못 잡았다


def _refusal(tool: "Tool", lot: "Lot", step) -> str:
    """`can_accept`가 거절한 이유의 이름. 판정은 `can_accept`가 하고 여기는
    이름만 붙인다 — 조건을 같은 순서로 따른다 (tool.py)."""
    if tool.is_down:
        return "down"
    if len(tool.resource.users) >= tool.resource.capacity:
        return "full"
    if tool.gate_closed:
        return "gate"
    if tool.setup_conflict(step):
        return "setup_conflict"
    forced = getattr(lot, "ltl_forced", None)
    if forced is not None and forced is not tool:
        return "ltl"
    if tool.reserved_for is not None and tool.reserved_for != lot.lot_id:
        return "reserved"
    return "unclassified"


def _why_not_top(tg: "ToolGroup", top: "Lot", chosen_key: tuple,
                 ctx: "DispatchContext", now: float) -> str:
    """1순위 lot이 이번 배정에서 빠진 이유. 빈 장비를 전부 본다."""
    step = top.current_step()
    labels, keys = set(), []
    for t in tg.free_tools:
        candidates, _ = _minrun_candidates(tg.queue, t, tg, ctx)
        if not any(c is top for c in candidates):
            labels.add("minrun")
        elif t.can_accept(top, step):
            keys.append(pair_rank(top, t, step, ctx, now))
        else:
            labels.add(_refusal(t, top, step))
    if keys:
        best = min(keys)
        if best[:3] < chosen_key[:3]:
            return "better_pair_available"
        b_setup = pair_setup_rank3(best, ctx, tg.name, now)[0]          # 디스패칭 베타면 자리가 바뀐다
        c_setup = pair_setup_rank3(chosen_key, ctx, tg.name, now)[0]
        return "setup_cost" if b_setup > c_setup else "tie"
    return "|".join(sorted(labels)) or "no_tool"


def lot_decision(path: str, tg: "ToolGroup", tool: "Tool", lot: "Lot",
                 ctx: "DispatchContext", now: float):
    """단일 lot 배정 1건 -> (행, 고른 lot, 1순위 lot 또는 None).

    `_commit` **직전**에 부른다 — 고른 lot이 아직 대기열에 있고 장비도 빈
    목록에 있다. "1순위"는 장비와 무관한 순위(우선순위 -> 3순위 -> 도착
    순서)다. 2순위(setup)는 장비마다 달라 1순위 판정에 쓰지 않고, 빠진 이유를
    가를 때 쓴다.
    """
    step = lot.current_step()
    name = tg.name
    keys = [(-l.priority, ctx.tier3(name, l, now), i) for i, l in enumerate(tg.queue)]
    # 고른 lot은 구조상 대기열에 있다(`_commit`이 거기서 뺀다). 없더라도 계측 때문에
    # 실행이 멈추면 안 되므로 맨 뒤 도착으로 본다.
    pos = next((i for i, l in enumerate(tg.queue) if l is lot), None)
    mine = keys[pos] if pos is not None else (-lot.priority, ctx.tier3(name, lot, now), len(keys))
    top_i = min(range(len(keys)), key=keys.__getitem__) if keys else None
    top = tg.queue[top_i] if top_i is not None and keys[top_i] < mine else lot
    chosen_key = pair_rank(lot, tool, step, ctx, now)
    reason = "top" if top is lot else _why_not_top(tg, top, chosen_key, ctx, now)
    setup_cost, rank3 = pair_setup_rank3(chosen_key, ctx, name, now)
    row = dict(path=path, time_min=now, toolgroup=name, station=tool.tool_id,
               product=lot.product, step_index=lot.current_step_index,
               priority=lot.priority, setup_cost=setup_cost, rank3=rank3,
               queue_len=len(tg.queue), free_tools=len(tg.free_tools),
               minrun=_minrun_candidates(tg.queue, tool, tg, ctx)[1],
               n_better=sum(k < mine for k in keys),
               top_priority=top.priority, top_rank3=ctx.tier3(name, top, now), top_reason=reason)
    return row, lot, (None if top is lot else top)


def batch_decision(tg: "ToolGroup", eligible: list, remaining: list, picked: tuple,
                   members: list, fifo_members: list, ctx: "DispatchContext",
                   now: float):
    """배치 형성 1건 -> (행, 대표 구성원, None).

    대기열에서 구성원을 빼고 장비를 잡기 **전**에 부른다. 수량 미달 그룹의
    최고 우선순위를 함께 남긴다 — 우선순위는 배치 형성에 개입하지 않으므로
    (04 R10) hot lot이 미달 그룹에서 기다리는 동안 다른 배치가 나갈 수 있다.
    그 시간을 이 열로 잰다.
    """
    key, waiting, _step, tool = picked
    name = tg.name
    top = select_batch_group(eligible, None, tg, ctx)
    short = [w for k, w in tg.batch_queue.items()
             if w and tg.batch_wafers[k] < w[0].current_step().batch_min]
    row = dict(path="batch", time_min=now, toolgroup=name, station=tool.tool_id,
               product=key[0], step_index=key[1],
               priority=max(l.priority for l in waiting),
               rank3=min(ctx.tier3(name, l, now) for l in waiting),
               queue_len=tg.waiting_count(), free_tools=len(tg.free_tools),
               n_better=len(eligible) - len(remaining),
               top_priority=max(l.priority for l in top[1]),
               top_rank3=min(ctx.tier3(name, l, now) for l in top[1]),
               top_reason="top" if top[0] == key else "no_tool",
               members=len(members), wafers=sum(l.wafers_per_lot for l in members),
               groups_eligible=len(eligible), short_groups=len(short),
               short_max_priority=max((l.priority for w in short for l in w), default=None),
               reordered=int([id(l) for l in members] != [id(l) for l in fifo_members]))
    return row, members[0], None
