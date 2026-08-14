#!/bin/bash


export DISPLAY=:0
/usr/bin/chromium-browser --password-store=basic --kiosk --noerrdialogs --disable-infobars http://localhost &

