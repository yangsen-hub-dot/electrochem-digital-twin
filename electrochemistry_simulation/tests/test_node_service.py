"""核验节点脚本的只读查询接口与网页条件仿真接口。"""

import importlib.util
import json
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from electrochem_sim.simulator import load_config

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("copper_cell_node_under_test", ROOT / "copper_cell_node.py")
NODE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NODE)
CONFIG_PATH = ROOT / "electrochemistry_simulation" / "config" / "copper_concentration_cell.json"


@contextmanager
def running_server(service):
    server = NODE.create_server("127.0.0.1", 0, service)
    server.RequestHandlerClass.log_message = lambda *args: None
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class NodeServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG_PATH)
        self.opener = build_opener(ProxyHandler({}))

    def fetch(self, url):
        with self.opener.open(url, timeout=5) as response:
            return response.status, dict(response.headers), json.load(response)

    def test_baseline_legacy_fields(self):
        service = NODE.NodeService(self.config)
        data = service.state_payload()
        self.assertEqual(data["mode"], "protocol_simulation")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["completed_steps"], 18)
        self.assertEqual(data["total_steps"], 18)
        self.assertAlmostEqual(data["voltage_mv"], 22.78883899493854)
        self.assertAlmostEqual(data["c_low_m"], 0.01)
        self.assertAlmostEqual(data["c_high_m"], 0.1)
        self.assertAlmostEqual(data["volume_low_ml"], 1)
        self.assertAlmostEqual(data["volume_high_ml"], 1)
        self.assertTrue(data["conservation"]["passed"])
        self.assertAlmostEqual(data["local_voltage_range_mv"]["minimum_mv"], 22.78883899493854)
        self.assertAlmostEqual(data["local_voltage_range_mv"]["maximum_mv"], 22.78883899493854)
        self.assertEqual(data["mixing"]["left_uniformity"], 1.0)
        self.assertEqual(data["mixing"]["right_uniformity"], 1.0)
        self.assertEqual(data["activity_correction"]["status"], "estimated")
        self.assertEqual(data["liquid_junction_potential"]["status"], "not_modeled")
        self.assertTrue(data["voltage_components_mv"]["reported_without_liquid_junction"])
        self.assertEqual(data["salt_bridge"]["type"], "KNO3")
        self.assertAlmostEqual(data["salt_bridge"]["concentration_m"], 0.1)
        self.assertEqual(data["salt_bridge"]["electrolytes"][0]["formula"], "KNO3")
        self.assertFalse(data["salt_bridge"]["liquid_junction_enabled"])
        self.assertAlmostEqual(data["condition_editor"]["current"]["left_concentration_m"], 0.01)
        self.assertEqual(data["condition_editor"]["current"]["salt_bridge_type"], "KNO3")
        self.assertEqual(
            data["condition_editor"]["current"]["operation_error_levels"],
            {"pipetting": "low", "tip_carryover": "low", "mixing": "low"},
        )
        self.assertEqual(
            data["condition_editor"]["measurable_ranges"]["temperature_c"]["activity_reference_range"],
            [0.0, 50.0],
        )
        self.assertEqual(
            data["condition_editor"]["operation_error_profile"]["tested_volume_bands_ul"],
            [],
        )
        pipette_profiles = data["condition_editor"]["operation_error_profile"]["pipetting"]["low"]["pipette_profiles"]
        self.assertEqual([profile["pipette_id"] for profile in pipette_profiles], ["gilson_pipetman_p200", "gilson_pipetman_p1000"])

    def test_changed_config_is_not_hardcoded(self):
        self.config["temperature_k"] = 310
        self.config["protocol"][1]["volume_ul"] = 200
        self.config["cell"]["activity_correction"]["enabled"] = False
        service = NODE.NodeService(self.config)
        data = service.state_payload()
        self.assertEqual(data["temperature_k"], 310)
        self.assertAlmostEqual(data["volume_low_ml"], 1.1)
        self.assertAlmostEqual(data["c_low_m"], 0.1 * 200 / 1100)
        self.assertNotAlmostEqual(data["voltage_mv"], 29.579674842860754)

    def test_state_exposes_configured_species_for_generic_visualization(self):
        self.config["species"]["Zn"] = self.config["species"].pop("Cu")
        self.config["species"]["Zn"].update(name="Zn²⁺", formula="Zn")
        for vessel in self.config["containers"].values():
            concentrations = vessel["concentrations_m"]
            if "Cu" in concentrations:
                concentrations["Zn"] = concentrations.pop("Cu")
        self.config["cell"]["active_species"] = "Zn"
        self.config["cell"]["redox_reaction"].update(
            name_zh="Zn²⁺/Zn", reduction_reaction_zh="Zn²⁺ + 2e⁻ → Zn(s)",
            display_species="Zn", reaction_quotient_terms={"Zn": -1},
        )
        self.config["cell"]["activity_correction"]["ionic_strength_species"] = ["Zn", "SO4"]
        service = NODE.NodeService(self.config)
        data = service.state_payload()
        self.assertEqual(data["active_species_name"], "Zn²⁺")
        self.assertEqual(data["active_species_formula"], "Zn")
        self.assertEqual(data["reaction_quotient_terms"], {"Zn": -1.0})
        self.assertEqual(data["redox_couple_name_zh"], "Zn²⁺/Zn")

    def test_state_exposes_salt_bridge_used_by_liquid_junction_model(self):
        config = load_config(CONFIG_PATH.parent / "copper_concentration_cell_ljp_reference_test.json")
        data = NODE.NodeService(config).state_payload()
        self.assertEqual(data["salt_bridge"]["type"], "KNO3")
        self.assertAlmostEqual(data["salt_bridge"]["concentration_m"], 0.1)
        self.assertEqual(data["salt_bridge"]["electrolytes"][0]["formula"], "KNO3")
        self.assertTrue(data["salt_bridge"]["liquid_junction_enabled"])
        self.assertEqual(data["salt_bridge"]["liquid_junction_model"], "henderson_approximation")
        editor = data["condition_editor"]
        self.assertEqual(editor["current"]["liquid_junction_model"], "henderson_approximation")
        self.assertIn("henderson_approximation", editor["liquid_junction_model_options"])
        self.assertIn(
            "nernst_planck_zero_current_linear_profile",
            editor["liquid_junction_model_options"],
        )

    def test_node_serves_non_copper_concentration_cell_metadata(self):
        zinc_config = load_config(CONFIG_PATH.parent / "zinc_concentration_cell.json")
        service = NODE.NodeService(zinc_config)
        data = service.state_payload()
        self.assertEqual(service.health_payload()["service"], "modular_electrochemistry_node")
        self.assertEqual(data["active_species_name"], "Zn²⁺")
        self.assertEqual(data["redox_couple_name_zh"], "Zn²⁺/Zn")
        self.assertEqual(data["container_names"]["zinc_stock"], "锌盐母液")

    def test_error_configuration_is_used(self):
        legacy_path = CONFIG_PATH.parent / "error_profiles" / "copper_cell_aqueous_100_500_900ul_reference_v2.json"
        legacy_catalog = json.loads(legacy_path.read_text(encoding="utf-8"))
        pipetting = self.config["error_model"]["factors"]["pipetting"]
        pipetting["model"] = "shared_bounded_bias_gaussian_repeatability"
        pipetting["parameters"] = legacy_catalog["models"]["pipetting"]["shared_bounded_bias_gaussian_repeatability"]["low"]
        self.config["error_model"].update(enabled=True, source_proportional_bias={"copper_stock": -0.02})
        data = NODE.NodeService(self.config).state_payload()
        self.assertTrue(data["errors_enabled"])
        self.assertAlmostEqual(data["volume_low_ml"], 0.998)
        self.assertAlmostEqual(data["voltage_mv"], 22.959796079272067)

    def test_comparison_reports_error_draws_and_no_error_reference(self):
        self.config["error_model"]["enabled"] = True
        service = NODE.NodeService(self.config, seed=17)
        data = service.comparison_payload()
        self.assertTrue(data["errors_enabled"])
        self.assertFalse(data["baseline_is_current_run"])
        self.assertTrue(data["baseline"]["valid"])
        self.assertAlmostEqual(data["baseline"]["voltage_mv"], 22.78883899493854)
        self.assertTrue(data["actual"]["valid"])
        self.assertEqual(len(data["events"]["aspirations"]), 4)
        self.assertEqual(len(data["events"]["dispenses"]), 4)
        self.assertEqual(len(data["events"]["mixing"]), 2)
        self.assertIsNotNone(data["operation_errors"]["tip_carryover"]["sampled_residual_volume_ul"])
        self.assertIsNotNone(data["operation_errors"]["mixing"]["sampled_efficiency_per_action"])
        self.assertIsNotNone(data["events"]["aspirations"][0]["simulated_volume_ul"])
        with running_server(service) as base:
            _, _, http_data = self.fetch(base + "/comparison")
            self.assertEqual(http_data["run_id"], data["run_id"])
            self.assertEqual(len(http_data["events"]["aspirations"]), 4)

    def test_queries_do_not_rerun_or_create_extra_results(self):
        with TemporaryDirectory() as directory:
            service = NODE.NodeService(self.config, seed=12, output_dir=Path(directory))
            first = service.state_payload()
            second = service.state_payload()
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(first["completed_steps"], second["completed_steps"])
            self.assertEqual(len(list(Path(directory).rglob("单_*.json"))), 1)
            self.assertTrue(service.health_payload()["results_saved"])
            with service.result_path.open(encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["run_id"], first["run_id"])

    def test_history_lists_saved_runs_with_comparison_fields(self):
        with TemporaryDirectory() as directory:
            service = NODE.NodeService(self.config, seed=12, output_dir=Path(directory))
            initial_run_id = service.result["run_id"]
            service.simulate_with_conditions({
                "left_concentration_m": 0.005,
                "right_concentration_m": 0.1,
                "left_volume_ml": 1.0,
                "right_volume_ml": 1.0,
                "temperature_c": 30.0,
                "salt_bridge_type": "KNO3",
                "salt_bridge_concentration_m": 0.1,
                "activity_correction_enabled": True,
                "liquid_junction_enabled": False,
                "errors_enabled": True,
                "operation_error_levels": {
                    "pipetting": "medium", "tip_carryover": "low", "mixing": "high",
                },
            })
            payload = service.history_payload()
            self.assertEqual(payload["count"], 2)
            by_id = {record["run_id"]: record for record in payload["records"]}
            self.assertEqual(set(by_id), {initial_run_id, service.result["run_id"]})
            latest = by_id[service.result["run_id"]]
            self.assertAlmostEqual(latest["target_c_left_m"], 0.005)
            self.assertAlmostEqual(latest["c_left_m"], service.result["prediction"]["c_left_m"])
            self.assertAlmostEqual(latest["temperature_c"], 30.0)
            self.assertEqual(latest["salt_bridge_type"], "KNO3")
            self.assertTrue(latest["activity_enabled"])
            self.assertTrue(latest["errors_enabled"])
            self.assertEqual(latest["error_levels"]["mixing"], "high")
            self.assertTrue(latest["conservation_passed"])
            self.assertFalse(latest["liquid_junction_enabled"])
            self.assertIsNone(latest["liquid_junction_henderson_mv"])
            with running_server(service) as base:
                _, _, http_data = self.fetch(base + "/history?cache_bust=1")
                self.assertEqual(http_data["count"], 2)
                self.assertIn("voltage_mv", http_data["records"][0])
                self.assertIn("liquid_junction_model_options", http_data)

    def test_history_recovers_nominal_targets_when_legacy_inputs_are_partial(self):
        service = NODE.NodeService(self.config, seed=12)
        legacy_config = service.result["config"]
        legacy_config["cell"]["copper_species"] = legacy_config["cell"].pop("active_species")
        legacy_config["condition_inputs"] = {"temperature_c": 30.0}

        record = NODE.NodeService._history_record_summary(service.result, "legacy.json")

        self.assertAlmostEqual(record["target_c_left_m"], 0.01)
        self.assertAlmostEqual(record["target_c_right_m"], 0.1)
        self.assertAlmostEqual(record["target_left_volume_ml"], 1.0)
        self.assertAlmostEqual(record["target_right_volume_ml"], 1.0)
        self.assertAlmostEqual(record["temperature_c"], 30.0)

    def test_history_exposes_liquid_junction_model_values_and_scope(self):
        config = load_config(CONFIG_PATH.parent / "copper_concentration_cell_ljp_reference_test.json")
        with TemporaryDirectory() as directory:
            service = NODE.NodeService(config, seed=12, output_dir=Path(directory))
            junction = service.result["prediction"]["liquid_junction_potential"]
            comparison = junction["model_comparison"]
            payload = service.history_payload()
            record = payload["records"][0]

            self.assertEqual(record["liquid_junction_model"], "henderson_approximation")
            self.assertEqual(record["liquid_junction_model_name_zh"], "Henderson 近似")
            self.assertAlmostEqual(
                record["liquid_junction_henderson_mv"],
                comparison["henderson_approximation"]["correction_mv"],
            )
            self.assertAlmostEqual(
                record["liquid_junction_nernst_planck_mv"],
                comparison["nernst_planck_zero_current_linear_profile"]["correction_mv"],
            )
            self.assertAlmostEqual(
                record["liquid_junction_model_difference_mv"],
                comparison["difference_nernst_planck_minus_henderson_mv"],
            )
            self.assertIn("scope_zh", payload["liquid_junction_model_options"]["henderson_approximation"])
            self.assertIn(
                "scope_zh",
                payload["liquid_junction_model_options"]["nernst_planck_zero_current_linear_profile"],
            )

    def test_web_conditions_create_a_new_result_without_mutating_source_config(self):
        with TemporaryDirectory() as directory:
            original = json.loads(json.dumps(self.config))
            service = NODE.NodeService(self.config, seed=12, output_dir=Path(directory))
            original_run_id = service.result["run_id"]
            payload = {
                "left_concentration_m": 0.005,
                "right_concentration_m": 0.1,
                "left_volume_ml": 1.0,
                "right_volume_ml": 1.0,
                "temperature_c": 25.0,
                "salt_bridge_type": "KNO3",
                "salt_bridge_concentration_m": 0.1,
                "activity_correction_enabled": True,
                "liquid_junction_enabled": False,
                "errors_enabled": False,
                "operation_error_levels": {
                    "pipetting": "medium",
                    "tip_carryover": "high",
                    "mixing": "medium",
                },
            }
            state = service.simulate_with_conditions(payload)
            self.assertNotEqual(state["run_id"], original_run_id)
            self.assertAlmostEqual(state["c_left_m"], 0.005)
            self.assertAlmostEqual(state["c_right_m"], 0.1)
            self.assertTrue(state["conservation"]["passed"])
            self.assertEqual(state["condition_editor"]["current"]["left_concentration_m"], 0.005)
            self.assertEqual(state["condition_editor"]["current"]["operation_error_levels"], {
                "pipetting": "medium", "tip_carryover": "high", "mixing": "medium",
            })
            self.assertEqual(self.config, original)
            self.assertNotIn("condition_inputs", self.config)
            self.assertTrue(service.result_path.is_file())
            self.assertEqual(len(list(Path(directory).rglob("单_*.json"))), 2)

    def test_each_submitted_run_gets_a_new_reproducible_error_seed(self):
        self.config["error_model"]["enabled"] = True
        service = NODE.NodeService(self.config, seed=12)
        initial_seed = service.result["seed"]
        payload = {
            "left_concentration_m": 0.01,
            "right_concentration_m": 0.1,
            "left_volume_ml": 1.0,
            "right_volume_ml": 1.0,
            "temperature_c": 25.0,
            "salt_bridge_type": "KNO3",
            "salt_bridge_concentration_m": 0.1,
            "activity_correction_enabled": True,
            "liquid_junction_enabled": False,
            "errors_enabled": True,
        }
        service.simulate_with_conditions(payload)
        self.assertEqual(initial_seed, 12)
        self.assertEqual(service.result["seed"], 13)
        self.assertEqual(service.baseline_result["seed"], 13)

    def test_web_simulation_endpoint_requires_local_file_origin_and_valid_json(self):
        service = NODE.NodeService(self.config)
        payload = {
            "left_concentration_m": 0.01,
            "right_concentration_m": 0.1,
            "left_volume_ml": 1.0,
            "right_volume_ml": 1.0,
            "temperature_c": 25.0,
            "salt_bridge_type": "KNO3",
            "salt_bridge_concentration_m": 0.1,
            "activity_correction_enabled": True,
            "liquid_junction_enabled": False,
            "errors_enabled": False,
        }
        body = json.dumps(payload).encode("utf-8")
        with running_server(service) as base:
            request = Request(
                base + "/simulate", data=body,
                headers={"Content-Type": "application/json", "Origin": "null"},
                method="POST",
            )
            with self.opener.open(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                result = json.load(response)
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"]["status"], "completed")

            foreign_request = Request(
                base + "/simulate", data=body,
                headers={"Content-Type": "application/json", "Origin": "https://example.com"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as caught:
                self.opener.open(foreign_request, timeout=5)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()

            invalid_payload = dict(payload, left_volume_ml=0)
            invalid_request = Request(
                base + "/simulate", data=json.dumps(invalid_payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": "null"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as caught:
                self.opener.open(invalid_request, timeout=5)
            self.assertEqual(caught.exception.code, 422)
            self.assertIn("体积", json.load(caught.exception)["message"])

    def test_chinese_dated_batch_filename_is_discovered(self):
        with TemporaryDirectory() as directory:
            filename = "批_铜电池_左浓度扫描_143000_abcd1234.csv"
            date_dir = Path(directory) / "2026-10-12"
            date_dir.mkdir()
            path = date_dir / filename
            path.write_text(
                "scenario,target_c_left_m,actual_c_left_m,actual_c_right_m,left_volume_ml,"
                "right_volume_ml,temperature_k,voltage_mv,status,valid,conservation_passed,"
                "completed_steps,run_id,detail_file\n"
                "left_0.01M,0.01,0.01,0.1,1,1,298.15,29.58,completed,True,True,16,abcd1234,detail.json\n",
                encoding="utf-8-sig",
            )
            service = NODE.NodeService(self.config, output_dir=Path(directory))
            payload = service.sweep_payload()
            self.assertTrue(payload["available"])
            self.assertEqual(payload["file"], "2026-10-12/" + filename)
            self.assertEqual(payload["scenario_count"], 1)

    def test_temperature_concentration_batch_csv_is_exposed_to_visualization(self):
        with TemporaryDirectory() as directory:
            filename = "批_铜电池_温度浓度扫描_114905_abcd1234.csv"
            date_dir = Path(directory) / "2026-10-13"
            date_dir.mkdir()
            path = date_dir / filename
            path.write_text(
                "scenario,temperature_k,target_c_left_m,actual_c_left_m,target_c_right_m,actual_c_right_m,"
                "activity_correction_mv,activity_status,liquid_junction_enabled,liquid_junction_model_name_zh,"
                "liquid_junction_correction_mv,liquid_junction_status,liquid_junction_henderson_mv,"
                "liquid_junction_nernst_planck_mv,liquid_junction_model_difference_mv,"
                "final_theoretical_voltage_mv,prediction_valid,run_status,scenario_status,"
                "completed_steps,total_steps,conservation_passed\n"
                "case_20C,293.15,0.01,0.01,0.1,0.1,-6.2,estimated,True,Henderson,1.9,estimated_from_reference_parameters,1.9,1.9,0,25.7,True,completed,completed,18,18,True\n"
                "case_25C,298.15,0.01,0.01,0.1,0.1,-6.4,estimated,True,Henderson,2.0,estimated_from_reference_parameters,2.0,2.0,0,25.2,True,completed,completed,18,18,True\n",
                encoding="utf-8-sig",
            )
            service = NODE.NodeService(self.config, output_dir=Path(directory))
            payload = service.sweep_payload("temperature_concentration")

            self.assertTrue(payload["available"])
            self.assertEqual(payload["batch_type"], "temperature_concentration")
            self.assertEqual(payload["file"], "2026-10-13/" + filename)
            self.assertEqual(payload["scenario_count"], 2)
            self.assertEqual(payload["scenarios"][0]["temperature_k"], 293.15)
            self.assertEqual(payload["scenarios"][0]["prediction_valid"], True)
            self.assertAlmostEqual(payload["scenarios"][0]["final_theoretical_voltage_mv"], 25.7)

            with running_server(service) as base:
                _, _, temperature_data = self.fetch(base + "/sweep?type=temperature_concentration")
                _, _, default_data = self.fetch(base + "/sweep")
                self.assertEqual(temperature_data["batch_type"], "temperature_concentration")
                self.assertEqual(temperature_data["scenario_count"], 2)
                self.assertEqual(default_data["batch_type"], "concentration")

    def test_experimental_design_batch_csv_is_exposed_with_condition_fields(self):
        with TemporaryDirectory() as directory:
            filename = "批_铜电池_条件扫描_120000_abcd1234.csv"
            date_dir = Path(directory) / "2026-10-13"
            date_dir.mkdir()
            path = date_dir / filename
            path.write_text(
                "scenario,matrix_index,replicate,replicate_count,temperature_k,target_c_left_m,actual_c_left_m,"
                "target_c_right_m,actual_c_right_m,salt_bridge_type,salt_bridge_concentration_m,"
                "activity_correction_enabled,activity_correction_mv,liquid_junction_enabled,liquid_junction_model,"
                "liquid_junction_model_name_zh,liquid_junction_correction_mv,liquid_junction_henderson_mv,"
                "liquid_junction_nernst_planck_mv,errors_enabled,pipetting_level,tip_carryover_level,mixing_level,"
                "final_theoretical_voltage_mv,prediction_valid,run_status,scenario_status,completed_steps,total_steps,conservation_passed\n"
                "case_a,1,1,2,298.15,0.01,0.0101,0.1,0.1,KNO3,0.1,True,-6.2,False,henderson_approximation,Henderson,,1.2,1.2,True,low,medium,high,23.4,True,completed,completed,18,18,True\n",
                encoding="utf-8-sig",
            )
            service = NODE.NodeService(self.config, output_dir=Path(directory))
            payload = service.sweep_payload("experimental_design")

            self.assertTrue(payload["available"])
            self.assertEqual(payload["batch_type"], "experimental_design")
            self.assertEqual(payload["file"], "2026-10-13/" + filename)
            row = payload["scenarios"][0]
            self.assertEqual(row["activity_correction_enabled"], True)
            self.assertEqual(row["liquid_junction_enabled"], False)
            self.assertEqual(row["errors_enabled"], True)
            self.assertEqual(row["salt_bridge_type"], "KNO3")
            self.assertEqual(row["replicate"], 1)
            self.assertEqual(row["prediction_valid"], True)

            with running_server(service) as base:
                _, _, matrix_data = self.fetch(base + "/sweep?type=experimental_design")
                self.assertEqual(matrix_data["batch_type"], "experimental_design")
                self.assertEqual(matrix_data["scenario_count"], 1)

    def test_browser_can_start_and_poll_an_experimental_design_batch(self):
        with TemporaryDirectory() as directory:
            service = NODE.NodeService(self.config, output_dir=Path(directory))
            settings = {
                "temperatures_k": [298.15],
                "left_concentrations_m": [0.01],
                "salt_bridge_types": ["KNO3"],
                "salt_bridge_concentrations_m": [0.1],
                "activity_correction_enabled": [True],
                "liquid_junction_enabled": [False],
                "liquid_junction_models": ["henderson_approximation"],
                "errors_enabled": [False],
                "replicates": 1,
                "max_scenarios": 10,
            }
            with running_server(service) as base:
                request = Request(
                    base + "/batch/start",
                    data=json.dumps(settings).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": "null"},
                    method="POST",
                )
                with self.opener.open(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    started = json.load(response)
                job_id = started["job"]["job_id"]
                self.assertEqual(started["job"]["scenario_count"], 1)

                job = None
                for _ in range(100):
                    _, _, job = self.fetch(base + f"/batch/status?job_id={job_id}")
                    if job["status"] != "running":
                        break
                    time.sleep(0.03)
                self.assertEqual(job["status"], "completed", job["message"])
                _, _, result = self.fetch(base + "/sweep?type=experimental_design")
                self.assertTrue(result["available"])
                self.assertEqual(result["scenario_count"], 1)

    def test_batch_after_web_condition_input_uses_current_inputs_and_source_protocol(self):
        with TemporaryDirectory() as directory:
            service = NODE.NodeService(self.config, output_dir=Path(directory))
            with running_server(service) as base:
                condition = {
                    "left_concentration_m": 0.005,
                    "right_concentration_m": 0.1,
                    "left_volume_ml": 1.0,
                    "right_volume_ml": 1.0,
                    "temperature_c": 25.0,
                    "salt_bridge_type": "KNO3",
                    "salt_bridge_concentration_m": 0.1,
                    "activity_correction_enabled": True,
                    "liquid_junction_enabled": False,
                    "errors_enabled": False,
                    "operation_error_levels": {
                        "pipetting": "medium", "tip_carryover": "low", "mixing": "high",
                    },
                }
                simulate_request = Request(
                    base + "/simulate",
                    data=json.dumps(condition).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": "null"},
                    method="POST",
                )
                with self.opener.open(simulate_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    single_run = json.load(response)["state"]
                self.assertTrue(single_run["valid"])

                settings = {"temperatures_k": [298.15, 303.15], "replicates": 1, "max_scenarios": 5}
                batch_request = Request(
                    base + "/batch/start",
                    data=json.dumps(settings).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": "null"},
                    method="POST",
                )
                with self.opener.open(batch_request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    started = json.load(response)
                job_id = started["job"]["job_id"]

                job = None
                for _ in range(100):
                    _, _, job = self.fetch(base + f"/batch/status?job_id={job_id}")
                    if job["status"] != "running":
                        break
                    time.sleep(0.03)
                self.assertEqual(job["status"], "completed", job["message"])
                self.assertEqual(job["successful_scenarios"], 2)

                _, _, matrix = self.fetch(base + "/sweep?type=experimental_design")
                self.assertEqual(matrix["scenario_count"], 2)
                self.assertEqual({row["target_c_left_m"] for row in matrix["scenarios"]}, {0.005})
                self.assertEqual({row["target_c_right_m"] for row in matrix["scenarios"]}, {0.1})
                self.assertEqual({row["activity_correction_enabled"] for row in matrix["scenarios"]}, {True})

    def test_browser_batch_endpoint_rejects_non_local_origin(self):
        service = NODE.NodeService(self.config)
        with running_server(service) as base:
            request = Request(
                base + "/batch/start",
                data=b"{}",
                headers={"Content-Type": "application/json", "Origin": "https://example.test"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as caught:
                self.opener.open(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)

    def test_http_endpoints_and_query_strings(self):
        service = NODE.NodeService(self.config)
        with running_server(service) as base:
            _, headers, health = self.fetch(base + "/health")
            self.assertTrue(health["ok"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            _, _, state = self.fetch(base + "/state?cache_bust=1")
            _, _, steps = self.fetch(base + "/steps")
            _, _, result = self.fetch(base + "/run")
            _, _, comparison = self.fetch(base + "/comparison")
            self.assertEqual(state["run_id"], result["run_id"])
            self.assertEqual(comparison["run_id"], result["run_id"])
            self.assertTrue(comparison["baseline_is_current_run"])
            self.assertEqual(steps["run_id"], result["run_id"])
            self.assertEqual(len(steps["steps"]), 18)
            self.assertAlmostEqual(state["voltage_mv"], result["prediction"]["voltage_mv"])

    def test_failed_simulation_is_not_a_successful_zero_reading(self):
        self.config["containers"]["copper_stock"]["volume_ml"] = 0.05
        service = NODE.NodeService(self.config)
        self.assertFalse(service.state_payload()["valid"])
        self.assertIsNone(service.state_payload()["voltage_mv"])
        with running_server(service) as base:
            with self.assertRaises(HTTPError) as caught:
                self.fetch(base + "/state")
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(json.load(caught.exception)["status"], "failed")
            _, _, result = self.fetch(base + "/run")
            self.assertEqual(result["status"], "failed")

    def test_unknown_path_and_preflight(self):
        service = NODE.NodeService(self.config)
        with running_server(service) as base:
            with self.assertRaises(HTTPError) as caught:
                self.fetch(base + "/unknown")
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
            with self.opener.open(Request(base + "/state", method="OPTIONS"), timeout=5) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")

    def test_polarity_is_preserved_not_sorted(self):
        self.config["cell"]["left_container"] = "right_half_cell"
        self.config["cell"]["right_container"] = "left_half_cell"
        data = NODE.NodeService(self.config).state_payload()
        self.assertAlmostEqual(data["voltage_mv"], -22.78883899493854)
        self.assertAlmostEqual(data["c_left_m"], 0.1)
        self.assertAlmostEqual(data["c_right_m"], 0.01)


if __name__ == "__main__":
    unittest.main()
