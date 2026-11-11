"""xlsx -> ModelData.

설계 원칙 (01_data_contract.md 3절):

* R1 read-only / values-only 로 연다.
* R2 헤더 범위가 전부 비어 있는 행은 건너뛴다. 시트가 선언한 행 수는 믿지 않는다.
  Toolgroups 시트는 100만 행 넘게 선언하고 실제 데이터는 106행이다.
* R3 route 컬럼은 **위치**로 접근한다. 28개 헤더를 먼저 대조한다.
  index 8과 18이 둘 다 이름이 OFFSET이라 이름 기반 접근은 조용히 하나를 잃는다.
* R4 단위 변환은 여기서만 한다. wafer 수는 변환하지 않는다.
* R5 R UNIT만 소문자로 정규화한다. toolgroup 이름은 건드리지 않는다
  (DefMEt_FE_118 은 대문자 E가 맞다).
* R6 행 순서가 공정 순서. 상호참조는 파싱 시점에 행 index로 해소한다.
* R8 예상 밖의 값은 즉시 예외. 행을 건너뛰거나 기본값으로 대체하지 않는다.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import pickle
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Sequence

from openpyxl import load_workbook

from . import config
from .model_data import (
    BreakdownSpec,
    ExplicitLot,
    InitialWipLot,
    ModelData,
    PMStreamSpec,
    ReleaseStream,
    Route,
    SetupSpec,
    Step,
    ToolGroupSpec,
    TransportSpec,
)

# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------


class ParseError(ValueError):
    """구조적 문제. 데이터는 전수 검증되었으므로 이 예외는 파서 쪽 버그를 뜻한다."""

    def __init__(self, sheet: str, row: int | None, column: str | None, message: str):
        loc = f"[{sheet}"
        if row is not None:
            loc += f" row {row}"
        if column is not None:
            loc += f" col {column!r}"
        loc += "]"
        super().__init__(f"{loc} {message}")
        self.sheet = sheet
        self.row = row
        self.column = column


# ---------------------------------------------------------------------------
# route 컬럼 위치 (R3)
# ---------------------------------------------------------------------------

ROUTE_HEADER: tuple[str, ...] = (
    "ROUTE",                                    # 0
    "STEP",                                     # 1
    "STEP DESCRIPTION",                         # 2
    "AREA",                                     # 3
    "TOOLGROUP",                                # 4
    "PROCESSING UNIT",                          # 5
    "PROCESSINGTIME DISTRIBUTION",              # 6
    "MEAN",                                     # 7
    "OFFSET",                                   # 8  <- 처리시간 offset
    "PT UNITS",                                 # 9
    "CASCADING INTERVAL",                       # 10
    "C Units",                                  # 11
    "BATCH MINIMUM",                            # 12
    "BATCH MAXIMUM",                            # 13
    "SETUP",                                    # 14
    "WHEN",                                     # 15
    "SETUP DISTRIBUTION",                       # 16
    "SETUP TIME",                               # 17
    "OFFSET",                                   # 18 <- setup offset
    "ST UNITS",                                 # 19
    "STEP FOR LTL DEDICATION",                  # 20
    "REWORK PROBABILITY in %",                  # 21
    "R UNIT",                                   # 22
    "STEP FOR REWORK",                          # 23
    "PROCESSING PROBABILITY in % (Sampling)",   # 24
    "STEP FOR CRITICAL QUEUE TIME",             # 25
    "CQT",                                      # 26
    "CQTUNITS",                                 # 27
)

(C_ROUTE, C_STEP, C_DESC, C_AREA, C_TG, C_UNIT, C_PTDIST, C_MEAN, C_PTOFF,
 C_PTUNITS, C_CASC, C_CUNITS, C_BMIN, C_BMAX, C_SETUP, C_WHEN, C_SDIST,
 C_STIME, C_SOFF, C_STUNITS, C_LTL, C_RWPROB, C_RUNIT, C_RWSTEP, C_SAMP,
 C_CQTSTEP, C_CQT, C_CQTUNITS) = range(28)

PRODUCT_COUNT = 10


# ---------------------------------------------------------------------------
# 낮은 수준 도우미
# ---------------------------------------------------------------------------


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _text(v: Any) -> str | None:
    """빈 셀은 None, 그 외에는 앞뒤 공백을 턴 문자열."""
    if _blank(v):
        return None
    return str(v).strip()


def _to_minutes(value: Any, unit: Any, *, sheet: str, row: int, column: str) -> float:
    """시간 값을 분으로 변환한다 (R4).

    단위 문자열은 대소문자를 가리지 않는다. 모르는 단위는 예외 (R8).
    """
    if _blank(value):
        raise ParseError(sheet, row, column, "시간 값이 비어 있다")
    u = (_text(unit) or "").lower()
    factor = {
        "min": 1.0,
        "mins": 1.0,
        "minute": 1.0,
        "minutes": 1.0,
        "hr": config.MINUTES_PER_HOUR,
        "hrs": config.MINUTES_PER_HOUR,
        "hour": config.MINUTES_PER_HOUR,
        "hours": config.MINUTES_PER_HOUR,
        "day": config.MINUTES_PER_DAY,
        "days": config.MINUTES_PER_DAY,
    }.get(u)
    if factor is None:
        raise ParseError(sheet, row, column, f"알 수 없는 시간 단위: {unit!r}")
    return float(value) * factor


def _is_wafer_unit(unit: Any) -> bool:
    """MTBPM/FOA 단위가 wafer 수인지 판별한다 (변환 금지 대상)."""
    return (_text(unit) or "").lower() in {"wafer", "wafers", "piece", "pieces"}


def _sheet_rows(wb, name: str) -> tuple[list[str], list[list[Any]]]:
    """헤더와 '실제' 데이터 행을 돌려준다 (R2).

    헤더 뒤쪽의 빈 컬럼을 잘라내고, 그 범위가 전부 빈 행은 건너뛴다.
    """
    if name not in wb.sheetnames:
        raise ParseError(name, None, None, "시트가 없다")
    ws = wb[name]
    it: Iterator[tuple] = ws.iter_rows(values_only=True)
    try:
        raw_header = next(it)
    except StopIteration:
        raise ParseError(name, None, None, "시트가 비어 있다") from None

    header = [(_text(h) or "") for h in raw_header]
    while header and header[-1] == "":
        header.pop()
    ncol = len(header)
    if ncol == 0:
        raise ParseError(name, 1, None, "헤더 행이 비어 있다")

    rows: list[list[Any]] = []
    for r in it:
        row = list(r[:ncol]) + [None] * max(0, ncol - len(r))
        if all(_blank(v) for v in row):
            continue
        rows.append(row)
    return header, rows


def _col(header: Sequence[str], name: str, sheet: str) -> int:
    try:
        return list(header).index(name)
    except ValueError:
        raise ParseError(sheet, 1, name, f"헤더에 컬럼이 없다. 실제 헤더: {list(header)}") from None


def _require(value: Any, *, sheet: str, row: int, column: str) -> Any:
    if _blank(value):
        raise ParseError(sheet, row, column, "필수 값이 비어 있다")
    return value


# ---------------------------------------------------------------------------
# 시트별 파서
# ---------------------------------------------------------------------------


def _parse_toolgroups(wb) -> dict[str, ToolGroupSpec]:
    sheet = "Toolgroups"
    header, rows = _sheet_rows(wb, sheet)
    ix = {c: _col(header, c, sheet) for c in (
        "AREA", "TOOLGROUP", "NUMBER OF TOOLS", "CASCADINGTOOL", "BACTHINGTOOL",
        "BATCHCRITERION", "BATCHING UNIT", "LOADINGTIME", "LT UNITS",
        "UNLOADINGTIME", "ULT UNITS", "TOOLGROUPLOCATION", "DISPATCHING",
        "Ranking 1", "Ranking 2", "Ranking 3", "TOOL WAKE UP Ranking")}

    out: dict[str, ToolGroupSpec] = {}
    for n, r in enumerate(rows, start=2):
        name = _text(_require(r[ix["TOOLGROUP"]], sheet=sheet, row=n, column="TOOLGROUP"))
        if name in out:
            raise ParseError(sheet, n, "TOOLGROUP", f"중복된 toolgroup: {name}")
        yes = lambda v: (_text(v) or "").upper() == "YES"  # noqa: E731
        out[name] = ToolGroupSpec(
            name=name,
            area=_text(_require(r[ix["AREA"]], sheet=sheet, row=n, column="AREA")),
            num_tools=int(_require(r[ix["NUMBER OF TOOLS"]], sheet=sheet, row=n,
                                   column="NUMBER OF TOOLS")),
            cascading=yes(r[ix["CASCADINGTOOL"]]),
            batching=yes(r[ix["BACTHINGTOOL"]]),
            batch_criterion=_text(r[ix["BATCHCRITERION"]]),
            batching_unit=_text(r[ix["BATCHING UNIT"]]),
            load_time=_to_minutes(r[ix["LOADINGTIME"]], r[ix["LT UNITS"]],
                                  sheet=sheet, row=n, column="LOADINGTIME"),
            unload_time=_to_minutes(r[ix["UNLOADINGTIME"]], r[ix["ULT UNITS"]],
                                    sheet=sheet, row=n, column="UNLOADINGTIME"),
            location=_text(_require(r[ix["TOOLGROUPLOCATION"]], sheet=sheet, row=n,
                                    column="TOOLGROUPLOCATION")),
            dispatching=_text(_require(r[ix["DISPATCHING"]], sheet=sheet, row=n,
                                       column="DISPATCHING")),
            rank1=_text(r[ix["Ranking 1"]]) or "",
            rank2=_text(r[ix["Ranking 2"]]) or "",
            rank3=_text(r[ix["Ranking 3"]]) or "",
            wake_up_rank=_text(r[ix["TOOL WAKE UP Ranking"]]),
        )
    return out


def _parse_routes(wb, toolgroups: dict[str, ToolGroupSpec]) -> dict[str, Route]:
    routes: dict[str, Route] = {}
    for i in range(1, PRODUCT_COUNT + 1):
        sheet = f"Route_Product_{i}"
        product = f"Product_{i}"
        header, rows = _sheet_rows(wb, sheet)

        # R3: 위치 기반 접근의 전제. 헤더가 정확히 일치하는지 먼저 본다.
        if tuple(header) != ROUTE_HEADER:
            raise ParseError(sheet, 1, None,
                             "route 헤더가 기대와 다르다.\n"
                             f"  기대: {ROUTE_HEADER}\n  실제: {tuple(header)}")

        # 1차: step_id -> 행 index 지도를 만든다 (R6).
        step_ids: list[int] = []
        for n, r in enumerate(rows, start=2):
            sid = _require(r[C_STEP], sheet=sheet, row=n, column="STEP")
            step_ids.append(int(sid))
        id_to_index: dict[int, int] = {}
        for idx, sid in enumerate(step_ids):
            if sid in id_to_index:
                raise ParseError(sheet, idx + 2, "STEP", f"route 내 중복 step id: {sid}")
            id_to_index[sid] = idx

        def resolve(raw: Any, *, row: int, column: str) -> int | None:
            """step 식별자를 행 index로 해소한다. 빈 셀이면 None."""
            if _blank(raw):
                return None
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                raise ParseError(sheet, row, column,
                                 f"step 식별자가 정수가 아니다: {raw!r}") from None
            if sid not in id_to_index:
                raise ParseError(sheet, row, column,
                                 f"route 안에 없는 step을 가리킨다: {sid}")
            return id_to_index[sid]

        # 2차: Step 객체를 만든다.
        steps: list[Step] = []
        for idx, r in enumerate(rows):
            n = idx + 2
            tg_name = _text(_require(r[C_TG], sheet=sheet, row=n, column="TOOLGROUP"))
            if tg_name not in toolgroups:
                raise ParseError(sheet, n, "TOOLGROUP",
                                 f"Toolgroups 시트에 없는 toolgroup: {tg_name}")

            pt_offset = 0.0
            if not _blank(r[C_PTOFF]):
                pt_offset = _to_minutes(r[C_PTOFF], r[C_PTUNITS],
                                        sheet=sheet, row=n, column="OFFSET(pt)")

            setup_dist = _text(r[C_SDIST])
            setup_time = None
            setup_offset = None
            if not _blank(r[C_STIME]):
                setup_time = _to_minutes(r[C_STIME], r[C_STUNITS],
                                         sheet=sheet, row=n, column="SETUP TIME")
            if not _blank(r[C_SOFF]):
                setup_offset = _to_minutes(r[C_SOFF], r[C_STUNITS],
                                           sheet=sheet, row=n, column="OFFSET(setup)")

            steps.append(Step(
                step_index=idx,
                step_id=step_ids[idx],
                description=_text(r[C_DESC]) or "",
                area=_text(_require(r[C_AREA], sheet=sheet, row=n, column="AREA")),
                toolgroup=tg_name,
                processing_unit=_text(_require(r[C_UNIT], sheet=sheet, row=n,
                                               column="PROCESSING UNIT")),
                pt_dist=_text(_require(r[C_PTDIST], sheet=sheet, row=n,
                                       column="PROCESSINGTIME DISTRIBUTION")),
                pt_mean=_to_minutes(r[C_MEAN], r[C_PTUNITS],
                                    sheet=sheet, row=n, column="MEAN"),
                pt_offset=pt_offset,
                cascade_interval=(None if _blank(r[C_CASC]) else
                                  _to_minutes(r[C_CASC], r[C_CUNITS], sheet=sheet,
                                              row=n, column="CASCADING INTERVAL")),
                batch_min=None if _blank(r[C_BMIN]) else int(r[C_BMIN]),
                batch_max=None if _blank(r[C_BMAX]) else int(r[C_BMAX]),
                setup_name=_text(r[C_SETUP]),
                setup_when=_text(r[C_WHEN]),
                setup_dist=setup_dist,
                setup_time=setup_time,
                setup_offset=setup_offset,
                ltl_target_index=resolve(r[C_LTL], row=n,
                                         column="STEP FOR LTL DEDICATION"),
                rework_prob=None if _blank(r[C_RWPROB]) else float(r[C_RWPROB]),
                # R5: 이 컬럼만 소문자로 정규화한다 ('lot' 45건 / 'Lot' 7건).
                rework_unit=(None if _blank(r[C_RUNIT])
                             else str(r[C_RUNIT]).strip().lower()),
                rework_target_index=resolve(r[C_RWSTEP], row=n,
                                            column="STEP FOR REWORK"),
                sampling_prob=None if _blank(r[C_SAMP]) else float(r[C_SAMP]),
                cqt_target_index=resolve(r[C_CQTSTEP], row=n,
                                         column="STEP FOR CRITICAL QUEUE TIME"),
                cqt_threshold=(None if _blank(r[C_CQT]) else
                               _to_minutes(r[C_CQT], r[C_CQTUNITS], sheet=sheet,
                                           row=n, column="CQT")),
            ))

        routes[product] = Route(
            product=product,
            steps=tuple(steps),
            step_id_to_index=MappingProxyType(id_to_index),
            suffix_raw_pt=_suffix_raw_pt(steps, toolgroups),
        )
    return routes


def build_ltl_chains(routes) -> dict[str, dict[int, tuple[int, int]]]:
    """lot-to-lens 체인을 만든다 (08_ltl.md 2절).

    명세: "exactly the same tool must be used for process step i and process
    step j (i < j)" — step i의 FORSTEP에 j를 적고 SVESTN=yes. 그리고
    "can be extended for process step k etc. (i < j < k)"로 **사슬**이 된다.

    반환: 제품 -> step_index -> (체인 id, 체인 내 위치).
    체인 id는 **머리 step의 index**다. 제품별로 유일하다.

    **체인 id를 toolgroup으로 잡으면 안 된다** (2절). 한 제품이 같은
    toolgroup에 별개 체인을 여럿 가질 수 있고, toolgroup으로 묶으면 그것들이
    하나로 합쳐져 route 나머지 구간까지 조용히 과잉 제약된다.

    머리의 정의: 목표를 갖고 있으면서 **자신은 누구의 목표도 아닌** step.
    """
    out: dict[str, dict[int, tuple[int, int]]] = {}
    for product, route in routes.items():
        steps = route.steps
        targets = {s.ltl_target_index for s in steps
                   if s.ltl_target_index is not None}
        index: dict[int, tuple[int, int]] = {}
        for step in steps:
            if step.ltl_target_index is None or step.step_index in targets:
                continue                       # 목표가 없거나 머리가 아니다
            head = step.step_index
            pos = 0
            cur: int | None = head
            seen: set[int] = set()
            while cur is not None:
                if cur in seen:                # 순환이면 데이터가 깨진 것
                    raise ValueError(
                        f"{product}: LTL 체인이 순환한다 (head {head}, step {cur})")
                seen.add(cur)
                index[cur] = (head, pos)
                pos += 1
                cur = steps[cur].ltl_target_index
        if index:
            out[product] = index
    return out


def _suffix_raw_pt(steps: Sequence[Step],
                   toolgroups: dict[str, ToolGroupSpec]) -> tuple[float, ...]:
    """index i부터 route 끝까지의 raw 처리시간 합 (분).

    07_dispatching.md R5 정의:
      wafer 단위는 mean x wafer 수, lot/batch 단위는 mean.
      delay가 아닌 step에는 load + unload를 더한다. delay step은 mean만.

    표본이 아니라 평균을 쓴다. route 위치가 같으면 항상 같은 값이어야 한다.
    """
    n_wafers = config.DEFAULT_WAFERS_PER_LOT
    suffix = [0.0] * (len(steps) + 1)
    for i in range(len(steps) - 1, -1, -1):
        step = steps[i]
        spec = toolgroups[step.toolgroup]
        if spec.is_delay:
            own = step.pt_mean
        else:
            unit = step.processing_unit.lower()
            own = step.pt_mean * n_wafers if unit == "wafer" else step.pt_mean
            own += spec.load_time + spec.unload_time
        suffix[i] = own + suffix[i + 1]
    return tuple(suffix[:-1])


def _rpt_paper(steps: Sequence[Step], toolgroups: dict[str, ToolGroupSpec],
               transport_min: float) -> float:
    """논문 규약의 RPT — **빈 fab을 지나는 lot 하나의 기대 CT** (분).

    논문(Kopp 2020) III-E-1절이 계산이 아니라 측정으로 정의한다:

      "we first run a model with lots starting at very long intervals ...
       these lots progress in an **empty fab** and their waiting time is
       close to zero ... **no minimum batch size is required** ... The
       products' **minimum CT is then measured** to obtain the RPT. The
       source of variability ... delays due to **PMs** or that they are
       **sampled in metrology steps**."

    그래서 `_suffix_raw_pt`와 셋이 다르다:

      1. **이송을 포함한다** — 혼자 가는 lot도 장비 사이를 이동한다
      2. **표본검사를 반영한다** — 건너뛴 step은 시간을 쓰지 않는다
      3. **cascading 식을 쓴다** — 점유가 `pt + (n-1)·interval`이다

    10제품 전부에서 논문 Table II와 **+0.8%**(최대 1.4%)로 맞는다. 남은 양의
    편차는 논문이 **최솟값**을 쟀기 때문으로 읽는다 — 기댓값보다 조금 아래다.

    **이 값은 모델이 읽지 않는다.** 디스패칭 분모는 `suffix_raw_pt`이고,
    이쪽을 거기 쓰면 critical ratio가 달라진다 (07 R5).

    `transport_min`은 Transport 시트에서 온다 — 소스에 7.5를 적지 않는다.
    """
    n_wafers = config.DEFAULT_WAFERS_PER_LOT
    total = 0.0
    for step in steps:
        spec = toolgroups[step.toolgroup]
        p = (1.0 if step.sampling_prob is None
             else step.sampling_prob / 100.0)       # 시트는 퍼센트다
        if spec.is_delay:
            total += p * step.pt_mean               # 장비도 이송도 없다
            continue
        unit = step.processing_unit.lower()
        if unit == "wafer":
            own = (step.pt_mean + (n_wafers - 1) * step.cascade_interval
                   if step.cascade_interval else step.pt_mean * n_wafers)
        else:
            own = step.pt_mean
        own += spec.load_time + spec.unload_time + transport_min
        total += p * own
    return total


def _fill_rpt_paper(routes: dict[str, Route],
                    toolgroups: dict[str, ToolGroupSpec],
                    transport: dict) -> dict[str, Route]:
    """route를 파싱한 **뒤에** 논문 규약 RPT를 채운다.

    이송 시간이 별도 시트라 route 파서 안에서는 알 수 없다. `Route`가
    frozen이므로 `replace`로 새로 만든다 — 파싱 시점에 한 번뿐이다.
    """
    fab = transport.get(("Fab", "Fab"))
    mean = fab.mean if fab is not None else 0.0
    return {p: dataclasses.replace(
        r, rpt_paper=_rpt_paper(r.steps, toolgroups, mean))
        for p, r in routes.items()}


def _parse_breakdowns(wb) -> dict[str, BreakdownSpec]:
    sheet = "Breakdown"
    header, rows = _sheet_rows(wb, sheet)
    ix = {c: _col(header, c, sheet) for c in (
        "DOWN EVENT NAME", "DOWN EVENT VALID FOR TYPE", "TYPE NAME", "DOWN TYPE",
        "TTF DISTRIBUTION", "MTTF", "MTTF UNITS", "TTR DISTRIBUTION", "MTTR",
        "MTTR UNITS", "FIRST ONE AT DISTRIBUTION", "FOA", "FOA UNITS")}

    out: dict[str, BreakdownSpec] = {}
    for n, r in enumerate(rows, start=2):
        scope = (_text(r[ix["DOWN EVENT VALID FOR TYPE"]]) or "").lower()
        if scope != "area":
            raise ParseError(sheet, n, "DOWN EVENT VALID FOR TYPE",
                             f"area 단위만 지원한다. 실제: {scope!r}")
        area = _text(_require(r[ix["TYPE NAME"]], sheet=sheet, row=n, column="TYPE NAME"))
        if area in out:
            raise ParseError(sheet, n, "TYPE NAME", f"중복된 area: {area}")
        out[area] = BreakdownSpec(
            area=area,
            event_name=_text(r[ix["DOWN EVENT NAME"]]) or "",
            down_type=_text(r[ix["DOWN TYPE"]]) or "",
            ttf_dist=_text(r[ix["TTF DISTRIBUTION"]]) or "",
            mttf=_to_minutes(r[ix["MTTF"]], r[ix["MTTF UNITS"]],
                             sheet=sheet, row=n, column="MTTF"),
            ttr_dist=_text(r[ix["TTR DISTRIBUTION"]]) or "",
            mttr=_to_minutes(r[ix["MTTR"]], r[ix["MTTR UNITS"]],
                             sheet=sheet, row=n, column="MTTR"),
            foa_dist=_text(r[ix["FIRST ONE AT DISTRIBUTION"]]) or "",
            foa=_to_minutes(r[ix["FOA"]], r[ix["FOA UNITS"]],
                            sheet=sheet, row=n, column="FOA"),
        )
    return out


def _parse_pm(wb) -> dict[str, tuple[PMStreamSpec, ...]]:
    sheet = "PM"
    header, rows = _sheet_rows(wb, sheet)
    ix = {c: _col(header, c, sheet) for c in (
        "PM EVENT NAME", "PM EVENT VALID FOR TYPE", "TYPE NAME", "PM TYPE",
        "MTBeforePM", "MTBPM UNITS", "TTR DISTRIBUTION", "MEAN", "OFFSET",
        "TTR UNITS", "FIRST ONE AT DISTRIBUTION", "FOA", "FOA UNITS")}

    grouped: dict[str, list[PMStreamSpec]] = defaultdict(list)
    for n, r in enumerate(rows, start=2):
        scope = (_text(r[ix["PM EVENT VALID FOR TYPE"]]) or "").lower()
        if scope != "toolgroup":
            raise ParseError(sheet, n, "PM EVENT VALID FOR TYPE",
                             f"toolgroup 단위만 지원한다. 실제: {scope!r}")
        pm_type = (_text(r[ix["PM TYPE"]]) or "").lower()
        if "counter" in pm_type:
            counter_based = True
        elif "time" in pm_type:
            counter_based = False
        else:
            raise ParseError(sheet, n, "PM TYPE", f"알 수 없는 PM 유형: {pm_type!r}")

        # counter 기반이면 MTBPM/FOA는 wafer 수다. 변환하지 않는다 (R4).
        mtbpm_unit = r[ix["MTBPM UNITS"]]
        foa_unit = r[ix["FOA UNITS"]]
        if counter_based != _is_wafer_unit(mtbpm_unit):
            raise ParseError(sheet, n, "MTBPM UNITS",
                             f"PM 유형({pm_type!r})과 단위({mtbpm_unit!r})가 어긋난다")

        if counter_based:
            mtbpm = float(_require(r[ix["MTBeforePM"]], sheet=sheet, row=n,
                                   column="MTBeforePM"))
            foa = float(_require(r[ix["FOA"]], sheet=sheet, row=n, column="FOA"))
        else:
            mtbpm = _to_minutes(r[ix["MTBeforePM"]], mtbpm_unit,
                                sheet=sheet, row=n, column="MTBeforePM")
            foa = _to_minutes(r[ix["FOA"]], foa_unit, sheet=sheet, row=n, column="FOA")

        tg = _text(_require(r[ix["TYPE NAME"]], sheet=sheet, row=n, column="TYPE NAME"))
        grouped[tg].append(PMStreamSpec(
            event_name=_text(r[ix["PM EVENT NAME"]]) or "",
            toolgroup=tg,
            counter_based=counter_based,
            mtbpm=mtbpm,
            ttr_dist=_text(r[ix["TTR DISTRIBUTION"]]) or "",
            ttr_mean=_to_minutes(r[ix["MEAN"]], r[ix["TTR UNITS"]],
                                 sheet=sheet, row=n, column="MEAN"),
            ttr_offset=(0.0 if _blank(r[ix["OFFSET"]]) else
                        _to_minutes(r[ix["OFFSET"]], r[ix["TTR UNITS"]],
                                    sheet=sheet, row=n, column="OFFSET")),
            foa_dist=_text(r[ix["FIRST ONE AT DISTRIBUTION"]]) or "",
            foa=foa,
        ))
    return {k: tuple(v) for k, v in grouped.items()}


def _parse_setups(wb) -> tuple[SetupSpec, ...]:
    sheet = "Setups"
    header, rows = _sheet_rows(wb, sheet)
    # 컬럼명이 원본에서 잘못 적혀 있다 (MINMAL). 시트 표기를 그대로 쓴다.
    ix = {c: _col(header, c, sheet) for c in (
        "SETUP GROUP NAME", "CURRENT SETUP", "NEW SETUP", "SETUP TIME",
        "ST UNITS", "MINMAL NUMBER OF RUNS")}

    out: list[SetupSpec] = []
    for n, r in enumerate(rows, start=2):
        out.append(SetupSpec(
            group=_text(r[ix["SETUP GROUP NAME"]]),
            current_setup=_text(r[ix["CURRENT SETUP"]]),
            new_setup=_text(_require(r[ix["NEW SETUP"]], sheet=sheet, row=n,
                                     column="NEW SETUP")),
            setup_time=_to_minutes(r[ix["SETUP TIME"]], r[ix["ST UNITS"]],
                                   sheet=sheet, row=n, column="SETUP TIME"),
            min_run=(None if _blank(r[ix["MINMAL NUMBER OF RUNS"]])
                     else int(r[ix["MINMAL NUMBER OF RUNS"]])),
        ))
    return tuple(out)


def _parse_transport(wb) -> dict[tuple[str, str], TransportSpec]:
    sheet = "Transport"
    header, rows = _sheet_rows(wb, sheet)
    ix = {c: _col(header, c, sheet) for c in (
        "FROM LOCATION", "TO LOCATION", "TRANSPORTTIME DISTRIBUTION",
        "MEAN", "OFFSET", "TT UNITS")}

    out: dict[tuple[str, str], TransportSpec] = {}
    for n, r in enumerate(rows, start=2):
        frm = _text(_require(r[ix["FROM LOCATION"]], sheet=sheet, row=n,
                             column="FROM LOCATION"))
        to = _text(_require(r[ix["TO LOCATION"]], sheet=sheet, row=n,
                            column="TO LOCATION"))
        out[(frm, to)] = TransportSpec(
            from_location=frm,
            to_location=to,
            dist=_text(r[ix["TRANSPORTTIME DISTRIBUTION"]]) or "",
            mean=_to_minutes(r[ix["MEAN"]], r[ix["TT UNITS"]],
                             sheet=sheet, row=n, column="MEAN"),
            offset=(0.0 if _blank(r[ix["OFFSET"]]) else
                    _to_minutes(r[ix["OFFSET"]], r[ix["TT UNITS"]],
                                sheet=sheet, row=n, column="OFFSET")),
        )
    return out


def _parse_release(wb) -> tuple[ReleaseStream, ...]:
    sheet = "Lotrelease"
    header, rows = _sheet_rows(wb, sheet)
    ix = {c: _col(header, c, sheet) for c in (
        "PRODUCT NAME", "ROUTE NAME", "LOT NAME/TYPE", "PRIORITY", "SUPERHOTLOT",
        "WAFERS PER LOT", "START DATE", "RELEASE DISTRIBUTION", "RELEASE INTERVAL",
        "R UNITS", "LOTS PER RELEASE", "DUE DATE")}

    out: list[ReleaseStream] = []
    for n, r in enumerate(rows, start=2):
        start = _require(r[ix["START DATE"]], sheet=sheet, row=n, column="START DATE")
        due = _require(r[ix["DUE DATE"]], sheet=sheet, row=n, column="DUE DATE")
        try:
            offset_min = (due - start).total_seconds() / 60.0
        except (AttributeError, TypeError):
            raise ParseError(sheet, n, "DUE DATE",
                             f"날짜로 해석되지 않는다: start={start!r} due={due!r}") from None
        if offset_min <= 0:
            raise ParseError(sheet, n, "DUE DATE", f"due date가 start보다 앞이다: {offset_min}")

        out.append(ReleaseStream(
            product=_text(_require(r[ix["PRODUCT NAME"]], sheet=sheet, row=n,
                                   column="PRODUCT NAME")),
            route_name=_text(r[ix["ROUTE NAME"]]) or "",
            lot_type=_text(_require(r[ix["LOT NAME/TYPE"]], sheet=sheet, row=n,
                                    column="LOT NAME/TYPE")),
            priority=int(_require(r[ix["PRIORITY"]], sheet=sheet, row=n,
                                  column="PRIORITY")),
            is_superhot=(_text(r[ix["SUPERHOTLOT"]]) or "").lower() == "yes",
            wafers_per_lot=int(_require(r[ix["WAFERS PER LOT"]], sheet=sheet, row=n,
                                        column="WAFERS PER LOT")),
            release_dist=_text(r[ix["RELEASE DISTRIBUTION"]]) or "",
            release_interval=_to_minutes(r[ix["RELEASE INTERVAL"]], r[ix["R UNITS"]],
                                         sheet=sheet, row=n, column="RELEASE INTERVAL"),
            lots_per_release=int(_require(r[ix["LOTS PER RELEASE"]], sheet=sheet,
                                          row=n, column="LOTS PER RELEASE")),
            due_date_offset=offset_min,
        ))
    return tuple(out)


# ---------------------------------------------------------------------------
# 유도 매핑 (R7)
# ---------------------------------------------------------------------------


def _derive_area_map(toolgroups: dict[str, ToolGroupSpec]) -> dict[str, tuple[str, ...]]:
    by_area: dict[str, list[str]] = defaultdict(list)
    for name, spec in toolgroups.items():
        by_area[spec.area].append(name)
    return {a: tuple(sorted(v)) for a, v in by_area.items()}


def _derive_setup_map(routes: dict[str, Route], *, strict: bool) -> dict[str, str]:
    """setup 이름 -> toolgroup.

    xlsx의 Setups 시트에는 이 대응이 없다 (DATA_FACTS.md 6.1절).
    route에서 해당 setup을 쓰는 step들의 toolgroup을 모아서 유도한다.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for route in routes.values():
        for step in route.steps:
            if step.setup_name is not None:
                seen[step.setup_name].add(step.toolgroup)

    out: dict[str, str] = {}
    for setup_name, tgs in seen.items():
        if len(tgs) != 1:
            msg = f"setup {setup_name!r}이 여러 toolgroup에서 쓰인다: {sorted(tgs)}"
            if strict:
                raise ParseError("Route_Product_*", None, "SETUP", msg)
            out[setup_name] = sorted(tgs)[0]
            continue
        out[setup_name] = next(iter(tgs))
    return out


# ---------------------------------------------------------------------------
# Phase 12 — explicit 투입과 초기 WIP
# ---------------------------------------------------------------------------

EXPLICIT_SHEET = "Lotrelease - variable due dates"

# 시트의 마지막 컬럼. 첫 몇 행에만 '1000 WSPW per Product', 'high service
# level' 같은 **범례 문자열**이 들어 있고 나머지 16만 행은 비어 있다.
# lot별 데이터가 아니라 사람이 읽는 주석이다. 읽으면 "시나리오별로 나눠야
# 하나"로 오독하게 되므로 이름을 박아 두고 무시한다.
EXPLICIT_LEGEND_COLUMN = "Release Scenario"


def _minutes_from_sim_start(value: Any, *, sheet: str, row: int,
                            column: str) -> float:
    """날짜를 t=0(`config.SIM_START`)부터의 분으로 바꾼다.

    **음수가 나와도 통과시킨다.** 초기 WIP의 due date는 t=0보다 앞일 수 있고
    (데이터의 첫 행이 정확히 0이다), 그것이 이미 납기가 지난 채 시작하는
    lot이 있다는 사실이다. 여기서 막으면 데이터를 고치게 된다.
    """
    v = _require(value, sheet=sheet, row=row, column=column)
    if isinstance(v, str):
        # WIP.txt 쪽 경로. 'MM/DD/YY HH:MM:SS'.
        try:
            v = datetime.strptime(v.strip(), "%m/%d/%y %H:%M:%S")
        except ValueError:
            raise ParseError(sheet, row, column,
                             f"날짜로 해석되지 않는다: {v!r}") from None
    if not isinstance(v, datetime):
        raise ParseError(sheet, row, column, f"날짜가 아니다: {v!r}")
    return (v - config.SIM_START).total_seconds() / 60.0


def _parse_explicit_release(wb) -> tuple[ExplicitLot, ...]:
    """'Lotrelease - variable due dates' 시트 -> lot별 투입 명세 (R1).

    `_parse_release`와 달리 **반복 컬럼이 없다.** 실제 헤더에
    `RELEASE INTERVAL`도 `LOTS PER RELEASE`도 없는데, 이것이 AutoSched 문서의
    "lots can be released individually, i.e. without the repeat functionality"가
    스키마로 드러난 모습이다. 그래서 due date를 offset이 아니라 **절대 시각**
    으로 읽는다 (R7) — 문서는 offset 방식을 "for each lot released during a
    **repeat cycle**"로 한정한다.

    투입 시각 순으로 정렬해 돌려준다. 투입 프로세스가 index 하나로 훑을 수
    있어야 `Lot` 16만 개를 t=0에 만들지 않는다 (R3).
    """
    header, rows = _sheet_rows(wb, EXPLICIT_SHEET)
    ix = {c: _col(header, c, EXPLICIT_SHEET) for c in (
        "PRODUCT NAME", "LOT NAME/TYPE", "PRIORITY", "SUPERHOTLOT",
        "WAFERS PER LOT", "START DATE", "DUE DATE")}

    out: list[ExplicitLot] = []
    for n, r in enumerate(rows, start=2):
        product = _text(_require(r[ix["PRODUCT NAME"]], sheet=EXPLICIT_SHEET,
                                 row=n, column="PRODUCT NAME"))
        start = _minutes_from_sim_start(r[ix["START DATE"]],
                                        sheet=EXPLICIT_SHEET, row=n,
                                        column="START DATE")
        due = _minutes_from_sim_start(r[ix["DUE DATE"]], sheet=EXPLICIT_SHEET,
                                      row=n, column="DUE DATE")
        if due <= start:
            raise ParseError(EXPLICIT_SHEET, n, "DUE DATE",
                             f"due date가 start보다 앞이다: {due} <= {start}")
        out.append(ExplicitLot(
            lot_id=_text(_require(r[ix["LOT NAME/TYPE"]], sheet=EXPLICIT_SHEET,
                                  row=n, column="LOT NAME/TYPE")),
            product=product,
            priority=int(_require(r[ix["PRIORITY"]], sheet=EXPLICIT_SHEET,
                                  row=n, column="PRIORITY")),
            is_superhot=(_text(r[ix["SUPERHOTLOT"]]) or "").lower() == "yes",
            wafers_per_lot=int(_require(r[ix["WAFERS PER LOT"]],
                                        sheet=EXPLICIT_SHEET, row=n,
                                        column="WAFERS PER LOT")),
            release_time=start,
            due_date=due,
        ))

    # **안정 정렬이다.** 같은 시각의 lot 순서가 시트 순서 그대로 유지되어야
    # 같은 seed에서 같은 결과가 나온다 (R2와 같은 이유의 결정성).
    out.sort(key=lambda e: e.release_time)
    return tuple(out)


def _parse_initial_wip(routes: dict[str, Route], path: Path | None = None,
                       ) -> tuple[InitialWipLot, ...]:
    """`WIP.txt` -> t=0에 fab 안에 있는 lot (R5).

    파일 형식은 UTF-16 탭 구분이고 헤더는
    `LOT PART PRIOR PIECES START CURSTEP DUE ORDER HOTLOT TRACE`다.

    **`reference/`를 읽는 유일한 모델 경로다.** `config.asd_input_path`를
    통과시켜 `.rep`(AutoSched 결과)로는 절대 못 가게 막는다. WIP.txt 자체는
    문서가 ORDER_FILE로 분류한 **입력** 파일이다.

    `CURSTEP`이 route에서 풀리는지 여기서 확인한다. 안 풀리면 예외다 — 조용히
    0번 step에 놓으면 그 lot이 route 전체를 다시 돌아 WIP이 무너진다
    (프롬프트 5절 "WIP starts right and then collapses").
    """
    p = path or config.asd_input_path("WIP.txt")
    sheet = p.name
    if not p.exists():
        raise FileNotFoundError(f"초기 WIP 파일이 없다: {p}")

    text = p.read_bytes().decode("utf-16")
    lines = text.splitlines()
    if not lines:
        raise ParseError(sheet, None, None, "파일이 비어 있다")
    header = lines[0].split("\t")
    ix = {c: _col(header, c, sheet) for c in (
        "LOT", "PART", "PRIOR", "PIECES", "CURSTEP", "DUE", "ORDER", "HOTLOT")}

    out: list[InitialWipLot] = []
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        f = line.split("\t")
        # part_1 -> Product_1. 파일과 시트의 명명 규칙이 다르다 (2.2절).
        part = _text(_require(f[ix["PART"]], sheet=sheet, row=n, column="PART"))
        product = f"Product_{part.rsplit('_', 1)[-1]}"
        if product not in routes:
            raise ParseError(sheet, n, "PART",
                             f"route가 없는 제품이다: {part!r} -> {product!r}")
        step_id = int(_require(f[ix["CURSTEP"]], sheet=sheet, row=n,
                               column="CURSTEP"))
        if step_id not in routes[product].step_id_to_index:
            raise ParseError(sheet, n, "CURSTEP",
                             f"{product}의 route에 없는 step 식별자다: {step_id}")
        out.append(InitialWipLot(
            lot_id=_text(_require(f[ix["LOT"]], sheet=sheet, row=n,
                                  column="LOT")),
            product=product,
            priority=int(_require(f[ix["PRIOR"]], sheet=sheet, row=n,
                                  column="PRIOR")),
            is_superhot=(_text(f[ix["HOTLOT"]]) or "").lower() == "superhot",
            wafers_per_lot=int(_require(f[ix["PIECES"]], sheet=sheet, row=n,
                                        column="PIECES")),
            due_date=_minutes_from_sim_start(f[ix["DUE"]], sheet=sheet, row=n,
                                             column="DUE"),
            current_step_id=step_id,
            order=_text(f[ix["ORDER"]]) or "",
        ))
    return tuple(out)


# -- explicit 일정 캐시 (R4) --------------------------------------------------
#
# 시트 파싱이 9초다. cold 기준(60초)은 이미 통과하지만 warm 기준(5초)은
# 캐시가 있어야 한다. 키는 **워크북의 내용 해시**라, 워크북이 바뀌면 캐시가
# 자동으로 무효가 된다 (R4가 요구하는 성질).

_CACHE_VERSION = 1


def _workbook_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _explicit_cache_path(path: Path) -> Path:
    return (config.CACHE_DIR /
            f"explicit_release_v{_CACHE_VERSION}_{_workbook_digest(path)}.pkl")


def _load_explicit_cached(path: Path, wb) -> tuple[ExplicitLot, ...]:
    """캐시가 있으면 읽고, 없으면 파싱해서 쓴다.

    캐시 읽기가 실패하면 **조용히 다시 파싱한다** — 캐시는 속도 장치이지
    정확성의 원천이 아니다. 다만 쓰기 실패는 삼키지 않는다: 매번 9초를
    내면서 이유를 모르는 것이 더 나쁘다.
    """
    cache = _explicit_cache_path(path)
    if cache.exists():
        try:
            with cache.open("rb") as fh:
                return pickle.load(fh)
        except Exception:
            cache.unlink(missing_ok=True)

    lots = _parse_explicit_release(wb)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(lots, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(cache)          # 원자적 교체. 중단돼도 반쪽 캐시가 안 남는다
    return lots


# ---------------------------------------------------------------------------
# 공개 진입점
# ---------------------------------------------------------------------------


_PARSE_MEMO: dict[tuple, ModelData] = {}


def parse_workbook(path: str | Path | None = None, *, strict: bool = True,
                   use_memo: bool = True,
                   release_mode: str | None = None,
                   initial_wip: bool | None = None) -> ModelData:
    """xlsx를 읽어 ModelData를 만든다.

    strict=False 는 감사 스크립트 전용이다. 유도 단계의 경고를 예외 대신
    통과시킨다. 시트 구조 오류는 strict와 무관하게 예외다 (R8).

    `release_mode="explicit"`이면 lot별 투입 시트(16만 행)와 초기 WIP까지
    읽는다 (Phase 12 R1). **기본은 parametric이고 그때는 두 원본을 아예 열지
    않는다** — 그러면 `explicit_lots`/`initial_wip`이 비고, `FabModel.start()`가
    Phase 11과 같은 경로를 탄다 (R2).

    같은 프로세스 안에서 같은 파일을 다시 읽으면 캐시된 결과를 돌려준다.
    파싱에 7초 넘게 걸리는데 한 번의 점검에서 여러 번 호출되기 때문이다
    (verify / 시뮬레이션 / 결정론 확인). 결과는 불변이므로 공유해도 안전하다.
    파일이 바뀌면(크기나 수정시각) 캐시가 자동으로 무효가 된다.
    """
    path = Path(path) if path is not None else config.XLSX_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"입력 파일이 없다: {path}\n"
            "prompt_phase/AGENTS.md 1절의 복사 명령을 먼저 실행할 것.")

    mode = config.RELEASE_MODE if release_mode is None else release_mode
    if mode not in ("parametric", "explicit"):
        raise ValueError(f"release_mode는 'parametric' 또는 'explicit'이다: {mode!r}")
    want_wip = config.INITIAL_WIP if initial_wip is None else initial_wip
    want_wip = want_wip and mode == "explicit"

    st = path.stat()
    memo_key = (str(path.resolve()), st.st_size, st.st_mtime_ns, strict,
                mode, want_wip)
    if use_memo and memo_key in _PARSE_MEMO:
        return _PARSE_MEMO[memo_key]

    explicit_lots: tuple[ExplicitLot, ...] = ()
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        toolgroups = _parse_toolgroups(wb)
        routes = _parse_routes(wb, toolgroups)
        breakdowns = _parse_breakdowns(wb)
        pm_streams = _parse_pm(wb)
        setups = _parse_setups(wb)
        release_streams = _parse_release(wb)
        transport = _parse_transport(wb)
        # 이송 시간이 route와 다른 시트에 있어 route 파서 안에서는 알 수 없다.
        # 논문 규약 RPT는 이송을 포함하므로 여기서 채운다 (보고 전용).
        routes = _fill_rpt_paper(routes, toolgroups, transport)
        if mode == "explicit":
            explicit_lots = _load_explicit_cached(path, wb)
    finally:
        wb.close()

    # **워크북을 닫은 뒤에 읽는다.** WIP.txt는 xlsx가 아니고, route가 이미
    # 만들어져 있어야 CURSTEP을 풀 수 있다.
    initial = _parse_initial_wip(routes) if want_wip else ()
    if mode == "explicit":
        missing = {e.product for e in explicit_lots} - set(routes)
        if missing:
            raise ParseError(EXPLICIT_SHEET, None, "PRODUCT NAME",
                             f"route가 없는 제품이다: {sorted(missing)}")

    # 모든 release stream의 wafer 수가 같아야 suffix_raw_pt 가 route 단위로 성립한다.
    wafer_counts = {s.wafers_per_lot for s in release_streams}
    if wafer_counts != {config.DEFAULT_WAFERS_PER_LOT}:
        msg = (f"release stream의 WAFERS PER LOT이 균일하지 않다: {sorted(wafer_counts)}. "
               "suffix_raw_pt 전제가 깨진다.")
        if strict:
            raise ParseError("Lotrelease", None, "WAFERS PER LOT", msg)

    data = ModelData(
        toolgroups=MappingProxyType(dict(toolgroups)),
        routes=MappingProxyType(dict(routes)),
        breakdowns=MappingProxyType(dict(breakdowns)),
        pm_streams=MappingProxyType(dict(pm_streams)),
        setups=setups,
        release_streams=release_streams,
        transport=MappingProxyType(dict(transport)),
        area_to_toolgroups=MappingProxyType(_derive_area_map(toolgroups)),
        setup_to_toolgroup=MappingProxyType(_derive_setup_map(routes, strict=strict)),
        explicit_lots=explicit_lots,
        initial_wip=initial,
    )
    if use_memo:
        _PARSE_MEMO[memo_key] = data
    return data


# ---------------------------------------------------------------------------
# 감사 스크립트 (01_data_contract.md 4절)
# ---------------------------------------------------------------------------


def _audit(path: Path) -> None:
    t0 = time.perf_counter()
    data = parse_workbook(path, strict=False)
    elapsed = time.perf_counter() - t0

    def line(label: str, value: Any) -> None:
        print(f"  {label:<44} {value}")

    print("=" * 74)
    print(f"SMT2020 workbook audit — {path.name}")
    print("=" * 74)
    print(f"\nparse time: {elapsed:.1f} s\n")

    print("-- sheet sizes ------------------------------------------------------")
    line("toolgroups", len(data.toolgroups))
    line("total tools", data.total_tools())
    line("breakdown areas", len(data.breakdowns))
    line("pm toolgroups / streams",
         f"{len(data.pm_streams)} / {sum(len(v) for v in data.pm_streams.values())}")
    line("setup rows", len(data.setups))
    line("release streams", len(data.release_streams))
    line("transport pairs", len(data.transport))

    print("\n-- steps per product ------------------------------------------------")
    for p, r in sorted(data.routes.items(), key=lambda kv: int(kv[0].split("_")[1])):
        line(p, len(r.steps))
    line("total", data.total_steps())

    print("\n-- toolgroup categoricals -------------------------------------------")
    for attr in ("area", "cascading", "batching", "batch_criterion", "batching_unit",
                 "location", "dispatching", "rank1", "rank2", "rank3", "wake_up_rank"):
        c = Counter(getattr(s, attr) for s in data.toolgroups.values())
        line(attr, dict(sorted(c.items(), key=lambda kv: (kv[0] is None, str(kv[0])))))

    print("\n-- route features ---------------------------------------------------")
    feats = Counter()
    units = Counter()
    ptdist = Counter()
    for r in data.routes.values():
        for s in r.steps:
            units[s.processing_unit] += 1
            ptdist[s.pt_dist] += 1
            if s.cascade_interval is not None:
                feats["cascade_interval"] += 1
            if s.batch_min is not None:
                feats["batch_min"] += 1
            if s.setup_name is not None:
                feats["setup"] += 1
                feats["setup_route_defined" if s.setup_dist == "constant"
                      else "setup_sheet_resolved"] += 1
            if s.ltl_target_index is not None:
                feats["ltl"] += 1
            if s.rework_target_index is not None:
                feats["rework"] += 1
            if s.sampling_prob is not None:
                feats["sampling"] += 1
            if s.cqt_target_index is not None:
                feats["cqt"] += 1
            if data.is_delay_step(s):
                feats["delay"] += 1
    line("processing unit", dict(units))
    line("pt distribution", dict(ptdist))
    for k in sorted(feats):
        line(k, feats[k])

    print("\n-- derived ----------------------------------------------------------")
    line("release rate (wspw)", f"{data.release_rate_wspw():.1f}")
    line("batching toolgroups", len(data.batching_toolgroups()))
    line("cascading toolgroups", len(data.cascading_toolgroups()))
    line("setup-avoidance toolgroups", list(data.setup_avoidance_toolgroups()))
    line("wake-up toolgroups", len(data.wake_up_toolgroups()))
    line("setup-bearing toolgroups", list(data.setup_bearing_toolgroups()))
    line("distinct setup names", len(data.setup_to_toolgroup))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SMT2020 workbook parser")
    ap.add_argument("--audit", action="store_true", help="워크북 구조를 출력한다")
    ap.add_argument("--xlsx", type=Path, default=config.XLSX_PATH)
    args = ap.parse_args(argv)

    if args.audit:
        _audit(args.xlsx)
        return 0

    t0 = time.perf_counter()
    data = parse_workbook(args.xlsx)
    print(f"parsed {len(data.toolgroups)} toolgroups, {data.total_steps()} steps "
          f"in {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
