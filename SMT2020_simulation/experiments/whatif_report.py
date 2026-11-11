"""What-if 실험 보고서 — 같은 시나리오의 seed 묶음에서 KPI 평균과 95% CI를 낸다 (D-57, LLM연계_검토.md 6절).

    python -B experiments/whatif_report.py --runs "results/<폴더>/W102_s*" --base "results/pilot_20261014/P00_base_s*" "results/pilot2_20261014/N_base_s*" --window 26 52

--runs, --base 는 실행 폴더(runner.py 출력) 여러 개 또는 glob 패턴이다.
--base 를 주면 seed 가 같은 기준 실행과 짝 차이(시나리오 − 기준)를 낸다. 모델 해시가 다르면 멈춘다.

지표 (측정 창 [w0, w1) 주):
  가동률    util_avail = 가동 / (가동 + 유휴), 툴그룹별 (bnpred/labels.py 와 같은 정의)
  대기 lot  툴그룹별 평균 대기 lot (대기열 + 배치 대기 + 정지 대기)
  WIP       모든 툴그룹 lot-분 합 / 60 / 시간 = 평균 재공 lot
  처리량    창 안에 끝난 regular lot / 주 (전체, 제품 그룹별)
  CT        그 lot 들의 (완료 − 투입) 평균 일 (전체, 제품 그룹별)
  (11-04 추가) 처리량 전체(모든 lot 유형), 처리량·CT hot(hot+superhot), 납기 준수 regular·hot(완료 ≤ 납기, %).
            hot 전환(hot_conversion) 시나리오에서는 regular 처리량이 전환분만큼 줄어드므로 처리량 전체와 hot 을 같이 본다
  병목 상위 5  seed 평균 가동률 순. seed 별 상위 5 에 든 횟수를 같이 쓴다
95% CI 는 seed 를 표본으로 한 t 구간이다 (seed 1개면 CI 없음).

분기 실행 (C1, 11-05): run_meta 에 branch 가 있으면 짝 번호는 seed 대신 갈래 번호(branch.seed)다. 짝지은 두 실행은
시작 상태(seed, 분기 주)가 같아야 한다 (다르면 멈춘다).
주별 궤적 (11-05): 짝이 있으면 측정 창의 주별 WIP 차이와 누적 완료 regular lot 차이의 평균 ± CI 를 낸다.
  최고점   짝마다 주별 WIP 차이의 최댓값 → 평균 ± CI, 평균 곡선이 가장 높은 주
  회복 주  평균 곡선 최고 주 뒤에 처음으로 평균 주별 WIP 차이가 최고값의 25% 이하로 내려온 주 (없으면 창 안에서
           회복 안 됨). "CI 가 0 을 포함" 으로 정하면 주별 잡음(±50~100 lot)으로 CI 가 넓어진 주를 회복으로 잘못 잡는다
  사건형 질의(정지·급주문 등)는 창 평균보다 이 궤적으로 본다.

정상상태 점검 (보고서에서 숫자보다 먼저 본다):
  WIP 추세  seed 마다 측정 창의 주별 WIP 를 주에 회귀한 기울기. seed 평균 기울기의 95% CI 하한이 0 보다 크고,
            창 동안 늘어난 양(기울기 × 창 길이)이 평균 WIP 의 --trend-tol(기본 5%)을 넘으면 "WIP 계속 증가"다.
            이때 평균값은 창 길이에 따라 달라지므로 그대로 비교하면 안 된다.
  warm-up   seed 평균 일별 WIP 에 MSER-5 를 적용한 절단점(주, Welch 식으로 seed 를 먼저 평균). 측정 창 시작보다
            늦으면 경고한다. seed 마다 따로 하면 주별 WIP 진동(±5%) 때문에 9~34주로 흔들린다(기준 seed 5개, 11-03).
            기준 믹스는 seed 평균 절단점 22주, 24주에 WIP 가 정상의 99.7% 라서 기본 창을 26~52주로 둔다.
출력: <out>/report.md  summary.json  kpi.csv  toolgroups.csv  runs.csv
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
HASH_EQUIV_FILE = Path(__file__).resolve().parent / "model_hash_equiv.json"


def canonical_hash(h: str) -> str:
    """결과가 같다고 확인한 지문 묶음(model_hash_equiv.json, 11-09 디스패칭 베타)이면 묶음의 첫 지문, 아니면 그대로."""
    try:
        groups = json.loads(HASH_EQUIV_FILE.read_text(encoding="utf-8")).get("groups") or []
    except (OSError, ValueError):
        return h
    return next((g["hashes"][0] for g in groups if h in g.get("hashes", [])), h)
sys.dont_write_bytecode = True

W = 168                                                    # 주당 시간
LOTMIN = ["lotmin_wait_queue", "lotmin_wait_batch", "lotmin_wait_down",
          "lotmin_on_tool", "lotmin_transport", "lotmin_delay"]
WAIT = ["lotmin_wait_queue", "lotmin_wait_batch", "lotmin_wait_down"]


def expand(patterns: list[str]) -> list[Path]:
    out = []
    for p in patterns:
        q = p if Path(p).is_absolute() else str(ROOT / p)
        hits = sorted(glob.glob(q)) or [q]
        out += [Path(h) for h in hits if (Path(h) / "run_meta.json").exists()]
        if not any((Path(h) / "run_meta.json").exists() for h in hits):
            raise SystemExit(f"[whatif] 실행 폴더가 없다: {p}")
    return out


def ci(a) -> tuple[float, float, float]:
    """(평균, 표준편차, 95% CI 반폭). 값이 1개면 표준편차·반폭은 nan."""
    a = np.asarray(a, float)
    if len(a) == 0:
        return np.nan, np.nan, np.nan
    if len(a) < 2:
        return float(a.mean()), np.nan, np.nan
    sd = a.std(ddof=1)
    return float(a.mean()), float(sd), float(stats.t.ppf(0.975, len(a) - 1) * sd / np.sqrt(len(a)))


def mser5(x: np.ndarray) -> tuple[int, bool]:
    """MSER-5 절단점(일)과 탐색 상한(앞 절반)에 닿았는지."""
    m = len(x) // 5
    z = x[:m * 5].reshape(m, 5).mean(1)
    stat = [((z[d:] - z[d:].mean()) ** 2).sum() / (m - d) ** 2 for d in range(m // 2 + 1)]
    d = int(np.argmin(stat))
    return d * 5, d >= m // 2


def product_groups(grouping: str | None) -> dict[str, str]:
    cfg = json.loads((ROOT / "datagen" / "product_groups.json").read_text(encoding="utf-8-sig"))
    groups = cfg["groupings"][grouping or cfg["default"]]["groups"]
    return {p: g for g, ps in groups.items() for p in ps}


def load_run(d: Path, w0: float, w1: float) -> dict:
    spec = json.loads((d / "spec.json").read_text(encoding="utf-8"))
    meta = json.loads((d / "run_meta.json").read_text(encoding="utf-8"))
    if w1 > float(meta["end_week"]):
        raise SystemExit(f"[whatif] {d.name}: 측정 창 끝 {w1}주가 실행 끝 {meta['end_week']}주보다 늦다")
    h0, h1, hend = int(w0 * W), int(w1 * W), int(float(meta["end_week"]) * W)
    h = pd.read_parquet(d / "tg_hourly.parquet", columns=["hour", "toolgroup", *LOTMIN, "st_work", "st_idle"])
    lots_h = (h.groupby("hour")[LOTMIN].sum().sum(axis=1) / 60).reindex(range(hend), fill_value=0.0).to_numpy()
    win = h[(h.hour >= h0) & (h.hour < h1)]
    g = win.groupby("toolgroup", observed=True)
    work, idle = g["st_work"].sum(), g["st_idle"].sum()
    util = (work / (work + idle)).where(work + idle > 0)
    queue = g[WAIT].sum().sum(axis=1) / 60 / (h1 - h0)
    nw = (h1 - h0) // W
    weekly = lots_h[h0:h0 + nw * W].reshape(nw, W).mean(1)
    slope = float(np.polyfit(np.arange(len(weekly)), weekly, 1)[0]) if len(weekly) >= 3 else np.nan
    daily = lots_h[: hend // 24 * 24].reshape(-1, 24).mean(1)
    cut_days, at_bound = mser5(daily)
    lots = pd.read_parquet(d / "lots.parquet", columns=["product", "lot_type", "release_min", "due_min", "end_min", "status"])
    fin = lots[(lots.status == "completed") & (lots.end_min >= h0 * 60) & (lots.end_min < h1 * 60)].copy()
    fin["ct"] = (fin.end_min - fin.release_min) / 1440
    fin["ontime"] = fin.end_min <= fin.due_min
    done = fin[fin.lot_type == "regular"].copy()
    hot = fin[fin.lot_type.isin(["hot", "superhot"])]
    done["group"] = done["product"].map(product_groups(spec["spec"].get("grouping")))
    kpi = {"WIP (lot)": float(lots_h[h0:h1].mean()),
           "처리량 (lot/주)": len(done) / (w1 - w0), "CT (일)": float(done.ct.mean())}
    for grp, t in done.groupby("group"):
        kpi[f"처리량 {grp} (lot/주)"] = len(t) / (w1 - w0)
        kpi[f"CT {grp} (일)"] = float(t.ct.mean())
    kpi["처리량 전체 (lot/주)"] = len(fin) / (w1 - w0)                    # regular + hot + superhot
    kpi["처리량 hot (lot/주)"] = len(hot) / (w1 - w0)                     # hot + superhot (hot 전환분 포함)
    kpi["CT hot (일)"] = float(hot.ct.mean()) if len(hot) else np.nan
    kpi["납기 준수 regular (%)"] = 100 * float(done.ontime.mean()) if len(done) else np.nan
    kpi["납기 준수 hot (%)"] = 100 * float(hot.ontime.mean()) if len(hot) else np.nan
    br = meta.get("branch")                                   # 분기 실행이면 짝 번호 = 갈래 번호
    wk_done = np.histogram((done.end_min.to_numpy() / 60 - h0) / W, bins=np.arange(nw + 1))[0] if nw else np.array([])
    return {"name": d.name, "dir": d, "seed": int(br["seed"]) if br else int(meta["seed"]),
            "state": (int(meta["seed"]), float(br["week"])) if br else None,
            "hash": meta["model_hash"], "spec": spec,
            "end_week": float(meta["end_week"]), "util": util, "queue": queue, "kpi": kpi,
            "wip_slope": slope, "mser_week": cut_days / 7, "mser_at_bound": at_bound, "daily_wip": daily,
            "weekly_wip": weekly, "weekly_done": wk_done}


def load_all(dirs: list[Path], w0, w1, tag: str) -> list[dict]:
    out, t0 = [], time.time()
    for i, d in enumerate(dirs, 1):
        r = load_run(d, w0, w1)
        out.append(r)
        print(f"[whatif] {tag} {i}/{len(dirs)} {r['name']} (seed {r['seed']}): WIP {r['kpi']['WIP (lot)']:.0f}, "
              f"기울기 {r['wip_slope']:+.2f} lot/주, MSER-5 {r['mser_week']:.1f}주  ({time.time() - t0:.0f}s)", flush=True)
    seeds = [r["seed"] for r in out]
    if len(set(seeds)) < len(seeds):
        raise SystemExit(f"[whatif] {tag}에 같은 seed가 두 번 있다: {seeds}")
    return out


def steady_state(runs: list[dict], w0, w1, tol: float) -> dict:
    s_mean, _, s_hw = ci([r["wip_slope"] for r in runs])
    wip = np.mean([r["kpi"]["WIP (lot)"] for r in runs])
    rise = s_mean * (w1 - w0) / wip
    if len(runs) < 2:
        growing, note = rise > tol, "seed 1개라 검정 없이 증가량만 봤다"
    else:
        growing, note = (s_mean - s_hw > 0) and rise > tol, ""
    n = min(len(r["daily_wip"]) for r in runs)
    cut_days, at_bound = mser5(np.mean([r["daily_wip"][:n] for r in runs], axis=0))
    return {"wip_slope": s_mean, "wip_slope_ci": s_hw, "rise_frac": rise, "growing": bool(growing), "note": note,
            "mser_week": cut_days / 7, "mser_at_bound": at_bound, "warmup_late": bool(cut_days / 7 > w0 or at_bound),
            "mser_weeks_per_seed": [round(r["mser_week"], 1) for r in runs]}


def describe(spec: dict) -> list[str]:
    lines = []
    for ph in spec["phases"]:
        top = ph["static_top5"][0]
        grp = ", ".join(f"{g} {v:,.0f}" for g, v in ph["group_wspw_regular"].items())
        lines.append(f"- {ph['start_week']}주부터: 총 {ph['total_wspw']:,.0f} WSPW (regular {grp}), "
                     f"정적 가동률 1위 {top[0]} {top[1]:.1f}%" + (f" — check 문제: {'; '.join(ph['problems'])}"
                                                               if ph["problems"] else ""))
    for e in spec.get("downtime", []):
        lines.append(f"- 다운타임: {json.dumps(e, ensure_ascii=False)}")
    return lines


def jsonable(x):
    """nan → None, numpy 값 → 파이썬 값 (summary.json 을 표준 JSON 으로)."""
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if np.isnan(x) else float(x)
    return x


def fmt(m, hw, nd=1) -> str:
    return f"{m:,.{nd}f}" + ("" if np.isnan(hw) else f" ± {hw:,.{nd}f}")


def trajectory(pairs: list, w0: float) -> tuple[dict, dict] | tuple[None, None]:
    """짝마다 주별 WIP 차이·누적 완료 차이 → 주별 평균 ± CI, 최고점, 회복 주."""
    n = min(len(r["weekly_wip"]) for r, _ in pairs) if pairs else 0
    if n == 0:
        return None, None
    dw = np.array([r["weekly_wip"][:n] - b["weekly_wip"][:n] for r, b in pairs])
    dc = np.array([np.cumsum(r["weekly_done"][:n] - b["weekly_done"][:n]) for r, b in pairs])
    wm, wh = zip(*[ci(dw[:, i])[0::2] for i in range(n)])
    cm, chw = zip(*[ci(dc[:, i])[0::2] for i in range(n)])
    weeks = [w0 + i for i in range(n)]
    i_pk = int(np.argmax(wm))
    rec = next((weeks[i] for i in range(i_pk + 1, n) if wm[i] <= 0.25 * wm[i_pk]), None) if wm[i_pk] > 0 else None
    pm, _, ph = ci(dw.max(axis=1))
    traj = {"week": weeks, "wip_diff": list(wm), "wip_diff_ci": list(wh), "cum_done_diff": list(cm), "cum_done_diff_ci": list(chw)}
    event = {"peak_wip_diff": pm, "peak_wip_diff_ci": ph, "peak_week": weeks[i_pk], "recovery_week": rec,
             "cum_done_diff_end": cm[-1], "cum_done_diff_end_ci": chw[-1],
             "peak_wip_diff_per_pair": [float(x) for x in dw.max(axis=1)]}
    return traj, event


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="시나리오 실행 폴더 또는 glob")
    ap.add_argument("--base", nargs="*", default=[], help="기준 실행 폴더 또는 glob (seed로 짝)")
    ap.add_argument("--window", nargs=2, type=float, default=[26.0, 52.0], metavar=("W0", "W1"))
    ap.add_argument("--trend-tol", type=float, default=0.05, help="WIP 계속 증가로 볼 창 안 증가 비율")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    w0, w1 = args.window
    if not (0 <= w0 < w1):
        raise SystemExit("[whatif] --window 은 0 ≤ W0 < W1 이어야 한다")

    runs = load_all(expand(args.runs), w0, w1, "시나리오")
    base = load_all(expand(args.base), w0, w1, "기준") if args.base else []
    hashes = {canonical_hash(r["hash"]) for r in runs + base}           # 11-09: 결과가 같다고 확인한 지문 묶음은 같은 모델로 본다
    if len(hashes) > 1:
        raise SystemExit(f"[whatif] 모델 해시가 섞였다: {sorted({r['hash'] for r in runs + base})} "
                         f"(같다고 확인한 묶음은 experiments/model_hash_equiv.json)")
    raw_hashes = sorted({r["hash"] for r in runs + base})
    name = re.sub(r"_[sb]\d+$", "", runs[0]["spec"]["spec"]["name"])     # seed·갈래 꼬리표만 뗀다
    phases = [json.dumps(r["spec"]["phases"], sort_keys=True) for r in runs]
    if len(set(phases)) > 1:
        print("[whatif] 경고: seed 끼리 시나리오(phase)가 다르다. 같은 시나리오만 묶어야 한다", flush=True)
    late_phase = [ph["start_week"] for ph in runs[0]["spec"]["phases"] if w0 < ph["start_week"] < w1]
    out = args.out or ROOT / "results" / "whatif_reports" / f"{name}_{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=False)

    by_seed = {r["seed"]: r for r in base}
    pairs = [(r, by_seed[r["seed"]]) for r in runs if r["seed"] in by_seed]
    if base and len(pairs) < len(runs):
        miss = sorted(r["seed"] for r in runs if r["seed"] not in by_seed)
        print(f"[whatif] 경고: 기준 실행이 없는 seed {miss} 는 짝 차이에서 뺀다", flush=True)
    bad = [(r["name"], r["state"], b["name"], b["state"]) for r, b in pairs if r["state"] != b["state"]]
    if bad:                                                   # 분기 실행은 같은 시작 상태끼리만 짝짓는다
        raise SystemExit(f"[whatif] 시작 상태(seed, 분기 주)가 다른 실행끼리 짝지었다: {bad[:3]}")
    states = {r["state"] for r in runs}
    traj, event = trajectory(pairs, w0)

    # KPI
    keys = list(dict.fromkeys(k for r in runs for k in r["kpi"]))
    krows = []
    for k in keys:
        m, sd, hw = ci([r["kpi"].get(k, np.nan) for r in runs])
        row = {"지표": k, "평균": m, "SD": sd, "CI 반폭": hw, "n": len(runs)}
        if pairs:
            row["기준 평균"] = ci([b["kpi"].get(k, np.nan) for _, b in pairs])[0]
            dm, _, dhw = ci([r["kpi"].get(k, np.nan) - b["kpi"].get(k, np.nan) for r, b in pairs])
            row.update({"차이": dm, "차이 CI 반폭": dhw, "짝 n": len(pairs)})
        krows.append(row)
    kpi = pd.DataFrame(krows)
    kpi.to_csv(out / "kpi.csv", index=False, encoding="utf-8-sig")

    # 툴그룹
    U = pd.concat([r["util"] for r in runs], axis=1)
    Q = pd.concat([r["queue"] for r in runs], axis=1)
    tg = pd.DataFrame({"가동률 평균": U.mean(axis=1), "가동률 SD": U.std(axis=1, ddof=1),
                       "대기 lot 평균": Q.mean(axis=1)})
    tg["가동률 CI 반폭"] = [ci(U.loc[t].dropna())[2] for t in tg.index]
    ranks = [U[c].dropna().sort_values(ascending=False).index[:5] for c in U.columns]
    tg["상위5 seed 수"] = [sum(t in rk for rk in ranks) for t in tg.index]
    if pairs:
        D = pd.concat([r["util"] - b["util"] for r, b in pairs], axis=1)
        tg["기준 가동률"] = pd.concat([b["util"] for _, b in pairs], axis=1).mean(axis=1)
        tg["가동률 차이"] = D.mean(axis=1)
        tg["차이 CI 반폭"] = [ci(D.loc[t].dropna())[2] for t in tg.index]
    tg = tg[tg["가동률 평균"].notna()].sort_values("가동률 평균", ascending=False)
    tg.index.name = "toolgroup"
    tg.to_csv(out / "toolgroups.csv", encoding="utf-8-sig")

    ss = steady_state(runs, w0, w1, args.trend_tol)
    ss_base = steady_state(base, w0, w1, args.trend_tol) if base else None
    pd.DataFrame([{"구분": tag, "실행": r["name"], "seed": r["seed"], "end_week": r["end_week"], **r["kpi"],
                   "WIP 기울기 (lot/주)": r["wip_slope"], "MSER-5 (주)": r["mser_week"],
                   "MSER 상한 닿음": r["mser_at_bound"], "model_hash": r["hash"]}
                  for tag, rs in (("시나리오", runs), ("기준", base)) for r in rs]
                 ).to_csv(out / "runs.csv", index=False, encoding="utf-8-sig")

    # 보고서
    L = [f"# What-if 보고서 — {name}", "",
         f"- 실행: seed {len(runs)}개 ({', '.join(str(r['seed']) for r in runs)}), 측정 창 {w0:g}~{w1:g}주, "
         f"모델 해시 {', '.join(raw_hashes)}" + (" (결과가 같다고 확인한 묶음, model_hash_equiv.json)" if len(raw_hashes) > 1 else ""),
         f"- 기준 짝: {len(pairs)}개" if base else "- 기준 짝: 없음 (차이 열 없음)", "", "## 시나리오", "",
         *describe(runs[0]["spec"]), ""]
    if late_phase:
        L += [f"> 주의: 측정 창 안에서 투입이 바뀐다({late_phase}주). 창 평균은 바뀌기 전후가 섞인 값이다.", ""]
    verdict = "**WIP 계속 증가 — 정상상태로 보기 어렵다.** 아래 평균은 창 길이에 따라 달라진다." if ss["growing"] \
        else "WIP 추세 이상 없음 (정상상태로 본다)."
    L += ["## 정상상태 점검", "", f"- {verdict}",
          f"- WIP 기울기 {fmt(ss['wip_slope'], ss['wip_slope_ci'], 2)} lot/주, 창 동안 {100 * ss['rise_frac']:+.1f}% "
          f"(판정 기준: CI 하한 > 0 이고 {100 * args.trend_tol:.0f}% 초과)" + (f". {ss['note']}" if ss["note"] else "")]
    if ss_base:
        L.append(f"- 참고, 기준 실행: 기울기 {fmt(ss_base['wip_slope'], ss_base['wip_slope_ci'], 2)} lot/주, "
                 f"창 동안 {100 * ss_base['rise_frac']:+.1f}%")
    L.append(f"- warm-up (seed 평균 WIP 의 MSER-5): {ss['mser_week']:.1f}주"
             + (f" — **측정 창 시작({w0:g}주)보다 늦거나 탐색 상한(실행의 앞 절반)에 닿았다. 창을 뒤로 미루거나 더 길게 돌린다.**"
                if ss["warmup_late"] else f" — 측정 창 시작({w0:g}주) 전")
             + f" (참고, seed 별: {ss['mser_weeks_per_seed']}주)")
    L += ["", "## 핵심 KPI", "", "값은 seed 평균 ± 95% CI 반폭이다."
          + (" 차이는 같은 seed 기준 실행과의 짝 차이(시나리오 − 기준)다." if pairs else ""), ""]
    if pairs:
        L += ["| 지표 | 시나리오 | 기준 | 차이 | 차이 유의 |", "|---|---:|---:|---:|---|"]
        for _, r in kpi.iterrows():
            nd = 0 if r["지표"].startswith("WIP") else 2 if r["지표"].startswith("CT") else 1
            sig = "—" if np.isnan(r["차이 CI 반폭"]) else "유의" if abs(r["차이"]) > r["차이 CI 반폭"] else "아님"
            L.append(f"| {r['지표']} | {fmt(r['평균'], r['CI 반폭'], nd)} | {r['기준 평균']:,.{nd}f} | "
                     f"{r['차이']:+,.{nd}f}" + ("" if np.isnan(r["차이 CI 반폭"]) else f" ± {r['차이 CI 반폭']:,.{nd}f}")
                     + f" | {sig} |")
    else:
        L += ["| 지표 | 값 |", "|---|---:|"]
        for _, r in kpi.iterrows():
            nd = 0 if r["지표"].startswith("WIP") else 2 if r["지표"].startswith("CT") else 1
            L.append(f"| {r['지표']} | {fmt(r['평균'], r['CI 반폭'], nd)} |")
    L += ["", "## 병목 상위 5 (가동률)", "",
          "가동률 = 가동 / (가동 + 유휴), %. \"상위 5 seed 수\"는 그 툴그룹이 seed 별 상위 5 에 든 횟수다.", ""]
    top = tg.head(5)
    if pairs:
        L += ["| 툴그룹 | 가동률 | 상위 5 seed 수 | 기준 | 차이 (%p) |", "|---|---:|---:|---:|---:|"]
        for t, r in top.iterrows():
            L.append(f"| {t} | {fmt(100 * r['가동률 평균'], 100 * r['가동률 CI 반폭'], 2)} | "
                     f"{int(r['상위5 seed 수'])}/{len(runs)} | {100 * r['기준 가동률']:.2f} | "
                     f"{100 * r['가동률 차이']:+.2f}" + ("" if np.isnan(r["차이 CI 반폭"])
                                                     else f" ± {100 * r['차이 CI 반폭']:.2f}") + " |")
        ch = tg.reindex(tg["가동률 차이"].abs().sort_values(ascending=False).index).head(5)
        L += ["", "## 가동률이 가장 많이 바뀐 툴그룹", "", "| 툴그룹 | 기준 | 시나리오 | 차이 (%p) |", "|---|---:|---:|---:|"]
        for t, r in ch.iterrows():
            L.append(f"| {t} | {100 * r['기준 가동률']:.2f} | {100 * r['가동률 평균']:.2f} | "
                     f"{100 * r['가동률 차이']:+.2f}" + ("" if np.isnan(r["차이 CI 반폭"])
                                                     else f" ± {100 * r['차이 CI 반폭']:.2f}") + " |")
    else:
        L += ["| 툴그룹 | 가동률 | 상위 5 seed 수 |", "|---|---:|---:|"]
        for t, r in top.iterrows():
            L.append(f"| {t} | {fmt(100 * r['가동률 평균'], 100 * r['가동률 CI 반폭'], 2)} | "
                     f"{int(r['상위5 seed 수'])}/{len(runs)} |")
    if event:
        rec_txt = f"{event['recovery_week']:g}주" if event["recovery_week"] is not None else "창 안에서 회복 안 됨"
        L += ["", "## 주별 궤적 (짝 차이 = 시나리오 − 기준)", "",
              f"- 주별 WIP 차이 최고: {fmt(event['peak_wip_diff'], event['peak_wip_diff_ci'], 0)} lot "
              f"(짝마다 최댓값의 평균, 평균 곡선 최고 {event['peak_week']:g}주)",
              f"- 회복: {rec_txt} (최고 주 뒤 처음으로 평균 주별 WIP 차이가 최고값의 25% 이하인 주)",
              f"- 창 끝 누적 완료 regular lot 차이: {fmt(event['cum_done_diff_end'], event['cum_done_diff_end_ci'], 0)}", "",
              "| 주 | WIP 차이 | 누적 완료 차이 |", "|---:|---:|---:|"]
        for i, wk in enumerate(traj["week"]):
            L.append(f"| {wk:g} | {fmt(traj['wip_diff'][i], traj['wip_diff_ci'][i], 0)} | "
                     f"{fmt(traj['cum_done_diff'][i], traj['cum_done_diff_ci'][i], 0)} |")
    if states != {None}:
        L += ["", f"- 분기 실행: 시작 상태 (seed, 분기 주) = {sorted(s for s in states if s)}, 짝 번호는 갈래 번호"]
    L += ["", "전체 값: `kpi.csv`, `toolgroups.csv`, `runs.csv` (seed 별)."]
    (out / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    summary = {"scenario": name, "seeds": [r["seed"] for r in runs], "window": [w0, w1], "n_pairs": len(pairs),
               "branch_states": sorted(s for s in states if s), "trajectory": traj, "event": event,
               "steady_state": ss, "steady_state_base": ss_base,
               "kpi": {r["지표"]: {"mean": r["평균"], "ci": r["CI 반폭"],
                                  **({"diff": r["차이"], "diff_ci": r["차이 CI 반폭"]} if pairs else {})}
                       for _, r in kpi.iterrows()},
               "top5": [{"toolgroup": t, "util": r["가동률 평균"], "ci": r["가동률 CI 반폭"],
                         "seeds_in_top5": int(r["상위5 seed 수"])} for t, r in top.iterrows()]}
    (out / "summary.json").write_text(json.dumps(jsonable(summary), ensure_ascii=False, indent=1, allow_nan=False),
                                      encoding="utf-8")
    print(f"[whatif] {verdict.replace('**', '')}", flush=True)
    print(f"[whatif] 끝 → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
