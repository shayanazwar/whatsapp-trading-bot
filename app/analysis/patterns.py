from __future__ import annotations

"""Causal 4H triangle detection and 1H breakout/retest confirmation.

Only completed candles should be supplied by the caller. Pivot points are not
available until their right-side confirmation bars have closed.
"""

import math
from itertools import combinations
from collections import Counter
from typing import Any, Iterable

FOUR_HOURS_MS = 14_400_000

# Public strategy constants. Keep detector gates centralized so the engine's
# configuration fingerprint accurately describes the behavior being tested.
TRIANGLE_LOOKBACK_4H = 64
TRIANGLE_PIVOT_LEFT = 2
TRIANGLE_PIVOT_RIGHT = 2
TRIANGLE_MAX_ANCHOR_AGE_4H = 8
TRIANGLE_MIN_SPAN_4H = 8
TRIANGLE_MAX_SPAN_4H = 48
TRIANGLE_MIN_HEIGHT_ATR = 1.0
TRIANGLE_MAX_HEIGHT_ATR = 8.0
TRIANGLE_MIN_CURRENT_WIDTH_ATR = 0.25
TRIANGLE_MAX_CURRENT_WIDTH_RATIO = 0.90
TRIANGLE_MAX_APEX_DISTANCE_4H = 48
TRIANGLE_MAX_BOUNDARY_EXTENSION_ATR = 1.5
# Trendline slope is measured in price per 4H bar. 0.015 ATR/bar allows
# normal shallow triangle boundaries while still rejecting effectively flat
# channels; the prior 0.05 ATR/bar threshold rejected many valid formations.
TRIANGLE_FLAT_SLOPE_ATR_PER_BAR = 0.015
TRIANGLE_MAX_PIVOTS_PER_SIDE = 6
TRIANGLE_MIN_TOUCHES_PER_BOUNDARY = 3
TRIANGLE_TOUCH_TOLERANCE_ATR = 0.35
TRIANGLE_MAX_BOUNDARY_VIOLATION_ATR = 0.45
BREAKOUT_RETEST_BARS_1H = 6
BREAKOUT_MIN_RELATIVE_VOLUME = 0.70
BREAKOUT_MAX_EXTENSION_ATR = 0.85
BREAKOUT_MIN_BODY_RATIO = 0.25
BREAKOUT_MIN_CLOSE_LOCATION = 0.58
BREAKOUT_BOUNDARY_BUFFER_ATR = 0.05
BREAKOUT_CROSS_TOLERANCE_ATR = 0.10
BREAKOUT_RETEST_TOLERANCE_ATR = 0.20
BREAKOUT_VOLUME_LOOKBACK_1H = 20


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _pivots(
    candles: list[dict[str, Any]],
    left: int = TRIANGLE_PIVOT_LEFT,
    right: int = TRIANGLE_PIVOT_RIGHT,
):
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        high = _finite(candles[i].get("high"))
        low = _finite(candles[i].get("low"))
        if all(high > _finite(candles[j].get("high")) for j in range(i - left, i)) and all(
            high > _finite(candles[j].get("high")) for j in range(i + 1, i + right + 1)
        ):
            highs.append((i, high))
        if all(low < _finite(candles[j].get("low")) for j in range(i - left, i)) and all(
            low < _finite(candles[j].get("low")) for j in range(i + 1, i + right + 1)
        ):
            lows.append((i, low))
    return highs, lows


def _fit_line(points: list[tuple[int, float]]) -> tuple[float, float]:
    """Return slope/intercept using ordinary least squares over pivot points."""
    if len(points) < 2:
        raise ValueError("At least two pivots are needed to fit a trendline")
    xs = [float(item[0]) for item in points]
    ys = [float(item[1]) for item in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        raise ValueError("Trendline pivots must have distinct indices")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    return slope, mean_y - slope * mean_x


def line_at_time(pattern: dict[str, Any], which: str, timestamp_ms: int, base_time_ms: int) -> float:
    slope = _finite(pattern.get(f"{which}_slope"))
    intercept = _finite(pattern.get(f"{which}_intercept"))
    x = (int(timestamp_ms) - int(base_time_ms)) / FOUR_HOURS_MS
    return slope * x + intercept


def detect_triangle_pattern(
    candles_4h: Iterable[dict[str, Any]],
    atr_4h: float,
    *,
    lookback: int = TRIANGLE_LOOKBACK_4H,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Detect a confirmed triangle using multiple candidate pivot pairs.

    The previous implementation fitted one line through only the last three
    highs and one through the last three lows. In noisy markets, a single newer
    swing from an adjacent structure could reverse either fitted slope and
    reject the entire window before breakout confirmation. This implementation
    tests multiple confirmed pivot pairs and accepts only pairs that independently
    satisfy convergence, touch, containment, width, apex and recency checks.

    All pivots use right-side confirmation bars; no unfinished/future candles are
    used. Only completed 4H candles should be supplied by the caller.
    """
    rows = list(candles_4h or [])
    atr = _finite(atr_4h)

    def reject(reason: str):
        if diagnostics is not None:
            diagnostics["reason"] = reason
        return None

    if diagnostics is not None:
        diagnostics["reason"] = "PATTERN_ACCEPTED"
    if len(rows) < 32:
        return reject("INSUFFICIENT_4H_CANDLES")
    if atr <= 0:
        return reject("INVALID_4H_ATR")
    if lookback < 24:
        return reject("LOOKBACK_TOO_SHORT")

    start = max(0, len(rows) - min(int(lookback), len(rows)))
    window = rows[start:]
    local_highs, local_lows = _pivots(window, left=TRIANGLE_PIVOT_LEFT, right=TRIANGLE_PIVOT_RIGHT)
    highs = [(idx + start, value) for idx, value in local_highs]
    lows = [(idx + start, value) for idx, value in local_lows]
    if len(highs) < 2 or len(lows) < 2:
        return reject("INSUFFICIENT_CONFIRMED_PIVOTS")

    # Use multiple recent confirmed swings. Candidate boundaries must be
    # supported by two real pivots; other pivots are used as fit/containment
    # evidence rather than blindly forcing the newest three into one line.
    recent_highs = highs[-TRIANGLE_MAX_PIVOTS_PER_SIDE:]
    recent_lows = lows[-TRIANGLE_MAX_PIVOTS_PER_SIDE:]
    upper_pairs = [pair for pair in combinations(recent_highs, 2) if pair[1][0] - pair[0][0] >= 3]
    lower_pairs = [pair for pair in combinations(recent_lows, 2) if pair[1][0] - pair[0][0] >= 3]
    if not upper_pairs or not lower_pairs:
        return reject("INSUFFICIENT_SEPARATED_PIVOT_PAIRS")

    flat_threshold = max(
        TRIANGLE_FLAT_SLOPE_ATR_PER_BAR * atr,
        abs(_finite(rows[-1].get("close"))) * 1e-5,
    )
    current_idx = len(rows) - 1
    tolerance = TRIANGLE_TOUCH_TOLERANCE_ATR * atr
    max_violation = TRIANGLE_MAX_BOUNDARY_VIOLATION_ATR * atr
    rejection_counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []

    for upper_pair in upper_pairs:
        try:
            upper_slope, upper_intercept = _fit_line(list(upper_pair))
        except ValueError:
            rejection_counts["INVALID_TRENDLINE_PIVOTS"] += 1
            continue
        for lower_pair in lower_pairs:
            try:
                lower_slope, lower_intercept = _fit_line(list(lower_pair))
            except ValueError:
                rejection_counts["INVALID_TRENDLINE_PIVOTS"] += 1
                continue

            anchor_points = [upper_pair[0], upper_pair[1], lower_pair[0], lower_pair[1]]
            pattern_start = min(point[0] for point in anchor_points)
            latest_anchor = max(point[0] for point in anchor_points)
            pattern_span = latest_anchor - pattern_start
            if pattern_span < TRIANGLE_MIN_SPAN_4H or pattern_span > TRIANGLE_MAX_SPAN_4H:
                rejection_counts["PATTERN_SPAN_OUT_OF_RANGE"] += 1
                continue
            if current_idx - latest_anchor > TRIANGLE_MAX_ANCHOR_AGE_4H:
                rejection_counts["STALE_PIVOT_ANCHORS"] += 1
                continue

            # A triangle narrows from left to right: the upper boundary must
            # fall faster (or rise more slowly) than the lower boundary.
            if upper_slope >= lower_slope:
                rejection_counts["BOUNDARIES_NOT_CONVERGING"] += 1
                continue

            if upper_slope < -flat_threshold and lower_slope > flat_threshold:
                kind = "SYMMETRICAL_TRIANGLE"
            elif upper_slope < -flat_threshold and abs(lower_slope) <= 1.8 * flat_threshold:
                kind = "DESCENDING_TRIANGLE"
            elif lower_slope > flat_threshold and abs(upper_slope) <= 1.8 * flat_threshold:
                kind = "ASCENDING_TRIANGLE"
            else:
                rejection_counts["UNSUPPORTED_TRIANGLE_SLOPES"] += 1
                continue

            def upper_at(index: float) -> float:
                return upper_slope * index + upper_intercept

            def lower_at(index: float) -> float:
                return lower_slope * index + lower_intercept

            upper_start = upper_at(pattern_start)
            lower_start = lower_at(pattern_start)
            upper_now = upper_at(current_idx)
            lower_now = lower_at(current_idx)
            start_width = upper_start - lower_start
            current_width = upper_now - lower_now
            if start_width <= 0 or current_width <= 0:
                rejection_counts["INVALID_PROJECTED_WIDTH"] += 1
                continue
            if not (TRIANGLE_MIN_HEIGHT_ATR * atr <= start_width <= TRIANGLE_MAX_HEIGHT_ATR * atr):
                rejection_counts["START_HEIGHT_OUTSIDE_ATR_RANGE"] += 1
                continue
            if current_width < TRIANGLE_MIN_CURRENT_WIDTH_ATR * atr or current_width >= TRIANGLE_MAX_CURRENT_WIDTH_RATIO * start_width:
                rejection_counts["CURRENT_WIDTH_FILTER"] += 1
                continue

            slope_gap = upper_slope - lower_slope
            if abs(slope_gap) <= 1e-12:
                rejection_counts["PARALLEL_TRENDLINES"] += 1
                continue
            apex = (lower_intercept - upper_intercept) / slope_gap
            if not (current_idx + 1 <= apex <= current_idx + TRIANGLE_MAX_APEX_DISTANCE_4H):
                rejection_counts["APEX_OUTSIDE_VALID_WINDOW"] += 1
                continue

            # Validate that intermediate confirmed swings broadly respect the
            # proposed boundaries. Wicks may overshoot slightly, but a line that
            # cuts through several pivots is not accepted as a triangle.
            interval_highs = [(idx, price) for idx, price in highs if pattern_start <= idx <= latest_anchor]
            interval_lows = [(idx, price) for idx, price in lows if pattern_start <= idx <= latest_anchor]
            if not interval_highs or not interval_lows:
                rejection_counts["INSUFFICIENT_BOUNDARY_TOUCHES"] += 1
                continue
            upper_violations = [max(0.0, price - upper_at(idx)) for idx, price in interval_highs]
            lower_violations = [max(0.0, lower_at(idx) - price) for idx, price in interval_lows]
            upper_violation = max(upper_violations, default=0.0)
            lower_violation = max(lower_violations, default=0.0)
            if upper_violation > max_violation or lower_violation > max_violation:
                rejection_counts["BOUNDARY_PENETRATION_TOO_LARGE"] += 1
                continue

            upper_touches = [
                (idx, price) for idx, price in interval_highs
                if -tolerance <= upper_at(idx) - price <= tolerance
            ]
            lower_touches = [
                (idx, price) for idx, price in interval_lows
                if -tolerance <= price - lower_at(idx) <= tolerance
            ]
            # Pair anchors define each line, but a third independent confirmed
            # touch is required on each boundary to reduce random-walk false
            # positives. The two fitted anchors alone are not sufficient evidence.
            if len(upper_touches) < TRIANGLE_MIN_TOUCHES_PER_BOUNDARY or len(lower_touches) < TRIANGLE_MIN_TOUCHES_PER_BOUNDARY:
                rejection_counts["INSUFFICIENT_BOUNDARY_TOUCHES"] += 1
                continue

            last_close = _finite(rows[-1].get("close"))
            if last_close > upper_now + TRIANGLE_MAX_BOUNDARY_EXTENSION_ATR * atr or last_close < lower_now - TRIANGLE_MAX_BOUNDARY_EXTENSION_ATR * atr:
                rejection_counts["PRICE_TOO_FAR_OUTSIDE_PATTERN"] += 1
                continue

            pattern_high = max(price for _, price in interval_highs)
            pattern_low = min(price for _, price in interval_lows)
            measured_height = pattern_high - pattern_low
            if measured_height < TRIANGLE_MIN_HEIGHT_ATR * atr or measured_height > TRIANGLE_MAX_HEIGHT_ATR * atr:
                rejection_counts["PIVOT_HEIGHT_OUTSIDE_ATR_RANGE"] += 1
                continue

            touches = len(upper_touches) + len(lower_touches)
            compression = (start_width - current_width) / max(start_width, 1e-12)
            age = current_idx - latest_anchor
            fit_penalty = (upper_violation + lower_violation) / atr
            # Rank valid alternatives using independent evidence: touch count,
            # compression, low boundary penetration, span and recency.
            quality = (
                10.0 * touches
                + 5.0 * _clamp(compression, 0.0, 1.0)
                + min(pattern_span, 32) / 16.0
                - 8.0 * fit_penalty
                - 0.25 * age
            )
            candidates.append({
                "kind": kind,
                "detector_type": "TRIANGLE",
                "allowed_sides": ["LONG", "SHORT"],
                "upper_slope": upper_slope,
                "upper_intercept": upper_intercept,
                "lower_slope": lower_slope,
                "lower_intercept": lower_intercept,
                "base_time_ms": int(rows[0].get("time", 0)),
                "start_idx": int(pattern_start),
                "end_idx": int(latest_anchor),
                "start_time": int(rows[pattern_start].get("time", 0)),
                "end_time": int(rows[latest_anchor].get("time", 0)),
                "upper_now": upper_now,
                "lower_now": lower_now,
                "height": measured_height,
                "start_width": start_width,
                "current_width": current_width,
                "apex_idx": apex,
                "high_points": upper_touches,
                "low_points": lower_touches,
                "pattern_high": pattern_high,
                "pattern_low": pattern_low,
                "touch_count": touches,
                "atr_4h": atr,
                "pattern_span_4h": pattern_span,
                "boundary_penetration_atr": fit_penalty,
                "quality": quality,
            })

    if candidates:
        best = max(
            candidates,
            key=lambda item: (
                float(item["quality"]),
                int(item["end_idx"]),
                int(item["touch_count"]),
            ),
        )
        if diagnostics is not None:
            diagnostics.update({
                "reason": "PATTERN_ACCEPTED",
                "candidate_count": len(candidates),
                "selected_kind": best["kind"],
                "selected_touch_count": best["touch_count"],
            })
        return best

    if diagnostics is not None:
        diagnostics["candidate_count"] = 0
        diagnostics["rejection_counts"] = dict(rejection_counts)
    if rejection_counts:
        # Most frequent specific failure is more actionable than a generic
        # final slope failure from a single arbitrary line fit.
        reason = rejection_counts.most_common(1)[0][0]
        return reject(reason)
    return reject("NO_VALID_TRIANGLE_CANDIDATE")


def _relative_volume(
    candles: list[dict[str, Any]],
    index: int,
    lookback: int = BREAKOUT_VOLUME_LOOKBACK_1H,
) -> float:
    start = max(0, index - lookback)
    prior = candles[start:index]
    if len(prior) < lookback:
        return 0.0
    average = sum(max(0.0, _finite(row.get("volume"))) for row in prior) / len(prior)
    return max(0.0, _finite(candles[index].get("volume"))) / average if average > 0 else 0.0


def confirm_triangle_breakout(
    candles_1h: Iterable[dict[str, Any]],
    candles_4h: Iterable[dict[str, Any]],
    pattern: dict[str, Any],
    side: str,
    atr_1h: float,
    *,
    max_retest_bars: int = BREAKOUT_RETEST_BARS_1H,
    min_relative_volume: float = BREAKOUT_MIN_RELATIVE_VOLUME,
    max_extension_atr: float = BREAKOUT_MAX_EXTENSION_ATR,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Confirm a fresh 1H close outside a 4H boundary, or its 1H retest.

    No unfinished candles, future pivots or post-decision prices are used. The
    newest 1H candle must itself be the breakout or a valid retest candle.
    """
    rows = list(candles_1h or [])
    c4 = list(candles_4h or [])
    side = str(side).upper()
    atr = _finite(atr_1h)

    def reject(reason: str):
        if diagnostics is not None:
            diagnostics["reason"] = reason
        return None

    if diagnostics is not None:
        diagnostics["reason"] = "TRIGGER_ACCEPTED"
    if len(rows) < 22:
        return reject("INSUFFICIENT_1H_TRIGGER_HISTORY")
    if not c4:
        return reject("MISSING_4H_PATTERN_CONTEXT")
    if atr <= 0:
        return reject("INVALID_1H_ATR")
    if side not in {"LONG", "SHORT"}:
        return reject("INVALID_SIDE")

    current = len(rows) - 1
    current_candle = rows[current]
    entry = _finite(current_candle.get("close"))
    open_price = _finite(current_candle.get("open"))
    high = _finite(current_candle.get("high"))
    low = _finite(current_candle.get("low"))
    candle_range = max(high - low, 1e-12)
    body_ratio = abs(entry - open_price) / candle_range
    location = (entry - low) / candle_range
    if side == "SHORT":
        location = 1.0 - location
    current_time = int(current_candle.get("time", 0))
    boundary_name = "upper" if side == "LONG" else "lower"
    boundary_now = line_at_time(pattern, boundary_name, current_time, int(c4[0].get("time", 0)))
    buffer = max(BREAKOUT_BOUNDARY_BUFFER_ATR * atr, abs(entry) * 1e-5)
    current_rvol = _relative_volume(rows, current, 20)

    strong = (
        body_ratio >= BREAKOUT_MIN_BODY_RATIO
        and location >= BREAKOUT_MIN_CLOSE_LOCATION
        and current_rvol >= min_relative_volume
        and (entry > open_price if side == "LONG" else entry < open_price)
    )
    if not strong:
        if body_ratio < BREAKOUT_MIN_BODY_RATIO:
            return reject("BREAKOUT_BODY_TOO_WEAK")
        if location < BREAKOUT_MIN_CLOSE_LOCATION:
            return reject("BREAKOUT_CLOSE_LOCATION_TOO_WEAK")
        if current_rvol < min_relative_volume:
            return reject("BREAKOUT_RELATIVE_VOLUME_TOO_LOW")
        return reject("BREAKOUT_CANDLE_DIRECTION_MISMATCH")

    distance_atr = abs(entry - boundary_now) / atr
    if distance_atr > max_extension_atr:
        return reject("BREAKOUT_TOO_EXTENDED_FROM_BOUNDARY")

    # Case 1: a fresh breakout on the decision candle.
    if current >= 1:
        previous = rows[current - 1]
        previous_time = int(previous.get("time", 0))
        previous_close = _finite(previous.get("close"))
        boundary_previous = line_at_time(pattern, boundary_name, previous_time, int(c4[0].get("time", 0)))
        if side == "LONG":
            crossed = previous_close <= boundary_previous + BREAKOUT_CROSS_TOLERANCE_ATR * atr and entry > boundary_now + buffer
        else:
            crossed = previous_close >= boundary_previous - BREAKOUT_CROSS_TOLERANCE_ATR * atr and entry < boundary_now - buffer
        if crossed:
            quality = min(1.0, 0.45 + 0.20 * min(body_ratio / 0.50, 1.0) + 0.20 * min(current_rvol / 1.20, 1.0) + 0.15 * min(location / 0.70, 1.0))
            return {
                "ready": True,
                "trigger_type": "TRIANGLE_BREAKOUT",
                "retest": False,
                "breakout_idx": current,
                "trigger_idx": current,
                "breakout_time": current_time,
                "trigger_time": current_time,
                "boundary": boundary_now,
                "body_ratio": body_ratio,
                "close_location": location,
                "rvol": current_rvol,
                "quality": quality,
                "distance_atr": distance_atr,
                "reason": f"confirmed 1H close above {pattern.get('kind')} resistance" if side == "LONG" else f"confirmed 1H close below {pattern.get('kind')} support",
            }

    # Case 2: breakout occurred recently and the current candle retests then
    # closes back outside the same projected boundary.
    first = max(1, current - max(1, int(max_retest_bars)))
    recent_cross_found = False
    for breakout_idx in range(current - 1, first - 1, -1):
        breakout = rows[breakout_idx]
        prior = rows[breakout_idx - 1]
        bt = int(breakout.get("time", 0))
        pt = int(prior.get("time", 0))
        breakout_boundary = line_at_time(pattern, boundary_name, bt, int(c4[0].get("time", 0)))
        prior_boundary = line_at_time(pattern, boundary_name, pt, int(c4[0].get("time", 0)))
        breakout_close = _finite(breakout.get("close"))
        prior_close = _finite(prior.get("close"))
        crossed = (
            prior_close <= prior_boundary + BREAKOUT_CROSS_TOLERANCE_ATR * atr and breakout_close > breakout_boundary + buffer
            if side == "LONG"
            else prior_close >= prior_boundary - BREAKOUT_CROSS_TOLERANCE_ATR * atr and breakout_close < breakout_boundary - buffer
        )
        if not crossed:
            continue
        recent_cross_found = True
        if side == "LONG":
            retested = low <= boundary_now + BREAKOUT_RETEST_TOLERANCE_ATR * atr and entry > boundary_now + buffer
        else:
            retested = high >= boundary_now - BREAKOUT_RETEST_TOLERANCE_ATR * atr and entry < boundary_now - buffer
        if not retested:
            continue
        quality = min(1.0, 0.45 + 0.20 * min(body_ratio / 0.50, 1.0) + 0.20 * min(current_rvol / 1.20, 1.0) + 0.15 * min(location / 0.70, 1.0))
        return {
            "ready": True,
            "trigger_type": "TRIANGLE_BREAKOUT_RETEST",
            "retest": True,
            "breakout_idx": breakout_idx,
            "trigger_idx": current,
            "breakout_time": bt,
            "trigger_time": current_time,
            "boundary": boundary_now,
            "body_ratio": body_ratio,
            "close_location": location,
            "rvol": current_rvol,
            "quality": quality,
            "distance_atr": distance_atr,
            "reason": f"1H breakout retest held {pattern.get('kind')} resistance" if side == "LONG" else f"1H breakout retest held {pattern.get('kind')} support",
        }
    return reject("BREAKOUT_RETEST_NOT_HELD" if recent_cross_found else "NO_RECENT_BOUNDARY_CROSS")
