"""Hardware-free regression checks for test-stand data safety."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
import os
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from app_storage import AppPaths, CsvLogger
from labjack_diagnostic import (
    auxiliary_stream_scans,
    demultiplex_stream_scans,
    ps12dc_output_request,
    summarize_ain2_samples,
    t4_device_temperature_celsius_from_ain14,
    t4_usb_air_temperature_celsius_from_ain14,
)
from test_stand_app import (
    CycleCapture,
    GreaseEventCapture,
    INJECTOR_ACTIVATION_PSI,
    TestStandApp,
    LivePlot,
    temperature_from_amplified_voltage,
    type_t_emf_millivolts,
    type_t_temperature_celsius,
    summarize_ain2_travel_range,
    write_live_run_archive,
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

    def test_archive_is_verified_and_contains_selected_run(self) -> None:
        logs = self.temporary_directory / "logs"
        logs.mkdir()
        logger = CsvLogger(logs)
        run = logger.begin_live_run({"test": "configuration"}, completed_cycles_at_start=0)
        run_id = str(run["id"])
        logger.log_event("Test event", "Archive regression check", cycles=0, live_run_id=run_id)
        logger.finish_live_run(run_id, status="stopped", completed_cycles=0, cycle_frequency_cpm=0.0)
        logger.close()

        exports = self.temporary_directory / "exports"
        exports.mkdir()
        destination = exports / "run.zip"
        count = write_live_run_archive(
            destination,
            [(logger.folder, CsvLogger.read_live_runs(logger.folder)[0])],
            {"cycles", "grease", "events", "metadata", "configuration"},
        )

        self.assertEqual(count, 6)  # export manifest plus five compact run files
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
        self.assertIn("export_manifest.json", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/cycle_summary.csv", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/grease_events.csv", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/events.csv", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/live_run.json", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/run_configuration.json", names)

    def test_csv_logger_can_create_a_direct_run_folder(self) -> None:
        output_root = self.temporary_directory / "chosen_output"
        logger = CsvLogger(output_root)
        self.assertEqual(logger.folder.parent, output_root)
        self.assertTrue(logger.folder.name.startswith("Run_"))
        logger.close()

    def test_app_paths_only_create_configuration_storage(self) -> None:
        paths = AppPaths.create(self.temporary_directory)
        self.assertTrue(paths.config.is_dir())
        self.assertFalse((paths.root / "logs").exists())

    def test_ain2_summary_rejects_unstable_samples(self) -> None:
        stable = summarize_ain2_samples([0.06, 0.061, 0.059, 0.06])
        unstable = summarize_ain2_samples([0.0, 0.2, 0.0, 0.2])
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

    def test_invalid_cycle_capture_keeps_count_row_but_omits_motion_metrics(self) -> None:
        capture = CycleCapture(
            reset_stop_reached_at="2026-11-11T10:00:00",
            reset_stop_monotonic=10.0,
            reset_stop_degrees=15.0,
            count_stop_degrees=25.0,
            direction=1,
        )
        capture.observe(20.0, 50.0, "2026-11-11T10:00:01", 11.0)
        self.assertTrue(capture.invalidate("AIN2 left calibrated span; motion metrics unavailable"))
        values = capture.completed_values(
            completed_at="2026-11-11T10:00:02",
            completed_monotonic=12.0,
            cycle_number=7,
            live_run_cycle_number=7,
            frequency=18.0,
        )
        self.assertEqual(values["cycle_number"], 7)
        self.assertEqual(values["reset_to_count_duration_seconds"], 2.0)
        self.assertEqual(values["bushing_peak_velocity"], "")
        self.assertEqual(values["bushing_velocity_sample_count"], 0)
        self.assertIn("AIN2 left calibrated span", str(values["bushing_data_quality"]))

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
        capture.invalidate("AIN2 left calibrated span; motion metrics unavailable")
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
        logger.log_event("Cycle metrics unavailable", "AIN2 left calibrated span", cycles=7, live_run_id=run_id)
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
        self.assertIn("AIN2 left calibrated span", cycles[0]["bushing_data_quality"])
        self.assertEqual([row["grease_event_id"] for row in grease_events], ["grease_001"])
        self.assertEqual([row["event"] for row in events], ["Cycle metrics unavailable"])
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["completed_cycles"], 7)
        self.assertFalse((logger.folder / "stream_trace.csv").exists())

    def test_pressure_calibration_interpolates_and_flags_outside_span(self) -> None:
        calibration = {
            "point_1": {"voltage": 0.5, "pressure_psi": 0.0},
            "point_2": {"voltage": 2.0, "pressure_psi": 3000.0},
        }
        pressure, outside = TestStandApp.pressure_from_voltage(1.25, calibration)
        self.assertEqual(pressure, 1500.0)
        self.assertFalse(outside)
        # One fifth of the 1.5 V calibration span is still displayed, so
        # ordinary endpoint noise does not make pressure blink out.
        _pressure, outside = TestStandApp.pressure_from_voltage(2.3, calibration)
        self.assertFalse(outside)
        _pressure, outside = TestStandApp.pressure_from_voltage(2.31, calibration)
        self.assertTrue(outside)

    def test_auxiliary_stream_samples_remain_separate_from_ain2(self) -> None:
        samples = demultiplex_stream_scans([1.0, 2.0, 3.0, 4.0, 1.5, 1.1, -9999.0, 3.1, 4.1, 1.6])
        self.assertEqual(samples["AIN0"], [1.0, 1.1])
        self.assertEqual(samples["AIN1"], [2.0])
        self.assertEqual(samples["AIN2"], [3.0, 3.1])
        self.assertEqual(samples["AIN4"], [4.0, 4.1])
        self.assertEqual(samples["AIN14"], [1.5, 1.6])

    def test_auxiliary_capture_rows_keep_invalid_readings_visible(self) -> None:
        scans = auxiliary_stream_scans([1.0, 2.0, 3.0, 4.0, 1.5, -9999.0, 2.1, 3.1, 4.1, 1.6])
        self.assertEqual(scans, [(1.0, 2.0), (None, 2.1)])

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

    def test_auxiliary_capture_uses_a_continuous_sample_clock(self) -> None:
        app = TestStandApp.__new__(TestStandApp)
        app._raw_auxiliary_samples = deque(maxlen=12020)
        app._last_auxiliary_capture_monotonic = None
        app._last_auxiliary_capture_at = None
        app.auxiliary_raw_voltages = {"AIN0": None, "AIN1": None}
        initial_at = datetime(2026, 10, 22, 13, 0, 0)
        app._record_raw_auxiliary_scans([(1.0, 2.0)] * 10, 200.0, 10.0, initial_at, 0, 0)
        # The next read arrived 155 ms later, but it holds the next ten 5 ms
        # samples. The saved sample clock must not manufacture a 155 ms gap.
        app._record_raw_auxiliary_scans(
            [(1.1, 2.1)] * 10,
            200.0,
            10.155,
            initial_at + timedelta(milliseconds=155),
            0,
            30,
        )
        samples = tuple(app._raw_auxiliary_samples)
        intervals = [samples[index].monotonic_seconds - samples[index - 1].monotonic_seconds for index in range(1, len(samples))]
        self.assertTrue(all(abs(interval - 0.005) < 1e-9 for interval in intervals))
        self.assertEqual(samples[-1].batch_received_at, (initial_at + timedelta(milliseconds=155)).isoformat(timespec="milliseconds"))

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
