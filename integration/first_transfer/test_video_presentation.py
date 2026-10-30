"""Video display tests; no GPU execution or device accuracy is implied."""
import unittest
from unittest.mock import patch
from PIL import Image
from video_presentation import Presentation, completed_message, project_point
from workflow_display import workflow_progress


class PresentationTests(unittest.TestCase):
    def test_scene_names_replace_numbers_and_nearby_cells_do_not_overlap(self):
        from PIL import ImageDraw
        from action_first_panel import draw_object_names
        draw = ImageDraw.Draw(Image.new('RGB', (1440, 1440)))
        labels = []
        original = draw.text
        def record(xy, text, *args, **kwargs):
            labels.append(text)
            return original(xy, text, *args, **kwargs)
        draw.text = record
        boxes = draw_object_names(draw, {
            'copper_stock': (700, 350), 'water': (500, 350),
            'left_half_cell': (700, 600), 'right_half_cell': (720, 600),
            'tip': (350, 350), 'waste': (400, 800)}, chinese=True,
            font=Presentation(1920, 1440).small, scene_w=1440, scene_h=1230, header=90)
        self.assertEqual(set(labels), {'母液', '水源', '左池', '右池', '移液器', '弃头盒'})
        self.assertEqual(len(boxes), 6)
        for i,a in enumerate(boxes):
            for b in boxes[i+1:]:
                self.assertTrue(a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    def test_pending_is_not_completion(self):
        self.assertIn('尚无已确认', completed_message(None))

    def test_dispense_uses_actual_not_plan_and_converts_ml(self):
        command = {'action': 'dispense', 'requested_volume_ul': 100,
                   'actual_volume_ul': 100.316215}
        self.assertIn('0.100316 mL', completed_message(command))
        self.assertIn('模型执行量', completed_message(command))

    def test_load_tip_has_no_fake_liquid_amount(self):
        self.assertNotIn('mL', completed_message({'action': 'load_tip'}))

    def test_tip_loading_message_names_the_selected_pipette_and_tip(self):
        message = completed_message({
            'action': 'load_tip', 'material_id': 'water',
            'pipette_id': 'gilson_pipetman_p1000',
            'tip_id': 'PIPETMAN_DIAMOND_D1200'})
        self.assertIn('P1000 / D1200', message)
        self.assertIn('参考型号', message)

    def test_projection_center_and_behind_camera(self):
        self.assertEqual(project_point((0, 0, 0), (1, 0, 1), (0, 0, 0),
                                       1920, 1080, 24, 36), (960, 540))
        self.assertIsNone(project_point((2, 0, 2), (1, 0, 1), (0, 0, 0),
                                        1920, 1080, 24, 36))

    def test_left_cell_projects_to_screen_left_of_right_cell(self):
        eye, target = (.45, -1.35, 1.10), (.45, .03, .22)
        left = project_point((.55, -.16, .045), eye, target, 1440, 1230, 24, 36)
        right = project_point((.59, -.16, .045), eye, target, 1440, 1230, 24, 36)
        self.assertLess(left[0], right[0])

    def test_rear_mixing_camera_up_vector_preserves_left_right(self):
        import numpy as np
        from mixer_presentation import mixing_camera_spec
        from microcell_labware import CELLS
        focus,distance,vertical,offset = mixing_camera_spec(
            {name:cell.at(None,1500) for name,cell in CELLS.items()})
        eye=np.array(focus)+[offset,-distance,vertical]
        forward=np.array(focus)-eye
        right=np.cross(forward,[0,1,0])
        self.assertGreater(right[0],0)

    def test_hd_frame_dimensions_preserved(self):
        frame = Image.new('RGB', (1920, 1080), '#666666')
        p = Presentation(1920, 1080)
        result = p.draw(frame, run_id='a'*32, phase='dispense/volume_transfer',
                        sim_time=3, volumes={'copper_stock': 4900, 'tip': 1,
                                            'left_half_cell': 100},
                        stock_concentration=.1, completed=None, planned_ul=100,
                        anchors={'copper_stock': (650, 520), 'tip': (1000, 420),
                                 'left_half_cell': (1200, 700)}, view='close')
        self.assertEqual(result.size, (1920, 1080))

    def test_sidebar_layout_does_not_cover_scene_with_boxes(self):
        p = Presentation(1920, 1440)
        scene = Image.new('RGB', (1440, 1230), '#658291')
        result = p.compose_overview(scene, run_id='b'*32, phase='dispense/position',
                    sim_time=9, volumes={'copper_stock':4900, 'left_half_cell':0, 'tip':100},
                    stock_concentration=.1, completed=None, planned_ul=100,
                    anchors={'tip':(650,600), 'left_half_cell':(680,780), 'copper_stock':(500,600)})
        self.assertEqual(result.size, (1920, 1440))
        # Former box region remains scene-colored except for tiny object numbers.
        self.assertEqual(result.getpixel((720,780)), (101,130,145))
        self.assertEqual(result.getpixel((100,1250)), (101,130,145))

    def test_paired_mixing_frame_supports_both_liquid_cards_and_shared_receipt(self):
        command = {'action': 'mix_pair', 'mixing_efficiencies':
                   {'left_half_cell': 0.985, 'right_half_cell': 0.989}}
        self.assertIn('98.5%', completed_message(command))
        self.assertIn('98.9%', completed_message(command))
        p = Presentation(1920, 1440)
        result = p.compose_overview(
            Image.new('RGB', (1440, 1230), '#658291'), run_id='c'*32,
            phase='process_completed', sim_time=120,
            volumes={'copper_stock': 3900, 'water': 18000,
                     'left_half_cell': 1995, 'right_half_cell': 999,
                     'tip': 0, 'waste': 0.5},
            stock_concentration=.1, completed=command, command=command,
            planned_ul=None, anchors={},
            detail_scene=Image.new('RGB', (480, 480), '#abcdef'))
        self.assertEqual(result.size, (1920, 1440))
        self.assertEqual(result.getpixel((100,1250)), (101,130,145))

    def test_six_value_cards_keep_title_and_volume_inside_their_borders(self):
        volumes = {'copper_stock': 4900, 'water': 9100, 'left_half_cell': 1000,
                   'tip': 0.12, 'waste': 0.12, 'right_half_cell': 1000}
        for height in (1080, 1440):
            with self.subTest(height=height):
                p = Presentation(1920, height)
                result = p.compose_overview(
                    Image.new('RGB', (1440, height - 210), '#658291'),
                    run_id='d'*32, phase='dispense/position', sim_time=8,
                    volumes=volumes, stock_concentration=.1, completed=None,
                    planned_ul=100, anchors={})
                self.assertEqual(result.size, (1920, height))

    def test_right_electrode_message_identifies_the_right_cell(self):
        message = completed_message({'action': 'place_electrode',
                                     'target': 'right_half_cell'})
        self.assertIn('右侧铜电极', message)
        self.assertIn('盐桥仍待连接', message)

    def test_workflow_is_driven_by_real_plan_and_confirmations(self):
        plan = [{'step_id': 'left', 'action': 'dispense'},
                {'step_id': 'right', 'action': 'dispense'},
                {'step_id': 'mix', 'action': 'mix_pair'},
                {'step_id': 'electrode', 'action': 'place_electrode'}]
        rows = workflow_progress(plan, 1, 'right')
        self.assertEqual(rows[0]['state'], 'current')
        self.assertEqual(rows[1]['state'], 'pending')
        rows = workflow_progress(plan, 4)
        self.assertEqual([row['state'] for row in rows],
                         ['done', 'done', 'done', 'future', 'future'])

    def test_panel_uses_confirmed_ledger_not_animated_volume(self):
        from PIL import ImageDraw
        labels = []
        original = ImageDraw.ImageDraw.text
        def record(draw, xy, text, *args, **kwargs):
            labels.append(text)
            return original(draw, xy, text, *args, **kwargs)
        command = {'action': 'dispense', 'step_id': 'left', 'target': 'left_half_cell'}
        with patch.object(ImageDraw.ImageDraw, 'text', record):
            Presentation(1920, 1440).compose_overview(
                Image.new('RGB', (1440, 1230), '#658291'), run_id='a'*32,
                phase='dispense/volume_transfer', sim_time=1,
                volumes={'left_half_cell': 999}, stock_concentration=.1,
                completed=None, planned_ul=100, anchors={}, command=command,
                plan=[command], completed_steps=0,
                confirmed_volumes={'left_half_cell': 100},
                cell_concentrations={'left_half_cell': .005})
        self.assertTrue(any('0.1000 mL' in label for label in labels))
        self.assertFalse(any('0.9990 mL' in label for label in labels))
        self.assertTrue(any('—' in label for label in labels))

    def test_supported_resolutions_fit_action_first_sidebar(self):
        command = {'action': 'place_electrode', 'target': 'right_half_cell', 'step_id': 'right'}
        for width, height in ((1280, 720), (1920, 1080), (1920, 1440)):
            with self.subTest(width=width, height=height):
                result = Presentation(width, height).compose_overview(
                    Image.new('RGB', (width * 3 // 4, height - 210)),
                    run_id='a'*32, phase='place_electrode/align_and_insert',
                    sim_time=10, volumes={}, stock_concentration=.1,
                    completed=command, command=command, planned_ul=None, anchors={},
                    plan=[command], completed_steps=0,
                    confirmed_volumes={'left_half_cell': 1996, 'right_half_cell': 999},
                    cell_concentrations={'left_half_cell': .00498, 'right_half_cell': .1})
                self.assertEqual(result.size, (width, height))

    def test_salt_bridge_caption_targets_both_cells(self):
        from PIL import ImageDraw
        labels = []
        original = ImageDraw.ImageDraw.text
        def record(draw, xy, text, *args, **kwargs):
            labels.append(text)
            return original(draw, xy, text, *args, **kwargs)
        command = {'action': 'connect_salt_bridge', 'target': 'salt_bridge_holder', 'step_id': 'bridge'}
        with patch.object(ImageDraw.ImageDraw, 'text', record):
            Presentation(1920, 1440).compose_overview(
                Image.new('RGB', (1440, 1230)), run_id='a'*32,
                phase='connect_salt_bridge/align_and_insert', sim_time=1,
                volumes={}, stock_concentration=.1, completed=None,
                planned_ul=None, anchors={}, command=command, plan=[command])
        self.assertIn('双端对准池口并插入', labels)
        self.assertNotIn('沿左侧定位插入', labels)


if __name__ == '__main__':
    unittest.main()
