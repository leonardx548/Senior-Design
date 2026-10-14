"""Read-only LabJack T4 connectivity diagnostic.

Run this separately from the simulation GUI:
    python labjack_diagnostic.py

It never writes to the LabJack.  The LJM open call runs in a child process so
an LJM/driver crash is reported cleanly instead of taking down the GUI.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
from typing import Any


AIN2_SAMPLE_COUNT = 25
AIN2_SAMPLE_INTERVAL_SECONDS = 0.02
LIVE_AIN2_STREAM_RATE_HZ = 200
LIVE_AIN2_STREAM_SCANS_PER_READ = 10
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


def summarize_ain2_samples(samples: list[float]) -> dict[str, Any]:
    """Summarize a stable AIN2 sample window from either supported read path."""
    if not samples:
        return {"status": "error", "error": "No AIN2 samples were received."}
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


def stream_ain2() -> int:
    """Continuously emit every AIN2 stream sample for real-time cycle counting.

    This configures only the T4's analog-input stream engine. It does not write
    outputs or command any external hardware.
    """
    handle = None
    stream_active = False
    last_status: str | None = None
    stop_requested = threading.Event()

    def watch_for_stop_request() -> None:
        """Let the GUI request eStreamStop before it starts calibration reads."""
        try:
            for line in sys.stdin:
                if line.strip().upper() == "STOP":
                    stop_requested.set()
                    return
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
                    ljm.eWriteNames(
                        handle,
                        2,
                        ["STREAM_RESOLUTION_INDEX", "STREAM_SETTLING_US"],
                        [AIN2_RESOLUTION_INDEX, AIN2_SETTLING_US],
                    )
                    ain2_address = ljm.namesToAddresses(1, ["AIN2"])[0]
                    actual_scan_rate = ljm.eStreamStart(
                        handle,
                        LIVE_AIN2_STREAM_SCANS_PER_READ,
                        1,
                        ain2_address,
                        LIVE_AIN2_STREAM_RATE_HZ,
                    )
                    stream_active = True
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
                stream_data, device_backlog, ljm_backlog = ljm.eStreamRead(handle)
                if stop_requested.is_set():
                    break
                samples = [float(value) for value in stream_data if float(value) != -9999.0]
                skipped_samples = len(stream_data) - len(samples)
                result = {
                    "status": "success",
                    "samples": samples,
                    "scan_rate_hz": actual_scan_rate,
                    "skipped_samples": skipped_samples,
                    "device_backlog": device_backlog,
                    "ljm_backlog": ljm_backlog,
                }
                print(json.dumps(result), flush=True)
                last_status = "success"
            except Exception as error:
                if stream_active:
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
