"""Single KPI collector, snapshot persistence and shared console/visualize reports.

Collection modes change retained history, not scheduling or random draws.
Existing snapshot fields and report functions are consumed by visualize.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import math
import sys
import sqlite3
import tempfile
import uuid
import os
import atexit
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config, paper
from .dispatch_audit import COLUMNS as DISPATCH_COLUMNS
from .lot import BUCKETS

if TYPE_CHECKING:  # pragma: no cover
    from .fab_model import FabModel
    from .lot import Lot

SCHEMA_VERSION = 4
RESULTS = config.RESULTS_DIR
YEAR_MINUTES = 365 * 24 * 60
WEEK_MINUTES = 7 * 24 * 60
AREA_ORDER = ["Litho", "Litho_Met", "Wet_Etch", "Diffusion", "TF", "TF_Met",
              "Planar", "Dry_Etch", "Dielectric", "Implant", "Def_Met"]


class _DetailStore:
    """Bounded write buffers; detailed history stays on disk, not in RAM."""

    def __init__(self, root=None):
        root = Path(root) if root is not None else config.RESULTS_DIR / "details"
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="collect-", dir=root)
        self.db = sqlite3.connect(str(Path(self.temp.name) / "history.sqlite"))
        self.closed = False
        atexit.register(self.close)
        self.buffers = defaultdict(list)
        self.size = 0
        buckets = ",".join(f"{b}_min REAL" for b in BUCKETS)
        dispatch = ",".join(f"{n} {t}" for n, t in DISPATCH_COLUMNS)
        self.db.executescript(f"""
            CREATE TABLE lots(lot_id TEXT PRIMARY KEY, product TEXT, lot_type TEXT,
                priority INTEGER, wafers INTEGER, initial_wip INTEGER,
                release_min REAL, due_min REAL, end_min REAL, status TEXT,
                step_index INTEGER, age_min REAL, lateness_min REAL,
                steps_executed INTEGER, steps_skipped INTEGER, rework_count INTEGER,
                buckets_json TEXT);
            CREATE TABLE visits(visit_id INTEGER PRIMARY KEY, lot_id TEXT,
                product TEXT, step_index INTEGER, step_id INTEGER, description TEXT,
                area TEXT, toolgroup TEXT, station TEXT, batch_id INTEGER,
                start_min REAL, end_min REAL, status TEXT, rework_target INTEGER,
                {buckets});
            CREATE TABLE segments(visit_id INTEGER, bucket TEXT, start_min REAL,
                end_min REAL, station TEXT, batch_id INTEGER);
            CREATE TABLE batches(batch_id INTEGER PRIMARY KEY, toolgroup TEXT,
                station TEXT, product TEXT, step_index INTEGER, lot_ids_json TEXT,
                lots INTEGER, wafers INTEGER, min_wafers INTEGER, max_wafers INTEGER,
                start_min REAL, end_min REAL, status TEXT, fill_pct REAL,
                wait_mean_min REAL, wait_max_min REAL);
            CREATE TABLE downtime(event_id INTEGER PRIMARY KEY, station TEXT,
                toolgroup TEXT, area TEXT, kind TEXT, name TEXT,
                start_min REAL, end_min REAL, status TEXT);
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE station_work(work_id INTEGER PRIMARY KEY, station TEXT,
                toolgroup TEXT, bucket TEXT, start_min REAL, end_min REAL, status TEXT);
            CREATE TABLE events(event_id INTEGER PRIMARY KEY, lot_id TEXT,
                product TEXT, kind TEXT, time_min REAL, attributes_json TEXT);
            CREATE TABLE lot_definitions(lot_id TEXT PRIMARY KEY,
                raw_pt_min REAL, rpt_paper_min REAL, start_step_index INTEGER,
                rework_span INTEGER);
            CREATE TABLE work_members(work_id INTEGER, visit_id INTEGER,
                lot_id TEXT, product TEXT, lot_type TEXT, wafers INTEGER,
                batch_id INTEGER);
            CREATE TABLE batch_queue_state(state_id INTEGER PRIMARY KEY,
                time_min REAL, toolgroup TEXT, product TEXT, step_index INTEGER,
                waiting_lots INTEGER, wafers INTEGER, min_wafers INTEGER);
            CREATE TABLE release_policy(lot_id TEXT PRIMARY KEY,due_base_min REAL,due_z REAL);
            CREATE TABLE batch_wait_summary(toolgroup TEXT,product TEXT,step_index INTEGER,
                lot_type TEXT,quantity_short_min REAL,quantity_ready_min REAL);
            CREATE TABLE flow_cube(product TEXT,step_index INTEGER,toolgroup TEXT,lot_type TEXT,
                arrivals INTEGER,completed INTEGER,skipped INTEGER,reworks INTEGER,censored INTEGER,
                wait_n INTEGER,wait_sum_min REAL,wait_sq_min REAL,wait_max_min REAL,{buckets});
            CREATE TABLE dispatch_log({dispatch});
        """)

    def add(self, table, row):
        if self.closed:
            return  # Generator finalizers can run after a finished collector closes.
        self.buffers[table].append(tuple(row))
        self.size += 1
        if self.size >= 4096:
            self.flush()

    def flush(self):
        for table, rows in self.buffers.items():
            if rows:
                marks = ",".join("?" for _ in rows[0])
                self.db.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({marks})", rows)
                rows.clear()
        self.db.commit()
        self.size = 0

    def save(self, metadata):
        self.flush()
        self.db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                            [(k, json.dumps(v)) for k, v in metadata.items()])
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS visits_lot ON visits(lot_id,visit_id);
            CREATE INDEX IF NOT EXISTS segments_visit ON segments(visit_id,start_min);
            CREATE INDEX IF NOT EXISTS visits_tg ON visits(toolgroup);
            CREATE INDEX IF NOT EXISTS work_members_work ON work_members(work_id);
            CREATE INDEX IF NOT EXISTS station_work_start ON station_work(station,start_min);
            CREATE INDEX IF NOT EXISTS station_work_end ON station_work(station,end_min);
            CREATE INDEX IF NOT EXISTS downtime_station ON downtime(station,start_min);
            CREATE INDEX IF NOT EXISTS batch_queue_group ON batch_queue_state(toolgroup,product,step_index,time_min,state_id);
            CREATE INDEX IF NOT EXISTS dispatch_log_tg ON dispatch_log(toolgroup,time_min);
        """)
        self.db.commit()
        path = self.root / f"kpi_{uuid.uuid4().hex}.sqlite"
        out = sqlite3.connect(path)
        try:
            self.db.backup(out)
        finally:
            out.close()
        return path

    def close(self):
        if self.closed:
            return
        self.db.close()
        self.temp.cleanup()
        self.closed = True
        atexit.unregister(self.close)

    def __del__(self):
        try:
            self.close()
        except (AttributeError, OSError, sqlite3.Error):
            pass


WAIT_BUCKETS = tuple(b for b in BUCKETS if b == "queue" or b.startswith("wait_"))


def _new_flow():
    return {"arrivals": 0, "completed": 0, "skipped": 0, "reworks": 0,
            "censored": 0, "buckets_min": dict.fromkeys(BUCKETS, 0.0),
            "wait_n": 0, "wait_sum": 0.0, "wait_sq": 0.0, "wait_max": 0.0}

# warm-up 경계에서 기준선을 뜨는 `Tool`의 누계 (`KPICollector.mark_warmup`).
#
# **여기 적힌 것만 warm-up 이후 값이 된다.** 새 누계를 `Tool`에 추가하고 여기
# 안 적으면 그 값만 조용히 전체 구간 기준으로 남아, 한 표 안에 두 기준이 섞이는
# 상태로 되돌아간다 — 이 목록이 존재하는 이유가 그것이다.
#
# `down_events`/`pm_events_count`는 정수지만 같이 뜬다. 시간(`down_time`)과
# 건수가 다른 구간을 재면 "한 번 오래"와 "여러 번 짧게"를 가를 수 없다.
TOOL_ACCUMULATORS: tuple[str, ...] = (
    "work_time", "busy_time", "proc_time", "loadunload_time", "setup_time",
    "down_time", "pm_time", "unavailable_time", "two_job_time",
    "down_events", "pm_events_count",
)


def _buckets_by(post) -> dict[str, dict[str, float]]:
    """lot 종류별 cycle-time 버킷 (시간). 종류마다 합 == 그 종류의 CT다."""
    out: dict[str, dict[str, float]] = {}
    by: dict[str, list] = defaultdict(list)
    for c in post:
        by[c.lot_type].append(c)
    for t, items in sorted(by.items()):
        tot: dict[str, float] = defaultdict(float)
        for c in items:
            for b, v in c.buckets.items():
                tot[b] += v
        n = len(items)
        out[t] = {b: round(v / n / 60.0, 4) for b, v in sorted(tot.items())}
        out[t]["n"] = n
    return out


def _ff_paper_stats(post, fab, pct) -> dict[str, Any]:
    """논문 규약의 Flow Factor (`Route.rpt_paper`가 분모).

    논문 IV-B절이 **regular만** 보므로 그쪽을 먼저 낸다. 전체도 같이 담는 건
    두 값을 나란히 놓아야 "hot을 섞으면 얼마나 밀리나"가 보이기 때문이다.

    `rpt_paper`가 0이면(= fixture) 빈 dict를 돌려준다 — 0으로 나누지 않는다.
    """
    routes = getattr(getattr(fab, "data", None), "routes", {}) or {}

    def ffs(items) -> list[float]:
        out = []
        for c in items:
            r = routes.get(c.product)
            if r is not None and r.rpt_paper > 0:
                out.append(c.cycle_time / r.rpt_paper)
        return out

    reg = ffs([c for c in post if c.lot_type == "regular"])
    if not reg:
        return {}
    qs = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
    out: dict[str, Any] = {
        "regular_pctl": {str(int(q * 100)): pct(reg, q) for q in qs},
        "regular_mean": round(statistics.fmean(reg), 4),
        "regular_n": len(reg),
        # 논문 Fig. 3 은 P0 과 P100 도 그린다. 분위수 목록에 0/100 을 넣지
        # 않고 min/max 로 따로 두는 이유는 그 둘이 **보간이 아니라 실제 lot**
        # 이기 때문이다 — P0 은 거의 안 기다리고 통과한 가장 빠른 lot 이다.
        "regular_min": round(min(reg), 4),
        "regular_max": round(max(reg), 4),
    }
    # **종류마다 분위수를 낸다.** 평균 한 점으로는 분포 모양을 못 본다.
    # regular의 FF가 납기 난수 z(U(0.8,1.2))를 그대로 베낀 모양인 것이
    # 드러났는데(G-25), hot도 그런지가 "CR 지배가 전역인가"를 가른다.
    for t in ("hot", "superhot"):
        v = ffs([c for c in post if c.lot_type == t])
        if v:
            out[f"{t}_mean"] = round(statistics.fmean(v), 4)
            out[f"{t}_n"] = len(v)
            out[f"{t}_pctl"] = {str(int(q * 100)): pct(v, q) for q in qs}
    allv = ffs(post)
    out["all_mean"] = round(statistics.fmean(allv), 4)
    out["all_p50"] = pct(allv, 0.50)
    return out


def _ff_cross(items, ffn) -> dict[str, dict[str, list[float]]]:
    """제품 × lot 종류로 FF를 모은다 (논문 Table II의 모양).

    모듈 수준에 두는 이유는 `snapshot`이 이미 길어서다. 순수 함수다.
    """
    out: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for c in items:
        if c.theoretical > 0:
            out[c.product][c.lot_type].append(ffn(c))
    return out
WEEK_MINUTES = 7 * 24 * 60

# 99% 양측 정규 분위수. 09 7절이 요구하는 "binomial 99 % interval".
Z_99 = 2.5758293035489004


def binomial_fit(tally: dict, prob_of, z: float = Z_99) -> dict:
    """step별 관측 비율이 **자기 n의** 이항 구간 안에 있는지 (09 7절).

    비율 하나에 고정 폭을 쓰면 정상 구현에서도 깨진다: 50% 비율을 500번
    관측하면 ±2pp 밖으로 나갈 확률이 3분의 1이고, 그런 step이 수백 개다.
    그래서 step마다 자기 n으로 구간을 만든다.

    세 갈래로 나눈다.

    * **확률이 0% 또는 100%**  — 분산이 없다. 구간이 아니라 **정확히** 맞아야
      한다. 여기서 어긋나면 부등호가 뒤집혔거나 확률을 분수로 쓴 것이다
      (09 5절의 두 실패 모드).
    * **표본이 모자람** (`n*p < 5` 또는 `n*(1-p) < 5`) — 정규 근사가 성립하지
      않는다. 통과도 실패도 아니고 **insufficient**로 따로 센다 (7절).
    * 나머지 — `|k - np| <= z*sqrt(np(1-p))` 인지 본다.

    구간 밖 개수는 0이 기대값이 아니다. 99% 구간이므로 **1%가 밖에 있는 것이
    정상**이다. 그 기대값도 같이 돌려준다 — 판정은 호출자가 한다.
    """
    inside = outside = insufficient = 0
    exact_bad: list[str] = []
    worst: list[tuple[float, str, int, int, float]] = []

    for key, (n, k) in tally.items():
        p = prob_of(key) / 100.0
        if p <= 0.0 or p >= 1.0:
            want = n if p >= 1.0 else 0
            if k != want:
                exact_bad.append(f"{key}: p={p * 100:g}% 인데 {k}/{n}")
            continue
        if n * p < 5.0 or n * (1.0 - p) < 5.0:
            insufficient += 1
            continue
        sd = (n * p * (1.0 - p)) ** 0.5
        zs = abs(k - n * p) / sd
        if zs <= z:
            inside += 1
        else:
            outside += 1
            worst.append((zs, f"{key[0]} step {key[1]}", n, k, p * 100.0))

    judged = inside + outside
    worst.sort(reverse=True)
    return {
        "steps": len(tally),
        "judged": judged,
        "inside": inside,
        "outside": outside,
        # 99% 구간이므로 판정한 step의 1%는 밖에 있는 것이 정상이다.
        "expected_outside": round(judged * 0.01, 2),
        "insufficient": insufficient,
        "exact_violations": len(exact_bad),
        "_exact_bad": exact_bad[:5],
        "_worst": worst[:8],
    }


def sampling_fit(fab) -> dict:
    """실행의 sampling 집계를 route의 확률과 대조한다."""
    def prob(key):
        product, i = key
        return fab.data.routes[product].steps[i].sampling_prob
    return binomial_fit(fab.sampling_tally, prob)


def rework_fit(fab) -> dict:
    def prob(key):
        product, i = key
        return fab.data.routes[product].steps[i].rework_prob
    return binomial_fit(fab.rework_tally, prob)


def _fit_scalars(fit: dict) -> dict:
    """스냅샷에 넣을 부분. 밑줄로 시작하는 진단용 목록은 뺀다 —
    `compare`가 phase 간 diff를 뜨는 대상이므로 스칼라만 남긴다."""
    return {k: v for k, v in fit.items() if not k.startswith("_")}


def _step_accounting_bad(items, fab) -> int:
    """경로 보존 항등식이 깨진 lot 수 (09 7절, Phase 12 R8).

        수행 + 건너뜀 == (route 길이 - 출발 step) + rework_span

    `- 출발 step`이 Phase 12가 넓힌 부분이다. 초기 WIP lot은 route 중간에서
    시작하므로 앞부분을 걸은 적이 없다. 보통 lot은 0이라 09의 식과 같다.
    """
    return sum(
        1 for c in items
        if c.steps_executed + c.steps_skipped
        != (len(fab.data.routes[c.product].steps) - c.start_step_index)
        + c.rework_span)


@dataclass(slots=True)
class Completion:
    """완료 lot 집계. 개별 step/구간 이력은 상세 SQLite에 저장한다."""
    lot_id: str
    product: str
    lot_type: str
    is_initial_wip: bool
    # Phase 12 R8 — 이 lot이 **출발한** step. 초기 WIP만 0이 아니다.
    # 경로 보존을 route의 처음이 아니라 여기서부터 재야 한다.
    start_step_index: int
    release_time: float
    fab_out_time: float
    cycle_time: float
    theoretical: float
    due_date: float
    on_time: bool
    lateness: float
    wafers: int
    steps_executed: int
    # Phase 09 — step 계산 항등식의 나머지 항 (09 7절 "Step accounting").
    #     수행 + 건너뜀 == route 길이 + rework_span
    steps_skipped: int
    rework_count: int
    rework_span: int
    buckets: dict[str, float]


@dataclass(slots=True)
class WipSample:
    t: float
    live: int
    released: int
    completed: int


class KPICollector:
    def __init__(self, env, *, warmup_minutes: float = 0.0, detail_dir: Path | None = None,
                 detail_level: str = 'full', trace_types=None):
        if detail_level not in {'lean','full'}:
            raise ValueError('detail_level은 lean/full이어야 합니다.')
        self.detail_level=detail_level
        # 디스패치 판단 로그 (dispatch_audit.py). full에서만 켜고, FabModel이 이 값을 보고 hook을 부른다.
        self.audit_dispatch=detail_level=='full'
        self._decision_seq=0
        self.trace_types=frozenset(config.KPI_TRACE_TYPES if trace_types is None else trace_types)
        if self.trace_types-{'regular','hot','superhot'}:
            raise ValueError('trace_types는 regular/hot/superhot이어야 합니다.')
        self.env = env
        self.warmup = warmup_minutes

        self.completions: list[Completion] = []
        self.n_released = 0
        self.n_completed = 0
        self.wafers_released = 0
        self.wafers_released_post = 0
        # Phase 12 — t=0에 이미 fab 안에 있던 lot. **투입과 따로 센다.**
        # 이들을 `n_released`에 넣으면 0주차 투입률이 5배로 뛰어 주간 시계열이
        # 망가진다. 투입된 적이 없고 그냥 거기 있었던 것이다 (R6).
        self.n_initial_wip = 0
        self.wafers_initial_wip = 0

        # 주 단위 시계열
        self.weekly_wip: list[int] = []
        # toolgroup -> 주마다 줄 서 있던 lot 수. 병목 타임라인 히트맵용.
        # 106 x 417주 = 4.4만 개라 스냅샷이 0.3 MB쯤 된다 — 주 1회 관찰이라
        # 실행 비용은 없다.
        self.weekly_queue_by_tg: dict[str, list[int]] = defaultdict(list)
        self.weekly_completions: dict[int, int] = defaultdict(int)   # wafer 수
        self.weekly_releases: dict[int, int] = defaultdict(int)      # wafer 수
        self.weekly_queue: dict[str, list[int]] = defaultdict(list)
        # area별 평균 WIP (레퍼런스 stnfam 보고서의 `WIPLOTAVG`).
        # 합과 표본 수만 들고 있으면 평균이 나온다 — 8년이면 417 표본이다.
        self.area_wip_sum: dict[str, float] = defaultdict(float)
        self.area_wip_samples = 0

        self.wip_samples: list[WipSample] = []
        self._wip_integral = 0.0        # 시간가중 평균 WIP 용 (전체 구간)
        self._wip_integral_post = 0.0   # warm-up 이후만
        self._wip_last_t = 0.0
        self._wip_last_v = 0

        # tool_id -> warm-up 경계에서의 누계 (`mark_warmup`). 비어 있으면
        # 기준선이 없다는 뜻이고, 장비 지표는 전체 구간으로 보고된다 —
        # warm-up이 0인 실행이 그 경우다.
        self._tool_base: dict[str, dict[str, float]] = {}
        self.details = _DetailStore(detail_dir)
        self._visits = {}
        self._visit_seq = self._batch_seq = self._down_seq = 0
        self._batches = {}
        self._down = {}
        self._work = {}
        self._work_context = {}
        self._queue_state_seq = 0
        self._interval_acc = {}
        self._idle_minutes = {}
        self._interval_acc_time = None
        self._work_seq = self._event_seq = 0
        self._step_catalog = {}
        self.flow_cube=defaultdict(_new_flow)
        self._lean_wip_visits=set()
        self._work_totals=defaultdict(float)
        self._physical_union={}
        self._visit_accounting_violations=0
        self._batch_quantity_state={}
        self._batch_quantity_totals=defaultdict(lambda:[0.0,0.0])
        self.flow = {d: defaultdict(_new_flow) for d in
                     ("area", "toolgroup", "route_step", "station", "product", "lot_type")}

    def _flow_keys(self, visit):
        return (("area", visit["area"]), ("toolgroup", visit["toolgroup"]),
                ("route_step", f"{visit['product']}:{visit['step_index']}"),
                ("product", visit["product"]), ("lot_type", visit["lot_type"]))

    def _traced(self, lot):
        return self.detail_level=='full' or lot.lot_type in self.trace_types

    def _cube_key(self, visit):
        return (visit['product'],visit['step_index'],visit['toolgroup'],visit['lot_type'])

    def _union_touch(self, station, work=0, down=0):
        # Monotonic start/end events integrate the physical union without raw history.
        state=self._physical_union.setdefault(station,[self.env.now,0,0,0.0])
        t,nwork,ndown,total=state
        if nwork or ndown:
            total+=max(0,self.env.now-max(t,self.warmup))
        state[:]=[self.env.now,nwork+work,ndown+down,total]
        if state[1]<0 or state[2]<0:
            raise RuntimeError('물리적 작업/정지 구간 시작·종료가 맞지 않습니다.')

    def close(self):
        """Release the working DB after saving the final snapshot."""
        self.details.close()

    def begin_visit(self, lot, step, tg):
        self._step_catalog.setdefault(f"{lot.product}:{step.step_index}",
            {"product": lot.product, "step_index": step.step_index,
             "step_id": step.step_id, "description": step.description,
             "toolgroup": tg.name, "area": step.area})
        self._visit_seq += 1
        visit = dict(visit_id=self._visit_seq, lot_id=lot.lot_id,
                     product=lot.product, lot_type=lot.lot_type,
                     step_index=step.step_index, step_id=step.step_id,
                     description=step.description, area=step.area,
                     toolgroup=tg.name, station=None, batch_id=None,
                     start_min=self.env.now, buckets=dict.fromkeys(BUCKETS, 0.0),
                     station_buckets=dict.fromkeys(BUCKETS, 0.0))
        self._visits[lot.lot_id] = visit
        if self.env.now >= self.warmup:
            for dim, key in self._flow_keys(visit):
                self.flow[dim][key]["arrivals"] += 1
            if self.detail_level=='lean':
                self.flow_cube[self._cube_key(visit)]['arrivals']+=1

    def record_segment(self, lot, bucket, start, end):
        visit = self._visits.get(lot.lot_id)
        if visit is None or end <= start:
            return
        visit["buckets"][bucket] += end - start
        if visit["station"]:
            visit["station_buckets"][bucket] += end - start
        if self._traced(lot):
            self.details.add("segments", (visit["visit_id"], bucket, start, end,
                                          visit["station"], visit["batch_id"]))
        duration = max(0.0, end - max(start, self.warmup))
        if duration:
            keys = list(self._flow_keys(visit))
            if visit["station"]:
                keys.append(("station", visit["station"]))
            for dim, key in keys:
                self.flow[dim][key]["buckets_min"][bucket] += duration
            if self.detail_level=='lean':
                self.flow_cube[self._cube_key(visit)]['buckets_min'][bucket]+=duration

    def assign_station(self, lot, tool, batch_id=None):
        # Flush the queue interval before assigning a physical station.
        if lot._bucket is not None:
            lot.enter(lot._bucket, self.env.now)
        visit = self._visits[lot.lot_id]
        visit["station"] = tool.tool_id
        visit["batch_id"] = batch_id
        if self.detail_level=='full':
            self._work_context[(tool.tool_id, self.env.active_process)] = (lot,)
        if self.env.now >= self.warmup:
            self.flow["station"][tool.tool_id]["arrivals"] += 1

    def _visit_row(self, visit, status, target=None):
        return tuple(visit[k] for k in ("visit_id", "lot_id", "product",
            "step_index", "step_id", "description", "area", "toolgroup",
            "station", "batch_id", "start_min")) + (
            self.env.now, status, target) + tuple(visit["buckets"][b] for b in BUCKETS)

    def end_visit(self, lot, status, rework_target=None):
        if lot._bucket is not None:
            lot.enter(lot._bucket, self.env.now)
        visit = self._visits.pop(lot.lot_id)
        self._visit_accounting_violations+=abs(sum(visit['buckets'].values())-(self.env.now-visit['start_min']))>1e-6
        if self._traced(lot):
            self.details.add("visits", self._visit_row(visit, status, rework_target))
        elif visit['visit_id'] in self._lean_wip_visits:
            self.details.flush()
            self.details.db.execute('DELETE FROM visits WHERE visit_id=?',(visit['visit_id'],))
            self.details.db.execute('DELETE FROM segments WHERE visit_id=?',(visit['visit_id'],))
            self._lean_wip_visits.discard(visit['visit_id'])
        if self.env.now >= self.warmup:
            keys = list(self._flow_keys(visit))
            if visit["station"]:
                keys.append(("station", visit["station"]))
            for dim, key in keys:
                rec = self.flow[dim][key]
                rec[status] += 1
                rec["reworks"] += rework_target is not None
                # Comparable wait observations: visits that began after warm-up.
                if status == "completed" and visit["start_min"] >= self.warmup:
                    wait = sum(visit["buckets"][b] for b in WAIT_BUCKETS)
                    if dim == "station":
                        wait = visit["station_buckets"]["wait_down"]
                    rec["wait_n"] += 1
                    rec["wait_sum"] += wait
                    rec["wait_sq"] += wait * wait
                    rec["wait_max"] = max(rec["wait_max"], wait)
            if self.detail_level=='lean':
                rec=self.flow_cube[self._cube_key(visit)]
                rec[status]+=1;rec['reworks']+=rework_target is not None
                if status=='completed' and visit['start_min']>=self.warmup:
                    wait=sum(visit['buckets'][b] for b in WAIT_BUCKETS)
                    rec['wait_n']+=1;rec['wait_sum']+=wait;rec['wait_sq']+=wait*wait
                    rec['wait_max']=max(rec['wait_max'],wait)

    def _lot_row(self, lot, status):
        now = self.env.now
        self.details.add("lot_definitions", (lot.lot_id,
            lot.route.suffix_raw_pt[0], getattr(lot.route, "rpt_paper", 0),
            lot.start_step_index, lot.rework_span))
        buckets = dict(lot.buckets)
        if lot._bucket is not None:
            buckets[lot._bucket] += now - lot._bucket_since
        return (lot.lot_id, lot.product, lot.lot_type, lot.priority,
                lot.wafers_per_lot, int(lot.is_initial_wip), lot.release_time,
                lot.due_date, now, status, lot.current_step_index,
                now - lot.release_time, now - lot.due_date, lot.steps_executed,
                lot.steps_skipped, lot.rework_count, json.dumps(buckets))

    def begin_batch(self, tg, tool, step, members, wafers):
        self._batch_seq += 1
        batch_id = self._batch_seq
        waits = [self.env.now - (m.batch_since if m.batch_since is not None
                                else self.env.now) for m in members]
        self._batches[batch_id] = (batch_id, tg.name, tool.tool_id,
            members[0].product, step.step_index,
            json.dumps([m.lot_id for m in members]), len(members), wafers,
            step.batch_min, step.batch_max, self.env.now)
        # Fill/wait are batch observations, never multiplied by member count.
        self._batches[batch_id] += (100.0 * wafers / step.batch_max,
                                   statistics.fmean(waits), max(waits))
        for m in members:
            self.assign_station(m, tool, batch_id)
        if self.detail_level=='full':
            self._work_context[(tool.tool_id, self.env.active_process)] = tuple(members)
        return batch_id

    def release_work_context(self, tool):
        self._work_context.pop((tool.tool_id, self.env.active_process), None)

    def _batch_quantity_touch(self, key):
        t,ready,counts=self._batch_quantity_state[key]
        duration=max(0,self.env.now-max(t,self.warmup))
        if duration:
            for kind,n in counts.items():
                self._batch_quantity_totals[key+(kind,)][int(ready)]+=duration*n
        self._batch_quantity_state[key][0]=self.env.now

    def note_batch_queue(self, tg, key, minimum, *, added=None, removed=()):
        """Observe quantity eligibility; no dispatch decision or RNG consumption."""
        if self.details.closed:return  # Generator teardown after the final saved result.
        if self.detail_level=='lean':
            state_key=(tg.name,key[0],key[1])
            self._batch_quantity_state.setdefault(state_key,[self.env.now,False,defaultdict(int)])
            self._batch_quantity_touch(state_key)
            state=self._batch_quantity_state[state_key]
            if added is not None:state[2][added.lot_type]+=1
            for lot in removed:state[2][lot.lot_type]-=1
            if sum(state[2].values())!=len(tg.batch_queue[key]) or any(n<0 for n in state[2].values()):
                raise RuntimeError('배치 대기 종류별 수량 누계와 대기열이 일치하지 않습니다.')
            state[1]=tg.batch_wafers[key]>=minimum
            return
        self._queue_state_seq += 1
        self.details.add("batch_queue_state", (self._queue_state_seq,
            self.env.now, tg.name, key[0], key[1], len(tg.batch_queue[key]),
            tg.batch_wafers[key], minimum))

    def _batch_row(self, rec, status):
        return rec[:11] + (self.env.now, status) + rec[11:]

    def end_batch(self, batch_id):
        rec = self._batches.pop(batch_id)
        self.details.add("batches", self._batch_row(rec, "completed"))

    def begin_downtime(self, tool, kind, name):
        if self.detail_level=='lean':
            self._union_touch(tool.tool_id,down=1)
        self._down_seq += 1
        self._down[self._down_seq] = (self._down_seq, tool.tool_id,
            tool.group.name, tool.group.spec.area, kind, name, self.env.now)
        return self._down_seq

    def record_event(self, lot, kind, **attributes):
        if not self._traced(lot):
            return
        attributes.setdefault("step_index", lot.current_step_index)
        visit = self._visits.get(lot.lot_id)
        if visit is not None:
            attributes.setdefault("toolgroup", visit["toolgroup"])
            attributes.setdefault("station", visit["station"])
        self._event_seq += 1
        self.details.add("events", (self._event_seq, lot.lot_id, lot.product,
                                    kind, self.env.now, json.dumps(attributes)))

    def record_dispatch(self, row, lot, top=None):
        """디스패치 판단 1건 (dispatch_audit.py가 만든 행). 방문 ID만 여기서 붙인다."""
        visit = self._visits.get(lot.lot_id)
        upper = self._visits.get(top.lot_id) if top is not None else None
        self._decision_seq += 1
        row = dict(row, decision_id=self._decision_seq,
                   visit_id=visit["visit_id"] if visit else None,
                   top_visit_id=upper["visit_id"] if upper else None)
        self.details.add("dispatch_log", tuple(row.get(n) for n, _ in DISPATCH_COLUMNS))

    def begin_work(self, tool, attr):
        if self.detail_level=='lean':
            self._union_touch(tool.tool_id,work=1)
        self._work_seq += 1
        key = (tool.tool_id, self.env.active_process)
        self._work[key] = (self._work_seq, tool.tool_id, tool.group.name,
                           attr, self.env.now)
        for lot in self._work_context.get(key, ()):
            visit = self._visits.get(lot.lot_id)
            if visit is not None:
                self.details.add("work_members", (self._work_seq, visit["visit_id"],
                    lot.lot_id, lot.product, lot.lot_type, lot.wafers_per_lot,
                    visit["batch_id"]))

    def end_work(self, tool):
        if self.details.closed:
            return
        if self.env.active_process is None and (tool.tool_id, None) not in self._work:
            return  # SimPy generator cleanup outside the event loop.
        rec = self._work.pop((tool.tool_id, self.env.active_process))
        if self.detail_level=='lean':
            self._work_totals[(tool.tool_id,rec[3])]+=max(0,self.env.now-max(rec[4],self.warmup))
            self._union_touch(tool.tool_id,work=-1)
        else:
            self.details.add("station_work", rec + (self.env.now, "completed"))

    def end_downtime(self, event_id):
        if self.detail_level=='lean':
            self._union_touch(self._down[event_id][1],down=-1)
        self.details.add("downtime", self._down.pop(event_id) +
                         (self.env.now, "completed"))

    def detailed_snapshot(self, fab):
        now = self.env.now
        span = max(0.0, now - self.warmup)
        for lot in fab.live_lots.values():
            visit = self._visits.get(lot.lot_id)
            if visit is not None and not self._traced(lot) and lot._bucket is not None and lot._bucket_since<now:
                self.details.add('segments',(visit['visit_id'],lot._bucket,lot._bucket_since,now,
                                             visit['station'],visit['batch_id']))
            if lot._bucket is not None:
                lot.enter(lot._bucket, now)
            self.details.add("lots", self._lot_row(lot, "wip"))
            visit = self._visits.get(lot.lot_id)
            if visit is not None:
                self.details.add("visits", self._visit_row(visit, "censored"))
                if not self._traced(lot):
                    self._lean_wip_visits.add(visit['visit_id'])
        for rec in self._batches.values():
            self.details.add("batches", self._batch_row(rec, "censored"))
        for rec in self._down.values():
            self.details.add("downtime", rec + (now, "censored"))
        if self.detail_level=='full':
            for rec in self._work.values():
                self.details.add("station_work", rec + (now, "censored"))
        self.details.flush()
        downtime = defaultdict(dict)
        work_time = defaultdict(dict)
        if self.detail_level=='lean':
            for (station,bucket),minutes in self._work_totals.items():
                work_time[station][bucket]=minutes
            for rec in self._work.values():
                work_time[rec[1]][rec[3]]=work_time[rec[1]].get(rec[3],0)+max(0,now-max(rec[4],self.warmup))
        else:
            for station, bucket, minutes in self.details.db.execute(
                    "SELECT station,bucket,SUM(MAX(0,end_min-MAX(start_min,?))) "
                    "FROM station_work WHERE end_min>? GROUP BY station,bucket",
                    (self.warmup, self.warmup)):
                work_time[station][bucket] = minutes
        for station, kind, minutes, count in self.details.db.execute(
                "SELECT station,kind,SUM(MAX(0,end_min-MAX(start_min,?))),"
                "SUM(start_min>=?) FROM downtime WHERE end_min>? GROUP BY station,kind",
                (self.warmup, self.warmup, self.warmup)):
            downtime[station][kind] = (minutes, count)
        self._interval_acc = {}
        for tg in fab.toolgroups.values():
            for tool in tg.tools:
                station = tool.tool_id
                for attr in ("proc_time", "setup_time", "loadunload_time"):
                    self._interval_acc[(station, attr)] = work_time[station].get(attr, 0.0)
                for kind, attr, events in (("breakdown", "down_time", "down_events"),
                                          ("pm", "pm_time", "pm_events_count")):
                    minutes, count = downtime[station].get(kind, (0, 0))
                    self._interval_acc[(station, attr)] = minutes
                    self._interval_acc[(station, events)] = count
        # Work and downtime can overlap during uninterruptible load/unload.
        # Idle is the complement of their union, not 1-util-unavailable.
        occupied = defaultdict(float)
        if self.detail_level=='lean':
            for station in self._physical_union:
                self._union_touch(station)
                occupied[station]=self._physical_union[station][3]
        else:
            previous, right = None, None
            for station, a, b in self.details.db.execute(
                    "SELECT station,MAX(start_min,?),end_min FROM ("
                    "SELECT station,start_min,end_min FROM station_work UNION ALL "
                    "SELECT station,start_min,end_min FROM downtime) WHERE end_min>? "
                    "ORDER BY station,start_min,end_min", (self.warmup, self.warmup)):
                if station != previous:
                    previous, right = station, None
                occupied[station] += b-a if right is None else max(0, b-max(a, right))
                right = b if right is None else max(right, b)
        self._idle_minutes = {tool.tool_id: max(0, span-occupied[tool.tool_id])
                              for tg in fab.toolgroups.values() for tool in tg.tools}
        self._interval_acc_time = now
        out = {}
        live_counts = defaultdict(lambda: defaultdict(int))
        for visit in self._visits.values():
            for dim, key in self._flow_keys(visit):
                live_counts[dim][key] += 1
            if visit["station"]:
                live_counts["station"][visit["station"]] += 1
        for dim, rows in self.flow.items():
            out[dim] = {}
            for key, rec in sorted(rows.items()):
                times = rec["buckets_min"]
                wait = sum(times[b] for b in WAIT_BUCKETS)
                n = rec["wait_n"]
                mean = rec["wait_sum"] / n if n else None
                out[dim][key] = {
                    **{k: rec[k] for k in ("arrivals", "completed", "skipped", "reworks")},
                    "censored": live_counts[dim][key],
                    "bucket_lot_h": {b: round(v / 60, 6) for b, v in times.items()},
                    "elapsed_lot_h": round(sum(times.values()) / 60, 6),
                    "wait_lot_h": round(wait / 60, 6),
                    "mean_wip_lots": sum(times.values()) / span if span else None,
                    "mean_waiting_lots": wait / span if span else None,
                    "wait_observations": n,
                    "wait_mean_h": mean / 60 if n else None,
                    "wait_std_h": max(0.0, rec["wait_sq"] / n - mean**2)**.5 / 60 if n else None,
                    "wait_max_h": rec["wait_max"] / 60 if n else None,
                }
        stations = {}
        for tg in fab.toolgroups.values():
            for tool in tg.tools:
                tool.close_intervals()
                denom = max(0.0, self.measured_span())
                values = {attr: self.tool_acc(tool, attr) for attr in TOOL_ACCUMULATORS}
                for attr in ("proc_time", "setup_time", "loadunload_time"):
                    values[attr] = work_time[tool.tool_id].get(attr, 0.0)
                for kind, attr, events in (("breakdown", "down_time", "down_events"),
                                           ("pm", "pm_time", "pm_events_count")):
                    values[attr], values[events] = downtime[tool.tool_id].get(kind, (0, 0))
                stations[tool.tool_id] = {"toolgroup": tg.name, "area": tg.spec.area,
                    "capacity": tool.resource.capacity, "jobs_now": len(tool.resource.users),
                    "down_now": tool.is_down,
                    "util_pct": 100 * values["work_time"] / denom if denom else None,
                    "occupancy_pct": 100 * values["busy_time"] / denom if denom else None,
                    "unavail_pct": 100 * values["unavailable_time"] / denom if denom else None,
                    "idle_pct": 100 * self._idle_minutes[tool.tool_id] / denom if denom else None,
                    "down_events": int(values["down_events"]),
                    "pm_events": int(values["pm_events_count"]),
                    **{k + "_h": v / 60 for k, v in values.items() if k.endswith("time")}}
        meta = {"schema_version": SCHEMA_VERSION, "end_min": now,
                "sim_start": config.SIM_START.isoformat(),
                "warmup_min": self.warmup, "time_unit": "minute",
                "flow_window": "time clipped to [warmup,end]; includes initial WIP and open visits",
                "wait_window": "completed visits starting at/after warmup; station wait is on-tool wait_down",
                "station": "physical tool ID; route_step = product:zero-based step_index",
                "lot_h": "lot-hours; shared batch process appears once per member",
                "detail_window": "whole run; censored end_min is observation end, not completion"}
        out["route_step"] = {key: {**self._step_catalog[key], **value}
                             for key, value in out["route_step"].items()}
        meta['collection']={
            'level':self.detail_level,'trace_types':sorted(self.trace_types) if self.detail_level=='lean' else ['regular','hot','superhot'],
            'all_lots':True,'all_batches':True,'all_downtime':True,
            'full_visits':self.detail_level=='full','station_work':self.detail_level=='full',
            'work_members':self.detail_level=='full','batch_queue_state':self.detail_level=='full',
            'batch_wait_summary':self.detail_level=='lean',
            'flow_cube':self.detail_level=='lean','final_wip_states':True,
            'dispatch_log':self.audit_dispatch,
            'scope':'all operational aggregates; selected lot-type traces plus final WIP states' if self.detail_level=='lean' else 'all detailed histories'}
        meta['visit_accounting']={'visits':self._visit_seq,'violations':self._visit_accounting_violations+
            sum(abs(sum(v['buckets'].values())-(now-v['start_min']))>1e-6 for v in self._visits.values()),
            'scope':'whole run; all closed and current visits, independently of trace coverage'}
        if self.detail_level=='lean':
            for key in self._batch_quantity_state:self._batch_quantity_touch(key)
            self.details.db.execute('DELETE FROM batch_wait_summary')
            self.details.db.executemany('INSERT INTO batch_wait_summary VALUES (?,?,?,?,?,?)',
                [key+tuple(value) for key,value in self._batch_quantity_totals.items()])
            live_cube=defaultdict(int)
            for visit in self._visits.values():
                live_cube[self._cube_key(visit)]+=1
            self.details.db.execute('DELETE FROM flow_cube')
            rows=[tuple(key)+tuple(rec[k] for k in ('arrivals','completed','skipped','reworks'))+
                  (live_cube[key],)+tuple(rec[k] for k in ('wait_n','wait_sum','wait_sq','wait_max'))+
                  tuple(rec['buckets_min'][b] for b in BUCKETS) for key,rec in self.flow_cube.items()]
            self.details.db.executemany('INSERT INTO flow_cube VALUES ('+','.join('?' for _ in range(24))+')',rows)
        path = self.details.save(meta)
        return {"definitions": meta, "database": str(path), "flow": out,
                "stations": stations, "visits": self._visit_seq,
                "batches": self._batch_seq, "downtime_events": self._down_seq}

    # -- warm-up 경계 --------------------------------------------------------

    def mark_warmup(self, fab: "FabModel") -> None:
        """warm-up이 끝난 순간의 장비 누계를 기준선으로 뜬다.

        **이게 없으면 한 스냅샷 안에 기준이 둘이 된다.** lot 쪽은 이미 전부
        warm-up 이후다 (`_post_warmup`, `mean_wip(post_warmup=True)`,
        `_weekly_rate`). 그런데 `Tool`의 누계는 t=0부터 쌓이므로 UTIL%/SETUP%만
        **빈 fab이 차오르는 구간을 포함한** 값이었다.

        **크기는 구성에 달렸다. 재 봤다 (2026-09-22, seed 42, 8년/warm-up 1년):**

          explicit + 초기 WIP (레퍼런스 구성)   area별 0.01~0.28 pp
          빈 fab (parametric, 부하 스윕)        아직 안 쟀다 — 큰 쪽은 여기다

        앞쪽이 작은 이유는 **차오르는 구간이 애초에 없기** 때문이다. 초기
        WIP 2,156 lot이 정상 WIP(2,146)과 거의 같아 t=0부터 이미 정상상태다.
        처음에 2pp쯤으로 적었는데 그건 빈 fab 기준이었고, 그 구성에만 맞는다.

        **이 저장소가 바로 그 빈 fab 쪽이다** — `reference/WIP.txt`가 없어
        초기 WIP이 항상 0이고, t=0이 빈 fab이다. 13주 시점에도 정상값의
        82~93%이고(`run.py`의 warm-up 주석), 그 구간이 1/8 가중치로 섞인다.

        **크기와 무관하게 분리가 맞다.** 같은 표에 기준이 둘이면 읽는 쪽이
        보정할 방법이 없고, 논문 Table IV 비교는 0.5pp 단위다.

        **DOWN%/PM%는 거의 안 움직인다** — TTF 시계가 부하와 무관하게 자유
        주행하므로(03 R2) warm-up 구간에도 같은 비율로 난다. 바뀌는 것은
        부하에 딸린 UTIL/SETUP/idle 쪽이다.

        난수도 이벤트도 쓰지 않는다. `main.run_with_data`가 `env.run`을 경계에서
        한 번 끊고 이 함수를 부르는데, **끊는 것 자체는 결과를 안 바꾼다** —
        SimPy는 `run(until=t)` 뒤에 이어서 돌릴 수 있고 프로세스가 죽지 않는다.
        그래서 같은 seed에서 KPI가 한 비트도 달라지지 않아야 하고, 달라지면
        이 함수가 부수효과를 낸 것이다.
        """
        for tg in fab.toolgroups.values():
            for tool in tg.tools:
                # 경계를 걸친 작업·정지 구간을 여기서 쪼갠다. 안 쪼개면 그
                # 구간 전체가 끝나는 시점 기준으로 post 몫에 들어간다.
                tool.close_intervals()
                self._tool_base[tool.tool_id] = {
                    a: getattr(tool, a) for a in TOOL_ACCUMULATORS}

    def tool_acc(self, tool, attr: str) -> float:
        """이 장비의 `attr` 누계 중 **측정 구간 몫**.

        기준선이 없으면 전체 값이다 — warm-up 0인 실행에서 예전과 같은 숫자가
        나오는 이유다.
        """
        if self._interval_acc_time == self.env.now and (tool.tool_id, attr) in self._interval_acc:
            return self._interval_acc[(tool.tool_id, attr)]
        base = self._tool_base.get(tool.tool_id)
        value = getattr(tool, attr)
        return value if base is None else value - base.get(attr, 0.0)

    def measured_span(self) -> float:
        """장비 지표의 분모가 되는 구간 길이 (분).

        `tool_acc`와 **반드시 짝**이다. 한쪽만 바꾸면 비율이 조용히 틀린다.
        """
        return self.env.now - (self.warmup if self._tool_base else 0.0)

    # -- 기록 ---------------------------------------------------------------

    def record_release(self, lot: "Lot") -> None:
        lot.kpi = self
        self.details.add('release_policy',(lot.lot_id,lot.due_base_min,lot.due_z))
        self.n_released += 1
        self.wafers_released += lot.wafers_per_lot
        if lot.release_time >= self.warmup:
            self.wafers_released_post += lot.wafers_per_lot
        self.weekly_releases[int(lot.release_time // WEEK_MINUTES)] += lot.wafers_per_lot

    def record_initial_wip(self, lot: "Lot") -> None:
        """t=0의 초기 WIP lot 1건 (Phase 12 R6).

        `record_release`를 쓰지 않는 이유: 이들은 투입된 것이 아니다.
        주간 투입 시계열에 넣으면 0주차만 5배로 솟아 `wspw`가 거짓말을 한다.
        WIP에는 들어가고(live_lots에 있으므로 자동), 완료하면 처리량에도
        들어간다(`record_completion`은 구분하지 않는다) — 그것이 R6이다.
        """
        lot.kpi = self
        self.n_initial_wip += 1
        self.wafers_initial_wip += lot.wafers_per_lot

    def record_completion(self, lot: "Lot") -> None:
        self.details.add("lots", self._lot_row(lot, "completed"))
        self.n_completed += 1
        ct = lot.cycle_time
        theo = lot.theoretical_cycle_time
        self.completions.append(Completion(
            lot_id=lot.lot_id,
            product=lot.product,
            lot_type=lot.lot_type,
            is_initial_wip=lot.is_initial_wip,
            start_step_index=lot.start_step_index,
            release_time=lot.release_time,
            fab_out_time=lot.fab_out_time,
            cycle_time=ct,
            theoretical=theo,
            due_date=lot.due_date,
            on_time=lot.fab_out_time <= lot.due_date,
            lateness=lot.fab_out_time - lot.due_date,
            wafers=lot.wafers_per_lot,
            steps_executed=lot.steps_executed,
            steps_skipped=lot.steps_skipped,
            rework_count=lot.rework_count,
            rework_span=lot.rework_span,
            buckets=dict(lot.buckets),
        ))
        self.weekly_completions[int(lot.fab_out_time // WEEK_MINUTES)] += lot.wafers_per_lot

    def _sample_area_wip(self, fab: "FabModel") -> None:
        """지금 각 area에 lot이 몇 개 있는가. warm-up 이후만 센다.

        **lot의 위치는 "현재 step의 area"다.** 대기 중이든 처리 중이든 이동
        중이든 그 area에 속한 것으로 본다. 예전 코드는 `waiting_count()`만
        세어서 **처리 중인 lot이 통째로 빠졌고**, 게다가 실행이 끝나는 순간
        한 번만 쟀다. 레퍼런스의 `WIPLOTAVG`는 그 둘 다와 다른 양이다.

        이동 중인 lot은 목적지 쪽에 세어진다 — `current_step_index`가 이미
        다음 step을 가리키기 때문이다. WIP을 "route의 어디까지 왔나"로 읽으면
        그쪽이 맞다.
        """
        if self.env.now < self.warmup:
            return
        per: dict[str, int] = defaultdict(int)
        for lot in fab.live_lots.values():
            step = lot.current_step()
            if step is not None:
                per[step.area] += 1
        for area, n in per.items():
            self.area_wip_sum[area] += n
        self.area_wip_samples += 1

    def mean_area_wip(self) -> dict[str, float]:
        n = self.area_wip_samples
        if not n:
            return {}
        return {a: s / n for a, s in self.area_wip_sum.items()}

    def note_wip(self, value: int) -> None:
        """WIP이 바뀔 때마다 시간가중 적분을 갱신한다.

        **warm-up 이후 몫을 따로 쌓는다.** 전체 구간으로 나눈 평균은 빈 fab이
        차오르는 구간을 포함해 낮게 나온다 — 8년/warm-up 1년 실험에서
        2,161 대 2,183으로 1% 차이였다. 다른 KPI(CT, 표준편차, X-factor,
        정시율, wspw)는 전부 warm-up 이후 값이므로, 그대로 두면 요약표가
        **기준이 다른 값을 한 줄에 놓게 된다.**
        """
        now = self.env.now
        self._wip_integral += self._wip_last_v * (now - self._wip_last_t)
        start = max(self._wip_last_t, self.warmup)
        if now > start:
            self._wip_integral_post += self._wip_last_v * (now - start)
        self._wip_last_t = now
        self._wip_last_v = value

    # -- 모니터 -------------------------------------------------------------

    def start_monitors(self, fab: "FabModel"):
        self._step_catalog = {}
        for product, route in fab.data.routes.items():
            for step in route.steps:
                key = f"{product}:{step.step_index}"
                self._step_catalog[key] = {"product": product, "step_index": step.step_index,
                    "step_id": step.step_id, "description": step.description,
                    "toolgroup": step.toolgroup, "area": step.area}
                self.flow["route_step"][key]
        for tg in fab.toolgroups.values():
            self.flow["toolgroup"][tg.name]
            self.flow["area"][tg.spec.area]
        for tg in fab.toolgroups.values():
            for tool in tg.tools:
                tool.kpi = self
                self.flow["station"][tool.tool_id]
        self.env.process(self._weekly_monitor(fab))

    def _weekly_monitor(self, fab: "FabModel"):
        """주 1회 WIP과 감시 대상 대기열 길이를 기록한다.

        area별 WIP도 여기서 표본으로 잡는다 — 레퍼런스가 `WIPLOTAVG`(평균)로
        보고하기 때문이다. 종료 시점 순간값은 그 양이 아니다.

        **관찰만 한다.** `env.timeout` 외에는 아무것도 하지 않으므로 난수를
        쓰지 않고, 이 계측을 켜고 꺼도 결과가 바뀌지 않는다.
        """
        while True:
            yield self.env.timeout(WEEK_MINUTES)
            self._sample_area_wip(fab)
            live = len(fab.live_lots)
            self.weekly_wip.append(live)
            self.wip_samples.append(WipSample(
                t=self.env.now, live=live,
                released=self.n_released, completed=self.n_completed))
            for name, groups in fab.watchlist.items():
                # waiting_count는 배치 그룹 대기까지 센다. len(queue)만 세면
                # Diffusion 대기열이 항상 0으로 보고된다 (Phase 04).
                self.weekly_queue[name].append(
                    sum(fab.toolgroups[g].waiting_count() for g in groups))

            # **병목 타임라인용 — toolgroup마다 한 칸씩.** area별 그림은 이걸
            # 합쳐서 만든다 (toolgroup -> area는 스냅샷에 이미 있다). 두 계열을
            # 따로 담으면 합이 어긋날 수 있어 원시 계열 하나만 둔다.
            #
            # `_sample_area_wip`과 **다른 양이다**: 저쪽은 lot의 현재 step이
            # 어느 area인가(처리 중 포함)이고, 이쪽은 **줄 서 있는 수**다.
            # 병목은 후자로 읽는다 — 처리 중인 lot은 정상이고 대기가 신호다.
            for name, tg in fab.toolgroups.items():
                if tg.tools:                       # delay toolgroup 제외
                    self.weekly_queue_by_tg[name].append(tg.waiting_count())

    # -- 집계 ---------------------------------------------------------------

    def _post_warmup(self) -> list[Completion]:
        """warm-up 이후 투입·완료한 신규 lot만 통계에 쓴다.

        warm-up 이전 투입 lot과 초기 WIP lot은 제외한다. 초기 WIP는
        route 중간에서 출발했으므로 cycle time이 짧다. 섞으면
        평균이 구조적으로 낮아지는데, 그건 fab이 빨라진 것이 아니라 다른 것을
        잰 것이다 (프롬프트 5절 "Cycle time is absurdly low").
        """
        return [c for c in self.completions
                if c.release_time >= self.warmup and not c.is_initial_wip]

    def _post_warmup_all(self) -> list[Completion]:
        """bootstrap lot까지 포함한 완료 목록.

        Phase 12 7절이 "cycle time is reported both with and without
        bootstrapped lots, and the difference is visible"를 요구한다. 보고
        전용이고, 어떤 KPI도 이쪽을 쓰지 않는다.
        """
        return [c for c in self.completions if c.fab_out_time >= self.warmup]

    def mean_wip(self, *, post_warmup: bool = False) -> float:
        """시간가중 평균 WIP. 주 단위 표본 평균과 다르다.

        `post_warmup=True`면 warm-up 이후 구간만 센다. warm-up이 0이면 두
        값이 같으므로, phase 점검 실행의 스냅샷은 바뀌지 않는다.

        마지막 구간은 아직 `note_wip`이 닫지 않았으므로 여기서 더한다.
        **적분기를 건드리지 않고** 지역 변수로만 더한다 — 두 번 부르면
        두 번 쌓인다.
        """
        now = self.env.now
        if now <= 0:
            return 0.0
        tail = self._wip_last_v * (now - self._wip_last_t)
        if not post_warmup:
            return (self._wip_integral + tail) / now

        span = now - self.warmup
        if span <= 0:
            return 0.0
        start = max(self._wip_last_t, self.warmup)
        tail_post = self._wip_last_v * (now - start) if now > start else 0.0
        return (self._wip_integral_post + tail_post) / span

    def _weekly_rate(self, series: dict[int, int]) -> float:
        """warm-up 이후 완전한 주만 평균낸다. 부분 주는 버린다."""
        first = math.ceil(self.warmup / WEEK_MINUTES)
        last = int(self.env.now // WEEK_MINUTES)          # 마지막 주는 미완성
        weeks = list(range(first, last))
        if not weeks:
            return 0.0
        return sum(series.get(w, 0) for w in weeks) / len(weeks)

    def _window_rate(self, wafers):
        """Normalize by the exact post-warmup span, including partial weeks."""
        span = self.env.now - self.warmup
        return wafers * WEEK_MINUTES / span if span > 0 else 0.0

    def _post_weekly_output(self):
        counts = defaultdict(int)
        for c in self.completions:
            if self.warmup <= c.fab_out_time < self.env.now:
                counts[int(c.fab_out_time // WEEK_MINUTES)] += c.wafers
        rows = []
        for week in range(int(self.warmup // WEEK_MINUTES), math.ceil(self.env.now / WEEK_MINUTES)):
            start = max(self.warmup, week * WEEK_MINUTES)
            end = min(self.env.now, (week + 1) * WEEK_MINUTES)
            if end > start:
                rows.append(dict(start_min=start, end_min=end, hours=(end-start)/60,
                    wafers=counts[week], wspw=counts[week]*WEEK_MINUTES/(end-start)))
        return rows

    # -- 스냅샷 -------------------------------------------------------------

    def snapshot(self, *, phase: str, mode: str, seed: int, years: float,
                 features: list[str], runtime_s: float,
                 fab: "FabModel", peak_mem_mb: float = 0.0) -> dict[str, Any]:
        """02_core_flow.md 4.4절의 고정 스키마.

        meta 하위는 비교에서 제외된다. wall-clock은 재현되지 않기 때문이다.
        """
        post = self._post_warmup()
        post_all = self._post_warmup_all()          # Phase 12 — 보고 전용

        # warm-up이 실행 전체를 먹으면 모든 통계가 0이 된다. 조용히 0을 내보내면
        # 모델이 고장난 것과 구분되지 않으므로 크게 알린다.
        if not post:
            reason = ("warm-up이 실행 기간 이상이다"
                      if self.warmup >= self.env.now
                      else "warm-up 이후 완료된 lot이 없다")
            print("\n" + "!" * 74, file=sys.stderr)
            print(f"  경고: 통계 표본이 0개다 — {reason}.", file=sys.stderr)
            print(f"        warm-up {self.warmup / (365 * 24 * 60):.2f}년, "
                  f"실행 {self.env.now / (365 * 24 * 60):.2f}년, "
                  f"완료 {len(self.completions)}건", file=sys.stderr)
            print("        아래 KPI의 0은 '측정 결과 0'이 아니라 '표본 없음'이다.",
                  file=sys.stderr)
            print("        warm-up을 줄이거나 실행 기간을 늘릴 것.", file=sys.stderr)
            print("!" * 74 + "\n", file=sys.stderr)

        cts = [c.cycle_time / 60.0 for c in post]          # 시간 단위
        xfs = [c.cycle_time / c.theoretical for c in post if c.theoretical > 0]

        def by(keyfn, valfn, items=post):
            out: dict[str, list[float]] = defaultdict(list)
            for c in items:
                out[keyfn(c)].append(valfn(c))
            return {k: round(statistics.fmean(v), 4) for k, v in sorted(out.items())}

        bucket_totals = dict.fromkeys(BUCKETS, 0.0)
        for c in post:
            for b, v in c.buckets.items():
                bucket_totals[b] += v
        n = max(len(post), 1)
        buckets_h = {b: round(v / n / 60.0, 4) for b, v in bucket_totals.items()}

        late = [c.lateness / 60.0 for c in post]
        late_sorted = sorted(late)

        def pct(xs, q):
            if not xs:
                return 0.0
            i = min(int(q * len(xs)), len(xs) - 1)
            return round(sorted(xs)[i], 4)

        # -- 분포 요약 (그림용) ---------------------------------------------
        # 평균 하나로는 boxplot도 violin도 그릴 수 없다. lot별 원본을 스냅샷에
        # 담으면 16만 줄이 되므로 **분위수만** 담는다. 여기 다섯 점이면
        # boxplot이 그려지고, 파일은 제품당 한 줄이다.
        QS = (0.05, 0.25, 0.50, 0.75, 0.95)

        def spread(xs: list[float]) -> dict[str, float]:
            """boxplot 한 칸에 필요한 것 전부. 표본 수를 **같이** 담는다 —
            없으면 superhot(연 19 lot)의 값을 읽을 수 없다 (07 8절)."""
            if not xs:
                return {"n": 0}
            s = sorted(xs)
            out = {f"p{int(q * 100)}": round(s[min(int(q * len(s)), len(s) - 1)], 4)
                   for q in QS}
            out.update(n=len(s), min=round(s[0], 4), max=round(s[-1], 4),
                       mean=round(statistics.fmean(s), 4))
            return out

        def spread_by(keyfn, valfn, items=post) -> dict[str, dict[str, float]]:
            g: dict[str, list[float]] = defaultdict(list)
            for c in items:
                g[keyfn(c)].append(valfn(c))
            return {k: spread(v) for k, v in sorted(g.items())}

        def _ff(c) -> float:
            return c.cycle_time / c.theoretical if c.theoretical > 0 else 0.0

        return {
            "schema_version": SCHEMA_VERSION,
            "detail": self.detailed_snapshot(fab),
            "run": {
                "phase": phase, "mode": mode, "seed": seed, "years": years,
                "features": sorted(features), "warmup_excluded": self.warmup > 0,
                "warmup_minutes": self.warmup,
                "toolgroup_wait_window": "finished queue waits clipped to [warmup,end]; includes carry-in queues",
                # 장비 지표(UTIL/SETUP/DOWN/PM/idle)가 lot 지표와 **같은
                # 구간**인가. 읽는 쪽이 판단할 수 있도록 스냅샷이 자기를
                # 설명한다 — 이 키가 없거나 False인 옛 스냅샷의 UTIL%는
                # warm-up을 포함한 값이라 이번 것과 직접 맞대면 안 된다.
                "tool_metrics_post_warmup": bool(self._tool_base),
                "measured_minutes": self.measured_span(),
                "lot_statistics_cohort": "non-initial lots released at or after warmup and completed by observation end",
                "diagnostics_scope": "raw counters and mean_h_with_initial_wip retain whole-run/completion-window audit definitions",
                # 아래 counters의 예약 수치가 무엇을 뜻하는지는 이 값이 정한다.
                # 안 적으면 "예약 0건"이 껐기 때문인지 걸 자리가 없었기
                # 때문인지 스냅샷만 보고는 구분되지 않는다 (G-19와 같은 함정).
                "superhot_reservation": bool(getattr(fab, "reservation", True)),
            },
            "throughput": {
                "wspw_completions": round(self._window_rate(sum(c.wafers for c in self.completions
                    if c.fab_out_time >= self.warmup)), 2),
                "wspw_releases": round(self._window_rate(self.wafers_released_post), 2),
                "wspw_completions_full_weeks": round(self._weekly_rate(self.weekly_completions), 2),
                "wspw_releases_full_weeks": round(self._weekly_rate(self.weekly_releases), 2),
                "rate_start_min": self.warmup,
                "rate_end_min": self.env.now,
                "rate_span_minutes": self.env.now-self.warmup,
                "weekly_post_warmup": self._post_weekly_output(),
                "lot_starts": self.n_released,
                "lot_completions": self.n_completed,
                "lot_completions_post_warmup": len(post),
                # Phase 12 — 두 개체를 **어디서나 따로 센다** (R9, 7절
                # "Population separation"). parametric 모드에서는 전부 0이다.
                "initial_wip_lots": self.n_initial_wip,
                "initial_wip_completions": sum(c.is_initial_wip for c in post_all),
                # Phase 13 3.3절 — 처리량 추세 교차확인에 **반복별 주간 계열**이
                # 필요하다. `weekly_completions`는 dict라 빠진 주가 생길 수
                # 있으므로 0으로 메워 길이 = 경과 주 수인 목록으로 편다.
                # 빠진 주를 그냥 건너뛰면 기울기가 그만큼 가팔라진다.
                "weekly_wafers": [self.weekly_completions.get(w, 0)
                                  for w in range(int(self.env.now
                                                     // WEEK_MINUTES))],
            },
            "cycle_time": {
                "mean_h": round(statistics.fmean(cts), 4) if cts else 0.0,
                # Phase 12 7절 — bootstrap lot을 **넣은** 값도 나란히 둔다.
                # 이 둘이 벌어지는 것이 정상이고, 벌어지지 않으면 R6의 제외가
                # 동작하지 않는 것이다. parametric 모드에서는 둘이 같다.
                "mean_h_with_initial_wip": (
                    round(statistics.fmean([c.cycle_time / 60.0
                                            for c in post_all]), 4)
                    if post_all else 0.0),
                "p50_h": pct(cts, 0.50),
                "p90_h": pct(cts, 0.90),
                "std_h": round(statistics.pstdev(cts), 4) if len(cts) > 1 else 0.0,
                "by_product": by(lambda c: c.product, lambda c: c.cycle_time / 60.0),
                "by_lot_type": by(lambda c: c.lot_type, lambda c: c.cycle_time / 60.0),
                # lot 종류별 X-factor. **우선순위 효과는 이걸로 봐야 한다.**
                # 원시 CT를 종류끼리 비교하면 route 길이를 재게 된다 —
                # superhot은 Product_3 전용이고 그 route가 583 step으로 가장
                # 길다 (DECISIONS B-4). 이론시간으로 나누면 길이가 상쇄된다.
                "x_factor_by_lot_type": by(
                    lambda c: c.lot_type,
                    lambda c: c.cycle_time / c.theoretical if c.theoretical else 0.0),
                # 배치 대기를 뺀 X-factor. **디스패칭이 통제하는 부분**이다.
                # 배치 형성은 우선순위를 보지 않으므로(04 R10 / DECISIONS G-3)
                # 위 값에는 디스패칭과 무관한 대기가 섞인다. Product_3(superhot
                # 전용)은 배치 그룹이 느리게 차서 그 몫이 특히 크다.
                "x_factor_dispatch_by_lot_type": by(
                    lambda c: c.lot_type,
                    lambda c: ((c.cycle_time - c.buckets.get("wait_batch", 0.0))
                               / c.theoretical) if c.theoretical else 0.0),
                # 07 8절이 "with sample counts"를 요구한다. superhot은 연 19 lot
                # 이라(B-4) 표본 수를 모르면 값을 읽을 수 없다.
                "count_by_lot_type": {
                    k: sum(1 for c in post if c.lot_type == k)
                    for k in sorted({c.lot_type for c in post})},
                "buckets_h": buckets_h,
                # **lot 종류별 11버킷.** 위 `buckets_h`는 전체 평균이라
                # "hot lot이 어디서 느린가"를 못 가린다. hot의 초과분이
                # `queue`면 디스패칭 문제이고, `wait_gate`/`wait_dedicated`/
                # `wait_down`이면 우선순위가 못 뚫는 구조적 한계다. 그 구분이
                # 붙어야 고칠 곳이 있는지 말할 수 있다.
                "buckets_by_lot_type": _buckets_by(post),
                # -- 분포 (그림용). 평균만으로는 boxplot을 못 그린다 --------
                "spread_by_product": spread_by(
                    lambda c: c.product, lambda c: c.cycle_time / 60.0),
                "spread_by_lot_type": spread_by(
                    lambda c: c.lot_type, lambda c: c.cycle_time / 60.0),
                # 논문 Table II가 **제품 × lot 종류**로 FF를 준다. 제품별 CT와
                # 종류별 FF를 따로 담으면 그 표와 맞댈 수 없어서 교차로 담는다.
                "ff_by_product_type": {
                    p: {t: round(statistics.fmean(v), 4) for t, v in sorted(d.items())}
                    for p, d in sorted(_ff_cross(post, _ff).items())},
            },
            "due_date": {
                "on_time_pct": round(100.0 * sum(c.on_time for c in post) / n, 4)
                if post else 0.0,
                "by_product": by(lambda c: c.product, lambda c: 100.0 * c.on_time),
                "by_lot_type": by(lambda c: c.lot_type, lambda c: 100.0 * c.on_time),
                "lateness_h": {
                    "mean": round(statistics.fmean(late), 4) if late else 0.0,
                    "p90": pct(late, 0.90),
                    "max": round(late_sorted[-1], 4) if late else 0.0,
                    # **분포를 직접 본다.** 세 점(평균/P90/최대)으로는 퍼짐을
                    # 정규 근사로 역산할 수밖에 없었다. CR이 납기 난수를 얼마나
                    # 흡수했는지가 여기서 측정된다 (G-25) — 좁으면 CT가 납기를
                    # 그대로 따라간 것이고, 넓으면 혼잡이 이긴 것이다.
                    "pctl": {str(int(q * 100)): pct(late, q)
                             for q in (0.05, 0.25, 0.50, 0.75, 0.95)},
                },
            },
            "wip": {
                # **warm-up 이후 기준이다.** 다른 KPI와 기준을 맞춘다.
                # warm-up 0이면 아래 두 값이 같다 — phase 스냅샷은 안 바뀐다.
                "mean_lots": round(self.mean_wip(post_warmup=True), 4),
                # 과도구간이 얼마나 끌어내렸는지 보려면 둘을 비교한다.
                "mean_lots_all": round(self.mean_wip(), 4),
                "final_lots": len(fab.live_lots),
                "x_factor": round(statistics.fmean(xfs), 4) if xfs else 0.0,
                # **논문(Kopp 2020)은 Flow Factor의 중앙값 1.95를 보고한다.**
                # 평균과 중앙값을 맞대면 분포의 오른쪽 꼬리만큼 우리가 불리하다
                # — CT 분포는 항상 오른쪽으로 길다. 같은 통계량으로 비교할 수
                # 있도록 둘 다 낸다. FF와 X-factor는 같은 양이다 (CT / 이론시간).
                "x_factor_p50": pct(xfs, 0.50),
                "x_factor_p90": pct(xfs, 0.90),
                # 논문 Fig. 3이 **분위수 곡선**으로 보고한다 (5/25/50/75/95).
                # 그 그림과 맞대려면 같은 점들이 있어야 한다.
                "x_factor_pctl": {
                    str(int(q * 100)): pct(xfs, q)
                    for q in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)},
                # Fig. 3은 **regular만** 본다 — 논문 IV-B절이 "Only regular
                # lots are represented, hot lots have a FF close to 1.0"라고
                # 적는다. 섞으면 hot(1.39)이 왼쪽 꼬리를 만들어 분위수가 밀린다.
                "x_factor_pctl_regular": {
                    str(int(q * 100)): pct(
                        [_ff(c) for c in post
                         if c.lot_type == "regular" and c.theoretical > 0], q)
                    for q in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)},
                # **논문 규약의 FF.** 분모가 위와 다르다 (`Route.rpt_paper`) —
                # 논문은 RPT를 "빈 fab lot 하나의 측정 CT"로 정의해 이송을
                # 포함하고 표본검사를 반영한다. 우리 `suffix_raw_pt`는
                # critical ratio의 분모라 정의가 고정돼 있어 바꿀 수 없다.
                # **1.95와 맞댈 것은 이쪽이다.**
                "ff_paper": _ff_paper_stats(post, fab, pct),
                # Phase 09 — 부하가 용량 아래로 내려갔으니 WIP이 정착해야 한다
                # (09 4.1절). 추세를 보려면 마지막 값이 아니라 시계열이 있어야
                # 한다. `compare`는 목록을 diff하지 않으므로 표는 안 늘어난다.
                "weekly_lots": list(self.weekly_wip),
            },
            "areas": self._area_states(fab),
            # toolgroup 단위 정지 시간. `areas`는 area로 뭉개므로 "어느 장비가
            # 가장 많이 섰나"를 못 본다 — 그림 하나가 그걸 묻는다. 106줄이라
            # 파일이 커지지 않는다.
            "toolgroups": self._toolgroup_states(fab),
            # toolgroup별 주간 대기열 길이 (병목 타임라인 히트맵).
            # area별 그림은 `toolgroups[*].area`로 묶어서 만든다.
            "weekly_queue_by_toolgroup": dict(sorted(self.weekly_queue_by_tg.items())),
            "counters": {
                "setups": fab.n_setups,
                "setup_skipped": fab.setup_skipped,
                "setup_initial": fab.setup_initial,
                "setup_blocked_slots": fab.setup_blocked_slots,
                # -- 배정 프로파일 ------------------------------------------
                # 배정 비용의 실제 크기와, 옛 구조(장비 index 첫 적합 + 첫
                # None에서 중단)와 **결정이 갈린 횟수**. 뒤쪽은 잡음이 없어
                # KPI 차이(단일 seed 분해능 ~1%)와 달리 단정할 수 있다.
                "dispatch": {
                    "passes": fab.dispatch_passes,
                    "commits": fab.dispatch_commits,
                    "select_lot_calls": fab.select_lot_calls,
                    "queue_elements_scanned": fab.queue_elements_scanned,
                    "free_tools_scanned": fab.free_tools_scanned,
                    "pairs_evaluated": fab.pairs_evaluated,
                    "elements_per_call": round(
                        fab.queue_elements_scanned / fab.select_lot_calls, 2)
                    if fab.select_lot_calls else 0.0,
                    "choice_differs": fab.dispatch_choice_differs,
                    "rescued": fab.dispatch_rescued,
                    # wake-up 해석이 갈릴 수 있었던 자리. 0이면 두 해석이 같은
                    # 답을 내므로 해석을 정할 필요가 없다.
                    "wake_up_ambiguous": fab.wake_up_ambiguous,
                    "batch_groups_evaluated": fab.batch_groups_evaluated,
                    "batch_group_retries": fab.batch_group_retries,
                },
                # Phase 05 진입 게이트 (DECISIONS G-19). `wait_gate` 버킷이
                # 늘 0.0인 것이 측정 결과인지 측정 부재인지 가른다.
                # passes == 0 이면 게이트 때문에 기다린 lot이 없다는 뜻이다.
                "gate_blocked_slots": fab.gate_blocked_slots,
                "gate_blocked_passes": fab.gate_blocked_passes,
                # 빈도가 아니라 **크기**. toolgroup이 "대기 lot이 있는데
                # 게이트 때문에만 못 내보낸" 구간의 합(시간).
                "gate_blocked_h": round(fab.gate_blocked_totals()[0] / 60.0, 3),
                # Phase 11 예약 (DECISIONS G-23). 위와 **같은 질문**을
                # `wait_reserved` 버킷에 한다. 그쪽도 선언만 돼 있고 아무도
                # 채우지 않아 0.0이었다.
                "reserved_blocked_slots": fab.reserved_blocked_slots,
                "reserved_blocked_passes": fab.reserved_blocked_passes,
                "reserved_blocked_h": round(
                    fab.reserved_blocked_totals()[0] / 60.0, 3),
                # 07 3.2절 — 최소 실행이 걸렸을 때 무엇이 후보가 되었는가.
                # 최소를 못 채우면 그 이유가 여기 있다: override로 깨졌는지,
                # 같은 setup 대기가 없어 바꿨는지(change), 놀렸는지(idle).
                "minrun_outcomes": dict(sorted(fab.ctx.minrun_stats.items())),
                # setup 후보 필터(B-1)가 실제로 후보를 줄인 횟수.
                # `dropped`가 놓칠 뻔한 (lot, 장비) 쌍을 건진 수다.
                "setup_filter": dict(sorted(fab.ctx.setup_filter_stats.items())),
                # 회피 toolgroup의 연속 실행 길이 중앙값. 07 7절 기준은 >= 7.
                "minrun_median_run": self._median_run_length(fab),
                # Phase 08 — lot-to-lens. 위반/누락은 warnings에도 들어간다.
                "ltl_waits": fab.ltl_waits,
                "ltl_max_wait_min": round(fab.ltl_max_wait, 3),
                # 최대 대기가 난 자리. 숫자만으로는 손댈 곳을 못 찾는다 (08 8절).
                "ltl_max_wait_where": (list(fab.ltl_max_wait_where)
                                       if fab.ltl_max_wait_where else None),
                # 4.2절 — 머리를 지나온 적이 없는 lot의 누락. 결함이 아니다.
                "ltl_missing_no_history": fab.ltl_missing_no_history,
                # 4.1절 — rework 재진입에서 직전 장비를 되찾은 횟수.
                "ltl_resumed": fab.ltl_resumed,
                # Phase 09 — sampling / rework.
                "reworks": fab.n_reworks,
                "sampling_skips": fab.n_skips,
                # R6의 상한. 0이 아니면 그 자체가 발견이다 (3.1절).
                "rework_bound_hits": fab.rework_bound_hits,
                # step별 관측 비율이 자기 n의 이항 구간 안인지 (7절).
                "sampling_fit": _fit_scalars(sampling_fit(fab)),
                "rework_fit": _fit_scalars(rework_fit(fab)),
                # 09 7절 "Step accounting" — 수행+건너뜀 == route+rework_span.
                # lot마다 정확히 성립해야 한다. 여기서는 깨진 lot 수만 남긴다.
                #
                # Phase 12 R8이 **출발 step을 뺀다.** 초기 WIP lot은 route
                # 앞부분을 걸은 적이 없으므로 좌변이 그만큼 작다. 보통 lot은
                # `start_step_index`가 0이라 식이 그대로다 — 값이 안 바뀐다.
                "step_accounting_violations": _step_accounting_bad(post, fab),
                # 같은 항등식을 bootstrap 개체에 대해 **따로** 센다 (R8/R9).
                # 섞으면 어느 쪽이 깨졌는지 알 수 없다.
                "step_accounting_violations_initial_wip": _step_accounting_bad(
                    [c for c in post_all if c.is_initial_wip], fab),
                # Phase 10 — CQT window. 네 종착 상태를 **따로** 센다.
                # 10 3.1절: opened == closed + abandoned + skipped + still_open
                "cqt_opened": fab.cqt_opened,
                "cqt_closed": fab.cqt_closed,
                "cqt_violated": fab.cqt_violated,
                "cqt_abandoned_by_rework": fab.cqt_abandoned,
                "cqt_target_skipped": fab.cqt_target_skipped,
                "cqt_still_open": sum(len(l.active_cqts)
                                      for l in fab.live_lots.values()),
                # Phase 11 — super hot lot 예약. 위반 둘은 0이어야 한다.
                "reservations": fab.reservations_made,
                "reservations_released": fab.reservations_released,
                "reservation_misses": fab.reservation_misses,
                "reservation_violations": fab.reservation_violations,
                "reservation_vs_dedication": fab.reservation_vs_dedication,
                "reservation_hold_mean_min": round(
                    fab.reservation_hold_sum / fab.reservations_released, 3)
                if fab.reservations_released else 0.0,
                "reservation_hold_max_min": round(fab.reservation_hold_max, 3),
                # 예약이 잡아먹은 장비 시간 / 전체 장비 시간 (11 7절 "Cost").
                "reservation_cost_pct": round(
                    100.0 * fab.reservation_hold_sum
                    / max(self.env.now * sum(len(tg.tools)
                                             for tg in fab.toolgroups.values()),
                          1e-9), 6),
                "reservations_open_at_end": sum(
                    len(l.reservations) for l in fab.live_lots.values()),
                "post_warmup_lots": len(post),
                "batches": fab.n_batches,
                # 07 R14 — 배치 구성원을 순위로 고르게 바꾼 것이 **몇 번
                # 물었나**. `reorders`는 도착 순서와 다르게 고른 횟수,
                # `priority_rescues`는 그중 더 높은 우선순위 lot을 실제로
                # 태운 횟수다. 0이면 이 변경이 결과를 안 바꿨다는 뜻이고,
                # 그것도 답이다.
                "batch_reorders": fab.batch_reorders,
                "batch_priority_rescues": fab.batch_priority_rescues,
                # cascading 장비에서 두 job이 동시에 있던 시간의 비율.
                # 0이면 capacity 2가 죽은 것이다 (05 5절 마지막 실패 모드).
                "cascading_two_job_pct": self._two_job_pct(fab),
                # cascading 장비 진입 건수. 게이트 위반은 warnings에 센다.
                "cascade_entries": sum(
                    t.entries for tg in fab.toolgroups.values()
                    if tg.spec.cascading for t in tg.tools),
                "breakdowns": sum(t.down_events for tg in fab.toolgroups.values()
                                  for t in tg.tools),
                "pms": sum(t.pm_events_count for tg in fab.toolgroups.values()
                           for t in tg.tools),
                "max_concurrent_down": max(
                    (t.max_concurrent_down for tg in fab.toolgroups.values()
                     for t in tg.tools), default=0),
                "tools_with_overlap": sum(
                    1 for tg in fab.toolgroups.values() for t in tg.tools
                    if t.max_concurrent_down >= 2),
                "warnings": dict(fab.warnings),
            },
            "meta": {
                "runtime_s": round(runtime_s, 3),
                "peak_mem_mb": round(peak_mem_mb, 1),
                "input_hash": input_hash(),
                # 어느 코드로 나온 점인지. 스윕이 점을 재사용해도 되는지
                # 판정하는 데 쓴다 (`model_hash` 독스트링).
                "model_hash": model_hash(),
            },
        }

    @staticmethod
    def _median_run_length(fab: "FabModel") -> dict[str, float]:
        """setup 회피 toolgroup별 연속 실행 길이 중앙값 (07 7절).

        평균이 아니라 중앙값이다 — 짧은 실행 하나가 평균을 끌어내리는 것과
        분포 전체가 낮은 것은 다른 문제다.
        """
        out = {}
        for name in fab.ctx.setup_avoidance:
            hist = fab.run_lengths.get(name)
            if not hist:
                continue
            total=sum(hist.values())
            if not total:
                out[name]=0.0
                continue
            middle=((total-1)//2,total//2)
            cumulative=0;values=[]
            for length,count in sorted(hist.items()):
                cumulative+=count
                while len(values)<2 and cumulative>middle[len(values)]:
                    values.append(length)
                if len(values)==2:break
            out[name]=float(sum(values)/2)
        return dict(sorted(out.items()))

    def _two_job_pct(self, fab: "FabModel") -> float:
        """cascading 장비의 2-job 시간 / 총 처리 시간 (%).

        capacity 2가 살아 있는지 한 숫자로 본다. 최댓값이 아니라 시간 비율인
        이유는 효과 크기를 알아야 하기 때문이다 — "한 번은 2였다"로는 두 번째
        슬롯이 유의미하게 쓰이는지 알 수 없다.

        분자와 분모를 **같은 구간**에서 뽑는다 (`tool_acc`). 한쪽만 warm-up을
        빼면 비율이 뜻 없는 값이 된다.
        """
        two = proc = 0.0
        for tg in fab.toolgroups.values():
            if not tg.spec.cascading:
                continue
            for t in tg.tools:
                t.close_intervals()        # 마지막 2-job 구간을 닫는다
                two += self.tool_acc(t, "two_job_time")
                proc += self.tool_acc(t, "proc_time")
        return round(100.0 * two / proc, 4) if proc > 0 else 0.0

    def _area_states(self, fab: "FabModel") -> dict[str, dict[str, float]]:
        """area별 장비 상태 시간 비율. 서로 배타적이어야 한다.

        **구간은 lot 지표와 같다** — warm-up 이후다 (`measured_span`). 예전에는
        여기만 t=0부터 8년 전체였고, 그래서 CT·FF는 warm-up 이후인데 UTIL%만
        빈 fab 구간을 포함하는 상태였다 (`mark_warmup` 참고).
        """
        total_t = self.measured_span()
        acc: dict[str, dict[str, float]] = defaultdict(
            lambda: {"proc": 0.0, "loadunload": 0.0, "setup": 0.0, "down": 0.0, "pm": 0.0,
                         "unavail": 0.0, "capacity": 0.0, "wip": 0.0, "two_job": 0.0,
                         "work": 0.0, "idle": 0.0})
        for tg in fab.toolgroups.values():
            a = acc[tg.spec.area]
            if tg.spec.is_delay:
                # delay는 장비를 점유하지 않는다. 용량 개념이 없으므로 대기열만 센다.
                a["wip"] += tg.waiting_count()
                continue
            for tool in tg.tools:
                # 열려 있는 구간 넷을 한 번에 닫는다. 예전에는 세 함수를
                # 불렀는데 `stop_busy`/`note_jobs`가 조건에 걸려 아무것도 안
                # 하는 경우가 있었고, 정지 구간은 아래에서 손으로 더했다.
                tool.close_intervals()
                a["two_job"] += self.tool_acc(tool, "two_job_time")
                a["proc"] += self.tool_acc(tool, "proc_time")
                a["loadunload"] += self.tool_acc(tool, "loadunload_time")
                a["setup"] += self.tool_acc(tool, "setup_time")
                a["work"] += self.tool_acc(tool, "work_time")   # 합집합. util의 재료
                # down/pm 은 사건별 총합. 레퍼런스가 이 형태로 보고한다.
                a["down"] += self.tool_acc(tool, "down_time")
                a["pm"] += self.tool_acc(tool, "pm_time")
                # idle 계산에는 합집합을 쓴다. 겹친 정지를 두 번 빼면 음수가 난다.
                # 열린 구간은 `close_intervals`가 이미 접어 넣었다.
                a["unavail"] += self.tool_acc(tool, "unavailable_time")
                a["idle"] += self._idle_minutes.get(tool.tool_id, max(0, total_t-
                    self.tool_acc(tool, "work_time")-self.tool_acc(tool, "unavailable_time")))
                a["capacity"] += total_t
            # 배치 toolgroup의 대기 lot은 queue가 아니라 batch_queue에 있다.
            # len(queue)만 세면 Diffusion WIP이 항상 0으로 보고된다.
            a["wip"] += tg.waiting_count()

        # 레퍼런스 stnfam 보고서의 `WIPLOTAVG`에 대응하는 값. 주간 표본의
        # 평균이고 warm-up 이후만 센다. 아래 `wip_lots`(종료 시점 순간값,
        # 대기열만)와는 **다른 양이다** — 비교에 쓸 것은 이쪽이다.
        avg_wip = self.mean_area_wip()

        out: dict[str, dict[str, float]] = {}
        for area, v in sorted(acc.items()):
            # cascading 장비에서 두 job이 동시에 있던 시간 / 처리 시간.
            # cascading이 아닌 area는 0이다 — capacity 1이라 구조적으로 불가능.
            two = (round(100.0 * v["two_job"] / v["proc"], 4)
                   if v["proc"] > 0 else 0.0)
            cap = v["capacity"]
            if cap <= 0:
                out[area] = {"util_pct": 0.0, "down_pct": 0.0, "pm_pct": 0.0,
                                 "setup_pct": 0.0, "proc_pct": 0.0, "idle_pct": 0.0,
                                 "loadunload_pct": 0.0,
                                 "wip_lots": round(v["wip"], 2), "two_job_pct": two,
                                 "wip_lots_avg": round(avg_wip.get(area, 0.0), 2)}
                continue
            # **합집합이다.** proc + loadunload + setup 을 더하면 cascading
            # 장비에서 겹친 시간이 두 번 들어가 util이 100%를 넘는다 (Planar
            # 107.7 %). 레퍼런스 `stngrp.rep`의 UTIL%는 배타적 상태 비율이라
            # 넘을 수 없다 — 맞대려면 이쪽이어야 한다.
            #
            # **아래 분해 열(proc/setup/loadunload)은 job별 총합 그대로 둔다.**
            # DOWN%/PM%/SETUP% 검증이 그 값을 쓰고 있고, 그쪽은 겹침이 없는
            # 양이라 바꿀 이유가 없다. 그래서 cascading area에서는
            # `util_pct < proc_pct + setup_pct + loadunload_pct` 가 된다.
            busy = v["work"]
            out[area] = {
                "two_job_pct": two,
                "util_pct": round(100.0 * busy / cap, 3),
                "proc_pct": round(100.0 * v["proc"] / cap, 3),
                "setup_pct": round(100.0 * v["setup"] / cap, 3),
                # 이제 util - proc - setup 으로 유도할 수 없다. 따로 적는다.
                "loadunload_pct": round(100.0 * v["loadunload"] / cap, 3),
                "down_pct": round(100.0 * v["down"] / cap, 3),
                "pm_pct": round(100.0 * v["pm"] / cap, 3),
                # 합집합으로 계산한다. down_pct + pm_pct 를 빼면 겹친 만큼
                # 중복으로 빠져서 idle이 음수가 될 수 있다.
                "idle_pct": round(100.0 * v["idle"] / cap, 3),
                "unavail_pct": round(100.0 * v["unavail"] / cap, 3),
                # 종료 시점 순간값, **대기열만**. 비교용이 아니다.
                "wip_lots": round(v["wip"], 2),
                # 레퍼런스 `WIPLOTAVG`에 대응. 주간 표본 평균, 처리 중 포함.
                "wip_lots_avg": round(avg_wip.get(area, 0.0), 2),
            }
        return out

    def _toolgroup_states(self, fab: "FabModel") -> dict[str, dict[str, float]]:
        """toolgroup별 정지·가동 요약 (시간 단위).

        `_area_states`와 **같은 양을 같은 방식으로** 잰다. 다른 식으로 재면
        area 합과 안 맞아서 어느 쪽이 틀렸는지 알 수 없게 된다. 구간도 같다 —
        warm-up 이후다 (`measured_span`).
        """
        total_t = self.measured_span()
        out: dict[str, dict[str, float]] = {}
        for name, tg in sorted(fab.toolgroups.items()):
            if not tg.tools:                    # delay toolgroup
                continue
            # **먼저 닫는다.** `_area_states`가 이미 닫았더라도 호출 순서에
            # 기대지 않는다 — `close_intervals`는 두 번 불러도 0을 더한다.
            for t in tg.tools:
                t.close_intervals()

            def acc(attr: str, tools=tg.tools) -> float:
                return sum(self.tool_acc(t, attr) for t in tools)

            down = acc("down_time")
            pm = acc("pm_time")
            down_n = int(acc("down_events"))
            pm_n = int(acc("pm_events_count"))
            unavail = acc("unavailable_time")   # 합집합
            busy = acc("work_time")             # `_area_states`와 같은 합집합
            loadunload = acc("loadunload_time")
            proc = acc("proc_time")
            setup = acc("setup_time")
            two = acc("two_job_time")
            cap = total_t * len(tg.tools)
            # 장비를 기다린 시간 [건수, 합, 최댓값] (분).
            n, tot, mx = fab.wait_by_toolgroup.get(name, (0.0, 0.0, 0.0))
            out[name] = {
                "area": tg.spec.area,
                "tools": len(tg.tools),
                "down_h": round(down / 60.0, 2),
                "pm_h": round(pm / 60.0, 2),
                # 시간만으로는 "한 번 오래"와 "여러 번 짧게"가 구분되지 않는다.
                # DOWN %가 맞아도 TTF 분포가 틀릴 수 있고, FOA 분산과 PM 주기는
                # 애초에 **건수**로만 검증된다. `counters.breakdowns`/`pms`는
                # fab 전체 합계라 어느 toolgroup인지 못 짚는다.
                #
                # `down_h`/`pm_h`와 **같은 기준**이다 — warm-up 포함 전체 기간
                # 누적이므로 기대 건수도 전체 기간으로 계산해서 맞댈 것.
                # `pm_events`는 월/분기/주간/counter PM이 **섞인** 값이다.
                "down_events": down_n,
                "pm_events": pm_n,
                "util_pct": round(100.0 * busy / cap, 3) if cap else 0.0,
                # 논문 Table IV의 재료다. `down_pct`/`pm_pct`는 **사건별 총합**
                # 이라 겹친 정지를 두 번 세고, `unavail_pct`는 **합집합**이다.
                # 가용률은 배타적 상태량이므로 반드시 `unavail_pct`로 낸다.
                "down_pct": round(100.0 * down / cap, 3) if cap else 0.0,
                "pm_pct": round(100.0 * pm / cap, 3) if cap else 0.0,
                "unavail_pct": round(100.0 * unavail / cap, 3) if cap else 0.0,
                # 분해 열. job별 총합이므로 cascading에서는 합이 util을 넘는다.
                "proc_pct": round(100.0 * proc / cap, 3) if cap else 0.0,
                "setup_pct": round(100.0 * setup / cap, 3) if cap else 0.0,
                "loadunload_pct": round(100.0 * loadunload / cap, 3) if cap else 0.0,
                # 겹친 시간 / 처리 시간. util에서 몇 %p가 빠졌는지 말해 준다.
                "two_job_pct": round(100.0 * two / proc, 4) if proc > 0 else 0.0,
                "wip_lots": tg.waiting_count(),
                # **방문당 평균 대기**다. lot당도 area당도 아니다 — 한 lot이
                # 같은 toolgroup을 수십 번 재방문하므로 그 구분이 중요하다.
                "wait_visits": int(n),
                "wait_mean_h": round(tot / n / 60.0, 4) if n else 0.0,
                "wait_max_h": round(mx / 60.0, 3),
            }
        return out

    # -- 출력 ---------------------------------------------------------------

    def print_kpi_lines(self, snap: dict[str, Any]) -> None:
        """`KPI <name> <value>` 한 줄씩. phase 간 기계적 diff용이라 stdout으로 간다."""
        flat: list[tuple[str, Any]] = [
            ("throughput_wspw_completions", snap["throughput"]["wspw_completions"]),
            ("throughput_wspw_releases", snap["throughput"]["wspw_releases"]),
            ("lot_starts", snap["throughput"]["lot_starts"]),
            ("lot_completions", snap["throughput"]["lot_completions"]),
            ("cycle_time_mean_h", snap["cycle_time"]["mean_h"]),
            ("cycle_time_p90_h", snap["cycle_time"]["p90_h"]),
            ("on_time_pct", snap["due_date"]["on_time_pct"]),
            ("wip_mean_lots", snap["wip"]["mean_lots"]),
            ("x_factor", snap["wip"]["x_factor"]),
            # 논문 목표(FF 중앙값 1.95)와 같은 통계량으로 읽을 수 있게 같이 낸다.
            ("x_factor_p50", snap["wip"]["x_factor_p50"]),
        ]
        for b, v in snap["cycle_time"]["buckets_h"].items():
            flat.append((f"ct_bucket_{b}_h", v))
        for name, line in flat:
            print(f"KPI {name} {line}")


# ---------------------------------------------------------------------------
# 스냅샷 파일
# ---------------------------------------------------------------------------

_HASH_CACHE: dict[str, str] = {}
_MODEL_HASH: str | None = None


def model_hash() -> str:
    """모델 소스(`smt2020_sim/*.py`)의 내용 해시. 16자.

    **스윕 때문에 있다.** 곡선의 점 하나는 실행 하나이고 곡선은 그 점들을
    잇는다. 코드를 고친 뒤 일부 점만 다시 돌리면 **한 곡선에 두 모델이
    섞이는데**, 그림은 멀쩡히 그려지고 원인은 아무 데도 안 보인다.

    실제로 그럴 뻔했다 — `results/sweep8y/` 의 5점은 디스패처 수정(R6/R8)과
    납기 난수 z 이전 코드로 나왔고, 파일 이름만 보는 재사용 판정은 그걸
    구분하지 못한다. 그래서 어느 코드로 나온 스냅샷인지를 적어 둔다.

    워크북 해시(`input_hash`)와 **다른 것을 잡는다** — 그쪽은 입력이 같은지,
    이쪽은 모델이 같은지다. 둘 다 같아야 한 곡선에 놓을 수 있다.
    """
    global _MODEL_HASH
    if _MODEL_HASH is None:
        h = hashlib.sha256()
        for p in sorted(Path(__file__).parent.glob("*.py")):
            h.update(p.name.encode("utf-8"))
            h.update(p.read_bytes())
        _MODEL_HASH = h.hexdigest()[:16]
    return _MODEL_HASH


def input_hash(path: Path | None = None) -> str:
    """입력 워크북의 내용 해시. 비교 대상이 같은 입력인지 확인하는 데 쓴다."""
    p = path or config.XLSX_PATH
    key = str(p)
    if key not in _HASH_CACHE:
        if not p.exists():
            return ""
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _HASH_CACHE[key] = h.hexdigest()[:16]
    return _HASH_CACHE[key]


def snapshot_path(*, phase: str, mode: str, seed: int, years: float) -> Path:
    y = int(years) if float(years).is_integer() else years
    return config.RESULTS_DIR / f"phase_{phase}_{mode}_seed{seed}_{y}y.json"


def write_snapshot(snap: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    snap = dict(snap)
    if "detail" in snap:
        snap["detail"] = dict(snap["detail"])
        db = Path(snap["detail"]["database"])
        if db.is_absolute():
            try:
                snap["detail"]["database"] = os.path.relpath(db, path.parent)
            except ValueError:  # Different Windows drive.
                snap["detail"]["database"] = str(db)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(snap, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


# Shared snapshot reports; kept here for run.py and visualize.py.


# ---------------------------------------------------------------------------
# 표 그리기 — 한글이 섞여도 칸이 맞게
# ---------------------------------------------------------------------------

def w(s: str) -> int:
    """한글을 두 칸으로 세는 표시 폭."""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in str(s))


def pad(s: str, n: int, right: bool = False) -> str:
    s = str(s)
    gap = " " * max(0, n - w(s))
    return gap + s if right else s + gap


def _wrap(text: str, width: int) -> list[str]:
    """표시 폭 기준 줄바꿈. `w()` 로 세므로 한글이 섞여도 넘치지 않는다."""
    out, line = [], ""
    for word in str(text).split():
        cand = f"{line} {word}" if line else word
        if line and w(cand) > width:
            out.append(line)
            line = word
        else:
            line = cand
    if line:
        out.append(line)
    return out or [""]


def rule(n: int = 78, ch: str = "-") -> None:
    print(ch * n)


def head(title: str) -> None:
    print()
    rule(78, "=")
    print(f"  {title}")
    rule(78, "=")


def pct_diff(ours: float, ref: float) -> str:
    if not ref:
        return "     —"
    return f"{100.0 * (ours - ref) / ref:+6.1f}%"


# ---------------------------------------------------------------------------
# 스냅샷 읽기
# ---------------------------------------------------------------------------

def load_snapshot(path: Path) -> dict:
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    if "run" not in snap or "cycle_time" not in snap:
        raise SystemExit(f"실험 스냅샷이 아니다: {path}")
    return snap


def latest_snapshot(root: Path | None = None) -> Path:
    """results 하위 폴더까지 찾아 가장 최근 일반 실행 스냅샷을 고른다.

    캐시·내보낸 표·검증 JSON은 제외한다. 스윕 한 점은 --snap으로 지정한다.
    """
    root = config.RUN_RESULTS_DIR if root is None else Path(root)
    excluded = {"notebook_cache", "notebook_exports", "details", ".cache"}
    cands = []
    for path in root.rglob("*.json"):
        if path.name == "verify_last.json" or excluded.intersection(path.relative_to(root).parts):
            continue
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(snapshot, dict):
            continue
        run = snapshot.get("run")
        if not isinstance(run, dict) or not isinstance(snapshot.get("cycle_time"), dict):
            continue
        if run.get("phase") == "sweep":
            continue
        cands.append(path)
    if not cands:
        raise SystemExit(
            f"스냅샷을 못 찾았다: {root} 및 하위 폴더\n"
            "  다른 곳에 저장했다면 --snap으로 결과 JSON을 지정할 것.\n"
            "  실행 결과가 없다면 먼저 python run.py로 생성할 것.")
    return max(cands, key=lambda p: (p.stat().st_mtime_ns, str(p)))


def area_loadunload_pct(a: dict) -> float:
    """area의 load/unload 비율.

    이제 스냅샷에 그대로 담긴다. 옛 스냅샷에는 없어서 유도해야 하는데,
    그때 쓰던 `util − proc − setup` 은 **util이 합집합이 된 뒤로 틀렸다** —
    cascading area에서 겹친 만큼 작게 나온다. 옛 스냅샷은 util도 옛 정의라
    그 안에서는 여전히 맞으므로, 그대로 fallback으로 둔다.
    """
    lu = a.get("loadunload_pct")
    if lu is not None:
        return lu
    return max(0.0, a.get("util_pct", 0.0) - a.get("proc_pct", 0.0)
               - a.get("setup_pct", 0.0))


def run_conditions(snap: dict) -> list[tuple[str, str]]:
    """실행 조건. **논문과 차이가 나면 여기부터 본다.**"""
    r = snap["run"]
    tp = snap["throughput"]
    ff = (snap["wip"].get("ff_paper") or {})
    return [
        ("투입 모드", r.get("mode", "?")),
        ("제품 수", str(len(snap["cycle_time"].get("by_product", {})))),
        ("초기 WIP", f"{tp.get('initial_wip_lots', 0):,} lot"),
        ("실행 기간", f"{r.get('years', 0):g} 년"),
        ("warm-up", f"{r.get('warmup_minutes', 0) / YEAR_MINUTES:g} 년"
                    f"  (제외 {r.get('warmup_excluded')})"),
        ("seed", str(r.get("seed"))),
        ("superhot 예약", "켬" if r.get("superhot_reservation") else "끔"),
        ("FF 분모 출처", "Route.rpt_paper (논문 규약)" if ff else "— ff_paper 없음"),
        ("측정 lot 수", f"{ff.get('regular_n', 0):,} (regular)"),
        ("실행 시간", f"{snap.get('meta', {}).get('runtime_s', 0) / 60:.1f} 분"),
    ]


# ---------------------------------------------------------------------------
# 표 1 — 기준 작업점 FF 대조
# ---------------------------------------------------------------------------

def table1_ff(snap: dict) -> None:
    head("표 1  기준 작업점 FF 대조  (regular lot, 논문 Fig. 3)")
    ff = snap["wip"].get("ff_paper") or {}
    reg = ff.get("regular_pctl") or {}
    if not reg:
        print("  ff_paper 가 없다. `Route.rpt_paper` 가 0인 fixture 실행이다.")
        return

    print("  논문 IV-B절의 비교 조건 넷이 모두 같아야 읽을 수 있다 —")
    print("  regular만 / 8년·warm-up 1년 / 10,000 WSPW / CR 디스패칭.")
    print()
    print(f"  {pad('지표', 16)}{pad('우리', 10, True)}{pad('논문', 10, True)}"
          f"{pad('차이', 10, True)}")
    rule(50)
    for q in sorted(paper.FIG3_PCTILES):
        ours = reg.get(str(q))
        if ours is None:
            continue
        want = paper.FIG3_PCTILES[q]
        print(f"  {pad(f'FF P{q}', 16)}{ours:10.3f}{want:10.2f}"
              f"{pad(pct_diff(ours, want), 10, True)}")
    mx = ff.get("regular_max")
    if mx is not None:
        print(f"  {pad('FF 최댓값', 16)}{mx:10.3f}{paper.FIG3_MAX:10.2f}"
              f"{pad(pct_diff(mx, paper.FIG3_MAX), 10, True)}")
        print("     최댓값은 표본 수와 seed에 민감하다 — 보조 지표로만 본다.")

    span_o = reg["95"] - reg["5"]
    span_p = paper.FIG3_PCTILES[95] - paper.FIG3_PCTILES[5]
    print()
    print(f"  {pad('P5~P95 폭', 16)}{span_o:10.3f}{span_p:10.2f}"
          f"{pad(f'{span_o / span_p:.2f}배', 10, True)}")
    print("     **폭이 중앙값보다 많이 말한다.** 넓으면 느린 꼬리가 길다는 뜻이고,")
    print("     배치 대기처럼 우선순위가 못 뚫는 것이 후보다.")

    print()
    print(f"  {pad('처리량 WSPW', 16)}"
          f"{snap['throughput']['wspw_completions']:10,.1f}"
          f"{paper.WSPW_TARGET:10,.1f}"
          f"{pad(pct_diff(snap['throughput']['wspw_completions'], paper.WSPW_TARGET), 10, True)}")


# ---------------------------------------------------------------------------
# 표 2 — 제품별 RPT / CT
# ---------------------------------------------------------------------------

def table2_rpt(snap: dict, routes=None) -> None:
    head("표 2  제품별 RPT / CT 대조  (FF의 분모가 타당한가)")
    print("  RPT가 3% 낮으면 FF도 3% 높다. 분자보다 **분모를 먼저** 확인한다.")
    print("  논문 III-E-1절의 RPT는 빈 fab 실측값이고, 우리 `rpt_paper`는")
    print("  같은 규약으로 계산한 해석값이다 — 같은 양을 다른 방법으로 낸 것이다.")
    print()

    ct_p = snap["cycle_time"].get("by_product", {})
    ffx = snap["cycle_time"].get("ff_by_product_type", {})
    cols = (f"  {pad('product', 12)}{pad('우리 RPT h', 12, True)}"
            f"{pad('논문 RPT h', 12, True)}{pad('차이', 9, True)}"
            f"{pad('CT h', 11, True)}{pad('FF(reg)', 10, True)}")
    print(cols)
    rule(w(cols))

    have_rpt = routes is not None
    gaps = []
    for p in sorted(paper.TABLE_II, key=lambda k: int(k.split("_")[1])):
        prpt = paper.rpt_hours(p)
        ours = (routes[p].rpt_paper / 60.0
                if have_rpt and p in routes and routes[p].rpt_paper > 0 else None)
        ct = ct_p.get(p)
        ffr = (ffx.get(p) or {}).get("regular")
        if ours is not None and prpt:
            gaps.append(100.0 * (ours - prpt) / prpt)
        print(f"  {pad(p, 12)}"
              f"{(f'{ours:12,.1f}' if ours is not None else pad('—', 12, True))}"
              f"{prpt:12,.1f}"
              f"{pad(pct_diff(ours, prpt) if ours is not None else '—', 9, True)}"
              f"{(f'{ct:11,.1f}' if ct is not None else pad('—', 11, True))}"
              f"{(f'{ffr:10.3f}' if ffr is not None else pad('—', 10, True))}")

    if gaps:
        mean_gap = sum(gaps) / len(gaps)
        print()
        print(f"  RPT 평균 격차 {mean_gap:+.1f}%  "
              f"(최소 {min(gaps):+.1f} / 최대 {max(gaps):+.1f})")
        if abs(mean_gap) > 1.0:
            print("  ** 전 제품에서 같은 방향이면 규약 차이다 — 개별 route 오류가")
            print("     아니라 RPT 정의를 다시 볼 것 (예: 이송·표본검사 포함 여부). **")
    else:
        print()
        print("  우리 RPT를 읽으려면 워크북 파싱이 필요하다: --xlsx 를 주거나,")
        print("  기본 경로에 데이터가 있으면 자동으로 읽는다.")


# ---------------------------------------------------------------------------
# 표 3 — 모델 규칙 검증
# ---------------------------------------------------------------------------

def _verdict(ok: bool | None) -> str:
    return {True: "통과", False: "** 확인 **", None: "기록"}[ok]


def table3_rules(snap: dict) -> None:
    head("표 3  모델 규칙 검증  (성능이 맞아도 규칙이 틀릴 수 있다)")
    print("  서로 다른 오류가 상쇄되면 KPI는 맞고 규칙은 틀린다. 그래서 따로 센다.")
    print("  '기록'은 합격/불합격이 아니라 값을 남기는 항목이다.")
    print()

    c = snap.get("counters", {})
    warn = c.get("warnings", {}) or {}
    tp = snap["throughput"]
    rows: list[tuple[str, str, str, bool | None]] = []

    # -- LTL ---------------------------------------------------------------
    viol = warn.get("ltl_chain_violation", 0)
    miss = warn.get("ltl_missing_dedication", 0)
    rows.append(("LTL", "지정 장비 불일치", f"{viol:,}", viol == 0))
    rows.append(("", "지정 누락", f"{miss:,}", miss == 0))
    rows.append(("", "이력 없는 초기 WIP (별도)",
                 f"{c.get('ltl_missing_no_history', 0):,}", None))
    rows.append(("", "지정 대기 건수 / 최대",
                 f"{c.get('ltl_waits', 0):,} / "
                 f"{c.get('ltl_max_wait_min', 0) / 60:,.1f} h", None))

    # -- Batch -------------------------------------------------------------
    # 제품·공정 혼합은 **구조적으로 불가능**하다 — 그룹 키가 (제품, step)이다
    # (fab_model._batch_step). 세는 카운터가 없는 이유가 그것이다.
    rows.append(("Batch", "다른 제품·공정 혼합",
                 "구조적 0 (키=(제품,step))", None))
    rows.append(("", "형성 배치 수", f"{c.get('batches', 0):,}", None))
    rows.append(("", "순위로 재구성 / 우선순위 구제",
                 f"{c.get('batch_reorders', 0):,} / "
                 f"{c.get('batch_priority_rescues', 0):,}", None))

    # -- Breakdown / PM ----------------------------------------------------
    rows.append(("Down", "고장 / PM 발생",
                 f"{c.get('breakdowns', 0):,} / {c.get('pms', 0):,}", None))
    rows.append(("", "동시 정지 최대 / 겹친 장비",
                 f"{c.get('max_concurrent_down', 0)} / "
                 f"{c.get('tools_with_overlap', 0):,}", None))
    rows.append(("", "정지 중 신규 처리 시작", "구조적 0 (03 R10)", None))

    # -- Cascading ---------------------------------------------------------
    gate = warn.get("cascade_gate_violation", 0)
    rows.append(("Cascade", "진입 간격 위반", f"{gate:,}", gate == 0))
    rows.append(("", "진입 횟수 / 2-job 비율",
                 f"{c.get('cascade_entries', 0):,} / "
                 f"{c.get('cascading_two_job_pct', 0):.2f}%", None))
    rows.append(("", "퇴장 순서 위반", "구조적 0 (05 R3 사슬)", None))

    # -- MINRUN ------------------------------------------------------------
    mo = c.get("minrun_outcomes", {}) or {}
    rows.append(("MINRUN", "최소 충족 / 같은 setup 제한",
                 f"{mo.get('met', 0):,} / {mo.get('same-setup', 0):,}", None))
    rows.append(("", "미달 대기(idle)", f"{mo.get('idle', 0):,}", None))
    rows.append(("", "허용 예외(hot override)",
                 f"{mo.get('override', 0):,}", None))
    rows.append(("", "강제 변경(STRICT=False일 때만)",
                 f"{mo.get('change', 0):,}", mo.get("change", 0) == 0))

    # -- CQT ---------------------------------------------------------------
    op, cl = c.get("cqt_opened", 0), c.get("cqt_closed", 0)
    vi = c.get("cqt_violated", 0)
    rate = 100.0 * vi / cl if cl else 0.0
    rows.append(("CQT", "열림 / 닫힘", f"{op:,} / {cl:,}", None))
    rows.append(("", "위반 (비율)", f"{vi:,} ({rate:.2f}%)", None))
    rows.append(("", "끝까지 열린 채", f"{c.get('cqt_still_open', 0):,}", None))

    # -- 작업 수 보존 -------------------------------------------------------
    init = tp.get("initial_wip_lots", 0)
    starts, done = tp.get("lot_starts", 0), tp.get("lot_completions", 0)
    final = snap["wip"].get("final_lots", 0)
    resid = init + starts - done - final
    rows.append(("보존", "초기WIP+투입−완료−WIP",
                 f"{init:,}+{starts:,}−{done:,}−{final:,} = {resid:,}",
                 resid == 0))
    sav = c.get("step_accounting_violations", 0)
    rows.append(("", "step 계산 위반", f"{sav:,}", sav == 0))

    cols = f"  {pad('항목', 9)}{pad('확인 내용', 32)}{pad('값', 34)}판정"
    print(cols)
    rule(w(cols) + 2)
    for grp, what, val, ok in rows:
        print(f"  {pad(grp, 9)}{pad(what, 32)}{pad(val, 34)}{_verdict(ok)}")

    bad = [r for r in rows if r[3] is False]
    print()
    print("  CQT 위반 0건은 합격 조건이 아니다 — 레퍼런스 자체가 CQT를 구현하지")
    print("  않았고(AS 3.13절), 우리도 측정만 한다.")
    if bad:
        print(f"\n  ** 확인이 필요한 항목 {len(bad)}개 — 위 표에서 '확인' 표시 **")
    else:
        print("\n  합격 조건이 있는 항목은 모두 통과했다.")
    if warn:
        print("\n  실행 중 경고 (0이 아니면 그 자체가 발견이다):")
        for k, v in sorted(warn.items()):
            print(f"     {pad(k, 30)}{v:,}")


# ---------------------------------------------------------------------------
# 보조 KPI
# ---------------------------------------------------------------------------

def aux_kpis(snap: dict) -> None:
    head("보조 KPI  (결과가 우연히 맞은 것은 아닌지)")

    # -- WIP 추이 ----------------------------------------------------------
    weekly = snap["wip"].get("weekly_lots") or []
    start_w = int(snap["run"].get("warmup_minutes", 0) / WEEK_MINUTES)
    post = weekly[start_w:]
    print("  [WIP 추이]  작업이 계속 쌓이는 불안정한 실행인지 본다.")
    if len(post) >= 20:
        n = len(post)
        first, last = post[:n // 4], post[-n // 4:]
        f_m = sum(first) / len(first)
        l_m = sum(last) / len(last)
        drift = 100.0 * (l_m - f_m) / f_m if f_m else 0.0
        print(f"     관측 구간 {n}주, 평균 {sum(post) / n:,.0f} lot")
        print(f"     앞 1/4 {f_m:,.0f}  ->  뒤 1/4 {l_m:,.0f}   "
              f"변화 {drift:+.1f}%")
        if abs(drift) > 5.0:
            print("     ** WIP이 한 방향으로 움직인다. 정상상태가 아닐 수 있고,")
            print("        그러면 완료 lot만 본 FF는 느린 lot을 빼고 잰 값이다. **")
        else:
            print("     평평하다 — 완료 lot 기준 통계를 읽어도 된다.")
    else:
        print(f"     주간 표본이 {len(post)}개뿐이라 추세를 말할 수 없다.")

    # -- 제품별 CT ---------------------------------------------------------
    sp = snap["cycle_time"].get("spread_by_product", {})
    if sp:
        print("\n  [제품별 CT]  전체 FF가 비슷해도 특정 제품만 느려질 수 있다.")
        cols = (f"     {pad('product', 12)}{pad('mean h', 10, True)}"
                f"{pad('P50', 10, True)}{pad('P95', 10, True)}"
                f"{pad('n', 9, True)}")
        print(cols)
        for p in sorted(sp, key=lambda k: int(k.split("_")[1])):
            v = sp[p]
            print(f"     {pad(p, 12)}{v.get('mean', 0):10,.1f}"
                  f"{v.get('p50', 0):10,.1f}{v.get('p95', 0):10,.1f}"
                  f"{v.get('n', 0):9,}")

    # -- 우선순위별 --------------------------------------------------------
    ff = snap["wip"].get("ff_paper") or {}
    ct_t = snap["cycle_time"].get("by_lot_type", {})
    cnt = snap["cycle_time"].get("count_by_lot_type", {})
    print("\n  [우선순위별]  우선순위 처리가 의도대로 작동하는가.")
    cols = (f"     {pad('종류', 10)}{pad('CT h', 11, True)}"
            f"{pad('FF 평균', 10, True)}{pad('FF P50', 10, True)}"
            f"{pad('n', 10, True)}")
    print(cols)
    for t in ("regular", "hot", "superhot"):
        pctl = ff.get(f"{t}_pctl") or {}
        mean = ff.get(f"{t}_mean") or (ff.get("regular_mean") if t == "regular" else None)
        p50 = pctl.get("50")
        print(f"     {pad(t, 10)}{ct_t.get(t, 0):11,.1f}"
              f"{(f'{mean:10.3f}' if mean else pad('—', 10, True))}"
              f"{(f'{p50:10.3f}' if p50 else pad('—', 10, True))}"
              f"{cnt.get(t, 0):10,}")
    print(f"     논문: hot은 FF가 1.0에 가깝다고만 적는다 "
          f"(HOT_FF_ABOUT = {paper.HOT_FF_ABOUT}).")

    # -- 배치 --------------------------------------------------------------
    c = snap.get("counters", {})
    b = snap["cycle_time"].get("buckets_h", {}) or {}
    print("\n  [배치]  저부하에서 배치를 기다리느라 FF가 높아지는 현상 확인.")
    print(f"     형성 배치 {c.get('batches', 0):,}개")
    wb = b.get("wait_batch")
    if wb is not None:
        print(f"     lot당 배치 형성 대기 평균 {wb:,.1f} h")
    print("     배치별 수량·충진율·대기는 상세 DB와 노트북의 배치 운영 표에서 확인한다.")

    # -- 대기시간 구성 ------------------------------------------------------
    if b:
        print("\n  [대기시간 구성]  논문과 차이가 날 때 원인을 여기서 찾는다.")
        tot = sum(v for v in b.values() if isinstance(v, (int, float)))
        for k, v in sorted(b.items(), key=lambda kv: -kv[1]):
            share = 100.0 * v / tot if tot else 0.0
            bar = "#" * int(round(share / 2))
            print(f"     {pad(k, 14)}{v:9,.1f} h  {share:5.1f}%  {bar}")
        print(f"     {pad('합계', 14)}{tot:9,.1f} h")


# ---------------------------------------------------------------------------
# 표 4 — 논문 Table IV
# ---------------------------------------------------------------------------
#
# **논문과 같은 정의로 낸 우리 측정값이다.** 논문 숫자를 베껴 온 표가 아니다.
# 칸의 뜻이 논문과 같아야 같은 칸에 놓을 수 있으므로, 집계를 논문에 맞춘다:
# 분모는 가동 가능 시간(전체 − 정지), 집계는 tool group 단순평균.
# 논문값 자체는 `paper.TABLE_IV`에 있다 — 비교는 읽는 사람이 한다.

TABLE4_AREAS = (
    ("Dielectric", "Dielectric"), ("Diffusion", "Diffusion"),
    ("Dry_Etch", "Dry Etch"), ("Implant", "Implant"),
    ("Litho", "Lithography"), ("Planar", "Planar"),
    ("TF", "Thin Films"), ("Wet_Etch", "Wet Etch"),
    ("Def_Met", "Defectivity Metrology"), ("Litho_Met", "Litho Metrology"),
    ("TF_Met", "Thin Films Metrology"),
)
TABLE4_COLUMNS = (
    ("tool_groups", "Number of tool groups"), ("tools", "Number of tools"),
    ("availability_pct", "Average Availability"), ("sdt_pct", "Proportion of SDT"),
    ("average_utilization_pct", "Average Utilization"),
    ("maximum_utilization_pct", "Maximum Utilization"),
)
TABLE4_NOTES = (
    "시뮬레이션 측정값이며 논문의 숫자를 복사한 표가 아닙니다. Delay 등 장비가 없는 그룹은 제외합니다.",
    "네 지표 모두 area 안의 tool group **단순평균**입니다. 장비 수로 가중하지 않습니다 — 논문 표와 같은 집계입니다.",
    "Average Availability = tool group별 (100 − unavail_pct)의 평균. 고장과 PM이 겹친 시간은 한 번만 뺍니다.",
    "Proportion of SDT = tool group별 pm_pct / (pm_pct + down_pct)의 평균. 정지 사건 누계 기준이며, 겹친 사건은 각각 셉니다.",
    "Average Utilization = tool group별 util_pct / 가용률의 평균. **분모가 전체 시간이 아니라 가동 가능 시간(전체 − 정지)입니다** — 논문과 같은 정의입니다.",
    "Maximum Utilization은 같은 값의 tool group 최댓값입니다. 개별 장비 최댓값이 아닙니다.",
    "Cascading에서 두 job이 겹친 시간은 한 번만 셉니다. 따라서 이용률은 100%를 넘지 않습니다. 2026-09-21 이전 스냅샷은 두 번 세던 때라 넘을 수 있습니다.",
    "스냅샷의 반올림된 값으로 계산합니다. 기록이 없거나 분모가 0인 지표는 —로 표시합니다.",
)


def tg_table4(tg: dict, years: float) -> dict | None:
    """tool group 하나를 논문 정의로 환산한다. 장비가 없으면(delay) None.

    옛 스냅샷에는 tool group별 `unavail_pct`가 없다. 그때는 `down_h`/`pm_h`
    에서 유도하되 **겹친 정지를 두 번 세므로** 가용률이 조금 낮게 나온다.
    유도했는지를 돌려주고, 표가 그것을 알린다.
    """
    tools = tg.get("tools", 0)
    if not tools:
        return None

    derived = False
    down, pm = tg.get("down_pct"), tg.get("pm_pct")
    if down is None or pm is None:
        cap_h = years * 365.0 * 24.0 * tools
        if cap_h <= 0:
            return None
        down = 100.0 * tg.get("down_h", 0.0) / cap_h
        pm = 100.0 * tg.get("pm_h", 0.0) / cap_h
        derived = True

    unavail = tg.get("unavail_pct")
    if unavail is None:
        unavail = down + pm
        derived = True

    avail = 100.0 - unavail
    if avail <= 0:
        return None
    return {
        "tools": tools,
        "avail": avail,
        "sdt": 100.0 * pm / (down + pm) if (down + pm) > 0 else 0.0,
        # **분모가 가동 가능 시간이다.** 스냅샷의 `util_pct`는 전체 시간 기준
        # 이므로 가용률로 나눠야 논문과 같은 양이 된다 (paper.TABLE_IV 주석).
        "util": 100.0 * tg.get("util_pct", 0.0) / avail,
        "derived": derived,
    }


def table4_rows(snap: dict) -> list[dict]:
    """area별 Table IV 한 행씩. **논문과 같은 양으로 낸다.** 입력은 안 건드린다."""
    groups: dict[str, list[dict]] = {}
    for group in (snap.get("toolgroups") or {}).values():
        if group.get("tools", 0) > 0:
            groups.setdefault(group["area"], []).append(group)

    years = float(snap.get("run", {}).get("years", 0.0))
    order = list(TABLE4_AREAS)
    known = {key for key, _ in order}
    order.extend((key, key) for key in sorted(groups) if key not in known)

    rows: list[dict] = []
    derived_any = False
    for key, label in order:
        members = groups.get(key)
        if not members:
            continue
        ms = [m for m in (tg_table4(g, years) for g in members) if m is not None]
        if not ms:
            continue
        n = len(ms)
        derived_any = derived_any or any(m["derived"] for m in ms)
        rows.append({
            "area": label,
            "tool_groups": n,
            "tools": sum(m["tools"] for m in ms),
            # 넷 다 tool group **단순평균**. 최대가동률만 최댓값이다.
            "availability_pct": sum(m["avail"] for m in ms) / n,
            "sdt_pct": sum(m["sdt"] for m in ms) / n,
            "average_utilization_pct": sum(m["util"] for m in ms) / n,
            "maximum_utilization_pct": max(m["util"] for m in ms),
            "ref": paper.TABLE_IV.get(key),
        })
    total = {key: None for key, _ in TABLE4_COLUMNS}
    for key in ("tool_groups", "tools"):
        if rows:
            total[key] = sum(row[key] for row in rows)
    rows.append({"area": "Total", **total, "ref": None})
    for row in rows:
        row["derived"] = derived_any
    return rows


def _table4_value(row: dict, key: str) -> str:
    value = row.get(key)
    if value is None:
        return "—"
    return f"{value:,}" if key in ("tool_groups", "tools") else f"{value:.1f}%"


def _table4_conditions(snap: dict) -> str:
    run = snap.get("run", {})
    warmup = run.get("warmup_minutes")
    warmup_text = f"{warmup / YEAR_MINUTES:g}" if warmup is not None else "?"
    # 장비 지표의 구간을 **스냅샷이 말하게 한다.** 고정 문구로 두면 코드가
    # 바뀐 뒤에도 옛 설명이 그대로 찍힌다 — 실제로 그랬다.
    scope = ("warm-up excluded" if run.get("tool_metrics_post_warmup")
             else "full run (includes warm-up)")
    return (f"mode={run.get('mode', '?')}, seed={run.get('seed', '?')}, "
            f"duration={run.get('years', '?')} y, CT warm-up={warmup_text} y; "
            f"equipment statistics: {scope}")


def _table4_scope_note(snap: dict) -> str:
    """장비 지표의 구간에 대한 한 줄. 옛 스냅샷이면 경고가 된다."""
    if snap.get("run", {}).get("tool_metrics_post_warmup"):
        return "장비 지표는 CT와 **같은 구간**(warm-up 제외)입니다."
    return ("** 장비 지표가 warm-up을 포함한 전체 실행 기간 기준입니다 — CT의 "
            "warm-up 제외 구간과 다릅니다. 빈 fab이 차오르는 구간이 섞여 "
            "가동률이 낮게 나옵니다. 현재 코드로 다시 돌리면 해소됩니다. **")


def table4_characteristics(snap: dict) -> None:
    head("표 4  Main Characteristics and Performance Measures of Dataset 2")
    print(f"  {_table4_conditions(snap)}")
    rows = table4_rows(snap)
    if rows and rows[0].get("derived"):
        print("  ** tool group별 `unavail_pct`가 없는 옛 스냅샷이라 고장+PM으로")
        print("     유도했습니다. 겹친 정지를 두 번 세므로 가용률이 조금 낮습니다. **")
    print()
    print(f"  {pad('Area', 24)}{pad('Population', 18, True)}"
          f"{pad('Availability', 24, True)}{pad('Utilization', 24, True)}")
    labels = ("Groups", "Tools", "Average", "SDT share", "Average", "Maximum")
    widths = (9, 9, 12, 12, 12, 12)
    print("  " + pad("", 24) + "".join(pad(s, n, True) for s, n in zip(labels, widths)))
    rule(92)
    for row in rows:
        if row["area"] == "Total":
            rule(92)
        print("  " + pad(row["area"], 24) + "".join(
            pad(_table4_value(row, key), width, True)
            for (key, _), width in zip(TABLE4_COLUMNS, widths)))
    print()
    print(f"  · {_table4_scope_note(snap)}")
    for note in TABLE4_NOTES:
        print(f"  · {note}")


# ---------------------------------------------------------------------------
# area / toolgroup 장비 성능
# ---------------------------------------------------------------------------

def table_areas(snap: dict) -> None:
    head("필수 4  area별 장비 상태 분해  (병목 위치)")
    print("  논문 Table IV 와 맞대는 것은 **표 4**다. 이 표는 그 격차가 어느")
    print("  상태에서 나왔는지 보려고 상태를 전부 펼친 것이다.")
    print("  util 은 **적어도 하나가 일하던 시간의 합집합**이다. 오른쪽 세 열")
    print("  (proc/setup/load-ul)은 job별 총합이라, cascading area 에서는 셋의")
    print("  합이 util 보다 크다. 그 차이가 곧 겹친 시간이다.")
    print()
    cols = (f"  {pad('area', 12)}{pad('util', 8, True)}{pad('proc', 8, True)}"
            f"{pad('setup', 8, True)}{pad('load/ul', 9, True)}"
            f"{pad('down', 8, True)}{pad('pm', 8, True)}{pad('idle', 8, True)}"
            f"{pad('WIP', 9, True)}")
    print(cols)
    rule(w(cols))
    areas = snap.get("areas", {})
    order = [a for a in AREA_ORDER if a in areas] + \
            [a for a in sorted(areas) if a not in AREA_ORDER]
    for a in order:
        v = areas[a]
        print(f"  {pad(a, 12)}{v.get('util_pct', 0):8.2f}"
              f"{v.get('proc_pct', 0):8.2f}{v.get('setup_pct', 0):8.2f}"
              f"{area_loadunload_pct(v):9.2f}{v.get('down_pct', 0):8.2f}"
              f"{v.get('pm_pct', 0):8.2f}{v.get('idle_pct', 0):8.2f}"
              f"{v.get('wip_lots_avg', 0):9.1f}")

    # 병목 — util 상위 toolgroup
    tgs = snap.get("toolgroups", {})
    if tgs:
        print("\n  병목 후보 — util 상위 12 tool group")
        cols = (f"     {pad('tool group', 22)}{pad('area', 12)}"
                f"{pad('대수', 6, True)}{pad('util', 8, True)}"
                f"{pad('대기 평균h', 11, True)}{pad('대기 최대h', 11, True)}")
        print(cols)
        top = sorted(tgs.items(), key=lambda kv: -kv[1].get("util_pct", 0))[:12]
        for name, v in top:
            print(f"     {pad(name, 22)}{pad(v.get('area', ''), 12)}"
                  f"{v.get('tools', 0):6d}{v.get('util_pct', 0):8.2f}"
                  f"{v.get('wait_mean_h', 0):11.2f}{v.get('wait_max_h', 0):11.1f}")
        print("     논문은 litho를 fab 제약으로 설계했다고 적는다 (IV-A절).")


# ---------------------------------------------------------------------------
# 부하 스윕 결과 읽기 — **돌리는 것은 `run.py sweep` 이다**
# ---------------------------------------------------------------------------

def _one_model_only(files: list[Path]) -> list[Path]:
    """한 곡선에는 **같은 코드로 나온 점만** 남긴다.

    `read_sweep`이 폴더의 파일을 전부 읽기 때문에, 코드를 고친 뒤 일부 부하만
    다시 돌리면 옛 점과 새 점이 한 곡선에 섞인다. 그림은 그려지고 원인은
    아무 데도 안 보인다 — 곡선의 굴곡이 모델 차이인지 부하 차이인지 구분할
    방법이 없다.

    **어느 것이 옳은지 고르지 않는다.** 가장 최근 파일의 `meta.model_hash`를
    기준으로 삼고, 다른 것은 빼면서 **뺐다고 말한다.** 조용히 고르면 이
    함수가 없는 것과 같다.

    `model_hash`가 없는 옛 스냅샷은 서로 구분할 수 없으므로 한 무리로 본다.
    """
    if len(files) < 2:
        return files
    tagged = []
    for p in files:
        try:
            meta = json.loads(p.read_text(encoding="utf-8")).get("meta", {})
        except (OSError, json.JSONDecodeError):
            continue
        tagged.append((p, meta.get("model_hash") or "(기록 없음)"))
    if not tagged:
        return files
    groups = {h for _, h in tagged}
    if len(groups) == 1:
        return [p for p, _ in tagged]

    newest = max(tagged, key=lambda t: t[0].stat().st_mtime)[1]
    keep = [p for p, h in tagged if h == newest]
    drop = [(p, h) for p, h in tagged if h != newest]
    print(f"\n  ** 스윕 폴더에 서로 다른 코드로 나온 점이 섞여 있다 "
          f"({len(groups)}종). 가장 최근 것({newest})만 잇는다. **")
    for p, h in drop:
        print(f"     제외: {p.name}  (model_hash {h})")
    print("     같은 곡선에 놓으려면 그 부하들을 다시 돌려야 한다.")
    return keep


def read_sweep(sweep_dir: Path) -> list[dict]:
    """스윕 결과를 operating curve 입력 형태로.

    **디스크의 파일이 기준이고 `sweep_index.json`은 보조다.** 예전에는 목록이
    있으면 거기 적힌 것만 읽었는데, 그러면 `run.py --load 0.92 --out <여기>`로
    따로 만들어 넣은 점이 조용히 무시된다 — 곡선에 안 나타나는데 파일은
    있으니 원인을 찾기 어렵다.

    목록은 실투입량(`wspw_completions`)을 채우는 데만 쓴다. 파일 이름에는
    목표 부하만 들어 있고 실제로 얼마가 나왔는지는 없기 때문이다.
    """
    by_file: dict[str, dict] = {}
    idx = sweep_dir / "sweep_index.json"
    if idx.exists():
        try:
            for e in json.loads(idx.read_text(encoding="utf-8")):
                by_file[e["file"]] = e
        except (json.JSONDecodeError, KeyError, TypeError):
            pass                      # 목록이 깨져도 파일은 읽을 수 있다

    files = sorted(sweep_dir.glob("run_load*.json"))
    files = _one_model_only(files)

    out = []
    for p in files:
        entry = by_file.get(p.name, {})
        load = entry.get("load")
        if load is None:              # 목록에 없는 점 — 이름에서 읽는다
            try:
                load = float(p.name.split("load")[1].split("_")[0])
            except (IndexError, ValueError):
                continue
        ff = load_snapshot(p)["wip"].get("ff_paper") or {}
        if not ff.get("regular_pctl"):
            continue
        out.append({"load": float(load),
                    "wspw": entry.get("wspw_completions", 0.0),
                    "pctl": ff["regular_pctl"],
                    # P0/P100. 옛 스냅샷에는 `regular_min` 이 없어 None 이 되고,
                    # 그리는 쪽이 그 선만 빼고 나머지를 그린다.
                    "min": ff.get("regular_min"),
                    "max": ff.get("regular_max")})
    return out


def table_sweep(points: list[dict]) -> None:
    if not points:
        return
    head("필수 1  Operating curve — 부하별 FF 분위수")
    print("  논문 IV-B절: dataset 2는 **저부하에서 상위 분위수가 오히려 커질 수**")
    print("  있다. 배치가 늦게 모이기 때문이고, 단조 증가를 합격 조건으로 삼으면")
    print("  안 된다.")
    print()
    # 논문 Fig. 3 과 같은 일곱 줄: P0 · P5 · P25 · P50 · P75 · P95 · P100.
    # P0/P100 은 보간한 분위수가 아니라 **실제 가장 빠른/느린 lot** 이라
    # `min`/`max` 에서 온다.
    cols = (f"  {pad('load', 8, True)}{pad('WSPW', 10, True)}{pad('P0', 9, True)}"
            + "".join(pad(f'P{q}', 9, True) for q in (5, 25, 50, 75, 95))
            + pad("P100", 9, True))
    print(cols)
    rule(w(cols))
    for s in sorted(points, key=lambda x: x["load"]):
        row = f"  {s['load']:8.3f}{s['wspw']:10,.0f}"
        row += f"{s['min']:9.3f}" if s.get("min") else pad("—", 9, True)
        for q in (5, 25, 50, 75, 95):
            v = s["pctl"].get(str(q))
            row += f"{v:9.3f}" if v is not None else pad("—", 9, True)
        row += f"{s['max']:9.3f}" if s.get("max") else pad("—", 9, True)
        print(row)
    rule(w(cols))
    x_ref = paper.WSPW_TARGET / paper.WSPW_CAPACITY
    row = f"  {x_ref:8.3f}{paper.WSPW_TARGET:10,.0f}{pad('—', 9, True)}"
    for q in (5, 25, 50, 75, 95):
        row += f"{paper.FIG3_PCTILES[q]:9.2f}"
    row += f"{paper.FIG3_MAX:9.2f}"
    print(f"{row}   <- 논문 Fig. 3 (P0 은 논문 본문에 수치가 없다)")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def table_flow(snap):
    detail = snap.get("detail")
    if not detail:
        return
    head("상세 흐름 KPI: 대기 부담 · route step · station")
    print("  시간은 warm-up 이후만 집계하며 초기 WIP·미완료 방문도 포함한다.")
    print("  완료 방문 평균은 warm-up 이후 시작·완료한 방문만 사용한다.")
    for dim in ("toolgroup", "route_step"):
        print(f"\n  [{dim}] 대기 lot-hour 상위 10개")
        print(f"  {'key':<28} {'wait lot-h':>12} {'mean h':>10} {'live':>6} {'rework':>7}")
        for key, v in sorted(detail["flow"][dim].items(), key=lambda kv: -kv[1]["wait_lot_h"])[:10]:
            mean = f"{v['wait_mean_h']:.3f}" if v["wait_mean_h"] is not None else "—"
            print(f"  {key:<28} {v['wait_lot_h']:12.2f} {mean:>10} {v['censored']:6} {v['reworks']:7}")
    print("\n  [station] 실제 작업시간 합집합 기준 가동률 상위 10개")
    for key, v in sorted(detail["stations"].items(), key=lambda kv: -(kv[1]["util_pct"] or 0))[:10]:
        util = f"{v['util_pct']:.2f}%" if v['util_pct'] is not None else "—"
        print(f"  {key:<28} util {util:>8}  jobs {v['jobs_now']}  down {v['down_now']}")
    print(f"\n  방문 {detail['visits']:,} · 배치 {detail['batches']:,} · 정지 사건 {detail['downtime_events']:,}")
    print(f"  상세 DB: {detail['database']}")


def report(snap_path: Path, *, sweep_dir: Path | None,
           xlsx: Path | None) -> None:
    snap = load_snapshot(snap_path)

    head("검증 리포트  —  논문 (Kopp et al. 2020) 대조")
    print(f"  스냅샷: {snap_path}")
    print()
    print("  [실행 조건]  논문과 차이가 나면 **먼저 이 조건이 같은지** 본다.")
    for k, v in run_conditions(snap):
        print(f"     {pad(k, 16)}{v}")
    # 레퍼런스와 갈리는 지점을 **둘로 나눠서** 말한다. 하나로 묶어 두었더니
    # explicit 으로 돌린 실행에서 "초기 WIP 없음" 이 통째로 사라졌다 —
    # 조건이 둘인데 판정이 하나면 한쪽이 조용히 묻힌다.
    gaps: list[str] = []
    if snap["run"].get("mode") != "explicit":
        gaps.append(
            "투입이 parametric 이다. 투입 간격과 납기 규칙은 논문과 같지만"
            "(고정 간격 + z ~ U(0.8,1.2)) z 는 우리가 뽑은 표본이다 — "
            "레퍼런스는 시트에 박힌 표본을 쓴다")
    if not snap["throughput"].get("initial_wip_lots"):
        gaps.append(
            "초기 WIP 없이 **빈 fab**에서 출발했다 — 레퍼런스는 "
            "WIP.txt 의 2,156 lot 으로 시작한다")
    if gaps:
        print()
        for g in gaps:
            for i, line in enumerate(_wrap(g, 66)):
                print(f"  {'**' if i == 0 else '  '} {line}")
        print("     위 조건이 다르므로 아래 논문 대조는 **위치 확인용 참고**이지")
        print("     합격선이 아니다. 직접 대조는 원본 저장소의 explicit 실행이 한다. **")

    sw = read_sweep(sweep_dir) if sweep_dir else []
    table_sweep(sw)
    table1_ff(snap)

    routes = None
    if xlsx is not False:
        try:
            from .data_parser import parse_workbook
            routes = parse_workbook(path=xlsx, release_mode="parametric").routes
        except Exception as exc:                     # noqa: BLE001
            print(f"\n  (워크북을 읽지 못했다 — 표 2의 우리 RPT는 생략: {exc})")
    table2_rpt(snap, routes)

    head("필수 3  산출량")
    tp = snap["throughput"]
    rows = [
        ("투입 WSPW", tp.get("wspw_releases", 0.0), paper.WSPW_TARGET),
        ("완성 WSPW", tp.get("wspw_completions", 0.0), paper.WSPW_TARGET),
    ]
    print(f"  {pad('지표', 16)}{pad('우리', 12, True)}{pad('논문', 12, True)}"
          f"{pad('차이', 10, True)}")
    rule(52)
    for name, ours, ref in rows:
        print(f"  {pad(name, 16)}{ours:12,.1f}{ref:12,.1f}"
              f"{pad(pct_diff(ours, ref), 10, True)}")
    ratio = (tp.get("wspw_completions", 0.0) / tp.get("wspw_releases", 1.0)
             if tp.get("wspw_releases") else 0.0)
    print(f"  {pad('산출/투입', 16)}{ratio:12.4f}{1.0:12.4f}"
          f"{pad(pct_diff(ratio, 1.0), 10, True)}")
    print("     1.0 에서 벗어나면 WIP이 쌓이거나 빠지는 중이다 — "
          "`visualize.py` 의 [13] WIP와 함께 볼 것.")

    table4_characteristics(snap)
    table_areas(snap)
    table3_rules(snap)
    aux_kpis(snap)
    table_flow(snap)

    print()
    rule(78, "=")
    print("  그림은 `python visualize.py` 가 그린다 — 여기서 따로 그리지 않는다.")
    print("  [13] WIP 시계열, [14] operating curve 가 위 표와 짝이다.")
    print()
    print("  **논문값에 맞추려고 파라미터를 조정하지 않는다.** 격차는 로직을")
    print("  다시 볼 이유이지 숫자를 고칠 이유가 아니다.")
    rule(78, "=")


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m smt2020_sim.kpi_collector")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("report", help="스냅샷을 읽어 표를 만든다")
    r.add_argument("--snap", type=Path, default=None,
                   help="실행 스냅샷 JSON. 없으면 가장 최근 것")
    r.add_argument("--sweep", type=Path, default=None,
                   help="부하 스윕 결과 디렉터리. 생략하면 config.SWEEP_DIR")
    r.add_argument("--xlsx", type=Path, default=None,
                   help="워크북 경로. 표 2의 우리 RPT를 읽는다")
    r.add_argument("--no-xlsx", dest="xlsx", action="store_const", const=False,
                   help="워크북을 읽지 않는다 (빠르다)")

    # `table4` 만 따로 찍는 명령은 두지 않는다 — `report` 가 이미 그 표를
    # 포함하고(`table4_characteristics`), 같은 것을 두 경로로 내면 언젠가
    # 한쪽만 고쳐진다. 그림으로 보려면 `visualize.py` 의 칸 [15]다.
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["report"])

    snap_path = args.snap or latest_snapshot()

    sweep_dir = args.sweep or config.SWEEP_DIR
    report(snap_path, sweep_dir=sweep_dir if sweep_dir.is_dir() else None,
           xlsx=args.xlsx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
