"""Policy selection must retain real windows and expire with its authority."""
import copy
import unittest
import test_goal_binding as fixtures


class UsagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GoalBindingTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.bind()
        self.policy = dict(run_id='run-a', epoch='epoch', decision_windows=[300],
                           exact_quote='Ignore weekly for this run.', valid_from=9900, valid_until=20000)
        self.f.engine.usage_policy_provider = lambda: self.policy

    def read_usage(self):
        return self.f.call('usage', observed_at=self.f.now, rateLimits=dict(
            primary=dict(windowDurationMins=300, usedPercent=10),
            secondary=dict(windowDurationMins=10080, usedPercent=95)))

    def test_authorized_weekly_exception_changes_decision_without_changing_measurements(self):
        result=self.read_usage()
        self.assertEqual(result['action'],'CONTINUE')
        self.assertEqual(result['remaining'],90)
        state=fixtures.m.read_json(self.f.engine.path)
        self.assertEqual(state['usage']['windows']['10080']['usedPercent'],95)
        self.assertEqual(state['usage']['decision_policy'],self.policy)
        self.assertEqual(state['status'],'active')

    def test_exception_expiry_shortens_deadline_and_requires_refresh(self):
        self.policy['valid_until']=self.f.now+10
        result=self.read_usage()
        self.assertEqual(result['next_check_at'],self.f.now+10)
        self.f.now+=11
        self.assertEqual(self.f.call('usage-due')['action'],'READ_USAGE')
        before=self.f.engine.path.read_bytes()
        with self.assertRaises(fixtures.m.Error):self.read_usage()
        self.assertEqual(self.f.engine.path.read_bytes(),before)

    def test_no_exception_restores_both_real_decision_windows(self):
        self.policy=None
        result=self.read_usage()
        self.assertEqual(result['action'],'SAVE_AND_STOP')
        self.assertEqual(result['remaining'],5)

    def test_policy_cannot_belong_to_other_run_or_have_missing_authority(self):
        original=copy.deepcopy(self.policy)
        for change in ({'run_id':'other'},{'exact_quote':''},{'decision_windows':[10080]},
                       {'valid_from':self.f.now+1},{'valid_until':self.f.now}):
            self.policy={**original,**change}
            before=self.f.engine.path.read_bytes()
            with self.subTest(change=change),self.assertRaises(fixtures.m.Error):self.read_usage()
            self.assertEqual(self.f.engine.path.read_bytes(),before)


if __name__=='__main__':unittest.main()
