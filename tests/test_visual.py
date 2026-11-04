import json
import math
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from notifier.driver import Controller, MacDriver
from notifier.errors import AppError
from notifier.models import Bind, Search
from notifier.store import Store
from notifier.visual import PROTOCOL, VisualSession, candidates, name_match, reading_order


def row(text, y=.2, x=.1):
    return {'text': text, 'confidence': 1, 'x': x, 'y': y, 'w': .12, 'h': .02}


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.driver = MacDriver(self.temp.name)
        self.visual = self.driver.visual
        self.profile = {'protocol': PROTOCOL, 'wechat_version': '4.1.15', 'size': [1000, 800],
                        'header_hash': 'correct', 'header_rect': {'x': .3, 'y': .02, 'w': .3, 'h': .05},
                        'composer_rect': {'x': .3, 'y': .7, 'w': .65, 'h': .2},
                        'search_point': [.1, .05]}
        self.capture = {'bounds': {'Width': 1000, 'Height': 800, 'X': 0, 'Y': 0}, 'window_id': 8,
                        'snapshots': {'header': {'hash': 'correct'},
                                      'composer': {'hash': 'draft', 'blank': True}},
                        'regions': {'composer': []}, 'outgoing_marker': False}

    def tearDown(self):
        self.temp.cleanup()

    def test_candidates_require_group_section_and_complete_name(self):
        items = [row('目标群', .1), row('群聊', .2), row('目标群', .3), row('目标群备用', .4),
                 row('联系人', .5), row('目标群', .6)]
        self.assertEqual([i['y'] for i in candidates(items, '目标群')], [.3])
        self.assertEqual(candidates([row('目标群')], '目标群'), [])
        self.assertFalse(name_match('目标群备用', '目标群'))

    def test_low_confidence_emoji_is_only_a_human_confirmation_candidate(self):
        self.assertTrue(name_match('测试通知群义（13）', '测试通知群✅'))
        self.assertTrue(name_match('测试通知群⑦（13）', '测试通知群✅'))
        self.assertTrue(name_match('测试通知群习（13）', '测试通知群✅'))
        self.assertFalse(name_match('其他通知群习（13）', '测试通知群✅'))
        self.assertFalse(name_match('测试通知群备用（13）', '测试通知群✅'))
        self.assertFalse(name_match('测试通知群备用', '测试通知群✅'))
        self.visual.capture = Mock()
        with self.assertRaises(AppError) as error:
            self.visual.bind('测试通知群✅', self.profile, False)
        self.assertEqual(error.exception.code, 'VISUAL_CONFIRMATION_REQUIRED')
        self.visual.capture.assert_not_called()
        self.assertFalse(self.visual.path.exists())

    def test_profile_excludes_preview_and_requires_matching_version_and_size(self):
        self.visual._save('测试群', {**self.profile, 'preview': 'private image'})
        self.assertNotIn('private image', self.visual.path.read_text())
        self.assertEqual(self.visual.path.stat().st_mode & 0o777, 0o600)
        cap = {'bounds': self.capture['bounds']}
        self.assertTrue(self.visual.ready(cap, '4.1.15'))
        self.assertFalse(self.visual.ready(cap, '4.1.16'))
        self.assertFalse(self.visual.ready({'bounds': {'Width': 900, 'Height': 800}}, '4.1.15'))
        self.visual._save('测试群', {**self.profile, 'title_anchor': {'scale': [2, 2]}})
        self.assertTrue(self.visual.ready({'bounds': {'Width': 900, 'Height': 800}}, '4.1.15'))
        self.assertFalse(self.visual.ready(cap, '4.1.16'))

    def test_title_pixels_are_required_even_when_name_is_similar(self):
        capture = {**self.capture, 'snapshots': {'header': {'hash': 'wrong'}}}
        with self.assertRaises(AppError) as error:
            self.visual._title(capture, self.profile)
        self.assertEqual(error.exception.code, 'TITLE_CHANGED')

    def test_window_resize_stops_before_input(self):
        self.driver._bridge = Mock(return_value={**self.capture, 'bounds': {'Width': 900, 'Height': 800}})
        with self.assertRaises(AppError) as error:
            self.visual.capture(self.profile)
        self.assertEqual(error.exception.code, 'WINDOW_LAYOUT_CHANGED')
        self.assertEqual(self.driver._bridge.call_args.args, ('capture',))

    def resize_frames(self, size=(900, 700)):
        profile = deepcopy(self.profile)
        profile['title_anchor'] = {'offset': [-6, -6], 'pixel_size': [300, 46], 'scale': [2, 2]}
        bounds = {'Width': size[0], 'Height': size[1], 'X': 200, 'Y': 100}
        current = {**deepcopy(self.capture), 'bounds': bounds,
                   'image_size': {'width': size[0]*2, 'height': size[1]*2},
                   'regions': {'header': [row('测试群（13）', .03, .32)],
                               'sidebar': [row('搜索', .03, .1)],
                               'lower': [row('发送', .9, .8)], 'composer': []}}
        proof = {**current, 'header_match': {'matched': True, 'rect': {'x': .31, 'y': .02, 'w': .2, 'h': .04}}}
        editor = {'x': .32, 'y': .75, 'w': .61, 'h': .12}
        self.visual.measure_composer = Mock(return_value=(editor, {}, []))
        return profile, current, proof, editor

    def test_resize_relocates_title_and_editor_without_mutating_the_confirmed_identity(self):
        profile, current, proof, editor = self.resize_frames()
        original = deepcopy(profile)
        self.driver._bridge = Mock(side_effect=[current, proof, current])
        updated, checked = self.visual.relocate('测试群', profile)
        self.assertEqual(updated['size'], [900, 700])
        self.assertEqual(updated['composer_rect'], editor)
        self.assertEqual(updated['header_hash'], 'correct')
        self.assertEqual(profile, original)
        self.assertEqual(updated['header_rect'], proof['header_match']['rect'])
        self.assertEqual([call.args for call in self.driver._bridge.call_args_list], [('capture',)]*3)
        self.assertEqual(self.driver._bridge.call_args_list[1].kwargs['header_identity']['hash'], 'correct')

    def test_same_outer_size_remeasures_the_internal_chat_split(self):
        profile, current, proof, editor = self.resize_frames((1000, 800))
        self.driver._bridge = Mock(side_effect=[current, proof, current])
        updated, _ = self.visual.relocate('测试群', profile)
        self.assertNotEqual(updated['composer_rect'], profile['composer_rect'])
        self.visual.measure_composer.assert_called_once()

    def test_ocr_box_drift_uses_painted_title_pixels_for_identity_and_editor_boundary(self):
        profile, current, proof, _ = self.resize_frames()
        profile['title_anchor']['origin_type'] = 'ink'
        title = current['regions']['header'][0]
        title['x'] = 309 / 900
        title['ink_box'] = {'x': 315 / 900, 'y': 14 / 700, 'w': .2, 'h': .03}
        self.driver._bridge = Mock(side_effect=[current, proof, current])
        self.visual.relocate('测试群', profile)
        identity = self.driver._bridge.call_args_list[1].kwargs['header_identity']
        self.assertAlmostEqual(identity['origin'][0], 315*2-6)
        area = self.visual._layout_region(current, title, current['regions']['lower'][0])
        self.assertAlmostEqual(area['x'], 315 / 900)

    def test_failed_composer_probe_exposes_only_numeric_layout_evidence(self):
        data = json.loads((Path(__file__).parent / 'fixtures/editor-layout-4.1.15.json').read_text())
        rows = [[row[0], 250, 250, 250] for row in data['rows']]
        scan = {**data, 'snapshots': {'composer': {'row_profile': rows}},
                'regions': {'composer': [{'text': 'private existing draft'}]}}
        self.driver._bridge = Mock(return_value=scan)
        with self.assertRaises(AppError) as caught:
            self.visual.measure_composer(data, data['title'], data['send'], diagnostic=True)
        self.assertEqual(caught.exception.layout_probe['layout_rows'], rows)
        self.assertNotIn('private existing draft', json.dumps(caught.exception.layout_probe))

    def test_resize_with_similar_ocr_but_different_title_pixels_never_accepts_new_layout(self):
        profile, current, proof, _ = self.resize_frames()
        proof['header_match'] = {'matched': False}
        self.driver._bridge = Mock(side_effect=[current, proof])
        with self.assertRaises(AppError) as caught:
            self.visual.relocate('测试群', profile)
        self.assertEqual(caught.exception.code, 'TITLE_CHANGED')
        self.visual.measure_composer.assert_not_called()
        self.assertFalse(self.visual.path.exists())

    def test_resize_during_read_only_location_never_accepts_coordinates(self):
        profile, current, proof, _ = self.resize_frames()
        proof = {**proof, 'bounds': {**proof['bounds'], 'Width': 901}}
        self.driver._bridge = Mock(side_effect=[current, proof])
        with self.assertRaises(AppError) as caught:
            self.visual.relocate('测试群', profile)
        self.assertEqual(caught.exception.code, 'WINDOW_CHANGED')

    def test_capture_scale_change_requires_new_title_confirmation(self):
        profile, current, _, _ = self.resize_frames()
        current['image_size']['width'] = 900
        self.driver._bridge = Mock(return_value=current)
        with self.assertRaises(AppError) as caught:
            self.visual.relocate('测试群', profile)
        self.assertEqual(caught.exception.code, 'TITLE_SCALE_CHANGED')
        self.driver._bridge.assert_called_once()

    def test_existing_draft_after_resize_still_prevents_any_binding_write(self):
        profile, current, proof, _ = self.resize_frames()
        current['regions']['composer'] = [row('保留原有草稿')]
        self.driver._bridge = Mock(side_effect=[current, proof, current])
        with self.assertRaises(AppError) as caught:
            self.visual.bind('测试群', profile, True)
        self.assertEqual(caught.exception.code, 'EXISTING_DRAFT')
        self.assertEqual([call.args for call in self.driver._bridge.call_args_list], [('capture',)]*3)
        self.assertFalse(self.visual.path.exists())

    def test_send_after_mid_transaction_resize_is_never_relocated_or_clicked(self):
        self.visual.prepared['task'] = (self.profile, 'draft')
        self.driver._bridge = Mock(return_value={**self.capture, 'bounds': {'Width': 900, 'Height': 700}})
        self.visual.relocate = Mock()
        with self.assertRaises(AppError) as caught:
            self.visual.send({'id': 'task'})
        self.assertEqual(caught.exception.code, 'WINDOW_LAYOUT_CHANGED')
        self.visual.relocate.assert_not_called()
        self.driver._bridge.assert_called_once()

    def test_send_button_in_a_wide_window_is_not_limited_to_the_rightmost_thirty_percent(self):
        send = row('发送', .9, .6)
        self.assertEqual(self.visual._send_button({'regions': {'lower': [send]}}), send)

    def test_search_magnifier_and_label_are_one_field_after_resize(self):
        icon = {'text': 'Q', 'x': 81/1075, 'y': 23/640, 'w': 13/1075, 'h': 11/640}
        label = {'text': '，搜索', 'x': 91/1075, 'y': 21/640, 'w': 32/1075, 'h': 14/640}
        frame = {'bounds': {'Width': 1075, 'Height': 640}, 'regions': {'sidebar': [icon, label]}}
        self.assertEqual(self.visual._search_point(frame), self.visual._center(label))
        frame['regions']['sidebar'] = [icon, label, {**label, 'y': .08}]
        with self.assertRaises(AppError):
            self.visual._search_point(frame)

    def test_existing_draft_is_preserved(self):
        occupied = {**self.capture, 'snapshots': {'header': {'hash': 'correct'}, 'composer': {'blank': False}},
                    'regions': {'composer': [row('已有草稿', .72, .3)]}}
        self.visual.open_group = Mock(return_value=(self.profile, self.capture))
        self.visual._view = Mock(return_value=occupied)
        self.visual._act = Mock()
        with self.assertRaises(AppError) as error:
            self.visual.prepare({'id': 'a'*32, 'group_name': '测试群', 'message': '通知'})
        self.assertEqual(error.exception.code, 'EXISTING_DRAFT')
        self.visual._act.assert_not_called()

    def test_editor_region_includes_first_draft_line_and_excludes_scrollbar(self):
        data = json.loads((Path(__file__).parent / 'fixtures/editor-layout-4.1.15.json').read_text())
        rect = self.visual._composer_from_rows(data, data['title'], data['send'], data['rows'])
        draft = data['draft_box']
        # Native CGImage cropping rounds the left edge down to the pixel grid.
        self.assertLessEqual(math.floor(rect['x'] * data['image_size']['width']) / data['image_size']['width'], draft['x'] + 1e-6)
        self.assertLessEqual(rect['y'], draft['y'])
        self.assertGreaterEqual(rect['x'] + rect['w'], draft['x'] + draft['w'])
        self.assertGreaterEqual(rect['y'] + rect['h'], draft['y'] + draft['h'])
        self.assertLess(rect['x'] + rect['w'], data['scrollbar_x'])

    def test_missing_input_boundary_stops_without_guessing_a_bottom_strip(self):
        data = json.loads((Path(__file__).parent / 'fixtures/editor-layout-4.1.15.json').read_text())
        rows = [[row[0], 250, 250, 250] for row in data['rows']]
        with self.assertRaises(AppError) as error:
            self.visual._composer_from_rows(data, data['title'], data['send'], rows)
        self.assertEqual(error.exception.code, 'COMPOSER_LAYOUT_UNKNOWN')

    def test_pixel_only_content_is_uncertain_and_never_cleared(self):
        capture = {**self.capture, 'snapshots': {'header': {'hash': 'correct'}, 'composer': {'blank': False}}}
        self.visual.capture = Mock(return_value=capture)
        self.visual._act = Mock()
        with self.assertRaises(AppError) as error:
            self.visual.bind('测试群', self.profile, True)
        self.assertEqual(error.exception.code, 'COMPOSER_REGION_UNCERTAIN')
        self.visual._act.assert_not_called()

    def test_recognized_draft_prevents_binding_before_any_write(self):
        capture = {**self.capture, 'regions': {'composer': [row('保留草稿', .72, .3)]}}
        self.visual.capture = Mock(return_value=capture)
        self.visual._act = Mock()
        with self.assertRaises(AppError) as error:
            self.visual.bind('测试群', self.profile, True)
        self.assertEqual(error.exception.code, 'EXISTING_DRAFT')
        self.visual._act.assert_not_called()

    def test_marker_recovery_does_not_write_or_send_again(self):
        self.visual.open_group = Mock(return_value=(self.profile, {**self.capture, 'outgoing_marker': True}))
        self.visual._act = Mock()
        self.assertEqual(self.visual.prepare({'id': 'a'*32, 'group_name': '测试群'}), 'already_observed')
        self.visual._act.assert_not_called()

    def test_full_text_verification_covers_overlapping_pages(self):
        pages = [{'regions': {'composer': [row(text)]}} for text in ('ABCD', 'CDEF', 'EFGH')]
        self.visual._view = Mock(side_effect=pages)
        self.assertEqual(self.visual._read_expected({}, self.profile, 'ABCDEFGH'), pages[-1])

    def test_missing_middle_or_changed_character_never_passes(self):
        for text in ('EFGH', 'ABXD'):
            self.visual._view = Mock(return_value={'regions': {'composer': [row(text)]}})
            with self.assertRaises(AppError) as error:
                self.visual._read_expected({}, self.profile, 'ABCDEFGH')
            self.assertEqual(error.exception.code, 'DRAFT_MISMATCH')

    def test_stalled_scroll_has_bounded_failure(self):
        self.visual._view = Mock(return_value={'regions': {'composer': [row('ABCD')]}})
        with self.assertRaises(AppError):
            self.visual._read_expected({}, self.profile, 'ABCDEFGH')
        self.assertEqual(self.visual._view.call_count, 2)

    def test_draft_change_prevents_send_button_click(self):
        self.visual.prepared['task'] = (self.profile, 'before')
        self.visual.capture = Mock(return_value=self.capture)
        self.visual._act = Mock()
        with self.assertRaises(AppError) as error:
            self.visual.send({'id': 'task'})
        self.assertEqual(error.exception.code, 'DRAFT_CHANGED')
        self.visual._act.assert_not_called()

    def test_binding_keeps_private_profile_on_server_and_requires_confirmation(self):
        self.driver.search = Mock(return_value=[{'group_name': '测试群', 'preview': 'local-title-image',
                                                 'requires_visual_confirmation': True,
                                                 '_visual_profile': self.profile}])
        controller = Controller(self.driver)
        candidate = controller.search('测试群')[0]
        self.assertNotIn('_visual_profile', candidate)
        self.assertEqual(candidate['preview'], 'local-title-image')
        store = Store(self.temp.name)
        with self.assertRaises(AppError) as error:
            controller.bind(store, Bind(candidate_id=candidate['candidate_id'], code='test', label='测试群'))
        self.assertEqual(error.exception.code, 'VISUAL_CONFIRMATION_REQUIRED')
        self.assertEqual(store.targets(), [])

    def test_reading_order_joins_split_lines_left_to_right(self):
        self.assertEqual(reading_order([row('C', .3), row('B', .2, .3), row('A', .201, .1)]), 'A B\nC')

    def test_search_cannot_inject_return_or_control_characters(self):
        for query in ('测试\n群', '测试\t群', '测试\x00群'):
            with self.assertRaises(ValueError):
                Search(query=query)

    def test_current_complete_group_title_avoids_search_mutation(self):
        title = row('测试通知群义（13）', .03, .3)
        capture = {**self.capture, 'regions': {'header': [title], 'sidebar': [row('搜索', .03)]}}
        self.visual.capture = Mock(return_value=capture)
        self.visual._act = Mock()
        opened, selected, point = self.visual._search_open('测试通知群✅')
        self.assertIs(opened, capture)
        self.assertEqual(selected, title)
        self.visual._act.assert_not_called()


if __name__ == '__main__':
    unittest.main()
