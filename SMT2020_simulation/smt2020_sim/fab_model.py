"""FabModel — lot 투입과 route 순회.

단계 순서는 02_core_flow.md R3 그대로다:
  (sampling: Phase 09) -> 이동 -> 대기 -> 장비 확보 -> (setup: Phase 06)
  -> load -> 처리 -> unload -> 진행

처리 대기는 `_run_processing`으로 감싸 둔다. Phase 03이 여기에 중단/재개를
붙일 때 step 루프를 다시 쓰지 않게 하기 위해서다 (R12).
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

import simpy

from . import config
from .dispatching import (
    DispatchContext, ltl_forced_tool, ltl_on_commit, ltl_on_step_done,
    pair_rank, pair_setup_rank3, pick_reservation_tool, reserve_target, select_batch_group,
    select_batch_members, select_lot,
    select_tool,
)
from .dispatch_audit import batch_decision, lot_decision
from .downtime import start_downtime
from .logistics import (
    calculate_processing_time, entry_interval, get_transport_time,
    mean_service_time,
)
from .lot import Lot
from .setup import build as build_setup_model
from .setup import setup_duration
from .tool import ToolGroup

if TYPE_CHECKING:  # pragma: no cover
    from .kpi_collector import KPICollector
    from .model_data import ModelData, ReleaseStream, Step

# Phase 10 — CQT 초과 시간의 P90을 내기 위한 히스토그램.
# 창의 단위가 시간이므로 10분 bin이면 P90에 충분하다. 마지막 bin이 100시간
# 이상을 모으므로, 거기 값이 크면 P90을 믿지 말고 최댓값을 봐야 한다.
CQT_BIN_MINUTES = 10.0
CQT_BINS = 600

# Phase 11 — 예약 보유 시간의 P90용 히스토그램. 예약은 lot이 한 step 떨어진
# 거리에서 거는 것이라 분 단위이고, 5분 bin으로 24시간까지 본다.
RESERVE_BIN_MINUTES = 5.0
RESERVE_BINS = 288


class FabModel:
    def __init__(self, env: simpy.Environment, data: "ModelData",
                 kpi: "KPICollector", rng: Any, *,
                 reservation: bool | None = None):
        self.env = env
        self.data = data
        self.kpi = kpi
        self.rng = rng
        # None이면 전역 기본값. 명시하면 그것을 쓴다 — 한 프로세스에서 두
        # 설정을 돌리는 A/B가 서로를 덮어쓰지 않게 하는 유일한 방법이다.
        self.reservation = (config.SUPERHOT_RESERVATION if reservation is None
                            else bool(reservation))
        # setup 모델은 2순위(least setup)와 MINRUN이 읽는다. 먼저 만든다.
        self.setup_model = build_setup_model(data)
        self.ctx = DispatchContext(env=env, data=data, rng=rng,
                                   setup_model=self.setup_model,
                                   reservation=self.reservation)

        self.toolgroups: dict[str, ToolGroup] = {
            name: ToolGroup(env, spec) for name, spec in data.toolgroups.items()
        }
        self.live_lots: dict[str, Lot] = {}
        self.warnings: dict[str, int] = defaultdict(int)
        self._lot_counters: dict[str, int] = defaultdict(int)
        self.downtime_counts: dict[str, int] = {}

        # -- Phase 04 배치 통계 ---------------------------------------------
        # 배치 건마다 기록을 남기면 1년 실행에서 수십만 건이 된다. 보고에 필요한
        # 것은 분포와 최댓값뿐이므로 집계 형태로만 유지한다.
        self.n_batches = 0
        # 순위 선택이 도착 순서와 **다르게 고른** 횟수, 그리고 그중 더 높은
        # 우선순위 lot을 실제로 태운 횟수 (07 R14). 0이면 이 변경이 한 번도
        # 물지 않았다는 뜻이고, 그것도 답이다.
        self.batch_reorders = 0
        self.batch_priority_rescues = 0
        self.batch_size_hist: dict[tuple[str, int], int] = defaultdict(int)
        # lot 종류별 배치 대기: [건수, 합, 최댓값]
        self.batch_wait_by_type: dict[str, list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0])
        # (최대 대기, toolgroup, 제품, step_index)
        self.max_batch_wait: tuple[float, str, str, int] = (0.0, "", "", -1)

        # -- Phase 06 setup 통계 ---------------------------------------------
        self.n_setups = 0             # 실제로 수행한 setup
        self.setup_skipped = 0        # 같은 setup이라 건너뛴 것 (R2)
        self.setup_initial = 0        # 처음이라 공짜로 적용한 것 (R3)
        self.setup_blocked_slots = 0  # setup 때문에 두 번째 슬롯이 막힌 횟수
        # -- 배정 프로파일 + 규칙 준수 계수 -----------------------------------
        #
        # **전부 순수 계수다.** 난수도 이벤트도 건드리지 않으므로 결과를
        # 바꾸지 않는다. 두 가지를 잰다.
        #
        # (1) 배정 비용의 실제 크기. 호출 수와 호출당 탐색 원소를 같이 봐야
        #     "호출이 줄었나"와 "호출당 비용이 줄었나"가 갈린다.
        # (2) 옛 구조(장비 index 첫 적합 + 첫 None에서 중단)와 **결정이 갈린
        #     횟수**. 0이면 그 수정이 이 데이터셋에서 한 번도 물지 않았다는
        #     뜻이고, 그것도 답이다. 잡음이 없어 KPI 차이와 달리 단정된다.
        #
        #   choice_differs  옛 코드와 **다른 장비**를 골랐다
        #   rescued         옛 코드였으면 **아무것도 배정 못 했을** pass
        #
        # 둘은 서로소다. `rescued`인 pass에서는 옛 코드가 첫 `None`에서 멈춰
        # 비교할 "옛 선택" 자체가 없다.
        self.dispatch_passes = 0
        self.dispatch_commits = 0
        self.select_lot_calls = 0
        self.queue_elements_scanned = 0
        self.free_tools_scanned = 0
        self.pairs_evaluated = 0
        self.dispatch_choice_differs = 0
        self.dispatch_rescued = 0
        # wake-up 해석이 **갈릴 수 있는** 자리. 고른 장비보다 setup이 적으면서
        # 그 lot을 받을 수 있는 다른 빈 장비가 있었는가. 그런 장비는 자기
        # 순위에서 다른 lot을 최선으로 꼽아 후보에 안 들어온 것이다.
        #
        # **위반 계수가 아니라 모호성 계수다.** 명세는 wake-up을 "lot이 도착했을
        # 때"의 규칙으로 적고 우리 구조는 (lot, 장비) 쌍으로 푼다. 0이면 두
        # 해석이 같은 답을 내므로 해석을 정할 필요가 없다.
        self.wake_up_ambiguous = 0
        self.batch_groups_evaluated = 0
        self.batch_group_retries = 0

        # -- Phase 05 진입 게이트 계측 (DECISIONS G-19) -----------------------
        # `wait_gate` 버킷이 늘 0.0인데, 그것이 **측정 결과**인지 **측정 부재**
        # 인지 구분되지 않아서 넣었다. 둘 다 순수 계수라 동역학에 영향이 없다.
        #   slots  — "게이트만 아니면 받을 수 있었다"인 장비-시도 수
        #   passes — 그런 시도가 있었고 **그 pass에서 아무도 못 나간** 횟수
        # passes가 0이면 게이트 때문에 실제로 기다린 lot이 없다는 뜻이고,
        # 그때는 `wait_gate = 0`이 측정된 사실이 된다.
        self.gate_blocked_slots = 0
        self.gate_blocked_passes = 0
        # 게이트가 toolgroup을 묶어 둔 **시간** (분). 빈도가 아니라 크기다 —
        # `passes`는 순간의 수라 시간을 말해 주지 않는다.
        #
        # **근사가 아니다.** `_tool_server`가 장비 반납(`free_event`)과 신규
        # 도착(`arrival_event`) 양쪽에 깨어나므로, "게이트가 막고 있나"의
        # 답이 바뀔 수 있는 시점이 곧 배정 pass 시점이다. 그 경계에서만
        # 누적하면 이벤트 단위로 정확하다.
        self._gate_block_since: dict[str, float] = {}
        self.gate_blocked_time = 0.0
        self.gate_blocked_by_area: dict[str, float] = defaultdict(float)
        # (A) toolgroup -> 지금 `wait_gate`로 표시해 둔 lot. 게이트가 풀리면
        # `queue`로 되돌려야 하므로 누구를 바꿨는지 기억해야 한다.
        self._gate_lot: dict[str, Lot] = {}
        # (현재, 새것) -> [횟수, 시간 합]. 방향별 시간을 확인하는 데 쓴다 (R5).
        self.setup_transitions: dict[tuple[str, str], list[float]] = defaultdict(
            lambda: [0.0, 0.0])
        # toolgroup -> [수행, 건너뜀]
        self.setup_by_group: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        # toolgroup -> {연속 실행 길이: 횟수}. 07 7절이 요구하는 히스토그램이고,
        # setup 회피 toolgroup에서 중앙값이 MINRUN(7) 이상이어야 한다.
        # 건너뛰기 비율에서 역산할 수도 있지만 분포는 평균으로 알 수 없다.
        self.run_lengths: dict[str, dict[int, int]] = defaultdict(
            lambda: defaultdict(int))

        # -- Phase 09 sampling / rework 통계 ---------------------------------
        self.n_skips = 0               # sampling으로 건너뛴 step (R2)
        self.n_reworks = 0             # 되돌아간 횟수 (R4)
        self.rework_bound_hits = 0     # 상한에 걸려 rework를 못 한 횟수 (R6)
        # (제품, step index) -> [관측 수 n, 성공 수 k].
        # 09 7절이 요구하는 것은 "각 step의 관측 비율이 **자기 n의** 이항
        # 99% 구간 안"이다. 그러려면 비율만으로는 안 되고 n을 같이 들고 있어야
        # 한다 — n이 20인 step과 20,000인 step에 같은 폭을 쓰면 둘 다 틀린다.
        self.sampling_tally: dict[tuple[str, int], list[int]] = {}
        self.rework_tally: dict[tuple[str, int], list[int]] = {}

        # -- Phase 10 CQT 통계 -----------------------------------------------
        # 10 3.1절의 항등식:
        #   opened == closed + abandoned_by_rework + target_skipped + still_open
        # `still_open`은 실행이 끝난 시점에 살아 있는 lot에서 센다.
        self.cqt_opened = 0
        self.cqt_closed = 0
        self.cqt_violated = 0          # violated <= closed
        self.cqt_abandoned = 0         # rework가 source 앞으로 되돌림 (B-5)
        self.cqt_target_skipped = 0    # sampling이 target을 건너뜀
        # (제품, source step의 area) -> [닫힘, 위반]. 10 8절이 둘 다 요구한다.
        self.cqt_by_key: dict[tuple[str, str], list[int]] = {}
        # 초과 시간의 평균과 P90. **전부 모으지 않는다** — 8년이면 window가
        # 수백만 개라 목록으로 들면 메모리가 위험하다. 평균은 합으로, P90은
        # 히스토그램으로 낸다 (bin 폭만큼의 오차는 시간 단위 창에서 무해하다).
        self.cqt_excess_sum = 0.0
        self.cqt_margin_sum = 0.0
        self.cqt_excess_hist = [0] * CQT_BINS

        # -- Phase 11 예약 통계 ------------------------------------------------
        self.reservations_made = 0
        self.reservations_released = 0
        # 예약할 장비를 못 찾은 횟수. 전부 바쁘면 예약 없이 간다 (3.1절).
        self.reservation_misses = 0
        # 예약된 장비를 **다른 lot이 가져간** 횟수 — 0이어야 한다 (R6).
        self.reservation_violations = 0
        # 예약을 지정과 다른 장비에 건 횟수 — 0이어야 한다 (R9).
        self.reservation_vs_dedication = 0
        self.reservation_hold_sum = 0.0
        self.reservation_hold_max = 0.0
        self.reservation_hold_hist = [0] * RESERVE_BINS

        # -- 예약이 **남을** 묶어 둔 양 (G-23) --------------------------------
        # 위 `reservation_hold_*`는 예약자 쪽 시간이다. 이쪽은 그 반대편 —
        # 예약된 장비 때문에 못 들어간 lot의 시간이다. `wait_reserved` 버킷이
        # 선언만 돼 있고 아무도 채우지 않아 0.0이었다 (G-19와 같은 부류:
        # "재보니 0"인지 "잴 길이가 없었다"인지 구분되지 않는 상태).
        #
        # 게이트 쪽(`gate_blocked_*`)과 **같은 모양**으로 맞춘다. 새 기구를
        # 만들면 두 값의 뜻이 달라져 나란히 읽을 수 없게 된다.
        self.reserved_blocked_slots = 0      # 시도 수 (한 lot이 여러 번 세진다)
        self.reserved_blocked_passes = 0     # 배정 pass 수
        self._reserved_block_since: dict[str, float] = {}
        self.reserved_blocked_time = 0.0
        self.reserved_blocked_by_area: dict[str, float] = defaultdict(float)
        self._reserved_lot: dict[str, Lot] = {}

        # -- toolgroup별 대기 (그림 [7]) --------------------------------------
        # toolgroup -> [건수, 합(분), 최댓값(분)]. 방문마다 기록을 남기면 8년
        # 실행에서 7천만 건이 되므로 **집계만** 유지한다. 평균과 최댓값이면
        # "어디가 막히나"는 답할 수 있고, 분포가 필요하면 그때 히스토그램을
        # 붙이면 된다.
        #
        # `areas`의 wip 평균과 다른 것을 잰다 — 저쪽은 "몇 개가 서 있었나",
        # 이쪽은 "하나가 얼마나 기다렸나"다. 둘 다 있어야 병목을 읽는다.
        self.wait_by_toolgroup: dict[str, list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0])

        # -- Phase 08 LTL 통계 -----------------------------------------------
        self.ltl_violations = 0   # 한 통과 안에서 장비가 바뀐 경우 — 0이어야 한다
        self.ltl_missing = 0      # 지정이 있어야 하는데 없던 경우 — 0이어야 한다
        # 08 4.2절 — **이 통과에서 머리를 지나온 적이 없는** lot의 누락은 결함이
        # 아니다. 초기 WIP은 이전 통과가 시뮬레이션된 적이 없고(4.2절), rework는
        # 머리를 건너뛰고 체인 안으로 되돌아올 수 있다(4.1절, C08.11이 이
        # 데이터에 7건 있음을 확인). 정책은 Phase 09/12의 몫이고, Phase 08은
        # **그 구분을 셀 수 있게** 해 둔다 — 4.2절이 그렇게 지시한다.
        self.ltl_missing_no_history = 0
        # 08 4.1절 — 꼬리를 지난 뒤 rework로 체인 안에 되돌아와 **직전 장비를
        # 되찾은** 횟수. 같은 통과이므로 결함이 아니다. 0이면 기억이 동작하지
        # 않는 것이고, 그러면 그 lot들이 자유 선택으로 새어 나간다.
        self.ltl_resumed = 0
        self.ltl_held_at_exit = 0  # fab을 나가며 지정을 들고 있던 lot (R6)
        self.ltl_max_wait = 0.0   # 지정 장비를 기다린 최대 시간
        self.ltl_max_wait_where = None   # (product, step index, toolgroup)
        self.ltl_waits = 0

        # 이후 phase가 제약하는 세 구조를 따로 본다 (02_core_flow.md 4.3절).
        # 전부 데이터에서 유도한다.
        self.watchlist: dict[str, tuple[str, ...]] = {
            "litho_queue": tuple(sorted(
                n for n, s in data.toolgroups.items() if s.area == "Litho")),
            "batch_queue": data.batching_toolgroups(),
            "setup_avoidance_queue": data.setup_avoidance_toolgroups(),
        }

    # -- 시작 ---------------------------------------------------------------

    def start(self) -> None:
        # batching과 cascading이 겹치면 두 점유 모델이 충돌한다. dataset 2에는
        # 겹치는 toolgroup이 없다 (04_batch.md 4절). 방어하지 않고 단언한다 —
        # 겹친다면 데이터 해석이 틀린 것이지 처리해야 할 경우가 아니다.
        both = (set(self.data.batching_toolgroups())
                & set(self.data.cascading_toolgroups()))
        if both:
            raise ValueError(
                f"batching이면서 cascading인 toolgroup: {sorted(both)}. "
                "Phase 05의 점유 모델과 충돌한다")

        # batch step은 배치 서버가 있는 toolgroup에만 있어야 한다. 아니면 그
        # lot은 아무도 깨워 주지 않아 **영원히 잠든다** — 50초짜리 실행이 조용히
        # 멈춘 것처럼 보인다. 여기서 먼저 터뜨린다 (C04.1과 같은 불변식).
        batching = self.data.batching_toolgroups()      # 정렬된 tuple
        orphan = sorted({
            step.toolgroup
            for route in self.data.routes.values() for step in route.steps
            if step.batch_min is not None and step.toolgroup not in batching})
        if orphan:
            raise ValueError(
                f"배치 서버가 없는 toolgroup에 batch step이 있다: {orphan}. "
                "BACTHINGTOOL 컬럼 해석을 확인할 것")

        self.downtime_counts = start_downtime(self.env, self, self.rng)

        # 배치 toolgroup마다 서버 하나. 배치 형성의 **유일한** 결정 주체다.
        # 이 루프는 Phase 04 그대로 둔다 — 프로세스 생성 순서가 같은 시각의
        # 이벤트 순서를 정하므로, 순서를 바꾸면 Phase 04 결과가 흔들린다.
        for name in batching:
            self.env.process(self._batch_server(self.toolgroups[name]))

        # 나머지 toolgroup마다 장비 배정 서버 하나. 선택의 유일한 결정 주체다.
        #
        # 두 종류가 toolgroup을 분할한다. C04.1이 "batch step <=> batching
        # toolgroup"을 위반 0으로 확인하므로, batching toolgroup에는
        # `_acquire_tool`을 쓰는 lot이 존재할 수 없다. 한 toolgroup에 두 서버가
        # 붙는 일은 없다. delay toolgroup은 장비 자체가 없다.
        #
        # 순서는 data.toolgroups의 삽입 순서(시트 순서)다. set으로 돌리면
        # 실행마다 순서가 달라져 결정론이 깨진다.
        for name, spec in self.data.toolgroups.items():
            if spec.is_delay or spec.batching:
                continue
            self.env.process(self._tool_server(self.toolgroups[name]))

        # Phase 12 R1 — 투입 모드. **파싱이 정한다.** `explicit_lots`가 비면
        # 아래 `else`가 Phase 11과 한 글자도 다르지 않으므로 R2("파라메트릭
        # 동작은 바뀌지 않는다")가 구조적으로 성립한다.
        if self.data.explicit_lots:
            self.env.process(self._release_explicit())
        else:
            for stream in self.data.release_streams:
                self.env.process(self._release_stream(stream))

        # R5 — 초기 WIP은 프로세스가 아니라 t=0의 상태다. 투입 프로세스보다
        # **뒤에** 놓는다: 같은 시각에 생기는 이벤트의 순서를 고정해 둬야
        # 결정성이 유지된다.
        if self.data.initial_wip:
            self._seed_initial_wip()

        self.kpi.start_monitors(self)

    # -- 투입 ---------------------------------------------------------------

    def _release_stream(self, stream: "ReleaseStream"):
        """한 투입 스트림. LOTS PER RELEASE를 그대로 지킨다 (R8).

        **납기는 lot마다 뽑는다** (논문 식 1: `d_j = r_j + z_j·FF_i·RPT_i`,
        `z_j ~ U(0.8, 1.2)`). `stream.due_date_offset`이 `FF_i·RPT_i`이므로
        거기에 z를 곱하면 식이 그대로 된다 — 시트의 offset이 그 곱이라는 것은
        논문 Table II와 대조해 확인했다 (`config.DUE_DATE_Z` 주석).

        **고정값으로 두면 안 되는 이유는 CR이다.** 납기는 critical ratio의
        분자이고 CR은 디스패치 3순위인데, regular lot은 우선순위가 전부 10으로
        동점이고 106개 toolgroup 중 97개에 setup이 없어 1·2순위가 갈리지
        않는다 — 사실상 CR 혼자 결정한다. z를 얼리면 같은 자리의 두 lot이
        투입 순서대로 고정되어 **추월이 일어나지 않는다.** 즉 출력이 아니라
        결정 규칙이 달라진다.

        투입 **시각**은 논문과 같다 — 논문도 "a new regular lot every 258.46
        minutes"로 고정 간격이다. 난수는 납기에만 들어간다.
        """
        route = self.data.routes[stream.product]
        lo, hi = config.DUE_DATE_Z
        while True:
            for _ in range(stream.lots_per_release):
                self._lot_counters[stream.lot_type] += 1
                n = self._lot_counters[stream.lot_type]
                due_z = self.rng.uniform(lo, hi)
                lot = Lot(
                    lot_id=f"{stream.lot_type}_{n}",
                    product=stream.product,
                    route=route,
                    priority=stream.priority,
                    wafers_per_lot=stream.wafers_per_lot,
                    release_time=self.env.now,
                    # 논문 식 1 — z 는 lot마다 새로 뽑는다
                    due_date=(self.env.now
                              + due_z * stream.due_date_offset),
                    is_superhot=stream.is_superhot,
                    due_base_min=stream.due_date_offset, due_z=due_z,
                )
                self.live_lots[lot.lot_id] = lot
                self.kpi.note_wip(len(self.live_lots))
                self.kpi.record_release(lot)
                self.env.process(self._process_lot(lot))
            yield self.env.timeout(stream.release_interval)

    def _release_explicit(self):
        """lot별 일정에 따라 투입한다 (Phase 12 R1/R3/R7).

        **`Lot`을 미리 만들지 않는다.** 일정은 투입 시각 순으로 정렬돼 있으므로
        index 하나로 훑으면서, 지금 시각에 해당하는 것만 만든다. 16만 개를
        t=0에 만들면 그 자체로 수백 MB이고 SimPy 프로세스도 16만 개가 된다 (R3).
        같은 시각의 lot은 한 번에 내보내고 다음 시각까지 한 번만 잔다.

        due date는 **각 lot 자신의 것**이다. 스트림 offset을 쓰지 않는다 (R7).
        """
        lots = self.data.explicit_lots
        i, n = 0, len(lots)
        while i < n:
            when = lots[i].release_time
            if when > self.env.now:
                yield self.env.timeout(when - self.env.now)
            while i < n and lots[i].release_time <= self.env.now:
                spec = lots[i]
                i += 1
                lot = Lot(
                    lot_id=spec.lot_id,
                    product=spec.product,
                    route=self.data.routes[spec.product],
                    priority=spec.priority,
                    wafers_per_lot=spec.wafers_per_lot,
                    release_time=self.env.now,
                    due_date=spec.due_date,              # R7 — 절대 시각이다
                    is_superhot=spec.is_superhot,
                )
                self.live_lots[lot.lot_id] = lot
                self.kpi.note_wip(len(self.live_lots))
                self.kpi.record_release(lot)
                self.env.process(self._process_lot(lot))

    def _seed_initial_wip(self) -> None:
        """t=0에 이미 fab 안에 있는 lot을 놓는다 (Phase 12 R5/R8/R9).

        **bootstrap이지 복원이 아니다.** `WIP.txt`는 위치(CURSTEP)만 주고
        이력은 주지 않는다. 문서에도 데이터에도 없는 것 셋을 여기서 가정으로
        고정하며, 가정임을 남기려고 개체수를 따로 센다 (3.1절):

          * LTL 지정 — 없음으로 출발한다. 2,156개 중 1,311개가 체인 안쪽에
            있지만, 어느 장비를 썼는지는 아무 데도 없다. 처음 만나는 체인
            머리에서 새로 수립한다. **원래 장비를 복원했다고 말하지 않는다.**
          * CQT window — 열린 것 없음으로 출발한다.
          * PM 카운터 — 장비의 것이지 lot의 것이 아니므로 0에서 시작한다
            (4절: 첫 PM 주기는 대표성이 없고 warm-up이 흡수한다).

        `start_step_index`를 lot이 갖고 있으므로 경로 보존(R8)은 route 처음이
        아니라 **출발 step부터** 재어진다 — `Lot`이 Phase 02부터 준비해 둔 자리다.
        """
        for spec in self.data.initial_wip:
            route = self.data.routes[spec.product]
            lot = Lot(
                lot_id=spec.lot_id,
                product=spec.product,
                route=route,
                priority=spec.priority,
                wafers_per_lot=spec.wafers_per_lot,
                release_time=self.env.now,          # t=0
                due_date=spec.due_date,             # 0보다 앞일 수 있다
                is_superhot=spec.is_superhot,
                start_step_index=route.step_id_to_index[spec.current_step_id],
                is_initial_wip=True,                # R6/R9 — 개체 구분
            )
            self.live_lots[lot.lot_id] = lot
            self.kpi.note_wip(len(self.live_lots))
            self.kpi.record_initial_wip(lot)     # record_release가 아니다 — R6
            self.env.process(self._process_lot(lot))

    # -- route 순회 ---------------------------------------------------------

    def _process_lot(self, lot: Lot):
        while not lot.is_complete():
            step = lot.current_step()
            tg = self.toolgroups[step.toolgroup]

            self.kpi.begin_visit(lot, step, tg)

            # 0) 표본 추출 (09 R1) — **가장 먼저다.** 이동보다도, 대기열보다도
            #    앞이다. 이 step에 아무것도 쓰기 전에 정해야 건너뛴 step이
            #    공짜가 된다 (R2). 이동 뒤로 미루면 안 간 곳까지 갔다 온 값이
            #    붙는다 (09 5절 "Skipped steps still cost travel time").
            if step.sampling_prob is not None and not self._draw_sampled(lot, step):
                lot.steps_skipped += 1
                self.n_skips += 1
                # 10 3.1절 — 이 step을 target으로 삼은 window는 영영 못 닫는다.
                # **지금** 닫는다. 끝에서 고아를 훑으면 `still_open_at_end`와
                # 구분이 사라지는데, 둘은 원인이 전혀 다르다.
                self._cqt_skip(lot, lot.current_step_index)
                # 11 R11 — 예약해 둔 step을 건너뛰면 그 장비는 영영 기다린다.
                # 지금 푼다 (5절 "A tool idles forever").
                self._release_reservations(lot, taken=step.toolgroup)
                self.kpi.end_visit(lot, "skipped")
                lot.advance_step()
                continue

            # 1) delay step — 장비를 쓰지 않는다. 양쪽으로 이동도 없다 (R2).
            if tg.spec.is_delay:
                lot.enter("delay", self.env.now)
                yield self.env.timeout(
                    calculate_processing_time(step, lot.wafers_per_lot, self.rng))
                lot.steps_executed += 1
                lot.last_location = tg.spec.location
                self.kpi.end_visit(lot, "completed")
                lot.advance_step()
                continue

            # 2) 이동 — **마지막으로 실제로 들렀던 위치**에서 이번 위치로 (R4).
            #    index-1이 아니다: 그 step이 건너뛰어졌다면 lot은 거기 간 적이
            #    없다 (09 R2). 건너뛰기가 없으면 index-1과 같은 값이므로
            #    Phase 02~08 동작과 동일하다.
            if lot.last_location is not None:
                tt = get_transport_time(lot.last_location, tg.spec.location,
                                        self.data.transport, self.rng)
                if tt > 0.0:
                    lot.enter("transport", self.env.now)
                    yield self.env.timeout(tt)

            # 3~7) 대기와 처리
            if step.batch_min is not None:
                # 배치 step. 장비 확보와 load/처리/unload를 배치 서버가 배치
                # 단위로 수행한다. 이 lot은 자기 배치가 끝날 때까지 기다린다.
                yield from self._batch_step(lot, step, tg)
            else:
                # 돌아온 시점에 lot은 이미 장비를 점유하고 있다. 자원 요청을
                # 기다리는 일은 _acquire_tool 안에서 끝난다.
                tool, req = yield from self._acquire_tool(lot, step, tg)
                self.kpi.assign_station(lot, tool)
                try:
                    yield from self._process_on_tool(lot, step, tool, tg)
                finally:
                    self.kpi.release_work_context(tool)
                    tool.resource.release(req)
                    tool.note_jobs()
                    # 뒤 job을 풀어 준다. 중간에 터져도 반드시 해야 한다 —
                    # 빠뜨리면 그 장비의 뒤 job이 영원히 잠든다 (05 R3).
                    self._release_successor(lot)
                    tg.mark_released(tool)
                    tool.stop_busy()
                    tg.signal_free()

            # R9 — 배치였더라도 step 진행은 각자 자기 프로세스에서 한다.
            lot.steps_executed += 1
            lot.last_location = tg.spec.location

            # 09 R3 — rework 판정은 step이 **끝난 뒤, 진행하기 전**이다.
            target = self._draw_rework(lot, step)
            self.kpi.end_visit(lot, "completed", rework_target=target)
            if target is None:
                lot.advance_step()
            else:
                # R4 — 목적지부터 다시 걷는다. 이번에 다시 걸을 index 수를
                # 세어 둔다: step 계산 항등식의 우변이다 (7절 Step accounting).
                lot.rework_span += lot.current_step_index - target + 1
                lot.rework_count += 1
                self.n_reworks += 1
                lot.jump_to_step(target)
                # 11 R10 / 09 R8 — 되돌아간 lot은 예약해 둔 자리에 안 온다.
                #
                # **오늘은 이 줄이 없어도 동작한다** — 아래 `_reserve`가 먼저
                # 전부 풀기 때문이다. 그래도 남겨 둔다: 규칙이 요구하는 자리에
                # 규칙을 적어 두는 편이, `_reserve`의 구조가 바뀌었을 때
                # 조용히 깨지는 것보다 낫다.
                self._release_reservations(lot)
                # 10 3.2절 / B-5 — source가 앞이 된 window를 정리한다.
                # LTL 지정 정리(아래 `ltl_on_step_done`)와 같은 논리다:
                # 되돌아갔으면 그 통과는 끝났다.
                self._cqt_rework(lot)

            # 11 R2 — 다음에 들를 step의 장비를 잡는다. **위치가 정해진 뒤**여야
            # 한다 — rework로 되돌아갔으면 예약할 곳이 달라지기 때문이다.
            if lot.is_superhot:
                self._reserve(lot)

            # 08 R4/R5 + 09 R7 — **위치가 정해진 뒤에** 지정을 정리한다.
            # rework로 체인 머리보다 앞으로 갔으면 그 통과는 끝난 것이므로
            # 지정을 버려야 한다. 순서를 뒤집으면 지난 통과의 장비가 다음
            # 통과로 새어 나가 자유로워야 할 머리가 강제된다.
            ltl_on_step_done(lot, step, self.ctx)

        # fab-out
        # 08 R6 — 나가는 lot은 어떤 지정도 들고 있지 않아야 한다. 들고 있으면
        # 체인 끝에서 푸는 경로가 빠진 것이다.
        if lot.ltl_assignments:
            self.ltl_held_at_exit += 1
            self.warnings["ltl_held_at_exit"] += 1
            lot.ltl_assignments.clear()
        # 기억은 지정이 아니다 (R6의 대상이 아니다). 다만 lot이 사라지므로
        # 같이 비운다 — 남겨 두면 참조가 장비를 붙들고 있게 된다.
        lot.ltl_recent.clear()
        # 11 — 나가는 lot이 장비를 붙들고 있으면 안 된다. R3에 따라 마지막
        # step을 마친 lot은 예약하지 않으므로 여기 남아 있을 것이 없지만,
        # 남아 있다면 그 장비가 영원히 놀게 되므로 반드시 푼다.
        self._release_reservations(lot)

        lot.close_buckets(self.env.now)
        lot.fab_out_time = self.env.now
        self.live_lots.pop(lot.lot_id, None)
        self.kpi.note_wip(len(self.live_lots))
        self.kpi.record_completion(lot)

    # -- Phase 11 super hot lot 예약 ------------------------------------------
    #
    # super hot 스트림이 데이터셋에 **하나뿐**이고 연 19 lot이다 (11 1.1절).
    # 그래서 fab 수준 KPI는 거의 움직이지 않아야 한다 — 눈에 띄게 움직이면
    # 예약이 일반 lot으로 새고 있다는 뜻이다 (5절).

    def _reserve(self, lot: Lot) -> None:
        """step을 마친 뒤 다음 step의 장비를 잡는다 (R2).

        **먼저 옛 예약을 푼다.** 안 그러면 lot이 앞으로 갈수록 장비를 쌓아
        들고 다니고, 그만큼 fab이 조용히 좁아진다 (5절 "A tool idles forever").
        """
        self._release_reservations(lot)
        target = reserve_target(lot, self.ctx)
        if target is None:
            return
        tg_name, step = target
        tg = self.toolgroups[tg_name]
        tool = pick_reservation_tool(tg, lot, step, self.ctx)
        if tool is None:
            self.reservation_misses += 1
            self.kpi.record_event(lot, "reservation_miss", toolgroup=tg_name)
            return
        tool.reserved_for = lot.lot_id
        lot.reservations[tg_name] = (tool, self.env.now)
        self.reservations_made += 1
        self.kpi.record_event(lot, "reservation_start", toolgroup=tg_name, station=tool.tool_id)

    def _release_reservations(self, lot: Lot, *, taken: str | None = None) -> None:
        """이 lot의 예약을 푼다. R8/R10/R11이 전부 여기로 온다.

        `taken`이 주어지면 그 toolgroup만 푼다 — lot이 장비를 **잡는 순간**
        이고, setup보다 앞이다 (R8). 명세가 "setup은 도착한 뒤에" 라고 하므로
        예약을 setup까지 들고 있을 이유가 없다.

        보유 시간을 여기서 집계한다 (11 7절 "Hold time").
        """
        if not lot.reservations:
            return
        names = [taken] if taken is not None else list(lot.reservations)
        now = self.env.now
        for name in names:
            got = lot.reservations.pop(name, None)
            if got is None:
                continue
            tool, since = got
            if tool.reserved_for == lot.lot_id:
                tool.reserved_for = None
                tg = self.toolgroups[name]
                tg.signal_free()        # 풀린 장비를 기다리던 lot을 깨운다
            held = now - since
            self.kpi.record_event(lot, "reservation_end", toolgroup=name,
                                  station=tool.tool_id, start_min=since, held_min=held)
            self.reservation_hold_sum += held
            if held > self.reservation_hold_max:
                self.reservation_hold_max = held
            b = min(int(held // RESERVE_BIN_MINUTES), RESERVE_BINS - 1)
            self.reservation_hold_hist[b] += 1
            self.reservations_released += 1

    # -- Phase 10 CQT window ------------------------------------------------
    #
    # **이 phase는 흐름을 바꾸지 않는다** (10 7절 첫 체크). 아래 넷 다 순수
    # 기록이다 — 난수를 뽑지 않고, yield가 없고, 스케줄 결정을 더하지 않는다.
    # 하나라도 어기면 같은 seed에서 CT가 움직이고, 그러면 측정이 측정을
    # 바꾼 것이라 이 phase 전체가 무의미해진다.

    def _cqt_close(self, lots, now: float) -> None:
        """R2 — target step의 처리가 **시작될 때** 닫는다.

        lot마다 **자기 위치**로 판정한다. 배치 멤버가 서로 다른 제품일 수
        있으므로 배치의 대표 step으로 판정하면 안 된다.
        """
        for lot in lots:
            open_ = lot.active_cqts
            if not open_:
                continue
            here = lot.current_step_index
            keep = []
            for w in open_:
                if w[1] != here:
                    keep.append(w)
                    continue
                elapsed = now - w[3]
                self.kpi.record_event(lot, "cqt_close", source_index=w[0], target_index=w[1],
                                      start_min=w[3], limit_min=w[2], elapsed_min=elapsed,
                                      violated=elapsed > w[2])
                self.cqt_closed += 1
                over = elapsed - w[2]
                area = self.data.routes[lot.product].steps[w[0]].area
                stat = self._cqt_stat(lot.product, area)
                stat[0] += 1
                if over > 0.0:
                    self.cqt_violated += 1
                    stat[1] += 1
                    self.cqt_excess_sum += over
                    b = min(int(over // CQT_BIN_MINUTES), CQT_BINS - 1)
                    self.cqt_excess_hist[b] += 1
                else:
                    self.cqt_margin_sum += -over
            lot.active_cqts = keep

    def _cqt_open(self, lots) -> None:
        """R1 — source step의 처리가 **끝날 때** 연다. unload 앞이다."""
        now = self.env.now
        for lot in lots:
            step = lot.current_step()
            if step is None or step.cqt_target_index is None:
                continue
            lot.active_cqts.append(
                (lot.current_step_index, step.cqt_target_index,
                 step.cqt_threshold, now))
            self.cqt_opened += 1
            self.kpi.record_event(lot, "cqt_open", source_index=lot.current_step_index,
                                  target_index=step.cqt_target_index, limit_min=step.cqt_threshold)

    def _cqt_skip(self, lot: Lot, skipped_index: int) -> None:
        """sampling이 target을 건너뛰면 그 자리에서 닫는다 (3.1절).

        끝에서 고아를 훑어 처리하지 않는다 — 그러면 `still_open_at_end`와
        구분이 안 되고, 둘은 원인이 전혀 다르다.
        """
        if not lot.active_cqts:
            return
        keep = [w for w in lot.active_cqts if w[1] != skipped_index]
        for w in lot.active_cqts:
            if w[1] == skipped_index:
                self.kpi.record_event(lot, "cqt_drop_sampling", source_index=w[0],
                                      target_index=w[1], start_min=w[3], limit_min=w[2])
        self.cqt_target_skipped += len(lot.active_cqts) - len(keep)
        lot.active_cqts = keep

    def _cqt_rework(self, lot: Lot) -> None:
        """source가 현재 위치보다 **앞**이 된 window를 폐기한다 (3.2절, B-5).

        `CQT_DROP_ON_REWORK=False`면 들고 간다 — lot이 앞으로 가며 target을
        다시 지나므로 닫힌다. **어느 쪽이든 violation으로 세지 않는다.**
        """
        if not config.CQT_DROP_ON_REWORK or not lot.active_cqts:
            return
        here = lot.current_step_index
        keep = [w for w in lot.active_cqts if w[0] <= here]
        for w in lot.active_cqts:
            if w[0] > here:
                self.kpi.record_event(lot, "cqt_drop_rework", source_index=w[0],
                                      target_index=w[1], start_min=w[3], limit_min=w[2])
        self.cqt_abandoned += len(lot.active_cqts) - len(keep)
        lot.active_cqts = keep

    def _cqt_stat(self, product: str, area: str) -> list:
        key = (product, area)
        s = self.cqt_by_key.get(key)
        if s is None:
            s = self.cqt_by_key[key] = [0, 0]      # [닫힘, 위반]
        return s

    # -- Phase 09 추첨 -------------------------------------------------------
    #
    # 두 함수 모두 **호출되면 반드시 난수를 하나 뽑는다.** 결과가 어떻든,
    # 상한에 걸렸든 상관없다 (R9). 조건에 따라 뽑고 안 뽑고 하면 같은 seed에서
    # 스트림이 갈라져 재현이 깨진다.
    #
    # 확률은 **퍼센트(0~100)**다. 분수로 쓰면 rework가 100배로 난다
    # (09 5절의 두 번째 실패 모드).

    @staticmethod
    def _tally(table: dict, lot: Lot, step: "Step", hit: bool) -> None:
        key = (lot.product, step.step_index)
        t = table.get(key)
        if t is None:
            t = table[key] = [0, 0]
        t[0] += 1
        t[1] += hit

    def _draw_sampled(self, lot: Lot, step: "Step") -> bool:
        """이 lot이 이 step에서 **처리되는가** (09 R1).

        route가 주는 것은 건너뛸 확률이 아니라 **처리될 확률**이다. 부등호를
        뒤집으면 거의 모든 step이 처리된다 (5절 첫 번째 실패 모드).
        """
        processed = self.rng.random() * 100.0 < step.sampling_prob
        self._tally(self.sampling_tally, lot, step, processed)
        return processed

    def _draw_rework(self, lot: Lot, step: "Step") -> int | None:
        """되돌아갈 index. 안 되돌아가면 None (09 R3~R6).

        목적지는 파싱 때 이미 index로 풀려 있다 (R5). 실행 중에 식별자로
        찾으면 엉뚱한 행에 맞아 lot이 영원히 끝나지 않는다 (5절).
        """
        if step.rework_prob is None or step.rework_target_index is None:
            return None
        hit = self.rng.random() * 100.0 < step.rework_prob
        self._tally(self.rework_tally, lot, step, hit)
        if not hit:
            return None
        # R6 — 상한. 이 데이터셋에서는 절대 닿지 않아야 한다 (3.1절).
        # 추첨은 이미 끝났으므로 상한이 난수 스트림을 바꾸지 않는다.
        if lot.rework_count >= config.MAX_REWORKS_PER_LOT:
            self.rework_bound_hits += 1
            self.warnings["rework_bound_hit"] += 1
            return None
        return step.rework_target_index

    def _acquire_tool(self, lot: Lot, step: "Step", tg: ToolGroup):
        """대기열에 들어가 장비를 확보한다. 반환 시점에 lot은 장비를 점유한다.

        예전에는 장비가 반납될 때마다 **대기 중인 lot 전원을 깨우고** 각자
        판정하게 했다. 규칙이 queue[0] 하나일 때는 값이 싸다. 하지만 Phase 07이
        3단 순위를 붙이면 대기자 N명이 각자 대기열 N개를 훑어 반납 1회가
        O(N^2)가 된다. 07_dispatching.md의 Modules에 fab_model.py가 없다 —
        호출 구조는 Phase 02에서 끝내 두라는 뜻이다 (02_core_flow.md R11).

        그래서 배정 주체를 toolgroup당 하나로 둔다(`_tool_server`). **규칙은
        바꾸지 않는다**: select_lot/select_tool을 그대로 쓰고 결과도 같다 —
        대기열 선두가 막히면 뒤도 막히는 동작까지 그대로다. 그 문제는
        lot별 제약이 생기는 Phase 08/11의 몫이고, 두 구조 모두 같은 결정을
        요구한다.

        **이벤트 홉 수를 기존과 맞춘다.** 홉이 하나 늘면 같은 시각에 도는 다른
        프로세스와의 순서가 바뀌고, 그러면 rng 소비 순서가 달라져 seed가 같아도
        결과가 흔들린다. 그래서 두 가지를 지킨다:
          * 빠른 경로(바로 잡히는 경우)는 기존 코드의 첫 반복 그대로 두고
            여기서 `yield req` 까지 한다.
          * 서버 경로에서는 서버가 **이미 확보해 둔** 요청을 다시 yield 하지
            않는다. 서버는 자리를 확인한 뒤에만 request 하므로 항상 즉시
            승인되어 있다.
        """
        # 08 R2 — 이번 step에서 강제된 장비를 한 번 계산해 둔다. `can_accept`가
        # 이걸 O(1)로 읽는다. 대기 중 장비가 멈췄다 살아나도 값은 그대로다.
        lot.ltl_forced = ltl_forced_tool(lot, step, self.ctx)
        if lot.ltl_forced is not None:
            self.kpi.record_event(lot, "dedication_target", toolgroup=tg.name,
                                  station=lot.ltl_forced.tool_id,
                                  step_index=lot.current_step_index)

        # 11 — **도착했으므로 예약을 푼다.** AutoSched 문서:
        #   "The tool only waits until the Super Hot Lot arrives."
        #   "The duration of the reservation can be adjusted ... using the
        #    HOTLOTDELAY% column. Here, the column is not explicitly used
        #    since the default is 0."
        # 지속 시간이 0이므로 도착 이후로 끌 근거가 없다. 예약의 효과는 도착
        # 시점에 장비가 **비어 있게** 만드는 것이고, 거기서부터는 우선순위
        # 30이 가져간다 (문서: "Super Hot Lots have the highest priority").
        self._release_reservations(lot, taken=tg.name)

        tg.queue.append(lot)
        # toolgroup별 대기 계측 (그림 [7]). **버킷 라벨과 무관하게** 큐에
        # 들어간 순간부터 장비를 잡는 순간까지를 잰다 — 그 사이가 `queue`든
        # `wait_gate`든 `wait_dedicated`든 lot 입장에서는 같은 기다림이다.
        # `_commit`이 닫는다. 순수 계수라 rng도 이벤트 홉도 안 건드린다.
        lot.queue_since = self.env.now
        # 지정이 걸린 lot의 대기는 `queue`가 아니라 `wait_dedicated`다 —
        # lot.py의 BUCKET_PRIORITY가 그 버킷을 "특정 장비를 기다리는 중"으로
        # 정의해 두었고, 지금이 정확히 그 상태다. 라벨만 바뀌므로 rng도
        # 이벤트 홉도 건드리지 않는다. 구분하지 않으면 LTL 비용이 분해표에서
        # 통째로 사라진다 (08 8절이 요구하는 보고 항목).
        lot.enter("wait_dedicated" if lot.ltl_forced is not None else "queue",
                  self.env.now)

        # -- 빠른 경로 ------------------------------------------------------
        # 기존 코드의 첫 반복과 같은 판정이다. 선택과 request 사이에 yield가
        # 없어야 같은 시각의 다른 프로세스가 같은 장비를 가져가지 못한다.
        tool = select_tool(tg, lot, step, self.ctx)
        # 선택한 장비만 확인하면, setup/LTL 때문에 여기서는 못 받지만 다른
        # 빈 장비에서는 받을 수 있는 상위 우선순위 lot을 놓친다. 그 경우에는
        # 전체 (lot, 장비) 쌍을 확인하는 배정 서버가 먼저 판단하게 한다.
        if (tool is not None
                and not any(waiting.priority > lot.priority for waiting in tg.queue)
                and select_lot(tg.queue, tool, tg, self.ctx) is lot):
            if getattr(self.kpi, "audit_dispatch", False):    # full 전용 계측
                self.kpi.record_dispatch(*lot_decision("arrival", tg, tool, lot, self.ctx, self.env.now))
            req = self._commit(tg, tool, lot)
            try:
                yield req
            except BaseException:
                # 아직 _process_lot의 finally 밖이다. 여기서 놓치면 장비가 샌다.
                self._undo_commit(tg, tool, req)
                raise
            return tool, req

        # -- 서버 경로 ------------------------------------------------------
        done = lot.dispatch_done = self.env.event()
        tg.signal_arrival()
        try:
            tool, req = yield done
        except BaseException:
            lot.dispatch_done = None
            self._drop_from_queue(tg, lot)
            if done.triggered:
                # 넘겨받은 직후에 죽었다. 지금은 대기 중인 lot을 interrupt하는
                # 경로가 없어 도달하지 않지만, 생기면 장비가 조용히 샌다.
                t, r = done.value
                self._undo_commit(tg, t, r)
            raise
        return tool, req

    # -- 배정 ---------------------------------------------------------------

    @staticmethod
    def _drop_from_queue(tg: ToolGroup, lot: Lot) -> None:
        """대기열에서 뺀다. 선두면 O(1)."""
        if tg.queue and tg.queue[0] is lot:
            tg.queue.popleft()
        elif lot in tg.queue:
            tg.queue.remove(lot)

    def _commit(self, tg: ToolGroup, tool, lot: Lot):
        """장비를 이 lot에 확정한다. 호출 직전까지 yield가 없어야 한다.

        `can_accept`로 자리를 확인한 뒤에만 부르므로 요청은 즉시 승인된다.

        cascading의 입장 처리도 여기서 한다 (05 R10). 판정과 게이트 닫기 사이에
        yield가 없으므로, 같은 시각에 두 job이 함께 들어가는 일이 구조적으로
        불가능하다. 배정 주체가 하나라는 사실이 그것을 보장한다.
        """
        req = tool.resource.request(priority=-lot.priority)   # R10: 부호 반전
        tg.mark_taken(tool)
        self._drop_from_queue(tg, lot)
        tool.note_jobs()

        step = lot.current_step()

        # 08 R1/R9 — 장비가 확정된 지금 체인에 기록한다. 선택 함수는 부수효과가
        # 없어야 하므로(07 R10) 기록은 여기서만 한다.
        if lot.ltl_forced is not None:
            # 지정 장비를 기다린 시간. 이 시점의 버킷은 "wait_dedicated"이고
            # 대기열에 들어간 뒤로 바뀌지 않았으므로 그 구간 길이가 곧 대기다.
            waited = self.env.now - lot._bucket_since
            self.ltl_waits += 1
            if waited > self.ltl_max_wait:
                self.ltl_max_wait = waited
                # 어디서 났는지 남긴다 (08 8절). 숫자만으로는 Phase 09에서
                # 손댈 곳을 찾을 수 없다 — 배치 보고서와 같은 형식이다.
                self.ltl_max_wait_where = (lot.product, lot.current_step_index,
                                           tg.name, lot.lot_type)

        # 11 R6/R8 — lot이 장비를 잡는 순간. 예약은 **setup보다 앞에서** 풀린다.
        # 그리고 여기서 위반을 센다: 예약된 장비를 예약자가 아닌 lot이 가져가면
        # `can_accept`가 새고 있는 것이다.
        if tool.reserved_for is not None and tool.reserved_for != lot.lot_id:
            self.reservation_violations += 1
            self.warnings["reservation_violation"] += 1
        self._release_reservations(lot, taken=tg.name)

        # toolgroup별 대기 구간을 닫는다 (그림 [7]). 배치 경로는 자기 자리에서
        # 따로 닫는다 — 거기서는 `_commit`이 배치의 **대표 lot**에 대해서만
        # 불리므로 여기서만 세면 나머지 구성원의 대기가 통째로 사라진다.
        since = getattr(lot, "queue_since", None)
        if since is not None:
            self._note_wait(tg.name, self.env.now - since)
            lot.queue_since = None

        problem = ltl_on_commit(lot, step, tool, self.ctx)
        if problem == "violation":
            self.ltl_violations += 1
            self.warnings["ltl_chain_violation"] += 1
        elif problem == "missing":
            self.ltl_missing += 1
            self.warnings["ltl_missing_dedication"] += 1
        elif problem == "no-history":
            self.ltl_missing_no_history += 1
            self.warnings["ltl_missing_no_history"] += 1
        elif problem == "resumed":
            # 결함이 아니다 — warnings에 넣지 않는다. 08 4.1절이 정한 동작이다.
            self.ltl_resumed += 1
        lot.ltl_forced = None

        # 퇴장 순서 사슬 (05 R3) — 직전 job보다 먼저 나갈 수 없다.
        # cascading이 아니면 사슬을 만들지 않는다. capacity 1이라 애초에
        # 동시에 두 job이 없고, 불필요한 이벤트를 만들면 홉이 늘어난다.
        if tg.spec.cascading:
            lot.exit_after = tool.last_exit_event
            lot.exit_event = tool.last_exit_event = self.env.event()

            gate = entry_interval(step, lot.wafers_per_lot)

            # 직전 진입과의 간격이 그때 걸린 게이트 이상인지 기록한다 (R2).
            # 정지 중에는 게이트가 멈추므로 벽시계 간격은 게이트보다 짧을 수
            # 없다. 짧으면 게이트가 새는 것이고, 유효 용량이 조용히 올라간다.
            now = self.env.now
            if tool.last_entry is not None and tool.last_gate > 0.0:
                gap = now - tool.last_entry
                if gap < tool.last_gate - 1e-9:
                    tool.gate_violations += 1
                    self.warnings["cascade_gate_violation"] += 1
                else:
                    tool.gap_slack += gap - tool.last_gate
                tool.entries += 1
            tool.last_entry = now
            tool.last_gate = gate

            if gate > 0.0:                                   # 05 R2
                tool.gate_closed = True
                self.env.process(self._gate_countdown(tool, tg, gate))
        return req

    def _gate_countdown(self, tool, tg: ToolGroup, duration: float):
        """진입 게이트가 열릴 때까지 세는 프로세스 (05 R2, R9).

        **남은 시간을 여기 지역변수로 들고 있는다.** 절대 시각으로 두면 정지
        중에도 계속 흘러, 복구 직후 게이트가 이미 열려 있게 된다 — 유효 용량이
        정지 시간만큼 올라간다 (5절 실패 모드).

        `current_processes`에 등록하므로 Phase 03의 `interrupt_if_working`이
        이 프로세스도 중단시킨다. 새 기구를 만들지 않는다.
        """
        remaining = duration
        proc = self.env.active_process
        tool.current_processes.add(proc)
        try:
            while remaining > 1e-9:
                yield from self._wait_until_up((), tool)      # R9
                started = self.env.now
                try:
                    yield self.env.timeout(remaining)
                    remaining = 0.0
                except simpy.Interrupt:
                    remaining -= self.env.now - started
        finally:
            tool.current_processes.discard(proc)
            tool.gate_closed = False
            tg.mark_released(tool)       # 후보 목록 복귀 — 두 번째 슬롯이 산다
            tg.signal_free()             # 배정 서버를 깨운다

    @staticmethod
    def _undo_commit(tg: ToolGroup, tool, req) -> None:
        """확정을 되돌린다. lot이 장비를 받자마자 죽은 경우에만 쓴다."""
        tool.resource.release(req)
        tg.mark_released(tool)
        tg.signal_free()

    def _tool_server(self, tg: ToolGroup):
        """한 toolgroup의 장비 배정을 전담하는 프로세스.

        자리가 있는 한 계속 배정한다. 더 못 하면 장비가 비거나 lot이 새로
        들어올 때까지 잔다 — 그 둘 말고는 상황이 바뀔 길이 없다.
        `_batch_server`와 같은 구조다.
        """
        while True:
            while self._try_dispatch(tg):
                pass
            yield tg.free_event() | tg.arrival_event()

    def _try_dispatch(self, tg: ToolGroup) -> bool:
        """대기열의 lot 하나에 장비를 넘겼으면 True.

        **후보를 (장비, lot) 쌍으로 모아 한 번에 고른다.** 옛 구조는 빈 장비를
        index 순으로 훑다가 받을 수 있는 첫 장비에 바로 넘겼는데, 거기에 규칙
        위반이 둘 있었다:

        * **wake-up 규칙(R6)이 적용되지 않았다.** `select_tool`을 부르지
          않으므로 "빈 장비가 여럿이면 재구성이 가장 적은 장비"가 빠졌다.
          문서도 같은 말을 한다 — generic_format의 `TOOL WAKE UP Ranking`,
          AutoSched의 `wake_LeastSetupTime`.
        * **한 장비의 거절을 toolgroup 전체의 거절로 읽었다.** `select_lot`이
          `None`을 주면 그 자리에서 `return False` 했다. `None`은 "이 장비가
          못 받는다"이지 "아무도 못 받는다"가 아니다. MINRUN이 걸린 implant
          3개와 LTL이 걸린 litho 3개에서 실제로 갈린다.

        지금은 못 받는 장비를 **건너뛰고**(`continue`), 받을 수 있는 쌍을 전부
        모아 `pair_rank`로 하나를 고른다. 규칙은 `dispatching.py`에 있다 —
        여기는 후보를 모으고 계수를 세는 일만 한다.

        크기는 계측했다 (0.5년 explicit): 뒤 장비를 봤기 때문에 배정이 일어난
        경우가 46,833회(배정의 2.2%), 옛 코드와 다른 장비를 고른 경우가 54회다.
        """
        if not tg.queue:
            self._gate_clear(tg)                   # 대기가 없으면 막힌 게 아니다
            return False
        gate_only = False
        gate_lot = None
        reserved_only = False
        reserved_lot = None
        best = None                                # (순위 키, 장비, lot)
        now = self.env.now
        # 옛 구조가 무엇을 했을지 재구성하기 위한 둘. 계수 전용이다.
        first_ok = None            # 옛 코드가 골랐을 장비 (index 첫 적합)
        none_before_ok = False     # 옛 코드가 멈췄을 `None`이 그 앞에 있었나
        self.dispatch_passes += 1
        # **순위가 장비에 의존하지 않는 toolgroup에서는 첫 적합 장비에서 멈춘다.**
        #
        # 결과가 같다는 것이 증명된다. `select_lot`의 순위에 장비가 들어오는
        # 경로는 둘뿐이다 — 2순위(setup 비용)와 LTL 필터. 둘 다 없으면 **모든
        # 빈 장비가 같은 lot을 고르고** setup 비용이 전부 0이므로, `pair_rank`는
        # `(-우선순위, 0, CR, 장비 index)`가 되어 index가 가장 작은 장비로
        # 떨어진다. 그건 "index 순으로 훑다 첫 적합"과 같은 답이다.
        #
        # **`continue` 수정은 여기서도 그대로다.** 멈추는 것은 쌍을 **찾은 뒤**
        # 이고, 못 받는 장비를 건너뛰는 동작은 변하지 않는다.
        #
        # 계수로 확인했다: 이 최적화 전후로 `choice_differs`(54)와
        # `rescued`(46,833)가 0.5년 실행에서 한 자리도 안 움직였다.
        single = (tg.name not in self.ctx.setup_bearing
                  and tg.name not in self.ctx.ltl_toolgroups)
        for tool in tg.free_tools:                 # index 순
            self.free_tools_scanned += 1
            self.select_lot_calls += 1
            self.queue_elements_scanned += len(tg.queue)
            lot = select_lot(tg.queue, tool, tg, self.ctx)
            if lot is None:
                # 이 장비가 지금 받을 lot이 없다 — MINRUN(STRICT)으로 놀아야
                # 하거나, LTL 지정 때문에 이 장비 몫의 lot이 대기열에 없다.
                # **다음 장비를 계속 본다.** 여기서 끊으면 돌릴 수 있는 장비가
                # 있는데도 toolgroup이 통째로 쉰다.
                if first_ok is None:
                    none_before_ok = True          # 옛 코드는 여기서 끝났다
                continue
            step = lot.current_step()
            if not tool.can_accept(lot, step):
                healthy = (not tool.is_down and not tool.setting_up
                           and len(tool.resource.users) < tool.resource.capacity)
                # 06 4.1절 — 자리도 있고 멀쩡한데 **setup 때문에만** 막혔는가.
                # `can_accept`는 부수효과가 없어야 하므로(07 R10) 세는 일은
                # 여기서 한다. 이 수가 크면 가정을 DECISIONS로 올려야 한다.
                if healthy and not tool.gate_closed and tool.setup_conflict(step):
                    self.setup_blocked_slots += 1
                # 05 R2 / DECISIONS G-19 — **진입 게이트만 아니었으면 받을 수
                # 있었는가.** `wait_gate` 버킷이 27M 진입 동안 0.0인데, 그게
                # "재보니 0"인지 "잴 길이 없었다"인지 구분되지 않아서 넣는다.
                # 순수 계수라 동역학에 영향이 없다.
                elif (healthy and tool.gate_closed
                        and tool.can_accept(lot, step, ignore_gate=True)):
                    self.gate_blocked_slots += 1
                    gate_only = True
                    if gate_lot is None:
                        # **첫 장비의 lot을 쓴다.** `free_tools`는 index 순이고,
                        # 그 장비가 게이트만 아니었으면 이 lot을 받았을 장비다.
                        gate_lot = lot
                # 11 R6 / DECISIONS G-23 — **예약만 아니었으면 받을 수 있었는가.**
                # 게이트와 같은 모양이다. 두 가지가 **동시에** 막는 경우는 어느
                # 쪽에도 안 세어진다 (`ignore_gate`는 예약을 무시하지 않고
                # `ignore_reserved`는 게이트를 무시하지 않는다). 즉 양쪽 다
                # **적게 세는 쪽으로** 틀린다 — 0을 0이라고 잘못 말하는 방향은
                # 아니므로, "0.0이 측정인가"를 묻는 데는 이 편이 안전하다.
                elif (healthy and tool.reserved_for is not None
                        and tool.reserved_for != lot.lot_id
                        and tool.can_accept(lot, step, ignore_reserved=True)):
                    self.reserved_blocked_slots += 1
                    reserved_only = True
                    if reserved_lot is None:
                        reserved_lot = lot
                continue
            if lot.dispatch_done is None:
                raise RuntimeError(
                    f"{lot.lot_id}가 대기열에 있는데 배정 이벤트가 없다")
            key = pair_rank(lot, tool, step, self.ctx, now)
            self.pairs_evaluated += 1
            if first_ok is None:
                first_ok = tool                    # 옛 코드가 여기서 확정했다
            if best is None or key < best[0]:
                best = (key, tool, lot)
            if single:
                break                              # 더 나은 쌍이 있을 수 없다

        if best is not None:
            _key, tool, lot = best
            # -- 옛 구조와 결정이 갈렸나 (순수 계수) ------------------------
            self.dispatch_commits += 1
            if none_before_ok:
                self.dispatch_rescued += 1         # 옛 코드는 배정 자체를 못 했다
            elif tool is not first_ok:
                self.dispatch_choice_differs += 1  # 옛 코드는 다른 장비를 골랐다
            # setup 비용이 0이면 더 적은 장비가 있을 수 없다 — 97개 toolgroup이
            # 전부 여기서 걸러지므로 아래 훑기는 거의 돌지 않는다.
            setup_cost = pair_setup_rank3(_key, self.ctx, tg.name, now)[0]   # 디스패칭 베타면 자리가 바뀐다
            if setup_cost > 0.0:
                self._note_wake_up_ambiguity(tg, tool, lot, setup_cost)
            # 여기부터 succeed 까지 yield 없음. `pair_rank`도 `select_lot`도
            # 부수효과가 없으므로(07 R10) 위 루프가 상태를 바꾸지 않았고,
            # 따라서 지금 고른 쌍은 여전히 유효하다.
            if getattr(self.kpi, "audit_dispatch", False):    # full 전용 계측
                self.kpi.record_dispatch(*lot_decision("server", tg, tool, lot, self.ctx, now))
            req = self._commit(tg, tool, lot)
            ev, lot.dispatch_done = lot.dispatch_done, None
            ev.succeed((tool, req))
            self._gate_clear(tg)                   # 나갔으니 안 막혔다
            return True

        # **여기까지 왔다 = 이 pass에서 아무도 못 나갔다.** 그중 게이트만
        # 아니었으면 나갈 수 있었던 경우가 있었다면, 그 시간은 `queue`가 아니라
        # `wait_gate`여야 한다 (02 4절의 정의). 위 `gate_blocked_slots`는
        # **시도 수**라 "한 lot이 오래 기다리면 여러 번" 세어지는 반면, 이쪽은
        # **배정 pass 수**다 — 둘을 같이 봐야 규모를 읽을 수 있다.
        if gate_only:
            self.gate_blocked_passes += 1
        if reserved_only:
            self.reserved_blocked_passes += 1
        self._gate_block_mark(tg, gate_only)
        self._gate_label(tg, gate_lot if gate_only else None)
        self._reserved_block_mark(tg, reserved_only)
        self._reserved_label(tg, reserved_lot if reserved_only else None)
        return False

    def _note_wake_up_ambiguity(self, tg: ToolGroup, chosen, lot,
                                cost: float) -> None:
        """고른 장비보다 setup이 적으면서 이 lot을 받을 수 있는 빈 장비가 있었나.

        **위반이 아니라 모호성을 센다.**

        명세는 wake-up을 "lot이 도착했을 때 빈 장비가 여럿이면 재구성이 가장
        적은 장비"로 적는다. 우리 구조는 (lot, 장비) 쌍의 순위로 푼다. 둘이
        갈리는 경우가 하나 있다 — 어떤 빈 장비가 **이 lot을 더 싸게 받을 수
        있는데도** 자기 순위에서는 다른 lot을 최선으로 꼽아 후보에 안 들어온
        경우다.

        어느 쪽이 레퍼런스의 동작인지는 문서로 정해지지 않는다. 그래서 정하지
        않고 **크기를 잰다.** 0.5년 explicit 실행에서 **0건**이므로 이
        데이터셋에서는 두 해석이 같은 답을 낸다 — 정할 필요가 없다.

        부수효과가 없고 난수도 이벤트도 쓰지 않는다 (07 R10).
        """
        step = lot.current_step()
        for tool in tg.free_tools:
            if tool is chosen:
                continue
            if not tool.can_accept(lot, step):
                continue
            if setup_duration(self.setup_model, step, tool.current_setup)[0] < cost:
                self.wake_up_ambiguous += 1
                return

    def _note_wait(self, tg_name: str, waited: float) -> None:
        """toolgroup 대기 한 건을 누적한다 (그림 [7]). 순수 계수다."""
        if self.env.now < self.kpi.warmup:return
        waited=max(0.0,self.env.now-max(self.env.now-waited,self.kpi.warmup))
        rec = self.wait_by_toolgroup[tg_name]
        rec[0] += 1
        rec[1] += waited
        if waited > rec[2]:
            rec[2] = waited

    def _gate_clear(self, tg: ToolGroup) -> None:
        """이 toolgroup은 지금 게이트에도 예약에도 막혀 있지 않다.

        **둘을 한 함수에서 푼다.** `_try_dispatch`의 출구가 네 군데인데 한
        쪽만 풀면 구간이 안 닫혀 시간이 부풀거나 lot이 `wait_*`에 갇힌다.
        """
        self._gate_block_mark(tg, False)
        self._gate_label(tg, None)
        self._reserved_block_mark(tg, False)
        self._reserved_label(tg, None)

    def _gate_label(self, tg: ToolGroup, lot) -> None:
        """게이트에 막힌 lot을 `wait_gate`로, 풀린 lot을 `queue`로 되돌린다.

        02 4절이 `wait_gate`를 "waiting on a cascading tool's entry interval"로
        정의한다. 그 시간이 `queue`에 섞여 있던 것을 여기서 갈라낸다 (G-19).

        **`queue`에 있는 lot만 건드린다.** `BUCKET_PRIORITY`가 `wait_down` >
        `wait_dedicated` > `wait_reserved` > `wait_gate` 순이므로, 그중 하나에
        들어 있는 lot은 게이트에도 막혀 있더라도 그쪽이 이긴다. 이미 배정됐거나
        fab을 나간 lot도 `_bucket`이 달라져 있어 자동으로 걸러진다.

        **표시 대상은 `select_lot`이 고른 lot 하나다.** 대기열의 나머지는
        게이트가 아니었어도 안 나갔으므로 `queue`가 맞다 — 08의
        `wait_dedicated`가 강제된 lot만 세는 것과 같은 논리다.

        `lot.enter`는 난수도 이벤트도 건드리지 않는다. 그래서 이 표시가
        CT·WIP·처리량을 바꾸면 안 되고, `compare`가 그것을 확인한다.
        """
        prev = self._gate_lot.get(tg.name)
        if prev is lot:
            return                              # 상태 그대로 — 구간을 쪼개지 않는다
        if prev is not None:
            if prev._bucket == "wait_gate":
                prev.enter("queue", self.env.now)
            del self._gate_lot[tg.name]
        if lot is not None and lot._bucket == "queue":
            lot.enter("wait_gate", self.env.now)
            self._gate_lot[tg.name] = lot

    def _gate_block_mark(self, tg: ToolGroup, blocked: bool) -> None:
        """게이트가 이 toolgroup을 묶고 있던 구간을 누적한다 (G-19).

        `_try_dispatch`의 **모든 출구**에서 부른다. 한 군데라도 빠뜨리면 구간이
        안 닫혀 시간이 부풀거나, 안 열려 0으로 남는다.

        재는 것은 **toolgroup 구간**이지 장비 수를 곱한 값이 아니다. 묻고 있는
        질문이 "lot이 게이트 때문에 얼마나 묶여 있었나"이기 때문이다 —
        "게이트가 용량을 얼마나 먹었나"와는 다른 양이다.
        """
        since = self._gate_block_since.get(tg.name)
        if blocked:
            if since is None:
                self._gate_block_since[tg.name] = self.env.now
        elif since is not None:
            del self._gate_block_since[tg.name]
            span = self.env.now - since
            self.gate_blocked_time += span
            self.gate_blocked_by_area[tg.spec.area] += span

    def gate_blocked_totals(self) -> tuple[float, dict[str, float]]:
        """(총 분, area별 분). **실행이 끝난 시점에 부른다.**

        끝나는 순간 열려 있던 구간을 여기서 닫는다 — `env.run(until=T)`는
        프로세스를 중간에 버리므로 `_try_dispatch`가 다시 불리지 않는다.
        빠뜨리면 마지막 구간만큼 조용히 적게 나온다.
        """
        total = self.gate_blocked_time
        by_area = dict(self.gate_blocked_by_area)
        for name, since in self._gate_block_since.items():
            span = self.env.now - since
            total += span
            area = self.toolgroups[name].spec.area
            by_area[area] = by_area.get(area, 0.0) + span
        return total, by_area

    # -- 예약이 남을 묶어 둔 구간 (G-23) -------------------------------------
    #
    # 아래 셋은 위 게이트 셋과 **한 글자만 다른 쌍**이다. 합치지 않는 이유는
    # `BUCKET_PRIORITY`가 둘을 다르게 취급하고(`wait_reserved` > `wait_gate`),
    # 한 toolgroup이 동시에 양쪽 상태일 수 있기 때문이다 — 구간을 공유하면
    # 한쪽이 열릴 때 다른 쪽까지 닫힌다.

    def _reserved_label(self, tg: ToolGroup, lot) -> None:
        """예약에 막힌 lot을 `wait_reserved`로, 풀린 lot을 `queue`로.

        02 4절이 이 버킷을 정의해 놓았는데 **아무도 채우지 않았다** (G-23).
        `lot.enter`는 난수도 이벤트도 건드리지 않으므로 이 표시가 CT·WIP·
        처리량을 바꾸면 안 되고, `compare`가 그것을 확인한다.
        """
        prev = self._reserved_lot.get(tg.name)
        if prev is lot:
            return                              # 상태 그대로 — 구간을 쪼개지 않는다
        if prev is not None:
            if prev._bucket == "wait_reserved":
                prev.enter("queue", self.env.now)
            del self._reserved_lot[tg.name]
        if lot is not None and lot._bucket == "queue":
            lot.enter("wait_reserved", self.env.now)
            self._reserved_lot[tg.name] = lot

    def _reserved_block_mark(self, tg: ToolGroup, blocked: bool) -> None:
        """예약이 이 toolgroup을 묶고 있던 구간을 누적한다."""
        since = self._reserved_block_since.get(tg.name)
        if blocked:
            if since is None:
                self._reserved_block_since[tg.name] = self.env.now
        elif since is not None:
            del self._reserved_block_since[tg.name]
            span = self.env.now - since
            self.reserved_blocked_time += span
            self.reserved_blocked_by_area[tg.spec.area] += span

    def reserved_blocked_totals(self) -> tuple[float, dict[str, float]]:
        """(총 분, area별 분). 게이트 쪽과 같이 **끝나는 시점에** 부른다."""
        total = self.reserved_blocked_time
        by_area = dict(self.reserved_blocked_by_area)
        for name, since in self._reserved_block_since.items():
            span = self.env.now - since
            total += span
            area = self.toolgroups[name].spec.area
            by_area[area] = by_area.get(area, 0.0) + span
        return total, by_area

    # -- 배치 (Phase 04) ----------------------------------------------------

    def _batch_step(self, lot: Lot, step: "Step", tg: ToolGroup):
        """배치 그룹에 들어가 자기 배치가 끝날 때까지 기다린다.

        R1  같은 제품의 같은 step 끼리만 묶인다 — 그룹 key가 그 규칙 자체다.
        R4  서버가 장비를 확보한 뒤에야 그룹에서 빠진다. 그 전까지 이 lot은
            항상 그룹 안에 있고, 대기열과 처리 어느 쪽에도 없는 상태가 없다.
        R9  대기 시작은 **자기 도착 시각**이다. 배치를 채운 마지막 lot이
            아니라 각자 기준으로 잰다.

        기다리는 신호는 서버가 만들고 서버가 올린다. lot이 자기가 올려야 할
        신호를 기다리는 구조가 아니다 (04_batch.md 5절).
        """
        # 11 — 도착했으므로 예약을 푼다. **배치에서 이게 특히 중요하다**:
        # 붙들고 있으면 lot이 "배치가 차기를 기다리는 동안" 그 배치를 돌릴
        # 장비를 잠그는 자기 교착이 된다. seed 42에서 실제로 그랬다 —
        # Diffusion_FE_100의 3대 중 2대가 super hot 두 lot에 영구 예약되고
        # 완료가 0이 됐다. 문서의 지속 시간 0(HOTLOTDELAY%)이 이걸 막는다.
        self._release_reservations(lot, taken=tg.name)

        key = (lot.product, step.step_index)
        group = tg.batch_queue.get(key)
        if group is None:
            # deque다. 배치가 나갈 때 앞에서 몇 개를 빼는데, list면 그때마다
            # 뒤 전체를 당긴다. 과부하 구간에서 한 그룹에 수천 개가 쌓이므로
            # (배치 수 x 대기 수)가 되어 실행 시간이 몇 배로 뛴다.
            group = tg.batch_queue[key] = deque()
            tg.batch_wafers[key] = 0
        group.append(lot)
        tg.batch_wafers[key] += lot.wafers_per_lot
        self.kpi.note_batch_queue(tg, key, step.batch_min,added=lot)

        lot.batch_since = self.env.now
        lot.enter("wait_batch", self.env.now)
        done = lot.batch_done = self.env.event()
        tg.signal_batch_arrival()
        try:
            yield done
        except BaseException:
            # 프로세스가 죽더라도 그룹에 시체를 남기지 않는다. wafer 합도 같이
            # 되돌린다 — 한쪽만 고치면 그 그룹은 영영 최소 수량을 못 맞춘다.
            # Batch formation replaces the deque; the arrival-time reference can
            # still contain lots that have already left in a formed batch.
            group = tg.batch_queue[key]
            if lot in group:
                group.remove(lot)
                tg.batch_wafers[key] -= lot.wafers_per_lot
                self.kpi.note_batch_queue(tg, key, step.batch_min,removed=(lot,))
            raise

    def _batch_server(self, tg: ToolGroup):
        """한 배치 toolgroup의 배치 형성을 전담하는 프로세스.

        lot이 각자 배치를 만들려 하면 같은 시각에 깨어난 여러 lot이 같은 그룹을
        보고 동시에 배치를 만드는 경쟁이 생긴다. 결정 주체를 하나로 두면 그
        경쟁이 성립하지 않는다. Phase 07이 바꿀 선택 지점도 여기 한 곳이다.

        자리가 있는 한 계속 만든다. 더 못 만들면 장비가 비거나 lot이 새로
        들어올 때까지 잔다 — 두 가지 말고는 상황이 바뀔 길이 없다.
        """
        while True:
            while self._try_form_batch(tg):
                pass
            yield tg.free_event() | tg.batch_arrival_event()

    def _try_form_batch(self, tg: ToolGroup) -> bool:
        """배치를 하나 만들어 띄웠으면 True.

        R2 최소 수량을 채운 그룹만 만든다. 타임아웃도 미달 배치도 없다.
        R3 최대 수량을 넘기지 않는다.
        R10 우선순위는 배치 형성에 개입하지 않는다. 기다리는 hot lot이 있다고
            미달 배치를 돌리지 않는다. 그 대가는 측정해서 보고한다.

        min/max는 **wafer 수**다 (BATCHING UNIT = Wafer). lot 수로 비교하면
        75 wafer 최소가 75 lot으로 읽혀 영원히 안 모인다.

        그룹은 `select_batch_group`이 최고 우선순위 -> 최소 CR -> 그룹 생성
        순서로 고른다. 구성원 선택은 `select_batch_members`가 담당한다.
        """
        # 빈 장비가 하나도 없으면 어떤 그룹도 나갈 수 없다. **먼저 끊는다.**
        # 없어도 결과는 같지만, 아래 재시도 루프가 자격 그룹을 전부 훑고 나서야
        # False를 낸다 — 0.5년 실행에서 재시도가 191,095건에서 54건으로 줄었고
        # 런타임이 33% 내려갔다.
        if not tg.free_tools:
            return False

        # 자격을 갖춘 그룹을 모은다. dict 삽입 순서 = 그룹이 처음 생긴 순서.
        eligible = [
            (key, waiting) for key, waiting in tg.batch_queue.items()
            if waiting and tg.batch_wafers[key] >= waiting[0].current_step().batch_min
        ]
        if not eligible:
            return False

        # 그룹 선택 규칙은 dispatching.py에 둔다.
        # **순위대로 훑다가 장비를 잡는 첫 그룹이 나간다.** 예전에는 1위 그룹이
        # 장비를 못 잡으면 거기서 `return False` 했는데, 그건 `_try_dispatch`가
        # 갖고 있던 것과 같은 결함이다 — 1위 그룹의 사정을 나머지 그룹의
        # 사정으로 읽는다.
        #
        # 목록에서 뺄 때 **상대 순서를 유지한다.** `select_batch_group`의 마지막
        # 동점은 "그룹이 처음 생긴 순서"이므로, 순서가 흐트러지면 동점 처리가
        # 실행마다 달라진다.
        remaining = list(eligible)
        picked = None
        while remaining:
            cand = select_batch_group(remaining, None, tg, self.ctx)
            self.batch_groups_evaluated += 1
            if cand is None:
                break
            key, waiting = cand
            step = waiting[0].current_step()
            tool = select_tool(tg, waiting[0], step, self.ctx)
            if tool is not None:
                picked = (key, waiting, step, tool)
                break
            # 이 그룹은 지금 장비를 못 잡는다. 예약된 장비가 섞여 있어도
            # 기다리면 된다 — 예약은 **예약자가 도착하는 순간 풀리므로**
            # (11 B-4) 이 막힘은 그 lot의 이동 시간만큼이고, 문서가 말하는
            # "the tool only waits until the Super Hot Lot arrives"가 바로
            # 그 상태다. 다음 순위 그룹을 본다.
            self.batch_group_retries += 1
            remaining = [g for g in remaining if g[0] != key]
        if picked is None:
            return False
        best_key, waiting, step, tool = picked

        # 대기열 전체에서 우선순위 -> CR -> 도착 순서로 최대 수량까지 고른다.
        # FIFO 구성원도 반환받아 순위 선택의 효과를 계측한다.
        members, fifo_members = select_batch_members(
            waiting, step.batch_max, self.ctx, toolgroup=tg)
        total = sum(l.wafers_per_lot for l in members)
        # 이 변경이 **몇 번 다르게 골랐나**. 크기를 모르면 고칠 값어치도 모른다.
        if [id(l) for l in members] != [id(l) for l in fifo_members]:
            self.batch_reorders += 1
            best = max(l.priority for l in members)
            if best > max((l.priority for l in fifo_members), default=0):
                self.batch_priority_rescues += 1

        if not members:
            # 대기 wafer는 batch_min을 넘겼는데 lot 하나도 batch_max에 안 들어간다
            # = lot 하나가 상한보다 크다. **데이터 오류다.** 이 데이터셋에서는
            # 상한이 75~150 wafer이고 lot이 25 wafer라 일어날 수 없다.
            # 조용히 False를 돌려주면 그 lot들이 영영 잠들고 WIP에만 남으므로
            # 여기서 무엇이 틀렸는지 말하고 멈춘다 — 예전에는 아래 `max()`가
            # 빈 목록을 받아 원인을 알 수 없는 예외를 냈다.
            raise ValueError(
                f"{tg.name} step {step.step_index}: lot 하나"
                f"({waiting[0].wafers_per_lot} wafer)가 batch_max"
                f"({step.batch_max} wafer)보다 크다. batch 상한은 lot 수가 "
                f"아니라 **wafer 수**다")

        # full 전용 계측 — 대기열에서 빼기 전의 상태로 판단을 남긴다.
        audit = (batch_decision(tg, eligible, remaining, picked, members, fifo_members,
                                self.ctx, self.env.now)
                 if getattr(self.kpi, "audit_dispatch", False) else None)

        # R4 — 장비를 확정한 지금에야 그룹에서 뺀다. 여기부터 request 까지
        # yield가 없어야 다른 프로세스가 같은 장비를 가져가지 못한다.
        req = tool.resource.request(priority=-max(l.priority for l in members))
        tg.mark_taken(tool)
        # **앞에서만 빼면 안 된다.** 순위로 고르면 구성원이 대기열 중간에
        # 있을 수 있다. `popleft`를 쓰면 엉뚱한 lot이 빠져 나가고 그 lot은
        # 처리되지 않은 채 사라진다 — WIP에서만 없어지는 종류의 결함이다.
        #
        # 남길 것만 새로 담는다. 한 그룹의 대기가 수십이라 비용이 안 든다.
        # `batch_wafers` 누계는 **뺀 것의 합**으로 갱신한다 (C04.16이 실행
        # 끝에 누계와 실제 대기열의 일치를 확인한다).
        chosen = {id(l) for l in members}
        keep = deque(l for l in waiting if id(l) not in chosen)
        if len(keep) != len(waiting) - len(members):
            raise RuntimeError(
                f"{tg.name} step {step.step_index}: 배치 구성원 제거가 "
                f"어긋났다 (대기 {len(waiting)}, 구성원 {len(members)}, "
                f"남은 {len(keep)})")
        tg.batch_queue[best_key] = keep
        tg.batch_wafers[best_key] -= total
        self.kpi.note_batch_queue(tg, best_key, step.batch_min,removed=members)

        self._record_batch(tg, step, members, total)
        if audit is not None:
            self.kpi.record_dispatch(*audit)
        self.env.process(self._run_batch(tg, tool, req, step, members, total))
        return True

    def _record_batch(self, tg: ToolGroup, step: "Step",
                      members: list[Lot], wafers: int) -> None:
        """배치 통계. 배치가 확정된 시점이 대기가 끝나는 시점이다."""
        now = self.env.now
        self.n_batches += 1
        self.batch_size_hist[(tg.name, len(members))] += 1
        for l in members:
            w = now - (l.batch_since if l.batch_since is not None else now)
            # 그림 [7] — 배치 toolgroup의 대기는 여기서 닫는다. `_commit`은
            # 배치당 한 번(대표 lot)만 불리므로 거기서만 세면 나머지 구성원의
            # 대기가 통째로 사라진다. 같은 toolgroup의 비배치 step과 한 통에
            # 들어가는 것이 맞다 — lot 입장에서는 같은 장비를 기다린 시간이다.
            self._note_wait(tg.name, w)
            rec = self.batch_wait_by_type[l.lot_type]
            rec[0] += 1
            rec[1] += w
            if w > rec[2]:
                rec[2] = w
            if w > self.max_batch_wait[0]:
                self.max_batch_wait = (w, tg.name, l.product, step.step_index)
            # AGENTS.md 7.4절: 7일 넘게 기다렸으면 경고하고 계속 간다.
            if w > 7 * 24 * 60:
                self.warnings["batch_wait_over_7d"] += 1

    def _run_batch(self, tg: ToolGroup, tool, req, step: "Step",
                   members: list[Lot], wafers: int):
        """배치 하나를 장비 위에서 돌린다.

        R5 처리 시간은 배치 크기와 무관하다. PROCESSING UNIT = Batch 이므로
           `calculate_processing_time`이 표본을 그대로 돌려준다.
        R6 load와 unload는 배치당 한 번이다. lot마다 걸면 Diffusion 가동률이
           배치 크기 배수만큼 부풀어 오른다.
        R7 PM 카운터에는 배치 전원의 wafer 합이 간다.
        R8 정지는 배치 전체를 한 단위로 중단·재개한다. `_run_processing`에
           전원을 넘기므로 중단/재개 로직이 한 벌로 유지된다.
        """
        try:
            yield req
            tool.start_busy()

            batch_id = self.kpi.begin_batch(tg, tool, step, members, wafers)

            now = self.env.now
            for m in members:
                m.enter("load_unload", now)
            if tg.spec.load_time > 0.0:
                t = self.env.now
                tool.work_enter("loadunload_time")
                try:
                    yield self.env.timeout(tg.spec.load_time)   # R6
                finally:
                    tool.work_exit("loadunload_time", t)

            pt = calculate_processing_time(step, members[0].wafers_per_lot,
                                           self.rng)            # R5
            yield from self._run_processing(members, tool, pt)
            tool.add_wafers(wafers)                             # R7

            now = self.env.now
            for m in members:
                m.enter("load_unload", now)
            if tg.spec.unload_time > 0.0:
                t = self.env.now
                tool.work_enter("loadunload_time")
                try:
                    yield self.env.timeout(tg.spec.unload_time)  # R6
                finally:
                    tool.work_exit("loadunload_time", t)
            self.kpi.end_batch(batch_id)
        finally:
            self.kpi.release_work_context(tool)
            tool.resource.release(req)
            tg.mark_released(tool)
            tool.stop_busy()
            tg.signal_free()
            # 중간에 무엇이 터지든 멤버를 깨운다. 여기서 빠뜨리면 그 lot들은
            # 영원히 잠들고 WIP에만 남는다.
            for m in members:
                ev, m.batch_done = m.batch_done, None
                if ev is not None and not ev.triggered:
                    ev.succeed()

    # -- 단일 lot 처리 -------------------------------------------------------

    def _process_on_tool(self, lot: Lot, step: "Step", tool, tg: ToolGroup):
        """load -> 처리 -> unload. 장비 상태 시간은 구간별로 따로 적는다.

        proc / loadunload / setup 은 서로 배타적이어야 한다. 각 구간이 끝난
        직후 그 길이만 더한다.
        """
        tool.start_busy()

        # setup — 장비를 잡은 **뒤**, load **앞**이다 (06 R8).
        yield from self._do_setup(lot, step, tool, tg)

        # load
        lot.enter("load_unload", self.env.now)
        if tg.spec.load_time > 0.0:
            t = self.env.now
            tool.work_enter("loadunload_time")
            try:
                yield self.env.timeout(tg.spec.load_time)
            finally:
                tool.work_exit("loadunload_time", t)

        # 처리 — 정지에 의해 중단되고 재개될 수 있다
        pt = calculate_processing_time(step, lot.wafers_per_lot, self.rng)
        yield from self._run_processing((lot,), tool, pt)

        # 05 R3 — 먼저 들어간 job보다 먼저 나갈 수 없다. 처리가 끝나도 앞
        # job이 아직 뒤 모듈에 있으면 여기서 막힌다. 그동안 이 lot은 여전히
        # 장비 위에 있으므로 process 버킷에 남는다 — 대기열에 있는 게 아니다.
        if tg.spec.cascading:
            yield from self._wait_for_predecessor(lot, tool)

        tool.add_wafers(self._wafers_for(step, lot))

        # unload
        lot.enter("load_unload", self.env.now)
        if tg.spec.unload_time > 0.0:
            t = self.env.now
            tool.work_enter("loadunload_time")
            try:
                yield self.env.timeout(tg.spec.unload_time)
            finally:
                tool.work_exit("loadunload_time", t)

    # -- setup (Phase 06) ---------------------------------------------------

    def _do_setup(self, lot: Lot, step: "Step", tool, tg: ToolGroup):
        """필요하면 장비를 재구성한다 (06_setup.md).

        R1  setup 이름이 없는 step은 아무것도 하지 않고 장비 상태도 안 바꾼다.
        R2  같은 setup이면 건너뛰고 비용 0. `WHEN`이 401개 전부 `need`다.
        R3  아직 아무 setup도 없는 장비는 **공짜로** 잡는다. 1,313대가 실행
            시작마다 한 번씩 물면 setup 비율이 부풀어 오른다 (DECISIONS B-6).
        R9  정지에 중단되고 남은 시간만 이어서 한다 — Phase 03 기구를 그대로
            쓴다. 두 번째 기구를 만들지 않는다.
        R11 setup 시간은 처리 시간과 **다른 상태**다. proc_time에 넣지 않는다.
        """
        name = step.setup_name
        if name is None:
            return                                           # R1

        group = tg.name
        if tool.current_setup == name:
            tool.run_count += 1
            self.setup_skipped += 1
            self.setup_by_group[group][1] += 1
            return                                           # R2

        dur, undefined = setup_duration(self.setup_model, step, tool.current_setup)
        if undefined:
            # AGENTS.md 7.4절 — 경고하고 0으로 진행한다. 이 데이터셋에서는
            # 0건이어야 하므로, 0이 아니면 그 자체가 발견이다.
            self.warnings["setup_transition_undefined"] += 1

        if tool.current_setup is None:
            self.kpi.record_event(lot, "setup_initial", station=tool.tool_id,
                                  toolgroup=tg.name, setup=name)
            tool.current_setup = name                        # R3
            tool.run_count = 1
            self.setup_initial += 1
            return

        previous = tool.current_setup
        self.kpi.record_event(lot, "setup_change", station=tool.tool_id,
                              toolgroup=tg.name, previous=previous, setup=name,
                              planned_duration_min=dur)
        # 방금 끝난 연속 실행의 길이. 이 값이 MINRUN보다 작으면 최소를 못
        # 지킨 채 바꾼 것이다 (override 또는 같은 setup 대기 없음).
        if tool.run_count > 0:
            self.run_lengths[group][tool.run_count] += 1
        self.n_setups += 1
        self.setup_by_group[group][0] += 1

        if dur > 0.0:
            # 재구성 중에는 아무도 못 들어온다. 이 순간 장비는 이전 상태도
            # 새 상태도 아니다 (06 4.1절).
            tool.setting_up = True
            try:
                yield from self._run_on_tool((lot,), tool, dur,
                                             "setup", "setup_time")   # R9, R11
            finally:
                tool.setting_up = False

        rec = self.setup_transitions[(previous, name)]
        rec[0] += 1
        rec[1] += dur
        tool.current_setup = name
        tool.run_count = 1
        tg.signal_free()          # 새 setup을 기다리던 lot이 있을 수 있다

    def _wait_for_predecessor(self, lot: Lot, tool):
        """이 장비에 나보다 먼저 들어간 job이 나갈 때까지 기다린다 (05 R3).

        진입 순서 = 퇴장 순서다. 두 점유를 독립적으로 타이밍하면 나중에 들어간
        job이 먼저 나갈 수 있다 — 05 7.2절의 TF_BE_40이 실제로 그런 경우다
        (Product_3 pt 71.29 / interval 6.59, Product_1 pt 62.82).

        사슬이므로 앞 job이 이미 나갔으면 이벤트가 발화해 있어 즉시 통과한다.
        """
        prev = lot.exit_after
        lot.exit_after = None
        if prev is not None and not prev.triggered:
            try:
                yield prev
            except simpy.Interrupt:
                # 정지가 걸려도 순서 제약은 그대로다. 다시 기다린다.
                if not prev.triggered:
                    yield prev

    @staticmethod
    def _release_successor(lot: Lot) -> None:
        """내가 나갔음을 뒤 job에게 알린다 (05 R3)."""
        ev, lot.exit_event = lot.exit_event, None
        if ev is not None and not ev.triggered:
            ev.succeed()

    def _run_processing(self, lots, tool, pt: float):
        """처리 대기. 정지가 걸리면 중단하고 복구 후 남은 시간만 이어서 한다.

        **CQT window의 양 끝이 여기다** (Phase 10 R1/R2). 이 함수가 단일 lot과
        배치의 **유일한** 처리 지점이고 `lots`가 배치 전원이므로, 훅을 여기
        하나에 걸면 R6("배치의 모든 멤버가 자기 window를 연다/닫는다")이
        구조적으로 따라온다 — 배치를 이끄는 lot만 훅을 받는 실수(10 5절)를
        할 수가 없다.

        닫기는 `_run_on_tool`의 **첫 시작**에 건다. 처리 중 정지로 중단됐다
        재개되는 것은 새 시작이 아니고, R2는 "장비가 살아나기를 기다린 뒤의
        실제 처리 시작"이라고 못 박는다.

        훅은 함수 호출일 뿐이라 난수도 이벤트도 쓰지 않는다 (R5).
        """
        yield from self._run_on_tool(lots, tool, pt, "process", "proc_time",
                                     on_first_start=self._cqt_close)
        self._cqt_open(lots)                       # R1 — unload 앞이다

    def _run_on_tool(self, lots, tool, duration: float,
                     bucket: str, accum: str, on_first_start=None):
        """장비 위에서 `duration`만큼 보낸다. 정지에 중단되고 재개된다.

        처리와 setup이 이 한 곳을 공유한다. 06 R9가 "두 번째 기구를 만들지
        말라"고 명시한다 — 중단/재개는 미묘하고, 두 벌이 되면 한쪽만 고쳐진다.

        `lots`는 **이 작업에 묶인 lot 전부**다. 단일 작업이면 원소 하나, 배치면
        전원이다 (04 R8). `bucket`은 CT 버킷 이름, `accum`은 장비 상태 시간
        속성 이름이다 — 처리와 setup은 서로 다른 상태다 (06 R11).

        R9  lot은 장비 위에 머문다. unload 되지 않고 다른 lot이 가져갈 수도 없다.
            그래서 자원 점유를 놓지 않는다.
        R10 정지 중에는 작업을 **시작하지 않는다**. load 중에 고장이 났다면
            load는 끝나도 처리는 시작하면 안 된다. 그래서 매 반복 앞에서 확인한다.
        R12 복구를 기다리는 동안 또 정지가 걸려도 안전하게 흡수한다.

        `on_first_start`는 **첫 진입에서 한 번만** 불린다 (Phase 10 R2).
        정지로 중단됐다 재개되는 것은 새 시작이 아니다. setup은 넘기지 않는다 —
        CQT window는 처리 시작에서 닫히고 setup은 창 **안**이다 (10 4절).
        """
        remaining = duration
        proc = self.env.active_process
        tool.current_processes.add(proc)
        first = True
        try:
            while remaining > 1e-9:
                # R10 — 실제 진입 직전마다 확인한다
                yield from self._wait_until_up(lots, tool)

                started = self.env.now
                if first:
                    first = False
                    if on_first_start is not None:
                        on_first_start(lots, started)
                for lot in lots:
                    lot.enter(bucket, started)
                # 상태별 누계와 작업시간 합집합을 한 자리에서 적는다.
                # `_wait_until_up`은 이 구간 **밖**이므로 정지 시간은 안 든다.
                # `finally`인 이유: 여기서 프로세스가 죽어도 카운터가 새면
                # 그 장비는 남은 실행 내내 "일하는 중"으로 남는다.
                tool.work_enter(accum)
                try:
                    try:
                        yield self.env.timeout(remaining)
                        remaining = 0.0
                    except simpy.Interrupt:
                        remaining -= self.env.now - started
                finally:
                    tool.work_exit(accum, started)
        finally:
            tool.current_processes.discard(proc)

    def _wait_until_up(self, lots, tool):
        """장비가 살아날 때까지 기다린다. 묶인 lot 전원이 함께 기다린다.

        복구 알림은 정지 에피소드당 하나이고 매번 새로 만들어지므로, 반복마다
        다시 읽어야 한다. 기다리는 도중 또 interrupt가 와도 그냥 다시 확인한다
        (R12) — 여기서 예외를 흘리면 lot의 프로세스가 죽는다.
        """
        if not tool.is_down:
            return
        now = self.env.now
        for lot in lots:
            lot.enter("wait_down", now)
        while tool.is_down:
            ev = tool.up_event
            if ev is None:
                break
            try:
                yield ev
            except simpy.Interrupt:
                continue

    @staticmethod
    def _wafers_for(step: "Step", lot: Lot) -> int:
        """PM 카운터에 더할 wafer 수. Phase 04가 batch 전체로 바꾼다."""
        return lot.wafers_per_lot


# ---------------------------------------------------------------------------
# 부하 진단 (02_core_flow.md 4.1절)
# ---------------------------------------------------------------------------


def lots_per_batch(step: "Step", wafers_per_lot: int) -> int:
    """이 step의 배치 하나에 들어가는 lot 수.

    BATCHING UNIT이 Wafer이므로 최대 wafer 수를 lot 크기로 나눈다.
    **최대**를 쓰는 이유: offered load는 "이상적으로 돌렸을 때 얼마나
    필요한가"를 묻는 값이고, rho > 1이 문제가 되는 포화 구간에서는 대기가
    충분해 배치가 최대까지 찬다. 최소로 나누면 필요 용량을 과대평가한다.
    """
    if step.batch_max is None or wafers_per_lot <= 0:
        return 1
    return max(1, step.batch_max // wafers_per_lot)


def expected_visits(route, *, sampling: bool = False, rework: bool = False,
                    iterations: int = 40) -> list[float]:
    """route의 각 step을 lot 하나가 **평균 몇 번 수행하는가**.

    sampling만 켜면 그냥 처리 확률이다. rework까지 켜면 되돌아가서 다시 걷는
    몫이 붙는다. 09 4.1절이 요구하는 "sampling을 반영한 필요 부하"의 계수다.

    rework는 뒤쪽 step이 앞쪽 step의 방문 수를 늘리고, 그 앞쪽 step이 다시
    뒤쪽으로 흘러 들어오는 순환이다. 닫힌 식 대신 **고정점 반복**으로 푼다 —
    이 데이터셋의 rework 확률이 1~2%라 몇 번이면 수렴한다. 수렴하지 않으면
    확률을 잘못 읽은 것이다 (09 3.1절).

        visits[i] = arrivals[i] * p_sample[i]           수행한 횟수
        out[i]    = visits[i]   * p_rework[i]           i에서 되돌아간 횟수
        arrivals[i] = arrivals[i-1] - out[i-1] + (i로 되돌아온 몫)

    **rework는 수행한 뒤에 판정한다** (R3). 건너뛴 step은 수행되지 않았으므로
    되돌려 보내지 않는다 — arrivals가 아니라 visits에 확률을 곱하는 이유다.
    """
    steps = route.steps
    n = len(steps)
    p_s = [1.0 if not sampling or s.sampling_prob is None
           else s.sampling_prob / 100.0 for s in steps]
    if not rework:
        return p_s[:]

    # 되돌아오는 흐름: (출발 index, 목적지 index, 확률)
    links = [(i, s.rework_target_index, s.rework_prob / 100.0)
             for i, s in enumerate(steps)
             if s.rework_prob is not None and s.rework_target_index is not None]
    if not links:
        return p_s[:]

    visits = p_s[:]
    for _ in range(iterations):
        into = [0.0] * n
        out = [0.0] * n
        for src, dst, p in links:
            out[src] = visits[src] * p
            into[dst] += out[src]

        arrivals = [0.0] * n
        arrivals[0] = 1.0 + into[0]
        for i in range(1, n):
            arrivals[i] = arrivals[i - 1] - out[i - 1] + into[i]

        new = [arrivals[i] * p_s[i] for i in range(n)]
        if max(abs(a - b) for a, b in zip(new, visits)) < 1e-12:
            return new
        visits = new
    return visits


def offered_load(data: "ModelData", *, batching: bool = False,
                 cascading: bool = False, sampling: bool = False,
                 rework: bool = False) -> dict[str, float]:
    """toolgroup별 필요 부하 / 보유 용량.

    투입률과 평균 서비스 시간만으로 계산한다. 실측 가동률이 아니라 "주어진 투입을
    전부 소화하려면 얼마나 필요한가"다. 1을 넘으면 그 toolgroup에서는 정상상태가
    성립하지 않는다.

    `batching=False`가 Phase 02~03의 값이다. `True`면 배치 step의 점유를
    배치당 한 번으로 계산한다(Phase 04). 기본값을 바꾸지 않는 이유는 C02.16이
    배치 이전 상태를 기준으로 판정하기 때문이다 — phase가 늘었다고 이전
    phase의 기댓값이 흔들리면 누적 검증의 의미가 없다.

    `sampling`/`rework`는 Phase 09다. step마다 "평균 몇 번 수행하는가"를
    `expected_visits`로 구해 곱한다. 09 4.1절에 따르면 sampling을 반영하면
    1.0을 넘는 toolgroup이 **하나도 없어야** 하고 최댓값이 약 0.88이다 —
    이 모델이 정상상태에 도달할 수 있게 되는 지점이다.
    """
    rate: dict[str, float] = defaultdict(float)     # product -> lots/min
    for s in data.release_streams:
        rate[s.product] += s.lots_per_release / s.release_interval

    need: dict[str, float] = defaultdict(float)
    for product, route in data.routes.items():
        r = rate.get(product, 0.0)
        if r <= 0.0:
            continue
        wafers = next((s.wafers_per_lot for s in data.release_streams
                       if s.product == product), 25)
        visits = expected_visits(route, sampling=sampling, rework=rework)
        for i, step in enumerate(route.steps):
            spec = data.toolgroups[step.toolgroup]
            if spec.is_delay:
                continue
            share = mean_service_time(step, spec, wafers, cascading=cascading)
            if batching and step.batch_min is not None:
                # 배치 하나가 장비를 한 번 점유하고 lot 여러 개를 처리한다.
                # lot 하나가 요구하는 몫은 그만큼 나뉜다 (R5, R6).
                share /= lots_per_batch(step, wafers)
            need[step.toolgroup] += r * share * visits[i]

    return {g: need[g] / data.toolgroups[g].num_tools for g in sorted(need)}
