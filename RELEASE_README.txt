Senior Design Test Stand - Windows Application
===============================================

Start the application
---------------------
1. Extract this entire ZIP to a normal folder. Do not run the application from
   inside the ZIP archive.
2. Double-click TestStand.exe.

Physical LabJack T4 operation
-----------------------------
The application opens on any Windows computer. It does not generate
placeholder sensor values.

For live bushing position and cycle counting:
1. Install the official LabJack LJM Windows driver/library once on this
   computer. Windows administrator access may be required for the driver.
   Download page: https://support.labjack.com/docs/ljm-software-installer-windows
2. Connect the LabJack T4 by USB.
3. Double-click TestStandLabJackDiagnostic.exe and confirm it reports
   "Connection: SUCCESS".
4. Start TestStand.exe.

The Bushing Velocity card and graph become active after the two-point AIN2
position calibration is saved.

Python is already included in this package. Do not install Python or run pip.

The newest application ZIP is always on the project's Releases page:
https://github.com/leonardx548/Senior-Design/releases/latest

Automatic archives
------------------
Use File > Configure Auto-Archive... to choose an existing destination folder
and the data included in each ZIP. The application writes a verified ZIP after
each hour of active cycle-counting time and writes the remaining partial ZIP
when counting stops. It retains local data if an archive cannot be created.

Auxiliary channels
------------------
The dashboard includes one grease-pressure card and four temperature cards so
the hardware inputs have a defined operator display. Until those inputs are
implemented, each card reports "Input not configured" rather than a fake
reading. The empty grease-pressure graph includes a 2,500 psi injector-
activation reference line. Use Recent Events > Expand... for the full-size,
live-updating event terminal.

Each computer keeps its own calibration and run logs under:
%LOCALAPPDATA%\SeniorDesignTestStand
