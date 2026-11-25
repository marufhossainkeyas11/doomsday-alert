import os
import asyncio
import hashlib
import httpx
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

# --- CONFIG ---
TG_TOKEN = os.environ["TG_TOKEN"]
BASE_URL  = "https://starcineplex.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

CHECK_EVERY = 30  # seconds

# slug → {chat_id → last_hash}
active_alerts: dict[str, dict[int, str]] = {}
alert_lock = asyncio.Lock()
# --- END CONFIG ---


# ─── Scraping helpers ───────────────────────────────────────────

async def fetch_movies(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all movies from /movies page."""
    r = await client.get(f"{BASE_URL}/movies", headers=HEADERS,
                         follow_redirects=True, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    movies = []
    for article in soup.select("article.group"):
        link = article.select_one("a[href^='/movies/']")
        if not link:
            continue
        slug = link["href"].replace("/movies/", "").strip("/")
        title = article.select_one("h3")
        title = title.get_text(strip=True) if title else slug
        badge = article.select_one(".editorial-eyebrow--night")
        status = badge.get_text(strip=True) if badge else "Now Showing"
        genres = [g.get_text(strip=True)
                  for g in article.select("[data-genre]")]
        movies.append({
            "slug": slug,
            "title": title,
            "status": status,
            "genres": genres,
        })
    return movies


async def fetch_showtime_hash(client: httpx.AsyncClient, slug: str) -> tuple[bool, str]:
    """
    Returns (booking_open, section_hash) for a movie page.
    booking_open = True when showtimes/stubs are present.
    """
    url = f"{BASE_URL}/movies/{slug}"
    r = await client.get(url, headers=HEADERS,
                         follow_redirects=True, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    section = soup.select_one("#showtimes")
    text = section.get_text(separator="\n", strip=True) if section else ""
    closed = "Bookings open closer to release" in text
    is_open = not closed and bool(soup.select(".stub"))
    h = hashlib.sha256(text.encode()).hexdigest()
    return is_open, h


# ─── Bot command handlers ────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Star Cineplex Bot*\n\n"
        "/movies — সব মুভির লিস্ট\n"
        "/alert `slug` — কোনো মুভিতে alert দাও\n"
        "  _উদাহরণ:_ `/alert avengers-doomsday`\n"
        "/alerts — তোমার active alerts\n"
        "/cancel `slug` — alert বন্ধ করো\n"
        "/check `slug` — এখনই status দেখো",
        parse_mode="Markdown"
    )


async def cmd_movies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient() as client:
        try:
            movies = await fetch_movies(client)
        except Exception as e:
            await update.message.reply_text(f"❌ Fetch failed: {e}")
            return

    now = [m for m in movies if m["status"] == "Now Showing"]
    soon = [m for m in movies if m["status"] != "Now Showing"]

    lines = ["🎬 *Now Showing*"]
    for m in now:
        g = ", ".join(m["genres"][:2]) or "—"
        lines.append(f"• [{m['title']}]({BASE_URL}/movies/{m['slug']}) _{g}_")

    if soon:
        lines.append("\n🔜 *Coming Soon*")
        for m in soon:
            lines.append(f"• [{m['title']}]({BASE_URL}/movies/{m['slug']})")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/alert avengers-doomsday`", parse_mode="Markdown")
        return

    slug = ctx.args[0].strip().lower()
    chat_id = update.effective_chat.id

    # verify slug exists
    async with httpx.AsyncClient() as client:
        try:
            is_open, h = await fetch_showtime_hash(client, slug)
        except Exception:
            await update.message.reply_text(
                f"❌ `{slug}` পাওয়া যায়নি। slug টা ঠিক আছে?",
                parse_mode="Markdown")
            return

    async with alert_lock:
        if slug not in active_alerts:
            active_alerts[slug] = {}
        active_alerts[slug][chat_id] = h

    if is_open:
        await update.message.reply_text(
            f"✅ *{slug}* — এখনই booking open!\n"
            f"👉 {BASE_URL}/movies/{slug}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"🔔 Alert set: *{slug}*\n"
            f"Booking open হলেই জানাবো।",
            parse_mode="Markdown"
        )


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with alert_lock:
        mine = [slug for slug, chats in active_alerts.items()
                if chat_id in chats]

    if not mine:
        await update.message.reply_text("কোনো active alert নেই। `/alert slug` দিয়ে set করো.", parse_mode="Markdown")
        return

    lines = ["🔔 *Active Alerts*"]
    for slug in mine:
        lines.append(f"• `{slug}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/cancel avengers-doomsday`", parse_mode="Markdown")
        return

    slug = ctx.args[0].strip().lower()
    chat_id = update.effective_chat.id

    async with alert_lock:
        removed = False
        if slug in active_alerts and chat_id in active_alerts[slug]:
            del active_alerts[slug][chat_id]
            if not active_alerts[slug]:
                del active_alerts[slug]
            removed = True

    if removed:
        await update.message.reply_text(f"🗑 Alert cancelled: `{slug}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❓ `{slug}` এর কোনো alert ছিল না।", parse_mode="Markdown")


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/check avengers-doomsday`", parse_mode="Markdown")
        return

    slug = ctx.args[0].strip().lower()
    async with httpx.AsyncClient() as client:
        try:
            is_open, _ = await fetch_showtime_hash(client, slug)
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
            return

    if is_open:
        await update.message.reply_text(
            f"✅ *{slug}* — Booking OPEN!\n👉 {BASE_URL}/movies/{slug}",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"⏳ *{slug}* — এখনো open হয়নি।",
            parse_mode="Markdown")


# ─── Background polling loop ─────────────────────────────────────

async def polling_loop(app: Application):
    """Check all active alerts every CHECK_EVERY seconds."""
    await asyncio.sleep(10)  # let bot start first
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(CHECK_EVERY)
            async with alert_lock:
                slugs = dict(active_alerts)  # snapshot

            for slug, chats in slugs.items():
                try:
                    is_open, current_hash = await fetch_showtime_hash(client, slug)
                except Exception:
                    continue

                to_notify = []
                async with alert_lock:
                    if slug not in active_alerts:
                        continue
                    for chat_id, last_hash in list(active_alerts[slug].items()):
                        if current_hash != last_hash:
                            active_alerts[slug][chat_id] = current_hash
                            if is_open:
                                to_notify.append(chat_id)

                for chat_id in to_notify:
                    try:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"🎟 *BOOKING OPEN!*\n\n"
                                f"*{slug}* এর টিকেট এখন available!\n\n"
                                f"👉 {BASE_URL}/movies/{slug}"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass


# ─── HTTP ping server (Render free tier) ─────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alive")
    def log_message(self, *args):
        pass

def start_ping_server():
    HTTPServer(("0.0.0.0", 10000), PingHandler).serve_forever()


# ─── Main ─────────────────────────────────────────────────────────

def main():
    threading.Thread(target=start_ping_server, daemon=True).start()

    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("movies",  cmd_movies))
    app.add_handler(CommandHandler("alert",   cmd_alert))
    app.add_handler(CommandHandler("alerts",  cmd_alerts))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("check",   cmd_check))

    # start polling loop as background task
    app.job_queue  # just ensure initialized

    async def post_init(app: Application):
        asyncio.create_task(polling_loop(app))

    app.post_init = post_init

    print("[*] Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
