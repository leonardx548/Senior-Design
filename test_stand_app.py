"""Senior Design Test Stand interface with live LabJack T4 AIN2 position.

The bushing position and cycle count use the physical string potentiometer.
Auxiliary pressure and temperature channels stay visible until their hardware
inputs are configured; the application never generates placeholder readings.
PS12DC S0 is available only through the explicit manual/timer control window.
"""

from __future__ import annotations

import json
import csv
import math
import os
import shutil
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
from typing import Callable

from app_storage import AppPaths, CsvLogger
from labjack_diagnostic import (
    summarize_ain2_samples,
    summarize_voltage_samples,
    t4_device_temperature_celsius_from_ain14,
    t4_usb_air_temperature_celsius_from_ain14,
)


UPDATE_MS = 100
APP_VERSION = "0.4.1"
AIN2_POLL_SECONDS = 0.75
INJECTOR_ACTIVATION_PSI = 1500.0
GREASE_COMPARISON_CYCLE_COUNT = 3
GREASE_PRESSURE_RESPONSE_WINDOW_SECONDS = 10.0
SNAPSHOT_HISTORY_SECONDS = 15
SNAPSHOT_REVIEW_SECONDS = 60
SNAPSHOT_STREAM_RATE_HZ = 200
SNAPSHOT_HISTORY_MAX_SAMPLES = SNAPSHOT_REVIEW_SECONDS * SNAPSHOT_STREAM_RATE_HZ
RAW_AUXILIARY_CAPTURE_SECONDS = 60
RAW_AUXILIARY_HISTORY_MAX_SAMPLES = RAW_AUXILIARY_CAPTURE_SECONDS * SNAPSHOT_STREAM_RATE_HZ + 20
SNAPSHOT_PAN_STEP_SECONDS = 5
RUN_OUTPUT_CONFIG_FILENAME = "run_output.json"
VELOCITY_SLOPE_WINDOW_SAMPLES = 11
VELOCITY_ZERO_DEADBAND_DPS = 0.25
DEFAULT_VELOCITY_GRAPH_LIMIT_DPS = 100.0
CYCLE_MID_STROKE_FRACTION = 0.05
MIN_CALIBRATION_VOLTAGE_SPAN = 0.050
MIN_PRESSURE_CALIBRATION_VOLTAGE_SPAN = 0.020
# A gauge may be captured at its practical pressure limit while normal analog
# noise still crosses that voltage slightly. Retain a bounded extrapolation
# band rather than making the live pressure readout blink at the endpoints.
PRESSURE_CALIBRATION_EXTRAPOLATION_FRACTION = 0.20
MIN_CYCLE_STOP_SEPARATION_DEGREES = 1.0
CALIBRATION_STREAM_SAMPLE_COUNT = 100
POSITION_RANGE_CAPTURE_SECONDS = 10.0
POSITION_RANGE_CAPTURE_MIN_SAMPLES = 1200
POSITION_RANGE_CAPTURE_HISTORY_SAMPLES = 2600
TEMPERATURE_NOMINAL_GAIN = 201.0
TEMPERATURE_NOMINAL_OFFSET_VOLTS = 1.25
TEMPERATURE_HISTORY_SAMPLE_SECONDS = 1.0
TYPE_T_MIN_CELSIUS = -200.0
TYPE_T_MAX_CELSIUS = 400.0
LABJACK_LJM_WINDOWS_URL = "https://support.labjack.com/docs/ljm-software-installer-windows"
GITHUB_RELEASES_URL = "https://github.com/leonardx548/Senior-Design/releases/latest"

EXPORT_FILE_OPTIONS = (
    ("cycles", "Cycle summary", "cycle_summary.csv"),
    ("grease", "Grease events", "grease_events.csv"),
    ("events", "Event log", "events.csv"),
    ("metadata", "Run metadata", "live_run.json"),
    ("configuration", "Run configuration", "run_configuration.json"),
)

# NIST Monograph 175, Type T reference function coefficients (millivolts). The
# forward function is relative to a 0 °C reference junction.
TYPE_T_NEGATIVE_COEFFICIENTS = (
    0.0, 0.387481063640e-1, 0.441944343470e-4, 0.118443231050e-6,
    0.200329735540e-7, 0.901380195590e-9, 0.226511565930e-10,
    0.360711542050e-12, 0.384939398830e-14, 0.282135219250e-16,
    0.142515947790e-18, 0.487686622860e-21, 0.107955392700e-23,
    0.139450270620e-26, 0.797951539270e-30,
)
TYPE_T_POSITIVE_COEFFICIENTS = (
    0.0, 0.387481063640e-1, 0.332922278800e-4, 0.206182434040e-6,
    -0.218822568460e-8, 0.109968809280e-10, -0.308157587720e-13,
    0.454791352900e-16, -0.275129016730e-19,
)


def type_t_emf_millivolts(temperature_celsius: float) -> float:
    """Return the NIST ITS-90 Type-T EMF in mV, referenced to 0 °C."""
    temperature = float(temperature_celsius)
    if not math.isfinite(temperature) or not TYPE_T_MIN_CELSIUS <= temperature <= TYPE_T_MAX_CELSIUS:
        raise ValueError("Type T temperature must be between -200 and 400 °C.")
    coefficients = TYPE_T_NEGATIVE_COEFFICIENTS if temperature < 0.0 else TYPE_T_POSITIVE_COEFFICIENTS
    # Horner's method avoids a needless loss of precision at room temperature.
    emf_millivolts = 0.0
    for coefficient in reversed(coefficients):
        emf_millivolts = emf_millivolts * temperature + coefficient
    return emf_millivolts


def type_t_temperature_celsius(emf_millivolts: float) -> float:
    """Invert the Type-T forward function by bounded bisection."""
    emf = float(emf_millivolts)
    low, high = TYPE_T_MIN_CELSIUS, TYPE_T_MAX_CELSIUS
    if not math.isfinite(emf) or not type_t_emf_millivolts(low) <= emf <= type_t_emf_millivolts(high):
        raise ValueError("Type T EMF is outside the supported -200 to 400 °C range.")
    for _ in range(48):
        midpoint = (low + high) / 2.0
        if type_t_emf_millivolts(midpoint) < emf:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2.0


def temperature_from_amplified_voltage(voltage: float, calibration: dict[str, object], channel: str) -> float:
    """Apply CJC and a saved LJTick-InAmp channel calibration to one T4 voltage."""
    cjc_celsius = float(calibration["cjc_celsius"])
    channels = calibration["channels"]
    if not isinstance(channels, dict) or channel not in channels or not isinstance(channels[channel], dict):
        raise ValueError(f"No temperature calibration exists for {channel}.")
    settings = channels[channel]
    gain = float(settings["gain"])
    offset = float(settings["offset_volts"])
    if not math.isfinite(gain) or gain <= 0 or not math.isfinite(offset):
        raise ValueError("Temperature calibration has an invalid gain or offset.")
    differential_millivolts = (float(voltage) - offset) * 1000.0 / gain
    return type_t_temperature_celsius(type_t_emf_millivolts(cjc_celsius) + differential_millivolts)


def summarize_ain2_travel_range(samples: list[float]) -> dict[str, float | int | str]:
    """Return robust AIN2 endpoints from a full-travel capture.

    The cylinder need not pause at either end.  Isolated electrical spikes are
    rejected with a broad median-absolute-deviation screen, then the outer
    0.5% of retained values are ignored before averaging a small endpoint band.
    """
    values: list[float] = []
    for raw_value in samples:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    values.sort()
    if len(values) < POSITION_RANGE_CAPTURE_MIN_SAMPLES:
        return {"status": "error", "error": "Too few AIN2 samples were captured. Move through the full range for the entire 10 seconds."}
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    if mad > 0:
        limit = mad * 8.0
        conditioned = [value for value in values if abs(value - median) <= limit]
    else:
        conditioned = values
    if len(conditioned) < POSITION_RANGE_CAPTURE_MIN_SAMPLES:
        return {"status": "error", "error": "Too many AIN2 samples were rejected as outliers."}
    trim_count = max(3, math.ceil(len(conditioned) * 0.005))
    endpoint_count = max(5, math.ceil(len(conditioned) * 0.01))
    if len(conditioned) <= 2 * (trim_count + endpoint_count):
        return {"status": "error", "error": "AIN2 capture did not contain enough conditioned samples."}
    low_band = conditioned[trim_count:trim_count + endpoint_count]
    high_band = conditioned[-trim_count - endpoint_count:-trim_count]
    low_voltage, high_voltage = statistics.fmean(low_band), statistics.fmean(high_band)
    return {
        "status": "success",
        "low_voltage": low_voltage,
        "high_voltage": high_voltage,
        "voltage_span": high_voltage - low_voltage,
        "sample_count": len(conditioned),
        "rejected_count": len(values) - len(conditioned) + 2 * trim_count,
    }


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
    invalid_reason: str | None = None

    def invalidate(self, reason: str) -> bool:
        """Keep the cycle record but prevent invalid motion metrics from being exported."""
        if self.invalid_reason is not None:
            return False
        self.invalid_reason = reason
        return True

    def observe(self, angle: float, velocity: float | None, sample_at: str, sample_monotonic: float) -> None:
        if self.invalid_reason is not None:
            return
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
        metrics_available = self.invalid_reason is None
        if self.invalid_reason:
            quality.append(self.invalid_reason)
        if self.stream_gap_detected:
            quality.append("AIN2 stream gap")
        if metrics_available and not self.mid_stroke_velocities:
            quality.append("mid-stroke velocity unavailable")
        if metrics_available and (self.motion_started_monotonic is None or self.near_count_stop_monotonic is None):
            quality.append("mid-stroke timing unavailable")
        return {
            "completed_at": completed_at,
            "cycle_number": cycle_number,
            "live_run_cycle_number": live_run_cycle_number,
            "reset_stop_reached_at": self.reset_stop_reached_at,
            "motion_started_at": self.motion_started_at if metrics_available and self.motion_started_at else "",
            "count_stop_reached_at": completed_at,
            "reset_to_count_duration_seconds": round(completed_monotonic - self.reset_stop_monotonic, 4),
            "mid_stroke_duration_seconds": (
                "" if not metrics_available or self.motion_started_monotonic is None or self.near_count_stop_monotonic is None
                else round(self.near_count_stop_monotonic - self.motion_started_monotonic, 4)
            ),
            "travel_direction": "increasing angle" if self.direction > 0 else "decreasing angle",
            "reset_stop_degrees": round(self.reset_stop_degrees, 4),
            "count_stop_degrees": round(self.count_stop_degrees, 4),
            "bushing_peak_velocity": "" if not metrics_available or self.peak_velocity is None else round(self.peak_velocity, 4),
            "bushing_peak_signed_velocity": "" if not metrics_available or self.peak_signed_velocity is None else round(self.peak_signed_velocity, 4),
            "bushing_median_travel_velocity": (
                "" if not metrics_available or not self.mid_stroke_velocities else round(statistics.median(self.mid_stroke_velocities), 4)
            ),
            "bushing_velocity_sample_count": len(self.mid_stroke_velocities) if metrics_available else 0,
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
    overlapping_event_ids: list[str] = field(default_factory=list)

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
            "pressure_response_threshold_psi": INJECTOR_ACTIVATION_PSI,
            "pressure_response_window_seconds": GREASE_PRESSURE_RESPONSE_WINDOW_SECONDS,
            "pressure_sample_count": self.pressure_sample_count,
            "overlapping_grease_event_ids": ";".join(self.overlapping_event_ids),
            "comparison_quality": "overlapping injection windows" if self.overlapping_event_ids else "isolated",
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


@dataclass(frozen=True)
class AuxiliaryRawSample:
    """One unscaled AIN0/AIN1 scan kept only for an operator diagnostic capture."""

    timestamp: str
    monotonic_seconds: float
    scan_rate_hz: float
    batch_received_at: str
    ain0_voltage: float | None
    ain1_voltage: float | None
    device_backlog: int
    ljm_backlog: int


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
        self._window_end_override: float | None = None
        self._time_labels: tuple[str, str] | None = None
        self.bind("<Configure>", lambda _event: self.redraw())

    def add(self, value: float, sample_monotonic: float | None = None) -> None:
        sample_time = time.monotonic() if sample_monotonic is None else sample_monotonic
        self._window_end_override = None
        self._time_labels = None
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
            self._window_end_override = None
            self._time_labels = None
        self.redraw()

    def set_reference_lines(self, lines: list[tuple[float, str, str]]) -> None:
        """Draw labeled horizontal thresholds without affecting live samples."""
        self.reference_lines = lines
        self.redraw()

    def show_window(
        self,
        samples: tuple[tuple[float, float], ...],
        *,
        window_end_monotonic: float,
        left_time_label: str,
        right_time_label: str,
    ) -> None:
        """Render an explicit, frozen time window without changing its scale."""
        self.samples = deque(
            (sample_time, max(self.minimum, min(self.maximum, value))) for sample_time, value in samples
        )
        self._window_end_override = window_end_monotonic
        self._time_labels = (left_time_label, right_time_label)
        self.redraw()

    @staticmethod
    def _render_samples(samples: tuple[tuple[float, float], ...], maximum_points: int) -> tuple[tuple[float, float], ...]:
        """Reduce dense raw data while preserving the high and low value in each bucket."""
        if len(samples) <= maximum_points:
            return samples
        bucket_count = max(1, maximum_points // 2)
        bucket_size = max(1, math.ceil(len(samples) / bucket_count))
        selected_indexes = {0, len(samples) - 1}
        for start in range(0, len(samples), bucket_size):
            stop = min(len(samples), start + bucket_size)
            indexes = range(start, stop)
            selected_indexes.add(min(indexes, key=lambda index: samples[index][1]))
            selected_indexes.add(max(indexes, key=lambda index: samples[index][1]))
        return tuple(samples[index] for index in sorted(selected_indexes))

    def _window_end_monotonic(self) -> float:
        return self._window_end_override if self._window_end_override is not None else (
            self.samples[-1][0] if self.samples else time.monotonic()
        )

    def _axis_time_labels(self) -> tuple[str, str]:
        return self._time_labels or (f"{self.history_seconds:g}s ago", "now")

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
            "window_end_monotonic": self._window_end_monotonic(),
            "left_time_label": self._axis_time_labels()[0],
            "right_time_label": self._axis_time_labels()[1],
            "width": max(self.winfo_width(), 600),
            "height": max(self.winfo_height(), 240),
        }

    def load_snapshot_state(self, state: dict[str, object]) -> None:
        """Mirror another plot's current view in an expanded read-only canvas."""
        self.title = str(state["title"])
        self.minimum = float(state["minimum"])
        self.maximum = float(state["maximum"])
        self.color = str(state["color"])
        self.samples = deque((float(sample[0]), float(sample[1])) for sample in state["samples"])
        self.reference_lines = [tuple(item) for item in state["reference_lines"]]  # type: ignore[list-item]
        self.history_seconds = float(state["history_seconds"])
        self._window_end_override = float(state["window_end_monotonic"])
        self._time_labels = (
            str(state.get("left_time_label", f"{self.history_seconds:g}s ago")),
            str(state.get("right_time_label", "now")),
        )
        self.redraw()

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
        left_time_label = str(state.get("left_time_label", f"{history_seconds:g}s ago"))
        right_time_label = str(state.get("right_time_label", "now"))
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
            f'<text x="{margin_left}" y="{height - 8}" fill="#64748b" font-family="Segoe UI, Arial" font-size="9">{xml_escape(left_time_label)}</text>',
            f'<text x="{width - margin_right}" y="{height - 8}" text-anchor="end" fill="#64748b" font-family="Segoe UI, Arial" font-size="9">{xml_escape(right_time_label)}</text>',
        ))
        for reference in reference_lines:
            value, reference_color = float(reference[0]), str(reference[1])
            if minimum <= value <= maximum:
                y = margin_top + chart_height * (1 - (value - minimum) / (maximum - minimum))
                lines.append(
                    f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" stroke="{reference_color}" stroke-width="2" stroke-dasharray="5 3"/>'
                )
        render_samples = LivePlot._render_samples(samples, max(200, int(chart_width * 2)))
        if len(render_samples) >= 2:
            points = []
            for sample_time, value in render_samples:
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
        window_end = self._window_end_monotonic()
        window_start = window_end - self.history_seconds
        left_time_label, right_time_label = self._axis_time_labels()
        self.create_text(margin_left, height - 9, text=left_time_label, anchor="w", fill="#64748b",
                         font=("Segoe UI", 8))
        self.create_text(width - margin_right, height - 9, text=right_time_label, anchor="e", fill="#64748b",
                         font=("Segoe UI", 8))
        for value, color, _label in self.reference_lines:
            if not self.minimum <= value <= self.maximum:
                continue
            y = margin_top + chart_height * (1 - (value - self.minimum) / (self.maximum - self.minimum))
            self.create_line(margin_left, y, width - margin_right, y, fill=color, width=2, dash=(5, 3))
        render_samples = self._render_samples(tuple(self.samples), max(200, int(chart_width * 2)))
        if len(render_samples) < 2:
            return
        points: list[float] = []
        for sample_time, value in render_samples:
            x = margin_left + chart_width * max(0.0, min(1.0, (sample_time - window_start) / self.history_seconds))
            y = margin_top + chart_height * (1 - (value - self.minimum) / (self.maximum - self.minimum))
            points.extend([x, y])
        # Raw AIN2 data should be drawn as measured. Tk spline smoothing can
        # invent loops/overshoot and becomes slow with a dense paused window.
        self.create_line(*points, fill=self.color, width=2)


class LabJackAin2Monitor:
    """Consume the persistent shared-input helper and its limited PS12DC S0 control."""

    def __init__(self) -> None:
        # Keep every batch until Tk has processed its counting samples.
        self._results: Queue[dict[str, object]] = Queue()
        self._process: subprocess.Popen[str] | None = None
        self._enabled = True
        self._stream_samples: deque[tuple[float, float]] = deque(maxlen=POSITION_RANGE_CAPTURE_HISTORY_SAMPLES)
        self._ain4_stream_samples: deque[tuple[float, float]] = deque(maxlen=1000)
        self._auxiliary_stream_samples: dict[str, deque[tuple[float, float]]] = {
            "AIN0": deque(maxlen=1000), "AIN1": deque(maxlen=1000),
        }
        self._ain14_stream_samples: deque[tuple[float, float]] = deque(maxlen=1000)
        self._samples_lock = threading.Lock()
        self._output_state: bool | None = None
        self._outputs_lock = threading.Lock()

    @property
    def is_reading(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start_read(self) -> None:
        if not self._enabled or self.is_reading:
            return
        with self._samples_lock:
            self._stream_samples.clear()
            self._ain4_stream_samples.clear()
            for samples in self._auxiliary_stream_samples.values():
                samples.clear()
            self._ain14_stream_samples.clear()
        with self._outputs_lock:
            self._output_state = None
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
        with self._outputs_lock:
            self._output_state = None
        if self._process is not None and self._process.poll() is None:
            try:
                if self._process.stdin is None:
                    raise OSError("AIN2 helper control pipe is unavailable.")
                self._process.stdin.write("STOP\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                # Fall back only if the cooperative stop channel has already failed.
                self._process.terminate()

    def set_ps12dc_s0(self, enabled: bool) -> bool:
        """Request a PS12DC S0 state change on the helper's LJM handle."""
        if not self.is_reading:
            return False
        try:
            if self._process is None or self._process.stdin is None:
                return False
            self._process.stdin.write(f"SET_OUTPUT DIO8 {1 if enabled else 0}\n")
            self._process.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def ps12dc_s0_state(self) -> bool | None:
        with self._outputs_lock:
            return self._output_state

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

    def samples_since(self, started_at: float, count: int | None) -> list[float]:
        """Return fresh AIN2 stream samples for calibration from this same read path."""
        with self._samples_lock:
            samples = [value for timestamp, value in self._stream_samples if timestamp >= started_at]
        return samples if count is None else samples[:count]

    def ain4_samples_since(self, started_at: float, count: int) -> list[float]:
        """Return fresh FIO4/AIN4 samples from the same T4 stream."""
        with self._samples_lock:
            samples = [value for timestamp, value in self._ain4_stream_samples if timestamp >= started_at]
        return samples[:count]

    def auxiliary_samples_since(self, channel: str, started_at: float, count: int) -> list[float]:
        """Return fresh AIN0/AIN1 samples without opening a second T4 connection."""
        with self._samples_lock:
            history = self._auxiliary_stream_samples.get(channel, ())
            samples = [value for timestamp, value in history if timestamp >= started_at]
        return samples[:count]

    def ain14_samples_since(self, started_at: float, count: int) -> list[float]:
        """Return fresh streamed T4 internal-temperature sensor voltages."""
        with self._samples_lock:
            samples = [value for timestamp, value in self._ain14_stream_samples if timestamp >= started_at]
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
                if result.get("status") == "output":
                    channel = result.get("channel")
                    enabled = result.get("enabled")
                    if channel == "DIO8" and isinstance(enabled, bool):
                        with self._outputs_lock:
                            self._output_state = enabled
                if result.get("status") == "success":
                    timestamp = time.monotonic()
                    try:
                        samples = [float(value) for value in result.get("samples", [])]
                    except (TypeError, ValueError):
                        samples = []
                    if samples:
                        with self._samples_lock:
                            self._stream_samples.extend((timestamp, value) for value in samples)
                    try:
                        ain4_samples = [float(value) for value in result.get("ain4_samples", [])]
                    except (TypeError, ValueError):
                        ain4_samples = []
                    if ain4_samples:
                        with self._samples_lock:
                            self._ain4_stream_samples.extend((timestamp, value) for value in ain4_samples)
                    with self._samples_lock:
                        for channel, field in (("AIN0", "ain0_samples"), ("AIN1", "ain1_samples")):
                            try:
                                values = [float(value) for value in result.get(field, [])]
                            except (TypeError, ValueError):
                                values = []
                            self._auxiliary_stream_samples[channel].extend(
                                (timestamp, value) for value in values if math.isfinite(value)
                            )
                        try:
                            ain14_values = [float(value) for value in result.get("ain14_samples", [])]
                        except (TypeError, ValueError):
                            ain14_values = []
                        self._ain14_stream_samples.extend(
                            (timestamp, value) for value in ain14_values if math.isfinite(value)
                        )
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
    """Full-travel AIN2-to-angle calibration without stopping the cylinder."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Calibrate Bushing Position — LabJack AIN2")
        self.resizable(False, False)
        self.configure(background="#f1f5f9")
        self.points: dict[int, dict[str, float]] = {}
        self._sampling = False
        self._sample_started_at = 0.0
        self.capture_buttons: list[ttk.Button] = []
        self.angle_vars = {1: tk.StringVar(), 2: tk.StringVar()}
        self.point_vars = {1: tk.StringVar(value="Not captured"), 2: tk.StringVar(value="Not captured")}
        self.preview_var = tk.StringVar(value="No AIN2 reading captured yet.")
        self.status_var = tk.StringVar(value="Enter endpoint angles, then capture one full 10-second travel range.")
        self._build()
        self._load_existing_points()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh_live_controls()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="AIN2 bushing-position calibration", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Enter the angles at the low- and high-voltage ends. Then move the cylinder repeatedly through its full travel during the 10-second capture.",
            wraplength=610,
            foreground="#475569",
        ).pack(anchor="w", pady=(4, 14))

        for point, endpoint in ((1, "Low AIN2 voltage"), (2, "High AIN2 voltage")):
            frame = ttk.LabelFrame(outer, text=endpoint, padding=12)
            frame.pack(fill="x", pady=(0, 10))
            ttk.Label(frame, text="Known bushing angle (degrees):").grid(row=0, column=0, sticky="w")
            ttk.Entry(frame, textvariable=self.angle_vars[point], width=12).grid(row=0, column=1, padx=8)
            ttk.Label(frame, textvariable=self.point_vars[point], foreground="#475569").grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(8, 0)
            )

        self.range_capture_button = ttk.Button(outer, text="Capture 10-Second Travel Range", command=self.capture_travel_range)
        self.range_capture_button.pack(anchor="w", pady=(0, 10))
        self.capture_buttons.append(self.range_capture_button)

        preview = ttk.LabelFrame(outer, text="AIN2 check", padding=12)
        preview.pack(fill="x", pady=(0, 10))
        self.preview_button = ttk.Button(preview, text="Read AIN2 Now", command=self.preview_ain2)
        self.preview_button.pack(side="left")
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

    def _refresh_live_controls(self) -> None:
        available = self.app.labjack_available and self.app.ain2_monitor.is_reading and not self._sampling
        state = "normal" if available else "disabled"
        for button in self.capture_buttons:
            button.configure(state=state)
        self.preview_button.configure(state=state)
        if self.winfo_exists():
            self.after(250, self._refresh_live_controls)

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
        self.status_var.set("Saved calibration loaded. Capture a new full travel range to replace it.")

    def _set_point_label(self, point: int) -> None:
        values = self.points[point]
        self.point_vars[point].set(
            f"{values['voltage']:.4f} V at {values['angle']:.3f}°"
        )

    def _parse_angle(self, point: int) -> float | None:
        try:
            return float(self.angle_vars[point].get())
        except ValueError:
            messagebox.showerror("Angle required", "Enter a numeric known bushing angle in degrees before capturing.", parent=self)
            return None

    def capture_travel_range(self) -> None:
        low_angle, high_angle = self._parse_angle(1), self._parse_angle(2)
        if low_angle is None or high_angle is None:
            return
        if low_angle == high_angle:
            messagebox.showerror("Angles must differ", "Enter different known angles for the two travel endpoints.", parent=self)
            return
        if self._sampling:
            return
        if not self.app.ain2_monitor.is_reading:
            self.status_var.set("Waiting for the live AIN2 stream…")
            return
        self._sampling = True
        self._sample_started_at = time.monotonic()
        self.status_var.set("Capturing AIN2 travel range for 10 seconds… move through the full stroke repeatedly.")
        self.status_label.configure(foreground="#1e3a8a")
        self.after(int(POSITION_RANGE_CAPTURE_SECONDS * 1000), lambda: self._finish_travel_range(low_angle, high_angle))

    def preview_ain2(self) -> None:
        self._start_sample()

    def _start_sample(self) -> None:
        if self._sampling:
            return
        if not self.app.ain2_monitor.is_reading:
            self.status_var.set("Waiting for the live AIN2 stream…")
            self.status_label.configure(foreground="#1e3a8a")
            self.after(100, self._start_sample)
            return
        self._sampling = True
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
        text = f"AIN2: {voltage:.4f} V (σ {standard_deviation * 1000:.1f} mV, span {span * 1000:.1f} mV)"
        calibration = self.app.position_calibration
        if calibration:
            estimated_angle, outside = self.app.angle_from_voltage(voltage, calibration)
            text += f"  →  {estimated_angle:.3f}°" + (" (outside calibrated span)" if outside else "")
        self.preview_var.set(text)
        self.status_var.set("AIN2 reading accepted.")
        self.status_label.configure(foreground="#166534")

    def _finish_travel_range(self, low_angle: float, high_angle: float) -> None:
        samples = self.app.ain2_monitor.samples_since(self._sample_started_at, None)
        self._sampling = False
        result = summarize_ain2_travel_range(samples)
        if result.get("status") != "success":
            self.status_var.set(f"Capture not accepted: {result.get('error', 'full travel was not captured.')}")
            self.status_label.configure(foreground="#b91c1c")
            return
        low_voltage, high_voltage = float(result["low_voltage"]), float(result["high_voltage"])
        span = high_voltage - low_voltage
        if span < MIN_CALIBRATION_VOLTAGE_SPAN:
            self.status_var.set("Capture not accepted: AIN2 did not cover enough voltage range. Move through the full stroke.")
            self.status_label.configure(foreground="#b91c1c")
            return
        self.points = {
            1: {"angle": low_angle, "voltage": low_voltage, "standard_deviation": 0.0, "span": span},
            2: {"angle": high_angle, "voltage": high_voltage, "standard_deviation": 0.0, "span": span},
        }
        self._set_point_label(1)
        self._set_point_label(2)
        self.status_var.set(
            f"Travel range captured: {low_voltage:.4f}–{high_voltage:.4f} V from {int(result['sample_count']):,} conditioned samples; {int(result['rejected_count']):,} endpoint/outlier samples excluded."
        )
        self.status_label.configure(foreground="#166534")
        self.app.log_event("AIN2 travel range captured", f"{low_voltage:.4f}–{high_voltage:.4f} V across 10 seconds.")

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
        if not self.app.save_position_calibration(calibration):
            return
        self.status_var.set("Calibration saved. AIN2 previews now show an interpolated bushing angle.")
        self.status_label.configure(foreground="#166534")
        self.app.log_event("AIN2 calibration saved", "Two-point linear AIN2-to-bushing-angle calibration saved.")

    def clear_calibration(self) -> None:
        if not self.app.position_calibration:
            return
        if messagebox.askyesno("Clear calibration", "Clear the saved AIN2 position calibration?", parent=self):
            if not self.app.clear_position_calibration():
                return
            self.points.clear()
            for point in (1, 2):
                self.point_vars[point].set("Not captured")
                self.angle_vars[point].set("")
            self.preview_var.set("No AIN2 reading captured yet.")
            self.status_var.set("Saved calibration cleared.")


class PressureCalibrationDialog(tk.Toplevel):
    """Two-point FIO4/AIN4-to-PSI calibration against the installed gauge."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Calibrate Grease Pressure — LabJack FIO4")
        self.resizable(False, False)
        self.configure(background="#f1f5f9")
        self.points: dict[int, dict[str, float]] = {}
        self._sampling = False
        self._pending_point: int | None = None
        self._pending_pressure: float | None = None
        self._sample_started_at = 0.0
        self.capture_buttons: list[ttk.Button] = []
        self.pressure_vars = {1: tk.StringVar(), 2: tk.StringVar()}
        self.point_vars = {1: tk.StringVar(value="Not captured"), 2: tk.StringVar(value="Not captured")}
        self.preview_var = tk.StringVar(value="No AIN4 reading captured yet.")
        self.status_var = tk.StringVar(value="Enter the gauge pressure and capture two steady points.")
        self._build()
        self._load_existing_points()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh_live_controls()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="FIO4 / AIN4 grease-pressure calibration", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Capture two steady gauge readings. AIN4 range: 0–2.5 V.",
            wraplength=620,
            foreground="#475569",
        ).pack(anchor="w", pady=(4, 14))

        for point in (1, 2):
            frame = ttk.LabelFrame(outer, text=f"Calibration point {point}", padding=12)
            frame.pack(fill="x", pady=(0, 10))
            ttk.Label(frame, text="Pressure shown on gauge (psi):").grid(row=0, column=0, sticky="w")
            ttk.Entry(frame, textvariable=self.pressure_vars[point], width=12).grid(row=0, column=1, padx=8)
            button = ttk.Button(frame, text=f"Capture Point {point}", command=lambda p=point: self.capture_point(p))
            button.grid(
                row=0, column=2, padx=(8, 0)
            )
            self.capture_buttons.append(button)
            ttk.Label(frame, textvariable=self.point_vars[point], foreground="#475569").grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(8, 0)
            )

        preview = ttk.LabelFrame(outer, text="AIN4 check", padding=12)
        preview.pack(fill="x", pady=(0, 10))
        self.preview_button = ttk.Button(preview, text="Read AIN4 Now", command=self.preview_ain4)
        self.preview_button.pack(side="left")
        ttk.Label(preview, textvariable=self.preview_var, wraplength=475, foreground="#475569").pack(
            side="left", padx=12
        )
        self.status_label = ttk.Label(outer, textvariable=self.status_var, wraplength=620, foreground="#1e3a8a")
        self.status_label.pack(anchor="w", pady=(2, 12))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Apply and Save Calibration", style="Grease.TButton", command=self.apply_calibration).pack(
            side="left"
        )
        ttk.Button(buttons, text="Clear Saved Calibration", command=self.clear_calibration).pack(side="left", padx=8)

    def _refresh_live_controls(self) -> None:
        available = self.app.labjack_available and self.app.ain2_monitor.is_reading and not self._sampling
        state = "normal" if available else "disabled"
        for button in self.capture_buttons:
            button.configure(state=state)
        self.preview_button.configure(state=state)
        if self.winfo_exists():
            self.after(250, self._refresh_live_controls)

    def close(self) -> None:
        if self._sampling:
            self.status_var.set("AIN4 sampling is still in progress. Please wait a moment.")
            self.status_label.configure(foreground="#1e3a8a")
            return
        self.destroy()

    def _load_existing_points(self) -> None:
        existing = self.app.pressure_calibration
        if not existing:
            return
        for point, values in ((1, existing["point_1"]), (2, existing["point_2"])):
            self.points[point] = dict(values)
            self.pressure_vars[point].set(f"{values['pressure_psi']:.1f}")
            self._set_point_label(point)
        self.status_var.set("Saved calibration loaded. Capture a point again to replace it.")

    def _set_point_label(self, point: int) -> None:
        values = self.points[point]
        self.point_vars[point].set(
            f"Captured {values['voltage']:.4f} V at {values['pressure_psi']:,.1f} psi "
            f"(σ {values['standard_deviation'] * 1000:.1f} mV, span {values['span'] * 1000:.1f} mV)"
        )

    def _parse_pressure(self, point: int) -> float | None:
        try:
            pressure = float(self.pressure_vars[point].get())
        except ValueError:
            messagebox.showerror("Pressure required", "Enter the numeric gauge pressure in psi before capturing.", parent=self)
            return None
        if not math.isfinite(pressure) or pressure < 0:
            messagebox.showerror("Valid pressure required", "Enter a finite, non-negative gauge pressure in psi.", parent=self)
            return None
        return pressure

    def capture_point(self, point: int) -> None:
        pressure = self._parse_pressure(point)
        if pressure is not None:
            self._start_sample(point, pressure)

    def preview_ain4(self) -> None:
        self._start_sample(None, None)

    def _start_sample(self, point: int | None, pressure: float | None) -> None:
        if self._sampling:
            return
        if not self.app.ain2_monitor.is_reading:
            self.status_var.set("Waiting for the live AIN4 stream…")
            self.status_label.configure(foreground="#1e3a8a")
            self.after(100, lambda: self._start_sample(point, pressure))
            return
        self._sampling = True
        self._pending_point, self._pending_pressure = point, pressure
        self._sample_started_at = time.monotonic()
        self.status_var.set("Sampling AIN4… hold the gauge pressure steady.")
        self.status_label.configure(foreground="#1e3a8a")
        self.after(550, self._check_stream_sample)

    def _check_stream_sample(self) -> None:
        samples = self.app.ain2_monitor.ain4_samples_since(self._sample_started_at, CALIBRATION_STREAM_SAMPLE_COUNT)
        if len(samples) < CALIBRATION_STREAM_SAMPLE_COUNT:
            if time.monotonic() - self._sample_started_at < 3.0:
                self.after(60, self._check_stream_sample)
                return
            result: dict[str, object] = {"status": "error", "error": "Live AIN4 samples were not available."}
        else:
            result = summarize_voltage_samples(samples, "AIN4")
        self._sampling = False
        point, pressure = self._pending_point, self._pending_pressure
        self._pending_point, self._pending_pressure = None, None
        if result.get("status") != "success":
            reason = result.get("error") or "Signal changed too much during the capture. Hold the pressure steady and try again."
            self.status_var.set(f"Capture not accepted: {reason}")
            self.status_label.configure(foreground="#b91c1c")
            return

        voltage = float(result["voltage"])
        standard_deviation = float(result["standard_deviation"])
        span = float(result["span"])
        if voltage > 2.45:
            self.status_var.set("Capture not accepted: AIN4 is at or near its 2.5 V input limit. Check the sensor output scaling.")
            self.status_label.configure(foreground="#b91c1c")
            return
        if point is not None and pressure is not None:
            self.points[point] = {
                "pressure_psi": pressure,
                "voltage": voltage,
                "standard_deviation": standard_deviation,
                "span": span,
            }
            self._set_point_label(point)
            self.status_var.set(f"Point {point} captured. Capture the other point, then apply the calibration.")
            self.app.log_event("AIN4 pressure calibration point captured", f"Point {point}: {voltage:.4f} V at {pressure:.1f} psi.")
        else:
            text = f"AIN4: {voltage:.4f} V (σ {standard_deviation * 1000:.1f} mV, span {span * 1000:.1f} mV)"
            calibration = self.app.pressure_calibration
            if calibration:
                estimated_pressure, outside = self.app.pressure_from_voltage(voltage, calibration)
                text += f"  →  {estimated_pressure:,.1f} psi" + (" (outside calibrated span)" if outside else "")
            self.preview_var.set(text)
            self.status_var.set("AIN4 reading accepted.")
        self.status_label.configure(foreground="#166534")

    def apply_calibration(self) -> None:
        if set(self.points) != {1, 2}:
            messagebox.showerror("Two points needed", "Capture both calibration points before applying.", parent=self)
            return
        first, second = self.points[1], self.points[2]
        if abs(second["voltage"] - first["voltage"]) < MIN_PRESSURE_CALIBRATION_VOLTAGE_SPAN:
            messagebox.showerror(
                "Points too close",
                "The two captured voltages are too close together. Use two gauge pressures farther apart.",
                parent=self,
            )
            return
        if first["pressure_psi"] == second["pressure_psi"]:
            messagebox.showerror("Pressures must differ", "Enter two different gauge pressures.", parent=self)
            return
        calibration = {"version": 1, "input": "AIN4", "terminal": "FIO4", "point_1": first, "point_2": second}
        if not self.app.save_pressure_calibration(calibration):
            return
        self.status_var.set("Calibration saved. The dashboard and graph now use interpolated AIN4 pressure.")
        self.status_label.configure(foreground="#166534")
        self.app.log_event("AIN4 pressure calibration saved", "Two-point linear FIO4/AIN4-to-pressure calibration saved.")

    def clear_calibration(self) -> None:
        if not self.app.pressure_calibration:
            return
        if messagebox.askyesno("Clear calibration", "Clear the saved AIN4 pressure calibration?", parent=self):
            if not self.app.clear_pressure_calibration():
                return
            self.points.clear()
            for point in (1, 2):
                self.point_vars[point].set("Not captured")
                self.pressure_vars[point].set("")
            self.preview_var.set("No AIN4 reading captured yet.")
            self.status_var.set("Saved calibration cleared.")


class TemperatureCalibrationDialog(tk.Toplevel):
    """Configure two Type-T / LJTick-InAmp temperature channels on AIN0/AIN1."""

    CHANNELS = (("AIN0", "Temperature 1"), ("AIN1", "Temperature 2"))

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Configure Temperatures — Type T / LJTick-InAmp")
        self.resizable(False, False)
        self.configure(background="#f1f5f9")
        self._sampling_channel: str | None = None
        self._sampling_kind = ""
        self._sample_started_at = 0.0
        self.mode_var = tk.StringVar(value="nominal")
        self.cjc_var = tk.StringVar(value="22.5")
        self.reference_var = tk.StringVar(value="25.0")
        self.channel_values: dict[str, dict[str, float | None]] = {
            channel: {"offset_volts": None, "reference_voltage": None, "reference_celsius": None, "reference_cjc_celsius": None}
            for channel, _label in self.CHANNELS
        }
        self.channel_status = {channel: tk.StringVar(value="Offset: 1.2500 V nominal") for channel, _label in self.CHANNELS}
        self.capture_buttons: list[ttk.Button] = []
        self.status_var = tk.StringVar(value="Enter the measured terminal temperature, then choose a mode.")
        self._build()
        self._load_existing()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._refresh_live_controls()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Type T temperature setup", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=("AIN0 and AIN1 read amplified Type T probes through the 201× LJTick-InAmp. "
                  "Enter the temperature at the thermocouple-to-copper terminals; the T4 board sensor is not used."),
            wraplength=720, foreground="#475569",
        ).pack(anchor="w", pady=(4, 12))

        cjc = ttk.LabelFrame(outer, text="Cold-junction compensation", padding=12)
        cjc.pack(fill="x", pady=(0, 10))
        ttk.Label(cjc, text="Measured terminal temperature (°C):").grid(row=0, column=0, sticky="w")
        ttk.Entry(cjc, textvariable=self.cjc_var, width=10).grid(row=0, column=1, padx=8, sticky="w")
        self.t4_cjc_button = ttk.Button(cjc, text="Capture T4 Estimate", command=self._start_t4_cjc_capture)
        self.t4_cjc_button.grid(row=0, column=2, padx=(8, 0))
        self.capture_buttons.append(self.t4_cjc_button)
        ttk.Label(
            cjc,
            text="Uses the T4 USB air-temperature estimate as nominal CJC. A terminal measurement is more direct.",
            foreground="#64748b",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        mode = ttk.LabelFrame(outer, text="Calibration mode", padding=12)
        mode.pack(fill="x", pady=(0, 10))
        ttk.Radiobutton(mode, text="Nominal — 201× gain; 1.250 V offset unless captured below", variable=self.mode_var, value="nominal", command=self._update_mode_hint).pack(anchor="w")
        ttk.Radiobutton(mode, text="True — determine each channel gain from a known-temperature reference", variable=self.mode_var, value="true", command=self._update_mode_hint).pack(anchor="w", pady=(4, 0))
        ttk.Label(mode, text="Reference temperature (°C):", foreground="#475569").pack(side="left", pady=(8, 0))
        ttk.Entry(mode, textvariable=self.reference_var, width=10).pack(side="left", padx=8, pady=(8, 0))
        self.mode_hint = ttk.Label(mode, foreground="#64748b")
        self.mode_hint.pack(anchor="w", pady=(8, 0))

        channels = ttk.LabelFrame(outer, text="Per-channel captures", padding=12)
        channels.pack(fill="x", pady=(0, 10))
        for row, (channel, label) in enumerate(self.CHANNELS):
            ttk.Label(channels, text=f"{label} ({channel})", font=("Segoe UI", 10, "bold")).grid(row=row * 2, column=0, sticky="w", pady=(0 if row == 0 else 9, 0))
            offset_button = ttk.Button(channels, text="Capture Zero Offset", command=lambda ch=channel: self._start_capture(ch, "offset"))
            offset_button.grid(row=row * 2, column=1, padx=(12, 4), pady=(0 if row == 0 else 9, 0))
            reference_button = ttk.Button(channels, text="Capture Reference", command=lambda ch=channel: self._start_capture(ch, "reference"))
            reference_button.grid(row=row * 2, column=2, padx=4, pady=(0 if row == 0 else 9, 0))
            self.capture_buttons.extend((offset_button, reference_button))
            ttk.Label(channels, textvariable=self.channel_status[channel], foreground="#64748b", wraplength=610).grid(row=row * 2 + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(outer, textvariable=self.status_var, foreground="#1e3a8a", wraplength=720).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save Temperature Setup", style="Grease.TButton", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Clear Saved Setup", command=self.clear).pack(side="left", padx=8)
        self._update_mode_hint()

    def _update_mode_hint(self) -> None:
        if self.mode_var.get() == "true":
            self.mode_hint.configure(text="Place each probe at the entered known temperature, then capture a reference for both channels.")
        else:
            self.mode_hint.configure(text="Optional: short each amplifier input and capture its actual zero offset.")

    def _refresh_live_controls(self) -> None:
        available = self.app.labjack_available and self.app.ain2_monitor.is_reading and self._sampling_channel is None
        for button in self.capture_buttons:
            button.configure(state="normal" if available else "disabled")
        if self.winfo_exists():
            self.after(250, self._refresh_live_controls)

    @staticmethod
    def _parse_finite(variable: tk.StringVar, description: str, parent: tk.Misc) -> float | None:
        try:
            value = float(variable.get())
        except ValueError:
            messagebox.showerror("Valid value required", f"Enter a numeric {description}.", parent=parent)
            return None
        if not math.isfinite(value):
            messagebox.showerror("Valid value required", f"Enter a finite {description}.", parent=parent)
            return None
        return value

    def _start_capture(self, channel: str, kind: str) -> None:
        if self._sampling_channel is not None:
            return
        if kind == "reference":
            reference = self._parse_finite(self.reference_var, "reference temperature", self)
            cjc = self._parse_finite(self.cjc_var, "terminal temperature", self)
            if reference is None or cjc is None:
                return
            try:
                if abs(type_t_emf_millivolts(reference) - type_t_emf_millivolts(cjc)) < 0.01:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Reference must differ", "Use a known reference temperature sufficiently different from the terminal temperature.", parent=self)
                return
        if not self.app.ain2_monitor.is_reading:
            self.status_var.set(f"Waiting for the live {channel} stream…")
            return
        self._sampling_channel, self._sampling_kind = channel, kind
        self._sample_started_at = time.monotonic()
        self.status_var.set(f"Sampling {channel}… keep the input steady.")
        self.after(550, self._finish_capture)

    def _start_t4_cjc_capture(self) -> None:
        """Capture the streamed AIN14 sensor and use its USB air estimate as CJC."""
        if self._sampling_channel is not None:
            return
        if not self.app.ain2_monitor.is_reading:
            self.status_var.set("Waiting for the live T4 stream…")
            return
        self._sampling_channel, self._sampling_kind = "AIN14", "t4_cjc"
        self._sample_started_at = time.monotonic()
        self.status_var.set("Sampling the T4 internal temperature…")
        self.after(550, self._finish_capture)

    def _finish_capture(self) -> None:
        channel, kind = self._sampling_channel, self._sampling_kind
        if channel is None:
            return
        samples = (
            self.app.ain2_monitor.ain14_samples_since(self._sample_started_at, CALIBRATION_STREAM_SAMPLE_COUNT)
            if channel == "AIN14" else self.app.ain2_monitor.auxiliary_samples_since(channel, self._sample_started_at, CALIBRATION_STREAM_SAMPLE_COUNT)
        )
        if len(samples) < CALIBRATION_STREAM_SAMPLE_COUNT and time.monotonic() - self._sample_started_at < 3.0:
            self.after(60, self._finish_capture)
            return
        self._sampling_channel, self._sampling_kind = None, ""
        result: dict[str, object] = summarize_voltage_samples(samples, channel) if len(samples) >= CALIBRATION_STREAM_SAMPLE_COUNT else {"status": "error", "error": f"Live {channel} samples were not available."}
        if result.get("status") != "success":
            self.status_var.set(f"Capture not accepted: {result.get('error', 'signal was not steady.')}")
            return
        voltage = float(result["voltage"])
        if channel == "AIN14":
            device_celsius = t4_device_temperature_celsius_from_ain14(voltage)
            cjc_celsius = t4_usb_air_temperature_celsius_from_ain14(voltage)
            if not -40.0 <= cjc_celsius <= 85.0:
                self.status_var.set("T4 temperature capture was outside its normal operating range.")
                return
            self.cjc_var.set(f"{cjc_celsius:.2f}")
            self.status_var.set(f"T4 estimate applied: {cjc_celsius:.2f} °C (device {device_celsius:.2f} °C).")
            return
        values = self.channel_values[channel]
        if kind == "offset":
            values["offset_volts"] = voltage
            self.status_var.set(f"{channel} zero offset captured at {voltage:.5f} V.")
        else:
            reference = self._parse_finite(self.reference_var, "reference temperature", self)
            reference_cjc = self._parse_finite(self.cjc_var, "terminal temperature", self)
            if reference is None or reference_cjc is None:
                return
            values["reference_voltage"] = voltage
            values["reference_celsius"] = reference
            values["reference_cjc_celsius"] = reference_cjc
            self.status_var.set(f"{channel} reference captured at {voltage:.5f} V.")
        self._update_channel_status(channel)

    def _update_channel_status(self, channel: str) -> None:
        values = self.channel_values[channel]
        offset = values["offset_volts"]
        reference = values["reference_voltage"]
        text = f"Offset: {float(offset):.5f} V" if offset is not None else "Offset: 1.25000 V nominal"
        if reference is not None:
            text += f"  •  Reference: {float(reference):.5f} V at {float(values['reference_celsius']):.2f} °C"
        self.channel_status[channel].set(text)

    def _load_existing(self) -> None:
        existing = self.app.temperature_calibration
        if not existing:
            return
        self.mode_var.set(str(existing.get("mode", "nominal")))
        self.cjc_var.set(f"{float(existing['cjc_celsius']):.2f}")
        for channel, _label in self.CHANNELS:
            saved = existing["channels"][channel]
            values = self.channel_values[channel]
            values["offset_volts"] = float(saved["offset_volts"])
            values["reference_voltage"] = saved.get("reference_voltage")
            values["reference_celsius"] = saved.get("reference_celsius")
            values["reference_cjc_celsius"] = saved.get("reference_cjc_celsius", existing["cjc_celsius"])
            self._update_channel_status(channel)
        self.status_var.set("Saved temperature setup loaded.")
        self._update_mode_hint()

    def save(self) -> None:
        cjc = self._parse_finite(self.cjc_var, "terminal temperature", self)
        if cjc is None:
            return
        try:
            type_t_emf_millivolts(cjc)
        except ValueError as error:
            messagebox.showerror("Terminal temperature out of range", str(error), parent=self)
            return
        mode = self.mode_var.get()
        channels: dict[str, dict[str, float]] = {}
        for channel, _label in self.CHANNELS:
            values = self.channel_values[channel]
            offset = TEMPERATURE_NOMINAL_OFFSET_VOLTS if values["offset_volts"] is None else float(values["offset_volts"])
            gain = TEMPERATURE_NOMINAL_GAIN
            saved: dict[str, float] = {"offset_volts": offset, "gain": gain}
            if mode == "true":
                if (
                    values["reference_voltage"] is None
                    or values["reference_celsius"] is None
                    or values["reference_cjc_celsius"] is None
                ):
                    messagebox.showerror("Reference needed", f"Capture a known-temperature reference for {channel} before saving true calibration.", parent=self)
                    return
                reference_celsius = float(values["reference_celsius"])
                try:
                    reference_cjc = float(values["reference_cjc_celsius"])
                    reference_emf = type_t_emf_millivolts(reference_celsius) - type_t_emf_millivolts(reference_cjc)
                except ValueError as error:
                    messagebox.showerror("Reference out of range", str(error), parent=self)
                    return
                gain = (float(values["reference_voltage"]) - offset) * 1000.0 / reference_emf
                if not math.isfinite(gain) or not 50.0 <= gain <= 400.0:
                    messagebox.showerror("Invalid reference capture", f"{channel} produced {gain:.1f}×. Check the offset, reference temperature, and wiring.", parent=self)
                    return
                saved.update({
                    "gain": gain,
                    "reference_voltage": float(values["reference_voltage"]),
                    "reference_celsius": reference_celsius,
                    "reference_cjc_celsius": reference_cjc,
                })
            channels[channel] = saved
        calibration: dict[str, object] = {"version": 1, "thermocouple_type": "T", "amplifier": "LJTick-InAmp", "mode": mode, "cjc_celsius": cjc, "channels": channels}
        if not self.app.save_temperature_calibration(calibration):
            return
        self.status_var.set("Temperature setup saved.")

    def clear(self) -> None:
        if self.app.temperature_calibration and messagebox.askyesno("Clear temperature setup", "Clear the saved AIN0/AIN1 temperature setup?", parent=self):
            if not self.app.clear_temperature_calibration():
                return
            for channel, _label in self.CHANNELS:
                self.channel_values[channel] = {
                    "offset_volts": None, "reference_voltage": None,
                    "reference_celsius": None, "reference_cjc_celsius": None,
                }
                self._update_channel_status(channel)
            self.status_var.set("Saved temperature setup cleared.")


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
        self.current_buttons: list[ttk.Button] = []
        self.status_var = tk.StringVar(
            value="Set the two angular positions that define one mechanical cycle."
        )
        self._build()
        self.lift()
        self.focus_force()
        self._refresh_live_controls()

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
            text="Reach Reset, then Count, to record a cycle.",
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
            button = ttk.Button(form, text="Use Current", command=lambda name=target: self.use_current(name))
            button.grid(
                row=row, column=3, padx=(14, 0), pady=5
            )
            self.current_buttons.append(button)

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

    def _refresh_live_controls(self) -> None:
        available = self.app.labjack_available and self.app.bushing_position is not None and self.app.bushing_unit == "degrees"
        for button in self.current_buttons:
            button.configure(state="normal" if available else "disabled")
        if self.winfo_exists():
            self.after(250, self._refresh_live_controls)

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
        if not self.app.save_cycle_stops({"version": 1, "retracted": retracted, "extended": extended}):
            return
        self.current_var.set(self._current_position_text())
        self.status_var.set("Cycle stops saved. Reset and Count markers are shown on the graph.")

    def clear(self) -> None:
        if not self.app.cycle_stops:
            return
        if messagebox.askyesno("Clear cycle stops", "Clear the saved cycle stop positions?", parent=self):
            if not self.app.clear_cycle_stops():
                return
            self.retracted_var.set("")
            self.extended_var.set("")
            self.status_var.set("Cycle stops cleared. Cycle counting is disabled.")


class ExportDataDialog(tk.Toplevel):
    """Package selected direct-output test folders into an optional ZIP."""

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
        self.status_var = tk.StringVar(value="Select runs to export.")
        self._build()
        self.refresh_runs()
        self.lift()
        self.focus_force()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Export Test Runs", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Select one or more retained runs.",
            wraplength=710,
            foreground="#475569",
        ).pack(anchor="w", pady=(4, 10))

        runs_frame = ttk.Frame(outer)
        runs_frame.pack(fill="both", expand=True)
        ttk.Label(runs_frame, text="Test runs (newest first):").pack(anchor="w")
        list_frame = ttk.Frame(runs_frame)
        list_frame.pack(fill="both", expand=True, pady=(4, 10))
        self.runs_list = tk.Listbox(list_frame, selectmode="extended", exportselection=False, height=9)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.runs_list.yview)
        self.runs_list.configure(yscrollcommand=scrollbar.set)
        self.runs_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        ttk.Button(runs_frame, text="Refresh", command=self.refresh_runs).pack(anchor="e")

        options = ttk.LabelFrame(outer, text="Include", padding=8)
        options.pack(fill="x", pady=(12, 8))
        for row, (key, label, _filename) in enumerate(self.FILE_OPTIONS):
            ttk.Checkbutton(options, text=label, variable=self.option_vars[key]).grid(
                row=row // 2, column=row % 2, sticky="w", padx=(0, 22), pady=2
            )

        ttk.Label(outer, textvariable=self.status_var, foreground="#1e3a8a", wraplength=710).pack(
            anchor="w", pady=(0, 8)
        )
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Export Selected…", style="Grease.TButton", command=self.export).pack(side="right")

    def refresh_runs(self) -> None:
        if self.app.logger is not None:
            self.app.logger.flush()
        self.live_runs: list[tuple[Path, dict[str, object]]] = []
        for folder in self.app.available_run_folders():
            for run in reversed(CsvLogger.read_live_runs(folder)):
                self.live_runs.append((folder, run))
        self.runs_list.delete(0, "end")
        selected_index: int | None = None
        for index, (folder, run) in enumerate(self.live_runs):
            run_id = str(run.get("id", "unknown"))
            state = str(run.get("status", "unknown")).replace("_", " ")
            export_note = "exported" if run.get("exported_at") else "not exported"
            current = " • active" if self.app.logger is not None and run_id == self.app.current_live_run_id and folder == self.app.logger.folder else ""
            completed = format_local_timestamp(run.get("ended_at"), fallback="In progress")
            self.runs_list.insert(
                "end",
                f"{folder.name}  |  {run_id}  |  Completed: {completed}  |  {state}, {export_note}{current}",
            )
            if self.app.logger is not None and run_id == self.preselect_run_id and folder == self.app.logger.folder:
                selected_index = index
        if self.live_runs:
            self.runs_list.selection_set(0 if selected_index is None else selected_index)
            self.status_var.set(f"{len(self.live_runs)} test run(s) found. Select the data to package.")
        else:
            self.status_var.set("No saved test rounds found in the current run output folder.")

    def export(self) -> None:
        selected_indexes = self.runs_list.curselection()
        selected_options = [option for option in self.FILE_OPTIONS if self.option_vars[option[0]].get()]
        if not selected_indexes:
            messagebox.showerror("Select a test run", "Select at least one test run to export.", parent=self)
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

        if self.app.logger is not None:
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
        self.status_var.set(f"Exported {count} file(s) from {len(selected_runs)} test run(s).")
        messagebox.showinfo(
            "Export complete",
            f"Created ZIP archive with {count} file(s) from {len(selected_runs)} test run(s).\n\n{destination}",
            parent=self,
        )
        self.refresh_runs()


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


class GraphReviewDialog(tk.Toplevel):
    """Expanded, synchronized view of the position and velocity charts."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Position and Velocity Review")
        self.geometry("1120x780")
        self.minsize(760, 560)
        self.configure(background="#f1f5f9")
        self._refresh_after_id: str | None = None

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="Position and velocity review", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(toolbar, textvariable=app.graph_window_var, foreground="#64748b").pack(side="left", padx=(12, 0))

        self.pan_left_button = ttk.Button(
            toolbar, text="◀", width=3, style="Icon.TButton", command=lambda: self._pan(SNAPSHOT_PAN_STEP_SECONDS)
        )
        self.pan_right_button = ttk.Button(
            toolbar, text="▶", width=3, style="Icon.TButton", command=lambda: self._pan(-SNAPSHOT_PAN_STEP_SECONDS)
        )
        self.pan_right_button.pack(side="right")
        self.pan_left_button.pack(side="right", padx=(6, 0))
        self.pause_button = ttk.Button(toolbar, command=self._toggle_pause)
        self.pause_button.pack(side="right", padx=(0, 6))
        self.save_button = ttk.Button(toolbar, text="Save Snapshot…", command=self.app.save_graph_snapshot)
        self.save_button.pack(side="right", padx=(0, 6))

        charts = ttk.Frame(outer)
        charts.pack(fill="both", expand=True)
        charts.columnconfigure(0, weight=1)
        charts.rowconfigure(0, weight=1)
        charts.rowconfigure(1, weight=1)
        self.position_plot = LivePlot(charts, "Bushing position", 1.0, "#0891b2")
        self.position_plot.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self.velocity_plot = LivePlot(charts, "Bushing velocity", 1.0, "#2563eb", minimum=-1.0)
        self.velocity_plot.grid(row=1, column=0, sticky="nsew")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    def _pan(self, seconds: float) -> None:
        self.app.pan_paused_graphs(seconds)

    def _toggle_pause(self) -> None:
        self.app.toggle_graph_pause()

    def _refresh(self) -> None:
        """Mirror the dashboard plots while this expanded window is open."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.position_plot.load_snapshot_state(self.app.bushing_plot.snapshot_state())
        self.velocity_plot.load_snapshot_state(self.app.velocity_plot.snapshot_state())
        self.pause_button.configure(text="Resume" if self.app.graphs_paused else "Pause")
        self.save_button.configure(state="normal" if self.app.graphs_paused else "disabled")
        maximum = self.app._maximum_graph_pan_seconds()
        self.pan_left_button.configure(
            state="normal" if self.app.graphs_paused and self.app.graph_pan_offset_seconds < maximum - 0.05 else "disabled"
        )
        self.pan_right_button.configure(
            state="normal" if self.app.graphs_paused and self.app.graph_pan_offset_seconds > 0.05 else "disabled"
        )
        self._refresh_after_id = self.after(100, self._refresh)

    def _close(self) -> None:
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self.app.graph_review_window = None
        self.destroy()


class TemperatureHistoryDialog(tk.Toplevel):
    """One temperature channel plotted across the active test duration."""

    def __init__(self, app: "TestStandApp", sensor_number: int) -> None:
        super().__init__(app)
        self.app = app
        self.sensor_number = sensor_number
        self.title(f"Temperature {sensor_number} History")
        self.geometry("980x580")
        self.minsize(700, 420)
        self.configure(background="#f1f5f9")
        self._refresh_after_id: str | None = None

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text=f"Temperature {sensor_number} history", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(header, textvariable=self.status_var, foreground="#64748b").pack(side="left", padx=12)
        self.plot = LivePlot(outer, f"Temperature {sensor_number} (°F)", 100.0, "#dc2626", minimum=0.0)
        self.plot.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    def _refresh(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        samples = tuple(self.app.temperature_history[self.sensor_number])
        duration = max(60.0, self.app.test_elapsed_seconds)
        if samples:
            values = tuple(value for _elapsed, value in samples)
            padding = max(1.0, (max(values) - min(values)) * 0.1)
            self.plot.set_scale(
                f"Temperature {self.sensor_number} (°F)", min(values) - padding, max(values) + padding, clear_samples=False
            )
            self.status_var.set(f"{len(samples):,} readings  •  Test elapsed {self.app._format_test_time(self.app.test_elapsed_seconds)}")
        else:
            self.plot.set_scale(f"Temperature {self.sensor_number} (°F)", 0.0, 100.0, clear_samples=False)
            self.status_var.set("No temperature data yet." if self.app.temperature_calibration else "Temperature input not configured.")
        self.plot.history_seconds = duration
        self.plot.show_window(
            samples,
            window_end_monotonic=duration,
            left_time_label="Test start",
            right_time_label=self.app._format_test_time(self.app.test_elapsed_seconds),
        )
        self._refresh_after_id = self.after(1000, self._refresh)

    def _close(self) -> None:
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self.app.temperature_history_windows[self.sensor_number] = None
        self.destroy()


class CycleFrequencyHistoryDialog(tk.Toplevel):
    """A one-second CPM trend across the full active test duration."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Cycle Frequency History")
        self.geometry("980x580")
        self.minsize(700, 420)
        self.configure(background="#f1f5f9")
        self._refresh_after_id: str | None = None

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Cycle frequency history", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(header, textvariable=self.status_var, foreground="#64748b").pack(side="left", padx=12)
        self.plot = LivePlot(outer, "Cycle frequency (cpm)", 1.0, "#7c3aed", minimum=0.0)
        self.plot.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    def _refresh(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        samples = tuple(self.app.cycle_frequency_history)
        duration = max(60.0, self.app.test_elapsed_seconds)
        maximum = max((value for _elapsed, value in samples), default=0.0)
        self.plot.set_scale("Cycle frequency (cpm)", 0.0, max(1.0, maximum * 1.15), clear_samples=False)
        if samples:
            self.status_var.set(f"{len(samples):,} one-second readings  •  Test elapsed {self.app._format_test_time(self.app.test_elapsed_seconds)}")
        else:
            self.status_var.set("Start cycle counting to build the CPM history.")
        self.plot.history_seconds = duration
        self.plot.show_window(
            samples,
            window_end_monotonic=duration,
            left_time_label="Test start",
            right_time_label=self.app._format_test_time(self.app.test_elapsed_seconds),
        )
        self._refresh_after_id = self.after(1000, self._refresh)

    def _close(self) -> None:
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self.app.cycle_frequency_history_window = None
        self.destroy()


class RawAuxiliaryInputsDialog(tk.Toplevel):
    """Small read-only aid for identifying AIN0/AIN1 sensor wiring."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Raw Auxiliary Inputs")
        self.geometry("510x300")
        self.minsize(440, 260)
        self.configure(background="#f1f5f9")
        self._refresh_after_id: str | None = None
        self.value_vars = {channel: tk.StringVar(value="— V") for channel in ("AIN0", "AIN1")}
        self.status_var = tk.StringVar(value="Waiting for the LabJack stream.")

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Raw auxiliary inputs", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Live AIN0/AIN1 voltage. Save 60 seconds for stream diagnostics.",
            foreground="#475569",
            wraplength=465,
            justify="left",
        ).pack(anchor="w", pady=(5, 14))

        values = ttk.Frame(outer)
        values.pack(fill="x")
        for column, channel in enumerate(("AIN0", "AIN1")):
            values.columnconfigure(column, weight=1, uniform="raw_aux")
            card = ttk.LabelFrame(values, text=channel, padding=12)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0))
            ttk.Label(card, text="Raw voltage", foreground="#64748b").pack(anchor="w")
            ttk.Label(card, textvariable=self.value_vars[channel], font=("Segoe UI", 20, "bold")).pack(anchor="w", pady=(5, 0))

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(16, 0))
        ttk.Label(footer, textvariable=self.status_var, foreground="#64748b", wraplength=290, justify="left").pack(side="left")
        self.save_button = ttk.Button(
            footer,
            text="Save 60-Second Capture…",
            command=self.app.save_raw_auxiliary_capture,
            state="disabled",
        )
        self.save_button.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    def _refresh(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        if not self.app.labjack_available:
            for variable in self.value_vars.values():
                variable.set("— V")
            self.save_button.configure(state="disabled")
            self.status_var.set("Waiting for the connected LabJack T4 stream.")
        else:
            updated = False
            for channel, variable in self.value_vars.items():
                value = self.app.auxiliary_raw_voltages.get(channel)
                if value is None:
                    variable.set("— V")
                else:
                    variable.set(f"{value:.5f} V")
                    updated = True
            history_seconds = self.app.raw_auxiliary_history_seconds()
            if history_seconds >= RAW_AUXILIARY_CAPTURE_SECONDS:
                self.save_button.configure(state="normal")
                self.status_var.set("60 seconds ready • 200 samples/s per channel • raw diagnostic CSV only")
            else:
                self.save_button.configure(state="disabled")
                self.status_var.set(
                    f"Collecting raw capture: {history_seconds:.1f} / {RAW_AUXILIARY_CAPTURE_SECONDS} seconds"
                    if updated else "AIN0/AIN1 samples have not arrived yet."
                )
        self._refresh_after_id = self.after(100, self._refresh)

    def _close(self) -> None:
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self.app.raw_auxiliary_inputs_window = None
        self.destroy()


class Ps12dcSolenoidDialog(tk.Toplevel):
    """Manual and repeating-timer controls for the PS12DC S0 solenoid output."""

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("PS12DC S0 Solenoid Control")
        self.geometry("540x400")
        self.minsize(490, 365)
        self.configure(background="#f1f5f9")
        self._refresh_after_id: str | None = None
        self._pending: bool | None = None
        self._timer_active = False
        self._timer_output_on = False
        self._next_transition_monotonic: float | None = None
        self.state_var = tk.StringVar(value="Checking output state…")
        self.on_seconds_var = tk.StringVar(value="0.50")
        self.off_seconds_var = tk.StringVar(value="2.00")

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="24 VDC solenoid — PS12DC S0", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="S0: T4 EIO0 / DIO8. It is set OFF on connect and shutdown.",
            foreground="#475569",
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(5, 14))

        output_frame = ttk.LabelFrame(outer, text="Manual control", padding=10)
        output_frame.pack(fill="x")
        output_frame.columnconfigure(0, weight=1)
        ttk.Label(output_frame, textvariable=self.state_var, foreground="#475569").grid(row=0, column=0, sticky="w")
        self.manual_button = ttk.Button(output_frame, command=self._toggle_manual)
        self.manual_button.grid(row=0, column=1, sticky="e")

        timer_frame = ttk.LabelFrame(outer, text="Automatic pulse timer", padding=10)
        timer_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(timer_frame, text="ON (seconds):").grid(row=0, column=0, sticky="w")
        self.on_entry = ttk.Entry(timer_frame, textvariable=self.on_seconds_var, width=9)
        self.on_entry.grid(row=0, column=1, sticky="w", padx=(7, 16))
        ttk.Label(timer_frame, text="OFF (seconds):").grid(row=0, column=2, sticky="w")
        self.off_entry = ttk.Entry(timer_frame, textvariable=self.off_seconds_var, width=9)
        self.off_entry.grid(row=0, column=3, sticky="w", padx=(7, 12))
        self.timer_button = ttk.Button(timer_frame, text="Start Timer", command=self._toggle_timer)
        self.timer_button.grid(row=0, column=4, sticky="e")
        ttk.Label(
            timer_frame,
            text="Alternates ON and OFF. Minimum interval: 0.10 s.",
            foreground="#64748b",
            wraplength=485,
            justify="left",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(9, 0))

        self.status_var = tk.StringVar(value="Manual mode ready.")
        ttk.Label(outer, textvariable=self.status_var, foreground="#64748b", wraplength=500, justify="left").pack(
            anchor="w", pady=(8, 0)
        )
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    @staticmethod
    def _duration(variable: tk.StringVar, label: str) -> float:
        try:
            value = float(variable.get())
        except ValueError as error:
            raise ValueError(f"Enter a valid {label} duration.") from error
        if not math.isfinite(value) or value < 0.10:
            raise ValueError(f"{label} duration must be at least 0.10 seconds.")
        return value

    def _request_s0(self, enabled: bool, commanded_pulse_seconds: float | None = None) -> bool:
        if not self.app.request_ps12dc_s0(enabled, commanded_pulse_seconds=commanded_pulse_seconds):
            return False
        self._pending = enabled
        return True

    def _toggle_manual(self) -> None:
        state = self.app.ain2_monitor.ps12dc_s0_state()
        if state is None:
            self.status_var.set("The T4 has not confirmed this output state yet.")
            return
        requested = not state
        if self._request_s0(requested):
            self.status_var.set(f"Requesting S0 {'ON' if requested else 'OFF'}…")
        else:
            self.status_var.set("The LabJack is not available. Reconnect it before changing an output.")

    def _toggle_timer(self) -> None:
        if self._timer_active:
            self._stop_timer("Timer stopped; S0 commanded OFF.")
            return
        if not self.app.is_running:
            self.status_var.set("Start cycle counting before starting the automatic pulse timer.")
            return
        try:
            on_seconds = self._duration(self.on_seconds_var, "ON")
            self._duration(self.off_seconds_var, "OFF")
        except ValueError as error:
            messagebox.showerror("Invalid timer duration", str(error), parent=self)
            return
        if self.app.ain2_monitor.ps12dc_s0_state() is not False:
            self.status_var.set("Wait for the T4 to confirm S0 is OFF before starting the timer.")
            return
        self._timer_active = True
        self._timer_output_on = True
        self._next_transition_monotonic = time.monotonic() + on_seconds
        if self._request_s0(True, on_seconds):
            self.status_var.set(f"Timer running: ON for {on_seconds:.2f} s.")
        else:
            self._timer_active = False
            self._next_transition_monotonic = None
            self.status_var.set("Timer could not start because the LabJack is unavailable.")

    def _stop_timer(self, status: str) -> None:
        was_active = self._timer_active
        self._timer_active = False
        self._timer_output_on = False
        self._next_transition_monotonic = None
        if was_active:
            self._request_s0(False)
        self.status_var.set(status)

    def stop_for_test_pause(self) -> None:
        """Make Pause a safe boundary: halt pulses and command the output low."""
        self._timer_active = False
        self._timer_output_on = False
        self._next_transition_monotonic = None
        self._request_s0(False)
        self.status_var.set("Test paused; timer stopped and S0 commanded OFF.")

    def _advance_timer(self, now: float) -> None:
        if not self._timer_active:
            return
        if not self.app.is_running:
            self._stop_timer("Timer stopped because the test is paused.")
            return
        if not self.app.labjack_available or not self.app.ain2_monitor.is_reading:
            self._stop_timer("Timer stopped because the LabJack connection is unavailable.")
            return
        if self._next_transition_monotonic is None or now < self._next_transition_monotonic:
            return
        try:
            on_seconds = self._duration(self.on_seconds_var, "ON")
            off_seconds = self._duration(self.off_seconds_var, "OFF")
        except ValueError:
            self._stop_timer("Timer stopped because a duration is invalid.")
            return
        # Advance from the scheduled transition time rather than from the UI
        # refresh instant, so ordinary Tk scheduling jitter does not accumulate.
        while self._next_transition_monotonic is not None and now >= self._next_transition_monotonic:
            self._timer_output_on = not self._timer_output_on
            interval = on_seconds if self._timer_output_on else off_seconds
            self._next_transition_monotonic += interval
        pulse_seconds = on_seconds if self._timer_output_on else None
        if self._request_s0(self._timer_output_on, pulse_seconds):
            state = "ON" if self._timer_output_on else "OFF"
            duration = on_seconds if self._timer_output_on else off_seconds
            self.status_var.set(f"Timer running: {state} for {duration:.2f} s.")
        else:
            self._stop_timer("Timer stopped because the output command was unavailable.")

    def _update_controls(self) -> None:
        available = self.app.labjack_available and self.app.ain2_monitor.is_reading
        state = self.app.ain2_monitor.ps12dc_s0_state()
        if self._pending is not None and state == self._pending:
            self._pending = None
        if self._pending is not None:
            self.state_var.set(f"{'ON' if self._pending else 'OFF'} requested")
            self.manual_button.configure(text="Updating…", state="disabled")
        elif not available or state is None:
            self.state_var.set("Unavailable — connect the LabJack T4")
            self.manual_button.configure(text="Unavailable", state="disabled")
        elif state:
            self.state_var.set("ON — output high")
            self.manual_button.configure(text="Turn Off", style="Stop.TButton", state="disabled" if self._timer_active else "normal")
        else:
            self.state_var.set("OFF — output low")
            self.manual_button.configure(text="Turn On", style="Start.TButton", state="disabled" if self._timer_active else "normal")
        entry_state = "disabled" if self._timer_active else "normal"
        self.on_entry.configure(state=entry_state)
        self.off_entry.configure(state=entry_state)
        self.timer_button.configure(
            text="Stop Timer" if self._timer_active else "Start Timer",
            style="Stop.TButton" if self._timer_active else "Start.TButton",
            state="normal" if available and state is not None else "disabled",
        )

    def _refresh(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._advance_timer(time.monotonic())
        self._update_controls()
        self._refresh_after_id = self.after(100, self._refresh)

    def _close(self) -> None:
        self._stop_timer("Timer stopped; window closed.")
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self.app.ps12dc_solenoid_window = None
        self.destroy()


class TestStandApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Senior Design Test Stand")
        self.minsize(1120, 720)
        self.geometry("1360x860")
        # Start maximized on Windows while keeping the standard title bar and
        # window controls available for operators who need to restore it.
        self.state("zoomed")
        self.configure(background="#f1f5f9")

        self.paths = AppPaths.create(Path(__file__).resolve().parent)
        self.logger: CsvLogger | None = None
        self.run_output_config = self.load_run_output_config()
        self.active_run_output_root: Path | None = None
        self.is_running = False
        self.current_live_run_id: str | None = None
        self._closing = False
        self._last_runtime_error = ""
        self.current_live_run_cycle_start = 0
        self.last_tick = time.monotonic()
        self.test_elapsed_seconds = 0.0
        self.cycles = 0
        self.cycle_state = "waiting_for_retracted"
        self.cycle_times: deque[float] = deque()
        self._cycle_capture: CycleCapture | None = None
        self._recent_cycle_summaries: deque[dict[str, float | int | str]] = deque(
            maxlen=GREASE_COMPARISON_CYCLE_COUNT
        )
        self._pending_grease_events: list[GreaseEventCapture] = []
        self._grease_event_sequence = 0
        self._pending_s0_requests: deque[tuple[bool, float | None]] = deque()
        self.position_calibration = self.load_position_calibration()
        self.pressure_calibration = self.load_pressure_calibration()
        self.temperature_calibration = self.load_temperature_calibration()
        self.cycle_stops = self.load_cycle_stops()
        self.ain2_monitor = LabJackAin2Monitor()
        self.labjack_available = False
        self.next_ain2_poll = 0.0
        self.last_labjack_signature = ""
        self.ain2_voltage: float | None = None
        self.ain4_voltage: float | None = None
        self.bushing_position: float | None = None
        self.bushing_velocity: float | None = None
        self.auxiliary_raw_voltages: dict[str, float | None] = {"AIN0": None, "AIN1": None}
        self._raw_auxiliary_samples: deque[AuxiliaryRawSample] = deque(maxlen=RAW_AUXILIARY_HISTORY_MAX_SAMPLES)
        self._last_auxiliary_capture_monotonic: float | None = None
        self._last_auxiliary_capture_at: datetime | None = None
        self.grease_pressure: float | None = None
        self._display_pressure_voltage_samples: deque[float] = deque(maxlen=5)
        self.temperature_history: dict[int, list[tuple[float, float]]] = {1: [], 2: []}
        self.current_temperatures: dict[int, float | None] = {1: None, 2: None}
        self.temperature_history_windows: dict[int, TemperatureHistoryDialog | None] = {1: None, 2: None}
        self.cycle_frequency_history: list[tuple[float, float]] = []
        self.cycle_frequency_history_window: CycleFrequencyHistoryDialog | None = None
        self.bushing_unit = "degrees" if self.position_calibration else "volts"
        self._display_voltage_samples: deque[float] = deque(maxlen=5)
        self._velocity_angles: deque[float] = deque(maxlen=VELOCITY_SLOPE_WINDOW_SAMPLES)
        self._snapshot_samples: deque[PositionVelocitySnapshotSample] = deque(maxlen=SNAPSHOT_HISTORY_MAX_SAMPLES)
        self._snapshot_stream_gap_pending = False
        self._latest_snapshot_sample_monotonic: float | None = None
        self._latest_display_sample_monotonic: float | None = None
        self._last_ain2_sample_monotonic: float | None = None
        self.graphs_paused = False
        self._frozen_snapshot_samples: tuple[PositionVelocitySnapshotSample, ...] | None = None
        self._frozen_snapshot_window_samples: tuple[PositionVelocitySnapshotSample, ...] | None = None
        self._frozen_live_position_samples: tuple[tuple[float, float], ...] | None = None
        self._frozen_live_velocity_samples: tuple[tuple[float, float], ...] | None = None
        self._frozen_position_graph: dict[str, object] | None = None
        self._frozen_velocity_graph: dict[str, object] | None = None
        self.graph_pan_offset_seconds = 0.0
        self.graph_window_var = tk.StringVar(value=f"Live: latest {SNAPSHOT_HISTORY_SECONDS:g} seconds")
        self.bushing_detail_var = tk.StringVar(value="Waiting for AIN2")
        self.pressure_detail_var = tk.StringVar(
            value="Input not configured" if not self.pressure_calibration else "Waiting for AIN4"
        )
        self.velocity_detail_var = tk.StringVar(value="Calibrate AIN2 to calculate velocity")
        self.temperature_detail_vars = {
            1: tk.StringVar(value="Input not configured" if not self.temperature_calibration else "Waiting for AIN0"),
            2: tk.StringVar(value="Input not configured" if not self.temperature_calibration else "Waiting for AIN1"),
        }
        self.event_terminal: EventTerminalDialog | None = None
        self.graph_review_window: GraphReviewDialog | None = None
        self.raw_auxiliary_inputs_window: RawAuxiliaryInputsDialog | None = None
        self.pressure_calibration_window: PressureCalibrationDialog | None = None
        self.temperature_calibration_window: TemperatureCalibrationDialog | None = None
        self.ps12dc_solenoid_window: Ps12dcSolenoidDialog | None = None

        self._build_style()
        self._build_menu()
        self._build_interface()
        self._set_labjack_dependent_controls(False)
        self.log_event("Application started", f"Run output: {self.run_output_root()}")
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
        style.configure("MetricHeader.TFrame", background="#ffffff", relief="flat", borderwidth=0)
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
        file_menu.add_command(label="Set Run Output Folder…", command=self.choose_run_output_folder)
        file_menu.add_command(label="Use Default Local Run Folder", command=self.use_default_run_output_folder)
        file_menu.add_command(label="Open Run Output Folder", command=self.open_run_output_folder)
        menu_bar.add_cascade(label="File", menu=file_menu)
        tools_menu = tk.Menu(menu_bar, tearoff=False)
        tools_menu.add_command(label="Calibrate AIN2 Position…", command=self.open_calibration_window)
        tools_menu.add_command(label="Set Cycle Stops…", command=self.open_cycle_stops_window)
        tools_menu.add_command(label="Calibrate FIO4 Pressure…", command=self.open_pressure_calibration_window)
        tools_menu.add_command(label="Configure Temperatures…", command=self.open_temperature_calibration_window)
        tools_menu.add_separator()
        tools_menu.add_command(label="Read Raw AIN0 / AIN1…", command=self.open_raw_auxiliary_inputs_window)
        tools_menu.add_command(label="PS12DC Solenoid Control…", command=self.open_ps12dc_solenoid_window)
        menu_bar.add_cascade(label="Tools", menu=tools_menu)
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
        ttk.Label(outer, text="Getting Started", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Install the LabJack driver to use live inputs.",
            wraplength=600,
            foreground="#475569",
        ).pack(anchor="w", pady=(6, 18))

        ttk.Label(outer, text="To use the physical LabJack T4", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "1. Install the official LabJack LJM Windows driver/library.\n"
                "2. Connect the T4 by USB.\n"
                "3. Confirm the header shows “LabJack T4 • Connected • USB”.\n"
                "4. If needed, run TestStandLabJackDiagnostic.exe."
            ),
            justify="left",
            wraplength=600,
        ).pack(anchor="w", pady=(6, 16))

        ttk.Label(outer, text="Data", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Export runs from File → Export Runs…", justify="left", wraplength=600).pack(
            anchor="w", pady=(6, 18)
        )

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
        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 12))
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        title = ttk.Frame(header, style="Header.TFrame")
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(title, text="Senior Design Test Stand", style="Header.TLabel").pack(anchor="w")
        self.header_detail_var = tk.StringVar(value="AIN2 position  •  AIN4 pressure  •  Cycle counting")
        ttk.Label(title, textvariable=self.header_detail_var, style="Subheader.TLabel").pack(anchor="w", pady=(3, 0))
        self.labjack_status_var = tk.StringVar(value="LabJack T4 • Checking connection")
        self.labjack_status_label = ttk.Label(
            header, textvariable=self.labjack_status_var, style="Subheader.TLabel", font=("Segoe UI", 10, "bold")
        )
        self.labjack_status_label.grid(row=0, column=1, sticky="e")

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=4, minsize=700)
        main.columnconfigure(1, weight=2, minsize=330)
        main.rowconfigure(1, weight=1)

        self.metrics = ttk.Frame(main)
        self.metrics.grid(row=0, column=0, columnspan=2, sticky="ew")
        for column in range(5):
            self.metrics.columnconfigure(column, weight=1, uniform="metrics")

        self.metric_vars: dict[str, tk.StringVar] = {}
        self._metric(self.metrics, 0, 0, "Completed Cycles", "cycles", "0", None)
        self._metric(
            self.metrics, 0, 1, "Cycle Frequency", "frequency", "0.0 cpm", None,
            expand_command=self.open_cycle_frequency_history_window,
        )
        self._metric(self.metrics, 0, 2, "Test Time", "test_time", "00:00:00", None)
        self._metric(self.metrics, 0, 3, "Bushing Position", "position", "Waiting…", self.bushing_detail_var)
        self._metric(self.metrics, 0, 4, "Bushing Velocity", "velocity", "— °/s", None)

        self.chart_holder = ttk.LabelFrame(main, text="Live Motion", padding=10)
        self.chart_holder.grid(row=1, column=0, sticky="nsew", padx=(0, 10), pady=(14, 0))
        self.chart_holder.columnconfigure(0, weight=1)
        self.chart_holder.columnconfigure(1, weight=1)
        self.chart_holder.rowconfigure(1, weight=1)
        self.chart_holder.rowconfigure(2, weight=1)
        chart_toolbar = ttk.Frame(self.chart_holder)
        chart_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(chart_toolbar, textvariable=self.graph_window_var, foreground="#64748b").pack(side="left")
        self.pan_left_graph_button = ttk.Button(
            chart_toolbar, text="◀", width=3, style="Icon.TButton", command=lambda: self.pan_paused_graphs(SNAPSHOT_PAN_STEP_SECONDS)
        )
        self.pan_left_graph_button.pack(side="left", padx=(12, 3))
        self.pan_right_graph_button = ttk.Button(
            chart_toolbar, text="▶", width=3, style="Icon.TButton", command=lambda: self.pan_paused_graphs(-SNAPSHOT_PAN_STEP_SECONDS)
        )
        self.pan_right_graph_button.pack(side="left")
        self.pause_graphs_button = ttk.Button(chart_toolbar, text="Pause", command=self.toggle_graph_pause)
        self.pause_graphs_button.pack(side="right")
        self.save_snapshot_button = ttk.Button(
            chart_toolbar, text="Save Snapshot…", command=self.save_graph_snapshot, state="disabled"
        )
        self.save_snapshot_button.pack(side="right", padx=(0, 6))
        ttk.Button(
            chart_toolbar, text="⛶", width=3, style="Icon.TButton", command=self.open_graph_review_window,
        ).pack(side="right", padx=(0, 6))
        self.bushing_plot = LivePlot(self.chart_holder, "Live bushing position (degrees, calibrated AIN2)", 5.0, "#0891b2")
        self.bushing_plot.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(0, 6))
        self.configure_bushing_plot(clear_samples=False)
        self.velocity_plot = LivePlot(
            self.chart_holder,
            "Live bushing velocity (°/s, calibrated AIN2)",
            DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
            "#2563eb",
            minimum=-DEFAULT_VELOCITY_GRAPH_LIMIT_DPS,
        )
        self.velocity_plot.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(0, 6))
        self.configure_velocity_plot(clear_samples=False)
        self.pressure_plot = LivePlot(
            self.chart_holder,
            "Grease pressure (psi)",
            5000.0,
            "#d97706",
        )
        self.pressure_plot.grid(row=2, column=0, sticky="nsew", padx=(0, 5))
        self.configure_pressure_plot(clear_samples=False)
        self._update_graph_pan_controls()

        status_box = ttk.LabelFrame(self.chart_holder, text="Recent Events", padding=10)
        status_box.grid(row=2, column=1, sticky="nsew", padx=(5, 0))
        event_toolbar = ttk.Frame(status_box)
        event_toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(event_toolbar, text="Activity", foreground="#475569").pack(side="left")
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

        sidebar = ttk.Frame(main)
        sidebar.grid(row=1, column=1, sticky="nsew", pady=(14, 0))
        sidebar.columnconfigure(0, weight=1)

        controls = ttk.LabelFrame(sidebar, text="Test Controls", padding=12)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)

        count_buttons = ttk.Frame(controls)
        count_buttons.grid(row=0, column=0, sticky="ew")
        self.start_button = ttk.Button(
            count_buttons, text="Start", style="Start.TButton", command=self.start_counting, state="disabled"
        )
        self.start_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(
            count_buttons, text="Pause", style="Stop.TButton", command=self.pause_counting, state="disabled"
        )
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.reset_button = ttk.Button(count_buttons, text="Save & Reset", command=self.reset_counting)
        self.reset_button.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.setup_status = ttk.LabelFrame(sidebar, text="Setup Status", padding=10)
        self.setup_status.grid(row=1, column=0, sticky="ew", pady=(10, 10))
        self._build_setup_status()

        auxiliary = ttk.LabelFrame(sidebar, text="Auxiliary Inputs", padding=8)
        auxiliary.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        auxiliary.columnconfigure(0, weight=1)
        auxiliary.columnconfigure(1, weight=1)
        self._metric(
            auxiliary, 0, 0, "Grease Pressure", "pressure", "— psi", self.pressure_detail_var, columnspan=2,
        )
        self._metric(
            auxiliary, 1, 0, "Temperature 1", "temp_1", "— °F", self.temperature_detail_vars[1],
            expand_command=lambda: self.open_temperature_history_window(1),
        )
        self._metric(
            auxiliary, 1, 1, "Temperature 2", "temp_2", "— °F", self.temperature_detail_vars[2],
            expand_command=lambda: self.open_temperature_history_window(2),
        )

        self._refresh_setup_status()

    def _build_setup_status(self) -> None:
        """Keep setup readiness visible without duplicating setup controls on the dashboard."""
        self.setup_status_vars: dict[str, tk.StringVar] = {}
        self.setup_status_labels: dict[str, ttk.Label] = {}
        rows = (
            ("labjack", "LabJack T4"),
            ("position", "AIN2 position"),
            ("stops", "Cycle stops"),
            ("pressure", "AIN4 pressure"),
            ("temperature", "Temperature inputs"),
        )
        for row, (key, label) in enumerate(rows):
            ttk.Label(self.setup_status, text=label).grid(row=row, column=0, sticky="w", pady=2)
            value = tk.StringVar()
            status = ttk.Label(self.setup_status, textvariable=value, foreground="#64748b")
            status.grid(row=row, column=1, sticky="e", padx=(18, 0), pady=2)
            self.setup_status_vars[key] = value
            self.setup_status_labels[key] = status
        self.setup_status.columnconfigure(1, weight=1)

    def _set_setup_status(self, key: str, value: str, color: str) -> None:
        self.setup_status_vars[key].set(value)
        self.setup_status_labels[key].configure(foreground=color)

    def _refresh_setup_status(self) -> None:
        if not hasattr(self, "setup_status_vars"):
            return
        self._set_setup_status("labjack", "Connected" if self.labjack_available else "Not connected", "#166534" if self.labjack_available else "#b45309")
        self._set_setup_status("position", "Saved" if self.position_calibration else "Required", "#166534" if self.position_calibration else "#b45309")
        if not self.position_calibration:
            self._set_setup_status("stops", "Needs position", "#64748b")
        else:
            self._set_setup_status("stops", "Saved" if self.cycle_stops else "Required", "#166534" if self.cycle_stops else "#b45309")
        self._set_setup_status("pressure", "Saved" if self.pressure_calibration else "Required", "#166534" if self.pressure_calibration else "#b45309")
        if self.temperature_calibration:
            mode = str(self.temperature_calibration.get("mode", "nominal")).capitalize()
            self._set_setup_status("temperature", mode, "#166534")
        else:
            self._set_setup_status("temperature", "Not configured", "#64748b")

    def _metric(
        self,
        parent: ttk.Frame,
        row: int,
        column: int,
        label: str,
        key: str,
        initial: str,
        detail: str | tk.StringVar | None,
        *,
        expand_command: Callable[[], None] | None = None,
        columnspan: int = 1,
    ) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Metric.TFrame", padding=(12, 10))
        frame.grid(
            row=row, column=column, columnspan=columnspan, sticky="nsew",
            padx=(0 if column == 0 else 5, 0), pady=(0 if row == 0 else 5, 0),
        )
        heading = ttk.Frame(frame, style="MetricHeader.TFrame")
        heading.pack(fill="x")
        ttk.Label(heading, text=label, style="MetricName.TLabel").pack(side="left")
        if expand_command is not None:
            ttk.Button(heading, text="⛶", width=3, style="Icon.TButton", command=expand_command).pack(side="right")
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
        if self.logger is not None:
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

    def open_graph_review_window(self) -> None:
        """Open one synchronized expanded position/velocity chart view."""
        if self.graph_review_window is not None and self.graph_review_window.winfo_exists():
            self.graph_review_window.deiconify()
            self.graph_review_window.lift()
            self.graph_review_window.focus_set()
            return
        self.graph_review_window = GraphReviewDialog(self)

    def open_temperature_history_window(self, sensor_number: int) -> None:
        """Open the full-test trend for one temperature input."""
        existing = self.temperature_history_windows[sensor_number]
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        self.temperature_history_windows[sensor_number] = TemperatureHistoryDialog(self, sensor_number)

    def open_cycle_frequency_history_window(self) -> None:
        """Open the full-test CPM trend."""
        existing = self.cycle_frequency_history_window
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        self.cycle_frequency_history_window = CycleFrequencyHistoryDialog(self)

    def open_raw_auxiliary_inputs_window(self) -> None:
        """Show unscaled AIN0/AIN1 values without opening another T4 connection."""
        existing = self.raw_auxiliary_inputs_window
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        self.raw_auxiliary_inputs_window = RawAuxiliaryInputsDialog(self)

    def open_pressure_calibration_window(self) -> None:
        """Open the FIO4/AIN4 gauge-calibration utility without another T4 handle."""
        existing = self.pressure_calibration_window
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        self.pressure_calibration_window = PressureCalibrationDialog(self)

    def open_temperature_calibration_window(self) -> None:
        """Open the saved Type-T setup without creating a second T4 connection."""
        existing = self.temperature_calibration_window
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        self.temperature_calibration_window = TemperatureCalibrationDialog(self)

    def open_ps12dc_solenoid_window(self) -> None:
        """Open manual/timed controls for PS12DC S0 without a second T4 handle."""
        existing = self.ps12dc_solenoid_window
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        self.ps12dc_solenoid_window = Ps12dcSolenoidDialog(self)

    def request_ps12dc_s0(self, enabled: bool, *, commanded_pulse_seconds: float | None = None) -> bool:
        """Queue an operator-requested state change for PS12DC S0 only."""
        if not self.labjack_available or not self.ain2_monitor.set_ps12dc_s0(enabled):
            return False
        pulse = None
        if enabled and commanded_pulse_seconds is not None:
            try:
                candidate = float(commanded_pulse_seconds)
                if math.isfinite(candidate) and candidate > 0:
                    pulse = candidate
            except (TypeError, ValueError):
                pass
        self._pending_s0_requests.append((enabled, pulse))
        return True

    def stop_ps12dc_for_test_pause(self) -> None:
        """Stop automatic greasing whenever the associated test is paused or saved."""
        dialog = self.ps12dc_solenoid_window
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.stop_for_test_pause()
                    return
            except tk.TclError:
                self.ps12dc_solenoid_window = None
        if self.request_ps12dc_s0(False):
            self.log_event("PS12DC S0 stopped", "Test paused; S0 commanded OFF.")

    def raw_auxiliary_history_seconds(self) -> float:
        """Return the time span currently available for a raw AIN0/AIN1 capture."""
        if len(self._raw_auxiliary_samples) < 2:
            return 0.0
        return max(
            0.0,
            self._raw_auxiliary_samples[-1].monotonic_seconds - self._raw_auxiliary_samples[0].monotonic_seconds,
        )

    @staticmethod
    def _raw_auxiliary_value(value: object) -> float | None:
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return None
        return candidate if math.isfinite(candidate) else None

    def _record_raw_auxiliary_scans(
        self,
        raw_scans: object,
        scan_rate_hz: float,
        batch_end_monotonic: float,
        batch_end_at: datetime,
        device_backlog: int,
        ljm_backlog: int,
    ) -> None:
        """Keep a continuous, sample-clocked minute of unscaled AIN0/AIN1 scans."""
        if not isinstance(raw_scans, list) or not raw_scans:
            return
        scan_count = len(raw_scans)
        sample_interval = 1.0 / max(scan_rate_hz, 1.0)
        if self._last_auxiliary_capture_monotonic is None or self._last_auxiliary_capture_at is None:
            first_monotonic = batch_end_monotonic - (scan_count - 1) * sample_interval
            first_at = batch_end_at - timedelta(seconds=(scan_count - 1) * sample_interval)
        else:
            # eStreamRead returns batches.  Their arrival time can jitter even
            # when the T4 is sampling continuously, so never turn host-side
            # scheduling delays into false sensor gaps in the diagnostic CSV.
            first_monotonic = self._last_auxiliary_capture_monotonic + sample_interval
            first_at = self._last_auxiliary_capture_at + timedelta(seconds=sample_interval)
        for index, scan in enumerate(raw_scans):
            if not isinstance(scan, (list, tuple)) or len(scan) != 2:
                continue
            ain0, ain1 = (self._raw_auxiliary_value(value) for value in scan)
            sample_monotonic = first_monotonic + index * sample_interval
            sample_at_value = first_at + timedelta(seconds=index * sample_interval)
            sample_at = sample_at_value.isoformat(timespec="milliseconds")
            self._raw_auxiliary_samples.append(
                AuxiliaryRawSample(
                    timestamp=sample_at,
                    monotonic_seconds=sample_monotonic,
                    scan_rate_hz=scan_rate_hz,
                    batch_received_at=batch_end_at.isoformat(timespec="milliseconds"),
                    ain0_voltage=ain0,
                    ain1_voltage=ain1,
                    device_backlog=device_backlog,
                    ljm_backlog=ljm_backlog,
                )
            )
            if ain0 is not None:
                self.auxiliary_raw_voltages["AIN0"] = ain0
            if ain1 is not None:
                self.auxiliary_raw_voltages["AIN1"] = ain1
            self._last_auxiliary_capture_monotonic = sample_monotonic
            self._last_auxiliary_capture_at = sample_at_value

    def save_raw_auxiliary_capture(self) -> None:
        """Save one operator-requested minute of uncalibrated auxiliary readings."""
        if self.raw_auxiliary_history_seconds() < RAW_AUXILIARY_CAPTURE_SECONDS:
            messagebox.showinfo(
                "Raw capture still collecting",
                "Wait until a full 60 seconds of AIN0/AIN1 data has been received.",
                parent=self,
            )
            return
        latest = self._raw_auxiliary_samples[-1].monotonic_seconds
        cutoff = latest - RAW_AUXILIARY_CAPTURE_SECONDS
        samples = tuple(sample for sample in self._raw_auxiliary_samples if sample.monotonic_seconds >= cutoff)
        if len(samples) < 2:
            messagebox.showerror("Raw capture unavailable", "No complete 60-second AIN0/AIN1 capture is available.", parent=self)
            return

        folder = (self.logger.folder / "diagnostics") if self.logger is not None else (self.run_output_root() / "diagnostics")
        destination = folder / f"raw_ain0_ain1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        temporary = destination.with_suffix(".tmp")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow([
                    "timestamp",
                    "elapsed_seconds",
                    "scan_rate_hz",
                    "batch_received_at",
                    "ain0_raw_volts",
                    "ain1_raw_volts",
                    "ain0_valid",
                    "ain1_valid",
                    "device_backlog",
                    "ljm_backlog",
                ])
                first_time = samples[0].monotonic_seconds
                for sample in samples:
                    writer.writerow([
                        sample.timestamp,
                        f"{sample.monotonic_seconds - first_time:.6f}",
                        f"{sample.scan_rate_hz:.3f}",
                        sample.batch_received_at,
                        "" if sample.ain0_voltage is None else f"{sample.ain0_voltage:.6f}",
                        "" if sample.ain1_voltage is None else f"{sample.ain1_voltage:.6f}",
                        "yes" if sample.ain0_voltage is not None else "",
                        "yes" if sample.ain1_voltage is not None else "",
                        sample.device_backlog,
                        sample.ljm_backlog,
                    ])
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(destination)
        except OSError as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            messagebox.showerror("Raw capture failed", f"Could not save the 60-second capture:\n{error}", parent=self)
            return

        duration = samples[-1].monotonic_seconds - samples[0].monotonic_seconds
        self.log_event("Raw auxiliary capture saved", f"{destination.name}: {len(samples):,} scans across {duration:.2f} seconds.")
        messagebox.showinfo(
            "Raw capture saved",
            f"Saved {len(samples):,} AIN0/AIN1 scans across {duration:.2f} seconds.\n\n{destination}",
            parent=self,
        )

    def record_temperature(self, sensor_number: int, temperature_fahrenheit: float) -> None:
        """Accept one calibrated physical temperature and extend its test-history plot."""
        if sensor_number not in self.temperature_history:
            raise ValueError("Temperature sensor number must be 1 or 2.")
        try:
            value = float(temperature_fahrenheit)
        except (TypeError, ValueError) as error:
            raise ValueError("Temperature input must be numeric.") from error
        if not math.isfinite(value):
            raise ValueError("Temperature input must be finite.")
        self.current_temperatures[sensor_number] = value
        elapsed = max(0.0, self.test_elapsed_seconds)
        history = self.temperature_history[sensor_number]
        # The cards may refresh with each shared-stream batch, but long-term
        # trends only need one representative value per second. Keeping every
        # 20 Hz display update made a full-duration graph increasingly costly
        # to redraw and could visibly stall Tk during long tests.
        if history and elapsed - history[-1][0] < TEMPERATURE_HISTORY_SAMPLE_SECONDS:
            history[-1] = (elapsed, value)
        else:
            history.append((elapsed, value))
        self.metric_vars[f"temp_{sensor_number}"].set(f"{value:.1f} °F")

    def _process_temperature_samples(self, channel: str, raw_samples: object) -> None:
        """Render one stable AIN0/AIN1 value per received shared-stream batch."""
        if not self.temperature_calibration or not isinstance(raw_samples, list):
            return
        temperatures: list[float] = []
        for raw_voltage in raw_samples:
            try:
                voltage = float(raw_voltage)
                if math.isfinite(voltage):
                    temperatures.append(temperature_from_amplified_voltage(voltage, self.temperature_calibration, channel))
            except (TypeError, ValueError):
                continue
        sensor_number = 1 if channel == "AIN0" else 2
        if temperatures:
            self.record_temperature(sensor_number, statistics.fmean(temperatures) * 9.0 / 5.0 + 32.0)
            voltage = self.auxiliary_raw_voltages.get(channel)
            if voltage is not None:
                self.temperature_detail_vars[sensor_number].set(f"{channel}: {voltage:.4f} V")
        elif self.labjack_available:
            self.metric_vars[f"temp_{sensor_number}"].set("— °F")
            self.temperature_detail_vars[sensor_number].set(f"{channel} outside Type T range")

    def _record_cycle_frequency_history(self) -> None:
        """Sample the rolling CPM once per active second for the long-term graph."""
        elapsed = self.test_elapsed_seconds
        if self.cycle_frequency_history and elapsed - self.cycle_frequency_history[-1][0] < 1.0:
            return
        self.cycle_frequency_history.append((elapsed, self._frequency()))

    def _run_configuration_values(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_version": APP_VERSION,
            "data_model": {
                "primary_record": "one summary row per completed cycle",
                "dashboard_snapshot_log": False,
                "grease_response_threshold_psi": INJECTOR_ACTIVATION_PSI,
                "grease_comparison_cycles": GREASE_COMPARISON_CYCLE_COUNT,
                "overlapping_grease_events_flagged": True,
            },
            "storage": {
                "mode": "direct_run_folder",
                "output_root": str(self.active_run_output_root or self.run_output_root()),
            },
            "position_calibration": self.position_calibration,
            "pressure_calibration": self.pressure_calibration,
            "temperature_calibration": self.temperature_calibration,
            "cycle_stops": self.cycle_stops,
            "signals": {
                "ain2_bushing_position": "LabJack T4 live read-only input",
                "ain4_grease_pressure": (
                    "LabJack T4 FIO4/AIN4 calibrated from installed gauge"
                    if self.pressure_calibration else "LabJack T4 FIO4/AIN4 awaiting gauge calibration"
                ),
                "grease_event_analysis": "confirmed PS12DC S0 ON events during active runs",
                "temperature_1_to_2": (
                    "Type T probes on AIN0/AIN1 through LJTick-InAmp; manual terminal-temperature CJC"
                    if self.temperature_calibration else "LabJack T4 AIN0/AIN1 awaiting Type T setup"
                ),
                "physical_outputs": "manual/timed PS12DC S0 control via T4 DIO8",
            },
        }

    def _write_run_configuration_snapshot(self) -> None:
        """Preserve current settings for the full application session."""
        if self.logger is not None:
            self.logger.write_run_configuration(self._run_configuration_values())

    def _begin_live_run(self) -> None:
        if self.logger is None:
            try:
                output_root = self.run_output_root()
                self.logger = CsvLogger(output_root)
                self.active_run_output_root = output_root
                self.logger.update_manifest(app_version=APP_VERSION, data_model="completed_cycle_summary")
            except OSError as error:
                self.logger = None
                self.active_run_output_root = None
                raise RuntimeError(f"Could not create the selected run folder: {error}") from error
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
        if self.logger is None:
            self.current_live_run_id = None
            return
        self.logger.finish_live_run(
            self.current_live_run_id,
            status=status,
            completed_cycles=self.cycles,
            cycle_frequency_cpm=round(self._frequency(), 2),
        )
        self.current_live_run_id = None
        self.logger.finalize(
            completed_cycles=self.cycles,
            cycle_frequency_cpm=round(self._frequency(), 2),
            active_test_seconds=round(self.test_elapsed_seconds, 1),
            final_auxiliary_status="not_configured",
        )
        self.logger.close()
        self.logger = None
        self.active_run_output_root = None

    def _set_labjack_dependent_controls(self, available: bool) -> None:
        """Enable only actions that have a confirmed live T4 behind them."""
        self.labjack_available = available
        if not available and self.is_running:
            self.is_running = False
            self.cycle_state = "waiting_for_retracted"
            self.log_event("Cycle counting stopped", "LabJack disconnected.")
            self._finish_live_run("interrupted_by_disconnect")
        count_ready = bool(available and self.position_calibration and self.cycle_stops)
        self._refresh_setup_status()
        if self.is_running:
            self.start_button.configure(text="Resume" if self.current_live_run_id else "Start", state="disabled")
            self.stop_button.configure(state="normal" if available else "disabled")
            return

        self.start_button.configure(text="Resume" if self.current_live_run_id else "Start", state="normal" if count_ready else "disabled")
        self.stop_button.configure(state="disabled")

    def start_counting(self) -> None:
        if not self.labjack_available:
            messagebox.showerror(
                "LabJack required",
                "Connect the LabJack T4 and wait for AIN2 to become active before starting cycle counting.",
                parent=self,
            )
            return
        if not self.position_calibration:
            messagebox.showerror(
                "Calibration required",
                "Save the two-point AIN2 position calibration before starting cycle counting.",
                parent=self,
            )
            return
        if not self.cycle_stops:
            messagebox.showerror(
                "Cycle stops required",
                "Set the reset and count stops before starting cycle counting.",
                parent=self,
            )
            return
        if self.is_running:
            return
        resuming = self.current_live_run_id is not None
        self.is_running = True
        self.cycle_state = "waiting_for_retracted"
        self._cycle_capture = None
        # A dashboard round is one direct-output folder. Start after Pause
        # resumes that same folder rather than opening another export item.
        self.cycle_times.clear()
        if not resuming:
            try:
                self._begin_live_run()
            except RuntimeError as error:
                self.is_running = False
                messagebox.showerror("Run folder unavailable", str(error), parent=self)
                return
        self._set_labjack_dependent_controls(True)
        self.log_event("Cycle counting resumed" if resuming else "Cycle counting started", "Waiting for the reset stop.")

    def pause_counting(self) -> None:
        if not self.is_running:
            return
        self.stop_ps12dc_for_test_pause()
        self.is_running = False
        self._discard_cycle_capture("Cycle counting paused before the count stop was reached.")
        # CPM is deliberately only for the uninterrupted active interval. Do
        # not let a restart borrow cycles from before the pause.
        self.cycle_times.clear()
        self.metric_vars["frequency"].set("0.0 cpm")
        self._set_labjack_dependent_controls(self.labjack_available)
        self.log_event("Cycle counting paused", "Dashboard totals and the current test run are retained.")

    def reset_counting(self) -> None:
        if self.current_live_run_id:
            self.is_running = False
            self.stop_ps12dc_for_test_pause()
            self._discard_cycle_capture("Saved before the count stop was reached.")
            self.log_event("Test round saved", "The test round was finalized and the dashboard cleared for the next round.")
            self._finish_live_run("saved")
        self.cycles = 0
        self.cycle_times.clear()
        self.test_elapsed_seconds = 0.0
        for history in self.temperature_history.values():
            history.clear()
        self.current_temperatures = {1: None, 2: None}
        self.cycle_frequency_history.clear()
        self.cycle_state = "waiting_for_retracted"
        self.metric_vars["cycles"].set("0")
        self.metric_vars["frequency"].set("0.0 cpm")
        self.metric_vars["test_time"].set("00:00:00")
        self._set_labjack_dependent_controls(self.labjack_available)
        self.log_event(
            "Dashboard reset",
            "Ready for a new test round.",
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

    def _configuration_change_is_locked(self) -> bool:
        """Keep one run's calibration and cycle-stop interpretation immutable."""
        if not self.current_live_run_id:
            return False
        messagebox.showwarning(
            "Save the test run first",
            "Calibration and cycle stops are locked while a test run is open. "
            "Choose Save & Reset before changing them so the saved run has one unambiguous configuration.",
            parent=self,
        )
        return True

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

    def save_position_calibration(self, calibration: dict[str, object]) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.write_config("position_calibration.json", calibration)
        self.position_calibration = calibration
        self.bushing_unit = "degrees"
        self.configure_bushing_plot()
        self.configure_velocity_plot()
        self._cycle_capture = None
        self._clear_snapshot_history()
        self._write_run_configuration_snapshot()
        self._set_labjack_dependent_controls(self.labjack_available)
        return True

    def clear_position_calibration(self) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.remove_config("position_calibration.json")
        self.position_calibration = None
        self.bushing_unit = "volts"
        self.configure_bushing_plot()
        self.configure_velocity_plot()
        self._cycle_capture = None
        self._clear_snapshot_history()
        self._write_run_configuration_snapshot()
        self._set_labjack_dependent_controls(self.labjack_available)
        self.log_event("AIN2 calibration cleared", "Saved two-point AIN2 calibration removed.")
        return True

    def load_pressure_calibration(self) -> dict[str, object] | None:
        calibration = self.paths.read_config("pressure_calibration.json")
        try:
            if calibration is None:
                return None
            first, second = calibration["point_1"], calibration["point_2"]
            if abs(float(second["voltage"]) - float(first["voltage"])) < MIN_PRESSURE_CALIBRATION_VOLTAGE_SPAN:
                return None
            if float(first["pressure_psi"]) < 0 or float(second["pressure_psi"]) < 0:
                return None
            return calibration
        except (KeyError, TypeError, ValueError):
            return None

    def save_pressure_calibration(self, calibration: dict[str, object]) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.write_config("pressure_calibration.json", calibration)
        self.pressure_calibration = calibration
        self._display_pressure_voltage_samples.clear()
        self.grease_pressure = None
        self.metric_vars["pressure"].set("— psi")
        self.pressure_detail_var.set("Waiting for AIN4")
        self.configure_pressure_plot()
        self._write_run_configuration_snapshot()
        self._refresh_setup_status()
        return True

    def clear_pressure_calibration(self) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.remove_config("pressure_calibration.json")
        self.pressure_calibration = None
        self._display_pressure_voltage_samples.clear()
        self.grease_pressure = None
        self.metric_vars["pressure"].set("— psi")
        self.pressure_detail_var.set("Input not configured")
        self.configure_pressure_plot()
        self._write_run_configuration_snapshot()
        self._refresh_setup_status()
        self.log_event("AIN4 pressure calibration cleared", "Saved two-point FIO4/AIN4 pressure calibration removed.")
        return True

    def load_temperature_calibration(self) -> dict[str, object] | None:
        calibration = self.paths.read_config("temperature_calibration.json")
        try:
            if calibration is None or calibration.get("thermocouple_type") != "T":
                return None
            mode = str(calibration["mode"])
            cjc = float(calibration["cjc_celsius"])
            if mode not in {"nominal", "true"}:
                return None
            type_t_emf_millivolts(cjc)
            channels = calibration["channels"]
            if not isinstance(channels, dict):
                return None
            for channel in ("AIN0", "AIN1"):
                values = channels[channel]
                if not isinstance(values, dict):
                    return None
                offset, gain = float(values["offset_volts"]), float(values["gain"])
                if not math.isfinite(offset) or not math.isfinite(gain) or not 50.0 <= gain <= 400.0:
                    return None
                if mode == "true":
                    type_t_emf_millivolts(float(values["reference_celsius"]))
                    float(values["reference_voltage"])
            return calibration
        except (KeyError, TypeError, ValueError):
            return None

    def save_temperature_calibration(self, calibration: dict[str, object]) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.write_config("temperature_calibration.json", calibration)
        self.temperature_calibration = calibration
        self.temperature_history = {1: [], 2: []}
        self.current_temperatures = {1: None, 2: None}
        for sensor, channel in ((1, "AIN0"), (2, "AIN1")):
            self.metric_vars[f"temp_{sensor}"].set("— °F")
            self.temperature_detail_vars[sensor].set(f"Waiting for {channel}")
        self._write_run_configuration_snapshot()
        self._refresh_setup_status()
        self.log_event("Temperature setup saved", f"Type T {str(calibration['mode'])} setup saved for AIN0 and AIN1.")
        return True

    def clear_temperature_calibration(self) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.remove_config("temperature_calibration.json")
        self.temperature_calibration = None
        self.temperature_history = {1: [], 2: []}
        self.current_temperatures = {1: None, 2: None}
        for sensor in (1, 2):
            self.metric_vars[f"temp_{sensor}"].set("— °F")
            self.temperature_detail_vars[sensor].set("Input not configured")
        self._write_run_configuration_snapshot()
        self._refresh_setup_status()
        self.log_event("Temperature setup cleared", "Saved AIN0/AIN1 Type T setup removed.")
        return True

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

    def save_cycle_stops(self, stops: dict[str, float]) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.write_config("cycle_stops.json", stops)
        self.cycle_stops = {"retracted": float(stops["retracted"]), "extended": float(stops["extended"])}
        self.cycle_state = "waiting_for_retracted"
        self._cycle_capture = None
        self.configure_bushing_plot(clear_samples=False)
        self._write_run_configuration_snapshot()
        self._set_labjack_dependent_controls(self.labjack_available)
        self.log_event(
            "Cycle stops saved",
            f"Reset at {self.cycle_stops['retracted']:.3f}°; count at {self.cycle_stops['extended']:.3f}°.",
        )
        return True

    def clear_cycle_stops(self) -> bool:
        if self._configuration_change_is_locked():
            return False
        self.paths.remove_config("cycle_stops.json")
        self.cycle_stops = None
        self.cycle_state = "waiting_for_retracted"
        self._cycle_capture = None
        self.configure_bushing_plot(clear_samples=False)
        self._write_run_configuration_snapshot()
        self._set_labjack_dependent_controls(self.labjack_available)
        self.log_event("Cycle stops cleared", "Live AIN2 cycle counting disabled until two new stops are saved.")
        return True

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

    @staticmethod
    def pressure_from_voltage(voltage: float, calibration: dict[str, object]) -> tuple[float, bool]:
        first = calibration["point_1"]
        second = calibration["point_2"]
        first_voltage, second_voltage = float(first["voltage"]), float(second["voltage"])
        first_pressure, second_pressure = float(first["pressure_psi"]), float(second["pressure_psi"])
        fraction = (voltage - first_voltage) / (second_voltage - first_voltage)
        pressure = first_pressure + fraction * (second_pressure - first_pressure)
        span = abs(second_voltage - first_voltage)
        extension = span * PRESSURE_CALIBRATION_EXTRAPOLATION_FRACTION
        outside = voltage < min(first_voltage, second_voltage) - extension or voltage > max(first_voltage, second_voltage) + extension
        return pressure, outside

    def configure_pressure_plot(self, clear_samples: bool = True) -> None:
        """Scale the grease-pressure plot around the saved FIO4 gauge calibration."""
        if not self.pressure_calibration:
            self.pressure_plot.set_scale("Grease pressure (psi) — calibration required", 0.0, 5000.0, clear_samples)
        else:
            first = self.pressure_calibration["point_1"]
            second = self.pressure_calibration["point_2"]
            high = max(float(first["pressure_psi"]), float(second["pressure_psi"]), INJECTOR_ACTIVATION_PSI)
            padding = max(250.0, high * 0.10)
            self.pressure_plot.set_scale("Grease pressure (psi, calibrated FIO4/AIN4)", 0.0, high + padding, clear_samples)
        self.pressure_plot.set_reference_lines([(INJECTOR_ACTIVATION_PSI, "#7c3aed", "Injector activation")])

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
        self.velocity_plot.add(self.bushing_velocity, self._latest_display_sample_monotonic)

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

    def _invalidate_cycle_capture(self, reason: str) -> None:
        """Retain a counted stroke while explicitly withholding invalid motion metrics."""
        if self._cycle_capture is not None and self._cycle_capture.invalidate(reason):
            self.log_event("Cycle metrics unavailable", reason)

    @staticmethod
    def _grease_event_status(event: GreaseEventCapture, completion: str) -> str:
        baseline_note = "" if len(event.pre_cycles) == GREASE_COMPARISON_CYCLE_COUNT else (
            f"; limited pre-injection baseline ({len(event.pre_cycles)}/{GREASE_COMPARISON_CYCLE_COUNT} cycles)"
        )
        overlap_note = "" if not event.overlapping_event_ids else "; overlapping injection window"
        return completion + baseline_note + overlap_note

    def _finalize_pending_grease_events(self, completion: str) -> None:
        """Persist pending injection comparisons before the active test run closes."""
        if not self.current_live_run_id:
            return
        for event in self._pending_grease_events:
            status = self._grease_event_status(event, completion)
            if self.logger is not None:
                self.logger.log_grease_event(event.values(status=status), self.current_live_run_id)
            self.log_event("Grease event saved", f"{event.event_id}: {status}.")
        self._pending_grease_events.clear()

    def _add_cycle_to_grease_comparisons(self, values: dict[str, float | int | str]) -> None:
        # A cycle whose bushing capture left its calibrated span is retained in
        # cycle_summary.csv, but it cannot support a valid grease comparison.
        if values.get("bushing_data_quality") != "complete":
            return
        self._recent_cycle_summaries.append(values)
        completed: list[GreaseEventCapture] = []
        for event in self._pending_grease_events:
            event.post_cycles.append(values)
            if len(event.post_cycles) >= GREASE_COMPARISON_CYCLE_COUNT:
                completed.append(event)
        for event in completed:
            self._pending_grease_events.remove(event)
            status = self._grease_event_status(event, "complete: 3 post-injection cycles")
            if self.current_live_run_id and self.logger is not None:
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
        if self._cycle_capture is not None and self.current_live_run_id and self.logger is not None:
            values = self._cycle_capture.completed_values(
                completed_at=sample_at,
                completed_monotonic=sample_monotonic,
                cycle_number=self.cycles,
                live_run_cycle_number=self.cycles - self.current_live_run_cycle_start,
                frequency=self._frequency(),
            )
            for sensor_number in (1, 2):
                value = self.current_temperatures[sensor_number]
                values[f"temp_{sensor_number}"] = "" if value is None else round(value, 2)
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
        latest_pressure_voltage: float | None = None
        for result in results:
            if result.get("status") == "success":
                self._set_labjack_connection_state("connected", "AIN2 stream is active.")
                # AIN0/AIN1 share the existing AIN2 stream.  Do not create a
                # second connection just to inspect prospective sensor wiring.
                for channel, field in (("AIN0", "ain0_samples"), ("AIN1", "ain1_samples")):
                    channel_samples = result.get(field, [])
                    if isinstance(channel_samples, list):
                        for raw_value in reversed(channel_samples):
                            try:
                                value = float(raw_value)
                            except (TypeError, ValueError):
                                continue
                            if math.isfinite(value):
                                self.auxiliary_raw_voltages[channel] = value
                                break
                    self._process_temperature_samples(channel, channel_samples)
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
                fallback_end_monotonic = time.monotonic()
                try:
                    batch_end_monotonic = float(result.get("batch_ended_monotonic", fallback_end_monotonic))
                except (TypeError, ValueError):
                    batch_end_monotonic = fallback_end_monotonic
                try:
                    batch_end_at = datetime.fromisoformat(str(result.get("batch_ended_at", "")))
                except (TypeError, ValueError):
                    batch_end_at = datetime.now().astimezone()
                try:
                    device_backlog = int(result.get("device_backlog", 0))
                except (TypeError, ValueError):
                    device_backlog = 0
                try:
                    ljm_backlog = int(result.get("ljm_backlog", 0))
                except (TypeError, ValueError):
                    ljm_backlog = 0
                scan_count = len(samples)
                if self._last_ain2_sample_monotonic is not None and batch_end_monotonic <= self._last_ain2_sample_monotonic:
                    # A restarted helper must never make graph time move backward.
                    corrected_end = self._last_ain2_sample_monotonic + scan_count / max(scan_rate_hz, 1.0)
                    correction = corrected_end - batch_end_monotonic
                    batch_end_monotonic = corrected_end
                    batch_end_at += timedelta(seconds=correction)
                self._record_raw_auxiliary_scans(
                    result.get("auxiliary_scans", []),
                    scan_rate_hz,
                    batch_end_monotonic,
                    batch_end_at,
                    device_backlog,
                    ljm_backlog,
                )
                pressure_samples = result.get("ain4_samples", [])
                if not isinstance(pressure_samples, list):
                    pressure_samples = []
                for index, raw_voltage in enumerate(pressure_samples):
                    try:
                        voltage = float(raw_voltage)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(voltage):
                        continue
                    seconds_before_end = (len(pressure_samples) - 1 - index) / max(scan_rate_hz, 1.0)
                    sample_monotonic = batch_end_monotonic - seconds_before_end
                    sample_at = (batch_end_at - timedelta(seconds=seconds_before_end)).isoformat(timespec="milliseconds")
                    self._process_pressure_voltage(voltage, sample_at, sample_monotonic)
                    latest_pressure_voltage = voltage
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
                    self._last_ain2_sample_monotonic = sample_monotonic
            elif result.get("status") == "output":
                channel = str(result.get("channel", ""))
                if channel == "DIO8":
                    enabled = bool(result.get("enabled"))
                    pulse_seconds: float | None = None
                    if self._pending_s0_requests and self._pending_s0_requests[0][0] == enabled:
                        _requested_state, pulse_seconds = self._pending_s0_requests.popleft()
                    if enabled:
                        self.record_grease_injection(pulse_seconds)
                    self.log_event("PS12DC S0 changed", f"S0 ({channel}) is {'ON' if enabled else 'OFF'}.")
            elif result.get("status") == "output_error":
                channel = str(result.get("channel", "output"))
                enabled = bool(result.get("enabled"))
                if channel == "DIO8" and self._pending_s0_requests and self._pending_s0_requests[0][0] == enabled:
                    self._pending_s0_requests.popleft()
                self.log_event("PS12DC output error", f"{channel}: {result.get('error', 'write failed.')}")
            elif result.get("status") == "disconnected":
                self._set_labjack_connection_state(
                    "disconnected", str(result.get("error", "LabJack T4 is disconnected."))
                )
            else:
                self._set_labjack_connection_state("error", str(result.get("error", "AIN2 stream failed.")))

        if latest_voltage is not None:
            self._render_live_bushing_position(statistics.fmean(self._display_voltage_samples))
            self._render_bushing_velocity()
        if latest_pressure_voltage is not None:
            self._render_live_pressure(statistics.fmean(self._display_pressure_voltage_samples))

    def _process_counting_voltage(
        self, voltage: float, scan_rate_hz: float, sample_at: str, sample_monotonic: float
    ) -> None:
        """Feed each hardware-timed sample to the detector; never reject it for motion."""
        self._display_voltage_samples.append(voltage)
        # The charts must advance with every valid AIN2 input, even if that
        # input sits just outside the saved calibration range and is excluded
        # from the snapshot data.
        self._latest_display_sample_monotonic = sample_monotonic
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
                self._invalidate_cycle_capture("AIN2 left calibrated span; motion metrics unavailable")

    def _process_pressure_voltage(self, voltage: float, sample_at: str, sample_monotonic: float) -> None:
        """Process every valid AIN4 sample without redrawing the UI at stream rate."""
        self._display_pressure_voltage_samples.append(voltage)
        if not self.pressure_calibration:
            return
        pressure, outside = self.pressure_from_voltage(voltage, self.pressure_calibration)
        if not outside and math.isfinite(pressure) and pressure >= 0:
            self.record_grease_pressure(
                pressure,
                sample_at=sample_at,
                sample_monotonic=sample_monotonic,
                render=False,
            )

    def _render_live_pressure(self, voltage: float) -> None:
        """Render a short display-only AIN4 average after each received stream batch."""
        self.ain4_voltage = voltage
        if not self.pressure_calibration:
            self.metric_vars["pressure"].set("— psi")
            self.pressure_detail_var.set(f"AIN4: {voltage:.4f} V • calibration required")
            return
        pressure, outside = self.pressure_from_voltage(voltage, self.pressure_calibration)
        if outside or not math.isfinite(pressure) or pressure < 0:
            self.metric_vars["pressure"].set("— psi")
            self.pressure_detail_var.set(f"AIN4: {voltage:.4f} V • OUT")
            return
        self.metric_vars["pressure"].set(f"{pressure:,.0f} psi")
        self.pressure_detail_var.set("")
        self.pressure_plot.add(pressure, self._latest_display_sample_monotonic)

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
                self.bushing_plot.add(angle, self._latest_display_sample_monotonic)
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
        self._frozen_snapshot_window_samples = None
        self._frozen_live_position_samples = None
        self._frozen_live_velocity_samples = None
        self._frozen_position_graph = None
        self._frozen_velocity_graph = None
        self.graph_pan_offset_seconds = 0.0
        self.graph_window_var.set(f"Live: latest {SNAPSHOT_HISTORY_SECONDS:g} seconds")
        if hasattr(self, "pause_graphs_button"):
            self.pause_graphs_button.configure(text="Pause")
            self.save_snapshot_button.configure(state="disabled")
            self._update_graph_pan_controls()

    @staticmethod
    def _format_before_pause(seconds: float) -> str:
        return "paused" if seconds <= 0.05 else f"{seconds:.0f}s before pause"

    def _maximum_graph_pan_seconds(self) -> float:
        if not self._frozen_snapshot_samples:
            return 0.0
        duration = self._frozen_snapshot_samples[-1].monotonic_seconds - self._frozen_snapshot_samples[0].monotonic_seconds
        return max(0.0, duration - SNAPSHOT_HISTORY_SECONDS)

    def _update_graph_pan_controls(self) -> None:
        if not hasattr(self, "pan_left_graph_button"):
            return
        maximum = self._maximum_graph_pan_seconds()
        self.pan_left_graph_button.configure(
            state="normal" if self.graphs_paused and self.graph_pan_offset_seconds < maximum - 0.05 else "disabled"
        )
        self.pan_right_graph_button.configure(
            state="normal" if self.graphs_paused and self.graph_pan_offset_seconds > 0.05 else "disabled"
        )

    def _render_frozen_graph_window(self) -> None:
        """Render the selected 15-second portion of the paused review buffer."""
        if not self._frozen_snapshot_samples:
            return
        history = self._frozen_snapshot_samples
        newest = history[-1].monotonic_seconds
        maximum_offset = self._maximum_graph_pan_seconds()
        self.graph_pan_offset_seconds = max(0.0, min(self.graph_pan_offset_seconds, maximum_offset))
        window_end = newest - self.graph_pan_offset_seconds
        window_start = window_end - SNAPSHOT_HISTORY_SECONDS
        window_samples = tuple(sample for sample in history if window_start <= sample.monotonic_seconds <= window_end)
        if not window_samples:
            return

        left_offset = self.graph_pan_offset_seconds + SNAPSHOT_HISTORY_SECONDS
        left_label = self._format_before_pause(left_offset)
        right_label = self._format_before_pause(self.graph_pan_offset_seconds)
        if self.graph_pan_offset_seconds <= 0.05 and self._frozen_live_position_samples is not None:
            # At the moment of pause, keep the exact chart that the operator
            # was already viewing. Raw 200 Hz data is for the saved CSV and
            # for intentional historical panning, not a surprise redraw.
            position_samples = self._frozen_live_position_samples
            velocity_samples = self._frozen_live_velocity_samples or ()
        else:
            position_samples = tuple((sample.monotonic_seconds, sample.position_degrees) for sample in window_samples)
            velocity_samples = tuple(
                (sample.monotonic_seconds, 0.0 if sample.velocity_degrees_per_second is None else sample.velocity_degrees_per_second)
                for sample in window_samples
            )
        self.bushing_plot.show_window(
            position_samples,
            window_end_monotonic=window_end,
            left_time_label=left_label,
            right_time_label=right_label,
        )
        self.velocity_plot.show_window(
            velocity_samples,
            window_end_monotonic=window_end,
            left_time_label=left_label,
            right_time_label=right_label,
        )
        self._frozen_snapshot_window_samples = window_samples
        self._frozen_position_graph = self.bushing_plot.snapshot_state()
        self._frozen_velocity_graph = self.velocity_plot.snapshot_state()
        self.graph_window_var.set(f"Paused: {left_label} to {right_label}")
        self._update_graph_pan_controls()

    def _render_live_graph_window(self) -> None:
        """Return both charts to the newest display-sized window after Resume."""
        if not self._snapshot_samples:
            return
        history = tuple(self._snapshot_samples)
        newest = history[-1].monotonic_seconds
        window_start = newest - SNAPSHOT_HISTORY_SECONDS
        window_samples = tuple(sample for sample in history if sample.monotonic_seconds >= window_start)
        if not window_samples:
            return
        position_samples = tuple((sample.monotonic_seconds, sample.position_degrees) for sample in window_samples)
        velocity_samples = tuple(
            (sample.monotonic_seconds, 0.0 if sample.velocity_degrees_per_second is None else sample.velocity_degrees_per_second)
            for sample in window_samples
        )
        self.bushing_plot.show_window(
            position_samples,
            window_end_monotonic=newest,
            left_time_label=f"{SNAPSHOT_HISTORY_SECONDS:g}s ago",
            right_time_label="now",
        )
        self.velocity_plot.show_window(
            velocity_samples,
            window_end_monotonic=newest,
            left_time_label=f"{SNAPSHOT_HISTORY_SECONDS:g}s ago",
            right_time_label="now",
        )

    def pan_paused_graphs(self, seconds: float) -> None:
        """Move the paused 15-second view through its 60-second review buffer."""
        if not self.graphs_paused:
            return
        self.graph_pan_offset_seconds += seconds
        self._render_frozen_graph_window()

    def toggle_graph_pause(self) -> None:
        """Freeze charts for review while hardware sampling continues in the background."""
        if self.graphs_paused:
            self.graphs_paused = False
            self._render_live_graph_window()
            self._frozen_snapshot_samples = None
            self._frozen_snapshot_window_samples = None
            self._frozen_live_position_samples = None
            self._frozen_live_velocity_samples = None
            self._frozen_position_graph = None
            self._frozen_velocity_graph = None
            self.graph_pan_offset_seconds = 0.0
            self.graph_window_var.set(f"Live: latest {SNAPSHOT_HISTORY_SECONDS:g} seconds")
            self.pause_graphs_button.configure(text="Pause")
            self.save_snapshot_button.configure(state="disabled")
            self._update_graph_pan_controls()
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
        self._frozen_live_position_samples = tuple(self.bushing_plot.samples)
        self._frozen_live_velocity_samples = tuple(self.velocity_plot.samples)
        self.graph_pan_offset_seconds = 0.0
        self._render_frozen_graph_window()
        self.pause_graphs_button.configure(text="Resume")
        self.save_snapshot_button.configure(state="normal")
        self.log_event(
            "Graph snapshot paused",
            f"Frozen {len(self._frozen_snapshot_samples):,} calibrated AIN2 samples for review.",
        )

    def _snapshot_output_root(self) -> Path:
        """Keep snapshots with their active test round whenever possible."""
        if self.logger is not None:
            return self.logger.folder / "snapshots"
        return self.run_output_root() / "snapshots"

    def save_graph_snapshot(self) -> None:
        """Export the displayed paused window and its matching full-rate samples."""
        samples = self._frozen_snapshot_window_samples
        position_graph, velocity_graph = self._frozen_position_graph, self._frozen_velocity_graph
        if not self.graphs_paused or not samples or position_graph is None or velocity_graph is None:
            return
        output_root = self._snapshot_output_root()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = output_root / f"Snapshot_{timestamp}"
        suffix = 2
        temporary_destination = output_root / f".{destination.name}.tmp"
        while destination.exists() or temporary_destination.exists():
            destination = output_root / f"Snapshot_{timestamp}_{suffix:02d}"
            temporary_destination = output_root / f".{destination.name}.tmp"
            suffix += 1
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            temporary_destination.mkdir()
            data_file = temporary_destination / "position_velocity_200hz.csv"
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
            LivePlot.write_svg(temporary_destination / "position_graph.svg", position_graph)
            LivePlot.write_svg(temporary_destination / "velocity_graph.svg", velocity_graph)
            metadata = {
                "schema_version": 1,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source": "paused position and velocity dashboard graphs",
                "sample_count": len(samples),
                "capture_duration_seconds": round(samples[-1].monotonic_seconds - first_time, 4),
                "display_window_seconds": SNAPSHOT_HISTORY_SECONDS,
                "review_buffer_seconds": SNAPSHOT_REVIEW_SECONDS,
                "view_offset_before_pause_seconds": round(self.graph_pan_offset_seconds, 3),
                "current_live_run_id": self.current_live_run_id,
                "position_calibration": self.position_calibration,
                "cycle_stops": self.cycle_stops,
                "files": ["position_velocity_200hz.csv", "position_graph.svg", "velocity_graph.svg"],
            }
            (temporary_destination / "snapshot_metadata.json").write_text(
                json.dumps(metadata, indent=2), encoding="utf-8"
            )
            temporary_destination.replace(destination)
        except Exception as error:
            shutil.rmtree(temporary_destination, ignore_errors=True)
            messagebox.showerror("Snapshot export failed", f"Could not save the graph snapshot:\n{error}", parent=self)
            return
        self.log_event("Graph snapshot saved", f"{destination.name}: {len(samples):,} full-rate AIN2 samples.")
        messagebox.showinfo("Snapshot saved", f"Saved {len(samples):,} full-rate samples and both graphs.\n\n{destination}", parent=self)

    def open_calibration_window(self) -> None:
        PositionCalibrationDialog(self)

    def open_export_window(self, preselect_run_id: str | None = None) -> None:
        ExportDataDialog(self, preselect_run_id=preselect_run_id)

    def run_output_root(self) -> Path:
        configured = str(self.run_output_config.get("directory", "")).strip()
        return Path(configured) if configured else self.paths.root / "runs"

    def load_run_output_config(self) -> dict[str, object]:
        saved = self.paths.read_config(RUN_OUTPUT_CONFIG_FILENAME)
        if not saved:
            return {"directory": ""}
        directory = str(saved.get("directory", "")).strip()
        return {"directory": directory}

    def choose_run_output_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self, title="Choose Run Output Folder", initialdir=str(self.run_output_root()))
        if not selected:
            return
        self.run_output_config = {"directory": selected}
        self.paths.write_config(RUN_OUTPUT_CONFIG_FILENAME, self.run_output_config)
        detail = "The selected folder applies to the next test round." if self.current_live_run_id else "New test rounds will be saved here."
        self.log_event("Run output folder changed", detail)

    def use_default_run_output_folder(self) -> None:
        self.run_output_config = {"directory": ""}
        self.paths.write_config(RUN_OUTPUT_CONFIG_FILENAME, self.run_output_config)
        self.log_event("Run output folder reset", f"New test rounds will use {self.run_output_root()}.")

    def open_run_output_folder(self) -> None:
        target = self.active_run_output_root or self.run_output_root()
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(target)  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror("Open Run Output Folder", f"Could not open the run output folder:\n{error}", parent=self)

    def available_run_folders(self) -> list[Path]:
        """Return direct-output test folders from the current and active roots."""
        roots = {self.run_output_root()}
        if self.logger is not None:
            roots.add(self.logger.folder.parent)
        folders: list[Path] = []
        for root in roots:
            try:
                folders.extend(folder for folder in root.glob("Run_*") if folder.is_dir())
            except OSError:
                continue
        return sorted(set(folders), key=lambda folder: folder.name, reverse=True)

    def open_cycle_stops_window(self) -> None:
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
        signature = f"{state}|{detail}"
        # A healthy helper emits a stream batch about every 50 ms. Reapplying
        # the same labels, buttons, and checklist for every batch creates
        # needless Tk work and can starve display updates during long charts.
        if signature == self.last_labjack_signature:
            return
        if state == "connected":
            text, color = "LabJack T4 • Connected • USB", "#86efac"
            event, event_detail = "LabJack connected", "AIN2 sampling active."
            self._last_runtime_error = ""
        elif state == "disconnected":
            self._pending_s0_requests.clear()
            text, color = "LabJack T4 • Disconnected", "#fbbf24"
            event, event_detail = "LabJack disconnected", detail
            self.bushing_position = None
            self.ain2_voltage = None
            self.ain4_voltage = None
            self.bushing_velocity = None
            self.auxiliary_raw_voltages = {"AIN0": None, "AIN1": None}
            self.current_temperatures = {1: None, 2: None}
            self._last_auxiliary_capture_monotonic = None
            self._last_auxiliary_capture_at = None
            self.metric_vars["position"].set("—")
            self.metric_vars["velocity"].set("— °/s")
            self.metric_vars["pressure"].set("— psi")
            self.metric_vars["temp_1"].set("— °F")
            self.metric_vars["temp_2"].set("— °F")
            self.bushing_detail_var.set("AIN2 unavailable")
            self.pressure_detail_var.set("AIN4 unavailable" if self.pressure_calibration else "Input not configured")
            self.temperature_detail_vars[1].set("AIN0 unavailable" if self.temperature_calibration else "Input not configured")
            self.temperature_detail_vars[2].set("AIN1 unavailable" if self.temperature_calibration else "Input not configured")
        else:
            self._pending_s0_requests.clear()
            text, color = "LabJack T4 • Connection error", "#fca5a5"
            event, event_detail = "LabJack connection error", detail
            self.bushing_position = None
            self.ain2_voltage = None
            self.ain4_voltage = None
            self.bushing_velocity = None
            self.auxiliary_raw_voltages = {"AIN0": None, "AIN1": None}
            self.current_temperatures = {1: None, 2: None}
            self._last_auxiliary_capture_monotonic = None
            self._last_auxiliary_capture_at = None
            self.metric_vars["position"].set("—")
            self.metric_vars["velocity"].set("— °/s")
            self.metric_vars["pressure"].set("— psi")
            self.metric_vars["temp_1"].set("— °F")
            self.metric_vars["temp_2"].set("— °F")
            self.bushing_detail_var.set("AIN2 unavailable")
            self.pressure_detail_var.set("AIN4 unavailable" if self.pressure_calibration else "Input not configured")
            self.temperature_detail_vars[1].set("AIN0 unavailable" if self.temperature_calibration else "Input not configured")
            self.temperature_detail_vars[2].set("AIN1 unavailable" if self.temperature_calibration else "Input not configured")

        self._set_labjack_dependent_controls(state == "connected")
        self.labjack_status_var.set(text)
        self.labjack_status_label.configure(foreground=color)
        self.last_labjack_signature = signature
        self.log_event(event, event_detail)

    def record_grease_injection(self, commanded_pulse_seconds: float | None = None) -> str | None:
        """Register a confirmed injector firing for later pressure/cycle comparison."""
        if not self.current_live_run_id or not self.is_running:
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
        # Preserve fast/manual testing exactly as it happened, but mark every
        # comparison whose three-cycle window overlaps another injection.
        for pending in self._pending_grease_events:
            pending.overlapping_event_ids.append(event.event_id)
            event.overlapping_event_ids.append(pending.event_id)
        self._pending_grease_events.append(event)
        overlap_detail = "; overlapping comparison window" if event.overlapping_event_ids else ""
        self.log_event(
            "Grease injection recorded",
            f"{event_id} registered for a {GREASE_COMPARISON_CYCLE_COUNT}-cycle comparison{overlap_detail}.",
        )
        return event_id

    def record_grease_pressure(
        self,
        pressure_psi: float,
        *,
        sample_at: str | None = None,
        sample_monotonic: float | None = None,
        render: bool = True,
    ) -> None:
        """Accept one calibrated physical pressure sample from FIO4/AIN4."""
        try:
            value = float(pressure_psi)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value < 0:
            return
        self.grease_pressure = value
        if render:
            self.metric_vars["pressure"].set(f"{value:,.0f} psi")
            self.pressure_plot.add(value, sample_monotonic)
        observed_at = sample_at or datetime.now().astimezone().isoformat(timespec="milliseconds")
        observed_monotonic = time.monotonic() if sample_monotonic is None else sample_monotonic
        for event in self._pending_grease_events:
            event.record_pressure(value, observed_at, observed_monotonic)

    def tick(self) -> None:
        try:
            now = time.monotonic()
            elapsed = max(0.0, now - self.last_tick)
            self.last_tick = now
            self._update_live_bushing_position(now)
            if self.is_running:
                self.test_elapsed_seconds += elapsed
                self._record_cycle_frequency_history()
            self.metric_vars["cycles"].set(str(self.cycles))
            self.metric_vars["frequency"].set(f"{self._frequency():0.1f} cpm")
            self.metric_vars["test_time"].set(self._format_test_time(self.test_elapsed_seconds))
        except Exception as error:
            self._handle_runtime_error(error)
        finally:
            if not self._closing:
                try:
                    self.after(UPDATE_MS, self.tick)
                except tk.TclError:
                    pass

    def _handle_runtime_error(self, error: Exception) -> None:
        """Keep the UI responsive and make an unexpected monitor failure visible."""
        detail = f"{type(error).__name__}: {error}"
        try:
            self.labjack_status_var.set("LabJack T4 • Monitoring error")
            self.labjack_status_label.configure(foreground="#fca5a5")
            self.bushing_detail_var.set("AIN2 monitor error — see Recent Events")
        except (AttributeError, tk.TclError):
            pass
        if detail == self._last_runtime_error:
            return
        self._last_runtime_error = detail
        try:
            self.log_event("Application monitoring error", detail)
        except Exception:
            # A full or unavailable local disk must not prevent the Tk event
            # loop from scheduling its next chance to recover.
            pass

    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.stop_ps12dc_for_test_pause()
            if self.current_live_run_id:
                self.is_running = False
                self.log_event("Test run interrupted", "Application closed before the test round was saved.")
                self._finish_live_run("interrupted_by_application_close")
            self.log_event("Application closed", "Operator closed the application.")
        except Exception as error:
            self._handle_runtime_error(error)
        finally:
            try:
                self.ain2_monitor.close()
            finally:
                if self.logger is not None:
                    try:
                        self.logger.close()
                    except Exception:
                        pass
                self.destroy()


if __name__ == "__main__":
    # In the packaged release, the GUI launches the dedicated diagnostic helper
    # for AIN2 streaming. Keep this dispatch for a direct helper invocation too.
    if any(argument in sys.argv for argument in ("--child-probe", "--sample-ain2", "--stream-ain2", "--json")):
        from labjack_diagnostic import main as diagnostic_main

        raise SystemExit(diagnostic_main())
    TestStandApp().mainloop()
