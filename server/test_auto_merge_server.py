"""The auto-merge notice that server.py sends. No tmux, no network: notify is replaced."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest
from unittest.mock import patch

import server


class Notice(unittest.TestCase):
    def test_a_pane_notice_links_to_the_pane_route_inside_the_app(self):
        # B25: /chat/open/N brings a running installed app to the pane. /mobile/ links open the browser instead.
        for session, url in (('%13', '/chat/open/13'), ('%308', '/chat/open/308')):
            with patch.object(server, 'notify') as notify:
                server.auto_merge_alert('Auto-merged', 'Merged.', session)
            notify.assert_called_once_with({
                'title': 'Auto-merged', 'body': 'Merged.', 'tag': 'auto-merge-' + session, 'priority': 'important',
                'silent': False, 'session': session, 'url': url}, False)

    def test_a_notice_without_a_pane_has_no_link_and_an_odd_pane_id_stays_in_the_app(self):
        with patch.object(server, 'notify') as notify:
            server.auto_merge_alert('Auto-merge failed', 'It failed.', None)
        self.assertNotIn('url', notify.call_args.args[0])
        for session in ('13', '%1/../x', '%'):
            with patch.object(server, 'notify') as notify:
                server.auto_merge_alert('Auto-merge failed', 'It failed.', session)
            self.assertEqual(notify.call_args.args[0]['url'], '/chat/')


if __name__ == '__main__':
    unittest.main()
