"""Local runtime storage for the Senior Design Test Stand application.

Source code stays portable in the project folder. Per-computer calibration,
cycle-stop settings, and run logs belong in the Windows local-app-data folder
instead, so a GitHub checkout or release ZIP starts cleanly for each teammate.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


APP_DATA_ENVIRONMENT_VARIABLE = "SENIOR_DESIGN_DATA_DIR"
APP_DATA_FOLDER_NAME = "SeniorDesignTestStand"
CONFIG_FILENAMES = ("position_calibration.json", "cycle_stops.json")
LIVE_RUNS_FILENAME = "live_runs.json"


def write_json_atomic(path: Path, values: Any) -> None:
    """Commit a JSON record only after its contents have reached the local disk."""
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(values, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class AppPaths:
    """The machine-local data layout used by the desktop application."""

    root: Path
    config: Path
    logs: Path
    migrated_legacy_config: tuple[str, ...] = ()

    @classmethod
    def create(cls, source_directory: Path) -> "AppPaths":
        override = os.environ.get(APP_DATA_ENVIRONMENT_VARIABLE)
        if override:
            root = Path(override).expanduser().resolve()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
            root = base / APP_DATA_FOLDER_NAME

        config = root / "config"
        logs = root / "logs"
        config.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        migrated: list[str] = []
        for filename in CONFIG_FILENAMES:
            legacy = source_directory / filename
            destination = config / filename
            if legacy.is_file() and not destination.exists():
                # Copy, rather than move, so the first upgraded run never risks existing data.
                shutil.copy2(legacy, destination)
                migrated.append(filename)
        return cls(root=root, config=config, logs=logs, migrated_legacy_config=tuple(migrated))

    def read_config(self, filename: str) -> dict[str, Any] | None:
        try:
            value = json.loads((self.config / filename).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def write_config(self, filename: str, value: dict[str, Any]) -> None:
        write_json_atomic(self.config / filename, value)

    def remove_config(self, filename: str) -> None:
        try:
            (self.config / filename).unlink()
        except FileNotFoundError:
            pass

    def list_run_directories(self) -> list[Path]:
        """Return saved run folders newest first, including any still-active run."""
        return sorted(
            (folder for folder in self.logs.glob("session_*") if folder.is_dir()),
            key=lambda folder: folder.name,
            reverse=True,
        )

    def delete_run_directory(self, folder: Path) -> None:
        """Remove one verified application-session folder, never the logs folder itself."""
        logs_root = self.logs.resolve()
        target = folder.resolve()
        if target.parent != logs_root or not target.is_dir() or not target.name.startswith("session_"):
            raise ValueError("Only a saved application-session folder can be removed.")
        shutil.rmtree(target)


class CsvLogger:
    """Keeps a per-run record in the app's machine-local logs directory."""

    def __init__(self, logs_directory: Path) -> None:
        base_name = datetime.now().strftime("session_%Y%m%d_%H%M%S")
        folder = logs_directory / base_name
        suffix = 2
        while folder.exists():
            folder = logs_directory / f"{base_name}_{suffix:02d}"
            suffix += 1
        folder.mkdir(parents=True)
        self.folder = folder
        self.started_at = datetime.now().astimezone()
        self.started_monotonic = time.monotonic()
        self.manifest_path = folder / "run_manifest.json"
        self.live_runs_path = folder / LIVE_RUNS_FILENAME
        self.cycle_file = (folder / "cycle_summary.csv").open("w", newline="", encoding="utf-8")
        self.grease_file = (folder / "grease_events.csv").open("w", newline="", encoding="utf-8")
        self.event_file = (folder / "events.csv").open("w", newline="", encoding="utf-8")
        self.cycles = csv.writer(self.cycle_file)
        self.grease_events = csv.writer(self.grease_file)
        self.events = csv.writer(self.event_file)
        self.cycles.writerow(
            [
                "completed_at",
                "live_run_id",
                "cycle_number",
                "live_run_cycle_number",
                "reset_stop_reached_at",
                "motion_started_at",
                "count_stop_reached_at",
                "reset_to_count_duration_seconds",
                "mid_stroke_duration_seconds",
                "travel_direction",
                "reset_stop_degrees",
                "count_stop_degrees",
                "bushing_peak_velocity_degrees_per_second",
                "bushing_peak_signed_velocity_degrees_per_second",
                "bushing_median_travel_velocity_degrees_per_second",
                "bushing_velocity_sample_count",
                "grease_event_ids",
                "temperature_1_f",
                "temperature_2_f",
                "cycle_frequency_cpm",
                "bushing_data_quality",
            ]
        )
        self.grease_events.writerow(
            [
                "grease_event_id",
                "live_run_id",
                "injection_started_at",
                "injection_cycle_number",
                "injection_cycle_phase",
                "commanded_pulse_seconds",
                "pre_injection_cycle_count",
                "pre_median_travel_velocity_degrees_per_second",
                "pre_median_reset_to_count_duration_seconds",
                "post_injection_cycle_count",
                "post_median_travel_velocity_degrees_per_second",
                "post_median_reset_to_count_duration_seconds",
                "median_travel_velocity_change_percent",
                "reset_to_count_duration_change_percent",
                "pressure_baseline_psi",
                "pressure_peak_psi",
                "pressure_peak_at",
                "pressure_time_to_2500_psi_seconds",
                "pressure_response_window_seconds",
                "pressure_sample_count",
                "analysis_status",
            ]
        )
        self.events.writerow(["timestamp", "live_run_id", "event", "detail", "completed_cycles"])
        self.flush()
        self._write_manifest({
            "schema_version": 2,
            "run_id": folder.name,
            "status": "active",
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": None,
            "data_model": "completed_cycle_summary",
        })
        self._write_live_runs([])

    @staticmethod
    def _write_json_atomic(path: Path, values: Any) -> None:
        write_json_atomic(path, values)

    def _write_manifest(self, values: dict[str, Any]) -> None:
        self._write_json_atomic(self.manifest_path, values)

    def update_manifest(self, **values: Any) -> None:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {"schema_version": 2, "run_id": self.folder.name}
        manifest.update(values)
        self._write_manifest(manifest)

    def write_run_configuration(self, values: dict[str, Any]) -> None:
        """Snapshot settings used by this run, independent of later edits."""
        target = self.folder / "run_configuration.json"
        self._write_json_atomic(target, values)

    @staticmethod
    def read_live_runs(folder: Path) -> list[dict[str, Any]]:
        """Return logical runs from one application-session folder, oldest first."""
        try:
            values = json.loads((folder / LIVE_RUNS_FILENAME).read_text(encoding="utf-8"))
            return values if isinstance(values, list) else []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def read_run_manifest(folder: Path) -> dict[str, Any]:
        """Read the application-session metadata used by Local Data management."""
        try:
            values = json.loads((folder / "run_manifest.json").read_text(encoding="utf-8"))
            return values if isinstance(values, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _live_runs(self) -> list[dict[str, Any]]:
        return self.read_live_runs(self.folder)

    def _write_live_runs(self, runs: list[dict[str, Any]]) -> None:
        self._write_json_atomic(self.live_runs_path, runs)

    def begin_live_run(self, configuration: dict[str, Any], completed_cycles_at_start: int) -> dict[str, Any]:
        """Create a logical test run within this always-recording app session."""
        runs = self._live_runs()
        run = {
            "schema_version": 2,
            "id": f"run_{len(runs) + 1:03d}",
            "status": "active",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ended_at": None,
            "completed_cycles_at_start": completed_cycles_at_start,
            "completed_cycles_at_end": None,
            "cycle_frequency_cpm_at_end": None,
            "exported_at": None,
            "export_count": 0,
            "configuration": configuration,
        }
        runs.append(run)
        self._write_live_runs(runs)
        return run

    def finish_live_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_cycles: int,
        cycle_frequency_cpm: float,
    ) -> None:
        runs = self._live_runs()
        for run in runs:
            if run.get("id") == run_id:
                run.update(
                    status=status,
                    ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    completed_cycles_at_end=completed_cycles,
                    cycle_frequency_cpm_at_end=cycle_frequency_cpm,
                )
                break
        self._write_live_runs(runs)

    @classmethod
    def mark_live_runs_exported_in_folder(cls, folder: Path, run_ids: set[str]) -> None:
        runs = cls.read_live_runs(folder)
        exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
        for run in runs:
            if run.get("id") in run_ids:
                run["exported_at"] = exported_at
                run["export_count"] = int(run.get("export_count", 0)) + 1
        cls._write_json_atomic(folder / LIVE_RUNS_FILENAME, runs)

    def mark_live_runs_exported(self, run_ids: set[str]) -> None:
        self.mark_live_runs_exported_in_folder(self.folder, run_ids)

    def flush(self) -> None:
        for output in (self.cycle_file, self.grease_file, self.event_file):
            output.flush()
            os.fsync(output.fileno())

    def finalize(self, **values: Any) -> None:
        self.flush()
        self.update_manifest(
            status="completed",
            ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            duration_seconds=round(time.monotonic() - self.started_monotonic, 1),
            **values,
        )

    @staticmethod
    def timestamp() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def log_cycle(self, values: dict[str, float | int | str], live_run_id: str) -> None:
        """Write one durable record for a completed mechanical cycle."""
        self.cycles.writerow(
            [
                values["completed_at"],
                live_run_id,
                values["cycle_number"],
                values["live_run_cycle_number"],
                values["reset_stop_reached_at"],
                values["motion_started_at"],
                values["count_stop_reached_at"],
                values["reset_to_count_duration_seconds"],
                values["mid_stroke_duration_seconds"],
                values["travel_direction"],
                values["reset_stop_degrees"],
                values["count_stop_degrees"],
                values["bushing_peak_velocity"],
                values["bushing_peak_signed_velocity"],
                values["bushing_median_travel_velocity"],
                values["bushing_velocity_sample_count"],
                values["grease_event_ids"],
                values["temp_1"],
                values["temp_2"],
                values["frequency"],
                values["bushing_data_quality"],
            ]
        )
        self.cycle_file.flush()
        os.fsync(self.cycle_file.fileno())

    def log_event(self, event: str, detail: str, cycles: int, live_run_id: str | None = None) -> None:
        self.events.writerow([self.timestamp(), live_run_id or "", event, detail, cycles])
        self.event_file.flush()
        os.fsync(self.event_file.fileno())

    def log_grease_event(self, values: dict[str, float | int | str], live_run_id: str) -> None:
        """Write one injection record with its associated pre/post cycle comparison."""
        self.grease_events.writerow(
            [
                values["grease_event_id"],
                live_run_id,
                values["injection_started_at"],
                values["injection_cycle_number"],
                values["injection_cycle_phase"],
                values["commanded_pulse_seconds"],
                values["pre_injection_cycle_count"],
                values["pre_median_travel_velocity"],
                values["pre_median_reset_to_count_duration"],
                values["post_injection_cycle_count"],
                values["post_median_travel_velocity"],
                values["post_median_reset_to_count_duration"],
                values["median_travel_velocity_change_percent"],
                values["reset_to_count_duration_change_percent"],
                values["pressure_baseline"],
                values["pressure_peak"],
                values["pressure_peak_at"],
                values["pressure_time_to_threshold"],
                values["pressure_response_window_seconds"],
                values["pressure_sample_count"],
                values["analysis_status"],
            ]
        )
        self.grease_file.flush()
        os.fsync(self.grease_file.fileno())

    def close(self) -> None:
        self.flush()
        self.cycle_file.close()
        self.grease_file.close()
        self.event_file.close()
