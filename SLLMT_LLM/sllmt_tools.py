"""SLLMT 도구 — LLM이 부르는 파이썬 함수와 입력 스키마. 특정 LLM 회사에 묶이지 않는다 (D-57, D-58).

    get_fab_info     제품군·툴그룹·기준 KPI·조정 가능 범위
    build_scenario   요청을 시나리오(seed별 JSON 틀)로 만들고 정적 사전 점검(datagen/scenario.py)까지 한다
    run_experiment   같은 시나리오의 기존 실행이 있으면 재사용한다. 없으면 실행 명령을 돌려준다
                     (allow_run=True 일 때만 datagen/run_batch.py 로 직접 돌린다)
    get_report       experiments/whatif_report.py 로 seed 묶음 보고서를 만들고 요약을 돌려준다
                     (기준 운영 또는 다른 시나리오와 같은 seed 끼리 짝 비교, 항목 이름은 한국어)

기존 코드(code/, datagen/, experiments/)는 고치지 않고 불러 쓰기만 한다.
수치는 모두 도구가 만든다. LLM은 숫자를 계산하지 않는다.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sllmt_core.overrides import build_overrides
from sllmt_core.recovery import report_fields as _recovery_fields
from sllmt_core import report_text as _rt
from sllmt_core.registry import ToolRegistry

def _sim_code_dir(root: Path) -> Path | None:
    """smt2020_sim 패키지가 든 폴더. 연구 폴더는 code/ 아래, GitHub 시뮬레이션 저장소는 맨 위 (11-12)."""
    return next((d for d in (root / "code", root) if (d / "smt2020_sim").is_dir()), None)


def find_research_root() -> Path:
    """SMT2020_research 폴더를 찾는다. SLLMT 폴더가 어디에 있어도 된다 (연구 폴더 안 / 바탕화면 등).

    순서: 환경변수 SLLMT_RESEARCH_ROOT → SLLMT 의 상위 폴더 → 상위 폴더 안의 SMT2020_research → 바탕화면의 SMT2020_research.
    """
    here = Path(__file__).resolve().parent
    env = os.environ.get("SLLMT_RESEARCH_ROOT", "").strip()
    cands = ([Path(env)] if env else []) + [here.parent, here.parent / "SMT2020_research",
                                            Path.home() / "Desktop" / "SMT2020_research"]
    for c in cands:
        if (c / "datagen" / "scenario.py").exists() and _sim_code_dir(c):
            return c.resolve()
    raise RuntimeError("SMT2020_research 폴더를 찾지 못했다. 환경변수 SLLMT_RESEARCH_ROOT 에 그 폴더 경로를 넣는다 "
                       f"(찾아본 곳: {[str(c) for c in cands]})")


ROOT = find_research_root()
for _p in (_sim_code_dir(ROOT), ROOT / "datagen", ROOT / "experiments"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
sys.dont_write_bytecode = True

GROUPING = "tier"
DEFAULT_SEEDS = [1, 2, 3, 4, 5]
DEFAULT_END_WEEK = 52
WARMUP_WEEK = 26                     # 기준 seed 평균 MSER-5 22주, 24주에 WIP 99.7% (LLM연계_검토.md 6.1)
LIMITS = {"max_static_util": 1.02}   # 기본 97% 대신. 파일럿에서 정적 100.5%(+4%)까지 정상상태 (6.6)
DEFAULT_LIMIT = 0.97
HOT_SHARE_WARN_PCT = 10.0            # hot 전환 기간 중 hot 비중이 이보다 크면 경고 (기준 약 2.6%, 11-05 밤)
BASE_RUNS = [ROOT / "results" / "pilot_20261014" / f"P00_base_s{s}" for s in (1, 2, 3)] + \
            [ROOT / "results" / "pilot2_20261014" / f"N_base_s{s}" for s in (4, 5)]
SEARCH_ROOTS = ["results/pilot_20261014", "results/pilot2_20261014", "results/whatif_pilot_20261103", "results/sllmt",
                "results/c1_step2*"]  # glob 가능
MIN_PER_WEEK_PARALLEL = 0.6          # 10개 병렬 52주 약 32분 (파일럿 실측) → 주당 약 0.6분
BASE_MSER_WEEK = 22                  # 기준 운영 seed 평균 WIP 안정화 지점(MSER-5). warm-up 이 이보다 짧으면 경고 (11-07)
DEFAULT_EVENT_START = 30             # 정지·고장 특성 시작 주 기본값 (프롬프트 기본값과 같음, 11-08 도구가 직접 넣음)
# 11-08 (T-v1 X2 r2): 판정 문구가 "차이 없음" 이면 답이 "늘지 못했다·같다"로 단정하기 쉬워 문구를 바꿈
NO_DIFF_LABEL = "유의한 차이 확인 못함 (CI 가 0 을 포함, '차이가 없다'는 뜻이 아님)"
# 분기(C1) 시작 상태: 기준 seed 의 그 주 상태 (LLM연계_검토.md 6.9절). 갈래 1~5 는 그 주에 난수만 바꿔 갈라진다
STATES = {"보통": {"seed": 2, "week": 34, "code": "normal"},
          "혼잡": {"seed": 3, "week": 40, "code": "busy"},
          "여유": {"seed": 3, "week": 32, "code": "slack"}}
BRANCH_HORIZON = 22                  # 분기 뒤 볼 주. 4주 정지 피해가 18주 뒤에도 회복 안 됨 (6.11절 1단계, 11-05)

# ---------------------------------------------------------------------------------------------------
# 입력 스키마 (JSON Schema). 모델 어댑터가 각 회사 형식으로 옮긴다.

TOOL_SPECS = [
    {
        "name": "get_fab_info",
        "description": "SMT2020 fab 기본 정보. 제품군(A_long·B_mid·C_short)과 제품, 툴그룹 이름·장비 수, 기준 운영 KPI, "
                       "정적 가동률 상위 툴그룹, 조정 가능한 항목과 범위. 툴그룹·제품 이름이 필요하거나 기준 수치가 필요할 때 먼저 부른다.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "build_scenario",
        "description": "사용자 요청을 시나리오로 만들고 정적 사전 점검까지 한다. 투입(전체 %, 제품군 배율, 제품 배율, hot·superhot 투입 배율), 장비 정지, "
                       "고장 특성(수리 시간·고장 간격 배율), regular → hot 전환, 장비 수 증감, 가공 시간 배율, "
                       "PM 주기·소요 시간 배율, 디스패칭 순서(베타: 3순위 도착 순서, setup 툴그룹의 CR·setup 순서)를 조합할 수 있다. "
                       "결과의 scenario_id 로 run_experiment·get_report 를 부른다.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "요청을 한 줄로 요약 (보고서 제목에 쓴다)"},
                "total_release_pct": {"type": "number",
                                      "description": "전체 regular 투입 증감 %. 예: 2 → +2%, -5 → -5%. hot·superhot 은 바뀌지 않는다"},
                "group_mult": {
                    "type": "object", "description": "제품군별 regular 투입 배율. 예: {\"A_long\": 1.1}. 적지 않은 군은 1.0",
                    "properties": {"A_long": {"type": "number"}, "B_mid": {"type": "number"}, "C_short": {"type": "number"}},
                },
                "product_mult": {
                    "type": "array", "description": "제품별 regular 투입 배율 (제품군 배율을 덮어쓴다)",
                    "items": {"type": "object", "properties": {"product": {"type": "string", "description": "예: Product_3"},
                                                               "mult": {"type": "number"}},
                              "required": ["product", "mult"]},
                },
                "lot_type_mult": {
                    "type": "object",
                    "description": "hot·superhot 투입 배율 (기준 대비, 11-07). 0 → 그 유형 투입 중단(제외·없앰), 0.5 → 절반, 2 → 두 배. "
                                   "0~5. regular 투입은 total_release_pct·group_mult·product_mult 로 바꾼다. 시작 주는 release_start_week",
                    "properties": {"hot": {"type": "number"}, "superhot": {"type": "number"}},
                },
                "release_start_week": {"type": "integer",
                                       "description": "투입 변경(regular·hot·superhot)을 시작하는 주. 0(기본)이면 처음부터 바뀐 투입으로 돌려 "
                                                      "정상상태를 비교한다"},
                "outages": {
                    "type": "array",
                    "description": "장비 정지 (고장·반출·장기 PM). duration_weeks 를 생략하거나 정지가 실행 끝까지 닿으면 "
                                   "영구 정지(그 주부터 끝까지, 장비 수 감축과 같음)로 처리한다. 기간이 있는 정지는 끝난 뒤 "
                                   "회복을 보도록 실행이 정지 끝 + 8주까지 늘어난다",
                    "items": {"type": "object",
                              "properties": {"toolgroup": {"type": "string"}, "n_tools": {"type": "integer"},
                                             "start_week": {"type": "integer",
                                                            "description": "사용자가 말한 시작 주만. 말하지 않았으면 빼고 부른다 (도구 기본 30주, 시작 상태면 그 주)"},
                                             "duration_weeks": {"type": "number", "description": "정지 기간(주). 생략하면 끝까지(영구)"}},
                              "required": ["toolgroup", "n_tools"]},
                },
                "breakdown_changes": {
                    "type": "array", "description": "고장 특성 변경 (노후화·수리 지연·보전 개선). area 와 toolgroup 중 하나만 준다",
                    "items": {"type": "object",
                              "properties": {"area": {"type": "string"}, "toolgroup": {"type": "string"},
                                             "mttr_mult": {"type": "number", "description": "수리 시간 배율. 2 → 두 배로 길어짐"},
                                             "mttf_mult": {"type": "number", "description": "고장 간격 배율. 0.5 → 두 배 자주 고장"},
                                             "start_week": {"type": "integer",
                                                            "description": "사용자가 말한 시작 주만. 말하지 않았으면 빼고 부른다 (도구 기본 30주, 시작 상태면 그 주)"},
                                             "end_week": {"type": "integer"}}},
                },
                "hot_conversion": {
                    "type": "array",
                    "description": "regular → hot 전환. 기간 동안 대상 제품 regular 투입 lot 중 share 비율을 hot(우선순위 20)으로 낸다. "
                                   "투입 총량은 그대로. 제품 하나만 지정할 수 있다 (products 에 그 제품만). "
                                   "products·groups 를 둘 다 생략하면 모든 제품. 결과에 기간 중 예상 전환 lot 수(약)와 "
                                   "hot 비중(기준 → 변경)을 도구가 계산해 돌려준다 (투입 변경 구간 반영)",
                    "items": {"type": "object",
                              "properties": {"share": {"type": "number",
                                                       "description": "전환 비율 0~1. 예: 0.1 → 10개 중 1개. '전부·모두·전량' 이면 1"},
                                             "products": {"type": "array", "items": {"type": "string"},
                                                          "description": "대상 제품. 하나만도 가능. 예: [\"Product_4\"] 는 Product_4 만 (제품군으로 넓히지 않음)"},
                                             "groups": {"type": "array", "items": {"type": "string"}, "description": "예: [\"A_long\"]"},
                                             "start_week": {"type": "integer"},
                                             "end_week": {"type": "integer", "description": "생략하면 실행 끝까지. duration_days 와 함께 쓰지 않는다"},
                                             "duration_days": {"type": "number",
                                                               "description": "기간을 일 단위로 줄 때 (end_week 대신). 예: 3 → 시작 주부터 3일 동안 "
                                                                              "투입되는 lot. 주 환산은 도구가 한다"}},
                              "required": ["share", "start_week"]},
                },
                "tool_changes": {
                    "type": "array",
                    "description": "장비 수 증감 (증설·반출). delta +1 → 1대 증설, -2 → 2대 감축 (1대 이상 남긴다). "
                                   "start_week 0(기본)이면 처음부터 바뀐 대수로 정상상태 비교. 0보다 크면 증설 장비는 그 주부터 가동, "
                                   "감축 장비는 그 주부터 끝까지 정지",
                    "items": {"type": "object",
                              "properties": {"toolgroup": {"type": "string"}, "delta": {"type": "integer"},
                                             "start_week": {"type": "integer"}},
                              "required": ["toolgroup", "delta"]},
                },
                "processing_time": {
                    "type": "array",
                    "description": "가공 시간 변경 (실행 처음부터). 시간 표현은 mult (0.9 → 가공 시간 10% 감소, 1.2 → 20% 증가), "
                                   "속도 표현은 speed_mult (1.12 → 속도 12% 빨라짐, 0.9 → 10% 느려짐)를 준다. speed_mult 는 도구가 "
                                   "가공 시간 배율(1/speed_mult)로 바꾼다. 둘 중 하나만. area 와 toolgroup 중 하나만 준다 "
                                   "(area 전체는 area 인자 하나로, 예: {\"area\": \"Wet_Etch\"}. toolgroup 에 area 이름을 넣지 않음). 범위 0.1~10",
                    "items": {"type": "object",
                              "properties": {"toolgroup": {"type": "string"}, "area": {"type": "string"},
                                             "mult": {"type": "number", "description": "가공 시간 배율 (시간 표현)"},
                                             "speed_mult": {"type": "number", "description": "가공 속도 배율 (속도 표현). 역수는 도구가 계산"}}},
                },
                "pm_changes": {
                    "type": "array",
                    "description": "PM 변경 (실행 처음부터). 간격·주기 표현은 interval_mult (1.2 → 간격 20% 길어짐, 0.9 → 10% 짧아짐), "
                                   "빈도 표현은 frequency_mult (1.1 → 10% 더 자주, 0.75 → 25% 덜 자주)를 준다. frequency_mult 는 도구가 "
                                   "간격 배율(1/frequency_mult)로 바꾼다. 둘 중 하나만. duration_mult 0.8 → PM 한 번이 20% 짧아짐. "
                                   "area 와 toolgroup 중 하나만 준다. 범위 0.1~10",
                    "items": {"type": "object",
                              "properties": {"toolgroup": {"type": "string"}, "area": {"type": "string"},
                                             "interval_mult": {"type": "number", "description": "PM 간격 배율 (간격·주기 표현)"},
                                             "frequency_mult": {"type": "number", "description": "PM 빈도 배율 (빈도 표현). 역수는 도구가 계산"},
                                             "duration_mult": {"type": "number"}}},
                },
                "dispatch_changes": {
                    "type": "array",
                    "description": "디스패칭 순서 변경 (베타, 11-09). 지금 규칙은 모든 툴그룹이 1순위 lot 우선순위(superhot > hot > regular) → "
                                   "2순위 setup 시간 적은 것 → 3순위 critical ratio(납기 여유 ÷ 남은 작업) 낮은 것 → 동점은 도착 순서. "
                                   "1순위는 바꾸지 않는다. 바꿀 수 있는 것은 둘: rank3 'fifo' → 3순위를 critical ratio 대신 도착 순서로, "
                                   "cr_before_setup true → setup 을 쓰는 툴그룹(get_fab_info 의 setup_toolgroups)에서 critical ratio 를 "
                                   "setup 시간보다 먼저 본다. 한 항목에 둘 중 하나만. area 와 toolgroup 중 하나만 준다. "
                                   "새 규칙(EDD·SPT 등)이나 1순위 변경은 지원하지 않는다",
                    "items": {"type": "object",
                              "properties": {"toolgroup": {"type": "string"}, "area": {"type": "string"},
                                             "rank3": {"type": "string", "enum": ["fifo"],
                                                       "description": "'fifo' = 3순위를 도착 순서로 (선입선출)"},
                                             "cr_before_setup": {"type": "boolean",
                                                                 "description": "true = critical ratio 를 setup 시간보다 먼저 (setup 툴그룹만)"},
                                             "start_week": {"type": "integer",
                                                            "description": "사용자가 말한 시작 주만. 생략하면 처음부터 (시작 상태면 그 주)"},
                                             "end_week": {"type": "integer", "description": "생략하면 실행 끝까지"}}},
                },
                "start_state": {
                    "type": "string", "enum": list(STATES),
                    "description": "고정 시작 상태에서 갈라지기(분기). 보통(34주)·혼잡(40주)·여유(32주) 중 하나. 주면 그 주의 같은 fab "
                                   "상태에서 seed 5개 갈래로 앞으로 22주를 본다. 모든 변경의 시작 주는 그 상태의 주 이상이어야 하고 "
                                   "(투입 변경 시작 주를 안 주면 그 주), 장비 증설·가공 시간·PM 변경은 함께 못 쓴다. "
                                   "생략하면 seed 마다 빈 fab 에서 시작하는 기본 방식"},
                "end_week": {"type": "integer",
                             "description": "실행 길이(끝 주), 주 단위. 기본 52 (1년). 년으로 말하면 1년 = 52주로 환산 (2년 = 104주). "
                                            "52주보다 길면 같은 길이 기준 운영도 함께 돌린다"},
                "warmup_weeks": {"type": "integer",
                                 "description": "warm-up(결과에서 빼는 앞 기간), 주 단위. 기본 26. 측정 구간은 warm-up 끝부터 실행 끝까지 "
                                                "(변경이 더 늦게 시작하면 그 주부터). 예: warm-up 52주, 전체 104주 → 52~104주 측정"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "request_approval",
        "description": "만든 시나리오를 사용자에게 '확인 후 실행' 카드로 보여 실행 승인을 요청한다 (11-06). 카드에는 도구가 계산한 실제 "
                       "설정·측정 구간·정적 점검·주의 사항과, 사용자가 말하지 않은 값이 표시된다. 사용자가 버튼을 눌러야만 실행할 수 있다. "
                       "비교 요청이면 scenario_id = 대상 안, compare_to = 기준 안 (보고서의 차이는 대상 − 기준). "
                       "부른 뒤에는 카드 요약을 한두 줄로 안내하고 멈춘다 (run_experiment 를 부르지 않는다).",
        "parameters": {"type": "object",
                       "properties": {"scenario_id": {"type": "string", "description": "실행할 시나리오 (비교면 대상 안)"},
                                      "compare_to": {"type": "string", "description": "비교의 기준 안 (단독 실행이면 생략)"},
                                      "note": {"type": "string", "description": "카드에 함께 보일 한 줄 안내 (선택)"},
                                      "seeds": {"type": "array", "items": {"type": "integer"},
                                                "description": "실행할 seed (기본 [1,2,3,4,5]). 사용자가 seed 를 정했을 때만 준다 "
                                                               "(예: 검증용 seed 1 하나 → [1]). 카드에 그대로 보이고 승인 뒤 이 seed 로만 실행된다"}},
                       "required": ["scenario_id"]},
    },
    {
        "name": "run_experiment",
        "description": "시나리오를 DES 로 seed 여러 개 실행한다. status: ready(기존 실행 재사용, 바로 get_report), "
                       "started(백그라운드 실행 시작, 수십 분), running(이미 실행 중), busy(다른 실행 중), "
                       "needs_run(실행 권한 없음, 사용자가 돌릴 명령을 돌려줌), rejected.",
        "parameters": {"type": "object",
                       "properties": {"scenario_id": {"type": "string"},
                                      "seeds": {"type": "array", "items": {"type": "integer"},
                                                "description": "승인한 카드의 seed 그대로 ('[승인]' 메시지에 적힘, 기본 [1,2,3,4,5]). "
                                                               "다른 seed 로 돌리려면 새 카드로 다시 승인받는다"}},
                       "required": ["scenario_id"]},
    },
    {
        "name": "get_report",
        "description": "실행이 준비된 시나리오의 보고서 요약. 비교 대상(기본: 같은 seed 기준 운영, compare_to 를 주면 그 시나리오)과의 "
                       "짝 차이 ± 95% CI 와 판정 (WIP, regular 처리량·CT·제품군별, 처리량 전체, hot 처리량·CT, 납기 준수 regular·hot %; "
                       "hot+superhot 은 둘을 합친 값), lot 유형별(hot만·superhot)·regular 제품군별 납기·CT 짝 비교, "
                       "비교 대상·시나리오 각각의 값, 가동률 상위 5(시나리오·비교 대상), 가동률이 가장 많이 바뀐 툴그룹, 정상상태 설명. "
                       "\"A 보다 B 가 얼마나\" 같은 두 시나리오 비교는 compare_to 로 직접 짝 비교한다. "
                       "보고서의 모든 수치는 여기서만 가져온다.",
        "parameters": {"type": "object",
                       "properties": {"scenario_id": {"type": "string"},
                                      "compare_to": {"type": "string",
                                                     "description": "비교 대상 시나리오의 scenario_id. 생략하면 기준 운영과 비교. "
                                                                    "그 시나리오도 실행 결과가 있어야 한다"},
                                      "window_start_week": {"type": "integer", "description": "측정 창 시작 주 (생략하면 시나리오 기본값)"},
                                      "window_end_week": {"type": "integer", "description": "측정 창 끝 주 (생략하면 시나리오 기본값)"}},
                       "required": ["scenario_id"]},
    },
    {
        "name": "get_toolgroup_stats",
        "description": "툴그룹별 가동률·가용률·고장·PM 비율·대기 lot 조회 (시뮬레이션 실행 없음, 저장된 결과를 읽음). "
                       "scenario_id 를 생략하면 기준 운영(seed 1~5, 26~52주), 주면 그 시나리오의 실행 결과와 비교 대상(기준 운영 또는 "
                       "compare_to)의 차이 ± 95% CI. 병목·가동률 상위·특정 툴그룹·area 별 질문에 쓴다. "
                       "가동률 = 쓸 수 있는 시간 중 일한 비율, 가용률 = 고장·PM 을 뺀 쓸 수 있는 시간 비율. 수치는 여기서 그대로 옮긴다.",
        "parameters": {"type": "object",
                       "properties": {"scenario_id": {"type": "string", "description": "생략하면 기준 운영. 실행 결과가 있는 시나리오만"},
                                      "compare_to": {"type": "string", "description": "비교 대상 시나리오 (생략하면 기준 운영)"},
                                      "toolgroups": {"type": "array", "items": {"type": "string"},
                                                     "description": "특정 툴그룹만. 예: [\"WE_FE_84\"]"},
                                      "area": {"type": "string", "description": "이 area 의 툴그룹만. 예: \"Litho\""},
                                      "sort_by": {"type": "string",
                                                  "description": "정렬 기준: 가동률(기본) | 대기 | 가용률 | 고장 | PM | 가동률 변화 (시나리오일 때)"},
                                      "ascending": {"type": "boolean", "description": "낮은 순이면 true (기본 높은 순)"},
                                      "top_n": {"type": "integer", "description": "몇 개 보일지 (기본 10, 최대 105)"}}},
    },
    {
        "name": "get_kpi",
        "description": "기준 운영(scenario_id 생략, seed 1~5, 26~52주) 또는 이미 실행 결과가 있는 시나리오의 KPI 값 조회 (시뮬레이션 실행 없음, "
                       "저장된 결과를 읽음). WIP, 처리량·CT (regular 전체·제품군별, hot만, superhot, hot+superhot), 납기 준수율 (lot 유형별 "
                       "regular·hot만·superhot·hot+superhot, regular 제품군별), 완료 lot 수. seed 평균 ± 95% CI. \"기본 상태의 납기 준수율\", "
                       "\"기준 운영 CT\" 처럼 값만 묻는 질문에 쓴다 (카드·버튼·실행 필요 없음). 두 운영의 차이는 get_report. 수치는 여기서 그대로 옮긴다.",
        "parameters": {"type": "object",
                       "properties": {"scenario_id": {"type": "string", "description": "생략하면 기준 운영. 실행 결과가 있는 시나리오만"},
                                      "window_start_week": {"type": "integer", "description": "측정 창 시작 주 (생략하면 기본값)"},
                                      "window_end_week": {"type": "integer", "description": "측정 창 끝 주 (생략하면 기본값)"}}},
    },
]


TOOL_REGISTRY = ToolRegistry.from_specs(TOOL_SPECS)


def _r(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return x
    return None if x != x else round(x, nd)


def canonical(spec: dict) -> str:
    """같은 시나리오인지 비교하는 키. 이름·seed·끝 주·확장·상한은 빼고 투입 구간과 다운타임만 본다."""
    phases = []
    for ph in sorted(spec["phases"], key=lambda p: float(p["start_week"])):
        d = {"start_week": float(ph["start_week"])}
        for k in ("group_mult", "group_wspw", "product_mult"):
            if ph.get(k):
                d[k] = {g: round(float(v), 4) for g, v in sorted(ph[k].items())}
        ltm = {k: round(float(v), 4) for k, v in sorted((ph.get("lot_type_mult") or {}).items()) if abs(float(v) - 1) > 1e-9}
        if ltm:                                                       # hot·superhot 투입 배율 (11-07). 1 이면 키를 넣지 않아 기존 실행과 같은 키
            d["lot_type_mult"] = ltm
        phases.append(d)
    dt = [{k: (round(float(v), 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
           for k, v in sorted(e.items())} for e in spec.get("downtime", []) or []]
    key = {"grouping": spec.get("grouping") or GROUPING, "phases": phases, "downtime": dt}
    hot = [{"share": round(float(h["share"]), 4), "start_week": float(h.get("start_week", 0)),
            "end_week": float(h["end_week"]) if h.get("end_week") is not None else None,
            "products": sorted(h.get("products") or []), "groups": sorted(h.get("groups") or [])}
           for h in spec.get("hot_conversion", []) or []]
    if hot:                                                           # 없으면 키를 넣지 않아 기존 실행과 같은 키가 된다
        key["hot_conversion"] = hot
    ov = spec.get("overrides") or {}
    if ov:                                                            # 장비 수·가공 시간·PM (11-04). 없으면 키를 넣지 않는다
        num = (int, float)
        key["overrides"] = {k: sorted(json.dumps({kk: (round(float(vv), 4) if isinstance(vv, num) and not isinstance(vv, bool)
                                                       else vv) for kk, vv in sorted(e.items())}, sort_keys=True,
                                                     ensure_ascii=False) for e in v)
                            for k, v in sorted(ov.items()) if v}
    if spec.get("branch"):                                            # 분기: 시작 상태(seed, 주)만 키에 넣는다 (갈래 번호는 실행별)
        key["branch"] = {"state_seed": int(spec["seed"]), "week": float(spec["branch"]["week"])}
    disp = [{"targets": sorted(d.get("toolgroups") or ([d["toolgroup"]] if d.get("toolgroup") else []) + ([f"area:{d['area']}"] if d.get("area") else [])),
             "rank3": d.get("rank3"), "cr_before_setup": bool(d.get("cr_before_setup")),
             "start_week": float(d.get("start_week", 0)), "end_week": float(d["end_week"]) if d.get("end_week") is not None else None}
            for d in spec.get("dispatch", []) or []]
    if disp:                                                          # 디스패칭 (베타, 11-09). 없으면 키를 넣지 않아 기존 실행과 같은 키
        key["dispatch"] = sorted(disp, key=lambda d: json.dumps(d, sort_keys=True))
    return json.dumps(key, sort_keys=True, ensure_ascii=False)


class Toolbox:
    """도구 묶음. state_dir 에 시나리오 기록, results_dir 에 새 실행·보고서를 둔다.

    background=True (대시보드): 새 DES 를 백그라운드 작업(run_job.py)으로 띄우고 바로 돌아온다. 끝났는지는
    job_status() 로 화면이 확인한다 — LLM 을 부르지 않으므로 기다리는 동안 토큰을 쓰지 않는다.
    background=False (터미널): 끝날 때까지 기다린다 (진행 로그가 터미널에 나온다).
    """

    def __init__(self, state_dir: Path, results_dir: Path, *, allow_run: bool = False, background: bool = False, log=print, registry: ToolRegistry | None = None):
        self.state_dir = Path(state_dir)
        self.results_dir = Path(results_dir)
        self.allow_run = allow_run
        self.background = background
        self.log = log
        self.tool_registry = registry if registry is not None else TOOL_REGISTRY.clone()
        self._data = None
        (self.state_dir / "scenarios").mkdir(parents=True, exist_ok=True)

    # -- 공통 ----------------------------------------------------------------------------------------
    @property
    def tool_specs(self) -> list:
        return getattr(self, "tool_registry", TOOL_REGISTRY).specs()

    def call(self, name: str, args: dict) -> dict:
        try:
            fn = getattr(self, "tool_registry", TOOL_REGISTRY).resolve(self, name)
            if fn is None:
                return {"error": f"없는 도구: {name}"}
            return fn(**(args or {}))
        except TypeError as e:
            return {"error": f"인자 오류: {e}"}
        except Exception as e:                                         # 도구 오류는 LLM 에 그대로 돌려준다
            return {"error": f"{type(e).__name__}: {e}"}

    def data(self):
        if self._data is None:
            from smt2020_sim.data_parser import parse_workbook
            with contextlib.redirect_stdout(io.StringIO()):
                self._data = parse_workbook(release_mode="parametric", initial_wip=False)
        return self._data

    def _record_path(self, sid: str) -> Path:
        return self.state_dir / "scenarios" / sid / "scenario.json"

    def _load(self, sid: str) -> dict:
        p = self._record_path(sid)
        if not p.exists():
            raise ValueError(f"없는 scenario_id: {sid} (build_scenario 를 먼저 부른다)")
        return json.loads(p.read_text(encoding="utf-8"))

    # -- get_fab_info -------------------------------------------------------------------------------
    def get_fab_info(self) -> dict:
        cache = self.state_dir / "fab_info_v8.json"                     # v2 hot 전환, v3 장비 수·가공·PM (11-04), v4 분기, v5 영구 정지, v6 속도·빈도 배율, v7 hot 전부·일 단위 (11-05), v8 디스패칭 베타 (11-09)
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        import scenario as sc
        import static_load as sl
        data = self.data()
        _, groups = sc.load_groups(GROUPING)
        base = {"start_week": 0, "group_mult": {g: 1.0 for g in groups}}
        _, lpw = sc.phase_lots_per_week(data, groups, base)
        static = sl.evaluate(sl.build(data), lpw)
        top = [(t, round(100 * u, 1)) for t, u in sl.ranking(static, 10)]
        tgs = {n: s for n, s in data.toolgroups.items() if not s.is_delay}
        info = {
            "product_groups": groups,
            "toolgroups": {n: {"area": s.area, "tools": s.num_tools} for n, s in sorted(tgs.items())},
            "areas": sorted({s.area for s in tgs.values()}),
            "totals": {"toolgroups": len(tgs), "tools": sum(s.num_tools for s in tgs.values())},
            "release": "regular: 제품 10개가 각각 4.3시간마다 1 lot (주당 390 lot). hot: 제품마다 주 1 lot. "
                       "superhot: Product_3 20일마다 1 lot. 총 주당 400 lot",
            "static_util_top10_pct": top,
            "setup_toolgroups": list(data.setup_bearing_toolgroups()),   # 11-09: 디스패칭 베타의 cr_before_setup 대상
            "dispatching": "모든 툴그룹 같은 순서 (SMT2020 dataset 2): 1순위 lot 우선순위 superhot(30) > hot(20) > regular(10) → "
                           "2순위 setup 시간 적은 것 (setup_toolgroups 만 의미 있음) → 3순위 critical ratio 낮은 것 → 동점은 도착 순서. "
                           "그 밖에 wake-up(setup 적은 장비 고르기), Implant setup 회피(같은 구성 최소 7 lot), superhot 다음 장비 예약",
            "base_kpi_26_52w": self._base_kpi(),
            "knobs": {
                "투입": "전체 % / 제품군 배율 / 제품 배율 (regular), 시작 주 지정 가능. 제품 배율 0.5~2.0",
                "lot 유형별 투입": "hot·superhot 투입 배율 0~5 (lot_type_mult). 0 = 그 유형 투입 중단(제외), 0.5 = 절반, 2 = 두 배. "
                                 "시작 주는 투입 변경과 같음. 엑셀은 그대로 두고 실행할 때 투입 일정만 바꿈 (11-07)",
                "장비 정지": "툴그룹 n대, 시작 주·기간(주). 기간이 없거나 실행 끝까지 닿으면 영구 정지(장비 수 감축과 같음)",
                "고장 특성": "area 또는 툴그룹의 수리 시간(MTTR)·고장 간격(MTTF) 배율, 시작·끝 주",
                "hot 전환": "기간별로 제품(하나도 가능) 또는 제품군의 regular 투입 lot 일정 비율을 hot 으로 "
                            "(투입 총량은 그대로, 전환 lot 은 hot 납기 여유). '전부'면 100%, 기간은 주 또는 일 단위. "
                            "도구가 기간 중 예상 전환 lot 수(약)와 hot 비중을 계산해 보여 줌",
                "장비 수": "툴그룹 장비 증설(+)·감축(-), 1대 이상 남김. 처음부터 또는 지정 주부터",
                "가공 시간": "툴그룹 또는 area 의 가공 시간 배율 0.1~10 (속도 표현은 속도 배율로 주면 도구가 환산). 실행 처음부터",
                "PM": "툴그룹 또는 area 의 PM 간격·소요 시간 배율 0.1~10 (빈도 표현은 빈도 배율로 주면 도구가 환산, "
                      "모든 툴그룹에 PM 있음). 실행 처음부터",
                "정적 상한": f"정적 가동률 {100 * LIMITS['max_static_util']:.0f}% 초과면 실행 거부. "
                             f"기본 기준 {100 * DEFAULT_LIMIT:.0f}% 초과는 경고 (파일럿에서 정적 100.5%까지 정상상태 확인). "
                             "장비 정지·지정 주부터의 감축은 경고만 (정지 기간 동안 WIP 가 쌓임)",
                "시작 상태 (분기)": "보통(seed 2·34주), 혼잡(seed 3·40주), 여유(seed 3·32주) 중 하나의 같은 fab 상태에서 "
                                   "갈래 5개로 22주 앞을 본다. 같은 시작 상태의 기준 갈래와 짝 비교. 변경은 그 주 이후 시작, "
                                   "장비 증설·가공 시간·PM 은 함께 못 씀. 처음엔 기준 갈래도 같이 돌아 실행 10개",
                "디스패칭 (베타)": "툴그룹 또는 area 의 순서만 바꿈 (1순위 lot 우선순위는 그대로): 3순위 critical ratio → 도착 순서(FIFO), "
                                   "또는 setup_toolgroups 에서 critical ratio 를 setup 시간보다 먼저. 시작·끝 주 지정 가능 (11-09)",
                "아직 안 되는 것": "이미 fab 안에 있는 lot 승격(기존 lot 의 우선순위 변경은 별도 구현 필요), "
                                   "hot 전환 lot 개수의 정확한 지정(예: 다음 20개), 새 디스패칭 규칙(EDD·SPT 등)·1순위 순서 변경, "
                                   "가공 시간·PM 변경의 중간 시작(처음부터만), 새 툴그룹·공정 경로 변경, 배치 크기·셋업",
            },
            "experiment": f"기본: seed 1~5, {DEFAULT_END_WEEK}주(1년) 실행, warm-up {WARMUP_WEEK}주(측정 {WARMUP_WEEK}~{DEFAULT_END_WEEK}주). "
                          "실행 길이(end_week)·warm-up(warmup_weeks)은 주 단위로 바꿀 수 있음 (1년 = 52주). 52주보다 길면 같은 길이 기준 운영을 "
                          "함께 돌림. 같은 seed 기준 운영과 짝 비교. "
                          f"분기(start_state): 고정 시작 상태에서 갈래 1~5, 분기 뒤 {BRANCH_HORIZON}주",
        }
        cache.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        return info

    def _base_kpi(self) -> dict:
        import whatif_report as wr
        runs = [wr.load_run(d, WARMUP_WEEK, DEFAULT_END_WEEK) for d in BASE_RUNS if (d / "run_meta.json").exists()]
        if not runs:
            return {}
        out = {}
        for k in ("WIP (lot)", "처리량 (lot/주)", "CT (일)"):
            m, _, hw = wr.ci([r["kpi"][k] for r in runs])
            out[k] = {"mean": _r(m), "ci": _r(hw)}
        import pandas as pd
        U = pd.concat([r["util"] for r in runs], axis=1).mean(axis=1).sort_values(ascending=False)
        out["가동률 상위 5 (%)"] = [(t, _r(100 * u, 1)) for t, u in U.head(5).items()]
        out["seeds"] = [r["seed"] for r in runs]
        return out

    # -- build_scenario -----------------------------------------------------------------------------
    def build_scenario(self, description: str, total_release_pct: float | None = None, group_mult: dict | None = None,
                       product_mult: list | None = None, release_start_week: int = 0, outages: list | None = None,
                       breakdown_changes: list | None = None, hot_conversion: list | None = None,
                       tool_changes: list | None = None, processing_time: list | None = None,
                       pm_changes: list | None = None, start_state: str | None = None, end_week: int | None = None,
                       lot_type_mult: dict | None = None, warmup_weeks: int | None = None,
                       dispatch_changes: list | None = None) -> dict:
        import scenario as sc
        _, groups = sc.load_groups(GROUPING)
        ltm = {}
        for k, v in (lot_type_mult or {}).items():                     # 11-07: hot·superhot 투입 배율
            if k not in ("hot", "superhot"):
                return {"error": f"lot_type_mult 는 hot·superhot 만 ({k!r}). regular 투입은 total_release_pct·group_mult·product_mult"}
            try:
                v = float(v)
            except (TypeError, ValueError):
                return {"error": f"lot_type_mult[{k!r}] 는 숫자 ({v!r})"}
            if not 0 <= v <= 5:
                return {"error": f"lot_type_mult[{k!r}] 는 0~5 (0 = 투입 중단) ({v:g})"}
            if abs(v - 1) > 1e-9:
                ltm[k] = round(v, 4)
        if total_release_pct is not None and group_mult:
            return {"error": "total_release_pct 와 group_mult 는 함께 쓰지 않는다"}
        mult = {g: 1.0 for g in groups}
        if total_release_pct is not None:
            mult = {g: round(1.0 + float(total_release_pct) / 100.0, 4) for g in groups}
        for g, v in (group_mult or {}).items():
            if g not in groups:
                return {"error": f"없는 제품군: {g} (가능: {list(groups)})"}
            mult[g] = round(float(v), 4)
        pmult = {}
        products = {p for ps in groups.values() for p in ps}
        for it in product_mult or []:
            if it.get("product") not in products:
                return {"error": f"없는 제품: {it.get('product')} (가능: Product_1~Product_10)"}
            pmult[it["product"]] = round(float(it["mult"]), 4)
        changed = any(v != 1.0 for v in mult.values()) or bool(pmult) or bool(ltm)
        st = None
        if start_state:
            st = STATES.get(start_state)
            if st is None:
                return {"error": f"없는 시작 상태: {start_state} (가능: {list(STATES)})"}
        rs = int(release_start_week or 0)
        if st and changed:
            rs = rs or st["week"]                                      # 분기: 투입 변경은 기본으로 분기 주부터
            if rs < st["week"]:
                return {"error": f"시작 상태 '{start_state}'는 {st['week']}주라 투입 변경도 {st['week']}주 이후에 시작해야 한다 "
                                 f"(release_start_week {rs})"}
        base_phase = {"start_week": 0, "group_mult": {g: 1.0 for g in groups}}
        new_phase = {"start_week": rs, "group_mult": mult}
        if pmult:
            new_phase["product_mult"] = pmult
        if ltm:
            new_phase["lot_type_mult"] = ltm
        if not changed:
            phases = [base_phase]
        elif new_phase["start_week"] == 0:
            phases = [new_phase]
        else:
            phases = [base_phase, new_phase]

        downtime = []
        # 기간 없는 정지·실행 끝까지 닿는 정지 = 영구 정지 → 장비 수 감축(그 주부터 끝까지 OUTAGE, D-60)으로 바꾼다.
        # 정지로 두면 아래 "정지 끝 + 8주" 연장 때문에 장비가 다시 살아나 영구 정지가 되지 않았다 (11-05 평가 E47).
        req_end = st["week"] + BRANCH_HORIZON if st else int(end_week or DEFAULT_END_WEEK)
        ev0 = st["week"] if st else DEFAULT_EVENT_START                 # 11-08: 정지·고장 특성 시작 주 기본값 (생략하면 도구가 넣음, B5)
        perm, perm_notes = [], []
        for o in outages or []:
            s0 = int(o["start_week"]) if o.get("start_week") is not None else ev0
            n0, d0 = int(o["n_tools"]), o.get("duration_weeks")
            if d0 is None or s0 + float(d0) >= req_end:
                perm.append({"toolgroup": o["toolgroup"], "delta": -n0, "start_week": s0})
                perm_notes.append(f"{o['toolgroup']} {n0}대 {s0}주부터 정지는 " +
                                  ("기간이 없어" if d0 is None else f"실행 끝({req_end}주)까지 닿아") +
                                  " 영구 정지(장비 수 감축)로 처리")
                continue
            downtime.append({"type": "outage", "toolgroup": o["toolgroup"], "n_tools": n0,
                             "start_week": s0, "duration_weeks": float(d0)})
        for b in breakdown_changes or []:
            e = {"type": "breakdown_scale"}
            for key in ("area", "toolgroup"):
                if b.get(key):
                    e[key] = b[key]
            if b.get("mttr_mult") not in (None, 1, 1.0):
                e["mttr"] = float(b["mttr_mult"])
            if b.get("mttf_mult") not in (None, 1, 1.0):
                e["mttf"] = float(b["mttf_mult"])
            e["start_week"] = int(b["start_week"]) if b.get("start_week") is not None else ev0
            if b.get("end_week") is not None:
                e["end_week"] = int(b["end_week"])
            downtime.append(e)
        hot = []
        for h in hot_conversion or []:
            r = {"share": float(h["share"]), "start_week": int(h.get("start_week", 0))}
            if h.get("duration_days") is not None:                      # 일 단위 기간 (11-05 밤): 주 환산은 도구가 (소수 넷째 자리)
                if h.get("end_week") is not None:
                    return {"error": "hot_conversion 의 end_week 와 duration_days 는 함께 쓰지 않는다"}
                d = float(h["duration_days"])
                if d <= 0:
                    return {"error": f"hot_conversion 의 duration_days 는 0 보다 커야 한다 ({h['duration_days']})"}
                r["end_week"] = round(r["start_week"] + d / 7.0, 6)        # 소수 6자리 (3일 → 0.428571주, 오차 1초 미만)
            elif h.get("end_week") is not None:
                r["end_week"] = int(h["end_week"])
            for key in ("products", "groups"):
                if h.get(key):
                    r[key] = list(h[key])
            hot.append(r)
        overrides, err = self._overrides(list(tool_changes or []) + perm, processing_time, pm_changes)
        if err:
            return {"error": err}
        dispatch = []                                                   # 11-09: 디스패칭 순서 (베타). 형식·대상 검사는 scenario.py
        for d in dispatch_changes or []:
            e = {k: d[k] for k in ("toolgroup", "area") if d.get(k)}
            if d.get("rank3") is not None:
                e["rank3"] = d["rank3"]
            if d.get("cr_before_setup"):
                e["cr_before_setup"] = True
            e["start_week"] = int(d["start_week"]) if d.get("start_week") is not None else (st["week"] if st else 0)
            if d.get("end_week") is not None:
                e["end_week"] = int(d["end_week"])
            dispatch.append(e)
        if not changed and not downtime and not hot and not overrides and not dispatch:
            return {"error": "바뀐 것이 없다. 투입(regular·hot·superhot)·장비 정지·고장 특성·hot 전환·장비 수·가공 시간·PM·"
                             "디스패칭 순서 중 하나 이상을 준다"}

        tool_starts = [t.get("start_week", 0) for t in overrides.get("tools", []) if t.get("start_week", 0) > 0]
        ends = [d["start_week"] + d.get("duration_weeks", 0) for d in downtime if d["type"] == "outage"] \
            + [s for s in tool_starts]
        starts = [d["start_week"] for d in downtime] + ([new_phase["start_week"]] if changed and new_phase["start_week"] > 0 else []) \
            + [h["start_week"] for h in hot if h["start_week"] > 0] + tool_starts \
            + [d["start_week"] for d in dispatch if d["start_week"] > 0]  # 11-09: 디스패칭 중간 시작도 변경 시작
        note_w = None
        warm = WARMUP_WEEK                                              # 11-07: warm-up(앞 제외 기간)·변경 시작·측정 구간을 따로 둔다
        if warmup_weeks is not None:
            if st:
                return {"error": "시작 상태(분기) 실험은 warm-up 을 따로 정하지 않는다 (분기 주부터 측정)"}
            warm = int(round(float(warmup_weeks)))
            if warm < 0:
                return {"error": f"warmup_weeks 는 0 이상 ({warmup_weeks})"}
        change_start = (st["week"] if st else (min(starts) if starts else 0))
        if st:                                                          # 분기: 분기 주부터 정해진 길이만 본다
            end = st["week"] + BRANCH_HORIZON
            if max(ends, default=0) > end - 4:
                return {"error": f"분기 뒤 {BRANCH_HORIZON}주({end}주) 안에 정지가 끝나야 회복을 볼 수 있다. 기간을 줄인다"}
            w0 = st["week"]
        else:
            end = int(end_week or DEFAULT_END_WEEK)
            end = max(end, int(max(ends, default=0)) + 8)               # 정지 뒤 회복을 볼 여유
            if warmup_weeks is not None and end - warm < 4:
                return {"error": f"warm-up {warm}주가 실행 길이 {end}주에 비해 길어 측정 구간이 없다(4주 미만). 실행 길이(end_week)를 "
                                 "함께 정한다 (예: warm-up 52주, 전체 104주)"}
            w0 = max(warm, min(starts)) if starts else warm
            if min(starts, default=warm) < warm:
                note_w = f"변경이 warm-up({warm}주) 전에 시작한다. 측정은 {warm}주부터"
            if warmup_weeks is not None and warm < BASE_MSER_WEEK:
                note_w = (note_w + ". " if note_w else "") + (f"warm-up {warm}주가 기준 운영 안정화 지점(약 {BASE_MSER_WEEK}주)보다 짧아 "
                                                               "초기 과도 구간이 측정에 섞일 수 있음")
            if end_week and end != int(end_week):                       # 11-07: 요청한 끝 주를 바꿨으면 숨기지 않는다
                note_w = (note_w + ". " if note_w else "") + f"요청한 실행 끝 주 {int(end_week)}주 → {end}주로 늘림 (최소 8주 또는 정지 끝 + 8주)"
            if end <= w0:
                note_w = (note_w + ". " if note_w else "") + (f"검증용 짧은 실행: 실행 {end}주가 측정 시작({w0}주)보다 짧아 측정 구간 없음 — "
                                                               "보고서 측정 대상 아님 (실행·기동 확인용)")
        spec = {"name": "tmp", "seed": st["seed"] if st else 1, "phases": phases, "grouping": GROUPING, "end_week": end,
                "label_start_week": warm, "limits": dict(LIMITS),
                "extensions": {"ic": False, "work_active": False}}
        if downtime:
            spec["downtime"] = downtime
        if hot:
            spec["hot_conversion"] = hot
        if overrides:
            spec["overrides"] = overrides
        if dispatch:
            spec["dispatch"] = dispatch
        if st:
            spec["branch"] = {"week": st["week"]}                      # 갈래 번호는 실행할 때 붙인다

        check_spec = dict(spec, branch={"week": st["week"], "seed": 1}) if st else spec
        plan, _, _ = sc.prepare(check_spec, self.data())             # overrides 를 반영한 정적 점검 (원본 data 는 그대로)
        result = {"description": description}
        if plan.errors:
            out = {**result, "status": "rejected", "errors": plan.errors}
            if st:
                out["hint"] = (f"시작 상태 '{start_state}'는 {st['week']}주다. 모든 변경은 {st['week']}주 이후에 시작해야 하고, "
                               "장비 증설·가공 시간·PM 변경은 분기와 함께 못 쓴다")
            return out
        key = canonical(spec)                                           # 실행 결과 재사용 키 (끝 주·warm-up 은 빼고 같은 실행을 찾음)
        if not st and (end != DEFAULT_END_WEEK or warm != WARMUP_WEEK):  # 11-07: 기간이 다르면 다른 시나리오 (카드 = 실행 조건)
            key += f"|end_week={end}|warmup={warm}"
        sid = "Q" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        problems = sum((p["problems"] for p in plan.phases), [])
        top = max(u for p in plan.phases for _, u in p["static_top5"])
        result.update({
            "scenario_id": sid,
            "status": "rejected" if problems else "ok",
            "phases": [{"start_week": p["start_week"], "total_wspw": p["total_wspw"],
                        "group_wspw_regular": p["group_wspw_regular"], "static_util_top5_pct": p["static_top5"],
                        "problems": p["problems"]} for p in plan.phases],
            "base_total_wspw": self._base_wspw(),
            "downtime_static": plan.downtime,
            "hot_conversion": self._hot_estimates(spec, plan.hot),
            "overrides": plan.overrides,
            **({"dispatch": plan.dispatch} if plan.dispatch else {}),   # 11-09: 디스패칭 (베타) 정규화 결과 (대상 툴그룹 목록)
            "window_weeks": [w0, end],
            "end_week": end,
            "warmup_week": None if st else warm,
            "change_start_week": change_start,
            "seeds": DEFAULT_SEEDS,
        })
        warns = []
        if top > 100 * DEFAULT_LIMIT and not problems:
            warns.append(f"정적 가동률 최고 {top:.1f}%로 기본 기준 {100 * DEFAULT_LIMIT:.0f}% 초과. 실행 거부 기준"
                         f"({100 * LIMITS['max_static_util']:.0f}%)보다는 낮아 경고만 하고 진행 "
                         "(파일럿에서 정적 100.5%까지 정상상태 확인, 그 위는 미확인)")
        if problems:
            warns.append(f"정적 가동률 상한 {100 * LIMITS['max_static_util']:.0f}% 또는 제품 배율 범위 위반 → 실행 거부. 투입을 줄인 대안을 제시한다")
        if note_w:
            warns.append(note_w)
        hots = result["hot_conversion"]
        if hots:
            peak = max(h["hot 비중 최고 % (모든 규칙 합산)"] for h in hots)
            base_share = hots[0]["hot 비중 % (기간 중, 기준 → 변경)"][0]
            if peak > HOT_SHARE_WARN_PCT:                               # 동시에 도는 규칙을 모두 합한 비중으로 판단 (11-06 검토)
                warns.append(f"hot 전환 기간 중 hot 비중 최고 약 {peak:.0f}% (모든 전환 규칙 합산, 기준 약 {base_share:.1f}%). "
                             "hot 이 많으면 우선순위 효과가 줄어 hot lot 끼리 줄을 서고 regular 는 더 기다림 "
                             "(사실상 대상 제품 우선 처리 정책에 가까움)")
            warns += self._hot_overlaps([(float(h["start_week"]), float(h["end_week"]), h["products"]) for h in hots])
        for h in hots:
            if h["end_week"] <= w0:
                warns.append(f"hot 전환 기간({h['start_week']:g}~{h['end_week']:g}주)이 측정 구간 시작({w0}주) 전에 끝남. 그 뒤로는 "
                             "새 전환이 없지만, 이미 hot 으로 투입된 lot 은 공정에 남아 측정 구간에도 영향을 줄 수 있음 "
                             "(기준 평균 CT 약 38일). 측정 구간에서 무엇을 볼지 사용자와 확인 (시작 주를 바꿀지는 사용자가 정함)")
        warns += perm_notes
        warns += [d["warning"] for d in plan.downtime if "warning" in d]
        for k, v in ltm.items():                                       # 11-07: lot 유형별 투입 배율
            if v == 0:
                warns.append(f"{k} 투입 중단: {rs}주부터 새 {k} lot 을 내지 않음 (그 전에 들어간 {k} lot 은 공정에 남아 끝까지 진행). "
                             f"측정 창에 완료된 {k} lot 이 없으면 {k} 납기 준수율은 '해당 없음'")
        if ltm:
            warns.append("lot 이 빠지거나 늘면 공정 중 난수(샘플링·재작업 등) 순서가 기준 실행과 달라진다. 같은 seed 끼리 짝 비교는 하지만 "
                         "regular 투입 변경과 같은 수준의 짝이다")
        if plan.dispatch:                                               # 11-09: 디스패칭 (베타)
            warns.append("디스패칭 순서 변경은 베타 기능이다. 1순위(lot 우선순위)·wake-up·setup 회피(최소 7 lot)·superhot 예약은 그대로이고, "
                         "바꾼 툴그룹에서 lot 고르기·배치 구성원·배치 그룹 순서에 같이 적용된다. 처리 순서가 바뀌면 그 뒤 난수 순서도 "
                         "기준 실행과 달라진다 (같은 seed 끼리 짝 비교는 함)")
        if not st and end > DEFAULT_END_WEEK:                           # 11-07: 52주보다 긴 실행은 같은 길이 기준 운영이 필요
            have = self._find_runs(self._base_rec(end, [w0, end]), DEFAULT_SEEDS)
            if len(have) < len(DEFAULT_SEEDS):
                warns.append(f"기준 운영 {end}주 결과가 없어(있는 seed {sorted(have)}) 실행할 때 기준 운영 seed 1~5 도 함께 돌림 "
                             f"(최대 10개 병렬, 약 {round(MIN_PER_WEEK_PARALLEL * end)}분 추정). 한 번 돌린 기준은 다음에 재사용")
        if st:
            result["start_state"] = {"이름": start_state, "기준 seed": st["seed"], "분기 주": st["week"], "갈래": DEFAULT_SEEDS}
            warns.append(f"분기 실행: 기준 seed {st['seed']} 의 {st['week']}주 상태에서 갈래 5개로 갈라져 {end}주까지 본다. "
                         "같은 시작 상태의 바뀐 것 없는 기준 갈래와 같은 갈래끼리 비교한다. 결과는 이 시작 상태 하나에 대한 답")
        result["warnings"] = warns
        rec = {"scenario_id": sid, "description": description, "spec": spec, "window_weeks": [w0, end],
               "warmup_week": None if st else warm, "change_start_week": change_start,
               "state": start_state if st else None,
               "check": result, "created": datetime.now().isoformat(timespec="seconds")}
        p = self._record_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return result

    def _hot_estimates(self, spec: dict, hot_plan: list) -> list:
        """hot 전환 규칙마다 기간 중 예상 전환 lot 수(약)와 hot 비중(기준 → 변경). 투입 변경 구간(phase)을 시간 가중으로 반영.

        11-06 검토 반영: ① hot 비중은 그 시점에 도는 모든 전환 규칙을 합산 ② 같은 제품·시점에 규칙이 겹치면 실행기
        (runner.ScheduledFab._to_hot)처럼 먼저 적힌 규칙만 그 lot 을 판정하므로 뒤 규칙의 전환량에서 뺀다.
        평균 투입률 × 기간 × 비율로 센 추정이라 제품마다 ±1 lot 정도 다를 수 있어 "약"으로 보인다 (정확한 개수는 별도 기능)."""
        if not hot_plan:
            return []
        import scenario as sc
        data = self.data()
        _, groups = sc.load_groups(GROUPING)
        is_reg = lambda s: s.priority == 10 and not s.is_superhot
        base = {s.product: s.lots_per_release / s.release_interval * sc.WEEK for s in data.release_streams if is_reg(s)}
        other = sum(s.lots_per_release / s.release_interval * sc.WEEK for s in data.release_streams if not is_reg(s))
        phases = sorted(spec["phases"], key=lambda p: float(p["start_week"]))
        end = float(spec["end_week"])
        segs = []
        for i, ph in enumerate(phases):
            a = float(ph["start_week"])
            b = float(phases[i + 1]["start_week"]) if i + 1 < len(phases) else end
            segs.append((a, b, sc.phase_lots_per_week(data, groups, ph)[0]))
        rules = [(float(h["start_week"]), min(float(h["end_week"]), end), list(h["products"]), float(h["share"])) for h in hot_plan]
        pts = sorted({a for a, _, _ in segs} | {b for _, b, _ in segs} | {r[0] for r in rules} | {r[1] for r in rules})
        lots = [{p: 0.0 for p in r[2]} for r in rules]                  # 규칙별·제품별 예상 전환 lot
        timeline = []                                                   # (구간 시작, 끝, 기준 hot %, 합산 hot %)
        for x0, x1 in zip(pts, pts[1:]):
            if x1 <= x0:
                continue
            mult = next((m for a, b, m in segs if a <= x0 < b), segs[-1][2])
            claimed, conv_lpw = set(), 0.0
            for i, (s0, s1, prods, share) in enumerate(rules):
                if s0 <= x0 and x1 <= s1:
                    for p in prods:
                        if p in claimed:                                # 앞 규칙이 이 제품의 lot 을 이미 판정
                            continue
                        lots[i][p] += base[p] * mult[p] * (x1 - x0) * share
                        conv_lpw += base[p] * mult[p] * share
                    claimed |= set(prods)
            total = sum(base[q] * mult[q] for q in base) + other
            timeline.append((x0, x1, 100 * other / total, 100 * (other + conv_lpw) / total))
        out = []
        for i, h in enumerate(hot_plan):
            s0, s1 = rules[i][0], rules[i][1]
            seg = [(x0, x1, b0, a0) for x0, x1, b0, a0 in timeline if s0 <= x0 and x1 <= s1]
            w = sum(x1 - x0 for x0, x1, _, _ in seg) or 1e-9
            before = sum((x1 - x0) * b0 for x0, x1, b0, _ in seg) / w
            after = sum((x1 - x0) * a0 for x0, x1, _, a0 in seg) / w
            peak = max((a0 for _, _, _, a0 in seg), default=after)
            conv = sum(lots[i].values())
            weeks = max(s1 - s0, 1e-9)
            out.append({"products": h["products"], "share": h["share"], "start_week": h["start_week"], "end_week": h["end_week"],
                        "기간": f"{s0:g}~{s1:g}주 (약 {7 * (s1 - s0):.1f}일)",
                        "converted_lots_per_week": round(conv / weeks, 1),
                        "예상 전환 lot (약, 기간 합계)": round(conv),
                        "제품별 예상 전환 lot (약)": {p: round(v, 1) for p, v in lots[i].items()},
                        "hot 비중 % (기간 중, 기준 → 변경)": [round(before, 1), round(after, 1)],
                        "hot 비중 최고 % (모든 규칙 합산)": round(peak, 1),
                        "참고": "평균 투입률로 센 추정. 실제 전환 수는 투입 시점에 따라 제품마다 ±1 lot 정도 다를 수 있음. "
                                "hot 비중은 그 기간에 도는 모든 전환 규칙을 합산"})
        return out

    @staticmethod
    def _hot_overlaps(rules: list) -> list:
        """같은 제품·시점에 겹치는 전환 규칙 경고. 실행기는 먼저 적힌 규칙만 그 lot 을 판정한다."""
        warns = []
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                a, b = rules[i], rules[j]
                lo, hi = max(a[0], b[0]), min(a[1], b[1])
                common = sorted(set(a[2]) & set(b[2]), key=lambda p: int(p.split("_")[1]))
                if lo < hi and common:
                    warns.append(f"hot 전환 규칙 {i + 1}·{j + 1} 이 같은 제품({', '.join(common)})·기간({lo:g}~{hi:g}주)에 겹침. "
                                 f"실행기는 먼저 적힌 규칙 {i + 1} 만 적용하므로 규칙 {j + 1} 은 그 구간에서 효과 없음 "
                                 "(예상 lot 도 같은 방식으로 셈). 의도와 맞는지 사용자와 확인")
        return warns

    @staticmethod
    def _overrides(tool_changes, processing_time, pm_changes) -> tuple[dict, str | None]:
        """Compatibility wrapper; conversion lives in sllmt_core.overrides."""
        return build_overrides(tool_changes, processing_time, pm_changes)

    def _base_wspw(self) -> float:
        import scenario as sc
        _, groups = sc.load_groups(GROUPING)
        spec = {"name": "base", "seed": 1, "end_week": 1, "grouping": GROUPING,
                "phases": [{"start_week": 0, "group_mult": {g: 1.0 for g in groups}}]}
        return sc.resolve(spec, self.data()).phases[0]["total_wspw"]

    # -- run_experiment -----------------------------------------------------------------------------
    def _index(self) -> dict:
        """canonical 키 → {seed: (실행 폴더, end_week)}. 기존 결과와 새 결과를 함께 찾는다."""
        idx = {}
        roots = [p for r in SEARCH_ROOTS for p in sorted(ROOT.glob(r))] + [self.results_dir]
        for root in dict.fromkeys(roots):
            if not root.exists():
                continue
            for meta in root.glob("**/run_meta.json"):
                d = meta.parent
                try:
                    sp = json.loads((d / "spec.json").read_text(encoding="utf-8"))["spec"]
                    m = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, KeyError, json.JSONDecodeError):
                    continue
                k = int(m["branch"]["seed"]) if m.get("branch") else int(m["seed"])   # 분기 실행은 갈래 번호
                # 11-08 (팀원 재검토 1): 같은 시나리오·seed 의 결과를 모두 남긴다. 전에는 먼저 찾은 하나만 남겨, 짧은 결과(예: 기준
                # 72주)가 먼저 잡히면 뒤의 긴 결과(104주)를 버려 재사용하지 못하고 다시 돌렸다
                idx.setdefault(canonical(sp), {}).setdefault(k, []).append((d, float(m["end_week"])))
        return idx

    def _find_runs(self, rec: dict, seeds: list) -> dict:
        """요청 측정 끝 주를 채우는 실행 {seed: 폴더}. 같은 seed 에 여러 길이가 있으면 채우는 것 중 가장 긴 결과 (11-08)."""
        hits = self._index().get(canonical(rec["spec"]), {})
        need_end = rec["window_weeks"][1]
        out = {}
        for s in seeds:
            ok = [c for c in hits.get(s, []) if c[1] >= need_end]
            if ok:
                out[s] = max(ok, key=lambda c: (c[1], str(c[0])))[0]
        return out

    def _batch_cmd(self, spec_dir: Path, out: Path, n: int) -> list:
        return [sys.executable, "-B", str(ROOT / "datagen" / "run_batch.py"), str(spec_dir), str(out),
                "--workers", str(min(n, 10)), "--drop-sqlite"]

    def job_status(self, scenario_id: str) -> dict | None:
        """백그라운드 작업 상태 (화면이 주기적으로 부른다. LLM 도구 아님)."""
        p = self.results_dir / scenario_id / "job.json"
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"state": "unknown"}
        prog = ""
        blog = self.results_dir / scenario_id / "batch.log"
        if blog.exists():
            lines = [l for l in blog.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            heads = [l for l in lines if "진행" in l or "배치 끝" in l or "완료" in l]
            prog = (heads[-1] if heads else lines[-1] if lines else "").strip()
        return {"state": rec.get("state"), "started": rec.get("started"), "ended": rec.get("ended"),
                "returncode": rec.get("returncode"), "progress": prog}

    def run_experiment(self, scenario_id: str, seeds: list | None = None) -> dict:
        rec = self._load(scenario_id)
        if rec["check"].get("status") != "ok":
            return {"status": "rejected", "reason": "정적 사전 점검을 통과하지 못한 시나리오", "warnings": rec["check"].get("warnings")}
        seeds = sorted({int(s) for s in (seeds or DEFAULT_SEEDS)})
        found = self._find_runs(rec, seeds)
        missing = [s for s in seeds if s not in found]
        base_rec = self._base_branch_rec(rec) if rec["spec"].get("branch") else None
        if base_rec is None and float(rec["window_weeks"][1]) > DEFAULT_END_WEEK:   # 11-07: 52주보다 긴 실행은 같은 길이 기준 운영
            base_rec = self._base_rec(rec["spec"]["end_week"], rec["window_weeks"])
        base_missing = []
        if base_rec:                                                    # 분기: 같은 시작 상태의 기준 갈래, 긴 실행: 같은 길이 기준
            bf = self._find_runs(base_rec, seeds)
            base_missing = [s for s in seeds if s not in bf]
        if not missing and not base_missing:
            return {"status": "ready", "seeds": seeds, "reused_runs": [str(found[s].relative_to(ROOT)) if ROOT in found[s].parents
                                                                         else str(found[s]) for s in seeds]}
        n_runs = len(missing) + len(base_missing)
        job = self.job_status(scenario_id)
        if job and job["state"] == "running":
            return {"status": "running", "started": job["started"], "progress": job["progress"],
                    "note": "이 시나리오의 실행이 이미 진행 중이다. 끝나면 결과를 받아 설명한다"}
        minutes = round(MIN_PER_WEEK_PARALLEL * rec["spec"]["end_week"])
        base_note = ({("base_branches_missing" if rec["spec"].get("branch") else "base_runs_missing"): base_missing}
                     if base_missing else {})
        if not self.allow_run:
            spec_dir, out = self._write_specs(scenario_id, rec, missing, base_rec, base_missing)
            return {"status": "needs_run", "seeds_found": sorted(found), "seeds_missing": missing, **base_note,
                    "estimated_minutes": f"약 {minutes}분 (실행 {n_runs}개 병렬, 추정)",
                    "command": f'cd "{ROOT}"; python -B datagen/run_batch.py "{spec_dir}" "{out}" '
                               f'--workers {min(n_runs, 10)} --drop-sqlite',
                    "note": "실행이 끝나면 같은 질의를 다시 하면 결과를 재사용한다"}
        spec_dir, out = self._write_specs(scenario_id, rec, missing, base_rec, base_missing)
        cmd = self._batch_cmd(spec_dir, out, n_runs)
        lock = self.results_dir / ".des_running"                       # 여러 사람·여러 창이 동시에 DES 를 돌리지 않게
        self.results_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return {"status": "busy", "reason": "다른 DES 실행이 진행 중이다. 끝난 뒤 다시 질의한다",
                    "running": lock.read_text(encoding="utf-8", errors="replace")[:200]}
        os.write(fd, f"{scenario_id} {datetime.now().isoformat(timespec='seconds')}".encode("utf-8"))
        os.close(fd)
        out.mkdir(parents=True, exist_ok=True)
        if self.background:
            # 창 없는 콘솔로 띄운다. DETACHED_PROCESS 로 띄우면 콘솔이 없어서 그 아래 run_batch 가 새 콘솔 창을
            # 열었다 (11-04 사용자 지적: 실행 중 cmd 창이 뜸). 새 프로세스 그룹이라 대시보드 창의 Ctrl+C 와 무관하다.
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
            try:
                subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve().parent / "run_job.py"),
                                  str(lock), str(out / "job.json"), str(out / "job_output.log"), "--", *cmd],
                                 cwd=str(ROOT), creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                lock.unlink(missing_ok=True)
                return {"status": "failed", "reason": f"작업을 띄우지 못했다: {e}"}
            return {"status": "started", "seeds": missing, **base_note, "estimated_minutes": f"약 {minutes}분 (추정)",
                    "note": "백그라운드에서 실행 중이다. 끝나면 화면이 자동으로 결과를 받아 설명을 요청한다. "
                            "지금은 시작 사실과 예상 시간만 알리고 멈춘다"}
        try:
            self.log(f"[SLLMT] DES 실행 시작: seed {missing}, 약 {minutes}분 예상 → {out}")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            r = subprocess.run(cmd, cwd=str(ROOT), env=env)
        finally:
            lock.unlink(missing_ok=True)
        if r.returncode != 0:
            return {"status": "failed", "returncode": r.returncode, "results_dir": str(out)}
        return self.run_experiment(scenario_id, seeds)

    def _write_specs(self, scenario_id: str, rec: dict, missing: list, base_rec: dict | None = None,
                     base_missing: list = ()) -> tuple[Path, Path]:
        spec_dir = self.state_dir / "scenarios" / scenario_id / f"batch_{datetime.now():%Y%m%d-%H%M%S}"
        spec_dir.mkdir(parents=True, exist_ok=True)
        br = rec["spec"].get("branch")
        todo = []
        for s in missing:                                               # 분기면 seed 는 시작 상태 seed 그대로, 갈래 번호만 바꾼다
            todo.append(dict(rec["spec"], name=f"{scenario_id}_b{s}", branch={"week": br["week"], "seed": s}) if br
                        else dict(rec["spec"], name=f"{scenario_id}_s{s}", seed=s))
        for s in base_missing:                                          # 분기: 기준 갈래, 긴 실행: 같은 길이 기준 운영 (11-07)
            todo.append(dict(base_rec["spec"], name=f"{base_rec['spec']['name']}_b{s}", branch={"week": br["week"], "seed": s}) if br
                        else dict(base_rec["spec"], name=f"{base_rec['spec']['name']}_s{s}", seed=s))
        for sp in todo:
            (spec_dir / f"{sp['name']}.json").write_text(json.dumps(sp, ensure_ascii=False, indent=1), encoding="utf-8")
        return spec_dir, self.results_dir / scenario_id

    def _base_rec(self, end_week, window) -> dict:
        """52주보다 긴 실행의 같은 길이 기준 운영 (바뀐 것 없음) 기록 (11-07). 실행 폴더는 canonical 키로 찾는다."""
        import scenario as sc
        _, groups = sc.load_groups(GROUPING)
        spec = {"name": f"BASE{int(end_week)}", "seed": 1, "grouping": GROUPING, "end_week": int(end_week),
                "phases": [{"start_week": 0, "group_mult": {g: 1.0 for g in groups}}], "label_start_week": WARMUP_WEEK,
                "limits": dict(LIMITS), "extensions": {"ic": False, "work_active": False}}
        return {"spec": spec, "window_weeks": [float(window[0]), float(window[1])], "description": f"기준 운영 {int(end_week)}주"}

    def _base_dirs(self, w1: float) -> dict:
        """측정 구간 끝 w1 까지 있는 기준 운영 실행 {seed: 폴더}. 52주 이하는 원래 기준(BASE_RUNS), 넘으면 같은 길이 기준 (11-07)."""
        if w1 <= DEFAULT_END_WEEK:
            return {self._seed_of(d): d for d in BASE_RUNS if (Path(d) / "run_meta.json").exists()}
        return self._find_runs(self._base_rec(w1, [WARMUP_WEEK, w1]), DEFAULT_SEEDS)

    @staticmethod
    def _change_start(rec: dict) -> float:
        """변경이 실제로 시작하는 주 (warm-up·측정 구간과 별개, 11-07). 옛 기록은 spec 에서 다시 계산."""
        if rec.get("change_start_week") is not None:
            return float(rec["change_start_week"])
        sp = rec["spec"]
        if sp.get("branch"):
            return float(sp["branch"]["week"])
        starts = [float(ph["start_week"]) for ph in sp.get("phases") or [] if float(ph["start_week"]) > 0]
        starts += [float(d["start_week"]) for d in sp.get("downtime") or []]
        starts += [float(h.get("start_week", 0)) for h in sp.get("hot_conversion") or [] if float(h.get("start_week", 0)) > 0]
        starts += [float(t.get("start_week", 0)) for t in (sp.get("overrides") or {}).get("tools") or [] if float(t.get("start_week", 0)) > 0]
        starts += [float(d.get("start_week", 0)) for d in sp.get("dispatch") or [] if float(d.get("start_week", 0)) > 0]
        return min(starts) if starts else 0.0

    @staticmethod
    def _warmup_of(rec: dict) -> float:
        w = rec.get("warmup_week")
        return float(w if w is not None else rec["spec"].get("label_start_week", WARMUP_WEEK))

    def _base_branch_rec(self, rec: dict) -> dict:
        """분기 시나리오와 같은 시작 상태의 기준 갈래(바뀐 것 없음) 기록. 실행 폴더는 canonical 키로 찾는다."""
        import scenario as sc
        _, groups = sc.load_groups(GROUPING)
        s0, t = int(rec["spec"]["seed"]), float(rec["spec"]["branch"]["week"])
        code = next((v["code"] for v in STATES.values() if v["seed"] == s0 and float(v["week"]) == t), f"s{s0}w{t:g}")
        spec = {"name": f"C1base_{code}", "seed": s0, "grouping": GROUPING, "end_week": rec["spec"]["end_week"],
                "phases": [{"start_week": 0, "group_mult": {g: 1.0 for g in groups}}], "label_start_week": WARMUP_WEEK,
                "limits": dict(LIMITS), "extensions": {"ic": False, "work_active": False},
                "branch": {"week": rec["spec"]["branch"]["week"]}}
        return {"spec": spec, "window_weeks": rec["window_weeks"], "description": f"기준 갈래 ({code})"}

    # -- get_report ---------------------------------------------------------------------------------
    def get_report(self, scenario_id: str, compare_to: str | None = None, window_start_week: int | None = None,
                   window_end_week: int | None = None) -> dict:
        """보고서 요약. 비교 대상은 기준 운영(기본) 또는 다른 시나리오(compare_to). 항목 이름은 한국어로 낸다
        (11-04 첫 실제 질의에서 LLM 이 영어 변수 이름을 그대로 옮기고, 기준값을 뺄셈으로 만들고, 비교를 건너뛴 것을 고침)."""
        import pandas as pd
        import whatif_report as wr
        rec = self._load(scenario_id)
        other = None
        if compare_to:
            if compare_to == scenario_id:
                return {"error": "compare_to 가 같은 시나리오다"}
            other = self._load(compare_to)
        recs = [rec] + ([other] if other else [])
        w0 = int(window_start_week if window_start_week is not None else max(r["window_weeks"][0] for r in recs))
        w1 = int(window_end_week if window_end_week is not None else min(r["window_weeks"][1] for r in recs))
        if w0 >= w1 and window_start_week is None and window_end_week is None:   # 11-07: 검증용 짧은 실행 (측정 구간 없음)
            return {"status": "no_window", "reason": f"검증용 짧은 실행이라 측정 구간이 없다 (실행 {rec['spec']['end_week']}주 < 측정 시작 {w0}주). "
                                                     "보고서 수치를 만들지 않는다. 실행이 정상으로 시작·종료됐는지만 알린다"}
        run_end = min(float(r["spec"]["end_week"]) for r in recs)
        if w1 > run_end:                                                 # 11-07: 실행 길이를 넘는 구간 ("실행 결과 없음"으로 잘못 안내하던 것)
            return {"status": "out_of_range", "reason": f"실행이 {run_end:g}주까지라 {w1}주까지는 볼 수 없다. 측정 구간 끝은 {run_end:g}주 "
                                                        "이하로 준다. 더 길게 보려면 실행 길이(end_week)를 늘린 새 실험이 필요하다"}
        if w1 - w0 < 4:
            return {"error": f"측정 창이 너무 짧다 ({w0}~{w1}주)"}
        found = self._find_runs({**rec, "window_weeks": [w0, w1]}, DEFAULT_SEEDS)
        if not found:
            return {"status": "not_ready", "reason": "실행 결과가 없다. run_experiment 를 먼저 부른다"}
        if other:
            ref = self._find_runs({**other, "window_weeks": [w0, w1]}, DEFAULT_SEEDS)
            if not ref:
                return {"status": "not_ready",
                        "reason": f"비교 대상 {compare_to} 의 실행 결과가 없다. 그 시나리오도 run_experiment 로 돌린다"}
            ref_dirs = [ref[s] for s in sorted(ref)]
            ref_label = f"시나리오 {compare_to} ({other['description']})"
        elif rec["spec"].get("branch"):                                 # 분기: 같은 시작 상태의 기준 갈래와 갈래끼리 짝
            ref = self._find_runs({**self._base_branch_rec(rec), "window_weeks": [w0, w1]}, DEFAULT_SEEDS)
            if not ref:
                return {"status": "not_ready", "reason": "같은 시작 상태의 기준 갈래가 없다. run_experiment 를 먼저 부른다"}
            ref_dirs = [ref[s] for s in sorted(ref)]
            name = f"{rec['state']}: " if rec.get("state") else ""
            ref_label = (f"같은 시작 상태({name}기준 seed {rec['spec']['seed']}, "
                         f"{rec['spec']['branch']['week']}주)의 바뀐 것 없는 기준 갈래, 같은 갈래끼리 짝")
        else:
            if w1 <= DEFAULT_END_WEEK:
                ref_dirs = list(BASE_RUNS)
                ref_label = "기준 운영"
            else:                                                       # 11-07: 52주보다 긴 구간은 같은 길이 기준 운영
                ref = self._base_dirs(w1)
                if not ref:
                    return {"status": "not_ready", "reason": f"{w1}주까지의 기준 운영 결과가 없다. run_experiment 가 시나리오와 함께 "
                                                             "같은 길이 기준 운영을 돌린다"}
                ref_dirs = [ref[s] for s in sorted(ref)]
                ref_label = f"기준 운영 (같은 길이 {w1}주 이상 실행)"
        tag = f"_vs_{compare_to}" if other else ""
        out = self.results_dir / scenario_id / f"report_w{w0}-{w1}{tag}_{datetime.now():%Y%m%d-%H%M%S}_{os.urandom(2).hex()}"
        argv = ["--runs", *[str(found[s]) for s in sorted(found)], "--base", *[str(d) for d in ref_dirs],
                "--window", str(w0), str(w1), "--out", str(out)]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                wr.main(argv)
        except SystemExit as e:                                        # whatif_report 는 오류를 SystemExit 로 낸다
            return {"status": "error", "reason": str(e)}
        s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        tg = pd.read_csv(out / "toolgroups.csv").set_index("toolgroup")
        paired_all = s["n_pairs"] == len(s["seeds"]) and s["n_pairs"] > 0

        kpi = {}
        hot_names = {"처리량 hot (lot/주)": "처리량 hot+superhot (lot/주)", "CT hot (일)": "CT hot+superhot (일)",
                     "납기 준수 hot (%)": "납기 준수 hot+superhot (%)"}   # 11-08 (D-80): 합친 값은 이름에 밝힘, 'hot' 만은 hot만 뜻
        for k, v in s["kpi"].items():
            k = hot_names.get(k, k)
            m, d, c = (v.get(x) for x in ("mean", "diff", "diff_ci"))
            m, d, c = (None if x is None or x != x else float(x) for x in (m, d, c))
            row = {"비교 대상": _r(m - d) if (paired_all and m is not None and d is not None) else None, "시나리오": _r(m)}
            if d is not None:
                row["차이"] = _r(d)
                row["차이 95% CI 반폭"] = _r(c)
                if c is None:
                    row["판정"] = "CI 없음 (짝 1개)"
                elif abs(d) > c:                                       # 유의 = CI 가 0 을 포함하지 않음
                    row["판정"] = "유의하게 증가" if d > 0 else "유의하게 감소"
                else:
                    row["판정"] = NO_DIFF_LABEL
            kpi[k] = row

        def util_rows(col, n=5):
            return [{"툴그룹": t, "가동률 (%)": _r(100 * u, 1)} for t, u in tg[col].dropna().sort_values(ascending=False).head(n).items()]

        changes = []
        if "가동률 차이" in tg.columns:
            for t, r in tg.reindex(tg["가동률 차이"].abs().sort_values(ascending=False).index).head(5).iterrows():
                changes.append({"툴그룹": t, "비교 대상 가동률 (%)": _r(100 * r["기준 가동률"], 1),
                                "시나리오 가동률 (%)": _r(100 * r["가동률 평균"], 1), "차이 (%p)": _r(100 * r["가동률 차이"]),
                                "차이 95% CI 반폭 (%p)": _r(100 * r["차이 CI 반폭"]), "대기 lot": _r(r["대기 lot 평균"], 1)})

        ss = s["steady_state"]
        end = float(rec["spec"]["end_week"])
        cap = (int(end * 7) // 5 // 2) * 5 / 7                          # whatif_report.mser5 의 탐색 상한 (실행 앞 절반)
        rise = 100 * ss["rise_frac"]
        if ss["growing"]:
            s_text = f"측정 창 동안 WIP 가 계속 늘어남 (창 동안 {rise:+.1f}%). 정상상태로 보기 어렵고 수치는 창 길이에 따라 달라짐"
        else:
            s_text = f"측정 창 동안 WIP 가 계속 늘지는 않음 (창 동안 {rise:+.1f}%)"
        if ss.get("mser_at_bound"):
            w_text = (f"warm-up 절단점(seed 평균 WIP 의 MSER-5)이 {ss['mser_week']:.1f}주로 탐색 상한({cap:.1f}주, 실행 앞 절반)에 닿음. "
                      "그보다 늦게 안정됐을 가능성을 배제할 수 없음")
        elif ss["mser_week"] > w0:
            w_text = f"warm-up 절단점 {ss['mser_week']:.1f}주가 측정 창 시작 {w0}주보다 늦음. 창 앞부분에 과도 구간이 섞였을 수 있음"
        else:
            w_text = f"warm-up 절단점 {ss['mser_week']:.1f}주로 측정 창 시작 {w0}주 전. warm-up 충분"
        branch = rec["spec"].get("branch")
        # 사건형: 변경이 실제로 warm-up 뒤에 시작 (정지·급주문·분기). 11-07 검토 의견: 조회 창 시작(w0)이 아니라 시나리오의 변경
        # 시작 주로 판단한다 — 처음부터 바꾼 정상상태 실험을 40~52주로 다시 조회해도 '40주 사건'으로 보지 않는다
        cs, warm_r = self._change_start(rec), self._warmup_of(rec)
        event_type = bool(branch) or cs > warm_r
        if event_type:                                                  # 사건 뒤 창에서 WIP 가 느는 것은 당연하다 (11-05 사용자 결과)
            s_text = (f"사건형 질의: 변경이 {cs:g}주에 시작함 (측정 창 {w0}~{w1}주). 창 안 WIP 변화(창 동안 {rise:+.1f}%)는 사건 영향임. "
                      "정상상태 여부가 아니라 주별 궤적(최고점, WIP 회복 판정, 누적 완료 차이)으로 봄")
            if branch:
                w_text = f"분기 주 {branch['week']:g}주는 warm-up({WARMUP_WEEK}주) 뒤라 시작 상태는 정상상태 구간임"
        traj = None
        ev, tr = s.get("event"), s.get("trajectory")
        if event_type and ev and tr:
            curve = [float(x) for x in tr["wip_diff"] if x is not None and x == x]
            traj = {"WIP 차이 최고 (lot, 짝마다 최댓값의 평균)": _r(ev["peak_wip_diff"], 1),
                    "최고점 95% CI 반폭": _r(ev["peak_wip_diff_ci"], 1),
                    "평균 곡선이 가장 높은 주": ev["peak_week"],
                    "평균 곡선 최고값 (lot)": _r(max(curve), 1) if curve else None,
                    "표현 주의": ("'WIP 차이 최고'는 짝마다 최댓값의 평균이라 평균 곡선의 최고값보다 클 수 있다. 두 값을 한 좌표처럼 "
                                "'N주에 X lot'으로 쓰지 않는다. 예: 평균 곡선은 N주에 가장 높고(약 Y lot), 짝마다의 최고는 평균 X lot"),
                    # 11-09 (D-89): WIP 회복 판정 baseline_band_v1 — 기준 WIP ±2%·3주·재이탈 없음, 복귀 뒤부터, CI 안 씀
                    # (D-87 의 25%+CI 정의를 바꿈). 기준 WIP 참조값 = 이 보고서 WIP 행의 비교 대상(측정 창 평균)
                    **_recovery_fields(tr["week"], tr["wip_diff"], (kpi.get("WIP (lot)") or {}).get("비교 대상"),
                                       rec["spec"], event_start=cs),
                    "창 끝 누적 완료 regular lot 차이": _r(ev["cum_done_diff_end"], 1),
                    "누적 완료 차이 95% CI 반폭": _r(ev["cum_done_diff_end_ci"], 1),
                    "주별 (2주 간격)": [{"주": tr["week"][i], "WIP 차이": _r(tr["wip_diff"][i], 1),
                                       "CI": _r(tr["wip_diff_ci"][i], 1), "누적 완료 차이": _r(tr["cum_done_diff"][i], 1)}
                                      for i in range(0, len(tr["week"]), 2)]}

        notes = ["값은 seed 평균. 차이는 같은 seed 끼리 짝지은 차이(시나리오 − 비교 대상)의 평균, CI 는 95% 신뢰구간 반폭",
                 "보고서의 모든 수치는 이 결과에서 그대로 옮긴다. 비교 대상 값을 뺄셈으로 만들지 않는다. 부호(+·−)도 그대로",
                 "가동률을 이 대화에서 처음 보이면 '정의'의 가동률 설명을 한 문장으로 함께 쓴다"]
        if len(s["seeds"]) < 2:
            notes.append("seed 가 1개라 신뢰구간 없음. 크기는 참고용")
        if not paired_all:
            notes.append(f"짝지은 seed 가 {s['n_pairs']}개로 시나리오 seed {len(s['seeds'])}개보다 적어 비교 대상 평균을 내지 않음")
        default_w = [max(r["window_weeks"][0] for r in recs), min(r["window_weeks"][1] for r in recs)]
        if [w0, w1] != [int(default_w[0]), int(default_w[1])]:          # 11-07: 조회 창만 바꾼 것 (실험 유형·변경 시작은 그대로)
            notes.append(f"측정 창을 {w0}~{w1}주로 바꿔 조회함 (이 실험의 기본 측정 구간 {default_w[0]:g}~{default_w[1]:g}주). "
                         f"창만 바뀐 것이고 변경 시작({cs:g}주)·실험 유형({'사건형' if event_type else '처음부터 바꾼 정상상태 비교'})은 그대로다")
        if w0 < warm_r:
            notes.append(f"측정 시작 {w0}주가 이 실험의 warm-up({warm_r:g}주)보다 앞이라 초기 과도 구간이 섞일 수 있음")
        try:                                                            # 11-07: lot 유형별(hot만·superhot)·regular 제품군별 납기 짝 비교
            lt_rows = self._lot_type_rows({s_: found[s_] for s_ in found}, {self._seed_of(d): d for d in ref_dirs}, w0, w1)
        except Exception as e:                                          # 보조 표라 실패해도 보고서는 낸다
            lt_rows = {"오류": str(e)[:200]}
        notes.append("kpi 의 'hot+superhot' 행(처리량·CT·납기 준수)은 둘을 합친 값. 'hot lot'·'hot 납기'를 물으면 'lot 유형별·제품군별' 표의 "
                     "hot만 행을 쓰고, 비교 대상(기준)도 hot만 값을 쓴다. 합친 값과 hot만 값을 한 문장에서 섞지 않는다")
        verdict_summary = {"유의하게 증가": [], "유의하게 감소": [], "유의한 차이 확인 못함 (변화 없음이 확인된 것이 아님)": []}
        for table in (kpi, lt_rows if isinstance(lt_rows, dict) else {}):   # 11-08 밤 (T-v2 X2·X3): 결론을 지표·lot 유형별 판정에 묶는다
            for k, v in table.items():
                if not isinstance(v, dict) or v.get("차이") is None or v.get("차이 95% CI 반폭") is None:
                    continue
                verdict = str(v.get("판정") or "")
                key = verdict if verdict in ("유의하게 증가", "유의하게 감소") else \
                    "유의한 차이 확인 못함 (변화 없음이 확인된 것이 아님)" if verdict == NO_DIFF_LABEL else None
                if key:
                    verdict_summary[key].append(f"{k}: {v['차이']:+g} ±{v['차이 95% CI 반폭']:g}")
        notes.append("② 결론·⑤ 다음에 볼 것은 '판정 요약'과 맞춘다. 같은 지표 묶음(예: 납기)에 유의한 항목(예: hot만)과 아닌 항목(예: regular)이 "
                     "섞이면 lot 유형을 밝혀 각각 쓴다. '유의한 차이 확인 못함' 항목을 '유지·늘지 않음·변화 없음'으로 바꾸지 않는다")
        result = {
            "status": "ok", "scenario_id": scenario_id, "description": rec["description"],
            "비교 대상": ref_label, "측정 창 (주)": [w0, w1], "seeds": s["seeds"], "짝지은 seed 수": s["n_pairs"],
            "기간": {"실행 (주)": [float(rec["spec"]["branch"]["week"]) if branch else 0.0, float(rec["spec"]["end_week"])],
                   "warm-up (주)": None if branch else warm_r, "변경 시작 (주)": cs,
                   "실험 유형": "사건형 (변경이 warm-up 뒤 시작)" if event_type else "정상상태 비교 (변경이 처음부터 또는 warm-up 전 시작)"},
            "정상상태": {"WIP 계속 증가": bool(ss["growing"]), "창 동안 WIP 변화 (%)": _r(rise, 1),
                       "warm-up 절단점 (주)": _r(ss["mser_week"], 1), "warm-up 부족 가능성": bool(ss["warmup_late"]),
                       "설명": f"{s_text}. {w_text}"},
            **({"주별 궤적": traj} if traj else {}),
            "kpi": kpi,
            "lot 유형별·제품군별 (짝 비교)": lt_rows,
            "판정 요약": verdict_summary,
            "가동률 상위 5 (시나리오)": [{"툴그룹": t["toolgroup"], "가동률 (%)": _r(100 * t["util"], 1),
                                    "가동률 95% CI 반폭 (%p, 변화량 아님)": _r(100 * t["ci"] if t["ci"] is not None else None),
                                    "상위 5에 든 seed 수": t["seeds_in_top5"]} for t in s["top5"]],
            "가동률 상위 5 (비교 대상)": util_rows("기준 가동률") if "기준 가동률" in tg.columns else [],
            "가동률이 가장 많이 바뀐 툴그룹": changes,
            "정의": {"가동률": "쓸 수 있는 시간(작업 + 유휴) 중 실제 작업한 시간의 비율 (시뮬레이션 실측). "
                             "사전 점검의 정적 가동률(투입량으로 계산한 부하)과는 다른 지표"},
            "notes": notes,
            "report_file": str(out / "report.md"),
        }
        # 11-09 (D-91, 글 설명 개선 A): 숫자가 들어간 문장은 도구가 쓰고 LLM 은 그대로 붙인다
        result[_rt.BLOCK_KEY] = _rt.report_sentences(result, changes=self._change_lines(rec))
        return result

    # -- request_approval (확인 후 실행 카드, 11-06) ---------------------------------------------------
    CHANGE_PREFIXES = ("투입:", "투입량:", "정지:", "고장 특성:", "hot 전환:", "장비 수:", "가공 시간:", "PM:", "디스패칭 (베타):",
                       "시작 상태:")

    def _change_lines(self, rec: dict) -> list:
        """카드와 같은 설정 줄 중 '바꾼 것'만 (11-09 D-91, 보고서의 바꾼 것 = 승인한 카드 그대로)."""
        try:
            return [l for l in self.describe_scenario(rec) if l.startswith(self.CHANGE_PREFIXES)]
        except Exception:                                               # 설정 줄을 못 만들어도 보고서는 낸다
            return []

    def describe_scenario(self, rec: dict, seeds: list | None = None) -> list:
        """시나리오 기록(spec·정적 점검 결과)에서 사용자에게 보일 실제 적용 설정. LLM 문장이 아니라 도구 값으로 만든다."""
        sp, chk = rec["spec"], rec.get("check") or {}
        out = []
        try:
            tg_area = {n: v.get("area") for n, v in (self.get_fab_info().get("toolgroups") or {}).items()}
        except Exception:                                              # fab 정보를 못 읽으면 툴그룹 이름 그대로
            tg_area = {}
        area_tgs = {}
        for n, a in tg_area.items():
            area_tgs.setdefault(a, set()).add(n)

        def pct(v):
            v = float(v)
            return f"{(v - 1) * 100:+.4g}% (×{v:.4g})"

        def targets(entries, pkey):
            """같은 변경값끼리 묶어 대상 이름 한 줄로. area 의 툴그룹을 모두 담으면 'X area 전체 (툴그룹 n개)' (11-06 카드).
            반환: [(대상 글, 대표 항목)]"""
            groups = {}
            for e in entries or []:
                groups.setdefault(pkey(e), []).append(e)
            res = []
            for es in groups.values():
                labels = [f"{e['area']} area 전체 (툴그룹 {len(area_tgs.get(e['area'], ())) or '?'}개)" for e in es if e.get("area")]
                tgs = {e["toolgroup"] for e in es if e.get("toolgroup")}
                for a, members in sorted(area_tgs.items(), key=lambda x: str(x[0])):
                    if len(members) > 1 and members <= tgs:
                        labels.append(f"{a} area 전체 (툴그룹 {len(members)}개)")
                        tgs -= members
                labels += sorted(tgs)
                res.append((", ".join(labels), es[0]))
            return res

        for ph in sp.get("phases") or []:
            gm = {g: v for g, v in (ph.get("group_mult") or {}).items() if abs(float(v) - 1) > 1e-9}
            pm = {p: v for p, v in (ph.get("product_mult") or {}).items() if abs(float(v) - 1) > 1e-9}
            lt = {k: v for k, v in (ph.get("lot_type_mult") or {}).items() if abs(float(v) - 1) > 1e-9}
            if not gm and not pm and not lt:
                continue
            parts = []
            allg = ph.get("group_mult") or {}
            if gm and len(gm) == len(allg) and len({round(float(v), 6) for v in gm.values()}) == 1:
                parts.append(f"전체 regular 투입 {pct(next(iter(gm.values())))}")
            else:
                parts += [f"{g} 제품군 투입 {pct(v)}" for g, v in gm.items()]
            parts += [f"{p} 투입 {pct(v)}" for p, v in pm.items()]
            parts += [f"{k} 투입 중단 (×0, 이 기간 {k} lot 없음)" if float(v) == 0 else f"{k} 투입 {pct(v)}"
                      for k, v in sorted(lt.items())]                  # 11-07: hot·superhot 투입 배율
            out.append(f"투입: {', '.join(parts)} ({float(ph.get('start_week', 0)):g}주부터)")
        base_w, chk_ph = chk.get("base_total_wspw"), chk.get("phases") or []    # 11-08 (T-v1 X1): 환산값을 도구 값으로 보인다
        if base_w and any(abs(float(p.get("total_wspw", base_w)) - float(base_w)) > 0.05 for p in chk_ph):
            out.append(f"투입량: 기준 {float(base_w):,.1f} WSPW → " +
                       ", ".join(f"{float(p['start_week']):g}주부터 {float(p['total_wspw']):,.1f}" for p in chk_ph) +
                       " WSPW (모든 lot 유형 합, 도구 계산 — 답에 옮길 때 직접 계산하지 않음)")
        for d in sp.get("downtime") or []:
            if d.get("type") == "outage":
                s, du = float(d["start_week"]), float(d["duration_weeks"])
                out.append(f"정지: {d['toolgroup']} {d['n_tools']}대, {s:g}주부터 {du:g}주 동안 ({s + du:g}주에 복귀)")
        bd = [d for d in sp.get("downtime") or [] if d.get("type") != "outage"]
        for tgt, d in targets(bd, lambda d: (d.get("mttr"), d.get("mttf"), d.get("start_week"), d.get("end_week"))):
            parts = ([f"수리 시간 ×{float(d['mttr']):g}"] if d.get("mttr") else []) + \
                    ([f"고장 간격 ×{float(d['mttf']):g}"] if d.get("mttf") else [])
            out.append(f"고장 특성: {tgt} {', '.join(parts)}, {float(d.get('start_week', 0)):g}주~"
                       + (f"{float(d['end_week']):g}주" if d.get("end_week") is not None else "끝"))
        for h in chk.get("hot_conversion") or []:
            prods = h.get("products") or []
            who = "모든 제품" if len(prods) >= 10 else ", ".join(prods)
            b, a = h.get("hot 비중 % (기간 중, 기준 → 변경)", [None, None])
            out.append(f"hot 전환: {who} 신규 regular 투입의 {100 * float(h['share']):g}%, {h.get('기간')}, "
                       f"예상 약 {h.get('예상 전환 lot (약, 기간 합계)')} lot, 기간 중 hot 비중 {b}% → {a}% (모든 규칙 합산)")
        ov = sp.get("overrides") or {}
        for t in ov.get("tools") or []:
            s = t.get("start_week")
            how = "정지(끝까지)" if t["delta"] < 0 and s else ("증설" if t["delta"] > 0 else "감축")
            out.append(f"장비 수: {t['toolgroup']} {int(t['delta']):+d}대 {how}" + (f" ({float(s):g}주부터)" if s else " (처음부터)"))
        for tgt, e in targets(ov.get("processing_time"), lambda e: round(float(e["mult"]), 9)):
            out.append(f"가공 시간: {tgt} ×{float(e['mult']):.4g} (처음부터)")
        for tgt, e in targets(ov.get("pm"), lambda e: (round(float(e.get("interval_mult", 1)), 9),
                                                     round(float(e.get("duration_mult", 1)), 9))):
            parts = ([f"PM 간격 ×{float(e['interval_mult']):.4g}"] if float(e.get("interval_mult", 1)) != 1 else []) + \
                    ([f"PM 소요 시간 ×{float(e['duration_mult']):.4g}"] if float(e.get("duration_mult", 1)) != 1 else [])
            out.append(f"PM: {tgt} {', '.join(parts)} (처음부터)")
        for d in chk.get("dispatch") or []:                             # 11-09: 디스패칭 순서 (베타). 정규화된 대상(툴그룹 목록)을 보인다
            tgs = d.get("toolgroups") or []
            who = (f"{d['area']} area" + (f" setup 툴그룹 {len(tgs)}개" if d.get("cr_before_setup") else f" 툴그룹 {len(tgs)}개")
                   + f" ({', '.join(tgs)})") if d.get("area") else ", ".join(tgs)
            what = ("3순위 critical ratio → 도착 순서(선입선출)" if d.get("rank3") == "fifo"
                    else "critical ratio 를 setup 시간보다 먼저 (2·3순위 교환)")
            when = f"{float(d['start_week']):g}주~" + (f"{float(d['end_week']):g}주" if d.get("end_week") is not None else "끝")
            out.append(f"디스패칭 (베타): {who} — {what}, {when}. 1순위(lot 우선순위 superhot > hot > regular)는 그대로")
        if rec.get("state"):
            out.append(f"시작 상태: {rec['state']} (분기 {sp['branch']['week']:g}주, 갈래 5개, 같은 상태의 기준 갈래와 비교)")
        run0 = float(sp["branch"]["week"]) if sp.get("branch") else 0.0         # 11-07 팀원 요청: 나머지 설정 유지 여부를 카드에
        out.append(f"그 밖의 설정: 위 변경 외에는 기준 운영 그대로 (실행 {run0:g}~{float(sp['end_week']):g}주)")
        seeds = sorted(int(s) for s in (seeds or DEFAULT_SEEDS))
        out.append(f"실행 seed: {self._seed_text(seeds)}" + (" (분기 갈래 번호)" if sp.get("branch") else ""))
        w0, w1 = rec.get("window_weeks") or [None, None]
        if w0 is not None and w1 is not None and float(w0) >= float(w1):  # 11-07 팀원 추가 요청: 짧은 실행을 측정 창처럼 보이지 않음
            out.append(f"측정 구간: 없음 — 검증용 짧은 실행 (실행 {run0:g}~{float(sp['end_week']):g}주가 측정 시작 {w0}주보다 짧음). "
                       "보고서 측정 대상 아님")
        elif sp.get("branch"):
            out.append(f"측정 구간: {float(w0):g}~{float(w1):g}주 (분기 주부터), 같은 갈래끼리 짝 비교")
        else:                                                           # 11-07: warm-up·변경 시작·측정 구간을 따로 보인다
            warm, cs = self._warmup_of(rec), self._change_start(rec)
            why = f"변경이 {cs:g}주에 시작해 그 주부터" if cs > warm else f"앞 {warm:g}주 warm-up 제외"
            out.append(f"측정 구간: {float(w0):g}~{float(w1):g}주 ({float(w1) - float(w0):g}주간, {why}), 비교 대상과 같은 seed 끼리 짝 비교")
            if float(sp["end_week"]) > DEFAULT_END_WEEK:
                have = self._find_runs(self._base_rec(sp["end_week"], [w0, w1]), seeds)
                miss_b = [s for s in seeds if s not in have]
                out.append(f"기준 운영: {float(sp['end_week']):g}주 " + ("기존 결과 재사용" if not miss_b else
                                                                    f"결과가 없어 seed {self._seed_text(miss_b)} 기준 운영도 함께 실행"))
            if warm == WARMUP_WEEK and float(sp["end_week"]) == DEFAULT_END_WEEK:
                out.append("기간 안내: 기본 1년(52주) 실행, warm-up 26주. 바꾸려면 주 단위로 알려 주면 됨 (예: warm-up 52주, 전체 104주)")
        tops = [(t, u) for p in chk.get("phases") or [] for t, u in p.get("static_util_top5_pct") or []]
        if tops:
            t, u = max(tops, key=lambda x: x[1])
            out.append(f"정적 점검: {'통과' if chk.get('status') == 'ok' else '거부 (실행 불가)'}, 정적 가동률 최고 {t} {u}%")
        out += [f"주의: {w}" for w in chk.get("warnings") or []]
        return out

    @staticmethod
    def _seed_text(seeds: list) -> str:
        s = sorted(int(x) for x in seeds)
        if len(s) > 2 and s == list(range(s[0], s[-1] + 1)):
            return f"{s[0]}~{s[-1]} ({len(s)}개)"
        return f"{', '.join(map(str, s))} ({len(s)}개)"

    def _run_conditions(self, rec: dict, seeds: list) -> dict:
        out = self._run_conditions_base(rec, seeds)
        if not rec["spec"].get("branch"):                               # 11-07: warm-up·변경 시작·같은 길이 기준 운영
            out["warm-up 주"] = self._warmup_of(rec)
            out["변경 시작 주"] = self._change_start(rec)
            if float(rec["spec"]["end_week"]) > DEFAULT_END_WEEK:
                have = self._find_runs(self._base_rec(rec["spec"]["end_week"], rec["window_weeks"]), seeds)
                out["기준 운영 함께 실행 seed"] = [s for s in sorted(int(x) for x in seeds) if s not in have]
        return out

    @staticmethod
    def _run_conditions_base(rec: dict, seeds: list) -> dict:
        """카드의 실행 조건 (11-07 팀원 추가 요청: 화면 값과 실행 입력 파일을 항목별로 대조할 수 있게 구조로 둔다).
        '설정값'은 실행기 입력 파일과 같은 키. 실행별 관리 필드 name·seed 는 입력 파일마다 다르게 붙으므로 빼고,
        seed 는 'seeds' 목록으로 따로 대조한다."""
        sp = rec["spec"]
        run0 = float(sp["branch"]["week"]) if sp.get("branch") else 0.0
        w0, w1 = rec.get("window_weeks") or [None, None]
        return {"실행 주": [run0, float(sp["end_week"])], "seeds": sorted(int(s) for s in seeds),
                "측정 구간": [w0, w1] if (w0 is not None and w1 is not None and float(w0) < float(w1)) else None,
                "설정값": {k: v for k, v in sp.items() if k not in ("name", "seed")}}

    def request_approval(self, scenario_id: str, compare_to: str | None = None, note: str | None = None,
                         seeds: list | None = None) -> dict:
        """사용자에게 보일 '확인 후 실행' 카드 내용. 실행 승인은 사용자의 버튼(대시보드)·명시 명령(CLI)으로만 생긴다
        (sllmt_core.run_gate.RunGate.grant). 이 도구는 카드를 만들 뿐 승인하지 않는다.
        seeds: 실행할 seed (기본 1~5). 카드에 그대로 보이고, 실행 확인 코드가 승인 뒤 같은 seed 로만 실행하게 한다 (11-07)."""
        rec = self._load(scenario_id)
        if (rec.get("check") or {}).get("status") != "ok":
            return {"status": "rejected", "reason": "정적 점검을 통과하지 못한 시나리오는 실행 승인을 요청할 수 없다. 이유와 대안을 설명한다"}
        seeds = sorted({int(s) for s in (seeds or DEFAULT_SEEDS)})
        if not seeds or any(s < 1 or s > 5 for s in seeds):
            return {"error": "seeds 는 1~5 안에서 고른다 (기준 실행이 seed 1~5)"}
        card = {"대상": {"scenario_id": scenario_id, "설명": rec.get("description"), "설정": self.describe_scenario(rec, seeds),
                       "실행 조건": self._run_conditions(rec, seeds)},
                "실행 조건": {"seeds": seeds}}
        if compare_to:
            if compare_to == scenario_id:
                return {"error": "compare_to 가 같은 시나리오다"}
            other = self._load(compare_to)
            if (other.get("check") or {}).get("status") != "ok":
                return {"status": "rejected", "reason": f"기준 안 {compare_to} 가 정적 점검을 통과하지 못했다"}
            card["기준"] = {"scenario_id": compare_to, "설명": other.get("description"), "설정": self.describe_scenario(other, seeds),
                          "실행 조건": self._run_conditions(other, seeds)}
            card["비교"] = "결과 차이 = 대상 − 기준 (같은 seed 끼리 짝 비교)"
        if note:
            card["안내"] = str(note)[:300]
        return {"status": "awaiting_user_approval", "card": card,
                "next": "화면에 '확인 후 실행' 카드를 띄웠다. 사용자가 버튼을 누르면 '[승인]' 메시지가 온다. 지금은 run_experiment 를 "
                        "부르지 않는다. 카드 내용을 한두 줄로 요약하고, 사용자가 말하지 않은 값이 있으면 그 값을 알려 달라고 한 뒤 멈춘다"}

    # -- get_toolgroup_stats ------------------------------------------------------------------------
    TG_SORT = {"가동률": "가동률 (%)", "대기": "대기 lot", "가용률": "가용률 (%)", "고장": "고장 (%)", "PM": "PM (%)",
               "가동률 변화": "가동률 차이 (%p)"}

    def _tg_run_stats(self, d: Path, w0: float, w1: float):
        """한 실행 폴더의 측정 창 툴그룹 지표 (tg_hourly). whatif_report.load_run 과 같은 가동률 정의 (가동 / (가동 + 유휴))."""
        import pandas as pd
        key = (str(d), float(w0), float(w1))
        cache = self.__dict__.setdefault("_tg_cache", {})
        if key in cache:
            return cache[key]
        cols = ["hour", "toolgroup", "lotmin_wait_queue", "lotmin_wait_batch", "lotmin_wait_down",
                "st_work", "st_idle", "st_breakdown", "st_pm"]
        h = pd.read_parquet(Path(d) / "tg_hourly.parquet", columns=cols)
        h0, h1 = int(w0 * 168), int(w1 * 168)
        g = h[(h.hour >= h0) & (h.hour < h1)].groupby("toolgroup", observed=True)
        s = g[["st_work", "st_idle", "st_breakdown", "st_pm", "lotmin_wait_queue", "lotmin_wait_batch", "lotmin_wait_down"]].sum()
        up = s.st_work + s.st_idle
        tot = up + s.st_breakdown + s.st_pm
        out = pd.DataFrame({"util": (s.st_work / up).where(up > 0), "avail": (up / tot).where(tot > 0),
                            "down": (s.st_breakdown / tot).where(tot > 0), "pm": (s.st_pm / tot).where(tot > 0),
                            "queue": (s.lotmin_wait_queue + s.lotmin_wait_batch + s.lotmin_wait_down) / 60 / max(h1 - h0, 1)})
        cache[key] = out
        return out

    @staticmethod
    def _seed_of(d: Path) -> int:
        m = json.loads((Path(d) / "run_meta.json").read_text(encoding="utf-8"))
        return int(m["branch"]["seed"]) if m.get("branch") else int(m["seed"])

    def get_toolgroup_stats(self, scenario_id: str | None = None, compare_to: str | None = None, toolgroups: list | None = None,
                            area: str | None = None, sort_by: str = "가동률", ascending: bool = False, top_n: int = 10) -> dict:
        """툴그룹별 가동률·가용률·고장·PM·대기 (11-06 사용자 요청: 병목·가동률 높은 툴그룹을 질의로 받기). 실행은 하지 않는다."""
        import numpy as np
        import pandas as pd
        from scipy import stats
        info = self.get_fab_info()
        tgs = info["toolgroups"]
        if sort_by not in self.TG_SORT:
            return {"error": f"sort_by 는 {list(self.TG_SORT)} 중 하나"}
        if area and area not in info["areas"]:
            return {"error": f"없는 area: {area} (가능: {info['areas']})"}
        bad = [t for t in toolgroups or [] if t not in tgs]
        if bad:
            return {"error": f"없는 툴그룹: {bad}. 이름은 get_fab_info 의 toolgroups 를 쓴다"}
        if scenario_id:
            rec = self._load(scenario_id)
            w0, w1 = rec["window_weeks"]
            found = self._find_runs(rec, DEFAULT_SEEDS)
            if not found:
                return {"status": "not_ready", "reason": "실행 결과가 없다. 확인받은 뒤 run_experiment 로 돌린 시나리오만 조회한다"}
            runs = {s: found[s] for s in sorted(found)}
            if compare_to:
                other = self._load(compare_to)
                ref = self._find_runs({**other, "window_weeks": [w0, w1]}, DEFAULT_SEEDS)
                if not ref:
                    return {"status": "not_ready", "reason": f"비교 대상 {compare_to} 의 실행 결과가 없다"}
                ref_label = f"시나리오 {compare_to} ({other['description']})"
            elif rec["spec"].get("branch"):
                ref = self._find_runs({**self._base_branch_rec(rec), "window_weeks": [w0, w1]}, DEFAULT_SEEDS)
                ref_label = "같은 시작 상태의 바뀐 것 없는 기준 갈래"
            else:
                ref = self._base_dirs(w1)                               # 11-07: 52주보다 긴 실험은 같은 길이 기준 운영
                ref_label = "기준 운영"
            target = f"시나리오 {scenario_id} ({rec['description']})"
        else:
            w0, w1 = WARMUP_WEEK, DEFAULT_END_WEEK
            runs = {self._seed_of(d): d for d in BASE_RUNS if (Path(d) / "run_meta.json").exists()}
            ref, ref_label, target = {}, None, "기준 운영"
            if not runs:
                return {"status": "not_ready", "reason": "기준 운영 실행 결과를 찾지 못했다"}
        per = {s: self._tg_run_stats(d, w0, w1) for s, d in runs.items()}
        refs = {s: self._tg_run_stats(d, w0, w1) for s, d in (ref or {}).items()}

        def mean_ci(frames, col):
            m = pd.concat([f[col] for f in frames], axis=1)
            n = m.notna().sum(axis=1)
            mean, sd = m.mean(axis=1), m.std(axis=1, ddof=1)
            hw = pd.Series([stats.t.ppf(0.975, k - 1) * v / np.sqrt(k) if k >= 2 else np.nan for k, v in zip(n, sd)], index=m.index)
            return mean, hw

        cols = {}
        for col in ("util", "avail", "down", "pm", "queue"):
            cols[col], cols[col + "_ci"] = mean_ci(list(per.values()), col)
        tab = pd.DataFrame(cols)
        pairs = sorted(set(per) & set(refs))
        if pairs:
            tab["ref_util"] = pd.concat([refs[s]["util"] for s in pairs], axis=1).mean(axis=1)
            tab["ref_queue"] = pd.concat([refs[s]["queue"] for s in pairs], axis=1).mean(axis=1)
            dd = [pd.DataFrame({"u": per[s]["util"] - refs[s]["util"], "q": per[s]["queue"] - refs[s]["queue"]}) for s in pairs]
            tab["du"], tab["du_ci"] = mean_ci(dd, "u")
            tab["dq"], _ = mean_ci(dd, "q")
        tab = tab[tab.index.isin(list(tgs))]
        tab["rank"] = tab["util"].rank(ascending=False, method="min")
        rows = []
        for t, r in tab.iterrows():
            row = {"툴그룹": t, "area": tgs[t]["area"], "장비 수": tgs[t]["tools"],
                   "가동률 (%)": _r(100 * r.util, 1), "가동률 95% CI 반폭 (%p)": _r(100 * r.util_ci, 1),
                   "가동률 순위": f"{int(r['rank'])} / {len(tab)}",
                   "가용률 (%)": _r(100 * r.avail, 1), "고장 (%)": _r(100 * r.down, 1), "PM (%)": _r(100 * r.pm, 1),
                   "대기 lot": _r(r.queue, 1)}
            if pairs:
                row.update({"비교 대상 가동률 (%)": _r(100 * r.ref_util, 1), "가동률 차이 (%p)": _r(100 * r.du, 2),
                            "차이 95% CI 반폭 (%p)": _r(100 * r.du_ci, 2), "비교 대상 대기 lot": _r(r.ref_queue, 1),
                            "대기 lot 차이": _r(r.dq, 1)})
            rows.append(row)
        if toolgroups:
            rows = [x for x in rows if x["툴그룹"] in toolgroups]
        if area:
            rows = [x for x in rows if x["area"] == area]
            by_util = sorted((x for x in rows if x["가동률 (%)"] is not None), key=lambda x: -x["가동률 (%)"])
            for x in rows:                                              # 11-09 (D-91, T-v2 A3 r3): 전체 순위를 area 안 순위로 쓰지 않게
                x["area 안 가동률 순위"] = (f"{next(i for i, y in enumerate(by_util, 1) if y is x)} / {len(by_util)}"
                                         if x["가동률 (%)"] is not None else None)
        key = self.TG_SORT[sort_by]
        if sort_by == "가동률 변화" and not pairs:
            return {"error": "가동률 변화 정렬은 비교 대상 결과가 있는 시나리오(scenario_id)일 때만 된다"}
        if sort_by == "가동률 변화":                                    # 변화 크기(절댓값) 순
            rows.sort(key=lambda x: (x[key] is None, abs(x[key] or 0) if ascending else -abs(x[key] or 0)))
        else:
            rows.sort(key=lambda x: (x[key] is None, (x[key] or 0) if ascending else -(x[key] or 0)))
        n_all = len(rows)
        if not toolgroups:
            rows = rows[:max(1, min(int(top_n or 10), 105))]
        out = {"status": "ok", "대상": target, "측정 창 (주)": [w0, w1], "seeds": sorted(per),
               "정렬": f"{sort_by} {'낮은' if ascending else '높은'} 순", "보인 행 / 해당 툴그룹": f"{len(rows)} / {n_all}",
               "정의": {"가동률": "쓸 수 있는 시간(가동 + 유휴) 중 가동 비율. 보고서의 가동률과 같음",
                       "가용률": "전체 시간 중 고장·PM 이 아닌 시간 비율", "고장·PM": "전체 시간 중 고장·PM 시간 비율",
                       "대기 lot": "그 툴그룹 앞에서 기다리는 lot 평균 (대기열 + 배치 대기 + 고장 대기)",
                       "가동률 순위": "전체 툴그룹 중 가동률 높은 순 (이 측정 창)"},
               "rows": rows,
               "notes": ["seed 평균. CI 는 seed 간 95% 신뢰구간 반폭", "수치는 이 결과에서 그대로 옮긴다"]}
        if pairs:
            out["비교 대상"] = ref_label
            out["notes"].append("차이는 같은 seed 끼리 짝지은 차이(시나리오 − 비교 대상)의 평균")
        if area and rows:
            sub = tab[[tgs[t]["area"] == area for t in tab.index]]
            out["area 평균"] = {"가동률 (%)": _r(100 * sub.util.mean(), 1), "가용률 (%)": _r(100 * sub.avail.mean(), 1),
                              "대기 lot 합": _r(sub.queue.sum(), 1)}
        out[_rt.BLOCK_KEY] = _rt.toolgroup_sentences(out)               # 11-09 (D-91): 숫자 문장은 도구가 씀
        return out

    # -- get_kpi --------------------------------------------------------------------------------------
    def _run_kpi(self, d: Path, w0: float, w1: float) -> dict:
        """한 실행 폴더의 KPI. 보고서(whatif_report.load_run)의 kpi 를 그대로 쓰고 lot 유형별·제품군별 납기를 더한다.
        보고서의 'hot'(hot+superhot 합침)은 이름을 'hot+superhot' 으로 바꾼다 (값은 같음)."""
        import pandas as pd
        import whatif_report as wr
        key = (str(d), float(w0), float(w1))
        cache = self.__dict__.setdefault("_kpi_cache", {})
        if key in cache:
            return cache[key]
        r = wr.load_run(Path(d), w0, w1)
        k = dict(r["kpi"])
        for old, new in (("처리량 hot (lot/주)", "처리량 hot+superhot (lot/주)"), ("CT hot (일)", "CT hot+superhot (일)"),
                         ("납기 준수 hot (%)", "납기 준수 hot+superhot (%)")):
            if old in k:
                k[new] = k.pop(old)
        k.update(self._lot_metrics(d, w0, w1, r["spec"]["spec"].get("grouping")))
        cache[key] = k
        return k

    @staticmethod
    def _lot_metrics(d: Path, w0: float, w1: float, grouping: str | None = None) -> dict:
        """lot 기록(lots.parquet)만으로 내는 lot 유형별·제품군별 지표 (11-07). 측정 창에 완료된 lot 기준.
        완료 lot 수는 측정 창 전체 합계 (주당 아님 — 주당은 처리량)."""
        import pandas as pd
        import whatif_report as wr
        lots = pd.read_parquet(Path(d) / "lots.parquet", columns=["product", "lot_type", "release_min", "due_min", "end_min", "status"])
        fin = lots[(lots.status == "completed") & (lots.end_min >= w0 * 168 * 60) & (lots.end_min < w1 * 168 * 60)].copy()
        fin["ct"] = (fin.end_min - fin.release_min) / 1440
        fin["ontime"] = fin.end_min <= fin.due_min
        k = {}
        for lt, label in (("regular", "regular"), ("hot", "hot만"), ("superhot", "superhot")):
            t = fin[fin.lot_type == lt]
            if lt != "regular":
                k[f"처리량 {label} (lot/주)"] = len(t) / (w1 - w0)
                k[f"CT {label} (일)"] = float(t.ct.mean()) if len(t) else float("nan")
                k[f"납기 준수 {label} (%)"] = 100 * float(t.ontime.mean()) if len(t) else float("nan")
            k[f"측정 창 완료 {label} (lot, 창 전체 합계)"] = float(len(t))
        reg = fin[fin.lot_type == "regular"].copy()
        reg["group"] = reg["product"].map(wr.product_groups(grouping))
        for g, t in reg.groupby("group"):
            k[f"납기 준수 regular {g} (%)"] = 100 * float(t.ontime.mean())
        return k

    def _lot_type_rows(self, runs: dict, refs: dict, w0: float, w1: float) -> dict:
        """보고서용 lot 유형별·제품군별 짝 비교 행 (11-07). runs·refs: {seed: 실행 폴더}. 같은 seed 끼리 짝.
        그 유형 lot 이 한쪽에 없으면(투입 중단 등) 차이 대신 '해당 없음'."""
        import numpy as np
        from scipy import stats
        pairs = sorted(set(runs) & set(refs))
        a = {s: self._lot_metrics(runs[s], w0, w1) for s in pairs}
        b = {s: self._lot_metrics(refs[s], w0, w1) for s in pairs}
        names = ["납기 준수 hot만 (%)", "납기 준수 superhot (%)", "CT hot만 (일)", "CT superhot (일)", "처리량 hot만 (lot/주)",
                 "처리량 superhot (lot/주)"] + sorted({k for v in a.values() for k in v if k.startswith("납기 준수 regular ")})
        rows = {}
        for n in names:
            x = np.array([a[s].get(n, np.nan) for s in pairs], dtype=float)
            y = np.array([b[s].get(n, np.nan) for s in pairs], dtype=float)
            row = {"비교 대상": _r(np.nanmean(y)) if np.isfinite(y).any() else None,
                   "시나리오": _r(np.nanmean(x)) if np.isfinite(x).any() else None}
            ok = np.isfinite(x) & np.isfinite(y)
            if not np.isfinite(x).any() or not np.isfinite(y).any():
                row["판정"] = "해당 없음 (한쪽에 이 유형의 완료 lot 없음)"
            elif ok.sum() >= 2:
                dd = x[ok] - y[ok]
                c = float(stats.t.ppf(0.975, len(dd) - 1) * dd.std(ddof=1) / np.sqrt(len(dd)))
                row.update({"차이": _r(dd.mean()), "차이 95% CI 반폭": _r(c),
                            "판정": ("유의하게 증가" if dd.mean() > 0 else "유의하게 감소") if abs(dd.mean()) > c
                            else NO_DIFF_LABEL})
            else:
                row["판정"] = "CI 없음 (짝 1개)"
            rows[n] = row
        rows["측정 창 완료 superhot (lot, seed 평균)"] = {
            "비교 대상": _r(np.mean([b[s].get("측정 창 완료 superhot (lot, 창 전체 합계)", 0) for s in pairs]), 1) if pairs else None,
            "시나리오": _r(np.mean([a[s].get("측정 창 완료 superhot (lot, 창 전체 합계)", 0) for s in pairs]), 1) if pairs else None}
        return rows

    def get_kpi(self, scenario_id: str | None = None, window_start_week: int | None = None,
                window_end_week: int | None = None) -> dict:
        """기준 운영 또는 실행한 시나리오의 KPI 값 (11-07 사용자: "기본상태에서 세 종류의 lot 별 납기준수율"을 답하지 못함).
        실행·비교는 하지 않는다 (비교는 get_report)."""
        import numpy as np
        from scipy import stats
        if scenario_id:
            rec = self._load(scenario_id)
            w0 = int(window_start_week if window_start_week is not None else rec["window_weeks"][0])
            w1 = int(window_end_week if window_end_week is not None else rec["window_weeks"][1])
            if w0 >= w1 and window_start_week is None and window_end_week is None:
                return {"status": "no_window", "reason": f"검증용 짧은 실행이라 측정 구간이 없다 (실행 {rec['spec']['end_week']}주 < 측정 시작 "
                                                         f"{w0}주). KPI 수치를 만들지 않는다. 기준 운영 값은 scenario_id 를 빼고 부른다"}
            if w1 > float(rec["spec"]["end_week"]):                     # 11-07: 실행 길이를 넘는 구간
                return {"status": "out_of_range", "reason": f"실행이 {float(rec['spec']['end_week']):g}주까지라 {w1}주까지는 볼 수 없다"}
            found = self._find_runs({**rec, "window_weeks": [w0, w1]}, DEFAULT_SEEDS)
            if not found:
                return {"status": "not_ready", "reason": "실행 결과가 없다. 확인받은 뒤 run_experiment 로 돌린 시나리오만 조회한다. "
                                                         "기준 운영 값은 scenario_id 를 빼고 부른다"}
            runs = {s: found[s] for s in sorted(found)}
            target = f"시나리오 {scenario_id} ({rec['description']})"
        else:
            w0 = int(window_start_week if window_start_week is not None else WARMUP_WEEK)
            w1 = int(window_end_week if window_end_week is not None else DEFAULT_END_WEEK)
            runs = self._base_dirs(w1)                                  # 11-07: 52주를 넘으면 같은 길이로 돌린 기준 운영
            target = "기준 운영"
            if not runs:
                return {"status": "not_ready", "reason": (f"{w1}주까지 돌린 기준 운영 결과가 없다 (저장된 기준 운영은 72주까지). "
                                                          "더 긴 기준은 그 길이의 실험을 돌릴 때 함께 만들어진다")
                        if w1 > DEFAULT_END_WEEK else "기준 운영 실행 결과를 찾지 못했다"}
        if w1 - w0 < 4:
            return {"error": f"측정 창이 너무 짧다 ({w0}~{w1}주)"}
        per = {s: self._run_kpi(d, w0, w1) for s, d in sorted(runs.items())}
        kpi = {}
        for k in dict.fromkeys(k for v in per.values() for k in v):
            vals = [per[s].get(k, float("nan")) for s in per]
            x = np.array([v for v in vals if v == v], dtype=float)
            if not len(x):
                continue
            hw = float(stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))) if len(x) >= 2 else None
            nd = 0 if k.startswith("측정 창 완료 ") else 2
            kpi[k] = {"평균": _r(x.mean(), nd), "95% CI 반폭": _r(hw, nd) if hw is not None else None,
                      "seed별": [_r(v, nd) for v in vals]}
        notes = ["seed 평균, CI 는 seed 간 95% 신뢰구간 반폭. 수치는 이 결과에서 그대로 옮긴다",
                 "hot+superhot 은 hot 과 superhot 을 합친 값 (get_report 의 hot+superhot 과 같음). 'hot lot'을 물으면 hot만 값을 쓴다"]
        small = [lt for lt in ("hot만", "superhot") if (kpi.get(f"측정 창 완료 {lt} (lot, 창 전체 합계)") or {}).get("평균") is not None
                 and kpi[f"측정 창 완료 {lt} (lot, 창 전체 합계)"]["평균"] < 30]
        if small:
            notes.append(f"{', '.join(small)} 는 seed 마다 측정 창 완료 lot 이 30개 미만이라 납기 준수율이 seed 마다 크게 흔들린다. "
                         "평균과 함께 CI·완료 lot 수를 같이 밝힌다. 이 주의는 납기 준수율 추정에 붙여 쓴다 — 완료 lot 수·처리량이 "
                         "흔들린다고 바꿔 쓰지 않는다 (완료 수의 seed별 값은 '측정 창 완료 …'의 seed별 그대로, 같을 수도 있음)")
        notes.append("'측정 창 완료 … (lot, 창 전체 합계)' 는 측정 창 전체에서 완료된 lot 수다 (주당 값이 아님, 주당은 처리량 lot/주)")
        if any((kpi.get(f"측정 창 완료 {lt} (lot, 창 전체 합계)") or {}).get("평균") == 0 for lt in ("hot만", "superhot")):
            notes.append("완료 lot 이 0 인 유형(투입 중단 등)은 납기 준수율·CT 가 없다 → '해당 없음'으로 쓴다")
        result = {"status": "ok", "대상": target, "측정 창 (주)": [w0, w1], "seeds": sorted(per),
                "정의": {"납기 준수": "측정 창 동안 완료된 lot 중 완료 시각 ≤ 납기인 비율 (%)",
                         "lot 유형": "regular(일반)·hot·superhot 세 종류. 'hot만'은 hot 하나, 'hot+superhot'은 둘을 합친 값 (보고서의 hot)",
                         "처리량": "측정 창 주당 완료 lot (regular 기준, '전체'는 모든 유형)",
                         "CT": "투입부터 완료까지 일수 (측정 창에 완료된 lot 평균)",
                         "WIP": "측정 창 동안 공정 안에 있는 lot 평균",
                         "측정 창 완료 lot": "측정 창 전체에서 완료된 lot 수 (seed 하나당 합계, 주당 아님)"},
                "kpi": kpi, "notes": notes}
        result[_rt.BLOCK_KEY] = _rt.kpi_sentences(result)               # 11-09 (D-91): 숫자 문장은 도구가 씀
        return result
