"""파싱 결과의 불변 데이터 모델.

필드 이름은 이후 12개 phase가 의존하는 인터페이스다 (01_data_contract.md 4절).
임의로 바꾸지 않는다.

모든 시간은 분(minute) 단위 float다. 단위 변환은 파서에서 끝난다.
counter 기반 값(wafer 수)은 변환하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Step:
    """route 한 행. 행 순서가 공정 순서이고, step_id는 식별자일 뿐이다."""

    step_index: int                     # 0-based 행 순서 == 공정 순서
    step_id: int                        # STEP 컬럼. 식별자
    description: str
    area: str
    toolgroup: str
    processing_unit: str                # "Wafer" | "Lot" | "Batch" (시트 표기 그대로)
    pt_dist: str
    pt_mean: float                      # 분
    pt_offset: float                    # 분. 빈 셀이면 0.0
    cascade_interval: float | None      # 분
    batch_min: int | None               # wafer 수
    batch_max: int | None               # wafer 수
    setup_name: str | None
    setup_when: str | None              # "need"
    setup_dist: str | None              # "constant" 또는 None
    setup_time: float | None            # 분. setup_dist == "constant"일 때만 유효
    setup_offset: float | None          # 분
    ltl_target_index: int | None        # 해소된 행 index (식별자 아님)
    rework_prob: float | None           # 퍼센트 0-100
    rework_unit: str | None             # 소문자로 정규화
    rework_target_index: int | None     # 해소된 행 index
    sampling_prob: float | None         # 퍼센트 0-100
    cqt_target_index: int | None        # 해소된 행 index
    cqt_threshold: float | None         # 분

    @property
    def is_delay(self) -> bool:
        """Delay step 여부는 toolgroup의 location으로 결정된다.

        Step 자체는 location을 모르므로 ModelData.is_delay_step()을 쓴다.
        이 property는 편의상 두지 않는다 — 잘못된 판단을 유도하기 때문.
        """
        raise NotImplementedError("ModelData.is_delay_step(step)을 사용할 것")


@dataclass(frozen=True, slots=True)
class Route:
    product: str                        # "Product_1"
    steps: tuple[Step, ...]
    step_id_to_index: Mapping[int, int]
    # index i에서 route 끝까지의 raw 처리시간 합 (분).
    # Phase 07의 critical ratio 분모이자 Phase 02의 X-factor 기준시간.
    # **바꾸면 디스패칭이 달라진다.** 07 R5가 정의를 고정한다.
    suffix_raw_pt: tuple[float, ...]
    # 논문(Kopp 2020) 규약의 RPT (분). **보고 전용이고 모델은 안 읽는다.**
    # 논문 III-E-1절이 RPT를 "빈 fab을 지나는 lot 하나의 측정 CT"로 정의하므로
    # 위 `suffix_raw_pt`와 다르다 — 이송을 포함하고 표본검사를 반영한다.
    # 기본값이 0.0인 이유는 fixture가 이 값을 안 쓰기 때문이다.
    rpt_paper: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolGroupSpec:
    name: str
    area: str
    num_tools: int
    cascading: bool
    batching: bool
    batch_criterion: str | None
    batching_unit: str | None
    load_time: float                    # 분
    unload_time: float                  # 분
    location: str                       # "Fab" | "Delay"
    dispatching: str
    rank1: str
    rank2: str
    rank3: str
    wake_up_rank: str | None

    @property
    def is_delay(self) -> bool:
        return self.location == "Delay"


@dataclass(frozen=True, slots=True)
class BreakdownSpec:
    area: str
    event_name: str
    down_type: str
    ttf_dist: str
    mttf: float                         # 분
    ttr_dist: str
    mttr: float                         # 분
    foa_dist: str
    foa: float                          # 분


@dataclass(frozen=True, slots=True)
class PMStreamSpec:
    event_name: str                     # "DE_BE_11_WK"
    toolgroup: str
    counter_based: bool
    # time 기반이면 분, counter 기반이면 wafer 수
    mtbpm: float
    ttr_dist: str
    ttr_mean: float                     # 분
    ttr_offset: float                   # 분
    foa_dist: str
    foa: float                          # time 기반이면 분, counter 기반이면 wafer 수


@dataclass(frozen=True, slots=True)
class SetupSpec:
    group: str | None                   # "Implant_Gas" 또는 None
    current_setup: str | None           # None이면 sequence-independent
    new_setup: str
    setup_time: float                   # 분
    min_run: int | None


@dataclass(frozen=True, slots=True)
class ReleaseStream:
    product: str
    route_name: str
    lot_type: str                       # "Regular_Lot_1"
    priority: int
    is_superhot: bool
    wafers_per_lot: int
    release_dist: str
    release_interval: float             # 분
    lots_per_release: int
    due_date_offset: float              # 분 (DUE - START)


@dataclass(frozen=True, slots=True)
class ExplicitLot:
    """explicit 모드에서 투입할 lot 하나. 시트 한 행이다 (Phase 12 R1).

    `ReleaseStream`과 다른 점은 **반복이 없다**는 것이다. AutoSched 문서:
    "lots can be released individually, i.e. without the repeat functionality."
    그래서 `due_date`가 offset이 아니라 **절대 시각**이다 — 문서가 offset
    방식을 "for each lot released during a **repeat cycle**"로 한정하고 있다.
    이것이 R7의 근거다.

    167,129개가 만들어진다. `Lot`이 아니라 이 작은 값 객체이고, `Lot`은
    투입 시각에 하나씩 만든다 (R3).
    """

    lot_id: str                         # "Regular_Lot_9_16280"
    product: str                        # "Product_9"
    priority: int
    is_superhot: bool
    wafers_per_lot: int
    release_time: float                 # 분, t=0(SIM_START)부터
    due_date: float                     # 분, t=0부터. **절대값이다**


@dataclass(frozen=True, slots=True)
class InitialWipLot:
    """t=0에 이미 fab 안에 있는 lot. `WIP.txt` 한 행이다 (Phase 12 R5).

    AutoSched 문서가 이 파일을 **ORDER_FILE**로 분류한다: "A file WIP.txt
    represents an ORDER_FILE. It contains initial WIP arising from a simulation
    run at high utilization." 즉 결과가 아니라 **투입 명세**다.

    `current_step_id`는 step **식별자**(STEP 컬럼)이지 행 index가 아니다 —
    문서: "the column CURSTEP refers to the current step of the lot".
    `Route.step_id_to_index`로 풀어야 한다.

    `due_date`는 t=0보다 **앞일 수 있다** (데이터의 첫 행이 정확히 0이다).
    이미 납기가 지난 채로 시작하는 lot이 있다는 뜻이고, R6이 이들을 on-time
    통계에서 빼는 이유다.
    """

    lot_id: str                         # "Init_Lot_1_1"
    product: str                        # "Product_1" (파일은 part_1)
    priority: int
    is_superhot: bool
    wafers_per_lot: int
    due_date: float                     # 분, t=0부터. 음수/0 가능
    current_step_id: int                # STEP 값. 행 index 아님
    order: str                          # "O_Init_WIP" — 개체 구분의 근거 (R9)


@dataclass(frozen=True, slots=True)
class TransportSpec:
    from_location: str
    to_location: str
    dist: str
    mean: float                         # 분
    offset: float                       # 분


@dataclass(frozen=True, slots=True)
class ModelData:
    toolgroups: Mapping[str, ToolGroupSpec]
    routes: Mapping[str, Route]
    breakdowns: Mapping[str, BreakdownSpec]              # key: area
    pm_streams: Mapping[str, tuple[PMStreamSpec, ...]]   # key: toolgroup
    setups: tuple[SetupSpec, ...]
    release_streams: tuple[ReleaseStream, ...]
    transport: Mapping[tuple[str, str], TransportSpec]
    area_to_toolgroups: Mapping[str, tuple[str, ...]]
    setup_to_toolgroup: Mapping[str, str]                # 유도값

    # -- Phase 12 — explicit 투입과 초기 WIP ---------------------------------
    # **비어 있으면 parametric 모드다.** 기본이 비어 있다는 것이 R2("파라메트릭
    # 동작은 바뀌지 않는다")를 구조적으로 보장한다 — 이 둘이 비면 `start()`가
    # 타는 경로가 Phase 11과 한 글자도 다르지 않다.
    explicit_lots: tuple["ExplicitLot", ...] = ()
    initial_wip: tuple["InitialWipLot", ...] = ()

    # -- 유도 집합 (AGENTS.md 1절: 이름 목록을 소스에 박지 않는다) ------------

    def batching_toolgroups(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, s in self.toolgroups.items() if s.batching))

    def cascading_toolgroups(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, s in self.toolgroups.items() if s.cascading))

    def setup_avoidance_toolgroups(self) -> tuple[str, ...]:
        """DISPATCHING 값이 다수와 다른 toolgroup.

        데이터에서 유도한다. 다수 값을 기본으로 보고 나머지를 예외로 취급한다.
        """
        counts: dict[str, int] = {}
        for spec in self.toolgroups.values():
            counts[spec.dispatching] = counts.get(spec.dispatching, 0) + 1
        majority = max(counts, key=lambda k: counts[k])
        return tuple(sorted(n for n, s in self.toolgroups.items()
                            if s.dispatching != majority))

    def wake_up_toolgroups(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, s in self.toolgroups.items() if s.wake_up_rank))

    def setup_bearing_toolgroups(self) -> tuple[str, ...]:
        """route에서 SETUP을 쓰는 step이 하나라도 있는 toolgroup."""
        found: set[str] = set()
        for route in self.routes.values():
            for step in route.steps:
                if step.setup_name is not None:
                    found.add(step.toolgroup)
        return tuple(sorted(found))

    def delay_toolgroups(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, s in self.toolgroups.items() if s.is_delay))

    # -- 조회 ---------------------------------------------------------------

    def is_delay_step(self, step: Step) -> bool:
        return self.toolgroups[step.toolgroup].is_delay

    def total_tools(self) -> int:
        return sum(s.num_tools for s in self.toolgroups.values())

    def total_steps(self) -> int:
        return sum(len(r.steps) for r in self.routes.values())

    def release_rate_wspw(self) -> float:
        """Lotrelease에서 유도한 주당 wafer 투입량 (DATA_FACTS.md 8절)."""
        from .config import MINUTES_PER_WEEK

        total = 0.0
        for s in self.release_streams:
            total += (s.wafers_per_lot * s.lots_per_release
                      / s.release_interval * MINUTES_PER_WEEK)
        return total
