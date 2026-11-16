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
- **Data:** Create or select a campaign before starting. Each Start creates one live-written dated `Run_…` folder inside that campaign; Pause/Resume retains it, and End Run closes it while retaining the campaign total. Every detected cycle receives a CSV row; incomplete AIN2 motion captures are explicitly flagged instead of omitted. Manual ZIP export remains available.
- **PS12DC S0:** manual ON/OFF and a repeating timer; the timer runs only during active counting and Pause/Save command S0 OFF. Confirmed ON events are recorded during an active run.
- **Temperature inputs:** AIN0/AIN1 Type T temperature setup with nominal 201× mode or a known-reference gain calibration; long-term trends are available from both cards.

## Calibration

Use **Calibrate AIN2 Position** for bushing angle and **Tools → Calibrate FIO4 Pressure…** for pressure. Position calibration uses one 10-second full-travel capture: enter the angles at the low- and high-voltage endpoints, then move through the full stroke repeatedly. It rejects spikes and conditions the endpoint values. A bounded 10% voltage-span margin on either side keeps normal endpoint overshoot in the velocity and cycle metrics. Pressure uses two steady, known points. Use **Tools → Configure Temperatures…** for the Type T probes. **Capture T4 Estimate** applies the T4 USB air-temperature estimate as a convenient nominal CJC value; a terminal-temperature measurement at the LJTick connections remains more direct. Nominal mode uses 201× and a 1.250 V offset (or a captured zero offset). True mode also requires a known-temperature reference capture for each channel.

Use **Tools → Export Calibration Package…** to share the saved position, pressure, temperature, and cycle-stop setup without test data. **Import Calibration Package…** previews and validates that setup before replacing local settings; it is unavailable while a run is open.

FIO4 is T4 **AIN4**, a 0–2.5 V input. Do not connect a 5 V sensor directly. Live pressure accepts readings up to 20% beyond the captured voltage span to avoid endpoint flicker; farther readings show `OUT`.

## Alarms

The application keeps active faults visible in a red banner, logs each fault transition, and plays one alert tone when a new fault activates. It does not command or stop hardware. A temperature batch is withheld from temperature histories and cycle summaries after two consecutive invalid batches if it has no plausible readings (outside -40 to 400 °F) or is inconsistent; three valid batches clear the alarm. The same banner also reports T4 connection loss, AIN2 stream gaps, and position or pressure beyond their saved calibration margins.

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

Create or select a campaign from **File → Create / Select Campaign…** before Start is enabled. By default campaigns are created under `%LOCALAPPDATA%\SeniorDesignTestStand\runs\Campaign_…`; each dated run is written to that campaign's `runs\Run_…` folder. Selecting a campaign restores its saved cumulative cycle count and active test time. **End Run** closes only the current dated run; **Finish Selected Campaign** closes the campaign once testing is complete.

**File → Export Campaign Package…** creates the teammate-handoff ZIP: it includes the campaign manifest, all dated runs, snapshots, and the saved position, pressure, temperature, and cycle-stop setup. End the current run before exporting a package. On the receiving computer, select **File → Import Campaign Package…**, then approve the saved setup if it differs from that computer's local setup. The import never overwrites an existing campaign folder.

## Safety

The position and pressure inputs are read-only. The only implemented physical output is PS12DC **S0** on T4 `DIO8` / `EIO0`. It is set OFF at connection, Pause, Save, and application close. Verify supply voltage, polarity, load current, and wiring before using the output control.

## Development

Install Python 3.10+ and run `setup_windows.bat`, then start with `run_test_stand.bat`. Before a release build, run:

```powershell
python -m unittest -v test_core.py
```

Run `build_release.bat` to install build dependencies, execute the hardware-free regression suite, build the Windows package, and create the release ZIP. Do not commit local calibration files, generated `Run_…` folders, build output, or release ZIPs.
