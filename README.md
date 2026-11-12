# Senior Design Test Stand

Windows desktop application for a LabJack T4 test stand. It reads bushing position from AIN2, grease pressure from FIO4/AIN4, and supports stop-based cycle counting, snapshots, data export, and manual PS12DC S0 control.

## Run the packaged app

1. Download and extract the [latest release](https://github.com/leonardx548/Senior-Design/releases/latest).
2. For live hardware, open `Install LabJack Driver.url`, install the [LabJack LJM Windows driver](https://support.labjack.com/docs/ljm-software-installer-windows), and connect the T4 by USB.
3. Run `Verify LabJack.bat` and confirm `Connection: SUCCESS`.
4. Run `TestStand.exe`, then optionally choose a destination under **File → Set Run Output Folder…**.

Python and application dependencies are bundled in the release. The LabJack driver remains a separate install because it supplies the signed USB driver and native library.

## Main functions

- **AIN2 position:** two-point angle calibration, live position and velocity, cycle stops, and cycle counting.
- **FIO4 / AIN4 pressure:** two-point gauge calibration and a live pressure card/graph.
- **Snapshots:** pause the position and velocity display without pausing collection; save the displayed 15-second window and matching 200 Hz CSV.
- **Data:** Start creates one live-written `Run_…` folder; Pause/Resume retains it, and Save & Reset closes it. Every detected cycle receives a CSV row; incomplete AIN2 motion captures are explicitly flagged instead of omitted. Manual ZIP export remains available.
- **PS12DC S0:** manual ON/OFF and a repeating timer; the timer runs only during active counting and Pause/Save command S0 OFF. Confirmed ON events are recorded during an active run.
- **Temperature inputs:** AIN0/AIN1 Type T temperature setup with nominal 201× mode or a known-reference gain calibration; long-term trends are available from both cards.

## Calibration

Use **Calibrate AIN2 Position** for bushing angle and **Tools → Calibrate FIO4 Pressure…** for pressure. Position calibration uses one 10-second full-travel capture: enter the angles at the low- and high-voltage endpoints, then move through the full stroke repeatedly. It rejects spikes and conditions the endpoint values. Pressure uses two steady, known points. Use **Tools → Configure Temperatures…** for the Type T probes. **Capture T4 Estimate** applies the T4 USB air-temperature estimate as a convenient nominal CJC value; a terminal-temperature measurement at the LJTick connections remains more direct. Nominal mode uses 201× and a 1.250 V offset (or a captured zero offset). True mode also requires a known-temperature reference capture for each channel.

FIO4 is T4 **AIN4**, a 0–2.5 V input. Do not connect a 5 V sensor directly. Live pressure accepts readings up to 20% beyond the captured voltage span to avoid endpoint flicker; farther readings show `OUT`.

## Run data location

Calibration and the selected-output preference are stored per computer under:

```text
%LOCALAPPDATA%\SeniorDesignTestStand\
├── config\
│   ├── position_calibration.json
│   ├── pressure_calibration.json
│   ├── temperature_calibration.json
│   └── cycle_stops.json
└── runs\                 default direct run-output folder
```

By default each round writes directly to `%LOCALAPPDATA%\SeniorDesignTestStand\runs\Run_…`. Use **File → Set Run Output Folder…** to select another root; a run is written to one location only. **File → Export Runs…** creates an optional ZIP with the selected files without removing the original run folder.

## Safety

The position and pressure inputs are read-only. The only implemented physical output is PS12DC **S0** on T4 `DIO8` / `EIO0`. It is set OFF at connection, Pause, Save, and application close. Verify supply voltage, polarity, load current, and wiring before using the output control.

## Development

Install Python 3.10+ and run `setup_windows.bat`, then start with `run_test_stand.bat`. Before a release build, run:

```powershell
python -m unittest -v test_core.py
```

Run `build_release.bat` to install build dependencies, execute the hardware-free regression suite, build the Windows package, and create the release ZIP. Do not commit local calibration files, generated `Run_…` folders, build output, or release ZIPs.
