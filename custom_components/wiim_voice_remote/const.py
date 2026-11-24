"""Constants for the WiiM Voice Remote integration."""

DOMAIN = "wiim_voice_remote"

CONF_MAC_ADDRESS = "mac_address"
CONF_MEDIA_PLAYER = "media_player"
CONF_WIIM_IP = "wiim_ip"
CONF_NAME = "name"
CONF_MUTE_ON_VOICE = "mute_on_voice"

DEFAULT_NAME = "WiiM Voice Remote"
DEFAULT_MUTE_ON_VOICE = True

# BLE HID & Battery GATT UUIDs
UUID_HID_SERVICE = "00001812-0000-1000-8000-00805f9b34fb"
UUID_HID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
UUID_REPORT_REF = "00002908-0000-1000-8000-00805f9b34fb"
UUID_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

SIGNAL_BATTERY = f"{DOMAIN}_battery_{{mac}}"

# Report Reference values (Report ID, Report Type=Input(1))
REPORT_REF_CONSUMER = b"\x01\x01"
REPORT_REF_KEYBOARD = b"\x02\x01"
REPORT_REF_VOICE_ADPCM = b"\x03\x01"

# HID Consumer Control key codes (little-endian 16-bit)
KEY_VOICE_PTT = b"\x21\x02"
KEY_RELEASE = b"\x00\x00"
KEY_VOLUME_UP = b"\xe9\x00"
KEY_VOLUME_DOWN = b"\xea\x00"
KEY_MUTE = b"\xe2\x00"
KEY_PLAY_PAUSE = b"\xcd\x00"
KEY_NEXT_TRACK = b"\xb5\x00"
KEY_PREV_TRACK = b"\xb6\x00"

# HID Keyboard preset keycodes (data[2])
KBD_PRESETS = {
    0x1E: 1,
    0x1F: 2,
    0x20: 3,
    0x21: 4,
}

EVENT_BUTTON = f"{DOMAIN}_button"
