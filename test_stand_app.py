"""Senior Design Test Stand interface with live read-only LabJack T4 AIN2 position.

The bushing position and cycle count use the physical string potentiometer.
Auxiliary pressure and temperature channels stay visible until their hardware
inputs are configured; the application never generates placeholder readings.
"""

from __future__ import annotations

import json
import csv
import os
import statistics
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape as xml_escape
from io import StringIO
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from app_storage import AppPaths, CsvLogger
from labjack_diagnostic import summarize_ain2_samples


UPDATE_MS = 100
APP_VERSION = "0.3.10"
AIN2_POLL_SECONDS = 0.75
INJECTOR_ACTIVATION_PSI = 2500.0
GREASE_COMPARISON_CYCLE_COUNT = 3
GREASE_PRESSURE_RESPONSE_WINDOW_SECONDS = 10.0
SNAPSHOT_HISTORY_SECONDS = 30
SNAPSHOT_STREAM_RATE_HZ = 200
SNAPSHOT_HISTORY_MAX_SAMPLES = SNAPSHOT_HISTORY_SECONDS * SNAPSHOT_STREAM_RATE_HZ
AUTO_ARCHIVE_INTERVAL_SECONDS = 60 * 60
AUTO_ARCHIVE_INTERVAL_OPTIONS = (
    ("15 minutes", 15 * 60),
    ("30 minutes", 30 * 60),
    ("1 hour", AUTO_ARCHIVE_INTERVAL_SECONDS),
    ("2 hours", 2 * 60 * 60),
)
AUTO_ARCHIVE_CONFIG_FILENAME = "auto_archive.json"
VELOCITY_SLOPE_WINDOW_SAMPLES = 11
VELOCITY_ZERO_DEADBAND_DPS = 0.25
DEFAULT_VELOCITY_GRAPH_LIMIT_DPS = 100.0
CYCLE_MID_STROKE_FRACTION = 0.05
MIN_CALIBRATION_VOLTAGE_SPAN = 0.050
MIN_CYCLE_STOP_SEPARATION_DEGREES = 1.0
CALIBRATION_STREAM_SAMPLE_COUNT = 100
LABJACK_LJM_WINDOWS_URL = "https://support.labjack.com/docs/ljm-software-installer-windows"
GITHUB_RELEASES_URL = "https://github.com/leonardx548/Senior-Design/releases/latest"

EXPORT_FILE_OPTIONS = (
    ("cycles", "Cycle summary", "cycle_summary.csv"),
    ("grease", "Grease events", "grease_events.csv"),
    ("events", "Event log", "events.csv"),
    ("metadata", "Run metadata", "live_run.json"),
    ("configuration", "Run configuration", "run_configuration.json"),
)
DEFAULT_AUTO_ARCHIVE_INCLUDE = ("cycles", "grease")
AUTO_ARCHIVE_CONFIG_SCHEMA_VERSION = 2


def auto_archive_interval_label(interval_seconds: int) -> str:
    for label, value in AUTO_ARCHIVE_INTERVAL_OPTIONS:
        if value == interval_seconds:
            return label
    return "1 hour"


@dataclass
class CycleCapture:
    """In-memory metrics for one reset-stop to count-stop mechanical stroke."""

    reset_stop_reached_at: str
    reset_stop_monotonic: float
    reset_stop_degrees: float
    count_stop_degrees: float
    direction: int
    stream_gap_detected: bool = False
    motion_started_at: str | None = None
    motion_started_monotonic: float | None = None
    near_count_stop_at: str | None = None
    near_count_stop_monotonic: float | None = None
    peak_velocity: float | None = None
    peak_signed_velocity: float | None = None
    mid_stroke_velocities: list[float] = field(default_factory=list)
    grease_event_ids: list[str] = field(default_factory=list)

    def observe(self, angle: float, velocity: float | None, sample_at: str, sample_monotonic: float) -> None:
        if velocity is not None and (self.peak_velocity is None or abs(velocity) > self.peak_velocity):
            self.peak_velocity = abs(velocity)
            self.peak_signed_velocity = velocity

        span = self.count_stop_degrees - self.reset_stop_degrees
        if not span:
            return
        progress = (angle - self.reset_stop_degrees) / span
        if progress >= CYCLE_MID_STROKE_FRACTION and self.motion_started_monotonic is None:
            self.motion_started_at = sample_at
            self.motion_started_monotonic = sample_monotonic
        if progress >= 1.0 - CYCLE_MID_STROKE_FRACTION and self.near_count_stop_monotonic is None:
            self.near_count_stop_at = sample_at
            self.near_count_stop_monotonic = sample_monotonic
        if CYCLE_MID_STROKE_FRACTION <= progress <= 1.0 - CYCLE_MID_STROKE_FRACTION and velocity is not None:
            # Normalize to the configured stroke direction, so both wiring/calibration
            # orientations export a positive travel-speed magnitude.
            self.mid_stroke_velocities.append(max(0.0, velocity * self.direction))

    def completed_values(
        self,
        *,
        completed_at: str,
        completed_monotonic: float,
        cycle_number: int,
        live_run_cycle_number: int,
        frequency: float,
    ) -> dict[str, float | int | str]:
        quality: list[str] = []
        if self.stream_gap_detected:
            quality.append("AIN2 stream gap")
        if not self.mid_stroke_velocities:
            quality.append("mid-stroke velocity unavailable")
        if self.motion_started_monotonic is None or self.near_count_stop_monotonic is None:
            quality.append("mid-stroke timing unavailable")
        return {
            "completed_at": completed_at,
            "cycle_number": cycle_number,
            "live_run_cycle_number": live_run_cycle_number,
            "reset_stop_reached_at": self.reset_stop_reached_at,
            "motion_started_at": self.motion_started_at or "",
            "count_stop_reached_at": completed_at,
            "reset_to_count_duration_seconds": round(completed_monotonic - self.reset_stop_monotonic, 4),
            "mid_stroke_duration_seconds": (
                "" if self.motion_started_monotonic is None or self.near_count_stop_monotonic is None
                else round(self.near_count_stop_monotonic - self.motion_started_monotonic, 4)
            ),
            "travel_direction": "increasing angle" if self.direction > 0 else "decreasing angle",
            "reset_stop_degrees": round(self.reset_stop_degrees, 4),
            "count_stop_degrees": round(self.count_stop_degrees, 4),
            "bushing_peak_velocity": "" if self.peak_velocity is None else round(self.peak_velocity, 4),
            "bushing_peak_signed_velocity": "" if self.peak_signed_velocity is None else round(self.peak_signed_velocity, 4),
            "bushing_median_travel_velocity": (
                "" if not self.mid_stroke_velocities else round(statistics.median(self.mid_stroke_velocities), 4)
            ),
            "bushing_velocity_sample_count": len(self.mid_stroke_velocities),
            "grease_event_ids": ";".join(self.grease_event_ids),
            # These are deliberately blank until physical temperature inputs exist.
            "temp_1": "",
            "temp_2": "",
            "frequency": round(frequency, 4),
            "bushing_data_quality": "; ".join(quality) if quality else "complete",
        }


@dataclass
class GreaseEventCapture:
    """One real injection and the nearby cycles used to measure its performance effect."""

    event_id: str
    started_at: str
    started_monotonic: float
    injection_cycle_number: int
    injection_cycle_phase: str
    commanded_pulse_seconds: float | str
    pressure_baseline: float | None
    pre_cycles: tuple[dict[str, float | int | str], ...]
    post_cycles: list[dict[str, float | int | str]] = field(default_factory=list)
    pressure_peak: float | None = None
    pressure_peak_at: str | None = None
    pressure_time_to_threshold: float | None = None
    pressure_sample_count: int = 0

    @staticmethod
    def _median(cycles: tuple[dict[str, float | int | str], ...] | list[dict[str, float | int | str]], key: str) -> float | str:
        values: list[float] = []
        for cycle in cycles:
            try:
                values.append(float(cycle[key]))
            except (KeyError, TypeError, ValueError):
                continue
        return "" if not values else round(statistics.median(values), 4)

    @staticmethod
    def _percent_change(before: float | str, after: float | str) -> float | str:
        if not isinstance(before, float) or not isinstance(after, float) or not before:
            return ""
        return round((after - before) / before * 100.0, 4)

    def record_pressure(self, pressure_psi: float, sample_at: str, sample_monotonic: float) -> None:
        if sample_monotonic - self.started_monotonic > GREASE_PRESSURE_RESPONSE_WINDOW_SECONDS:
            return
        self.pressure_sample_count += 1
        if self.pressure_peak is None or pressure_psi > self.pressure_peak:
            self.pressure_peak = pressure_psi
            self.pressure_peak_at = sample_at
        if self.pressure_time_to_threshold is None and pressure_psi >= INJECTOR_ACTIVATION_PSI:
            self.pressure_time_to_threshold = round(sample_monotonic - self.started_monotonic, 4)

    def values(self, *, status: str) -> dict[str, float | int | str]:
        pre_velocity = self._median(self.pre_cycles, "bushing_median_travel_velocity")
        post_velocity = self._median(self.post_cycles, "bushing_median_travel_velocity")
        pre_duration = self._median(self.pre_cycles, "reset_to_count_duration_seconds")
        post_duration = self._median(self.post_cycles, "reset_to_count_duration_seconds")
        return {
            "grease_event_id": self.event_id,
            "injection_started_at": self.started_at,
            "injection_cycle_number": self.injection_cycle_number,
            "injection_cycle_phase": self.injection_cycle_phase,
            "commanded_pulse_seconds": self.commanded_pulse_seconds,
            "pre_injection_cycle_count": len(self.pre_cycles),
            "pre_median_travel_velocity": pre_velocity,
            "pre_median_reset_to_count_duration": pre_duration,
            "post_injection_cycle_count": len(self.post_cycles),
            "post_median_travel_velocity": post_velocity,
            "post_median_reset_to_count_duration": post_duration,
            "median_travel_velocity_change_percent": self._percent_change(pre_velocity, post_velocity),
            "reset_to_count_duration_change_percent": self._percent_change(pre_duration, post_duration),
            "pressure_baseline": "" if self.pressure_baseline is None else round(self.pressure_baseline, 4),
            "pressure_peak": "" if self.pressure_peak is None else round(self.pressure_peak, 4),
            "pressure_peak_at": self.pressure_peak_at or "",
            "pressure_time_to_threshold": (
                "" if self.pressure_time_to_threshold is None else self.pressure_time_to_threshold
            ),
            "pressure_response_window_seconds": GREASE_PRESSURE_RESPONSE_WINDOW_SECONDS,
            "pressure_sample_count": self.pressure_sample_count,
            "analysis_status": status,
        }


@dataclass(frozen=True)
class PositionVelocitySnapshotSample:
    """One calibrated raw-stream sample retained only for an on-demand snapshot."""

    timestamp: str
    monotonic_seconds: float
    scan_rate_hz: float
    ain2_voltage: float
    position_degrees: float
    velocity_degrees_per_second: float | None
    stream_gap_before: bool = False


def format_local_timestamp(value: object, fallback: str = "Not available") -> str:
    """Render saved ISO timestamps compactly for operator-facing lists."""
    try:
        timestamp = datetime.fromisoformat(str(value))
        return timestamp.astimezone().strftime("%Y-%m-%d %I:%M %p")
    except (TypeError, ValueError):
        return fallback


def filtered_live_run_csv(folder: Path, filename: str, run_id: str) -> str:
    """Return one logical run's CSV rows with their original header."""
    source = folder / filename
    if not source.is_file():
        return ""
    output = StringIO(newline="")
    with source.open("r", newline="", encoding="utf-8") as source_file:
        reader = csv.DictReader(source_file)
        if not reader.fieldnames or "live_run_id" not in reader.fieldnames:
            return ""
        writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in reader:
            if row.get("live_run_id") == run_id:
                writer.writerow(row)
    return output.getvalue()


def write_live_run_archive(
    destination: Path,
    selected_runs: list[tuple[Path, dict[str, object]]],
    selected_keys: set[str],
    *,
    manifest_extra: dict[str, object] | None = None,
) -> int:
    """Write and verify a self-contained ZIP before replacing its destination."""
    options = [option for option in EXPORT_FILE_OPTIONS if option[0] in selected_keys]
    if not selected_runs:
        raise ValueError("At least one live run is required for an archive.")
    if not options:
        raise ValueError("Choose at least one data type for the archive.")
    if not destination.parent.is_dir():
        raise OSError(f"Archive folder is unavailable: {destination.parent}")

    manifest = {
        "schema_version": 4,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_application": "Senior Design Test Stand",
        "selected_live_runs": [
            {"application_session": folder.name, "live_run_id": run.get("id")} for folder, run in selected_runs
        ],
        "included_file_types": [filename for _key, _label, filename in options],
    }
    if manifest_extra:
        manifest.update(manifest_extra)

    temporary = destination.with_name(f".{destination.name}.{time.time_ns()}.tmp")
    count = 0
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("export_manifest.json", json.dumps(manifest, indent=2))
            count += 1
            for folder, run in selected_runs:
                run_id = str(run["id"])
                archive_folder = f"{folder.name}/{run_id}"
                for key, _label, filename in options:
                    archive_filename = filename
                    if key in {"cycles", "grease", "events"}:
                        contents = filtered_live_run_csv(folder, filename, run_id)
                        # Older local sessions used the retired snapshot log. Keep
                        # those records exportable without producing a mislabeled CSV.
                        if key == "cycles" and not contents and (folder / "sensor_readings.csv").is_file():
                            archive_filename = "sensor_readings.csv"
                            contents = filtered_live_run_csv(folder, archive_filename, run_id)
                    elif key == "metadata":
                        contents = json.dumps(run, indent=2)
                    else:
                        contents = json.dumps(run.get("configuration", {}), indent=2)
                    archive.writestr(f"{archive_folder}/{archive_filename}", contents)
                    count += 1
        with zipfile.ZipFile(temporary, "r") as archive:
            if archive.testzip() is not None:
                raise OSError("The archive failed ZIP integrity verification.")
        temporary.replace(destination)
        return count
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class LivePlot(tk.Canvas):
    """A small dependency-free live line chart."""

    def __init__(self, parent: tk.Misc, title: str, maximum: float, color: str, minimum: float = 0.0) -> None:
        super().__init__(parent, height=190, background="#ffffff", highlightthickness=1,
                         highlightbackground="#cbd5e1")
        self.title = title
        self.minimum = minimum
        self.maximum = maximum
        self.color = color
        self.samples: deque[tuple[float, float]] = deque()
        self.history_seconds = SNAPSHOT_HISTORY_SECONDS
        self.reference_lines: list[tuple[float, str, str]] = []
        self.bind("<Configure>", lambda _event: self.redraw())

    def add(self, value: float, sample_monotonic: float | None = None) -> None:
        sample_time = time.monotonic() if sample_monotonic is None else sample_monotonic
        self.samples.append((sample_time, max(self.minimum, min(self.maximum, value))))
        while self.samples and self.samples[0][0] < sample_time - self.history_seconds:
            self.samples.popleft()
        self.redraw()

    def set_scale(self, title: str, minimum: float, maximum: float, clear_samples: bool = True) -> None:
        self.title = title
        self.minimum = minimum
        self.maximum = maximum if maximum > minimum else minimum + 1.0
        if clear_samples:
            self.samples.clear()
        self.redraw()

    def set_reference_lines(self, lines: list[tuple[float, str, str]]) -> None:
        """Draw labeled horizontal thresholds without affecting live samples."""
        self.reference_lines = lines
        self.redraw()

    def snapshot_state(self) -> dict[str, object]:
        """Capture the displayed graph state without depending on a screen capture tool."""
        return {
            "title": self.title,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "color": self.color,
            "samples": tuple(self.samples),
            "reference_lines": tuple(self.reference_lines),
            "history_seconds": self.history_seconds,
            "window_end_monotonic": self.samples[-1][0] if self.samples else time.monotonic(),
            "width": max(self.winfo_width(), 600),
            "height": max(self.winfo_height(), 240),
        }

    @staticmethod
    def write_svg(path: Path, state: dict[str, object]) -> None:
        """Write a portable static chart that matches a saved dashboard graph."""
        width, height = int(state["width"]), int(state["height"])
        minimum, maximum = float(state["minimum"]), float(state["maximum"])
        title, color = str(state["title"]), str(state["color"])
        samples = tuple((float(sample[0]), float(sample[1])) for sample in state["samples"])
        reference_lines = tuple(state["reference_lines"])
        history_seconds = float(state["history_seconds"])
        window_end = float(state["window_end_monotonic"])
        window_start = window_end - history_seconds
        margin_left, margin_right, margin_top, margin_bottom = 42, 14, 28, 28
        chart_width = width - margin_left - margin_right
        chart_height = height - margin_top - margin_bottom
        lines = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
            f'<text x="12" y="16" fill="#0f172a" font-family="Segoe UI, Arial" font-size="12" font-weight="700">{xml_escape(title)}</text>',
        ]
        for fraction in (0, 0.5, 1):
            y = margin_top + chart_height * (1 - fraction)
            value = minimum + (maximum - minimum) * fraction
            lines.extend((
                f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" stroke="#e2e8f0"/>',
                f'<text x="{margin_left - 5}" y="{y + 3:.2f}" text-anchor="end" fill="#64748b" font-family="Segoe UI, Arial" font-size="9">{value:,.1f}</text>',
            ))
        lines.extend((
            f'<text x="{margin_left}" y="{height - 8}" fill="#64748b" font-family="Segoe UI, Arial" font-size="9">{history_seconds:g}s ago</text>',
            f'<text x="{width - margin_right}" y="{height - 8}" text-anchor="end" fill="#64748b" font-family="Segoe UI, Arial" font-size="9">now</text>',
        ))
        for reference in reference_lines:
            value, reference_color = float(reference[0]), str(reference[1])
            if minimum <= value <= maximum:
                y = margin_top + chart_height * (1 - (value - minimum) / (maximum - minimum))
                lines.append(
                    f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" stroke="{reference_color}" stroke-width="2" stroke-dasharray="5 3"/>'
                )
        if len(samples) >= 2:
            points = []
            for sample_time, value in samples:
                x = margin_left + chart_width * max(0.0, min(1.0, (sample_time - window_start) / history_seconds))
                y = margin_top + chart_height * (1 - (value - minimum) / (maximum - minimum))
                points.append(f"{x:.2f},{y:.2f}")
            lines.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2"/>')
        lines.append("</svg>")
        path.write_text("\n".join(lines), encoding="utf-8")

    def redraw(self) -> None:
        self.delete("all")
        width, height = max(self.winfo_width(), 100), max(self.winfo_height(), 100)
        margin_left, margin_right, margin_top, margin_bottom = 42, 14, 28, 28
        chart_width = width - margin_left - margin_right
        chart_height = height - margin_top - margin_bottom

        self.create_text(12, 12, text=self.title, anchor="w", fill="#0f172a",
                         font=("Segoe UI", 10, "bold"))
        for fraction in (0, 0.5, 1):
            y = margin_top + chart_height * (1 - fraction)
            self.create_line(margin_left, y, width - margin_right, y, fill="#e2e8f0")
            value = self.minimum + (self.maximum - self.minimum) * fraction
            self.create_text(margin_left - 5, y, text=f"{value:,.1f}",
                             anchor="e", fill="#64748b", font=("Segoe UI", 8))
        window_end = self.samples[-1][0] if self.samples else time.monotonic()
        window_start = window_end - self.history_seconds
        self.create_text(margin_left, height - 9, text=f"{self.history_seconds:g}s ago", anchor="w", fill="#64748b",
                         font=("Segoe UI", 8))
        self.create_text(width - margin_right, height - 9, text="now", anchor="e", fill="#64748b",
                         font=("Segoe UI", 8))
        for value, color, _label in self.reference_lines:
            if not self.minimum <= value <= self.maximum:
                continue
            y = margin_top + chart_height * (1 - (value - self.minimum) / (self.maximum - self.minimum))
            self.create_line(margin_left, y, width - margin_right, y, fill=color, width=2, dash=(5, 3))
        if len(self.samples) < 2:
            return
        points: list[float] = []
        for sample_time, value in self.samples:
            x = margin_left + chart_width * max(0.0, min(1.0, (sample_time - window_start) / self.history_seconds))
            y = margin_top + chart_height * (1 - (value - self.minimum) / (self.maximum - self.minimum))
            points.extend([x, y])
        self.create_line(*points, fill=self.color, width=2, smooth=True)


class LabJackAin2Monitor:
    """Consume a persistent read-only AIN2 helper process without blocking Tk."""

    def __init__(self) -> None:
        # Keep every batch until Tk has processed its counting samples.
        self._results: Queue[dict[str, object]] = Queue()
        self._process: subprocess.Popen[str] | None = None
        self._enabled = True
        self._stream_samples: deque[tuple[float, float]] = deque(maxlen=1000)
        self._samples_lock = threading.Lock()

    @property
    def is_reading(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start_read(self) -> None:
        if not self._enabled or self.is_reading:
            return
        with self._samples_lock:
            self._stream_samples.clear()
        try:
            if getattr(sys, "frozen", False):
                # The release package supplies a separate console helper so the
                # windowed dashboard can still consume its JSON stream safely.
                diagnostic_command = [
                    str(Path(sys.executable).with_name("TestStandLabJackDiagnostic.exe")),
                    "--stream-ain2",
                ]
            else:
                diagnostic = Path(__file__).with_name("labjack_diagnostic.py")
                diagnostic_command = [sys.executable, str(diagnostic), "--stream-ain2"]
            process = subprocess.Popen(
                diagnostic_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            self._put_result({"status": "error", "error": f"Could not start AIN2 reader: {error}"})
            return
        self._process = process
        threading.Thread(target=self._consume, args=(process,), daemon=True).start()

    def pause(self) -> None:
        self._enabled = False
        if self._process is not None and self._process.poll() is None:
            try:
                if self._process.stdin is None:
                    raise OSError("AIN2 helper control pipe is unavailable.")
                self._process.stdin.write("STOP\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                # Fall back only if the cooperative stop channel has already failed.
                self._process.terminate()

    def resume(self) -> None:
        self._enabled = True

    def close(self) -> None:
        self.pause()

    def drain_results(self) -> list[dict[str, object]]:
        """Return every pending stream batch in arrival order; do not skip count data."""
        results: list[dict[str, object]] = []
        try:
            while True:
                results.append(self._results.get_nowait())
        except Empty:
            return results

    def samples_since(self, started_at: float, count: int) -> list[float]:
        """Return fresh AIN2 stream samples for calibration from this same read path."""
        with self._samples_lock:
            samples = [value for timestamp, value in self._stream_samples if timestamp >= started_at]
        return samples[:count]

    def _put_result(self, result: dict[str, object]) -> None:
        if self._results.full():
            try:
                self._results.get_nowait()
            except Empty:
                pass
        self._results.put(result)

    def _consume(self, process: subprocess.Popen[str]) -> None:
        try:
            if process.stdout is None:
                raise OSError("AIN2 helper has no stdout pipe.")
            for line in process.stdout:
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if result.get("status") == "success":
                    timestamp = time.monotonic()
                    try:
                        samples = [float(value) for value in result.get("samples", [])]
                    except (TypeError, ValueError):
                        samples = []
                    if samples:
                        with self._samples_lock:
                            self._stream_samples.extend((timestamp, value) for value in samples)
                self._put_result(result)
        except OSError as error:
            self._put_result({"status": "error", "error": f"AIN2 live reader failed: {error}"})
        finally:
            if self._process is process:
                self._process = None
            exit_code = process.poll()
            if self._enabled and exit_code not in (0, None):
                self._put_result({"status": "error", "error": "AIN2 live reader stopped unexpectedly."})


class PositionCalibrationDialog(tk.Toplevel):
    """Two-point AIN2-to-angle calibration using stable short sample bursts."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Calibrate Bushing Position — LabJack AIN2")
        self.resizable(False, False)
        self.configure(background="#f1f5f9")
        self.points: dict[int, dict[str, float]] = {}
        self._sampling = False
        self._pending_point: int | None = None
        self._pending_angle: float | None = None
        self._sample_started_at = 0.0
        self.angle_vars = {1: tk.StringVar(), 2: tk.StringVar()}
        self.point_vars = {1: tk.StringVar(value="Not captured"), 2: tk.StringVar(value="Not captured")}
        self.preview_var = tk.StringVar(value="No AIN2 reading captured yet.")
        self.status_var = tk.StringVar(value="Enter a known bushing angle, hold it still, then capture each point.")
        self._build()
        self._load_existing_points()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="AIN2 bushing-position calibration", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Hold the bushing still while capturing each known angle. Unstable readings are rejected.",
            wraplength=610,
            foreground="#475569",
        ).pack(anchor="w", pady=(4, 14))

        for point in (1, 2):
            frame = ttk.LabelFrame(outer, text=f"Calibration point {point}", padding=12)
            frame.pack(fill="x", pady=(0, 10))
            ttk.Label(frame, text="Known bushing angle (degrees):").grid(row=0, column=0, sticky="w")
            ttk.Entry(frame, textvariable=self.angle_vars[point], width=12).grid(row=0, column=1, padx=8)
            button = ttk.Button(frame, text=f"Capture Point {point}", command=lambda p=point: self.capture_point(p))
            button.grid(row=0, column=2, padx=(8, 0))
            ttk.Label(frame, textvariable=self.point_vars[point], foreground="#475569").grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(8, 0)
            )

        preview = ttk.LabelFrame(outer, text="AIN2 check", padding=12)
        preview.pack(fill="x", pady=(0, 10))
        ttk.Button(preview, text="Read AIN2 Now", command=self.preview_ain2).pack(side="left")
        ttk.Label(preview, textvariable=self.preview_var, wraplength=475, foreground="#475569").pack(
            side="left", padx=12
        )

        self.status_label = ttk.Label(outer, textvariable=self.status_var, wraplength=610, foreground="#1e3a8a")
        self.status_label.pack(anchor="w", pady=(2, 12))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Apply and Save Calibration", style="Grease.TButton", command=self.apply_calibration).pack(
            side="left"
        )
        ttk.Button(buttons, text="Clear Saved Calibration", command=self.clear_calibration).pack(side="left", padx=8)

    def close(self) -> None:
        if self._sampling:
            self.status_var.set("AIN2 sampling is still in progress. Please wait a moment.")
            self.status_label.configure(foreground="#1e3a8a")
            return
        self.destroy()

    def _load_existing_points(self) -> None:
        existing = self.app.position_calibration
        if not existing:
            return
        for point, values in ((1, existing["point_1"]), (2, existing["point_2"])):
            self.points[point] = dict(values)
            self.angle_vars[point].set(f"{values['angle']:.3f}")
            self._set_point_label(point)
        self.status_var.set("Saved calibration loaded. Capture a point again to replace it.")

    def _set_point_label(self, point: int) -> None:
        values = self.points[point]
        self.point_vars[point].set(
            f"Captured {values['voltage']:.4f} V at {values['angle']:.3f}° "
            f"(σ {values['standard_deviation'] * 1000:.1f} mV, span {values['span'] * 1000:.1f} mV)"
        )

    def _parse_angle(self, point: int) -> float | None:
        try:
            return float(self.angle_vars[point].get())
        except ValueError:
            messagebox.showerror("Angle required", "Enter a numeric known bushing angle in degrees before capturing.", parent=self)
            return None

    def capture_point(self, point: int) -> None:
        angle = self._parse_angle(point)
        if angle is not None:
            self._start_sample(point, angle)

    def preview_ain2(self) -> None:
        self._start_sample(None, None)

    def _start_sample(self, point: int | None, angle: float | None) -> None:
        if self._sampling:
            return
        if not self.app.ain2_monitor.is_reading:
            self.status_var.set("Waiting for the live AIN2 stream…")
            self.status_label.configure(foreground="#1e3a8a")
            self.after(100, lambda: self._start_sample(point, angle))
            return
        self._sampling = True
        self._pending_point, self._pending_angle = point, angle
        self._sample_started_at = time.monotonic()
        self.status_var.set("Sampling AIN2… hold the bushing still.")
        self.status_label.configure(foreground="#1e3a8a")
        self.after(550, self._check_stream_sample)

    def _check_stream_sample(self) -> None:
        samples = self.app.ain2_monitor.samples_since(self._sample_started_at, CALIBRATION_STREAM_SAMPLE_COUNT)
        if len(samples) < CALIBRATION_STREAM_SAMPLE_COUNT:
            if time.monotonic() - self._sample_started_at < 3.0:
                self.after(60, self._check_stream_sample)
                return
            result: dict[str, object] = {"status": "error", "error": "Live AIN2 samples were not available."}
        else:
            result = summarize_ain2_samples(samples)
        self._sampling = False
        point, angle = self._pending_point, self._pending_angle
        self._pending_point, self._pending_angle = None, None
        if result.get("status") != "success":
            reason = result.get("error") or (
                "Signal changed too much during the capture. Hold the bushing still and try again."
            )
            self.status_var.set(f"Capture not accepted: {reason}")
            self.status_label.configure(foreground="#b91c1c")
            return

        voltage = float(result["voltage"])
        standard_deviation = float(result["standard_deviation"])
        span = float(result["span"])
        if point is not None and angle is not None:
            self.points[point] = {
                "angle": angle,
                "voltage": voltage,
                "standard_deviation": standard_deviation,
                "span": span,
            }
            self._set_point_label(point)
            self.status_var.set(f"Point {point} captured. Capture the other point, then apply the calibration.")
            self.app.log_event("AIN2 calibration point captured", f"Point {point}: {voltage:.4f} V at {angle:.3f}°.")
        else:
            text = f"AIN2: {voltage:.4f} V (σ {standard_deviation * 1000:.1f} mV, span {span * 1000:.1f} mV)"
            calibration = self.app.position_calibration
            if calibration:
                estimated_angle, outside = self.app.angle_from_voltage(voltage, calibration)
                text += f"  →  {estimated_angle:.3f}°" + (" (outside calibrated span)" if outside else "")
            self.preview_var.set(text)
            self.status_var.set("AIN2 reading accepted.")
        self.status_label.configure(foreground="#166534")

    def apply_calibration(self) -> None:
        if set(self.points) != {1, 2}:
            messagebox.showerror("Two points needed", "Capture both calibration points before applying.", parent=self)
            return
        first, second = self.points[1], self.points[2]
        voltage_span = abs(second["voltage"] - first["voltage"])
        if voltage_span < MIN_CALIBRATION_VOLTAGE_SPAN:
            messagebox.showerror(
                "Points too close",
                "The two captured voltages are too close together. Move the bushing farther before capturing again.",
                parent=self,
            )
            return
        if first["angle"] == second["angle"]:
            messagebox.showerror("Angles must differ", "Enter two different known angular positions.", parent=self)
            return
        calibration = {"version": 1, "point_1": first, "point_2": second}
        self.app.save_position_calibration(calibration)
        self.status_var.set("Calibration saved. AIN2 previews now show an interpolated bushing angle.")
        self.status_label.configure(foreground="#166534")
        self.app.log_event("AIN2 calibration saved", "Two-point linear AIN2-to-bushing-angle calibration saved.")

    def clear_calibration(self) -> None:
        if not self.app.position_calibration:
            return
        if messagebox.askyesno("Clear calibration", "Clear the saved AIN2 position calibration?", parent=self):
            self.app.clear_position_calibration()
            self.points.clear()
            for point in (1, 2):
                self.point_vars[point].set("Not captured")
                self.angle_vars[point].set("")
            self.preview_var.set("No AIN2 reading captured yet.")
            self.status_var.set("Saved calibration cleared.")


class CycleStopsDialog(tk.Toplevel):
    """Set the two real-position stops that arm and complete a counted cycle."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Set Cycle Stops")
        self.resizable(False, False)
        self.configure(background="#f1f5f9")
        self.transient(app)
        existing = app.cycle_stops or {}
        self.retracted_var = tk.StringVar(
            value="" if "retracted" not in existing else f"{float(existing['retracted']):.3f}"
        )
        self.extended_var = tk.StringVar(
            value="" if "extended" not in existing else f"{float(existing['extended']):.3f}"
        )
        self.current_var = tk.StringVar(value=self._current_position_text())
        self.status_var = tk.StringVar(
            value="Set the two angular positions that define one mechanical cycle."
        )
        self._build()
        self.lift()
        self.focus_force()

    def _current_position_text(self) -> str:
        if self.app.bushing_position is None or self.app.bushing_unit != "degrees":
            return "Live calibrated bushing angle is not available yet."
        return f"Current live bushing angle: {self.app.bushing_position:.3f}°"

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Cycle stop positions", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Reach Reset before Count to record one cycle. Stops may be entered in either direction.",
            wraplength=600, foreground="#475569",
        ).pack(anchor="w", pady=(4, 14))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        for row, (label, variable, color, target) in enumerate((
            ("Retracted / reset stop (green):", self.retracted_var, "#15803d", "retracted"),
            ("Extended / count stop (red):", self.extended_var, "#dc2626", "extended"),
        )):
            ttk.Label(form, text=label, foreground=color).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(form, textvariable=variable, width=13).grid(row=row, column=1, padx=(10, 5), pady=5)
            ttk.Label(form, text="degrees").grid(row=row, column=2, sticky="w", pady=5)
            ttk.Button(form, text="Use Current", command=lambda name=target: self.use_current(name)).grid(
                row=row, column=3, padx=(14, 0), pady=5
            )

        ttk.Label(outer, textvariable=self.current_var, foreground="#475569").pack(
            anchor="w", pady=(10, 4)
        )
        ttk.Label(outer, textvariable=self.status_var, wraplength=600, foreground="#1e3a8a").pack(
            anchor="w", pady=(0, 12)
        )
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save Cycle Stops", style="Grease.TButton", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Clear Stops", command=self.clear).pack(side="left", padx=8)

    def use_current(self, target: str) -> None:
        self.current_var.set(self._current_position_text())
        if self.app.bushing_position is None or self.app.bushing_unit != "degrees":
            self.status_var.set("Wait for a live calibrated AIN2 angle before using the current position.")
            return
        variable = self.retracted_var if target == "retracted" else self.extended_var
        variable.set(f"{self.app.bushing_position:.3f}")
        self.status_var.set(f"Current live angle copied to the {target} stop.")

    def save(self) -> None:
        try:
            retracted = float(self.retracted_var.get())
            extended = float(self.extended_var.get())
        except ValueError:
            messagebox.showerror("Angles required", "Enter numeric degrees for both cycle stops.", parent=self)
            return
        if abs(extended - retracted) < MIN_CYCLE_STOP_SEPARATION_DEGREES:
            messagebox.showerror(
                "Stops too close",
                f"Cycle stops must be at least {MIN_CYCLE_STOP_SEPARATION_DEGREES:g}° apart.",
                parent=self,
            )
            return
        calibration = self.app.position_calibration
        if calibration:
            calibrated_angles = (
                float(calibration["point_1"]["angle"]),
                float(calibration["point_2"]["angle"]),
            )
            low, high = min(calibrated_angles), max(calibrated_angles)
            if not (low <= retracted <= high and low <= extended <= high):
                messagebox.showerror(
                    "Stops outside calibration",
                    f"Both stops must be between the calibrated angles ({low:.3f}° to {high:.3f}°). "
                    "Recalibrate across the full mechanical travel first.",
                    parent=self,
                )
                return
        self.app.save_cycle_stops({"version": 1, "retracted": retracted, "extended": extended})
        self.current_var.set(self._current_position_text())
        self.status_var.set("Cycle stops saved. Reset and Count markers are shown on the graph.")

    def clear(self) -> None:
        if not self.app.cycle_stops:
            return
        if messagebox.askyesno("Clear cycle stops", "Clear the saved cycle stop positions?", parent=self):
            self.app.clear_cycle_stops()
            self.retracted_var.set("")
            self.extended_var.set("")
            self.status_var.set("Cycle stops cleared. Cycle counting is disabled.")


class ExportDataDialog(tk.Toplevel):
    """Export logical Start-to-Stop test runs retained in local staging."""

    FILE_OPTIONS = EXPORT_FILE_OPTIONS

    def __init__(self, app: "TestStandApp", preselect_run_id: str | None = None) -> None:
        super().__init__(app)
        self.app = app
        self.preselect_run_id = preselect_run_id
        self.title("Export Test Stand Data")
        self.geometry("880x650")
        self.minsize(760, 560)
        self.transient(app)
        self.option_vars = {key: tk.BooleanVar(value=True) for key, _label, _filename in self.FILE_OPTIONS}
        self.status_var = tk.StringVar(value="Choose one or more live test runs to create a ZIP archive.")
        self._build()
        self.refresh_runs()
        self.lift()
        self.focus_force()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Export live test runs", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Live runs begin with Start Counting and end with Stop or Reset. Retained local runs can be packaged here.",
            wraplength=710,
            foreground="#475569",
        ).pack(anchor="w", pady=(4, 10))

        runs_frame = ttk.Frame(outer)
        runs_frame.pack(fill="both", expand=True)
        ttk.Label(runs_frame, text="Live runs (newest first):").pack(anchor="w")
        list_frame = ttk.Frame(runs_frame)
        list_frame.pack(fill="both", expand=True, pady=(4, 10))
        self.runs_list = tk.Listbox(list_frame, selectmode="extended", exportselection=False, height=9)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.runs_list.yview)
        self.runs_list.configure(yscrollcommand=scrollbar.set)
        self.runs_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        ttk.Button(runs_frame, text="Refresh Run List", command=self.refresh_runs).pack(anchor="e")

        options = ttk.LabelFrame(outer, text="Include", padding=8)
        options.pack(fill="x", pady=(12, 8))
        for row, (key, label, _filename) in enumerate(self.FILE_OPTIONS):
            ttk.Checkbutton(options, text=label, variable=self.option_vars[key]).grid(
                row=row // 2, column=row % 2, sticky="w", padx=(0, 22), pady=2
            )
        ttk.Label(
            options,
            text="An archive manifest is included automatically.",
            foreground="#475569",
            wraplength=680,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(outer, textvariable=self.status_var, foreground="#1e3a8a", wraplength=710).pack(
            anchor="w", pady=(0, 8)
        )
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Export Selected…", style="Grease.TButton", command=self.export).pack(side="right")

    def refresh_runs(self) -> None:
        self.app.logger.flush()
        self.live_runs: list[tuple[Path, dict[str, object]]] = []
        for folder in self.app.paths.list_run_directories():
            for run in reversed(CsvLogger.read_live_runs(folder)):
                self.live_runs.append((folder, run))
        self.runs_list.delete(0, "end")
        selected_index: int | None = None
        for index, (folder, run) in enumerate(self.live_runs):
            run_id = str(run.get("id", "unknown"))
            state = str(run.get("status", "unknown")).replace("_", " ")
            export_note = "exported" if run.get("exported_at") else "not exported"
            current = " • active" if run_id == self.app.current_live_run_id and folder == self.app.logger.folder else ""
            completed = format_local_timestamp(run.get("ended_at"), fallback="In progress")
            self.runs_list.insert(
                "end",
                f"{folder.name}  |  {run_id}  |  Completed: {completed}  |  {state}, {export_note}{current}",
            )
            if run_id == self.preselect_run_id and folder == self.app.logger.folder:
                selected_index = index
        if self.live_runs:
            self.runs_list.selection_set(0 if selected_index is None else selected_index)
            self.status_var.set(f"{len(self.live_runs)} live run(s) found. Select the run data to package.")
        else:
            self.status_var.set("No live runs yet. Start and then stop counting to create an exportable run.")

    def export(self) -> None:
        selected_indexes = self.runs_list.curselection()
        selected_options = [option for option in self.FILE_OPTIONS if self.option_vars[option[0]].get()]
        if not selected_indexes:
            messagebox.showerror("Select a live run", "Select at least one live run to export.", parent=self)
            return
        if not selected_options:
            messagebox.showerror("Select data", "Choose at least one data type to include.", parent=self)
            return
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Save Test Stand Export",
            defaultextension=".zip",
            filetypes=(("ZIP archive", "*.zip"),),
            initialfile=f"TestStand_Export_{datetime.now():%Y%m%d_%H%M%S}.zip",
        )
        if not destination:
            return

        self.app.logger.flush()
        selected_runs = [self.live_runs[index] for index in selected_indexes]
        exported_run_ids_by_folder: dict[Path, set[str]] = {}
        try:
            count = write_live_run_archive(
                Path(destination),
                selected_runs,
                {key for key, _label, _filename in selected_options},
                manifest_extra={"archive_type": "manual"},
            )
            for folder, run in selected_runs:
                exported_run_ids_by_folder.setdefault(folder, set()).add(str(run["id"]))
        except (OSError, ValueError, csv.Error, zipfile.BadZipFile) as error:
            messagebox.showerror("Export failed", f"Could not create the ZIP archive:\n{error}", parent=self)
            return
        for folder, run_ids in exported_run_ids_by_folder.items():
            CsvLogger.mark_live_runs_exported_in_folder(folder, run_ids)
        self.status_var.set(f"Exported {count} file(s) from {len(selected_runs)} live run(s).")
        messagebox.showinfo(
            "Export complete",
            f"Created ZIP archive with {count} file(s) from {len(selected_runs)} live run(s).\n\n{destination}",
            parent=self,
        )
        self.refresh_runs()


class AutoArchiveDialog(tk.Toplevel):
    """Configure hands-off ZIP archives for active test time."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Configure Automatic Archives")
        self.geometry("760x520")
        self.minsize(680, 480)
        self.resizable(False, False)
        self.transient(app)
        config = app.auto_archive_config
        self.enabled_var = tk.BooleanVar(value=bool(config["enabled"]))
        self.directory_var = tk.StringVar(value=str(config["directory"]))
        self.interval_var = tk.StringVar(value=auto_archive_interval_label(int(config["interval_seconds"])))
        selected = set(config["include"])
        self.option_vars = {key: tk.BooleanVar(value=key in selected) for key, _label, _filename in EXPORT_FILE_OPTIONS}
        self.status_var = tk.StringVar(
            value="Settings changed during a test apply when the next test starts."
            if app.is_running else f"Archives are created after each active {self.interval_var.get().lower()}."
        )
        self._build()
        self.interval_var.trace_add("write", self._update_interval_status)
        self.lift()
        self.focus_force()

    def _update_interval_status(self, *_args: object) -> None:
        """Keep the timing summary aligned with the selected interval."""
        if not self.app.is_running:
            self.status_var.set(f"Archives are created after each active {self.interval_var.get().lower()}.")

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Automatic test archives", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "While cycle counting is active, the app creates a verified ZIP at the selected active-test interval. "
                "Stopping counting creates a final ZIP for the remaining portion. Local segment data is removed only "
                "after that archive succeeds."
            ),
            wraplength=700,
            foreground="#475569",
        ).pack(anchor="w", pady=(4, 10))

        ttk.Checkbutton(
            outer,
            text="Automatically archive active test data",
            variable=self.enabled_var,
        ).pack(anchor="w", pady=(0, 8))

        timing = ttk.LabelFrame(outer, text="Archive interval", padding=10)
        timing.pack(fill="x", pady=(0, 8))
        ttk.Label(timing, text="Create an archive every").pack(side="left")
        ttk.Combobox(
            timing,
            textvariable=self.interval_var,
            values=tuple(label for label, _seconds in AUTO_ARCHIVE_INTERVAL_OPTIONS),
            state="readonly",
            width=14,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(timing, text="of active test time").pack(side="left", padx=(5, 0))

        directory = ttk.LabelFrame(outer, text="Archive folder", padding=10)
        directory.pack(fill="x")
        directory.columnconfigure(0, weight=1)
        ttk.Entry(directory, textvariable=self.directory_var, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(directory, text="Choose Folder…", command=self.choose_directory).grid(row=0, column=1, padx=(8, 0))

        options = ttk.LabelFrame(outer, text="Include in each ZIP", padding=10)
        options.pack(fill="x", pady=(8, 6))
        for index, (key, label, _filename) in enumerate(EXPORT_FILE_OPTIONS):
            ttk.Checkbutton(options, text=label, variable=self.option_vars[key]).grid(
                row=index // 3, column=index % 3, sticky="w", padx=(0, 22), pady=2
            )
        ttk.Label(
            options,
            text="The archive manifest is included automatically.",
            foreground="#475569",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))

        ttk.Label(outer, textvariable=self.status_var, foreground="#1e3a8a", wraplength=700).pack(
            anchor="w", pady=(4, 6)
        )
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save Settings", style="Grease.TButton", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")

    def choose_directory(self) -> None:
        selected = filedialog.askdirectory(parent=self, title="Choose Automatic Archive Folder")
        if selected:
            self.directory_var.set(selected)

    def save(self) -> None:
        selected_keys = [key for key, _label, _filename in EXPORT_FILE_OPTIONS if self.option_vars[key].get()]
        directory = self.directory_var.get().strip()
        interval_by_label = {label: seconds for label, seconds in AUTO_ARCHIVE_INTERVAL_OPTIONS}
        interval_seconds = interval_by_label.get(self.interval_var.get(), AUTO_ARCHIVE_INTERVAL_SECONDS)
        if self.enabled_var.get() and not directory:
            messagebox.showerror("Archive folder required", "Choose a folder for automatic ZIP archives.", parent=self)
            return
        if self.enabled_var.get() and not Path(directory).is_dir():
            messagebox.showerror("Archive folder unavailable", "Choose an existing folder that can receive ZIP files.", parent=self)
            return
        if self.enabled_var.get() and not selected_keys:
            messagebox.showerror("Select data", "Choose at least one data type to include in each archive.", parent=self)
            return
        self.app.configure_auto_archive(
            enabled=self.enabled_var.get(),
            directory=directory,
            include=selected_keys,
            interval_seconds=interval_seconds,
        )
        if self.app.is_running:
            self.app.log_event("Automatic archive settings changed", "The updated settings apply to the next test run.")
        self.destroy()


class LocalDataDialog(tk.Toplevel):
    """Review and remove old complete application sessions without touching the current one."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Manage Local Data")
        self.geometry("820x510")
        self.minsize(700, 440)
        self.transient(app)
        self.status_var = tk.StringVar()
        self._build()
        self.refresh_sessions()
        self.lift()
        self.focus_force()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Manage Local Data", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=("Select completed application sessions to remove from this computer. "
                  "The current session and exported ZIP files are not affected."),
            wraplength=760,
            foreground="#475569",
        ).pack(anchor="w", pady=(4, 10))

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        self.sessions_list = tk.Listbox(list_frame, selectmode="extended", exportselection=False, height=10)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.sessions_list.yview)
        self.sessions_list.configure(yscrollcommand=scrollbar.set)
        self.sessions_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(outer, textvariable=self.status_var, foreground="#1e3a8a", wraplength=760).pack(
            anchor="w", pady=(10, 8)
        )
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Refresh", command=self.refresh_sessions).pack(side="left")
        ttk.Button(buttons, text="Delete Selected…", style="Stop.TButton", command=self.delete_selected).pack(
            side="right"
        )

    @staticmethod
    def _folder_size(folder: Path) -> int:
        try:
            return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
        except OSError:
            return 0

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024 * 1024:
            return f"{max(1, round(size_bytes / 1024))} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"

    @staticmethod
    def _is_unexported(runs: list[dict[str, object]]) -> bool:
        return not runs or any(not run.get("exported_at") for run in runs)

    def refresh_sessions(self) -> None:
        self.app.logger.flush()
        self.sessions: list[tuple[Path, list[dict[str, object]], dict[str, object], int]] = []
        for folder in self.app.paths.list_run_directories():
            if folder == self.app.logger.folder:
                continue
            runs = CsvLogger.read_live_runs(folder)
            manifest = CsvLogger.read_run_manifest(folder)
            self.sessions.append((folder, runs, manifest, self._folder_size(folder)))
        self.sessions_list.delete(0, "end")
        for folder, runs, manifest, size in self.sessions:
            run_count = f"{len(runs)} live run(s)" if runs else "legacy session"
            export_state = "unexported data" if self._is_unexported(runs) else "exported"
            completed = format_local_timestamp(manifest.get("ended_at"), fallback="Completion time unavailable")
            self.sessions_list.insert(
                "end",
                f"{folder.name}  |  Completed: {completed}  |  {run_count}  |  {self._format_size(size)}  |  {export_state}",
            )
        if self.sessions:
            self.status_var.set("Select one or more completed sessions to remove.")
        else:
            self.status_var.set("No completed application sessions are available to remove.")

    def delete_selected(self) -> None:
        selected_indexes = self.sessions_list.curselection()
        if not selected_indexes:
            messagebox.showerror("Select sessions", "Select at least one completed session to remove.", parent=self)
            return
        selected = [self.sessions[index] for index in selected_indexes]
        total_size = sum(size for _folder, _runs, _manifest, size in selected)
        unexported_count = sum(1 for _folder, runs, _manifest, _size in selected if self._is_unexported(runs))
        warning = ""
        if unexported_count:
            warning = f"\n\n{unexported_count} selected session(s) contain unexported data."
        if not messagebox.askyesno(
            "Delete local data?",
            f"Permanently remove {len(selected)} local session(s) ({self._format_size(total_size)})?"
            f"{warning}\n\nExported ZIP files will not be removed.",
            parent=self,
        ):
            return
        failures: list[str] = []
        for folder, _runs, _manifest, _size in selected:
            try:
                self.app.paths.delete_run_directory(folder)
            except (OSError, ValueError) as error:
                failures.append(f"{folder.name}: {error}")
        self.refresh_sessions()
        if failures:
            messagebox.showerror("Some data was not removed", "\n".join(failures), parent=self)
        else:
            messagebox.showinfo("Local data removed", f"Removed {len(selected)} local session(s).", parent=self)


class EventTerminalDialog(tk.Toplevel):
    """A full-size, read-only view of the operator event stream."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Test Stand Event Terminal")
        self.geometry("940x620")
        self.minsize(680, 400)
        self.configure(background="#f1f5f9")

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Event Terminal", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(
            header,
            text="Read-only live activity from this application session",
            foreground="#475569",
        ).pack(side="left", padx=12)
        self.output = ScrolledText(
            outer,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
            background="#f8fafc",
            foreground="#1e293b",
            relief="solid",
            borderwidth=1,
        )
        self.output.pack(fill="both", expand=True)
        self.append(app.event_log.get("1.0", "end-1c"))

    def append(self, text: str) -> None:
        if not text:
            return
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.configure(state="disabled")
        self.output.see("end")


class TestStandApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Senior Design Test Stand")
        self.minsize(1040, 720)
        self.geometry("1240x820")
        self.configure(background="#f1f5f9")

        self.paths = AppPaths.create(Path(__file__).resolve().parent)
        self.logger = CsvLogger(self.paths.logs)
        self.is_running = False
        self.current_live_run_id: str | None = None
        self.current_live_run_cycle_start = 0
        self.last_tick = time.monotonic()
        self.test_elapsed_seconds = 0.0
        self.auto_archive_config = self.load_auto_archive_config()
        self.active_auto_archive_config: dict[str, object] | None = None
        self.auto_archive_active = False
        self.auto_archive_elapsed_seconds = 0.0
        self.auto_archive_segment_number = 0
        self.cycles = 0
        self.cycle_state = "waiting_for_retracted"
        self.cycle_times: deque[float] = deque()
        self._cycle_capture: CycleCapture | None = None
        self._recent_cycle_summaries: deque[dict[str, float | int | str]] = deque(
            maxlen=GREASE_COMPARISON_CYCLE_COUNT
        )
        self._pending_grease_events: list[GreaseEventCapture] = []
        self._grease_event_sequence = 0
        self.position_calibration = self.load_position_calibration()
        self.cycle_stops = self.load_cycle_stops()
        self.ain2_monitor = LabJackAin2Monitor()
        self.labjack_available = False
        self.next_ain2_poll = 0.0
        self.last_labjack_signature = ""
        self.ain2_voltage: float | None = None
        self.bushing_position: float | None = None
        self.bushing_velocity: float | None = None
        self.grease_pressure: float | None = None
        self.bushing_unit = "degrees" if self.position_calibration else "volts"
        self._display_voltage_samples: deque[float] = deque(maxlen=5)
        self._velocity_angles: deque[float] = deque(maxlen=VELOCITY_SLOPE_WINDOW_SAMPLES)
        self._snapshot_samples: deque[PositionVelocitySnapshotSample] = deque(maxlen=SNAPSHOT_HISTORY_MAX_SAMPLES)
        self._snapshot_stream_gap_pending = False
        self._latest_snapshot_sample_monotonic: float | None = None
        self.graphs_paused = False
        self._frozen_snapshot_samples: tuple[PositionVelocitySnapshotSample, ...] | None = None
        self._frozen_position_graph: dict[str, object] | None = None
        self._frozen_velocity_graph: dict[str, object] | None = None
        self.bushing_detail_var = tk.StringVar(value="Waiting for AIN2")
        self.velocity_detail_var = tk.StringVar(value="Calibrate AIN2 to calculate velocity")
        self.cycle_stops_var = tk.StringVar(value=self.cycle_stops_summary())
        self.event_terminal: EventTerminalDialog | None = None

        self._build_style()
        self._build_menu()
        self._build_interface()
        self._set_labjack_dependent_controls(False)
        self.logger.update_manifest(
            app_version=APP_VERSION,
            data_model="completed_cycle_summary",
        )
        self._write_run_configuration_snapshot()
        self.log_event("Application started", f"CSV folder: {self.logger.folder}")
        if self.paths.migrated_legacy_config:
            migrated = ", ".join(self.paths.migrated_legacy_config)
            self.log_event("Local configuration migrated", f"Copied {migrated} to {self.paths.config}.")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(UPDATE_MS, self.tick)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f1f5f9")
        style.configure("Header.TFrame", background="#0f172a")
        style.configure("Header.TLabel", background="#0f172a", foreground="#f8fafc",
                        font=("Segoe UI", 18, "bold"))
        style.configure("Subheader.TLabel", background="#0f172a", foreground="#cbd5e1",
                        font=("Segoe UI", 9))
        style.configure("Metric.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("MetricName.TLabel", background="#ffffff", foreground="#475569",
                        font=("Segoe UI", 10, "bold"))
        style.configure("MetricValue.TLabel", background="#ffffff", foreground="#0f172a",
                        font=("Segoe UI", 22, "bold"))
        style.configure("MetricDetail.TLabel", background="#ffffff", foreground="#64748b",
                        font=("Segoe UI", 8))
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8))
        style.configure("Start.TButton", background="#166534", foreground="white")
        style.map("Start.TButton", background=[("active", "#15803d")])
        style.configure("Stop.TButton", background="#b91c1c", foreground="white")
        style.map("Stop.TButton", background=[("active", "#dc2626")])
        style.configure("Grease.TButton", background="#1d4ed8", foreground="white")
        style.map("Grease.TButton", background=[("active", "#2563eb")])
        style.configure("Icon.TButton", font=("Segoe UI Symbol", 11), padding=(3, 1))
        style.configure("TLabelframe", background="#ffffff", bordercolor="#cbd5e1")
        style.configure("TLabelframe.Label", background="#ffffff", foreground="#0f172a",
                        font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#f1f5f9", foreground="#0f172a", font=("Segoe UI", 10))

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)
        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Export Runs…", command=self.open_export_window)
        file_menu.add_command(label="Configure Auto-Archive…", command=self.open_auto_archive_window)
        file_menu.add_command(label="Open Local Data Folder", command=self.open_local_data_folder)
        file_menu.add_command(label="Manage Local Data…", command=self.open_local_data_window)
        menu_bar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="Getting Started…", command=self.open_getting_started_window)
        menu_bar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu_bar)

    def open_getting_started_window(self) -> None:
        """Show concise setup guidance without interrupting normal operation."""
        dialog = tk.Toplevel(self)
        dialog.title("Getting Started")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        outer = ttk.Frame(dialog, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Set up this test station", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="The application is ready to run without a Python installation. "
                 "The LabJack driver is only needed for live position and cycle counting.",
            wraplength=600,
            foreground="#475569",
        ).pack(anchor="w", pady=(6, 18))

        ttk.Label(outer, text="To use the physical LabJack T4", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "1. Install the official LabJack LJM Windows driver/library.\n"
                "2. Connect the T4 by USB.\n"
                "3. Restart this application. The header will show “LabJack T4 • Connected • USB”.\n"
                "4. If it remains disconnected, run TestStandLabJackDiagnostic.exe from the release folder."
            ),
            justify="left",
            wraplength=600,
        ).pack(anchor="w", pady=(6, 16))

        ttk.Label(outer, text="Data and calibration", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Each computer stores its own calibration and exported-run history under "
                 "%LOCALAPPDATA%\\SeniorDesignTestStand. Use File → Export Runs… to create shareable ZIP files.",
            justify="left",
            wraplength=600,
        ).pack(anchor="w", pady=(6, 18))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(
            buttons,
            text="Open LabJack Driver Download",
            command=lambda: webbrowser.open_new_tab(LABJACK_LJM_WINDOWS_URL),
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="Open Latest Release",
            command=lambda: webbrowser.open_new_tab(GITHUB_RELEASES_URL),
        ).pack(side="left", padx=(8, 0))

    def _build_interface(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 15))
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        title = ttk.Frame(header, style="Header.TFrame")
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(title, text="Senior Design Test Stand", style="Header.TLabel").pack(anchor="w")
        self.header_detail_var = tk.StringVar(
            value="Live AIN2 bushing position and cycle counting  •  Auxiliary inputs awaiting configuration"
        )
        ttk.Label(title, textvariable=self.header_detail_var, style="Subheader.TLabel").pack(anchor="w", pady=(3, 0))
        self.labjack_status_var = tk.StringVar(value="LabJack T4 • Checking connection")
        self.labjack_status_label = ttk.Label(
            header, textvariable=self.labjack_status_var, style="Subheader.TLabel", font=("Segoe UI", 10, "bold")
        )
        self.labjack_status_label.grid(row=0, column=1, sticky="e")

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(2, weight=1)

        self.metrics = ttk.Frame(main)
        self.metrics.grid(row=0, column=0, columnspan=2, sticky="ew")
        for column in range(5):
            self.metrics.columnconfigure(column, weight=1, uniform="metrics")
        for row in range(2):
            self.metrics.rowconfigure(row, weight=1)

        self.metric_vars: dict[str, tk.StringVar] = {}
        self._metric(self.metrics, 0, 0, "Completed Cycles", "cycles", "0", None)
        self._metric(self.metrics, 0, 1, "Bushing Position", "position", "Waiting…", self.bushing_detail_var)
        self._metric(self.metrics, 0, 2, "Bushing Velocity", "velocity", "— °/s", None)
        self._metric(self.metrics, 0, 3, "Cycle Frequency", "frequency", "0.0 cpm", None)
        self._metric(self.metrics, 0, 4, "Grease Pressure", "pressure", "— psi", "Input not configured")
        self._metric(self.metrics, 1, 0, "Test Time", "test_time", "00:00:00", None)
        self._metric(self.metrics, 1, 1, "Temperature 1", "temp_1", "— °F", "Input not configured")
        self._metric(self.metrics, 1, 2, "Temperature 2", "temp_2", "— °F", "Input not configured")

        controls = ttk.LabelFrame(main, text="Test Controls", padding=12)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 10))
        controls.columnconfigure(6, weight=1)

        count_buttons = ttk.Frame(controls)
        count_buttons.grid(row=0, column=0, padx=(0, 12), sticky="w")
        self.start_button = ttk.Button(
            count_buttons, text="Start Counting", style="Start.TButton", command=self.start_counting, state="disabled"
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            count_buttons, text="Stop", style="Stop.TButton", command=self.stop_counting, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(6, 0))
        self.reset_button = ttk.Button(count_buttons, text="Reset", command=self.reset_counting)
        self.reset_button.pack(side="left", padx=(6, 0))
        self.calibrate_button = ttk.Button(controls, text="Calibrate AIN2 Position", command=self.open_calibration_window)
        self.calibrate_button.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        calibration_text = "Calibration saved" if self.position_calibration else "Calibration required"
        ttk.Label(controls, text=calibration_text, foreground="#475569").grid(
            row=1, column=2, columnspan=4, sticky="w", pady=(12, 0)
        )
        self.cycle_stops_button = ttk.Button(
            controls, text="Input Cycle Stops", command=self.open_cycle_stops_window
        )
        self.cycle_stops_button.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(controls, textvariable=self.cycle_stops_var, foreground="#475569").grid(
            row=2, column=2, columnspan=4, sticky="w", pady=(10, 0)
        )

        self.chart_holder = ttk.Frame(main)
        self.chart_holder.grid(row=2, column=0, sticky="nsew", padx=(0, 10))
        self.chart_holder.columnconfigure(0, weight=1)
        self.chart_holder.columnconfigure(1, weight=1)
        self.chart_holder.columnconfigure(2, weight=1)
        self.chart_holder.rowconfigure(1, weight=1)
        chart_toolbar = ttk.Frame(self.chart_holder)
        chart_toolbar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(chart_toolbar, text="Position and velocity", foreground="#475569").pack(side="left")
        self.pause_graphs_button = ttk.Button(chart_toolbar, text="Pause", command=self.toggle_graph_pause)
        self.pause_graphs_button.pack(side="right")
        self.save_snapshot_button = ttk.Button(
            chart_toolbar, text="Save Snapshot…", command=self.save_graph_snapshot, state="disabled"
        )
        self.save_snapshot_button.pack(side="right", padx=(0, 6))
        self.bushing_plot = LivePlot(self.chart_holder, "Live AIN2 voltage (V) — calibration not set", 5.0, "#0891b2")
        self.bushing_plot.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        self.configure_bushing_plot(clear_samples=False)
        self.velocity_plot = LivePlot(
            self.chart_holder,
            "Live bushing velocity (°/s) — calibration required",
            DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
            "#2563eb",
            minimum=-DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
        )
        self.velocity_plot.grid(row=1, column=1, sticky="nsew", padx=5)
        self.configure_velocity_plot(clear_samples=False)
        self.pressure_plot = LivePlot(
            self.chart_holder,
            "Grease pressure (psi) — input not configured",
            5000.0,
            "#d97706",
        )
        self.pressure_plot.grid(row=1, column=2, sticky="nsew", padx=(5, 0))
        self.pressure_plot.set_reference_lines([
            (INJECTOR_ACTIVATION_PSI, "#7c3aed", "Injector activation"),
        ])

        status_box = ttk.LabelFrame(main, text="Recent Events", padding=12)
        status_box.grid(row=2, column=1, sticky="nsew")
        event_toolbar = ttk.Frame(status_box)
        event_toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(event_toolbar, text="Latest application activity", foreground="#475569").pack(side="left")
        ttk.Button(
            event_toolbar,
            text="⛶",
            width=3,
            style="Icon.TButton",
            command=self.open_event_terminal,
        ).pack(side="right")
        self.event_log = ScrolledText(status_box, height=9, wrap="word", state="disabled",
                                      font=("Consolas", 9), background="#f8fafc", foreground="#1e293b",
                                      relief="flat")
        self.event_log.pack(fill="both", expand=True)

    def _metric(
        self, parent: ttk.Frame, row: int, column: int, label: str, key: str, initial: str, detail: str | tk.StringVar | None
    ) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Metric.TFrame", padding=(12, 10))
        frame.grid(
            row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0), pady=(0 if row == 0 else 5, 0)
        )
        ttk.Label(frame, text=label, style="MetricName.TLabel").pack(anchor="w")
        variable = tk.StringVar(value=initial)
        self.metric_vars[key] = variable
        ttk.Label(frame, textvariable=variable, style="MetricValue.TLabel").pack(anchor="w", pady=(5, 2))
        if isinstance(detail, tk.StringVar):
            ttk.Label(frame, textvariable=detail, style="MetricDetail.TLabel", wraplength=155).pack(anchor="w")
        elif detail:
            ttk.Label(frame, text=detail, style="MetricDetail.TLabel", wraplength=155).pack(anchor="w")
        return frame

    def log_event(self, event: str, detail: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.log_event(event, detail, self.cycles, live_run_id=self.current_live_run_id)
        text = f"{timestamp}  {event}\n{detail}\n\n"
        self.event_log.configure(state="normal")
        self.event_log.insert("end", text)
        self.event_log.configure(state="disabled")
        self.event_log.see("end")
        if self.event_terminal is not None and self.event_terminal.winfo_exists():
            self.event_terminal.append(text)

    def open_event_terminal(self) -> None:
        """Open one full-size, live-updating event terminal for this session."""
        if self.event_terminal is not None and self.event_terminal.winfo_exists():
            self.event_terminal.deiconify()
            self.event_terminal.lift()
            self.event_terminal.focus_set()
            return
        self.event_terminal = EventTerminalDialog(self)

    def _run_configuration_values(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_version": APP_VERSION,
            "data_model": {
                "primary_record": "one summary row per completed cycle",
                "raw_ain2_samples_persisted": False,
                "dashboard_snapshot_log": False,
            },
            "automatic_archive": {
                "enabled": self.auto_archive_config["enabled"],
                "interval_seconds": self.auto_archive_config["interval_seconds"],
                "included_data": self.auto_archive_config["include"],
            },
            "position_calibration": self.position_calibration,
            "cycle_stops": self.cycle_stops,
            "signals": {
                "ain2_bushing_position": "LabJack T4 live read-only input",
                "grease_pressure": "hardware input not configured",
                "grease_event_analysis": "awaiting physical injector trigger integration",
                "temperature_1_to_2": "hardware inputs not configured",
                "physical_outputs": "not implemented",
            },
        }

    @staticmethod
    def _default_auto_archive_config() -> dict[str, object]:
        return {
            "schema_version": AUTO_ARCHIVE_CONFIG_SCHEMA_VERSION,
            "enabled": False,
            "directory": "",
            "include": list(DEFAULT_AUTO_ARCHIVE_INCLUDE),
            "interval_seconds": AUTO_ARCHIVE_INTERVAL_SECONDS,
        }

    def load_auto_archive_config(self) -> dict[str, object]:
        """Read a conservative, validated auto-archive configuration."""
        config = self._default_auto_archive_config()
        saved = self.paths.read_config(AUTO_ARCHIVE_CONFIG_FILENAME)
        if not saved:
            return config
        configured_keys = saved.get("include", config["include"])
        if not isinstance(configured_keys, list):
            configured_keys = config["include"]
        allowed_keys = {key for key, _label, _file in EXPORT_FILE_OPTIONS}
        include = [str(key) for key in configured_keys if str(key) in allowed_keys]
        try:
            schema_version = int(saved.get("schema_version", 1))
        except (TypeError, ValueError):
            schema_version = 1
        if schema_version < AUTO_ARCHIVE_CONFIG_SCHEMA_VERSION:
            # Bring existing installations forward to the two useful default
            # data files once, while retaining every selection they made.
            configured_set = set(include) | set(DEFAULT_AUTO_ARCHIVE_INCLUDE)
            include = [key for key, _label, _file in EXPORT_FILE_OPTIONS if key in configured_set]
        allowed_intervals = {seconds for _label, seconds in AUTO_ARCHIVE_INTERVAL_OPTIONS}
        try:
            interval_seconds = int(saved.get("interval_seconds", config["interval_seconds"]))
        except (TypeError, ValueError):
            interval_seconds = AUTO_ARCHIVE_INTERVAL_SECONDS
        if interval_seconds not in allowed_intervals:
            interval_seconds = AUTO_ARCHIVE_INTERVAL_SECONDS
        return {
            "schema_version": AUTO_ARCHIVE_CONFIG_SCHEMA_VERSION,
            "enabled": bool(saved.get("enabled", False)),
            "directory": str(saved.get("directory", "")).strip(),
            "include": include or list(config["include"]),
            "interval_seconds": interval_seconds,
        }

    def configure_auto_archive(
        self, *, enabled: bool, directory: str, include: list[str], interval_seconds: int
    ) -> None:
        self.auto_archive_config = {
            "schema_version": AUTO_ARCHIVE_CONFIG_SCHEMA_VERSION,
            "enabled": bool(enabled),
            "directory": directory,
            "include": include,
            "interval_seconds": interval_seconds,
        }
        self.paths.write_config(AUTO_ARCHIVE_CONFIG_FILENAME, self.auto_archive_config)
        self._write_run_configuration_snapshot()

    @staticmethod
    def _auto_archive_config_is_ready(config: dict[str, object]) -> bool:
        if not config["enabled"] or not config["include"]:
            return False
        try:
            return Path(str(config["directory"])).is_dir()
        except (TypeError, OSError):
            return False

    def _replace_logger(self) -> None:
        """Start a clean local staging session after a verified archive."""
        self.logger = CsvLogger(self.paths.logs)
        self.logger.update_manifest(
            app_version=APP_VERSION,
            data_model="completed_cycle_summary",
        )
        self._write_run_configuration_snapshot()

    def _start_auto_archive_run(self) -> None:
        self.auto_archive_elapsed_seconds = 0.0
        self.auto_archive_segment_number = 0
        candidate = {
            "enabled": self.auto_archive_config["enabled"],
            "directory": self.auto_archive_config["directory"],
            "include": list(self.auto_archive_config["include"]),
            "interval_seconds": self.auto_archive_config["interval_seconds"],
        }
        self.auto_archive_active = self._auto_archive_config_is_ready(candidate)
        self.active_auto_archive_config = candidate if self.auto_archive_active else None
        if self.auto_archive_active:
            self.auto_archive_segment_number = 1
            self.log_event(
                "Automatic archive enabled",
                f"Segment 1 will be archived after {auto_archive_interval_label(int(candidate['interval_seconds'])).lower()} of active test time.",
            )
        elif self.auto_archive_config["enabled"]:
            self.log_event(
                "Automatic archive unavailable",
                "The configured archive folder is unavailable. Data remains local for manual export.",
            )

    def _auto_archive_destination(self, reason: str) -> Path:
        if self.active_auto_archive_config is None:
            raise ValueError("Automatic archive settings are not active for this run.")
        directory = Path(str(self.active_auto_archive_config["directory"]))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"TestStand_{timestamp}_segment_{self.auto_archive_segment_number:03d}_{reason}"
        destination = directory / f"{stem}.zip"
        suffix = 2
        while destination.exists():
            destination = directory / f"{stem}_{suffix:02d}.zip"
            suffix += 1
        return destination

    def _archive_auto_segment(self, status: str, reason: str, *, continue_running: bool) -> bool:
        """Archive one completed segment; never discard it until its ZIP verifies."""
        if not self.auto_archive_active or not self.current_live_run_id:
            return True
        run_id = self.current_live_run_id
        segment_seconds = self.auto_archive_elapsed_seconds
        self.log_event(
            "Automatic archive started",
            f"Preparing segment {self.auto_archive_segment_number} after {segment_seconds / 60:.1f} active minutes.",
        )
        self._finish_live_run(status)
        runs = CsvLogger.read_live_runs(self.logger.folder)
        run = next((item for item in runs if item.get("id") == run_id), None)
        if run is None:
            self.log_event("Automatic archive failed", "The completed run metadata could not be located. Local data was retained.")
            return False
        destination = self._auto_archive_destination(reason)
        try:
            count = write_live_run_archive(
                destination,
                [(self.logger.folder, run)],
                set(self.active_auto_archive_config["include"] if self.active_auto_archive_config else []),
                manifest_extra={
                    "archive_type": "automatic",
                    "archive_reason": reason,
                    "segment_number": self.auto_archive_segment_number,
                    "active_test_seconds": round(segment_seconds, 1),
                    "archive_interval_seconds": int(self.active_auto_archive_config["interval_seconds"]),
                },
            )
        except (OSError, ValueError, csv.Error, zipfile.BadZipFile) as error:
            self.log_event("Automatic archive failed", f"Local data was retained: {error}")
            if continue_running:
                self._begin_live_run()
                self.auto_archive_elapsed_seconds = 0.0
            return False

        cleanup_error: OSError | ValueError | None = None
        self.logger.close()
        try:
            self.paths.delete_run_directory(self.logger.folder)
        except (OSError, ValueError) as error:
            cleanup_error = error
        self._replace_logger()
        self.auto_archive_elapsed_seconds = 0.0

        if continue_running:
            self.auto_archive_segment_number += 1
            self._begin_live_run()
            self.log_event(
                "Automatic archive complete",
                f"Created {destination.name} ({count} files). Segment {self.auto_archive_segment_number} is active.",
            )
        else:
            self.auto_archive_active = False
            self.active_auto_archive_config = None
            self.log_event("Automatic archive complete", f"Created {destination.name} ({count} files).")
        if cleanup_error:
            self.log_event(
                "Archive cleanup incomplete",
                f"The ZIP was created, but the local segment could not be removed: {cleanup_error}",
            )
        return True

    def _write_run_configuration_snapshot(self) -> None:
        """Preserve current settings for the full application session."""
        self.logger.write_run_configuration(self._run_configuration_values())

    def _begin_live_run(self) -> None:
        live_run = self.logger.begin_live_run(self._run_configuration_values(), self.cycles)
        self.current_live_run_id = str(live_run["id"])
        self.current_live_run_cycle_start = int(live_run["completed_cycles_at_start"])
        self._recent_cycle_summaries.clear()

    def _finish_live_run(self, status: str) -> None:
        if not self.current_live_run_id:
            return
        self._discard_cycle_capture("Live run ended before the count stop was reached.")
        self._finalize_pending_grease_events("partial: live run ended before comparison window completed")
        self.cycle_state = "waiting_for_retracted"
        self.logger.finish_live_run(
            self.current_live_run_id,
            status=status,
            completed_cycles=self.cycles,
            cycle_frequency_cpm=round(self._frequency(), 2),
        )
        self.current_live_run_id = None

    def _latest_unexported_live_run(self) -> dict[str, object] | None:
        if self.current_live_run_id:
            for run in reversed(CsvLogger.read_live_runs(self.logger.folder)):
                if run.get("id") == self.current_live_run_id:
                    return run if not run.get("exported_at") else None
        for run in reversed(CsvLogger.read_live_runs(self.logger.folder)):
            if not run.get("exported_at"):
                return run
        return None

    def _set_labjack_dependent_controls(self, available: bool) -> None:
        """Enable only actions that have a confirmed live T4 behind them."""
        self.labjack_available = available
        action_state = "normal" if available else "disabled"
        self.calibrate_button.configure(state=action_state)
        self.cycle_stops_button.configure(state=action_state)
        if available and self.is_running:
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            return

        self.start_button.configure(state=action_state)
        self.stop_button.configure(state="disabled")
        if not available and self.is_running:
            self.is_running = False
            self.cycle_state = "waiting_for_retracted"
            self.log_event("Cycle counting stopped", "LabJack disconnected.")
            if self.auto_archive_active:
                self._archive_auto_segment("interrupted_by_disconnect", "interrupted", continue_running=False)
            else:
                self._finish_live_run("interrupted_by_disconnect")

    def start_counting(self) -> None:
        if not self.labjack_available:
            messagebox.showerror(
                "LabJack required",
                "Connect the LabJack T4 and wait for AIN2 to become active before starting cycle counting.",
                parent=self,
            )
            return
        if self.is_running:
            return
        self.is_running = True
        self.test_elapsed_seconds = 0.0
        self.metric_vars["test_time"].set("00:00:00")
        self.cycle_state = "waiting_for_retracted"
        self._cycle_capture = None
        self._begin_live_run()
        self._start_auto_archive_run()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        if self.cycle_stops:
            self.log_event("Cycle counting started", "Waiting for the reset stop.")
        else:
            self.log_event("Cycle counting started", "Cycle stops are not configured.")

    def stop_counting(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.log_event("Cycle counting stopped", "Cycle and CPM totals retained.")
        if self.auto_archive_active:
            self._archive_auto_segment("stopped", "final", continue_running=False)
        else:
            self._finish_live_run("stopped")

    def reset_counting(self) -> None:
        unexported_run = self._latest_unexported_live_run()
        if unexported_run and not self.auto_archive_active:
            choice = messagebox.askyesnocancel(
                "Save before reset?",
                "This live run has not been exported. Save it before resetting?",
                parent=self,
            )
            if choice is None:
                return
            if choice:
                self.open_export_window(preselect_run_id=str(unexported_run.get("id", "")))
                return

        was_running = self.is_running
        if was_running:
            self.log_event("Live run ended", "Reset started a new live run.")
            if self.auto_archive_active:
                self._archive_auto_segment("ended_by_reset", "reset", continue_running=False)
            else:
                self._finish_live_run("ended_by_reset")
        self.cycles = 0
        self.cycle_times.clear()
        self.test_elapsed_seconds = 0.0
        self.cycle_state = "waiting_for_retracted"
        self.metric_vars["cycles"].set("0")
        self.metric_vars["frequency"].set("0.0 cpm")
        self.metric_vars["test_time"].set("00:00:00")
        if was_running:
            self._begin_live_run()
            self._start_auto_archive_run()
            self.log_event("Live run started", "Cycle and CPM totals were reset.")
        self.log_event(
            "Cycle counting reset",
            "Cycle and CPM totals cleared.",
        )

    def _frequency(self) -> float:
        now = time.monotonic()
        while self.cycle_times and now - self.cycle_times[0] > 60:
            self.cycle_times.popleft()
        if len(self.cycle_times) < 2:
            return 0.0
        period = self.cycle_times[-1] - self.cycle_times[0]
        return 60 * (len(self.cycle_times) - 1) / period if period else 0.0

    @staticmethod
    def _format_test_time(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def load_position_calibration(self) -> dict[str, object] | None:
        calibration = self.paths.read_config("position_calibration.json")
        try:
            if calibration is None:
                return None
            first, second = calibration["point_1"], calibration["point_2"]
            if abs(float(second["voltage"]) - float(first["voltage"])) < MIN_CALIBRATION_VOLTAGE_SPAN:
                return None
            return calibration
        except (KeyError, TypeError, ValueError):
            return None

    def save_position_calibration(self, calibration: dict[str, object]) -> None:
        self.paths.write_config("position_calibration.json", calibration)
        self.position_calibration = calibration
        self.bushing_unit = "degrees"
        self.configure_bushing_plot()
        self.configure_velocity_plot()
        self._cycle_capture = None
        self._clear_snapshot_history()
        self._write_run_configuration_snapshot()

    def clear_position_calibration(self) -> None:
        self.paths.remove_config("position_calibration.json")
        self.position_calibration = None
        self.bushing_unit = "volts"
        self.configure_bushing_plot()
        self.configure_velocity_plot()
        self._cycle_capture = None
        self._clear_snapshot_history()
        self._write_run_configuration_snapshot()
        self.log_event("AIN2 calibration cleared", "Saved two-point AIN2 calibration removed.")

    def load_cycle_stops(self) -> dict[str, float] | None:
        try:
            stops = self.paths.read_config("cycle_stops.json")
            if stops is None:
                return None
            retracted, extended = float(stops["retracted"]), float(stops["extended"])
            if abs(extended - retracted) < MIN_CYCLE_STOP_SEPARATION_DEGREES:
                return None
            return {"retracted": retracted, "extended": extended}
        except (KeyError, TypeError, ValueError):
            return None

    def cycle_stops_summary(self) -> str:
        if not self.cycle_stops:
            return "Cycle stops not set — counting disabled"
        return (
            f"Reset: {self.cycle_stops['retracted']:.2f}°  •  "
            f"Count: {self.cycle_stops['extended']:.2f}°"
        )

    def save_cycle_stops(self, stops: dict[str, float]) -> None:
        self.paths.write_config("cycle_stops.json", stops)
        self.cycle_stops = {"retracted": float(stops["retracted"]), "extended": float(stops["extended"])}
        self.cycle_state = "waiting_for_retracted"
        self._cycle_capture = None
        self.cycle_stops_var.set(self.cycle_stops_summary())
        self.configure_bushing_plot(clear_samples=False)
        self._write_run_configuration_snapshot()
        self.log_event(
            "Cycle stops saved",
            f"Reset at {self.cycle_stops['retracted']:.3f}°; count at {self.cycle_stops['extended']:.3f}°.",
        )

    def clear_cycle_stops(self) -> None:
        self.paths.remove_config("cycle_stops.json")
        self.cycle_stops = None
        self.cycle_state = "waiting_for_retracted"
        self._cycle_capture = None
        self.cycle_stops_var.set(self.cycle_stops_summary())
        self.configure_bushing_plot(clear_samples=False)
        self._write_run_configuration_snapshot()
        self.log_event("Cycle stops cleared", "Live AIN2 cycle counting disabled until two new stops are saved.")

    @staticmethod
    def angle_from_voltage(voltage: float, calibration: dict[str, object]) -> tuple[float, bool]:
        first = calibration["point_1"]
        second = calibration["point_2"]
        first_voltage, second_voltage = float(first["voltage"]), float(second["voltage"])
        first_angle, second_angle = float(first["angle"]), float(second["angle"])
        fraction = (voltage - first_voltage) / (second_voltage - first_voltage)
        angle = first_angle + fraction * (second_angle - first_angle)
        outside = voltage < min(first_voltage, second_voltage) or voltage > max(first_voltage, second_voltage)
        return angle, outside

    def configure_bushing_plot(self, clear_samples: bool = True) -> None:
        """Scale the live graph for raw voltage before calibration, then for angle."""
        if not self.position_calibration:
            self.bushing_plot.set_scale("Live AIN2 voltage (V) — calibration not set", 0.0, 5.0, clear_samples)
            self.bushing_plot.set_reference_lines([])
            return
        first = self.position_calibration["point_1"]
        second = self.position_calibration["point_2"]
        graph_values = [float(first["angle"]), float(second["angle"])]
        if self.cycle_stops:
            graph_values.extend((self.cycle_stops["retracted"], self.cycle_stops["extended"]))
        low, high = min(graph_values), max(graph_values)
        padding = max(1.0, (high - low) * 0.10)
        self.bushing_plot.set_scale(
            "Live bushing position (degrees, calibrated AIN2)", low - padding, high + padding, clear_samples
        )
        if self.cycle_stops:
            self.bushing_plot.set_reference_lines([
                (self.cycle_stops["retracted"], "#15803d", "Reset stop"),
                (self.cycle_stops["extended"], "#dc2626", "Count stop"),
            ])
        else:
            self.bushing_plot.set_reference_lines([])

    def configure_velocity_plot(self, clear_samples: bool = True) -> None:
        """Reset velocity state when calibration changes and show a signed speed scale."""
        self._velocity_angles.clear()
        self.bushing_velocity = None
        self.metric_vars["velocity"].set("— °/s")
        if not self.position_calibration:
            self.velocity_detail_var.set("Calibrate AIN2 to calculate velocity")
            self.velocity_plot.set_scale(
                "Live bushing velocity (°/s) — calibration required",
                -DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
                DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
                clear_samples,
            )
        else:
            self.velocity_detail_var.set("Waiting for live AIN2 samples")
            self.velocity_plot.set_scale(
                "Live bushing velocity (°/s, calibrated AIN2)",
                -DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
                DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
                clear_samples,
            )
        self.velocity_plot.set_reference_lines([(0.0, "#94a3b8", "Stationary")])

    def _update_bushing_velocity(self, angle: float, scan_rate_hz: float) -> None:
        """Fit a short local line to raw calibrated positions for stable signed velocity."""
        self._velocity_angles.append(angle)
        if len(self._velocity_angles) < VELOCITY_SLOPE_WINDOW_SAMPLES:
            # A stream is present but its local fit is still warming up.  Plot
            # zero rather than a gap so a rearming estimator does not make the
            # velocity trace look like a missing connection.
            self.bushing_velocity = 0.0
            self.metric_vars["velocity"].set("+0.00 °/s")
            self.velocity_detail_var.set("Collecting calibrated AIN2 samples")
            return

        values = tuple(self._velocity_angles)
        center = (len(values) - 1) / 2
        denominator = sum((index - center) ** 2 for index in range(len(values)))
        slope_per_sample = sum((index - center) * value for index, value in enumerate(values)) / denominator
        sample_rate = max(1.0, scan_rate_hz)
        velocity = slope_per_sample * sample_rate
        if abs(velocity) < VELOCITY_ZERO_DEADBAND_DPS:
            velocity = 0.0
        self.bushing_velocity = velocity
        self.velocity_detail_var.set(f"{len(values)}-sample fit • {sample_rate:.0f} Hz")

    def _render_bushing_velocity(self) -> None:
        """Render the latest raw-stream velocity at the same cadence as the position UI."""
        if self.bushing_velocity is None:
            return
        self.metric_vars["velocity"].set(f"{self.bushing_velocity:+.2f} °/s")
        if self.graphs_paused:
            return
        limit = max(abs(self.velocity_plot.minimum), abs(self.velocity_plot.maximum))
        if abs(self.bushing_velocity) > limit * 0.95:
            new_limit = max(DEFAULT_VELOCITY_GRAPH_LIMIT_DPS, (int(abs(self.bushing_velocity) / 25) + 2) * 25.0)
            self.velocity_plot.set_scale(
                "Live bushing velocity (°/s, calibrated AIN2)", -new_limit, new_limit, clear_samples=False
            )
            self.velocity_plot.set_reference_lines([(0.0, "#94a3b8", "Stationary")])
        self.velocity_plot.add(self.bushing_velocity, self._latest_snapshot_sample_monotonic)

    def _start_cycle_capture(self, retracted: float, extended: float, sample_at: str, sample_monotonic: float) -> None:
        self._cycle_capture = CycleCapture(
            reset_stop_reached_at=sample_at,
            reset_stop_monotonic=sample_monotonic,
            reset_stop_degrees=retracted,
            count_stop_degrees=extended,
            direction=1 if extended > retracted else -1,
        )

    def _discard_cycle_capture(self, reason: str) -> None:
        """Do not write a summary for a partial stroke; retain its reason in events."""
        if self._cycle_capture is not None:
            self.log_event("Partial cycle discarded", reason)
        self._cycle_capture = None

    @staticmethod
    def _grease_event_status(event: GreaseEventCapture, completion: str) -> str:
        baseline_note = "" if len(event.pre_cycles) == GREASE_COMPARISON_CYCLE_COUNT else (
            f"; limited pre-injection baseline ({len(event.pre_cycles)}/{GREASE_COMPARISON_CYCLE_COUNT} cycles)"
        )
        return completion + baseline_note

    def _finalize_pending_grease_events(self, completion: str) -> None:
        """Persist pending injection comparisons before the active live run closes."""
        if not self.current_live_run_id:
            return
        for event in self._pending_grease_events:
            status = self._grease_event_status(event, completion)
            self.logger.log_grease_event(event.values(status=status), self.current_live_run_id)
            self.log_event("Grease event saved", f"{event.event_id}: {status}.")
        self._pending_grease_events.clear()

    def _add_cycle_to_grease_comparisons(self, values: dict[str, float | int | str]) -> None:
        self._recent_cycle_summaries.append(values)
        completed: list[GreaseEventCapture] = []
        for event in self._pending_grease_events:
            event.post_cycles.append(values)
            if len(event.post_cycles) >= GREASE_COMPARISON_CYCLE_COUNT:
                completed.append(event)
        for event in completed:
            self._pending_grease_events.remove(event)
            status = self._grease_event_status(event, "complete: 3 post-injection cycles")
            if self.current_live_run_id:
                self.logger.log_grease_event(event.values(status=status), self.current_live_run_id)
            self.log_event("Grease event saved", f"{event.event_id}: {status}.")

    def _update_cycle_count_from_bushing(self, angle: float, sample_at: str, sample_monotonic: float) -> None:
        """Use direction-independent stop hysteresis to count real motion once per stroke."""
        if not self.is_running or not self.cycle_stops:
            return
        retracted, extended = self.cycle_stops["retracted"], self.cycle_stops["extended"]
        increasing = extended > retracted
        at_retracted = angle <= retracted if increasing else angle >= retracted
        at_extended = angle >= extended if increasing else angle <= extended
        if at_retracted:
            if self.cycle_state != "ready_for_extension":
                self._start_cycle_capture(retracted, extended, sample_at, sample_monotonic)
            self.cycle_state = "ready_for_extension"
            return
        if self.cycle_state != "ready_for_extension":
            return
        if self._cycle_capture is not None:
            self._cycle_capture.observe(angle, self.bushing_velocity, sample_at, sample_monotonic)
        if not at_extended:
            return
        self.cycle_state = "waiting_for_retracted"
        self.cycles += 1
        self.cycle_times.append(sample_monotonic)
        if self._cycle_capture is not None and self.current_live_run_id:
            values = self._cycle_capture.completed_values(
                completed_at=sample_at,
                completed_monotonic=sample_monotonic,
                cycle_number=self.cycles,
                live_run_cycle_number=self.cycles - self.current_live_run_cycle_start,
                frequency=self._frequency(),
            )
            self.logger.log_cycle(values, self.current_live_run_id)
            self._add_cycle_to_grease_comparisons(values)
            self._cycle_capture = None
        self.log_event(
            "Cycle detected",
            f"Cycle {self.cycles} reached count stop ({extended:.3f}°) after reset stop ({retracted:.3f}°).",
        )

    def _update_live_bushing_position(self, now: float) -> None:
        """Process every AIN2 stream sample, then render one smoothed UI update."""
        if now >= self.next_ain2_poll:
            self.ain2_monitor.start_read()
            self.next_ain2_poll = now + AIN2_POLL_SECONDS

        results = self.ain2_monitor.drain_results()
        if not results:
            return

        latest_voltage: float | None = None
        for result in results:
            if result.get("status") == "success":
                self._set_labjack_connection_state("connected", "AIN2 stream is active.")
                try:
                    scan_rate_hz = float(result.get("scan_rate_hz", 200.0))
                except (TypeError, ValueError):
                    scan_rate_hz = 200.0
                try:
                    skipped_samples = int(result.get("skipped_samples", 0))
                except (TypeError, ValueError):
                    skipped_samples = 0
                if skipped_samples:
                    self._velocity_angles.clear()
                    self.bushing_velocity = 0.0
                    self.metric_vars["velocity"].set("+0.00 °/s")
                    self.velocity_detail_var.set("AIN2 stream gap — velocity rearming")
                    if self._cycle_capture is not None:
                        self._cycle_capture.stream_gap_detected = True
                    self._snapshot_stream_gap_pending = True
                samples = result.get("samples", [])
                if not isinstance(samples, list):
                    samples = []
                batch_end_at = datetime.now().astimezone()
                batch_end_monotonic = time.monotonic()
                for index, raw_voltage in enumerate(samples):
                    try:
                        voltage = float(raw_voltage)
                    except (TypeError, ValueError):
                        continue
                    seconds_before_end = (len(samples) - 1 - index) / max(scan_rate_hz, 1.0)
                    sample_monotonic = batch_end_monotonic - seconds_before_end
                    sample_at = (batch_end_at - timedelta(seconds=seconds_before_end)).isoformat(timespec="milliseconds")
                    self._process_counting_voltage(voltage, scan_rate_hz, sample_at, sample_monotonic)
                    latest_voltage = voltage
            elif result.get("status") == "disconnected":
                self._set_labjack_connection_state(
                    "disconnected", str(result.get("error", "LabJack T4 is disconnected."))
                )
            else:
                self._set_labjack_connection_state("error", str(result.get("error", "AIN2 stream failed.")))

        if latest_voltage is not None:
            self._render_live_bushing_position(statistics.fmean(self._display_voltage_samples))
            self._render_bushing_velocity()

    def _process_counting_voltage(
        self, voltage: float, scan_rate_hz: float, sample_at: str, sample_monotonic: float
    ) -> None:
        """Feed each hardware-timed sample to the detector; never reject it for motion."""
        self._display_voltage_samples.append(voltage)
        if self.position_calibration:
            angle, outside = self.angle_from_voltage(voltage, self.position_calibration)
            if not outside:
                self._update_bushing_velocity(angle, scan_rate_hz)
                self._record_snapshot_sample(sample_at, sample_monotonic, scan_rate_hz, voltage, angle)
                self._update_cycle_count_from_bushing(angle, sample_at, sample_monotonic)
            else:
                self._velocity_angles.clear()
                self.bushing_velocity = 0.0
                self.metric_vars["velocity"].set("+0.00 °/s")
                self.velocity_detail_var.set("Outside calibrated AIN2 span")
                self._discard_cycle_capture("AIN2 left the calibrated span before reaching the count stop.")

    def _render_live_bushing_position(self, voltage: float) -> None:
        """Render a short display-only average without affecting cycle detection."""
        self.ain2_voltage = voltage
        if self.position_calibration:
            angle, outside = self.angle_from_voltage(voltage, self.position_calibration)
            self.bushing_position, self.bushing_unit = angle, "degrees"
            self.metric_vars["position"].set(f"{angle:.2f} °")
            detail = f"AIN2: {voltage:.4f} V"
            if outside:
                detail += " • OUT"
            self.bushing_detail_var.set(detail)
            if not self.graphs_paused:
                self.bushing_plot.add(angle, self._latest_snapshot_sample_monotonic)
        else:
            self.bushing_position, self.bushing_unit = voltage, "volts"
            self.metric_vars["position"].set(f"{voltage:.4f} V")
            self.bushing_detail_var.set("Live AIN2 voltage • calibration required for angle")
            self.bushing_plot.add(voltage)

    def _record_snapshot_sample(
        self,
        sample_at: str,
        sample_monotonic: float,
        scan_rate_hz: float,
        voltage: float,
        angle: float,
    ) -> None:
        """Keep a bounded, calibrated raw-stream history solely for operator snapshots."""
        self._snapshot_samples.append(
            PositionVelocitySnapshotSample(
                timestamp=sample_at,
                monotonic_seconds=sample_monotonic,
                scan_rate_hz=scan_rate_hz,
                ain2_voltage=voltage,
                position_degrees=angle,
                velocity_degrees_per_second=self.bushing_velocity,
                stream_gap_before=self._snapshot_stream_gap_pending,
            )
        )
        self._latest_snapshot_sample_monotonic = sample_monotonic
        self._snapshot_stream_gap_pending = False

    def _clear_snapshot_history(self) -> None:
        """Discard incompatible raw history after changing the position calibration."""
        self._snapshot_samples.clear()
        self._snapshot_stream_gap_pending = False
        self._latest_snapshot_sample_monotonic = None
        self.graphs_paused = False
        self._frozen_snapshot_samples = None
        self._frozen_position_graph = None
        self._frozen_velocity_graph = None
        if hasattr(self, "pause_graphs_button"):
            self.pause_graphs_button.configure(text="Pause")
            self.save_snapshot_button.configure(state="disabled")

    def toggle_graph_pause(self) -> None:
        """Freeze only the two bushing charts and a matching high-rate data window."""
        if self.graphs_paused:
            self.graphs_paused = False
            self._frozen_snapshot_samples = None
            self._frozen_position_graph = None
            self._frozen_velocity_graph = None
            self.pause_graphs_button.configure(text="Pause")
            self.save_snapshot_button.configure(state="disabled")
            return
        if not self._snapshot_samples:
            messagebox.showinfo(
                "No snapshot data",
                "Wait for calibrated AIN2 position samples before pausing the graphs.",
                parent=self,
            )
            return
        self.graphs_paused = True
        self._frozen_snapshot_samples = tuple(self._snapshot_samples)
        self._frozen_position_graph = self.bushing_plot.snapshot_state()
        self._frozen_velocity_graph = self.velocity_plot.snapshot_state()
        self.pause_graphs_button.configure(text="Resume")
        self.save_snapshot_button.configure(state="normal")
        self.log_event(
            "Graph snapshot paused",
            f"Frozen {len(self._frozen_snapshot_samples):,} calibrated AIN2 samples for review or export.",
        )

    def _snapshot_output_root(self) -> Path:
        """Use the configured test-output location when available, otherwise local app data."""
        configured_directory = str(self.auto_archive_config.get("directory", "")).strip()
        if configured_directory:
            try:
                candidate = Path(configured_directory)
                if candidate.is_dir():
                    return candidate / "snapshots"
            except OSError:
                pass
        return self.paths.root / "snapshots"

    def save_graph_snapshot(self) -> None:
        """Export the paused full-rate capture and the two frozen graphs as a self-contained folder."""
        samples = self._frozen_snapshot_samples
        position_graph, velocity_graph = self._frozen_position_graph, self._frozen_velocity_graph
        if not self.graphs_paused or not samples or position_graph is None or velocity_graph is None:
            return
        output_root = self._snapshot_output_root()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = output_root / f"Snapshot_{timestamp}"
        suffix = 2
        while destination.exists():
            destination = output_root / f"Snapshot_{timestamp}_{suffix:02d}"
            suffix += 1
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            destination.mkdir()
            data_file = destination / "position_velocity_200hz.csv"
            first_time = samples[0].monotonic_seconds
            with data_file.open("w", newline="", encoding="utf-8") as source:
                writer = csv.writer(source)
                writer.writerow([
                    "timestamp",
                    "elapsed_seconds",
                    "scan_rate_hz",
                    "ain2_voltage_volts",
                    "bushing_position_degrees",
                    "bushing_velocity_degrees_per_second",
                    "stream_gap_before",
                ])
                for sample in samples:
                    writer.writerow([
                        sample.timestamp,
                        f"{sample.monotonic_seconds - first_time:.6f}",
                        f"{sample.scan_rate_hz:.3f}",
                        f"{sample.ain2_voltage:.6f}",
                        f"{sample.position_degrees:.6f}",
                        "" if sample.velocity_degrees_per_second is None else f"{sample.velocity_degrees_per_second:.6f}",
                        "yes" if sample.stream_gap_before else "",
                    ])
            LivePlot.write_svg(destination / "position_graph.svg", position_graph)
            LivePlot.write_svg(destination / "velocity_graph.svg", velocity_graph)
            metadata = {
                "schema_version": 1,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source": "paused position and velocity dashboard graphs",
                "sample_count": len(samples),
                "capture_duration_seconds": round(samples[-1].monotonic_seconds - first_time, 4),
                "configured_history_seconds": SNAPSHOT_HISTORY_SECONDS,
                "current_live_run_id": self.current_live_run_id,
                "position_calibration": self.position_calibration,
                "cycle_stops": self.cycle_stops,
                "files": ["position_velocity_200hz.csv", "position_graph.svg", "velocity_graph.svg"],
            }
            (destination / "snapshot_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        except (OSError, csv.Error, ValueError) as error:
            messagebox.showerror("Snapshot export failed", f"Could not save the graph snapshot:\n{error}", parent=self)
            return
        self.log_event("Graph snapshot saved", f"{destination.name}: {len(samples):,} full-rate AIN2 samples.")
        messagebox.showinfo("Snapshot saved", f"Saved {len(samples):,} full-rate samples and both graphs.\n\n{destination}", parent=self)

    def open_calibration_window(self) -> None:
        if not self.labjack_available:
            return
        PositionCalibrationDialog(self)

    def open_export_window(self, preselect_run_id: str | None = None) -> None:
        ExportDataDialog(self, preselect_run_id=preselect_run_id)

    def open_auto_archive_window(self) -> None:
        AutoArchiveDialog(self)

    def open_local_data_folder(self) -> None:
        try:
            os.startfile(self.paths.root)  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror("Open Local Data Folder", f"Could not open the local data folder:\n{error}", parent=self)

    def open_local_data_window(self) -> None:
        LocalDataDialog(self)

    def open_cycle_stops_window(self) -> None:
        if not self.labjack_available:
            return
        if not self.position_calibration:
            messagebox.showerror(
                "Calibration required",
                "Save the two-point AIN2 position calibration before setting degree-based cycle stops.",
                parent=self,
            )
            return
        CycleStopsDialog(self)

    def _set_labjack_connection_state(self, state: str, detail: str) -> None:
        """Render a connection state from the same process that reads AIN2."""
        if state == "connected":
            text, color = "LabJack T4 • Connected • USB", "#86efac"
            event, event_detail = "LabJack connected", "AIN2 sampling active."
        elif state == "disconnected":
            text, color = "LabJack T4 • Disconnected", "#fbbf24"
            event, event_detail = "LabJack disconnected", detail
            self.bushing_position = None
            self.ain2_voltage = None
            self.bushing_velocity = None
            self.metric_vars["position"].set("—")
            self.metric_vars["velocity"].set("— °/s")
            self.bushing_detail_var.set("AIN2 unavailable")
        else:
            text, color = "LabJack T4 • Connection error", "#fca5a5"
            event, event_detail = "LabJack connection error", detail
            self.bushing_position = None
            self.ain2_voltage = None
            self.bushing_velocity = None
            self.metric_vars["position"].set("—")
            self.metric_vars["velocity"].set("— °/s")
            self.bushing_detail_var.set("AIN2 unavailable")

        self._set_labjack_dependent_controls(state == "connected")
        self.labjack_status_var.set(text)
        self.labjack_status_label.configure(foreground=color)
        signature = f"{state}|{detail}"
        if signature != self.last_labjack_signature:
            self.last_labjack_signature = signature
            self.log_event(event, event_detail)

    def record_grease_injection(self, commanded_pulse_seconds: float | None = None) -> str | None:
        """Register a real injector firing for later pre/post-cycle comparison.

        A future physical output adapter must call this at the actual firing time.
        It intentionally does not command hardware or create a synthetic event.
        """
        if not self.current_live_run_id:
            return None
        pulse: float | str = ""
        if commanded_pulse_seconds is not None:
            try:
                candidate = float(commanded_pulse_seconds)
                if candidate > 0:
                    pulse = round(candidate, 4)
            except (TypeError, ValueError):
                pass
        self._grease_event_sequence += 1
        event_id = f"grease_{self._grease_event_sequence:03d}"
        if self._cycle_capture is not None:
            phase = "during reset-to-count stroke"
            injection_cycle_number = self.cycles + 1
            self._cycle_capture.grease_event_ids.append(event_id)
        else:
            phase = "between completed cycles"
            injection_cycle_number = self.cycles
        event = GreaseEventCapture(
            event_id=event_id,
            started_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            started_monotonic=time.monotonic(),
            injection_cycle_number=injection_cycle_number,
            injection_cycle_phase=phase,
            commanded_pulse_seconds=pulse,
            pressure_baseline=self.grease_pressure,
            pre_cycles=tuple(self._recent_cycle_summaries),
        )
        self._pending_grease_events.append(event)
        self.log_event("Grease injection recorded", f"{event_id} registered for a {GREASE_COMPARISON_CYCLE_COUNT}-cycle comparison.")
        return event_id

    def record_grease_pressure(self, pressure_psi: float) -> None:
        """Accept a real grease-pressure reading when that input is implemented.

        This intentionally has no generated-data caller. A future LabJack input
        adapter can call it for each validated hardware sample.
        """
        try:
            value = float(pressure_psi)
        except (TypeError, ValueError):
            return
        if value < 0:
            return
        self.grease_pressure = value
        self.metric_vars["pressure"].set(f"{value:,.0f} psi")
        self.pressure_plot.add(value)
        sample_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        sample_monotonic = time.monotonic()
        for event in self._pending_grease_events:
            event.record_pressure(value, sample_at, sample_monotonic)

    def tick(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self.last_tick)
        self.last_tick = now
        self._update_live_bushing_position(now)
        if self.is_running:
            self.test_elapsed_seconds += elapsed
        if self.is_running and self.auto_archive_active:
            self.auto_archive_elapsed_seconds += elapsed
            interval_seconds = int(self.active_auto_archive_config["interval_seconds"])
            if self.auto_archive_elapsed_seconds >= interval_seconds:
                self._archive_auto_segment("scheduled_archive", "scheduled", continue_running=True)
        self.metric_vars["cycles"].set(str(self.cycles))
        self.metric_vars["frequency"].set(f"{self._frequency():0.1f} cpm")
        self.metric_vars["test_time"].set(self._format_test_time(self.test_elapsed_seconds))
        self.after(UPDATE_MS, self.tick)

    def on_close(self) -> None:
        if self.is_running:
            self.is_running = False
            self.log_event("Live run interrupted", "Application closed while cycle counting was active.")
            if self.auto_archive_active:
                self._archive_auto_segment("interrupted_by_application_close", "interrupted", continue_running=False)
            else:
                self._finish_live_run("interrupted_by_application_close")
        self.log_event("Application closed", "Operator closed the application.")
        self.ain2_monitor.close()
        self.logger.finalize(
            completed_cycles=self.cycles,
            cycle_frequency_cpm=round(self._frequency(), 2),
            final_auxiliary_status="not_configured",
        )
        self.logger.close()
        if not CsvLogger.read_live_runs(self.logger.folder):
            try:
                self.paths.delete_run_directory(self.logger.folder)
            except (OSError, ValueError):
                pass
        self.destroy()


if __name__ == "__main__":
    # In the packaged release, the GUI launches the dedicated diagnostic helper
    # for AIN2 streaming. Keep this dispatch for a direct helper invocation too.
    if any(argument in sys.argv for argument in ("--child-probe", "--sample-ain2", "--stream-ain2", "--json")):
        from labjack_diagnostic import main as diagnostic_main

        raise SystemExit(diagnostic_main())
    TestStandApp().mainloop()
