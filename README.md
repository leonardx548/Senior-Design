# Senior Design Test Stand

This Windows desktop application uses the physical LabJack T4 read-only AIN2 signal for bushing position and cycle counting. The dashboard also reserves a grease-pressure channel and two temperature channels for future hardware inputs; it does not generate placeholder readings for them. The pressure chart includes a purple injector-activation reference line at 2,500 psi, ready for real pressure input.

## Quick start for teammates

1. Download and extract the **Windows application ZIP** from the [latest GitHub Release](https://github.com/leonardx548/Senior-Design/releases/latest).
2. Double-click `TestStand.exe`.
3. For a computer using the physical T4, install the [official LabJack LJM Windows driver/library](https://support.labjack.com/docs/ljm-software-installer-windows) once, connect the T4, then run `TestStandLabJackDiagnostic.exe` to confirm the USB connection.

The release package includes Python and the LabJack Python wrapper; teammates do not need to install Python, create a virtual environment, or run `pip`. The official LabJack LJM Windows driver/library remains a separate installation because it supplies the signed USB driver and native hardware library. Without a connected T4, the app stays open but disables actions that require its live AIN2 feed. Inside the app, **Help → Getting Started…** shows these same steps and links.

## Development setup

The source ZIP is for development rather than ordinary operation. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/) and select **Add Python to PATH**, then double-click [setup_windows.bat](setup_windows.bat). Use [run_diagnostic.bat](run_diagnostic.bat) and [run_test_stand.bat](run_test_stand.bat) to run from source.

## Manual launch

1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/). During installation, select **Add Python to PATH**.
2. Open PowerShell in this folder.
3. Run:

   ```powershell
   python test_stand_app.py
   ```

If you used `setup_windows.bat`, run `.venv\Scripts\python.exe test_stand_app.py` instead.

## Local data and team sharing

The GitHub project folder contains source code and documentation only. Each Windows computer stores its test-stand runtime data in:

```text
%LOCALAPPDATA%\SeniorDesignTestStand\
├── config\
│   ├── position_calibration.json
│   └── cycle_stops.json
└── logs\
    └── session_YYYYMMDD_HHMMSS\
```

Calibration and cycle stops are specific to the physical test stand, so they are intentionally not committed to GitHub. The first run after this update copies any older root-folder calibration files into this local location without deleting the originals. For a portable or shared data location, set the `SENIOR_DESIGN_DATA_DIR` environment variable before launching the app.

## Automatic archives, exporting, and local data

A **live run** begins when you press **Start Counting** and ends when you press **Stop**, Reset while counting, a LabJack disconnect, or application close. Use **File → Configure Auto-Archive…** to choose an existing destination folder, included data types, and archive interval (15 minutes, 30 minutes, 1 hour, or 2 hours). Once configured, the application writes a timestamped, verified ZIP after each selected interval of active test time; paused time does not count. Stop, Reset, disconnect, and application close each write a final ZIP for the remaining partial segment.

Each automatic ZIP is first written to a temporary file and integrity-checked. Only after that succeeds is the corresponding local staging session removed. If the destination becomes unavailable or ZIP creation fails, nothing is discarded: the application records the failure and keeps the local data available for recovery.

**File → Export Runs…** remains available as a manual recovery/export tool. It lets an operator select one or more retained live runs and create a portable ZIP. Both automatic and manual archives can include:

- `cycle_summary.csv` — one measured record for each completed reset-to-count stroke;
- `grease_events.csv` — one real injector firing with its pressure response and nearby-cycle comparison;
- `events.csv` — cycle detections, connection changes, and application events;
- live-run metadata — start/end status, cycle totals, and whether it has been exported; and
- `run_configuration.json` — the calibration, cycle stops, and measurement model captured when that live run started.

The ZIP always contains a small `export_manifest.json` and keeps every selected live run inside its own folder. This makes multi-run exports safe to unzip without one run's `events.csv` overwriting another's. Automatic archives clear only their successfully archived local segment; manual exports retain local files. If automatic archiving is not configured, Reset offers the existing manual-export prompt before clearing the dashboard totals.

The application does not create a periodic dashboard-snapshot log or retain raw AIN2 samples. It uses the 200 Hz AIN2 stream while each stroke is underway, then writes only its completed-cycle metrics. This keeps long tests compact while preserving the measurements used for comparison.

## Position and velocity graph snapshots

The **Pause** button above the position and velocity charts freezes only those two chart displays, so an operator can inspect an event without interrupting the LabJack stream, cycle counting, velocity calculation, or normal run logging. It simultaneously freezes a rolling window of up to two minutes of calibrated AIN2 data at the hardware stream rate (normally 200 Hz). **Resume** returns the charts to the live view and discards that frozen copy.

While paused, **Save Snapshot…** writes a timestamped folder. If an automatic archive/output folder is configured, it is created under `snapshots` in that folder; otherwise it is created under `%LOCALAPPDATA%\SeniorDesignTestStand\snapshots`. Each folder contains:

- `position_velocity_200hz.csv` — timestamp, elapsed time, stream rate, raw AIN2 voltage, calibrated bushing angle, derived velocity, and stream-gap markers;
- `position_graph.svg` and `velocity_graph.svg` — static copies of the paused chart displays; and
- `snapshot_metadata.json` — capture duration, calibration, cycle stops, live-run ID, and file list.

Use **File → Open Local Data Folder** to inspect the stored configuration and any retained logs in Windows Explorer. **File → Manage Local Data…** lists prior completed application sessions and can permanently remove selected sessions from the local computer. It never lists the application session that is currently open, warns when a selected session contains unexported data, and does not alter exported ZIP files. Calibration and cycle stops remain managed through their own setup dialogs.

## LabJack T4 connection diagnostic

To check the connected T4 without reading sensors or changing outputs, run this separate, read-only diagnostic:

```powershell
python labjack_diagnostic.py
```

It checks that Windows can see a connected LabJack T4, reports the installed LJM Python/native-library versions, opens the T4 on USB with the official `labjack-ljm` library, prints the LJM handle information, and closes it immediately. The actual open happens in a short-lived child process so a native driver failure cannot close the dashboard. It is not tied to a particular laptop or serial number: any Windows computer with the official LabJack T4 driver and LJM library can use it.

When the dashboard is running, its live AIN2 reader is also the connection check. The header changes between **Connected** and **Connection error** from that same single read path, avoiding competing USB connections. It never writes a register or commands an output.

Until that read path confirms a connected T4, **Start Counting**, **Calibrate AIN2 Position**, and **Input Cycle Stops** are disabled. If the T4 is unplugged while counting, the app pauses counting automatically and preserves the displayed totals for inspection.

## AIN2 bushing-position calibration

Use **Calibrate AIN2 Position** in the dashboard to create a two-point calibration for the string potentiometer. Enter the known bushing angle at each of two positions, hold the mechanism stationary, and capture each point. A capture uses 100 fresh samples from the same live AIN2 stream used by the dashboard, over about half a second. It removes brief outliers and rejects a signal that is moving or too noisy. The saved calibration linearly interpolates voltage to angle; a preview warns if its voltage is outside the calibrated span.

The dashboard's **Bushing Position** card and live graph use this real AIN2 feed, not a generated cylinder-position signal. A persistent read-only helper uses the T4's hardware-timed AIN2 stream at 200 samples/second for cycle detection. Every received sample is processed in order for counting; only the displayed card and graph use a five-sample average. Before calibration, the dashboard shows measured AIN2 voltage; after calibration, it shows interpolated bushing angle.

After calibration, the **Bushing Velocity** card and graph use the same raw 200 Hz angle stream. The app fits a straight line through the newest 11 calibrated samples (about 55 ms) and uses its signed slope in degrees/second. A small ±0.25°/s deadband shows stationary motion as zero. Velocity is unavailable outside the calibrated AIN2 span. For every completed cycle, the app saves peak velocity plus the median direction-normalized velocity across the middle 90% of the stroke.

## AIN2 cycle stops and real cycle counting

After saving the angle calibration, click **Input Cycle Stops** in the main control panel. It opens a dedicated window, like the calibration utility, with **Retracted / reset stop (green)** and **Extended / count stop (red)** degree fields. You can also use its **Use Current** buttons while the mechanism is positioned at each stop, then click **Save Cycle Stops**. Both stops must be inside the calibrated angular range; calibrate across the complete mechanical travel and place the stops slightly inside the physical endpoints. The left position graph draws a green horizontal reset line and a red horizontal count line.

With **Start Cycle Counting** active, the mechanism must first reach the green reset stop; one cycle is then counted only when it reaches the red count stop. This directional two-stop hysteresis prevents repeated counts while the bushing remains near an endpoint and works whether the calibrated angle increases or decreases during extension. Stops are saved in the local configuration folder described above.

The string potentiometer must share LabJack ground: connect its two end terminals to **VS** and **GND**, and connect its wiper to **AIN2**. AIN2 is a ±10 V high-voltage T4 analog input, so the expected VS-referenced 0–5 V wiper signal is in range. The calibration file is stored in the local data folder described above.

Required dependencies are the official LabJack LJM Windows library/USB driver and the `labjack-ljm` Python package, installed for the exact Python interpreter used to run the command. The official LabJack Windows installer supplies the native library and USB driver; the Python package is installed with `python -m pip install labjack-ljm`.

## What the prototype demonstrates

- A readable operator dashboard with real AIN2 bushing position, signed velocity, real stop-based cycle detection, frequency, and reserved grease-pressure and temperature channels.
- Directional cycle detection using two saved angular stops and hysteresis—not a timer counter.
- Separate **Start Counting**, **Stop**, and **Reset** controls for cycle counting only. Start and Stop preserve the accumulated cycle total and its rolling CPM history; Reset clears both and re-arms the detector. None controls a pneumatic cylinder.
- A **Test Time** stopwatch that advances only while counting is active. It starts with a new test, pauses on Stop or disconnect, resets with Reset, and continues across automatic archive boundaries.
- Live bushing-position and grease-pressure plots. The pressure plot stays empty until its hardware input is implemented and retains a 2,500 psi injector-activation reference line.
- A compact Recent Events preview plus an **Expand…** button that opens a full-size, live-updating event terminal.
- Per-run CSV output in `%LOCALAPPDATA%\SeniorDesignTestStand\logs\session_YYYYMMDD_HHMMSS\`:
  - `cycle_summary.csv` records one row per completed reset-to-count stroke. It includes timing, configured stops, travel direction, peak and median bushing velocity, current CPM, two reserved temperature fields, any grease-event IDs that occurred during the stroke, and a bushing-data-quality indicator.
  - `grease_events.csv` records each actual injector firing separately. It captures the injection timestamp and pulse duration, pressure baseline, peak pressure and time to the 2,500 psi threshold during the first 10 seconds, and medians from the three completed cycles immediately before and after the injection. It also stores the resulting velocity and stroke-duration percentage changes for straightforward Excel or AI analysis.
  - `events.csv` records cycle detections, connection changes, and program events, with the active live-run ID when applicable.
  - `live_runs.json` defines the Start-to-Stop export boundaries and preserves each run's starting configuration.
  - `run_manifest.json` and `run_configuration.json` describe the complete application session.

## Important limits

- The AIN2 bushing-position reading and its derived cycle count are real, read-only T4 measurements. Grease pressure, solenoid commands, and both temperature channels are not yet connected or implemented. Their dashboard cards stay visible and report "Input not configured" until that hardware work is completed.
- `grease_events.csv` is ready for the future physical injector integration, but it does not create rows until that integration reports an actual firing. The application never invents grease events or pressure data.
- The diagnostic utility only opens a read-only T4 connection to identify the device; it does not read auxiliary channels or control outputs.

## Preparing a GitHub Release

Commit the source files, README, batch files, `requirements.txt`, `build_release.bat`, and `RELEASE_README.txt`; do not add `position_calibration.json`, `cycle_stops.json`, `.venv`, build folders, release ZIPs, or local logs. On the project maintainer's Windows computer, run `build_release.bat`. It creates the versioned Windows ZIP named in that script, containing `TestStand.exe`, its bundled runtime files, and the LabJack diagnostic helper. Attach that ZIP to a GitHub Release created from the matching version tag. Each computer creates its own local calibration and run-log folder on first launch.

## Next practical step when hardware arrives

Validate the actual position calibration and selected cycle stops through several full mechanical strokes. Then validate grease-pressure thresholds with the apparatus before considering any output control.
