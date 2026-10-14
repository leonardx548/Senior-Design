# Senior Design Test Stand — Interface Prototype

This is a small Windows desktop application for the senior-design bushing test stand. It uses the physical LabJack T4 read-only AIN2 signal for bushing position; the grease and temperature portions remain simulated.

## Quick start for teammates

1. Download and extract the **Windows application ZIP** from the GitHub Release page.
2. Double-click `TestStand.exe`.
3. For a computer using the physical T4, install the official LabJack LJM Windows driver/library once, connect the T4, then run `TestStandLabJackDiagnostic.exe` to confirm the USB connection.

The release package includes Python and the LabJack Python wrapper; teammates do not need to install Python, create a virtual environment, or run `pip`. The official LabJack LJM Windows driver/library remains a separate installation because it supplies the signed USB driver and native hardware library. The dashboard runs in simulation mode without that driver or a connected T4.

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

## Exporting and managing run data

The application always keeps a complete local history for each launch, including time before/after cycle counting. Within that history, a **live run** begins when you press **Start Counting** and ends when you press **Stop**, Reset while counting, a LabJack disconnect, or application close. Use **File → Export Runs…** to select one or more of those live runs and create a portable ZIP. Toggle exactly what to include:

- `sensor_readings.csv` — timestamped dashboard measurements;
- `events.csv` — cycle detections, connection changes, and simulated grease-command outcomes;
- live-run metadata — start/end status, cycle totals, and whether it has been exported; and
- `run_configuration.json` — the calibration, cycle stops, grease interval, and logging setup captured when that live run started.

The ZIP always contains a small `export_manifest.json` and keeps every selected live run inside its own folder. This makes multi-run exports safe to unzip without one run's `events.csv` overwriting another's. The original local data stays in place after export, so it remains available for a later export or inspection; the app does not delete anything automatically. If Reset would clear the dashboard totals for a live run that has not yet been exported, the app offers to open the export window first. Choosing to reset anyway still retains the local data.

Use **Edit → Advanced Logging…** (also available from the export window) to choose the standard `sensor_readings.csv` snapshot interval. It starts at **1 second** and accepts any positive interval. This controls file size only: live AIN2 is still acquired at 200 samples/second for display and cycle detection, and those raw samples are not continuously logged.

Use **File → Open Local Data Folder** to inspect the stored configuration and logs in Windows Explorer. **File → Manage Local Data…** lists prior completed application sessions and can permanently remove selected sessions from the local computer. It never lists the application session that is currently open, warns when a selected session contains unexported data, and does not alter exported ZIP files. Calibration and cycle stops remain managed through their own setup dialogs.

## LabJack T4 connection diagnostic

To check the connected T4 without reading sensors or changing outputs, run this separate, read-only diagnostic:

```powershell
python labjack_diagnostic.py
```

It checks that Windows can see a connected LabJack T4, reports the installed LJM Python/native-library versions, opens the T4 on USB with the official `labjack-ljm` library, prints the LJM handle information, and closes it immediately. The actual open happens in a short-lived child process so a native driver failure cannot close the simulation GUI. It is not tied to a particular laptop or serial number: any Windows computer with the official LabJack T4 driver and LJM library can use it.

When the dashboard is running, its live AIN2 reader is also the connection check. The header changes between **Connected** and **Connection error** from that same single read path, avoiding competing USB connections. It never writes a register or commands an output.

Until that read path confirms a connected T4, **Start Counting**, **Calibrate AIN2 Position**, and **Input Cycle Stops** are disabled. If the T4 is unplugged while counting, the app pauses counting automatically and preserves the displayed totals for inspection. Reset and the explicitly simulated grease controls remain available.

## AIN2 bushing-position calibration

Use **Calibrate AIN2 Position** in the dashboard to create a two-point calibration for the string potentiometer. Enter the known bushing angle at each of two positions, hold the mechanism stationary, and capture each point. A capture uses 100 fresh samples from the same live AIN2 stream used by the dashboard, over about half a second. It removes brief outliers and rejects a signal that is moving or too noisy. The saved calibration linearly interpolates voltage to angle; a preview warns if its voltage is outside the calibrated span.

The dashboard's **Bushing Position** card and left live graph use this real AIN2 feed, not a simulated cylinder-position signal. A persistent read-only helper uses the T4's hardware-timed AIN2 stream at 200 samples/second for cycle detection. Every received sample is processed in order for counting; only the displayed card and graph use a five-sample average. Before calibration, the dashboard shows measured AIN2 voltage; after calibration, it shows interpolated bushing angle.

## AIN2 cycle stops and real cycle counting

After saving the angle calibration, click **Input Cycle Stops** in the main control panel. It opens a dedicated window, like the calibration utility, with **Retracted / reset stop (green)** and **Extended / count stop (red)** degree fields. You can also use its **Use Current** buttons while the mechanism is positioned at each stop, then click **Save Cycle Stops**. Both stops must be inside the calibrated angular range; calibrate across the complete mechanical travel and place the stops slightly inside the physical endpoints. The left position graph draws a green horizontal reset line and a red horizontal count line.

With **Start Cycle Counting** active, the mechanism must first reach the green reset stop; one cycle is then counted only when it reaches the red count stop. This directional two-stop hysteresis prevents repeated counts while the bushing remains near an endpoint and works whether the calibrated angle increases or decreases during extension. Stops are saved in the local configuration folder described above.

The string potentiometer must share LabJack ground: connect its two end terminals to **VS** and **GND**, and connect its wiper to **AIN2**. AIN2 is a ±10 V high-voltage T4 analog input, so the expected VS-referenced 0–5 V wiper signal is in range. The calibration file is stored in the local data folder described above.

Required dependencies are the official LabJack LJM Windows library/USB driver and the `labjack-ljm` Python package, installed for the exact Python interpreter used to run the command. The official LabJack Windows installer supplies the native library and USB driver; the Python package is installed with `python -m pip install labjack-ljm`.

## What the prototype demonstrates

- A readable operator dashboard with real AIN2 bushing position, real stop-based cycle detection, frequency, simulated grease-line pressure, and four placeholder temperature channels.
- Directional cycle detection using two saved angular stops and hysteresis—not a timer counter.
- Separate **Start Counting**, **Stop**, and **Reset** controls for cycle counting only. Start and Stop preserve the accumulated cycle total and its rolling CPM history; Reset clears both and re-arms the detector. None controls a pneumatic cylinder.
- Configurable automatic grease interval based on active test-session time (seconds or minutes), plus a manual simulated grease command.
- Three selectable manual outcomes: successful pressure sequence, insufficient pressure rise, and insufficient reset after venting.
- A clearly labeled status/event log and two compact live plots.
- Per-run CSV output in `%LOCALAPPDATA%\SeniorDesignTestStand\logs\session_YYYYMMDD_HHMMSS\`:
  - `sensor_readings.csv` records timestamped readings at the configurable standard snapshot interval (one second by default), with the active live-run ID when applicable.
  - `events.csv` records cycle detections, grease commands/outcomes, and program events, with the active live-run ID when applicable.
  - `live_runs.json` defines the Start-to-Stop export boundaries and preserves each run's starting configuration.
  - `run_manifest.json` and `run_configuration.json` describe the complete application session.

## Important limits

- The AIN2 bushing-position reading and its derived cycle count are real, read-only T4 measurements. Grease pressure, solenoid commands, all four temperature channels, and grease results are simulated. The diagnostic utility only opens a read-only T4 connection to identify the device; it does not read channels or control outputs.
- The pressure values, timing, and pass/fail limits are demonstration values only; physical testing must establish the real settings.
- A successful result means the simulated *common grease-line pressure sequence* rose and reset. It does **not** verify delivery through each individual injector.

## Preparing a GitHub Release

Commit the source files, README, batch files, `requirements.txt`, `build_release.bat`, and `RELEASE_README.txt`; do not add `position_calibration.json`, `cycle_stops.json`, `.venv`, build folders, release ZIPs, or local logs. On the project maintainer's Windows computer, run `build_release.bat`. It creates `release\\SeniorDesignTestStand-v0.1.0-windows.zip`, containing `TestStand.exe`, its bundled runtime files, and the LabJack diagnostic helper. Attach that ZIP to a GitHub Release created from the matching version tag. Each computer creates its own local calibration and run-log folder on first launch.

## Next practical step when hardware arrives

Validate the actual position calibration and selected cycle stops through several full mechanical strokes. Then validate grease-pressure thresholds with the apparatus before considering any output control.
