"""LabJack T4 connectivity diagnostic and shared input/output helper.

Run this separately from the simulation GUI:
    python labjack_diagnostic.py

The normal diagnostic modes never write to the LabJack.  The live-stream mode
also accepts narrowly scoped, explicit PS12DC commands for DIO8, which maps
to PS12DC S0 when the board is connected to the T4 DB15 port.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from queue import Empty, Queue
from typing import Any


AIN2_SAMPLE_COUNT = 25
AIN2_SAMPLE_INTERVAL_SECONDS = 0.02
LIVE_AIN2_STREAM_RATE_HZ = 200
LIVE_AIN2_STREAM_SCANS_PER_READ = 10
# AIN2 remains the motion-critical input. AIN0/AIN1 support the raw
# temperature-wiring check, while FIO4 (AIN4) is the low-voltage grease
# pressure input. Keeping all five on the same hardware stream avoids
# competing LabJack connections and keeps cycle counting uninterrupted.
# AIN14 is the T4's internal temperature-sensor voltage.  It must be included
# in the stream because T4 analog inputs cannot be read command-response while
# streaming.
LIVE_STREAM_CHANNELS = ("AIN0", "AIN1", "AIN2", "AIN4", "AIN14")
T4_DEVICE_TEMPERATURE_SLOPE_K_PER_VOLT = -92.6
T4_DEVICE_TEMPERATURE_OFFSET_KELVIN = 467.6
T4_USB_AIR_TEMPERATURE_OFFSET_CELSIUS = 4.3


def t4_device_temperature_celsius_from_ain14(voltage: float) -> float:
    """Convert the T4's streamed internal AIN14 voltage to device temperature."""
    value = float(voltage)
    return value * T4_DEVICE_TEMPERATURE_SLOPE_K_PER_VOLT + T4_DEVICE_TEMPERATURE_OFFSET_KELVIN - 273.15


def t4_usb_air_temperature_celsius_from_ain14(voltage: float) -> float:
    """Return LabJack's USB air-temperature estimate for nominal LJTick CJC."""
    return t4_device_temperature_celsius_from_ain14(voltage) - T4_USB_AIR_TEMPERATURE_OFFSET_CELSIUS
# A PS12DC connected to the T4 DB15 (P1) maps S0 -> EIO0/DIO8. Keep the
# helper deliberately limited to this one requested solenoid output; it must
# not become a general, unguarded digital-output command channel.
PS12DC_SOLENOID_OUTPUT = "DIO8"
# Match the command-response calibration and live stream conversion quality.
# At 200 Hz with one channel, the T4 has ample margin for resolution index 5.
AIN2_RESOLUTION_INDEX = 5
AIN2_SETTLING_US = 0
AIN2_MAX_STDDEV_VOLTS = 0.010
AIN2_MAX_SPAN_VOLTS = 0.040


def find_t4_in_windows() -> dict[str, str] | None:
    """Return a connected T4 USB PnP entry, without relying on its serial number."""
    try:
        result = subprocess.run(
            ["pnputil.exe", "/enum-devices", "/connected", "/bus", "USB"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    blocks = result.stdout.split("\n\n")
    for block in blocks:
        # LabJack's USB vendor ID is 0CD5; PID 0004 identifies the T4.
        if "VID_0CD5&PID_0004" not in block:
            continue
        details: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                details[key.strip()] = value.strip()
        return details
    return None


def native_ljm_version(ljm: Any) -> str:
    try:
        return str(ljm.readLibraryConfigS("LJM_LIBRARY_VERSION"))
    except Exception as error:  # A version read is diagnostic-only.
        return f"unavailable ({error})"


def child_probe() -> int:
    """Open the connected USB T4, read its info, then close it."""
    try:
        from labjack import ljm

        handle = ljm.openS("T4", "USB", "ANY")
        try:
            device_type, connection_type, serial, ip_address, port, max_bytes = ljm.getHandleInfo(handle)
            print(
                json.dumps(
                    {
                        "status": "connected",
                        "device_type": device_type,
                        "connection_type": connection_type,
                        "serial_number": serial,
                        "ip_address": ip_address,
                        "port_or_usb_pipe": port,
                        "max_bytes_per_mb": max_bytes,
                    }
                )
            )
        finally:
            ljm.close(handle)
        return 0
    except Exception as error:
        print(json.dumps({"status": "ljm_error", "error": f"{type(error).__name__}: {error}"}))
        return 1


def summarize_voltage_samples(samples: list[float], channel_name: str = "input") -> dict[str, Any]:
    """Summarize a stable short voltage window from the shared input stream."""
    if not samples:
        return {"status": "error", "error": f"No {channel_name} samples were received."}
    median = statistics.median(samples)
    median_absolute_deviation = statistics.median(abs(value - median) for value in samples)
    # Remove brief communication/ADC spikes while retaining ordinary T4 noise.
    outlier_limit = max(0.010, median_absolute_deviation * 4)
    inliers = [value for value in samples if abs(value - median) <= outlier_limit]
    voltage = statistics.fmean(inliers)
    standard_deviation = statistics.pstdev(inliers) if len(inliers) > 1 else 0.0
    span = max(inliers) - min(inliers)
    stable = standard_deviation <= AIN2_MAX_STDDEV_VOLTS and span <= AIN2_MAX_SPAN_VOLTS
    return {
        "status": "success" if stable else "unstable",
        "voltage": voltage,
        "standard_deviation": standard_deviation,
        "span": span,
        "sample_count": len(inliers),
        "raw_sample_count": len(samples),
        "stable": stable,
    }


def summarize_ain2_samples(samples: list[float]) -> dict[str, Any]:
    """Compatibility wrapper for the established position-calibration path."""
    return summarize_voltage_samples(samples, "AIN2")


def ain2_sample_result(ljm: Any, handle: int, count: int, interval_seconds: float) -> dict[str, Any]:
    """Return a robust short AIN2 sample burst from an already-open T4 handle."""
    samples: list[float] = []
    for index in range(count):
        samples.append(float(ljm.eReadName(handle, "AIN2")))
        if index < count - 1:
            time.sleep(interval_seconds)
    return summarize_ain2_samples(samples)


def sample_ain2() -> int:
    """Capture a calibration-quality AIN2 burst without changing T4 settings."""
    handle = None
    try:
        if not find_t4_in_windows():
            print(json.dumps({"status": "disconnected", "error": "Windows PnP did not find a connected LabJack T4 on USB."}))
            return 2
        from labjack import ljm

        handle = ljm.openS("T4", "USB", "ANY")
        ljm.eWriteNames(
            handle,
            2,
            ["AIN2_RESOLUTION_INDEX", "AIN2_SETTLING_US"],
            [AIN2_RESOLUTION_INDEX, AIN2_SETTLING_US],
        )
        # Give the newly opened command-response path a brief settle period.
        time.sleep(0.10)
        result = ain2_sample_result(ljm, handle, AIN2_SAMPLE_COUNT, AIN2_SAMPLE_INTERVAL_SECONDS)
        print(json.dumps(result))
        return 0 if result["stable"] else 2
    except Exception as error:
        print(json.dumps({"status": "error", "error": f"{type(error).__name__}: {error}"}))
        return 1
    finally:
        if handle is not None:
            try:
                ljm.close(handle)
            except Exception:
                pass


def demultiplex_stream_scans(stream_data: list[float], channels: tuple[str, ...] = LIVE_STREAM_CHANNELS) -> dict[str, list[float]]:
    """Split interleaved LJM stream scans into valid samples for each input.

    LJM returns one value for each requested channel in every scan.  The
    sentinel -9999.0 is an invalid stream value, not a voltage, so it is kept
    out of the per-channel values.  Keeping this transformation independent of
    LJM makes the stream packet format straightforward to regression-test.
    """
    values = {channel: [] for channel in channels}
    channel_count = len(channels)
    if channel_count == 0:
        return values
    for offset in range(0, len(stream_data), channel_count):
        scan = stream_data[offset:offset + channel_count]
        if len(scan) != channel_count:
            continue
        for channel, raw_value in zip(channels, scan):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value != -9999.0:
                values[channel].append(value)
    return values


def auxiliary_stream_scans(stream_data: list[float]) -> list[tuple[float | None, float | None]]:
    """Return every AIN0/AIN1 scan, retaining invalid values as ``None``.

    Unlike the compatibility sample lists, this preserves one row per hardware
    scan.  The raw-input capture can therefore show missing readings instead of
    silently closing a gap in time.
    """
    scans: list[tuple[float | None, float | None]] = []
    channel_count = len(LIVE_STREAM_CHANNELS)
    for offset in range(0, len(stream_data), channel_count):
        scan = stream_data[offset:offset + channel_count]
        if len(scan) != channel_count:
            continue
        values: list[float | None] = []
        for raw_value in scan[:2]:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = -9999.0
            values.append(None if value == -9999.0 else value)
        scans.append((values[0], values[1]))
    return scans


def ps12dc_output_request(command_text: str) -> bool | None:
    """Parse only an explicit dashboard ON/OFF request for PS12DC S0."""
    command = command_text.strip().upper().split()
    if (
        len(command) == 3
        and command[0] == "SET_OUTPUT"
        and command[1] == PS12DC_SOLENOID_OUTPUT
        and command[2] in {"0", "1"}
    ):
        return command[2] == "1"
    return None


def stream_ain2() -> int:
    """Stream inputs plus AIN14 and service explicit S0 output requests.

    Command-response writes are permitted by the T4 while an analog stream is
    active.  All writes stay on this process's existing LJM handle, avoiding a
    competing USB connection and preserving the motion stream.
    """
    handle = None
    stream_active = False
    last_status: str | None = None
    stop_requested = threading.Event()
    output_requests: Queue[bool] = Queue()

    def watch_for_stop_request() -> None:
        """Let the GUI request eStreamStop before it starts calibration reads."""
        try:
            for line in sys.stdin:
                command = line.strip().upper().split()
                if command == ["STOP"]:
                    stop_requested.set()
                    return
                request = ps12dc_output_request(line)
                if request is not None:
                    output_requests.put(request)
        except OSError:
            # Process shutdown closes stdin; the finally block below still releases the stream.
            return

    threading.Thread(target=watch_for_stop_request, daemon=True).start()
    try:
        from labjack import ljm

        while not stop_requested.is_set():
            if handle is None:
                if not find_t4_in_windows():
                    status = {"status": "disconnected", "error": "Windows PnP did not find a connected LabJack T4 on USB."}
                    if status["status"] != last_status:
                        print(json.dumps(status), flush=True)
                        last_status = status["status"]
                    time.sleep(0.25)
                    continue
                try:
                    handle = ljm.openS("T4", "USB", "ANY")
                    # PS12DC inputs are weakly on when a connected LabJack
                    # line is left as an input. Establish an explicit low
                    # state before the stream starts, so S0 always begins OFF.
                    ljm.eWriteName(handle, PS12DC_SOLENOID_OUTPUT, 0)
                    ljm.eWriteNames(
                        handle,
                        2,
                        ["STREAM_RESOLUTION_INDEX", "STREAM_SETTLING_US"],
                        [AIN2_RESOLUTION_INDEX, AIN2_SETTLING_US],
                    )
                    stream_addresses = ljm.namesToAddresses(len(LIVE_STREAM_CHANNELS), list(LIVE_STREAM_CHANNELS))[0]
                    actual_scan_rate = ljm.eStreamStart(
                        handle,
                        LIVE_AIN2_STREAM_SCANS_PER_READ,
                        len(LIVE_STREAM_CHANNELS),
                        stream_addresses,
                        LIVE_AIN2_STREAM_RATE_HZ,
                    )
                    stream_active = True
                    print(json.dumps({"status": "output", "channel": PS12DC_SOLENOID_OUTPUT, "enabled": False}), flush=True)
                    last_status = None
                except Exception as error:
                    if handle is not None:
                        try:
                            ljm.close(handle)
                        except Exception:
                            pass
                        handle = None
                    stream_active = False
                    status = {"status": "error", "error": f"{type(error).__name__}: {error}"}
                    if status["status"] != last_status:
                        print(json.dumps(status), flush=True)
                        last_status = status["status"]
                    time.sleep(0.25)
                    continue

            try:
                # Keep all LJM operations on the stream-owning thread.  The
                # command is applied before the next 50 ms stream read, which
                # keeps the manual toggle responsive without another handle.
                while True:
                    try:
                        enabled = output_requests.get_nowait()
                    except Empty:
                        break
                    try:
                        ljm.eWriteName(handle, PS12DC_SOLENOID_OUTPUT, 1 if enabled else 0)
                        print(json.dumps({"status": "output", "channel": PS12DC_SOLENOID_OUTPUT, "enabled": enabled}), flush=True)
                    except Exception as error:
                        print(json.dumps({
                            "status": "output_error",
                            "channel": PS12DC_SOLENOID_OUTPUT,
                            "enabled": enabled,
                            "error": f"{type(error).__name__}: {error}",
                        }), flush=True)
                stream_data, device_backlog, ljm_backlog = ljm.eStreamRead(handle)
                if stop_requested.is_set():
                    break
                channel_samples = demultiplex_stream_scans(stream_data)
                raw_auxiliary_scans = auxiliary_stream_scans(stream_data)
                # The dashboard's existing motion path reads ``samples``. Keep
                # that name as an AIN2-only compatibility field.
                samples = channel_samples["AIN2"]
                skipped_samples = len(raw_auxiliary_scans) - len(samples)
                # The dashboard may briefly be busy drawing or opening a dialog.
                # Stamp this batch here, beside eStreamRead, so a queued batch
                # keeps its true position on the time axis instead of being
                # squeezed into the instant that Tk later consumes it.
                batch_ended_monotonic = time.monotonic()
                batch_ended_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
                result = {
                    "status": "success",
                    "samples": samples,
                    "ain0_samples": channel_samples["AIN0"],
                    "ain1_samples": channel_samples["AIN1"],
                    "ain4_samples": channel_samples["AIN4"],
                    "ain14_samples": channel_samples["AIN14"],
                    "auxiliary_scans": raw_auxiliary_scans,
                    "scan_rate_hz": actual_scan_rate,
                    "skipped_samples": skipped_samples,
                    "device_backlog": device_backlog,
                    "ljm_backlog": ljm_backlog,
                    "batch_ended_monotonic": batch_ended_monotonic,
                    "batch_ended_at": batch_ended_at,
                }
                print(json.dumps(result), flush=True)
                last_status = "success"
            except Exception as error:
                if stream_active:
                    try:
                        ljm.eWriteName(handle, PS12DC_SOLENOID_OUTPUT, 0)
                    except Exception:
                        pass
                    try:
                        ljm.eStreamStop(handle)
                    except Exception:
                        pass
                    stream_active = False
                try:
                    ljm.close(handle)
                except Exception:
                    pass
                handle = None
                print(json.dumps({"status": "error", "error": f"{type(error).__name__}: {error}"}), flush=True)
                last_status = "error"
                time.sleep(0.25)
    finally:
        if handle is not None:
            # Leave the active PS12DC channel as output-low. The board's
            # datasheet specifies that an input state is weakly ON, so merely
            # closing the handle is not a safe substitute for this write.
            try:
                ljm.eWriteName(handle, PS12DC_SOLENOID_OUTPUT, 0)
            except Exception:
                pass
            if stream_active:
                try:
                    ljm.eStreamStop(handle)
                except Exception:
                    pass
            try:
                ljm.close(handle)
            except Exception:
                pass
def build_report() -> dict[str, Any]:
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "windows_t4": find_t4_in_windows(),
    }
    try:
        from labjack import ljm

        report["ljm_python_wrapper"] = ljm.__version__
        report["ljm_native_library"] = native_ljm_version(ljm)
    except Exception as error:
        report["ljm_dependency_error"] = f"{type(error).__name__}: {error}"

    if not report["windows_t4"]:
        report["connection"] = {
            "state": "disconnected",
            "detail": "Windows PnP did not find a connected LabJack T4 on USB.",
        }
        return report

    try:
        probe = subprocess.run(
            [sys.executable, __file__, "--child-probe"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        report["connection"] = {"state": "error", "detail": "Timed out while opening the USB T4."}
        return report

    if probe.returncode == 0:
        try:
            report["connection"] = {"state": "connected", **json.loads(probe.stdout)}
            return report
        except json.JSONDecodeError:
            report["connection"] = {
                "state": "error",
                "detail": "LJM opened the device but returned unreadable device information.",
            }
            return report

    if probe.returncode < 0 or probe.returncode in {0xC0000374, -1073740940}:
        report["connection"] = {
            "state": "error",
            "detail": f"The native LJM open call crashed (Windows exit code {probe.returncode}).",
        }
        return report

    try:
        detail = json.loads(probe.stdout).get("error", probe.stdout.strip())
    except json.JSONDecodeError:
        detail = probe.stderr.strip() or probe.stdout.strip() or f"exit code {probe.returncode}"
    report["connection"] = {"state": "error", "detail": detail}
    return report


def main() -> int:
    if "--child-probe" in sys.argv:
        return child_probe()
    if "--sample-ain2" in sys.argv:
        return sample_ain2()
    if "--stream-ain2" in sys.argv:
        return stream_ain2()

    report = build_report()
    connection = report["connection"]
    if "--json" in sys.argv:
        print(json.dumps(report))
        return 0 if connection["state"] == "connected" else 2

    print("LabJack T4 diagnostic (read-only; no channels or outputs are changed)")
    if report["windows_t4"]:
        device = report["windows_t4"]
        print(f"Windows device: {device.get('Device Description', 'LabJack T4')}")
        print(f"Driver status: {device.get('Status', 'unknown')}")
        print(f"Driver: {device.get('Driver Name', 'unknown')}")
    else:
        print("Windows device: No connected LabJack T4 was found by PnP.")
    print(f"LJM Python wrapper: {report.get('ljm_python_wrapper', 'missing')}")
    print(f"Native LJM library: {report.get('ljm_native_library', 'missing')}")

    if connection["state"] == "connected":
        print("Connection: SUCCESS")
        print(f"Device type: T4 ({connection['device_type']})")
        print(f"Connection type: USB ({connection['connection_type']})")
        print(f"Serial number: {connection['serial_number']}")
        print(f"USB pipe: {connection['port_or_usb_pipe']}")
        print(f"Maximum bytes per message block: {connection['max_bytes_per_mb']}")
        return 0
    print(f"Connection: {connection['state'].upper()} - {connection['detail']}")
    return 2




if __name__ == "__main__":
    raise SystemExit(main())
