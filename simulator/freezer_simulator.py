"""
아보카도 병원 - 냉동고 웹 시뮬레이터 (Python 3.12 이상).

- 설치: python -m pip install Flask==3.1.3
- 실행: python freezer_simulator.py
- 접속: http://<실행 PC 주소>:<SIM_WEB_PORT>

- 실제 냉동고 대신 온도와 문 상태를 만들어내는 프로그램입니다.
- coldguard_config.py 맨 위에서 주소 / 시설 / 장비 번호를 수정합니다.
- 문을 열면 온도가 상승하고, 닫으면 설정 온도로 돌아갑니다.
- 웹 화면에 냉동고 이미지 / 온도 / 상태 / 로그를 표시합니다.
"""

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory, render_template_string


# 환경변수 우선 / 미설정 시 coldguard_config.py 맨 위 기본값
from common.coldguard_config import values as config_values
_config = config_values("simulator")
HOST = _config['WEB_HOST']
PORT = int(_config['WEB_PORT'])
SITE = _config['SITE']
DEVICE = _config['DEVICE']
LIMIT = -25.0
REARM_TEMP = -29.5  # 이 온도보다 따뜻할 때 닫은 이력은 재개방 경보에 사용한다.
from common.temperature_analysis import SIM_RISE
RISE = SIM_RISE  # 예측 계산과 실제 시뮬레이터 상승 속도를 일치시킨다.
FALL = 0.08
SWING_SEC = 2.0  # 정상 범위에서는 2초에 0.1°C씩 변화한다.
IMG_DIR = Path(__file__).resolve().parent.parent / "image"


def select_device(env=None):
    # 입력 없이 공통 설정에 지정한 시설/장비를 검증한다.
    from common.coldguard_protocol import identity
    cfg = config_values("simulator") if env is None else env
    site, device = cfg.get('SITE', ''), cfg.get('DEVICE', '')
    identity(site, device)
    return site, device


class Freezer:
    # 잠금으로 웹 요청과 1초 주기 측정 간의 상태 충돌을 방지한다.

    def __init__(self, site="site01", device=None):
        self.site = site
        self.device = DEVICE if device is None else device
        self.lock = threading.RLock()
        self.temp = -30.0
        self.target = -30.0
        self.door = False
        self.cooling = True
        self.history = deque()
        self.risk_at = None
        self.early = False
        self.eta = None
        self.stamp = None
        self.rearm = False  # 고온에서 문을 닫은 이력: 이후 재개방은 즉시 경보.
        self.tint = 0.0
        self.logs = deque(maxlen=1000)
        self.last_alarm = "정상 운전"
        self.swing_time = 0.0
        self.swing_dir = -1  # 정상 범위에서 하강/상승 방향 (-1/+1).

    def set_door(self, opened):
        with self.lock:
            if self.door != opened:
                if not opened and self.temp > REARM_TEMP:
                    self.rearm = True
                self.door = opened
                # 이전 문 열림 구간의 추세를 새 구간에 섞지 않는다.
                self.history.clear()
                self.risk_at = None
                self.early = False
                self.eta = None
                if opened and not self.is_hot():
                    # 새 관측이 쌓이기 전에는 시뮬레이터의 순상승 속도로 즉시 계산한다.
                    self.eta = max(0.0, (LIMIT - self.temp) / RISE)
                self.swing_time = 0.0
                self.record("문 조작", "문 열림" if opened else "문 닫힘")
            # 문 개폐는 냉각 장치의 전원을 끄지 않는다.
            self.cooling = True
            self.update_alarm()

    def step(self, now, dt=1.0):
        # 브라우저 접속과 무관하게 서버에서 실제 경과 시간만큼 온도를 바꾼다.
        with self.lock:
            if self.door:
                self.temp = self.temp + RISE * dt
                self.swing_time = 0.0
                self.swing_dir = -1
            elif abs(self.temp - self.target) > 0.5:
                diff = self.target - self.temp
                move = min(abs(diff), FALL * dt)
                self.temp += move if diff > 0 else -move
                self.swing_time = 0.0
                self.swing_dir = -1 if diff < 0 else 1
            else:
                # 랜덤 점프 없이 -30.5 ~ -29.5°C 사이를 0.1°C씩 왕복한다.
                # 측정은 1초마다 유지하고 온도 변경만 2초 간격으로 늦춘다.
                self.swing_time += dt
                while self.swing_time >= SWING_SEC:
                    self.swing_time -= SWING_SEC
                    low, high = self.target - 0.5, self.target + 0.5
                    if self.temp <= low:
                        self.swing_dir = 1
                    elif self.temp >= high:
                        self.swing_dir = -1
                    self.temp = round(min(high, max(low, self.temp + self.swing_dir * 0.1)), 10)
            self.stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            self.check_alarm(now)
            self.update_alarm()
            self.record("측정", self.log_reason())

    def is_hot(self):
        # 화면과 로그에 표시되는 소수 첫째 자리 기준으로 고온을 판정한다.
        return round(self.temp, 1) > LIMIT

    def is_alarm(self):
        # 닫힌 문에서는 이전 경보 이력과 무관하게 실제 고온만 경보로 표시한다.
        return self.is_hot() or (self.door and (self.early or self.rearm))

    def log_reason(self):
        if self.is_hot():
            return "고온 경보"
        return "고온 예측 경보" if self.is_alarm() else "정상 운전"

    def alarm_reason(self):
        if round(self.temp, 1) <= -27.0:
            return "정상적으로 운영 중입니다."
        if self.is_hot():
            return "고온 경보 - 위험 온도 도달"
        return "온도가 위험 수준까지 상승하고 있습니다!" if self.is_alarm() else "정상 운전"

    def update_alarm(self):
        # 색은 과거 최고 온도가 아닌 현재 온도로 결정한다.
        if self.is_alarm() and round(self.temp, 1) > -27.0:
            # 예측 구간의 옅은 배경 농도. 실제 고온은 화면에서 고정 진한 빨강 사용.
            self.tint = min(1.0, max(0.0, (round(self.temp, 1) + 27.0) / 2.0))
        else:
            self.tint = 0.0
        reason = self.log_reason()
        if reason != self.last_alarm:
            self.record("상태 변경", reason)
            self.last_alarm = reason

    def record(self, kind, message):
        # 호출자는 장비 잠금을 보유한다. 로그는 시설별 최근 1,000건만 유지한다.
        self.logs.append(dict(stamp=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                              site=self.site, device=self.device, kind=kind, message=message,
                              temp=round(self.temp, 1), door=self.door))

    def check_alarm(self, now):
        """최근 60초 선형 회귀로 -25도 도달 시간을 예상한다.

        문을 연 뒤 60초의 관측을 확보하고, 예상 시간이 50초 이하인 상태가
        연속 10초 유지될 때만 조기 경보를 켠다. 고온 경보는 실측값으로 판단한다.
        """
        self.eta = None
        if not self.door:
            self.history.clear()
            self.risk_at = None
            self.early = False
            return
        # 재개방으로 60초 이력이 초기화되어도 남은 시간은 계속 표시한다.
        # 충분한 이력이 쌓이면 아래 회귀 계산값으로 대체한다.
        if not self.is_hot():
            self.eta = max(0.0, (LIMIT - self.temp) / RISE)
        # 실측 -25°C 도달 후에도 반올림된 표시값은 잠시 -25.0°C일 수 있다.
        # 이 구간에서 기존 예측 경보를 유지해 고온 경보 전환 사이의 흰색 깜빡임을 막는다.
        if self.early and self.temp >= LIMIT and not self.is_hot():
            self.eta = 0.0
            return
        self.history.append((now, self.temp))
        # 60초 경계 바로 앞 표본 하나를 남겨 주기 오차에도 추세를 계산한다.
        while len(self.history) > 2 and self.history[1][0] <= now - 60:
            self.history.popleft()
        span = now - self.history[0][0]
        if span >= 60 and self.temp < LIMIT:
            xs = [point[0] - now for point in self.history]
            ys = [point[1] for point in self.history]
            x_avg, y_avg = sum(xs) / len(xs), sum(ys) / len(ys)
            denom = sum((x - x_avg) ** 2 for x in xs)
            slope = sum((x - x_avg) * (y - y_avg) for x, y in zip(xs, ys)) / denom
            if slope > 0:
                self.eta = (LIMIT - self.temp) / slope
        if span >= 60 and self.eta is not None and self.eta <= 50:
            if self.risk_at is None:
                self.risk_at = now
            self.early = now - self.risk_at >= 10
        else:
            self.risk_at = None
            self.early = False

    def snapshot(self):
        with self.lock:
            reason = self.alarm_reason()
            return dict(site=self.site, device=self.device, temp=round(self.temp, 1), tint=self.tint,
                        target=self.target, door=self.door, cooling=self.cooling,
                        abnormal=self.is_hot(), alarm=self.is_alarm(), hot=self.is_hot(), reason=reason,
                        eta=round(self.eta) if self.eta is not None else None,
                        stamp=self.stamp)


app = Flask(__name__, static_folder=None)
freezer = Freezer(SITE, DEVICE)
stop = threading.Event()
links = []


def measure():
    # 단일 백그라운드 스레드가 측정한다. 페이지마다 타이머를 만들지 않는다.
    prev = time.monotonic()
    freezer.step(prev, 0)
    while not stop.wait(1):
        now = time.monotonic()
        freezer.step(now, now - prev)
        for link in links:
            try:
                link.sample()
            except Exception as exc:
                # 저장 장애를 표시하고 웹 측정은 유지한다. 저장 실패 자료는 복구할 수 없다.
                print(json.dumps(dict(event="sim_sample_store_failed",
                                                   error=type(exc).__name__)), flush=True)
        prev = now


@app.get("/")
def index():
    freezer = selected()
    return render_template_string(PAGE, site=freezer.site)


def selected():
    # 현재 프로세스에 배정된 시설 한 곳만 제공한다.
    if request.args.get('site', SITE) != SITE:
        abort(404)
    return freezer


@app.get("/image/<name>")
def photo(name):
    # 명시한 이미지 두 개만 제공한다.
    if name not in {"door_open.png", "door_closed.png"}:
        abort(404)
    return send_from_directory(IMG_DIR, name)


@app.get("/api/state")
def state():
    response = jsonify(selected().snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/door")
def door():
    freezer = selected()
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("open")) is not bool:
        return jsonify(error="open에는 true 또는 false가 필요합니다."), 400
    freezer.set_door(data["open"])
    return jsonify(freezer.snapshot())


@app.get("/api/logs")
def logs():
    freezer = selected()
    with freezer.lock:
        response = jsonify(site=freezer.site, logs=list(reversed(freezer.logs)))
    response.headers["Cache-Control"] = "no-store"
    return response


# 원형 버튼과 직사각형 상태판은 이미지 없이 CSS로 그린다.
PAGE = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AVOCADO HOSPITAL</title>
<style>
*{box-sizing:border-box}html,body{width:100%;height:100%;overflow:hidden}
body{margin:0;background:#fff;color:#203326;
font-family:"Malgun Gothic",sans-serif;transition:background 1s linear}
#dashboard{display:flex;flex-direction:column;transform-origin:top left}
body:not(.alarm):not(.hot){transition:none}
body.alarm{background:var(--alarm-bg,#ffe7e7)}
body.hot{background:#ff8585;transition:none}
header{padding:14px 4.5%;border-bottom:1px solid #cad4c5;display:flex;align-items:baseline;gap:20px;white-space:nowrap}
header strong{font-size:24px;letter-spacing:2px}header p{margin:0;color:#65735f;font-weight:400}
/* 좌우 열 너비를 같게 두어 냉동고와 온도판을 화면 정중앙에 맞춘다. */
main{width:100%;flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,480px) minmax(0,1fr);
gap:38px;align-items:center;padding:50px 4.5% 24px}.controls{display:flex;flex-direction:column;gap:40px;align-items:flex-start}
.fridge{height:100%;min-height:0;display:flex;flex-direction:column;position:relative}
.info{width:380px;max-width:100%;min-height:0;justify-self:end;align-self:stretch;display:flex;flex-direction:column}
.switch{display:block;align-self:flex-end;margin-top:auto;width:250px;max-width:100%;padding:18px 12px;
border:3px solid #4189ab;background:#b9e5fa;color:#203326;text-align:center;text-decoration:none;font-size:24px;font-weight:700}
.switch:focus-visible{outline:3px solid #254bd1;outline-offset:4px}
.door{width:155px;height:155px;border-radius:50%;border:3px solid #416b36;background:#aab7a6;
color:#263d21;font-size:22px;font-weight:bold;cursor:pointer;box-shadow:0 5px 0 #bcc8b6}
.door.active{background:#b9ec87;box-shadow:0 0 0 8px #e2f0d6,0 5px 0 #98af87}
.door:disabled{cursor:default;opacity:.6}.door:focus-visible{outline:4px solid #254bd1;outline-offset:9px}
.screen{background:#111814;color:white;border:5px solid #39423b;padding:16px;text-align:center}
#temp{font: bold 50px monospace}#target{margin-top:6px;color:#ff7777;font-size:19px}
.screen{flex-shrink:0}
.fridge img{display:block;width:100%;height:0;min-height:0;flex:1;object-fit:contain;object-position:center bottom;margin-top:14px}
.box{border:3px solid #45563f;background:#fff;padding:23px 18px;margin-bottom:20px;border-radius:0}
.site{font-size:30px;text-align:center;font-weight:bold}.status{font-size:23px;font-weight:bold}
.warning{background:#fff2bf;border-color:#c5a33c}
.bad{background:#ff7373;border-color:#b43434}
.detail{font-size:15px;line-height:1.9;margin-top:16px;overflow-wrap:anywhere}
#reason{margin-top:14px;line-height:1.6;font-weight:700}#error{color:#a11616;font-size:14px;min-height:22px}
#error{position:absolute;bottom:0;margin:0;background:white}#error:empty{display:none}
#eta{color:#c01919;font-weight:700;margin-top:10px}
#eta:empty{margin-top:0}
#statusBox{cursor:pointer}#statusBox:focus-visible{outline:3px solid #254bd1;outline-offset:4px}
.log-hint{margin:12px 0 0;font-size:13px;color:#364a3b}
/* 팝업은 축소되는 dashboard 밖에 두고 항상 흰 배경을 사용한다. */
#logModal{width:min(1000px,90vw);max-height:80vh;padding:24px;border:1px solid #b9c3ba;
background:#fff;color:#203326;box-shadow:0 18px 70px #0005}
#logModal::backdrop{background:#0006}
.log-head{display:flex;align-items:center;justify-content:space-between;gap:16px}
.log-head h2{margin:0;font-size:24px}.log-head button{font-size:26px;border:0;background:white;cursor:pointer;padding:4px 12px}
.log-scroll{max-height:55vh;overflow:auto}#logModal table{width:100%;border-collapse:collapse;font-size:14px}
#logModal th,#logModal td{padding:10px;text-align:left;border-bottom:1px solid #ddd;overflow-wrap:anywhere}
#logModal th{position:sticky;top:0;background:#f0f4f0}#logNote{font-size:14px}
.log-tools{display:flex;justify-content:flex-end;margin:16px 0;gap:8px;align-items:center}
.log-tools select,.log-tools input,.log-tools button{font:inherit;font-size:14px;padding:8px;border:1px solid #a8b6ab;background:white;color:#203326;border-radius:3px}
#logValue{display:flex;gap:6px;align-items:center;min-width:0}
#logValue input[type=number]{width:110px}#logValue select{min-width:180px}
.log-tools button{background:#b9e5fa;font-weight:700;cursor:pointer}
@media(max-width:600px){.log-tools{flex-wrap:wrap}#logValue input[type=number]{width:85px}}
</style>
</head><body>
<div id="dashboard">
<header><strong>AVOCADO HOSPITAL</strong><p>가상 냉동고/현장 시뮬레이터</p></header>
<main>
<section class="controls" aria-label="문 조작">
<button class="door" id="open" disabled>Door<br>Open</button>
<button class="door" id="close" disabled>Door<br>Closed</button>
</section>
<section class="fridge" aria-label="냉동고">
<div class="screen"><div id="temp">--.- °C</div><div id="target">설정 --.- °C</div></div>
<img id="photo" src="/image/door_closed.png" alt="냉동고 문 닫힘">
<p id="error" role="alert"></p>
</section>
<aside class="info">
<div class="box site" id="site">시설 {{ site }} 현장</div>
<div class="box" id="statusBox" role="button" tabindex="0" aria-haspopup="dialog" aria-controls="logModal" aria-label="시설 로그 보기"><div class="status" id="status" role="status">상태 확인 중</div>
<div id="reason"></div><div class="detail">
<div id="device"></div><div id="doorText"></div><div id="cooling"></div>
<div id="stamp">마지막 측정 : -</div><div id="eta"></div>
</div><p class="log-hint">클릭하면 로그를 확인할 수 있습니다.</p></div>

</aside>
</main></div>
<dialog id="logModal" aria-labelledby="logTitle">
<div class="log-head"><h2 id="logTitle">시설 {{ site }} 로그</h2><button id="logClose" aria-label="로그 닫기" autofocus>×</button></div>
<form class="log-tools" id="logSearch">
<select id="logField" aria-label="검색 항목"><option value="stamp">시각</option><option value="kind">종류</option><option value="temp">온도</option><option value="door">문</option><option value="message">내용</option></select>
<div id="logValue"></div><button type="submit" id="logSubmit">검색</button>
</form>
<p id="logNote" role="status">로그를 불러오는 중입니다.</p>
<div class="log-scroll"><table><thead><tr><th>시각</th><th>종류</th><th>온도</th><th>문</th><th>내용</th></tr></thead><tbody id="logRows"></tbody></table></div>
</dialog>
<script>
const el = id => document.getElementById(id);
const site = {{ site|tojson }};
// 작은 창에서도 요소를 잘라 숨기지 않고 전체 배치를 같은 비율로 축소한다.
function fitScreen(){
  const scale = Math.min(1, innerWidth/1440, innerHeight/760);
  const panel = el('dashboard');
  panel.style.width = innerWidth/scale + 'px';
  panel.style.height = innerHeight/scale + 'px';
  panel.style.transform = 'scale(' + scale + ')';
}
addEventListener('resize', fitScreen);
fitScreen();
// 서버의 경보 강도에 따라 배경색을 보간한다. 경보 판정 조건은 바꾸지 않는다.
function alarmColor(tint){
  const mix = (start, end) => 'rgb(' + start.map((n,i) => Math.round(n+(end[i]-n)*tint)).join(',') + ')';
  document.body.style.setProperty('--alarm-bg', mix([255,242,242],[255,213,213]));
}
let busy = false, last = null, revision = 0;
// 화면은 서버의 상태를 표시만 한다. 온도를 브라우저에서 따로 계산하지 않는다.
function render(s){
  last = s;
  el('temp').textContent = s.temp.toFixed(1) + ' °C';
  el('target').textContent = '설정 ' + s.target.toFixed(1) + ' °C';
  el('site').textContent = '시설 ' + s.site + ' 현장';
  el('device').textContent = '장비 : ' + s.device;
  el('doorText').textContent = '문 : ' + (s.door ? '열림' : '닫힘');
  el('cooling').textContent = '냉각 : ' + (s.cooling ? '가동' : '대기');
  el('stamp').textContent = '마지막 측정 : ' + (s.stamp ? new Date(s.stamp).toLocaleString('ko-KR', {timeZone:'Asia/Seoul'}) : '-');
  const stale = !s.stamp || Date.now() - Date.parse(s.stamp) > 5000;
  el('status').textContent = stale ? '데이터 확인 불가' : 'State : ' + (s.abnormal ? '비정상' : '정상');
  el('reason').textContent = stale ? '측정값 갱신이 중단되었습니다.' : s.reason;
  el('eta').textContent = s.eta === null ? '' : '위험 온도까지 예상 : ' + s.eta + '초';
  const warm = !stale && s.temp > -27 && s.alarm;
  document.body.classList.toggle('alarm', warm);
  document.body.classList.toggle('hot', s.hot && !stale);
  alarmColor(stale ? 0 : s.tint);
  el('statusBox').classList.toggle('warning', warm && !s.hot);
  el('statusBox').classList.toggle('bad', s.hot && !stale);
  el('photo').src = '/image/' + (s.door ? 'door_open.png' : 'door_closed.png');
  el('photo').alt = '냉동고 문 ' + (s.door ? '열림' : '닫힘');
  el('open').classList.toggle('active', !s.door);
  el('close').classList.toggle('active', s.door);
  el('open').disabled = busy || stale || s.door;
  el('close').disabled = busy || stale || !s.door;
}
function offline(){
  document.body.classList.remove('hot');
  el('statusBox').classList.remove('warning','bad');
  alarmColor(0);
  el('status').textContent = '데이터 확인 불가';
  el('reason').textContent = '서버 연결을 확인하세요. 마지막 측정값을 표시합니다.';
  document.body.classList.remove('alarm');
  el('open').disabled = el('close').disabled = true;
}
async function api(path, options = {}){
  const res = await fetch(path + '?site=' + encodeURIComponent(site), {...options, cache:'no-store', signal:AbortSignal.timeout(4000)});
  if(!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}
async function setDoor(open){
  busy = true;revision++;if(last) render(last);
  try{const s = await api('/api/door', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({open})});
    busy = false;render(s);
  }catch(err){busy = false;offline();}
}
el('open').onclick = () => setDoor(true);
el('close').onclick = () => setDoor(false);
// showModal은 배경 조작을 막고 팝업 내부로 키보드 초점을 이동한다.
let logData = [];
function searchControl(){
  const field = el('logField').value;
  const box = el('logValue');box.replaceChildren();
  if(field==='stamp'){
    const input=document.createElement('input');input.type='datetime-local';input.id='logInput';
    input.step='60';input.setAttribute('aria-label','검색 시각 (해당 분)');box.append(input);
  }else if(field==='temp'){
    for(const [id,label] of [['logMin','최저 온도'],['logMax','최고 온도']]){
      const input=document.createElement('input');input.type='number';input.step='any';input.id=id;
      input.placeholder=label;input.setAttribute('aria-label',label+' (°C)');
      if(id==='logMax') box.append(document.createTextNode('~'));
      box.append(input);
    }
  }else{
    const select=document.createElement('select');select.id='logInput';select.setAttribute('aria-label','검색 조건');
    const options=field==='kind'?['측정','문 조작','상태 변경']:field==='door'?['열림','닫힘']:['정상 운전','고온 예측 경보','고온 경보','문 열림','문 닫힘'];
    select.add(new Option('전체',''));
    for(const text of options) select.add(new Option(text,text));
    box.append(select);
  }
}
// 시각은 브라우저 현지 시간의 해당 1분, 온도는 양 끝을 포함한 범위로 검색한다.
function filterLogs(data, field, value, low, high){
  if(field==='temp'){
    const min=low===''?-Infinity:Number(low), max=high===''?Infinity:Number(high);
    if(Number.isNaN(min)||Number.isNaN(max)||min>max) throw new Error('최저 온도는 최고 온도보다 작거나 같아야 합니다.');
    return data.filter(log=>log.temp>=min && log.temp<=max);
  }
  if(!value) return data;
  if(field==='stamp'){
    const start=new Date(value).getTime();
    if(!Number.isFinite(start)) throw new Error('날짜와 시간을 확인하세요.');
    return data.filter(log=>{const t=Date.parse(log.stamp);return t>=start && t<start+60000;});
  }
  return data.filter(log=>(field==='door'?(log.door?'열림':'닫힘'):log[field])===value);
}
function drawLogs(data){
  const rows=document.createDocumentFragment();
  for(const log of data){
    const row=document.createElement('tr');
    for(const value of [new Date(log.stamp).toLocaleString('ko-KR', {timeZone:'Asia/Seoul'}),log.kind,log.temp.toFixed(1)+' °C',log.door?'열림':'닫힘',log.message]){
      const cell=document.createElement('td');cell.textContent=value;row.append(cell);
    }
    rows.append(row);
  }
  el('logRows').replaceChildren(rows);
  el('logNote').textContent='검색 결과 '+data.length+'건 / 최근 '+logData.length+'건 - 서버 재시작 시 삭제됩니다.';
  el('logRows').closest('.log-scroll').scrollTop=0;
}
el('logField').onchange=searchControl;
el('logSearch').onsubmit=event=>{
  event.preventDefault();
  try{
    const field=el('logField').value;
    drawLogs(filterLogs(logData,field,el('logInput')?.value??'',el('logMin')?.value??'',el('logMax')?.value??''));
  }catch(err){el('logNote').textContent=err.message;}
};
searchControl();
async function showLogs(){
  const modal = el('logModal');
  if(modal.open) return;
  modal.showModal();
  logData=[];el('logSubmit').disabled=true;
  el('logField').value='stamp';searchControl();
  el('logRows').replaceChildren();
  el('logNote').textContent = '로그를 불러오는 중입니다.';
  try{
    const data = await api('/api/logs');
    logData=data.logs;drawLogs(logData);el('logSubmit').disabled=false;
  }catch(err){el('logNote').textContent = '로그를 불러오지 못했습니다. 닫은 뒤 다시 시도하세요.';}
}
el('statusBox').onclick = showLogs;
el('statusBox').onkeydown = event => {
  if(event.key==='Enter' || event.key===' '){event.preventDefault();showLogs();}
};
el('logClose').onclick = () => el('logModal').close();
el('logModal').addEventListener('close', () => el('statusBox').focus());
el('photo').onerror = () => {el('error').textContent = 'image 폴더의 door_open.png / door_closed.png를 확인하세요.';};
el('photo').onload = () => {el('error').textContent = '';};
async function refresh(){
  // 문 조작 도중의 이전 조회 결과가 최신 화면을 덮어쓰지 않도록 한다.
  const seen = revision;
  if(!busy){try{const s=await api('/api/state');if(!busy && seen===revision) render(s);}catch(err){if(!busy && seen===revision) offline();}}
  setTimeout(refresh,1000);
}
refresh();
</script></body></html>'''


if __name__ == "__main__":
    try:
        SITE, DEVICE = select_device()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    freezer = Freezer(SITE, DEVICE)
    print(f'실행 장비 : {SITE} / {DEVICE}')
    for name in ("door_open.png", "door_closed.png"):
        if not (IMG_DIR / name).is_file():
            raise SystemExit(f"이미지가 없습니다: {IMG_DIR / name}")
    from simulator.simulator_mqtt import all_settings, Link
    configs = all_settings()
    worker = None
    try:
        # 설정한 시설/장비 한 대를 MQTT에 연결한다.
        for cfg in configs:
            links.append(Link(cfg, freezer))
        for link in links:
            link.start()
        worker = threading.Thread(target=measure, name="freezer-clock", daemon=True)
        worker.start()
        # 재로더를 끄므로 측정 스레드가 두 번 시작되지 않는다.
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    finally:
        stop.set()
        if worker:
            worker.join(timeout=2)
        for link in links:
            link.close()
