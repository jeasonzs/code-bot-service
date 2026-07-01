"""Raw EP2 IN read — no protocol parser, just dump bytes for 5 seconds."""
import sys
import time

import usb.core, usb.util

dev = usb.core.find(idVendor=0x1A86, idProduct=0xCB0B)
if dev is None:
    print("device not found")
    sys.exit(1)
print(f"found: bus={dev.bus} addr={dev.address}")

# Detach kernel driver
try:
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
except Exception as e:
    print(f"detach iface0: {e}")
try:
    if dev.is_kernel_driver_active(1):
        dev.detach_kernel_driver(1)
except Exception as e:
    print(f"detach iface1: {e}")

dev.set_configuration()
usb.util.claim_interface(dev, 0)
usb.util.claim_interface(dev, 1)
print("claimed iface 0,1")

ep2 = None
for ep in dev.get_active_configuration()[(0, 0)]:
    if (ep.bEndpointAddress & 0x80) and (ep.bEndpointAddress == 0x82):
        ep2 = ep
        break
if ep2 is None:
    print("EP2 IN (0x82) not found!")
    sys.exit(2)

# Also send a PING via EP1 OUT to provoke a response
ep1 = None
for ep in dev.get_active_configuration()[(0, 0)]:
    if (ep.bEndpointAddress & 0x80) == 0 and ep.bEndpointAddress == 0x01:
        ep1 = ep
        break

# PING frame: magic(1) + version(1) + cmd(1) + flags(1) + length(2 LE) + CRC16
# From protocol.py: MAGIC=?, VERSION=?, CMD_PING=?
# Let me just read what's there for 5s, no send first
print("\n=== Phase 1: passive read for 2s ===")
t_end = time.monotonic() + 2.0
n = 0
while time.monotonic() < t_end:
    try:
        data = ep2.read(64, timeout=100)
        n += 1
        print(f"  [{n}] EP2 read {len(data)}B: {bytes(data).hex()}")
    except usb.core.USBTimeoutError:
        pass
    except Exception as e:
        print(f"  ERR: {e}")
        break
print(f"Phase 1 done, {n} reads")

if ep1 is not None:
    print("\n=== Phase 2: send 3 PINGs, then read 3s ===")
    # PING = empty payload. Frame: hdr(6) + payload(0) + crc16(2) = 8 bytes
    # Let me look up MAGIC/VERSION/CMD from the Python protocol
    from codebot.protocol import build_ping
    ping_bytes = build_ping().encode()
    print(f"  PING frame: {ping_bytes.hex()} ({len(ping_bytes)}B)")
    for i in range(3):
        try:
            ep1.write(ping_bytes, timeout=500)
            print(f"  sent PING #{i+1}")
        except Exception as e:
            print(f"  send ERR: {e}")
    t_end = time.monotonic() + 3.0
    n2 = 0
    while time.monotonic() < t_end:
        try:
            data = ep2.read(64, timeout=100)
            n2 += 1
            print(f"  [{n2}] EP2 read {len(data)}B: {bytes(data).hex()}")
        except usb.core.USBTimeoutError:
            pass
    print(f"Phase 2 done, {n2} reads")

# Cleanup
try: usb.util.release_interface(dev, 1)
except: pass
try: usb.util.release_interface(dev, 0)
except: pass
try: usb.util.dispose_resources(dev)
except: pass
print("\ndone")
