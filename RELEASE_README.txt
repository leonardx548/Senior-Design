Senior Design Test Stand
========================

1. Extract the ZIP to a normal folder. Do not run it from inside the ZIP.
2. If using the physical T4, double-click `Install LabJack Driver.url` and install LJM.
3. Run `Verify LabJack.bat`; confirm `Connection: SUCCESS`.
4. Run `TestStand.exe`.

Live LabJack T4
---------------
The driver installer page is included as `Install LabJack Driver.url`.
`Verify LabJack.bat` keeps the diagnostic result visible after it finishes.

Python and the application dependencies are included. Do not install Python or run pip for this package.

Use Calibrate AIN2 Position for bushing position: enter the angles at the
low/high AIN2-voltage endpoints, then move through full travel during its
10-second capture. Tools > Calibrate FIO4 Pressure is for grease pressure.
FIO4/AIN4 accepts 0–2.5 V only.

Use Tools > Configure Temperatures for the Type T probes on AIN0/AIN1. Capture
T4 Estimate supplies a nominal USB air-temperature CJC value; a measurement at
the LJTick terminal connections remains more direct.

Run data is written live to:
%LOCALAPPDATA%\SeniorDesignTestStand\runs\Run_...

Use File > Set Run Output Folder... to choose a different location. Each run
is written to one location only. Start begins a test round, Pause holds it,
and Save & Reset closes the run folder and clears the dashboard. Pause and
Save stop the automatic S0 timer and command the output OFF.

Use File > Export Runs... to make an optional ZIP of selected run files.

PS12DC S0 is the only physical output. During an active run, confirmed ON
events are recorded as grease injections. Verify wiring and supply limits first.

Latest release:
https://github.com/leonardx548/Senior-Design/releases/latest
