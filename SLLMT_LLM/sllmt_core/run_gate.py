"""실행 확인 코드 3판 (11-06 오후, 버튼 승인 — 사용자 결정, 팀원 전달사항 권고안).

3판의 실행 허용 계약: 실행 도구로 넘어가려면 그 설정(sid = canonical spec 해시)이 사용자의 명시 조작으로 승인돼 있어야 한다.
  - 승인은 RunGate.grant(card_id) 로만 생긴다: 대시보드 '확인 후 실행' 버튼, CLI '/승인', 평가기의 버튼 대역. 모델·도구·자연어로는 안 생김
  - 모델은 build_scenario → request_approval(대상, 기준) 로 카드만 띄운다. 카드에는 도구가 계산한 실제 설정과 사용자가 말하지
    않은 값(항목·단위·방향·출처 대조, 아래 2판 규칙)이 표시되고, 그런 값이 있으면 '확인했다'는 별도 표시 없이는 승인 안 됨
  - 비교는 (대상, 기준) 쌍으로 승인 → get_report 의 비교 방향도 승인과 같아야 함
  - 사용자가 버튼 대신 글로 답하면(질문 제외) 떠 있던 카드는 무효, 승인만 받고 시작 전인 설정의 승인도 무효. 취소한 요청은 실행 안 됨
  - 이미 시작한 실행의 상태 확인·완료 알림·결과 다시 보기는 그대로
  - 대상 넓힘 검사(build 전)는 그대로
  11-06 밤 보완 (사람 판정 확정 결과): build 전에 값 검사 — 사용자가 말하지 않은 값(2판 값 대조, 장비 증감 시작 주 포함)이나
  같은 제품군 안의 배율 불일치가 있으면 만들지 않음(status value_problem). 같은 차례에 started·running·busy 를 받은 실행을
  다시 조회하면 막음(status wait_for_notice, Agent 는 두 번 막히면 차례를 끝냄)
아래는 2판 설명 (값 대조 규칙은 3판에서 카드의 '사용자가 말하지 않은 값' 표시에 쓴다).

실행 확인 코드 2판 (11-06). 모델이 run_experiment 를 요청해도 코드가 조건을 다시 확인한다.

1판(11-05 밤): 프롬프트만으로는 "첫 차례 실행 금지"와 "보충 답 뒤 바로 실행"을 모델이 섞어서 실행 판단을 코드로 옮김.
2판(11-06 추가 검토 반례 6건 재현 → 보완): 취소 우선, 질문에는 실행 권한 없음, 값은 항목·단위·방향과 함께 사용자 말에서만
확인(확인 메시지 속 모델 값은 출처 아님), 철회한 비교 안 제외, 요청별 활성 시나리오 관리.
이 판도 표본에서 관찰한 결과로만 말한다: 자연어 판정이라 모든 경우의 차단을 보장하지 않는다.

실행 허용 계약 (모두 만족해야 도구로 넘어감):
  1. 요청 상태
     - 요청 번호: 새 요청마다 증가. 취소한 요청은 그 뒤 어떤 실행도 안 됨(승인도 거둠)
     - 답 차례: 바로 앞 차례에 모델이 확인 메시지(시나리오를 만들어 보임)나 빠진 값 질문을 보냈고, 지금 사용자 메시지가 그 답
     - 메시지 종류: 취소 > 알림 > 질문(실행 권한 없음) > 승인 > 보충·수정
  2. 승인 범위 (활성 시나리오)
     - 승인("맞음·네·진행"): 확인 메시지로 보인 활성 시나리오만. 빠진 값 질문에 값 없는 "네"는 승인 아님
     - 보충·수정: 이번 차례에 다시 만든 시나리오 + (철회 표현이 없으면) 바뀌지 않은 비교 안. 수정 전 시나리오는 빠짐
  3. 필수 값: 항목마다 값·단위·방향이 사용자 말에 있어야 함
     - 대수 = "n대", 기간 = "n주"·"n일", 투입·가공·PM·수리의 % = 같은 방향(늘·줄)의 "n%", 배율 = "n배"·"기준의 n%"
     - 사용자 원문에 숫자가 있다는 것 ≠ 그 항목 값을 정한 것. 승인했다고 모델이 만든 미정 값이 사용자 값이 되지 않음
  4. 정적 점검 통과 (rejected 아님)
이미 승인된 시나리오는 완료 알림·결과 다시 보기 차례에서 다시 부를 수 있다 (취소 차례 제외).

대상 넓힘 검사 (확인 단계와 별도): build_scenario 의 제품·제품군·툴그룹 대상이 사용자가 말한 대상보다 넓으면 만들지 않는다.
기록: events 에 차례(요청 번호·메시지 종류), 모델 요청, 차단(이유), 통과, 결과, 필수 값의 출처를 남긴다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_GROUPS = {"A_long": ["Product_1", "Product_2", "Product_3"],
                  "B_mid": ["Product_7", "Product_8", "Product_9", "Product_10"],
                  "C_short": ["Product_4", "Product_5", "Product_6"]}
AREAS = ("Def_Met", "Dielectric", "Diffusion", "Dry_Etch", "Implant", "Litho", "Litho_Met", "Planar", "TF", "TF_Met", "Wet_Etch")
AREA_ALIASES = {                       # 일반 명칭 → area (prompts.py 해석 규칙과 같은 대응 + 흔한 한국어 이름)
    "Litho": ("리소그래피", "리소", "노광", "포토"), "Dry_Etch": ("식각", "건식 식각", "드라이 에치", "건식"),
    "Wet_Etch": ("식각", "습식 식각", "웻 에치", "습식"), "Diffusion": ("확산",), "Implant": ("이온 주입", "이온주입", "임플란트"),
    "Planar": ("평탄화", "CMP"), "TF": ("박막",), "Dielectric": ("절연",),
    "Def_Met": ("계측",), "Litho_Met": ("계측",), "TF_Met": ("계측",)}
ALIAS_AREAS = {}                       # 일반 명칭 → 해당 area 들 (식각 → Dry_Etch·Wet_Etch)
for _a, _ws in AREA_ALIASES.items():
    for _w in _ws:
        ALIAS_AREAS.setdefault(_w, set()).add(_a)
STATE_WORDS = {"여유": ("여유", "한가"), "혼잡": ("혼잡", "붐빌", "붐비", "붐빔"), "보통": ("보통",)}
STATE_WEEK = {"보통": 34, "혼잡": 40, "여유": 32}     # sllmt_tools.STATES 와 같음 (분기 주)
BRANCH_HORIZON = 22                                   # sllmt_tools.BRANCH_HORIZON 과 같음
DEFAULT_RUN_SEEDS = [1, 2, 3, 4, 5]                   # sllmt_tools.DEFAULT_SEEDS 와 같음 (카드에 seed 가 없으면 이것)
DEFAULT_END_WEEK = 52
DEFAULT_WARMUP_WEEK = 26
DEFAULT_EVENT_START = 30.0              # 정지·고장 특성 시작 주 기본값 (sllmt_tools 와 같음, 11-08)
START0_RE = re.compile(r"처음부터|시작부터|0\s*주|첫\s*주|바로")

# -- 메시지 종류 -----------------------------------------------------------------------------------------------
NOTICE_RE = re.compile(r"^\s*\[알림\]")
APPROVAL_NOTICE_RE = re.compile(r"^\s*\[승인\]")
CANCEL_RE = re.compile(r"취소|실행하지\s*마|돌리지\s*마|진행하지\s*마|그만(?:두|해|하자|할게)|없던\s*걸로|안\s*할래|"
                       r"안\s*해도\s*돼|(?:실행|진행|돌려)\S{0,8}\s*(?:달라는|하라는|해\s*달라는)?\s*(?:뜻|말|얘기)\S{0,2}\s*아니")
# 시나리오 변경을 말하는 요청인지 (빠진 값 질문 대기를 만들 때만 씀: 정보 질문 뒤의 "다른 게 궁금하면 알려 주세요"로 대기가 생기지 않게)
LEVER_RE = re.compile(r"투입|정지|멈|세워|고장|수리|hot|핫|증설|감축|반출|장비|가공|PM|주기|간격|시작\s*상태|혼잡|여유|한가|붐|비교"
                      r"|디스패칭|디스패치|FIFO|선입선출|critical\s*ratio")
# 11-09 (D-88): 디스패칭 순서 변경 (베타) 값 출처. 3순위 도착 순서 / CR 을 setup 보다 먼저
DISPATCH_FIFO_RE = re.compile(r"FIFO|선입선출|도착\s*순|먼저\s*(?:온|들어온|도착한)|들어온\s*순|온\s*순서", re.I)
DISPATCH_CR_RE = re.compile(r"critical\s*ratio|(?<![A-Za-z])CR(?![A-Za-z])|크리티컬|납기\s*여유|긴급도|임계\s*비", re.I)
DISPATCH_SETUP_RE = re.compile(r"setup|셋업|세트업", re.I)
PARTIAL_RE = re.compile(r"(?:취소하고|빼고|철회하고|제외하고|말고)\s*.{0,40}?만\s*(?:실행|진행|돌려|적용)")   # 일부 철회 + 나머지 실행
QUESTION_END_RE = re.compile(r"(?:[?？]|거야|건가|건지|나요|까요|인가요|는지|어때|뭐야)\s*[?？]?\s*$")
RUN_WORD_RE = re.compile(r"(?:실행|진행|돌려)\s*(?:해|하자|합시다|해\s*줘|해\s*주세요|해주세요|부탁)|맞(?:습니다|음|아요)|그대로")
APPROVE_RE = re.compile(r"^\s*(?:네|넵|예|응|어|ㅇㅇ|좋아|좋습니다|좋아요|오케이|ok|okay|그래|맞(?:음|아|습니다|아요|네요))\b|"
                        r"맞(?:음|아|습니다|아요)|그대로\s*(?:진행|실행|해)|진행(?:해|하자|합시다|해\s*주세요|해주세요)|"
                        r"실행(?:해|하자|합시다|해\s*주세요|해주세요)|좋습니다|조건이\s*맞", re.IGNORECASE)
MODIFY_RE = re.compile(r"고치|고쳐|바꿔|바꾸|대신|말고|아니라|아니고|빼고|빼\s*줘|추가|수정|철회|줄여|늘려|낮춰|높여|"
                       r"만\s*(?:실행|진행|바꿔|적용)")
WITHDRAW_RE = re.compile(r"빼고|빼\s*줘|제외|없이|말고|취소하고|철회|만\s*(?:실행|진행|돌려|적용)|하나만|안만")
NEW_REQUEST_RE = re.compile(r"별도\s*(?:질의|요청|질문)|새\s*(?:질의|요청|질문)|다른\s*(?:질의|요청|질문)|그건\s*그렇고")
ALL_PRODUCTS_RE = re.compile(r"각\s*제품|모든\s*제품|전\s*제품|제품마다|제품별로|전체\s*(?:regular\s*)?투입|전체\s*제품")
ALL_SHARE_RE = re.compile(r"전부|모두|전량|전체|다\s*(?:hot|핫)|100\s*%")
# lot 유형별 투입 배율 (11-07): 0 = 투입 중단을 뜻하는 말, 대상 lot 유형 이름, 배율 말
LOT_STOP_RE = re.compile(r"(?<![\d.])0\s*(?:으로|로|개|%|배)|없애|없앤|없이|제외|중단|빼고|빼면|빼서|빼 |안\s*넣|투입하지\s*않|멈추|끊")
SUPERHOT_RE = re.compile(r"super\s*-?\s*hot|슈퍼\s*-?\s*핫", re.I)
HOT_RE = re.compile(r"hot|핫", re.I)
LOT_MULT_WORDS = {0.5: re.compile(r"절반|반으로|반만"), 2.0: re.compile(r"두\s*배|2\s*배")}
ASK_RE = re.compile(r"[?？]|까요|나요|가요|는지요|주세요|주시겠|주실\s*수|알려\s*주|말씀해|정해\s*주|골라\s*주|선택해\s*주|맞으면|"
                    r"면\s*됨|바람(?![가-힣])")                       # 11-10: 명사형 되묻기 ('답하면 됨', '골라 주기 바람')

# -- 값·단위·방향 -------------------------------------------------------------------------------------------------
NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+")           # Product_4, Litho_FE_111 등 (이름 속 숫자 제외용)
TOOLGROUP_RE = re.compile(r"(?<![A-Za-z0-9_])((?!Product_)[A-Za-z]+(?:_[A-Za-z]+)*_\d+)(?![A-Za-z0-9_])")
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)")
KOR_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
KOR_RE = re.compile(r"(다섯|여섯|일곱|여덟|아홉|한|두|세|네|열)\s*(?:대|배|개|주|일|lot|랏)")
FACT_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?|다섯|여섯|일곱|여덟|아홉|한|두|세|네|열)\s*(%|퍼센트|프로|배|대|개|lot|랏|주|일|년)")
WEEKS_PER_YEAR = 52                     # 11-07 사용자: 기간은 주 단위, "1년" 이라 하면 52주로 환산
HALF_YEAR_RE = re.compile(r"반\s*년")
BASE_PCT_RE = re.compile(r"기준(?:의|값의)?\s*(\d+(?:\.\d+)?)\s*%\s*(?:로|수준|만큼)")              # "기준의 75%로" = 배율 0.75
UNIT_OF = {"%": "pct", "퍼센트": "pct", "프로": "pct", "배": "x", "대": "count", "개": "lot", "lot": "lot", "랏": "lot",
           "주": "week", "일": "day", "년": "year"}
UP_RE = re.compile(r"늘|증가|높|올리|올려|올린|추가|증설|도입|많아|길어|키우|키워|확대|상향")
DOWN_RE = re.compile(r"줄|감소|낮|내리|내려|내린|단축|빼|감축|반출|적게|짧아|축소|하향|떨어")
STOP_RE = re.compile(r"[.,!?？\n;]")
SLOT_KEYS = {"release": re.compile(r"투입"), "pt": re.compile(r"가공"), "pm": re.compile(r"PM|주기|간격"),
             "mttr": re.compile(r"수리"), "tool": re.compile(r"장비|증설|감축|반출")}
COMPONENT_KEYS = {"total_release_pct": "투입", "group_mult": "투입", "product_mult": "투입", "outages": "정지",
                  "breakdown_changes": "고장 특성", "hot_conversion": "hot 전환", "tool_changes": "장비 수",
                  "processing_time": "가공 시간", "pm_changes": "PM", "start_state": "시작 상태",
                  "dispatch_changes": "디스패칭"}


@dataclass
class Fact:
    value: float
    unit: str            # pct | x | count | lot | week | day
    dir: int             # +1 늘림, -1 줄임, 0 모름
    msg: int             # 몇 번째 사용자 메시지(이 요청 안)
    text: str


def _has_name(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None


def _num(tok: str) -> float:
    return float(KOR_NUM[tok]) if tok in KOR_NUM else float(tok)


def _direction(s: str) -> int:
    up, down = bool(UP_RE.search(s)), bool(DOWN_RE.search(s))
    return 1 if up and not down else -1 if down and not up else 0


def extract_facts(texts: list) -> list:
    """사용자 메시지들에서 (값, 단위, 방향) 사실을 뽑는다. 방향은 숫자 뒤(다음 숫자·문장부호 전까지), 없으면 앞을 본다."""
    out = []
    for i, raw in enumerate(texts):
        t = NAME_TOKEN_RE.sub(lambda m: " " * len(m.group(0)), raw or "")     # 이름 속 숫자 제외 (위치 유지)
        ms = list(FACT_RE.finditer(t))
        for j, m in enumerate(ms):
            nxt = ms[j + 1].start() if j + 1 < len(ms) else len(t)
            after = t[m.end():min(nxt, m.end() + 16)]
            stop = STOP_RE.search(after)
            after = after[:stop.start()] if stop else after
            before = t[max(0, m.start() - 10):m.start()]
            bstop = list(STOP_RE.finditer(before))
            before = before[bstop[-1].end():] if bstop else before
            d = _direction(after) or _direction(before)
            unit, val = UNIT_OF[m.group(2)], _num(m.group(1))
            if unit == "year":                                          # 11-07: 1년 = 52주 (기간은 주로 다룬다)
                unit, val = "week", val * WEEKS_PER_YEAR
            out.append(Fact(val, unit, d, i, m.group(0)))
        for m in HALF_YEAR_RE.finditer(t):
            out.append(Fact(WEEKS_PER_YEAR / 2, "week", 0, i, m.group(0)))
        for m in BASE_PCT_RE.finditer(t):
            out.append(Fact(float(m.group(1)) / 100.0, "x", 0, i, m.group(0)))
        if re.search(r"절반|반으로|반만", t):
            out.append(Fact(0.5, "x", 0, i, "절반"))
            out.append(Fact(50.0, "pct", -1, i, "절반"))
    return out


def slot_direction(texts: list, slot: str) -> int:
    """요청 전체에서 그 항목 낱말(투입·가공·PM·수리·장비) 바로 뒤의 늘·줄 표현. 한쪽만 있으면 그 방향."""
    key = SLOT_KEYS.get(slot)
    if key is None:
        return 0
    dirs = set()
    for raw in texts:
        t = raw or ""
        for m in key.finditer(t):
            w = t[m.end():m.end() + 14]
            stop = STOP_RE.search(w)
            d = _direction(w[:stop.start()] if stop else w)
            if d:
                dirs.add(d)
    return dirs.pop() if len(dirs) == 1 else 0


def numbers_in(text: str) -> set:
    """사용자·확인 메시지의 숫자 (이름 속 숫자 제외, 한국어 수사 + 단위 포함). 메시지 종류 판정에만 씀."""
    t = NAME_TOKEN_RE.sub(" ", text or "")
    nums = set()
    for m in NUM_RE.findall(t.replace(",", "")):
        try:
            nums.add(float(m))
        except ValueError:
            pass
    nums |= {float(KOR_NUM[m.group(1)]) for m in KOR_RE.finditer(t)}
    if re.search(r"절반|반으로|반만", t):
        nums |= {0.5, 50.0}
    return nums


def classify(text: str, prev_assistant: str = "") -> str:
    """사용자 메시지 종류: notice | cancel | inquiry | approve | supplement. 답 차례 여부는 RunGate 가 정한다."""
    t = text or ""
    if APPROVAL_NOTICE_RE.search(t):
        return "approval_notice"                                       # 버튼 승인 뒤 화면이 보내는 메시지 (승인 자체는 grant 로 이미 생김)
    if NOTICE_RE.search(t):
        return "notice"
    if CANCEL_RE.search(t) and not PARTIAL_RE.search(t):
        return "cancel"                                                # 취소가 우선 ("실행해 달라는 뜻이 아니야" 포함)
    if QUESTION_END_RE.search(t.strip()) and not RUN_WORD_RE.search(t):
        return "inquiry"                                               # 질문에는 실행 권한 없음
    if APPROVE_RE.search(t) and not MODIFY_RE.search(t):
        new_nums = numbers_in(t) - numbers_in(prev_assistant)
        names = [m.group(0) for m in NAME_TOKEN_RE.finditer(t) if not _has_name(prev_assistant, m.group(0))]
        if not new_nums and not names:
            return "approve"
    return "supplement"


@dataclass
class Build:
    sid: str
    status: str
    args: dict
    turn: int
    request: int
    version: int


@dataclass
class RunGate:
    """대화 하나(Agent 하나)의 실행 확인 상태 3판 (11-06, 버튼 승인).

    실행 승인은 사용자의 명시 조작(대시보드 '확인 후 실행' 버튼, CLI '/승인')으로 grant() 를 부를 때만 생긴다.
    모델·도구·자연어("맞음")로는 승인이 생기지 않는다. 모델은 request_approval 로 카드를 띄울 수만 있다."""
    groups: dict = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_GROUPS.items()})
    toolgroup_area_provider: object = None         # () -> {툴그룹: area}. 처음 필요할 때 한 번 부른다
    toolgroup_areas: dict | None = None
    turn: int = 0
    request: int = 0
    request_texts: list = field(default_factory=list)   # 이 요청의 사용자 말 (값의 출처)
    all_texts: list = field(default_factory=list)       # 대화 전체의 사용자 말 (대상 이름만 여기서도 찾음: "그 툴그룹")
    builds: dict = field(default_factory=dict)     # sid -> Build (마지막으로 만든 것). sid = 설정 지문(canonical spec 해시)
    cards: dict = field(default_factory=dict)      # card_id -> {target, reference, request, turn, flags, status, card}
    approved: dict = field(default_factory=dict)   # sid -> request (버튼으로 승인된 설정)
    approved_seeds: dict = field(default_factory=dict)  # sid -> 승인한 카드의 seed 목록 (실행은 이 seed 로만, 11-07)
    pairs: set = field(default_factory=set)        # 승인된 비교 (대상 sid, 기준 sid)
    started: set = field(default_factory=set)      # 실행 도구가 ready·started·running 을 돌려준 sid (상태 확인·다시 보기 대상)
    cancelled: set = field(default_factory=set)    # 취소한 요청 번호
    events: list = field(default_factory=list)
    msg_kind: str = "new"
    awaiting: bool = False                         # 모델이 앞 차례에서 이 요청의 값을 물었다 (카드 없이) → 다음 사용자 말은 같은 요청의 답
    _turn_ran: bool = False
    _turn_runs: dict = field(default_factory=dict)  # 이번 차례 sid -> 실행 도구 응답 상태 (같은 차례 재조회 막기)
    _versions: dict = field(default_factory=dict)  # request -> 만든 시나리오 수 (설정 판)

    # -- 차례 ---------------------------------------------------------------------------------------
    def begin_turn(self, user_text: str) -> dict:
        """사용자 메시지 한 차례 시작. 알림·승인 알림은 요청을 바꾸지 않는다. 사용자가 직접 쓴 메시지는:
        - 카드가 떠 있거나 모델이 앞 차례에 이 요청의 값을 물었으면 같은 요청의 보충·수정·질문
          (카드는 질문이 아니면 무효 → 모델이 다시 만든 뒤 새 카드)
        - 그 밖에는 새 요청 (요청 번호 증가). "별도 질의·새 요청"이라고 하면 늘 새 요청
        취소·수정·철회·새 요청이면 승인만 받고 아직 시작하지 않은 시나리오의 승인도 무효 (이미 시작한 실행의 확인·다시 보기는 유지)."""
        self.turn += 1
        self._turn_ran = False
        self._turn_runs = {}
        kind = classify(user_text)
        if kind in ("notice", "approval_notice"):
            self.msg_kind = kind
            self.events.append({"event": "turn", "turn": self.turn, "request": self.request, "kind": kind})
            return {"turn": self.turn, "request": self.request, "kind": kind}
        self.all_texts.append(user_text)
        card = self.pending_card()
        if kind == "cancel":
            self.cancelled.add(self.request)
            for sid, req in list(self.approved.items()):               # 취소한 요청의 승인을 거둠
                if req == self.request and sid not in self.started:
                    del self.approved[sid]
        elif (card or self.awaiting) and not NEW_REQUEST_RE.search(user_text or ""):
            self.request_texts.append(user_text)                       # 카드·모델 질문에 대한 보충·수정·질문
        else:
            self.request += 1
            self.request_texts = [user_text]
        self.awaiting = False
        if kind != "inquiry":
            gone = [s for s in self.approved if s not in self.started]
            for s in gone:
                del self.approved[s]
            self.pairs = {p for p in self.pairs if p[0] in self.approved or p[0] in self.started}
            for c in self.cards.values():
                if c["status"] == "pending":
                    c["status"] = "stale"                              # 사용자가 버튼 대신 글로 답함 → 그 카드는 무효
            if gone:
                self.events.append({"event": "approval_revoked", "turn": self.turn, "scenario_ids": gone,
                                    "reason": "사용자 메시지(취소·수정·철회·새 요청)"})
        self.msg_kind = kind
        info = {"turn": self.turn, "request": self.request, "kind": kind}
        self.events.append({"event": "turn", **info})
        return info

    def end_turn(self, final_text: str) -> None:
        """모델 답이 이 요청(시나리오 변경 요청)의 값을 묻고 끝났으면 다음 사용자 말을 같은 요청의 답으로 본다.
        실행을 시작했거나 취소한 차례, 정보 질문(투입·장비 등 변경 낱말이 없는 요청)은 제외."""
        if self.msg_kind not in ("notice", "approval_notice"):
            self.awaiting = (self.msg_kind != "cancel" and not self._turn_ran and self.pending_card() is None
                             and bool(ASK_RE.search(final_text or ""))
                             and any(LEVER_RE.search(t or "") for t in self.request_texts))
        self.events.append({"event": "turn_end", "turn": self.turn, "pending_card": (self.pending_card() or {}).get("id"),
                            "awaiting_answer": self.awaiting})

    # -- 사용자 승인 (UI·CLI·평가 대역만 부른다. 도구로는 노출하지 않는다) -----------------------------------
    def pending_card(self) -> dict | None:
        live = [c for c in self.cards.values() if c["status"] == "pending"]
        return live[-1] if live else None

    def grant(self, card_id: str, accept_flags: bool = False) -> tuple[bool, str]:
        """사용자가 '확인 후 실행'을 누름. (성공 여부, 이유 또는 모델에 보낼 승인 메시지)."""
        c = self.cards.get(card_id)
        if c is None:
            return False, "없는 카드"
        if c["status"] != "pending":
            return False, "이미 처리됐거나 무효가 된 카드 (설정이 바뀌었으면 새 카드에서 승인)"
        if c["request"] in self.cancelled:
            return False, "취소한 요청의 카드"
        if c["flags"] and not accept_flags:
            return False, "사용자가 말하지 않은 값이 있음: " + ", ".join(c["flags"]) + " — 값을 확인했다는 표시가 필요"
        for sid in (c["target"], c["reference"]):
            if sid:
                if self.builds.get(sid) is None or self.builds[sid].status != "ok":
                    return False, f"{sid} 는 정적 점검을 통과한 시나리오가 아님"
        for sid in (c["target"], c["reference"]):
            if sid:
                self.approved[sid] = c["request"]
                self.approved_seeds[sid] = list(c.get("seeds") or DEFAULT_RUN_SEEDS)
        if c["reference"]:
            self.pairs.add((c["target"], c["reference"]))
        c["status"] = "approved"
        self.events.append({"event": "approved_by_user", "turn": self.turn, "card": card_id, "target": c["target"],
                            "reference": c["reference"], "accepted_flags": list(c["flags"]) if accept_flags else []})
        return True, self.approval_message(c)

    @staticmethod
    def approval_message(c: dict) -> str:
        seeds = list(c.get("seeds") or DEFAULT_RUN_SEEDS)
        sarg = "" if seeds == DEFAULT_RUN_SEEDS else f", seeds={seeds}"
        stxt = f" 승인한 seed: {seeds} (run_experiment 에 이 seed 그대로)." if sarg else ""
        if c["reference"]:
            return (f"[승인] 사용자가 '확인 후 실행' 버튼으로 비교 실행을 승인했다: 대상 {c['target']}, 기준 {c['reference']}.{stxt} "
                    f"두 시나리오를 run_experiment(scenario_id=…{sarg}) 로 실행하고(DES 는 한 번에 하나), 결과가 모두 준비되면 "
                    f"get_report(scenario_id={c['target']}, compare_to={c['reference']}) 로 직접 비교해 보고한다.")
        return (f"[승인] 사용자가 '확인 후 실행' 버튼으로 시나리오 {c['target']} 실행을 승인했다.{stxt} "
                f"run_experiment(scenario_id={c['target']}{sarg}) 로 실행하고, 결과가 준비되면 get_report 로 받아 처음 질의의 목적에 맞게 보고한다.")

    # -- 도구 전후 ------------------------------------------------------------------------------------
    def before(self, name: str, args: dict) -> dict | None:
        """도구를 부르기 전. 막으면 모델에 돌려줄 결과 dict, 통과면 None."""
        args = args or {}
        if name == "build_scenario":
            why = self.target_problem(args)
            if why:
                hint = ""
                if args.get("hot_conversion") and "hot 전환" in why:   # 11-07: 차단 이유를 "제품 하나는 안 됨"으로 오해한 사례
                    hint = (" hot 전환 대상은 hot_conversion 항목 안의 products 에 넣는다 (제품 하나도 가능, 예: "
                            "hot_conversion=[{share, products: ['Product_5']}]). 제품군으로 넓히자고 제안하지 않는다.")
                return self._not_built(name, args, "target_mismatch", why,
                                       "사용자가 말한 대상만 넣어 다시 만들거나, 대상을 넓혀야 하면 사용자에게 묻는다." + hint)
            if self.msg_kind not in ("notice", "approval_notice"):     # 값 검사 (11-06 밤): 말하지 않은 값·제품군 배율 불일치
                probs = []
                miss = self.missing_values(args, self.request_texts)
                if miss:
                    probs.append("사용자가 말하지 않은 값: " + ", ".join(miss))
                probs += self.mapping_problem(args)
                if probs:
                    amb = self.ambiguous_aliases()
                    ask = "".join(f" '{w}'은 {'·'.join(a)} 여러 area 다. 한쪽을 정하지 말고 {'·'.join(a)} 를 모두 선택지로 보이며 "
                                  "어느 것인지 묻는다 ('~로 이해하면 될까요?'처럼 한쪽을 전제로 묻지 않는다)." for w, a in amb)
                    return self._not_built(name, args, "value_problem", "; ".join(probs),
                                           "값을 지어내지 않는다. 이미 받은 값은 다시 묻지 않고 빠진 값(대수·비율·크기·대상·시작 주)만 묻는다. "
                                           "정적 점검을 통과시키려고 사용자가 말하지 않은 제품·값을 바꾸지 않는다. 제품군에 속한 제품은 "
                                           "fab 정보로 확인하고, 사용자가 말한 값만으로 다시 만든다." + ask)
            return None
        if name == "request_approval":
            for sid in (args.get("scenario_id"), args.get("compare_to")):
                if not sid:
                    continue
                b = self.builds.get(sid)
                if b is None:
                    return self._block(name, args, "blocked", f"{sid} 는 이 대화에서 build_scenario 로 만든 시나리오가 아니다",
                                       "scenario_id 를 지어내지 않는다. build_scenario 가 돌려준 scenario_id 만 쓴다. 만든 시나리오가 "
                                       "없으면 카드도 없으니 사용자에게 버튼을 누르라고 하지 않는다")
                if b.status != "ok":
                    return self._block(name, args, "blocked", f"{sid} 는 정적 점검을 통과하지 못했다(rejected)",
                                       "실행할 수 없다고 이유와 대안을 설명한다")
            if self.msg_kind == "cancel":
                return self._block(name, args, "blocked", "사용자가 이번 메시지에서 취소했다", "취소를 인정한다")
            return None
        if name == "run_experiment":
            sid = args.get("scenario_id")
            self.events.append({"event": "model_request", "turn": self.turn, "tool": name, "scenario_id": sid})
            if self._turn_runs.get(sid) in ("started", "running", "busy"):   # 같은 차례 재조회 (11-06 확인 실행: 11번 조회 뒤 호출 한도)
                return self._block(name, args, "wait_for_notice",
                                   f"{sid} 는 이번 차례에 이미 '{self._turn_runs[sid]}' 응답을 받았다. 같은 차례에 상태를 다시 조회하지 않는다",
                                   "실행이 진행 중이라고 알리고 이번 답을 끝낸다. 실행이 끝나면 화면이 완료 알림을 보낸다")
            why = self.run_problem(sid)
            if why:
                return self._block(name, args, "blocked", why, self._next_hint())
            want = self.approved_seeds.get(sid)                       # 11-07: 화면에 보인 seed 와 실제 실행 seed 를 같게
            if want is not None:
                try:
                    req = sorted({int(s) for s in (args.get("seeds") or DEFAULT_RUN_SEEDS)})
                except (TypeError, ValueError):
                    req = None
                if req != sorted(want):
                    return self._block(name, args, "blocked",
                                       f"승인한 카드의 seed {sorted(want)} 와 다른 seed {req} 로 실행하려 한다",
                                       f"run_experiment(scenario_id={sid}, seeds={sorted(want)}) 로 부른다. 다른 seed 로 돌리려면 "
                                       "request_approval(seeds=…) 로 새 카드를 띄워 사용자 승인을 다시 받는다")
            self.events.append({"event": "passed", "turn": self.turn, "tool": name, "scenario_id": sid})
            return None
        if name in ("get_report", "get_toolgroup_stats", "get_kpi"):   # 승인 전 시나리오의 결과는 보이지 않음 (기준 운영 조회는 그대로)
            sid, ref = args.get("scenario_id"), args.get("compare_to")
            for s in (sid, ref):
                if s and s in self.builds and s not in self.approved and s not in self.started:
                    return self._block(name, args, "blocked", f"{s} 는 아직 사용자가 버튼으로 승인하지 않은 시나리오라 결과를 보지 않는다",
                                       self._next_hint())
            if name == "get_report" and sid and ref and sid in self.builds and ref in self.builds:
                if (sid, ref) not in self.pairs:
                    if (ref, sid) in self.pairs:
                        return self._block(name, args, "blocked", "비교 방향이 사용자가 승인한 것(대상 − 기준)과 반대다",
                                           f"get_report(scenario_id={ref}, compare_to={sid}) 로 부른다")
                    return self._block(name, args, "blocked", "이 두 시나리오의 직접 비교는 사용자가 승인한 비교가 아니다",
                                       "request_approval(scenario_id=대상, compare_to=기준) 으로 비교 카드를 띄워 승인받는다")
            return None
        return None

    def after(self, name: str, args: dict, result: dict) -> None:
        args, result = args or {}, result or {}
        if name == "build_scenario" and result.get("scenario_id"):
            sid = result["scenario_id"]
            v = self._versions.get(self.request, 0) + 1
            self._versions[self.request] = v
            self.builds[sid] = Build(sid, result.get("status") or "", dict(args), self.turn, self.request, v)
            self.events.append({"event": "build", "turn": self.turn, "request": self.request, "version": v, "scenario_id": sid,
                                "status": result.get("status"), "slots": self.slot_sources(args, self.request_texts)})
        elif name == "request_approval" and result.get("status") == "awaiting_user_approval":
            tgt, ref = args.get("scenario_id"), args.get("compare_to") or None
            flags = []
            for role, sid in (("대상", tgt), ("기준", ref)):
                if sid and sid in self.builds and sid not in self.approved and sid not in self.started:
                    # 사용자가 이미 버튼으로 승인한 같은 설정(sid = 설정 지문)은 다시 표시하지 않음
                    flags += [f"{role}: {m}" if ref else m for m in self.missing_values(self.builds[sid].args, self.request_texts)]
            for c in self.cards.values():                              # 새 카드가 앞 카드를 대신함
                if c["status"] == "pending":
                    c["status"] = "stale"
            cid = f"C{self.turn}_{len(self.cards) + 1}"
            seeds = ((result.get("card") or {}).get("실행 조건") or {}).get("seeds") or args.get("seeds") or DEFAULT_RUN_SEEDS
            self.cards[cid] = {"id": cid, "target": tgt, "reference": ref, "request": self.request, "turn": self.turn,
                               "flags": flags, "status": "pending", "card": result.get("card"),
                               "seeds": sorted({int(s) for s in seeds})}
            self.events.append({"event": "card", "turn": self.turn, "card": cid, "target": tgt, "reference": ref, "flags": flags})
        elif name == "run_experiment":
            sid = args.get("scenario_id")
            st = result.get("status")
            self.events.append({"event": "tool_result", "turn": self.turn, "tool": name, "scenario_id": sid, "status": st})
            if sid:
                self._turn_runs[sid] = st
            if sid and st in ("ready", "started", "running"):
                self.started.add(sid)
                self._turn_ran = True
            if st in ("started", "running"):                           # 실제 DES 시작(대시보드) — 평가에서는 대역 응답
                self.events.append({"event": "des_started", "turn": self.turn, "scenario_id": sid, "status": st})

    # -- 판정 -----------------------------------------------------------------------------------------
    def run_problem(self, sid: str | None) -> str | None:
        if not sid:
            return "scenario_id 가 없다"
        if self.msg_kind == "cancel":
            return "사용자가 이번 메시지에서 취소했다. 실행하지 않는다"
        b = self.builds.get(sid)
        if b is not None and b.request in self.cancelled and sid not in self.started:
            return "사용자가 취소한 요청의 시나리오다. 실행하지 않는다"
        if b is not None and b.status != "ok":
            return f"{sid} 는 정적 점검을 통과하지 못했다(rejected). 실행하지 않는다"
        if sid in self.approved or sid in self.started:
            return None                                                # 버튼으로 승인됨, 또는 이미 시작한 실행의 상태 확인·다시 보기
        if b is None:
            return (f"{sid} 는 이 대화에서 사용자가 승인한 시나리오가 아니다. build_scenario → request_approval 로 카드를 띄우고 "
                    "사용자의 '확인 후 실행' 버튼을 기다린다")
        return ("사용자가 '확인 후 실행' 버튼으로 승인하지 않은 시나리오다. 글로 '맞음'이라고 해도 승인이 아니다. "
                "request_approval 로 카드를 띄우고 버튼을 눌러 달라고 안내한다")

    def missing_values(self, args: dict, sources: list, name_sources: list | None = None) -> list:
        """필수 값이 사용자 말에 항목·단위·방향까지 맞게 있는지. 없는 것 목록 (빈 목록 = 모두 있음). 카드의 '사용자가 말하지 않은 값'."""
        return [s["slot"] for s in self.slot_sources(args, sources, name_sources) if s["source"] is None]

    def slot_sources(self, args: dict, sources: list, name_sources: list | None = None) -> list:
        """필수 항목마다 {slot, value, source}. source = 근거가 된 사용자 표현 (없으면 None).

        값(숫자·단위·방향·시작 상태)은 이 요청의 사용자 말(sources)에서만. 대상 이름(툴그룹·area·제품·제품군)은 대화 전체의
        사용자 말(name_sources, 없으면 이 대화의 all_texts)에서도 찾는다 — "WE_FE_84 가동률은?" 뒤 "그 툴그룹에 1대 증설" 같은 경우."""
        texts = [s for s in sources if s]
        text = "\n".join(texts)
        ntext = "\n".join(s for s in (name_sources if name_sources is not None else (self.all_texts or texts)) if s)
        ntext = text + "\n" + ntext
        facts = extract_facts(texts)
        out = []

        def add(slot, value, fact):
            out.append({"slot": slot, "value": value, "source": fact if isinstance(fact, str) or fact is None else fact.text})

        def dir_ok(f, want, slot):
            d = f.dir or slot_direction(texts, slot)
            return d == 0 or want == 0 or d == want

        def find_pct(v, slot=None, signed=0):
            v = abs(float(v))
            return next((f for f in facts if f.unit == "pct" and abs(f.value - v) <= 0.06 and dir_ok(f, signed, slot)), None)

        def find_mult(m, slot=None, directional=True):
            m = float(m)
            if m <= 0:
                return None
            want = (1 if m > 1 else -1) if directional else 0
            f = find_pct(abs(m - 1) * 100, slot, want)                   # "n% 늘·줄"
            if f is None and not directional:
                f = find_pct(abs(1 / m - 1) * 100)                       # 속도·빈도·고장 간격: 역수 표현
            if f is None and directional:                                # 역수 후보는 방향을 모를 때만
                f = next((x for x in facts if x.unit == "pct" and abs(x.value - abs(1 / m - 1) * 100) <= 0.06
                          and not (x.dir or slot_direction(texts, slot))), None)
            if f is None:
                f = next((x for x in facts if x.unit == "x" and (abs(x.value - m) <= 5e-3 or abs(x.value - 1 / m) <= 5e-3)), None)
            return f

        def find_unit(v, unit, signed=0, slot=None):
            v = abs(float(v))
            return next((f for f in facts if f.unit == unit and abs(f.value - v) < 1e-6 and dir_ok(f, signed, slot)), None)

        def name_src(n):
            return n if _has_name(ntext, n) else None

        def area_src(a):                                               # 여러 area 에 걸치는 일반 명칭(식각·계측)은 area 를 정한 것이 아님
            if _has_name(ntext, a):
                return a
            return next((w for w in AREA_ALIASES.get(a, ()) if w in ntext and len(ALIAS_AREAS.get(w, ())) == 1), None)

        all_products = ALL_PRODUCTS_RE.search(text)
        v = args.get("total_release_pct")
        if v not in (None, 0, 0.0):                                    # 바뀌지 않는 값(0%, 배율 1, 0대)은 사용자 값이 아니어도 됨
            add(f"투입 {v}%", v, find_pct(v, "release", 1 if float(v) > 0 else -1))
        for g, m in (args.get("group_mult") or {}).items():
            if abs(float(m) - 1) > 1e-9:
                add(f"{g} 투입 배율 {m}", m, find_mult(m, "release"))
                add(f"대상 제품군 {g}", g, name_src(g) or (all_products.group(0) if all_products else None))
        for it in args.get("product_mult") or []:
            m = float(it.get("mult", 1))
            if abs(m - 1) < 1e-9:
                continue
            add(f"{it.get('product')} 투입 배율 {m}", m, find_mult(m, "release"))
            p = it.get("product", "")
            add(f"대상 제품 {p}", p, (all_products.group(0) if all_products else None) or self._product_src(p, ntext))
        for k, m in (args.get("lot_type_mult") or {}).items():         # 11-07: hot·superhot 투입 배율
            try:
                m = float(m)
            except (TypeError, ValueError):
                continue
            if abs(m - 1) < 1e-9:
                continue
            if m == 0:
                src = LOT_STOP_RE.search(text)
                src = src.group(0) if src else None
            else:
                src = find_mult(m, "release")
                w = next((r for v, r in LOT_MULT_WORDS.items() if abs(v - m) < 1e-9), None)
                if src is None and w is not None and w.search(text):
                    src = w.search(text).group(0)
            add(f"{k} 투입 배율 {m:g}", m, src)
            if k == "superhot":
                name = SUPERHOT_RE.search(ntext)
            else:                                                      # superhot 을 지운 뒤 hot 을 찾는다
                name = HOT_RE.search(SUPERHOT_RE.sub(" ", ntext))
            add(f"대상 lot 유형 {k}", k, name.group(0) if name else None)
        st = args.get("start_state")
        for key, default, label in (("end_week", DEFAULT_END_WEEK, "실행 길이"), ("warmup_weeks", DEFAULT_WARMUP_WEEK, "warm-up")):
            v = args.get(key)                                           # 11-07: 기간 값도 사용자 말(주·년, 1년 = 52주)에 근거가 있어야 함
            try:
                v = None if v is None else float(v)
            except (TypeError, ValueError):
                continue
            if v is not None and v != default and not st:
                add(f"{label} {v:g}주", v, find_unit(v, "week"))
        run_end = (STATE_WEEK[st] + BRANCH_HORIZON) if st in STATE_WEEK else float(args.get("end_week") or DEFAULT_END_WEEK)
        ev_default = float(STATE_WEEK[st]) if st in STATE_WEEK else DEFAULT_EVENT_START

        def event_start(label, sw):                                    # 11-08 (B5): 정지·고장 특성 시작 주는 기본값(30주, 시작 상태면 그 주)
            if sw is None:                                              # 이 아니면 사용자 말에 근거가 있어야 함
                return
            sw = float(sw)
            if sw != ev_default:
                src = find_unit(sw, "week")
                if src is None and sw == 0:
                    m = START0_RE.search(text)
                    src = m.group(0) if m else None
                add(f"{label} 시작 {sw:g}주", sw, src)

        for o in args.get("outages") or []:
            add(f"정지 툴그룹 {o.get('toolgroup')}", o.get("toolgroup"), name_src(o.get("toolgroup", "")))
            add(f"정지 대수 {o.get('n_tools')}대", o.get("n_tools"), find_unit(o.get("n_tools", 0), "count"))
            event_start("정지", o.get("start_week"))
            d = o.get("duration_weeks")                                 # 실행 끝까지 닿는 기간 = 기간 없음(끝까지)과 같음
            s_eff = float(o["start_week"]) if o.get("start_week") is not None else ev_default
            if d is not None and s_eff + float(d) < run_end:
                days = float(d) * 7                                     # "3일 동안" → 0.4286주 (일 단위로 말한 기간도 인정)
                add(f"정지 기간 {d}주", d, find_unit(d, "week")
                    or next((f for f in facts if f.unit == "day" and abs(f.value - days) < 0.02), None))
        for b in args.get("breakdown_changes") or []:
            event_start("고장 특성", b.get("start_week"))
        for t in args.get("tool_changes") or []:
            if not t.get("delta"):
                continue
            add(f"장비 수 툴그룹 {t.get('toolgroup')}", t.get("toolgroup"), name_src(t.get("toolgroup", "")))
            dv = int(t["delta"])
            add(f"장비 증감 {dv:+d}대", dv, find_unit(dv, "count", 1 if dv > 0 else -1, "tool"))
            sw = t.get("start_week")                                   # 장비 증감의 기본은 처음부터(0주). 다른 주는 사용자가 말해야 함 (11-06)
            if sw not in (None, 0, 0.0) and not st:
                add(f"장비 증감 시작 {float(sw):g}주", sw, find_unit(sw, "week"))
        tg_area = self._tg_areas()
        for key, mults in (("processing_time", {"mult": True, "speed_mult": False}),
                           ("pm_changes", {"interval_mult": True, "frequency_mult": False, "duration_mult": True}),
                           ("breakdown_changes", {"mttr_mult": True, "mttf_mult": False})):
            slot = {"processing_time": "pt", "pm_changes": "pm", "breakdown_changes": "mttr"}[key]
            listed = {x.get("toolgroup") for x in args.get(key) or [] if x.get("toolgroup")}
            for x in args.get(key) or []:
                if x.get("area"):
                    add(f"{COMPONENT_KEYS[key]} 대상 area {x['area']}", x["area"], area_src(x["area"]))
                tg = x.get("toolgroup")
                if tg:
                    src = name_src(tg)
                    if src is None:                                    # area 를 말했고 그 area 툴그룹을 모두 나열 = area 전체
                        a = tg_area.get(tg)
                        if a and area_src(a) and {t for t, aa in tg_area.items() if aa == a} <= listed:
                            src = area_src(a)
                    add(f"{COMPONENT_KEYS[key]} 대상 툴그룹 {tg}", tg, src)
                for m, directional in mults.items():
                    if x.get(m) is not None and abs(float(x[m]) - 1) > 1e-9:
                        add(f"{COMPONENT_KEYS[key]} {m} {x[m]}", x[m], find_mult(x[m], slot, directional))
        for h in args.get("hot_conversion") or []:
            share = float(h.get("share", 0))
            src = find_pct(100 * share) if share < 0.999 else (ALL_SHARE_RE.search(text).group(0) if ALL_SHARE_RE.search(text)
                                                                else find_pct(100.0))
            add(f"hot 전환 비율 {share}", share, src)
            if h.get("duration_days") is not None:
                add(f"hot 전환 기간 {h['duration_days']}일", h["duration_days"], find_unit(h["duration_days"], "day"))
            if self._covers_all(set(h.get("products") or []), set(h.get("groups") or [])) and \
                    not any(self._product_src(p, text, groups=False) for ps in self.groups.values() for p in ps) and \
                    not any(_has_name(text, g) for g in self.groups):
                continue                                               # 제품 전부 나열 = 대상 생략 (사용자가 대상을 말하지 않음)
            for p in h.get("products") or []:
                add(f"hot 대상 제품 {p}", p, self._product_src(p, ntext))
            for g in h.get("groups") or []:
                add(f"hot 대상 제품군 {g}", g, name_src(g))
        for x in args.get("dispatch_changes") or []:                   # 11-09 (D-88): 디스패칭 순서 변경 (베타)
            if x.get("area"):
                add(f"디스패칭 대상 area {x['area']}", x["area"], area_src(x["area"]))
            if x.get("toolgroup"):
                add(f"디스패칭 대상 툴그룹 {x['toolgroup']}", x["toolgroup"], name_src(x["toolgroup"]))
            if x.get("rank3") is not None:
                m = DISPATCH_FIFO_RE.search(text)
                add("디스패칭 3순위 도착 순서(FIFO)", x["rank3"], m.group(0) if m else None)
            if x.get("cr_before_setup"):
                m, s = DISPATCH_CR_RE.search(text), DISPATCH_SETUP_RE.search(text)
                add("디스패칭 critical ratio 를 setup 보다 먼저", True, f"{m.group(0)} … {s.group(0)}" if m and s else None)
            sw = x.get("start_week")                                   # 기본은 처음부터(시작 상태면 그 주). 다른 주는 사용자가 말해야 함
            if sw is not None and float(sw) != (float(STATE_WEEK[st]) if st in STATE_WEEK else 0.0):
                add(f"디스패칭 시작 {float(sw):g}주", sw, find_unit(sw, "week"))
            if x.get("end_week") is not None:
                add(f"디스패칭 끝 {float(x['end_week']):g}주", x["end_week"], find_unit(x["end_week"], "week"))
        if st:
            add(f"시작 상태 {st}", st, next((w for w in STATE_WORDS.get(st, (st,)) if w in text), None))
        return out

    def target_problem(self, args: dict) -> str | None:
        """대상 넓힘 검사 (build 전). 사용자 말(이 요청)의 제품·제품군·툴그룹보다 넓은 대상이면 이유."""
        text = "\n".join(self.request_texts)
        named_p = {p for ps in self.groups.values() for p in ps if self._product_src(p, text, groups=False)}
        named_g = {g for g in self.groups if _has_name(text, g)}
        allowed_p = named_p | {p for g in named_g for p in self.groups[g]}
        probs = []
        for k, h in enumerate(args.get("hot_conversion") or []):
            ps, gs = set(h.get("products") or []), set(h.get("groups") or [])
            if (not ps and not gs) or self._covers_all(ps, gs):         # 대상 생략 = 제품 전부 나열 = 모든 제품
                if named_p or named_g:
                    probs.append(f"hot 전환 대상을 모든 제품으로 넓힘 (요청: {', '.join(sorted(named_p | named_g))})")
                continue
            extra_g = gs - named_g
            extra_p = ps - allowed_p
            if extra_g:
                probs.append(f"hot 전환에 요청에 없는 제품군 {', '.join(sorted(extra_g))}"
                             + (f" (요청: {', '.join(sorted(named_p))})" if named_p else ""))
            if extra_p:
                probs.append(f"hot 전환에 요청에 없는 제품 {', '.join(sorted(extra_p, key=_pnum))}"
                             + (f" (요청: {', '.join(sorted(named_p | named_g, key=_pnum))})" if named_p or named_g else ""))
        extra_pm = {it.get("product") for it in args.get("product_mult") or []} - allowed_p - {None}
        if extra_pm and not ALL_PRODUCTS_RE.search(text):
            probs.append(f"투입 배율에 요청에 없는 제품 {', '.join(sorted(extra_pm, key=_pnum))}")
        if not ALL_PRODUCTS_RE.search(text):
            extra_gm = {g for g, v in (args.get("group_mult") or {}).items() if abs(float(v) - 1) > 1e-9} - named_g
            if extra_gm and (named_g or named_p):
                probs.append(f"투입 배율에 요청에 없는 제품군 {', '.join(sorted(extra_gm))}")
        named_tg = set(TOOLGROUP_RE.findall(text))
        named_area = any(_has_name(text, a) or any(w in text for w in AREA_ALIASES.get(a, ())) for a in AREAS)
        if named_tg and not named_area:
            for key in ("outages", "tool_changes", "processing_time", "pm_changes", "breakdown_changes", "dispatch_changes"):
                for x in args.get(key) or []:
                    if x.get("area"):
                        probs.append(f"{COMPONENT_KEYS[key]}: 요청은 툴그룹({', '.join(sorted(named_tg))})인데 area {x['area']} 전체로 넓힘")
                    elif x.get("toolgroup") and x["toolgroup"] not in named_tg:
                        probs.append(f"{COMPONENT_KEYS[key]}: 요청에 없는 툴그룹 {x['toolgroup']}")
        return "; ".join(probs) or None

    def ambiguous_aliases(self) -> list:
        """이 요청의 사용자 말에 여러 area 에 걸리는 일반 명칭(식각·계측)이 있고, 그중 어느 area 인지 아직 정하지 않았으면
        [(명칭, [area…])] (11-07 팀원 추가 요청: 양쪽 선택지를 모두 보이며 묻게). '건식 식각'처럼 하나로 정해지는 말이나
        area·툴그룹 이름을 말했으면 정한 것으로 본다."""
        text = "\n".join(self.request_texts)
        tg_area = self._tg_areas()
        out = []
        for alias, areas in ALIAS_AREAS.items():
            if len(areas) < 2 or alias not in text:
                continue
            chosen = [a for a in areas if _has_name(text, a)
                      or any(w in text for w in AREA_ALIASES.get(a, ()) if len(ALIAS_AREAS.get(w, ())) == 1)]
            chosen += [t for t, a in tg_area.items() if a in areas and _has_name(text, t)]
            if not chosen:
                out.append((alias, sorted(areas)))
        return out

    def mapping_problem(self, args: dict) -> list:
        """제품군 매핑 검사 (11-06 밤). 제품별 투입 배율에서, 같은 제품군 안의 '사용자가 따로 이름을 말하지 않은' 제품끼리는
        배율이 같아야 한다 (예외로 말한 제품만 다를 수 있음). 11-06 확인 실행: B_mid 증가를 C_short 제품 일부에 적용하고
        Product_10 만 ×0.5 로 바꿈 → 같은 제품군 안에서 배율이 갈림."""
        text = "\n".join(self.request_texts)
        pm = {}
        for it in args.get("product_mult") or []:
            if it.get("product"):
                pm[it["product"]] = round(float(it.get("mult", 1)), 6)
        probs = []
        for g, members in self.groups.items():
            unnamed = {p: pm[p] for p in members if p in pm and not self._product_src(p, text, groups=False)}
            if len(set(unnamed.values())) > 1:
                desc = ", ".join(f"{p} ×{m:g}" for p, m in sorted(unnamed.items(), key=lambda x: _pnum(x[0])))
                probs.append(f"제품군 {g} 안에서 사용자가 따로 말하지 않은 제품의 투입 배율이 다름 ({desc})")
        return probs

    # -- 보조 -----------------------------------------------------------------------------------------
    def _tg_areas(self) -> dict:
        """툴그룹 → area (Agent 가 도구의 fab 정보로 채워 줌). 못 구하면 빈 dict (그러면 툴그룹은 이름으로만 확인)."""
        if self.toolgroup_areas is None and self.toolgroup_area_provider is not None:
            try:
                self.toolgroup_areas = dict(self.toolgroup_area_provider() or {})
            except Exception:                                          # fab 정보를 못 읽어도 검사는 이름 기준으로 계속
                self.toolgroup_areas = {}
        return self.toolgroup_areas or {}

    def _product_src(self, p: str, text: str, groups: bool = True) -> str | None:
        if _has_name(text, p):
            return p
        n = _pnum(p)
        m = re.search(rf"(?<!\d){n}\s*번\s*제품|제품\s*{n}(?!\d)", text) if n else None
        if m:
            return m.group(0)
        if groups:
            return next((g for g, ps in self.groups.items() if p in ps and _has_name(text, g)), None)
        return None

    def _covers_all(self, ps: set, gs: set) -> bool:
        allp = {p for v in self.groups.values() for p in v}
        got = set(ps) | {p for g in gs for p in self.groups.get(g, [])}
        return bool(got) and got >= allp

    @staticmethod
    def _kinds(args: dict) -> set:
        return {COMPONENT_KEYS[k] for k, v in (args or {}).items() if k in COMPONENT_KEYS and v not in (None, [], {})}

    def _superseded(self, pending: list, new: list) -> set:
        """수정 답 차례에 새로 만든 시나리오가 대신하는 확인 메시지의 시나리오 (= 수정 전 시나리오).

        새 시나리오마다, 변경 항목 종류가 겹치는 앞 시나리오 중 인자가 가장 비슷한 것 하나를 대신한 것으로 본다
        (다른 인자 수가 적은 것, 같으면 숫자 차이가 작은 것). 같은 id 로 다시 만든 것은 대신이 아니라 그대로."""
        out = set()
        for n in new:
            bn = self.builds.get(n)
            if bn is None or bn.status != "ok" or n in pending:
                continue
            kn, fn = self._kinds(bn.args), _flat(bn.args)
            best = None
            for p in pending:
                bp = self.builds.get(p)
                if bp is None or p in out or not (self._kinds(bp.args) & kn):
                    continue
                fp = _flat(bp.args)
                keys = set(fn) | set(fp)
                diff = sum(1 for k in keys if fn.get(k) != fp.get(k))
                num = sum(abs(fn[k] - fp[k]) for k in keys & set(fn) & set(fp)
                          if isinstance(fn[k], (int, float)) and isinstance(fp[k], (int, float)) and fn[k] != fp[k])
                if best is None or (diff, num) < best[0]:
                    best = ((diff, num), p)
            if best:
                out.add(best[1])
        return out

    def _next_hint(self) -> str:
        if self.msg_kind == "cancel":
            return "취소를 인정하고 실행하지 않았다고 답한다"
        return ("실행하지 않았다. build_scenario → request_approval 로 '확인 후 실행' 카드를 띄우고, 사용자가 버튼을 누를 때까지 "
                "기다린다. 같은 차례에 run_experiment 를 다시 부르지 않는다")

    def _not_built(self, name: str, args: dict, status: str, reason: str, nxt: str) -> dict:
        """생성 전 차단 결과 (11-07): 만들어지지 않았다는 것을 분명히 해 없는 id·카드를 지어내지 않게 한다."""
        r = self._block(name, args, status, reason, nxt)
        r.update({"scenario_id": None, "created": False,
                  "note": "시나리오가 만들어지지 않았다 (scenario_id 없음). request_approval·run_experiment 를 부르지 않고, "
                          "사용자에게 카드나 '확인 후 실행' 버튼을 안내하지 않는다. 빠진 값은 묻고, 아니면 사용자가 말한 값만으로 다시 만든다"})
        return r

    def _block(self, name: str, args: dict, status: str, reason: str, nxt: str) -> dict:
        ev = {"event": "blocked", "turn": self.turn, "request": self.request, "kind": self.msg_kind, "tool": name,
              "status": status, "reason": reason, "scenario_id": (args or {}).get("scenario_id")}
        self.events.append(ev)
        return {"status": status, "reason": reason, "next": nxt,
                "note": "실행 확인 코드가 이 요청을 막았다. 도구는 실행되지 않았다 (사용자에게 실행했다고 말하지 않는다)"}


def _flat(x, prefix: str = "") -> dict:
    """build 인자를 (경로 → 값) 으로 편다 (description 제외). 시나리오끼리 얼마나 다른지 셀 때 쓴다."""
    out = {}
    if isinstance(x, dict):
        for k, v in x.items():
            if k != "description":
                out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(x, list):
        for i, v in enumerate(x):
            out.update(_flat(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else x
    return out


def _pnum(p: str) -> int:
    m = re.search(r"(\d+)$", p or "")
    return int(m.group(1)) if m else 0
