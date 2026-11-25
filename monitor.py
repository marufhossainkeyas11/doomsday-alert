import os
import asyncio
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from rebrowser_playwright.async_api import async_playwright, Browser, BrowserContext
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ─── CONFIG ──────────────────────────────────────────────────────
TG_TOKEN    = os.environ["TG_TOKEN"]
BASE_URL    = "https://starcineplex.com"
CHECK_EVERY = 60

DEFAULT_HALLS: list[str] = []

active_alerts: dict[str, dict[int, dict]] = {}
alert_lock = asyncio.Lock()

_browser: Browser | None = None
_context: BrowserContext | None = None
_pw_lock  = asyncio.Lock()

PICK_MOVIE, PICK_HALL = range(2)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
# ─────────────────────────────────────────────────────────────────


async def get_context() -> BrowserContext:
    global _browser, _context
    async with _pw_lock:
        if _context is None:
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            _context = await _browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=HEADERS["User-Agent"],
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        return _context


async def fetch_html(url: str) -> str:
    ctx  = await get_context()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # CF challenge solve হওয়ার জন্য wait
        for _ in range(15):
            content = await page.content()
            if "Just a moment" not in content and len(content) > 10_000:
                break
            await asyncio.sleep(2)
        return await page.content()
    finally:
        await page.close()


# ─── Scraping ────────────────────────────────────────────────────

async def fetch_movies() -> list[dict]:
    html = await fetch_html(f"{BASE_URL}/movies")
    soup = BeautifulSoup(html, "html.parser")
    movies = []
    for wrap in soup.select('div[data-testid="poster-preview-wrap"]'):
        link = wrap.select_one('a[href^="/movies/"]')
        if not link:
            continue
        slug   = link["href"].replace("/movies/", "").strip("/")
        h3     = wrap.select_one("h3 a")
        title  = h3.get_text(strip=True) if h3 else slug
        badge  = wrap.select_one(".editorial-eyebrow--night")
        status = badge.get_text(strip=True) if badge else "Now Showing"
        genres = [
            s.get_text(strip=True)
            for s in wrap.select('span[data-genre][aria-hidden="true"]')
        ]
        movies.append({"slug": slug, "title": title, "status": status, "genres": genres})
    return movies


async def cmd_debug(update: Update, _: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ fetching...")
    try:
        html = await fetch_html(f"{BASE_URL}/movies")
        soup = BeautifulSoup(html, "html.parser")

        wraps    = soup.select('div[data-testid="poster-preview-wrap"]')
        articles = soup.find_all("article")
        links    = soup.select('a[href^="/movies/"]')
        title    = soup.select_one("title")
        html_len = len(html)

        info = (
            f"HTML size: {html_len} bytes\n"
            f"Title: {title.get_text() if title else 'none'}\n"
            f"poster-preview-wrap: {len(wraps)}\n"
            f"articles: {len(articles)}\n"
            f"movie links: {len(links)}\n"
            f"First link: {links[0]['href'] if links else 'none'}\n"
            f"First wrap classes: {wraps[0].get('class') if wraps else 'none'}"
        )
        await msg.edit_text(f"```\n{info}\n```", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


def _parse_showtime_page(html: str, halls: list[str]) -> tuple[bool, str]:
    soup    = BeautifulSoup(html, "html.parser")
    section = soup.select_one("#showtimes")
    text    = section.get_text(separator="\n", strip=True) if section else ""
    h       = hashlib.sha256(text.encode()).hexdigest()

    if not section or not text:
        return False, h
    if "Bookings open closer to release" in text:
        return False, h

    all_stubs = soup.select(".stub")
    if not all_stubs:
        return False, h

    if not halls:
        open_stubs = [s for s in all_stubs if "stub--closed" not in s.get("class", [])]
        return bool(open_stubs), h

    for branch in section.select("li"):
        h3 = branch.select_one("h3")
        if not h3:
            continue
        if not any(kw.lower() in h3.get_text(strip=True).lower() for kw in halls):
            continue
        open_stubs = [
            s for s in branch.select(".stub")
            if "stub--closed" not in s.get("class", [])
        ]
        if open_stubs:
            return True, h

    return False, h


async def fetch_showtime_data(slug: str, halls: list[str]) -> tuple[bool, str]:
    html = await fetch_html(f"{BASE_URL}/movies/{slug}")
    return _parse_showtime_page(html, halls)


def halls_label(halls: list[str]) -> str:
    return ", ".join(halls) if halls else "যেকোনো হল"


def movie_keyboard(movies: list[dict], prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(m["title"], callback_data=f"{prefix}:{m['slug']}")
        for m in movies
    ]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


# ─── /start ──────────────────────────────────────────────────────

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Star Cineplex Bot*\n\n"
        "/movies — সব মুভি দেখো\n"
        "/alert — booking alert সেট করো\n"
        "/alerts — active alerts\n"
        "/cancel — alert বন্ধ করো\n"
        "/check `slug` \\[hall\\] — status দেখো",
        parse_mode="Markdown",
    )


# ─── /movies ─────────────────────────────────────────────────────

async def cmd_movies(update: Update, _: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ লোড হচ্ছে…")
    try:
        movies = await fetch_movies()
    except Exception as e:
        await msg.edit_text(f"❌ Fetch failed: {e}")
        return

    if not movies:
        await msg.edit_text("কোনো মুভি পাওয়া গেল না।")
        return

    now  = [m for m in movies if m["status"] == "Now Showing"]
    soon = [m for m in movies if m["status"] != "Now Showing"]

    lines = []
    if now:
        lines.append("🎬 *Now Showing*")
        for m in now:
            g = ", ".join(m["genres"][:2]) or "—"
            lines.append(f"• [{m['title']}]({BASE_URL}/movies/{m['slug']}) — _{g}_")
    if soon:
        lines.append("\n🔜 *Coming Soon*")
        for m in soon:
            g = ", ".join(m["genres"][:2]) or "—"
            lines.append(f"• [{m['title']}]({BASE_URL}/movies/{m['slug']}) — _{g}_")

    kb = movie_keyboard(movies, "quickalert")
    await msg.edit_text(
        "\n".join(lines) + "\n\n_নিচ থেকে সরাসরি alert দাও:_",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


# ─── /alert conversation ─────────────────────────────────────────

async def cmd_alert_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        slug  = ctx.args[0].strip().lower()
        halls = [a.strip() for a in ctx.args[1:]]
        await _do_set_alert(update, ctx, slug, halls)
        return ConversationHandler.END

    msg = await update.message.reply_text("⏳ মুভির লিস্ট আনছি…")
    try:
        movies = await fetch_movies()
    except Exception as e:
        await msg.edit_text(f"❌ {e}")
        return ConversationHandler.END

    if not movies:
        await msg.edit_text("মুভি পাওয়া গেল না।")
        return ConversationHandler.END

    ctx.user_data["movies"] = movies
    kb = movie_keyboard(movies, "alertpick")
    await msg.edit_text("কোন মুভিতে alert দেবে?", reply_markup=kb)
    return PICK_MOVIE


async def cb_alert_pick_movie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slug  = query.data.split(":", 1)[1]
    ctx.user_data["alert_slug"] = slug
    movies = ctx.user_data.get("movies", [])
    title  = next((m["title"] for m in movies if m["slug"] == slug), slug)
    await query.edit_message_text(
        f"*{title}*\n\nকোন হলের জন্য? হলের নাম টাইপ করো (যেমন `shimanto`).\n"
        "যেকোনো হলের জন্য /skip দাও।",
        parse_mode="Markdown",
    )
    return PICK_HALL


async def cb_alert_hall_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    slug  = ctx.user_data.get("alert_slug")
    halls = [h.strip() for h in update.message.text.split(",") if h.strip()]
    await _do_set_alert(update, ctx, slug, halls)
    return ConversationHandler.END


async def cmd_alert_skip_hall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    slug = ctx.user_data.get("alert_slug")
    await _do_set_alert(update, ctx, slug, [])
    return ConversationHandler.END


async def _do_set_alert(
    update: Update, _: ContextTypes.DEFAULT_TYPE,
    slug: str, halls: list[str],
):
    chat_id = update.effective_chat.id
    send    = update.message.reply_text if update.message else update.callback_query.message.reply_text

    wait = await send("⏳ চেক করছি…")
    try:
        is_open, h = await fetch_showtime_data(slug, halls)
    except Exception:
        await wait.edit_text(f"❌ `{slug}` পাওয়া যায়নি।", parse_mode="Markdown")
        return

    async with alert_lock:
        active_alerts.setdefault(slug, {})[chat_id] = {"hash": h, "halls": halls}

    hall_txt = halls_label(halls)
    if is_open:
        await wait.edit_text(
            f"✅ *{slug}* — এখনই booking open! ({hall_txt})\n👉 {BASE_URL}/movies/{slug}",
            parse_mode="Markdown",
        )
    else:
        await wait.edit_text(
            f"🔔 Alert set: *{slug}*\n🏟 হল: _{hall_txt}_\nBooking open হলেই জানাবো।",
            parse_mode="Markdown",
        )


async def cb_quickalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slug = query.data.split(":", 1)[1]
    await _do_set_alert(update, ctx, slug, [])


# ─── /alerts ─────────────────────────────────────────────────────

async def cmd_alerts(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with alert_lock:
        mine = {
            slug: data[chat_id]
            for slug, data in active_alerts.items()
            if chat_id in data
        }
    if not mine:
        await update.message.reply_text("কোনো active alert নেই। /alert দিয়ে set করো।")
        return

    lines   = ["🔔 *Active Alerts*"]
    buttons = []
    for slug, info in mine.items():
        lines.append(f"• `{slug}` — _{halls_label(info['halls'])}_")
        buttons.append([InlineKeyboardButton(f"🗑 {slug}", callback_data=f"cancel:{slug}")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─── /cancel ─────────────────────────────────────────────────────

async def cmd_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await _do_cancel(update, update.effective_chat.id, ctx.args[0].strip().lower())
        return

    chat_id = update.effective_chat.id
    async with alert_lock:
        mine = [slug for slug, d in active_alerts.items() if chat_id in d]

    if not mine:
        await update.message.reply_text("কোনো active alert নেই।")
        return

    buttons = [[InlineKeyboardButton(slug, callback_data=f"cancel:{slug}")] for slug in mine]
    await update.message.reply_text("কোনটা বন্ধ করবে?", reply_markup=InlineKeyboardMarkup(buttons))


async def cb_cancel(update: Update, _: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slug = query.data.split(":", 1)[1]
    await _do_cancel(update, query.from_user.id, slug)
    await query.edit_message_reply_markup(reply_markup=None)


async def _do_cancel(update: Update, chat_id: int, slug: str):
    async with alert_lock:
        removed = False
        if slug in active_alerts and chat_id in active_alerts[slug]:
            del active_alerts[slug][chat_id]
            if not active_alerts[slug]:
                del active_alerts[slug]
            removed = True

    reply = update.message or update.callback_query.message
    text  = f"🗑 Alert cancelled: `{slug}`" if removed else f"❓ `{slug}` এর কোনো alert ছিল না।"
    await reply.reply_text(text, parse_mode="Markdown")


# ─── /check ──────────────────────────────────────────────────────

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/check avengers-doomsday shimanto`", parse_mode="Markdown"
        )
        return

    slug  = ctx.args[0].strip().lower()
    halls = [a.strip() for a in ctx.args[1:]]
    msg   = await update.message.reply_text("⏳ চেক করছি…")

    try:
        is_open, _ = await fetch_showtime_data(slug, halls)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
        return

    hall_txt = halls_label(halls)
    if is_open:
        await msg.edit_text(
            f"✅ *{slug}* — Booking OPEN! ({hall_txt})\n👉 {BASE_URL}/movies/{slug}",
            parse_mode="Markdown",
        )
    else:
        await msg.edit_text(
            f"⏳ *{slug}* — এখনো open হয়নি। ({hall_txt})",
            parse_mode="Markdown",
        )


# ─── Polling loop ────────────────────────────────────────────────

async def polling_loop(app: Application):
    await asyncio.sleep(15)
    while True:
        await asyncio.sleep(CHECK_EVERY)
        async with alert_lock:
            snapshot = {
                slug: {cid: dict(info) for cid, info in chats.items()}
                for slug, chats in active_alerts.items()
            }

        for slug, chats in snapshot.items():
            hall_groups: dict[str, list[int]] = {}
            hall_map:   dict[int, list[str]]  = {}
            for cid, info in chats.items():
                key = "|".join(sorted(info["halls"]))
                hall_groups.setdefault(key, []).append(cid)
                hall_map[cid] = info["halls"]

            for hall_key, cids in hall_groups.items():
                halls = hall_key.split("|") if hall_key else []
                try:
                    is_open, cur_hash = await fetch_showtime_data(slug, halls)
                except Exception:
                    continue

                to_notify: list[tuple[int, list[str]]] = []
                async with alert_lock:
                    if slug not in active_alerts:
                        continue
                    for cid in cids:
                        if cid not in active_alerts[slug]:
                            continue
                        if cur_hash != active_alerts[slug][cid]["hash"]:
                            active_alerts[slug][cid]["hash"] = cur_hash
                            if is_open:
                                to_notify.append((cid, hall_map[cid]))

                for cid, halls_for_cid in to_notify:
                    try:
                        await app.bot.send_message(
                            chat_id=cid,
                            text=(
                                f"🎟 *BOOKING OPEN!*\n\n"
                                f"*{slug}* এর টিকেট এখন available!\n"
                                f"🏟 হল: _{halls_label(halls_for_cid)}_\n\n"
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
    def log_message(self, *_):
        pass


# ─── Main ─────────────────────────────────────────────────────────

def main():
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", 10000), PingHandler).serve_forever(),
        daemon=True,
    ).start()

    app = Application.builder().token(TG_TOKEN).build()

    alert_conv = ConversationHandler(
        entry_points=[CommandHandler("alert", cmd_alert_start)],
        states={
            PICK_MOVIE: [CallbackQueryHandler(cb_alert_pick_movie, pattern=r"^alertpick:")],
            PICK_HALL:  [
                CommandHandler("skip", cmd_alert_skip_hall),
                MessageHandler(filters.TEXT & ~filters.COMMAND, cb_alert_hall_input),
            ],
        },
        fallbacks=[CommandHandler("skip", cmd_alert_skip_hall)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("movies",  cmd_movies))
    app.add_handler(alert_conv)
    app.add_handler(CommandHandler("alerts",  cmd_alerts))
    app.add_handler(CommandHandler("cancel",  cmd_cancel_cmd))
    app.add_handler(CommandHandler("check",   cmd_check))
    app.add_handler(CommandHandler("debug",   cmd_debug))
    app.add_handler(CallbackQueryHandler(cb_quickalert, pattern=r"^quickalert:"))
    app.add_handler(CallbackQueryHandler(cb_cancel,     pattern=r"^cancel:"))

    async def post_init(application: Application):
        asyncio.create_task(polling_loop(application))

    app.post_init = post_init
    print("[*] Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
