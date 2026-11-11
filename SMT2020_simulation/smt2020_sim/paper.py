"""논문(Kopp et al. 2020)이 보고한 값. **비교점이지 기준이 아니다.**

여기 숫자는 **모델에 들어가지 않는다.** AGENTS.md 1절이 금지하는 것은 시뮬레이션
파라미터를 소스에 적는 것이고, 이것은 `run_experiment.REFERENCE`와 같은 부류 —
바깥에서 보고된 **대조용 상수**다. 모델 경로에서 import 하면 안 된다.

**Table II와 Fig. 3은 다른 것을 잰다. 성능 기준은 Fig. 3 하나뿐이다.**

| | 무엇 | 통계량 | 규칙 |
|---|---|---|---|
| Table II | **납기 계산식의 입력** | **P92** | **FCFS** |
| Fig. 3 | 달성된 FF 분포 | 5/50/95 분위수 | CR (dataset 2의 실제 규칙) |

논문 IV-3절:

  "z_j is sampled from a continuous uniform distribution between 0.8 and 1.2
   to randomize due dates, RPT_i is product i's RPT, and **FF_i is a
   user-determined FF for product i**. For each product i, baseline values for
   FF_i were calculated as the **92% percentile FF while running the model
   under a FCFS rule** at 10,000 WSPW."

즉 납기가 `d_j = r_j + z_j · FF_i · RPT_i`로 정해지고, Table II는 그 `FF_i`다.
`.asd`의 주문 파일 이름이 `order_high_SL_**92PCTL**.txt`인 것이 이것이다
(medium은 50PCTL, low는 25PCTL).

**그래서 Table II를 우리 달성 FF와 맞대면 안 된다.** 한때 "논문 안에서 두 표가
서로 맞지 않는다"고 적었는데 틀렸다 — 논문을 읽기 전이었다. 서로 다른 규칙의
서로 다른 통계량이라 애초에 같을 이유가 없다.

값은 이전 과제의 `visualize.py`(`PAPER_FF`, `PAPER_PCTILES`)에서 옮겼고,
Fig. 3 쪽은 논문 IV-B절 본문으로 대조했다.
"""

from __future__ import annotations

# 논문 Table II — **납기 파라미터**다. 달성 성능이 아니다 (위 docstring).
#
#   rpt_days  raw processing time(일). **우리 분모와 3% 어긋난다**
#             (우리 440.4 h 대 논문 453.8 h, 전 제품에서 일관되게 낮다).
#             정의 후보 여섯을 재봤지만 맞는 것이 없어 **재구성하지 않고
#             논문 값을 그대로 쓴다** — 장부의 "X-factor" 행 참고.
#   regular/hot  FCFS 하의 P92 FF. 납기 `d = r + z·FF·RPT` (z~U(0.8,1.2))의
#             입력이다. 우리 달성 FF와 맞대면 안 된다.
TABLE_II: dict[str, dict[str, float]] = {
    "Product_1":  {"regular": 2.10, "hot": 1.43, "rpt_days": 23.76},
    "Product_2":  {"regular": 2.06, "hot": 1.42, "rpt_days": 25.46},
    "Product_3":  {"regular": 2.07, "hot": 1.41, "rpt_days": 27.18},
    "Product_4":  {"regular": 2.02, "hot": 1.45, "rpt_days": 15.96},
    "Product_5":  {"regular": 2.17, "hot": 1.54, "rpt_days": 10.99},
    "Product_6":  {"regular": 2.12, "hot": 1.48, "rpt_days": 14.16},
    "Product_7":  {"regular": 2.08, "hot": 1.43, "rpt_days": 16.85},
    "Product_8":  {"regular": 2.17, "hot": 1.46, "rpt_days": 17.47},
    "Product_9":  {"regular": 2.14, "hot": 1.46, "rpt_days": 18.27},
    "Product_10": {"regular": 2.11, "hot": 1.46, "rpt_days": 18.98},
}

# 논문 Fig. 3 — **이것이 유일한 성능 기준이다.** 1.95가 과제 목표였던 값이다.
#
# 논문 IV-B절이 본문으로 못박는 조건 넷. **하나라도 다르면 비교가 아니다:**
#
#   1. **regular lot만** — "Only regular lots are represented,
#      hot lots have a FF close to 1.0"
#   2. **8년 단일 실행, 앞 1년 warm-up** — 우리 실험 구성과 같다
#   3. 10,000 WSPW 작업점 (모델 용량은 10,250)
#   4. CR 디스패칭
#
# 25/75는 논문 본문에 없다 — 이전 과제가 Fig. 3 그림에서 읽은 값이다.
# 5/50/95/max는 본문 숫자와 대조했다.
FIG3_PCTILES: dict[int, float] = {5: 1.79, 25: 1.88, 50: 1.95, 75: 2.04, 95: 2.13}
FIG3_MAX = 2.46                  # "the maximum reach values of ... 2.46"

# 논문 본문: "hot lots have a FF close to 1.0". Table II의 hot 열(1.41~1.54)은
# **납기용 P92**이지 이 값이 아니다. 우리 hot은 1.394다.
HOT_FF_ABOUT = 1.0

# 정상상태 주간 처리량 작업점. 모델 용량은 10,250 WSPW.
WSPW_TARGET = 10_009.0
WSPW_CAPACITY = 10_250.0

# 같은 데이터셋을 돌린 다른 구현들의 FF **평균**. 어느 것도 정답이 아니다 —
# 넷이 서로 다르다는 사실이 정보다.
#
#   AutoSched    `perf.rep` Period_1. **그 도구를 실제로 돌린 결과**이고
#                1.95가 아니다. 즉 1.95는 "상용 내부 최적화"로 설명되지 않는다.
#   PySCFabSim   이전 과제가 직접 클론·실행 (2.12~2.28)
#   이전 구현     최종보고서 5.1절, 3년 측정
PEER_FF: dict[str, float] = {
    "AutoSched (perf.rep)": 2.23,
    "PySCFabSim": 2.22,
    "이전 자체 구현": 2.32,
}


# 논문 Table IV — "Main Characteristics and Performance Measures of Dataset 2".
#
# **네 열의 정의를 추측하지 않았다.** AutoSched 결과 `stnfam.rep`(tool group별
# DOWN%/PM%/UTIL%)과 `.asd`의 `tool.txt`(tool group -> area, 대수)로 역산해서
# Period_1·2·3 모두에서 아래 값을 재현하는 조합을 찾았다:
#
#   Number of tool groups  area의 tool group 수. `Delay_32`는 **뺀다**
#   Number of tools        그 합. 1,313 - 400(Delay_32) = 913 으로 합계가 맞는다
#   Average Availability   tool group별 (100 - DOWN% - PM%) 의 **단순평균**
#   Proportion of SDT      PM / (DOWN + PM)
#   Average Utilization    tool group별 UTIL% / 가용률 의 단순평균
#                          ← 분모가 전체 시간이 아니라 **가동 가능 시간**이다.
#                            Dielectric이 가용률 80.4 %인데 가동률 80.7 %로
#                            적힌 것이 이 정의가 아니면 설명되지 않는다.
#   Maximum Utilization    같은 값의 tool group 최댓값
#
# 역산 결과 (Period_1, 논문값과 나란히): 가용률은 11개 area 전부 0.1 pp 이내,
# SDT 비율은 0.4 pp 이내, 가동률은 `UTIL%/가용률`을 취하면 11개 전부 0.5 pp
# 이내로 맞는다. 단순평균이 아니라 장비수 가중평균을 쓰면 Dielectric이
# 80.7 → 67.1 로 어긋난다 — 그래서 단순평균이다.
#
# `label`은 논문 표의 행 이름이고, 키는 우리 area 이름이다.
TABLE_IV: dict[str, dict[str, float | str]] = {
    "Dielectric": {"label": "Dielectric", "toolgroups": 10, "tools": 54,
                   "avail": 80.4, "sdt": 71.4, "util": 80.7, "util_max": 89.9},
    "Diffusion":  {"label": "Diffusion", "toolgroups": 10, "tools": 73,
                   "avail": 92.5, "sdt": 80.6, "util": 86.1, "util_max": 92.1},
    "Dry_Etch":   {"label": "Dry Etch", "toolgroups": 21, "tools": 312,
                   "avail": 88.5, "sdt": 80.4, "util": 85.1, "util_max": 91.5},
    "Implant":    {"label": "Implant", "toolgroups": 9, "tools": 36,
                   "avail": 80.2, "sdt": 70.8, "util": 56.1, "util_max": 85.6},
    "Litho":      {"label": "Lithography", "toolgroups": 11, "tools": 170,
                   "avail": 86.4, "sdt": 51.6, "util": 85.6, "util_max": 96.8},
    "Planar":     {"label": "Planar", "toolgroups": 6, "tools": 26,
                   "avail": 80.0, "sdt": 90.3, "util": 69.7, "util_max": 89.4},
    "TF":         {"label": "Thin Films", "toolgroups": 11, "tools": 79,
                   "avail": 85.2, "sdt": 71.1, "util": 79.2, "util_max": 89.2},
    "Wet_Etch":   {"label": "Wet Etch", "toolgroups": 14, "tools": 99,
                   "avail": 89.1, "sdt": 80.5, "util": 77.3, "util_max": 90.9},
    "Def_Met":    {"label": "Defectivity Metrology", "toolgroups": 7, "tools": 15,
                   "avail": 96.5, "sdt": 89.8, "util": 45.5, "util_max": 78.4},
    "Litho_Met":  {"label": "Litho Metrology", "toolgroups": 4, "tools": 45,
                   "avail": 96.5, "sdt": 90.0, "util": 87.7, "util_max": 91.8},
    "TF_Met":     {"label": "Thin Films Metrology", "toolgroups": 2, "tools": 4,
                   "avail": 96.5, "sdt": 90.3, "util": 45.0, "util_max": 72.7},
}

# 논문 표의 Total 행. 파서가 이 둘을 못 맞추면 area 매핑이 틀린 것이다.
TABLE_IV_TOTAL_TOOLGROUPS = 105
TABLE_IV_TOTAL_TOOLS = 913

# 논문 표의 행 순서. 앞 8개가 생산 area, 뒤 3개가 계측 area로 줄이 갈린다.
TABLE_IV_ORDER: tuple[str, ...] = (
    "Dielectric", "Diffusion", "Dry_Etch", "Implant", "Litho", "Planar",
    "TF", "Wet_Etch", "Def_Met", "Litho_Met", "TF_Met",
)
TABLE_IV_METROLOGY = ("Def_Met", "Litho_Met", "TF_Met")


def rpt_hours(product: str) -> float | None:
    """논문 Table II의 RPT를 시간 단위로. 모르는 제품이면 None."""
    row = TABLE_II.get(product)
    return None if row is None else row["rpt_days"] * 24.0


def pad(label: str, width: int) -> str:
    """한글을 두 칸으로 세어 표를 맞춘다.

    `f"{s:<20}"`은 **글자 수**를 세므로 한글이 섞이면 표가 어긋난다.
    """
    w = sum(2 if ord(ch) > 0x2E7F else 1 for ch in label)
    return label + " " * max(0, width - w)
