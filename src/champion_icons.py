"""Optional inline champion portraits using Discord application-owned emojis."""

import asyncio
import json
import re
import time

import discord
import requests


CHAMPION_ICONS_STATE_KEY = "champion_icons_enabled"
CDN = "https://ddragon.leagueoflegends.com"
EMOJI_CACHE_SECONDS = 300
CATALOG_CACHE_SECONDS = 86400
RETRY_SECONDS = 300


def champion_icons_enabled(raw):
    return str(raw or "").strip().casefold() in {"1", "true", "on"}


def _download(url, limit):
    with requests.get(url, timeout=(3, 8), stream=True) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(8192):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("Champion asset exceeds size limit")
        return bytes(data)


def _load_catalog():
    versions = json.loads(_download(f"{CDN}/api/versions.json", 64 * 1024))
    version = versions[0]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid Data Dragon version")
    data = json.loads(_download(f"{CDN}/cdn/{version}/data/en_US/champion.json", 2 * 1024 * 1024))
    icons = {}
    for champion in data["data"].values():
        filename = champion["image"]["full"]
        if not re.fullmatch(r"[A-Za-z0-9_]+\.png", filename):
            continue
        # Numeric IDs avoid discrepancies such as FiddleSticks vs Fiddlesticks.
        icons[int(champion["key"])] = f"{CDN}/cdn/{version}/img/champion/{filename}"
    return icons


class ChampionIcons:
    def __init__(self, client, log):
        self.client = client
        self.log = log
        self._lock = asyncio.Lock()
        self._emojis = {}
        self._emoji_expires = 0
        self._catalog = {}
        self._catalog_expires = 0
        self._retry_at = 0

    async def get(self, champion_id):
        try:
            champion_id = int(champion_id)
        except (TypeError, ValueError):
            return None
        if champion_id <= 0:
            return None
        name = f"rw_champ_{champion_id}"
        async with self._lock:
            now = time.monotonic()
            if now < self._emoji_expires and name in self._emojis:
                return self._emojis[name]
            if now < self._retry_at:
                return None
            try:
                return await asyncio.wait_for(self._resolve(champion_id, name), timeout=10)
            except (discord.HTTPException, requests.RequestException, TimeoutError,
                    ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                # A timed-out create may still succeed remotely; re-fetch before
                # trying another upload. Fall back to text without losing recaps.
                self._emoji_expires = 0
                self._retry_at = time.monotonic() + RETRY_SECONDS
                self.log(f"[champicons] Using champion names; retry in {RETRY_SECONDS}s: {type(exc).__name__}")
                return None

    async def _resolve(self, champion_id, name):
        if time.monotonic() >= self._emoji_expires:
            emojis = await self.client.fetch_application_emojis()
            self._emojis = {emoji.name: str(emoji) for emoji in emojis if emoji.name.startswith("rw_champ_")}
            self._emoji_expires = time.monotonic() + EMOJI_CACHE_SECONDS
        if name in self._emojis:
            return self._emojis[name]
        if time.monotonic() >= self._catalog_expires:
            self._catalog = await asyncio.to_thread(_load_catalog)
            self._catalog_expires = time.monotonic() + CATALOG_CACHE_SECONDS
        url = self._catalog.get(champion_id)
        if url is None:
            return None
        image = await asyncio.to_thread(_download, url, 256 * 1024)
        emoji = await self.client.create_application_emoji(name=name, image=image)
        self._emojis[name] = str(emoji)
        return self._emojis[name]
