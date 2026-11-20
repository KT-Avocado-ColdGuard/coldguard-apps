"""재개방 경보의 예상 시간이 MQTT 이벤트까지 유지되는지 확인."""
import unittest
from common.temperature_analysis import analyze, SIM_RISE
from collector.data_collector import Store

class PredictionEventTests(unittest.TestCase):
    def step(self, old, ts, temp, door, received=None):
        msg=dict(site='site01',device='fz01',ts=ts,temp=temp,door=door)
        return analyze(old,msg,ts if received is None else received,15)[0]

    def test_reopen_first_sample_has_prediction(self):
        state=self.step(None,0,-27,True)
        state=self.step(state,1,-27.1,False)
        state=self.step(state,2,-27,True)
        self.assertEqual(state['status'],'early')
        self.assertEqual(state['eta'],50)
        event=Store.alarm(state,2)
        self.assertEqual(event['event_type'],'early_warning')
        self.assertEqual(event['predicted_sec'],50)

    def test_measured_slope_wins_over_fallback(self):
        state=self.step(None,0,-27,True)
        state=self.step(state,1,-27,False)
        state=self.step(state,2,-27,True)
        state=self.step(state,3,-26.9,True)
        self.assertAlmostEqual(state['eta'],19)

    def test_stale_does_not_receive_estimate(self):
        state=self.step(None,0,-27,True)
        state=self.step(state,1,-27,False)
        state=self.step(state,2,-27,True,received=30)
        self.assertEqual(state['status'],'stale')
        self.assertIsNone(state['eta'])

    def test_normal_prediction_event(self):
        state=None
        for t in range(71):
            state=self.step(state,t,-29+t*SIM_RISE,True)
        self.assertEqual(state['status'],'early')
        self.assertIsInstance(Store.alarm(state,70)['predicted_sec'],int)
