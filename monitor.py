import os
import asyncio
import hashlib
import httpx
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup

# --- CONFIG ---
TICKET_URL    = "https://starcineplex.com/movies/avengers-doomsday"
CHECK_EVERY   = 10
TG_TOKEN      = os.environ["TG_TOKEN"]
# comma দিয়ে multiple ID: "123456,789012"
TG_CHAT_IDS   = [cid.strip() for cid in os.environ["TG_CHAT_ID"].split(",")]

CLOSED_TEXT       = "Bookings open closer to release"
SHOWTIME_SELECTOR = "#showtimes"
TARGET_BRANCH     = "Narayanganj"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
# --- END CONFIG ---


async def fetch_showtime_section(client: httpx.AsyncClient):
    r = await client.get(TICKET_URL, headers=HEADERS, follow_redirects=True, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    section = soup.select_one(SHOWTIME_SELECTOR)
    section_text = section.get_text(separator="\n", strip=True) if section else ""

    is_closed    = CLOSED_TEXT.lower() in section_text.lower()
    nj_available = TARGET_BRANCH.lower() in section_text.lower() and "stub" in r.text.lower()
    h            = hashlib.sha256(section_text.encode()).hexdigest()

    return (not is_closed), nj_available, section_text[:500], h


async def send_tg(client: httpx.AsyncClient, msg: str):
    for cid in TG_CHAT_IDS:
        try:
            await client.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            print(f"[!] TG send fail ({cid}): {e}")


async def monitor():
    last_hash = None
    was_open  = False

    async with httpx.AsyncClient() as client:
        print("[*] Monitoring:", TICKET_URL)
        print(f"[*] Sending alerts to {len(TG_CHAT_IDS)} chat(s)")

        try:
            is_open, nj_ok, _, last_hash = await fetch_showtime_section(client)
            print(f"[*] Baseline — open={is_open} NJ={nj_ok} hash={last_hash[:10]}…")
            was_open = is_open
        except Exception as e:
            print(f"[!] Baseline fail: {e}")

        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                is_open, nj_ok, _, current_hash = await fetch_showtime_section(client)

                if current_hash != last_hash:
                    print(f"[!!!] PAGE CHANGED {last_hash[:8]} → {current_hash[:8]}")
                    last_hash = current_hash

                    nj_line = "✅ Shimanto Tower, Narayanganj — available!" if nj_ok \
                              else "⚠️ Check manually — NJ not confirmed yet"

                    if is_open and not was_open:
                        msg = (
                            f"🎟 <b>AVENGERS DOOMSDAY — BOOKING OPEN!</b>\n\n"
                            f"{nj_line}\n\n"
                            f"👉 {TICKET_URL}"
                        )
                        await send_tg(client, msg)
                        print("[!!!] ALERT SENT")
                        was_open = True

                    elif is_open and was_open:
                        msg = (
                            f"🔄 <b>Doomsday page updated</b> (booking already live)\n"
                            f"{nj_line}\n👉 {TICKET_URL}"
                        )
                        await send_tg(client, msg)
                        print("[!] PAGE UPDATED (already open)")

                else:
                    status = "OPEN" if is_open else "closed"
                    print(f"[.] {current_hash[:10]}… {status} — no change")

            except Exception as e:
                print(f"[!] Error: {e}")


# --- HTTP server for Render web service ---
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alive")
    def log_message(self, *args):
        pass

def start_server():
    HTTPServer(("0.0.0.0", 10000), PingHandler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    asyncio.run(monitor())
