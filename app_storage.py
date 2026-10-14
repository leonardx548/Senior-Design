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
        target = self.config / filename
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        temporary.replace(target)

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
        self.sensor_file = (folder / "sensor_readings.csv").open("w", newline="", encoding="utf-8")
        self.event_file = (folder / "events.csv").open("w", newline="", encoding="utf-8")
        self.sensors = csv.writer(self.sensor_file)
        self.events = csv.writer(self.event_file)
        self.sensors.writerow(
            [
                "timestamp",
                "live_run_id",
                "elapsed_seconds",
                "bushing_position",
                "bushing_position_unit",
                "ain2_voltage_volts",
                "simulated_pressure_psi",
                "simulated_temperature_1_f",
                "simulated_temperature_2_f",
                "simulated_temperature_3_f",
                "simulated_temperature_4_f",
                "completed_cycles",
                "cycle_frequency_cpm",
                "grease_status",
            ]
        )
        self.events.writerow(["timestamp", "live_run_id", "event", "detail", "completed_cycles"])
        self.sensor_file.flush()
        self.event_file.flush()
        self._write_manifest({
            "schema_version": 1,
            "run_id": folder.name,
            "status": "active",
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": None,
            "sensor_log_interval_seconds": None,
        })
        self._write_live_runs([])

    @staticmethod
    def _write_json_atomic(path: Path, values: Any) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _write_manifest(self, values: dict[str, Any]) -> None:
        self._write_json_atomic(self.manifest_path, values)

    def update_manifest(self, **values: Any) -> None:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {"schema_version": 1, "run_id": self.folder.name}
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
            "schema_version": 1,
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
        self.sensor_file.flush()
        self.event_file.flush()

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

    def log_reading(self, values: dict[str, float | int | str], live_run_id: str | None = None) -> None:
        self.sensors.writerow(
            [
                self.timestamp(),
                live_run_id or "",
                f"{values['elapsed']:.1f}",
                values["bushing_position"],
                values["bushing_position_unit"],
                values["ain2_voltage"],
                f"{values['pressure']:.1f}",
                f"{values['temp_1']:.2f}",
                f"{values['temp_2']:.2f}",
                f"{values['temp_3']:.2f}",
                f"{values['temp_4']:.2f}",
                values["cycles"],
                f"{values['frequency']:.2f}",
                values["grease_status"],
            ]
        )
        self.sensor_file.flush()

    def log_event(self, event: str, detail: str, cycles: int, live_run_id: str | None = None) -> None:
        self.events.writerow([self.timestamp(), live_run_id or "", event, detail, cycles])
        self.event_file.flush()

    def close(self) -> None:
        self.flush()
        self.sensor_file.close()
        self.event_file.close()
