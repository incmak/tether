import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import live_usage

RESULT = {'rateLimits': {'primary': {'usedPercent': 66, 'windowDurationMins': 10080, 'resetsAt': 1793774280}, 'secondary': None}}


class LiveUsageTests(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, HOME=self.home))
        live_usage._cache.clear()

    def test_codex_week_window_and_cache(self):
        calls = []
        ask = lambda: calls.append(1) or RESULT
        entry = live_usage.check(30, now=1000, ask=ask)
        self.assertEqual(entry['report']['windows'], [{'label': '7d', 'percent': 66, 'resets_at': 1793774280}])
        live_usage.check(30, now=1020, ask=ask)
        self.assertEqual(len(calls), 1)  # inside max_age: no second app-server run

    def test_failure_keeps_the_last_report(self):
        live_usage.check(30, now=1000, ask=lambda: RESULT)
        def broken():
            raise TimeoutError('Codex did not answer in time.')
        entry = live_usage.check(30, now=1100, ask=broken)
        self.assertEqual(entry['error'], 'Codex did not answer in time.')
        self.assertEqual(entry['report']['windows'][0]['percent'], 66)

    def test_app_server_exchange_with_a_fake_server(self):
        fake = ('import json,os,sys\n'
                'assert os.path.samefile(os.getcwd(), sys.argv[1])\n'
                'for line in sys.stdin:\n'
                ' m=json.loads(line)\n'
                ' if m.get("id")==1: print(json.dumps({"id":1,"result":{}}),flush=True)\n'
                ' if m.get("id")==2: print(json.dumps({"id":2,"result":%s}),flush=True)\n' % repr(RESULT).replace('None', 'None'))
        result = live_usage.ask_app_server((sys.executable, '-c', fake, self.home), timeout=10)
        self.assertEqual(result['rateLimits']['primary']['usedPercent'], 66)

    def test_silent_server_times_out(self):
        with self.assertRaises(TimeoutError):
            live_usage.ask_app_server((sys.executable, '-c', 'import time; time.sleep(30)'), timeout=1)


if __name__ == '__main__':
    unittest.main()
