#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Please run:  bash setup.command   first."
  exit 1
fi
echo "Running the FULL app against a simulated console (no hardware needed)..."
.venv/bin/python launcher.py --rehearse
