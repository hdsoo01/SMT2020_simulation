"""Claude (Anthropic) 어댑터 — 공식 anthropic SDK 의 Messages API (2026-11-10).

agent 의 대화 기록은 Gemini 모양 그대로 두고, 요청을 보낼 때 Claude 모양으로 바꾼다.
- 모델 차례: Claude 가 돌려준 content 블록(thinking·text·tool_use, 서명 포함)을 content["_raw"] 에 그대로 두고 다음 요청에
  바꾸지 않고 다시 보낸다 (thinking 블록은 손대지 않고 돌려줘야 함. 기록은 덧붙이기만 함)
- 도구 결과: Gemini functionResponse(id = tool_use id) → 같은 user 메시지 맨 앞의 tool_result 블록
- 답이 없는 tool_use 가 남으면(도구 실행 중 오류 등) 자리 채움 결과를 넣어 요청이 깨지지 않게 함
- 프롬프트 캐시: 요청 최상위 cache_control (시스템 프롬프트·도구·지난 대화가 매번 같은 앞부분)
- 거절 대비: claude-opus-5-5·claude-sonnet-5-5 는 서버 쪽 fallbacks "default" (beta server-side-fallback-2026-07-01).
  안전 분류기가 요청을 거절하면 서버가 다른 모델로 다시 돌린다. Haiku 5.5 는 서버 쪽 fallback 이 없어 보내지 않음
- effort: 기본 medium. 환경변수 SLLMT_CLAUDE_EFFORT (low/medium/high/xhigh/max) 로 바꿈
키: 대시보드 입력 또는 환경변수 ANTHROPIC_API_KEY. 파일·로그에 남기지 않는다.
"""
from __future__ import annotations

import json
import os

import anthropic

import gemini_client as gc


class ClaudeError(gc.GeminiError):
    pass


EFFORT_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}   # effort 를 받는다고 확인한 모델
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}                   # 서버 쪽 fallbacks "default" 를 쓰는 모델
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16000                                                            # 스트리밍 없는 요청의 권장 상한
STOP_MAP = {"end_turn": "STOP", "tool_use": "STOP", "stop_sequence": "STOP", "max_tokens": "MAX_TOKENS",
            "refusal": "REFUSAL", "pause_turn": "PAUSE_TURN"}
PLACEHOLDER = {"error": "이 도구 호출의 결과가 기록되지 않았다 (실행 중 중단)"}


def tools_for(tool_specs: list) -> list:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tool_specs]


def _echo_blocks(blocks: list) -> list:
    """모델 차례 블록을 다시 보낼 모양으로. 마지막 fallback 블록 앞의 thinking·tool_use 등은 빼고 text 와 fallback 은 그 자리에 둔다
    (Anthropic 안내: 출력 도중 다른 모델로 넘어간 차례를 돌려보낼 때). fallback 이 없으면 그대로."""
    last = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    return [b for i, b in enumerate(blocks) if i >= last or b.get("type") in ("text", "fallback")]


def _result_text(fr: dict) -> str:
    return json.dumps((fr.get("response") or {}).get("result"), ensure_ascii=False)


def to_messages(contents: list) -> list:
    """Gemini 모양 대화 기록 → Claude messages. 같은 역할이 이어지면 API 가 한 차례로 합친다."""
    msgs, pending = [], []                                         # pending: 결과를 아직 못 받은 tool_use id
    for c in contents:
        parts = c.get("parts") or []
        if c.get("role") == "model":
            if pending:                                            # 결과 없이 다음 모델 차례가 오면 자리 채움
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": i,
                                                          "content": json.dumps(PLACEHOLDER, ensure_ascii=False)} for i in pending]})
                pending = []
            if "_raw" in c:
                blocks = _echo_blocks(c["_raw"])
            else:                                                  # 어댑터가 만들지 않은 모델 차례 (agent 가 쓴 대기 안내 글 등)
                blocks = []
                for n, p in enumerate(parts):
                    if p.get("text"):
                        blocks.append({"type": "text", "text": p["text"]})
                    elif "functionCall" in p:
                        fc = p["functionCall"]
                        blocks.append({"type": "tool_use", "id": fc.get("id") or f"call_{len(msgs)}_{n}",
                                       "name": fc["name"], "input": fc.get("args") or {}})
            if blocks:
                msgs.append({"role": "assistant", "content": blocks})
                pending = [b["id"] for b in blocks if b.get("type") == "tool_use"]
            continue
        results, texts, answered = [], [], set()
        for p in parts:
            fr = p.get("functionResponse")
            if fr:
                answered.add(fr.get("id"))
                results.append({"type": "tool_result", "tool_use_id": fr.get("id"), "content": _result_text(fr)})
            elif p.get("text"):
                texts.append({"type": "text", "text": p["text"]})
        filler = [{"type": "tool_result", "tool_use_id": i, "content": json.dumps(PLACEHOLDER, ensure_ascii=False)}
                  for i in pending if i not in answered]
        pending = []
        blocks = filler + results + texts                         # tool_result 블록이 user 메시지 맨 앞
        if blocks:
            msgs.append({"role": "user", "content": blocks})
    if pending:
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": i,
                                                  "content": json.dumps(PLACEHOLDER, ensure_ascii=False)} for i in pending]})
    return msgs


def to_gemini(msg: dict) -> dict:
    """Claude 응답(dict) → agent 가 읽는 Gemini 응답 모양. 원래 블록은 content['_raw'] 에 그대로."""
    blocks = list(msg.get("content") or [])
    parts = []
    for b in blocks:
        if b.get("type") == "text" and b.get("text"):
            parts.append({"text": b["text"]})
        elif b.get("type") == "tool_use":
            parts.append({"functionCall": {"name": b["name"], "args": b.get("input") or {}, "id": b["id"]}})
    u = msg.get("usage") or {}
    read, write = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
    stop = msg.get("stop_reason") or ""
    return {"candidates": [{"content": {"role": "model", "parts": parts, "_raw": blocks},
                            "finishReason": STOP_MAP.get(stop, stop.upper())}],
            "usageMetadata": {"promptTokenCount": (u.get("input_tokens") or 0) + read + write,
                              "candidatesTokenCount": u.get("output_tokens") or 0,
                              "cachedContentTokenCount": read, "cacheCreationTokenCount": write},
            "modelVersion": msg.get("model")}


class ClaudeClient:
    reports_cache = True                                           # agent 가 캐시 토큰을 따로 셈

    def __init__(self, model: str, *, key: str | None = None, timeout: float = 300.0, max_retries: int = 4,
                 base_url: str | None = None):
        """key 를 주면 그것(대시보드에서 사용자가 넣은 자기 키)을, 없으면 환경변수 ANTHROPIC_API_KEY. 키는 메모리에만 둔다."""
        self.model = model
        key = (key or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ClaudeError("Claude API 키가 없다. 대시보드에 넣거나 환경변수 ANTHROPIC_API_KEY 를 설정한다")
        kw = {"base_url": base_url} if base_url else {}
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries, **kw)
        effort = os.environ.get("SLLMT_CLAUDE_EFFORT", "").strip().lower()
        self.effort = effort or (DEFAULT_EFFORT if model in EFFORT_MODELS else None)
        self.sent = 0                                              # 요청 수 (SDK 안의 재시도는 따로 세지 못함)
        self.ok = 0

    def generate(self, contents: list, *, system: str, tool_specs: list) -> dict:
        kw = dict(model=self.model, max_tokens=MAX_TOKENS, system=system, messages=to_messages(contents),
                  cache_control={"type": "ephemeral"})
        if tool_specs:
            kw["tools"] = tools_for(tool_specs)
        if self.effort:
            kw["output_config"] = {"effort": self.effort}
        self.sent += 1
        try:
            if self.model in FALLBACK_MODELS:
                resp = self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
            else:
                resp = self._client.messages.create(**kw)
        except anthropic.APIStatusError as e:
            raise ClaudeError(f"HTTP {e.status_code}: {str(e)[:800]}") from None
        except anthropic.APITimeoutError as e:
            raise ClaudeError(f"응답 시간 초과: {e}") from None
        except anthropic.APIConnectionError as e:
            raise ClaudeError(f"연결 실패: {e}") from None
        self.ok += 1
        return to_gemini(resp.to_dict(mode="json"))
