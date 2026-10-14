"""Senior Design Test Stand interface with live read-only LabJack T4 AIN2 position.

The bushing position and cycle count can use the physical string potentiometer.
Grease pressure and temperature signals remain simulated operator-interface data.
"""

from __future__ import annotations

import json
import csv
import math
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from collections import deque
from datetime import datetime
from io import StringIO
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from app_storage import AppPaths, CsvLogger
from labjack_diagnostic import summarize_ain2_samples


PRESSURE_REACHED_PSI = 2500.0
PRESSURE_RESET_PSI = 250.0
UPDATE_MS = 100
APP_VERSION = "0.1.0"
DEFAULT_SENSOR_LOG_SECONDS = 1.0
AIN2_POLL_SECONDS = 0.75
MIN_CALIBRATION_VOLTAGE_SPAN = 0.050
MIN_CYCLE_STOP_SEPARATION_DEGREES = 1.0
CALIBRATION_STREAM_SAMPLE_COUNT = 100


def format_local_timestamp(value: object, fallback: str = "Not available") -> str:
    """Render saved ISO timestamps compactly for operator-facing lists."""
    try:
        timestamp = datetime.fromisoformat(str(value))
        return timestamp.astimezone().strftime("%Y-%m-%d %I:%M %p")
    except (TypeError, ValueError):
        return fallback


class LivePlot(tk.Canvas):
    """A small dependency-free live line chart."""

    def __init__(self, parent: tk.Misc, title: str, maximum: float, color: str, minimum: float = 0.0) -> None:
        super().__init__(parent, height=190, background="#ffffff", highlightthickness=1,
                         highlightbackground="#cbd5e1")
        self.title = title
        self.minimum = minimum
        self.maximum = maximum
        self.color = color
        self.samples: deque[float] = deque(maxlen=120)
        self.reference_lines: list[tuple[float, str, str]] = []
        self.bind("<Configure>", lambda _event: self.redraw())

    def add(self, value: float) -> None:
        self.samples.append(max(self.minimum, min(self.maximum, value)))
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
        self.create_text(margin_left, height - 9, text="older", anchor="w", fill="#64748b",
                         font=("Segoe UI", 8))
        self.create_text(width - margin_right, height - 9, text="now", anchor="e", fill="#64748b",
                         font=("Segoe UI", 8))
        for value, color, label in self.reference_lines:
            if not self.minimum <= value <= self.maximum:
                continue
            y = margin_top + chart_height * (1 - (value - self.minimum) / (self.maximum - self.minimum))
            self.create_line(margin_left, y, width - margin_right, y, fill=color, width=2, dash=(5, 3))
            self.create_text(width - margin_right - 3, y - 3, text=label, anchor="se", fill=color,
                             font=("Segoe UI", 8, "bold"))
        if len(self.samples) < 2:
            return
        points: list[float] = []
        count = len(self.samples)
        for index, value in enumerate(self.samples):
            x = margin_left + chart_width * index / (count - 1)
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
            diagnostic = Path(__file__).with_name("labjack_diagnostic.py")
            process = subprocess.Popen(
                [sys.executable, str(diagnostic), "--stream-ain2"],
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
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")

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
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")

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


class AdvancedLoggingDialog(tk.Toplevel):
    """Choose how often normal dashboard snapshots are added to the CSV log."""

    INTERVAL_CHOICES = ("0.1", "0.2", "0.5", "1", "2", "5", "10")

    def __init__(self, app: "TestStandApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Advanced Logging")
        self.resizable(False, False)
        self.transient(app)
        self.interval_var = tk.StringVar(value=f"{app.sensor_log_interval_seconds:g}")
        self._build()
        self.lift()
        self.focus_force()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Standard sensor-log rate", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Choose how often dashboard readings are written to the CSV. This does not affect cycle counting.",
            wraplength=510,
            foreground="#475569",
        ).pack(anchor="w", pady=(5, 13))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        ttk.Label(form, text="Record one snapshot every").grid(row=0, column=0, sticky="w")
        interval = ttk.Combobox(
            form,
            textvariable=self.interval_var,
            values=self.INTERVAL_CHOICES,
            state="normal",
            width=8,
        )
        interval.grid(row=0, column=1, padx=(8, 5))
        ttk.Label(form, text="seconds").grid(row=0, column=2, sticky="w")
        ttk.Label(
            outer,
            text="AIN2 continues at 200 samples/second for the display and cycle detector; raw samples are not saved.",
            wraplength=510,
            foreground="#1e3a8a",
        ).pack(anchor="w", pady=(13, 15))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Apply", style="Grease.TButton", command=self.apply).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")

    def apply(self) -> None:
        try:
            interval_seconds = float(self.interval_var.get())
            if interval_seconds <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid log rate", "Enter a positive number of seconds.", parent=self)
            return
        self.app.set_sensor_log_interval(interval_seconds)
        self.destroy()


class ExportDataDialog(tk.Toplevel):
    """Export logical Start-to-Stop test runs while retaining the full local session log."""

    FILE_OPTIONS = (
        ("sensor", "Sensor readings (filtered to each live run)", "sensor_readings.csv"),
        ("events", "Event log / command outcomes (filtered to each live run)", "events.csv"),
        ("metadata", "Live-run metadata", "live_run.json"),
        ("configuration", "Configuration captured when each live run started", "run_configuration.json"),
    )

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
            text="Live runs begin with Start Counting and end with Stop or Reset. Complete local history is retained.",
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
        ttk.Button(buttons, text="Advanced Logging…", command=self.app.open_advanced_logging_window).pack(side="left")
        ttk.Button(buttons, text="Export Selected…", style="Grease.TButton", command=self.export).pack(side="right")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right", padx=(0, 8))

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

    @staticmethod
    def _filtered_csv(folder: Path, filename: str, run_id: str) -> str:
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
        archive_manifest = {
            "schema_version": 2,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_application": "Senior Design Test Stand",
            "selected_live_runs": [
                {"application_session": folder.name, "live_run_id": run.get("id")} for folder, run in selected_runs
            ],
            "included_file_types": [filename for _key, _label, filename in selected_options],
        }
        count = 0
        exported_run_ids_by_folder: dict[Path, set[str]] = {}
        try:
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("export_manifest.json", json.dumps(archive_manifest, indent=2))
                count += 1
                for folder, run in selected_runs:
                    run_id = str(run["id"])
                    archive_folder = f"{folder.name}/{run_id}"
                    for key, _label, filename in selected_options:
                        if key in {"sensor", "events"}:
                            contents = self._filtered_csv(folder, filename, run_id)
                        elif key == "metadata":
                            contents = json.dumps(run, indent=2)
                        else:
                            contents = json.dumps(run.get("configuration", {}), indent=2)
                        archive.writestr(f"{archive_folder}/{filename}", contents)
                        count += 1
                    exported_run_ids_by_folder.setdefault(folder, set()).add(run_id)
        except (OSError, ValueError, csv.Error) as error:
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
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right", padx=(0, 8))

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


class TestStandApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Senior Design Test Stand — Simulation Prototype")
        self.minsize(1040, 720)
        self.geometry("1240x820")
        self.configure(background="#f1f5f9")

        self.paths = AppPaths.create(Path(__file__).resolve().parent)
        self.logger = CsvLogger(self.paths.logs)
        self.is_running = False
        self.current_live_run_id: str | None = None
        self.simulation_elapsed = 0.0
        self.last_tick = time.monotonic()
        self.last_sensor_log = 0.0
        self.sensor_log_interval_seconds = DEFAULT_SENSOR_LOG_SECONDS
        self.pressure = 35.0
        self.temperature_1 = 72.0
        self.temperature_2 = 73.0
        self.temperature_3 = 71.5
        self.temperature_4 = 72.5
        self.cycles = 0
        self.cycle_state = "waiting_for_retracted"
        self.cycle_times: deque[float] = deque()
        self.grease_interval_seconds = 10 * 60.0
        self.next_grease_time_seconds = self.grease_interval_seconds
        self.grease_phase = "Idle (simulated)"
        self.grease_started = 0.0
        self.vent_started = 0.0
        self.pressure_at_vent = 35.0
        self.grease_outcome = "Successful pressure sequence"
        self.position_calibration = self.load_position_calibration()
        self.cycle_stops = self.load_cycle_stops()
        self.ain2_monitor = LabJackAin2Monitor()
        self.labjack_available = False
        self.next_ain2_poll = 0.0
        self.last_labjack_signature = ""
        self.ain2_voltage: float | None = None
        self.bushing_position: float | None = None
        self.bushing_unit = "degrees" if self.position_calibration else "volts"
        self._display_voltage_samples: deque[float] = deque(maxlen=5)
        self.bushing_detail_var = tk.StringVar(value="Waiting for AIN2")
        self.cycle_stops_var = tk.StringVar(value=self.cycle_stops_summary())

        self._build_style()
        self._build_menu()
        self._build_interface()
        self._set_labjack_dependent_controls(False)
        self.logger.update_manifest(
            app_version=APP_VERSION,
            sensor_log_interval_seconds=self.sensor_log_interval_seconds,
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
        style.configure("TLabelframe", background="#ffffff", bordercolor="#cbd5e1")
        style.configure("TLabelframe.Label", background="#ffffff", foreground="#0f172a",
                        font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#f1f5f9", foreground="#0f172a", font=("Segoe UI", 10))

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)
        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Export Runs…", command=self.open_export_window)
        file_menu.add_command(label="Open Local Data Folder", command=self.open_local_data_folder)
        file_menu.add_command(label="Manage Local Data…", command=self.open_local_data_window)
        menu_bar.add_cascade(label="File", menu=file_menu)
        edit_menu = tk.Menu(menu_bar, tearoff=False)
        edit_menu.add_command(label="Advanced Logging…", command=self.open_advanced_logging_window)
        menu_bar.add_cascade(label="Edit", menu=edit_menu)
        self.configure(menu=menu_bar)

    def _build_interface(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 15))
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        title = ttk.Frame(header, style="Header.TFrame")
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(title, text="Senior Design Test Stand", style="Header.TLabel").pack(anchor="w")
        ttk.Label(title, text="Live bushing position from AIN2  •  Grease pressure and temperatures are simulated",
                  style="Subheader.TLabel").pack(anchor="w", pady=(3, 0))
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

        metrics = ttk.Frame(main)
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew")
        for column in range(4):
            metrics.columnconfigure(column, weight=1, uniform="metrics")
        for row in range(2):
            metrics.rowconfigure(row, weight=1)

        self.metric_vars: dict[str, tk.StringVar] = {}
        self._metric(metrics, 0, 0, "Completed Cycles", "cycles", "0", "Configured cycle stops")
        self._metric(metrics, 0, 1, "Bushing Position", "position", "Waiting…", self.bushing_detail_var)
        self._metric(metrics, 0, 2, "Cycle Frequency", "frequency", "0.0 cpm", "Rolling 60-second rate")
        self._metric(metrics, 0, 3, "Grease Pressure", "pressure", "35 psi", "Simulated")
        self._metric(metrics, 1, 0, "Temperature 1", "temp_1", "72.0 °F", "Simulated")
        self._metric(metrics, 1, 1, "Temperature 2", "temp_2", "73.0 °F", "Simulated")
        self._metric(metrics, 1, 2, "Temperature 3", "temp_3", "71.5 °F", "Simulated")
        self._metric(metrics, 1, 3, "Temperature 4", "temp_4", "72.5 °F", "Simulated")

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
        ttk.Label(controls, text="Grease every").grid(row=0, column=1, sticky="e")
        self.interval_var = tk.StringVar(value="10")
        ttk.Spinbox(controls, from_=0.1, to=100000, increment=1, textvariable=self.interval_var,
                    width=9).grid(row=0, column=2, padx=5)
        self.interval_unit_var = tk.StringVar(value="minutes")
        ttk.Combobox(
            controls, textvariable=self.interval_unit_var, state="readonly", width=9,
            values=("seconds", "minutes"),
        ).grid(row=0, column=3, sticky="w")
        ttk.Button(controls, text="Apply", command=self.apply_interval).grid(row=0, column=4, padx=(6, 18))
        self.grease_button = ttk.Button(controls, text="Grease Now", style="Grease.TButton",
                                        command=self.manual_grease)
        self.grease_button.grid(row=0, column=5, padx=(0, 12))

        ttk.Label(controls, text="Next lubrication:").grid(row=1, column=0, columnspan=2,
                                                                        sticky="w", pady=(10, 0))
        self.next_lube_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.next_lube_var, font=("Segoe UI", 10, "bold")).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(controls, text="Simulated outcome:").grid(row=1, column=4, sticky="e", pady=(10, 0))
        self.outcome_var = tk.StringVar(value=self.grease_outcome)
        outcome = ttk.Combobox(
            controls,
            textvariable=self.outcome_var,
            state="readonly",
            width=31,
            values=(
                "Successful pressure sequence",
                "Fail: pressure does not rise",
                "Fail: pressure does not reset",
            ),
        )
        outcome.grid(row=1, column=5, columnspan=2, sticky="w", padx=(8, 0), pady=(10, 0))
        self.calibrate_button = ttk.Button(controls, text="Calibrate AIN2 Position", command=self.open_calibration_window)
        self.calibrate_button.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        calibration_text = "Calibration saved" if self.position_calibration else "Calibration required"
        ttk.Label(controls, text=calibration_text, foreground="#475569").grid(
            row=2, column=2, columnspan=4, sticky="w", pady=(12, 0)
        )
        self.cycle_stops_button = ttk.Button(
            controls, text="Input Cycle Stops", command=self.open_cycle_stops_window
        )
        self.cycle_stops_button.grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(controls, textvariable=self.cycle_stops_var, foreground="#475569").grid(
            row=3, column=2, columnspan=4, sticky="w", pady=(10, 0)
        )

        chart_holder = ttk.Frame(main)
        chart_holder.grid(row=2, column=0, sticky="nsew", padx=(0, 10))
        chart_holder.columnconfigure(0, weight=1)
        chart_holder.columnconfigure(1, weight=1)
        chart_holder.rowconfigure(0, weight=1)
        self.bushing_plot = LivePlot(chart_holder, "Live AIN2 voltage (V) — calibration not set", 5.0, "#0891b2")
        self.bushing_plot.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.configure_bushing_plot(clear_samples=False)
        self.pressure_plot = LivePlot(chart_holder, "Simulated grease pressure (psi)", 5000, "#d97706")
        self.pressure_plot.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        status_box = ttk.LabelFrame(main, text="Greasing Status (Simulated)", padding=12)
        status_box.grid(row=2, column=1, sticky="nsew")
        self.grease_status_var = tk.StringVar(value=self.grease_phase)
        ttk.Label(status_box, textvariable=self.grease_status_var, wraplength=330,
                  font=("Segoe UI", 12, "bold"), foreground="#1e3a8a").pack(anchor="w")
        ttk.Label(status_box, text="Event Log", font=("Segoe UI", 10, "bold"), background="#ffffff").pack(
            anchor="w", pady=(12, 4)
        )
        self.event_log = ScrolledText(status_box, height=14, wrap="word", state="disabled",
                                      font=("Consolas", 9), background="#f8fafc", foreground="#1e293b",
                                      relief="flat")
        self.event_log.pack(fill="both", expand=True)

    def _metric(
        self, parent: ttk.Frame, row: int, column: int, label: str, key: str, initial: str, detail: str | tk.StringVar
    ) -> None:
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
        else:
            ttk.Label(frame, text=detail, style="MetricDetail.TLabel", wraplength=155).pack(anchor="w")

    def log_event(self, event: str, detail: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.log_event(event, detail, self.cycles, live_run_id=self.current_live_run_id)
        self.event_log.configure(state="normal")
        self.event_log.insert("end", f"{timestamp}  {event}\n{detail}\n\n")
        self.event_log.configure(state="disabled")
        self.event_log.see("end")

    def _run_configuration_values(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_version": APP_VERSION,
            "sensor_log_interval_seconds": self.sensor_log_interval_seconds,
            "grease_interval_seconds": self.grease_interval_seconds,
            "position_calibration": self.position_calibration,
            "cycle_stops": self.cycle_stops,
            "signals": {
                "ain2_bushing_position": "LabJack T4 live read-only input",
                "grease_pressure": "simulated",
                "temperature_1_to_4": "simulated",
                "physical_outputs": "not implemented",
            },
        }

    def _write_run_configuration_snapshot(self) -> None:
        """Preserve current settings for the full application session."""
        self.logger.write_run_configuration(self._run_configuration_values())

    def _begin_live_run(self) -> None:
        live_run = self.logger.begin_live_run(self._run_configuration_values(), self.cycles)
        self.current_live_run_id = str(live_run["id"])

    def _finish_live_run(self, status: str) -> None:
        if not self.current_live_run_id:
            return
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

    def set_sensor_log_interval(self, interval_seconds: float) -> None:
        self.sensor_log_interval_seconds = interval_seconds
        self.last_sensor_log = time.monotonic()
        self.logger.update_manifest(sensor_log_interval_seconds=interval_seconds)
        self._write_run_configuration_snapshot()
        self.log_event(
            "Sensor log rate changed",
            f"Standard dashboard snapshots now record every {interval_seconds:g} seconds.",
        )

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
        self.cycle_state = "waiting_for_retracted"
        self._begin_live_run()
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
        self._finish_live_run("stopped")

    def reset_counting(self) -> None:
        unexported_run = self._latest_unexported_live_run()
        if unexported_run:
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
            self._finish_live_run("ended_by_reset")
        self.cycles = 0
        self.cycle_times.clear()
        self.cycle_state = "waiting_for_retracted"
        self.metric_vars["cycles"].set("0")
        self.metric_vars["frequency"].set("0.0 cpm")
        if was_running:
            self._begin_live_run()
            self.log_event("Live run started", "Cycle and CPM totals were reset.")
        self.log_event(
            "Cycle counting reset",
            "Cycle and CPM totals cleared.",
        )

    def apply_interval(self) -> None:
        try:
            interval = float(self.interval_var.get())
            if interval <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid interval", "Enter a positive lubrication interval.")
            return
        unit_seconds = {"seconds": 1.0, "minutes": 60.0}
        unit = self.interval_unit_var.get()
        self.grease_interval_seconds = interval * unit_seconds[unit]
        self.next_grease_time_seconds = self.simulation_elapsed + self.grease_interval_seconds
        self._write_run_configuration_snapshot()
        self.log_event(
            "Grease interval changed",
            f"Every {interval:g} {unit} of active test time.",
        )

    def manual_grease(self) -> None:
        if self.grease_phase.startswith(("Pressurizing", "Venting")):
            self.log_event("Manual grease ignored", "A simulated grease sequence is already in progress.")
            return
        self.begin_grease(manual=True)

    def begin_grease(self, manual: bool) -> None:
        self.grease_outcome = self.outcome_var.get()
        self.grease_started = time.monotonic()
        self.grease_phase = "Pressurizing (simulated)"
        self.next_grease_time_seconds = self.simulation_elapsed + self.grease_interval_seconds
        reason = "Manual" if manual else "Automatic scheduled"
        self.log_event(
            f"{reason} grease command",
            f"Simulated command started. Selected outcome: {self.grease_outcome}.",
        )

    def _update_position(self, dt: float) -> None:
        """Keep simulated auxiliary signals advancing only while a session is active."""
        if self.is_running:
            self.simulation_elapsed += dt

    def _update_timed_grease(self) -> None:
        """Start the simulated grease sequence on active-session elapsed time, never cycles."""
        if (
            self.is_running
            and self.grease_phase.startswith("Idle")
            and self.simulation_elapsed >= self.next_grease_time_seconds
        ):
            self.begin_grease(manual=False)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, math.ceil(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds_part = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes:02d}m {seconds_part:02d}s"
        if minutes:
            return f"{minutes}m {seconds_part:02d}s"
        return f"{seconds_part}s"

    def _update_grease_pressure(self) -> None:
        now = time.monotonic()
        if self.grease_phase.startswith("Pressurizing"):
            elapsed = now - self.grease_started
            target = 3500.0 if self.grease_outcome != "Fail: pressure does not rise" else 900.0
            self.pressure = 35 + (target - 35) * (1 - math.exp(-elapsed * 1.25)) + random.uniform(-18, 18)
            if elapsed >= 3.0:
                if self.pressure < PRESSURE_REACHED_PSI:
                    self.grease_phase = "Fault: pressure did not reach threshold"
                    self.log_event("Grease fault", "Simulated pressure was below 2,500 psi; command released.")
                else:
                    self.pressure_at_vent = self.pressure
                    self.vent_started = now
                    self.grease_phase = "Venting (simulated)"
                    self.log_event("Pressure reached", "Simulated common-line pressure reached verification threshold; venting started.")
        elif self.grease_phase.startswith("Venting"):
            elapsed = now - self.vent_started
            if self.grease_outcome == "Fail: pressure does not reset":
                floor = 900.0
                decay_rate = 0.65
            else:
                floor = 35.0
                decay_rate = 1.45
            self.pressure = floor + (self.pressure_at_vent - floor) * math.exp(-elapsed * decay_rate)
            self.pressure += random.uniform(-10, 10)
            if self.pressure <= PRESSURE_RESET_PSI:
                self.grease_phase = "Idle (sequence verified)"
                self.log_event(
                    "Grease sequence complete",
                    "Simulated common-line pressure rose and reset. Individual injector delivery is not verified.",
                )
            elif elapsed >= 4.5:
                self.grease_phase = "Fault: pressure did not reset"
                self.log_event("Grease fault", "Simulated pressure remained above reset threshold after venting.")
        elif self.grease_phase.startswith(("Idle", "Fault")):
            self.pressure = max(20.0, 35.0 + random.uniform(-4.0, 4.0))

    def _frequency(self) -> float:
        now = time.monotonic()
        while self.cycle_times and now - self.cycle_times[0] > 60:
            self.cycle_times.popleft()
        if len(self.cycle_times) < 2:
            return 0.0
        period = self.cycle_times[-1] - self.cycle_times[0]
        return 60 * (len(self.cycle_times) - 1) / period if period else 0.0

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
        self._write_run_configuration_snapshot()

    def clear_position_calibration(self) -> None:
        self.paths.remove_config("position_calibration.json")
        self.position_calibration = None
        self.bushing_unit = "volts"
        self.configure_bushing_plot()
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

    def _update_cycle_count_from_bushing(self, angle: float) -> None:
        """Use direction-independent stop hysteresis to count real motion once per stroke."""
        if not self.is_running or not self.cycle_stops:
            return
        retracted, extended = self.cycle_stops["retracted"], self.cycle_stops["extended"]
        increasing = extended > retracted
        at_retracted = angle <= retracted if increasing else angle >= retracted
        at_extended = angle >= extended if increasing else angle <= extended
        if at_retracted:
            self.cycle_state = "ready_for_extension"
            return
        if not at_extended or self.cycle_state != "ready_for_extension":
            return
        self.cycle_state = "waiting_for_retracted"
        self.cycles += 1
        now = time.monotonic()
        self.cycle_times.append(now)
        self.log_event(
            "Cycle detected",
            f"Live bushing reached count stop ({extended:.3f}°) after reset stop ({retracted:.3f}°).",
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
                for raw_voltage in result.get("samples", []):
                    try:
                        voltage = float(raw_voltage)
                    except (TypeError, ValueError):
                        continue
                    self._process_counting_voltage(voltage)
                    latest_voltage = voltage
            elif result.get("status") == "disconnected":
                self._set_labjack_connection_state(
                    "disconnected", str(result.get("error", "LabJack T4 is disconnected."))
                )
            else:
                self._set_labjack_connection_state("error", str(result.get("error", "AIN2 stream failed.")))

        if latest_voltage is not None:
            self._render_live_bushing_position(statistics.fmean(self._display_voltage_samples))

    def _process_counting_voltage(self, voltage: float) -> None:
        """Feed each hardware-timed sample to the detector; never reject it for motion."""
        self._display_voltage_samples.append(voltage)
        if self.position_calibration:
            angle, outside = self.angle_from_voltage(voltage, self.position_calibration)
            if not outside:
                self._update_cycle_count_from_bushing(angle)

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
            self.bushing_plot.add(angle)
        else:
            self.bushing_position, self.bushing_unit = voltage, "volts"
            self.metric_vars["position"].set(f"{voltage:.4f} V")
            self.bushing_detail_var.set("Live AIN2 voltage • calibration required for angle")
            self.bushing_plot.add(voltage)

    def open_calibration_window(self) -> None:
        if not self.labjack_available:
            return
        PositionCalibrationDialog(self)

    def open_export_window(self, preselect_run_id: str | None = None) -> None:
        ExportDataDialog(self, preselect_run_id=preselect_run_id)

    def open_local_data_folder(self) -> None:
        try:
            os.startfile(self.paths.root)  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror("Open Local Data Folder", f"Could not open the local data folder:\n{error}", parent=self)

    def open_local_data_window(self) -> None:
        LocalDataDialog(self)

    def open_advanced_logging_window(self) -> None:
        AdvancedLoggingDialog(self)

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
            self.metric_vars["position"].set("—")
            self.bushing_detail_var.set("AIN2 unavailable")
        else:
            text, color = "LabJack T4 • Connection error", "#fca5a5"
            event, event_detail = "LabJack connection error", detail
            self.bushing_position = None
            self.ain2_voltage = None
            self.metric_vars["position"].set("—")
            self.bushing_detail_var.set("AIN2 unavailable")

        self._set_labjack_dependent_controls(state == "connected")
        self.labjack_status_var.set(text)
        self.labjack_status_label.configure(foreground=color)
        signature = f"{state}|{detail}"
        if signature != self.last_labjack_signature:
            self.last_labjack_signature = signature
            self.log_event(event, event_detail)

    def _values(self) -> dict[str, float | int | str]:
        frequency = self._frequency()
        return {
            "elapsed": self.simulation_elapsed,
            "bushing_position": "" if self.bushing_position is None else f"{self.bushing_position:.4f}",
            "bushing_position_unit": self.bushing_unit if self.bushing_position is not None else "",
            "ain2_voltage": "" if self.ain2_voltage is None else f"{self.ain2_voltage:.5f}",
            "pressure": self.pressure,
            "temp_1": self.temperature_1,
            "temp_2": self.temperature_2,
            "temp_3": self.temperature_3,
            "temp_4": self.temperature_4,
            "cycles": self.cycles,
            "frequency": frequency,
            "grease_status": self.grease_phase,
        }

    def tick(self) -> None:
        now = time.monotonic()
        dt = min(now - self.last_tick, 0.5)
        self.last_tick = now
        self._update_live_bushing_position(now)
        self._update_position(dt)
        self._update_timed_grease()
        self._update_grease_pressure()
        self.temperature_1 = 72.0 + 1.4 * math.sin(self.simulation_elapsed / 35) + random.uniform(-0.08, 0.08)
        self.temperature_2 = 73.0 + 1.1 * math.sin(self.simulation_elapsed / 41 + 0.7) + random.uniform(-0.08, 0.08)
        self.temperature_3 = 71.5 + 1.2 * math.sin(self.simulation_elapsed / 47 + 1.1) + random.uniform(-0.08, 0.08)
        self.temperature_4 = 72.5 + 0.9 * math.sin(self.simulation_elapsed / 53 + 2.0) + random.uniform(-0.08, 0.08)
        values = self._values()
        self.metric_vars["cycles"].set(str(self.cycles))
        self.metric_vars["frequency"].set(f"{values['frequency']:0.1f} cpm")
        self.metric_vars["pressure"].set(f"{self.pressure:,.0f} psi")
        self.metric_vars["temp_1"].set(f"{self.temperature_1:0.1f} °F")
        self.metric_vars["temp_2"].set(f"{self.temperature_2:0.1f} °F")
        self.metric_vars["temp_3"].set(f"{self.temperature_3:0.1f} °F")
        self.metric_vars["temp_4"].set(f"{self.temperature_4:0.1f} °F")
        self.grease_status_var.set(self.grease_phase)
        remaining = max(0.0, self.next_grease_time_seconds - self.simulation_elapsed)
        self.next_lube_var.set(f"{self._format_duration(remaining)} remaining (active test time)")
        self.pressure_plot.add(self.pressure)
        if now - self.last_sensor_log >= self.sensor_log_interval_seconds:
            self.logger.log_reading(values, live_run_id=self.current_live_run_id)
            self.last_sensor_log = now
        self.after(UPDATE_MS, self.tick)

    def on_close(self) -> None:
        if self.is_running:
            self.log_event("Live run interrupted", "Application closed while cycle counting was active.")
            self._finish_live_run("interrupted_by_application_close")
        self.log_event("Application closed", "Operator closed the simulation prototype.")
        self.ain2_monitor.close()
        self.logger.finalize(
            completed_cycles=self.cycles,
            cycle_frequency_cpm=round(self._frequency(), 2),
            final_grease_status=self.grease_phase,
        )
        self.logger.close()
        self.destroy()


if __name__ == "__main__":
    TestStandApp().mainloop()
