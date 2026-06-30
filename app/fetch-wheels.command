#!/bin/bash
cd "$(dirname "$0")"
echo "Downloading dependency wheels for THIS Mac into ./wheels ..."
echo "(Run on a Mac WITH internet matching the FOH laptop's macOS arch +"
echo " Python version, then copy the whole folder over for offline setup.)"
python3 -m pip download -r requirements-run.txt -d wheels
echo
echo "Done. The wheels/ folder now lets setup.command install fully OFFLINE."
