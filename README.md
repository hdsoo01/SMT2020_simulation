# SMT2020 시뮬레이션

SMT2020 반도체 fab 이산사건 시뮬레이터와 시나리오 실행기입니다. 혼자 쓸 수 있고,
LLM 도구 **SLLMT_LLM** 이 이 저장소를 실험 엔진으로 씁니다 (두 저장소를 같은 폴더에 나란히 둠).

## 바로 쓰기

1. Python 3.14 설치 — https://www.python.org/downloads/
2. 패키지 설치

   ```
   python -m pip install -r requirements.txt
   ```

3. 시험 실행 (3주, seed 1, 약 30초)

   ```
   python datagen/run_batch.py examples/demo results/demo
   ```

   결과는 `results/demo/demo_s1/` 에 생깁니다 (lot 기록 `lots.parquet`, 툴그룹 시간대 기록 `tg_hourly.parquet`, `run_meta.json` 등).

## 폴더

```
code/smt2020_sim/   시뮬레이터 본체 (fab, 장비, lot, 디스패칭, 고장·PM, 물류, KPI)
datagen/            시나리오 JSON → 실행 (run_batch: 여러 개 병렬, runner: 한 개)
experiments/        결과 비교 (whatif_report)
data/               SMT2020 모델 데이터 엑셀 (읽기만 함)
results/            기준 운영 결과 seed 5개 (SLLMT 가 비교 기준으로 씀)
examples/demo/      시험 시나리오
```

## 시나리오 JSON

`examples/demo/demo_s1.json` 처럼 `name`, `seed`, `phases`(투입 배율), `grouping`, `end_week`, `label_start_week` 를 줍니다.
정지·고장(`downtime`), 장비 수·가공 시간·PM 배율(`overrides`), regular→hot 전환(`hot_conversion`), 시작 상태(`branch`)는
`datagen/scenario.py` 맨 위 설명을 봅니다. 엑셀은 읽기만 하고 바꿀 값은 실행 중 메모리에만 적용하며,
실행마다 `run_meta.json` 에 시뮬레이터 지문(`model_hash`)과 엑셀 지문(`input_hash`)이 남습니다.

`results/` 의 `tg_hourly.parquet` 는 GitHub 파일 크기 제한 때문에 같은 표를 더 작은 인코딩으로 다시 저장한 것입니다 (내용 같음).

## 데이터

`data/SMT_2020_Model_Data_-_LVHM.xlsx` — D. Kopp, M. Hassoun, A. Kalir, L. Mönch,
"SMT2020—A Semiconductor Manufacturing Testbed", IEEE Transactions on Semiconductor Manufacturing, 2020.
