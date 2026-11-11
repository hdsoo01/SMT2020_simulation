"""답 마무리 코드 (11-06 밤, 사람 판정 확정 결과 반영). LLM 답 글을 사용자에게 내보내기 직전에 한 번 본다.

1. 가동률·가용률 첫 정의 (사용자 결정: 같은 대화에서 실측 가동률을 처음 보여 줄 때 1회, 보고서 보조 절 포함)
   - 이번 차례에 실측 결과를 돌려준 도구(get_report ok, get_toolgroup_stats ok)가 있고 답에 그 용어와 % 수치가 있을 때만 본다
     (확인 카드·사전 점검의 '정적 가동률'은 다른 지표라 제외)
   - 모델이 정의를 이미 썼으면 붙이지 않고 '설명함'으로 기록. 한 번 설명한 용어는 그 대화에서 다시 붙이지 않는다
2. 보고서 부호 대조: 이번 차례 보고서의 부호 있는 수치(창 동안 WIP 변화 %, 창 끝 누적 완료 차이)가 답에 반대 부호로
   옮겨졌으면 도구 값을 밝히는 줄을 붙인다 (11-06 확인 실행: 도구 +0.9% 를 답에 −0.9% 로 씀)
3. 카드·id 대조 (11-07 팀원 요청): 떠 있는 카드가 없는데 버튼을 누르라고 하면, 또는 만들어지지 않은 시나리오 id 를
   안내하면 바로잡는 줄을 붙인다 (11-06 23:45 확인 실행: '식각' 가정이 막힌 뒤 "카드를 준비했으니 버튼을 눌러 달라"고 답함)
4. 선택지 대조 (11-07 팀원 추가 요청): '식각'처럼 여러 area 에 걸리는 명칭이 아직 정해지지 않았는데 답이 후보를 모두
   보이지 않으면(한쪽을 전제로 물음) 후보를 모두 밝히는 줄을 붙인다 (V-v1: "Dry_Etch 전체로 이해하면 될까요?")
5. hot 값 대조 (11-08 사용자 D-80): 보고서·KPI 조회의 hot+superhot(합친 값)이 'hot' 이라는 말과 함께 합쳤다는 표시 없이 나오면
   hot만 값을 밝히는 줄을 붙인다 (T-v1 H2 r2: hot만 74.18% 의 기준을 합친 69.87% 로 씀)
6. 보고·설명 대조 (11-08 밤, T-v2 공동 판정 실패 10행). 프롬프트 지시만으로는 재발해서(1판 수정 #7·#8) 도구 값과 대조한다.
   글을 직접 고치는 것은 뜻이 분명한 세 가지뿐: 가동률 CI 반폭을 '+변화'로 쓴 것(42) → '±', 사용자가 말한 값 뒤의 '(기본값)'(64) 삭제,
   5열 표의 '차이 | ±CI' 6칸 행(138) → 한 칸. 나머지는 ※ 줄을 붙인다: 유의하지 않은 지표를 '유지·늘지 않음'으로 단정(88·134),
   유의한 납기 항목이 있는데 납기 전반을 '차이 확인 못함'으로 묶음(44·90), 기본값 목록에 사용자가 말한 값(106),
   납기 준수율 표본 주의를 완료 수 흔들림으로 바꿈(97), 등록되지 않은 툴그룹 이름(116), 본문의 '기준 → 시나리오' 수치가 도구 값과 다름(138).
   11-09 (D-87, D-89): 회복 주 대조 — 도구의 WIP 회복 판정(sllmt_core.recovery, baseline_band_v1)과 다른 주를 'N주에 회복'으로
   쓰거나 회복이 확인되지 않았는데 단정하면 ※ 줄 (T-v2 X4: 한 주만 최고의 25% 아래였던 60주를 회복으로 씀)

대화별 상태(state)는 Agent 가 들고 있다: {"defined": set()}. 덧붙인 것은 반환값으로 알려 기록에 남긴다.
"""
from __future__ import annotations

import re

from .recovery import AFTER_KEY, AUX_KEY, BAND_KEY, ENTRY_KEY, STATUS_KEY
from .run_gate import TOOLGROUP_RE, extract_facts

UTIL_DEF = ("가동률 = 쓸 수 있는 시간(작업 + 유휴) 중 실제 작업한 시간의 비율 (시뮬레이션 실측). "
            "확인 카드·사전 점검의 '정적 가동률'(투입량으로 계산한 부하)과는 다른 지표")
AVAIL_DEF = "가용률 = 전체 시간 중 고장·PM 이 아닌 시간(작업 + 유휴)의 비율"
# 11-10 (사용자: 공정을 잘 모르는 관리자도 읽게): 결과·KPI 를 이 대화에서 처음 보일 때 한 번 붙이는 용어 설명
TERMS_DEF = ("용어: regular(일반 lot), hot(긴급 lot), superhot(가장 급한 lot), 처리량(주당 완료 lot 수), "
             "WIP(공장 안에 있는 lot 수), CT(투입부터 완료까지 걸린 일수), 납기 준수(납기 안에 끝난 lot 비율), "
             "%p(퍼센트 포인트: 98% 에서 97% 는 −1%p), seed(같은 조건의 반복 실험), "
             "±(95% 신뢰구간 반폭: 반복 실험으로 본 오차 범위. 차이가 이 값보다 작으면 0일 수도 있어 '유의한 차이 확인 못함')")
TERMS_TOOLS = ("get_report", "get_kpi")
MEASURED_TOOLS = ("get_report", "get_toolgroup_stats")

UTIL_RE = re.compile(r"(?<!정적 )(?<!정적)가동률")
AVAIL_RE = re.compile(r"가용률")
PCT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
UTIL_DEF_RE = re.compile(r"가동률[^\n]{0,40}?(?:쓸\s*수\s*있는|가용\s*시간|사용\s*가능한\s*시간|작업\s*\+\s*유휴|유휴\s*\+\s*작업)")
AVAIL_DEF_RE = re.compile(r"가용률[^\n]{0,50}?(?:고장|PM)[^\n]{0,20}?(?:아닌|제외|뺀|빼)")
SIGN = {"+": 1, "-": -1, "−": -1, "–": -1}
BUTTON_ASK_RE = re.compile(r"버튼을\s*(?:눌러\s*주|누르시면|누르면|눌러\s*주시면|클릭)|/승인\s*(?:을|를|으로|입력)")   # 11-10: 명사형 '누르면 됨'
SID_RE = re.compile(r"(?<![A-Za-z0-9_])(Q[0-9a-f]{8}|scenario_\d+)(?![A-Za-z0-9_])")


def _signed_hits(text: str, value: float, context: tuple, window: int = 50) -> list:
    """답에서 |value| 와 같은 수(소수 첫째 자리)가 context 낱말 뒤 window 글자 안에 부호와 함께 나온 곳: [(부호, 원문)]."""
    a = f"{abs(value):.1f}"
    out = []
    for m in re.finditer(rf"([+\-−–])\s?{re.escape(a)}(?!\d)", text):
        before = text[max(0, m.start() - window):m.start()]
        if any(c in before for c in context):
            out.append((SIGN[m.group(1)], m.group(0)))
    return out


def sign_checks(text: str, turn_results: list) -> list:
    """보고서 부호 대조. 반대 부호로 옮긴 곳마다 바로잡는 줄."""
    lines = []
    for name, res in turn_results:
        if name != "get_report" or (res or {}).get("status") != "ok":
            continue
        cands = [("측정 창 동안 WIP 변화", ((res.get("정상상태") or {}).get("창 동안 WIP 변화 (%)")), "%", ("창 동안", "WIP 변화", "WIP가", "WIP 가")),
                 ("창 끝 누적 완료 regular lot 차이", ((res.get("주별 궤적") or {}).get("창 끝 누적 완료 regular lot 차이")), " lot",
                  ("누적 완료",))]
        for label, v, unit, ctx in cands:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or round(abs(v), 1) == 0:
                continue
            want = 1 if v > 0 else -1
            bad = [raw for s, raw in _signed_hits(text, v, ctx) if s != want]
            if bad:
                lines.append(f"도구 값 확인: {label}는 {v:+.1f}{unit} (보고서 값). 위 글의 '{bad[0]}'는 부호가 반대")
    return lines


def card_checks(text: str, turn_results: list, card_pending: bool | None, known_ids: set | None) -> list:
    """카드·id 대조 (11-07 팀원 요청: 카드가 없는데 버튼을 누르라고 하거나 없는 실행 id 를 안내하지 않음).
    card_pending/known_ids 가 None 이면(실행 확인 코드 꺼짐) 보지 않는다."""
    lines = []
    if card_pending is False and BUTTON_ASK_RE.search(text):
        why = next((r.get("reason") for n, r in reversed(turn_results)
                    if n == "build_scenario" and (r or {}).get("status") in ("value_problem", "target_mismatch")), None)
        lines.append("아직 '확인 후 실행' 카드가 없음. 이번 차례에 시나리오가 만들어지지 않아 위 안내의 버튼은 화면에 없음"
                     + (f" (만들지 않은 이유: {why[:120]})" if why else "") + ". 빠진 값이나 고칠 내용을 알려 주면 카드를 띄움")
    if known_ids is not None:
        bad = sorted({m for m in SID_RE.findall(text) if m not in known_ids})
        if bad:
            lines.append(f"답에 나온 시나리오 id {', '.join(bad)} 는 이 대화에서 만들어진 적이 없음")
    return lines


def alias_checks(text: str, turn_results: list, ambiguous: list | None) -> list:
    """여러 area 에 걸리는 명칭(식각 → Dry_Etch·Wet_Etch)이 아직 정해지지 않았는데 답이 후보를 모두 보이지 않으면 (한쪽만
    말하거나 전제로 물으면) 후보를 모두 밝히는 줄 (11-07 팀원 추가 요청: 한쪽으로 유도하지 않음)."""
    if not ambiguous or any(n == "build_scenario" and (r or {}).get("status") == "ok" for n, r in turn_results):
        return []
    return [f"'{w}'에는 {'·'.join(areas)} 가 있음. 어느 쪽인지(또는 모두인지) 골라 주기 바람"
            for w, areas in ambiguous if not all(re.search(rf"(?<![A-Za-z_]){a}(?![A-Za-z_])", text) for a in areas)]


HOT_METRICS = (("납기 준수", "%"), ("CT", "일"), ("처리량", " lot/주"))
# 11-10: 숫자가 같아도 그 줄이 다른 지표면 보지 않는다 (사용자 확인 '자세히': 세부 표의 '처리량 hot만 +0.02' 를
# 'CT hot+superhot 차이 0.02' 로 오인해 고쳐 쓰게 했고, 고쳐 쓴 답이 전체 보고서로 돌아감. 3판 F3 오탐과 같은 종류)
HOT_METRIC_WORD_RE = {"납기 준수": re.compile(r"납기"), "CT": re.compile(r"CT|사이클\s*타임"), "처리량": re.compile(r"처리량|생산량")}
HOT_WORD_RE = re.compile(r"hot|핫", re.I)
HOT_COMBINED_WORD_RE = re.compile(r"superhot|super\s*hot|슈퍼|합친|합산|합계|hot\s*\+|\+\s*superhot|hot·superhot|hot와\s*superhot|hot\s*과\s*superhot", re.I)
NUM_TOKEN_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])")
SEG_SPLIT_RE = re.compile(r"\n|(?<=[다음함됨요])\.\s|(?<=[.!?])\s")


def _remember_hot(turn_results: list, state: dict) -> None:
    """보고서·KPI 조회에서 hot+superhot(합친 값)과 hot만 값을 짝으로 기억 (대화별 state['hot_pairs'])."""
    pairs = state.setdefault("hot_pairs", [])
    for name, res in turn_results:
        res = res or {}
        if res.get("status") != "ok":
            continue
        for metric, unit in HOT_METRICS:
            if name == "get_report":
                kpi, lot = res.get("kpi") or {}, res.get("lot 유형별·제품군별 (짝 비교)") or {}
                comb = next((kpi[k] for k in kpi if k.startswith(f"{metric} hot") and "hot만" not in k
                             and ("superhot" in k or k.startswith(f"{metric} hot ("))), None)
                only = next((lot[k] for k in lot if k.startswith(f"{metric} hot만")), None)
                if comb and only:
                    for side in ("비교 대상", "시나리오", "차이"):
                        a, b = comb.get(side), only.get(side)
                        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                            pairs.append({"metric": metric, "unit": unit, "side": side, "comb": float(a), "only": float(b)})
            elif name == "get_kpi":
                kpi = res.get("kpi") or {}
                comb = kpi.get(f"{metric} hot+superhot ({'%' if unit == '%' else unit.strip()})") or \
                    next((kpi[k] for k in kpi if k.startswith(f"{metric} hot+superhot")), None)
                only = next((kpi[k] for k in kpi if k.startswith(f"{metric} hot만")), None)
                if comb and only and isinstance(comb.get("평균"), (int, float)) and isinstance(only.get("평균"), (int, float)):
                    pairs.append({"metric": metric, "unit": unit, "side": res.get("대상") or "조회", "comb": float(comb["평균"]),
                                  "only": float(only["평균"])})


def hot_checks(text: str, turn_results: list, state: dict) -> list:
    """hot만 값과 hot+superhot(합친 값) 섞음 점검 (11-08, 사용자 D-80; T-v1 H2 r2: hot만 74.18% 를 말하며 기준을 합친 69.87% 로 씀).
    합친 값이 'hot' 이라는 말과 함께 나오고 그 문장에 superhot·합친 같은 말이 없으면 hot만 값을 밝히는 줄을 붙인다."""
    _remember_hot(turn_results, state)
    out, seen = [], set()
    for seg in SEG_SPLIT_RE.split(text or ""):
        if not HOT_WORD_RE.search(seg) or HOT_COMBINED_WORD_RE.search(seg):
            continue
        nums = [float(m.group(1)) for m in NUM_TOKEN_RE.finditer(seg)]
        for p in state.get("hot_pairs", []):
            if round(abs(p["comb"]), 2) == round(abs(p["only"]), 2):
                continue
            if not HOT_METRIC_WORD_RE[p["metric"]].search(seg):            # 이 줄은 그 지표 이야기가 아님
                continue
            if any(abs(n - abs(p["comb"])) < 0.006 for n in nums) and not any(abs(n - abs(p["only"])) < 0.006 for n in nums):
                key = (p["metric"], p["side"], p["comb"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(f"hot 값 확인: {abs(p['comb']):g}{p['unit'].strip() if p['unit'] == '%' else p['unit']} 는 hot+superhot 을 합친 "
                           f"{p['metric']} ({p['side']}) 입니다. hot만 {p['metric']} ({p['side']}) 은 {p['only']:g}"
                           f"{p['unit'].strip() if p['unit'] == '%' else p['unit']}")
    return out


# ---- 6. 보고·설명 대조 (11-08 밤, T-v2 공동 판정) ------------------------------------------------------------
FAMILIES = ("처리량", "CT", "WIP", "납기")
FAMILY_RE = {"처리량": re.compile(r"처리량|생산량"), "CT": re.compile(r"CT|사이클\s*타임"),
             "WIP": re.compile(r"WIP|재공"), "납기": re.compile(r"납기")}
LOT_QUALS = ("regular", "hot+superhot", "hot만", "superhot")               # 납기 묶음의 lot 유형 행
QUAL_WORD_RE = re.compile(r"regular|일반\s*lot|hot|superhot|핫|A_long|B_mid|C_short", re.I)
SIG_WORD_RE = re.compile(r"유의하게|유의한\s*(?:감소|증가|악화|개선|하락|상승)")
NO_CHANGE_RE = re.compile(r"유지(?:되|됨|된|돼|하고|하며|했)|늘지\s*않|줄지\s*않|늘어나지\s*않|줄어들지\s*않|변화(?:가|는)?\s*없|"
                          r"차이(?:가|는)?\s*없|변함(?:이|은)?\s*없|효과(?:가|는)?\s*없|영향(?:이|은)?\s*없|그대로(?:임|였|이었|유지)|"
                          r"정체(?:됨|되|된|돼|하|했|임|$|[\s.,)])")              # 11-09 (D-91): T-v2 X2 r3 '처리량 정체' 단정
NO_DIFF_CLAIM_RE = re.compile(r"차이\)?\s*(?:를|은|는|가|이|도)?\s*(?:확인(?:하지|되지|할\s*수)\s*(?:못|않|없)|확인\s*못|보이지\s*않|없)")
NEGATED_RE = re.compile(r"뜻이\s*아|뜻은\s*아|의미가\s*아|의미는\s*아|아니라|단정(?:하지|할\s*수)")
FLUCT_RE = re.compile(r"흔들|변동(?:이|폭이)?\s*크|편차(?:가)?\s*크|들쭉날쭉")
DEFAULT_PAREN_RE = re.compile(r"[ \t]*\((?:기본값|기본)\)")
DEFAULT_LABEL_RE = re.compile(r"^(?P<label>[^:\n]*(?:기본값|기본 값|말하지 않은|말씀하지 않은)[^:\n]*):(?P<items>.+)$", re.M)
SRC_OK_RE = re.compile(r"요청|사용자\s*지정|말씀|지정한|직접|질의|입력|말한|주신")
ARROW_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(?:lot/주|/주|lot|일|%p|%)?\s*(?:→|->|에서)\s*(\d[\d,]*(?:\.\d+)?)")
SENT_SPLIT_RE = re.compile(r"\n|(?<=[다음함됨요])\.\s|(?<=[.!?])\s(?=\S)")
NUM_SIGNED = r"[+\-−]?\s*\d[\d,]*(?:\.\d+)?"


def _f(s: str) -> float:
    return float(str(s).replace(",", "").replace("−", "-").replace(" ", ""))


def _num_eq(tok: str, val) -> bool:
    """답의 숫자 글(tok)이 도구 값(val)과 같은가 (답에 적힌 자릿수로 반올림해 비교)."""
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return False
    t = str(tok).replace(",", "")
    nd = len(t.split(".")[1]) if "." in t else 0
    x = _f(t)
    return abs(round(float(val), nd) - x) < 1e-9 or abs(float(val) - x) < 0.006


def _is_table_line(line: str) -> bool:
    return line.count("|") >= 3


def _sentences(text: str) -> list:
    """표가 아닌 줄의 문장들."""
    out = []
    for line in (text or "").splitlines():
        if _is_table_line(line):
            continue
        out += [s for s in SENT_SPLIT_RE.split(line) if s and s.strip()]
    return out


def _row_unit(name: str) -> str:
    m = re.search(r"\(([^)]*)\)\s*$", name)
    u = m.group(1) if m else ""
    return "%p" if u == "%" else u if u in ("일", "") else " " + u


def _clause_start(sent: str, pos: int) -> int:
    """pos 앞에서 괄호 밖의 마지막 쉼표·쌍반점 다음 위치 (주장의 주어가 있는 구간의 시작)."""
    depth = 0
    for i in range(pos - 1, -1, -1):
        ch = sent[i]
        if ch in ")）":
            depth += 1
        elif ch in "(（":
            depth = max(0, depth - 1)
        elif ch in ",，;" and depth == 0:
            return i + 1
    return 0


def _subjects(sent: str, pos: int) -> tuple[set, str]:
    """pos 의 주장(차이 확인 못함·유지 등)이 가리키는 지표 묶음: 같은 구간에서 주장 앞에 나온 지표 낱말. (묶음, 그 구간 글)."""
    span = sent[_clause_start(sent, pos):pos]
    return {f for f in FAMILIES if FAMILY_RE[f].search(span)}, span


def _row_qual(name: str, family: str) -> str:
    k = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    k = k[len("납기 준수"):] if family == "납기" else k[len(family):]
    return k.strip()


def report_rows(turn_results: list) -> list:
    """이번 차례 get_report(ok) 의 지표 행: kpi 와 lot 유형별·제품군별 표. {name, family, qual, base, scen, diff, ci, verdict, unit}."""
    rows = []
    for name, res in turn_results:
        res = res or {}
        if name != "get_report" or res.get("status") != "ok":
            continue
        for table in ("kpi", "lot 유형별·제품군별 (짝 비교)"):
            for k, v in (res.get(table) or {}).items():
                if not isinstance(v, dict) or not isinstance(v.get("차이"), (int, float)):
                    continue
                fam = next((f for f in FAMILIES if k.startswith("납기 준수" if f == "납기" else f)), None)
                if fam is None:
                    continue
                rows.append({"name": k, "family": fam, "qual": _row_qual(k, fam), "base": v.get("비교 대상"),
                             "scen": v.get("시나리오"), "diff": float(v["차이"]), "ci": v.get("차이 95% CI 반폭"),
                             "verdict": str(v.get("판정") or ""), "unit": _row_unit(k)})
    return rows


def _sig(r: dict) -> bool:
    return r["verdict"].startswith("유의하게")


def _desc(r: dict) -> str:
    ci = f" ±{r['ci']:g}" if isinstance(r.get("ci"), (int, float)) else ""
    base = f"{r['base']:g} → {r['scen']:g}, " if isinstance(r.get("base"), (int, float)) and isinstance(r.get("scen"), (int, float)) else ""
    return f"{r['name']} {base}차이 {r['diff']:+g}{ci}{r['unit']}"


def fix_util_ci(text: str, turn_results: list) -> tuple[str, int]:
    """'가동률 상위 5 (시나리오)'의 CI 반폭을 '+변화'처럼 쓴 곳 (T-v2 X2 r1: 'Litho_FE_111 (97.3%, +0.47%p)') → '±'.
    같은 툴그룹의 실제 변화(기준 대비 차이)가 CI 반폭과 같은 수치면 뜻을 알 수 없으므로 고치지 않는다."""
    n = 0
    for name, res in turn_results:
        res = res or {}
        if name != "get_report" or res.get("status") != "ok":
            continue
        base = {t.get("툴그룹"): t.get("가동률 (%)") for t in res.get("가동률 상위 5 (비교 대상)") or []}
        chg = {t.get("툴그룹"): t.get("차이 (%p)") for t in res.get("가동률이 가장 많이 바뀐 툴그룹") or []}
        for t in res.get("가동률 상위 5 (시나리오)") or []:
            tg, u = t.get("툴그룹"), t.get("가동률 (%)")
            ci = next((t[k] for k in ("가동률 95% CI 반폭 (%p, 변화량 아님)", "가동률 95% CI 반폭 (%p)", "CI (%p)") if k in t), None)
            if not tg or not isinstance(u, (int, float)) or not isinstance(ci, (int, float)) or ci == 0:
                continue
            if _num_eq(f"{ci:g}", chg.get(tg)):                     # 도구가 준 변화량과 같은 수치면 뜻을 알 수 없음
                continue
            ci_alt = "|".join(re.escape(x) for x in dict.fromkeys((f"{ci:g}", f"{ci:.2f}", f"{ci:.1f}")))
            u_pat = rf"{re.escape(f'{u:g}')}(?:\.0+)?" if float(u).is_integer() else rf"{re.escape(f'{u:g}')}0*"
            pat = re.compile(rf"({re.escape(tg)}[^\n|]{{0,25}}?(?<![\d.]){u_pat}\s*%\s*[,/]?\s*)\+\s*({ci_alt})(\s*%p)")
            b = base.get(tg)

            def repl(m):
                nonlocal n                                             # 적힌 자릿수로 '시나리오 − 기준' 과 같으면 변화일 수도 있어 둔다
                if isinstance(b, (int, float)) and _num_eq(m.group(2), u - b):
                    return m.group(0)
                n += 1
                return f"{m.group(1)}±{m.group(2)}{m.group(3)}"

            text = pat.sub(repl, text)
    return text, n


def fix_table_columns(text: str) -> tuple[str, int]:
    """머리글보다 한 칸 많은 표 행에서 '차이 | ±CI' 로 나뉜 두 칸을 한 칸으로 (T-v2 X4 r3: 5열 머리글에 6칸 행)."""
    lines, n, i = (text or "").split("\n"), 0, 0

    def cells(line):
        s = line.strip()
        s = s[1:] if s.startswith("|") else s
        s = s[:-1] if s.endswith("|") else s
        return [c.strip() for c in s.split("|")]

    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip().startswith("|"):
            j += 1
        n0 = len(cells(lines[i]))
        for k in range(i + 1, j):
            cs = cells(lines[k])
            if len(cs) != n0 + 1:
                continue
            for a in range(len(cs) - 1):
                if re.fullmatch(NUM_SIGNED, cs[a]) and re.fullmatch(r"±\s*\d[\d,]*(?:\.\d+)?", cs[a + 1]):
                    cs = cs[:a] + [f"{cs[a]} {cs[a + 1].replace(' ', '')}"] + cs[a + 2:]
                    lines[k] = "| " + " | ".join(cs) + " |"
                    n += 1
                    break
        i = j
    return "\n".join(lines), n


def _user_vals(user_texts: list) -> set:
    return {(f.unit, round(f.value, 4)) for f in extract_facts([t for t in (user_texts or []) if t])
            if f.unit in ("week", "pct", "count", "x", "day")}


PAREN_RE = re.compile(r"\(([^()]*)\)")
DERIVED_RE = re.compile(r"기준\s*운영|동시|함께|측정|예상|추정|약\s|환산|따라|이므로|없어|없으므로|않아|생략")   # '기간이 없어 …' = 빠진 값의 기본값 설명


def _strip_notes(seg: str) -> str:
    """설명 괄호(낱말이 든 괄호: '(측정은 30주부터이므로 …)')는 빼고, 값만 든 괄호('(30주)')는 남긴다."""
    def keep(m):
        rest = re.sub(r"\d+(?:\.\d+)?\s*(?:%|주|일|대|배|lot)?|[~·,\s]", "", m.group(1))
        return m.group(0) if len(re.findall(r"[가-힣]", rest)) < 2 else " "
    return PAREN_RE.sub(keep, seg)


def _user_hits(seg: str, uv: set) -> list:
    return [f.text for f in extract_facts([_strip_notes(seg)]) if (f.unit, round(f.value, 4)) in uv]


def fix_default_labels(text: str, user_texts: list) -> tuple[str, list]:
    """사용자가 말한 값 바로 뒤의 '(기본값)' 삭제 (T-v2 D1 r2: '40주부터 3주 정지 (기본값)'; 사용자 결정 = 삭제).
    반환: (고친 글, 삭제한 곳의 사용자 값 목록)."""
    uv = _user_vals(user_texts)
    if not uv:
        return text, []
    done = []

    def repl(m):
        start = m.start()
        seg = text[max(text.rfind(c, 0, start) for c in "\n,;:") + 1:start]
        hits = _user_hits(seg, uv)
        if hits:
            done.append("·".join(dict.fromkeys(hits)))
            return ""
        return m.group(0)

    return DEFAULT_PAREN_RE.sub(repl, text), done


def _split_items(s: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch in "(（"
        depth -= ch in ")）"
        if ch in ",，" and depth <= 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [x.strip() for x in out + [cur] if x.strip()]


def source_checks(text: str, user_texts: list) -> list:
    """'기본값 (사용자가 말하지 않은 값): …' 같은 목록에 사용자가 말한 값이 들어 있으면 출처를 밝히는 줄 (T-v2 B8 r3: 시작 주 30주)."""
    uv = _user_vals(user_texts)
    if not uv:
        return []
    out = []
    for m in DEFAULT_LABEL_RE.finditer(text or ""):
        for it in _split_items(m.group("items")):
            hits = _user_hits(it, uv)
            if not hits or SRC_OK_RE.search(it) or DERIVED_RE.search(_strip_notes(it)):
                continue                                               # 출처를 밝혔거나, 사용자 값에서 나온 파생 조건(같은 길이 기준 운영 등)
            item = it.strip(" *.")
            item = item if len(item) <= 40 else item[:40] + "…"
            out.append(f"값 출처 확인: '{item}' 의 {'·'.join(dict.fromkeys(hits))}는 사용자가 말한 값임 (기본값·말하지 않은 값이 아님)")
    return out


def no_change_checks(text: str, rows: list) -> list:
    """유의한 차이를 확인하지 못한 지표를 '유지·늘지 않음·변화 없음'으로 단정 (T-v2 X2 r2 '처리량은 … 유지되나', r3 '처리량은 늘지 않고')."""
    out, seen = [], set()
    for sent in _sentences(text):
        if "창 동안" in sent:                                          # ⑥ 신뢰도의 정상상태 설명 ("WIP 가 계속 늘지는 않음")
            continue
        for cm in NO_CHANGE_RE.finditer(sent):
            if NEGATED_RE.search(sent[cm.end():cm.end() + 40]):
                continue
            fams, span = _subjects(sent, cm.start())
            for fam in fams:
                cand = [r for r in rows if r["family"] == fam]
                named = [r for r in cand if r["qual"] and r["qual"] in span]
                main = named or sorted((r for r in cand if r["qual"] in (("전체", "") if fam != "납기" else ("regular",))),
                                       key=lambda r: r["qual"] != "전체")
                nd = [r for r in main if not _sig(r)]
                if nd and len(nd) == len(main) and fam not in seen:
                    seen.add(fam)
                    out.append(f"판정 확인: {_desc(nd[0])} 는 CI 가 0 을 포함해 유의한 차이를 확인하지 못한 것임. "
                               f"변화가 없다('{cm.group(0)}…')고 확인된 것은 아님")
    return out


def scope_checks(text: str, rows: list) -> list:
    """납기 전반을 '유의한 차이 확인 못함'으로 묶었는데 유의하게 바뀐 lot 유형 납기가 있으면 그 항목을 밝히는 줄
    (T-v2 X3 r1·r2: 'CT와 납기 준수율은 유의한 차이를 확인하지 못함', 표의 hot만·hot+superhot 납기는 유의 감소).
    '유의한 개선 미확인'은 악화와 모순되지 않으므로 보지 않는다. regular·hot 등 유형을 밝힌 문장도 보지 않는다."""
    sents = _sentences(text)

    def stated(r):                                                     # 다른 문장에서 이미 그 유형의 유의한 변화를 밝힘
        word = r["qual"].replace("만", "").split("+")[0]
        return bool(word) and any(SIG_WORD_RE.search(s) and word in s for s in sents)

    out, seen = [], set()
    for sent in sents:
        for cm in NO_DIFF_CLAIM_RE.finditer(sent):
            if NEGATED_RE.search(sent[cm.end():cm.end() + 40]):
                continue
            fams, span = _subjects(sent, cm.start())
            if "납기" in fams and not QUAL_WORD_RE.search(span):
                sig = [r for r in rows if r["family"] == "납기" and r["qual"] in LOT_QUALS and _sig(r)
                       and r["name"] not in seen and not stated(r)]
                if sig:
                    seen.update(r["name"] for r in sig)
                    out.append("범위 확인: 위 '유의한 차이 확인 못함'은 납기 전체에 해당하지 않음. "
                               + "; ".join(f"{_desc(r)} 로 {r['verdict']}" for r in sig))
            for fam in fams - {"납기"}:
                if QUAL_WORD_RE.search(span):
                    continue
                for r in rows:
                    if r["family"] == fam and r["qual"] in ("", "전체") and _sig(r) and r["name"] not in seen and not stated(r):
                        seen.add(r["name"])
                        out.append(f"판정 확인: {_desc(r)} 로 {r['verdict']} (위 '유의한 차이 확인 못함'과 다름)")
    return out


def number_checks(text: str, rows: list) -> list:
    """본문의 '기준 → 시나리오' 수치가 같은 지표 행의 도구 값과 다르면 바로잡는 줄 (T-v2 X4 r3: '399.4/주 → 399.95/주', 도구 기준 404.4)."""
    out, seen = [], set()
    for sent in _sentences(text):
        for m in ARROW_RE.finditer(sent):
            a, c = m.group(1), m.group(2)
            for r in rows:
                if not FAMILY_RE[r["family"]].search(sent) or not isinstance(r.get("base"), (int, float)) \
                        or not isinstance(r.get("scen"), (int, float)):
                    continue
                if _num_eq(a, r["base"]) and _num_eq(c, r["scen"]):
                    break                                               # 맞는 행이 있으면 그 쌍은 바름
            else:
                for r in rows:
                    if not FAMILY_RE[r["family"]].search(sent) or not isinstance(r.get("base"), (int, float)) \
                            or not isinstance(r.get("scen"), (int, float)):
                        continue
                    for side, tok, val, other_ok in (("비교 대상", a, r["base"], _num_eq(c, r["scen"])),
                                                     ("시나리오", c, r["scen"], _num_eq(a, r["base"]))):
                        near = abs(_f(tok) - val) <= 0.05 * max(abs(val), 1e-9)
                        if other_ok and near and not _num_eq(tok, val) and (r["name"], side) not in seen:
                            seen.add((r["name"], side))
                            out.append(f"도구 값 확인: {r['name']} 의 {side} 값은 {val:g} (위 글의 '{tok}'는 도구 값과 다름)")
    return out


def warning_checks(text: str, turn_results: list) -> list:
    """완료 lot 이 적어 '납기 준수율'이 흔들린다는 도구 주의를 완료 수·처리량의 흔들림으로 바꿔 쓴 곳 (T-v2 A5 r3:
    'seed마다 완료 lot 수가 적고 흔들림이 큼', 실제 완료 수는 seed별 [9, 9, 9, 9, 9])."""
    out = []
    for name, res in turn_results:
        res = res or {}
        if name != "get_kpi" or res.get("status") != "ok" or not any("30개 미만" in str(n) for n in res.get("notes") or []):
            continue
        kpi = res.get("kpi") or {}
        small = [(lt, kpi[k]) for lt in ("hot만", "superhot") for k in [f"측정 창 완료 {lt} (lot, 창 전체 합계)"]
                 if isinstance((kpi.get(k) or {}).get("평균"), (int, float)) and kpi[k]["평균"] < 30]
        if not small:
            continue
        bad = [s for s in _sentences(text) if FLUCT_RE.search(s) and "납기" not in s]
        if bad:
            lt, v = small[-1] if any("superhot" in s for s in bad) or len(small) == 1 else small[0]
            per = [float(x) if isinstance(x, (int, float)) else x for x in v.get("seed별") or []]
            same = len(set(per)) == 1 and per
            per = "[" + ", ".join(f"{x:g}" if isinstance(x, float) else str(x) for x in per) + "]"
            out.append(f"표본 주의 확인: 불확실성이 큰 것은 완료 lot 이 적은 {lt} 의 납기 준수율 추정임 (완료 수 자체의 흔들림이 아님). "
                       f"{lt} 측정 창 완료 lot 은 seed별 {per}" + (" 로 모두 같음" if same else ""))
    return out


def name_checks(text: str, known_names: set | None) -> list:
    """등록되지 않은 툴그룹 이름 (T-v2 E4 r3: 예시로 'Wet_Etch_FE_41', 등록 이름은 WE_FE_41)."""
    if not known_names:
        return []
    out, seen = [], set()
    for m in TOOLGROUP_RE.finditer(text or ""):
        tok = m.group(1)
        if tok in known_names or tok in seen or tok.lower().startswith(("scenario", "product")):
            continue
        seen.add(tok)
        suf = re.search(r"_(?:FE|BE)_\d+$", tok)
        cands = sorted(n for n in known_names if suf and n.endswith(suf.group(0)) and n != tok)
        out.append(f"이름 확인: '{tok}' 는 등록된 툴그룹 이름이 아님"
                   + (f" (같은 번호의 등록 이름: {', '.join(cands)})" if cands and len(cands) <= 3 else ""))
    return out


REC_CLAIM_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*주(?:차)?([^\n\d|]{0,8}?)회복")
REC_NEG_RE = re.compile(r"^[^\n.]{0,8}?(?:안\s*됨|안\s*돼|안\s*되|못|않|없|아님|아니|여부|되지)")


REC_DUR_RE = re.compile(r"만에|뒤|후|지나")


def recovery_checks(text: str, turn_results: list) -> list:
    """WIP 회복 대조 (11-09 D-87, D-89 baseline_band_v1). 'N주(에) 회복'처럼 회복을 단정한 주가 도구의 회복 진입 주와 다르거나,
    도구가 회복을 확인하지 못했는데(관측 부족·미회복·대상 아님) 회복을 단정하면 도구 값을 밝히는 줄.
    'N주 만에·뒤 회복'은 복귀 후 경과 주와 대조한다. '까지'(관측 끝까지 …)나 뒤에 부정('확인 못함', '보지 않음')이 오면 보지 않는다."""
    out = []
    for name, res in turn_results:
        res = res or {}
        traj = res.get("주별 궤적") or {}
        band = traj.get(BAND_KEY)
        if name != "get_report" or res.get("status") != "ok" or not isinstance(band, dict):
            continue
        status = str(band.get(STATUS_KEY) or "")
        recovered = status.startswith("관측기간 내 WIP 회복 확인")
        entry, after = band.get(ENTRY_KEY), band.get(AFTER_KEY)
        claimed = []
        for m in REC_CLAIM_RE.finditer(text or ""):
            if "까지" in m.group(2) or REC_NEG_RE.search(text[m.end():]):
                continue
            w = float(m.group(1))
            target = after if REC_DUR_RE.search(m.group(2)) else entry
            if not recovered or not isinstance(target, (int, float)) or abs(w - float(target)) >= 0.5:
                claimed.append((w, m.group(0).strip()))
        if not claimed:
            continue
        w, raw = claimed[0]
        tool = (f"회복 진입 {float(entry):g}주 (복귀 후 {float(after):g}주)" if recovered and isinstance(entry, (int, float))
                else f"회복 확인 안 됨 — {status}")
        line = (f"회복 확인: 도구의 WIP 회복 판정은 {tool}. 정의: 장비 복귀 뒤 평균 WIP 차이가 기준 WIP 의 {band.get('허용 비율')}"
                f"(±{band.get('허용 폭 (lot)')} lot) 안에 {band.get('최소 유지 주')}주 이상, 관측 끝까지 재이탈 없음.")
        aux = traj.get(AUX_KEY)
        if isinstance(aux, (int, float)) and abs(w - float(aux)) < 0.5:
            line += f" 위 글의 '{raw}'의 {w:g}주는 '초과 WIP 최고값 대비 75% 해소 시점'(보조 지표)이지 회복 주가 아님"
        else:
            line += f" 위 글의 '{raw}'는 도구 판정과 다름"
        out.append(line)
    return out


# -- 11-10 양식 보충 (사용자 확인 1번: 모델이 '조건' 줄과 주의 한 줄을 빼고, 표 바로 밑 안내 줄이 표의 행으로 보임) ------------
HEADER_RE = re.compile(r"^\s*\*\*\s*([^*]+?)\s*\*\*\s*:?\s*$")
TONE_RE = re.compile(r"[가-힣]니다|세요|십시오|[가-힣]까요?\s*\?|어떨까|볼까|할까|드릴까")
QUOTE_RE = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|‘[^’\n]*’|“[^”\n]*”")


def fix_table_gap(text: str) -> tuple[str, int]:
    """표 바로 다음 줄이 표가 아니면 빈 줄을 넣는다 (마크다운이 그 줄을 표의 행으로 읽지 않게)."""
    lines, out, n = (text or "").split("\n"), [], 0
    for i, line in enumerate(lines):
        out.append(line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.strip().startswith("|") and nxt.strip() and not nxt.strip().startswith("|"):
            out.append("")
            n += 1
    return "\n".join(out), n


def fix_header_gap(text: str) -> tuple[str, int]:
    """'**한 줄 답**' 같은 머리말 바로 다음 줄이 글이면 빈 줄을 넣는다 (마크다운이 머리말과 글을 한 문단으로 붙이지 않게)."""
    lines, out, n = (text or "").split("\n"), [], 0
    for i, line in enumerate(lines):
        out.append(line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if HEADER_RE.match(line) and nxt.strip():
            out.append("")
            n += 1
    return "\n".join(out), n


def _last_report_block(turn_results: list) -> dict | None:
    for name, res in reversed(turn_results):
        res = res or {}
        if name == "get_report" and res.get("status") == "ok" and isinstance(res.get("답에 그대로 쓸 문장"), dict):
            return res["답에 그대로 쓸 문장"]
    return None


def _header_index(lines: list, label: str) -> int | None:
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line)
        if m and m.group(1).startswith(label):
            return i
    return None


def _section_end(lines: list, start: int) -> int:
    """머리말 start 의 내용 끝 (다음 머리말 앞, 뒤쪽 빈 줄 제외)."""
    end = next((j for j in range(start + 1, len(lines)) if HEADER_RE.match(lines[j])), len(lines))
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return end


def fix_report_sections(text: str, turn_results: list) -> tuple[str, int]:
    """결과 보고(② 결론이 있는 답)에 도구의 '조건' 줄·'사건 결론' 줄·'주의' 줄이 빠졌으면 그 자리에 그대로 채운다.
    사건 결론은 ② 의 첫 줄(질문한 지표) 바로 뒤 (11-10 사용자 확인 5번: 정지 질문 답에 WIP 곡선·회복 판정이 빠짐)."""
    blk = _last_report_block(turn_results)
    lines = (text or "").split("\n")
    if not blk or _header_index(lines, "② 결론") is None:
        return text, 0
    n = 0
    cond = blk.get("조건")
    h = _header_index(lines, "① 바꾼 것")
    if cond and cond not in text and h is not None:
        lines.insert(_section_end(lines, h), f"- 조건: {cond}")
        n += 1
    ev = [x for x in blk.get("사건 결론") or [] if x]
    miss_ev = [x for x in ev if x not in text]
    if miss_ev:
        h = _header_index(lines, "② 결론")
        body = range(h + 1, _section_end(lines, h))
        present = [i for i in body if any(x in lines[i] for x in ev)]
        content = [i for i in body if lines[i].strip()]
        at = present[-1] + 1 if present else (content[0] + 1 if content else h + 1)
        lines[at:at] = [f"- {x}" for x in miss_ev]
        n += len(miss_ev)
    missing = [x for x in blk.get("주의") or [] if x and x not in text]
    if missing:
        h = _header_index(lines, "④ 주의")
        if h is not None:
            at = _section_end(lines, h)
            lines[at:at] = [f"- {x}" for x in missing]
        else:                                                          # 주의 절이 없으면 ⑤·분석 메모 앞(없으면 끝)에 만든다
            at = next((i for i in (_header_index(lines, "⑤"), _header_index(lines, "분석 메모")) if i is not None), len(lines))
            lines[at:at] = ["**④ 주의**", *[f"- {x}" for x in missing], ""]
        n += len(missing)
    return "\n".join(lines), n


COUNT_ASK_RE = re.compile(r"몇\s*(?:개|lot|랏|건)|개수")
PCT_ASK_RE = re.compile(r"몇\s*(?:%|퍼|프로)|퍼센트|비율")
ASK_METRIC_RE = (("납기", re.compile(r"납기")), ("CT", re.compile(r"CT|사이클\s*타임")), ("WIP", re.compile(r"WIP|재공")),
                 ("처리량", re.compile(r"처리량|생산량|완료")))


def _asked_variant(user_texts: list | None) -> tuple[str | None, list]:
    """질문이 '몇 개'면 ('개수 먼저', [지표 이름…]), '몇 %'면 ('비율 먼저', …). 지표는 질문에 나온 것만."""
    q = "\n".join(t for t in (user_texts or []) if t)
    first = "개수 먼저" if COUNT_ASK_RE.search(q) else "비율 먼저" if PCT_ASK_RE.search(q) else None
    if not first:
        return None, []
    hot, reg = re.search(r"hot|핫|긴급", q, re.I), re.search(r"regular|일반", q, re.I)
    names = {"납기": "납기 준수 hot만" if hot else "납기 준수 regular", "CT": "regular CT", "WIP": "WIP (모든 lot 유형)",
             "처리량": "regular 처리량" if reg else "전체 처리량 (모든 lot 유형)"}
    return first, [names[f] for f, rx in ASK_METRIC_RE if rx.search(q)]


def fix_asked_variant(text: str, turn_results: list, user_texts: list | None) -> tuple[str, int]:
    """'몇 개'·'몇 %' 질문이면 그 지표의 기본 줄을 '개수 먼저'·'비율 먼저' 줄로 바꾼다 (11-10 사용자 확인 3번:
    '납기를 넘기는 lot 이 몇 개나 늘어?' 에 개수가 괄호 안에만 있는 기본 줄을 씀). 숫자는 같은 도구 값."""
    blk = _last_report_block(turn_results)
    first, names = _asked_variant(user_texts)
    if not blk or not first:
        return text, 0
    n = 0
    for name in names:
        base_line, variant = (blk.get("지표별 결론") or {}).get(name), (blk.get(first) or {}).get(name)
        if base_line and variant and base_line in text and variant not in text:
            text = text.replace(base_line, variant, 1)
            n += 1
    return text, n


def tone_checks(text: str) -> list:
    """명사형이 아닌 끝말 (따옴표로 인용한 사용자 말은 뺌). 고쳐 쓰기만 부탁하고 ※ 줄은 붙이지 않는다."""
    bad = sorted({m.group(0).strip() for m in TONE_RE.finditer(QUOTE_RE.sub("", text or ""))})
    return [f"말투 확인: 명사형(~함·~임·~됨)이 아닌 끝말 {', '.join(repr(b) for b in bad[:4])}. "
            "질문·제안도 '~하면 됨', '~볼 수 있음'처럼 명사형으로 고친다 (도구 줄은 그대로)"] if bad else []


def report_checks(text: str, turn_results: list, user_texts: list | None = None,
                  known_names: set | None = None) -> tuple[str, list, list]:
    """6번 묶음. 반환: (고친 글, 붙일 줄, 기록용 이름)."""
    added, notes = [], []
    text, k = fix_util_ci(text, turn_results)
    added += ["CI 표기 수정"] * k
    text, k = fix_table_columns(text)
    added += ["표 열 맞춤"] * k
    text, k = fix_report_sections(text, turn_results)
    added += ["양식 보충"] * k
    text, k = fix_asked_variant(text, turn_results, user_texts)
    added += ["개수·비율 줄"] * k
    text, k = fix_table_gap(text)
    added += ["표 뒤 빈 줄"] * k
    text, k = fix_header_gap(text)
    added += ["머리말 뒤 빈 줄"] * k
    if user_texts:
        text, done = fix_default_labels(text, user_texts)
        added += ["기본값 표시 삭제"] * len(done)
        src = source_checks(text, user_texts)
        notes += src
        added += ["값 출처 확인"] * len(src)
    rows = report_rows(turn_results)
    for got in (no_change_checks(text, rows), scope_checks(text, rows), number_checks(text, rows),
                warning_checks(text, turn_results), name_checks(text, known_names), recovery_checks(text, turn_results)):
        notes += got
        added += [n.split(":")[0] for n in got]                          # 줄 머리말(판정 확인·범위 확인·도구 값 확인 …)이 기록 이름
    return text, notes, added


EDIT_LABELS = ("CI 표기 수정", "표 열 맞춤", "기본값 표시 삭제", "양식 보충", "표 뒤 빈 줄",
               "머리말 뒤 빈 줄", "개수·비율 줄")                     # 글을 직접 고친 것 (줄을 붙이지 않음)
# 11-09 (D-91, 글 설명 개선 B): 이 이름의 지적이 있으면 ※ 줄을 붙이기 전에 모델이 한 번 고쳐 쓴다 (정의 덧붙임은 오류가 아님)
RETRY_LABELS = frozenset({"부호 확인", "카드·id 확인", "선택지 확인", "hot 값 확인", "판정 확인", "범위 확인", "도구 값 확인",
                          "값 출처 확인", "표본 주의 확인", "이름 확인", "회복 확인", "수치 출처 확인", "말투 확인"})
SILENT_LABELS = frozenset({"말투 확인"})                             # 고쳐 쓰기만 부탁하고, 남아도 ※ 줄로 붙이지 않음 (11-10)
REWRITE_PROMPT = ("[답 점검 코드 — 사용자 메시지가 아님] 위 답에서 도구 결과와 맞지 않는 곳을 찾았다. 도구는 다시 부르지 않는다. "
                  "도구 결과의 '답에 그대로 쓸 문장'(결론·표·주의 줄)을 고치지 않고 그대로 쓰고, 직접 쓰는 문장에는 도구 줄에 없는 숫자를 넣지 않고, "
                  "앞 답과 같은 머리말·구성(예: 한 줄 답 + 세부 표만 쓴 답이면 그대로)을 지키며 "
                  "아래 지적을 반영해 답 전체를 다시 쓴다. 지적:\n")
FIG_RE = re.compile(r"(?<![\w.])([+\-−]?\d+(?:\.\d+)?)\s*(%p|%|lot/주|lot|WSPW|일|시간)(?![A-Za-z/])")
SPAN_RE = re.compile(r"(?<![\w.])(\d+)\s*주\s*(?:간|동안)")
TOOL_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _remember_numbers(turn_results: list, state: dict, user_texts: list | None) -> set:
    """이 대화에서 도구 결과·사용자 말에 나온 수 (절댓값, 비율이면 ×100 도). state['seen_nums'] 에 쌓는다."""
    seen = state.setdefault("seen_nums", set())
    import json as _json
    blobs = [_json.dumps(r, ensure_ascii=False, default=str) for _, r in turn_results] + list(user_texts or [])
    for b in blobs:
        for m in TOOL_NUM_RE.finditer(b):
            v = abs(float(m.group(0)))
            seen.add(v)
            if v <= 1:
                seen.add(round(v * 100, 6))
    return seen


def _matches(v: float, decimals: int, seen: set) -> bool:
    return any(abs(round(k, decimals) - v) < 1e-9 or abs(k - v) < 1e-9 for k in seen)


def figure_checks(text: str, turn_results: list, state: dict, user_texts: list | None = None) -> list:
    """수치 출처 대조 (11-09, D-91). 단위가 붙은 수(%, %p, lot, lot/주, 일, N주간)가 이 대화의 도구 결과·사용자 말 어디에도 없으면
    도구 값을 밝히라는 줄 (T-v2 F3 r1: CI 13.85 를 1.385 로, 12주를 13주로). 쓴 자릿수로 반올림해 같으면 같은 수로 본다."""
    seen = _remember_numbers(turn_results, state, user_texts)
    if not seen:
        return []
    bad = []
    for m in list(FIG_RE.finditer(text or "")) + list(SPAN_RE.finditer(text or "")):
        tok = m.group(1).replace("−", "-").lstrip("+")
        v = abs(float(tok))
        dec = len(tok.split(".")[1]) if "." in tok else 0
        if not _matches(v, dec, seen):
            raw = m.group(0).strip()
            if raw not in bad:
                bad.append(raw)
    if not bad:
        return []
    return [f"수치 출처 확인: 도구 결과에서 찾지 못한 수치 {', '.join(repr(b) for b in bad[:6])}. 도구가 계산한 값만 그대로 쓴다 "
            "(직접 더하기·빼기·기간 계산 금지)"]


def review_answer(text: str, turn_results: list, state: dict, card_pending: bool | None = None,
                  known_ids: set | None = None, ambiguous: list | None = None,
                  user_texts: list | None = None, known_names: set | None = None) -> tuple[str, list, list]:
    """답 점검 (11-09 D-91). 반환: (글만 고친 답, [(이름, 붙일 줄)], 글을 고친 이름 목록). state 를 바꾼다
    (가동률 정의를 보였는지, hot 값 짝, 이 대화의 도구 수치). 고쳐 쓰기 전 점검은 state 사본으로 부른다."""
    text = text or ""
    text, rep_notes, rep_added = report_checks(text, turn_results, user_texts, known_names)
    edits = [a for a in rep_added if a in EDIT_LABELS]
    rep_labels = [a for a in rep_added if a not in EDIT_LABELS]
    pairs = []
    defined = state.setdefault("defined", set())
    if "용어" not in defined and any(n in TERMS_TOOLS and (r or {}).get("status") == "ok" for n, r in turn_results):
        defined.add("용어")                                           # 11-10: 결과·KPI 를 처음 보인 답에 한 번
        pairs.append(("용어 정의", TERMS_DEF))
    measured = any(n in MEASURED_TOOLS and (r or {}).get("status") == "ok" for n, r in turn_results)
    if measured and PCT_RE.search(text):
        for term, used_re, def_re, definition in (("가동률", UTIL_RE, UTIL_DEF_RE, UTIL_DEF),
                                                  ("가용률", AVAIL_RE, AVAIL_DEF_RE, AVAIL_DEF)):
            if term in defined or not used_re.search(text):
                continue
            defined.add(term)
            if not def_re.search(text):
                pairs.append((f"{term} 정의", definition))
    pairs += [("부호 확인", n) for n in sign_checks(text, turn_results)]
    pairs += [("카드·id 확인", n) for n in card_checks(text, turn_results, card_pending, known_ids)]
    pairs += [("선택지 확인", n) for n in alias_checks(text, turn_results, ambiguous)]
    pairs += [("hot 값 확인", n) for n in hot_checks(text, turn_results, state)]
    pairs += list(zip(rep_labels, rep_notes))
    pairs += [("수치 출처 확인", n) for n in figure_checks(text, turn_results, state, user_texts)]
    pairs += [("말투 확인", n) for n in tone_checks(text)]
    return text, pairs, edits


def retry_issues(pairs: list) -> list:
    """고쳐 쓰기를 부탁할 지적 (정의 덧붙임 제외)."""
    return [n for label, n in pairs if label in RETRY_LABELS]


def finish_answer(text: str, turn_results: list, state: dict, card_pending: bool | None = None,
                  known_ids: set | None = None, ambiguous: list | None = None,
                  user_texts: list | None = None, known_names: set | None = None) -> tuple[str, list]:
    """text: 모델 답, turn_results: 이번 차례 [(도구 이름, 결과 dict)], state: 대화별 상태,
    card_pending: 지금 떠 있는 카드가 있는지, known_ids: 이 대화에서 만든 시나리오 id,
    ambiguous: 아직 정하지 않은 여러 area 명칭 [(명칭, [area…])],
    user_texts: 이 요청의 사용자 말 (값 출처 대조), known_names: 등록된 툴그룹·area·제품 이름 (이름 대조).
    반환: (다듬은 답, 덧붙인 것)."""
    text, pairs, edits = review_answer(text, turn_results, state, card_pending, known_ids, ambiguous, user_texts, known_names)
    silent = [p[0] + " (남음)" for p in pairs if p[0] in SILENT_LABELS]  # 기록에만 남김
    pairs = [p for p in pairs if p[0] not in SILENT_LABELS]
    edits = edits + silent
    lead_labels = {"부호 확인", "카드·id 확인", "선택지 확인", "hot 값 확인"}  # 옛 순서: 정의·부호·카드·선택지·hot → 글 고침 → 6번 묶음
    lead = [p for p in pairs if p[0] in lead_labels or p[0].endswith(" 정의")]
    rest = [p for p in pairs if not (p[0] in lead_labels or p[0].endswith(" 정의"))]
    added = [p[0] for p in lead] + edits + [p[0] for p in rest]
    notes = [p[1] for p in lead + rest]
    if not notes:
        return text, added                                            # 글만 고친 경우(CI 표기·표 열·기본값 표시)도 기록
    return text.rstrip() + "\n\n" + "\n".join(f"※ {n}" for n in notes), added
