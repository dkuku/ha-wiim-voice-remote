#!/usr/bin/env python3
"""
WiiM Voice Remote -> Home Assistant Wyoming Satellite (Push-to-Talk)

Listens to a paired WiiM Voice Remote (BLE HID, 2717:32b9) via BlueZ D-Bus,
decodes its 131-byte IMA ADPCM (16 kHz, 16-bit mono) voice packets on the fly,
and exposes a native Wyoming Protocol Satellite server for Home Assistant.
"""

import argparse
import asyncio
import json
import logging
import shutil
import ssl
import struct
import subprocess
import threading
import time
import urllib.request
from typing import Optional

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wiim-satellite")

# Standard IMA ADPCM tables
STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)
INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)

UUID_HID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
UUID_REPORT_REF = "00002908-0000-1000-8000-00805f9b34fb"


def decode_wiim_adpcm_packet(pkt: bytes) -> bytes:
    """Decode a 131-byte WiiM BLE ADPCM packet into 512 bytes of 16-bit LE PCM."""
    if len(pkt) < 4:
        return b""
    pred = struct.unpack(">h", pkt[0:2])[0]
    idx = pkt[2]
    if idx > 88:
        idx = 88

    out = bytearray((len(pkt) - 3) * 4)
    pos = 0
    for b in pkt[3:]:
        # Low nibble first (verified by packet-boundary continuity)
        for nib in (b & 0x0F, (b >> 4) & 0x0F):
            step = STEP_TABLE[idx]
            diff = step >> 3
            if nib & 1:
                diff += step >> 2
            if nib & 2:
                diff += step >> 1
            if nib & 4:
                diff += step
            if nib & 8:
                pred -= diff
            else:
                pred += diff
            if pred > 32767:
                pred = 32767
            elif pred < -32768:
                pred = -32768
            idx += INDEX_TABLE[nib]
            if idx < 0:
                idx = 0
            elif idx > 88:
                idx = 88
            struct.pack_into("<h", out, pos, pred)
            pos += 2
    return bytes(out)


async def wyoming_write_event(
    writer: asyncio.StreamWriter,
    event_type: str,
    data: Optional[dict] = None,
    payload: bytes = b"",
) -> None:
    header: dict = {"type": event_type}
    data_bytes = b""
    if data:
        data_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        header["data_length"] = len(data_bytes)
    if payload:
        header["payload_length"] = len(payload)
    line = (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")
    writer.write(line)
    if data_bytes:
        writer.write(data_bytes)
    if payload:
        writer.write(payload)
    await writer.drain()


async def wyoming_read_event(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        return None
    header = json.loads(line.decode("utf-8"))
    event_type = header.get("type")
    data = header.get("data") or {}
    data_length = int(header.get("data_length") or 0)
    payload_length = int(header.get("payload_length") or 0)
    if data_length > 0:
        raw_data = await reader.readexactly(data_length)
        data.update(json.loads(raw_data.decode("utf-8")))
    payload = b""
    if payload_length > 0:
        payload = await reader.readexactly(payload_length)
    return event_type, data, payload


class WyomingSatelliteServer:
    def __init__(self, name: str, area: Optional[str], play_audio: bool):
        self.name = name
        self.area = area
        self.play_audio = play_audio
        self.active_writer: Optional[asyncio.StreamWriter] = None
        self.streaming = False
        self.stream_start_ts = 0.0
        self.send_lock = asyncio.Lock()
        self.player_proc: Optional[subprocess.Popen] = None

    def _info_data(self) -> dict:
        return {
            "asr": [],
            "tts": [],
            "handle": [],
            "intent": [],
            "wake": [],
            "satellite": {
                "name": self.name,
                "attribution": {
                    "name": "WiiM Voice Satellite",
                    "url": "https://www.home-assistant.io/voice_control/",
                },
                "installed": True,
                "description": "WiiM Voice Remote BLE Push-to-Talk Satellite",
                "version": "1.0.0",
                "area": self.area,
                "has_vad": False,
                "active_wake_words": [],
                "max_active_wake_words": 0,
                "supports_trigger": False,
            },
        }

    def _start_player(self, rate: int, width: int, channels: int) -> None:
        self._stop_player()
        if not self.play_audio:
            return
        if shutil.which("aplay"):
            fmt = "S16_LE" if width == 2 else "U8"
            cmd = ["aplay", "-q", "-t", "raw", "-f", fmt, "-r", str(rate), "-c", str(channels)]
        elif shutil.which("paplay"):
            cmd = [
                "paplay",
                "--raw",
                f"--rate={rate}",
                f"--channels={channels}",
                "--format=s16le",
            ]
        else:
            log.warning("No aplay/paplay found; skipping TTS playback")
            return
        log.info("Playing TTS audio (%d Hz, %d ch) via %s", rate, channels, cmd[0])
        self.player_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def _write_player(self, pcm: bytes) -> None:
        if self.player_proc and self.player_proc.stdin:
            try:
                self.player_proc.stdin.write(pcm)
                self.player_proc.stdin.flush()
            except BrokenPipeError:
                pass

    def _stop_player(self) -> None:
        if self.player_proc:
            try:
                if self.player_proc.stdin:
                    self.player_proc.stdin.close()
                self.player_proc.wait(timeout=10)
            except Exception:
                self.player_proc.kill()
            finally:
                self.player_proc = None

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Wyoming client connected: %s", peer)
        is_satellite_conn = False
        try:
            while True:
                ev = await wyoming_read_event(reader)
                if ev is None:
                    break
                ev_type, data, payload = ev

                if ev_type == "describe":
                    await wyoming_write_event(writer, "info", self._info_data())
                elif ev_type == "ping":
                    await wyoming_write_event(writer, "pong", data)
                elif ev_type == "run-satellite":
                    log.info("Home Assistant attached satellite session (%s)", peer)
                    is_satellite_conn = True
                    self.active_writer = writer
                elif ev_type == "pause-satellite":
                    log.info("Satellite paused by Home Assistant")
                elif ev_type == "transcript":
                    log.info("STT Transcript: %r", data.get("text"))
                elif ev_type == "synthesize":
                    log.info("Assistant TTS: %r", data.get("text"))
                elif ev_type == "audio-start":
                    rate = int(data.get("rate", 22050))
                    width = int(data.get("width", 2))
                    channels = int(data.get("channels", 1))
                    self._start_player(rate, width, channels)
                elif ev_type == "audio-chunk":
                    self._write_player(payload)
                elif ev_type == "audio-stop":
                    await asyncio.to_thread(self._stop_player)
                    async with self.send_lock:
                        await wyoming_write_event(writer, "played")
                elif ev_type == "error":
                    log.warning("HA pipeline error: %s", data)
                else:
                    log.debug("Wyoming event: %s %s", ev_type, data)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception("Wyoming client error: %s", e)
        finally:
            if is_satellite_conn and self.active_writer is writer:
                self.active_writer = None
                self.streaming = False
            self._stop_player()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info("Wyoming client disconnected: %s", peer)

    async def on_voice_start(self) -> None:
        if not self.active_writer:
            log.warning("Voice button pressed, but Home Assistant is not connected yet!")
            return
        async with self.send_lock:
            if self.streaming:
                return
            self.streaming = True
            self.stream_start_ts = time.monotonic()
            log.info("Voice PTT START -> starting HA Assist pipeline")
            try:
                await wyoming_write_event(
                    self.active_writer,
                    "run-pipeline",
                    {
                        "start_stage": "asr",
                        "end_stage": "tts",
                        "restart_on_end": False,
                        "snd_format": {"rate": 22050, "width": 2, "channels": 1},
                    },
                )
                await wyoming_write_event(
                    self.active_writer,
                    "audio-start",
                    {"rate": 16000, "width": 2, "channels": 1, "timestamp": 0},
                )
            except Exception as e:
                log.error("Failed to start pipeline: %s", e)
                self.streaming = False

    async def on_audio_pcm(self, pcm: bytes) -> None:
        if not self.streaming or not self.active_writer or not pcm:
            return
        ts_ms = int((time.monotonic() - self.stream_start_ts) * 1000)
        async with self.send_lock:
            if not self.streaming or not self.active_writer:
                return
            try:
                await wyoming_write_event(
                    self.active_writer,
                    "audio-chunk",
                    {"rate": 16000, "width": 2, "channels": 1, "timestamp": ts_ms},
                    payload=pcm,
                )
            except Exception as e:
                log.error("Failed to send audio chunk: %s", e)
                self.streaming = False

    async def on_voice_stop(self) -> None:
        if not self.streaming or not self.active_writer:
            return
        ts_ms = int((time.monotonic() - self.stream_start_ts) * 1000)
        async with self.send_lock:
            if not self.streaming or not self.active_writer:
                return
            self.streaming = False
            log.info("Voice PTT STOP (%.2fs)", ts_ms / 1000.0)
            try:
                await wyoming_write_event(
                    self.active_writer,
                    "audio-stop",
                    {"timestamp": ts_ms},
                )
            except Exception as e:
                log.error("Failed to send audio-stop: %s", e)


class WiiMController:
    """Forwards non-voice remote buttons to a WiiM Amp Pro via local HTTPS API."""

    def __init__(self, ip: Optional[str]):
        self.ip = ip
        self._ssl_ctx = ssl._create_unverified_context()

    def _call(self, command: str) -> Optional[str]:
        if not self.ip:
            return None
        url = f"https://{self.ip}/httpapi.asp?command={command}"
        try:
            with urllib.request.urlopen(url, timeout=2.0, context=self._ssl_ctx) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            log.warning("WiiM API '%s' failed: %s", command, e)
            return None

    def send_async(self, command: str) -> None:
        if not self.ip:
            return
        log.info("WiiM -> %s", command)
        threading.Thread(target=self._call, args=(command,), daemon=True).start()

    def toggle_mute_async(self) -> None:
        if not self.ip:
            return

        def _toggle():
            raw = self._call("getPlayerStatus")
            muted = False
            if raw:
                try:
                    muted = str(json.loads(raw).get("mute", "0")) == "1"
                except Exception:
                    pass
            cmd = "setPlayerCmd:mute:0" if muted else "setPlayerCmd:mute:1"
            log.info("WiiM -> %s", cmd)
            self._call(cmd)

        threading.Thread(target=_toggle, daemon=True).start()


class WiiMRemoteBLE:
    """Subscribes to WiiM Voice Remote GATT HID reports via BlueZ D-Bus."""

    def __init__(
        self,
        mac: str,
        loop: asyncio.AbstractEventLoop,
        sat: WyomingSatelliteServer,
        wiim: WiiMController,
    ):
        self.mac = mac.upper()
        self.dev_path = f"/org/bluez/hci0/dev_{self.mac.replace(':', '_')}"
        self.loop = loop
        self.sat = sat
        self.wiim = wiim
        self.consumer_path: Optional[str] = None
        self.kbd_path: Optional[str] = None
        self.voice_path: Optional[str] = None
        self.voice_pressed = False
        self.subscribed = False

    def _discover_chars(self, bus: dbus.SystemBus) -> bool:
        om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        objs = om.GetManagedObjects()
        self.consumer_path = None
        self.kbd_path = None
        self.voice_path = None

        for path, ifaces in objs.items():
            if not path.startswith(self.dev_path):
                continue
            char = ifaces.get("org.bluez.GattCharacteristic1")
            if not char or str(char.get("UUID", "")).lower() != UUID_HID_REPORT:
                continue
            # Find its Report Reference descriptor (0x2908)
            for dpath, difaces in objs.items():
                if not dpath.startswith(path + "/"):
                    continue
                desc = difaces.get("org.bluez.GattDescriptor1")
                if desc and str(desc.get("UUID", "")).lower() == UUID_REPORT_REF:
                    try:
                        d_obj = dbus.Interface(
                            bus.get_object("org.bluez", dpath), "org.bluez.GattDescriptor1"
                        )
                        ref = bytes(d_obj.ReadValue({}))
                        if ref == b"\x01\x01":
                            self.consumer_path = path
                        elif ref == b"\x02\x01":
                            self.kbd_path = path
                        elif ref == b"\x03\x01":
                            self.voice_path = path
                    except Exception as e:
                        log.debug("Could not read descriptor %s: %s", dpath, e)

        return bool(self.consumer_path and self.voice_path)

    def _subscribe(self, bus: dbus.SystemBus) -> None:
        if not self._discover_chars(bus):
            log.warning("WiiM HID report characteristics not resolved yet")
            return
        targets = [("Consumer", self.consumer_path), ("VoiceADPCM", self.voice_path)]
        if self.kbd_path:
            targets.append(("Keyboard", self.kbd_path))
        for label, path in targets:
            try:
                c = dbus.Interface(
                    bus.get_object("org.bluez", path), "org.bluez.GattCharacteristic1"
                )
                c.StartNotify()
                log.info("Subscribed to %s (%s)", label, path)
            except Exception as e:
                if "Already notifying" not in str(e):
                    log.warning("StartNotify failed for %s: %s", label, e)
        self.subscribed = True

    def _handle_consumer_key(self, data: bytes) -> None:
        # 0x0221 (LE: 21 02) = AC Search (Voice PTT button)
        if data == b"\x21\x02":
            if not self.voice_pressed:
                self.voice_pressed = True
                asyncio.run_coroutine_threadsafe(self.sat.on_voice_start(), self.loop)
            return
        if data == b"\x00\x00":
            if self.voice_pressed:
                self.voice_pressed = False
                asyncio.run_coroutine_threadsafe(self.sat.on_voice_stop(), self.loop)
            return

        key_map = {
            b"\xe9\x00": "setPlayerCmd:vol++",
            b"\xea\x00": "setPlayerCmd:vol--",
            b"\xcd\x00": "setPlayerCmd:onepause",
            b"\xb5\x00": "setPlayerCmd:next",
            b"\xb6\x00": "setPlayerCmd:prev",
        }
        if data == b"\xe2\x00":
            self.wiim.toggle_mute_async()
        elif data in key_map:
            self.wiim.send_async(key_map[data])
        else:
            log.info("Unmapped Consumer key: %s", data.hex())

    def _handle_kbd_key(self, data: bytes) -> None:
        if len(data) < 3 or data[2] == 0:
            return
        preset_map = {
            0x1E: "MCUKeyShortClick:1",
            0x1F: "MCUKeyShortClick:2",
            0x20: "MCUKeyShortClick:3",
            0x21: "MCUKeyShortClick:4",
        }
        code = data[2]
        if code in preset_map:
            self.wiim.send_async(preset_map[code])
        else:
            log.info("Unmapped Keyboard key: 0x%02x (raw=%s)", code, data.hex())

    def run_forever(self) -> None:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()

        def on_props_changed(interface, changed, invalidated, path=None):
            if path == self.dev_path and interface == "org.bluez.Device1":
                if "Connected" in changed:
                    connected = bool(changed["Connected"])
                    log.info("WiiM Remote Connected=%s", connected)
                    if not connected:
                        self.subscribed = False
                        if self.voice_pressed:
                            self.voice_pressed = False
                            asyncio.run_coroutine_threadsafe(self.sat.on_voice_stop(), self.loop)
                if "ServicesResolved" in changed and bool(changed["ServicesResolved"]):
                    self._subscribe(bus)
                return

            if interface != "org.bluez.GattCharacteristic1" or "Value" not in changed:
                return

            data = bytes(changed["Value"])
            if path == self.consumer_path:
                self._handle_consumer_key(data)
            elif path == self.kbd_path:
                self._handle_kbd_key(data)
            elif path == self.voice_path:
                if not self.voice_pressed:
                    self.voice_pressed = True
                    asyncio.run_coroutine_threadsafe(self.sat.on_voice_start(), self.loop)
                pcm = decode_wiim_adpcm_packet(data)
                if pcm:
                    asyncio.run_coroutine_threadsafe(self.sat.on_audio_pcm(pcm), self.loop)

        bus.add_signal_receiver(
            on_props_changed,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path_keyword="path",
        )

        # Initial subscribe if already connected
        try:
            props = dbus.Interface(
                bus.get_object("org.bluez", self.dev_path), "org.freedesktop.DBus.Properties"
            )
            if bool(props.Get("org.bluez.Device1", "ServicesResolved")):
                self._subscribe(bus)
            else:
                log.info("Waiting for WiiM Remote (%s) to connect (press any button)...", self.mac)
        except Exception as e:
            log.warning("Device %s not found in BlueZ yet: %s", self.mac, e)

        GLib.MainLoop().run()


async def main_async(args) -> None:
    sat = WyomingSatelliteServer(
        name=args.name,
        area=args.area,
        play_audio=not args.no_audio,
    )
    wiim = WiiMController(args.wiim_ip)
    loop = asyncio.get_running_loop()

    ble = WiiMRemoteBLE(args.mac, loop, sat, wiim)
    t = threading.Thread(target=ble.run_forever, daemon=True)
    t.start()

    server = await asyncio.start_server(sat.handle_client, args.host, args.port)
    log.info(
        "Wyoming Satellite '%s' listening on %s:%d (WiiM MAC: %s, WiiM IP: %s)",
        args.name,
        args.host,
        args.port,
        args.mac,
        args.wiim_ip or "disabled",
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="WiiM Voice Remote Wyoming Satellite")
    parser.add_argument("--mac", default="7B:B7:0E:04:5E:6A", help="WiiM Voice Remote BLE MAC")
    parser.add_argument("--wiim-ip", default=None, help="WiiM Amp Pro IP for forwarding media/preset keys")
    parser.add_argument("--host", default="0.0.0.0", help="Wyoming TCP listen host")
    parser.add_argument("--port", type=int, default=10700, help="Wyoming TCP listen port")
    parser.add_argument("--name", default="WiiM Voice Remote", help="Satellite name in Home Assistant")
    parser.add_argument("--area", default=None, help="Optional Home Assistant area name")
    parser.add_argument("--no-audio", action="store_true", help="Disable local TTS audio playback")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
