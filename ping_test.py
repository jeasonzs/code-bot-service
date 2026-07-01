"""Standalone USB connectivity test for Code Bot.

Sends a PING frame on Vendor bulk OUT, reads back PONG + any touch events
on Vendor bulk IN for ~2 seconds, prints a summary.
"""

import sys
import time

from codebot.transport.usb import UsbTransport
from codebot.protocol import build_ping, Frame, Cmd


def main() -> int:
    t = UsbTransport()
    info = t.find()
    if info is None:
        print("FAIL: device 1a86:cb0b not found on USB bus")
        print("  - is the device plugged in?")
        print("  - is the udev rule loaded? (sudo udevadm control --reload)")
        return 1

    print(f"FOUND: VID=0x{info.vendor_id:04X} PID=0x{info.product_id:04X} "
          f"bus={info.bus} addr={info.address} product={info.product_name!r} "
          f"serial={info.serial!r}")

    if not t.open():
        print("FAIL: open() returned False (interface claim failed?)")
        return 2

    try:
        print("OPEN: claimed interfaces 0,1,2")
        n_ping_ok = 0
        n_pong_rx = 0
        n_other_rx = 0
        t_start = time.monotonic()
        deadline = t_start + 2.0
        last_print = t_start

        # Send an initial burst of 5 PINGs to make sure at least one survives
        for _ in range(5):
            if t.send_ping():
                n_ping_ok += 1

        while time.monotonic() < deadline:
            for f in t.poll(timeout_ms=20):
                if f.cmd == Cmd.PONG:
                    n_pong_rx += 1
                else:
                    n_other_rx += 1
                    print(f"  RX: cmd=0x{f.cmd:02X} payload={f.payload.hex()}")
            if time.monotonic() - last_print > 0.5:
                last_print = time.monotonic()
        elapsed = time.monotonic() - t_start

        print(f"\n--- Result ---")
        print(f"PING sent OK : {n_ping_ok} / 5")
        print(f"PONG received: {n_pong_rx}")
        print(f"other frames : {n_other_rx}")
        print(f"elapsed      : {elapsed:.2f}s")

        if n_pong_rx > 0:
            print("USB connectivity: OK (bidirectional)")
            return 0
        elif n_ping_ok == 0:
            print("USB connectivity: FAIL (could not write EP1 OUT)")
            return 3
        else:
            print("USB connectivity: PARTIAL (writes OK, no PONG — check device firmware)")
            return 4
    finally:
        t.close()
        print("CLOSED: released interfaces")


if __name__ == "__main__":
    sys.exit(main())
