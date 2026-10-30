"""Action-to-detail-camera mapping tests; no scene execution is implied."""
import unittest

from action_camera import focus_key


class ActionCameraTests(unittest.TestCase):
    def test_tip_loading_focuses_on_rack_for_stock_and_water(self):
        for phase, material in (("load_tip/dock", "copper_stock"),
                                ("load_tip_water/dock", "water")):
            with self.subTest(phase=phase):
                self.assertEqual(focus_key(phase, {"action": "load_tip", "material_id": material}),
                                 "tip_rack")

    def test_aspiration_and_dispense_focus_on_their_actual_source_and_target(self):
        self.assertEqual(focus_key("aspirate_water/position", {
            "action": "aspirate", "material_id": "water", "source": "water"}), "water")
        self.assertEqual(focus_key("dispense_water/volume_transfer", {
            "action": "dispense", "material_id": "water", "target": "left_half_cell"}),
            "left_half_cell")

    def test_tip_disposal_and_completed_frame_focus_on_waste(self):
        command = {"action": "discard_tip", "material_id": "water"}
        self.assertEqual(focus_key("discard_tip_water/fall", command), "waste")
        self.assertEqual(focus_key("process_completed", completed=command), "waste")

    def test_electrode_installation_focuses_the_targeted_side(self):
        self.assertEqual(focus_key("place_electrode/align_and_insert", {
            "action": "place_electrode", "target": "left_half_cell"}),
            "left_electrode_installation")
        self.assertEqual(focus_key("place_electrode/align_and_insert", {
            "action": "place_electrode", "target": "right_half_cell"}),
            "right_electrode_installation")


if __name__ == "__main__":
    unittest.main()
