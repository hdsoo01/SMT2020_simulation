"""Lot — fab을 통과하는 개체.

cycle time 버킷(02_core_flow.md 4.2절)이 여기 산다. 모든 경과 구간이 정확히
하나의 버킷에 들어가야 하므로, "현재 버킷"을 하나만 두고 전환할 때 직전 구간을
닫는 방식으로 구현한다. 배타성과 합계 항등식이 구조적으로 보장된다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .model_data import Route, Step

# 02_core_flow.md 4.2절의 버킷. 뒤쪽 것들은 담당 phase가 오기 전까지 0으로 남는다.
BUCKETS: tuple[str, ...] = (
    "process",
    "setup",           # Phase 06
    "load_unload",
    "transport",
    "delay",
    "queue",
    "wait_down",       # Phase 03
    "wait_batch",      # Phase 04
    "wait_gate",       # Phase 05
    "wait_dedicated",  # Phase 08
    "wait_reserved",   # Phase 11
)

# 두 버킷이 동시에 성립할 수 있을 때의 우선순위 (02_core_flow.md 4.2절).
# 앞쪽이 이긴다. 지금은 겹치는 상황이 없지만, 나중에 의미가 바뀌지 않도록
# 지금 고정해 둔다.
BUCKET_PRIORITY: tuple[str, ...] = (
    "wait_down",       # 정지가 가장 강하다. 대기 사유가 무엇이든 장비가 멈춰 있으면 이것
    "wait_dedicated",  # 특정 장비를 기다리는 중
    "wait_reserved",
    "wait_gate",
    "wait_batch",
    "queue",
    "setup",
    "load_unload",
    "process",
    "transport",
    "delay",
)


class Lot:
    """route를 따라 이동하는 lot 하나.

    Phase 08/10/11이 쓸 상태를 미리 선언해 두되 여기서는 건드리지 않는다
    (02_core_flow.md 6절).
    """

    __slots__ = (
        "lot_id", "product", "route", "priority", "lot_type", "wafers_per_lot",
        "release_time", "due_date", "is_superhot", "current_step_index",
        "due_base_min", "due_z",
        "start_step_index", "fab_out_time", "is_initial_wip",
        "ltl_assignments", "ltl_recent", "ltl_forced", "active_cqts",
        "reservations",
        "buckets", "_bucket", "_bucket_since", "steps_executed", "rework_count",
        "steps_skipped", "rework_span", "last_location",
        "batch_done", "batch_since", "queue_since", "dispatch_done",
        "exit_after", "exit_event",
        "kpi",
    )

    def __init__(self, *, lot_id: str, product: str, route: "Route",
                 priority: int, wafers_per_lot: int, release_time: float,
                 due_date: float, is_superhot: bool,
                 start_step_index: int = 0, is_initial_wip: bool = False,
                 due_base_min: float | None = None, due_z: float | None = None):
        self.lot_id = lot_id
        self.kpi = None
        self.product = product
        self.route = route
        self.priority = priority
        self.wafers_per_lot = wafers_per_lot
        self.release_time = release_time
        self.due_date = due_date
        self.due_base_min = due_base_min
        self.due_z = due_z
        self.is_superhot = is_superhot
        self.lot_type = ("superhot" if is_superhot
                         else "hot" if priority >= 20
                         else "regular")

        self.current_step_index = start_step_index
        # 출발 위치. 08 4.2절 — 초기 WIP(Phase 12)은 route 중간에서 시작하므로
        # 그 앞의 LTL 체인 머리를 지나온 적이 없다. 누락을 결함과 구분하려면
        # 현재 위치가 아니라 **출발 위치**를 알아야 한다.
        self.start_step_index = start_step_index
        self.fab_out_time: float | None = None
        self.is_initial_wip = is_initial_wip      # Phase 12

        # Phase 08 — lot-to-lens 지정. {체인 id: 장비}. 체인 끝에서 풀린다 (R4).
        self.ltl_assignments: dict = {}
        # **직전 방문의 장비 기억.** 꼬리에서 풀어도 남는다.
        #
        # 08 4.1절: rework가 체인 안쪽에 떨어지면 "the lot is still on the same
        # pass"이므로 같은 장비를 써야 한다. 그런데 꼬리로 되돌아오는 rework가
        # 이 데이터에 7건 있고(C08.11), 그때는 꼬리에서 이미 풀린 뒤다.
        # 지정(R4대로 풀린다)과 기억(다음 통과의 머리에서 덮어쓴다)을 나눠,
        # 두 규칙을 동시에 지킨다.
        self.ltl_recent: dict = {}
        # 이번 step에서 반드시 써야 하는 장비 (없으면 None). 대기열에 들어갈
        # 때 한 번 계산해 두면 `can_accept`가 O(1)로 판정한다.
        self.ltl_forced = None
        # Phase 10 — 열려 있는 CQT window. `(source_i, target_i, limit, opened_at)`.
        # **여러 개가 동시에 열릴 수 있다** (10 R3). 하나를 닫아도 나머지는
        # 그대로다 — 그래서 dict가 아니라 목록이다. 같은 source를 다시 지나면
        # (rework) 같은 제약의 window가 **하나 더** 열린다 (3.2절).
        self.active_cqts: list[tuple[int, int, float, float]] = []
        # Phase 11 — 이 lot이 잡아 둔 장비. {toolgroup 이름: (장비, 시작 시각)}.
        # **toolgroup당 하나**가 R12다. dict로 두면 그 규칙이 구조적으로
        # 지켜진다 — 같은 toolgroup에 두 번 걸면 덮어쓰기지 중복이 아니다.
        self.reservations: dict = {}

        self.buckets: dict[str, float] = dict.fromkeys(BUCKETS, 0.0)
        self._bucket: str | None = None
        self._bucket_since: float = release_time
        self.steps_executed = 0

        # -- Phase 09 -------------------------------------------------------
        self.rework_count = 0      # 되돌아간 횟수 (R6의 상한 대상)
        self.steps_skipped = 0     # sampling으로 건너뛴 step 수 (R2)
        # rework 한 번이 다시 걸어야 하는 index 수의 합: Σ(출발 - 목적지 + 1).
        # step 계산 항등식의 우변이다 (09 7절 "Step accounting"):
        #     수행 + 건너뜀 == route 길이 + rework_span
        self.rework_span = 0
        # **마지막으로 실제로 들렀던 위치.** 이동 시간의 출발점이다 (R1/R2).
        # 건너뛴 step은 간 적이 없으므로 여기를 바꾸지 않는다 — index-1을
        # 출발점으로 쓰면 가지도 않은 곳에서 출발한 거리를 물게 된다.
        # 건너뛰기가 없으면 index-1과 항상 같아 Phase 02~08 동작과 동일하다.
        self.last_location: str | None = None

        # Phase 04 — 배치 대기 상태.
        # batch_done: 이 lot이 속한 배치가 끝나면 발화한다. 배치 서버가 만들고
        #   발화까지 책임진다. lot 자신이 올려야 할 신호를 기다리는 구조가 아니다
        #   (04_batch.md 5절의 교착 사례).
        # batch_since: 배치 그룹에 들어온 시각. 대기 시간을 각자 자기 도착
        #   기준으로 재기 위한 것이다 (R9).
        self.batch_done = None
        self.batch_since: float | None = None

        # queue_since: toolgroup 대기열에 들어온 시각. 그림 [7]의 "장비를
        #   기다린 시간"을 toolgroup별로 재는 데 쓴다. 버킷(`buckets`)과 다른
        #   것을 잰다 — 버킷은 **사유별**로 쪼개고 이쪽은 **장비별**로 합친다.
        self.queue_since: float | None = None

        # 배정 서버가 장비를 확보해 넘겨줄 때 발화한다. 값은 (tool, request).
        # 서버가 만들고 서버가 올린다.
        self.dispatch_done = None

        # Phase 05 — cascading 퇴장 순서 사슬 (R3).
        # exit_after: 이 장비에 나보다 먼저 들어간 job의 퇴장 이벤트.
        # exit_event: 내 퇴장 이벤트. 뒤 job이 이걸 기다린다.
        self.exit_after = None
        self.exit_event = None

    # -- route 이동 ---------------------------------------------------------

    def current_step(self) -> "Step | None":
        if self.current_step_index < len(self.route.steps):
            return self.route.steps[self.current_step_index]
        return None

    def advance_step(self) -> None:
        self.current_step_index += 1

    def jump_to_step(self, index: int) -> None:
        """rework로 되돌아간다 (Phase 09).

        Phase 08/11이 여기서 만료된 상태를 정리하게 된다. 지금은 위치만 옮긴다.
        """
        self.current_step_index = index

    def is_complete(self) -> bool:
        return self.current_step_index >= len(self.route.steps)

    # -- cycle time 버킷 ----------------------------------------------------

    def enter(self, bucket: str, now: float) -> None:
        """현재 구간을 닫고 새 버킷을 연다.

        모든 경과 시간이 정확히 하나의 버킷에 들어가는 이유가 이것이다.
        """
        if bucket not in self.buckets:
            raise KeyError(f"알 수 없는 버킷: {bucket}")
        if self._bucket is not None:
            if self.kpi is not None:
                self.kpi.record_segment(self, self._bucket, self._bucket_since, now)
            self.buckets[self._bucket] += now - self._bucket_since
        self._bucket = bucket
        self._bucket_since = now

    def close_buckets(self, now: float) -> None:
        """마지막 구간을 닫는다. fab-out 시점에 한 번 호출한다."""
        if self._bucket is not None:
            if self.kpi is not None:
                self.kpi.record_segment(self, self._bucket, self._bucket_since, now)
            self.buckets[self._bucket] += now - self._bucket_since
            self._bucket = None
        self._bucket_since = now

    @property
    def cycle_time(self) -> float:
        if self.fab_out_time is None:
            raise ValueError(f"{self.lot_id}: 아직 fab-out 하지 않았다")
        return self.fab_out_time - self.release_time

    @property
    def bucket_total(self) -> float:
        return sum(self.buckets.values())

    @property
    def remaining_raw_pt(self) -> float:
        """현재 위치에서 route 끝까지의 raw 처리시간 (분). critical ratio의 분모.

        Phase 01이 route 단위로 미리 계산해 둔 suffix 배열을 읽기만 한다.
        여기서 route를 걸어 다니면 디스패치 결정마다 O(대기 수 x step 수)가
        된다 (07_dispatching.md 3.1절).

        **X-factor의 분모와 같은 정의다.** 07 3.1절이 "하나로 쓰거나 둘로
        나누고 이유를 대라"고 하는데, 같은 배열을 쓰므로 하나다.
        rework로 위치가 뒤로 가도 같은 배열이 그대로 답한다.
        """
        i = self.current_step_index
        suffix = self.route.suffix_raw_pt
        return suffix[i] if 0 <= i < len(suffix) else 0.0

    def critical_ratio(self, now: float) -> float:
        """(납기까지 남은 시간) / (남은 작업). 작을수록 급하다 (07 R4).

        음수면 이미 늦었다. 표본이 아니라 평균으로 계산하므로 route 위치가
        같으면 항상 같은 값이다 — 같은 seed에서 결과가 흔들리지 않는다.

        남은 작업이 0이면(마지막 step을 지난 경우) 비교 불가이므로 가장 급한
        쪽으로 보낸다.
        """
        remaining = self.remaining_raw_pt
        if remaining <= 0.0:
            return float("-inf")
        return (self.due_date - now) / remaining

    @property
    def theoretical_cycle_time(self) -> float:
        """이 lot이 시작 위치에서부터 겪는 raw 처리시간 (X-factor의 분모).

        Phase 01이 route 단위로 미리 계산해 둔 suffix 배열을 읽기만 한다.
        """
        return self.route.suffix_raw_pt[0]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Lot {self.lot_id} {self.product} step={self.current_step_index}>"
