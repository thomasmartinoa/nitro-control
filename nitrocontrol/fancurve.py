"""Fan curve maths and the safety rules that sit on top of every manual fan speed."""

MIN_POINTS, MAX_POINTS = 2, 8
TEMP_MIN, TEMP_MAX = 20, 100

# Built-in curves: (temperature °C, fan %)
CURVES = {
    "silent": [[45, 0], [55, 20], [65, 35], [75, 55], [83, 80], [90, 100]],
    "balanced": [[40, 15], [55, 30], [65, 45], [75, 65], [82, 85], [88, 100]],
    "performance": [[40, 30], [55, 45], [65, 60], [72, 80], [80, 100]],
}

# Whatever the user asks for, fans never run slower than this at a given temperature.
SAFETY_FLOOR = [(75, 40), (85, 70), (90, 100)]

HYSTERESIS = 3.0      # °C the temperature must drop before a curve slows the fan
RAMP_DOWN = 4         # max % per second a curve may slow down (ramping up is instant)


def validate_curve(points):
    """Return a clean, sorted, monotonic curve or raise ValueError."""
    if not isinstance(points, (list, tuple)) or not MIN_POINTS <= len(points) <= MAX_POINTS:
        raise ValueError("a curve needs %d..%d points" % (MIN_POINTS, MAX_POINTS))
    clean = []
    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError("each point must be [temperature, percent]")
        t, pct = float(pt[0]), float(pt[1])
        if not TEMP_MIN <= t <= TEMP_MAX or not 0 <= pct <= 100:
            raise ValueError("point %r out of range" % (pt,))
        clean.append([round(t), round(pct)])
    clean.sort(key=lambda x: x[0])
    for a, b in zip(clean, clean[1:]):
        if a[0] == b[0]:
            raise ValueError("two points share %d °C" % a[0])
    # Fan speed may never drop as temperature rises
    top = 0
    for pt in clean:
        top = max(top, pt[1])
        pt[1] = top
    return clean


def interpolate(points, temp):
    if temp is None:
        return 100
    if temp <= points[0][0]:
        return points[0][1]
    if temp >= points[-1][0]:
        return points[-1][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            return p0 + (p1 - p0) * (temp - t0) / float(t1 - t0)
    return points[-1][1]


def safety_floor(temp):
    floor = 0
    if temp is None:
        return 0
    for t, pct in SAFETY_FLOOR:
        if temp >= t:
            floor = pct
    return floor


def next_speed(points, temp, previous):
    """Curve target with hysteresis and a gentle ramp-down; never below the safety floor."""
    target = interpolate(points, temp)
    if previous is not None and target < previous:
        # Only slow down once the temperature has fallen HYSTERESIS degrees further
        target = min(previous, interpolate(points, temp + HYSTERESIS))
        target = max(target, previous - RAMP_DOWN)
    return int(round(max(target, safety_floor(temp))))
