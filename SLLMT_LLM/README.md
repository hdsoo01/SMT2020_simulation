# SLLMT (LLM)

자연어 질문("Litho_FE_111 2대가 40주부터 3주 멈추면?")을 SMT2020 fab 시뮬레이션 실험으로 바꾸고,
결과를 기준 운영과 비교해 설명하는 LLM 도구입니다. 실험 엔진은 옆 폴더 **SMT2020_simulation** 입니다.

## 바로 쓰기 (Windows)

1. 이 폴더는 **SMT2020_simulation** 폴더와 나란히 있어야 합니다 (저장소에 받은 그대로).

   ```
   저장소\
     SLLMT.bat             ← 여기서 시작해도 됨
     SLLMT_LLM\            ← 이 폴더
     SMT2020_simulation\   ← 시뮬레이션 (폴더 이름은 달라도 됨)
   ```

2. Python 3.14 설치 — https://www.python.org/downloads/ (설치할 때 "Add python.exe to PATH" 체크)
3. 저장소 맨 위나 이 폴더의 `SLLMT.bat` 더블클릭 — 처음 한 번은 패키지 설치로 몇 분 걸립니다.
4. 브라우저가 열리면 왼쪽에 API 키(Gemini·Claude·OpenAI 중 하나)를 넣습니다. 키는 저장되지 않습니다.

다른 OS:

```
python -m pip install -r requirements.txt
python run.py dashboard
```

시뮬레이션 폴더를 다른 곳에 두었으면 환경변수 `SLLMT_SIM_ROOT` 에 그 경로를 넣습니다.

## 파일

```
SLLMT.bat        Windows 더블클릭 시작
run.py           실행기 — doctor(점검) / dashboard(화면) / cli(터미널)
app.py           웹 화면
agent.py         LLM 왕복 (질문 → 도구 호출 → 답)
prompts.py       해석·확인·보고 규칙
sllmt_tools.py   도구: 설정 만들기, 정적 점검, 실행 요청, 결과 읽기·비교
run_job.py       시뮬레이션을 뒤에서 돌리는 작업 관리
llm_clients.py, *_client.py   Gemini·Claude·OpenAI 연결
sllmt_core/      공통 기능: 도구 등록, 실행 승인 검사, 답 점검, 보고 문장
```

## 동작 방식

LLM 은 질문을 정해진 실험 설정으로만 바꿉니다. 확인 카드의 버튼을 눌러야 시뮬레이션 저장소의 실행기가 돌고,
결과는 기준 운영과 seed 끼리 짝지어 비교합니다. 숫자가 든 문장은 도구가 씁니다. 한 실험은 약 30분(seed 5개 병렬) 걸립니다.
