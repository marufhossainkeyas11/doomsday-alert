import httpx, random, string, hashlib, re, json
from bs4 import BeautifulSoup

BASE_URL = "https://starcineplex.com"

RSC_HEADERS = {
    "rsc": "1",
    "next-router-state-tree": "%5B%22%22%2C%7B%7D%2Cnull%2C%22metadata-only%22%5D",
    "next-url": "/",
    "next-router-prefetch": "1",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Mobile Safari/537.36",
    "Referer": "https://starcineplex.com/",
    "Accept-Encoding": "gzip, deflate, br",
}

def _rsc_token() -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=16))

async def fetch_rsc(path: str, params: dict = {}) -> str:
    p = {**params, "_rsc": _rsc_token()}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(f"{BASE_URL}{path}", headers=RSC_HEADERS, params=p)
        r.raise_for_status()
        return r.text

# ── movie list ──────────────────────────────────────────────────

def _parse_movies_rsc(text: str, status: str) -> list[dict]:
    """
    RSC flight এ movie data আসে এরকম JSON chunk এ:
    {"slug":"avengers-doomsday","title":"..."}
    slug আছে এমন সব object নাও।
    """
    movies = []
    seen = set()
    for line in text.splitlines():
        raw = re.sub(r'^\w+:', '', line, count=1).strip()
        for chunk in re.findall(r'\{[^{}]+\}', raw):
            try:
                obj = json.loads(chunk)
                slug = obj.get('slug') or obj.get('id')
                if slug and slug not in seen:
                    seen.add(slug)
                    movies.append({
                        "slug":   slug,
                        "title":  obj.get('title') or obj.get('name') or slug,
                        "status": status,
                        "genres": obj.get('genres', []),
                    })
            except Exception:
                pass
    return movies

async def fetch_movies() -> list[dict]:
    ns   = await fetch_rsc("/movies", {"status": "now-showing"})
    soon = await fetch_rsc("/movies", {"status": "coming-soon"})
    
    now_movies  = _parse_movies_rsc(ns,   "Now Showing")
    soon_movies = _parse_movies_rsc(soon, "Coming Soon")
    
    # fallback: RSC তে slug না পেলে performance entries থেকে জানা slugs
    return now_movies + soon_movies

# ── showtime ────────────────────────────────────────────────────

async def fetch_showtime_data(slug: str, halls: list[str]) -> tuple[bool, str]:
    text = await fetch_rsc(f"/movies/{slug}")
    return _parse_showtime_page(text, halls)  # existing function same থাকবে

# ── debug ───────────────────────────────────────────────────────

async def cmd_debug(update, _):
    msg = await update.message.reply_text("⏳ fetching...")
    try:
        raw = await fetch_rsc("/movies", {"status": "now-showing"})
        movies = _parse_movies_rsc(raw, "Now Showing")
        info = (
            f"RSC size: {len(raw)} bytes\n"
            f"Movies parsed: {len(movies)}\n"
            f"First 500 chars:\n{raw[:500]}"
        )
        await msg.edit_text(f"```\n{info}\n```", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")
