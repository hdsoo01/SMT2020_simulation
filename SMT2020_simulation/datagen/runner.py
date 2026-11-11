"""시나리오 1개 실행 → L1 원시 로그.

시뮬레이터 코드(code/smt2020_sim)는 고치지 않는다. 여기서 하는 일은 셋뿐이다.

1. ScheduledFab  : FabModel._release_stream 만 바꿔, 제품별 투입 간격을 시간 구간별로 따른다.
                   구간이 하나면 원래 코드와 같은 이벤트·난수 순서를 낸다 (smoke test가 확인).
                   대기열/배치 진입 지점에서 툴그룹 도착을 센다 (이벤트를 만들지 않는다).
2. LogKPI        : KPICollector 의 기록 함수를 감싸 시간 단위 집계와 장비 활성 구간을 만든다.
3. 하루 1회 감시 : lot 위치, 장비 PM counter 진행률을 표본으로 남긴다 (env.timeout 만 쓴다).

출력 (out/):
  spec.json                  시나리오 + 해석된 투입 일정 + 정적 사전 점검
  release_schedule.csv       stream별 (시작 분, 투입 간격 분)
  tg_hourly.parquet          툴그룹 x 시간: lot-분(대기열/배치대기/정지대기/장비 위/이송/delay),
                             도착·처리 시작·완료 수, 장비 상태 분(가동/고장/PM/유휴)
  active_periods.parquet     장비별 활성 구간 (작업 또는 정지가 하나라도 있는 연속 구간)
  lot_positions_daily.parquet 하루 1회 살아 있는 lot의 제품·step·버킷
  station_daily.parquet      하루 1회 장비별 counter PM 진행률(최댓값), 정지 여부
  stations.csv               장비 번호 -> 장비 ID, 툴그룹
  lots.parquet               lot별 제품·종류·투입·납기·완료 시각 (서버용, SQLite에서 추출)
  hot_conversions.csv        (hot_conversion 시나리오만) regular → hot 으로 낸 lot 의 id·제품·투입 시각
  downtime.parquet           장비별 고장/PM 구간 (서버용). 다운타임 시나리오의 장기 정지는 name "OUTAGE"
  ic_hourly.parquet          (확장 ic) 툴그룹별 대기 lot 수·대기 경과시간 합, 하류 대기 — 논문 IC 의 재료
  active_periods_work.parquet (확장 work_active) 장비별 '작업만' 활성 구간
  snapshot.json, details/    기존 KPI 스냅샷(lean)과 SQLite(lots, downtime, batches ...) — 이 PC에만 둔다
  run_meta.json              실행 시간, 메모리, 모델/입력 해시

    python -B datagen/runner.py <spec.json> <out_dir>
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sqlite3
import sys
import time
from array import array
from pathlib import Path

import numpy as np
import pandas as pd
import simpy

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from smt2020_sim import config  # noqa: E402
from smt2020_sim.data_parser import parse_workbook  # noqa: E402
from smt2020_sim.fab_model import FabModel  # noqa: E402
from smt2020_sim.kpi_collector import KPICollector, input_hash, model_hash, write_snapshot  # noqa: E402
from smt2020_sim.lot import BUCKETS, Lot  # noqa: E402
from smt2020_sim.main import FEATURES  # noqa: E402
import downtime_scenarios  # noqa: E402
import extensions  # noqa: E402
import scenario as sc  # noqa: E402

WEEK = 7 * 1440.0
YEAR = 365 * 1440.0
CATS = ("wait_queue", "wait_batch", "wait_down", "on_tool", "transport", "delay")
BUCKET_CAT = {"queue": 0, "wait_gate": 0, "wait_dedicated": 0, "wait_reserved": 0,
              "wait_batch": 1, "wait_down": 2, "process": 3, "setup": 3, "load_unload": 3,
              "transport": 4, "delay": 5}
COUNTS = ("arrivals", "starts", "completions")
STATES = ("st_work", "st_breakdown", "st_pm", "st_idle")     # 우선순위: 고장 > PM > 작업 > 유휴
BUCKET_CODE = {b: i for i, b in enumerate(BUCKETS)}
LOT_TYPE_CODE = {"regular": 0, "hot": 1, "superhot": 2}


# ---------------------------------------------------------------------------
# 기록기
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self, env, toolgroups: list[str], stations: list[tuple[str, str]], total_min: float):
        self.env = env
        self.toolgroups = toolgroups
        self.tg_index = {n: i for i, n in enumerate(toolgroups)}
        self.stations = stations
        self.st_index = {s: i for i, (s, _) in enumerate(stations)}
        self.st_tg = [self.tg_index[tg] for _, tg in stations]
        self.H = int(math.ceil(total_min / 60.0)) + 1
        n_tg, n_st = len(toolgroups), len(stations)
        self.occ = np.zeros((len(CATS), n_tg, self.H))
        self.cnt = np.zeros((len(COUNTS), n_tg, self.H), dtype=np.int32)
        self.stt = np.zeros((len(STATES), n_tg, self.H))
        self.nwork = [0] * n_st
        self.nbd = [0] * n_st
        self.npm = [0] * n_st
        self.state = [3] * n_st
        self.since = [0.0] * n_st
        self.ap_start = [None] * n_st
        # 유휴로 바뀐 시각. 같은 시각에 다시 활성이 되면(로딩→처리→언로딩 사이의 0분 유휴)
        # 구간을 끊지 않고 이어 붙인다.
        self.ap_pending = [None] * n_st
        self.ap_bd = [False] * n_st
        self.ap_pm = [False] * n_st
        self.ap = {"station": array("i"), "start_min": array("d"), "end_min": array("d"),
                   "had_breakdown": array("b"), "had_pm": array("b"), "censored": array("b")}
        self.pos = {"day": array("i"), "product": array("b"), "step_index": array("h"),
                    "bucket": array("b"), "lot_type": array("b")}
        self.std = {"day": array("i"), "station": array("i"), "counter_progress": array("d"), "down": array("b")}
        self.states_final = False
        self.station_exts: list = []          # extensions.py — 장비 작업·정지 수 변화를 받는 확장

    @staticmethod
    def _acc(row, a: float, b: float) -> None:
        h0, h1 = int(a // 60.0), int(b // 60.0)
        if h0 == h1:
            row[h0] += b - a
            return
        row[h0] += (h0 + 1) * 60.0 - a
        if h1 > h0 + 1:
            row[h0 + 1:h1] += 60.0
        if h1 < len(row):
            row[h1] += b - h1 * 60.0

    # -- lot 쪽 -------------------------------------------------------------
    def segment(self, tg: str, bucket: str, a: float, b: float) -> None:
        if b > a:
            self._acc(self.occ[BUCKET_CAT[bucket], self.tg_index[tg]], a, b)

    def count(self, kind: int, tg: str) -> None:
        self.cnt[kind, self.tg_index[tg], int(self.env.now // 60.0)] += 1

    # -- 장비 쪽 ------------------------------------------------------------
    def station_delta(self, station: str, dwork=0, dbd=0, dpm=0) -> None:
        if self.states_final:
            return
        i = self.st_index[station]
        self.nwork[i] += dwork
        self.nbd[i] += dbd
        self.npm[i] += dpm
        if min(self.nwork[i], self.nbd[i], self.npm[i]) < 0:
            raise RuntimeError(f"{station}: 작업/정지 시작·종료 짝이 맞지 않는다")
        new = 1 if self.nbd[i] else 2 if self.npm[i] else 0 if self.nwork[i] else 3
        old = self.state[i]
        now = self.env.now
        for ext in self.station_exts:
            ext.on_station(i, self.nwork[i], self.nbd[i], self.npm[i], now)
        if new != old:
            if now > self.since[i]:
                self._acc(self.stt[old, self.st_tg[i]], self.since[i], now)
            self.since[i] = now
            self.state[i] = new
            if old == 3:
                pend = self.ap_pending[i]
                if pend is not None and now - pend > 1e-9:
                    self._emit_ap(i, pend, censored=False)        # 진짜 유휴가 있었다
                if self.ap_start[i] is None:
                    self.ap_start[i], self.ap_bd[i], self.ap_pm[i] = now, False, False
                self.ap_pending[i] = None
            elif new == 3:
                self.ap_pending[i] = now
        if new == 1:
            self.ap_bd[i] = True
        elif new == 2:
            self.ap_pm[i] = True

    def _emit_ap(self, i: int, end: float, censored: bool) -> None:
        self.ap["station"].append(i)
        self.ap["start_min"].append(self.ap_start[i])
        self.ap["end_min"].append(end)
        self.ap["had_breakdown"].append(self.ap_bd[i])
        self.ap["had_pm"].append(self.ap_pm[i])
        self.ap["censored"].append(censored)
        self.ap_start[i] = None

    def finalize_states(self, now: float) -> None:
        for i in range(len(self.stations)):
            if now > self.since[i]:
                self._acc(self.stt[self.state[i], self.st_tg[i]], self.since[i], now)
            self.since[i] = now
            if self.ap_pending[i] is not None:
                self._emit_ap(i, self.ap_pending[i], censored=False)
                self.ap_pending[i] = None
            elif self.ap_start[i] is not None:
                self._emit_ap(i, now, censored=True)
        self.states_final = True

    # -- 출력 ---------------------------------------------------------------
    def hourly_frame(self, end_min: float) -> pd.DataFrame:
        H = int(math.ceil(end_min / 60.0))
        tg, hr = np.meshgrid(np.arange(len(self.toolgroups)), np.arange(H), indexing="ij")
        df = pd.DataFrame({"hour": hr.ravel().astype(np.int32),
                           "toolgroup": pd.Categorical.from_codes(tg.ravel(), self.toolgroups)})
        for k, name in enumerate(CATS):
            df[f"lotmin_{name}"] = self.occ[k, :, :H].ravel().astype(np.float32)
        for k, name in enumerate(COUNTS):
            df[name] = self.cnt[k, :, :H].ravel()
        for k, name in enumerate(STATES):
            df[name] = self.stt[k, :, :H].ravel().astype(np.float32)
        return df


# ---------------------------------------------------------------------------
# 시뮬레이터 확장 (상속만, 원본 수정 없음)
# ---------------------------------------------------------------------------

class LogKPI(KPICollector):
    rec: Recorder | None = None

    def record_segment(self, lot, bucket, start, end):
        super().record_segment(lot, bucket, start, end)
        if self.rec is not None and end > start:
            v = self._visits.get(lot.lot_id)
            if v is not None:
                self.rec.segment(v["toolgroup"], bucket, start, end)

    def assign_station(self, lot, tool, batch_id=None):
        super().assign_station(lot, tool, batch_id)
        if self.rec is not None:
            self.rec.count(1, tool.group.name)

    visit_exts: list = []                 # extensions.py — 방문 완료를 받는 확장

    def end_visit(self, lot, status, rework_target=None):
        v = self._visits.get(lot.lot_id)
        super().end_visit(lot, status, rework_target)
        if self.rec is not None and v is not None and status == "completed":
            self.rec.count(2, v["toolgroup"])
            for ext in self.visit_exts:
                ext.on_visit_completed(lot, v["toolgroup"])

    def begin_work(self, tool, attr):
        super().begin_work(tool, attr)
        if self.rec is not None:
            self.rec.station_delta(tool.tool_id, dwork=1)

    def end_work(self, tool):
        known = (not self.details.closed) and (tool.tool_id, self.env.active_process) in self._work
        super().end_work(tool)
        if known and self.rec is not None:
            self.rec.station_delta(tool.tool_id, dwork=-1)

    def begin_downtime(self, tool, kind, name):
        eid = super().begin_downtime(tool, kind, name)
        if self.rec is not None:
            self.rec.station_delta(tool.tool_id, **({"dbd": 1} if kind == "breakdown" else {"dpm": 1}))
        return eid

    def end_downtime(self, event_id):
        rec = self._down.get(event_id)
        super().end_downtime(event_id)
        if rec is not None and self.rec is not None:
            self.rec.station_delta(rec[1], **({"dbd": -1} if rec[4] == "breakdown" else {"dpm": -1}))


class ScheduledFab(FabModel):
    def __init__(self, env, data, kpi, rng, *, schedule: dict, rec: Recorder, reservation=None, hot_rules=None,
                 dispatch_rules=None):
        super().__init__(env, data, kpi, rng, reservation=reservation)
        self.schedule = schedule
        self.rec = rec
        # 디스패칭 순서 변경 (베타, scenario.py dispatch, 11-09). 규칙이 없으면 ctx 를 건드리지 않는다 (기준 실행과 같은 결과)
        if dispatch_rules:
            self.ctx.set_dispatch_rules([{"toolgroups": d["toolgroups"], "rank3": d["rank3"],
                                          "cr_before_setup": d["cr_before_setup"], "start_min": d["start_week"] * WEEK,
                                          "end_min": math.inf if d["end_week"] is None else d["end_week"] * WEEK}
                                         for d in dispatch_rules])
        # regular → hot 전환 (scenario.py hot_conversion). 규칙이 없으면 아래 _to_hot 은 불리지 않는다
        self.hot_rules = [{"start": h["start_week"] * WEEK, "end": h["end_week"] * WEEK,
                           "products": set(h["products"]), "share": h["share"]} for h in (hot_rules or [])]
        self.hot_due = {s.product: s.due_date_offset for s in data.release_streams if s.priority >= 20 and not s.is_superhot}
        self._hot_seen: dict = {}
        self.hot_log: list = []

    def _to_hot(self, product: str) -> bool:
        """이 regular lot 을 hot 으로 낼지. 규칙·제품별 k번째 lot 이 floor((k+1)·share) > floor(k·share) 이면 전환한다.
        난수를 쓰지 않으므로 다른 사건의 난수 순서가 기준 실행과 같다."""
        now = self.env.now
        for i, r in enumerate(self.hot_rules):
            if r["start"] <= now < r["end"] and product in r["products"]:
                k = self._hot_seen.get((i, product), 0)
                self._hot_seen[(i, product)] = k + 1
                return math.floor((k + 1) * r["share"] + 1e-9) > math.floor(k * r["share"] + 1e-9)
        return False

    def _next_delay(self, segs, now: float) -> float | None:
        """지금부터 다음 투입까지의 시간. 구간별 투입률을 적분해 lot 1개가 차는 시점을 찾는다.

        구간을 넘지 않으면 interval 그대로 돌려준다 — 원래 코드의 `timeout(interval)`과
        부동소수점까지 같게 하기 위해서다.
        """
        need, delay, t = 1.0, 0.0, now
        while True:
            k = max(i for i, (s, _) in enumerate(segs) if s <= t + 1e-9)
            interval = segs[k][1]
            seg_end = segs[k + 1][0] if k + 1 < len(segs) else math.inf
            if interval is None or interval <= 0:
                if math.isinf(seg_end):
                    return None
                delay += seg_end - t
                t = seg_end
                continue
            dt = need * interval
            if t + dt <= seg_end:
                return delay + dt if delay else dt
            need -= (seg_end - t) / interval
            delay += seg_end - t
            t = seg_end

    @staticmethod
    def _until_release(segs, now: float) -> float | None:
        """지금 구간이 투입 없음(간격 None·0, lot_type_mult 0)이면 다음 투입 구간 시작까지의 시간, 투입 구간이면 0.
        끝까지 투입이 없으면 None (11-07)."""
        k = max(i for i, (s, _) in enumerate(segs) if s <= now + 1e-9)
        if segs[k][1] is not None and segs[k][1] > 0:
            return 0.0
        nxt = [s for s, iv in segs[k + 1:] if iv is not None and iv > 0]
        return nxt[0] - now if nxt else None

    def _release_stream(self, stream):
        """fab_model.FabModel._release_stream 과 같은 lot 생성, 간격만 일정표를 따른다."""
        route = self.data.routes[stream.product]
        lo, hi = config.DUE_DATE_Z
        segs = self.schedule.get(stream.lot_type) or [(0.0, stream.release_interval)]
        wait = self._until_release(segs, self.env.now)                 # 11-07: 투입 0 구간에서 시작하면 첫 lot 도 내지 않음
        if wait is None:
            return
        if wait > 0:
            yield self.env.timeout(wait)
        while True:
            for _ in range(stream.lots_per_release):
                self._lot_counters[stream.lot_type] += 1
                n = self._lot_counters[stream.lot_type]
                due_z = self.rng.uniform(lo, hi)
                lot_id, prio, due_off = f"{stream.lot_type}_{n}", stream.priority, stream.due_date_offset
                if self.hot_rules and stream.priority == 10 and not stream.is_superhot and self._to_hot(stream.product):
                    lot_id, prio = f"{lot_id}_H", 20
                    due_off = self.hot_due.get(stream.product, stream.due_date_offset)
                    self.hot_log.append((lot_id, stream.product, self.env.now))
                lot = Lot(
                    lot_id=lot_id,
                    product=stream.product,
                    route=route,
                    priority=prio,
                    wafers_per_lot=stream.wafers_per_lot,
                    release_time=self.env.now,
                    due_date=(self.env.now + due_z * due_off),
                    is_superhot=stream.is_superhot,
                    due_base_min=due_off, due_z=due_z,
                )
                self.live_lots[lot.lot_id] = lot
                self.kpi.note_wip(len(self.live_lots))
                self.kpi.record_release(lot)
                self.env.process(self._process_lot(lot))
            d = self._next_delay(segs, self.env.now)
            if d is None:
                return
            yield self.env.timeout(d)

    def _acquire_tool(self, lot, step, tg):
        self.rec.count(0, tg.name)
        return (yield from super()._acquire_tool(lot, step, tg))

    def _batch_step(self, lot, step, tg):
        self.rec.count(0, tg.name)
        yield from super()._batch_step(lot, step, tg)


def daily_monitor(env, fab, rec: Recorder, products: list[str]):
    pidx = {p: i for i, p in enumerate(products)}
    tools = [t for tg in fab.toolgroups.values() for t in tg.tools]
    while True:
        yield env.timeout(1440.0)
        day = int(env.now // 1440.0)
        for lot in fab.live_lots.values():
            rec.pos["day"].append(day)
            rec.pos["product"].append(pidx[lot.product])
            rec.pos["step_index"].append(lot.current_step_index)
            rec.pos["bucket"].append(BUCKET_CODE.get(lot._bucket, -1))
            rec.pos["lot_type"].append(LOT_TYPE_CODE[lot.lot_type])
        for t in tools:
            prog = [t.pm_counters[n] / t.pm_thresholds[n] for n in t.pm_counters if t.pm_thresholds.get(n)]
            rec.std["day"].append(day)
            rec.std["station"].append(rec.st_index[t.tool_id])
            rec.std["counter_progress"].append(max(prog) if prog else float("nan"))
            rec.std["down"].append(t.is_down)


def progress(env, fab, kpi, total, every_weeks=2):
    """진행 한 줄. 예상 남은 시간은 **최근 구간의 속도**로 계산한다 — WIP가 차오르며 느려지므로
    처음부터의 평균 속도로 계산하면 남은 시간을 짧게 잡는다. env.timeout 만 쓰므로 결과에 영향이 없다."""
    t0 = last_wall = time.perf_counter()
    last_sim = 0.0
    while True:
        yield env.timeout(every_weeks * WEEK)
        now_wall = time.perf_counter()
        rate = (now_wall - last_wall) / max(env.now - last_sim, 1e-9)       # 실제 초 / 시뮬레이션 분
        last_wall, last_sim = now_wall, env.now
        print(f"week {env.now / WEEK:4.0f}/{total / WEEK:.0f}  wip {len(fab.live_lots):5d}  "
              f"rel {kpi.n_released:6d}  done {kpi.n_completed:6d}  elapsed {now_wall - t0:6.0f}s  "
              f"eta {rate * max(total - env.now, 0):6.0f}s", flush=True)


def _reseed(env, rng, at: float, value: int):
    """분기 (C1, 11-05): 분기 주 1분 전에 난수 생성기를 다시 seed 한다. 분기 주에 시작하는 사건(정지·투입 변경)보다
    먼저 일어나도록 1분 앞당긴다. 시뮬레이터는 난수 생성기 하나만 쓴다 (smt2020_sim main.py R13, runner.py rng)."""
    yield env.timeout(max(at, 0.0))
    rng.seed(value)


# ---------------------------------------------------------------------------

def run(spec_path: Path, out: Path, *, end_week_override: float | None = None, drop_sqlite: bool = False) -> dict:
    spec = sc.load(spec_path)
    if end_week_override is not None:
        spec["end_week"] = end_week_override
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "run_meta.json").exists():
        raise SystemExit(f"이미 완료된 출력 폴더다: {out}")

    data = parse_workbook(release_mode="parametric", initial_wip=False)
    plan, data, extra_dt = sc.prepare(spec, data)        # overrides (장비 수·가공 시간·PM) 를 적용한 data
    if plan.errors:                                       # 형식 오류는 allow_unstable 로도 넘기지 않는다
        raise SystemExit("시나리오 형식 오류: " + "; ".join(plan.errors))
    if not plan.feasible and not spec.get("allow_unstable", False):
        raise SystemExit("정적 사전 점검 위반: " + "; ".join(sum((p["problems"] for p in plan.phases), [])))
    exts = extensions.build(spec)
    (out / "spec.json").write_text(json.dumps({"spec": spec, "phases": plan.phases, "downtime": plan.downtime,
                                               "extensions": [e.name for e in exts], "hot_conversion": plan.hot,
                                               "overrides": plan.overrides, "branch": plan.branch,
                                               **({"dispatch": plan.dispatch} if plan.dispatch else {})},
                                              indent=1, ensure_ascii=False), encoding="utf-8")
    pd.DataFrame([(lt, s, iv) for lt, segs in plan.schedule.items() for s, iv in segs],
                 columns=["lot_type", "start_min", "interval_min"]).to_csv(out / "release_schedule.csv", index=False)

    total = float(spec["end_week"]) * WEEK
    warm = min(float(spec["label_start_week"]) * WEEK, total)
    toolgroups = list(data.toolgroups)
    stations = [(f"{n}#{i + 1}", n) for n, s in data.toolgroups.items() if not s.is_delay for i in range(s.num_tools)]
    products = sorted(data.routes, key=lambda p: int(p.split("_")[1]))

    env = simpy.Environment()
    rng = random.Random(int(spec["seed"]))
    rec = Recorder(env, toolgroups, stations, total)
    kpi = LogKPI(env, warmup_minutes=warm if warm < total else 0.0, detail_dir=out / "details", detail_level="lean")
    kpi.rec = rec
    fab = ScheduledFab(env, data, kpi, rng, schedule=plan.schedule, rec=rec, hot_rules=plan.hot,
                       dispatch_rules=plan.dispatch)
    # 확장(관찰 전용)과 다운타임 시나리오(동작 변경) — 둘 다 시나리오 JSON 으로 켜고 끈다
    for e in exts:
        e.attach(env, fab, rec)
    rec.station_exts = [e for e in exts if e.station_hook]
    kpi.visit_exts = [e for e in exts if e.visit_hook]
    dt_installed = downtime_scenarios.install(env, fab, list(spec.get("downtime", [])) + extra_dt, data)  # fab.start() 전
    if plan.branch:                                       # 분기 (C1): 분기 주까지는 seed 의 기준 실행과 같은 경로
        env.process(_reseed(env, rng, plan.branch["week"] * WEEK - 1.0, plan.branch["reseed"]))
    try:
        fab.start()
        for e in exts:
            for p in e.processes():
                env.process(p)
        env.process(daily_monitor(env, fab, rec, products))
        env.process(progress(env, fab, kpi, total))

        t0 = time.perf_counter()
        if 0.0 < warm < total:
            env.run(until=warm)
            kpi.mark_warmup(fab)
        env.run(until=total)
        runtime = time.perf_counter() - t0
    finally:
        dt_installed.uninstall()
    print(f"시뮬레이션 끝 ({runtime:.0f}s) — 결과 저장 중", flush=True)
    rec.finalize_states(env.now)
    for e in exts:
        e.finalize(env.now)

    snap = kpi.snapshot(phase="datagen", mode="parametric", seed=int(spec["seed"]), years=total / YEAR,
                        features=sorted(FEATURES), runtime_s=runtime, fab=fab)
    rec.hourly_frame(total).to_parquet(out / "tg_hourly.parquet", index=False)
    pd.DataFrame({k: np.frombuffer(v, dtype=v.typecode) if len(v) else np.array([], dtype=v.typecode)
                  for k, v in rec.ap.items()}).to_parquet(out / "active_periods.parquet", index=False)
    pd.DataFrame({k: np.frombuffer(v, dtype=v.typecode) if len(v) else np.array([], dtype=v.typecode)
                  for k, v in rec.pos.items()}).to_parquet(out / "lot_positions_daily.parquet", index=False)
    pd.DataFrame({k: np.frombuffer(v, dtype=v.typecode) if len(v) else np.array([], dtype=v.typecode)
                  for k, v in rec.std.items()}).to_parquet(out / "station_daily.parquet", index=False)
    pd.DataFrame(stations, columns=["station_id", "toolgroup"]).rename_axis("station").to_csv(out / "stations.csv")
    write_snapshot(snap, out / "snapshot.json")
    kpi.close()
    # 서버는 SQLite 없이 parquet만 읽는다. lot 투입·완료와 정지 이력을 따로 뽑아 둔다.
    db = Path(snap["detail"]["database"])      # 이번 실행이 저장한 DB (재실행 시 옛 DB를 집지 않도록)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        pd.read_sql_query("SELECT lot_id, product, lot_type, priority, release_min, due_min, end_min, status, "
                          "step_index FROM lots", con).to_parquet(out / "lots.parquet", index=False)
        pd.read_sql_query("SELECT station, toolgroup, area, kind, name, start_min, end_min, status FROM downtime",
                          con).to_parquet(out / "downtime.parquet", index=False)
    finally:
        con.close()
    ext_meta = {e.name: e.write(out) for e in exts}
    if fab.hot_log:                                       # regular → hot 으로 낸 lot (hot_conversion)
        pd.DataFrame(fab.hot_log, columns=["lot_id", "product", "release_min"]).to_csv(out / "hot_conversions.csv", index=False)
    if drop_sqlite:                       # 내려받을 용량을 줄인다 — 필요한 표는 위에서 parquet 로 뽑았다
        shutil.rmtree(out / "details", ignore_errors=True)

    meta = {"name": spec["name"], "seed": spec["seed"], "end_week": spec["end_week"],
            "label_start_week": spec["label_start_week"], "runtime_s": round(runtime, 1),
            "peak_rss_mb": _peak_rss_mb(), "sqlite_kept": not drop_sqlite,
            "model_hash": model_hash(), "input_hash": input_hash(config.XLSX_PATH),
            "lots_released": kpi.n_released, "lots_completed": kpi.n_completed,
            "active_periods": len(rec.ap["station"]), "warnings": dict(fab.warnings),
            "extensions": ext_meta, "downtime": spec.get("downtime", []),
            "hot_conversion": spec.get("hot_conversion", []), "hot_converted_lots": len(fab.hot_log),
            "overrides": spec.get("overrides", {}), "override_summary": plan.overrides, "branch": plan.branch,
            **({"dispatch": plan.dispatch} if plan.dispatch else {}),
            "buckets": list(BUCKETS), "lot_types": LOT_TYPE_CODE, "products": products}
    (out / "run_meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"done {spec['name']}: {runtime:.0f}s, peak {meta['peak_rss_mb']} MB", flush=True)
    return meta


def _peak_rss_mb() -> float:
    """최대 메모리(MB). Windows 는 peak working set, Linux/macOS 는 ru_maxrss."""
    try:
        import psutil
        info = psutil.Process().memory_info()
        if hasattr(info, "peak_wset"):
            return round(info.peak_wset / 2**20, 1)
    except ImportError:
        pass
    try:
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(kb / 1024 if sys.platform != "darwin" else kb / 2**20, 1)
    except ImportError:
        return float("nan")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--end-week", type=float, default=None, help="짧은 시험용으로 기간만 줄인다")
    ap.add_argument("--drop-sqlite", action="store_true",
                    help="끝난 뒤 details/ (SQLite) 를 지운다. parquet·snapshot 은 남는다 (run_experiment.py sim 의 기본)")
    args = ap.parse_args(argv)
    run(args.spec, args.out, end_week_override=args.end_week, drop_sqlite=args.drop_sqlite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
