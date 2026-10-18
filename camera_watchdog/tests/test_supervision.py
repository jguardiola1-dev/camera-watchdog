import unittest
from copy import deepcopy

import web_ui


class SupervisionTests(unittest.TestCase):
    def test_disabled_alert_does_not_flash_even_if_diagnostic_loss_is_persistent(self):
        state = {'last_ok': 990, 'changed': True, 'alarms_enabled': False,
                 'census': {'status': 'scene_change', 'observed_scene_loss': True}}
        self.assertEqual(web_ui.presentation('principal', state), ('Avisos desactivados', 'neutral'))
        state['alarms_enabled'] = True
        self.assertEqual(web_ui.presentation('principal', state)[1], 'danger alarm')

    def test_operational_requires_configured_validated_live_detection(self):
        camera = {'slug': 'porche', 'enabled': True}
        state = {'last_ok': 990, 'alarm_ready': True, 'census': {
            'status': 'stable', 'reliable': True, 'comparison_mode': 'day_night_references',
            'reference_comparisons': {k: {'reference_id': k} for k in ('day', 'night')}}}
        refs = {'day': True, 'night': True}
        evaluate = lambda c, s, r, p={}, collection=False: web_ui.sabotage_status(c, s, collection, r, p, now=1000)
        self.assertTrue(evaluate(camera, state, refs)['operational'])
        self.assertTrue(evaluate({**camera, 'slug': 'principal'}, state, refs)['operational'])
        self.assertFalse(evaluate({**camera, 'slug': 'principal', 'enabled': False}, state, refs)['operational'])
        for kind in ('day', 'night'):
            self.assertTrue(evaluate(camera, state, {kind: True})['operational'])
        for change in ('disabled', 'global', 'empty', 'offline', 'stale', 'temporal', 'unvalidated', 'unreliable', 'api'):
            with self.subTest(change=change):
                c, s, r, p = deepcopy(camera), deepcopy(state), dict(refs), {}
                if change == 'disabled': c['enabled'] = False
                if change == 'empty': r = {}
                if change == 'offline': s['offline'] = True
                if change == 'stale': s['last_ok'] = 800
                if change == 'temporal': s['census']['comparison_mode'] = 'temporal'
                if change == 'unvalidated': s['alarm_ready'] = False
                if change == 'unreliable': s['census']['reliable'] = False
                if change == 'api': p['reason'] = 'homeassistant_api_error'
                answer = evaluate(c, s, r, p, change == 'global')
                self.assertFalse(answer['operational'])
                self.assertTrue(answer['reason'])
        state['changed'] = True
        state['census']['status'] = 'scene_change'
        self.assertTrue(evaluate(camera, state, refs)['operational'], 'A detected incident does not disable protection')

    def test_added_night_must_be_evaluated_before_showing_operational(self):
        camera={'slug':'piscina','enabled':True}
        state={'last_ok':990,'alarm_ready':True,'census':{'comparison_mode':'day_night_references',
               'status':'stable','reliable':True,'reference_comparisons':{k:{'reference_id':k} for k in ('day','night')}}}
        refs={'day':True,'night':True,'night_alt':True}
        self.assertFalse(web_ui.sabotage_status(camera,state,False,refs,{},now=1000)['operational'])
        state['census']['reference_comparisons']['night_alt']={'reference_id':'alternate'}
        self.assertTrue(web_ui.sabotage_status(camera,state,False,refs,{},now=1000)['operational'])

    def test_capture_badge_is_separate_from_sabotage_readiness(self):
        camera = {'slug': 'lateral', 'name': 'Lateral'}
        state = {'last_ok': 990, 'census': {'status': 'stable'}, 'sabotage': {
            'operational': False, 'label': 'Sabotaje no operativo', 'reason': 'Falta referencia Census de noche'}}
        page = web_ui.camera_card(camera, state, '')
        self.assertIn('Escena estable', page)
        self.assertIn('sabotage-status danger', page)
        self.assertIn('Falta referencia Census de noche', page)
