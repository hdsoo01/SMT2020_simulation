"""lot 선택과 tool 선택 — 07_dispatching.md.

데이터셋은 **106개 toolgroup 전부에 같은 3단 순위**를 지정한다:

  1 우선순위 높은 것        superhot(30) > hot(20) > regular(10)
  2 setup 시간 적은 것      같은 우선순위 안에서, 이 장비가 가장 적게
                           재구성하고 시작할 수 있는 lot
  3 critical ratio 낮은 것  (납기까지 남은 시간) / (남은 작업). 작을수록 급하다

여기에 규칙 둘이 더 붙는다:

  wake-up      lot이 도착했을 때 받을 수 있는 장비가 여럿이면, setup이 가장
               적게 드는 장비를 고른다. 9개 toolgroup에만 정의돼 있고, 그
               9개는 setup을 쓰는 toolgroup과 정확히 같다.
  setup 회피   gas setup 그룹이 걸린 implant toolgroup 3개는 한 구성으로
               최소 7 lot을 돌린 뒤에 바꾼다.

**집합은 전부 데이터에서 유도한다.** 이 파일에 toolgroup 이름이 나오면 버그다
(07 2절).

설계 제약 (하나라도 어기면 같은 seed에서 결과가 흔들린다):
  R2  순위는 안정적이어야 하고, 순서 없는 컬렉션을 훑으면 안 된다.
      마지막 동점은 **도착 순서**로 깬다.
  R3  2순위의 setup 시간은 **장비마다 다르다.** 한 번 계산해 장비들 사이에
      돌려 쓸 수 없다.
  R4  critical ratio는 표본이 아니라 평균으로 계산한다. route 위치가 같으면
      항상 같은 값이다.
  R10 후보 평가에 부수효과가 없다. 상태는 lot이 장비에 확정될 때만 바뀐다.

비용 (07 3.1절): `select_lot`은 **디스패치당 한 번** 돈다. toolgroup마다
배정 서버가 하나이기 때문이다(`FabModel._tool_server`). 예전처럼 장비 반납마다
대기 lot 전원을 깨우면 2순위가 반납 1회당 O(대기 수^2)가 되고, 이 데이터셋의
병목 대기열은 수천 개다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from . import config
from .setup import required_setup, setup_duration

if TYPE_CHECKING:  # pragma: no cover
    from .lot import Lot
    from .model_data import ModelData, Step
    from .setup import SetupModel
    from .tool import Tool, ToolGroup

# 최소 실행이 걸렸을 때 무엇이 후보가 되었는지. 보고용이다.
MINRUN_NA = "n/a"              # 이 toolgroup은 setup 회피 대상이 아니다
MINRUN_MET = "met"             # 최소를 이미 채웠다
MINRUN_SAME = "same-setup"     # 같은 setup lot으로 제한했다
MINRUN_OVERRIDE = "override"   # 우선순위가 최소를 이겼다
MINRUN_CHANGE = "change"       # 같은 setup이 없어 바꾼다 (STRICT=False)
MINRUN_IDLE = "idle"           # 같은 setup이 없어 놀린다 (STRICT=True)


@dataclass(slots=True)
class DispatchContext:
    """선택 함수에 넘기는 입력.

    필드가 늘어도 호출부는 바뀌지 않는다 (02_core_flow.md R11).
    유도 집합은 한 번 만들고 읽기만 한다 — 디스패치마다 다시 만들면 비싸다.
    """
    env: Any
    data: "ModelData"
    rng: Any
    setup_model: "SetupModel | None" = None

    # super hot lot이 장비를 예약하는가 (`config.SUPERHOT_RESERVATION`).
    # 여기 필드로 받는 이유는 **한 프로세스에서 두 설정을 돌릴 수 있어야**
    # 하기 때문이다. 전역을 읽으면 A/B가 서로를 덮어쓴다.
    reservation: bool = True

    # 3순위를 FIFO로 강제한다 — **실험 전용**, 기본은 시트를 따른다.
    #
    # **전역 설정으로 두지 않는다.** dataset 2에서 의미 있는 값이 `"fifo"`
    # 하나뿐이고, 그 값은 모델을 벤치마크가 아닌 것으로 바꾼다. 전역이면
    # "켜 둔 채 레퍼런스 대조를 돌리는" 경로가 생기는데, 방금 고친 결함이
    # 바로 그 부류(조용히 틀린 기본값)였다. 여기 필드로 두면 실험이 호출부에
    # 명시적으로 드러나고, 실수로 남아 있을 수가 없다.
    #
    # 쓸 자리: G-26의 질문 — regular FF 분포가 납기 난수 z를 그대로 베낀
    # 모양인데(b/a 1.548 대 z의 1.500), CR을 빼면 폭이 줄어드는가.
    rank3_fifo: bool = False

    setup_avoidance: frozenset = field(default_factory=frozenset, init=False)
    wake_up: frozenset = field(default_factory=frozenset, init=False)
    setup_bearing: frozenset = field(default_factory=frozenset, init=False)
    # 3순위가 critical ratio가 **아닌** toolgroup. 비어 있으면 전부 CR이다.
    fifo_toolgroups: frozenset = field(default_factory=frozenset, init=False)
    # 디스패칭 순서 변경 (베타, 2026-11-09, SLLMT D-88). toolgroup -> (시작 분, 끝 분, 3순위 FIFO, CR을 setup보다 먼저).
    # **1순위(lot 우선순위)는 바꾸지 않는다.** 비어 있으면 아래 두 메서드가 옛 판정과 같은 값을 돌려준다 —
    # 규칙이 없는 실행은 결과가 비트 단위로 같아야 한다 (datagen/scenario.py "dispatch", set_dispatch_rules).
    dispatch_rules: dict = field(default_factory=dict, init=False)
    minrun_stats: dict = field(default_factory=dict, init=False)
    # setup 후보 필터의 계수 (`note_setup_filter`). 보고용이다.
    setup_filter_stats: dict = field(default_factory=dict, init=False)

    # Phase 08 — 제품 -> step_index -> (체인 id, 위치)
    ltl_chains: dict = field(default_factory=dict, init=False)
    ltl_toolgroups: frozenset = field(default_factory=frozenset, init=False)

    def __post_init__(self) -> None:
        if self.data is None:
            return
        # DISPATCHING 값이 다수와 다른 toolgroup = setup 회피 대상.
        # wake-up은 TOOL WAKE UP Ranking이 비어 있지 않은 toolgroup.
        self.setup_avoidance = frozenset(self.data.setup_avoidance_toolgroups())
        self.wake_up = frozenset(self.data.wake_up_toolgroups())

        from .data_parser import build_ltl_chains
        self.ltl_chains = build_ltl_chains(self.data.routes)
        self.ltl_toolgroups = frozenset(
            self.data.routes[p].steps[i].toolgroup
            for p, idx in self.ltl_chains.items() for i in idx)
        # setup step이 하나라도 있는 toolgroup (9개). 나머지 97개에서는
        # 2순위가 **모든 후보에 0**이라 판별에 기여하지 못한다 — 계산을
        # 건너뛴다. 병목 대기열이 수천 개인 곳이 전부 여기에 속한다.
        self.setup_bearing = frozenset(self.data.setup_bearing_toolgroups())

        # 3순위는 **시트가 정한다** (`Ranking 3`). 예전에는 CR을 박아 두었고,
        # dataset 2는 106개 전부 `Critical Ratio`라 결과가 맞았다 — 그래서
        # 틀린 줄 몰랐다. generic 문서는 같은 컬럼을 "FIFO (dataset 1) 또는
        # Critical Ratio (dataset 2)"로 적으므로, 박아 두면 dataset 1에서
        # **조용히 CR을 쓴다** (DECISIONS G-27).
        #
        # 값이 아니라 **의미**로 판정한다 — 컬럼에 `Critical Ratio`가 아닌 것이
        # 오면 도착 순서로 본다. 3순위가 빠지면 마지막 동점 처리(도착 순서)가
        # 그대로 FIFO가 되므로, 문서의 `rank_HP -> rank_RSETUP -> rank_FIFO`와
        # 같은 식이 된다.
        if self.rank3_fifo:
            self.fifo_toolgroups = frozenset(self.data.toolgroups)   # 실험
        else:
            self.fifo_toolgroups = frozenset(
                n for n, s in self.data.toolgroups.items()
                if "critical" not in (s.rank3 or "").lower())

    def set_dispatch_rules(self, rules) -> None:
        """디스패칭 순서 변경 (베타). rules: [{"toolgroups", "start_min", "end_min", "rank3", "cr_before_setup"}].

        형식·대상 검사는 datagen/scenario.py `_check_dispatch`가 한다 (한 toolgroup에 규칙 하나, 둘 중 하나만,
        CR-먼저는 setup toolgroup만). 여기서는 다시 검사하지 않고 그대로 담는다."""
        out = {}
        for r in rules or ():
            for name in r["toolgroups"]:
                out[name] = (float(r["start_min"]), float(r["end_min"]),
                             r.get("rank3") == "fifo", bool(r.get("cr_before_setup")))
        self.dispatch_rules = out

    def rank3_is_fifo(self, toolgroup_name: str, now: float) -> bool:
        """3순위가 도착 순서인가. 규칙이 없으면 시트(`fifo_toolgroups`) 그대로."""
        r = self.dispatch_rules.get(toolgroup_name) if self.dispatch_rules else None
        if r is not None and r[2] and r[0] <= now < r[1]:
            return True
        return toolgroup_name in self.fifo_toolgroups

    def cr_first(self, toolgroup_name: str, now: float) -> bool:
        """critical ratio를 setup 시간보다 먼저 보는가 (2·3순위 교환). 규칙이 없으면 False (시트 순서)."""
        r = self.dispatch_rules.get(toolgroup_name) if self.dispatch_rules else None
        return r is not None and r[3] and r[0] <= now < r[1]

    def tier3(self, toolgroup_name: str, lot: "Lot", now: float) -> float:
        """3순위 값. CR이면 critical ratio, FIFO면 상수 (R1).

        상수를 돌려주면 그 항이 순위에서 빠지고 **마지막 동점 처리(도착
        순서)가 결정**한다. 그것이 곧 FIFO다 — 별도 분기를 만들 필요가 없다.
        """
        if self.rank3_is_fifo(toolgroup_name, now):
            return 0.0
        return lot.critical_ratio(now)

    # -- 보고용 계수. 선택 자체에는 영향이 없다 -------------------------------

    def note_minrun(self, outcome: str) -> None:
        self.minrun_stats[outcome] = self.minrun_stats.get(outcome, 0) + 1

    def note_setup_filter(self, outcome: str) -> None:
        """setup 후보 필터가 실제로 후보를 줄인 횟수 (`select_lot` 참고).

        `dropped`  줄었지만 남았다 — 여기서 놓칠 뻔한 쌍을 건진다
        `emptied`  전부 걸러졌다 — 원래도 못 돌렸으니 결과가 같다
        """
        self.setup_filter_stats[outcome] = (
            self.setup_filter_stats.get(outcome, 0) + 1)


def _setup_cost(ctx: DispatchContext, tool: "Tool", step: "Step") -> float:
    """이 장비가 이 step을 시작하는 데 드는 setup 시간 (R3).

    장비마다 다르다 — `tool.current_setup`이 들어간다. 순수하고 난수를
    쓰지 않는다 (06 R12).
    """
    if ctx.setup_model is None or step is None:
        return 0.0
    return setup_duration(ctx.setup_model, step, tool.current_setup)[0]


def _minrun_candidates(queue: "Sequence[Lot]", tool: "Tool",
                       toolgroup: "ToolGroup", ctx: DispatchContext):
    """최소 실행 규칙을 적용한 후보 목록과 그 사유 (R8, 3.2절).

    **적용 순서를 명시한다.** 07 3.2절이 경고한 자기모순 — "override를 켜 두고도
    다른 setup의 hot lot이 기다리는데 같은 setup의 regular lot을 반환" — 은
    순서를 안 정해서 생긴다. 여기서는:

      1) 이 toolgroup이 setup 회피 대상이 아니거나 최소를 채웠으면 제한 없음
      2) 같은 setup lot이 없고 override가 켜져 있으면 hot/superhot만 허용한다.
         긴급 lot이 없거나 override가 꺼져 있으면 STRICT가 결정한다.
      3) 같은 setup lot이 있는데 override가 켜져 있고, 전체 대기열의 최고
         우선순위가 그 무리의 최고보다 **높으면** -> 제한 풀기
      4) 그 외에는 같은 setup lot만 후보

    3이 4보다 **위**에 있다. 그래서 hot lot이 기다리는 동안 같은 setup의
    regular lot이 먼저 나가는 일이 없다 — 3.2절이 경고한 자기모순이다.

    빈 무리의 최고 우선순위를 -1로 비교하면 regular 하나도 override가 된다.
    따라서 빈 무리에서는 Lot의 종류를 명시적으로 확인한다. STRICT는 regular에
    적용하고, 명세의 "hot lot ... can trigger a setup" 예외는 단독 긴급 lot에도
    적용한다. 작업 중인 장비의 setup 충돌은 select_lot의 can_accept 필터가 지킨다.
    """
    if toolgroup.name not in ctx.setup_avoidance:
        return queue, MINRUN_NA

    model = ctx.setup_model
    minrun = None if model is None else model.min_run.get(tool.current_setup)
    if minrun is None or tool.run_count >= minrun:
        return queue, MINRUN_MET

    same = [lot for lot in queue
            if required_setup(lot.current_step()) == tool.current_setup]

    if not same:
        if config.MINRUN_HOTLOT_OVERRIDE:
            urgent = [lot for lot in queue if lot.lot_type in ("hot", "superhot")]
            if urgent:
                return urgent, MINRUN_OVERRIDE
        if config.MINRUN_STRICT:
            return [], MINRUN_IDLE
        return queue, MINRUN_CHANGE

    if config.MINRUN_HOTLOT_OVERRIDE:
        best_all = max(lot.priority for lot in queue)
        best_same = max(lot.priority for lot in same)
        if best_all > best_same:
            return queue, MINRUN_OVERRIDE

    return same, MINRUN_SAME


def select_lot(queue: "Sequence[Lot]", tool: "Tool", toolgroup: "ToolGroup",
               ctx: DispatchContext) -> "Lot | None":
    """이 장비가 다음에 처리할 lot (R1).

    우선순위 내림차순 -> setup 시간 오름차순 -> critical ratio 오름차순 ->
    도착 순서. 마지막 항이 없으면 동점에서 순서가 정해지지 않아 같은 seed로도
    결과가 흔들린다 (R2).

    부수효과가 없다 (R10). 계수는 `ctx.note_minrun`이 따로 한다 — 선택 결과에
    영향을 주지 않는다.
    """
    if not queue:
        return None

    candidates, outcome = _minrun_candidates(queue, tool, toolgroup, ctx)
    ctx.note_minrun(outcome)

    # lot마다 받을 수 있는 장비가 다른 toolgroup에서는, **이 장비가 받을 수
    # 있는 lot만** 후보다 (07 R7을 대칭으로 읽은 것).
    #
    # **거는 자리가 둘이다.** `can_accept`가 lot에 의존하는 경로가 둘이기
    # 때문이다:
    #
    #   1. LTL 지정 (`lot.ltl_forced`) — litho 3개. 지정이 붙은 lot을 그
    #      장비가 아닌 곳의 1순위로 올리면 뒤 lot까지 함께 막힌다.
    #   2. setup 충돌 (`Tool.setup_conflict`) — setup을 쓰는 9개 중 **장비가
    #      이미 차 있을 때.** cascading 장비(슬롯 2개)는 지금 들고 있는 setup과
    #      같은 lot만 받는다. 비어 있으면 무엇이든 받으므로 `is_loaded`가 조건이다.
    #
    # 2를 빼먹으면 이렇게 샌다: `select_lot`은 lot을 **하나만** 돌려주므로,
    # 1등이 다른 setup이면 `_try_dispatch`가 그 장비를 통째로 건너뛰고
    # (`can_accept` 실패 -> `continue`) 슬롯이 논다. 대기열 뒤에 같은 setup lot이
    # 있어도 그렇다. 실행 가능한 (lot, 장비) 쌍을 놓치는 것이고, R6/R8 수정이
    # **장비 축**에서 고친 것과 같은 결함의 **lot 축**이다 (가설 md 7절).
    #
    # 실제 배정 경로로 재현했다: cascading이면서 setup을 쓰는 Implant 5개
    # (119/90/128/132/91) 전부에서, 같은 setup lot이 대기열에 있는데도
    # `_try_dispatch`가 False를 냈다. MINRUN 상태와 무관하다 — 오히려
    # hot lot override가 같은-setup 제한을 풀어 버려서 더 잘 드러난다.
    #
    # 나머지 toolgroup에서는 걸지 않는다. capacity 1이면 차 있는 장비는 애초에
    # 후보가 아니라 `setup_conflict`가 lot에 의존하지 않고, 병목 대기열
    # (Def_Met 6,700개)에서 비용만 든다.
    setup_busy = toolgroup.name in ctx.setup_bearing and tool.is_loaded
    if setup_busy or toolgroup.name in ctx.ltl_toolgroups:
        before = len(candidates)
        candidates = [lot for lot in candidates
                      if tool.can_accept(lot, lot.current_step())]
        if setup_busy and len(candidates) < before:
            # 보고용. 선택에는 영향이 없다 (R10).
            ctx.note_setup_filter("emptied" if not candidates else "dropped")

    if not candidates:
        return None                       # STRICT로 놀리거나, 받을 lot이 없다

    if len(candidates) == 1:
        return candidates[0]

    now = ctx.env.now

    # setup이 없는 toolgroup(106개 중 97개)에서는 2순위가 모든 후보에 0이라
    # 순위를 가르지 못한다. 그런데도 후보마다 계산하면 병목 대기열에서 비용이
    # 그대로 든다 — Def_Met 대기열은 6,700개다. 결과가 같으므로 건너뛴다.
    # 3순위는 시트가 정한다 (`Ranking 3`). CR이 아니면 그 항이 빠지고 마지막
    # 동점 처리(도착 순서)가 결정한다 = FIFO (G-27).
    cr = not ctx.rank3_is_fifo(toolgroup.name, now)

    if toolgroup.name in ctx.setup_bearing and cr and ctx.cr_first(toolgroup.name, now):
        def rank(indexed):                               # 디스패칭 베타: CR을 setup보다 먼저
            i, lot = indexed
            return (-lot.priority,                       # 1순위 (그대로)
                    lot.critical_ratio(now),             # critical ratio
                    _setup_cost(ctx, tool, lot.current_step()),   # setup (장비별)
                    i)
    elif toolgroup.name in ctx.setup_bearing and cr:
        def rank(indexed):
            i, lot = indexed
            return (-lot.priority,                       # 1순위
                    _setup_cost(ctx, tool, lot.current_step()),   # 2순위 (장비별)
                    lot.critical_ratio(now),             # 3순위
                    i)                                   # 동점은 도착 순서
    elif toolgroup.name in ctx.setup_bearing:
        def rank(indexed):
            i, lot = indexed
            return (-lot.priority,
                    _setup_cost(ctx, tool, lot.current_step()),
                    i)
    elif cr:
        def rank(indexed):
            i, lot = indexed
            return (-lot.priority, lot.critical_ratio(now), i)
    else:
        def rank(indexed):
            i, lot = indexed
            return (-lot.priority, i)

    return min(enumerate(candidates), key=rank)[1]


def select_tool(toolgroup: "ToolGroup", lot: "Lot", step: "Step",
                ctx: DispatchContext) -> "Tool | None":
    """이 lot을 처리할 장비 (R6, R7).

    R7 후보는 "지금 사용자가 없는 장비"가 아니라 "**받을 수 있는** 장비"다.
       capacity 2에서 "비었는가"로 물으면 두 번째 슬롯이 조용히 죽는다.
    R6 wake-up 대상 toolgroup에서는 setup이 가장 적게 드는 후보를 고르고,
       동점은 장비 index로 깬다. 나머지는 index 순 첫 번째다.

    `free_tools`만 훑는다. 자리가 없는 장비는 어차피 `can_accept`가 거부하므로
    결과는 같고, 118대짜리 toolgroup에서 비용이 크게 준다.
    """
    if toolgroup.name not in ctx.wake_up:
        for tool in toolgroup.free_tools:            # index 순
            if tool.can_accept(lot, step):
                return tool
        return None

    best = None
    best_key = None
    for tool in toolgroup.free_tools:                # index 순 -> 동점 안정
        if not tool.can_accept(lot, step):
            continue
        key = _setup_cost(ctx, tool, step)
        if best_key is None or key < best_key:
            best, best_key = tool, key
            if key == 0.0:
                break                                # 더 나을 수 없다
    return best


def pair_rank(lot: "Lot", tool: "Tool", step: "Step",
              ctx: DispatchContext, now: float) -> tuple:
    """(lot, 장비) 쌍의 순위 키. 작을수록 먼저다. 순수하다 (R10).

    **장비를 먼저 고를 수 없기 때문에 있는 함수다.** 2순위(setup 시간)가
    장비마다 다르므로(R3), 장비가 바뀌면 그 장비가 고를 lot도 바뀐다. 즉
    "lot 고르기"와 "장비 고르기"는 따로 풀 수 있는 두 문제가 아니라 **쌍
    하나를 고르는 한 문제**다.

    키는 `select_lot`과 **같은 3단 순위**다. 그래서 wake-up 규칙(R6,
    "빈 장비가 여럿이면 재구성이 가장 적은 장비")을 따로 적지 않는다 —
    같은 lot을 여러 장비가 최선으로 꼽으면 앞 세 항이 같아지고 2순위가
    setup이 적은 장비를 고르기 때문이다. 규칙을 새로 만들면 `select_lot`과
    `select_tool` 사이에 세 번째 기준이 생겨 어긋날 자리가 하나 늘어난다.

    마지막 항이 장비 index인 것도 R6 그대로다 ("ties by tool index").

    **setup이 없는 97개 toolgroup에서는 이 키가 옛 동작과 정확히 같다.**
    거기서는 `select_lot`의 순위에 setup이 들어가지 않아 **모든 장비가 같은
    lot을 고르고**, 따라서 앞 세 항이 전부 같아져 index가 가장 작은 장비가
    이긴다 — 옛 코드의 "index 순으로 훑다 첫 적합 장비"와 같은 결과다.
    바뀌는 것은 setup이 있는 9개뿐이고, 그 9개가 R6의 적용 대상이다.

    디스패칭 베타(`ctx.cr_first`)면 2·3번째 항이 바뀐다 — `select_lot`과 같은 순서를 지킨다.
    setup·3순위 값을 꺼낼 때는 자리 대신 `pair_setup_rank3`를 쓴다.
    """
    name = tool.group.name
    if ctx.cr_first(name, now):
        return (-lot.priority,                      # 1순위 (그대로)
                ctx.tier3(name, lot, now),          # critical ratio (베타: setup보다 먼저)
                _setup_cost(ctx, tool, step),       # setup
                tool.index)
    return (-lot.priority,                          # 1순위
            _setup_cost(ctx, tool, step),           # 2순위 (장비별 — R3)
            ctx.tier3(name, lot, now),              # 3순위 (시트가 정한다)
            tool.index)                             # 동점은 장비 index (R6)


def pair_setup_rank3(key: tuple, ctx: DispatchContext, toolgroup_name: str, now: float) -> tuple:
    """`pair_rank` 키에서 (setup 비용, 3순위 값). 디스패칭 베타로 자리가 바뀌어도 같은 뜻을 꺼낸다."""
    if ctx.cr_first(toolgroup_name, now):
        return key[2], key[1]
    return key[1], key[2]


# ---------------------------------------------------------------------------
# Lot-to-lens dedication (Phase 08)
#
# 명세: "exactly the same tool must be used for process step i and process
# step j (i < j)". 강제이지 선호가 아니다 — 장비가 바쁘면 기다리고, 멈춰 있으면
# 수리를 기다린다. 다른 장비로 넘어가면 제약의 목적이 사라진다.
# ---------------------------------------------------------------------------


def ltl_chain_of(lot: "Lot", step: "Step", ctx: DispatchContext):
    """이 step이 속한 (체인 id, 위치). 아니면 None. 순수하다."""
    if step is None:
        return None
    idx = ctx.ltl_chains.get(lot.product)
    return None if idx is None else idx.get(step.step_index)


def ltl_forced_tool(lot: "Lot", step: "Step", ctx: DispatchContext):
    """이 step에서 반드시 써야 하는 장비. 자유 선택이면 None (R1, R2).

    머리(위치 0)에서는 Phase 07 규칙이 자유롭게 고르고, 그 뒤부터 체인 끝까지
    같은 장비로 강제된다.

    지정이 없으면 **직전 방문의 기억**을 본다 (08 4.1절). rework가 꼬리를
    지난 뒤 체인 안으로 되돌리면 지정은 이미 풀렸지만 통과는 끝나지 않았다 —
    "the lot is still on the same pass"이므로 같은 장비로 돌아가야 한다.
    자유롭게 고르게 두면 그 lot의 overlay 제약이 조용히 깨진다.

    기억까지 없으면 이 통과에서 머리를 지나온 적이 없는 lot이다 (4.2절의
    초기 WIP). 그건 `ltl_on_commit`이 결함과 구분해 센다.
    """
    chain = ltl_chain_of(lot, step, ctx)
    if chain is None or chain[1] == 0:
        return None
    held = lot.ltl_assignments.get(chain[0])
    if held is not None:
        return held
    return lot.ltl_recent.get(chain[0])


def ltl_on_commit(lot: "Lot", step: "Step", tool: "Tool",
                  ctx: DispatchContext) -> str | None:
    """장비가 확정된 순간의 기록. 사유 문자열 또는 None.

    머리면 고른 장비를 체인에 기록한다 (R1). 머리가 아닌데 기록이 없으면
    세고, 지금 장비로 이어 붙여 실행은 계속한다 (R9).

    지정 없이 비-머리 step에 도착하는 경우가 **세 종류다** (4.1/4.2절).

    * `"resumed"` — 직전 방문의 기억이 있다. rework가 꼬리를 지난 뒤 체인
      안으로 되돌린 경우이고, 4.1절 기준 **같은 통과**이므로 그 장비로 돌아간다.
      결함이 아니다. 이 데이터에 그런 rework 링크가 7개 있다 (C08.11).
    * `"no-history"` — 기억도 없다. 이 통과에서 머리를 지나온 적이 **없을 수
      있는** lot이다 — 초기 WIP은 이전 통과가 시뮬레이션된 적이 없다 (4.2절).
      결함이 아니다.
    * `"missing"` — 처음부터 route를 걸어온 lot이 머리를 지나왔는데도 기록이
      없다. 결함이다.

    4.2절이 "Do not write a check that assumes every lot's chain history
    exists"라고 하므로 셋을 갈라 센다. 초기 WIP의 부트스트랩 정책은 여전히
    Phase 12의 몫이다.
    """
    chain = ltl_chain_of(lot, step, ctx)
    if chain is None:
        return None
    chain_id, pos = chain
    if pos == 0:
        # 새 통과가 시작된다. 지정과 기억을 **둘 다** 이 장비로 덮어쓴다 —
        # 지난 통과의 기억이 남아 있으면 안 된다 (08 4.1절).
        lot.ltl_assignments[chain_id] = tool
        lot.ltl_recent[chain_id] = tool
        return None
    held = lot.ltl_assignments.get(chain_id)
    if held is None:
        # 지정은 없지만 기억이 있으면 같은 통과의 재진입이다 (4.1절).
        # `ltl_forced_tool`이 이미 그 장비로 강제했으므로 여기서 다른 장비가
        # 나오면 강제가 새고 있다는 뜻이다 — 위반으로 센다.
        prev = lot.ltl_recent.get(chain_id)
        lot.ltl_assignments[chain_id] = tool          # R9 — 이어서 진행
        if prev is not None:
            return "resumed" if prev is tool else "violation"
        if lot.start_step_index > chain_id or lot.rework_count > 0:
            return "no-history"
        return "missing"
    if held is not tool:
        lot.ltl_assignments[chain_id] = tool
        return "violation"
    return None


def ltl_on_step_done(lot: "Lot", step: "Step", ctx: DispatchContext) -> None:
    """step을 마친 뒤의 정리 (R4, R5).

    체인 끝을 지났으면 그 체인의 지정을 푼다. 풀지 않으면 lot이 route 나머지
    내내 한 장비에 묶여 Litho WIP이 무한히 자란다 (5절 두 번째 실패 모드).

    그리고 **머리가 앞쪽에 있는 체인의 지정은 들고 있지 않는다** (R5).
    rework가 lot을 머리 이전으로 되돌리면 그 통과는 끝난 것이고, 다음 통과는
    처음부터 다시 정해야 한다 (4.1절). Phase 09가 rework를 넣기 전에 여기서
    이미 지켜 둔다.
    """
    if not lot.ltl_assignments and not lot.ltl_recent:
        return
    chain = ltl_chain_of(lot, step, ctx)
    if chain is not None:
        chain_id, _ = chain
        # 이 체인의 마지막 멤버인가 — 뒤따르는 목표가 없으면 끝이다.
        if step.ltl_target_index is None:
            lot.ltl_assignments.pop(chain_id, None)
            # **여기서 return 하지 않는다.** 꼬리를 마쳤다는 이유로 R5 검사를
            # 건너뛰면, rework가 체인 A의 머리 뒤에서 체인 B의 꼬리로 되돌려
            # 놓은 경우에 B의 지정이 살아남는다. 다음 통과의 자유로워야 할
            # 머리가 강제된다 (4.1절).
    # R5 — 현재 위치보다 머리가 뒤에 있는 체인은 들고 있을 이유가 없다.
    # **기억도 같이 버린다** — 머리가 앞에 있으면 그 통과는 끝났고, 다음 통과는
    # 머리에서 처음부터 정한다 (08 4.1절). 기억을 남기면 새 통과의 비-머리
    # step이 지난 통과의 장비로 강제된다.
    here = lot.current_step_index
    stale = [cid for cid in lot.ltl_assignments if cid > here]
    for cid in stale:
        del lot.ltl_assignments[cid]
    for cid in [cid for cid in lot.ltl_recent if cid > here]:
        del lot.ltl_recent[cid]


# ---------------------------------------------------------------------------
# Phase 11 — super hot lot의 장비 예약
#
# 명세: "Super Hot Lots reserve a tool at the next downstream step. The tool
# only waits until the Super Hot Lot arrives. A setup will be performed if
# necessary. The setup is carried out as soon as the Super Hot Lot is at the
# tool and not before."
#
# 두 가지가 따라온다 — 예약된 장비는 **다른 lot을 받지 않고**, 재구성은
# **도착한 뒤에** 한다. 즉 예약은 setup을 줄이는 게 아니라 **대기열만** 없앤다.
# ---------------------------------------------------------------------------


def next_visit(lot: "Lot", ctx: DispatchContext):
    """lot이 **지금 위치에서 실제로 들를** step. 없으면 None (11 R2/R3/R4).

    **`current_step_index`부터 센다.** 호출 시점이 `advance_step()` /
    `jump_to_step()` **뒤**라, 그 index가 이미 "다음에 할 step"이다. 처음에
    `+1`에서 시작했더니 예약이 한 칸씩 밀려 배치 toolgroup에는 한 번도 걸리지
    않았다 — fixture는 멈추지 않아 통과처럼 보였고, 실제로 그 규칙이
    시험되는지 세고 나서야 드러났다.

    **한 곳에서만 계산한다** (11 3.3절). 세 규칙이 전부 "다음 step이 무엇이냐"에
    걸려 있어서, 두 군데서 따로 구하면 R3와 R4가 서로 어긋난다.

    * R3 — route 밖으로 나가지 않는다. 마지막 step을 마친 lot은 `index`가
      route 길이와 같아지므로 자연히 None이 된다.
    * R4 — delay step은 장비를 쓰지 않으므로 건너뛰고 첫 비-delay step을 잡는다.

    sampling으로 건너뛸지는 여기서 보지 않는다 — 추첨은 그 step에 **도달했을
    때** 일어나고(09 R1), 미리 뽑으면 난수 순서가 바뀐다. 건너뛰어지면 그때
    예약을 푼다 (R11).
    """
    steps = lot.route.steps
    i = lot.current_step_index
    while i < len(steps):
        if not ctx.data.toolgroups[steps[i].toolgroup].is_delay:
            return steps[i]
        i += 1
    return None


def reserve_target(lot: "Lot", ctx: DispatchContext):
    """예약할 (toolgroup 이름, step). 예약하지 않으면 None.

    R1 super hot만. R5 setup 회피 toolgroup에는 걸지 않는다 — 명세가 그
    toolgroup을 다른 규칙으로 돌리고, 둘은 충돌한다. 제외 집합은 이름이
    아니라 **DISPATCHING 컬럼**에서 유도한다 (11 2절: "Names in source are
    a bug").

    `ctx.reservation`이 False면 아무도 예약하지 않는다. 그것이 레퍼런스가
    실제로 돌린 구성이다 — `.asd`의 어느 주문 파일에도 `HOTLOT`이 yes가 아니고,
    문서는 예약을 그 컬럼에 건다 (`config.SUPERHOT_RESERVATION`).
    우선순위 30은 그대로 남는다. **꺼지는 것은 예약뿐이고 순위가 아니다.**
    """
    if not ctx.reservation or not lot.is_superhot:
        return None
    step = next_visit(lot, ctx)
    if step is None:
        return None
    if step.toolgroup in ctx.setup_avoidance:
        return None
    return step.toolgroup, step


def pick_reservation_tool(tg: "ToolGroup", lot: "Lot", step: "Step",
                          ctx: DispatchContext):
    """예약할 장비 하나. 없으면 None (11 3.1절).

    **규칙: 살아 있고 비어 있고 예약되지 않은 장비 중 index가 가장 작은 것.
    그런 장비가 없으면 예약하지 않는다.**

    3.1절이 "idle-and-up first, then lowest index"면 충분하다고 하므로 그대로
    쓴다. "가장 빨리 빌 장비"까지 보지 않는 이유는, 그 판단이 남은 처리 시간을
    들여다봐야 하고 그러면 선택이 순수하지 않게 되기 때문이다 (07 R10).

    R9 — 지정(Phase 08)이 이미 장비를 정해 놨으면 **그 장비를 예약하거나
    아무것도 하지 않는다.** 둘을 따로 적용하면 예약이 지정과 다른 장비를
    가리켜 lot이 영영 못 들어간다 (11 5절 마지막 줄).
    """
    forced = ltl_forced_tool(lot, step, ctx)
    if forced is not None:
        ok = (forced.group is tg and not forced.is_down
              and forced.is_idle and forced.reserved_for is None)
        return forced if ok else None
    for tool in tg.tools:
        if (not tool.is_down and tool.is_idle
                and tool.reserved_for is None and not tool.gate_closed):
            return tool
    return None


def select_batch_group(groups: "Sequence[tuple]", tool: "Tool",
                       toolgroup: "ToolGroup", ctx: DispatchContext):
    """자격을 갖춘 배치 그룹 중 다음에 돌릴 것 (07 4절).

    Phase 04가 "도착 순서"로 두고 여기로 넘긴 결정이다. 배치는 lot 여러 개라
    우선순위와 critical ratio를 **구성원에서 유도해야** 한다:

      배치 우선순위      구성원 중 최고 — 급한 lot이 들어 있으면 배치가 급하다
      배치 critical ratio 구성원 중 최소 — 가장 급한 구성원이 배치를 대표한다

    2순위(setup)는 배치에 적용되지 않는다. batching toolgroup과 setup을 쓰는
    toolgroup은 서로소이기 때문이다(C04.4가 확인). 그래서 1순위와 3순위만 쓴다.

    `groups`는 (key, 대기열) 쌍이고, 주어진 순서가 그룹이 처음 생긴 순서다 —
    마지막 동점을 그걸로 깬다.
    """
    if not groups:
        return None
    if len(groups) == 1:
        return groups[0]

    now = ctx.env.now

    name = getattr(toolgroup, "name", "")

    def rank(indexed):
        i, (_key, waiting) = indexed
        return (-max(lot.priority for lot in waiting),
                min(ctx.tier3(name, lot, now) for lot in waiting),
                i)
    return min(enumerate(groups), key=rank)[1]


def select_batch_members(waiting: "Sequence[Lot]", max_wafers: int,
                         ctx: DispatchContext, *,
                         toolgroup: "ToolGroup | None" = None) -> list:
    """이 배치에 **누구를 태울지** (07 R14, 2026-09-19).

    **시트가 배치 toolgroup에도 순위를 적어 둔다.** Diffusion 10개 전부
    `Ranking 1 = Highest Lotpriority`다. 그런데 여기가 오래 도착 순서였다:

        for l in islice(waiting, batch_max): ...    # deque = 도착 순서

    그러면 앞뒤가 안 맞는다. `select_batch_group`은 **구성원 중 최고
    우선순위**로 그룹을 새치기시키는데, 정작 그 hot lot이 도착 순서상 뒤면
    배치에 못 탄다. hot lot 덕분에 그룹이 먼저 가고 hot lot은 남는다.

    배치 구성은 **도착 시점이 아니라 형성 시점에** 정해진다. 아직 안 만들어진
    배치에 "먼저 왔다"가 구속력을 가질 이유가 없다.

    순서는 `select_lot`과 같다 — 우선순위 -> CR -> 도착. 2순위(setup)는
    빼는데, 배치 toolgroup과 setup toolgroup이 서로소이기 때문이다(C04.4).

    **기아는 안 생긴다.** CR이 2순위에 있어서 오래 기다린 lot은 납기가
    다가오며 CR이 내려가 저절로 올라온다.

    반환: (선택된 lot 목록, 옛 규칙이라면 골랐을 목록). 둘째는 계수용이다 —
    이 변경이 실제로 몇 번 다르게 골랐는지를 세지 않으면 크기를 모른다.
    """
    now = ctx.env.now

    def take(order) -> list:
        out, total = [], 0
        for lot in order:
            if total + lot.wafers_per_lot > max_wafers:
                continue        # 안 들어가면 건너뛰고 다음을 본다
            out.append(lot)
            total += lot.wafers_per_lot
            if total == max_wafers:
                break
        return out

    # 3순위는 시트가 정한다 (G-27). `toolgroup`이 없으면 CR로 본다 — 이
    # 함수를 직접 부르는 단위 테스트를 위한 기본값이다.
    name = getattr(toolgroup, "name", "")
    indexed = list(enumerate(waiting))
    ranked = [lot for _, lot in sorted(
        indexed,
        key=lambda p: (-p[1].priority, ctx.tier3(name, p[1], now), p[0]))]
    return take(ranked), take(lot for _, lot in indexed)
