# Create new structure
mkdir modules -Force
mkdir strategy -Force

# Create __init__.py files
New-Item modules/__init__.py -Force
New-Item strategy/__init__.py -Force

# Move modules
Move-Item scanner.py modules/ -Force
Move-Item executor.py modules/ -Force
Move-Item kraken_executor.py modules/ -Force
Move-Item kraken_scanner.py modules/ -Force

# Move strategy files (optional)
Move-Item strategies.py strategy/ -Force
Move-Item strategy.py strategy/ -Force

Write-Host "Reorganization Complete! Please update your imports in run_all.py."