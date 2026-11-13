
import os
import re
import json
import html
import math
import time
import hashlib
import logging
import calendar

from pathlib import Path
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests
import feedparser

# ============================================================
# JACKY - DAILY DEVELOPER RADAR
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GITHUB_API = "https://api.github.com/search/repositories"
TELEGRAM_API = "https://api.telegram.org/bot"

HISTORY_FILE = Path("data/seen_items.json")

NEWS_DAYS = 7
HISTORY_DAYS = 60
REPOSITORY_COOLDOWN_DAYS = 14

MAX_REPOSITORIES = 4
MAX_NEWS = 8

REQUEST_TIMEOUT = 20

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "JACKY-Daily-Developer-Radar/2.0"
})


# ============================================================
# NEWS SOURCES
# ============================================================

RSS_FEEDS = [
    {
        "name": "Hacker News",
        "url": "https://hnrss.org/frontpage",
        "category": "Developer Tools"
    },
    {
        "name": "TechCrunch",
        "url": "https://techcrunch.com/feed/",
        "category": "Industry & Security"
    },
    {
        "name": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml",
        "category": "Industry & Security"
    },
    {
        "name": "OpenAI",
        "url": "https://openai.com/news/rss.xml",
        "category": "AI & Models"
    },
    {
        "name": "Google DeepMind",
        "url": "https://deepmind.google/blog/rss.xml",
        "category": "AI & Models"
    },
    {
        "name": "Hugging Face",
        "url": "https://huggingface.co/blog/feed.xml",
        "category": "AI & Models"
    },
    {
        "name": "Google Research",
        "url": "https://research.google/blog/rss/",
        "category": "AI & Models"
    },
    {
        "name": "NVIDIA",
        "url": "https://blogs.nvidia.com/feed/",
        "category": "AI & Models"
    },
    {
        "name": "AWS Machine Learning",
        "url": "https://aws.amazon.com/blogs/machine-learning/feed/",
        "category": "Developer Tools"
    }
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_html(text):
    """Remove HTML tags and clean article descriptions."""

    if not text:
        return ""

    text = re.sub(r"<[^>]+>", " ", str(text))
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def parse_date(value):
    """Parse a date safely into UTC."""

    if not value:
        return None

    try:
        from dateutil import parser

        result = parser.parse(str(value))

        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)

        return result.astimezone(UTC)

    except Exception:
        return None


def format_date(value):
    """Format a date in Indian Standard Time."""

    if not value:
        return "Date unavailable"

    return value.astimezone(IST).strftime("%d %b %Y")


def canonical_url(url):
    """Remove tracking parameters from URLs."""

    if not url:
        return ""

    url = url.strip().split("#")[0]

    url = re.sub(
        r"([?&])(utm_[^=&]+|ref|source|fbclid|gclid)=[^&]*",
        "",
        url,
        flags=re.IGNORECASE
    )

    url = url.rstrip("?&")

    return url


def make_item_id(url, title):
    """Create a stable identifier for a news item."""

    identity = canonical_url(url) or title.lower().strip()

    return hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()


def youtube_search_link(title):
    """Create a clearly labelled YouTube search link."""

    query = quote_plus(title + " explained tutorial")

    return f"https://www.youtube.com/results?search_query={query}"


# ============================================================
# HISTORY MANAGEMENT
# ============================================================

def load_history():
    """Load previously sent news and repositories."""

    if not HISTORY_FILE.exists():
        return {
            "items": {},
            "repositories": {},
            "last_updated": None
        }

    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as file:
            history = json.load(file)

        history.setdefault("items", {})
        history.setdefault("repositories", {})

        return history

    except (json.JSONDecodeError, OSError):
        logging.exception("Could not read history file.")

        return {
            "items": {},
            "repositories": {},
            "last_updated": None
        }


def save_history(history):
    """Save history for future GitHub Actions runs."""

    HISTORY_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    history["last_updated"] = datetime.now(UTC).isoformat()

    with HISTORY_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            history,
            file,
            indent=2,
            ensure_ascii=False
        )


def cleanup_history(history):
    """Remove old records from the history."""

    cutoff = datetime.now(UTC) - timedelta(
        days=HISTORY_DAYS
    )

    for category in ("items", "repositories"):

        records = history.get(category, {})

        cleaned = {}

        for key, value in records.items():

            seen_at = parse_date(value.get("seen_at"))

            if seen_at and seen_at >= cutoff:
                cleaned[key] = value

        history[category] = cleaned

    return history


def is_already_seen(history, item_id, category="items"):
    """Check whether an item was previously delivered."""

    return item_id in history.get(category, {})


def is_similar_news(history, title):
    """Avoid similar headlines even when URLs differ."""

    new_title = clean_html(title).lower()

    for record in history.get("items", {}).values():

        old_title = record.get("title", "").lower()

        if not old_title:
            continue

        similarity = SequenceMatcher(
            None,
            new_title,
            old_title
        ).ratio()

        if similarity >= 0.88:
            return True

    return False


def mark_as_seen(history, item_id, title, url, category="items"):
    """Record an item after successful Telegram delivery."""

    history.setdefault(category, {})

    history[category][item_id] = {
        "title": title,
        "url": canonical_url(url),
        "seen_at": datetime.now(UTC).isoformat()
    }


# ============================================================
# GITHUB REPOSITORY RADAR
# ============================================================

def repository_relevance(repo):
    """Estimate relevance to the user's developer interests."""

    text = " ".join([
        repo.get("name", ""),
        repo.get("description") or "",
        " ".join(repo.get("topics", [])),
        repo.get("language") or ""
    ]).lower()

    keywords = {
        "ai": 4,
        "agent": 5,
        "llm": 5,
        "rag": 5,
        "machine-learning": 4,
        "python": 3,
        "fastapi": 5,
        "react": 4,
        "nextjs": 4,
        "developer-tools": 4,
        "automation": 4,
        "database": 3,
        "backend": 4,
        "security": 3,
        "docker": 3,
        "open-source": 2
    }

    score = 0

    for keyword, weight in keywords.items():

        if keyword in text:
            score += weight

    return score


def fetch_github_repositories(history):
    """Fetch popular and recently active GitHub repositories."""

    logging.info("Fetching GitHub repositories...")

    cutoff = (
        datetime.now(UTC) - timedelta(days=30)
    ).strftime("%Y-%m-%d")

    params = {
        "q": f"stars:>100 pushed:>{cutoff}",
        "sort": "stars",
        "order": "desc",
        "per_page": 50
    }

    try:
        response = session.get(
            GITHUB_API,
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        repositories = response.json().get(
            "items", []
        )

    except requests.RequestException:
        logging.exception("GitHub API request failed.")
        return []

    results = []

    now = datetime.now(UTC)

    for repo in repositories:

        full_name = repo.get("full_name", "")

        if not full_name:
            continue

        repo_id = hashlib.sha256(
            full_name.lower().encode("utf-8")
        ).hexdigest()

        previous = history.get(
            "repositories", {}
        ).get(repo_id)

        if previous:

            last_seen = parse_date(
                previous.get("seen_at")
            )

            if last_seen and (
                now - last_seen
            ).days < REPOSITORY_COOLDOWN_DAYS:
                continue

        pushed_at = parse_date(
            repo.get("pushed_at")
        )

        if not pushed_at:
            continue

        relevance = repository_relevance(repo)

        stars = repo.get("stargazers_count", 0)

        recency_days = max(
            0,
            (now - pushed_at).days
        )

        score = (
            math.log10(stars + 1) * 3
            + relevance
            - recency_days * 0.1
        )

        results.append({
            "id": repo_id,
            "name": full_name,
            "description": clean_html(
                repo.get("description") or
                "No description provided."
            ),
            "language": repo.get("language") or "Not specified",
            "stars": stars,
            "url": repo.get("html_url", ""),
            "pushed_at": pushed_at,
            "topics": repo.get("topics", []),
            "relevance": relevance,
            "score": score
        })

    results.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    selected = results[:MAX_REPOSITORIES]

    logging.info(
        "GitHub picks found: %s",
        len(selected)
    )

    return selected


# ============================================================
# RSS NEWS RADAR
# ============================================================

def parse_feed_entry_date(entry):
    """Read RSS publication date."""

    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if parsed:

        try:
            timestamp = calendar.timegm(parsed[:6])

            return datetime.fromtimestamp(
                timestamp,
                tz=UTC
            )

        except (ValueError, OverflowError):
            pass

    return (
        parse_date(entry.get("published"))
        or parse_date(entry.get("updated"))
    )


def explain_why(category):
    """Give a practical developer-focused explanation."""

    explanations = {
        "AI & Models": (
            "Important for understanding AI models, "
            "LLM development, RAG and AI agent engineering."
        ),
        "Developer Tools": (
            "Useful for improving development workflows, "
            "backend engineering and developer productivity."
        ),
        "Industry & Security": (
            "Helps developers understand technology trends, "
            "platform changes and security developments."
        )
    }

    return explanations.get(
        category,
        "Worth understanding for modern software development."
    )


def fetch_rss_news(history):
    """Fetch fresh articles and filter previously sent news."""

    logging.info("Fetching RSS news...")

    cutoff = datetime.now(UTC) - timedelta(
        days=NEWS_DAYS
    )

    collected = []
    run_seen = set()

    for feed_info in RSS_FEEDS:

        try:
            logging.info(
                "Reading feed: %s",
                feed_info["name"]
            )

            feed = feedparser.parse(
                feed_info["url"],
                request_headers={
                    "User-Agent": "JACKY Developer Radar/2.0"
                }
            )

            for entry in feed.entries:

                title = clean_html(
                    entry.get("title", "")
                )

                url = canonical_url(
                    entry.get("link", "")
                )

                if not title or not url:
                    continue

                published = parse_feed_entry_date(entry)

                # Do not present undated articles as fresh news.
                if not published or published < cutoff:
                    continue

                if published > datetime.now(UTC) + timedelta(days=1):
                    continue

                item_id = make_item_id(url, title)

                if item_id in run_seen:
                    continue

                if is_already_seen(
                    history,
                    item_id,
                    "items"
                ):
                    continue

                if is_similar_news(history, title):
                    continue

                summary = clean_html(
                    entry.get("summary", "")
                )

                if not summary:
                    summary = clean_html(
                        entry.get("description", "")
                    )

                if not summary:
                    summary = "Open the original source for full details."

                if len(summary) > 300:
                    summary = summary[:297].rsplit(
                        " ", 1
                    )[0] + "..."

                collected.append({
                    "id": item_id,
                    "title": title,
                    "url": url,
                    "source": feed_info["name"],
                    "category": feed_info["category"],
                    "published": published,
                    "summary": summary,
                    "why": explain_why(
                        feed_info["category"]
                    )
                })

                run_seen.add(item_id)

        except Exception:
            logging.exception(
                "Feed failed: %s",
                feed_info["name"]
            )

    collected.sort(
        key=lambda item: item["published"],
        reverse=True
    )

    logging.info(
        "Fresh RSS articles found: %s",
        len(collected)
    )

    return collected


# ============================================================
# NEWS SELECTION
# ============================================================

def select_news(news):
    """Select a balanced category-wise news briefing."""

    limits = {
        "Developer Tools": 3,
        "AI & Models": 3,
        "Industry & Security": 2
    }

    selected = []

    for category, limit in limits.items():

        category_items = [
            item for item in news
            if item["category"] == category
        ]

        selected.extend(
            category_items[:limit]
        )

    selected.sort(
        key=lambda item: item["published"],
        reverse=True
    )

    return selected[:MAX_NEWS]


# ============================================================
# TELEGRAM MESSAGE BUILDER
# ============================================================

def build_briefing(repositories, news):
    """Build the final Telegram briefing."""

    today = datetime.now(IST).strftime(
        "%A, %d %B %Y"
    )

    lines = [
        "🚀 <b>JACKY | DAILY DEVELOPER RADAR</b>",
        f"📅 {today}",
        "",
        "<i>Your daily intelligence briefing for modern software development.</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🔥 <b>1. GITHUB TRENDING & USEFUL REPOSITORIES</b>",
        "━━━━━━━━━━━━━━━━━━━━"
    ]

    if repositories:

        for index, repo in enumerate(
            repositories,
            start=1
        ):

            description = html.escape(
                repo["description"][:220]
            )

            language = html.escape(
                repo["language"]
            )

            lines.extend([
                "",
                f"<b>{index}. {html.escape(repo['name'])}</b>",
                f"⭐ {repo['stars']:,} stars | 💻 {language}",
                f"📝 {description}",
                f"🕒 Last updated: {format_date(repo['pushed_at'])}",
                "🎯 <b>Why explore:</b> "
                + (
                    "Relevant to AI, agents or your development stack."
                    if repo["relevance"] >= 5
                    else "Explore its implementation, architecture and practical use."
                ),
                f'🔗 <a href="{html.escape(repo["url"], quote=True)}">Open GitHub Repository</a>'
            ])

    else:
        lines.append(
            "No new eligible repositories found today."
        )

    category_sections = [
        ("Developer Tools", "🛠️ 2. DEVELOPER TOOLS & RELEASES"),
        ("AI & Models", "🧠 3. AI & MODEL INTELLIGENCE"),
        ("Industry & Security", "🌐 4. INDUSTRY & SECURITY")
    ]

    for category, heading in category_sections:

        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"<b>{heading}</b>",
            "━━━━━━━━━━━━━━━━━━━━"
        ])

        items = [
            item for item in news
            if item["category"] == category
        ]

        if not items:
            lines.append(
                "No fresh verified items available in this category."
            )
            continue

        for index, item in enumerate(
            items,
            start=1
        ):

            lines.extend([
                "",
                f"<b>{index}. {html.escape(item['title'])}</b>",
                f"📰 {html.escape(item['source'])} | {format_date(item['published'])}",
                "",
                "<b>What happened?</b>",
                html.escape(item["summary"]),
                "",
                "<b>Why developers should care?</b>",
                html.escape(item["why"]),
                "",
                f'🔗 <a href="{html.escape(item["url"], quote=True)}">Read Original Source</a>',
                f'▶️ <a href="{html.escape(youtube_search_link(item["title"]), quote=True)}">Find YouTube Explainers</a>'
            ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🎯 <b>5. ONE THING TO TRY TODAY</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "Choose one repository or technology from today's briefing.",
        "Explore its documentation, run a small example and understand how it works.",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🤖 <i>JACKY | Built for continuous learning.</i>"
    ])

    return "\n".join(lines)


# ============================================================
# TELEGRAM DELIVERY
# ============================================================

def split_message(message, limit=3800):
    """Split long messages without breaking paragraphs."""

    paragraphs = message.split("\n\n")

    chunks = []
    current = ""

    for paragraph in paragraphs:

        candidate = (
            current + "\n\n" + paragraph
            if current else paragraph
        )

        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(paragraph) > limit:
            remaining = paragraph

            while len(remaining) > limit:

                split_at = remaining.rfind(
                    "\n",
                    0,
                    limit
                )

                if split_at == -1:
                    split_at = limit

                chunks.append(
                    remaining[:split_at]
                )

                remaining = remaining[split_at:].strip()

            current = remaining

        else:
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def send_telegram_message(message):
    """Send the briefing through Telegram Bot API."""

    if not BOT_TOKEN or not CHAT_ID:

        logging.error(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID."
        )

        return False

    chunks = split_message(message)

    endpoint = (
        f"{TELEGRAM_API}{BOT_TOKEN}/sendMessage"
    )

    for index, chunk in enumerate(chunks, start=1):

        try:
            response = session.post(
                endpoint,
                json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=REQUEST_TIMEOUT
            )

            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                logging.error(
                    "Telegram rejected message: %s",
                    result
                )
                return False

            logging.info(
                "Telegram message delivered: %s/%s",
                index,
                len(chunks)
            )

        except requests.RequestException:
            logging.exception(
                "Telegram delivery failed."
            )

            return False

    return True


# ============================================================
# MAIN EXECUTION
# ============================================================

def main():

    logging.info(
        "JACKY DAILY DEVELOPER RADAR STARTING"
    )

    history = load_history()
    history = cleanup_history(history)

    repositories = fetch_github_repositories(
        history
    )

    news = fetch_rss_news(history)

    selected_news = select_news(news)

    logging.info(
        "Selected news: %s",
        len(selected_news)
    )

    briefing = build_briefing(
        repositories,
        selected_news
    )

    delivered = send_telegram_message(
        briefing
    )

    if not delivered:

        logging.error(
            "JACKY could not deliver the briefing. History was not updated."
        )

        raise SystemExit(1)

    # Save records only after successful delivery.

    for item in selected_news:

        mark_as_seen(
            history,
            item["id"],
            item["title"],
            item["url"],
            "items"
        )

    for repo in repositories:

        mark_as_seen(
            history,
            repo["id"],
            repo["name"],
            repo["url"],
            "repositories"
        )

    save_history(history)

    logging.info(
        "History successfully saved."
    )

    logging.info(
        "JACKY DAILY DEVELOPER RADAR COMPLETED SUCCESSFULLY."
    )


if __name__ == "__main__":
    main()
