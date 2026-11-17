"""Hardware-free regression checks for test-stand data safety."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
import os
from collections import deque
from pathlib import Path

from app_storage import AppPaths, Campaign, CsvLogger
from labjack_diagnostic import (
    demultiplex_stream_scans,
    ps12dc_output_request,
    summarize_voltage_samples,
    t4_device_temperature_celsius_from_ain14,
    t4_usb_air_temperature_celsius_from_ain14,
)
from test_stand_app import (
    CycleCapture,
    GreaseEventCapture,
    INJECTOR_ACTIVATION_PSI,
    TestStandApp,
    LivePlot,
    summarize_temperature_fahrenheit_samples,
    temperature_from_amplified_voltage,
    type_t_emf_millivolts,
    type_t_temperature_celsius,
    summarize_ain2_travel_range,
    import_campaign_package,
    read_calibration_package,
    write_campaign_package,
    write_calibration_package,
)


class CoreSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = Path(tempfile.mkdtemp(prefix="test_stand_core_", dir=Path.cwd()))
        self.previous_data_directory = os.environ.get("SENIOR_DESIGN_DATA_DIR")
        os.environ["SENIOR_DESIGN_DATA_DIR"] = str(self.temporary_directory / "app_data")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.temporary_directory, ignore_errors=True)
        if self.previous_data_directory is None:
            os.environ.pop("SENIOR_DESIGN_DATA_DIR", None)
        else:
            os.environ["SENIOR_DESIGN_DATA_DIR"] = self.previous_data_directory

    def test_csv_logger_can_create_a_direct_run_folder(self) -> None:
        output_root = self.temporary_directory / "chosen_output"
        logger = CsvLogger(output_root)
        self.assertEqual(logger.folder.parent, output_root)
        self.assertTrue(logger.folder.name.startswith("Run_"))
        logger.close()

    def test_campaign_retains_total_across_multiple_dated_runs(self) -> None:
        output_root = self.temporary_directory / "campaign_output"
        campaign = Campaign.create(output_root, "Lubrication endurance")
        self.assertEqual(campaign.completed_cycles, 0)
        self.assertTrue(campaign.runs_root.is_dir())

        first_logger = CsvLogger(campaign.runs_root)
        first_session = campaign.begin_session(first_logger.folder, completed_cycles_at_start=0)
        campaign.record_progress(42, 125.4)
        campaign.finish_session(first_session, status="saved", completed_cycles=42, active_test_seconds=125.4)
        first_logger.close()

        resumed = Campaign.open(campaign.folder)
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed.completed_cycles, 42)
        self.assertAlmostEqual(resumed.active_test_seconds, 125.4)

        second_logger = CsvLogger(resumed.runs_root)
        second_session = resumed.begin_session(second_logger.folder, completed_cycles_at_start=resumed.completed_cycles)
        resumed.finish_session(second_session, status="saved", completed_cycles=59, active_test_seconds=180.0)
        self.assertEqual(resumed.completed_cycles, 59)
        self.assertEqual(len(resumed.manifest["sessions"]), 2)
        second_logger.close()

        resumed.finish()
        finished = Campaign.open(campaign.folder)
        self.assertIsNotNone(finished)
        assert finished is not None
        self.assertEqual(finished.manifest["status"], "completed")

    def test_campaign_marks_unclosed_session_before_resuming(self) -> None:
        campaign = Campaign.create(self.temporary_directory, "Interrupted session")
        first_logger = CsvLogger(campaign.runs_root)
        campaign.begin_session(first_logger.folder, completed_cycles_at_start=0)
        first_logger.close()

        resumed = Campaign.open(campaign.folder)
        self.assertIsNotNone(resumed)
        assert resumed is not None
        second_logger = CsvLogger(resumed.runs_root)
        resumed.begin_session(second_logger.folder, completed_cycles_at_start=0)
        self.assertEqual(resumed.manifest["sessions"][0]["status"], "interrupted_before_resume")
        second_logger.close()

    def test_campaign_package_round_trip_preserves_setup_and_runs(self) -> None:
        source_root = self.temporary_directory / "source"
        campaign = Campaign.create(source_root, "Team handoff")
        setup = {
            "schema_version": 1,
            "position_calibration": {"point_1": {"voltage": 0.5, "angle": 0.0}},
            "pressure_calibration": None,
            "temperature_calibration": None,
            "cycle_stops": {"retracted": 0.0, "extended": 30.0},
        }
        campaign.write_configuration(setup)
        logger = CsvLogger(campaign.runs_root)
        run = logger.begin_live_run({}, completed_cycles_at_start=12)
        session_id = campaign.begin_session(logger.folder, completed_cycles_at_start=12)
        logger.finish_live_run(str(run["id"]), status="saved", completed_cycles=14, cycle_frequency_cpm=20.0)
        campaign.finish_session(session_id, status="saved", completed_cycles=14, active_test_seconds=8.5)
        logger.close()

        package = self.temporary_directory / "handoff.zip"
        count = write_campaign_package(package, campaign)
        self.assertGreaterEqual(count, 4)
        with zipfile.ZipFile(package) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("campaign_package.json", archive.namelist())
            self.assertIn(f"{campaign.folder.name}/campaign_configuration.json", archive.namelist())

        destination_root = self.temporary_directory / "destination"
        imported = import_campaign_package(package, destination_root)
        self.assertEqual(imported.name, "Team handoff")
        self.assertEqual(imported.completed_cycles, 14)
        self.assertEqual(imported.read_configuration(), setup)
        self.assertEqual(len(list(imported.runs_root.glob("Run_*"))), 1)
        duplicate = import_campaign_package(package, destination_root)
        self.assertNotEqual(duplicate.folder, imported.folder)
        self.assertTrue(duplicate.folder.name.endswith("_02"))

        folder_archive = self.temporary_directory / "campaign_folder.zip"
        with zipfile.ZipFile(folder_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source_file in campaign.folder.rglob("*"):
                if source_file.is_file():
                    archive.write(source_file, f"{campaign.folder.name}/{source_file.relative_to(campaign.folder).as_posix()}")
        imported_folder_archive = import_campaign_package(folder_archive, destination_root)
        self.assertEqual(imported_folder_archive.name, "Team handoff")
        self.assertEqual(imported_folder_archive.completed_cycles, 14)

    def test_calibration_package_round_trip_contains_setup_only(self) -> None:
        setup = {
            "schema_version": 1,
            "captured_at": "2026-11-13T10:00:00-05:00",
            "position_calibration": {
                "point_1": {"voltage": 0.5, "angle": 0.0},
                "point_2": {"voltage": 2.0, "angle": 90.0},
            },
            "pressure_calibration": None,
            "temperature_calibration": {"mode": "nominal", "cjc_celsius": 22.0, "channels": {}},
            "cycle_stops": {"retracted": 0.0, "extended": 90.0},
        }
        destination = self.temporary_directory / "calibration.zip"
        self.assertEqual(write_calibration_package(destination, setup), 2)
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {"calibration_package.json", "calibration_setup.json"})
        self.assertEqual(read_calibration_package(destination), setup)

    def test_app_paths_only_create_configuration_storage(self) -> None:
        paths = AppPaths.create(self.temporary_directory)
        self.assertTrue(paths.config.is_dir())
        self.assertFalse((paths.root / "logs").exists())

    def test_voltage_summary_rejects_unstable_samples(self) -> None:
        stable = summarize_voltage_samples([0.06, 0.061, 0.059, 0.06], "AIN2")
        unstable = summarize_voltage_samples([0.0, 0.2, 0.0, 0.2], "AIN2")
        self.assertEqual(stable["status"], "success")
        self.assertEqual(unstable["status"], "unstable")

    def test_ain2_travel_capture_uses_conditioned_endpoints(self) -> None:
        samples = [index / 1000.0 for index in range(1001)] + [index / 1000.0 for index in range(1000, -1, -1)]
        samples.extend((-20.0, 20.0))  # isolated electrical spikes
        result = summarize_ain2_travel_range(samples)
        self.assertEqual(result["status"], "success")
        self.assertLess(float(result["low_voltage"]), 0.04)
        self.assertGreater(float(result["high_voltage"]), 0.96)
        self.assertGreaterEqual(int(result["rejected_count"]), 20)

    def test_stream_gap_marks_cycle_without_discarding_motion_metrics(self) -> None:
        capture = CycleCapture(
            reset_stop_reached_at="2026-11-11T10:00:00",
            reset_stop_monotonic=10.0,
            reset_stop_degrees=15.0,
            count_stop_degrees=25.0,
            direction=1,
        )
        capture.observe(20.0, 50.0, "2026-11-11T10:00:01", 11.0)
        capture.stream_gap_detected = True
        values = capture.completed_values(
            completed_at="2026-11-11T10:00:02",
            completed_monotonic=12.0,
            cycle_number=7,
            live_run_cycle_number=7,
            frequency=18.0,
        )
        self.assertEqual(values["cycle_number"], 7)
        self.assertEqual(values["reset_to_count_duration_seconds"], 2.0)
        self.assertEqual(values["bushing_peak_velocity"], 50.0)
        self.assertEqual(values["bushing_velocity_sample_count"], 1)
        self.assertIn("AIN2 stream gap", str(values["bushing_data_quality"]))

    def test_grease_response_uses_1500_psi_threshold(self) -> None:
        event = GreaseEventCapture(
            event_id="grease_001",
            started_at="2026-11-11T10:00:00",
            started_monotonic=100.0,
            injection_cycle_number=1,
            injection_cycle_phase="between completed cycles",
            commanded_pulse_seconds=0.5,
            pressure_baseline=0.0,
            pre_cycles=(),
        )
        event.record_pressure(INJECTOR_ACTIVATION_PSI - 1.0, "2026-11-11T10:00:00.100", 100.1)
        event.record_pressure(INJECTOR_ACTIVATION_PSI, "2026-11-11T10:00:00.200", 100.2)
        values = event.values(status="complete")
        self.assertEqual(values["pressure_response_threshold_psi"], 1500.0)
        self.assertAlmostEqual(float(values["pressure_time_to_threshold"]), 0.2, places=4)

    def test_run_files_preserve_grease_quality_and_active_time(self) -> None:
        logger = CsvLogger(self.temporary_directory / "output")
        run = logger.begin_live_run({}, completed_cycles_at_start=0)
        event = GreaseEventCapture(
            event_id="grease_001",
            started_at="2026-11-11T10:00:00",
            started_monotonic=100.0,
            injection_cycle_number=1,
            injection_cycle_phase="between completed cycles",
            commanded_pulse_seconds="",
            pressure_baseline=0.0,
            pre_cycles=(),
            overlapping_event_ids=["grease_002"],
        )
        event.record_pressure(1500.0, "2026-11-11T10:00:00.100", 100.1)
        logger.log_grease_event(event.values(status="complete; overlapping injection window"), str(run["id"]))
        logger.finish_live_run(str(run["id"]), status="saved", completed_cycles=1, cycle_frequency_cpm=0.0)
        logger.finalize(active_test_seconds=12.5)
        logger.close()

        with (logger.folder / "grease_events.csv").open(newline="", encoding="utf-8") as source:
            row = next(csv.DictReader(source))
        self.assertEqual(row["pressure_time_to_1500_psi_seconds"], "0.1")
        self.assertEqual(row["comparison_quality"], "overlapping injection windows")
        manifest = json.loads((logger.folder / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["active_test_seconds"], 12.5)
        self.assertIn("wall_duration_seconds", manifest)

    def test_compact_run_files_keep_each_record_type_after_finalization(self) -> None:
        """A completed run retains cycle, grease, and event rows without raw tracing."""
        logger = CsvLogger(self.temporary_directory / "output")
        run = logger.begin_live_run({"cycle_stops": [15.0, 25.0]}, completed_cycles_at_start=6)
        run_id = str(run["id"])

        capture = CycleCapture(
            reset_stop_reached_at="2026-11-11T10:00:00",
            reset_stop_monotonic=10.0,
            reset_stop_degrees=15.0,
            count_stop_degrees=25.0,
            direction=1,
        )
        capture.stream_gap_detected = True
        capture.observe(20.0, 50.0, "2026-11-11T10:00:01", 11.0)
        logger.log_cycle(
            capture.completed_values(
                completed_at="2026-11-11T10:00:02",
                completed_monotonic=12.0,
                cycle_number=7,
                live_run_cycle_number=1,
                frequency=18.0,
            ),
            run_id,
        )

        grease = GreaseEventCapture(
            event_id="grease_001",
            started_at="2026-11-11T10:00:02",
            started_monotonic=12.0,
            injection_cycle_number=7,
            injection_cycle_phase="after completed cycle",
            commanded_pulse_seconds=0.5,
            pressure_baseline=10.0,
            pre_cycles=(),
        )
        grease.record_pressure(1500.0, "2026-11-11T10:00:02.200", 12.2)
        logger.log_grease_event(grease.values(status="complete"), run_id)
        logger.log_event("AIN2 stream gap", "Cycle metrics were retained and flagged.", cycles=7, live_run_id=run_id)
        logger.finish_live_run(run_id, status="saved", completed_cycles=7, cycle_frequency_cpm=18.0)
        logger.finalize(active_test_seconds=2.0, completed_cycles=7, cycle_frequency_cpm=18.0)
        logger.close()

        with (logger.folder / "cycle_summary.csv").open(newline="", encoding="utf-8") as source:
            cycles = list(csv.DictReader(source))
        with (logger.folder / "grease_events.csv").open(newline="", encoding="utf-8") as source:
            grease_events = list(csv.DictReader(source))
        with (logger.folder / "events.csv").open(newline="", encoding="utf-8") as source:
            events = list(csv.DictReader(source))
        manifest = json.loads((logger.folder / "run_manifest.json").read_text(encoding="utf-8"))

        self.assertEqual([row["cycle_number"] for row in cycles], ["7"])
        self.assertIn("AIN2 stream gap", cycles[0]["bushing_data_quality"])
        self.assertEqual([row["grease_event_id"] for row in grease_events], ["grease_001"])
        self.assertEqual([row["event"] for row in events], ["AIN2 stream gap"])
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["completed_cycles"], 7)
        self.assertFalse((logger.folder / "stream_trace.csv").exists())

    def test_pressure_calibration_interpolates_without_range_rejection(self) -> None:
        calibration = {
            "point_1": {"voltage": 0.5, "pressure_psi": 0.0},
            "point_2": {"voltage": 2.0, "pressure_psi": 3000.0},
        }
        pressure = TestStandApp.pressure_from_voltage(1.25, calibration)
        self.assertEqual(pressure, 1500.0)
        self.assertEqual(TestStandApp.pressure_from_voltage(2.5, calibration), 4000.0)

    def test_position_calibration_maps_live_motion_beyond_captured_endpoints(self) -> None:
        calibration = {
            "point_1": {"voltage": 0.5, "angle": 0.0},
            "point_2": {"voltage": 2.0, "angle": 90.0},
        }
        # A valid AIN2 stream continues to provide meaningful motion data when
        # a dynamic calibration capture misses an instantaneous endpoint.
        angle = TestStandApp.angle_from_voltage(0.4, calibration)
        self.assertAlmostEqual(angle, -6.0)
        self.assertAlmostEqual(TestStandApp.angle_from_voltage(2.75, calibration), 135.0)

    def test_shared_stream_samples_remain_separate_from_ain2(self) -> None:
        samples = demultiplex_stream_scans([1.0, 2.0, 3.0, 4.0, 1.5, 1.1, -9999.0, 3.1, 4.1, 1.6])
        self.assertEqual(samples["AIN0"], [1.0, 1.1])
        self.assertEqual(samples["AIN1"], [2.0])
        self.assertEqual(samples["AIN2"], [3.0, 3.1])
        self.assertEqual(samples["AIN4"], [4.0, 4.1])
        self.assertEqual(samples["AIN14"], [1.5, 1.6])

    def test_t4_air_temperature_estimate_uses_usb_offset(self) -> None:
        # 1.5961 V corresponds to 46.65 °C device temperature from the
        # documented T4 conversion, then 42.35 °C estimated USB air temp.
        self.assertAlmostEqual(t4_device_temperature_celsius_from_ain14(1.5961), 46.65, places=2)
        self.assertAlmostEqual(t4_usb_air_temperature_celsius_from_ain14(1.5961), 42.35, places=2)

    def test_ps12dc_control_protocol_accepts_only_s0(self) -> None:
        self.assertTrue(ps12dc_output_request("SET_OUTPUT DIO8 1"))
        self.assertFalse(ps12dc_output_request("set_output dio8 0"))
        self.assertIsNone(ps12dc_output_request("SET_OUTPUT DIO9 1"))
        self.assertIsNone(ps12dc_output_request("SET_OUTPUT DIO7 1"))
        self.assertIsNone(ps12dc_output_request("SET_OUTPUT DIO8 ON"))

    def test_graph_reduction_preserves_time_order_and_extremes(self) -> None:
        samples = tuple((float(index), float(index % 5)) for index in range(1000))
        samples = samples[:501] + ((501.0, 99.0),) + samples[502:]
        reduced = LivePlot._render_samples(samples, maximum_points=100)
        self.assertLessEqual(len(reduced), 102)
        self.assertEqual(reduced[0], samples[0])
        self.assertEqual(reduced[-1], samples[-1])
        self.assertIn((501.0, 99.0), reduced)
        self.assertEqual(tuple(point[0] for point in reduced), tuple(sorted(point[0] for point in reduced)))

    def test_temperature_history_uses_test_elapsed_time(self) -> None:
        class ValueCapture:
            value = ""

            def set(self, value: str) -> None:
                self.value = value

        app = TestStandApp.__new__(TestStandApp)
        app.temperature_history = {1: [], 2: []}
        app.current_temperatures = {1: None, 2: None}
        app.metric_vars = {"temp_1": ValueCapture(), "temp_2": ValueCapture()}
        app.test_elapsed_seconds = 12.5
        app.record_temperature(1, 72.25)
        app.test_elapsed_seconds = 13.5
        app.record_temperature(1, 72.5)
        self.assertEqual(app.temperature_history[1], [(12.5, 72.25), (13.5, 72.5)])
        self.assertEqual(app.metric_vars["temp_1"].value, "72.5 °F")
        self.assertEqual(app.current_temperatures[1], 72.5)
        with self.assertRaises(ValueError):
            app.record_temperature(3, 70.0)

    def test_temperature_history_is_reduced_to_one_point_per_second(self) -> None:
        class ValueCapture:
            def set(self, _value: str) -> None:
                pass

        app = TestStandApp.__new__(TestStandApp)
        app.temperature_history = {1: [], 2: []}
        app.current_temperatures = {1: None, 2: None}
        app.metric_vars = {"temp_1": ValueCapture(), "temp_2": ValueCapture()}
        for index in range(20):
            app.test_elapsed_seconds = index / 20.0
            app.record_temperature(1, 70.0 + index)
        self.assertEqual(len(app.temperature_history[1]), 1)
        self.assertEqual(app.temperature_history[1][0], (0.95, 89.0))

    def test_type_t_reference_function_round_trips_at_room_temperature(self) -> None:
        self.assertAlmostEqual(type_t_emf_millivolts(25.0), 0.99198, places=4)
        self.assertAlmostEqual(type_t_temperature_celsius(type_t_emf_millivolts(25.0)), 25.0, places=5)

    def test_nominal_temperature_setup_applies_cjc_and_gain(self) -> None:
        calibration = {
            "cjc_celsius": 22.5,
            "channels": {"AIN0": {"gain": 201.0, "offset_volts": 1.25}},
        }
        # Ten room-temperature Type-T degrees are approximately 405 µV.
        voltage = 1.25 + 201.0 * 40.5e-6 * 10.0
        self.assertAlmostEqual(temperature_from_amplified_voltage(voltage, calibration, "AIN0"), 32.4, places=1)

    def test_temperature_batch_rejects_implausible_open_probe_readings(self) -> None:
        calibration = {
            "cjc_celsius": 22.5,
            "channels": {"AIN0": {"gain": 201.0, "offset_volts": 1.25}},
        }
        normal_voltage = 1.25 + 201.0 * 40.5e-6 * 10.0
        normal_fahrenheit, normal_fault = summarize_temperature_fahrenheit_samples(
            [normal_voltage] * 10, calibration, "AIN0"
        )
        self.assertIsNone(normal_fault)
        self.assertAlmostEqual(float(normal_fahrenheit), 90.3, places=1)

        open_probe_voltage = 1.25 + 201.0 * (
            type_t_emf_millivolts(400.0) - type_t_emf_millivolts(22.5)
        ) / 1000.0
        open_probe_fahrenheit, open_probe_fault = summarize_temperature_fahrenheit_samples(
            [open_probe_voltage] * 10, calibration, "AIN0"
        )
        self.assertIsNone(open_probe_fahrenheit)
        self.assertIn("plausible", str(open_probe_fault))

    def test_fault_warning_debounces_and_logs_only_state_changes(self) -> None:
        app = TestStandApp.__new__(TestStandApp)
        app._active_faults = {}
        app._fault_failure_batches = {}
        app._fault_recovery_batches = {}
        events: list[tuple[str, str]] = []
        bells: list[bool] = []
        app._render_fault_banner = lambda: None
        app.log_event = lambda event, detail: events.append((event, detail))
        app.bell = lambda: bells.append(True)

        app._observe_fault("temperature_1_input", "Temperature 1 input fault", "Probe check needed.", True)
        self.assertNotIn("temperature_1_input", app._active_faults)
        app._observe_fault("temperature_1_input", "Temperature 1 input fault", "Probe check needed.", True)
        self.assertIn("temperature_1_input", app._active_faults)
        self.assertEqual(events, [("ALARM — Temperature 1 input fault", "Probe check needed.")])
        self.assertEqual(bells, [True])

        app._observe_fault("temperature_1_input", "Temperature 1 input fault", "", False)
        app._observe_fault("temperature_1_input", "Temperature 1 input fault", "", False)
        self.assertIn("temperature_1_input", app._active_faults)
        app._observe_fault("temperature_1_input", "Temperature 1 input fault", "", False)
        self.assertNotIn("temperature_1_input", app._active_faults)
        self.assertEqual(events[-1], ("Alarm cleared — Temperature 1 input fault", "Valid readings have recovered."))

    def test_cycle_frequency_history_samples_once_per_second(self) -> None:
        app = TestStandApp.__new__(TestStandApp)
        app.cycle_frequency_history = []
        app._frequency = lambda: 12.5
        app.test_elapsed_seconds = 0.1
        app._record_cycle_frequency_history()
        app.test_elapsed_seconds = 0.9
        app._record_cycle_frequency_history()
        app.test_elapsed_seconds = 1.1
        app._record_cycle_frequency_history()
        self.assertEqual(app.cycle_frequency_history, [(0.1, 12.5), (1.1, 12.5)])

if __name__ == "__main__":
    unittest.main()
