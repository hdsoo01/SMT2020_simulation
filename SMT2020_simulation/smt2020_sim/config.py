"""시뮬레이션 제어 설정.

모델 파라미터는 여기 두지 않는다. 전부 xlsx에서 읽는다 (AGENTS.md 1절).
여기 있는 값은 "어떻게 실행할지"에 대한 것뿐이다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
# config.py -> smt2020_sim -> <repo root>
REPO_ROOT: Path = Path(__file__).resolve().parents[1]

DATA_DIR: Path = REPO_ROOT / "data"
REFERENCE_DIR: Path = REPO_ROOT / "reference"
RESULTS_DIR: Path = REPO_ROOT / "results"
# 수정된 배정 로직의 새 본 실행. 기존 결과와 저장/자동 조회 경로를 분리한다.
RUN_RESULTS_DIR: Path = RESULTS_DIR / "explicit8y_full_dispatchfix_20261007"
CACHE_DIR: Path = DATA_DIR / ".cache"

# 부하 스윕(operating curve)이 쌓이는 곳. **이름에 실행 길이가 들어간다.**
#
# 스윕 점 하나는 실행 하나이고, 곡선은 그 점들을 잇는다 — 길이가 다른 점을
# 한 곡선에 섞으면 안 된다. 특히 P0/P100 은 표본 수에 딸려서, 8년 점과 2년
# 점을 나란히 놓으면 길이 차이가 곡선의 모양처럼 보인다.
#
# **그래서 폴더 이름을 손으로 적지 않고 길이에서 유도한다.** 예전에는 폴더가
# 상수 하나(`sweep2y`)라 `run.py sweep --years 8` 이 8년 점을 2년 폴더에
# 그대로 떨어뜨렸다 — 곡선은 그려지는데 점들의 길이가 달랐다. 이름이 길이의
# 함수면 그 사고가 구조적으로 불가능하다.
def sweep_dir(years: float) -> Path:
    """`years` 년짜리 스윕이 쌓이는 폴더. 쓰는 쪽과 읽는 쪽이 같이 본다."""
    return RESULTS_DIR / f"sweep{years:g}y"


# 읽는 쪽(`visualize.py`, `report_all`)이 기본으로 그릴 곡선의 길이.
# **여기 한 곳만 바꾸면** 돌리는 쪽 기본값과 그리는 쪽이 함께 따라온다.
SWEEP_YEARS: float = 8.0
SWEEP_DIR: Path = sweep_dir(SWEEP_YEARS)

XLSX_PATH: Path = DATA_DIR / "SMT_2020_Model_Data_-_LVHM.xlsx"

# ---------------------------------------------------------------------------
# reference/ — **이 최소 구성에는 없다**
# ---------------------------------------------------------------------------
# 초기 WIP 2,156 lot은 xlsx에 시트가 없고 `reference/`의 `WIP.txt`에만 있다.
# 그래서 이 구성은 **parametric 투입 전용**이다 (빈 fab, 반복 투입).
#
# 아래 경로는 남겨 둔다 — 지우면 `data_parser`가 import 단계에서 깨진다.
# explicit 모드를 요청하면 `_parse_initial_wip`가 파일이 없다고 멈추는데,
# 그게 옳은 동작이다: 조용히 빈 WIP으로 도는 것보다 낫다.
ASD_DIR: Path = REFERENCE_DIR / "LVHM_Model.asd"
WIP_TXT_PATH: Path = ASD_DIR / "WIP.txt"

# 시뮬레이션 t=0. options.def의 SIM_START에 해당한다. 시트와 WIP.txt의
# 날짜를 분으로 바꾸는 기준점이다.
SIM_START: datetime = datetime(2018, 1, 1, 0, 0, 0)


class ReferenceAccessError(RuntimeError):
    """모델이 AutoSched **결과**를 읽으려 했다. 구조적으로 막는다."""


def asd_input_path(name: str) -> Path:
    """`.asd` 입력 파일 하나의 경로. **결과 파일은 여기를 통과하지 못한다.**

    이름에 경로 구분자나 `..`가 있으면 거부한다 — `ASD_DIR` 밖으로 나가는
    유일한 길이다. 확장자가 `.rep`이면 거부한다.

    가드를 함수로 둔 이유: 모델 쪽에서 `reference/`를 여는 자리가 **하나로**
    모여야 나중에 검사할 수 있기 때문이다 (C12.1이 이걸 검사한다).
    """
    if "/" in name or "\\" in name or name.startswith("."):
        raise ReferenceAccessError(f"{ASD_DIR} 밖을 가리킨다: {name!r}")
    if name.lower().endswith(".rep"):
        raise ReferenceAccessError(
            f"모델은 AutoSched 결과(.rep)를 읽지 않는다: {name!r}. "
            "결과는 kind='ref' 체크에서 비교 기준으로만 쓴다 (AGENTS.md 1.1절)")
    return ASD_DIR / name

# ---------------------------------------------------------------------------
# 재현성
# ---------------------------------------------------------------------------
RANDOM_SEED: int = 42

# ---------------------------------------------------------------------------
# 투입 모드
# ---------------------------------------------------------------------------
# "parametric"  Lotrelease 시트의 반복 투입, 빈 fab. **이 구성의 유일한 모드다.**
# "explicit"    lot별 투입 + 초기 WIP. `reference/`가 있어야 하고 여기엔 없다.
#
# operating curve를 그리려면 투입률을 스케일해야 하는데, explicit은 lot마다
# 시각이 박혀 있어 스케일할 방법이 없다. 그것이 parametric으로 고정한 이유다.
RELEASE_MODE: str = "parametric"

# explicit 모드에서만 뜻이 있다 (위 참고).
INITIAL_WIP: bool = True

# ---------------------------------------------------------------------------
# 구조 상수
# ---------------------------------------------------------------------------
# 단위 변환 계수. 파서에서만 쓴다 (AGENTS.md 1절이 허용하는 리터럴).
MINUTES_PER_HOUR: float = 60.0
MINUTES_PER_DAY: float = 1440.0
MINUTES_PER_WEEK: float = 7.0 * 1440.0

# Lotrelease 전 스트림이 25로 동일하다 (DATA_FACTS.md 8절). 파서가 검증한다.
# route 단위 잔여 처리시간을 미리 계산할 때만 쓰는 기본값이다.
DEFAULT_WAFERS_PER_LOT: int = 25

# ---------------------------------------------------------------------------
# 해석이 갈렸던 규칙 — 전부 문서로 확정됐다
# ---------------------------------------------------------------------------
# wafer cascading에서 **lot 사이**의 진입 간격 (05_cascading.md 3.1절).
#
#   "n_interval"   n x interval
#   "one_interval" 1 x interval
#
# **확정: n_interval. 근거는 AutoSched 문서다** (2026-09-18, G-3 재결정).
# 05 3.1절은 이걸 "명세가 말하지 않는 유일한 규칙"으로 적었지만, 문서를 다시
# 읽으니 말하고 있었다:
#
#   "the time between finished lots depends on the time defined in the
#    **BatchInterval** ... Cascading of wafers is modeled in an **analogous**
#    way, while the **PartInterval** and PartIntUnits columns are used"
#
# 두 방식이 **다른 컬럼**을 쓴다. part = wafer이므로 wafer cascading에서
# 간격의 단위는 웨이퍼 하나다. 데이터가 그대로다 — lot cascading 면적은
# 44~65분, wafer cascading 면적은 0.70~0.80분.
#
# "analogous"를 적용하면: lot cascading에서 lot 하나가 슬롯 1을 한 interval
# 점유하듯, wafer cascading에서는 **웨이퍼 하나**가 한 interval 점유한다.
# 웨이퍼 n개가 차례로 지나가므로 다음 lot의 첫 웨이퍼는 n x interval 뒤다.
#
# **one_interval은 물리적으로 불가능하다.** 그러면 다음 lot의 첫 웨이퍼가
# 들어오는 순간 이 lot의 두 번째 웨이퍼도 슬롯 1에 있어야 한다 — 슬롯 하나에
# 둘이다. `STNCAP = 2`(직렬 슬롯 2개)로 표현할 수 없다.
#
# 플래그는 남겨 둔다. 값을 고르기 위해서가 아니라 **이 해석이 결과를 얼마나
# 움직이는지 보이기 위해서**다 (05 7.1절). 레퍼런스에 가까운 쪽을 고르는
# 용도가 아니다 — 그건 커브 피팅이다.
WAFER_CASCADE_GATE: str = "n_interval"

# 최소 연속 실행(MINRUN = 7). 최소 미달인데 같은 setup을 쓰는 대기 lot이
# 없을 때 장비는 놀 것인가, setup을 바꿀 것인가?
#
# **명세가 결정한다 — 미결정 사항이 아니었다.** 세 곳이 같은 말을 한다:
#
#   AutoSched 문서: "a minimum number of lots **is ensured** in a run"
#   generic_format: "Minimum number of processed lots required after a tool has
#                    changed its setup (**before a new setup**)"
#   generic_format: "The setupavoidance rule **ensures** a prescribed number of
#                    lots in a run"
#
# "선호한다"가 아니라 "보장한다/필요하다"다. 최소를 채우기 전에 새 setup을
# 하면 규칙 위반이다. 그리고 회피 대상 세 toolgroup에는 **setup이 필요 없는
# step이 0개**라(데이터로 확인) 다른 lot을 돌릴 길도 없다 — 대기가 강제된다.
#
# 처음에 False로 두었다가 07 3.2절의 민감도 실험에서 바로잡았다. 근거는
# 레퍼런스가 아니라 문서다 (DECISIONS G-6).
MINRUN_STRICT: bool = True

# 우선순위가 높은 lot이 최소 실행을 깨고 들어올 수 있는가?
#
# **명세가 결정한다.** AutoSched 문서가 hot lot 항목에서 명시한다:
#   "If there is one hot lot chosen, **it can trigger a setup**."
# 같은 문단이 순위도 확정한다 — rank_HP -> rank_RSETUP -> CR (dataset 2).
#
# 07 3.2절: 원래 프롬프트는 이 설정을 켜 두고도 "다른 setup의 hot lot이
# 기다리는데 같은 setup의 regular lot을 반환"해서 자기모순이었다. 적용 순서를
# 명시하고 충돌 사례를 테스트한다 (C07.9).
# 같은 setup lot이 없는 경우에도 hot/superhot 예외를 적용한다. regular만
# 기다리는 경우에는 MINRUN_STRICT를 따른다 (2026-10-07 재현 검증).
MINRUN_HOTLOT_OVERRIDE: bool = True

# ---------------------------------------------------------------------------
# Phase 11 — super hot lot의 장비 예약 (2026-09-19, 문서 전수 대조에서 발견)
# ---------------------------------------------------------------------------
# super hot lot이 다음 step의 장비를 **예약**하는가.
#
#   True  (기본)  xlsx `SUPERHOTLOT` 컬럼을 따른다 — 149 lot이 예약한다
#   False         `.asd` 주문 파일의 `HOTLOT` 컬럼을 따른다 — 아무도 예약 안 한다
#
# **이건 우리가 고른 값이 아니라 출처 선택이다.** 두 입력이 서로 다른 말을
# 하고, 값마다 어느 쪽을 따르는지가 정해져 있다. 자유 파라미터가 아니다 (G-20).
#
# AutoSched 문서가 예약의 조건을 **둘 다** 요구한다:
#
#   "To enable the reservation function, the lot has to be set as a 'HOTLOT'
#    **and** the rule_HotLotFirst rule ... has to be applied. ...
#    **If there is a yes** for the Super Hot Lots, they reserve a tool at the
#    next downstream step."
#
# 장비 쪽은 양쪽 출처가 일치한다 — `tool.txt`의 RULE이 rule_HotLotFIRST 103개
# + rule_LSSU 3개이고, xlsx DISPATCHING도 103 + 3이다.
#
# lot 쪽이 갈린다. dataset 2의 `.asd` 주문 파일에는 **yes가 하나도 없다**:
#
#   order_high_SL_92PCTL.txt (활성)  167,129행 전부 빈칸 (PRIOR=30인 149개 포함)
#   order_medium / order_low          동일
#   WIP.txt                           2,156행 전부 빈칸
#   order.txt (비활성)                21행 전부 `no` — PRIOR=30 행도 `no`
#
# 빈칸이 아니라 **명시적 `no`**를 적은 파일이 있다는 것이 결정적이다. 컬럼
# 누락이 아니라 의도다. 즉 **레퍼런스 KPI는 예약 없이 나온 값이다.**
#
# 기본을 True로 두는 이유: 우리 명세 출처는 xlsx이고(DECISIONS D), generic
# 문서가 `SUPERHOTLOT`을 Lotrelease 표의 정식 컬럼으로 기술한다. 문서화된
# 기능을 지우는 쪽이 더 큰 변경이다.
#
# **크기는 이미 재어 두었다** — `counters.reservation_cost_pct`가 0.010%다
# (2년 실행에서 예약 7,555회, 평균 점유 12.7분). CT 격차 2.05%의 200분의
# 1이고 한 seed 분해능(1.11%) 아래다. 어느 쪽으로 두든 fab KPI는 안 움직인다.
# 그래서 이 플래그의 쓸모는 값을 고르는 게 아니라 **그 사실을 보이는 것**이다.
#
# 켜고 끄는 것이 난수 순서를 바꾸므로, 두 설정의 실행은 seed가 같아도 서로
# 다른 표본이다. `compare`가 머리말에 크게 찍는다 (EXPECTED_DIFF_KEYS).
SUPERHOT_RESERVATION: bool = True

# ---------------------------------------------------------------------------
# 납기 난수 z (논문 식 1)
# ---------------------------------------------------------------------------
# 논문 III-D절:
#
#   d_j = r_j + z_j · FF_i · RPT_i
#   "z_j is sampled from a continuous uniform distribution between 0.8 and 1.2
#    to randomize due dates"
#
# **여기 상수로 두는 이유.** 모델 파라미터는 xlsx에서 읽는 것이 원칙이지만
# (AGENTS.md 1절), 이 값은 parametric 모드가 읽는 `Lotrelease` 시트에 없다.
# 그 시트는 z를 이미 적용해 평균만 남긴 판이기 때문이다 — `DUE DATE - START
# DATE`가 곧 `FF_i · RPT_i` 다 (논문 Table II의 FF x RPT와 21개 스트림 전부
# 0.3% 안에서 일치한다).
#
# 값 자체는 **데이터로 확인했다.** `Lotrelease - variable due dates` 시트
# 167,129행의 스트림별 offset을 재면 `min/mean = 0.799`, `max/mean = 1.199`,
# `max/min = 1.500` 이다. 논문의 U(0.8, 1.2) 그대로다. 즉 고를 여지가 있는
# 자유 파라미터가 아니라 **두 출처가 일치하는 명세값**이다.
#
# explicit 모드에는 적용하지 않는다. 그쪽 lot은 이미 자기 납기를 갖고 있고,
# 그 납기가 바로 이 분포에서 뽑힌 표본이다 — 두 번 뽑으면 분산이 두 배가 된다.
DUE_DATE_Z: tuple[float, float] = (0.8, 1.2)

# Operational summaries for every lot/step; detailed trajectories for urgent lots.
# This controls recording only, never dispatching, releases or random draws.
KPI_DETAIL_LEVEL: str = "lean"  # lean / full
KPI_TRACE_TYPES: tuple[str, ...] = ("hot", "superhot")

# ---------------------------------------------------------------------------
# Phase 09 — rework 상한 (09 R6, 3.1절)
# ---------------------------------------------------------------------------
# lot 하나가 rework될 수 있는 최대 횟수. **조절 손잡이가 아니라 안전장치다.**
# 이 데이터셋의 rework 확률은 1~2%이므로 기대 반복 횟수는 1 미만이고, 상한에
# 닿을 일이 없다. 닿는다면 확률을 잘못 읽은 것이다 — 1.7을 1.7%가 아니라
# 170%로 쓴 경우가 09 3.1절이 지목한 사례다.
#
# **한 번이라도 걸리면 그 자체가 발견이다** (3.1절). 걸리면 세고, rework 없이
# 진행시켜 실행은 계속한다 (R6).
MAX_REWORKS_PER_LOT: int = 50

# ---------------------------------------------------------------------------
# Phase 10 — CQT window과 rework (10 3.2절, DECISIONS B-5)
# ---------------------------------------------------------------------------
# rework가 lot을 **열려 있는 window의 source보다 앞으로** 되돌리면 그 window를
# 어떻게 할 것인가. 문서에 없다.
#
#   True  (기본)  폐기하고 `abandoned_by_rework`로 따로 센다
#   False         들고 간다. lot이 앞으로 가며 target을 다시 지나므로 닫힌다
#
# **어느 쪽이든 violation으로 세지 않는다.** 되돌아간 시간까지 창에 넣으면
# 측정이 아니라 rework 벌점이 된다.
#
# 폐기가 기본인 이유: window는 "이번 통과에서 두 공정 사이에 얼마나 떴는가"를
# 재는 것이고, 되돌아간 lot의 그 통과는 끝났다. 08 R5(LTL 지정 폐기)와 같은
# 논리다. 다만 필연은 아니므로 플래그로 두고 10 7절이 양쪽을 다 보고하게 한다.
#
# **문서로 결정할 수 없음이 확인됐다** (2026-09-18). AutoSched 문서:
#   "There is **no extension for critical queue times provided by AutoSched
#    AP**. Therefore, customizing is necessary ... here only the corresponding
#    data is provided for all routes and it is prepared for customization."
# 레퍼런스 자체가 CQT를 구현하지 않았다. 대조할 동작이 없다.
#
# **그래도 무해하다** — CQT는 측정 전용이고 아무 lot의 경로도 바꾸지 않는다
# (C10.1이 "tracking changes nothing about the flow"를 53 lot 완전 일치로
# 확인한다). 이 플래그는 KPI를 움직이지 않는다.
CQT_DROP_ON_REWORK: bool = True

# ---------------------------------------------------------------------------
# 로깅 (AGENTS.md 7.2절) — 기본 전부 off
# ---------------------------------------------------------------------------
LOG_SETUP = False
LOG_REWORK = False
LOG_CQT = False
LOG_RESERVATION = False
LOG_LTL = False
LOG_BATCH = False
LOG_DOWNTIME = False
