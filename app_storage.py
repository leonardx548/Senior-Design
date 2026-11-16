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
CONFIG_FILENAMES = (
    "position_calibration.json",
    "pressure_calibration.json",
    "temperature_calibration.json",
    "cycle_stops.json",
    "run_output.json",
)
LIVE_RUNS_FILENAME = "live_runs.json"
CAMPAIGN_MANIFEST_FILENAME = "campaign_manifest.json"
CAMPAIGN_RUNS_DIRECTORY = "runs"
CAMPAIGN_CONFIGURATION_FILENAME = "campaign_configuration.json"


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
        config.mkdir(parents=True, exist_ok=True)

        migrated: list[str] = []
        for filename in CONFIG_FILENAMES:
            legacy = source_directory / filename
            destination = config / filename
            if legacy.is_file() and not destination.exists():
                # Copy, rather than move, so the first upgraded run never risks existing data.
                shutil.copy2(legacy, destination)
                migrated.append(filename)
        return cls(root=root, config=config, migrated_legacy_config=tuple(migrated))

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


def _campaign_slug(name: str) -> str:
    """Make a readable, Windows-safe campaign folder suffix."""
    cleaned = "".join(character if character.isalnum() else "_" for character in name.strip())
    return "_".join(part for part in cleaned.split("_") if part)[:48] or "Test"


@dataclass
class Campaign:
    """Durable parent record for one multi-session test campaign."""

    folder: Path
    manifest: dict[str, Any]

    @property
    def runs_root(self) -> Path:
        return self.folder / CAMPAIGN_RUNS_DIRECTORY

    @property
    def name(self) -> str:
        return str(self.manifest.get("name", self.folder.name))

    @property
    def campaign_id(self) -> str:
        return str(self.manifest.get("campaign_id", self.folder.name))

    @property
    def completed_cycles(self) -> int:
        try:
            return max(0, int(self.manifest.get("completed_cycles", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def active_test_seconds(self) -> float:
        try:
            return max(0.0, float(self.manifest.get("active_test_seconds", 0.0)))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def create(cls, output_root: Path, name: str) -> "Campaign":
        """Create a user-named campaign; sessions are created only when started."""
        display_name = name.strip()
        if not display_name:
            raise ValueError("Enter a campaign name.")
        output_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"Campaign_{timestamp}_{_campaign_slug(display_name)}"
        folder = output_root / base_name
        suffix = 2
        while folder.exists():
            folder = output_root / f"{base_name}_{suffix:02d}"
            suffix += 1
        folder.mkdir(parents=True)
        (folder / CAMPAIGN_RUNS_DIRECTORY).mkdir()
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "campaign_id": folder.name,
            "name": display_name,
            "status": "active",
            "created_at": created_at,
            "completed_at": None,
            "completed_cycles": 0,
            "active_test_seconds": 0.0,
            "sessions": [],
        }
        write_json_atomic(folder / CAMPAIGN_MANIFEST_FILENAME, manifest)
        return cls(folder=folder, manifest=manifest)

    @classmethod
    def open(cls, folder: Path) -> "Campaign" | None:
        try:
            manifest = json.loads((folder / CAMPAIGN_MANIFEST_FILENAME).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                return None
            if not str(manifest.get("campaign_id", "")).strip():
                return None
            return cls(folder=folder, manifest=manifest)
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def available(cls, output_root: Path) -> list["Campaign"]:
        """Return explicit campaign folders newest first; legacy runs are excluded."""
        try:
            campaigns = (cls.open(folder) for folder in output_root.glob("Campaign_*") if folder.is_dir())
            return sorted((campaign for campaign in campaigns if campaign is not None), key=lambda item: item.folder.name, reverse=True)
        except OSError:
            return []

    def _save(self) -> None:
        write_json_atomic(self.folder / CAMPAIGN_MANIFEST_FILENAME, self.manifest)

    def read_configuration(self) -> dict[str, Any] | None:
        """Return the campaign's portable setup snapshot, if one was saved."""
        try:
            values = json.loads((self.folder / CAMPAIGN_CONFIGURATION_FILENAME).read_text(encoding="utf-8"))
            return values if isinstance(values, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def write_configuration(self, configuration: dict[str, Any]) -> None:
        """Persist the setup that must travel with this campaign package."""
        write_json_atomic(self.folder / CAMPAIGN_CONFIGURATION_FILENAME, configuration)

    def begin_session(self, run_folder: Path, completed_cycles_at_start: int) -> str:
        sessions = self.manifest.setdefault("sessions", [])
        if not isinstance(sessions, list):
            sessions = []
            self.manifest["sessions"] = sessions
        # A power loss cannot run the normal close handler. Preserve that fact
        # before opening the next dated run rather than silently treating the
        # abandoned session as healthy or deleting its durable CSV rows.
        for session in sessions:
            if isinstance(session, dict) and session.get("status") == "active":
                session["status"] = "interrupted_before_resume"
                session["recovered_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        session_id = f"session_{len(sessions) + 1:03d}"
        sessions.append(
            {
                "id": session_id,
                "run_folder": run_folder.name,
                "status": "active",
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "ended_at": None,
                "completed_cycles_at_start": int(completed_cycles_at_start),
                "completed_cycles_at_end": None,
            }
        )
        self._save()
        return session_id

    def record_progress(self, completed_cycles: int, active_test_seconds: float) -> None:
        """Persist campaign totals after a completed mechanical cycle."""
        self.manifest["completed_cycles"] = max(0, int(completed_cycles))
        self.manifest["active_test_seconds"] = max(0.0, round(float(active_test_seconds), 1))
        self._save()

    def finish_session(self, session_id: str, *, status: str, completed_cycles: int, active_test_seconds: float) -> None:
        sessions = self.manifest.get("sessions", [])
        if isinstance(sessions, list):
            for session in sessions:
                if isinstance(session, dict) and session.get("id") == session_id:
                    session.update(
                        status=status,
                        ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        completed_cycles_at_end=int(completed_cycles),
                    )
                    break
        self.manifest["completed_cycles"] = max(0, int(completed_cycles))
        self.manifest["active_test_seconds"] = max(0.0, round(float(active_test_seconds), 1))
        self._save()

    def finish(self) -> None:
        self.manifest.update(
            status="completed",
            completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._save()

class CsvLogger:
    """Keeps one durable dated test run in its own folder."""

    def __init__(self, output_root: Path) -> None:
        output_root.mkdir(parents=True, exist_ok=True)
        base_name = datetime.now().strftime("Run_%Y%m%d_%H%M%S")
        folder = output_root / base_name
        suffix = 2
        while folder.exists():
            folder = output_root / f"{base_name}_{suffix:02d}"
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
                "pressure_time_to_1500_psi_seconds",
                "pressure_response_threshold_psi",
                "pressure_response_window_seconds",
                "pressure_sample_count",
                "overlapping_grease_event_ids",
                "comparison_quality",
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

    def _write_manifest(self, values: dict[str, Any]) -> None:
        write_json_atomic(self.manifest_path, values)

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
        write_json_atomic(target, values)

    @staticmethod
    def read_live_runs(folder: Path) -> list[dict[str, Any]]:
        """Return the retained test-round metadata for a direct run folder."""
        try:
            values = json.loads((folder / LIVE_RUNS_FILENAME).read_text(encoding="utf-8"))
            return values if isinstance(values, list) else []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _live_runs(self) -> list[dict[str, Any]]:
        return self.read_live_runs(self.folder)

    def _write_live_runs(self, runs: list[dict[str, Any]]) -> None:
        write_json_atomic(self.live_runs_path, runs)

    def begin_live_run(self, configuration: dict[str, Any], completed_cycles_at_start: int) -> dict[str, Any]:
        """Initialize the single logical run recorded in this dated folder."""
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
            "configuration": configuration,
        }
        runs.append(run)
        self._write_live_runs(runs)
        return run

    def _update_live_run(self, run_id: str, **values: Any) -> None:
        """Atomically update one run's compact metadata record."""
        runs = self._live_runs()
        for run in runs:
            if run.get("id") == run_id:
                run.update(values)
                break
        self._write_live_runs(runs)

    def finish_live_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_cycles: int,
        cycle_frequency_cpm: float,
    ) -> None:
        self._update_live_run(
            run_id,
            status=status,
            ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            completed_cycles_at_end=completed_cycles,
            cycle_frequency_cpm_at_end=cycle_frequency_cpm,
        )

    def update_live_run_configuration(self, run_id: str, configuration: dict[str, Any]) -> None:
        """Refresh the exported per-run configuration after campaign session creation."""
        self._update_live_run(run_id, configuration=configuration)

    def flush(self) -> None:
        for output in (self.cycle_file, self.grease_file, self.event_file):
            output.flush()
            os.fsync(output.fileno())

    def finalize(self, **values: Any) -> None:
        self.flush()
        self.update_manifest(
            status="completed",
            ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            wall_duration_seconds=round(time.monotonic() - self.started_monotonic, 1),
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
                values["pressure_response_threshold_psi"],
                values["pressure_response_window_seconds"],
                values["pressure_sample_count"],
                values["overlapping_grease_event_ids"],
                values["comparison_quality"],
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
