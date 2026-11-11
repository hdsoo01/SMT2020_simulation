"""생산 시나리오(L0) → 제품별 투입 일정 + 정적 사전 점검.

시나리오 JSON 예시:

{
  "name": "P04_A125_B075_C075",
  "seed": 1,
  "grouping": "tier",                 # product_groups.json 의 그룹핑 이름 (생략하면 default)
  "end_week": 72,
  "label_start_week": 20,             # 여기부터가 학습용 구간 (KPI warm-up 경계로도 쓴다)
  "phases": [
    {"start_week": 0,  "group_mult": {"A_long": 1.0,  "B_mid": 1.0,  "C_short": 1.0}},
    {"start_week": 20, "group_mult": {"A_long": 1.25, "B_mid": 0.75, "C_short": 0.75}}
  ]
}

- phase마다 그룹 지정 방식은 둘 중 하나다.
  group_mult  : 기준(엑셀 Lotrelease) 대비 배율
  group_wspw  : 그룹의 regular 목표 생산량 (wafer/주). 그룹 안 제품에 기준 비율대로 나눈다
  선택적으로 product_mult 로 그룹 안 특정 제품만 따로 줄 수 있다.
- 위 배율이 바꾸는 것은 regular lot 투입 간격뿐이다. hot/superhot 은 엑셀 그대로 둔다.
- lot_type_mult (11-07 추가, 생략 가능): phase 마다 hot·superhot 투입 배율. 예: {"superhot": 0} → 그 phase 동안 superhot
  투입 중단, {"hot": 0.5} → hot 투입 간격 두 배(절반), {"hot": 2} → 간격 절반(두 배). 0 ≤ 배율 ≤ 5.
  "hot" 은 우선순위 20 흐름(HotLot_1~10), "superhot" 은 superhot 흐름. 엑셀은 그대로 두고 실행할 때 투입 일정만 바꾼다.
  생략하거나 1 이면 원래 모델과 똑같다 (투입 간격·난수 순서 그대로). 0 인 구간에서는 lot 을 내지 않는다.
- limits (생략 가능): min_product_mult 0.5 (배치 대기 폭증 방지), max_product_mult 2.0,
  max_static_util 0.97 (정적 다운타임 후 가동률 상한, 넘으면 정상상태가 없을 가능성이 크다).

켜고 끄는 선택 항목 (생략하면 기본값):
  "extensions": {"ic": true, "work_active": true}
      관찰 전용 기록 확장 (datagen/extensions.py). 꺼도 시뮬레이션 결과는 같다.
  "downtime": [
      {"type": "outage", "toolgroup": "Litho_FE_111", "n_tools": 2, "start_week": 40, "duration_weeks": 3},
      {"type": "breakdown_scale", "area": "Dielectric", "mttr": 1.5, "start_week": 30, "end_week": 60}]
      다운타임 시나리오 (datagen/downtime_scenarios.py). 비우면 원래 모델 그대로다.
      check 명령이 정지 기간의 정적 가동률을 보여 주고, 100%를 넘으면 경고한다 (실행은 막지 않음).
  "hot_conversion": [
      {"share": 0.1, "products": ["Product_5"], "start_week": 30, "end_week": 34},
      {"share": 0.05, "groups": ["A_long"], "start_week": 30}]
      regular → hot 전환 (11-04 추가). [start_week, end_week) 에 투입되는 대상 regular lot 중 share 비율을
      우선순위 20(hot)으로 투입한다. products·groups 를 둘 다 생략하면 모든 제품. end_week 생략은 끝까지.
      전환은 결정적이다 (제품별 k번째 lot 이 floor((k+1)·share) > floor(k·share) 이면 전환) — 난수를 쓰지 않아
      전환 외의 사건(고장·PM·납기 난수)은 기준 실행과 같은 순서로 뽑힌다. 전환된 lot 의 납기는 그 제품 hot 흐름의
      납기 여유를 쓰고, lot_id 끝에 "_H" 를 붙인다 (runner.py 가 hot_conversions.csv 로도 남긴다).
      투입 총량은 그대로라 정적 가동률은 바뀌지 않는다. 비우면 원래 모델 그대로다.
  "overrides": {"tools": [...], "processing_time": [...], "pm": [...]}
      엑셀 파라미터 덮어쓰기 (11-04 추가, datagen/data_overrides.py): 장비 수 증감, 가공 시간 배율, PM 간격·소요 배율.
      prepare() 가 덮어쓴 ModelData 위에서 정적 점검을 하므로 정적 가동률에 반영된다. 비우면 원래 모델 그대로다.
  "branch": {"week": 34, "seed": 3}
      고정 시작 상태에서 갈라지기 (C1, 11-05 추가, LLM연계_검토.md 6.11절). "seed" 로 week 주까지 기준 실행과 똑같이
      간 뒤, week 주 1분 전에 난수 생성기를 BRANCH_SEED_OFFSET + branch.seed 로 다시 seed 한다. 같은 (seed, week) 의
      갈래들은 week 주의 상태가 같고 그 뒤 난수만 다르다. 바뀌는 것(투입 phase, downtime, hot_conversion, 장비 감축)은
      모두 week 주 이후에 시작해야 하고, 처음부터 적용되는 가공 시간·PM 덮어쓰기와 장비 증설은 함께 쓸 수 없다.
      비교 대상은 같은 (seed, week, branch.seed) 의 바뀐 것 없는 갈래다. 비우면 원래 모델 그대로다.
  "dispatch": [
      {"toolgroup": "Litho_FE_111", "rank3": "fifo", "start_week": 0},
      {"area": "Implant", "cr_before_setup": true, "start_week": 30, "end_week": 40}]
      디스패칭 순서 변경 (베타, 11-09, SLLMT D-88). 시트 규칙은 106개 toolgroup 전부 1순위 lot 우선순위 → 2순위 setup 시간 →
      3순위 critical ratio → 도착 순서다. **1순위는 바꾸지 않는다.** 바꿀 수 있는 것은 둘이고 한 항목에 하나만:
        rank3 "fifo"          3순위를 critical ratio 대신 도착 순서로 (lot 고르기·배치 그룹·배치 구성원 모두)
        cr_before_setup true  setup 을 쓰는 toolgroup(9개)에서 critical ratio 를 setup 시간보다 먼저
      대상은 toolgroup·toolgroups·area 중 하나. area + cr_before_setup 이면 그 area 의 setup toolgroup 만 대상이다.
      [start_week, end_week) 동안만 적용 (end_week 생략은 끝까지). 한 toolgroup 에 규칙 하나. 분기와 함께 쓰면 분기 주 이후 시작.
      비우면 원래 모델 그대로다 (규칙 없는 실행은 결과가 비트 단위로 같다 — 11-09 짧은 실행으로 확인).

명령줄:
    python -B datagen/scenario.py check  datagen/scenarios/pilot/P04.json
    python -B datagen/scenario.py quick  A_long=1.25 B_mid=0.75 C_short=0.75
    python -B datagen/scenario.py quick  --wspw A_long=3500 B_mid=3000 C_short=2500
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import static_load as sl  # noqa: E402

WEEK = sl.WEEK
NW = sl.NW
DEFAULT_LIMITS = {"min_product_mult": 0.5, "max_product_mult": 2.0, "max_static_util": 0.97}
BRANCH_SEED_OFFSET = 1_000_000           # 분기 뒤 난수 seed = 이 값 + branch.seed (원래 seed 1~5 와 겹치지 않게)
LOT_KINDS = ("hot", "superhot")          # lot_type_mult 로 바꿀 수 있는 투입 흐름 (regular 는 group_mult·product_mult)
MAX_LOT_TYPE_MULT = 5.0


def stream_kind(s) -> str:
    """투입 흐름의 lot 유형: regular(우선순위 10) / hot(우선순위 20 이상, superhot 아님) / superhot."""
    if s.is_superhot:
        return "superhot"
    return "regular" if s.priority == 10 else "hot"


def lot_type_mult_errors(phase: dict) -> list:
    """phase 의 lot_type_mult 형식 검사 (11-07)."""
    ltm = phase.get("lot_type_mult")
    if ltm is None:
        return []
    if not isinstance(ltm, dict):
        return [f"phase {phase.get('start_week')}: lot_type_mult 는 {{'hot': 배율, 'superhot': 배율}} 형식"]
    errs = []
    for k, v in ltm.items():
        if k not in LOT_KINDS:
            errs.append(f"phase {phase.get('start_week')}: lot_type_mult 의 {k!r} 는 쓸 수 없다 (가능: {list(LOT_KINDS)}, "
                        "regular 는 group_mult·product_mult)")
        elif not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= float(v) <= MAX_LOT_TYPE_MULT:
            errs.append(f"phase {phase.get('start_week')}: lot_type_mult[{k!r}] 는 0 이상 {MAX_LOT_TYPE_MULT:g} 이하 ({v!r})")
    return errs


def load_groups(name: str | None = None) -> tuple[str, dict]:
    cfg = json.loads((HERE / "product_groups.json").read_text(encoding="utf-8-sig"))
    name = name or cfg["default"]
    return name, cfg["groupings"][name]["groups"]


@dataclass
class Plan:
    schedule: dict                      # stream lot_type -> [(start_min, interval_min), ...]
    phases: list = field(default_factory=list)   # phase별 점검 결과
    feasible: bool = True
    downtime: list = field(default_factory=list)  # 다운타임 이벤트별 정적 영향 (경고만, 실행은 막지 않음)
    errors: list = field(default_factory=list)    # 형식 오류 (실행을 막는다)
    hot: list = field(default_factory=list)       # regular → hot 전환 규칙 (정규화, runner 가 쓴다)
    overrides: list = field(default_factory=list)  # 파라미터 덮어쓰기 요약 (prepare 가 채운다)
    branch: dict | None = None                    # 분기 (C1): {"week", "seed", "reseed"} (prepare 가 채운다)
    dispatch: list = field(default_factory=list)  # 디스패칭 순서 변경 (베타, 정규화, runner 가 쓴다)


def _regular_streams(data):
    return {s.product: s for s in data.release_streams if s.priority == 10 and not s.is_superhot}


def phase_lots_per_week(data, groups: dict, phase: dict) -> tuple[dict, dict]:
    """(제품 -> regular 배율, 제품 -> 총 투입 lot/주(hot·superhot 포함))."""
    reg = _regular_streams(data)
    base_reg_lpw = {p: s.lots_per_release / s.release_interval * WEEK for p, s in reg.items()}
    mult = {p: 1.0 for p in reg}
    if "group_mult" in phase and "group_wspw" in phase:
        raise ValueError("phase에는 group_mult와 group_wspw 중 하나만 쓴다")
    for g, prods in groups.items():
        if "group_mult" in phase and g in phase["group_mult"]:
            for p in prods:
                mult[p] = float(phase["group_mult"][g])
        elif "group_wspw" in phase and g in phase["group_wspw"]:
            base_g = sum(base_reg_lpw[p] for p in prods) * NW
            for p in prods:
                mult[p] = float(phase["group_wspw"][g]) / base_g
    for p, m in phase.get("product_mult", {}).items():
        mult[p] = float(m)
    lpw = {p: base_reg_lpw[p] * mult[p] for p in reg}
    ltm = phase.get("lot_type_mult") or {}
    for s in data.release_streams:                       # hot/superhot 은 엑셀 그대로 (lot_type_mult 가 있으면 그 배율)
        if not (s.priority == 10 and not s.is_superhot):
            m = float(ltm.get(stream_kind(s), 1.0))
            lpw[s.product] = lpw.get(s.product, 0.0) + s.lots_per_release / s.release_interval * WEEK * m
    return mult, lpw


def resolve(spec: dict, data, model=None) -> Plan:
    gname, groups = load_groups(spec.get("grouping"))
    limits = {**DEFAULT_LIMITS, **spec.get("limits", {})}
    model = model or sl.build(data)
    phases = sorted(spec["phases"], key=lambda ph: ph["start_week"])
    if phases[0]["start_week"] != 0:
        raise ValueError("첫 phase는 start_week 0 이어야 한다")
    reg = _regular_streams(data)
    schedule = {s.lot_type: [] for s in data.release_streams}
    plan = Plan(schedule=schedule)
    fmt = sum((lot_type_mult_errors(ph) for ph in phases), [])
    if fmt:
        plan.errors += fmt
        plan.feasible = False
        return plan
    for ph in phases:
        mult, lpw = phase_lots_per_week(data, groups, ph)
        start = float(ph["start_week"]) * WEEK
        ltm = ph.get("lot_type_mult") or {}
        for s in data.release_streams:
            if s.product in reg and reg[s.product] is s:
                schedule[s.lot_type].append((start, s.release_interval / mult[s.product]))
            elif stream_kind(s) in ltm and float(ltm[stream_kind(s)]) != 1.0:   # 11-07: hot·superhot 투입 배율 (0 = 투입 없음)
                m = float(ltm[stream_kind(s)])
                schedule[s.lot_type].append((start, s.release_interval / m if m > 0 else None))
            else:
                schedule[s.lot_type].append((start, s.release_interval))
        res = sl.evaluate(model, lpw)
        top = sl.ranking(res, 5)
        group_wspw = {g: round(sum(reg[p].lots_per_release / reg[p].release_interval * WEEK * mult[p]
                                   for p in ps) * NW, 1) for g, ps in groups.items()}
        problems = []
        for p, m in mult.items():
            if m < limits["min_product_mult"] or m > limits["max_product_mult"]:
                problems.append(f"{p} 배율 {m:.2f}가 허용 범위 [{limits['min_product_mult']}, {limits['max_product_mult']}] 밖")
        if top[0][1] > limits["max_static_util"]:
            problems.append(f"{top[0][0]} 정적 가동률 {100 * top[0][1]:.1f}% > {100 * limits['max_static_util']:.0f}%")
        plan.phases.append({
            "start_week": ph["start_week"], "grouping": gname, "group_wspw_regular": group_wspw,
            "total_wspw": round(sum(lpw.values()) * NW, 1),
            "product_mult": {p: round(m, 4) for p, m in sorted(mult.items(), key=lambda x: int(x[0].split("_")[1]))},
            "static_top5": [(t, round(100 * u, 2)) for t, u in top], "problems": problems})
        if problems:
            plan.feasible = False
    _check_downtime(spec, data, groups, phases, model, plan)
    _check_hot(spec, data, groups, phases, plan)
    _check_dispatch(spec, data, plan)
    return plan


def prepare(spec: dict, data):
    """overrides 를 적용한 ModelData 와 그 위의 Plan. (plan, data, 추가 outage 이벤트).

    실행기와 정적 점검이 같은 길을 쓰게 한다. 장비 증설·감축을 시점 지정으로 하면 생기는 outage 이벤트를
    다운타임 점검에도 넣는다. overrides 형식 오류는 plan.errors 로 돌려준다."""
    import data_overrides as do
    try:
        data2, extra, summary = do.apply(data, spec)
    except do.OverrideError as e:
        plan = Plan(schedule={}, feasible=False)
        plan.errors = list(e.problems)
        return plan, data, []
    spec2 = dict(spec, downtime=list(spec.get("downtime", [])) + extra) if extra else spec
    plan = resolve(spec2, data2)
    plan.overrides = summary
    _check_branch(spec, plan)
    return plan, data2, extra


def _neutral_phase(ph: dict) -> bool:
    if ph.get("group_wspw"):
        return False
    vals = list((ph.get("group_mult") or {}).values()) + list((ph.get("product_mult") or {}).values()) \
        + list((ph.get("lot_type_mult") or {}).values())
    return all(float(v) == 1.0 for v in vals)


def _check_branch(spec, plan) -> None:
    """분기(C1) 형식 검사. 바뀌는 것이 모두 분기 주 이후에 시작해야 갈래가 분기 주까지 기준 실행과 같다."""
    b = spec.get("branch")
    if not b:
        return
    end = float(spec["end_week"])
    t, k = b.get("week"), b.get("seed")
    errs = []
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 0 < t < end:
        errs.append(f"branch: week 는 0 초과 실행 끝({end:g}주) 미만 ({t!r})")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        errs.append(f"branch: seed 는 1 이상 정수 ({k!r})")
    if errs:
        plan.errors += errs
        return
    t = float(t)
    for ph in spec["phases"]:
        if float(ph["start_week"]) < t and not _neutral_phase(ph):
            errs.append(f"branch: 분기 주({t:g}) 전에 시작하는 투입 변경이 있다 (phase start_week {ph['start_week']})")
    for i, d in enumerate(spec.get("downtime") or []):
        if float(d.get("start_week", 0)) < t:
            errs.append(f"branch: downtime[{i}] 가 분기 주({t:g}) 전에 시작한다 ({d.get('start_week')})")
    for i, h in enumerate(spec.get("hot_conversion") or []):
        if float(h.get("start_week", 0)) < t:
            errs.append(f"branch: hot_conversion[{i}] 가 분기 주({t:g}) 전에 시작한다 ({h.get('start_week')})")
    for i, d in enumerate(spec.get("dispatch") or []):
        if float(d.get("start_week", 0)) < t:
            errs.append(f"branch: dispatch[{i}] 가 분기 주({t:g}) 전에 시작한다 ({d.get('start_week', 0)})")
    ov = spec.get("overrides") or {}
    if ov.get("processing_time") or ov.get("pm"):
        errs.append("branch: 가공 시간·PM 덮어쓰기는 실행 처음부터 적용돼 분기와 함께 쓸 수 없다")
    for i, e in enumerate(ov.get("tools") or []):
        if isinstance(e.get("delta"), int) and e["delta"] > 0:
            errs.append(f"branch: overrides.tools[{i}] 장비 증설은 새 장비가 처음부터 난수를 써서 분기와 함께 쓸 수 없다 (감축만 가능)")
        elif float(e.get("start_week", 0)) < t:
            errs.append(f"branch: overrides.tools[{i}] 감축이 분기 주({t:g}) 전에 시작한다")
    plan.errors += errs
    if not errs:
        plan.branch = {"week": t, "seed": int(k), "reseed": BRANCH_SEED_OFFSET + int(k)}


def _check_hot(spec, data, groups, phases, plan) -> None:
    """regular → hot 전환 규칙의 형식 검사와 정규화. 투입 총량은 같으므로 정적 가동률은 다시 계산하지 않는다."""
    rules = spec.get("hot_conversion") or []
    if not rules:
        return
    end = float(spec["end_week"])
    reg = _regular_streams(data)
    hot_due = {s.product: s.due_date_offset for s in data.release_streams if s.priority >= 20 and not s.is_superhot}
    order = lambda p: int(p.split("_")[1])
    for k, r in enumerate(rules):
        share = r.get("share")
        if not isinstance(share, (int, float)) or isinstance(share, bool) or not 0 < share <= 1:
            plan.errors.append(f"hot_conversion[{k}]: share 는 0 초과 1 이하의 수 ({share!r})")
            continue
        s0, s1 = float(r.get("start_week", 0)), float(r.get("end_week", end))
        if not 0 <= s0 < s1:
            plan.errors.append(f"hot_conversion[{k}]: start_week < end_week 이어야 한다 ({s0}, {s1})")
            continue
        prods, bad = set(), []
        for g in r.get("groups", []) or []:
            if g in groups:
                prods.update(groups[g])
            else:
                bad.append(f"없는 제품군 {g!r}")
        for p in r.get("products", []) or []:
            if p in reg:
                prods.add(p)
            else:
                bad.append(f"없는 제품 {p!r}")
        if bad:
            plan.errors.append(f"hot_conversion[{k}]: " + ", ".join(bad))
            continue
        if not r.get("groups") and not r.get("products"):
            prods = set(reg)
        ph = [p for p in phases if p["start_week"] <= s0][-1]
        mult, _ = phase_lots_per_week(data, groups, ph)
        lpw = sum(reg[p].lots_per_release / reg[p].release_interval * WEEK * mult[p] for p in prods)
        plan.hot.append({"rule": k, "products": sorted(prods, key=order), "share": float(share),
                         "start_week": s0, "end_week": min(s1, end),
                         "converted_lots_per_week": round(lpw * share, 1),
                         "hot_due_missing": sorted((p for p in prods if p not in hot_due), key=order)})


DISPATCH_KEYS = {"toolgroup", "toolgroups", "area", "rank3", "cr_before_setup", "start_week", "end_week"}


def _check_dispatch(spec, data, plan) -> None:
    """디스패칭 순서 변경(베타)의 형식·대상 검사와 정규화. 정적 부하는 바뀌지 않으므로 다시 계산하지 않는다.

    효과가 없는 규칙(setup 없는 toolgroup 의 CR-먼저, 이미 FIFO 인 3순위)은 조용히 넘기지 않고 오류로 돌려준다."""
    rules = spec.get("dispatch") or []
    if not rules:
        return
    if not isinstance(rules, list):
        plan.errors.append("dispatch 는 목록")
        return
    end = float(spec["end_week"])
    tgs = {n: s for n, s in data.toolgroups.items() if not s.is_delay}
    areas = {s.area for s in tgs.values()}
    setup_tgs = set(data.setup_bearing_toolgroups())
    sheet_fifo = {n for n, s in tgs.items() if "critical" not in (getattr(s, "rank3", "") or "").lower()}
    seen: dict = {}
    for k, r in enumerate(rules):
        if not isinstance(r, dict):
            plan.errors.append(f"dispatch[{k}]: 항목은 객체")
            continue
        errs = [f"dispatch[{k}]: 모르는 키 {x!r}" for x in sorted(set(r) - DISPATCH_KEYS)]
        n_tgt = sum(1 for x in ("toolgroup", "toolgroups", "area") if r.get(x))
        if n_tgt != 1:
            errs.append(f"dispatch[{k}]: toolgroup·toolgroups·area 중 하나만 준다")
        fifo = r.get("rank3") is not None
        crf = r.get("cr_before_setup") is not None and r.get("cr_before_setup") is not False
        if fifo and r.get("rank3") != "fifo":
            errs.append(f"dispatch[{k}]: rank3 는 'fifo' 만 (3순위를 도착 순서로). 지금 시트는 critical ratio ({r.get('rank3')!r})")
        if crf and r.get("cr_before_setup") is not True:
            errs.append(f"dispatch[{k}]: cr_before_setup 은 true 만 ({r.get('cr_before_setup')!r})")
        if fifo == crf:
            errs.append(f"dispatch[{k}]: rank3 'fifo' 와 cr_before_setup 중 하나만 준다 (둘 다 없으면 바뀌는 것이 없고, "
                        "둘 다면 3순위가 도착 순서라 setup 앞에 둘 critical ratio 가 없다)")
        s0 = r.get("start_week", 0)
        s1 = r.get("end_week", end)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (s0, s1)) or not 0 <= float(s0) < float(s1) <= end:
            errs.append(f"dispatch[{k}]: 0 ≤ start_week < end_week ≤ 실행 끝({end:g}주) ({s0!r}, {s1!r})")
        if errs:
            plan.errors += errs
            continue
        if r.get("area"):
            if r["area"] not in areas:
                plan.errors.append(f"dispatch[{k}]: 없는 area {r['area']!r}")
                continue
            names = sorted(n for n, s in tgs.items() if s.area == r["area"])
            if crf:
                names = [n for n in names if n in setup_tgs]
                if not names:
                    plan.errors.append(f"dispatch[{k}]: area {r['area']!r} 에는 setup 을 쓰는 toolgroup 이 없어 cr_before_setup 이 "
                                       f"효과가 없다 (setup toolgroup: {', '.join(sorted(setup_tgs))})")
                    continue
        else:
            names = [r["toolgroup"]] if r.get("toolgroup") else list(r["toolgroups"])
            bad = [n for n in names if n not in tgs]
            if bad:
                plan.errors.append(f"dispatch[{k}]: 없는 toolgroup {', '.join(map(repr, bad))}")
                continue
            if crf and any(n not in setup_tgs for n in names):
                plan.errors.append(f"dispatch[{k}]: setup 을 쓰지 않는 toolgroup({', '.join(n for n in names if n not in setup_tgs)})은 "
                                   f"2순위(setup)가 늘 0 이라 cr_before_setup 이 효과가 없다 (setup toolgroup: {', '.join(sorted(setup_tgs))})")
                continue
        if fifo and all(n in sheet_fifo for n in names):
            plan.errors.append(f"dispatch[{k}]: 3순위가 이미 도착 순서다 ({', '.join(names)})")
            continue
        dup = [n for n in names if n in seen]
        if dup:
            plan.errors.append(f"dispatch[{k}]: toolgroup 하나에 규칙 하나만 (dispatch[{seen[dup[0]]}] 와 겹침: {', '.join(dup)})")
            continue
        for n in names:
            seen[n] = k
        plan.dispatch.append({"rule": k, "toolgroups": sorted(names), **({"area": r["area"]} if r.get("area") else {}),
                              "rank3": "fifo" if fifo else None, "cr_before_setup": bool(crf),
                              "start_week": float(s0), "end_week": None if r.get("end_week") is None else float(s1)})


def _check_downtime(spec, data, groups, phases, model, plan) -> None:
    """다운타임 이벤트의 형식 검사와 정지 기간 정적 가동률. 100% 초과는 경고만 한다 —
    일시적 과부하(그동안 WIP가 쌓였다 풀리는 것)는 의도한 시나리오일 수 있다."""
    events = spec.get("downtime", [])
    if not events:
        return
    import downtime_scenarios as ds
    plan.errors += ds.validate(events, data, float(spec["end_week"]))
    if plan.errors:
        return
    for k, e in enumerate(events):
        ph = [p for p in phases if p["start_week"] <= e["start_week"]][-1]
        _, lpw = phase_lots_per_week(data, groups, ph)
        tgs = ds.affected(e, data)
        if e["type"] == "outage":
            n_out = len(ds.outage_tool_indices(e, data.toolgroups[e["toolgroup"]].num_tools))
            during = sl.evaluate(model, lpw, tools_out={e["toolgroup"]: n_out})
        else:
            during = sl.evaluate(model, lpw, breakdown_scale={t: (float(e.get("mttr", 1.0)), float(e.get("mttf", 1.0)))
                                                             for t in tgs})
        before = sl.evaluate(model, lpw)
        worst = max(tgs, key=lambda t: during[t]["util_after"])
        rep = {"event": k, "type": e["type"], "toolgroups": tgs, "start_week": e["start_week"],
               "worst_toolgroup": worst,
               "static_util_before_pct": round(100 * before[worst]["util_after"], 2),
               "static_util_during_pct": round(100 * during[worst]["util_after"], 2),
               "top3_during": [(t, round(100 * u, 2)) for t, u in sl.ranking(during, 3)]}
        if during[worst]["util_after"] > 1.0:
            rep["warning"] = (f"{worst} 정적 가동률 {100 * during[worst]['util_after']:.0f}% — 정지 기간 동안 "
                              "WIP가 계속 쌓인다 (의도한 일시 과부하면 무시)")
        plan.downtime.append(rep)


def load(path) -> dict:
    # utf-8-sig: 메모장·PowerShell 5.1 이 붙이는 BOM 이 있어도 읽는다
    spec = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for key in ("name", "seed", "end_week", "phases"):
        if key not in spec:
            raise ValueError(f"시나리오에 '{key}'가 없다: {path}")
    spec.setdefault("label_start_week", max(ph["start_week"] for ph in spec["phases"]))
    return spec


def _print(plan: Plan) -> None:
    for ph in plan.phases:
        print(f"\n[phase from week {ph['start_week']}] 그룹핑 {ph['grouping']}  총 투입 {ph['total_wspw']:,} WSPW")
        print("  그룹 regular WSPW:", ph["group_wspw_regular"])
        print("  정적 병목 상위 5 (다운타임 후 가동률 %):", ph["static_top5"])
        for msg in ph["problems"]:
            print("  !!", msg)
    for d in plan.downtime:
        print(f"\n[downtime {d['event']}: {d['type']} from week {d['start_week']}] {d['worst_toolgroup']} 정적 가동률 "
              f"{d['static_util_before_pct']}% -> {d['static_util_during_pct']}%  (정지 중 상위 3: {d['top3_during']})")
        if "warning" in d:
            print("  !!", d["warning"])
    for msg in plan.overrides:
        print("\n[덮어쓰기]", msg)
    if plan.branch:
        print(f"\n[분기] {plan.branch['week']:g}주 1분 전에 난수 seed {plan.branch['reseed']} 로 갈라짐 "
              f"(갈래 {plan.branch['seed']}, 그 전까지는 seed 의 기준 실행과 같은 경로)")
    for h in plan.hot:
        print(f"\n[hot 전환 {h['rule']}: {h['start_week']:g}~{h['end_week']:g}주] 대상 {len(h['products'])}개 제품, "
              f"regular 의 {100 * h['share']:g}% → hot (주당 약 {h['converted_lots_per_week']} lot)")
    for msg in plan.errors:
        print("  !! 형식 오류:", msg)
    print("\n판정:", "형식 오류" if plan.errors else "실행 가능" if plan.feasible
          else "제한 위반 — 그대로 돌리면 정상상태가 없거나 배치 대기가 폭증할 수 있다")


def main(argv=None) -> int:
    from smt2020_sim.data_parser import parse_workbook
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("spec")
    q = sub.add_parser("quick"); q.add_argument("items", nargs="+"); q.add_argument("--wspw", action="store_true")
    q.add_argument("--grouping", default=None)
    args = ap.parse_args(argv)
    data = parse_workbook(release_mode="parametric", initial_wip=False)
    if args.cmd == "check":
        spec = load(args.spec)
    else:
        vals = {k: float(v) for k, v in (it.split("=") for it in args.items)}
        spec = {"name": "quick", "seed": 0, "end_week": 1, "grouping": args.grouping,
                "phases": [{"start_week": 0, ("group_wspw" if args.wspw else "group_mult"): vals}]}
    plan, _, _ = prepare(spec, data)
    _print(plan)
    return 0 if plan.feasible and not plan.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
