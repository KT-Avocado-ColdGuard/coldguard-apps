"""수집기와 현장 웹이 공유하는 온도 추세 및 경보 판정."""
LIMIT = -25.0
WINDOW = 60.0
EARLY_SEC = 50.0
HOLD_SEC = 10.0
SIM_RISE = 0.04  # 시뮬레이터와 공유하는 문 열림 상승 속도 (°C/초)

def analyze(old, msg, received, stale_sec):
    # 순서가 뒤바뀐 기록은 보관만 하고 최신 상태/추세를 과거로 되돌리지 않는다.
    if old and msg["ts"] <= old["ts"]:
        return old, False
    msg = dict(msg)  # 판정은 전송된 temp_c 그대로 사용한다.
    rearm = bool(old and old.get("rearm"))
    if old and old["door"] and not msg["door"] and old["temp"] > -29.5:
        rearm = True
    gap = old is None or msg["ts"] - old["ts"] > stale_sec or old["status"] == "stale"
    history = [] if gap or not msg["door"] or not old["door"] else old["history"][:]
    history.append([msg["ts"], msg["temp"]])
    # 경계 바로 앞 표본 하나를 남겨 1초 주기의 작은 지연에도 60초 창을 확보한다.
    while len(history)>2 and history[1][0] <= msg["ts"]-WINDOW:
        history.pop(0)
    eta, slope, risk = None, 0.0, None
    if msg["door"] and len(history) >= 2:
        # 최근 60초의 최소제곱 직선 기울기(°C/초). 절대 시각을 빼 수치 오차를 줄인다.
        xs = [p[0] - history[0][0] for p in history]
        ys = [p[1] for p in history]
        xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
        denom = sum((x - xbar) ** 2 for x in xs)
        slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom if denom else 0.0
        if slope > 0 and msg["temp"] <= LIMIT:
            eta = max(0.0, (LIMIT - msg["temp"]) / slope)
            if eta <= EARLY_SEC and msg["ts"] - history[0][0] >= WINDOW:
                risk = old.get("risk") if not gap and old["door"] else None
                if risk is None:
                    risk = msg["ts"]
    status = "normal"
    if msg["temp"] > LIMIT:
        status = "hot"
    elif msg["door"] and (rearm or (risk is not None and msg["ts"] - risk >= HOLD_SEC)):
        status = "early"
    # 재개방 즉시 경보는 첫 표본에서도 발생한다. 추세가 없을 때만
    # 시뮬레이터의 알려진 상승 속도로 예측해 null 이벤트를 방지한다.
    if status == "early" and eta is None:
        eta = max(0.0, (LIMIT - msg["temp"]) / SIM_RISE)
    if received - msg["ts"] > stale_sec:
        status, risk, eta = "stale", None, None
    state = {**msg, "received": received, "history": history, "risk": risk,
             "eta": eta, "slope": slope, "status": status, "rearm": rearm}
    return state, old is None or status != old["status"]


