@echo off 
echo "=== CLEARING PYTHON PACKAGE CACHE ===" 
pip cache purge 
pip install --upgrade pip 
pip install -r requirements.txt --force-reinstall --no-cache-dir 
