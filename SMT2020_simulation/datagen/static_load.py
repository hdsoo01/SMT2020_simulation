"""엑셀만으로 계산하는 툴그룹 정적 부하 (시뮬레이션 없이).

    부하      = Σ_제품 투입률(lot/분) × lot 하나당 유효 장비 점유(분) ÷ 장비 대수
    비가용률  = 고장 MTTR/(MTTF+MTTR) + Σ 날짜PM TTR/MTBPM + Σ counterPM TTR × (장비당 wafer/분)/MTBPM
    다운타임 후 가동률 = 부하 ÷ (1 - 비가용률) × k   (k: 캐스케이딩 보정, 그 외 1)

lot 하나당 유효 점유는 step마다 (처리 확률) × (점유 + load/unload) 이며,
  wafer 단위 25×pt, wafer 캐스케이딩 max(25×interval, (pt+24×interval)/2), lot 캐스케이딩 max(interval, pt/2),
  배치는 배치당 lot 수로 나눈다 (기본: 기존 8년 실행의 실제 평균 배치 크기, 없으면 최대).
리워크(8년 6,978건)는 무시한다. 셋업은 디스패칭에 달려 넣을 수 없다.

점검보고서 4.4절: 기준 믹스에서 이 식(배치 실측, 캐스케이딩 보정)과 시뮬 가동률의 순위 상관 0.92.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

NW = 25
WEEK = 10080.0
CALIB = Path(__file__).resolve().parent / "calib"


@dataclass(frozen=True)
class StaticModel:
    num_tools: dict          # tg -> 대수
    area: dict               # tg -> area
    per_lot_min: dict        # tg -> {product: lot 하나 투입당 유효 점유(분)}
    wafer_visits: dict       # tg -> {product: lot 하나 투입당 처리 wafer 수}
    down_frac: dict          # tg -> 고장 비율
    pm_time_frac: dict       # tg -> 날짜 기준 PM 비율 합
    pm_counter_coef: dict    # tg -> Σ TTR/MTBPM  (× 장비당 wafer/분 = counter PM 비율)
    cascade_k: dict          # tg -> 보정 계수


def load_calib():
    b = json.loads((CALIB / "batch_lots_base.json").read_text(encoding="utf-8"))
    k = json.loads((CALIB / "cascade_k.json").read_text(encoding="utf-8"))
    return b, k


def build(data, batch_lots: dict | None = None, cascade_k: dict | None = None) -> StaticModel:
    if batch_lots is None or cascade_k is None:
        b, k = load_calib()
        batch_lots = b if batch_lots is None else batch_lots
        cascade_k = k if cascade_k is None else cascade_k
    per = defaultdict(lambda: defaultdict(float))
    wv = defaultdict(lambda: defaultdict(float))
    for prod, route in data.routes.items():
        for s in route.steps:
            spec = data.toolgroups[s.toolgroup]
            if spec.is_delay:
                continue
            p = 1.0 if s.sampling_prob is None else s.sampling_prob / 100.0
            unit = s.processing_unit.lower()
            if unit == "wafer":
                occ = s.pt_mean + (NW - 1) * s.cascade_interval if s.cascade_interval else s.pt_mean * NW
            else:
                occ = s.pt_mean
            if s.cascade_interval:
                gate = NW * s.cascade_interval if unit == "wafer" else s.cascade_interval
                occ = max(gate, occ / 2.0)
            t = occ + spec.load_time + spec.unload_time
            if s.batch_min is not None:
                t /= batch_lots.get(f"{prod}:{s.step_index}", s.batch_max // NW)
            per[s.toolgroup][prod] += p * t
            wv[s.toolgroup][prod] += p * NW
    down, pmt, pmc = {}, {}, {}
    for tg, spec in data.toolgroups.items():
        if spec.is_delay:
            continue
        bd = data.breakdowns.get(spec.area)
        down[tg] = bd.mttr / (bd.mttf + bd.mttr) if bd else 0.0
        pmt[tg] = sum(ps.ttr_mean / ps.mtbpm for ps in data.pm_streams.get(tg, ()) if not ps.counter_based)
        pmc[tg] = sum(ps.ttr_mean / ps.mtbpm for ps in data.pm_streams.get(tg, ()) if ps.counter_based)
    tgs = [tg for tg, s in data.toolgroups.items() if not s.is_delay]
    return StaticModel(
        num_tools={tg: data.toolgroups[tg].num_tools for tg in tgs},
        area={tg: data.toolgroups[tg].area for tg in tgs},
        per_lot_min={tg: dict(per[tg]) for tg in tgs},
        wafer_visits={tg: dict(wv[tg]) for tg in tgs},
        down_frac=down, pm_time_frac=pmt, pm_counter_coef=pmc,
        cascade_k={tg: float(cascade_k.get(tg, 1.0)) for tg in tgs},
    )


def base_lots_per_week(data) -> dict:
    """엑셀 Lotrelease 그대로의 제품별 투입 lot/주 (regular + hot + superhot)."""
    r = defaultdict(float)
    for s in data.release_streams:
        r[s.product] += s.lots_per_release / s.release_interval * WEEK
    return dict(r)


def evaluate(model: StaticModel, lots_per_week: dict, *, breakdown_scale: dict | None = None,
             tools_out: dict | None = None) -> dict:
    """툴그룹별 {load, unavail, util_after}. 값은 비율(0~1).

    다운타임 시나리오 반영 (datagen/downtime_scenarios.py):
      breakdown_scale  tg -> (MTTR 배율, MTTF 배율)
      tools_out        tg -> 정지시킨 장비 대수 (남은 장비가 같은 부하를 처리)
    """
    out = {}
    breakdown_scale = breakdown_scale or {}
    tools_out = tools_out or {}
    for tg, n_all in model.num_tools.items():
        n = n_all - tools_out.get(tg, 0)
        if n <= 0:
            out[tg] = {"load": float("inf"), "unavail": 1.0, "util_after": float("inf")}
            continue
        rate = {p: lots_per_week.get(p, 0.0) / WEEK for p in model.per_lot_min[tg]}
        load = sum(rate[p] * m for p, m in model.per_lot_min[tg].items()) / n
        wpm = sum(rate[p] * w for p, w in model.wafer_visits[tg].items()) / n
        down = model.down_frac[tg]
        if tg in breakdown_scale and down > 0:
            fr, ff = breakdown_scale[tg]
            ratio = down / (1.0 - down)                    # = MTTR / MTTF
            down = fr * ratio / (ff + fr * ratio)
        un = down + model.pm_time_frac[tg] + model.pm_counter_coef[tg] * wpm
        out[tg] = {"load": load, "unavail": un,
                   "util_after": load / (1.0 - un) * model.cascade_k[tg]}
    return out


def ranking(result: dict, k: int = 5) -> list[tuple[str, float]]:
    return sorted(((tg, v["util_after"]) for tg, v in result.items()), key=lambda x: -x[1])[:k]
