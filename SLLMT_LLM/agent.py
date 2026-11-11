"""SLLMT 에이전트 — 사용자 질의 → LLM(도구 호출) ↔ 도구(시나리오·DES·보고서) 왕복 → 보고서.

    python -B SLLMT/agent.py                       # 대화형 (빈 줄이나 exit 로 끝)
    python -B SLLMT/agent.py -q "전체 투입을 2% 늘리면?" -q /승인      # 같은 대화로: 확인 카드 → 명시 승인 → 실행 (11-06)
    python -B SLLMT/agent.py --model gemini-3.8-flash --no-run
    python -B SLLMT/agent.py --model claude-sonnet-5-5      # 다른 제공자 (11-10): claude-* / gpt-* · openai:<이름>, 키는 그 제공자의 환경변수

새 DES 실행이 필요하면 기본으로 바로 돌리고 끝날 때까지 기다린다 (터미널에 진행 표시, 약 30분).
--no-run 이면 실행하지 않고 명령만 안내한다. 기존 결과는 언제나 즉시 재사용한다.
대화 기록: SLLMT/logs/<시각>.jsonl (요청·응답·도구 호출·결과, 키는 남기지 않음), 최종 답: SLLMT/outputs/<시각>.md
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import gemini_client as gc  # noqa: E402
import llm_clients  # noqa: E402
from prompts import SYSTEM  # noqa: E402
from sllmt_core.answer_guard import REWRITE_PROMPT, finish_answer, retry_issues, review_answer  # noqa: E402
from sllmt_core.run_gate import RunGate  # noqa: E402
from sllmt_tools import ROOT, TOOL_SPECS, Toolbox  # noqa: E402

MAX_STEPS = 12


class Agent:
    def __init__(self, client, toolbox: Toolbox, *, log_dir: Path = HERE / "logs", on_event=None, user: str = "",
                 system_prompt: str = SYSTEM, tool_specs: list | None = None, max_steps: int = MAX_STEPS,
                 run_gate: bool = True):
        """run_gate: 실행 확인 코드(sllmt_core.run_gate, 11-05). 끄면 모델의 실행 요청이 그대로 도구로 간다 (비교 시험용)."""
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        self.system_prompt = system_prompt
        self.tool_specs = tool_specs if tool_specs is not None else getattr(toolbox, "tool_specs", TOOL_SPECS)
        self.max_steps = max_steps
        self.client = client
        self.tb = toolbox
        self.gate = RunGate(toolgroup_area_provider=self._toolgroup_areas) if run_gate else None
        self.answer_state: dict = {"defined": set()}                  # 답 마무리 코드의 대화별 상태 (가동률 정의를 보였는지 등)
        self.contents: list = []
        self.usage = {"in": 0, "out": 0, "calls": 0, "sent": 0}       # calls = 받은 응답, sent = 재시도 포함 실제 전송
        self.on_event = on_event or (lambda kind, payload: None)
        log_dir.mkdir(parents=True, exist_ok=True)
        tag = "".join(ch for ch in user if ch.isalnum())[:20]           # 기록용 이름 (여러 사람이 쓸 때)
        self.log_path = log_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{tag + '_' if tag else ''}{os.urandom(2).hex()}.jsonl"

    def _log(self, kind: str, payload) -> None:
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "data": payload},
                                ensure_ascii=False) + "\n")
        self.on_event(kind, payload)

    def _toolgroup_areas(self) -> dict:
        """실행 확인 코드용 툴그룹 → area (도구의 fab 정보, 보통 캐시에서 바로 읽음)."""
        info = self.tb.get_fab_info() if callable(getattr(self.tb, "get_fab_info", None)) else {}
        return {n: v.get("area") for n, v in (info.get("toolgroups") or {}).items()}

    def _known_names(self) -> set | None:
        """등록된 툴그룹·area·제품·제품군 이름 (답 마무리 코드의 이름 대조용, 11-08 밤 T-v2 E4). 못 읽으면 None (대조 안 함)."""
        if "_names" not in self.__dict__:
            try:
                info = self.tb.get_fab_info() if callable(getattr(self.tb, "get_fab_info", None)) else {}
                names = set(info.get("toolgroups") or {}) | set(info.get("areas") or [])
                for g, ps in (info.get("product_groups") or {}).items():
                    names |= {g, *ps}
                self._names = names or None
            except Exception:                                          # fab 정보를 못 읽어도 답은 낸다
                self._names = None
        return self._names

    def _known_ids(self) -> set:
        """실제로 있는 시나리오 id: 이 대화에서 만든 것 + 사용자 메시지(완료 알림 포함)·막히지 않은 도구 결과에 나온 것.
        모델이 지어낸 id 는 막힌 도구 결과의 이유 글에만 나오므로 빠진다 (답 마무리 코드의 id 대조용, 11-07)."""
        from sllmt_core.answer_guard import SID_RE
        known = set(self.gate.builds) if self.gate else set()
        for c in self.contents:
            for p in c.get("parts", []):
                if c.get("role") == "user" and "text" in p:
                    known.update(SID_RE.findall(p["text"]))
                fr = p.get("functionResponse")
                if fr:
                    res = (fr.get("response") or {}).get("result") or {}
                    if res.get("status") not in ("blocked", "value_problem", "target_mismatch", "wait_for_notice"):
                        known.update(SID_RE.findall(json.dumps(res, ensure_ascii=False)))
        return known

    def _generate(self) -> dict:
        sent0 = getattr(self.client, "sent", None)
        try:
            return self.client.generate(self.contents, system=self.system_prompt, tool_specs=self.tool_specs)
        finally:                                                       # 재시도 포함 실제 전송 수 (실패한 요청도 셈)
            sent1 = getattr(self.client, "sent", None)
            self.usage["sent"] += (sent1 - sent0) if isinstance(sent0, int) and isinstance(sent1, int) else 1

    def _call_tool(self, name: str, args: dict) -> dict:
        """실행 확인 코드를 거쳐 도구를 부른다. 막히면 도구를 부르지 않고 막힌 이유를 모델에 돌려준다."""
        blocked = self.gate.before(name, args) if self.gate else None
        if blocked is not None:
            self._log("gate_block", {"name": name, "args": args, "result": blocked})
            note = getattr(self.tb, "note_blocked", None)               # 평가 도구 묶음이면 trace 에 남김
            if callable(note):
                note(name, args, blocked)
            return blocked
        result = self.tb.call(name, args)
        if self.gate:
            self.gate.after(name, args, result)
        return result

    def ask(self, text: str) -> str:
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        self._log("user", text)
        if self.gate:
            self.gate.begin_turn(text)                                 # 차례 구분은 gate.events 에 (막힐 때만 gate_block 기록)
        out = None
        turn_results: list = []                                        # 이번 차례 (도구 이름, 결과) — 답 마무리 코드용
        waits = 0                                                      # 같은 차례 실행 중 재조회를 코드가 막은 횟수
        rewritten = False                                              # 11-09 (D-91): 답 점검 뒤 고쳐 쓰기는 차례당 한 번
        try:
            for _ in range(self.max_steps):
                resp = self._generate()
                um = resp.get("usageMetadata", {})
                self.usage["in"] += um.get("promptTokenCount", 0)
                self.usage["out"] += um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)
                if getattr(self.client, "reports_cache", False):        # Claude·OpenAI 어댑터 (11-10): 캐시 토큰은 값이 달라 따로 셈
                    for k, src in (("cache_read", "cachedContentTokenCount"), ("cache_write", "cacheCreationTokenCount")):
                        self.usage[k] = self.usage.get(k, 0) + (um.get(src) or 0)
                self.usage["calls"] += 1
                content, calls, out_text, finish = gc.parse(resp)
                self.contents.append(content)                          # 서명(thoughtSignature) 포함 그대로
                self._log("model", {"finish": finish, "text": out_text, "calls": calls, "usage": um})
                if not calls:
                    if finish and finish not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
                        out_text += f"\n\n(응답이 '{finish}' 로 끝났다)"
                    guard_kw = dict(card_pending=(self.gate.pending_card() is not None) if self.gate else None,
                                    known_ids=self._known_ids() if self.gate else None,
                                    ambiguous=self.gate.ambiguous_aliases() if self.gate else None,
                                    user_texts=list(self.gate.request_texts) if self.gate else [text],
                                    known_names=self._known_names())
                    if not rewritten:                                  # 11-09 (D-91, 글 설명 개선 B): ※ 줄 전에 한 번 고쳐 쓰기
                        _, pairs, _ = review_answer(out_text, turn_results, copy.deepcopy(self.answer_state), **guard_kw)
                        issues = retry_issues(pairs)
                        if issues:
                            rewritten = True
                            self._log("answer_review", {"issues": issues, "draft": out_text})
                            self.contents.append({"role": "user", "parts": [{"text": REWRITE_PROMPT + "\n".join(
                                f"- {n}" for n in issues)}]})
                            continue
                    out_text, added = finish_answer(out_text, turn_results, self.answer_state, **guard_kw)
                    if added:
                        self._log("answer_guard", {"added": added})
                    out = out_text
                    return out_text
                parts = []
                for call in calls:
                    self._log("tool_call", call)
                    result = self._call_tool(call["name"], call.get("args") or {})
                    self._log("tool_result", {"name": call["name"], "result": result})
                    turn_results.append((call["name"], result))
                    waits += (result or {}).get("status") == "wait_for_notice"
                    parts.append(gc.function_response(call, result))
                self.contents.append({"role": "user", "parts": parts})
                if waits >= 2:                                         # 막혀도 계속 조회하면 코드가 차례를 끝낸다 (11-06 확인 실행)
                    sids = sorted({(c.get("args") or {}).get("scenario_id") for c in calls} - {None})
                    out = (f"시나리오 {', '.join(sids)} 의 DES 실행이 진행 중임. 같은 차례에 상태를 다시 확인하지 않고, "
                           "실행이 끝나면 완료 알림을 받아 결과를 이어서 설명함.")
                    self.contents.append({"role": "model", "parts": [{"text": out}]})
                    self._log("wait_stop", {"text": out})
                    return out
            out = "(도구 호출이 너무 많아 멈췄다. 질의를 더 구체적으로 다시 한다)"
            return out
        finally:
            if self.gate:
                self.gate.end_turn(out or "")

    # -- 사용자 승인 (11-06 버튼 승인): 화면·CLI 만 부른다. 모델 도구로는 노출하지 않는다 --------------------------
    def pending_card(self) -> dict | None:
        """지금 떠 있는 '확인 후 실행' 카드 (없으면 None)."""
        return self.gate.pending_card() if self.gate else None

    def approve(self, card_id: str, accept_flags: bool = False) -> tuple[bool, str]:
        """사용자가 '확인 후 실행'을 누름. (성공, 모델에 보낼 '[승인] …' 메시지 또는 실패 이유). 성공하면 호출자가 ask(메시지)."""
        if not self.gate:
            return False, "실행 확인 코드가 꺼져 있어 버튼 승인이 없다"
        ok, msg = self.gate.grant(card_id, accept_flags)
        self._log("user_approval", {"card": card_id, "ok": ok, "accept_flags": accept_flags, "detail": msg[:300]})
        return ok, msg

    def cost_line(self) -> str:
        sent = f" (재시도 포함 전송 {self.usage['sent']}회)" if self.usage.get("sent", 0) > self.usage["calls"] else ""
        cache = (f" (캐시 읽기 {self.usage['cache_read']:,})" if self.usage.get("cache_read") else "")
        return (f"[{self.client.model}] 호출 {self.usage['calls']}회{sent}, 입력 {self.usage['in']:,}{cache} / 출력 {self.usage['out']:,} 토큰, "
                + llm_clients.cost_text(self.client.model, self.usage))          # Gemini 는 예전 글 그대로


def card_label(c: dict) -> str:
    """화면에 보일 카드 이름 (11-10 사용자: 'C1_1' 대신 '시나리오 1'). 번호 = 이 대화에서 몇 번째 카드인지.
    카드 id(C<차례>_<번호>) 자체는 승인·기록용으로 그대로 쓴다."""
    n = str(c.get("id") or "").rsplit("_", 1)[-1]
    return f"시나리오 {n}" if n.isdigit() else str(c.get("id"))


def format_card(c: dict) -> str:
    """'확인 후 실행' 카드를 글로 (CLI·대화 기록용). 설정은 도구가 계산한 값 그대로."""
    body = c.get("card") or {}
    L = [f"**{card_label(c)} · 확인 후 실행**" + (" (비교: 결과 차이 = 대상 − 기준)" if c.get("reference") else "")]
    for role in ("대상", "기준"):
        if role in body:
            sc = body[role]
            L.append(f"- {role if c.get('reference') else '시나리오 ID'} {sc['scenario_id']}: {sc.get('설명') or ''}")
            L += [f"    - {x}" for x in sc.get("설정") or []]
    if body.get("안내"):
        L.append(f"- 안내: {body['안내']}")
    if c.get("flags"):
        L.append("- **사용자가 말하지 않은 값 (확인 필요):** " + ", ".join(c["flags"]))
    return "\n".join(L)


def make_agent(model: str, *, allow_run: bool = False, on_event=None, state_dir: Path | None = None,
               results_dir: Path | None = None, user: str = "", key: str | None = None,
               background: bool = False) -> Agent:
    """key: 사용자 자신의 API 키 (대시보드 입력). 없으면 그 제공자의 환경변수. background: 새 DES 를 백그라운드로.
    모델 이름으로 제공자를 고른다 (llm_clients: gemini-* / claude-* / gpt-* · openai:<이름>, 11-10).

    폴더는 환경변수로 바꿀 수 있다: SLLMT_STATE_DIR(시나리오 기록), SLLMT_RESULTS_DIR(새 실행·보고서), SLLMT_LOG_DIR(대화 기록).
    """
    env = os.environ.get
    tb = Toolbox(state_dir or Path(env("SLLMT_STATE_DIR") or HERE / "state"),
                 results_dir or Path(env("SLLMT_RESULTS_DIR") or ROOT / "results" / "sllmt"),
                 allow_run=allow_run, background=background)
    return Agent(llm_clients.make_client(model, key=key), tb, on_event=on_event, user=user,
                 log_dir=Path(env("SLLMT_LOG_DIR") or HERE / "logs"))


def _print_event(kind: str, p) -> None:
    if kind == "tool_call":
        print(f"  → 도구 {p['name']}({json.dumps(p.get('args') or {}, ensure_ascii=False)[:200]})", flush=True)
    elif kind == "tool_result":
        r = p["result"]
        brief = r.get("status") or ("error: " + r["error"] if "error" in r else "ok")
        print(f"  ← {p['name']}: {brief}", flush=True)
    elif kind == "gate_block":
        print(f"  [차단] 실행 확인 코드가 막음: {p['result'].get('reason', '')[:160]}", flush=True)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query", action="append", default=None,
                    help="질의 (여러 번 주면 같은 대화로 차례대로 보냄. 카드 승인은 /승인. 예: -q \"투입 2%% 늘리면?\" -q /승인)")
    ap.add_argument("--model", default=llm_clients.DEFAULT_MODEL,
                    help=f"기본 {llm_clients.DEFAULT_MODEL}. 목록: {llm_clients.MODELS} (그 밖은 gemini-…, claude-…, gpt-…, openai:<이름>). "
                         "키 환경변수: GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY")
    ap.add_argument("--no-run", action="store_true", help="새 DES 를 돌리지 않고 명령만 안내한다")
    ap.add_argument("--allow-run", action="store_true", help=argparse.SUPPRESS)      # 예전 옵션 (이제 기본)
    args = ap.parse_args(argv)
    args.allow_run = not args.no_run
    ran = []                                                           # 차례마다 run_experiment 를 불렀는지 (확인 대기 안내용)

    def on_event(kind, p):
        if kind == "tool_result" and p["name"] == "run_experiment" and (p.get("result") or {}).get("status") != "blocked":
            ran.append(True)                                           # 실행 확인 코드가 막은 요청은 실행 요청으로 세지 않음
        _print_event(kind, p)

    try:
        agent = make_agent(args.model, allow_run=args.allow_run, on_event=on_event)
    except gc.GeminiError as e:
        print(f"[SLLMT] {e}")
        return 1
    out_dir = HERE / "outputs"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{agent.log_path.stem}.md"
    scripted = list(args.query) if args.query else None
    print(f"[SLLMT] 모델 {args.model}, 새 DES {'필요하면 바로 실행' if args.allow_run else '실행 안 함(명령만 안내)'}. 기록: {agent.log_path}")
    last_ran = False
    while True:
        if scripted is not None:
            if not scripted:
                break
            q = scripted.pop(0)
        else:
            q = input("\n질의> ").strip()
        if not q or q.lower() in ("exit", "quit"):
            break
        if q.split()[0] in ("/승인", "/실행"):                         # 명시 승인 명령 (11-06 버튼 승인의 CLI 판)
            card = agent.pending_card()
            if not card:
                print("[SLLMT] 승인할 카드가 없음 (모델이 '확인 후 실행' 카드를 띄운 뒤에 /승인)")
                continue
            ok, msg = agent.approve(card["id"], accept_flags="확인" in q.split()[1:])
            if not ok:
                print(f"[SLLMT] 승인 안 됨: {msg}" + (" → 값이 맞으면 '/승인 확인' 으로 승인" if card.get("flags") else ""))
                continue
            print(f"[SLLMT] 카드 {card['id']} 승인 → 실행 요청")
            q = msg
        ran.clear()
        try:
            ans = agent.ask(q)
        except gc.GeminiError as e:
            print(f"[SLLMT] API 오류: {e}")
            break
        last_ran = bool(ran)
        print("\n" + ans + "\n")
        card = agent.pending_card()
        if card:
            print(format_card(card) + "\n\n[SLLMT] 실행하려면 /승인 (사용자가 말하지 않은 값이 있으면 /승인 확인), 고치려면 그대로 입력\n")
        print(agent.cost_line())
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(f"## 질의: {q}\n\n{ans}\n\n" + (format_card(card) + "\n\n" if card else "") + f"_{agent.cost_line()}_\n\n")
    if scripted is not None and not last_ran:
        # -q 는 이 프로세스 안에서만 대화를 이어 간다 (11-05 팀원 O02). 카드에서 끝나면 다음 명령의 /승인은 이어지지 않는다
        print("[SLLMT] 마지막 차례에는 실행 요청이 없었음. 이전 차례의 실행 여부는 위 도구 기록을 확인한다. -q 대화는 이 명령 안에서만 이어지므로, "
              "다음에 따로 실행한 /승인은 앞 카드와 이어지지 않는다. 이어서 하려면 -q 를 여러 번 주거나 (예: -q \"질의\" -q /승인) "
              "-q 없이 대화형으로 실행한다")
    print(f"[SLLMT] 답 저장: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
