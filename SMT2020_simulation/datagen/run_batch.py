"""시나리오 폴더의 JSON을 병렬로 실행한다. 이미 끝난 것(run_meta.json 있음)은 건너뛴다.

    python -B datagen/run_batch.py datagen/scenarios/pilot results/pilot_20261014 --workers 9

콘솔(같은 내용이 <out>/batch.log 에도 남는다):
  - 실행마다 시작·완료·실패를 시각과 함께 찍는다. 실패하면 그 실행 로그의 마지막 줄들을 보여 준다.
  - --status-every 초(기본 60)마다 진행 중인 실행의 주차와 예상 남은 시간을 한 줄로 찍는다.
실행마다 <out>/<name>/run.log 에 자세한 진행 상황, <out>/batch_status.csv 에 결과표를 남긴다.
--drop-sqlite 를 주면 실행마다 details/(SQLite)를 지워 용량을 줄인다 (run_experiment.py sim 의 기본).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROGRESS = re.compile(r"week\s+(\d+)(?:/(\d+))?\s+wip\s+(\d+).*?eta\s+(\d+)s")
_lock = threading.Lock()
_log_path: Path | None = None
_running: dict[str, Path] = {}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        print(line, flush=True)
        if _log_path is not None:
            with open(_log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def _tail(path: Path, n: int = 1) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [l for l in lines if l.strip()][-n:]


def _fmt_s(sec: float) -> str:
    return f"{sec / 60:.0f}분" if sec >= 90 else f"{sec:.0f}초"


def run_one(spec: Path, out_root: Path, end_weeks: dict, drop_sqlite: bool = False) -> dict:
    sp = json.loads(spec.read_text(encoding="utf-8-sig"))         # BOM 허용
    name = sp["name"]
    out = out_root / name
    if (out / "run_meta.json").exists():
        return {"name": name, "status": "skipped (done)", "seconds": 0}
    out.mkdir(parents=True, exist_ok=True)
    end_weeks[name] = sp["end_week"]
    log(f"시작  {name}  ({sp['end_week']}주, seed {sp['seed']})")
    t0 = time.time()
    # 하위 프로세스를 UTF-8 모드로 띄운다. Windows에서 파이프/파일로 나가는 출력은 기본이 cp949라
    # 한글 오류 메시지가 utf-8 로그에서 깨진다.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with _lock:
        _running[name] = out / "run.log"
    try:
        with open(out / "run.log", "w", encoding="utf-8") as fh:
            cmd = [sys.executable, "-B", str(HERE / "runner.py"), str(spec), str(out)]
            if drop_sqlite:
                cmd.append("--drop-sqlite")
            r = subprocess.run(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, env=env)
    finally:
        with _lock:
            _running.pop(name, None)
    sec = round(time.time() - t0)
    if r.returncode == 0:
        return {"name": name, "status": "ok", "seconds": sec}
    tail = "\n        ".join(_tail(out / "run.log", 6))
    log(f"실패  {name}  (종료코드 {r.returncode}) — {out / 'run.log'} 마지막 줄:\n        {tail}")
    return {"name": name, "status": f"failed ({r.returncode})", "seconds": sec}


def status_loop(stop: threading.Event, every: float, end_weeks: dict, counts: dict) -> None:
    while not stop.wait(every):
        with _lock:
            running = dict(_running)
        if not running:
            continue
        parts, etas = [], []
        for name, path in sorted(running.items()):
            last = _tail(path)
            m = PROGRESS.search(last[0]) if last else None
            if last and "결과 저장 중" in last[0]:
                parts.append(f"{name} 결과 저장 중")
            elif m:
                week, total = int(m.group(1)), int(m.group(2) or end_weeks.get(name, 0))
                etas.append(int(m.group(4)))
                parts.append(f"{name} {week}/{total}주")
            else:
                parts.append(f"{name} 준비 중")
        eta = f", 예상 남은 시간 약 {_fmt_s(max(etas))}" if etas else ""
        log(f"진행  완료 {counts['done']}/{counts['total']}, 실행 중 {len(running)}, 대기 "
            f"{counts['total'] - counts['done'] - len(running)}{eta}\n        " + " | ".join(parts))


def main(argv=None) -> int:
    global _log_path
    ap = argparse.ArgumentParser()
    ap.add_argument("spec_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--status-every", type=float, default=60.0, help="진행 상황 출력 간격(초)")
    ap.add_argument("--drop-sqlite", action="store_true", help="실행마다 details/(SQLite) 를 지운다")
    args = ap.parse_args(argv)
    specs = sorted(args.spec_dir.glob("*.json"))
    if not specs:
        raise SystemExit(f"시나리오 파일이 없다: {args.spec_dir}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    _log_path = args.out_dir / "batch.log"
    done_before = sum(1 for s in specs if (args.out_dir / json.loads(s.read_text(encoding="utf-8-sig"))["name"]
                                           / "run_meta.json").exists())
    log(f"배치 시작: 시나리오 {len(specs)}개 (이미 끝난 것 {done_before}개는 건너뜀), 동시 실행 {args.workers}, "
        f"출력 {args.out_dir.resolve()}")
    t0 = time.time()
    rows, end_weeks = [], {}
    counts = {"done": 0, "total": len(specs)}
    stop = threading.Event()
    th = threading.Thread(target=status_loop, args=(stop, args.status_every, end_weeks, counts), daemon=True)
    th.start()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_one, s, args.out_dir, end_weeks, args.drop_sqlite): s for s in specs}
            for f in as_completed(futs):
                row = f.result()
                rows.append(row)
                counts["done"] += 1
                if row["status"] == "ok":
                    log(f"완료  {row['name']}  ({_fmt_s(row['seconds'])}) — {counts['done']}/{counts['total']}")
                elif row["status"].startswith("skipped"):
                    log(f"건너뜀 {row['name']} (이미 완료) — {counts['done']}/{counts['total']}")
                with open(args.out_dir / "batch_status.csv", "w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=["name", "status", "seconds"])
                    w.writeheader()
                    w.writerows(sorted(rows, key=lambda r: r["name"]))
    finally:
        stop.set()
    ok = sum(r["status"] == "ok" for r in rows)
    failed = [r["name"] for r in rows if r["status"].startswith("failed")]
    skipped = sum(r["status"].startswith("skipped") for r in rows)
    log(f"배치 끝 ({_fmt_s(time.time() - t0)}): 성공 {ok}, 건너뜀 {skipped}, 실패 {len(failed)}"
        + (f" — 실패: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
