"""로컬 디자인 확인용. 실제 MQTT/DB에 연결하지 않고 샘플만 표시한다."""
import ipaddress
import secrets
import time
from datetime import datetime, timezone
from field_web.field_web import create_app

class Samples:
    def state(self, device):
        return dict(temp=-29.7,target=-30.0,door=False,cooling=True,
                    stamp=datetime.now(timezone.utc).isoformat(),ts=time.time(),eta=None,status='normal')
    def commands(self, device):
        # 실환경에서는 DB의 최신 다섯 건, 미리보기에서는 거절/성공 예시를 표시한다.
        return [dict(cmd_id='preview-rejected',status='rejected',
                     request=dict(target=100.0),result=dict(reason='out_of_range')),
                dict(cmd_id='preview-accepted',status='accepted',
                     request=dict(target=-28.0),result=dict(reason=None)),
                dict(cmd_id='preview-locked',status='rejected',
                     request=dict(target=-20.0),result=dict(reason='locked')),
                dict(cmd_id='preview-restored',status='accepted',
                     request=dict(target=-30.0),result=dict(reason=None)),
                dict(cmd_id='preview-expired',status='rejected',
                     request=dict(target=-27.0),result=dict(reason='expired'))]
    def locked(self, device):
        return False
    def logs(self, device, kind, limit):
        stamp=datetime.now(timezone.utc).isoformat()
        if kind=='measurements':
            return [dict(stamp=stamp,temp=-30+i*.1,target=-30.0,door=bool(i%2)) for i in range(10)]
        if kind=='alarms':
            return [dict(stamp=stamp,reason=r,temp=t,eta=None) for r,t in [('고온 예측 경보',-26),('고온 경보',-24),('현장 잠금',None)]]
        return [dict(stamp=stamp,cmd_id='preview-'+str(i),status=s,target=-28.0,reason=r) for i,(s,r) in enumerate([('accepted',None),('rejected','locked'),('rejected','expired')])]

def app():
    cfg=dict(SITE='site01',DEVICES=['fz01'],WEB_SECRET=secrets.token_hex(32),
             WEB_ALLOW_NETS=[ipaddress.ip_network('127.0.0.1/32')],CONTROL=False,STALE_SEC=30)
    return create_app(cfg,store=Samples())

if __name__=='__main__':
    print('디자인 미리보기 / 샘플 데이터 / 실제 문 조작과 잠금은 비활성화')
    app().run(host='127.0.0.1',port=8081,debug=False,use_reloader=False)
