"""시뮬레이션 실행 진입점.

    python -m smt2020_sim.main --years 2 --seed 42

진행 표시는 stderr로 간다. stdout은 `KPI <name> <value>` 전용이다
(AGENTS.md 7.1절). phase 간 기계적 diff 대상이기 때문이다.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Sequence

import simpy

from . import config
from .data_parser import parse_workbook
from .fab_model import FabModel, offered_load
from .kpi_collector import KPICollector, snapshot_path, write_snapshot

YEAR_MINUTES = 365 * 24 * 60
WEEK_MINUTES = 7 * 24 * 60
MONTH_MINUTES = YEAR_MINUTES / 12.0

# 이 phase에서 구현된 기능. 체크 레지스트리의 requires 판정에 쓴다.
# phase가 진행되며 여기에 하나씩 추가된다.
FEATURES: frozenset[str] = frozenset(
    {"breakdown", "pm", "batch", "cascading", "setup", "dispatch", "ltl",
     "sampling", "rework", "cqt", "reservation",
     # Phase 12 — 구현된 **기능**이지 켜진 모드가 아니다. 파서와 투입 분기가
     # 있으면 참이고, 실제로 explicit으로 도는지는 `ModelData`가 정한다.
     "explicit_release", "initial_wip"})


# WIP 상승 추세로 볼 기준. 최근 몇 틱을 보고, 그 사이 상승률이 이보다 크면
# 알린다. 8년 실행에서 정상상태가 깨지는 유일한 모양이 이것이다 — 대기열이
# 자라기 시작하면 그 뒤 값은 전부 '아직 채워지는 중'이지 정상상태가 아니다.
WIP_TREND_TICKS = 4
WIP_TREND_RISE = 0.15
# 창의 길이는 **틱 수가 아니라 시뮬레이션 시간**으로 정한다. 틱 수로 고정했더니
# 간격이 바뀔 때 뜻이 같이 바뀌었다 — 8년 실행(3개월 간격)에서 4틱은 1년이라
# 제대로 걸렀지만, 1년 실행(0.5개월 간격)에서 4틱은 2개월이라 **빈 fab이
# 채워지는 구간 안에 통째로 들어가** 거짓 경보를 냈다. phase 11 실행에서
# 2.0/2.5개월에 두 번 뜬 것이 그것이다.
#
# 6개월은 fill(약 2~3 CT = 3~4개월)보다 길어 램프를 잡지 않고, 1년 실행
# 안에는 들어가 감시 기능이 살아 있다.
WIP_TREND_MONTHS = 6.0


def wip_trend_ticks(every_months: float) -> int:
    """`every_months` 간격에서 WIP 추세 창을 몇 틱으로 볼 것인가.

    최소 `WIP_TREND_TICKS`틱은 본다. 두세 점으로는 단조성이 우연히
    성립해서 시끄럽다. 간격이 촘촘하면 같은 시간을 덮도록 틱을 더 본다.
    """
    if every_months <= 0:
        return WIP_TREND_TICKS
    return max(WIP_TREND_TICKS, math.ceil(WIP_TREND_MONTHS / every_months))


def progress_interval(years: float, ticks: int = 32,
                      minimum_months: float = 0.25) -> float:
    """진행 표시 간격(개월). **간격이 아니라 개수로 정한다.**

    간격을 고정하면 실행 길이가 바뀔 때마다 망가진다. 3개월로 고정해 두었더니
    0.5년 실행에서 틱이 둘뿐이었고, 그중 마지막은 정확히 종료 시각이라
    `env.run(until=T)`가 처리하지 않아 **한 줄만** 나왔다. 반대로 20년을
    1개월로 돌리면 240줄이 되어 못 읽는다.

    `minimum_months`는 1주보다 촘촘하게 찍지 않기 위한 바닥이다. 아주 짧은
    실행에서는 틱이 목표 개수보다 적게 나온다 — 그쪽이 낫다.
    """
    return max(minimum_months, years * 12.0 / ticks)


def wip_is_rising(history, ticks=WIP_TREND_TICKS, rise=WIP_TREND_RISE) -> bool:
    """최근 `ticks`개가 **단조 증가하면서** 그만큼 올랐는가.

    둘 다 요구하는 이유가 있다. 상승률만 보면 오르내리다 우연히 끝값이 높은
    경우를 잡아 매 틱마다 시끄럽고, 단조성만 보면 1씩 오르는 것도 잡는다.
    정상상태의 WIP은 평평하게 흔들리므로 둘이 동시에 성립하기 어렵다.
    """
    if len(history) < ticks:
        return False
    window = history[-ticks:]
    base = window[0]
    if base <= 0:
        return False
    return (all(b >= a for a, b in zip(window, window[1:]))
            and (window[-1] - base) / base > rise)


def _progress(env, fab, kpi, *, every=MONTH_MINUTES, total_minutes=None,
              log_path=None, warmup_minutes=0.0, stream=None):
    """진행 상황을 주기적으로 한 줄씩. 긴 실행의 감시 장치다.

    기본 출력은 stderr다 — `python -m smt2020_sim.main`의 stdout은
    `KPI <name> <value>` 전용이기 때문이다 (AGENTS.md 7.1절). 하지만
    `run_all.py`/`run_experiment.py`처럼 stdout을 파싱하지 않는 쪽에서는
    `stream=sys.stdout`을 준다. 두 흐름을 섞으면 IDE나 파이프에서 각자
    버퍼링되어 **진행 줄이 표 한가운데나 맨 끝에 뭉쳐서** 나온다. 실제로
    그랬다. 한 흐름으로 몰면 순서가 보장된다.

    `sim mo/s`(실제 1초당 진행한 시뮬레이션 개월)를 같이 찍는다. 이 값이 뒤로
    갈수록 떨어지면 WIP 증가에 따라 비용이 커지고 있다는 뜻이고, 일정하면
    선형이다. 오래 걸릴 때 "멈춘 건지 도는 건지"를 구분하는 용도이기도 하다.

    한 줄 요약 외에 **두 가지를 더 본다.** 8년짜리를 끝까지 돌린 뒤에야
    이상을 아는 것은 너무 늦다.

    * **새 경고** — `fab.warnings`는 "0이 아니면 그 자체가 발견"인 채널이다
      (AGENTS.md 7.4절). 직전 틱 이후 늘어난 것만 골라 그 자리에서 알린다.
    * **WIP 상승 추세** — warm-up이 지난 뒤에도 WIP이 계속 오르면 정상상태가
      아니다. 그 뒤 KPI는 '아직 채워지는 중'인 값이라 평균을 내도 의미가 없다.

    `log_path`를 주면 같은 내용을 TSV로도 남긴다. **틱마다 열고 닫는다** —
    `env.run(until=...)`은 프로세스를 중간에 버리므로 `finally`가 돌지 않아
    핸들을 들고 있으면 파일이 비어 있게 된다. 이렇게 하면 실행 중에 다른
    창에서 열어 봐도 항상 마지막 틱까지 들어 있다.

    이 프로세스는 `env.timeout`만 쓰므로 난수를 소비하지 않는다. 진행 표시
    간격을 바꿔도 결과가 흔들리지 않는다.
    """
    out = stream if stream is not None else sys.stderr
    # 틱 간격을 개월로 보고 필요한 소수 자릿수를 정한다. 간격이 정수면 0자리,
    # 아니면 이웃한 두 틱이 구분될 만큼만.
    every_months = every / MONTH_MINUTES
    month_dec = (0 if abs(every_months - round(every_months)) < 1e-9
                 else 1 if every_months >= 0.1 else 2)
    # 이 실행에서 WIP 추세 창이 몇 틱인가. 간격에서 유도한다 — 고정된 틱 수는
    # 간격이 바뀌면 다른 뜻이 된다 (`WIP_TREND_MONTHS` 주석).
    trend_ticks = wip_trend_ticks(every_months)
    t0 = time.perf_counter()
    n = 0
    last_wall = t0
    last_sim = 0.0
    last_wafers = 0
    seen_warnings: dict[str, int] = {}
    wip_history: list[int] = []

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "tick\tsim_years\twip\treleased\tcompleted\twspw\telapsed_s\tnote\n",
            encoding="utf-8")

    while True:
        yield env.timeout(every)
        n += 1
        wall = time.perf_counter()
        d_wall = max(wall - last_wall, 1e-9)
        d_sim = env.now - last_sim
        rate = d_sim / MONTH_MINUTES / d_wall
        last_wall, last_sim = wall, env.now

        wip = len(fab.live_lots)
        wip_history.append(wip)

        # **직전 틱 이후의 평균 완료율이다.** 원래는 "마지막 한 주의 완료
        # wafer 수"를 찍었는데, 표본 하나라 7,700~12,800처럼 크게 흔들려
        # 추세를 읽을 수 없었다. 이름은 같고 뜻이 다른 값이 두 개가 되는
        # 것도 나빴다 — KPI의 `throughput_wspw_completions`는 warm-up 이후
        # **모든 주의 평균**이다. 틱 간격으로 평균내면 3개월 실행에서 13주
        # 평균이 되어 그 KPI와 바로 견줄 수 있다.
        done_wafers = sum(kpi.weekly_completions.values())
        d_weeks = d_sim / WEEK_MINUTES
        wspw = (done_wafers - last_wafers) / d_weeks if d_weeks > 0 else 0.0
        last_wafers = done_wafers
        eta = ""
        if total_minutes:
            done = env.now / total_minutes
            if done > 0:
                left = (wall - t0) * (1 - done) / done
                eta = f"  eta {left / 60:.0f}m" if left > 90 else f"  eta {left:.0f}s"
        # **도달한 시각을 틱 간격의 단위로 찍는다.** 처음에는 틱 번호
        # (`[m  1]`)였는데 'm'이 month로 읽혀 8년 실행에서 오해를 샀고,
        # 다음엔 연 단위(`0.04y`)로 바꿨더니 간격이 0.5개월인 실행에서
        # 소수점 둘째 자리만 움직여 읽을 수가 없었다.
        #
        # 자릿수를 **간격에서 유도한다** — 고정하면 간격이 바뀔 때 또 어긋난다.
        # 3개월 간격이면 3, 6, 9 / 0.5개월이면 0.5, 1.0, 1.5로 떨어진다.
        # Phase 12 — 초기 WIP이 있으면 장부가 `wip == rel + init - done`이다.
        # `init`을 안 찍으면 2,156만큼 안 맞는 것처럼 보여, 멈춘 실행을
        # 알아보는 데 쓰던 눈대중이 망가진다. 없으면 예전 그대로 찍는다.
        init = f" init={kpi.n_initial_wip:5d}" if kpi.n_initial_wip else ""
        print(f"[{env.now / MONTH_MINUTES:6.{month_dec}f}개월] wip={wip:6d} "
              f"rel={kpi.n_released:7d}{init} done={kpi.n_completed:7d} "
              f"wspw={wspw:7,.0f}  {rate:5.2f} sim mo/s  "
              f"elapsed {wall - t0:5.0f}s{eta}", file=out, flush=True)

        notes = []

        # 1) 직전 틱 이후 새로 생기거나 늘어난 경고
        fresh = {k: v - seen_warnings.get(k, 0) for k, v in fab.warnings.items()}
        fresh = {k: d for k, d in fresh.items() if d > 0}
        if fresh:
            seen_warnings.update(fab.warnings)
            notes.append("새 경고 " + ", ".join(
                f"{k} +{d:,}" for k, d in sorted(fresh.items())))

        # 2) warm-up을 지났는데도 WIP이 계속 오르는가
        if env.now >= warmup_minutes and wip_is_rising(wip_history,
                                                       ticks=trend_ticks):
            notes.append(
                f"WIP 상승 추세 {wip_history[-trend_ticks]:,} -> {wip:,} "
                f"(최근 {trend_ticks}틱 = {trend_ticks * every_months:.3g}개월 "
                f"단조 증가) — 정상상태가 아니다")

        for note in notes:
            print(f"        !! {note}", file=out, flush=True)

        if log_path is not None:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{n}\t{env.now / YEAR_MINUTES:.3f}\t{wip}\t"
                         f"{kpi.n_released}\t{kpi.n_completed}\t{wspw:.0f}\t"
                         f"{wall - t0:.0f}\t{' | '.join(notes)}\n")


def run(*, years: float, seed: int, warmup_years: float,
        xlsx: Path | None = None, progress: bool = True,
        release_mode: str | None = None, reservation: bool | None = None):
    """시뮬레이션 1회. (kpi, fab, runtime_s) 반환.

    `release_mode`는 **파싱에 넘긴다** — 모드는 데이터의 성질이고, 실행은
    `ModelData`에 explicit 일정이 들어 있는지만 본다 (Phase 12 R1/R2).

    `reservation`은 반대로 **실행에 넘긴다** — 같은 데이터로 두 설정을 돌리는
    것이 이 플래그의 용도이기 때문이다 (`config.SUPERHOT_RESERVATION`).
    """
    data = parse_workbook(xlsx or config.XLSX_PATH, release_mode=release_mode)
    return run_with_data(data, years=years, seed=seed,
                         warmup_years=warmup_years, progress=progress,
                         reservation=reservation)


def run_with_data(data, *, years: float, seed: int, warmup_years: float = 0.0,
                  progress: bool = False, progress_every_months: float = 1.0,
                  progress_log: Path | None = None, progress_stream=None,
                  reservation: bool | None = None, detail_dir: Path | None = None,
                  detail_level: str | None = None):
    """이미 파싱된 데이터로 실행한다. 테스트가 이 경로를 쓴다.

    `progress_log`를 주면 진행 상황을 TSV로도 남긴다 — 긴 실행을 다른 창에서
    들여다보기 위한 것이다. 진행 프로세스는 난수를 소비하지 않으므로 켜고 끄는
    것이 결과를 바꾸지 않는다.

    `reservation=None`이면 `config.SUPERHOT_RESERVATION`. 진행 프로세스와 달리
    **이건 난수 순서를 바꾼다** — 두 설정의 실행은 seed가 같아도 다른 표본이다.

    warm-up이 있으면 `env.run`을 **그 경계에서 한 번 끊는다.** 장비 누계의
    기준선을 떠서 UTIL%/SETUP%를 lot 지표와 같은 구간으로 맞추기 위한 것이다
    (`KPICollector.mark_warmup`). 끊는 것 자체는 결과를 바꾸지 않는다 —
    SimPy는 `run(until=t)` 뒤에 이어서 돌릴 수 있고 프로세스가 죽지 않으며,
    `mark_warmup`은 난수도 이벤트도 쓰지 않는다. 같은 seed에서 KPI가 움직이면
    그건 이 줄이 부수효과를 냈다는 뜻이다.
    """
    if not (0 < years < float('inf') and 0 <= warmup_years < years):
        raise ValueError('years는 유한한 양수, warmup_years는 0 이상 years 미만이어야 합니다.')
    env = simpy.Environment()
    rng = random.Random(seed)                       # R13: 인스턴스 하나만
    warmup_minutes = warmup_years * YEAR_MINUTES
    kpi = KPICollector(env, warmup_minutes=warmup_minutes, detail_dir=detail_dir,
                       detail_level=detail_level or config.KPI_DETAIL_LEVEL)
    fab = FabModel(env, data, kpi, rng, reservation=reservation)
    fab.start()
    total = years * YEAR_MINUTES
    if progress:
        env.process(_progress(env, fab, kpi,
                              every=progress_every_months * MONTH_MINUTES,
                              total_minutes=total, log_path=progress_log,
                              warmup_minutes=warmup_minutes,
                              stream=progress_stream))

    t0 = time.perf_counter()
    if 0.0 < warmup_minutes < total:
        env.run(until=warmup_minutes)
        kpi.mark_warmup(fab)
    env.run(until=total)
    return kpi, fab, time.perf_counter() - t0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m smt2020_sim.main")
    ap.add_argument("--years", type=float, default=2.0)
    # warm-up 기본값은 0. Phase 09 이전에는 정상상태가 없어 잘라낼 것이 없고,
    # warm-up >= years면 표본이 0개가 되어 모든 KPI가 조용히 0으로 나온다.
    ap.add_argument("--warmup-years", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    # 기본값은 일부러 phase 번호가 아니다. 그냥 돌렸다가 phase 기준선
    # 스냅샷을 덮어쓰는 사고를 막는다. 기준선은 run_all.py의 PHASE로 찍는다.
    ap.add_argument("--phase", default="dev", help="스냅샷 파일 이름에 들어간다")
    ap.add_argument("--mode", default="parametric")
    ap.add_argument("--xlsx", type=Path, default=config.XLSX_PATH)
    ap.add_argument("--no-snapshot", action="store_true")
    ap.add_argument('--detail-level',choices=('lean','full'),default=config.KPI_DETAIL_LEVEL)
    ap.add_argument("--report-areas", action="store_true")
    ap.add_argument("--report-overload", action="store_true",
                    help="toolgroup별 필요 부하 / 용량 표")
    ap.add_argument("--quiet", action="store_true", help="진행 표시를 끈다")
    ap.add_argument("--progress-months", type=float, default=1.0,
                    help="진행 표시 간격(시뮬레이션 개월). 기본 1")
    # super hot 예약. 기본값은 config가 정한다 (xlsx SUPERHOTLOT = 켬).
    # `--no-reservation`이 레퍼런스가 실제로 돌린 구성이다 — `.asd`의 어느
    # 주문 파일에도 HOTLOT이 yes가 아니다.
    ap.add_argument("--reservation", dest="reservation", action="store_true",
                    default=None, help="super hot 장비 예약을 켠다")
    ap.add_argument("--no-reservation", dest="reservation",
                    action="store_false",
                    help="예약을 끈다 (레퍼런스 `.asd`의 구성). 순위 30은 남는다")
    args = ap.parse_args(argv)

    print(f"parsing {args.xlsx.name} ...", file=sys.stderr)
    data = parse_workbook(args.xlsx)

    if args.report_overload:
        rho = offered_load(data, batching="batch" in FEATURES,
                           cascading="cascading" in FEATURES,
                           sampling="sampling" in FEATURES,
                           rework="rework" in FEATURES)
        over = {g: v for g, v in rho.items() if v > 1.0}
        print(f"\n-- offered load / capacity  ({len(over)} of {len(rho)} above 1.0) --",
              file=sys.stderr)
        for g, v in sorted(rho.items(), key=lambda kv: -kv[1])[:12]:
            spec = data.toolgroups[g]
            flag = "  <== over" if v > 1.0 else ""
            print(f"  {g:<22} {v:6.3f}   {spec.area:<12} {spec.num_tools:>3} tools{flag}",
                  file=sys.stderr)

    tracemalloc.start()
    print(f"running {args.years:g} y, seed {args.seed} ...", file=sys.stderr)
    kpi, fab, runtime = run_with_data(
        data, years=args.years, seed=args.seed,
        warmup_years=args.warmup_years, progress=not args.quiet,
        progress_every_months=args.progress_months,
        reservation=args.reservation,detail_level=args.detail_level)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    snap = kpi.snapshot(phase=args.phase, mode=args.mode, seed=args.seed,
                        years=args.years, features=sorted(FEATURES),
                        runtime_s=runtime, fab=fab,
                        peak_mem_mb=peak / (1024 * 1024))

    kpi.print_kpi_lines(snap)

    if args.report_areas:
        print("\n-- area states (%) --", file=sys.stderr)
        print(f"  {'area':<12} {'util':>7} {'proc':>7} {'setup':>7} {'down':>7} "
              f"{'pm':>7} {'idle':>7} {'wip':>8}", file=sys.stderr)
        for area, v in snap["areas"].items():
            print(f"  {area:<12} {v['util_pct']:7.2f} {v['proc_pct']:7.2f} "
                  f"{v['setup_pct']:7.2f} {v['down_pct']:7.2f} {v['pm_pct']:7.2f} "
                  f"{v['idle_pct']:7.2f} {v['wip_lots']:8.1f}", file=sys.stderr)

    print(f"\nruntime {runtime:.1f} s, peak mem {peak / 1024 / 1024:.0f} MB",
          file=sys.stderr)

    if not args.no_snapshot:
        p = write_snapshot(snap, snapshot_path(
            phase=args.phase, mode=args.mode, seed=args.seed, years=args.years))
        print(f"wrote {p}", file=sys.stderr)
    kpi.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
