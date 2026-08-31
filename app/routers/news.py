from fastapi import APIRouter, HTTPException, Depends
from typing import Optional
import feedparser
import httpx
from datetime import datetime, timedelta
import re
import os
import html
from urllib.parse import quote
from app.auth.firebase_auth import get_firebase_user

router = APIRouter()

# In-memory cache (in production, use Redis)
news_cache = {}
# Feed contents change on the order of tens of minutes, and the frontend holds
# its own 15-minute React Query staleTime (see `nowry/src/hooks/useNews.js`).
# Keep the two in step so a Home revisit inside the window costs nothing.
CACHE_DURATION = timedelta(minutes=int(os.getenv("NEWS_CACHE", 15)))


def google_news_search(query: str, hl: str, gl: str) -> str:
    """
    Build a Google News RSS search URL for a topic in a given locale.

    Used for every category that has no dedicated publisher feed. `hl` is the
    interface language ("en-US", "es"), `gl` the country code ("US", "ES"); the
    `ceid` Google expects is "<country>:<language>" with the language stripped
    of any regional suffix.
    """
    language = hl.split("-")[0]
    return (
        f"https://news.google.com/rss/search?q={quote(query)}"
        f"&hl={hl}&gl={gl}&ceid={gl}:{language}"
    )


def google_news_home(hl: str, gl: str) -> str:
    """Build the Google News RSS top-stories URL for a locale."""
    language = hl.split("-")[0]
    return f"https://news.google.com/rss?hl={hl}&gl={gl}&ceid={gl}:{language}"


# RSS feeds by language and category.
#
# Every one of the 14 taxonomy topics in `nowry/src/constants/learningTaxonomy.js`
# maps to a category that resolves here, in every supported language. Six topics
# (mathematics, history, languages, philosophy, design, psychology) used to fall
# back to "general", which meant a user who picked only those saw exactly the
# same feed as a user with no interests at all; they now have real feeds. `en`
# keeps its BBC section feeds where BBC publishes one and uses Google News search
# for the rest.
NEWS_FEEDS = {
    "en": {
        "general": "https://feeds.bbci.co.uk/news/rss.xml",
        "technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "science": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "health": "https://feeds.bbci.co.uk/news/health/rss.xml",
        "entertainment": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
        "politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
        "mathematics": google_news_search("mathematics", "en-US", "US"),
        "history": google_news_search("archaeology OR historians", "en-US", "US"),
        "languages": google_news_search("language learning", "en-US", "US"),
        "philosophy": google_news_search("philosophy ethics", "en-US", "US"),
        "design": google_news_search("design", "en-US", "US"),
        "psychology": google_news_search("psychology", "en-US", "US"),
    },
    "es": {
        "general": google_news_home("es", "ES"),
        "technology": google_news_search("tecnología", "es", "ES"),
        "science": google_news_search("ciencia", "es", "ES"),
        "business": google_news_search("economía", "es", "ES"),
        "health": google_news_search("salud", "es", "ES"),
        "entertainment": google_news_search("cultura", "es", "ES"),
        "politics": google_news_search("política", "es", "ES"),
        "mathematics": google_news_search("matemáticas", "es", "ES"),
        "history": google_news_search("arqueología OR historiador", "es", "ES"),
        "languages": google_news_search("aprendizaje de idiomas", "es", "ES"),
        "philosophy": google_news_search("filosofía", "es", "ES"),
        "design": google_news_search("diseño", "es", "ES"),
        "psychology": google_news_search("psicología", "es", "ES"),
    },
    "fr": {
        "general": google_news_home("fr", "FR"),
        "technology": google_news_search("technologie", "fr", "FR"),
        "science": google_news_search("science", "fr", "FR"),
        "business": google_news_search("économie", "fr", "FR"),
        "health": google_news_search("santé", "fr", "FR"),
        "entertainment": google_news_search("culture", "fr", "FR"),
        "politics": google_news_search("politique", "fr", "FR"),
        "mathematics": google_news_search("mathématiques", "fr", "FR"),
        "history": google_news_search("archéologie OR historien", "fr", "FR"),
        "languages": google_news_search("apprentissage des langues", "fr", "FR"),
        "philosophy": google_news_search("philosophie", "fr", "FR"),
        "design": google_news_search("design", "fr", "FR"),
        "psychology": google_news_search("psychologie", "fr", "FR"),
    },
    "de": {
        "general": google_news_home("de", "DE"),
        "technology": google_news_search("technologie", "de", "DE"),
        "science": google_news_search("wissenschaft", "de", "DE"),
        "business": google_news_search("wirtschaft", "de", "DE"),
        "health": google_news_search("gesundheit", "de", "DE"),
        "entertainment": google_news_search("kultur", "de", "DE"),
        "politics": google_news_search("politik", "de", "DE"),
        "mathematics": google_news_search("mathematik", "de", "DE"),
        "history": google_news_search("archäologie OR historiker", "de", "DE"),
        "languages": google_news_search("sprachen lernen", "de", "DE"),
        "philosophy": google_news_search("philosophie", "de", "DE"),
        "design": google_news_search("design", "de", "DE"),
        "psychology": google_news_search("psychologie", "de", "DE"),
    },
}


def extract_image_from_html(html: str) -> Optional[str]:
    """Extract image URL from HTML content"""
    if not html:
        return None

    # Try to find img tag
    match = re.search(r'<img[^>]+src="([^">]+)"', html)
    if match:
        return match.group(1)

    return None


def strip_html(markup: str) -> str:
    """
    Remove HTML tags from text and decode character entities.

    Entity decoding matters for the Google News feeds: their summaries are HTML
    fragments whose text is littered with `&nbsp;` and `&#39;`, which used to be
    rendered literally on the news card once the tags alone were stripped.
    """
    if not markup:
        return ""
    return html.unescape(re.sub("<[^<]+?>", "", markup))


def _letters_only(text: str) -> str:
    """Lowercase alphanumerics only — separators and spacing carry no meaning here."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


def is_echo_of_title(description: str, title: str) -> bool:
    """Is this summary just the headline restated?"""
    normalized_title = _letters_only(title)
    if not normalized_title:
        return False
    return _letters_only(description).startswith(normalized_title[:50])


@router.get("/news/{language}/{category}")
async def get_news(language: str = "en", category: str = "general", user: dict = Depends(get_firebase_user)):
    """
    Fetch news from RSS feeds based on language and category.

    Returns cached results when the entry is younger than CACHE_DURATION
    (NEWS_CACHE minutes, default 15).
    """
    try:
        # Get feed URL first
        lang_feeds = NEWS_FEEDS.get(language, NEWS_FEEDS["en"])
        feed_url = lang_feeds.get(category, lang_feeds.get("general"))

        if not feed_url:
            raise HTTPException(status_code=404, detail="Feed not found")

        # Cache key includes feed URL hash to auto-invalidate when feeds change
        import hashlib

        url_hash = hashlib.md5(feed_url.encode(), usedforsecurity=False).hexdigest()[:8]
        cache_key = f"{language}_{category}_{url_hash}"

        # Check cache
        if cache_key in news_cache:
            cached_data, cached_time = news_cache[cache_key]
            if datetime.now() - cached_time < CACHE_DURATION:
                return {"status": "success", "articles": cached_data, "cached": True}

        # Fetch RSS feed
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(feed_url)
            response.raise_for_status()

        # Parse RSS feed
        feed = feedparser.parse(response.content)

        articles = []
        for entry in feed.entries[:15]:  # Limit to 15 articles
            # Extract image
            image_url = None

            # Try media content
            if hasattr(entry, "media_content") and entry.media_content:
                image_url = entry.media_content[0].get("url")

            # Try media thumbnail
            if (
                not image_url
                and hasattr(entry, "media_thumbnail")
                and entry.media_thumbnail
            ):
                image_url = entry.media_thumbnail[0].get("url")

            # Try enclosure
            if not image_url and hasattr(entry, "enclosures") and entry.enclosures:
                for enclosure in entry.enclosures:
                    if enclosure.get("type", "").startswith("image"):
                        image_url = enclosure.get("href")
                        break

            # Try content:encoded
            if not image_url and hasattr(entry, "content"):
                for content in entry.content:
                    if content.get("type") == "text/html":
                        image_url = extract_image_from_html(content.get("value", ""))
                        if image_url:
                            break

            # Try extracting from description/summary
            if not image_url:
                image_url = extract_image_from_html(getattr(entry, "description", ""))

            if not image_url:
                image_url = extract_image_from_html(getattr(entry, "summary", ""))

            # Use smart placeholder if no image found (use Picsum for random images)
            if not image_url:
                # Use category as seed for consistent but varied images per category
                # Picsum provides random placeholder images that are reliable
                seed_map = {
                    "general": 100,
                    "technology": 200,
                    "science": 300,
                    "business": 400,
                    "health": 500,
                    "entertainment": 600,
                    "politics": 700,
                    "mathematics": 800,
                    "history": 900,
                    "languages": 1000,
                    "philosophy": 1100,
                    "design": 1200,
                    "psychology": 1300,
                }
                seed = seed_map.get(category, 100)
                # Add entry index for variety within same category
                import hashlib

                url_hash = hashlib.md5(entry.link.encode(), usedforsecurity=False).hexdigest()
                unique_id = seed + int(url_hash[:4], 16) % 100
                # Picsum.photos provides reliable random images
                image_url = f"https://picsum.photos/seed/{unique_id}/800/450"

            # Include article if it has title and link
            if hasattr(entry, "title") and hasattr(entry, "link"):
                # Clean up description (remove HTML and Google News clutter)
                raw_desc = getattr(entry, "summary", "") or getattr(
                    entry, "description", ""
                )
                description = strip_html(raw_desc)[:200]

                # Try to clean up Google News specific text "View full coverage"
                description = description.replace("View full coverage", "").strip()

                # Google News summaries are frequently just the headline again,
                # sometimes with the publisher appended and different separators
                # ("Title - Publisher" vs "Title&nbsp;&nbsp;Publisher"), so the
                # comparison ignores everything but the letters and digits.
                # Repeating the title in the card's excerpt is worse than no
                # excerpt, and the frontend renders its own localized fallback.
                if is_echo_of_title(description, strip_html(entry.title)):
                    description = ""

                articles.append(
                    {
                        "title": entry.title,
                        # Deliberately empty rather than an English "Click to
                        # read more..." — the card is rendered in five locales,
                        # so the placeholder belongs in the frontend bundle.
                        "description": description,
                        "urlToImage": image_url,
                        "url": entry.link,
                        "publishedAt": getattr(entry, "published", None),
                    }
                )

        # Cache results
        news_cache[cache_key] = (articles, datetime.now())

        return {
            "status": "success",
            "articles": articles[:15],
            "cached": False,
            "feed_url": feed_url,
        }

    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch RSS feed: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing feed: {str(e)}")


@router.delete("/news/cache/clear")
async def clear_news_cache(current_user: dict = Depends(get_firebase_user)):
    """
    Clear the news cache (for testing/debugging)
    Restricted to admin or dev roles.
    """
    role = current_user.get("role", "user")
    if role not in ["admin", "dev"]:
        raise HTTPException(status_code=403, detail="Insufficient admin privileges to flush cache.")

    global news_cache
    news_cache.clear()
    return {"status": "success", "message": "News cache cleared"}
