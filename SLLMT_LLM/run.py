"""SLLMT 실행기 (LLM 저장소). 시뮬레이션 저장소는 이 폴더 옆에 두면 찾아 쓴다.

  python run.py doctor                 설치·시뮬레이션 위치·지문 점검 (LLM·시뮬레이션 실행 없음)
  python run.py dashboard              웹 화면 (http://localhost:8501)
  python run.py cli [--model 이름]     터미널 대화 (나머지 인자는 agent.py 로)

시뮬레이션 폴더 찾는 순서: 환경변수 SLLMT_SIM_ROOT → 이 폴더와 같은 곳에 있는 폴더 중
smt2020_sim(또는 연구 폴더의 code/smt2020_sim)과 datagen/scenario.py 가 있는 것 (하나일 때).
실행 중 생기는 상태·결과·로그는 workspace/ 에만 쓴다.
API 키는 화면이나 터미널에서 받고 파일에 저장하지 않는다.
"""
from __future__ import annotations

import importlib.metadata as md
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "workspace"
PACKAGES = ("streamlit", "numpy", "pandas", "scipy", "simpy", "openpyxl", "pyarrow", "psutil", "anthropic", "openai")
BASELINE_FILES = ("spec.json", "run_meta.json", "lots.parquet", "tg_hourly.parquet")


def is_sim(p: Path) -> bool:
    has_pkg = (p / "smt2020_sim").is_dir() or (p / "code" / "smt2020_sim").is_dir()
    return has_pkg and (p / "datagen" / "scenario.py").is_file()


def find_sim() -> Path:
    env = os.environ.get("SLLMT_SIM_ROOT", "").strip()
    if env:
        if not is_sim(Path(env)):
            raise SystemExit(f"SLLMT_SIM_ROOT 가 시뮬레이션 폴더가 아니다: {env}")
        return Path(env).resolve()
    found = [p for p in sorted(ROOT.parent.iterdir()) if p.is_dir() and p != ROOT and is_sim(p)]
    if len(found) == 1:
        return found[0].resolve()
    if not found:
        raise SystemExit("시뮬레이션 폴더(SMT2020_simulation)를 찾지 못했다. 이 폴더와 나란히 둔다 (저장소에 받은 그대로).")
    raise SystemExit(f"시뮬레이션 폴더가 여러 개다 {[p.name for p in found]}. 쓸 것을 환경변수 SLLMT_SIM_ROOT 로 정한다.")


def configure() -> Path:
    sim = find_sim()
    os.environ["SLLMT_RESEARCH_ROOT"] = str(sim)                  # sllmt_tools.py 가 이 시뮬레이터만 쓰게
    for name, sub in (("SLLMT_STATE_DIR", "state"), ("SLLMT_RESULTS_DIR", "results"), ("SLLMT_LOG_DIR", "logs")):
        path = WORK / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return sim


def doctor(sim: Path) -> int:
    status, errors = {"python": sys.version.split()[0], "simulation": sim.name, "packages": {}}, []
    for name in PACKAGES:
        try:
            status["packages"][name] = md.version(name)
        except md.PackageNotFoundError:
            errors.append(f"패키지 없음: {name} (pip install -r requirements.txt)")
    try:
        import sllmt_tools
        import runner
        import scenario
        import whatif_report
        from smt2020_sim import config
        from smt2020_sim.kpi_collector import input_hash, model_hash
        if not Path(sllmt_tools.__file__).resolve().is_relative_to(ROOT):
            errors.append("sllmt_tools 를 이 폴더 밖에서 읽었다")
        for m in (runner, scenario, whatif_report, config):
            if not Path(m.__file__).resolve().is_relative_to(sim):
                errors.append(f"시뮬레이션 폴더 밖에서 읽은 모듈: {m.__name__}")
        if Path(sllmt_tools.ROOT).resolve() != sim:
            errors.append("sllmt_tools 가 다른 시뮬레이션 폴더를 쓴다")
        if not config.XLSX_PATH.is_file():
            errors.append("시뮬레이션 폴더에 엑셀이 없다: data/SMT_2020_Model_Data_-_LVHM.xlsx")
        status["model_hash"], status["input_hash"] = model_hash(), input_hash(config.XLSX_PATH)
        status["tools"] = [s["name"] for s in sllmt_tools.TOOL_REGISTRY.specs()]
        runs = [Path(p) for p in sllmt_tools.BASE_RUNS]
        ready = [p for p in runs if all((p / n).is_file() for n in BASELINE_FILES)]
        status["baseline_runs"] = f"{len(ready)}/{len(runs)}"
        if len(ready) < len(runs):
            errors.append("시뮬레이션 폴더에 기준 운영 결과(results/)가 없다")
        else:
            metas = [json.loads((p / "run_meta.json").read_text(encoding="utf-8")) for p in ready]
            if any(m.get("input_hash") != status["input_hash"] for m in metas):
                errors.append("기준 운영 결과의 엑셀 지문(input_hash)이 지금 엑셀과 다르다")
            base = {whatif_report.canonical_hash(m.get("model_hash", "")) for m in metas}
            if base != {whatif_report.canonical_hash(status["model_hash"])}:
                errors.append("기준 운영 결과의 시뮬레이터 지문(model_hash)이 지금 코드와 다르다")
    except Exception as exc:                                       # 점검 결과로 보여 준다
        errors.append(f"{type(exc).__name__}: {exc}")
    status["errors"] = errors
    print(json.dumps(status, ensure_ascii=False, indent=1))
    return int(bool(errors))


def dashboard(args: list[str]) -> int:
    cmd = [sys.executable, "-B", "-m", "streamlit", "run", str(ROOT / "app.py"),
           "--server.address", "127.0.0.1", "--server.port", "8501",
           "--browser.gatherUsageStats", "false", "--server.showEmailPrompt", "false", *args]
    return subprocess.run(cmd, cwd=WORK, check=False).returncode


def cli(args: list[str]) -> int:
    import llm_clients
    model = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--model"), None) or \
        next((a.split("=", 1)[1] for a in args if a.startswith("--model=")), llm_clients.DEFAULT_MODEL)
    try:
        provider = llm_clients.provider_of(model)
    except llm_clients.LLMError as exc:
        print(exc)
        return 1
    info = llm_clients.PROVIDERS[provider]
    if not llm_clients.env_key(provider):
        import getpass
        key = getpass.getpass(f"{info['label']} API key (화면에 안 보임, 이 실행 안에서만 씀): ").strip()
        if not key:
            print("키를 넣지 않아 시작하지 않았다.")
            return 1
        os.environ[info["env"][0]] = key
    sys.argv = [str(ROOT / "agent.py"), *args]
    runpy.run_path(str(ROOT / "agent.py"), run_name="__main__")
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args.pop(0) if args else "doctor"
    if mode not in ("doctor", "dashboard", "cli"):
        print(__doc__)
        return 2
    sim = configure()
    if mode == "doctor":
        return doctor(sim)
    if mode == "dashboard":
        return dashboard(args)
    return cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
