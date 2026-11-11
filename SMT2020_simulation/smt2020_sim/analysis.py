"""Phase 13 — warm-up 판정, 반복 통계, 레퍼런스 대조.

여기 있는 것은 **전부 순수 함수**다. 시뮬레이션을 돌리지 않고 계열(list)만
받는다. 그래야 까다로운 부분 — 지수 정렬, 수렴하지 않는 계열, 작은 표본 구간 —
을 시뮬레이션 없이 시험할 수 있다. 13 7절이 그 셋을 각각 체크로 요구한다.

실행과 보고서 작성은 `run_analysis.py`가 한다.

**신뢰구간은 t 분포를 쓴다.** 13 4절: "A confidence interval at ten
replications needs the small-sample distribution, not a normal approximation.
`scipy` is not installed — either add it as a declared dependency of this phase
or embed the few critical values needed, and say which you did."
→ **임계값을 박아 넣었다.** 새 의존성을 들이지 않는다 (아래 `T_95`).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 작은 표본 신뢰구간
# ---------------------------------------------------------------------------

# 양측 95% t 임계값, 자유도 1..30. 그 위는 정규 근사로 충분하다(오차 <1%).
# **scipy를 들이지 않기로 한 선택의 결과물이다** (13 4절이 둘 중 하나를 고르고
# 무엇을 골랐는지 밝히라고 요구한다). 반복 10회면 df=9, 즉 2.262다 —
# 정규 근사 1.960을 쓰면 구간이 **15% 좁게** 나온다.
T_95: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
    14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
    20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
T_95_LARGE = 1.960          # df > 30


def t_critical(df: int) -> float:
    """양측 95% t 임계값. df<=0이면 구간을 만들 수 없다."""
    if df <= 0:
        raise ValueError(f"자유도가 {df}다 — 반복이 2회 이상이어야 구간이 나온다")
    return T_95.get(df, T_95_LARGE)


@dataclass(frozen=True, slots=True)
class Stat:
    """반복 통계 한 묶음. 13 4절이 요구하는 네 값 전부."""
    n: int
    mean: float
    sd: float
    cv_pct: float
    half_width: float           # 95% 반폭. n<2면 0
    method: str                 # 어떤 분포를 썼는지 — 보고서에 그대로 찍는다

    @property
    def lo(self) -> float:
        return self.mean - self.half_width

    @property
    def hi(self) -> float:
        return self.mean + self.half_width


def summarize(values: list[float]) -> Stat:
    """평균/표준편차/변동계수/95% 반폭.

    **반복이 1회면 반폭을 0으로 주지 않고 산포를 말할 수 없다고 표시한다.**
    0을 주면 "구간이 아주 좁다"로 읽힌다 — 표본 하나에서 가장 하기 쉬운 거짓말이다.
    """
    n = len(values)
    if n == 0:
        return Stat(0, 0.0, 0.0, 0.0, 0.0, "표본 없음")
    m = statistics.fmean(values)
    if n == 1:
        return Stat(1, m, 0.0, 0.0, 0.0, "반복 1회 — 산포를 말할 수 없다")
    sd = statistics.stdev(values)
    cv = 100.0 * sd / abs(m) if m else 0.0
    t = t_critical(n - 1)
    return Stat(n, m, sd, cv, t * sd / n ** 0.5,
                f"t 분포 (df={n - 1}, t={t:.3f})")


# ---------------------------------------------------------------------------
# 이동평균과 **지수 정렬**
# ---------------------------------------------------------------------------
#
# 13 3.1절: "State how a smoothed index maps back to a real week. An off-by-
# half-window error here is easy and invisible."
#
# 규약을 여기 한 번 못박고 `smoothed_week`로만 되돌린다. 계산하는 쪽에서
# 그때그때 더하면 창 크기가 바뀔 때 조용히 어긋난다.


def moving_average(series: list[float], window: int) -> list[float]:
    """중심 이동평균. 길이는 `len(series) - window + 1`이다.

    결과의 index `i`는 원 계열의 **`[i, i+window-1]` 구간**을 덮고,
    그 중심 주는 `smoothed_week(i, window)`다.

    짧아서 창이 안 들어가면 빈 목록을 준다 — 0으로 채우지 않는다.
    채우면 "평평하다"로 오독된다.
    """
    if window <= 0:
        raise ValueError(f"창 크기가 {window}다")
    if len(series) < window:
        return []
    out, run = [], sum(series[:window])
    out.append(run / window)
    for i in range(window, len(series)):
        run += series[i] - series[i - window]
        out.append(run / window)
    return out


def smoothed_week(index: int, window: int) -> int:
    """평활 index -> 원 계열의 주 번호 (창의 중심).

    이 함수가 존재하는 이유가 13 3.1절의 "off-by-half-window" 경고다.
    """
    return index + (window - 1) // 2


# ---------------------------------------------------------------------------
# warm-up 판정
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WarmupEstimate:
    window: int
    settled: bool
    week: int | None            # 원 계열 기준 주 번호
    detail: str


@dataclass(slots=True)
class WarmupVerdict:
    """여러 창의 판정을 모은 것. **가장 큰 값을 조용히 고르지 않는다.**"""
    estimates: list[WarmupEstimate] = field(default_factory=list)
    agreed: bool = False
    week: int | None = None
    detail: str = ""


def warmup_by_ensemble(series: list[float], *, windows=(5, 9, 13, 21),
                       flat_for: int = 26, drift_tol: float = 0.02,
                       ) -> WarmupVerdict:
    """평활 계열이 **추세를 멈추는** 첫 지점 (13 3.1절).

    창마다 따로 판정하고 **전부 보고한다.** 전이 구간과 비슷한 길이의 창은
    찾으려는 바로 그것을 뭉개 버리므로, 하나만 쓰면 창 선택이 답을 정한다.

    판정: 후보 index `j` 이후 `flat_for`개 점에 최소제곱 직선을 맞추고,
    그 구간의 **총 변화량**이 평균 대비 `drift_tol` 안이면 평평하다고 본다.
    기울기가 아니라 총 변화량을 보는 이유는 13 3.4절이다 — 꾸준히 오르는
    계열도 후반에는 **상대 기울기**가 작아져서 기울기만으로는 속는다.

    `flat_for`개가 남지 않으면 그 창은 **판정 불가**다. 남은 데이터가 모자란
    것을 "평평하다"로 읽으면 안 된다 (13 7절 "Sufficient data").

    창끼리 크게 어긋나면 `agreed=False`로 두고 **정착하지 않았다고 말한다.**
    """
    ests: list[WarmupEstimate] = []
    for w in windows:
        sm = moving_average(series, w)
        if len(sm) < flat_for + 1:
            ests.append(WarmupEstimate(
                w, False, None,
                f"평활 후 {len(sm)}점 — 평평 판정에 {flat_for}점이 필요하다"))
            continue
        found = None
        for j in range(len(sm) - flat_for):
            seg = sm[j:j + flat_for]
            base = statistics.fmean(seg)
            if base == 0:
                continue
            slope = _ls_slope(seg)
            if abs(slope * (flat_for - 1)) / abs(base) <= drift_tol:
                found = j
                break
        if found is None:
            ests.append(WarmupEstimate(
                w, False, None,
                f"끝까지 평평해지지 않았다 (허용 총변화 {drift_tol:.1%})"))
        else:
            ests.append(WarmupEstimate(
                w, True, smoothed_week(found, w),
                f"평활 index {found} -> 주 {smoothed_week(found, w)}"))

    ok = [e for e in ests if e.settled]
    if not ok:
        return WarmupVerdict(ests, False, None,
                             "어떤 창에서도 평평해지지 않았다 — **정착하지 않았다**")
    weeks = [e.week for e in ok]
    lo, hi = min(weeks), max(weeks)
    # 창끼리의 불일치 허용폭. 가장 큰 창의 절반 — 그보다 벌어지면 창이 답을
    # 정하고 있다는 뜻이다.
    tol = max(windows) // 2 + 1
    if len(ok) < len(ests):
        return WarmupVerdict(
            ests, False, None,
            f"{len(ests) - len(ok)}개 창이 판정하지 못했다 — "
            "남은 창의 값만 쓰면 창 선택이 답을 정한다")
    if hi - lo > tol:
        return WarmupVerdict(
            ests, False, None,
            f"창끼리 {lo}~{hi}주로 {hi - lo}주 어긋난다 (허용 {tol}주) — "
            "**정착하지 않았다.** 가장 큰 값을 고르지 않는다")
    return WarmupVerdict(ests, True, hi,
                         f"{len(ok)}개 창이 {lo}~{hi}주로 일치 — 보수적으로 {hi}주")


def _ls_slope(y: list[float]) -> float:
    """등간격 최소제곱 기울기 (단위: 점당)."""
    n = len(y)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = statistics.fmean(y)
    num = sum((i - mx) * (v - my) for i, v in enumerate(y))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def warmup_by_cv(series: list[float], *, smooth: int = 5, look: int = 26,
                 cv_tol: float = 0.05) -> WarmupEstimate:
    """두 번째 의견 (13 3.2절).

    짧은 이동평균을 만든 뒤, **이어지는 `look`개 구간의 변동계수**가 기준
    아래로 내려가는 첫 지점을 찾는다. 앞의 방법과 창 파라미터를 공유하지
    않으므로 **다르게 실패한다** — 그래서 일치하면 정보가 되고 어긋나면 경고다.

    **알려진 약점: 느린 램프에 속는다.** CV는 상대량이라 계열이 커지면 같은
    기울기라도 작아진다. 13 3.4절이 지목한 "a relative-slope criterion alone
    can be fooled"가 이것이다. 일부러 두는 것이다 — 이 함수를 앙상블 방법과
    같은 방식으로 단단하게 만들면 **둘이 같은 방식으로 실패**해서 불일치
    신호가 죽는다. 속는 것을 막는 것은 `warmup_by_ensemble`의 총변화 기준과
    `absolute_sanity`이고, 이 약점은 테스트로 고정해 두었다.
    """
    sm = moving_average(series, smooth)
    if len(sm) < look + 1:
        return WarmupEstimate(smooth, False, None,
                              f"평활 후 {len(sm)}점 — {look}점이 필요하다")
    for j in range(len(sm) - look):
        seg = sm[j:j + look]
        m = statistics.fmean(seg)
        if m == 0:
            continue
        if statistics.pstdev(seg) / abs(m) <= cv_tol:
            wk = smoothed_week(j, smooth)
            return WarmupEstimate(smooth, True, wk,
                                  f"CV <= {cv_tol:.0%} 지점, 주 {wk}")
    return WarmupEstimate(smooth, False, None,
                          f"끝까지 CV가 {cv_tol:.0%} 아래로 안 내려갔다")


# ---------------------------------------------------------------------------
# 처리량 추세 교차확인 (13 3.3절)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrendVerdict:
    # **`decided`가 먼저다.** 표본이 모자라 판정하지 못한 것을 `rising`으로
    # 뭉뚱그리면 "모른다"가 "오른다"로 둔갑한다. `wait_gate`가 6개 phase 동안
    # 0.0이었던 것과 같은 부류의 오류다 (G-19).
    decided: bool
    rising: bool
    slope_per_week: float       # 반복별 기울기의 평균
    half_width: float           # 그 평균의 95% 반폭
    drift_pct: float            # 구간 전체에 걸친 상대 변화
    detail: str


def throughput_trend(per_replication: list[list[float]], *, after: int,
                     drift_tol_pct: float = 2.0) -> TrendVerdict:
    """후보 지점 이후 주간 처리량이 아직 오르고 있는가.

    **반복마다 따로 기울기를 재고 그 기울기들을 모은다** (13 3.3절). 여러
    반복의 주를 한 통에 넣고 독립 표본처럼 다루면, 같은 반복의 주들이 서로
    상관돼 있어서 불확실성을 과소평가한다.

    **판정은 크기가 한다.** 구간 전체 상대 변화가 `drift_tol_pct`를 넘으면
    오르는 것이다. 3.3절이 "A test that fails to reject a zero trend is **not**
    evidence of steady state. Also state the drift magnitude you are willing to
    accept"라고 한 그대로다.

    구간(95%)은 **함께 보고하되 판정하지 않는다.** 처음에는 "구간이 0을
    포함해야 평평"으로 짰다가 뒤집었다 — 반복들이 거의 같으면 기울기의 분산이
    0에 가까워져 구간이 한 점으로 수축하고, 그러면 **무시해도 될 만큼 작은
    드리프트까지 '오른다'로 잡힌다.** 통계적 유의성과 실질적 크기는 다른
    질문이고, 여기서 답해야 하는 것은 후자다.

    다만 "작지만 일관된 드리프트"는 알아야 하므로 구간이 0을 배제하면
    상세에 적는다.
    """
    slopes, drifts = [], []
    for s in per_replication:
        seg = s[after:]
        if len(seg) < 3:
            continue
        m = statistics.fmean(seg)
        if m == 0:
            continue
        sl = _ls_slope(seg)
        slopes.append(sl)
        drifts.append(100.0 * sl * (len(seg) - 1) / abs(m))
    if not slopes:
        return TrendVerdict(False, False, 0.0, 0.0, 0.0,
                            f"주 {after} 이후 표본이 모자라 **판정할 수 없다**")

    st = summarize(slopes)
    drift = statistics.fmean(drifts)
    rising = abs(drift) > drift_tol_pct          # **크기가 판정한다**
    parts = [f"기울기 {st.mean:+.4f}/주"]
    if st.n > 1:
        parts.append(f"95% [{st.lo:+.4f}, {st.hi:+.4f}]")
    parts.append(f"구간 변화 {drift:+.2f}% (허용 ±{drift_tol_pct:g}%)")
    if rising:
        parts.append("**변화 크기가 허용을 넘는다**")
    elif st.n > 1 and not (st.lo <= 0.0 <= st.hi):
        # 판정은 평평이지만 방향이 일관된다. 숨기지 않는다.
        parts.append("작지만 일관된 드리프트 (구간이 0을 배제한다)")
    return TrendVerdict(True, rising, st.mean, st.half_width, drift,
                        ", ".join(parts))


def settle_by_trend(per_replication: list[list[float]], *, start: int,
                    step: int = 13, max_tries: int = 8,
                    drift_tol_pct: float = 2.0) -> tuple[int | None, list[str]]:
    """아직 오르고 있으면 후보를 뒤로 밀며 다시 본다 (13 3.3절, 반복 상한 있음).

    상한에 닿으면 `None`을 준다 — **마지막 후보를 답으로 내놓지 않는다.**
    내놓으면 "판정 실패"가 "판정 성공"으로 둔갑한다.

    표본이 모자라 판정할 수 없게 되면 **거기서 멈춘다.** 계속 밀면 남은 주가
    더 줄어들 뿐인데, 그걸 "아직 오른다"로 찍으면 "모른다"가 "오른다"로
    둔갑한다. 판정 불가와 상승은 다른 결론이다.
    """
    log, at = [], start
    for _ in range(max_tries):
        v = throughput_trend(per_replication, after=at,
                             drift_tol_pct=drift_tol_pct)
        if not v.decided:
            log.append(f"주 {at}: {v.detail}")
            log.append("표본이 모자라 더 밀어도 나아지지 않는다 — "
                       "**판정하지 못했다.** 기간을 늘려야 한다")
            return None, log
        log.append(f"주 {at}: {'아직 오른다' if v.rising else '평평'} — {v.detail}")
        if not v.rising:
            return at, log
        at += step
    log.append(f"상한 {max_tries}회에 닿았다 — **판정하지 못했다**")
    return None, log


# ---------------------------------------------------------------------------
# 절대 궤적 온전성 (13 3.4절)
# ---------------------------------------------------------------------------


def absolute_sanity(series: list[float], week: int, *, target: float,
                    reach_frac: float = 0.9) -> tuple[bool, str]:
    """빈 fab에서 아주 짧은 warm-up은 **겉보기부터 틀렸다** (13 3.4절).

    상대 기울기만 보면 꾸준히 오르는 계열도 후반에 작아 보여 속는다. 그래서
    "그 시점에 WIP이 정상상태 수준의 `reach_frac`에 닿았는가"를 절대값으로
    같이 본다.
    """
    if week >= len(series):
        return False, f"주 {week}이 계열 길이 {len(series)}를 넘는다"
    got = series[week]
    ok = got >= target * reach_frac
    return ok, (f"주 {week}의 WIP {got:,.0f} / 목표 {target:,.0f}의 "
                f"{100.0 * got / target if target else 0:.0f}% "
                f"(기준 {reach_frac:.0%})")


# ---------------------------------------------------------------------------
# 앙상블
# ---------------------------------------------------------------------------


def ensemble(series_list: list[list[float]]) -> list[float]:
    """반복별 주간 계열을 주마다 평균낸다 (13 3.1절).

    길이가 다르면 **가장 짧은 것에 맞춰 자른다.** 없는 주를 0이나 마지막
    값으로 채우면 그 자리에서 가짜 추세가 생긴다.
    """
    if not series_list:
        return []
    n = min(len(s) for s in series_list)
    if n == 0:
        return []
    return [statistics.fmean([s[i] for s in series_list]) for i in range(n)]
