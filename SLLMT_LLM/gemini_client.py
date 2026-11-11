"""Gemini API 어댑터 — generateContent REST 를 표준 라이브러리(urllib)로 부른다. 추가 설치 없음.

키는 사용자 각자의 것이다. 대시보드에서는 사용자가 넣은 키(세션 메모리에만 둠), 터미널에서는 환경변수
GEMINI_API_KEY (없으면 GOOGLE_API_KEY) 를 쓴다. 키를 파일·로그·화면에 남기지 않는다.
요청 형식 (2026-11-04 확인, aistudio.google.com/api-reference/generate-content):
  POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent, 헤더 x-goog-api-key
  tools[].functionDeclarations[] = {name, description, parametersJsonSchema}
  모델 응답의 functionCall 부분(thoughtSignature 포함)은 다음 요청에 그대로 돌려준다 → contents 에 응답 content 를 통째로 붙인다
  도구 결과는 role "user" 의 functionResponse {name, response, id} 로 보낸다
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# 유료 가격 (USD / 1M 토큰, 입력·출력). 2026-11-04 aistudio.google.com/docs/pricing. 무료 등급은 0원(한도 있음)
PRICES = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),                 # 2027-01-01 부터 1.50 / 7.50
}
DEFAULT_MODEL = "gemini-2.5-flash-lite"               # 가장 싼 모델


class GeminiError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise GeminiError("API 키가 없다. 환경변수 GEMINI_API_KEY 를 설정한다 (README 'API 키 발급' 참고)")
    return key


def declarations(tool_specs: list) -> list:
    return [{"name": t["name"], "description": t["description"], "parametersJsonSchema": t["parameters"]}
            for t in tool_specs]


class GeminiClient:
    def __init__(self, model: str = DEFAULT_MODEL, *, key: str | None = None, timeout: float = 120.0, max_retries: int = 4):
        """key 를 주면 그것(대시보드에서 사용자가 넣은 자기 키)을, 없으면 환경변수를 쓴다. 키는 메모리에만 둔다."""
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._key = (key or "").strip() or api_key()
        self.sent = 0                       # 재시도 포함 실제 HTTP 전송 수 (무료 하루 한도는 이 기준에 가까움, 11-05)
        self.ok = 0                         # 정상 응답 수

    def generate(self, contents: list, *, system: str, tool_specs: list) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": [{"functionDeclarations": declarations(tool_specs)}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(URL.format(model=self.model), data=data, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
        for attempt in range(self.max_retries + 1):
            self.sent += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.loads(r.read().decode("utf-8"))
                self.ok += 1
                return out
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:800]
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(min(60, 4 * 2 ** attempt))                # 무료 등급 분당 한도 등
                    continue
                raise GeminiError(f"HTTP {e.code}: {msg}") from None
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    time.sleep(4 * 2 ** attempt)
                    continue
                raise GeminiError(f"연결 실패: {e.reason}") from None
            except (TimeoutError, ConnectionError) as e:               # 응답 읽기 시간 초과·연결 끊김도 재시도 (11-06 확인 실행 3건)
                if attempt < self.max_retries:
                    time.sleep(4 * 2 ** attempt)
                    continue
                raise GeminiError(f"응답 시간 초과·연결 끊김: {type(e).__name__}: {e}") from None
        raise GeminiError("재시도 한도 초과")


def parse(resp: dict) -> tuple[dict, list, str, str]:
    """(모델 content, functionCall 목록, 본문 텍스트, finishReason)."""
    cands = resp.get("candidates") or []
    if not cands:
        fb = resp.get("promptFeedback", {})
        raise GeminiError(f"응답 후보 없음: {json.dumps(fb, ensure_ascii=False)[:300]}")
    c = cands[0]
    content = c.get("content") or {"role": "model", "parts": []}
    content.setdefault("role", "model")
    parts = content.get("parts", [])
    calls = [p["functionCall"] for p in parts if "functionCall" in p]
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
    return content, calls, text, c.get("finishReason", "")


def function_response(call: dict, result: dict) -> dict:
    fr = {"name": call["name"], "response": {"result": result}}
    if call.get("id"):
        fr["id"] = call["id"]
    return {"functionResponse": fr}


def cost(model: str, usage: dict) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (usage.get("in", 0) * pin + usage.get("out", 0) * pout) / 1e6
