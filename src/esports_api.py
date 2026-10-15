import asyncio

import requests

ESPORTS_API_BASE_URL = "https://esports-api.lolesports.com/persisted/gw"

# Fallback IDs used when getLeagues is unavailable (verified 2026-10-15).
KNOWN_LEAGUE_IDS = {
    "worlds": "98767975604431411",
    "msi": "98767991325878492",
    "first_stand": "113464388705111224",
}
AUTH_FAILURE_STATUS_CODES = {401, 403}


class EsportsAuthError(Exception):
    """ESPORTS_API_KEY is missing or was rejected (likely rotated by Riot)."""


class EsportsApiClient:
    """Thin client for the (unofficial) LoL Esports web API that powers lolesports.com."""

    def __init__(self, *, api_key, log, locale="en-US", timeout=(5.05, 20.05)):
        self.api_key = api_key
        self.log = log
        self.locale = locale
        self.timeout = timeout
        self._league_ids = {}
        self._warned_unknown_slugs = set()

    def _get_json(self, endpoint, params=None):
        if not self.api_key:
            raise EsportsAuthError("ESPORTS_API_KEY is not set.")
        query = {"hl": self.locale}
        if params:
            query.update(params)
        response = requests.get(
            f"{ESPORTS_API_BASE_URL}/{endpoint}",
            headers={"x-api-key": self.api_key},
            params=query,
            timeout=self.timeout,
        )
        if response.status_code in AUTH_FAILURE_STATUS_CODES:
            raise EsportsAuthError(f"LoL Esports API returned HTTP {response.status_code} for {endpoint}.")
        response.raise_for_status()
        return response.json()

    async def resolve_league_ids(self, slugs):
        """Map league slugs (e.g. 'worlds') to league IDs, caching the lookup."""
        missing = [slug for slug in slugs if slug not in self._league_ids]
        if missing:
            try:
                payload = await asyncio.to_thread(self._get_json, "getLeagues")
                for league in (payload.get("data") or {}).get("leagues") or []:
                    slug = str(league.get("slug") or "").lower()
                    league_id = league.get("id")
                    if slug and league_id:
                        self._league_ids[slug] = str(league_id)
            except (requests.RequestException, ValueError) as exc:
                self.log(f"[esports] League lookup failed; using built-in IDs: {exc}")

        resolved = {}
        for slug in slugs:
            league_id = self._league_ids.get(slug) or KNOWN_LEAGUE_IDS.get(slug)
            if league_id:
                resolved[slug] = league_id
            elif slug not in self._warned_unknown_slugs:
                self._warned_unknown_slugs.add(slug)
                self.log(f"[esports] Unknown league slug '{slug}' in ESPORTS_LEAGUES; skipping.")
        return resolved

    async def fetch_schedule_events(self, league_id):
        """Return the raw schedule events page around 'now' for one league."""
        payload = await asyncio.to_thread(self._get_json, "getSchedule", {"leagueId": league_id})
        return ((payload.get("data") or {}).get("schedule") or {}).get("events") or []
