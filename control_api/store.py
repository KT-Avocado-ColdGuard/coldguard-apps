"""중앙 MySQL 조회와 명령 요청 기록.

- DB 계정 coldguard_api는 전체 SELECT와 command_request INSERT만 할 수 있다(수정·삭제 권한 없음).
- 요청마다 연결을 새로 열고 닫는다(공통 Database). 오래 쥔 연결이 DB 재시작 뒤 끊겨 있는 문제를 피한다.
- 조회는 5초가 넘으면 DB가 끊는다(MAX_EXECUTION_TIME). 부하 시험 중 느린 조회가 스레드를 오래 잡지 않게 한다.
- 시각은 모두 UTC(연결 time_zone +00:00, DATETIME(3))로 읽고 쓴다.
"""
from common.coldguard_runtime import Database

SELECT = 'SELECT /*+ MAX_EXECUTION_TIME(5000) */ '
REQUEST_COLUMNS = 'cmd_id,site_id,device_id,action,value,issued_by,issued_at,expires_at'


def _in(values):
    return ','.join(['%s'] * len(values))


class Store(Database):
    def ping(self):
        self.query('SELECT 1 AS ok')

    def insert_request(self, row):
        """row: cmd_id, site_id, device_id, action, value(Decimal), issued_by, issued_at, expires_at(시간대 없는 UTC)."""
        db = self.connect()
        try:
            with db.cursor() as cur:
                cur.execute(f'INSERT INTO command_request({REQUEST_COLUMNS}) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                            (row['cmd_id'], row['site_id'], row['device_id'], row['action'], row['value'],
                             row['issued_by'], row['issued_at'], row['expires_at']))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def request(self, cmd_id, sites):
        rows = self.query(f'{SELECT}{REQUEST_COLUMNS} FROM command_request '
                          f'WHERE cmd_id=%s AND site_id IN ({_in(sites)})', (cmd_id, *sites))
        return rows[0] if rows else None

    def requests(self, sites, device_id, limit):
        sql = f'{SELECT}{REQUEST_COLUMNS} FROM command_request WHERE site_id IN ({_in(sites)})'
        args = list(sites)
        if device_id:
            sql += ' AND device_id=%s'
            args.append(device_id)
        sql += ' ORDER BY issued_at DESC, id DESC LIMIT %s'
        args.append(limit)
        return self.query(sql, tuple(args))

    def acks(self, cmd_ids):
        if not cmd_ids:
            return []
        return self.query(f'{SELECT}id,cmd_id,site_id,device_id,result,reason,acked_at FROM command_ack '
                          f'WHERE cmd_id IN ({_in(cmd_ids)}) ORDER BY acked_at, id', tuple(cmd_ids))

    def latest(self, sites):
        """장비마다 가장 최근 측정값 한 줄. (site_id, device_id, measured_at) 색인으로 장비별 최대 시각을 찾는다."""
        rows = self.query(
            f'{SELECT}t.site_id,t.device_id,t.seq,t.measured_at,t.temp_c,t.setpoint_c,t.door_open,t.cooling,'
            't.status,t.received_at FROM telemetry t JOIN ('
            'SELECT site_id,device_id,MAX(measured_at) AS m FROM telemetry '
            f'WHERE site_id IN ({_in(sites)}) GROUP BY site_id,device_id) x '
            'ON t.site_id=x.site_id AND t.device_id=x.device_id AND t.measured_at=x.m '
            'ORDER BY t.site_id, t.device_id, t.seq DESC', tuple(sites))
        seen, result = set(), []
        for r in rows:  # 같은 시각의 측정이 여러 개면 seq가 가장 큰 것만 남긴다.
            if (r['site_id'], r['device_id']) not in seen:
                seen.add((r['site_id'], r['device_id']))
                result.append(r)
        return result

    def series(self, site_id, device_id, since, step):
        """step초 단위로 묶은 온도 추이. 구간마다 평균·최저·최고 온도, 마지막 설정 온도, 문 열림 여부, 측정 수."""
        return self.query(
            f'{SELECT}FLOOR(UNIX_TIMESTAMP(measured_at)/%s) AS bucket, AVG(temp_c) AS temp_avg, '
            'MIN(temp_c) AS temp_min, MAX(temp_c) AS temp_max, '
            "SUBSTRING_INDEX(GROUP_CONCAT(setpoint_c ORDER BY measured_at DESC, seq DESC), ',', 1) AS setpoint_c, "
            'MAX(door_open) AS door_open, COUNT(*) AS samples FROM telemetry '
            'WHERE site_id=%s AND device_id=%s AND measured_at>=%s GROUP BY bucket ORDER BY bucket',
            (step, site_id, device_id, since))

    def events(self, sites, device_id, since, limit):
        sql = (f'{SELECT}id,site_id,device_id,event_type,severity,temp_c,predicted_sec,detail,occurred_at,'
               f'received_at FROM event_log WHERE site_id IN ({_in(sites)}) AND occurred_at>=%s')
        args = [*sites, since]
        if device_id:
            sql += ' AND device_id=%s'
            args.append(device_id)
        sql += ' ORDER BY occurred_at DESC, id DESC LIMIT %s'
        args.append(limit)
        return self.query(sql, tuple(args))
