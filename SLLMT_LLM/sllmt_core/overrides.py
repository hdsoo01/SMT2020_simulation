"""Pure conversion of tool arguments; workbook validation remains in datagen."""
from __future__ import annotations

def build_overrides(tool_changes, processing_time, pm_changes) -> tuple[dict, str | None]:
    """LLM 인자 → 시나리오 overrides. 배율 1(바뀌지 않음)은 뺀다. 이름·범위 검사는 datagen/data_overrides.py 가 한다."""
    def whole(v):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v

    def target(e):
        return {k: e[k] for k in ("toolgroup", "area") if e.get(k)}

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def recip(e, direct, rate, what):
        """시간·간격 배율(direct) 또는 속도·빈도 배율(rate) 중 하나 → 시간·간격 배율. 역수는 도구가 계산 (11-05, D-66)."""
        if e.get(direct) is not None and e.get(rate) is not None:
            return None, f"{what}: {direct} 와 {rate} 는 함께 쓰지 않는다"
        if e.get(rate) is not None:
            if not num(e[rate]) or float(e[rate]) <= 0:
                return None, f"{what}: {rate} 는 0보다 큰 수 ({e[rate]!r})"
            return round(1.0 / float(e[rate]), 4), None
        return e.get(direct), None

    ov = {}
    tools = []
    for t in tool_changes or []:
        d = whole(t.get("delta"))
        if not isinstance(d, int) or isinstance(d, bool):
            return {}, f"tool_changes: delta 는 정수 ({t.get('delta')!r})"
        r = {"toolgroup": t.get("toolgroup"), "delta": d}
        if t.get("start_week"):
            r["start_week"] = whole(t["start_week"])
        tools.append(r)
    if tools:
        ov["tools"] = tools
    pt = []
    for e in processing_time or []:
        m, err = recip(e, "mult", "speed_mult", "processing_time")
        if err:
            return {}, err
        if m is None:
            return {}, "processing_time: mult(시간 표현) 또는 speed_mult(속도 표현) 중 하나를 준다"
        if num(m):
            if float(m) == 1.0:
                continue
            m = float(m)
        pt.append({**target(e), "mult": m})                        # 숫자가 아니면 그대로 넘겨 검사에서 거부
    if pt:
        ov["processing_time"] = pt
    pm = []
    for e in pm_changes or []:
        r = {**target(e)}
        iv, err = recip(e, "interval_mult", "frequency_mult", "pm_changes")
        if err:
            return {}, err
        for k, v in (("interval_mult", 1.0 if iv is None else iv), ("duration_mult", e.get("duration_mult", 1.0))):
            r[k] = float(v) if num(v) else v
        if r["interval_mult"] != 1.0 or r["duration_mult"] != 1.0:
            pm.append(r)
    if pm:
        ov["pm"] = pm
    return ov, None

