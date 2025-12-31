from fastapi import APIRouter, HTTPException
from typing import Optional
import feedparser
import httpx
from datetime import datetime, timedelta
import re
import os

router = APIRouter()

# In-memory cache (in production, use Redis)
news_cache = {}
CACHE_DURATION = timedelta(
    minutes=int(os.getenv("NEWS_CACHE", 5))
)  # Reduced to 5 minutes for testing

# RSS feeds by language and category
NEWS_FEEDS = {
    "en": {
        "general": "https://feeds.bbci.co.uk/news/rss.xml",
        "technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "science": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "health": "https://feeds.bbci.co.uk/news/health/rss.xml",
        "entertainment": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
        "politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
    },
    "es": {
        "general": "https://news.google.com/rss?hl=es&gl=ES&ceid=ES:es",
        "technology": "https://news.google.com/rss/search?q=tecnolog%C3%ADa&hl=es&gl=ES&ceid=ES:es",
        "science": "https://news.google.com/rss/search?q=ciencia&hl=es&gl=ES&ceid=ES:es",
        "business": "https://news.google.com/rss/search?q=econom%C3%ADa&hl=es&gl=ES&ceid=ES:es",
        "entertainment": "https://news.google.com/rss/search?q=cultura&hl=es&gl=ES&ceid=ES:es",
        "politics": "https://news.google.com/rss/search?q=pol%C3%ADtica&hl=es&gl=ES&ceid=ES:es",
    },
    "fr": {
        "general": "https://news.google.com/rss?hl=fr&gl=FR&ceid=FR:fr",
        "technology": "https://news.google.com/rss/search?q=technologie&hl=fr&gl=FR&ceid=FR:fr",
        "science": "https://news.google.com/rss/search?q=science&hl=fr&gl=FR&ceid=FR:fr",
        "business": "https://news.google.com/rss/search?q=%C3%A9conomie&hl=fr&gl=FR&ceid=FR:fr",
        "entertainment": "https://news.google.com/rss/search?q=culture&hl=fr&gl=FR&ceid=FR:fr",
        "politics": "https://news.google.com/rss/search?q=politique&hl=fr&gl=FR&ceid=FR:fr",
    },
    "de": {
        "general": "https://news.google.com/rss?hl=de&gl=DE&ceid=DE:de",
        "technology": "https://news.google.com/rss/search?q=technologie&hl=de&gl=DE&ceid=DE:de",
        "science": "https://news.google.com/rss/search?q=wissenschaft&hl=de&gl=DE&ceid=DE:de",
        "business": "https://news.google.com/rss/search?q=wirtschaft&hl=de&gl=DE&ceid=DE:de",
        "entertainment": "https://news.google.com/rss/search?q=kultur&hl=de&gl=DE&ceid=DE:de",
        "politics": "https://news.google.com/rss/search?q=politik&hl=de&gl=DE&ceid=DE:de",
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


def strip_html(html: str) -> str:
    """Remove HTML tags from text"""
    if not html:
        return ""
    return re.sub("<[^<]+?>", "", html)


# Category colors for placeholders
CATEGORY_COLORS = {
    "general": "607d8b",  # Grey Blue
    "technology": "2196f3",  # Blue
    "science": "9c27b0",  # Purple
    "business": "4caf50",  # Green
    "health": "f44336",  # Red
    "entertainment": "e91e63",  # Pink
    "politics": "795548",  # Brown
}


@router.get("/news/{language}/{category}")
async def get_news(language: str = "en", category: str = "general"):
    """
    Fetch news from RSS feeds based on language and category
    Returns cached results if available (5 min cache)
    """
    try:
        # Get feed URL first
        lang_feeds = NEWS_FEEDS.get(language, NEWS_FEEDS["en"])
        feed_url = lang_feeds.get(category, lang_feeds.get("general"))

        if not feed_url:
            raise HTTPException(status_code=404, detail="Feed not found")

        # Cache key includes feed URL hash to auto-invalidate when feeds change
        import hashlib

        url_hash = hashlib.md5(feed_url.encode()).hexdigest()[:8]
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
                }
                seed = seed_map.get(category, 100)
                # Add entry index for variety within same category
                import hashlib

                url_hash = hashlib.md5(entry.link.encode()).hexdigest()
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

                articles.append(
                    {
                        "title": entry.title,
                        "description": description or "Click to read more...",
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
async def clear_news_cache():
    """
    Clear the news cache (for testing/debugging)
    """
    global news_cache
    news_cache.clear()
    return {"status": "success", "message": "News cache cleared"}
