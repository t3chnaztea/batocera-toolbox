#!/bin/bash
# Batocera Toolbox launcher for the PORTS menu.
# Deployed to /userdata/roms/ports/Toolbox.sh ; the python package lives at
# /userdata/roms/ports/toolbox/ . EmulationStation runs this script.
cd /userdata/roms/ports || exit 1
python3 -m toolbox
