"""
Exercise the live feed: connection, pushed state, clock control and the
server-authoritative guarantee that two clients see the same instant.

Start the service first:
    uvicorn pravah.api:app --port 8000

Usage:  python analysis/test_live_feed.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets

URL = "ws://localhost:8000/ws/live"


async def latest(ws, timeout: float = 1.5):
    """Most recent state frame, discarding backlog.

    The server pushes at a fixed rate, so recv() yields the oldest queued
    frame. Any check that sleeps must drain to the newest one or it reads
    state from before the command it just sent.
    """
    msg = None
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0 and msg is not None:
                return msg
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.05)))
            if m.get("type") == "state":
                msg = m
            # keep draining whatever is already buffered
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.02))
                    if m.get("type") == "state":
                        msg = m
                except asyncio.TimeoutError:
                    break
            if msg is not None:
                return msg
        except asyncio.TimeoutError:
            if msg is not None:
                return msg
            raise


async def main() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    print("connecting ...")
    async with websockets.connect(URL) as a:
        hello = json.loads(await a.recv())
        check("hello frame", hello.get("type") == "hello", f"{hello.get('services')} services, day {hello.get('day')}")
        check("feed declares itself a replay", hello.get("feed") == "replay")

        first = json.loads(await a.recv())
        check("initial state frame", first.get("type") == "state",
              f"t={first.get('t')} trains={len(first.get('trains', []))}")

        # --- seek -------------------------------------------------------
        await a.send(json.dumps({"cmd": "seek", "t": 21600}))
        f = await latest(a)
        check("seek to 06:00", abs(f["t"] - 21600) < 2, f"t={f['t']}")
        n_at_6 = len(f["trains"])
        check("11 trains at 06:00", n_at_6 == 11, f"got {n_at_6}")

        sample = f["trains"][0]
        check("state shape", all(k in sample for k in
              ("id", "type", "dir", "km", "speed", "status")), str(sample))

        # --- pause holds time -------------------------------------------
        await a.send(json.dumps({"cmd": "pause"}))
        t1 = (await latest(a))["t"]
        await asyncio.sleep(1.2)
        t2 = (await latest(a))["t"]
        check("paused clock does not advance", abs(t2 - t1) < 1, f"{t1} -> {t2}")

        # --- play advances at the set speed ------------------------------
        await a.send(json.dumps({"cmd": "speed", "speed": 600}))
        await a.send(json.dumps({"cmd": "play"}))
        t3 = (await latest(a))["t"]
        await asyncio.sleep(2.0)
        t4 = (await latest(a))["t"]
        rate = (t4 - t3) / 2.0
        check("plays at ~600x", 400 < rate < 800, f"{rate:.0f} sim-seconds per second")

        # --- second client shares the clock ------------------------------
        async with websockets.connect(URL) as b:
            json.loads(await b.recv())                 # hello
            # Compare while paused. At 600x a few milliseconds between the two
            # reads is several sim-seconds, so a running clock cannot be used
            # to test that the clock is shared.
            await a.send(json.dumps({"cmd": "pause"}))
            await asyncio.sleep(0.4)
            ta = (await latest(a))["t"]
            tb = (await latest(b))["t"]
            check("two clients share one clock", abs(ta - tb) < 0.5,
                  f"A={ta:.1f} B={tb:.1f}")

            # control from B must reach A
            await b.send(json.dumps({"cmd": "pause"}))
            await b.send(json.dumps({"cmd": "seek", "t": 43200}))
            await asyncio.sleep(0.5)
            m = await latest(a)
            seen = m["t"] if abs(m["t"] - 43200) < 60 else None
            check("control from one client reaches the other",
                  seen is not None, f"A saw t={seen}")

        await a.send(json.dumps({"cmd": "pause"}))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except OSError as e:
        print(f"could not connect to {URL}: {e}")
        print("start the service first:  uvicorn pravah.api:app --port 8000")
        sys.exit(2)
