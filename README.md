# WiiM Voice Remote for Home Assistant (HACS)

Turn your **WiiM Voice Remote** (BLE HID `2717:32b9`) into a native **Home Assistant Assist** Push-to-Talk voice satellite — completely locally, without Amazon Alexa or cloud dependencies!

## Features
- **Native `assist_satellite` entity** in Home Assistant (works with any Assist Pipeline: Whisper, Piper, OpenAI, Gemini, etc.).
- **Push-to-Talk over BLE:** Hold the microphone button to speak, release to send. Decodes the remote's 131-byte IMA ADPCM (16 kHz, 16-bit mono) BLE HID Report 3 stream on the fly.
- **Auto-mute while speaking (`mute_on_voice`):** Automatically mutes the target `media_player` while holding the Voice button and restores audio before TTS playback.
- **TTS Playback on your WiiM / Media Player:** Optionally route Assistant TTS responses directly to any `media_player` entity (e.g. your WiiM Amp Pro via `mjcumming/wiim`, Amazon Echo, etc.).
- **Media & Preset Key Forwarding:** Volume +/-, Mute, Play/Pause, Next, Previous are forwarded to the configured `media_player`, and Presets 1–4 can trigger `MCUKeyShortClick:1..4` on your WiiM IP.
- **Home Assistant Events:** Every button press also fires a `wiim_voice_remote_button` event on the HA event bus for custom automations.
- **Companion Wyoming Satellite (`companion/`):** If your Home Assistant host does not have Bluetooth in the room with the remote, run the standalone Linux Wyoming Satellite daemon included in `companion/`.

## Compatibility
- **Tested with:** **WiiM Amp Pro** + **WiiM Voice Remote** (`2717:32b9`, Belon/Realtek BLE HID).
- **Expected to work with:** Any WiiM Voice Remote shipped with WiiM Mini, Pro, Pro Plus, Amp, Amp Pro, or Ultra (as well as standalone usage with any Home Assistant `media_player`).

---

## Installation via HACS

1. Push this repository to GitHub (or use your fork URL).
2. In Home Assistant, open **HACS → Integrations → ⋮ → Custom repositories**.
3. Add the repository URL and select **Integration** as the category.
4. Search for **WiiM Voice Remote**, click **Download**, and restart Home Assistant.
5. Pair your WiiM Voice Remote to your Home Assistant Linux host via Bluetooth (hold **Source + Mute** for ~2–3 seconds to enter pairing mode).
6. Go to **Settings → Devices & Services → Add Integration → WiiM Voice Remote**:
   - Enter the remote's **Bluetooth MAC address** (auto-filled if discovered),
   - Optionally select a **Target Media Player** (for TTS playback and Volume/Play/Next/Prev forwarding),
   - Optionally enter your **WiiM IP address** (for Preset 1–4 buttons).

---

## Standalone Linux Companion (when HA is on another host)

If Home Assistant runs on a different machine without Bluetooth in range of the remote, use the standalone Wyoming Satellite in [`companion/wiim_wyoming_satellite.py`](companion/wiim_wyoming_satellite.py):

```bash
python3 companion/wiim_wyoming_satellite.py --mac <REMOTE_MAC> --wiim-ip <WIIM_IP> --port 10700
```

Then in Home Assistant: **Settings → Devices & Services → Add Integration → Wyoming Protocol** and point it to `<LINUX_IP>:10700`.

To prevent the Linux host desktop from reacting to the remote's HID Volume/Keyboard keys, install [`companion/99-wiim-remote.rules`](companion/99-wiim-remote.rules) to `/etc/udev/rules.d/`.

---

## Protocol Notes (Reverse-Engineered)
- **Device:** WiiM Voice Remote (`2717:32b9`, BLE HID over GATT `0x1812`)
- **HID Report 1 (`01 01`, Consumer Control):**
  - `21 02` (`0x0221`) = Voice Push-to-Talk pressed, `00 00` = released
  - `e9 00` / `ea 00` = Volume+ / Volume-, `e2 00` = Mute, `cd 00` = Play/Pause, `b5 00` / `b6 00` = Next / Prev
- **HID Report 2 (`02 01`, Keyboard):** `0x1e..0x21` = Presets 1–4
- **HID Report 3 (`03 01`, Vendor Audio):** 131-byte packets (~62.5 Hz) = `>h` (big-endian int16 IMA ADPCM predictor) + `B` (uint8 step_index) + 128 bytes 4-bit IMA ADPCM (low nibble first) → 256 samples @ 16 kHz 16-bit mono PCM.
