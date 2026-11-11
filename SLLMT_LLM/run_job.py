"""DES 배치를 백그라운드 작업으로 돌리고 상태 파일을 남긴다 (sllmt_tools.Toolbox 가 띄운다).

    python -B SLLMT/run_job.py <잠금 파일> <상태 파일> <출력 로그> -- <명령...>

상태 파일(JSON): state = running → done | failed, 시작·끝 시각, 반환 코드. 끝나면 잠금 파일을 지운다.
대기하는 동안 LLM 은 부르지 않는다 (토큰 소모 없음).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def write(path: Path, rec: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sep = argv.index("--")
    lock, status, outlog = (Path(a) for a in argv[:3])
    cmd = argv[sep + 1:]
    rec = {"state": "running", "pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds"), "cmd": cmd}
    write(status, rec)
    rc = -1
    try:
        with open(outlog, "w", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).returncode
    finally:
        rec.update({"state": "done" if rc == 0 else "failed", "returncode": rc,
                    "ended": datetime.now().isoformat(timespec="seconds")})
        write(status, rec)
        lock.unlink(missing_ok=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
