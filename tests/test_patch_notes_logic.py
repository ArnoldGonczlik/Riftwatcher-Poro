import json

import pytest

from src import patch_notes_logic as logic
from src.patch_notes_logic import PatchNotesParseError

BASE = "https://www.leagueoflegends.com"


def make_item(title, slug, published, content_id=None, description="Blurb."):
    item = {
        "title": title,
        "action": {"type": "weblink", "payload": {"url": f"/en-us/news/game-updates/{slug}"}},
        "publishedAt": published,
        "description": {"type": "html", "body": description},
    }
    if content_id:
        item["analytics"] = {"contentId": content_id}
    return item


def make_listing_html(items):
    data = {
        "props": {
            "pageProps": {
                "page": {
                    "blades": [
                        {"type": "riotbar", "loginLink": {"title": "Sign In", "action": {"payload": {"url": "/x"}}}},
                        {"type": "articleCardGrid", "fragmentId": "news", "items": items},
                    ]
                }
            }
        }
    }
    return (
        "<html><head></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
        "</body></html>"
    )


ARTICLE_HTML = """
<html><body>
<h1>League of Legends Patch 26.21 Notes</h1>
<h2>Mid-Patch Updates</h2>
<h2>Patch Highlights</h2>
<h2>Champions</h2>
<h3 class="change-title">Azir</h3>
<h3 class="change-title"><a href="#">Nunu &amp; Willump</a></h3>
<h3 class="change-title">Azir</h3>
<h3 class="something-else">Not a change</h3>
<h2>Items</h2>
<h3 class="change-title">Hubris</h3>
<h2>ARAM: Mayhem</h2>
<h2>Bugfixes &amp; QoL Changes</h2>
<h2>Upcoming Skins &amp; Chromas</h2>
<h2>Related Articles</h2>
<script>var x = "<h2>Hidden</h2>";</script>
</body></html>
"""


def test_parse_listing_returns_patch_articles_newest_first():
    html = make_listing_html(
        [
            make_item("League of Legends Patch 26.20 Notes", "patch-26-20-notes", "2026-10-06T18:00:00.000Z", "patchNote.b"),
            make_item("League of Legends Patch 26.21 Notes", "patch-26-21-notes", "2026-10-20T18:00:00.000Z", "patchNote.a"),
            make_item("New Champion Reveal", "champ-reveal", "2026-10-21T18:00:00.000Z", "articles.c"),
        ]
    )

    articles = logic.parse_patch_listing(html, base_url=BASE)

    assert [a["title"] for a in articles] == ["League of Legends Patch 26.21 Notes", "League of Legends Patch 26.20 Notes"]
    first = articles[0]
    assert first["id"] == "patchNote.a"
    assert first["url"] == f"{BASE}/en-us/news/game-updates/patch-26-21-notes/"
    assert first["description"] == "Blurb."
    assert first["published_at"] == 1792519200


def test_parse_listing_falls_back_to_url_id_and_strips_html():
    html = make_listing_html(
        [make_item("Patch 25.S1.1 Notes", "patch-25-s1-1-notes", "2025-01-08T19:00:00Z", description="<p>Big &amp; bold</p>")]
    )

    [article] = logic.parse_patch_listing(html, base_url=BASE)

    assert article["id"] == article["url"]
    assert article["description"] == "Big & bold"


def test_parse_listing_without_page_data_raises():
    with pytest.raises(PatchNotesParseError):
        logic.parse_patch_listing("<html><body>redesigned</body></html>")


def test_parse_listing_without_patch_articles_raises():
    with pytest.raises(PatchNotesParseError):
        logic.parse_patch_listing(make_listing_html([make_item("Cosplay", "cosplay", "2026-10-21T18:00:00Z")]))


def test_parse_article_sections_skips_boilerplate_and_dedupes():
    sections = logic.parse_patch_article_sections(ARTICLE_HTML)

    assert sections == [
        ("Champions", ["Azir", "Nunu & Willump"]),
        ("Items", ["Hubris"]),
        ("ARAM: Mayhem", []),
        ("Bugfixes & QoL Changes", []),
    ]


def test_format_patch_post_full():
    article = {
        "title": "League of Legends Patch 26.21 Notes",
        "url": f"{BASE}/en-us/news/game-updates/patch-26-21-notes/",
        "description": "True Damage has arrived with style!",
        "published_at": 1792519200,
    }
    sections = [("Champions", ["Azir", "Caitlyn"]), ("Items", ["Hubris"]), ("Arena", []), ("Classic", [])]

    [message] = logic.format_patch_post(article, sections)

    assert message.splitlines() == [
        "\U0001F4DC **League of Legends Patch 26.21 Notes**",
        "\U0001F5D3\uFE0F Published <t:1792519200:D>",
        "> True Damage has arrived with style!",
        "",
        "\U0001F9D9 **Champions (2):** Azir, Caitlyn",
        "\U0001F6E1\uFE0F **Items (1):** Hubris",
        "\U0001F9E9 **Also in this patch:** Arena, Classic",
        "",
        f"\U0001F517 {BASE}/en-us/news/game-updates/patch-26-21-notes/",
    ]


def test_format_patch_post_fallback_without_sections():
    article = {"title": "Patch 26.22 Notes", "url": "https://x/", "description": "", "published_at": 0}

    [message] = logic.format_patch_post(article, [])

    assert message == "\U0001F4DC **Patch 26.22 Notes**\n\n\U0001F517 https://x/"


def test_format_patch_post_caps_names_and_splits_long_posts():
    article = {"title": "Patch", "url": "https://x/", "description": "d" * 500, "published_at": 1}
    names = [f"Champion{i:03d}LongName" for i in range(60)]
    sections = [(f"Section {n}", names) for n in range(4)]

    messages = logic.format_patch_post(article, sections)

    assert len(messages) > 1
    assert all(len(message) <= logic.DISCORD_MESSAGE_LIMIT for message in messages)
    joined = "\n".join(messages)
    assert "+20 more" in joined
    assert "d" * 297 + "..." in joined
    assert joined.endswith("https://x/")


def test_seen_state_round_trip_and_trim():
    assert logic.load_seen_state(None) is None
    assert logic.load_seen_state("not json") is None
    keys = [f"k{i}" for i in range(80)] + ["k79"]
    restored = logic.load_seen_state(logic.dump_seen_state(keys))
    assert len(restored) == logic.SEEN_STATE_MAX_ENTRIES
    assert restored[-1] == "k79"
    assert logic.load_seen_state(logic.dump_seen_state([])) == []


def test_select_new_articles_filters_seen_and_old():
    now = 2_000_000
    articles = [
        {"id": "new2", "url": "u-new2", "published_at": now - 100},
        {"id": "new1", "url": "u-new1", "published_at": now - 200},
        {"id": "moved", "url": "u-seen", "published_at": now - 300},
        {"id": "old", "url": "u-old", "published_at": now - 10_000},
    ]

    to_post = logic.select_new_articles(articles, ["u-seen"], now_unix=now, max_age_seconds=1_000)

    assert [a["id"] for a in to_post] == ["new1", "new2"]


def test_seconds_until_next_check():
    assert logic.seconds_until_next_check(0, now_unix=1000, interval_seconds=600) == 0
    assert logic.seconds_until_next_check(900, now_unix=1000, interval_seconds=600) == 500
    assert logic.seconds_until_next_check(100, now_unix=1000, interval_seconds=600) == 0
