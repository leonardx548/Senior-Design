"""Hardware-free regression checks for test-stand data safety."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
import os
from pathlib import Path

from app_storage import AppPaths, CsvLogger
from labjack_diagnostic import summarize_ain2_samples
from test_stand_app import (
    AUTO_ARCHIVE_CONFIG_SCHEMA_VERSION,
    DEFAULT_AUTO_ARCHIVE_INCLUDE,
    EXPORT_FILE_OPTIONS,
    TestStandApp,
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
            {"events", "metadata", "configuration"},
        )

        self.assertEqual(count, 4)  # export manifest plus three selected files
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
        self.assertIn("export_manifest.json", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/events.csv", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/live_run.json", names)
        self.assertIn(f"{logger.folder.name}/{run_id}/run_configuration.json", names)

    def test_local_data_deletion_rejects_outside_targets(self) -> None:
        paths = AppPaths.create(self.temporary_directory)
        outside = self.temporary_directory / "not_a_session"
        outside.mkdir()
        with self.assertRaises(ValueError):
            paths.delete_run_directory(outside)

    def test_legacy_auto_archive_config_adds_sensible_defaults_once(self) -> None:
        app = TestStandApp.__new__(TestStandApp)
        app.paths = type("ConfigReader", (), {"read_config": lambda _self, _name: {"include": ["events"]}})()
        config = app.load_auto_archive_config()
        self.assertEqual(config["schema_version"], AUTO_ARCHIVE_CONFIG_SCHEMA_VERSION)
        self.assertEqual(
            config["include"],
            [key for key, _label, _filename in EXPORT_FILE_OPTIONS if key in {*DEFAULT_AUTO_ARCHIVE_INCLUDE, "events"}],
        )

    def test_ain2_summary_rejects_unstable_samples(self) -> None:
        stable = summarize_ain2_samples([0.06, 0.061, 0.059, 0.06])
        unstable = summarize_ain2_samples([0.0, 0.2, 0.0, 0.2])
        self.assertEqual(stable["status"], "success")
        self.assertEqual(unstable["status"], "unstable")

    def test_automatic_archive_retains_a_shared_session_folder(self) -> None:
        """Exporting one run must never delete earlier runs in its CSV folder."""
        paths = AppPaths.create(self.temporary_directory / "app_data")
        export_directory = self.temporary_directory / "exports"
        export_directory.mkdir()
        logger = CsvLogger(paths.logs)
        earlier_run = logger.begin_live_run({"name": "earlier"}, completed_cycles_at_start=0)
        logger.finish_live_run(
            str(earlier_run["id"]), status="stopped", completed_cycles=1, cycle_frequency_cpm=1.0
        )
        archived_run = logger.begin_live_run({"name": "to archive"}, completed_cycles_at_start=1)
        archived_run_id = str(archived_run["id"])
        original_folder = logger.folder

        app = TestStandApp.__new__(TestStandApp)
        app.paths = paths
        app.logger = logger
        app.current_live_run_id = archived_run_id
        app.cycles = 2
        app.auto_archive_active = True
        app.active_auto_archive_config = {
            "directory": str(export_directory),
            "include": ["events", "metadata", "configuration"],
            "interval_seconds": 900,
        }
        app.auto_archive_elapsed_seconds = 900.0
        app.auto_archive_segment_number = 1
        archive_events: list[tuple[object, ...]] = []
        app.log_event = lambda *args, **_kwargs: archive_events.append(args)

        def finish_live_run(status: str) -> None:
            logger.finish_live_run(
                archived_run_id, status=status, completed_cycles=app.cycles, cycle_frequency_cpm=0.0
            )
            app.current_live_run_id = None

        def replace_logger() -> None:
            app.logger = CsvLogger(paths.logs)

        app._finish_live_run = finish_live_run
        app._replace_logger = replace_logger

        self.assertTrue(
            app._archive_auto_segment("stopped", "stopped", continue_running=False), archive_events
        )
        app.logger.close()

        self.assertTrue(original_folder.is_dir())
        stored_runs = CsvLogger.read_live_runs(original_folder)
        self.assertEqual({run["id"] for run in stored_runs}, {earlier_run["id"], archived_run_id})
        completed_run = next(run for run in stored_runs if run["id"] == archived_run_id)
        self.assertTrue(completed_run.get("exported_at"))
        self.assertFalse(next(run for run in stored_runs if run["id"] == earlier_run["id"]).get("exported_at"))
        archives = list(export_directory.glob("*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            self.assertIsNone(archive.testzip())


if __name__ == "__main__":
    unittest.main()
