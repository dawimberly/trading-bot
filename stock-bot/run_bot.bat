@echo off
:: Step 1: Refresh the universe list
python update_universe.py

:: Step 2: Run the scanner
python scanner.py

:: Step 3: Run the executor
python executor.py

pause