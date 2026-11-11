"""실행기 기록 확장 — 켜고 끄기: 시나리오 JSON 의 "extensions".

    "extensions": {"ic": true, "work_active": false}

생략한 항목은 DEFAULTS 를 따른다. 확장은 **관찰만** 한다 (난수·디스패칭·투입에 손대지 않는다).
켜고 꺼도 시뮬레이션 결과(투입·완료·CT·WIP)는 같다 (테스트로 확인).

  ic           1시간마다 툴그룹별 대기 lot 수와 대기 경과시간 합 → ic_hourly.parquet
               논문(Liu et al. 2023)의 IC = α·SRA + (1-α)·DRA 를 L2 에서 계산할 수 있게 한다.
                 SRA 재료: 이 툴그룹 앞에서 기다리는 lot (일반 대기열 / 배치 수량 미달 / 배치 수량 충족)
                 DRA 재료: 이 툴그룹에서 직전에 처리된 lot 이 지금 다른 곳에서 기다리는 것
               배치 수량 미달 대기는 따로 남긴다 — 장비 부족이 아니라 짝을 기다리는 시간이기 때문이다.
  work_active  장비별 '작업만' 활성 구간 (고장·PM 은 활성으로 세지 않음) → active_periods_work.parquet
               기본 active_periods.parquet(작업 또는 정지)와 비교해 수리 시간이 병목 라벨을 끌고 가는지 본다.

새 확장을 붙이려면 Extension 을 상속해 REGISTRY 에 넣으면 된다.
"""
from __future__ import annotations

import math
from array import array
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULTS = {"ic": True, "work_active": True}


class PeriodTracker:
    """키(장비·툴그룹)별 연속 구간 기록. 같은 시각에 꺼졌다 켜지면(0분 공백) 끊지 않고 이어 붙인다."""

    def __init__(self, n: int):
        self.start = [None] * n
        self.pending = [None] * n
        self.cols = {"key": array("i"), "start_min": array("d"), "end_min": array("d"), "censored": array("b")}

    def on(self, k: int, now: float) -> None:
        p = self.pending[k]
        if p is not None:
            if now - p > 1e-9:
                self._emit(k, p, False)
            self.pending[k] = None
        if self.start[k] is None:
            self.start[k] = now

    def off(self, k: int, now: float) -> None:
        if self.start[k] is not None and self.pending[k] is None:
            self.pending[k] = now

    def _emit(self, k: int, end: float, censored: bool) -> None:
        c = self.cols
        c["key"].append(k)
        c["start_min"].append(self.start[k])
        c["end_min"].append(end)
        c["censored"].append(censored)
        self.start[k] = None

    def finalize(self, now: float) -> None:
        for k in range(len(self.start)):
            if self.pending[k] is not None:
                self._emit(k, self.pending[k], False)
                self.pending[k] = None
            elif self.start[k] is not None:
                self._emit(k, now, True)

    def frame(self, key_name: str) -> pd.DataFrame:
        df = pd.DataFrame({k: np.frombuffer(v, dtype=v.typecode) if len(v) else np.array([], dtype=v.typecode)
                           for k, v in self.cols.items()})
        return df.rename(columns={"key": key_name})


class Extension:
    name = ""
    station_hook = False          # True 면 장비 작업·정지 수가 바뀔 때마다 on_station 이 불린다
    visit_hook = False            # True 면 방문 완료마다 on_visit_completed 가 불린다

    def __init__(self, options=True):
        self.options = options if isinstance(options, dict) else {}

    def attach(self, env, fab, rec) -> None:
        self.env, self.fab, self.rec = env, fab, rec

    def processes(self) -> list:
        return []

    def on_station(self, i: int, nwork: int, nbd: int, npm: int, now: float) -> None:
        pass

    def on_visit_completed(self, lot, tg_name: str) -> None:
        pass

    def finalize(self, now: float) -> None:
        pass

    def write(self, out: Path) -> dict:
        """파일을 쓰고 run_meta 에 남길 요약을 돌려준다."""
        return {}


class WorkActive(Extension):
    name = "work_active"
    station_hook = True

    def attach(self, env, fab, rec):
        super().attach(env, fab, rec)
        n = len(rec.stations)
        self.tracker = PeriodTracker(n)
        self.working = [False] * n

    def on_station(self, i, nwork, nbd, npm, now):
        w = nwork > 0
        if w != self.working[i]:
            self.working[i] = w
            (self.tracker.on if w else self.tracker.off)(i, now)

    def finalize(self, now):
        self.tracker.finalize(now)

    def write(self, out):
        df = self.tracker.frame("station")
        df.to_parquet(out / "active_periods_work.parquet", index=False)
        return {"periods": len(df)}


class ICLog(Extension):
    """1시간마다 툴그룹별 대기 스냅샷 (lot 수, 대기 경과시간 합[분])."""
    name = "ic"
    visit_hook = True
    COLS = ("q_n", "q_age_min", "bs_n", "bs_age_min", "br_n", "br_age_min",
            "dra_q_n", "dra_q_age_min", "dra_bs_n", "dra_bs_age_min")

    def attach(self, env, fab, rec):
        super().attach(env, fab, rec)
        self.tg_index = rec.tg_index
        self.last_tg: dict = {}                              # lot_id -> 직전에 처리를 마친 툴그룹 번호
        self.arr = np.zeros((len(self.COLS), len(rec.toolgroups), rec.H))
        self.tools = [(self.tg_index[n], tg) for n, tg in fab.toolgroups.items() if tg.tools]

    def on_visit_completed(self, lot, tg_name):
        if self.fab.toolgroups[tg_name].tools:               # delay 툴그룹은 '처리'가 아니다
            self.last_tg[lot.lot_id] = self.tg_index[tg_name]

    def processes(self):
        return [self._monitor()]

    def _monitor(self):
        env, a, last = self.env, self.arr, self.last_tg
        while True:
            yield env.timeout(60.0)
            now = env.now
            h = int(now // 60.0) - 1
            if h >= a.shape[2]:
                continue
            for g, tg in self.tools:
                for lot in tg.queue:
                    since = lot.queue_since if lot.queue_since is not None else lot._bucket_since
                    age = now - since
                    a[0, g, h] += 1
                    a[1, g, h] += age
                    p = last.get(lot.lot_id)
                    if p is not None:
                        a[6, p, h] += 1
                        a[7, p, h] += age
                for key, waiting in tg.batch_queue.items():
                    if not waiting:
                        continue
                    ready = tg.batch_wafers[key] >= waiting[0].current_step().batch_min
                    ci, di = (4, 6) if ready else (2, 8)
                    for lot in waiting:
                        age = now - (lot.batch_since if lot.batch_since is not None else now)
                        a[ci, g, h] += 1
                        a[ci + 1, g, h] += age
                        p = last.get(lot.lot_id)
                        if p is not None:
                            a[di, p, h] += 1
                            a[di + 1, p, h] += age
            if h % 168 == 0:                                   # 끝난 lot 정리 (메모리)
                live = self.fab.live_lots
                for k in [k for k in last if k not in live]:
                    del last[k]

    def write(self, out):
        H = int(math.ceil(self.env.now / 60.0))
        tg, hr = np.meshgrid(np.arange(len(self.rec.toolgroups)), np.arange(H), indexing="ij")
        df = pd.DataFrame({"hour": hr.ravel().astype(np.int32),
                           "toolgroup": pd.Categorical.from_codes(tg.ravel(), self.rec.toolgroups)})
        for k, c in enumerate(self.COLS):
            df[c] = self.arr[k, :, :H].ravel().astype(np.float32)
        df.to_parquet(out / "ic_hourly.parquet", index=False)
        return {"hours": H}


REGISTRY = {cls.name: cls for cls in (ICLog, WorkActive)}


def build(spec: dict) -> list:
    cfg = {**DEFAULTS, **spec.get("extensions", {})}
    unknown = set(cfg) - set(REGISTRY)
    if unknown:
        raise ValueError(f"알 수 없는 확장: {sorted(unknown)} (가능: {sorted(REGISTRY)})")
    return [REGISTRY[n](opt) for n, opt in cfg.items() if opt]
