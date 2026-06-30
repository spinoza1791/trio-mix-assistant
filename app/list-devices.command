#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Please run:  bash setup.command   first."
  exit 1
fi
.venv/bin/python launcher.py --list-devices
echo
echo "Put the device NUMBER (or exact NAME) into AUDIO_DEVICE in show.conf"
