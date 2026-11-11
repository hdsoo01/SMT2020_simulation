# SLLMT — SMT2020 what-if 실험 에이전트

자연어 what-if 질문("Litho_FE_111 2대가 40주부터 3주 멈추면?")을 SMT2020 반도체 fab 시뮬레이션 실험으로 바꿔 돌리고,
결과를 기준 운영과 비교해 설명하는 LLM 도구입니다. 이 저장소에 두 부분이 같이 들어 있습니다.

```
SLLMT.bat             Windows 에서 더블클릭해 시작
SMT2020_simulation/   시뮬레이션 — 시뮬레이터, SMT2020 엑셀, 기준 운영 결과 (혼자도 실행됨)
SLLMT_LLM/            LLM 도구 — 화면, 대화, 실험 설정·실행·보고 (옆의 시뮬레이션 폴더를 찾아 씀)
```

## 바로 쓰기 (Windows)

1. 이 저장소를 받아 압축을 풉니다 (Code → Download ZIP). 두 폴더는 받은 그대로 나란히 둡니다.
2. Python 3.14 설치 — https://www.python.org/downloads/ (설치할 때 "Add python.exe to PATH" 체크)
3. 맨 위 `SLLMT.bat` 더블클릭 — 처음 한 번은 패키지 설치로 몇 분 걸립니다.
4. 브라우저가 열리면 왼쪽에 API 키(Gemini·Claude·OpenAI 중 하나)를 넣습니다. 키는 저장되지 않습니다.

다른 OS:

```
cd SLLMT_LLM
python -m pip install -r requirements.txt
python run.py dashboard
```

시뮬레이션만 돌려 보려면 `SMT2020_simulation/README.md` 를 봅니다.

## 동작 방식

LLM 은 질문을 정해진 실험 설정으로만 바꿉니다. 확인 카드의 버튼을 눌러야 시뮬레이션이 돌고,
결과는 기준 운영과 seed 끼리 짝지어 비교합니다. 숫자가 든 문장은 도구가 씁니다. 한 실험은 약 30분(seed 5개 병렬) 걸립니다.

## 데이터

`SMT2020_simulation/data/SMT_2020_Model_Data_-_LVHM.xlsx` — D. Kopp, M. Hassoun, A. Kalir, L. Mönch,
"SMT2020—A Semiconductor Manufacturing Testbed", IEEE Transactions on Semiconductor Manufacturing, 2020.
