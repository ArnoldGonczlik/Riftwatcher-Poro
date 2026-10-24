import asyncio

import requests

from src.patch_notes_logic import LEAGUE_SITE_BASE_URL, parse_patch_article_sections, parse_patch_listing

USER_AGENT = "RiftwatcherPoro/1.0 (Discord bot; patch-notes watcher)"


class PatchNotesClient:
    """Fetches League patch notes from leagueoflegends.com (no official API exists).

    Requests are plain gzip-compressed page loads (~60 KB for the listing). The site's CDN
    does not honour conditional requests, so cost is controlled by the caller's poll interval.
    """

    def __init__(self, *, locale, log, timeout=(5.05, 20.05), base_url=LEAGUE_SITE_BASE_URL):
        self.locale = str(locale or "en-us").strip().lower()
        self.log = log
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    @property
    def listing_url(self):
        return f"{self.base_url}/{self.locale}/news/tags/patch-notes/"

    def _get_text(self, url):
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    async def fetch_articles(self):
        """Return patch articles, newest first. Raises on HTTP errors or an unrecognised page."""
        html = await asyncio.to_thread(self._get_text, self.listing_url)
        return parse_patch_listing(html, base_url=self.base_url)

    async def fetch_article_sections(self, url):
        """Best effort: return parsed sections, or [] if the article can't be fetched/read."""
        try:
            html = await asyncio.to_thread(self._get_text, url)
            return parse_patch_article_sections(html)
        except Exception as exc:  # Network errors or markup changes: post without the change list.
            self.log(f"[patch] Could not read article sections from {url}: {exc}")
            return []
