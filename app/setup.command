#!/bin/bash
cd "$(dirname "$0")"
echo "============================================================"
echo "  Trio Mix-Assistant - one-time setup (macOS)"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "Python 3.10+ was not found."
  echo "Install it from https://www.python.org/downloads/ (or 'brew install python'),"
  echo "then run:  bash setup.command   again."
  echo
  exit 1
fi

echo "Creating virtual environment (.venv) ..."
python3 -m venv .venv || { echo "Failed to create the virtual environment."; exit 1; }

if [ -d wheels ]; then
  echo "Installing dependencies from the bundled wheels folder (offline) ..."
  .venv/bin/python -m pip install --no-index --find-links wheels -r requirements-run.txt || exit 1
else
  echo "Installing dependencies from the internet (first time only) ..."
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements-run.txt || {
    echo; echo "Install failed. If it mentions PortAudio, run: brew install portaudio"
    echo "then run setup again."; exit 1; }
fi

echo
echo "============================================================"
echo "  Setup complete."
echo "  1) Open  show.conf  and set CONSOLE_IP and AUDIO_DEVICE"
echo "     (run:  bash list-devices.command  to find the audio device)."
echo "  2) Run:  bash start.command"
echo "  NOTE: macOS will ask for Microphone + Network permission the"
echo "        first time you run start - click Allow / enable both."
echo "============================================================"
