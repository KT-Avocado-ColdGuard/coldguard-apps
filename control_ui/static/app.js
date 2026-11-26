'use strict';
/*
 * 중앙 통제 화면 (KT 허브)
 * - 로그인 → 연결 상태·냉동고 상태·명령 이력 조회(15초마다) → 설정 온도 명령과 결과 확인(2초마다, 최대 10분).
 * - 통제 API 키는 서버에만 있다. 화면은 로그인 쿠키(HttpOnly)와 CSRF 토큰으로 이 서버만 부른다.
 * - 서버에서 받은 값은 모두 textContent로 넣는다(HTML로 해석하지 않는다).
 * - 명령을 자동으로 다시 보내지 않는다. 실패하면 알리고, 다시 보낼지는 사람이 정한다.
 * - 자동 갱신 요청은 사용 시간에 넣지 않는다. 클릭·키 입력이 있었을 때만 다음 요청에 X-User-Active를 붙인다.
 * - ?device=fz03 이 붙으면 그 장비를 미리 고른다.
 */
(function () {
  const REFRESH_MS = 15000;               // 연결 상태·냉동고·명령 이력 갱신 간격
  const TRACK_MS = 2000;                  // 보낸 명령 결과 확인 간격
  const TRACK_LIMIT_MS = 10 * 60 * 1000;  // 시설 단절(시나리오 2)에도 결과를 받도록 10분까지 확인
  const MIN_TEMP = -50;                   // 현장 controller 허용 범위: -50 이상
  const MAX_TEMP = -10;                   //                           -10 미만
  const DEVICE_RE = /^fz[0-9]{1,18}$/;
  const VALUE_RE = /^-?[0-9]{1,3}(\.[0-9]{1,2})?$/; // 통제 API 형식: 소수 둘째 자리까지, 절댓값 999.99 이하

  const STATUS_SHORT = {pending: '응답 대기', accepted: '적용 완료', rejected: '거부', no_response: '응답 없음'};
  const REASON_TEXT = {
    locked: '현장에서 잠금 중이라 거부됨',
    out_of_range: '허용 온도 범위 밖이라 거부됨',
    duplicate: '같은 명령이 이미 처리됨',
    unknown_device: '등록되지 않은 장비',
    apply_timeout: '장비 적용 시간 초과',
  };
  const ERROR_TEXT = {
    mqtt_unavailable: '중앙 통신 끊김, 명령이 나가지 않음',
    db_unavailable: '중앙 DB 오류, 명령이 나가지 않음',
    publish_unconfirmed: '전달 확인 실패 → 결과를 계속 조회합니다 (다시 보내지 마세요)',
    upstream_auth: '권한 오류 → 관리자에게 문의 (키 설정 문제)',
    api_unreachable: '통제 API에 연결하지 못했습니다.',
    api_no_response: '통제 API 응답이 없어 전송 여부를 알 수 없습니다. 명령 이력에서 확인한 뒤 다시 보낼지 정하세요.',
    api_error: '통제 API 응답 오류',
    csrf_failed: '보안 토큰이 맞지 않아 처리하지 않았습니다. 화면 정보를 새로 받았으니 다시 시도하세요.',
    cross_site: '다른 사이트에서 보낸 요청이라 거부했습니다.',
    not_found: '찾을 수 없습니다.',
  };

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(window.location.search);
  const timeFmt = new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const tempFmt = new Intl.NumberFormat('ko-KR', {minimumFractionDigits: 1, maximumFractionDigits: 2});

  const state = {
    user: null, csrf: null, cmdLimit: 10,
    status: undefined, devices: [], selected: null,
    wanted: DEVICE_RE.test(params.get('device') || '') ? params.get('device') : null,
    tracked: new Map(), pending: null, timers: [], busy: {},
  };
  let activity = false; // 마지막 요청 뒤 사람이 화면을 쓴 적이 있는지

  // ---------- 서버 호출 ----------

  class Failure extends Error {
    constructor(status, body) {
      super((body && body.error) || 'http_' + status);
      this.status = status;
      this.body = body || {};
    }
  }

  async function call(path, options) {
    const opts = options || {};
    const method = opts.method || 'GET';
    const headers = {'Accept': 'application/json'};
    if (opts.active || activity) {
      headers['X-User-Active'] = '1';
      activity = false;
    }
    if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf || '';
    const init = {method, headers, cache: 'no-store', credentials: 'same-origin'};
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (e) {
      throw new Failure(0, {error: 'network'});
    }
    let body = null;
    try { body = await response.json(); } catch (e) { body = null; }
    if (!response.ok) throw new Failure(response.status, body);
    return body;
  }

  // 로그인이 끝났으면(401) 로그인 화면으로 돌아간다.
  function lost(err) {
    if (!(err instanceof Failure) || err.status !== 401) return false;
    if (!state.user) return true; // 함께 보낸 다른 요청이 이미 로그인 화면으로 돌렸다
    signedOut(err.body.error === 'session_expired'
      ? '2시간 동안 사용하지 않았거나 로그인한 지 하루가 지나 로그아웃했습니다. 다시 로그인하세요.'
      : '로그인이 필요합니다.');
    return true;
  }

  function describe(err) {
    if (!(err instanceof Failure)) return '화면 오류가 났습니다. 새로고침해 주세요.';
    const b = err.body;
    if (err.status === 0) return '서버에 연결하지 못했습니다. 테일넷 연결을 확인하세요.';
    if (b.error === 'rate_limited') {
      return '명령은 1분에 ' + state.cmdLimit + '건까지 보낼 수 있습니다. ' + (b.retry_after || 60) + '초 뒤 다시 시도하세요.';
    }
    if (b.error === 'api_unreachable' && b.sent === false) return '통제 API에 연결하지 못해 명령이 나가지 않았습니다.';
    if (ERROR_TEXT[b.error]) return ERROR_TEXT[b.error];
    if ([400, 413, 415].includes(err.status)) return '입력 형식 오류' + (b.detail ? ' (' + b.detail + ')' : '');
    return '요청이 거부되었습니다(' + (b.error || err.status) + ').';
  }

  // 같은 조회가 겹쳐 돌지 않게 한다(응답이 늦을 때 요청이 쌓이지 않게).
  async function once(name, fn) {
    if (state.busy[name]) return;
    state.busy[name] = true;
    try { await fn(); } finally { state.busy[name] = false; }
  }

  // ---------- 표시 도우미 ----------

  function text(node, value, cls) {
    node.textContent = value;
    node.className = cls || '';
  }

  function cell(row, value, cls) {
    const td = document.createElement('td');
    if (value instanceof Node) td.appendChild(value);
    else td.textContent = value;
    if (cls) td.className = cls;
    row.appendChild(td);
    return td;
  }

  function badge(label, cls) {
    const span = document.createElement('span');
    span.className = 'badge ' + cls;
    span.textContent = label;
    return span;
  }

  function note(value) {
    const span = document.createElement('span');
    span.className = 'note';
    span.textContent = value;
    return span;
  }

  function when(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '-' : timeFmt.format(d);
  }

  function temp(value) {
    return typeof value === 'number' ? tempFmt.format(value) + ' ℃' : '-';
  }

  function ago(sec) {
    if (typeof sec !== 'number' || sec < 0) return '';
    if (sec < 60) return Math.floor(sec) + '초 전';
    if (sec < 3600) return Math.floor(sec / 60) + '분 전';
    return Math.floor(sec / 3600) + '시간 전';
  }

  function shortId(cmdId) {
    return String(cmdId).slice(0, 8);
  }

  function keyOf(d) {
    return d.site_id + '/' + d.device_id;
  }

  function multiSite() {
    return new Set(state.devices.map((d) => d.site_id)).size > 1;
  }

  function deviceName(d) {
    return multiSite() ? d.site_id + ' ' + d.device_id : d.device_id;
  }

  // 거부 사유 문구(요구사항 표). 만료는 명령의 실제 유효시간을 넣는다.
  function reasonText(cmd) {
    if (cmd.reason === 'expired') {
      const ttl = Math.round((Date.parse(cmd.expires_at) - Date.parse(cmd.issued_at)) / 1000);
      return '명령 유효시간(' + (Number.isFinite(ttl) && ttl > 0 ? ttl : 60) + '초)이 지나 거부됨';
    }
    return REASON_TEXT[cmd.reason] || ('거부됨(' + (cmd.reason || '사유 없음') + ')');
  }

  // ---------- 로그인 ----------

  function showLogin(message) {
    $('console').hidden = true;
    $('who').hidden = true;
    $('login').hidden = false;
    text($('login-msg'), message || '', 'msg error');
    $('login-pass').value = '';
    $('login-user').focus();
  }

  function signedOut(message) {
    stopTimers();
    state.user = null;
    state.csrf = null;
    state.status = undefined;
    state.devices = [];
    state.selected = null;
    state.tracked.clear();
    hideConfirm();
    $('device-rows').replaceChildren();
    $('command-rows').replaceChildren();
    text($('cmd-msg'), '', 'msg');
    $('cmd-value').value = '';
    showLogin(message);
  }

  async function login(event) {
    event.preventDefault();
    const user = $('login-user').value.trim();
    const pass = $('login-pass').value;
    if (!user || !pass) {
      text($('login-msg'), '아이디와 비밀번호를 넣어 주세요.', 'msg error');
      return;
    }
    $('login-submit').disabled = true;
    try {
      startConsole(await call('/api/login', {method: 'POST', body: {username: user, password: pass}, active: true}));
    } catch (err) {
      const b = err instanceof Failure ? err.body : {};
      let message = '로그인하지 못했습니다. ' + describe(err);
      if (err.status === 401) message = '아이디 또는 비밀번호가 맞지 않습니다.';
      if (err.status === 429 && b.error === 'locked') {
        message = '로그인에 5번 실패해 잠겼습니다. ' + Math.max(1, Math.ceil((b.retry_after || 300) / 60)) + '분 뒤 다시 시도하세요.';
      }
      text($('login-msg'), message, 'msg error');
    } finally {
      $('login-pass').value = '';
      $('login-submit').disabled = false;
    }
  }

  async function logout() {
    try {
      await call('/api/logout', {method: 'POST', active: true});
    } catch (err) {
      // 이미 끝난 세션이어도 로그인 화면으로 돌아간다.
    }
    signedOut('로그아웃했습니다.');
  }

  function applySession(s) {
    state.user = s.user;
    state.csrf = s.csrf;
    state.cmdLimit = s.cmd_limit || 10;
    $('who-name').textContent = s.user;
  }

  function startConsole(s) {
    applySession(s);
    $('login').hidden = true;
    $('console').hidden = false;
    $('who').hidden = false;
    text($('login-msg'), '', 'msg error');
    renderStatus();
    refreshAll();
    startTimers();
  }

  // CSRF 토큰이 맞지 않으면(다른 탭에서 다시 로그인한 경우 등) 세션 정보를 새로 받는다.
  async function reloadSession() {
    try {
      const s = await call('/api/session', {active: true});
      if (s.user !== state.user) {
        state.tracked.clear();
        startConsole(s);
      } else {
        applySession(s);
      }
    } catch (err) {
      lost(err);
    }
  }

  function startTimers() {
    stopTimers();
    state.timers.push(window.setInterval(() => { if (!document.hidden) refreshAll(); }, REFRESH_MS));
    state.timers.push(window.setInterval(() => { if (!document.hidden) trackCommands(); }, TRACK_MS));
  }

  function stopTimers() {
    state.timers.forEach((t) => window.clearInterval(t));
    state.timers = [];
  }

  function refreshAll() {
    loadStatus();
    loadDevices();
    loadCommands();
  }

  // ---------- 연결 상태 ----------

  function loadStatus() {
    return once('status', async () => {
      const owner = state.user;
      if (!owner) return;
      let st;
      try {
        st = await call('/api/status');
      } catch (err) {
        if (lost(err)) return;
        st = null;
      }
      if (state.user !== owner) return; // 기다리는 사이 로그아웃했으면 그리지 않는다
      state.status = st;
      renderStatus();
      $('status-updated').textContent = '갱신 ' + timeFmt.format(new Date());
    });
  }

  function haltReason(st) {
    if (st === undefined) return '';
    if (st === null) return '원격 제어 중단: 연결 상태를 확인하지 못했습니다.';
    if (!st.api) return '원격 제어 중단: 통제 API에 연결할 수 없습니다.';
    if (!st.mqtt) return '원격 제어 중단: 중앙 MQTT 연결이 끊겨 명령을 보낼 수 없습니다.';
    if (!st.db) return '원격 제어 중단: 중앙 DB 오류로 명령을 기록할 수 없습니다.';
    if (st.key === false) return '원격 제어 중단: 통제 API 키 설정 오류입니다. 관리자에게 문의하세요.';
    if (!st.can_command) return '원격 제어 중단: 통제 API 상태를 확인하지 못했습니다.';
    return '';
  }

  function renderStatus() {
    const st = state.status;
    const known = st !== undefined;
    const up = !!(st && st.api);
    text($('conn-api'), !known ? '확인 중' : up ? '연결' : '끊김', 'badge ' + (!known ? 'none' : up ? 'ok' : 'bad'));
    text($('conn-db'), !known ? '확인 중' : !up ? '-' : st.db ? '정상' : '오류', 'badge ' + (!up ? 'none' : st.db ? 'ok' : 'bad'));
    text($('conn-mqtt'), !known ? '확인 중' : !up ? '-' : st.mqtt ? '연결' : '끊김', 'badge ' + (!up ? 'none' : st.mqtt ? 'ok' : 'bad'));
    const reason = haltReason(st);
    $('halt').textContent = reason;
    $('halt').hidden = !reason;
    const allowed = !!(st && st.can_command);
    ['cmd-device', 'cmd-value', 'cmd-review', 'confirm-send'].forEach((id) => { $(id).disabled = !allowed; });
  }

  // ---------- 냉동고 상태 ----------

  function loadDevices() {
    return once('devices', async () => {
      const owner = state.user;
      if (!owner) return;
      try {
        const data = await call('/api/devices');
        if (state.user !== owner) return;
        state.devices = Array.isArray(data.devices) ? data.devices : [];
        if (state.wanted) {
          const found = state.devices.find((d) => d.device_id === state.wanted);
          if (found) {
            state.selected = keyOf(found);
            state.wanted = null;
          }
        }
        if (!state.selected && state.devices.length) state.selected = keyOf(state.devices[0]);
        renderDevices();
        renderDeviceOptions();
        text($('devices-msg'), state.devices.length ? '' : '아직 받은 측정값이 없습니다.', 'msg');
        $('devices-updated').textContent = '갱신 ' + timeFmt.format(new Date());
      } catch (err) {
        if (lost(err)) return;
        text($('devices-msg'), '냉동고 상태를 불러오지 못했습니다. ' + describe(err), 'msg error');
      }
    });
  }

  function renderDevices() {
    const body = $('device-rows');
    body.replaceChildren();
    state.devices.forEach((d) => {
      const row = document.createElement('tr');
      const key = keyOf(d);
      if (key === state.selected) row.classList.add('selected');
      if (d.state === 'unknown' || d.state === 'abnormal') row.classList.add(d.state);

      const pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'device-pick';
      pick.dataset.key = key;
      pick.textContent = deviceName(d);
      pick.title = '이 장비를 명령 대상으로 고릅니다';
      cell(row, pick);
      const stateCell = document.createElement('span');
      stateCell.appendChild(badge(d.state_label || '확인 불가', d.state || 'unknown'));
      stateCell.appendChild(note(d.state_detail || ''));
      cell(row, stateCell);
      cell(row, temp(d.temp_c), 'num');
      cell(row, temp(d.setpoint_c), 'num');
      cell(row, d.door_open === true ? '열림' : d.door_open === false ? '닫힘' : '-');
      cell(row, d.cooling === true ? '가동' : d.cooling === false ? '대기' : '-');
      cell(row, when(d.measured_at) + (typeof d.age_sec === 'number' ? ' (' + ago(d.age_sec) + ')' : ''));
      if (typeof d.grafana_url === 'string' && /^https?:\/\//.test(d.grafana_url)) {
        const link = document.createElement('a');
        link.href = d.grafana_url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.className = 'grafana';
        link.textContent = 'Grafana에서 보기';
        cell(row, link);
      } else {
        cell(row, '-');
      }
      body.appendChild(row);
    });
  }

  function renderDeviceOptions() {
    const select = $('cmd-device');
    const keys = state.devices.map(keyOf);
    // 목록이 그대로면 다시 만들지 않는다(15초마다 갱신할 때 열어 둔 선택 상자가 닫히지 않게).
    if (!(keys.length && keys.join(',') === Array.from(select.options, (o) => o.value).join(','))) {
      select.replaceChildren();
      if (!keys.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '장비 없음';
        select.appendChild(option);
      }
      state.devices.forEach((d) => {
        const option = document.createElement('option');
        option.value = keyOf(d);
        option.textContent = deviceName(d);
        select.appendChild(option);
      });
    }
    if (keys.includes(state.selected)) select.value = state.selected;
  }

  function selected() {
    return state.devices.find((d) => keyOf(d) === state.selected) || null;
  }

  function selectDevice(key) {
    if (!state.devices.some((d) => keyOf(d) === key)) return;
    state.selected = key;
    $('cmd-device').value = key;
    hideConfirm();
    renderDevices();
    if ($('only-selected').checked) loadCommands();
  }

  // ---------- 명령 ----------

  function hideConfirm() {
    state.pending = null;
    $('confirm').hidden = true;
  }

  function review(event) {
    event.preventDefault();
    hideConfirm();
    const d = selected();
    const raw = $('cmd-value').value.trim();
    if (!(state.status && state.status.can_command)) {
      text($('cmd-msg'), '원격 제어 중단 중에는 명령을 보낼 수 없습니다.', 'msg error');
      return;
    }
    if (!d) {
      text($('cmd-msg'), '명령을 보낼 장비를 고르세요.', 'msg error');
      return;
    }
    if (!VALUE_RE.test(raw)) {
      text($('cmd-msg'), '입력 형식 오류: 소수 둘째 자리까지의 숫자로 넣으세요(예: -22.0).', 'msg error');
      return;
    }
    const value = Number(raw);
    state.pending = {site: d.site_id, device: d.device_id, value};
    $('confirm-text').textContent = deviceName(d) + ' 설정 온도 '
      + (typeof d.setpoint_c === 'number' ? tempFmt.format(d.setpoint_c) : '(현재 값 모름)')
      + ' → ' + tempFmt.format(value) + '℃로 바꿉니다';
    const warnings = [];
    const outOfRange = !(value >= MIN_TEMP && value < MAX_TEMP);
    if (outOfRange) warnings.push('허용 범위(-50 이상 -10 미만) 밖의 값이라 현장 controller가 거부합니다(out_of_range). 거부 시험이 아니면 취소하세요.');
    if (d.state === 'unknown') warnings.push('이 장비는 측정이 끊겨(확인 불가) 명령이 현장에 바로 닿지 않을 수 있습니다. 결과는 최대 10분까지 확인합니다.');
    $('confirm-warn').textContent = warnings.join(' ');
    $('confirm-warn').hidden = !warnings.length;
    $('confirm').classList.toggle('danger', outOfRange);
    text($('cmd-msg'), '', 'msg');
    $('confirm').hidden = false;
    $('confirm-send').focus();
  }

  async function send() {
    const p = state.pending;
    if (!p) return;
    $('confirm-send').disabled = true;
    $('confirm-cancel').disabled = true;
    const label = p.device + ' ' + tempFmt.format(p.value) + '℃';
    try {
      const cmd = await call('/api/commands', {method: 'POST', body: {site_id: p.site, device_id: p.device, value: p.value}, active: true});
      track(cmd.cmd_id, label);
      text($('cmd-msg'), label + ' 명령을 보냈습니다. 현장 응답 대기 중 (명령 ' + shortId(cmd.cmd_id) + ')', 'msg ok');
    } catch (err) {
      if (lost(err)) return;
      const b = err instanceof Failure ? err.body : {};
      if (b.error === 'publish_unconfirmed' && b.cmd_id) {
        // 기록은 됐고 전달 여부만 모른다. 같은 명령 ID로 결과를 계속 확인한다.
        track(b.cmd_id, label);
        text($('cmd-msg'), label + ': ' + ERROR_TEXT.publish_unconfirmed + ' (명령 ' + shortId(b.cmd_id) + ')', 'msg warn');
      } else {
        if (b.error === 'csrf_failed') await reloadSession();
        text($('cmd-msg'), label + ' 명령을 보내지 못했습니다: ' + describe(err), 'msg error');
      }
    } finally {
      $('confirm-cancel').disabled = false;
      hideConfirm();
      renderStatus();
      loadCommands();
    }
  }

  function track(cmdId, label) {
    if (cmdId) state.tracked.set(cmdId, {label, since: Date.now(), status: 'pending'});
  }

  function trackCommands() {
    return once('track', async () => {
      let changed = false;
      const owner = state.user;
      for (const [cmdId, t] of Array.from(state.tracked)) {
        if (!owner || state.user !== owner) return;
        if (Date.now() - t.since > TRACK_LIMIT_MS) {
          state.tracked.delete(cmdId);
          text($('cmd-msg'), t.label + ' 명령 결과를 10분 동안 받지 못했습니다. 명령 이력에서 계속 확인할 수 있습니다.', 'msg warn');
          continue;
        }
        let cmd;
        try {
          cmd = await call('/api/commands/' + encodeURIComponent(cmdId));
        } catch (err) {
          if (lost(err)) return;
          continue; // 다음 주기에 다시 확인한다
        }
        if (state.user !== owner) return;
        if (cmd.status === t.status) continue;
        t.status = cmd.status;
        changed = true;
        if (cmd.status === 'accepted') {
          text($('cmd-msg'), t.label + ': 적용 완료', 'msg ok');
          state.tracked.delete(cmdId);
        } else if (cmd.status === 'rejected') {
          text($('cmd-msg'), t.label + ': ' + reasonText(cmd), 'msg error');
          state.tracked.delete(cmdId);
        } else if (cmd.status === 'no_response') {
          text($('cmd-msg'), t.label + ': 현장 응답 없음 (연결 복구 후 결과가 바뀔 수 있음)', 'msg warn');
        }
      }
      if (changed) loadCommands();
    });
  }

  // ---------- 명령 이력 ----------

  function loadCommands() {
    return once('commands', async () => {
      const owner = state.user;
      if (!owner) return;
      let path = '/api/commands';
      const d = selected();
      if ($('only-selected').checked && d) path += '?device_id=' + encodeURIComponent(d.device_id);
      try {
        const data = await call(path);
        if (state.user !== owner) return;
        const commands = Array.isArray(data.commands) ? data.commands : [];
        renderCommands(commands);
        text($('commands-msg'), commands.length ? '' : '보낸 명령이 없습니다.', 'msg');
        $('commands-updated').textContent = '갱신 ' + timeFmt.format(new Date());
      } catch (err) {
        if (lost(err)) return;
        text($('commands-msg'), '명령 이력을 불러오지 못했습니다. ' + describe(err), 'msg error');
      }
    });
  }

  function renderCommands(commands) {
    const body = $('command-rows');
    body.replaceChildren();
    commands.forEach((c) => {
      const row = document.createElement('tr');
      if (state.tracked.has(c.cmd_id)) row.classList.add('tracked');
      cell(row, when(c.issued_at));
      cell(row, String(c.device_id || '-'));
      cell(row, temp(c.value), 'num');
      cell(row, String(c.issued_by || '-'));
      const result = document.createElement('span');
      result.appendChild(badge(STATUS_SHORT[c.status] || String(c.status), STATUS_SHORT[c.status] ? c.status : 'none'));
      const notes = [];
      if (c.status === 'rejected') notes.push(reasonText(c));
      if (c.status === 'no_response') notes.push('연결 복구 후 결과가 바뀔 수 있음');
      if (c.replay_rejected > 0) notes.push('재전송 거부 ' + c.replay_rejected + '회');
      if (notes.length) result.appendChild(note(notes.join(' · ')));
      cell(row, result);
      cell(row, when(c.decided_at));
      body.appendChild(row);
    });
  }

  // ---------- 시작 ----------

  async function init() {
    ['pointerdown', 'keydown'].forEach((type) => {
      document.addEventListener(type, () => { activity = true; }, {capture: true, passive: true});
    });
    $('login-form').addEventListener('submit', login);
    $('logout').addEventListener('click', logout);
    $('device-rows').addEventListener('click', (event) => {
      const pick = event.target.closest('button.device-pick');
      if (pick) selectDevice(pick.dataset.key);
    });
    $('cmd-device').addEventListener('change', (event) => selectDevice(event.target.value));
    $('cmd-form').addEventListener('submit', review);
    $('confirm-send').addEventListener('click', send);
    $('confirm-cancel').addEventListener('click', () => {
      hideConfirm();
      text($('cmd-msg'), '명령을 취소했습니다.', 'msg');
    });
    $('only-selected').addEventListener('change', () => loadCommands());
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.user) refreshAll();
    });

    try {
      startConsole(await call('/api/session', {active: true}));
    } catch (err) {
      showLogin(err instanceof Failure && err.status === 401 ? '' : '서버에 연결하지 못했습니다. 잠시 뒤 새로고침하세요.');
    }
  }

  init();
}());
