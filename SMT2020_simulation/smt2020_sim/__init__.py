"""SMT2020 Dataset 2 (LVHM) SimPy 모델 — 최소 구성.

    python run.py                                  # 실행 (code/ 에서)
    python run.py --release explicit               # lot별 납기 시트로 실행
    python -m smt2020_sim.kpi_collector report     # 표만
    python visualize.py                            # 기존 그림 모음

`kpi_collector` 한 모듈에서 실행 중 수집과 저장된 스냅샷 보고서를 제공한다.
보고서 조회는 시뮬레이션을 시작하지 않는다.

검증 스위트(`checks/`, `verify.py`)와 레퍼런스 대조(`asd_reader.py`,
`compare.py`, `fixtures.py`)는 이 구성에 넣지 않았다.
"""

__all__ = ["config", "model_data", "data_parser", "main",
           "kpi_collector"]
