#!/bin/bash
# Batocera Toolbox launcher for the PORTS menu.
# Deployed to /userdata/roms/ports/Toolbox.sh ; the python package lives at
# /userdata/roms/ports/toolbox/ . EmulationStation runs this script.
cd /userdata/roms/ports || exit 1
python3 -m toolbox

# ROMarr Sync drops this sentinel and exits so we restart ES *after* the Port
# is gone and the freshly synced games get scanned in. SIGKILL, not graceful:
# ES on a clean quit saves its stale in-memory gamelists, and the supervisor
# respawns it fresh either way. Kill by pidof, not pkill: Linux truncates the
# process comm to 15 chars, so the 16-char name never matches pkill's default.
if [ -f /tmp/toolbox-es-restart ]; then
    rm -f /tmp/toolbox-es-restart
    kill -9 "$(pidof emulationstation)" 2>/dev/null
fi
