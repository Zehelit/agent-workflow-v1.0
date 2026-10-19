import importlib.util
import functools
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

script=Path(__file__).resolve().parents[1]/'private/limit-watcher/scripts/monitor.py'
spec=importlib.util.spec_from_file_location('monitor',script)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
helper=Path(__file__).resolve().parents[1]/'scripts/workflow.py'
m.workflow_schedule=functools.partial(m.workflow_schedule, helper=helper,
    helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest())

class MonitorTests(unittest.TestCase):
    def test_unknown_retries_once_then_requests_recovery(self):
        with patch.object(m,'measure',side_effect=RuntimeError('unavailable')) as call:
            self.assertEqual(m.sample()['action'],'SAVE_AND_STOP')
            self.assertEqual(call.call_count,2)

    def test_threshold_ends_sampler_without_claiming_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);control=m.default_control('threshold-test');m.write(base/'control.json',control)
            windows={duration:{'windowDurationMins':duration,'usedPercent':95,'resetsAt':9999999999}
                     for duration in (300,10080)}
            with patch.object(m,'measure',return_value=windows):result=m.run(base)
            self.assertFalse((base/'running.json').exists())
            self.assertEqual(m.read(base/'latest.json')['action'],'SAVE_AND_STOP')
            self.assertEqual(result['exitReason'],'usage_threshold')
            self.assertEqual(m.read(base/'save-latch.json')['epoch'],control['epoch'])
            self.assertNotIn('saved',m.read(base/'latest.json'))

    def test_expired_lease_does_not_measure(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);control=m.default_control('expired-test');control['expiresAt']=0
            m.write(base/'control.json',control)
            with patch.object(m,'sample') as call:m.run(base)
            call.assert_not_called()
            self.assertFalse((base/'running.json').exists())

    def test_timer_waits_until_due_and_keeps_one_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);control=m.default_control('timer-test',100)
            m.write(base/'control.json',control)
            clock=[100]
            def tick(_):
                clock[0]+=100
                if clock[0]>=500:m.write(base/'control.json',dict(control,active=False,expiresAt=0))
                return False
            def result(previous,current):return {'action':'CONTINUE','at':clock[0],
                    'next_check_at':clock[0]+300,'policy':m.policy_card(current,clock[0])}
            real_event=m.threading.Event
            stopped=real_event()
            with patch.object(m.time,'time',side_effect=lambda:clock[0]),patch.object(stopped,'wait',side_effect=tick),\
                    patch.object(m.threading,'Event',side_effect=[stopped,real_event()]),\
                    patch.object(m.threading.Thread,'start'),patch.object(m,'sample',side_effect=result) as call:m.run(base)
            self.assertEqual(call.call_count,2)
            self.assertEqual(m.read(base/'latest.json')['at'],400)
            self.assertEqual(len(list(base.glob('latest*.json'))),1)
            self.assertFalse((base/'running.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
