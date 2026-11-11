"""SLLMT 대시보드 (Streamlit). 질의 → LLM 이 시나리오를 만들고 DES 를 돌림 → 끝나면 결과를 받아 질의 목적에 맞게 설명.

    streamlit run SLLMT/app.py                              # 같은 네트워크의 다른 PC 에서도 접속
    streamlit run SLLMT/app.py --server.address localhost   # 이 PC 에서만

사용자는 각자 자기 API 키를 화면에 넣는다 (브라우저 세션 메모리에만 두고 저장·기록하지 않는다).
키 칸은 하나다. 키 앞부분으로 회사(Gemini·Claude·OpenAI)를 알아보고 모델 목록을 그 회사 것으로 줄인다 (11-10).
운영자 PC 의 환경변수:
    SLLMT_ACCESS_CODE   설정하면 접속 코드를 물어본다
    SLLMT_ALLOW_RUN=0   새 DES 실행을 막는다. 기본(없음)은 LLM 이 필요하면 바로 돌린다 (한 번에 하나, 백그라운드)
    SLLMT_SERVER_KEY=1  사용자가 키를 넣지 않으면 운영자의 키 환경변수(GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY)를 쓴다 (기본은 쓰지 않음)
새 DES 는 백그라운드로 돌고, 화면이 20초마다 상태 파일만 확인한다 (LLM 호출 없음 → 기다리는 동안 토큰 0).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
import gemini_client as gc  # noqa: E402
import llm_clients  # noqa: E402
from agent import card_label, format_card, make_agent  # noqa: E402

def md(text: str) -> str:
    """화면용 markdown: '~' 를 글자 그대로 보이게 한다 (11-07: "seed 1~5, 26~52주" 가 취소선 "seed 15, 2652주" 로 보임).
    코드 표시(`…`) 안은 그대로 둔다."""
    import re
    parts = re.split(r"(`[^`]*`)", text or "")
    return "".join(p if p.startswith("`") else p.replace("~", r"\~") for p in parts)


st.set_page_config(page_title="SLLMT", layout="wide")
st.title("SLLMT · SMT2020 what-if 실험 에이전트")
st.caption("기본 실험: 1년(52주) 실행, 앞 26주 warm-up(결과에서 제외), 뒤 26주(26~52주) 측정, seed 5개 짝 비교. "
           "기간은 주 단위로 질의 (1년 = 52주로 환산). 예: warm-up 52주, 전체 104주")

code = os.environ.get("SLLMT_ACCESS_CODE", "")
if code and not st.session_state.get("authed"):
    entered = st.text_input("접속 코드", type="password")
    if entered and hmac.compare_digest(entered, code):
        st.session_state["authed"] = True
        st.rerun()
    if entered:
        st.error("접속 코드가 맞지 않습니다.")
    st.stop()

run_ok = os.environ.get("SLLMT_ALLOW_RUN", "1") != "0"                # 기본: 허용 (11-04 사용자 요청)
server_key = os.environ.get("SLLMT_SERVER_KEY") == "1"
with st.sidebar:
    # 11-10: 키 칸은 하나. 키 앞부분으로 회사(Gemini·Claude·OpenAI)를 알아보고 모델 목록을 그 회사 것으로 줄인다
    key = st.text_input("API 키 (각자)", type="password",
                        help="Gemini(AIza…)·Claude(sk-ant-…)·OpenAI(sk-…) 키 중 하나. 키 모양으로 회사를 알아본다. "
                             "Gemini 는 https://aistudio.google.com/apikey 에서 무료로 발급. 이 브라우저 세션 메모리에만 두고 저장하지 않는다")
    key_provider = llm_clients.provider_of_key(key) if key else None
    options = ([m for m in llm_clients.MODELS if llm_clients.provider_of(m) == key_provider] if key_provider
               else llm_clients.MODELS)
    default = llm_clients.PROVIDER_DEFAULTS.get(key_provider, llm_clients.DEFAULT_MODEL)
    picked = st.selectbox("모델", options, index=options.index(default), key=f"model_{key_provider or 'all'}",
                          help="평가 수치는 gemini-3.5-flash-lite 로 잰 것. Claude·OpenAI 는 유료")
    custom = st.text_input("목록에 없는 모델 (선택)", value="",
                           help="예: claude-sonnet-5, gpt-5-mini, openai:<모델 이름>. 비우면 위 목록의 모델")
    model = custom.strip() or picked
    try:
        provider = llm_clients.provider_of(model)
    except gc.GeminiError as e:
        st.error(str(e))
        st.stop()
    pinfo = llm_clients.PROVIDERS[provider]
    if key_provider and key_provider != provider:
        st.error(f"넣은 키는 {llm_clients.PROVIDERS[key_provider]['label']} 키인데 모델 {model} 은 {pinfo['label']} 모델이다")
        st.stop()
    if key:
        st.caption(f"{pinfo['label']} 키로 씀" if key_provider else
                   f"키 모양으로 회사를 알 수 없어 고른 모델의 회사({pinfo['label']}) 키로 씀")
    user = st.text_input("이름 (기록용, 선택)", value=st.session_state.get("user", ""), max_chars=20)
    allow_run = st.checkbox("새 DES 자동 실행 (약 30분, 백그라운드)", value=run_ok, disabled=not run_ok)
    st.caption("LLM 이 바꿀 설정을 정리하면 확인 카드의 '확인 후 실행' 버튼으로 승인한다. "
               "켜져 있으면 기존 결과가 없을 때 새로 돌리고, 끝나면 자동으로 결과를 설명한다. "
               "끄면 기존 결과만 재사용한다." if run_ok else
               "이 서버는 새 DES 실행을 막아 두었다 (SLLMT_ALLOW_RUN=0). 기존 결과 재사용과 명령 안내만 한다.")
    if st.button("대화 새로 시작"):
        for k in ("agent", "pending", "job_finished"):
            st.session_state.pop(k, None)
        st.session_state["history"] = []
    st.markdown("**예시 질의** (설정 카드의 '확인 후 실행' 버튼을 눌러야 실행)\n- 전체 투입을 2% 늘리면?\n- 투입을 4% 늘리면 병목이 바뀌나?\n"
                "- 가동률 높은 툴그룹 5개 알려줘 (바로 답, 실행 없음)\n"
                "- 기본 상태에서 lot 유형별 납기 준수율은? (바로 답, 실행 없음)\n"
                "- Litho_FE_111 4대가 30주부터 4주 정지하면?\n- 혼잡할 때 Litho_FE_111 4대가 4주 멈추면?\n"
                "- 투입 3%에 Litho_FE_111 1대 증설까지 하면, 3%만 늘렸을 때보다 CT가 얼마나 나아져?")

if not key and not (server_key and llm_clients.env_key(provider)):
    st.info("왼쪽에 자신의 API 키(Gemini·Claude·OpenAI 중 하나)를 넣으면 시작한다. "
            "Gemini 키는 https://aistudio.google.com/apikey 에서 무료로 발급")
    st.stop()

agent_key = (model, allow_run and run_ok, user, hashlib.sha256(key.encode()).hexdigest()[:12] if key else "server")
prev_key = st.session_state.get("agent_key")
if prev_key != agent_key:
    # 사이드바 설정이 바뀌면 Agent 를 새로 만든다. 같은 모델·키(자동 실행·이름만 바뀜)면 대화 맥락을 그대로 넘기고,
    # 모델·키가 바뀌면 맥락을 새로 시작한다는 것을 화면에 알린다 (11-05 팀원 O01: 화면만 남고 맥락을 잃으면 안 됨).
    # 진행 중인 실행(pending)은 세션에 그대로 두어 완료 알림을 계속 받는다.
    old = st.session_state.pop("agent", None)
    st.session_state["agent_key"] = agent_key
    st.session_state["user"] = user
    if old is not None and prev_key and old.contents:
        names = ["모델", "자동 실행", "이름", "API 키"]
        changed = [n for n, a, b in zip(names, prev_key, agent_key) if a != b]
        keep = prev_key[0] == agent_key[0] and prev_key[3] == agent_key[3]
        st.session_state["carry_contents"] = list(old.contents) if keep else None
        st.session_state["carry_gate"] = getattr(old, "gate", None) if keep else None   # 실행 확인 상태도 같은 대화로 이어 감
        st.session_state["carry_answer_state"] = getattr(old, "answer_state", None) if keep else None   # 가동률 정의를 보였는지 등
        note = (f"⚙ 설정 변경({', '.join(changed)}): 대화 맥락을 그대로 이어 감. 앞의 카드에서 '확인 후 실행'을 누르거나 바꿀 내용을 입력하면 됨"
                if keep else
                f"⚙ 설정 변경({', '.join(changed)}): 모델·키가 바뀌어 대화 맥락을 새로 시작함. 위의 카드·답은 이어지지 않으니 "
                "질의를 다시 보내 주세요")
        if st.session_state.get("pending"):
            note += f". 진행 중인 실행({st.session_state['pending']})의 완료 알림은 계속 받음"
        st.session_state.setdefault("history", []).append({"role": "assistant", "text": note})
if "agent" not in st.session_state:
    try:
        st.session_state["agent"] = make_agent(model, allow_run=allow_run and run_ok, user=user,
                                               key=key or None, background=True)
    except gc.GeminiError as e:
        st.error(str(e))
        st.stop()
    carry = st.session_state.pop("carry_contents", None)
    carry_gate = st.session_state.pop("carry_gate", None)
    carry_answer_state = st.session_state.pop("carry_answer_state", None)
    if carry:
        st.session_state["agent"].contents = carry
        if carry_gate is not None and getattr(st.session_state["agent"], "gate", None) is not None:
            st.session_state["agent"].gate = carry_gate
        if carry_answer_state is not None:
            st.session_state["agent"].answer_state = carry_answer_state
agent = st.session_state["agent"]
history = st.session_state.setdefault("history", [])

# 대화 기록. '확인 후 실행' 카드(11-06 버튼 승인)는 버튼과 함께 그린다. 승인은 이 버튼으로만 생긴다 (글로 '맞음'은 승인 아님)
clicked = None
for h in history:
    with st.chat_message(h["role"]):
        for ev in h.get("events", []):
            st.caption(ev)
        st.markdown(md(h["text"]))
        cid = h.get("card_id")
        if cid:
            c = agent.gate.cards.get(cid) if getattr(agent, "gate", None) else None
            status = (c or {}).get("status")
            if status == "pending":
                flags_ok = True
                if c["flags"]:
                    flags_ok = st.checkbox("위 '사용자가 말하지 않은 값'이 내가 정한 값이 맞음", key=f"flags_{cid}")
                if st.button("확인 후 실행", key=f"approve_{cid}", type="primary", disabled=not flags_ok):
                    clicked = (cid, bool(c["flags"]))
                st.caption("고치려면 버튼을 누르지 말고 바꿀 내용을 입력 (이 카드는 무효가 되고 새 카드가 뜸)")
            elif status:
                st.caption({"approved": "✔ 승인됨 (버튼)", "stale": "무효: 설정이 바뀌었거나 글로 답함"}.get(status, status))
            else:
                st.caption("이전 대화의 카드: 모델·키가 바뀌어 무효 (질의를 다시 보내면 새 카드가 뜸)")


def run_turn(text: str, shown: str) -> None:
    """질의 한 번: LLM ↔ 도구 왕복. 백그라운드 실행이 시작되면 pending 에 기록한다."""
    history.append({"role": "user", "text": shown})
    with st.chat_message("user"):
        st.markdown(md(shown))
    events, last_sid = [], {}

    def on_event(kind, p):
        if kind == "tool_call":
            events.append(f"→ {p['name']}({json.dumps(p.get('args') or {}, ensure_ascii=False)[:160]})")
            if p["name"] == "run_experiment":
                last_sid["sid"] = (p.get("args") or {}).get("scenario_id")
        elif kind == "tool_result":
            r = p["result"]
            events.append(f"← {p['name']}: {r.get('status') or r.get('error', 'ok')}")
            if p["name"] == "run_experiment" and r.get("status") in ("started", "running") and last_sid.get("sid"):
                st.session_state["pending"] = last_sid["sid"]

    agent.on_event = on_event
    with st.chat_message("assistant"):
        with st.status("LLM ↔ 도구 왕복 중…", expanded=True) as box:
            try:
                ans = agent.ask(text)
            except gc.GeminiError as e:
                ans = f"API 오류: {e}"
            for ev in events:
                st.write(md(ev))
            box.update(label=f"완료 · {agent.cost_line()}", state="complete", expanded=False)
        st.markdown(md(ans))
    history.append({"role": "assistant", "text": ans, "events": events})
    card = agent.pending_card() if hasattr(agent, "pending_card") else None
    if card and card["id"] not in {h.get("card_id") for h in history}:   # 새 카드 → 다음 그리기에서 버튼과 함께 보임
        history.append({"role": "assistant", "text": format_card(card), "card_id": card["id"]})
        st.session_state["new_card"] = True


if clicked:                                                             # '확인 후 실행' 버튼 → 승인 → 모델에 실행 요청
    cid, had_flags = clicked
    ok, msg = agent.approve(cid, accept_flags=had_flags)
    if ok:
        run_turn(msg, f"✅ 확인 후 실행 ({card_label({'id': cid})})")
        st.rerun()
    else:
        st.error(f"승인 안 됨: {msg}")


# 백그라운드 실행이 끝났으면 LLM 에게 결과 설명을 요청한다
done = st.session_state.pop("job_finished", None)
if done:
    sid, js = done
    if js["state"] == "done":
        run_turn(f"[알림] 시나리오 {sid} 의 DES 실행이 끝났다. run_experiment 로 ready 를 확인하고 get_report 로 결과를 받아, "
                 "처음 질의의 목적에 맞게 설명해 달라. 다른 시나리오와 비교하는 질의였으면 비교 대상 결과도 확보한 뒤 "
                 "compare_to 로 직접 비교한다. 사건형이면 주별 궤적을 중심으로 쓴다.", f"⏱ 실행 끝 알림 ({sid}) — 결과 설명 요청")
    else:
        run_turn(f"[알림] 시나리오 {sid} 의 DES 실행이 실패했다 (반환 코드 {js.get('returncode')}). 사용자에게 알리고 "
                 f"로그 위치 results/sllmt/{sid}/job_output.log 를 안내해 달라.", f"⚠ 실행 실패 알림 ({sid})")


@st.fragment(run_every=20)
def job_panel():
    sid = st.session_state.get("pending")
    if not sid:
        return
    js = agent.tb.job_status(sid)
    if not js or js["state"] == "running":
        st.info(f"DES 실행 중 · {sid} · 시작 {js.get('started') if js else '-'} · {js.get('progress', '') if js else ''}  "
                "(기다리는 동안 LLM 을 부르지 않음 → 토큰 0)")
        return
    st.session_state["job_finished"] = (sid, js)
    st.session_state.pop("pending", None)
    st.rerun(scope="app")


job_panel()

q = st.chat_input("질의를 입력 (예: 전체 투입을 2% 늘리면?)")
if q:
    run_turn(q, q)
    if st.session_state.get("pending") or st.session_state.pop("new_card", False):
        st.rerun()
