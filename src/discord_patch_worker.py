import asyncio
import time

from src.patch_notes_logic import (
    article_keys,
    dump_seen_state,
    format_patch_post,
    load_seen_state,
    select_new_articles,
)

PATCH_NOTES_SEEN_STATE_KEY = "patch_notes_seen"
PATCH_NOTES_LAST_CHECKED_STATE_KEY = "patch_notes_last_checked_at"
# Unseen articles older than this are never posted (downtime, state reset, CMS migration).
PATCH_NOTES_MAX_AGE_SECONDS = 7 * 24 * 3600
# Articles recorded on the first (silent) observation.
BASELINE_ARTICLE_COUNT = 10
POST_SPACING_SECONDS = 2.0


async def get_last_checked_unix(db_get_state):
    raw = await asyncio.to_thread(db_get_state, PATCH_NOTES_LAST_CHECKED_STATE_KEY)
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def process_patch_notes_cycle(
    *,
    patch_client,
    channel,
    db_get_state,
    db_set_state,
    log,
    now_unix=None,
    max_age_seconds=PATCH_NOTES_MAX_AGE_SECONDS,
):
    """Fetch the patch-notes listing and post any new article. Returns the number posted.

    Raises when the listing can't be fetched/parsed or a Discord send fails, so the worker
    records an error (and the stall watchdog alerts if it keeps failing).
    """
    now_unix = int(now_unix if now_unix is not None else time.time())
    articles = await patch_client.fetch_articles()

    seen_keys = load_seen_state(await asyncio.to_thread(db_get_state, PATCH_NOTES_SEEN_STATE_KEY))
    if seen_keys is None:
        # First observation: record the current articles silently (no backlog post on deploy).
        baseline = articles[:BASELINE_ARTICLE_COUNT]
        keys = [key for article in reversed(baseline) for key in article_keys(article)]
        await asyncio.to_thread(db_set_state, PATCH_NOTES_SEEN_STATE_KEY, dump_seen_state(keys))
        await asyncio.to_thread(db_set_state, PATCH_NOTES_LAST_CHECKED_STATE_KEY, str(now_unix))
        latest = articles[0]["title"] if articles else "none"
        log(f"[patch] Baselined {len(baseline)} patch articles without posting (latest: {latest}).")
        return 0

    to_post = select_new_articles(articles, seen_keys, now_unix=now_unix, max_age_seconds=max_age_seconds)
    posted = 0
    for article in to_post:
        sections = await patch_client.fetch_article_sections(article["url"])
        messages = format_patch_post(article, sections)
        for index, message in enumerate(messages):
            if index > 0:
                await asyncio.sleep(POST_SPACING_SECONDS)
            await channel.send(message)
        # Persist after each article so a later failure can't cause a repost.
        seen_keys.extend(article_keys(article))
        await asyncio.to_thread(db_set_state, PATCH_NOTES_SEEN_STATE_KEY, dump_seen_state(seen_keys))
        posted += 1
        log(
            f"[patch] Posted {article['title']} ({len(messages)} message(s), "
            f"{sum(len(entries) for _title, entries in sections)} named changes)."
        )

    await asyncio.to_thread(db_set_state, PATCH_NOTES_LAST_CHECKED_STATE_KEY, str(now_unix))
    if not posted:
        log(f"[patch] No new patch notes (latest: {articles[0]['title']}).")
    return posted
