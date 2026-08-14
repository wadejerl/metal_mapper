#!/bin/bash

. /home/rtk/rtkvenv/bin/activate
python /home/rtk/base_station.py -p /dev/ttyACM0 -w
