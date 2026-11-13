
import os
import html
import logging
from datetime import datetime, timedelta, timezone

import requests
import feedparser
from dateutil import parser as date_parser


# ==========================================
# JACKY - DAILY DEVELOPER RADAR
# ==========================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GITHUB_API = "https://api.github.com/search/repositories"

RSS_FEEDS = {
    "Tech News": "https://techcrunch.com/feed/",
    "Developer News": "https://hnrss.org/frontpage",
    "Technology": "https://www.theverge.com/rss/index.xml",
}

HEADERS = {
    "User-Agent": "Jacky-Daily-Developer-Radar/1.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ==========================================
# DATE HELPERS
# ==========================================

def get_cutoff_date():
    return datetime.now(timezone.utc) - timedelta(days=7)


def parse_date(date_string):
    if not date_string:
        return None

    try:
        parsed = date_parser.parse(date_string)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError):
        return None


# ==========================================
# GITHUB REPOSITORY RADAR
# ==========================================

def fetch_github_repositories():
    logging.info("Fetching GitHub repositories...")

    cutoff = get_cutoff_date().strftime("%Y-%m-%d")

    params = {
        "q": f"stars:>50 pushed:>{cutoff}",
        "sort": "updated",
        "order": "desc",
        "per_page": 8,
    }

    try:
        response = requests.get(
            GITHUB_API,
            params=params,
            headers={
                **HEADERS,
                "Accept": "application/vnd.github+json"
            },
            timeout=20
        )

        response.raise_for_status()

        repositories = response.json().get("items", [])

        results = []

        for repo in repositories[:4]:
            name = repo.get("full_name", "Unknown")
            description = repo.get("description") or "No description available."
            language = repo.get("language") or "Not specified"
            stars = repo.get("stargazers_count", 0)
            url = repo.get("html_url", "")
            updated = repo.get("pushed_at", "")[:10]

            results.append({
                "category": "GITHUB PICK",
                "title": name,
                "description": description,
                "why": (
                    "Worth exploring as an active open-source "
                    "project. Review its code and use cases."
                ),
                "url": url,
                "extra": (
                    f"Language: {language} | "
                    f"Stars: {stars} | Updated: {updated}"
                )
            })

        logging.info("GitHub picks found: %s", len(results))
        return results

    except requests.RequestException as error:
        logging.error("GitHub API error: %s", error)
        return []


# ==========================================
# RSS NEWS RADAR
# ==========================================

def fetch_rss_news():
    logging.info("Fetching RSS news...")

    results = []
    cutoff = get_cutoff_date()

    for source, feed_url in RSS_FEEDS.items():

        try:
            response = requests.get(
                feed_url,
                headers=HEADERS,
                timeout=20
            )

            response.raise_for_status()

            feed = feedparser.parse(response.content)

            for entry in feed.entries:

                published = (
                    entry.get("published")
                    or entry.get("updated")
                )

                article_date = parse_date(published)

                if not article_date or article_date < cutoff:
                    continue

                title = entry.get("title", "Untitled")
                link = entry.get("link", "")

                summary = entry.get("summary", "")
                summary = html.unescape(summary)
                summary = html.unescape(
                    __import__("re").sub(
                        r"<[^>]+>", " ", summary
                    )
                )
                summary = " ".join(summary.split())

                if len(summary) > 220:
                    summary = summary[:217] + "..."

                results.append({
                    "category": source.upper(),
                    "title": title,
                    "description": summary or (
                        "Read the original article for details."
                    ),
                    "why": (
                        "Review the development to understand "
                        "its possible impact on software engineering."
                    ),
                    "url": link,
                    "extra": article_date.strftime("%d %b %Y")
                })

        except requests.RequestException as error:
            logging.error("%s RSS error: %s", source, error)

    # Remove duplicate articles
    unique = {}
    for item in results:
        if item["url"]:
            unique[item["url"]] = item

    news = list(unique.values())

    news.sort(
        key=lambda item: item["extra"],
        reverse=True
    )

    logging.info("RSS articles found: %s", len(news))

    return news[:6]


# ==========================================
# BUILD DAILY BRIEFING
# ==========================================

def build_briefing():
    today = datetime.now().strftime("%d %B %Y")

    github_news = fetch_github_repositories()
    rss_news = fetch_rss_news()

    all_news = github_news + rss_news

    if not all_news:
        return (
            f"🤖 <b>JACKY DAILY DEVELOPER RADAR</b>\n"
            f"📅 {today}\n\n"
            "No fresh news could be collected today.\n"
            "Please try again later."
        )

    message = [
        "🤖 <b>JACKY DAILY DEVELOPER RADAR</b>",
        f"📅 {today}",
        "",
        (
            "Your daily briefing on open source, "
            "AI and developer technology."
        ),
        "",
        f"📊 <b>{len(all_news)} DEVELOPMENTS FOUND</b>",
        ""
    ]

    for index, item in enumerate(all_news, start=1):

        message.extend([
            f"<b>{index}. {html.escape(item['title'])}</b>",
            f"🏷 {html.escape(item['category'])}",
            "",
            "<b>What happened:</b>",
            html.escape(item["description"]),
            "",
            "<b>Why developers care:</b>",
            html.escape(item["why"]),
            "",
            f"ℹ️ {html.escape(item['extra'])}",
        ])

        if item["url"]:
            message.append(
                f'🔗 <a href="{html.escape(item["url"], quote=True)}">'
                "Read original source</a>"
            )

        message.append("")

    message.extend([
        "━━━━━━━━━━━━━━━━━━",
        "💡 <b>ONE THING TO TRY TODAY</b>",
        "",
        (
            "Pick one GitHub repository from today's list. "
            "Read its README, inspect its architecture, "
            "and run a small experiment."
        ),
        "",
        "⚡ <i>Stay curious. Keep building.</i>",
        "<b>— JACKY</b>"
    ])

    return "\n".join(message)


# ==========================================
# SEND MESSAGE TO TELEGRAM
# ==========================================

def send_telegram_message(message):

    if not BOT_TOKEN or not CHAT_ID:
        logging.error(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID."
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/sendMessage"
    )

    # Telegram text message limit is 4096 characters.
    # Keep a safety margin for formatting.
    chunks = []
    current = ""

    for line in message.split("\n"):
        candidate = current + line + "\n"

        if len(candidate) > 3800 and current:
            chunks.append(current)
            current = line + "\n"
        else:
            current = candidate

    if current:
        chunks.append(current)

    try:
        for chunk in chunks:
            response = requests.post(
                url,
                json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=30
            )

            response.raise_for_status()

            if not response.json().get("ok"):
                logging.error(
                    "Telegram rejected the message: %s",
                    response.text
                )
                return False

        logging.info("Telegram briefing sent successfully!")
        return True

    except requests.RequestException as error:
        logging.error("Telegram sending error: %s", error)
        return False


# ==========================================
# MAIN ENTRY POINT
# ==========================================

def main():

    logging.info("Jacky Daily Developer Radar starting...")

    briefing = build_briefing()

    if send_telegram_message(briefing):
        logging.info("Jacky completed successfully.")
    else:
        logging.error("Jacky could not deliver the briefing.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
