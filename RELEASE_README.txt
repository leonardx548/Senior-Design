Senior Design Test Stand - Windows Application
===============================================

Start the application
---------------------
1. Extract this entire ZIP to a normal folder. Do not run the application from
   inside the ZIP archive.
2. Double-click TestStand.exe.

Physical LabJack T4 operation
-----------------------------
The application will run in simulation mode on any Windows computer.

For live bushing position and cycle counting:
1. Install the official LabJack LJM Windows driver/library once on this
   computer. Windows administrator access may be required for the driver.
   Download page: https://support.labjack.com/docs/ljm-software-installer-windows
2. Connect the LabJack T4 by USB.
3. Double-click TestStandLabJackDiagnostic.exe and confirm it reports
   "Connection: SUCCESS".
4. Start TestStand.exe.

Python is already included in this package. Do not install Python or run pip.

The newest application ZIP is always on the project's Releases page:
https://github.com/leonardx548/Senior-Design/releases/latest

Each computer keeps its own calibration and run logs under:
%LOCALAPPDATA%\SeniorDesignTestStand
