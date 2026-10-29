"""Configured project carrier instruction contracts, not automated lifecycle enforcement.

CPU identities/approval are from the retained r5 incident. Reason/event time below are
synthetic renderer inputs; this file neither replays a carrier nor touches a board.
"""
import hashlib
import unittest

from clickup_intake import render


def cpu_incident(kind='needs_input', reason=None):
    reason = reason if reason is not None else (
        'CPU r5 source handoff names the same existing baseline t_ab000087 for '
        'current-dev refresh; preserve its prior source run and the unfinished source parent.')
    return dict(task='86pub00004', card='t_ab000053', run_id=1323,
                event_kind='blocked', event_created_s=1, block_kind='needs_input',
                **{'class': kind}, source_status='ready', reason=reason,
                reason_sha256=hashlib.sha256(reason.encode('utf-8')).hexdigest(),
                approval_id=23, rev=5,
                plan_sha256='76b2ff6981354783fc3c8dde937f583747fd15cb6563a3a74fe02b48e74e30bc',
                attempt=1, adopted=0)


class CurrentAuthorityCarrierTest(unittest.TestCase):
    def test_both_rendered_carriers_replace_fixed_verbs_with_guarded_current_authority(self):
        for kind in ('needs_input', 'diagnostic'):
            with self.subTest(kind=kind):
                body = render.recovery_card_body(cpu_incident(kind))
                for required in (
                    'Two outcomes only:',
                    'supported normal lifecycle',
                    'existing capable owner',
                    'intermediate, not completion',
                    'genuinely human-required question',
                    'reply/wait/resume',
                    'exact owned lineage',
                    'healthy active descendants',
                    'operator-adopted recovery scope',
                    'current-dev-baseline.md',
                    'same existing startup/baseline card',
                ):
                    self.assertIn(required, body)
                for obsolete in (
                    'The permitted operator-only verbs are exactly:',
                    'Only use already-authorized SUPPORTED existing recovery operations:',
                    'A diagnostic wake does not expand that permitted set.',
                ):
                    self.assertNotIn(obsolete, body)
                self.assertIn('immediately before', body)
                self.assertIn('STALE', body)
                self.assertIn('UNAVAILABLE', body)
                self.assertIn('never complete a question' if kind == 'diagnostic'
                              else 'never touch CLICKUP-QUESTION', body)

    def test_cpu_binding_and_supported_source_resume_are_not_replaced(self):
        for kind in ('needs_input', 'diagnostic'):
            with self.subTest(kind=kind):
                p = cpu_incident(kind)
                body = render.recovery_card_body(p)
                for token in ('86pub00004', 't_ab000053', '1323', '23', 'r5',
                              p['reason_sha256'], p['plan_sha256'],
                              'recovery-check --card t_ab000053 --run 1323',
                              'CLICKUP-RECOVERY-RECEIPT' if kind == 'needs_input'
                              else 'existing recovery receipt'):
                    self.assertIn(token, body)
                if kind == 'needs_input':
                    self.assertIn(p['reason'], body)

    def test_body_limit_and_existing_multibyte_reason_placement(self):
        reason = '🧭界é' * 2000
        for kind in ('needs_input', 'diagnostic'):
            with self.subTest(kind=kind):
                p = cpu_incident(kind, reason)
                empty = render.recovery_card_body(dict(p, reason=''))
                body = render.recovery_card_body(p)
                self.assertLessEqual(len(body.encode('utf-8')), render.RECOVERY_BODY_MAX)
                room = render.RECOVERY_BODY_MAX - len(empty.encode('utf-8'))
                expected = reason[:render.RECOVERY_REASON_MAX].encode('utf-8')[:room].decode('utf-8', 'ignore')
                self.assertTrue(expected)
                self.assertNotEqual(expected, reason)
                if kind == 'needs_input':
                    self.assertIn(expected, body)
                    self.assertEqual(body.replace(expected, '', 1), empty)
                else:
                    # Diagnostic deliberately carries the fingerprint, not the raw reason.
                    self.assertEqual(body, empty)
                self.assertNotIn('\ufffd', body)
                self.assertIn(p['reason_sha256'], body)


if __name__ == '__main__':
    unittest.main()
