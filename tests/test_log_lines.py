"""로그 한 줄 쓰기 시험.

- 여러 스레드(MQTT 콜백, 요청 처리)가 동시에 찍어도 줄이 섞이지 않는다.
- 출력 내용은 예전 print 방식과 글자 하나까지 같다(시설 controller·수집 워커의 로그 형식이 바뀌지 않는다).
"""
import io
import json
import threading
import time
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

from central_worker import central_worker as cw
from common import coldguard_runtime as runtime


class RecordingStdout:
    """write 호출을 하나씩 기록한다. 호출마다 다른 스레드에 차례를 넘겨 끼어들기가 잘 일어나게 한다."""

    def __init__(self):
        self.calls, self.flushes = [], 0

    def write(self, text):
        self.calls.append(text)
        time.sleep(0)
        return len(text)

    def flush(self):
        self.flushes += 1


class LogLineTests(unittest.TestCase):
    def test_each_record_is_one_write_ending_with_newline(self):
        out = RecordingStdout()
        with patch('sys.stdout', out):
            runtime.log('control_api_start', sites=['site01'])
            cw.log('mqtt_connect', reason='Success')
        self.assertEqual(len(out.calls), 2)
        for call in out.calls:
            self.assertTrue(call.endswith('\n') and call.count('\n') == 1, call)
            json.loads(call)
        self.assertEqual(out.flushes, 2)

    def test_same_output_as_previous_print(self):
        fields = dict(detail='현장 잠금', value=-22.0, codes=[1, 1])

        def previous(event, **kw):  # 바꾸기 전 common.coldguard_runtime.log
            print(json.dumps(dict(event=event, at=runtime.utc(), **kw), ensure_ascii=False, allow_nan=False),
                  flush=True)

        with patch('common.coldguard_runtime.utc', return_value='2026-11-26T03:00:00.000000Z'):
            old, new = io.StringIO(), io.StringIO()
            with redirect_stdout(old):
                previous('command_decision', **fields)
            with redirect_stdout(new):
                runtime.log('command_decision', **fields)
        self.assertEqual(new.getvalue(), old.getvalue())
        self.assertIn('현장 잠금', new.getvalue())  # 한글을 \u 이스케이프하지 않는 것도 그대로

    def test_worker_format_unchanged(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cw.log('storage_retry', error='OperationalError', value=Decimal('-29.90'))
        record = json.loads(out.getvalue())
        self.assertEqual(list(record)[:2], ['event', 'logged_at'])
        self.assertEqual(record['value'], '-29.90')  # default=str 그대로
        self.assertTrue(out.getvalue().endswith('}\n'))

    def test_invalid_value_still_raises_without_partial_output(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(ValueError):
            runtime.log('x', value=float('nan'))  # allow_nan=False는 예전과 같다
        self.assertEqual(out.getvalue(), '')

    def test_threads_do_not_mix_lines(self):
        out = RecordingStdout()
        per_thread, threads = 200, 8

        def worker(n):
            for i in range(per_thread):
                if i % 2:
                    runtime.log('control_api_auth_rejected', thread=n, i=i)
                else:
                    cw.log('message_rejected', thread=n, i=i)

        with patch('sys.stdout', out):
            pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
            for t in pool:
                t.start()
            for t in pool:
                t.join()
        lines = ''.join(out.calls).split('\n')
        self.assertEqual(lines[-1], '')
        records = [json.loads(line) for line in lines[:-1]]  # 섞인 줄이 있으면 여기서 실패한다
        self.assertEqual(len(records), per_thread * threads)

    def test_missing_stdout_is_ignored_like_print(self):
        with patch('sys.stdout', None):
            runtime.write_line('{"event": "x"}')


if __name__ == '__main__':
    unittest.main()
