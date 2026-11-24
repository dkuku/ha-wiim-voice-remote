"""Assist Satellite entity for the WiiM Voice Remote."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
import logging
import ssl
import urllib.request

from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .const import (
    CONF_MAC_ADDRESS,
    CONF_MEDIA_PLAYER,
    CONF_MUTE_ON_VOICE,
    CONF_NAME,
    CONF_WIIM_IP,
    DEFAULT_MUTE_ON_VOICE,
    DEFAULT_NAME,
    DOMAIN,
    EVENT_BUTTON,
    SIGNAL_BATTERY,
)
from .remote_ble import WiiMRemoteClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WiiM Voice Remote Assist Satellite from a config entry."""
    satellite = WiiMVoiceSatellite(hass, entry)
    async_add_entities([satellite])


class WiiMVoiceSatellite(AssistSatelliteEntity):
    """Native Home Assistant Assist Satellite backed by a WiiM Voice Remote."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:remote-tv"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._mac = entry.data[CONF_MAC_ADDRESS].upper()
        self._device_name = entry.data.get(CONF_NAME, DEFAULT_NAME)
        self._media_player: str | None = entry.options.get(
            CONF_MEDIA_PLAYER, entry.data.get(CONF_MEDIA_PLAYER)
        )
        self._wiim_ip: str | None = entry.options.get(
            CONF_WIIM_IP, entry.data.get(CONF_WIIM_IP)
        )
        self._mute_on_voice: bool = bool(
            entry.options.get(
                CONF_MUTE_ON_VOICE,
                entry.data.get(CONF_MUTE_ON_VOICE, DEFAULT_MUTE_ON_VOICE),
            )
        )
        self._muted_by_us = False

        self._unique_prefix = f"wiim_voice_remote_{self._mac.replace(':', '').lower()}"
        self._attr_unique_id = self._unique_prefix
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            name=self._device_name,
            manufacturer="WiiM / LinkPlay",
            model="WiiM Voice Remote",
        )
        if self._media_player or self._wiim_ip:
            self._attr_supported_features = AssistSatelliteEntityFeature.ANNOUNCE

        self._audio_queue: asyncio.Queue[bytes | None] | None = None
        self._pipeline_task: asyncio.Task | None = None
        self._ssl_ctx = ssl._create_unverified_context()

        self._ble = WiiMRemoteClient(
            hass=hass,
            mac_address=self._mac,
            on_voice_start=self._on_voice_start,
            on_voice_pcm=self._on_voice_pcm,
            on_voice_stop=self._on_voice_stop,
            on_button=self._on_button,
            on_battery=self._on_battery,
        )

    def _on_battery(self, level: int) -> None:
        async_dispatcher_send(
            self.hass,
            SIGNAL_BATTERY.format(mac=self._mac),
            level,
        )

    @property
    def pipeline_entity_id(self) -> str | None:
        """Return the select entity ID that picks the Assist pipeline."""
        return er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"{self._unique_prefix}-pipeline"
        )

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        return None

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        return AssistSatelliteConfiguration(
            available_wake_words=[],
            active_wake_words=[],
            max_active_wake_words=0,
        )

    async def async_set_configuration(
        self, config: AssistSatelliteConfiguration
    ) -> None:
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._ble.start()

    async def async_will_remove_from_hass(self) -> None:
        await self._async_restore_mute()
        await self._ble.async_stop()
        await super().async_will_remove_from_hass()

    async def _async_mute_for_voice(self) -> None:
        if not self._mute_on_voice or not self._media_player or self._muted_by_us:
            return
        state = self.hass.states.get(self._media_player)
        if state and state.attributes.get("is_volume_muted"):
            return
        self._muted_by_us = True
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_mute",
                {"entity_id": self._media_player, "is_volume_muted": True},
            )
        except Exception as err:
            self._muted_by_us = False
            _LOGGER.debug("Failed to mute %s: %s", self._media_player, err)

    async def _async_restore_mute(self) -> None:
        if not self._muted_by_us or not self._media_player:
            return
        self._muted_by_us = False
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_mute",
                {"entity_id": self._media_player, "is_volume_muted": False},
            )
        except Exception as err:
            _LOGGER.debug("Failed to unmute %s: %s", self._media_player, err)

    async def _audio_stream(self, queue: asyncio.Queue[bytes | None]) -> AsyncIterable[bytes]:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

    def _on_voice_start(self) -> None:
        _LOGGER.debug("WiiM Voice PTT start (%s)", self._mac)
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(None)

        self.hass.async_create_task(self._async_mute_for_voice())
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._audio_queue = queue
        self._pipeline_task = self.hass.async_create_task(
            self._run_pipeline(queue),
            name=f"wiim_voice_pipeline_{self._mac}",
        )

    def _on_voice_pcm(self, pcm: bytes) -> None:
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(pcm)

    def _on_voice_stop(self) -> None:
        _LOGGER.debug("WiiM Voice PTT stop (%s)", self._mac)
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(None)
            self._audio_queue = None

    async def _run_pipeline(self, queue: asyncio.Queue[bytes | None]) -> None:
        try:
            has_speaker = bool(self._media_player or self._wiim_ip)
            end_stage = PipelineStage.TTS if has_speaker else PipelineStage.INTENT
            await self.async_accept_pipeline_from_satellite(
                self._audio_stream(queue),
                start_stage=PipelineStage.STT,
                end_stage=end_stage,
            )
        except Exception as err:
            _LOGGER.warning("Assist pipeline failed for %s: %s", self._mac, err)
            await self._async_restore_mute()
            self.tts_response_finished()

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """Handle Assist pipeline events and play TTS on WiiM (playPromptUrl) or media_player."""
        if event.type == PipelineEventType.TTS_END and event.data:
            tts_output = event.data.get("tts_output") or {}
            media_url = tts_output.get("url")
            if media_url and (self._wiim_ip or self._media_player):
                self.hass.async_create_task(self._play_tts_url(media_url))
            else:
                self.hass.async_create_task(self._async_restore_mute())
                self.tts_response_finished()
        elif event.type in (PipelineEventType.ERROR, PipelineEventType.RUN_END):
            if event.type == PipelineEventType.ERROR:
                self.hass.async_create_task(self._async_restore_mute())
                self.tts_response_finished()
            elif not (self._wiim_ip or self._media_player):
                self.hass.async_create_task(self._async_restore_mute())

    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        """Play an announcement via WiiM playPromptUrl or the configured media_player."""
        if (self._wiim_ip or self._media_player) and announcement.media_id:
            await self._play_tts_url(announcement.media_id)

    async def _play_tts_url(self, media_url: str) -> None:
        try:
            await self._async_restore_mute()

            # Prefer native WiiM playPromptUrl when wiim_ip is configured so current
            # playback (Spotify, Tidal, AirPlay, HDMI, etc.) is NOT interrupted/replaced.
            if self._wiim_ip:
                full_url = media_url
                if full_url.startswith("/"):
                    try:
                        base_url = get_url(self.hass, prefer_external=False)
                        full_url = f"{base_url.rstrip('/')}{full_url}"
                    except Exception as err:
                        _LOGGER.debug("Could not resolve local HA URL: %s", err)
                if full_url.startswith(("http://", "https://")):
                    resp = await self.hass.async_add_executor_job(
                        self._call_wiim_http, f"playPromptUrl:{full_url}"
                    )
                    if resp is not None and resp.strip().upper() == "OK":
                        _LOGGER.debug("Played TTS via WiiM playPromptUrl (%s)", full_url)
                        return
                    _LOGGER.debug(
                        "WiiM playPromptUrl returned %r; falling back to media_player", resp
                    )

            if not self._media_player:
                return
            await self.hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": self._media_player,
                    "media_content_id": media_url,
                    "media_content_type": "music",
                    "announce": True,
                },
                blocking=True,
            )
        except Exception as err:
            _LOGGER.warning("Failed to play TTS: %s", err)
        finally:
            await self._async_restore_mute()
            self.tts_response_finished()

    def _on_button(self, button: str) -> None:
        """Fire HA event and optionally forward media/preset buttons to media_player or WiiM IP."""
        self.hass.bus.async_fire(
            EVENT_BUTTON,
            {
                "mac_address": self._mac,
                "button": button,
                "entity_id": self.entity_id,
            },
        )
        self.hass.async_create_task(self._dispatch_button(button))

    async def _dispatch_button(self, button: str) -> None:
        if button.startswith("preset_") and self._wiim_ip:
            preset_num = button.split("_", 1)[1]
            await self.hass.async_add_executor_job(
                self._call_wiim_http, f"MCUKeyShortClick:{preset_num}"
            )
            return

        if not self._media_player:
            return

        service_map = {
            "volume_up": ("volume_up", {}),
            "volume_down": ("volume_down", {}),
            "play_pause": ("media_play_pause", {}),
            "next_track": ("media_next_track", {}),
            "previous_track": ("media_previous_track", {}),
        }
        if button == "mute":
            state = self.hass.states.get(self._media_player)
            is_muted = bool(state and state.attributes.get("is_volume_muted"))
            await self.hass.services.async_call(
                "media_player",
                "volume_mute",
                {"entity_id": self._media_player, "is_volume_muted": not is_muted},
            )
        elif button in service_map:
            service, extra = service_map[button]
            await self.hass.services.async_call(
                "media_player",
                service,
                {"entity_id": self._media_player, **extra},
            )

    def _call_wiim_http(self, command: str) -> str | None:
        if not self._wiim_ip:
            return None
        url = f"https://{self._wiim_ip}/httpapi.asp?command={command}"
        try:
            with urllib.request.urlopen(url, timeout=3.0, context=self._ssl_ctx) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as err:
            _LOGGER.warning("WiiM HTTP command '%s' failed: %s", command, err)
            return None
