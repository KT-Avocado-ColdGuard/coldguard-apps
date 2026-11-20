"""로컬 통합 실행기. 네 서비스를 독립 프로세스로 실행하고 함께 종료한다."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ('collector.data_collector', 'controller.controller_svc',
           'simulator.freezer_simulator', 'field_web.field_web')

def preflight():
    # 공통 인증/포트를 모든 프로세스에 적용하면 충돌하므로 역할별 설정을 사용한다.
    ambiguous = ('MQTT_USER','MQTT_PASS','MYSQL_USER','MYSQL_PASSWORD',
                 'MQTT_CLIENT_ID','WEB_PORT','METRICS_PORT','STATE_DB')
    if any(key in os.environ for key in ambiguous):
        raise ValueError('통합 실행은 공통 계정/포트 환경변수 대신 서비스별 변수를 사용하세요: '
                         + ', '.join(key for key in ambiguous if key in os.environ))
    from collector.data_collector import settings as collector
    from controller.controller_svc import settings as controller
    from field_web.field_web import settings as web
    from simulator.simulator_mqtt import settings as simulator
    from common.coldguard_config import values
    configs = [collector(), controller(), web()]
    sim = simulator()
    if sim is None:
        raise ValueError('전체 연동 실행은 SIM_MQTT=true가 필요합니다. 로컬 화면만 보려면 시뮬레이터를 단독 실행하세요.')
    configs.append(sim)
    for cfg in configs:
        if not Path(cfg['MQTT_CA']).is_file():
            raise ValueError('CA 인증서 파일을 확인하세요.')
    if any(sim['DEVICE'] not in cfg['DEVICES'] for cfg in configs[:3]):
        raise ValueError('DEVICES 목록에 시뮬레이터 DEVICE를 포함하세요.')
    ports = [configs[0]['METRICS_PORT'],configs[1]['METRICS_PORT'],configs[2]['WEB_PORT'],int(values('simulator')['WEB_PORT'])]
    if any(not 1 <= p <= 65535 for p in ports) or len(set(ports)) != 4:
        raise ValueError('통합 실행의 웹/지표 포트 4개는 유효하고 서로 달라야 합니다.')

def stop_all(children):
    for child in children:
        if child.poll() is None:
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM)
            except OSError:
                pass
    deadline = time.monotonic()+8
    for child in children:
        try:
            child.wait(timeout=max(.1,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()

def main():
    try:
        preflight()
    except (ValueError, OSError) as exc:
        print('실행 설정 오류: '+str(exc),file=sys.stderr)
        return 2
    children=[]
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        for module in MODULES:
            child=subprocess.Popen([sys.executable,'-u','-m',module],cwd=ROOT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
            children.append(child)
            print('시작: '+module,flush=True)
        while True:
            for module,child in zip(MODULES,children):
                code=child.poll()
                if code is not None:
                    print(f'{module} 종료({code}): 나머지 서비스도 종료합니다.',flush=True)
                    return code if code else 1
            time.sleep(.2)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print('프로세스 실행 실패: '+type(exc).__name__,file=sys.stderr)
        return 1
    finally:
        stop_all(children)

if __name__=='__main__':
    sys.exit(main())
