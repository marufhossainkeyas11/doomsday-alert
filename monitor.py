import os
import asyncio
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIG ---
TG_TOKEN = os.environ["TG_TOKEN"]
BASE_URL  = "https://starcineplex.com"
CHECK_EVERY = 30  # seconds

# Default halls to watch (substring match, case-insensitive)
# User can override per-alert with /alert slug hall-keyword
DEFAULT_HALLS: list[str] = []  # empty = any hall

# slug → {chat_id → {last_hash, halls: list[str]}}
active_alerts: dict[str, dict[int, dict]] = {}
alert_lock = asyncio.Lock()

_browser: Browser | None = None
_context: BrowserContext | None = None
_pw_lock = asyncio.Lock()
# --- END CONFIG ---


# ─── Playwright ──────────────────────────────────────────────────

async def get_context() -> BrowserContext:
    global _browser, _context
    async with _pw_lock:
        if _context is None:
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            _context = await _browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        return _context


async def fetch_html(url: str) -> str:
    ctx = await get_context()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1500)
        return await page.content()
    finally:
        await page.close()


# ─── Scraping ────────────────────────────────────────────────────

async def fetch_movies() -> list[dict]:
    html = await fetch_html(f"{BASE_URL}/movies")
    soup = BeautifulSoup(html, "html.parser")
    movies = []
    for article in soup.select("article.group"):
        link = article.select_one("a[href^='/movies/']")
        if not link:
            continue
        slug = link["href"].replace("/movies/", "").strip("/")
        h3 = article.select_one("h3")
        title = h3.get_text(strip=True) if h3 else slug
        badge = article.select_one(".editorial-eyebrow--night")
        status = badge.get_text(strip=True) if badge else "Now Showing"
        genres = [g.get_text(strip=True) for g in article.select("[data-genre]")]
        movies.append({"slug": slug, "title": title, "status": status, "genres": genres})
    return movies


def _parse_showtime_page(html: str, halls: list[str]) -> tuple[bool, str]:
    """
    Returns (booking_open, section_hash).
    booking_open = True if .stub buttons exist for the requested halls (or any hall if halls=[]).
    Hash is of the full #showtimes section text.
    """
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one("#showtimes")
    text = section.get_text(separator="\n", strip=True) if section else ""

    closed_msg = "Bookings open closer to release" in text
    if closed_msg:
        return False, hashlib.sha256(text.encode()).hexdigest()

    all_stubs = soup.select(".stub")
    if not all_stubs:
        return False, hashlib.sha256(text.encode()).hexdigest()

    if not halls:
        # any hall, just check stubs exist and not all closed
        open_stubs = [s for s in all_stubs if "stub--closed" not in s.get("class", [])]
        return bool(open_stubs), hashlib.sha256(text.encode()).hexdigest()

    # Hall-filtered: find branch blocks, match by h3 text
    is_open = False
    for branch_block in section.select("li") if section else []:
        h3 = branch_block.select_one("h3")
        if not h3:
            continue
        branch_name = h3.get_text(strip=True)
        matched = any(kw.lower() in branch_name.lower() for kw in halls)
        if not matched:
            continue
        open_stubs = [
            s for s in branch_block.select(".stub")
            if "stub--closed" not in s.get("class", [])
        ]
        if open_stubs:
            is_open = True
            break

    return is_open, hashlib.sha256(text.encode()).hexdigest()


async def fetch_showtime_data(slug: str, halls: list[str]) -> tuple[bool, str]:
    html = await fetch_html(f"{BASE_URL}/movies/{slug}")
    return _parse_showtime_page(html, halls)


def halls_label(halls: list[str]) -> str:
    return ", ".join(halls) if halls else "যেকোনো হল"


# ─── Bot handlers ────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Star Cineplex Bot*\n\n"
        "/movies — সব মুভির লিস্ট\n"
        "/alert `slug` \\[hall\\] — alert দাও\n"
        "  _উদাহরণ:_ `/alert avengers-doomsday shimanto`\n"
        "  _হল ছাড়া:_ `/alert avengers-doomsday`\n"
        "/alerts — active alerts\n"
        "/cancel `slug` — alert বন্ধ করো\n"
        "/check `slug` \\[hall\\] — এখনই status দেখো",
        parse_mode="MarkdownV2"
    )


async def cmd_movies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        movies = await fetch_movies()
    except Exception as e:
        await update.message.reply_text(f"❌ Fetch failed: {e}")
        return

    now  = [m for m in movies if m["status"] == "Now Showing"]
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
        disable_web_page_preview=True,
    )


async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/alert avengers-doomsday shimanto`", parse_mode="Markdown")
        return

    slug  = ctx.args[0].strip().lower()
    halls = [a.strip() for a in ctx.args[1:]] if len(ctx.args) > 1 else DEFAULT_HALLS[:]
    chat_id = update.effective_chat.id

    try:
        is_open, h = await fetch_showtime_data(slug, halls)
    except Exception:
        await update.message.reply_text(
            f"❌ `{slug}` পাওয়া যায়নি। slug টা ঠিক আছে?",
            parse_mode="Markdown")
        return

    async with alert_lock:
        if slug not in active_alerts:
            active_alerts[slug] = {}
        active_alerts[slug][chat_id] = {"hash": h, "halls": halls}

    hall_txt = halls_label(halls)
    if is_open:
        await update.message.reply_text(
            f"✅ *{slug}* — এখনই booking open! ({hall_txt})\n"
            f"👉 {BASE_URL}/movies/{slug}",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"🔔 Alert set: *{slug}*\n"
            f"🏟 হল: _{hall_txt}_\n"
            f"Booking open হলেই জানাবো।",
            parse_mode="Markdown")


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with alert_lock:
        mine = {
            slug: data[chat_id]
            for slug, data in active_alerts.items()
            if chat_id in data
        }

    if not mine:
        await update.message.reply_text(
            "কোনো active alert নেই। `/alert slug` দিয়ে set করো.",
            parse_mode="Markdown")
        return

    lines = ["🔔 *Active Alerts*"]
    for slug, info in mine.items():
        hall_txt = halls_label(info["halls"])
        lines.append(f"• `{slug}` — _{hall_txt}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/cancel avengers-doomsday`", parse_mode="Markdown")
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
        await update.message.reply_text(
            "Usage: `/check avengers-doomsday shimanto`", parse_mode="Markdown")
        return

    slug  = ctx.args[0].strip().lower()
    halls = [a.strip() for a in ctx.args[1:]] if len(ctx.args) > 1 else []

    try:
        is_open, _ = await fetch_showtime_data(slug, halls)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    hall_txt = halls_label(halls)
    if is_open:
        await update.message.reply_text(
            f"✅ *{slug}* — Booking OPEN! ({hall_txt})\n👉 {BASE_URL}/movies/{slug}",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"⏳ *{slug}* — এখনো open হয়নি। ({hall_txt})",
            parse_mode="Markdown")


# ─── Polling loop ────────────────────────────────────────────────

async def polling_loop(app: Application):
    await asyncio.sleep(10)
    while True:
        await asyncio.sleep(CHECK_EVERY)
        async with alert_lock:
            slugs = {
                slug: {cid: dict(info) for cid, info in chats.items()}
                for slug, chats in active_alerts.items()
            }

        for slug, chats in slugs.items():
            # Group by hall config to minimize fetches
            hall_groups: dict[str, list[int]] = {}
            hall_map: dict[int, list[str]] = {}
            for cid, info in chats.items():
                key = "|".join(sorted(info["halls"]))
                hall_groups.setdefault(key, []).append(cid)
                hall_map[cid] = info["halls"]

            for hall_key, cids in hall_groups.items():
                halls = hall_key.split("|") if hall_key else []
                try:
                    is_open, current_hash = await fetch_showtime_data(slug, halls)
                except Exception:
                    continue

                to_notify = []
                async with alert_lock:
                    if slug not in active_alerts:
                        continue
                    for cid in cids:
                        if cid not in active_alerts[slug]:
                            continue
                        if current_hash != active_alerts[slug][cid]["hash"]:
                            active_alerts[slug][cid]["hash"] = current_hash
                            if is_open:
                                to_notify.append((cid, hall_map[cid]))

                for cid, halls_for_cid in to_notify:
                    hall_txt = halls_label(halls_for_cid)
                    try:
                        await app.bot.send_message(
                            chat_id=cid,
                            text=(
                                f"🎟 *BOOKING OPEN!*\n\n"
                                f"*{slug}* এর টিকেট এখন available!\n"
                                f"🏟 হল: _{hall_txt}_\n\n"
                                f"👉 {BASE_URL}/movies/{slug}"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass


# ─── Ping server ─────────────────────────────────────────────────

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

    async def post_init(app: Application):
        asyncio.create_task(polling_loop(app))

    app.post_init = post_init
    print("[*] Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
