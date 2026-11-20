"""
공통 통신 규칙

- 시설/장비 ID와 MQTT 토픽 형식을 검사합니다.
- 메시지 필드 / 온도 / 시각 / 명령 유효시간 등을 검사합니다.
- 온도 반올림과 UTC 시각 변환도 담당합니다.
- 여러 프로그램이 같은 규칙을 사용하도록 모아 둔 파일입니다.
"""
import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone

CHANNELS = {'telemetry', 'cmd', 'ack', 'event', 'apply', 'applied', 'door', 'lock'}
EVENTS = {'early_warning', 'high_temp', 'data_lost', 'cmd_rejected', 'lock_on', 'lock_off'}
REASONS = {'locked', 'expired', 'out_of_range', 'duplicate', 'unknown_device', 'apply_timeout'}


def identity(site, device):
    if not isinstance(site, str) or not re.fullmatch(r'site[0-9]+', site):
        raise ValueError('invalid_site_id')
    if not isinstance(device, str) or not re.fullmatch(r'fz[0-9]+', device):
        raise ValueError('invalid_device_id')
    if len(site) > 20 or len(device) > 20:
        raise ValueError('identity_too_long')
    return site, device


def topic(site, device, channel):
    identity(site, device)
    if channel not in CHANNELS:
        raise ValueError('invalid_channel')
    return f'coldguard/{site}/{device}/{channel}'


def split_topic(value):
    parts = value.split('/')
    if len(parts) != 4 or parts[0] != 'coldguard' or parts[3] not in CHANNELS:
        raise ValueError('invalid_topic')
    identity(parts[1], parts[2])
    return tuple(parts[1:])


def utc(value=None):
    # 저장/전송 시각은 UTC Z로 통일한다.
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError('timezone_required')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parse_time(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('utc_z_required')
    try:
        stamp = datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        raise ValueError('invalid_time') from None
    return stamp


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def temperature(value):
    if not number(value):
        raise ValueError('invalid_temperature')
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def loads(payload):
    if len(payload) > 16384:
        raise ValueError('payload_too_large')
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('duplicate_json_key')
            obj[key] = value
        return obj
    def bad(value):
        raise ValueError('invalid_number')
    data = json.loads(payload, object_pairs_hook=pairs, parse_constant=bad)
    if not isinstance(data, dict):
        raise ValueError('object_required')
    return data


def telemetry(data, source):
    site, device, channel = split_topic(source)
    if channel != 'telemetry':
        raise ValueError('wrong_channel')
    keys = ('site_id','device_id','seq','measured_at','temp_c','setpoint_c','door_open','cooling','status')
    if any(k not in data for k in keys):
        raise ValueError('missing_field')
    if (data['site_id'], data['device_id']) != (site, device):
        raise ValueError('topic_identity_mismatch')
    if type(data['seq']) is not int or not 1 <= data['seq'] <= 9223372036854775807:
        raise ValueError('invalid_seq')
    parse_time(data['measured_at'])
    if not number(data['temp_c']) or not number(data['setpoint_c']):
        raise ValueError('invalid_temperature')
    if type(data['door_open']) is not bool or type(data['cooling']) is not bool:
        raise ValueError('invalid_state')
    # 송신 온도와 status의 불일치를 거부한다. 예측 경보는 telemetry status가 아니다.
    expected = 'high_temp' if data['temp_c'] > -25 else 'normal'
    if data['status'] != expected:
        raise ValueError('temperature_status_mismatch')
    return {k: data[k] for k in keys}


def door(data, source):
    site, device, channel = split_topic(source)
    if channel != 'door' or (data.get('site_id'),data.get('device_id')) != (site,device):
        raise ValueError('topic_identity_mismatch')
    if data.get('action') not in ('open','close') or data.get('requested_by') != 'web':
        raise ValueError('invalid_door_action')
    parse_time(data.get('at'))
    return data['action'] == 'open'


def lock_message(data, source):
    # 잠금은 controller에서 처리한다. 시뮬레이터 문 조작에는 영향을 주지 않는다.
    site, device, channel = split_topic(source)
    if channel != 'lock' or (data.get('site_id'), data.get('device_id')) != (site, device):
        raise ValueError('topic_identity_mismatch')
    if type(data.get('locked')) is not bool or data.get('by') != 'web':
        raise ValueError('invalid_lock')
    parse_time(data.get('at'))
    return data['locked']


def delivery(channel, qos, retained):
    # lock만 retained 메시지를 허용한다. MQTT 전달 시 retain=False인 lock도 정상이다.
    if channel not in CHANNELS or qos != 1 or (retained and channel != 'lock'):
        raise ValueError('invalid_delivery')


# 시간 정책: 최대 TTL 300초 유지. 미래 발급 시각 오차 제한은 제거(2026-11-20).
MAX_TTL_SEC = 300.0


def time_reason(data, now=None):
    # 시간 거부의 세부 원인. 외부 ack는 기존 expired로 통일한다.
    now = now or datetime.now(timezone.utc)
    try:
        issued = parse_time(data.get('issued_at'))
        expires = parse_time(data.get('expires_at'))
    except ValueError:
        return 'invalid_time'
    if expires <= now:
        return 'expired'
    ttl = (expires-issued).total_seconds()
    if ttl <= 0:
        return 'invalid_lifetime'
    if ttl > MAX_TTL_SEC:
        return 'ttl_exceeded'
    return None


def command_reason(data, source, site, devices, duplicate=False, locked=False, now=None):
    # 확정 순서: unknown_device -> duplicate -> expired -> locked -> out_of_range.
    src_site, src_device, channel = split_topic(source)
    if channel != 'cmd':
        raise ValueError('wrong_channel')
    if (src_site != site or src_device not in devices or
            (data.get('site_id'), data.get('device_id')) != (src_site, src_device)):
        return 'unknown_device'
    if not isinstance(data.get('cmd_id'), str) or not 1 <= len(data['cmd_id']) <= 50:
        raise ValueError('invalid_cmd_id')
    if duplicate:
        return 'duplicate'
    if time_reason(data, now):
        return 'expired'
    if locked:
        return 'locked'
    if data.get('action') != 'set_temp' or not number(data.get('value')) or not -50 <= data['value'] < -10:
        return 'out_of_range'
    if not -50 <= temperature(data['value']) < -10:
        return 'out_of_range'
    if not isinstance(data.get('issued_by'), str) or not data['issued_by'].strip():
        raise ValueError('invalid_issued_by')
    return None


def approved(data, at=None):
    # 검증 통과 뒤에만 호출한다. 원래 만료 시각을 장비까지 전달한다.
    return {**{key:data[key] for key in ('site_id','device_id','cmd_id','action','value','expires_at')},
            'value':temperature(data['value']), 'approved_at':utc(at)}


def check_apply(data, source, now=None):
    site, device, channel = split_topic(source)
    if channel != 'apply' or (data.get('site_id'),data.get('device_id')) != (site,device):
        raise ValueError('topic_identity_mismatch')
    if not isinstance(data.get('cmd_id'),str) or not 1 <= len(data['cmd_id']) <= 50:
        raise ValueError('invalid_cmd_id')
    parse_time(data.get('approved_at'))
    if parse_time(data.get('expires_at')) <= (now or datetime.now(timezone.utc)):
        raise ValueError('expired')
    if data.get('action') != 'set_temp' or not number(data.get('value')) or not -50 <= data['value'] < -10:
        raise ValueError('out_of_range')
    value=temperature(data['value'])
    if not -50 <= value < -10:
        raise ValueError('out_of_range')
    return value
