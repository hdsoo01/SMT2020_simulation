"""Tool / ToolGroup.

개별 tool을 처음부터 따로 둔다 (00_overview.md 구조적 결정 1).
downtime, setup 상태, dedication, reservation 전부 tool 단위 상태를 요구하므로
나중에 바꾸면 Phase 02의 검증이 무효가 된다.

자원 기본형은 **PriorityResource**다. PreemptiveResource가 아니다:
lot 우선순위는 다음에 누구를 고를지만 정하고, 진행 중인 작업을 밀어내지 않는다
(02_core_flow.md R10). 진행 중 작업을 중단시키는 것은 Phase 03의 정지 사건뿐이고,
그건 자원 선점이 아니라 프로세스 interrupt로 구현한다.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import simpy

if TYPE_CHECKING:  # pragma: no cover
    from .model_data import ToolGroupSpec


class Tool:
    """개별 장비 1대."""

    __slots__ = (
        "env", "tool_id", "group", "index", "resource",
        "kpi",
        "_down_count", "up_event", "current_processes",
        "current_setup", "run_count", "reserved_for",
        "pm_counters", "pm_thresholds", "pm_events",
        "busy_time", "proc_time", "loadunload_time", "down_time", "pm_time",
        "setup_time", "wafers_processed", "_busy_since",
        "work_time", "_work_jobs", "_work_since",
        "unavailable_time", "_down_since", "down_events", "pm_events_count",
        "max_concurrent_down",
        "setting_up",
        "gate_closed", "last_exit_event", "max_concurrent_jobs",
        "two_job_time", "_two_since",
        "last_entry", "last_gate", "gate_violations", "entries", "gap_slack",
    )

    def __init__(self, env: simpy.Environment, tool_id: str,
                 group: "ToolGroup", index: int, capacity: int = 1):
        self.env = env
        self.kpi = None
        self.tool_id = tool_id
        self.group = group
        self.index = index                    # 0-based. PM FOA 분산에 쓴다
        self.resource = simpy.PriorityResource(env, capacity=capacity)

        # -- Phase 03이 채울 자리 -------------------------------------------
        # 정수 카운트다. bool이 아니다: PM과 고장이 겹칠 수 있고 각자 독립적으로
        # 풀려야 한다 (03_tool_availability.md R8).
        self._down_count = 0
        self.up_event: simpy.Event | None = None
        # 한 장비 위에 여러 작업이 있을 수 있다 (Phase 05의 capacity 2).
        # 단수라고 못박지 않는다 (03 상호작용 표).
        self.current_processes: set = set()

        # -- Phase 06 setup ---------------------------------------------------
        # 장비가 지금 들고 있는 setup. None이면 아직 한 번도 안 잡았다.
        self.current_setup: str | None = None
        # setup 진행 중. 그동안은 **아무도** 들어오지 못한다 — 재구성 중인
        # 장비에 두 번째 lot이 들어오면 어느 상태로 처리되는지 알 수 없다.
        self.setting_up = False
        # 지금 setup으로 돌린 lot 수. Phase 07의 MINRUN이 읽는다. 이 phase는
        # 유지만 하고 판단에 쓰지 않는다 (06 6절).
        self.run_count = 0
        self.reserved_for: str | None = None    # Phase 11

        # Phase 03: PM 스트림마다 자기 카운터를 갖는다
        self.pm_counters: dict[str, float] = {}
        self.pm_thresholds: dict[str, float] = {}
        self.pm_events: dict[str, simpy.Event] = {}

        # -- 상태 시간 --------------------------------------------------------
        # busy = proc + loadunload + setup.
        # down_time / pm_time 은 **사건별 총합**이다. 두 정지가 겹치면 합이
        # 실제 사용 불가 시간보다 커진다. 레퍼런스가 DOWN%/PM%를 따로 보고하므로
        # 사건별 총합을 유지하되, 배타적 상태 계산에는 unavailable_time(합집합)을
        # 쓴다 (03_tool_availability.md 7.3절).
        # **작업 시간의 합집합.** 아래 세 값(proc/loadunload/setup)은 job마다
        # 더한 총합이라, cascading 장비(STNCAP=2)에서 두 job이 겹친 시간을 두
        # 번 센다. 레퍼런스는 장비를 한 번에 한 상태로만 보므로 UTIL%가 100을
        # 넘을 수 없는데 우리는 넘었다 (Planar 107.7 %). 그래서 "적어도 하나가
        # 일하고 있던 시간"을 따로 잰다.
        #
        # 점유(`busy_time`)와 **다르다**: lot이 올라간 채 장비가 고장 난 시간은
        # 점유에는 들어가고 작업에는 안 들어간다. 레퍼런스도 그때를 DOWN으로
        # 잡으므로 UTIL의 재료는 이쪽이다.
        self.work_time = 0.0
        self._work_jobs = 0             # 지금 이 장비에서 일하고 있는 작업 수
        self._work_since: float | None = None
        self.busy_time = 0.0
        self.proc_time = 0.0
        self.loadunload_time = 0.0
        self.down_time = 0.0            # 고장 사건 총합
        self.pm_time = 0.0              # PM 사건 총합
        self.setup_time = 0.0
        self.wafers_processed = 0
        self._busy_since: float | None = None

        # -- Phase 05 cascading -----------------------------------------------
        # 진입 게이트. 앞 job이 들어간 뒤 entry interval 동안 닫힌다 (R2).
        # **남은 시간은 게이트 프로세스가 들고 있다.** 절대 시각으로 두면
        # 정지 중에도 계속 흘러 유효 용량이 올라간다 (R9, 5절 실패 모드).
        self.gate_closed = False
        # 퇴장 순서 사슬. 새 job은 직전 job의 이 이벤트를 기다렸다가 나간다.
        # 먼저 들어간 job보다 먼저 나갈 수 없다 (R3).
        self.last_exit_event: simpy.Event | None = None

        self.max_concurrent_jobs = 0    # capacity 2가 실제로 쓰였는지
        self.two_job_time = 0.0         # 두 job이 동시에 있던 시간
        self._two_since: float | None = None

        # 진입 간격이 전체 모델에서 실제로 지켜지는지 (R2/R6).
        # fixture에서만 확인하면 전체 모델에서 깨져도 모른다. 정지 중에는
        # 게이트가 멈추므로 **벽시계 간격은 항상 게이트 이상**이어야 한다.
        self.last_entry: float | None = None
        self.last_gate = 0.0
        self.gate_violations = 0
        self.entries = 0
        self.gap_slack = 0.0            # (실제 간격 - 게이트)의 합

        self.unavailable_time = 0.0     # 실제로 쓸 수 없던 시간의 합집합
        self._down_since: float | None = None
        self.down_events = 0            # 고장 발생 횟수
        self.pm_events_count = 0        # PM 발생 횟수
        self.max_concurrent_down = 0    # 동시 정지 최대 중첩 수

    # -- 가용성 -------------------------------------------------------------

    @property
    def is_down(self) -> bool:
        return self._down_count > 0

    def set_down(self) -> None:
        """정지 사건 하나가 시작됐다.

        정수 카운트다. PM과 고장이 겹칠 수 있고 각자 독립적으로 풀려야 한다 (R8).
        복구 알림 이벤트는 **정지 에피소드당 하나**다. 사건마다 새로 만들면
        한쪽이 다른 쪽을 덮어써서 대기 중인 lot이 영영 깨어나지 않는다.
        """
        if self._down_count == 0:
            self.up_event = self.env.event()
            self._down_since = self.env.now
            self.group.mark_down(self)        # 후보 목록에서 제외 (R14)
        self._down_count += 1
        if self._down_count > self.max_concurrent_down:
            self.max_concurrent_down = self._down_count

    def set_up(self) -> None:
        """정지 사건 하나가 끝났다. 마지막 하나가 끝나야 장비가 살아난다."""
        self._down_count -= 1
        if self._down_count > 0:
            return
        if self._down_since is not None:
            self.unavailable_time += self.env.now - self._down_since
            self._down_since = None
        ev, self.up_event = self.up_event, None
        self.group.mark_released(self)
        if ev is not None and not ev.triggered:
            ev.succeed()
        self.group.signal_free()              # 대기 중이던 lot을 깨운다

    @property
    def is_idle(self) -> bool:
        return len(self.resource.users) == 0

    @property
    def is_loaded(self) -> bool:
        """지금 job이 하나라도 올라가 있는가.

        `is_idle`의 반대이지만 **쓰는 곳이 다르다.** 이 값이 참일 때만
        `setup_conflict`가 lot에 의존한다(아래 참고) — 즉 "이 장비가 받을 수
        있는 lot"이 lot마다 갈리는 조건이다. Phase 07의 후보 필터가 이걸
        읽는다. 비어 있는 장비에서는 무엇이든 받으므로 필터가 무의미하다.
        """
        return bool(self.resource.users)

    def can_accept(self, lot, step, *, ignore_gate: bool = False,
                   ignore_reserved: bool = False) -> bool:
        """이 lot을 지금 받을 수 있는가.

        "비어 있는가"가 아니라 "받을 수 있는가"를 묻는다. capacity 2에서
        "현재 사용자가 없는가"로 물으면 두 번째 슬롯이 조용히 죽는다
        (05 5절 마지막 실패 모드, 07 R7).

        Phase 08의 dedication, Phase 11의 reservation도 여기에 붙는다.
        판정은 부수효과가 없어야 한다 (07 R10).

        `ignore_gate=True`는 **계측 전용**이다 (DECISIONS G-19). "게이트만
        아니었으면 받을 수 있었나"를 묻는 데 쓰고, 배정 판정에는 절대 쓰지
        않는다. 조건을 복제하지 않고 이 함수를 그대로 다시 부르는 이유는,
        복제하면 나중에 조건이 늘 때 조용히 어긋나기 때문이다.

        `ignore_reserved=True`도 같은 용도다 (G-23). `wait_reserved` 버킷이
        선언만 돼 있고 아무도 채우지 않아 0.0이었다 — G-19와 같은 부류다.
        **둘 다 배정 경로에서는 절대 켜지 않는다.**
        """
        if self.is_down:
            return False
        if len(self.resource.users) >= self.resource.capacity:
            return False
        if self.gate_closed and not ignore_gate:   # 05 R2 — 진입 간격
            return False
        if self.setup_conflict(step):         # 06 4.1절
            return False
        # 08 R2 — lot-to-lens 지정은 **강제**다. 선호가 아니므로 다른 장비로
        # 넘어가지 않는다. 명세: "exactly the same tool must be used".
        forced = getattr(lot, "ltl_forced", None)
        if forced is not None and forced is not self:
            return False
        if (self.reserved_for is not None and not ignore_reserved
                and self.reserved_for != lot.lot_id):
            return False
        return True

    def setup_conflict(self, step) -> bool:
        """지금 이 장비에 들어오면 setup 충돌이 나는가 (06 4.1절).

        setup 상태는 **장비 단위**다. cascading 장비는 lot 두 개를 동시에
        담는데, 둘이 서로 다른 setup을 요구하면 그 상태를 표현할 수 없다.
        Implant toolgroup 5개가 실제로 cascading이면서 setup 대상이므로
        가정이 아니라 실제 구성이다.

        택한 규칙: **장비가 비어 있지 않으면, 지금 들고 있는 setup을 그대로
        쓰는 lot만 받는다.** 다른 setup이 필요한 lot은 장비가 빌 때까지
        기다린다. 얼마나 자주 막히는지는 세어서 보고한다 — 무시할 수준이면
        선택이 무의미하고, 크면 DECISIONS로 올려야 한다.

        재구성 중(setting_up)에는 아무도 받지 않는다. 그 순간 장비는 이전
        상태도 새 상태도 아니다.

        **부수효과가 없다** (07 R10). 세는 일은 호출부가 한다.
        """
        if self.setting_up:
            return True
        if not self.resource.users:           # 비어 있으면 무엇이든 받는다
            return False
        need = getattr(step, "setup_name", None)
        if need is None:                      # R1 — setup이 필요 없는 step
            return False
        return need != self.current_setup

    # -- 동시 job 관측 (05 실험 3) -------------------------------------------

    def note_jobs(self) -> None:
        """현재 점유 수를 기록한다. 진입·퇴장 직후에 부른다.

        capacity 2가 실제로 쓰이는지 보려면 최댓값만으로는 부족하다. 2-job
        상태로 있던 **시간**을 재야 효과 크기를 알 수 있다.
        """
        n = len(self.resource.users)
        if n > self.max_concurrent_jobs:
            self.max_concurrent_jobs = n
        now = self.env.now
        if n >= 2 and self._two_since is None:
            self._two_since = now
        elif n < 2 and self._two_since is not None:
            self.two_job_time += now - self._two_since
            self._two_since = None

    # -- 통계 ---------------------------------------------------------------

    def work_enter(self, attr: str = "proc_time") -> None:
        """작업 구간 하나가 시작된다. `work_exit`와 **반드시 짝**이다.

        카운터가 0에서 1로 오를 때만 시계를 켠다. 두 번째 job이 올라와도
        시계는 하나다 — 그게 합집합이다.

        **종료 시각 순서에 기대지 않는다.** 처음에는 "덮은 마지막 시각"
        하나로 줄이려 했는데 틀린다: A가 [0,1], C가 [2,5], B가 [3,4]로
        일하면 닫히는 순서가 A·B·C라, C를 닫을 때 이미 시계가 4까지 가 있어
        [2,3]을 놓친다. 그리고 그 엇갈림이 바로 cascading이다.

        **조정할 값이 없다.** 상수도 문턱값도 쓰지 않는다.
        """
        if self.kpi is not None:
            self.kpi.begin_work(self, attr)
        if self._work_jobs == 0:
            self._work_since = self.env.now
        self._work_jobs += 1

    def work_exit(self, attr: str, start: float) -> None:
        """작업 구간 `[start, now]` 하나가 끝난다. **`finally`에서 부른다.**

        상태별 총합(`attr`)과 합집합(`work_time`)을 한 자리에서 적는다.
        따로 부르게 두면 한쪽만 부르는 자리가 반드시 생긴다.
        """
        now = self.env.now
        if self.kpi is not None:
            self.kpi.end_work(self)
        setattr(self, attr, getattr(self, attr) + now - start)
        self._work_jobs -= 1
        if self._work_jobs <= 0:
            self._work_jobs = 0
            if self._work_since is not None:
                self.work_time += now - self._work_since
                self._work_since = None

    def close_work(self) -> None:
        """스냅샷 시점에 열려 있는 합집합 구간을 닫는다.

        `stop_busy`와 같은 이유다 — 마지막 구간을 닫지 않으면 실행 끝에
        일하던 장비의 시간이 통째로 빠진다. 두 번 불러도 안전하다.
        """
        if self._work_since is not None:
            self.work_time += self.env.now - self._work_since
            self._work_since = self.env.now

    def start_busy(self) -> None:
        if self._busy_since is None:
            self._busy_since = self.env.now

    def stop_busy(self) -> None:
        if self._busy_since is not None and self.is_idle:
            self.busy_time += self.env.now - self._busy_since
            self._busy_since = None

    def close_intervals(self) -> None:
        """지금 열려 있는 누계 구간을 **전부** 닫고 같은 자리에서 다시 연다.

        쓰는 자리가 둘이고 요구가 같다 — "여기까지의 누계를 확정하라":

          warm-up 경계   `KPICollector.mark_warmup`
          스냅샷 시점    `_area_states` / `_toolgroup_states`

        그 요구가 **네 개의 열린 구간**에 걸쳐 있는데, 예전에는 호출부가
        `stop_busy()` + `close_work()` + `note_jobs()`를 차례로 부르고 있었다.
        셋 중 둘이 자기 일을 안 한다:

          `_work_since`  `close_work`가 닫는다                      OK
          `_busy_since`  `stop_busy`는 **`is_idle`일 때만** 닫는다 — lot이
                         올라가 있으면 그냥 지나가 마지막 점유가 통째로 빠진다
          `_two_since`   `note_jobs`는 `n >= 2`면 두 분기 어디에도 안 걸린다.
                         "마지막 2-job 구간을 닫는다"는 주석이 붙어 있었지만
                         닫지 않았다
          `_down_since`  아무도 안 닫아서 호출부가 매번 손으로
                         `total - _down_since`를 더하고 있었다 — 같은 식이
                         두 군데에 복사돼 있었다

        **초기화가 아니라 되물림(re-anchor)이다.** 더한 뒤 시작점을 `now`로
        옮긴다. 그래서

          * 두 번 불러도 0을 더한다 (호출 순서에 기대지 않아도 된다)
          * 뒤이어 정상 경로(`work_exit`, `set_up`, `stop_busy`)가 닫으면
            **나머지만** 더해진다

        둘째가 warm-up 경계에서 구간을 쪼갤 수 있는 이유다. 경계를 걸친
        작업/정지 하나를 앞뒤로 정확히 나눠 준다.

        난수도 이벤트도 건드리지 않는다. 언제 불러도 동역학이 안 바뀐다.
        """
        now = self.env.now
        if self._work_since is not None:
            self.work_time += now - self._work_since
            self._work_since = now
        if self._busy_since is not None:
            self.busy_time += now - self._busy_since
            self._busy_since = now
        if self._two_since is not None:
            self.two_job_time += now - self._two_since
            self._two_since = now
        if self._down_since is not None:
            self.unavailable_time += now - self._down_since
            self._down_since = now

    def add_wafers(self, count: int) -> None:
        """작업 하나가 끝날 때 호출한다.

        counter 기반 PM은 스트림마다 **자기 카운터**를 갖는다 (R6). WK PM이
        일어나도 MN/QT 카운터는 그대로다. polling 대신 임계 도달 시점에
        이벤트를 발화한다.

        배치 작업이면 count는 **배치 전원의 wafer 합**이다 (04_batch.md R7).
        한 lot 분량만 넘기면 Diffusion의 counter 기반 PM이 배치 크기 배수만큼
        늦게 온다.
        """
        self.wafers_processed += count
        for name, c in self.pm_counters.items():
            new = c + count
            self.pm_counters[name] = new
            ev = self.pm_events.get(name)
            if ev is not None and not ev.triggered and new >= self.pm_thresholds[name]:
                ev.succeed()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tool {self.tool_id}>"


class ToolGroup:
    """같은 사양의 장비 묶음."""

    __slots__ = ("env", "spec", "name", "tools", "queue", "batch_queue",
                 "batch_wafers", "_free_event", "_batch_event", "_arrival_event",
                 "free_tools")

    def __init__(self, env: simpy.Environment, spec: "ToolGroupSpec"):
        self.env = env
        self.spec = spec
        self.name = spec.name

        # delay 위치 toolgroup은 장비를 점유하지 않는다 (R2). 객체를 만들지 않는다.
        capacity = self._capacity_for(spec)
        self.tools: list[Tool] = ([] if spec.is_delay else
                                  [Tool(env, f"{spec.name}#{i + 1}", self, i, capacity)
                                   for i in range(spec.num_tools)])

        # 자리가 남은 장비만 모아 둔 목록.
        # 이게 없으면 tool 선택이 매번 전체 장비를 훑는다. DE_FE_86은 118대이고,
        # 과부하 구간에서는 장비 반납마다 대기 lot 전부가 깨어나 각자 훑으므로
        # 비용이 (대기 수 x 장비 수)로 커진다. 목록을 유지하면 대개 0~1개만 본다.
        # 순서는 tool index 순을 유지한다 — 결정론이 여기에 걸려 있다.
        self.free_tools: list[Tool] = list(self.tools)

        # 일반 대기열. deque다 — 배정 시 앞에서 빼는데 list면 매번 뒤 전체를
        # 당긴다. 과부하 toolgroup에는 수천 개가 쌓인다.
        self.queue: deque = deque()

        # Phase 04 — 배치 그룹. key = (제품, step_index), value = 도착순 lot deque.
        # key 자체가 R1(같은 제품의 같은 step 끼리만) 규칙이다. 별도로 검사할
        # 것이 없고, 잘못 묶는 일이 구조적으로 불가능하다.
        # 그룹 **사이**의 순서는 여기서 정하지 않는다. 배치 서버가 맨 앞 lot의
        # 도착 시각으로 고른다 — dict 순서로 고르면 뒤 그룹이 기아에 빠진다
        # (04_batch.md 3.1절, `_try_form_batch` 참조).
        self.batch_queue: dict[tuple, deque] = {}

        # 그룹별 대기 wafer 합. 매번 다시 세지 않는 이유는 비용이다: 배치 step
        # 도착마다 서버가 모든 그룹을 훑는데, 과부하 구간에서는 한 그룹에
        # 수천 개가 쌓인다. 다시 세면 (도착 수 x 그룹 수 x 대기 수)가 되어
        # 연 단위 실행에서 10^9 규모가 된다. 누적값으로 유지하면 상수다.
        # batch_queue와 항상 함께 갱신해야 한다 — 어긋나면 배치가 영영 안 만들어지거나
        # 미달 배치가 나간다. C04.16이 실행 끝에 두 값의 일치를 확인한다.
        self.batch_wafers: dict[tuple, int] = {}

        self._free_event: simpy.Event = env.event()
        self._batch_event: simpy.Event = env.event()
        self._arrival_event: simpy.Event = env.event()

    @staticmethod
    def _capacity_for(spec: "ToolGroupSpec") -> int:
        """장비 1대가 동시에 담는 작업 수.

        cascading 장비는 모듈이 직렬로 이어져 있어 앞 job이 뒤 모듈에 있는 동안
        다음 job이 앞 모듈에 들어갈 수 있다. 명세는 이를 직렬 슬롯 2개로 적고
        있고, 45개 toolgroup 전부 `STNCAP = 2`다 (05_cascading.md 2절).
        capacity 1인 cascading toolgroup은 없다.

        용량을 정하는 자리는 여기 한 곳뿐이다.
        """
        return 2 if spec.cascading else 1

    # -- 빈 장비 알림 --------------------------------------------------------

    def free_event(self) -> simpy.Event:
        """장비가 하나라도 비면 발화하는 이벤트. 대기자는 매번 다시 읽어야 한다."""
        return self._free_event

    def signal_free(self) -> None:
        """장비에 자리가 생겼다. 이 toolgroup의 배정 서버를 깨운다.

        예전에는 대기 중인 lot **전원**을 깨워 각자 판정하게 했다. 지금은
        서버 하나만 깨어난다. 대기자가 수천 명인 toolgroup에서 반납 1회
        비용이 O(대기 수)에서 O(1)이 된다 (fab_model._tool_server 참조).
        """
        ev, self._free_event = self._free_event, self.env.event()
        if not ev.triggered:
            ev.succeed()

    def arrival_event(self) -> simpy.Event:
        """대기열에 lot이 새로 들어오면 발화한다. 배정 서버만 기다린다."""
        return self._arrival_event

    def signal_arrival(self) -> None:
        """lot이 대기열에 들어왔다. 바로 배정될 수 있는 상황일지도 모른다."""
        ev, self._arrival_event = self._arrival_event, self.env.event()
        if not ev.triggered:
            ev.succeed()

    # -- 배치 (Phase 04) ------------------------------------------------------

    def batch_arrival_event(self) -> simpy.Event:
        """배치 그룹에 lot이 새로 들어오면 발화한다. 배치 서버만 기다린다."""
        return self._batch_event

    def signal_batch_arrival(self) -> None:
        """lot이 배치 그룹에 들어왔다. 최소 수량이 채워졌을 수 있다.

        대기 중인 lot 전부가 아니라 **서버 하나만** 깨어난다. 과부하 구간의
        Diffusion 그룹에는 수천 개가 쌓이므로, 도착마다 전원을 깨우면
        (도착 수 x 대기 수)로 비용이 커진다.
        """
        ev, self._batch_event = self._batch_event, self.env.event()
        if not ev.triggered:
            ev.succeed()

    def waiting_count(self) -> int:
        """대기 중인 lot 수. 일반 대기열과 배치 그룹 양쪽을 센다.

        배치 toolgroup의 lot은 `queue`가 아니라 `batch_queue`에 있다. 이걸
        빠뜨리면 Diffusion 대기열이 항상 0으로 보고된다.
        """
        return len(self.queue) + sum(len(v) for v in self.batch_queue.values())

    # -- 빈 자리 목록 --------------------------------------------------------

    def mark_taken(self, tool: "Tool") -> None:
        """장비가 꽉 찼으면 빈 자리 목록에서 뺀다."""
        if len(tool.resource.users) >= tool.resource.capacity:
            try:
                self.free_tools.remove(tool)
            except ValueError:
                pass

    def mark_down(self, tool: "Tool") -> None:
        """정지한 장비는 후보에서 뺀다 (R14)."""
        try:
            self.free_tools.remove(tool)
        except ValueError:
            pass

    def mark_released(self, tool: "Tool") -> None:
        """자리가 생겼으면 목록에 되돌린다. index 순서를 유지한다."""
        if tool.is_down:
            return
        if len(tool.resource.users) >= tool.resource.capacity:
            return
        if tool in self.free_tools:
            return
        # index 순 삽입. 결정론을 위해 순서가 실행마다 같아야 한다.
        lo, hi = 0, len(self.free_tools)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.free_tools[mid].index < tool.index:
                lo = mid + 1
            else:
                hi = mid
        self.free_tools.insert(lo, tool)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ToolGroup {self.name} x{len(self.tools)}>"
