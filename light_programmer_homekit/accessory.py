"""HomeKit accessory definitions — per-light + whole-system reachability.

Every light light-programmer manages gets a Contact Sensor named
"Communication to <light>", plus one system sensor named "Light Programmer".
Polarity follows the standard contact-sensor convention: a *reachable* light
reads as **Closed** (contact detected — the link is intact) and a lost one as
**Opened**, matching how Apple Home treats Opened as the alert state.
"""
import asyncio
import logging
import zlib

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_SENSOR

from . import __version__
from .programmer_client import ProgrammerClient

MANUFACTURER = "dongnh"
MODEL = "LightProgrammerBridge"
DEFAULT_PREFIX = "Communication to "
DEFAULT_POLL_INTERVAL = 30  # seconds between /lights polls
# Consecutive failed /lights polls required before the system sensor flips to
# Opened. Debounces a single transient timeout into a flap-free, no-notification
# blip. Recovery (back to Closed) is NOT debounced — one good poll restores it.
DEFAULT_FAIL_THRESHOLD = 3

# ContactSensorState: 1 = "not detected" → Apple Home renders "Opened";
# 0 = "detected" → "Closed". Reachable = closed, lost = opened (standard).
_OPEN = 1
_CLOSED = 0

# HomeKit Accessory IDs (AIDs). pyhap auto-assigns child AIDs by INSERTION
# ORDER (itertools.count(2), skipping 7) and never persists them, so a changed
# light set re-targets Apple Home notifications/automations. We instead assign a
# DETERMINISTIC aid per light derived from its stable light id, so the binding
# follows the light, not its position. 1 = bridge (STANDALONE_AID), 2 reserved
# for the system sensor (equals the value it received implicitly before, so
# existing system-sensor bindings are preserved). 7 is unusable in pyhap.
_BRIDGE_AID = 1
_SYSTEM_AID = 2
_AID_MIN = 8          # first per-light aid (skips reserved 1/2 and the 3..7 band)
_AID_MAX = 2 ** 31 - 1
_AID_SKIP = {7}


def _set_info(accessory: Accessory, serial: str, model: str) -> None:
    """HomeKit requires non-empty Manufacturer/Model/Serial/FirmwareRevision —
    Apple Home drops the session and shows 'No Response' otherwise."""
    info = accessory.get_service("AccessoryInformation")
    info.configure_char("Manufacturer", value=MANUFACTURER)
    info.configure_char("Model", value=model)
    info.configure_char("SerialNumber", value=serial)
    info.configure_char("FirmwareRevision", value=__version__)


def _light_aid(lid: str) -> int:
    """Map a light id to a stable aid in [_AID_MIN, _AID_MAX] via crc32.
    Deterministic across restarts and independent of insertion order."""
    span = _AID_MAX - _AID_MIN + 1
    return _AID_MIN + (zlib.crc32(lid.encode("utf-8")) % span)


def _assign_aids(light_ids) -> dict:
    """Return {light_id: aid} deterministically. Each aid is derived from the
    light id (stable across restarts, independent of set membership or /lights
    order); collisions are resolved by linear probing (+1, wrapping within the
    range and skipping reserved/forbidden aids) since pyhap raises on a duplicate
    aid. Iterate ids in sorted order so probing is itself stable."""
    used = {_BRIDGE_AID, _SYSTEM_AID} | _AID_SKIP
    out: dict = {}
    span = _AID_MAX - _AID_MIN + 1
    for lid in sorted(light_ids):
        aid = _light_aid(lid)
        while aid in used:
            aid = _AID_MIN + ((aid - _AID_MIN + 1) % span)
        used.add(aid)
        out[lid] = aid
    return out


class _ContactSensor(Accessory):
    category = CATEGORY_SENSOR

    def __init__(self, driver, display_name: str, serial: str,
                 connected: bool = True, aid: int = None):
        super().__init__(driver, display_name, aid=aid)
        _set_info(self, serial=serial, model="LightProgrammerContactSensor")
        serv = self.add_preload_service("ContactSensor")
        self.char_contact = serv.configure_char(
            "ContactSensorState", value=_CLOSED if connected else _OPEN,
        )

    def set_connected(self, connected: bool) -> None:
        # set_value only notifies Apple Home when the value actually changes,
        # so re-applying the same state every poll is cheap and spam-free.
        self.char_contact.set_value(_CLOSED if connected else _OPEN)


class _StatusBridge(Bridge):
    """Bridge that polls light-programmer's /lights and mirrors it onto the
    per-light + system Contact Sensors."""

    def __init__(self, driver, name: str, client: ProgrammerClient,
                 light_sensors: dict, system_sensor: _ContactSensor,
                 interval: int = DEFAULT_POLL_INTERVAL,
                 fail_threshold: int = DEFAULT_FAIL_THRESHOLD):
        super().__init__(driver, name)
        self.client = client
        self.light_sensors = light_sensors  # id -> _ContactSensor
        self.system_sensor = system_sensor
        self.interval = max(5, int(interval))
        # Consecutive failed polls before the system sensor is declared down.
        self.fail_threshold = max(1, int(fail_threshold))
        self._fail_count = 0
        # /lights ids we've already warned are unmonitored (need a bridge
        # restart to get a sensor). Tracked so we log once per distinct set
        # rather than every poll, but still re-warn if a *new* light appears.
        self._unmonitored_warned: set = set()

    def _apply(self, lights, reachable: bool) -> None:
        # `reachable` is the debounced system verdict decided by run(); `lights`
        # may still be None on a single (not-yet-debounced) failed poll.
        self.system_sensor.set_connected(reachable)
        if lights is None:
            # This poll failed → we can't know the per-light states. Freeze them
            # (leave each sensor at its last value); flapping them all would spam
            # notifications. set_connected() is a no-op when value is unchanged.
            return
        by_id = {l["id"]: l for l in lights if "id" in l}
        for lid, sensor in self.light_sensors.items():
            info = by_id.get(lid)
            if info is not None:
                sensor.set_connected(bool(info.get("connected", True)))
        # Lights advertised by /lights that have no sensor were added to
        # light-programmer after this bridge was built; HomeKit fixes the
        # accessory set at pairing time, so they stay unmonitored until a
        # restart. Warn once per distinct set (not every poll).
        unmonitored = {lid for lid in by_id if lid not in self.light_sensors}
        if unmonitored != self._unmonitored_warned:
            if unmonitored - self._unmonitored_warned:
                labels = ", ".join(
                    sorted(by_id[lid].get("name") or lid for lid in unmonitored)
                )
                logging.warning(
                    "%d light(s) need a bridge restart to appear: %s",
                    len(unmonitored), labels,
                )
            self._unmonitored_warned = unmonitored

    async def run(self) -> None:
        # The driver schedules this coroutine as a background task. HTTP is
        # offloaded so a slow/hung light-programmer never blocks the HAP event
        # loop (which would trip Apple Home's "No Response").
        while True:
            try:
                lights = await asyncio.to_thread(self.client.get_lights)
            except Exception as e:  # noqa: BLE001 - polling must never die
                logging.warning("lights poll failed: %s", e)
                lights = None
            if lights is None:
                # Debounce: a single transient failure shouldn't flip the system
                # sensor (one notification flap). Only declare down after
                # `fail_threshold` consecutive misses; recovery is immediate.
                self._fail_count += 1
                if self._fail_count == self.fail_threshold:
                    logging.warning(
                        "light-programmer unreachable for %d consecutive poll(s); "
                        "marking system sensor down", self._fail_count)
                reachable = self._fail_count < self.fail_threshold
            else:
                if self._fail_count >= self.fail_threshold:
                    logging.info("light-programmer reachable again; clearing system down")
                self._fail_count = 0
                reachable = True
            self._apply(lights, reachable)
            await asyncio.sleep(self.interval)


def build_bridge(driver: AccessoryDriver, name: str, client: ProgrammerClient,
                 lights: list, reachable: bool = True,
                 prefix: str = DEFAULT_PREFIX,
                 interval: int = DEFAULT_POLL_INTERVAL,
                 fail_threshold: int = DEFAULT_FAIL_THRESHOLD) -> _StatusBridge:
    """Construct the bridge with one system sensor and one sensor per light.

    `lights` is the initial /lights snapshot (seeds each sensor's value);
    `reachable` is whether that snapshot was actually fetched (vs. a fallback
    empty list when light-programmer was down at startup).
    """
    system = _ContactSensor(driver, "Light Programmer", serial="lp-system",
                            connected=reachable, aid=_SYSTEM_AID)
    valid = [e for e in lights if e.get("id")]
    aids = _assign_aids([e["id"] for e in valid])
    light_sensors: dict = {}
    for entry in valid:
        lid = entry["id"]
        label = entry.get("name") or lid
        light_sensors[lid] = _ContactSensor(
            driver, f"{prefix}{label}", serial=f"lp-light-{lid}",
            connected=bool(entry.get("connected", True)),
            aid=aids[lid],
        )

    bridge = _StatusBridge(driver, name, client, light_sensors, system, interval,
                           fail_threshold=fail_threshold)
    _set_info(bridge, serial="lp-bridge", model=MODEL)
    bridge.add_accessory(system)
    for sensor in light_sensors.values():
        bridge.add_accessory(sensor)
    return bridge
