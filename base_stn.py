from serial import Serial
import time
from pynmeagps import NMEAReader
from pynmeagps import NMEAMessage, GET, SET, POLL
msgs = []
def send_set(msgs,nmr):
  for msg in msgs:
    print (f"Sending this message {msg}: ", end="")
    print(msg.serialize())
    stream.write(msg.serialize())
    stream.flush()
    #if (msg._mode == 0):
      #print("It's a GET:", end="")
      #raw, parsed = nmr.read()
      #print (f"I got back {parsed}")
    #time.sleep(.2)

import sys

def send_reset():
  stream.write(  NMEAMessage('P','QTMSRR', SET).serialize())
  stream.flush()
  time.sleep(3) # lower no workie

def send_factory_reset():
  stream.write(  NMEAMessage('P','QTMRESTOREPAR', SET).serialize())
  stream.flush()
  time.sleep(1) # Lower no workie


  
cold_reset_msgs = [ 
  NMEAMessage('P','QTMCFGRCVRMODE', SET, payload=['W','2']),
  NMEAMessage('P','QTMCFGRTCM', SET, payload=['W','7','0','-90','07','06','1','0']),
  NMEAMessage('P','QTMCFGSVIN', SET, payload=['W','1','3600','1.5','0.0','0.0','0.0']),
  NMEAMessage('P','QTMSAVEPAR', SET),     # Good!
  ]

post_reset_msgs = [   
  NMEAMessage('P','QTMCFGMSGRATE', SET, payload=['W','PQTMSVINSTATUS','1','1']),  # do this before save par but after reboot to svin mode
  NMEAMessage('P','QTMSAVEPAR', SET),     # Good!
  ]

with Serial('/dev/cu.usbmodem59320022501', 460800, timeout=3) as stream:
  nmr = NMEAReader(stream)
  
  send_factory_reset()
  send_reset()
  sys.stdout.flush()
  print("Now beginning main program")
  send_set(cold_reset_msgs,nmr)
  send_reset()
  send_set(post_reset_msgs,nmr)
  #send_reset()
  #send_set(cold_reset_msgs,nmr)
  stream.write(  NMEAMessage('P','QTMCFGSVIN', POLL).serialize())
  
  while 1:
    raw_data, parsed_data = nmr.read()
    if parsed_data is not None:
      print(parsed_data)
    
    