"""이동 시간과 처리 시간 산식.

전부 데이터에서 읽는다. 5나 10 같은 숫자를 소스에 적으면 안 된다
(02_core_flow.md R5, AGENTS.md 1절).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from . import config

if TYPE_CHECKING:  # pragma: no cover
    from .model_data import Step, ToolGroupSpec, TransportSpec


def sample_uniform(mean: float, offset: float, rng) -> float:
    """U(mean-offset, mean+offset). offset이 0이면 mean 그대로."""
    if offset <= 0.0:
        return mean
    return rng.uniform(mean - offset, mean + offset)


def get_transport_time(from_location: str, to_location: str,
                       transport: "Mapping[tuple[str, str], TransportSpec]",
                       rng) -> float:
    """두 위치 사이 이동 시간.

    표에 없는 조합은 0이다. Delay 위치가 여기에 해당한다 (R5).
    """
    spec = transport.get((from_location, to_location))
    if spec is None:
        return 0.0
    return sample_uniform(spec.mean, spec.offset, rng)


def calculate_processing_time(step: "Step", wafers_per_lot: int, rng) -> float:
    """장비 점유 시간. load/unload는 포함하지 않는다 (R6, R7).

    Wafer 단위: 표본 x wafer 수
    Lot  단위: 표본
    Batch 단위: 표본 (batch 크기와 무관)

    cascading (05_cascading.md):
      R4 lot cascading  — 점유는 표본 그대로. 바뀌는 것은 진입 간격뿐이다.
      R5 wafer cascading — 점유는 `표본(wafer 1장) + (n-1) x interval`.

    R5는 근사가 아니라 **닫힌 식**이다. wafer k가 `k x interval`에 들어가고
    각자 표본만큼 걸리므로, 마지막 wafer는 `(n-1) x interval`에 들어가
    `(n-1) x interval + 표본`에 나온다. wafer를 개별 엔티티로 만들어도 같은
    값이 나온다 (C05.3이 그것을 확인한다).
    """
    sample = sample_uniform(step.pt_mean, step.pt_offset, rng)
    unit = step.processing_unit.lower()
    if unit == "wafer":
        if step.cascade_interval is not None:                     # R5
            return sample + (wafers_per_lot - 1) * step.cascade_interval
        return sample * wafers_per_lot                            # R7
    if unit in ("lot", "batch"):
        return sample                                             # R4 / batch
    raise ValueError(f"알 수 없는 processing unit: {step.processing_unit!r}")


def entry_interval(step: "Step", wafers_per_lot: int,
                   gate_rule: str | None = None) -> float:
    """이 step에서 **다음 job이 들어올 수 있기까지**의 시간 (05 R2).

    cascading step이 아니면 0이다 — 자리만 나면 바로 들어간다.

    R4 lot cascading  : interval
    R6 wafer cascading: n x interval  (미결정 규칙, config.WAFER_CASCADE_GATE)

    R8: setup 시간은 여기에 더하지 않는다 (`SEQ_ADDS_SETUP_DELAYS = N`).
    """
    i = step.cascade_interval
    if i is None:
        return 0.0
    if step.processing_unit.lower() != "wafer":
        return i                                                  # R4
    rule = gate_rule if gate_rule is not None else config.WAFER_CASCADE_GATE
    if rule == "n_interval":
        return wafers_per_lot * i                                 # R6
    if rule == "one_interval":
        return i
    raise ValueError(f"알 수 없는 WAFER_CASCADE_GATE: {rule!r}")


def mean_service_time(step: "Step", spec: "ToolGroupSpec",
                      wafers_per_lot: int, *, cascading: bool = False) -> float:
    """표본이 아닌 평균으로 본 장비 점유 시간. 부하 계산용.

    delay step은 장비를 쓰지 않으므로 0이다.

    `cascading=True`면 cascading의 유효 서비스 시간을 쓴다. 장비 하나가
    job을 내보내는 간격은 두 제약의 **큰 쪽**이다:
      * 진입 간격  — `interval` 마다 한 job (게이트)
      * 동시 2개  — 점유 / 2 (capacity)
    즉 `max(entry_interval, 점유/2)`. 기본값을 바꾸지 않는 이유는 앞 phase의
    기댓값(C02.16의 29개)이 흔들리면 누적 검증이 무의미해지기 때문이다.
    """
    if spec.is_delay:
        return 0.0
    unit = step.processing_unit.lower()
    if cascading and step.cascade_interval is not None:
        if unit == "wafer":
            own = step.pt_mean + (wafers_per_lot - 1) * step.cascade_interval
        else:
            own = step.pt_mean
        gate = entry_interval(step, wafers_per_lot)
        return max(gate, own / 2.0) + spec.load_time + spec.unload_time
    own = step.pt_mean * wafers_per_lot if unit == "wafer" else step.pt_mean
    return own + spec.load_time + spec.unload_time
