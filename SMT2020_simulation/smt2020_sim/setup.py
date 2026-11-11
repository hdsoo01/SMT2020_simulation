"""Setup 메커니즘 — 06_setup.md.

장비는 한 번에 하나의 setup 상태를 갖는다. lot의 step이 다른 상태를 요구하면
lot을 싣기 전에 장비를 바꿔야 하고, 거기에 시간이 든다.

**메커니즘은 세 가지고, 시간이 어디서 오는지가 다르다** (2.2절).
이름으로 판단하면 안 된다 — `SU128_3`과 `SU015_3`은 비슷해 보이지만 서로 다른
메커니즘이다. 데이터에서 유도한다:

  SETUP DISTRIBUTION == "constant"        -> route-defined,      route의 SETUP TIME
  Setups 시트에 CURRENT SETUP과 함께 있음   -> sequence-dependent, (현재, 새것) 조회
  Setups 시트의 SETUP GROUP NAME이 있음     -> Implant_Gas,        목표 setup만으로 결정

유도 결과 (확인함): 182 / 50 / 169 = 401 step, toolgroup 9개.
그 9개는 `TOOL WAKE UP Ranking`이 비어 있지 않은 집합과 정확히 같다.

**이 모듈의 함수는 전부 순수하다** (R12). 상태를 바꾸지 않고 난수를 쓰지
않는다. Phase 07이 디스패치 결정마다 후보 lot과 후보 장비 수만큼 이 함수를
부르므로, 부수효과나 후보 수에 비례하는 난수 소비가 있으면 결과가 달라진다.

난수를 쓰지 않아도 되는 이유: 401개 setup step의 `OFFSET`이 전부 0이고
`Setups` 시트의 시간도 고정값이다. setup 시간은 결정적이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:  # pragma: no cover
    from .model_data import ModelData, Step

ROUTE_DEFINED = "route-defined"
SEQUENCE_DEPENDENT = "sequence-dependent"
IMPLANT_GAS = "implant-gas"


@dataclass(frozen=True, slots=True)
class SetupModel:
    """유도된 setup 규칙. 파싱 결과에서 한 번 만들고 그대로 읽기만 한다."""

    mechanism: Mapping[str, str]                    # setup 이름 -> 메커니즘
    seq_time: Mapping[tuple[str, str], float]       # (현재, 새것) -> 분
    target_time: Mapping[str, float]                # 새것 -> 분 (Implant_Gas)
    min_run: Mapping[str, int]                      # 새것 -> MINRUN (Phase 07)
    toolgroup: Mapping[str, str]                    # setup 이름 -> toolgroup

    def steps_by_mechanism(self, data: "ModelData") -> dict[str, int]:
        """메커니즘별 step 수. 체크가 182/50/169를 확인하는 데 쓴다."""
        out: dict[str, int] = {}
        for route in data.routes.values():
            for step in route.steps:
                if step.setup_name is None:
                    continue
                m = self.mechanism_of(step)
                out[m] = out.get(m, 0) + 1
        return out

    def mechanism_of(self, step: "Step") -> str:
        """이 step의 메커니즘. **step의 SETUP DISTRIBUTION이 먼저다.**

        route-defined 판정은 setup 시간의 존재가 아니라 분포 컬럼으로 한다
        (5절 실패 모드). 이름으로 판정하지 않는다.
        """
        if (step.setup_dist or "").lower() == "constant":
            return ROUTE_DEFINED
        return self.mechanism.get(step.setup_name, SEQUENCE_DEPENDENT)


def build(data: "ModelData") -> SetupModel:
    """파싱 결과에서 setup 모델을 유도한다. 이름 목록을 소스에 적지 않는다."""
    mechanism: dict[str, str] = {}
    seq_time: dict[tuple[str, str], float] = {}
    target_time: dict[str, float] = {}
    min_run: dict[str, int] = {}

    for spec in data.setups:
        if spec.current_setup is not None:
            # 방향이 있는 전이. 두 방향의 시간이 다르다 (R5).
            mechanism[spec.new_setup] = SEQUENCE_DEPENDENT
            mechanism.setdefault(spec.current_setup, SEQUENCE_DEPENDENT)
            seq_time[(spec.current_setup, spec.new_setup)] = spec.setup_time
        elif spec.group:
            # 목표 setup만으로 시간이 정해진다 (R6).
            mechanism[spec.new_setup] = IMPLANT_GAS
            target_time[spec.new_setup] = spec.setup_time
            if spec.min_run is not None:
                min_run[spec.new_setup] = spec.min_run

    # route에 나오지만 시트에 없는 이름은 route-defined다.
    for route in data.routes.values():
        for step in route.steps:
            if step.setup_name is None:
                continue
            if (step.setup_dist or "").lower() == "constant":
                mechanism.setdefault(step.setup_name, ROUTE_DEFINED)

    return SetupModel(
        mechanism=mechanism,
        seq_time=seq_time,
        target_time=target_time,
        min_run=min_run,
        toolgroup=dict(data.setup_to_toolgroup),
    )


def required_setup(step: "Step") -> str | None:
    """이 step이 요구하는 setup 이름. 없으면 None (R1).

    순수하다. Phase 07의 순위 계산이 후보마다 부른다.
    """
    return None if step is None else step.setup_name


def setup_duration(model: SetupModel, step: "Step",
                   current: str | None) -> tuple[float, bool]:
    """(소요 시간, 미정의 전이 여부). **순수하다** (R12).

    R2 같은 setup이면 0이고 건너뛴다.
    R3 아직 아무 setup도 없는 장비는 **공짜로** 적용한다. 1,313대가 실행
       시작마다 setup을 한 번씩 물면 setup 비율이 부풀어 오른다
       (DECISIONS B-6). 이 함수는 0을 돌려주고, 상태 기록은 호출부가 한다.
    R4 route-defined: 그 step 자신의 setup 시간.
    R5 sequence-dependent: (현재, 새것)으로 조회. 방향마다 다르다.
    R6 Implant_Gas: 목표 setup만으로 결정.
    R7 시트에 없는 전이는 0이고 **센다.** 이 데이터셋에서는 0건이어야 한다.
    """
    name = required_setup(step)
    if name is None:
        return 0.0, False                       # R1
    if current == name:
        return 0.0, False                       # R2
    if current is None:
        return 0.0, False                       # R3

    mech = model.mechanism_of(step)
    if mech == ROUTE_DEFINED:
        return float(step.setup_time or 0.0), False
    if mech == IMPLANT_GAS:
        t = model.target_time.get(name)
        return (0.0, True) if t is None else (t, False)

    t = model.seq_time.get((current, name))
    return (0.0, True) if t is None else (t, False)   # R7
