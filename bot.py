import os
import json
import random
import string
import asyncio
import base64
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).resolve().parent
load_dotenv(BOT_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
EVOMI_API_KEY = os.getenv("EVOMI_API_KEY", "")
PROXIDIZE_API_TOKEN = os.getenv("PROXIDIZE_API_TOKEN", "")
OXYLABS_MOBILE_USERNAME = os.getenv("OXYLABS_MOBILE_USERNAME", "")
OXYLABS_MOBILE_PASSWORD = os.getenv("OXYLABS_MOBILE_PASSWORD", "")
OXYLABS_RESIDENTIAL_USERNAME = os.getenv("OXYLABS_RESIDENTIAL_USERNAME", "")
OXYLABS_RESIDENTIAL_PASSWORD = os.getenv("OXYLABS_RESIDENTIAL_PASSWORD", "")

EVOMI_ENABLED = bool(EVOMI_API_KEY)
PROXIDIZE_ENABLED = bool(PROXIDIZE_API_TOKEN)
OXYLABS_MOBILE_ENABLED = bool(OXYLABS_MOBILE_USERNAME)
OXYLABS_RESI_ENABLED = bool(OXYLABS_RESIDENTIAL_USERNAME)

# Colors
CLR_EVOMI = 0x0099FF
CLR_PROXIDIZE = 0x7B2FBE
CLR_OXYLABS = 0x00A86B
CLR_SUCCESS = 0x00CC66
CLR_ERROR = 0xFF4444

MAX_LIFETIME = 300

# ---------------------------------------------------------------------------
# Data file helpers
# ---------------------------------------------------------------------------
DATA_FILES = [
    "evomi_alerts.json", "proxidize_alerts.json",
    "oxylabs_mobile_alerts.json", "oxylabs_resi_alerts.json",
    "data_caps.json", "proxidize_caps.json",
    "oxylabs_mobile_caps.json", "oxylabs_resi_caps.json",
    "evomi_history.json", "proxidize_history.json",
    "oxylabs_mobile_history.json", "oxylabs_resi_history.json",
]


def _ensure_data_files():
    for f in DATA_FILES:
        p = BOT_DIR / f
        if not p.exists():
            p.write_text("{}")


def load_json(name: str) -> dict:
    try:
        return json.loads((BOT_DIR / name).read_text())
    except Exception:
        (BOT_DIR / name).write_text("{}")
        return {}


def save_json(name: str, data: dict):
    try:
        (BOT_DIR / name).write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def rand8() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def footer(embed: discord.Embed):
    embed.set_footer(text=f"ProxyBot • {now_ts()}")
    return embed


def not_configured_embed(provider: str) -> discord.Embed:
    e = discord.Embed(
        title="⚠️ Provider Not Configured",
        description=f"**{provider}** is not configured. Add credentials to `.env` to enable.",
        color=CLR_ERROR,
    )
    return footer(e)


def lifetime_blocked_embed() -> discord.Embed:
    e = discord.Embed(
        title="❌ Session Too Long",
        description=(
            "Sessions over 300 minutes are blocked.\n"
            "Real-world testing shows long sessions get stuck on flagged IPs.\n\n"
            "✅ **Recommended:** 120-300 min for best IP freshness.\n"
            "Use `/proxy_best_practices` for full guidance."
        ),
        color=CLR_ERROR,
    )
    return footer(e)


def lifetime_note(minutes: int) -> str:
    if minutes <= 60:
        return "💡 Good for scraping/testing. For account tasks like Walmart, 120-300 min gives better results."
    if minutes <= 120:
        return ""
    if minutes <= 300:
        return "✅ Optimal session range"
    return ""


def session_summary_fields(embed: discord.Embed, lifetime: int, provider: str):
    embed.add_field(name="Session Length", value=f"{lifetime} min", inline=True)
    embed.add_field(name="Pool Filters", value="None (full pool — optimal)", inline=True)
    embed.add_field(name="IP Priority", value="Fresh → Clean → Fast", inline=True)
    embed.add_field(name="Provider", value=provider, inline=True)
    note = lifetime_note(lifetime)
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    if 120 <= lifetime <= 300:
        embed.add_field(name="", value="✅ Optimal session range", inline=False)
    embed.add_field(name="", value="✅ Full pool access — maximum IP freshness", inline=False)


FORMAT_CHOICES = [
    app_commands.Choice(name="user:pass@host:port", value=1),
    app_commands.Choice(name="host:port:user:pass", value=2),
    app_commands.Choice(name="user:pass:host:port", value=3),
]


def format_proxy(user: str, pwd: str, host: str, port: int, fmt: int, protocol: str = "http") -> str:
    prefix = ""
    if protocol == "socks5":
        prefix = "socks5://"
    if fmt == 1:
        return f"{prefix}{user}:{pwd}@{host}:{port}"
    if fmt == 3:
        return f"{prefix}{user}:{pwd}:{host}:{port}"
    # default fmt 2
    return f"{prefix}{host}:{port}:{user}:{pwd}"


def parse_proxy(line: str):
    """Parse any of the 3 proxy formats. Returns (user, pwd, host, port, protocol) or None."""
    line = line.strip()
    if not line:
        return None
    protocol = "http"
    if line.startswith("socks5://"):
        protocol = "socks5"
        line = line[len("socks5://"):]
    elif line.startswith("http://"):
        line = line[len("http://"):]
    # Format 1: user:pass@host:port
    if "@" in line:
        userpass, hostport = line.rsplit("@", 1)
        parts_hp = hostport.split(":")
        parts_up = userpass.split(":", 1)
        if len(parts_hp) == 2 and len(parts_up) == 2:
            return parts_up[0], parts_up[1], parts_hp[0], int(parts_hp[1]), protocol
    # Format 2: host:port:user:pass  or  Format 3: user:pass:host:port
    parts = line.split(":")
    if len(parts) == 4:
        # Try format 2 first (host:port:user:pass)
        try:
            port = int(parts[1])
            return parts[2], parts[3], parts[0], port, protocol
        except ValueError:
            pass
        # Try format 3 (user:pass:host:port)
        try:
            port = int(parts[3])
            return parts[0], parts[1], parts[2], port, protocol
        except ValueError:
            pass
    return None


def progress_bar(used: float, total: float, length: int = 10) -> str:
    if total <= 0:
        return "░" * length
    ratio = min(used / total, 1.0)
    filled = int(ratio * length)
    return "█" * filled + "░" * (length - filled)


# ---------------------------------------------------------------------------
# Cooldown decorator
# ---------------------------------------------------------------------------
cooldown_buckets: dict[str, dict[int, float]] = {}


def check_cooldown(command_name: str, user_id: int, seconds: int) -> float:
    """Returns 0 if OK, else seconds remaining."""
    bucket = cooldown_buckets.setdefault(command_name, {})
    now = datetime.now(timezone.utc).timestamp()
    last = bucket.get(user_id, 0)
    remaining = seconds - (now - last)
    if remaining > 0:
        return remaining
    bucket[user_id] = now
    return 0


# ---------------------------------------------------------------------------
# Oxylabs JWT management
# ---------------------------------------------------------------------------
oxylabs_mobile_jwt = {"token": None, "user_id": None, "expires_at": None}
oxylabs_resi_jwt = {"token": None, "user_id": None, "expires_at": None}


async def oxylabs_login(session: aiohttp.ClientSession, username: str, password: str) -> dict | None:
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    try:
        async with session.post(
            "https://residential-api.oxylabs.io/v2/login",
            headers={"Authorization": f"Basic {creds}"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "token": data.get("token"),
                    "user_id": data.get("user_id"),
                    "expires_at": datetime.now(timezone.utc) + timedelta(minutes=55),
                }
    except Exception:
        pass
    return None


async def ensure_jwt(session: aiohttp.ClientSession, jwt_store: dict, username: str, password: str) -> bool:
    if jwt_store["token"] and jwt_store["expires_at"] and jwt_store["expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=5):
        return True
    result = await oxylabs_login(session, username, password)
    if result:
        jwt_store.update(result)
        return True
    return False


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
http_session: aiohttp.ClientSession | None = None


@bot.event
async def on_ready():
    global http_session
    http_session = aiohttp.ClientSession()
    _ensure_data_files()

    # Oxylabs logins
    if OXYLABS_MOBILE_ENABLED:
        ok = await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
        print(f"  Oxylabs Mobile:     {'✅ connected' if ok else '⚠️ login failed'}")
    else:
        print("  Oxylabs Mobile:     ⚠️ no credentials")

    if OXYLABS_RESI_ENABLED:
        ok = await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
        print(f"  Oxylabs Residential:{'✅ connected' if ok else '⚠️ login failed'}")
    else:
        print("  Oxylabs Residential:⚠️ no credentials")

    # Proxidize check
    if PROXIDIZE_ENABLED:
        try:
            async with http_session.get(
                "https://api.proxidize.com/api/v1/pergb/mobile/user-info",
                headers={"Authorization": f"Bearer {PROXIDIZE_API_TOKEN}"},
            ) as resp:
                print(f"  Proxidize:          {'✅ connected' if resp.status == 200 else '⚠️ connection failed'}")
        except Exception:
            print("  Proxidize:          ⚠️ connection failed")
    else:
        print("  Proxidize:          ⚠️ no credentials")

    print("=================================================")
    print("ProxyBot is online!")
    print(f"  Evomi:              {'✅ enabled' if EVOMI_ENABLED else '⚠️ no credentials'}")
    print(f"  Proxidize:          {'✅ enabled' if PROXIDIZE_ENABLED else '⚠️ no credentials'}")
    print(f"  Oxylabs Mobile:     {'✅ enabled' if OXYLABS_MOBILE_ENABLED else '⚠️ no credentials'}")
    print(f"  Oxylabs Residential:{'✅ enabled' if OXYLABS_RESI_ENABLED else '⚠️ no credentials'}")
    print("  Background tasks:   running (staggered)")
    print("=================================================")

    # Start background tasks
    if not evomi_background.is_running():
        evomi_background.start()
    if not proxidize_background.is_running():
        proxidize_background.start()
    if not oxylabs_mobile_background.is_running():
        oxylabs_mobile_background.start()
    if not oxylabs_resi_background.is_running():
        oxylabs_resi_background.start()
    if not jwt_refresh_task.is_running():
        jwt_refresh_task.start()

    try:
        guild_id = os.getenv("DISCORD_GUILD_ID", "")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash commands to guild.")
        # Also sync globally (takes longer to propagate)
        await bot.tree.sync()
    except Exception as ex:
        print(f"Failed to sync commands: {ex}")


# ===================================================================
# EVOMI COMMANDS
# ===================================================================
EVOMI_BASE = "https://api.evomi.com/public"
EVOMI_PRODUCTS = {
    "rp": "Premium Residential",
    "rpc": "Core Residential",
    "mp": "Mobile",
    "dcp": "Datacenter",
}


def evomi_parse_products(data: dict) -> list[dict]:
    """Parse Evomi API response into a flat list of product dicts."""
    products_raw = data.get("products", {})
    if isinstance(products_raw, list):
        return products_raw
    result = []
    for code, info in products_raw.items():
        if code == "static_residential":
            continue
        if not isinstance(info, dict):
            continue
        ports = info.get("ports", {})
        result.append({
            "code": code,
            "name": EVOMI_PRODUCTS.get(code, code),
            "balance_mb": float(info.get("balance_mb", 0)),
            "endpoint": info.get("endpoint", "N/A"),
            "http_port": ports.get("http", "N/A"),
            "socks5_port": ports.get("socks5", "N/A"),
            "username": info.get("username", ""),
            "password": info.get("password", ""),
        })
    return result

EVOMI_PRODUCT_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in EVOMI_PRODUCTS.items()]


async def evomi_get(path: str = "") -> dict | None:
    url = f"{EVOMI_BASE}{path}"
    sep = "&" if "?" in url else "?"
    url += f"{sep}apikey={EVOMI_API_KEY}"
    try:
        async with http_session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None


@bot.tree.command(name="evomi_status", description="Show Evomi account status for all products")
async def evomi_status(interaction: discord.Interaction):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    await interaction.response.defer()
    data = await evomi_get()
    if not data:
        e = discord.Embed(title="❌ Evomi API Error", description="Could not reach Evomi API.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    e = discord.Embed(title="📡 Evomi Status", color=CLR_EVOMI)
    products = evomi_parse_products(data)

    for prod in products:
        balance_mb = prod["balance_mb"]
        balance_gb = balance_mb / 1024

        if balance_mb > 1000:
            icon = "🟢"
        elif balance_mb >= 100:
            icon = "🟡"
        else:
            icon = "🔴"

        e.add_field(
            name=f"{icon} {prod['name']} ({prod['code']})",
            value=(
                f"Balance: **{balance_mb:.2f} MB** ({balance_gb:.2f} GB)\n"
                f"Endpoint: `{prod['endpoint']}`\n"
                f"HTTP: `{prod['http_port']}` | SOCKS5: `{prod['socks5_port']}`"
            ),
            inline=False,
        )
    e.set_footer(text=f"Last checked: {now_ts()}")
    await interaction.followup.send(embed=e)


@bot.tree.command(name="evomi_generate", description="Generate Evomi proxies")
@app_commands.describe(
    product="Proxy product type",
    amount="Number of proxies (no limit)",
    countries="Country codes e.g. US,DE",
    session="Session type",
    protocol="Protocol",
    format="Output format",
    lifetime="Session lifetime in minutes (1-300)",
    activesince="Min node connection time in minutes (e.g. 60) — great for Walmart",
)
@app_commands.choices(
    product=EVOMI_PRODUCT_CHOICES,
    session=[
        app_commands.Choice(name="none", value="none"),
        app_commands.Choice(name="sticky", value="sticky"),
        app_commands.Choice(name="hard", value="hard"),
    ],
    protocol=[
        app_commands.Choice(name="http", value="http"),
        app_commands.Choice(name="socks5", value="socks5"),
    ],
    format=FORMAT_CHOICES,
)
async def evomi_generate(
    interaction: discord.Interaction,
    product: app_commands.Choice[str],
    amount: int = 10,
    countries: str = "US",
    session: app_commands.Choice[str] = None,
    protocol: app_commands.Choice[str] = None,
    format: app_commands.Choice[int] = None,
    lifetime: int = 120,
    activesince: int = None,
):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    if lifetime > MAX_LIFETIME:
        return await interaction.response.send_message(embed=lifetime_blocked_embed())
    cd = check_cooldown("evomi_generate", interaction.user.id, 5)
    if cd > 0:
        e = discord.Embed(title="⏳ Cooldown", description=f"Please wait {cd:.1f}s", color=CLR_ERROR)
        return await interaction.response.send_message(embed=footer(e), ephemeral=True)

    await interaction.response.defer()
    amount = max(1, amount)
    sess_val = session.value if session else "sticky"
    proto_val = protocol.value if protocol else "http"
    fmt_val = format.value if format else 2

    params = {
        "apikey": EVOMI_API_KEY,
        "product": product.value,
        "amount": amount,
        "countries": countries,
        "session": sess_val,
        "protocol": proto_val,
        "lifetime": lifetime,
        "prepend_protocol": "false",
    }
    try:
        async with http_session.get(f"{EVOMI_BASE}/generate", params=params) as resp:
            if resp.status != 200:
                txt = await resp.text()
                e = discord.Embed(title="❌ Evomi Generate Failed", description=f"Status {resp.status}: {txt[:500]}", color=CLR_ERROR)
                return await interaction.followup.send(embed=footer(e))
            txt = await resp.text()
            proxies = [p.strip() for p in txt.strip().splitlines() if p.strip()]
    except Exception as ex:
        e = discord.Embed(title="❌ Evomi API Error", description=str(ex)[:500], color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    # Reformat if needed, append activesince to password
    pwd_suffix = f"_activesince-{activesince}" if activesince else ""
    formatted = []
    for p in proxies:
        parsed = parse_proxy(str(p))
        if parsed:
            u, pw, h, pt, _ = parsed
            formatted.append(format_proxy(u, pw + pwd_suffix, h, pt, fmt_val, proto_val))
        else:
            formatted.append(str(p))

    proxy_text = "\n".join(formatted)

    e = discord.Embed(title="✅ Evomi Proxies Generated", color=CLR_EVOMI)
    e.add_field(name="Product", value=EVOMI_PRODUCTS.get(product.value, product.value), inline=True)
    e.add_field(name="Amount", value=str(len(formatted)), inline=True)
    e.add_field(name="Countries", value=countries, inline=True)
    e.add_field(name="Session", value=sess_val, inline=True)
    e.add_field(name="Protocol", value=proto_val, inline=True)
    session_summary_fields(e, lifetime, "Evomi")

    if len(proxy_text) <= 1900:
        e.description = f"```\n{proxy_text}\n```"
        await interaction.followup.send(embed=footer(e))
    else:
        buf = io.BytesIO(proxy_text.encode())
        file = discord.File(buf, filename="proxies_evomi.txt")
        await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="evomi_generate_spread", description="Generate Evomi proxies with pool spread strategy")
@app_commands.describe(
    product="Proxy product type",
    total_amount="Total proxies (no limit)",
    session="Session type",
    protocol="Protocol",
    format="Output format",
    lifetime="Session lifetime in minutes (1-300)",
)
@app_commands.choices(
    product=EVOMI_PRODUCT_CHOICES,
    session=[
        app_commands.Choice(name="none (rotating)", value="none"),
        app_commands.Choice(name="sticky", value="sticky"),
    ],
    protocol=[
        app_commands.Choice(name="http", value="http"),
        app_commands.Choice(name="socks5", value="socks5"),
    ],
    format=FORMAT_CHOICES,
)
async def evomi_generate_spread(
    interaction: discord.Interaction,
    product: app_commands.Choice[str],
    total_amount: int = 50,
    session: app_commands.Choice[str] = None,
    protocol: app_commands.Choice[str] = None,
    format: app_commands.Choice[int] = None,
    lifetime: int = 120,
):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    if lifetime > MAX_LIFETIME:
        return await interaction.response.send_message(embed=lifetime_blocked_embed())

    await interaction.response.defer()
    total_amount = max(10, total_amount)
    sess_val = session.value if session else "none"
    proto_val = protocol.value if protocol else "http"
    fmt_val = format.value if format else 2

    batch_size = 10
    batches = (total_amount + batch_size - 1) // batch_size
    all_proxies = []

    async def fetch_batch(i):
        params = {
            "apikey": EVOMI_API_KEY,
            "product": product.value,
            "amount": min(batch_size, total_amount - i * batch_size),
            "countries": "US",
            "session": sess_val,
            "protocol": proto_val,
            "lifetime": lifetime,
            "seed": random.randint(1, 999999),
            "prepend_protocol": "false",
        }
        try:
            async with http_session.get(f"{EVOMI_BASE}/generate", params=params) as resp:
                if resp.status == 200:
                    txt = await resp.text()
                    return [p.strip() for p in txt.strip().splitlines() if p.strip()]
        except Exception:
            pass
        return []

    results = await asyncio.gather(*[fetch_batch(i) for i in range(batches)])
    for batch in results:
        all_proxies.extend(batch)

    # Deduplicate
    seen = set()
    unique = []
    for p in all_proxies:
        ps = str(p).strip()
        if ps and ps not in seen:
            seen.add(ps)
            unique.append(ps)

    formatted = []
    for p in unique:
        parsed = parse_proxy(p)
        if parsed:
            u, pw, h, pt, _ = parsed
            formatted.append(format_proxy(u, pw, h, pt, fmt_val, proto_val))
        else:
            formatted.append(p)

    proxy_text = "\n".join(formatted)
    buf = io.BytesIO(proxy_text.encode())
    file = discord.File(buf, filename="proxies_evomi_spread.txt")

    e = discord.Embed(title="✅ Evomi Spread Proxies", color=CLR_EVOMI)
    e.add_field(name="Total", value=str(len(formatted)), inline=True)
    e.add_field(name="Product", value=EVOMI_PRODUCTS.get(product.value, product.value), inline=True)
    e.add_field(name="Unique Sessions", value=str(len(formatted)), inline=True)
    e.add_field(name="Strategy", value=f"{batches} batches × {batch_size} proxies with unique seeds", inline=False)
    e.add_field(name="", value="💡 Full pool, no filters, maximum IP freshness", inline=False)
    session_summary_fields(e, lifetime, "Evomi")
    await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="evomi_rotate", description="Rotate Evomi proxy session")
@app_commands.describe(product="Product to rotate")
@app_commands.choices(product=EVOMI_PRODUCT_CHOICES)
async def evomi_rotate(interaction: discord.Interaction, product: app_commands.Choice[str]):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    await interaction.response.defer()
    try:
        async with http_session.get(
            f"{EVOMI_BASE}/rotate", params={"apikey": EVOMI_API_KEY, "product": product.value}
        ) as resp:
            if resp.status == 200:
                e = discord.Embed(title="✅ Session Rotated", description=f"Product: **{EVOMI_PRODUCTS[product.value]}**", color=CLR_SUCCESS)
            else:
                txt = await resp.text()
                e = discord.Embed(title="❌ Rotation Failed", description=txt[:500], color=CLR_ERROR)
    except Exception as ex:
        e = discord.Embed(title="❌ Rotation Error", description=str(ex)[:500], color=CLR_ERROR)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="evomi_balance", description="Show Evomi balance for all products")
async def evomi_balance(interaction: discord.Interaction):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    await interaction.response.defer()
    data = await evomi_get()
    if not data:
        e = discord.Embed(title="❌ Evomi API Error", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    caps = load_json("data_caps.json")
    e = discord.Embed(title="💰 Evomi Balance", color=CLR_EVOMI)
    products = evomi_parse_products(data)

    for prod in products:
        balance_mb = prod["balance_mb"]
        balance_gb = balance_mb / 1024
        cap = caps.get(prod["code"])
        bar = ""
        if cap:
            cap_mb = cap * 1024
            bar = f"\n{progress_bar(cap_mb - balance_mb, cap_mb)} ({balance_gb:.2f} / {cap:.2f} GB)"
        e.add_field(name=f"{prod['name']} ({prod['code']})", value=f"**{balance_mb:.2f} MB** ({balance_gb:.2f} GB){bar}", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="evomi_set_cap", description="Set a usage cap for an Evomi product")
@app_commands.describe(product="Product", cap_gb="Cap in GB")
@app_commands.choices(product=EVOMI_PRODUCT_CHOICES)
async def evomi_set_cap(interaction: discord.Interaction, product: app_commands.Choice[str], cap_gb: float):
    caps = load_json("data_caps.json")
    caps[product.value] = cap_gb
    save_json("data_caps.json", caps)
    e = discord.Embed(title="✅ Cap Set", description=f"**{EVOMI_PRODUCTS[product.value]}**: {cap_gb:.2f} GB", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="evomi_set_alert", description="Set a balance alert for an Evomi product")
@app_commands.describe(product="Product", threshold_mb="Alert threshold in MB", channel="Channel for alerts")
@app_commands.choices(product=EVOMI_PRODUCT_CHOICES)
async def evomi_set_alert(
    interaction: discord.Interaction, product: app_commands.Choice[str], threshold_mb: int, channel: discord.TextChannel
):
    alerts = load_json("evomi_alerts.json")
    alerts[product.value] = {"threshold_mb": threshold_mb, "channel_id": channel.id, "breached": False}
    save_json("evomi_alerts.json", alerts)
    e = discord.Embed(
        title="✅ Alert Set",
        description=f"**{EVOMI_PRODUCTS[product.value]}**: Alert when balance ≤ {threshold_mb} MB in {channel.mention}",
        color=CLR_SUCCESS,
    )
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="evomi_remove_alert", description="Remove a balance alert for an Evomi product")
@app_commands.describe(product="Product")
@app_commands.choices(product=EVOMI_PRODUCT_CHOICES)
async def evomi_remove_alert(interaction: discord.Interaction, product: app_commands.Choice[str]):
    alerts = load_json("evomi_alerts.json")
    alerts.pop(product.value, None)
    save_json("evomi_alerts.json", alerts)
    e = discord.Embed(title="✅ Alert Removed", description=f"Removed alert for **{EVOMI_PRODUCTS[product.value]}**", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="evomi_alerts_list", description="List all Evomi balance alerts")
async def evomi_alerts_list(interaction: discord.Interaction):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    await interaction.response.defer()
    alerts = load_json("evomi_alerts.json")
    data = await evomi_get()
    e = discord.Embed(title="🔔 Evomi Alerts", color=CLR_EVOMI)
    if not alerts:
        e.description = "No alerts configured. Use `/evomi_set_alert` to add one."
    else:
        for prod_code, alert in alerts.items():
            name = EVOMI_PRODUCTS.get(prod_code, prod_code)
            ch = bot.get_channel(alert["channel_id"])
            ch_name = ch.mention if ch else f"#{alert['channel_id']}"
            balance = "N/A"
            if data:
                for p in evomi_parse_products(data):
                    if p["code"] == prod_code:
                        balance = f"{p['balance_mb']:.2f} MB"
            status = "⚠️ BREACHED" if alert.get("breached") else "✅ OK"
            e.add_field(
                name=f"{name}",
                value=f"Threshold: {alert['threshold_mb']} MB | Channel: {ch_name}\nBalance: {balance} | {status}",
                inline=False,
            )
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="evomi_usage_history", description="Show Evomi usage history (last 7 days)")
async def evomi_usage_history(interaction: discord.Interaction):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    history = load_json("evomi_history.json")
    e = discord.Embed(title="📈 Evomi Usage History (7 days)", color=CLR_EVOMI)
    if not history:
        e.description = "No history yet. Data is collected every 30 minutes."
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        for prod_code in EVOMI_PRODUCTS:
            entries = [(k, v.get(prod_code, 0)) for k, v in sorted(history.items()) if k >= cutoff and prod_code in v]
            if entries:
                lines = ["Date | Balance MB | Daily Change"]
                for i, (date, bal) in enumerate(entries[-7:]):
                    change = f"{bal - entries[max(0, i-1)][1]:+.2f}" if i > 0 else "—"
                    lines.append(f"{date} | {bal:.2f} | {change}")
                e.add_field(name=EVOMI_PRODUCTS[prod_code], value="```\n" + "\n".join(lines) + "\n```", inline=False)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="evomi_top_up", description="Get Evomi top-up links and current balance")
async def evomi_top_up(interaction: discord.Interaction):
    if not EVOMI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Evomi"))
    await interaction.response.defer()
    data = await evomi_get()
    e = discord.Embed(title="💳 Evomi — Top Up", color=CLR_ERROR)
    if data:
        for p in evomi_parse_products(data):
            bal = p["balance_mb"]
            e.add_field(name=p["name"], value=f"{bal:.2f} MB ({bal/1024:.2f} GB)", inline=True)
    else:
        e.description = "Could not fetch balance."
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="💳 Top Up", url="https://my.evomi.com/billing"))
    view.add_item(discord.ui.Button(label="📊 Dashboard", url="https://my.evomi.com/dashboard"))
    e.set_footer(text="Tip: Use /evomi_set_alert to get notified before you run out")
    await interaction.followup.send(embed=e, view=view)


# ===================================================================
# PROXIDIZE COMMANDS
# ===================================================================
async def proxidize_get(path: str) -> dict | None:
    try:
        async with http_session.get(
            f"https://api.proxidize.com/api/v1{path}",
            headers={"Authorization": f"Bearer {PROXIDIZE_API_TOKEN}"},
        ) as resp:
            if resp.status in (401, 403, 404):
                return None
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None


async def proxidize_post(path: str, payload: dict = None) -> dict | None:
    try:
        async with http_session.post(
            f"https://api.proxidize.com/api/v1{path}",
            headers={"Authorization": f"Bearer {PROXIDIZE_API_TOKEN}"},
            json=payload or {},
        ) as resp:
            if resp.status in (401, 403, 404):
                return None
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None


def proxidize_error_embed() -> discord.Embed:
    e = discord.Embed(
        title="⚠️ Proxidize API Error",
        description="Proxidize API returned unexpected response.\nVerify token or check app.proxidize.com",
        color=CLR_ERROR,
    )
    return footer(e)


CARRIER_CHOICES = [
    app_commands.Choice(name="Fastest", value="fastest"),
    app_commands.Choice(name="Purple (T-Mobile)", value="purple"),
    app_commands.Choice(name="Blue (AT&T)", value="blue"),
    app_commands.Choice(name="Red (Verizon)", value="red"),
]

PROXIDIZE_AMOUNT_CHOICES = [
    app_commands.Choice(name="10", value=10),
    app_commands.Choice(name="20", value=20),
    app_commands.Choice(name="50", value=50),
    app_commands.Choice(name="100", value=100),
]

PROXIDIZE_SPREAD_CHOICES = [
    app_commands.Choice(name="50", value=50),
    app_commands.Choice(name="100", value=100),
]

PROXIDIZE_FORMAT_CHOICES = [
    app_commands.Choice(name="user:pass@host:port", value=1),
    app_commands.Choice(name="host:port:user:pass", value=2),
]


PROXIDIZE_HOST = "pg.proxi.es"
PROXIDIZE_PORT = 20000

PROXIDIZE_CITIES = [
    {"city": "Newark", "state": "NJ"},
    {"city": "Alpharetta", "state": "Georgia"},
    {"city": "NewYorkCity", "state": "NY"},
    {"city": "WashingtonDC", "state": "DC"},
    {"city": "Philadelphia", "state": "PA"},
    {"city": "Phoenix", "state": "AZ"},
    {"city": "Chicago", "state": "IL"},
    {"city": "Dallas", "state": "TX"},
    {"city": "Houston", "state": "TX"},
    {"city": "Greensboro", "state": "NC"},
    {"city": "Tampa", "state": "FL"},
    {"city": "GrandRapids", "state": "MI"},
    {"city": "Portland", "state": "OR"},
    {"city": "Seattle", "state": "WA"},
    {"city": "Omaha", "state": "NE"},
    {"city": "Baltimore", "state": "MD"},
    {"city": "KansasCity", "state": "MO"},
    {"city": "LosAngeles", "state": "CA"},
    {"city": "SanDiego", "state": "CA"},
    {"city": "SanJose", "state": "CA"},
    {"city": "SantaClara", "state": "CA"},
    {"city": "LosGatos", "state": "CA"},
    {"city": "Louisville", "state": "KY"},
    {"city": "NewYorkCity", "state": "NewYork"},
]


def rand10() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=10))


def build_proxidize_proxy(base_user: str, password: str, state: str = None, city: str = None,
                           fmt: int = 2, protocol: str = "http") -> str:
    """Build a Proxidize proxy string with unique session ID and optional city targeting."""
    sessid = rand10()
    # Username from API already contains _BareMetal, just append -s-SESSID
    user_part = f"{base_user}-s-{sessid}-co-USA"
    if state:
        user_part += f"-st-{state}"
    if city:
        user_part += f"-ci-{city}"
    return format_proxy(user_part, password, PROXIDIZE_HOST, PROXIDIZE_PORT, fmt, protocol)


def proxidize_bytes_to_gb(b) -> float:
    return float(b) / (1024 ** 3) if b else 0.0


def proxidize_parse_balance(data: dict) -> tuple[float, float, float]:
    """Returns (gb_used, gb_remaining, gb_total). bytes_available = total allocation."""
    bytes_used = float(data.get("bytes_used", data.get("byte_sum", 0)))
    bytes_total = float(data.get("bytes_available", 0))  # this is TOTAL, not remaining
    gb_used = proxidize_bytes_to_gb(bytes_used)
    gb_total = proxidize_bytes_to_gb(bytes_total)
    gb_remaining = max(0, gb_total - gb_used)
    return gb_used, gb_remaining, gb_total


@bot.tree.command(name="proxidize_status", description="Show Proxidize account status")
async def proxidize_status(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    data = await proxidize_get("/pergb/mobile/user-info")
    if not data:
        return await interaction.followup.send(embed=proxidize_error_embed())

    gb_used, gb_remaining, gb_total = proxidize_parse_balance(data)

    if gb_remaining > 5:
        icon = "🟢"
    elif gb_remaining >= 1:
        icon = "🟡"
    else:
        icon = "🔴"

    e = discord.Embed(title="📡 Proxidize Status", color=CLR_PROXIDIZE)
    e.add_field(name="GB Used", value=f"{gb_used:.2f} GB", inline=True)
    e.add_field(name="GB Total", value=f"{gb_total:.2f} GB", inline=True)
    e.add_field(name=f"{icon} GB Remaining", value=f"{gb_remaining:.2f} GB", inline=True)
    e.add_field(name="Progress", value=progress_bar(gb_used, gb_total), inline=False)
    e.add_field(name="Username", value=data.get("username", "N/A"), inline=True)
    e.add_field(name="Enabled", value="✅" if data.get("enabled") else "❌", inline=True)

    # Also fetch access points
    aps = await proxidize_get("/pergb/mobile/access-point")
    if aps and isinstance(aps, list):
        e.add_field(name="Access Points", value=str(len(aps)), inline=True)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="proxidize_generate", description="Generate Proxidize mobile proxies with unique sessions")
@app_commands.describe(
    amount="Number of proxies (no limit)",
    city="City name (leave empty for random)",
    state="State code (leave empty for random)",
    format="Output format",
)
@app_commands.choices(format=PROXIDIZE_FORMAT_CHOICES)
async def proxidize_generate(
    interaction: discord.Interaction,
    amount: int = 10,
    city: str = None,
    state: str = None,
    format: app_commands.Choice[int] = None,
):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    cd = check_cooldown("proxidize_generate", interaction.user.id, 5)
    if cd > 0:
        e = discord.Embed(title="⏳ Cooldown", description=f"Please wait {cd:.1f}s", color=CLR_ERROR)
        return await interaction.response.send_message(embed=footer(e), ephemeral=True)

    await interaction.response.defer()
    fmt_val = format.value if format else 2

    # Get access point credentials
    aps = await proxidize_get("/pergb/mobile/access-point")
    if not aps or not isinstance(aps, list) or len(aps) == 0:
        e = discord.Embed(title="⚠️ No Access Points", description="Create access points at https://app.proxidize.com/proxies/mobile/per-gb/", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    # Use first access point's credentials
    ap = next((a for a in aps if a.get("username") and a.get("password")), None)
    if not ap:
        e = discord.Embed(title="⚠️ No Credentials", description="No access points with credentials found.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    base_user = ap["username"]
    pwd = ap["password"]
    proxies = []
    for _ in range(amount):
        proxies.append(build_proxidize_proxy(base_user, pwd, state, city, fmt_val))

    proxy_text = "\n".join(proxies)
    e = discord.Embed(title="✅ Proxidize Proxies Generated", color=CLR_PROXIDIZE)
    e.add_field(name="Amount", value=f"{len(proxies):,}", inline=True)
    e.add_field(name="Location", value=f"{city or 'Random'}, {state or 'Random'}", inline=True)
    session_summary_fields(e, 120, "Proxidize")

    if len(proxy_text) <= 1900:
        e.description = f"```\n{proxy_text}\n```"
        await interaction.followup.send(embed=footer(e))
    else:
        buf = io.BytesIO(proxy_text.encode())
        file = discord.File(buf, filename="proxies_proxidize.txt")
        await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="proxidize_generate_spread", description="Generate Proxidize proxies spread across 24 US cities")
@app_commands.describe(
    total_amount="Total proxies (no limit)",
    format="Output format",
)
@app_commands.choices(format=PROXIDIZE_FORMAT_CHOICES)
async def proxidize_generate_spread(
    interaction: discord.Interaction,
    total_amount: int = 1000,
    format: app_commands.Choice[int] = None,
):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    fmt_val = format.value if format else 2

    aps = await proxidize_get("/pergb/mobile/access-point")
    if not aps or not isinstance(aps, list) or len(aps) == 0:
        e = discord.Embed(title="⚠️ No Access Points", description="Create access points at https://app.proxidize.com/proxies/mobile/per-gb/", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    ap = next((a for a in aps if a.get("username") and a.get("password")), None)
    if not ap:
        e = discord.Embed(title="⚠️ No Credentials", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    import itertools
    base_user = ap["username"]
    pwd = ap["password"]
    city_cycle = itertools.cycle(PROXIDIZE_CITIES)
    proxies = []
    for _ in range(total_amount):
        c = next(city_cycle)
        proxies.append(build_proxidize_proxy(base_user, pwd, c["state"], c["city"], fmt_val))

    proxy_text = "\n".join(proxies)
    buf = io.BytesIO(proxy_text.encode())
    file = discord.File(buf, filename="proxies_proxidize_spread.txt")

    e = discord.Embed(title="✅ Proxidize Spread Proxies", color=CLR_PROXIDIZE)
    e.add_field(name="Total", value=f"{len(proxies):,}", inline=True)
    e.add_field(name="Cities", value=f"{len(PROXIDIZE_CITIES)} US cities", inline=True)
    e.add_field(name="", value="💡 Each proxy has unique session ID + city for maximum diversity", inline=False)
    session_summary_fields(e, 120, "Proxidize")
    await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="proxidize_locations", description="List available Proxidize proxy locations")
async def proxidize_locations(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    data = await proxidize_get("/pergb/mobile/locations-proxy")
    if not data:
        return await interaction.followup.send(embed=proxidize_error_embed())
    e = discord.Embed(title="📍 Proxidize Available Locations", color=CLR_PROXIDIZE)
    locations = data.get("locations", data) if isinstance(data, dict) else data
    all_cities = []
    if isinstance(locations, list):
        for loc in locations:
            if isinstance(loc, dict):
                country = loc.get("country", {})
                c_name = country.get("name", "?") if isinstance(country, dict) else str(country)
                cities = country.get("locations", []) if isinstance(country, dict) else []
                for c in cities:
                    label = c.get("label", "?")
                    state = c.get("state", "")
                    city = c.get("city", "")
                    all_cities.append(f"{label} | city={city} | state={state} | country={c_name}")
    if all_cities:
        e.description = f"**{len(all_cities)} locations found** — see attached file"
        txt = "\n".join(all_cities)
        buf = io.BytesIO(txt.encode())
        file = discord.File(buf, filename="proxidize_locations.txt")
        await interaction.followup.send(embed=footer(e), file=file)
    else:
        e.description = f"```json\n{json.dumps(data, indent=2)[:1800]}\n```"
        await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="proxidize_carriers", description="List available Proxidize carriers")
async def proxidize_carriers(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    data = await proxidize_get("/pergb/mobile/carriers-proxy")
    if not data:
        return await interaction.followup.send(embed=proxidize_error_embed())
    e = discord.Embed(title="📡 Proxidize Available Carriers", color=CLR_PROXIDIZE)
    if isinstance(data, list):
        for carrier in data[:25]:
            if isinstance(carrier, dict):
                name = carrier.get("name", "?")
                avail = "✅" if carrier.get("available") else "❌"
                e.add_field(name=name, value=f"ID: {carrier.get('value', '?')} | {avail}", inline=True)
    else:
        e.description = f"```json\n{json.dumps(data, indent=2)[:1800]}\n```"
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="proxidize_rotate", description="Rotate Proxidize proxy session (refresh access point IPs)")
async def proxidize_rotate(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    # Proxidize doesn't have a dedicated rotate endpoint — IP rotation happens
    # automatically in "Random IP" mode or by changing settings
    e = discord.Embed(
        title="ℹ️ Proxidize IP Rotation",
        description=(
            "Proxidize handles rotation automatically:\n"
            "• **Random IP mode** — new IP every connection\n"
            "• **Sticky IP mode** — update session key in dashboard to get new IP\n\n"
            "Configure at https://app.proxidize.com/proxies/mobile/per-gb/"
        ),
        color=CLR_PROXIDIZE,
    )
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="proxidize_balance", description="Show Proxidize balance")
async def proxidize_balance(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    data = await proxidize_get("/pergb/mobile/user-info")
    if not data:
        return await interaction.followup.send(embed=proxidize_error_embed())

    gb_used, gb_remaining, gb_total = proxidize_parse_balance(data)
    caps = load_json("proxidize_caps.json")
    cap = caps.get("cap_gb")

    e = discord.Embed(title="💰 Proxidize Balance", color=CLR_PROXIDIZE)
    e.add_field(name="GB Used", value=f"{gb_used:.2f}", inline=True)
    e.add_field(name="GB Total", value=f"{gb_total:.2f}", inline=True)
    e.add_field(name="GB Remaining", value=f"{gb_remaining:.2f}", inline=True)
    e.add_field(name="Progress", value=progress_bar(gb_used, gb_total), inline=False)
    if cap:
        pct = (gb_used / cap * 100) if cap > 0 else 0
        cost = gb_used * 1.0  # $1/GB
        e.add_field(name="Cap", value=f"{gb_used:.2f} / {cap:.2f} GB ({pct:.1f}%) | Est. cost: ${cost:.2f}", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="proxidize_set_cap", description="Set Proxidize usage cap")
@app_commands.describe(cap_gb="Cap in GB")
async def proxidize_set_cap(interaction: discord.Interaction, cap_gb: float):
    caps = load_json("proxidize_caps.json")
    caps["cap_gb"] = cap_gb
    save_json("proxidize_caps.json", caps)
    e = discord.Embed(title="✅ Cap Set", description=f"Proxidize cap: {cap_gb:.2f} GB", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="proxidize_set_alert", description="Set Proxidize balance alert")
@app_commands.describe(threshold_gb="Alert when GB remaining drops below", channel="Channel for alerts")
async def proxidize_set_alert(interaction: discord.Interaction, threshold_gb: float, channel: discord.TextChannel):
    alerts = {"threshold_gb": threshold_gb, "channel_id": channel.id, "breached": False}
    save_json("proxidize_alerts.json", alerts)
    e = discord.Embed(
        title="✅ Alert Set",
        description=f"Alert when remaining ≤ {threshold_gb:.2f} GB in {channel.mention}",
        color=CLR_SUCCESS,
    )
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="proxidize_remove_alert", description="Remove Proxidize balance alert")
async def proxidize_remove_alert(interaction: discord.Interaction):
    save_json("proxidize_alerts.json", {})
    e = discord.Embed(title="✅ Alert Removed", description="Proxidize alert removed.", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="proxidize_alerts_list", description="List Proxidize alerts")
async def proxidize_alerts_list(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    alerts = load_json("proxidize_alerts.json")
    e = discord.Embed(title="🔔 Proxidize Alerts", color=CLR_PROXIDIZE)
    if not alerts or "threshold_gb" not in alerts:
        e.description = "No alerts configured."
    else:
        data = await proxidize_get("/pergb/mobile/user-info")
        remaining = "N/A"
        if data:
            _, gb_rem, _ = proxidize_parse_balance(data)
            remaining = f"{gb_rem:.2f} GB"
        ch = bot.get_channel(alerts["channel_id"])
        ch_name = ch.mention if ch else f"#{alerts['channel_id']}"
        status = "⚠️ BREACHED" if alerts.get("breached") else "✅ OK"
        e.add_field(
            name="Alert",
            value=f"Threshold: {alerts['threshold_gb']:.2f} GB | Channel: {ch_name}\nRemaining: {remaining} | {status}",
            inline=False,
        )
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="proxidize_usage_history", description="Show Proxidize usage history (last 7 days)")
async def proxidize_usage_history(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    history = load_json("proxidize_history.json")
    e = discord.Embed(title="📈 Proxidize Usage History (7 days)", color=CLR_PROXIDIZE)
    if not history:
        e.description = "No history yet."
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        entries = [(k, v) for k, v in sorted(history.items()) if k >= cutoff]
        lines = ["Date | GB Remaining | Daily Change"]
        for i, (date, val) in enumerate(entries[-7:]):
            rem = val.get("remaining", 0)
            change = f"{rem - entries[max(0, i-1)][1].get('remaining', 0):+.2f}" if i > 0 else "—"
            lines.append(f"{date} | {rem:.2f} | {change}")
        e.description = "```\n" + "\n".join(lines) + "\n```"
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="proxidize_top_up", description="Get Proxidize top-up links")
async def proxidize_top_up(interaction: discord.Interaction):
    if not PROXIDIZE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Proxidize"))
    await interaction.response.defer()
    data = await proxidize_get("/pergb/mobile/user-info")
    e = discord.Embed(title="💳 Proxidize — Top Up", color=CLR_ERROR)
    if data:
        _, gb_rem, _ = proxidize_parse_balance(data)
        e.add_field(name="Remaining", value=f"{gb_rem:.2f} GB", inline=True)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="💳 Top Up", url="https://app.proxidize.com/billing"))
    view.add_item(discord.ui.Button(label="📊 Dashboard", url="https://app.proxidize.com/proxies/mobile/per-gb/"))
    await interaction.followup.send(embed=footer(e), view=view)


# ===================================================================
# OXYLABS COMMANDS — SHARED HELPERS
# ===================================================================
OXYLABS_SPREAD_STATES = ["CA", "TX", "FL", "NY", "IL", "OH", "PA", "GA", "NC", "MI", "AZ", "WA", "CO", "TN", "IN"]


US_STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new_hampshire",
    "NJ": "new_jersey", "NM": "new_mexico", "NY": "new_york", "NC": "north_carolina",
    "ND": "north_dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode_island", "SC": "south_carolina", "SD": "south_dakota", "TN": "tennessee",
    "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west_virginia", "WI": "wisconsin", "WY": "wyoming",
}


def build_oxylabs_proxy(username: str, password: str, country: str = "US", state: str = None,
                         city: str = None, session_type: str = "sticky", sessid: str = None,
                         protocol: str = "http", fmt: int = 2, product: str = "mobile") -> str:
    user_part = f"customer-{username}-cc-{country}"
    if state:
        # Mobile uses us_statename format, residential uses abbreviation
        if product == "mobile":
            state_name = US_STATE_NAMES.get(state.upper(), state.lower())
            user_part += f"-st-us_{state_name}"
        else:
            user_part += f"-st-{state}"
    if city:
        user_part += f"-city-{city.lower().replace(' ', '_')}"
    if session_type == "sticky" and sessid:
        user_part += f"-sessid-{sessid}-sesstime-120"
    return format_proxy(user_part, password, "pr.oxylabs.io", 7777, fmt, protocol)


async def oxylabs_get_stats(jwt_store: dict) -> dict | None:
    if not jwt_store.get("token") or not jwt_store.get("user_id"):
        return None
    try:
        async with http_session.get(
            f"https://residential-api.oxylabs.io/v2/users/{jwt_store['user_id']}/client-stats",
            headers={"Authorization": f"Bearer {jwt_store['token']}"},
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None


# ===================================================================
# OXYLABS MOBILE COMMANDS
# ===================================================================
@bot.tree.command(name="oxylabs_mobile_status", description="Show Oxylabs Mobile status")
async def oxylabs_mobile_status(interaction: discord.Interaction):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    await interaction.response.defer()
    await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
    data = await oxylabs_get_stats(oxylabs_mobile_jwt)
    if not data:
        e = discord.Embed(title="🔴 Oxylabs Mobile Error", description="Could not fetch stats.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    gb_used = float(data.get("traffic", data.get("gb_used", 0)))
    date_from = data.get("date_from", "N/A")
    date_to = data.get("date_to", "N/A")
    caps = load_json("oxylabs_mobile_caps.json")
    cap = caps.get("cap_gb")

    e = discord.Embed(title="📱 Oxylabs Mobile Status", color=CLR_OXYLABS)
    e.add_field(name="🟢 Connected", value="pr.oxylabs.io:7777", inline=False)
    e.add_field(name="GB Used", value=f"{gb_used:.2f} GB", inline=True)
    e.add_field(name="Billing Period", value=f"{date_from} → {date_to}", inline=True)
    e.add_field(name="Protocols", value="HTTP / SOCKS5 (port 7777)", inline=True)
    if cap:
        remaining = max(0, cap - gb_used)
        e.add_field(name="Cap", value=f"{progress_bar(gb_used, cap)} {gb_used:.2f} / {cap:.2f} GB", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="oxylabs_mobile_generate", description="Generate Oxylabs Mobile proxies")
@app_commands.describe(
    amount="Number of proxies (no limit)",
    country="Country code",
    state="US state code e.g. CA",
    city="City name e.g. Miami",
    session="Session type",
    protocol="Protocol",
    format="Output format",
    lifetime="Session lifetime in minutes (1-300)",
)
@app_commands.choices(
    session=[app_commands.Choice(name="rotating", value="rotating"), app_commands.Choice(name="sticky", value="sticky")],
    protocol=[app_commands.Choice(name="http", value="http"), app_commands.Choice(name="socks5", value="socks5")],
    format=FORMAT_CHOICES,
)
async def oxylabs_mobile_generate(
    interaction: discord.Interaction,
    amount: int = 10,
    country: str = "US",
    state: str = None,
    city: str = None,
    session: app_commands.Choice[str] = None,
    protocol: app_commands.Choice[str] = None,
    format: app_commands.Choice[int] = None,
    lifetime: int = 120,
):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    if lifetime > MAX_LIFETIME:
        return await interaction.response.send_message(embed=lifetime_blocked_embed())
    cd = check_cooldown("oxylabs_mobile_generate", interaction.user.id, 5)
    if cd > 0:
        e = discord.Embed(title="⏳ Cooldown", description=f"Please wait {cd:.1f}s", color=CLR_ERROR)
        return await interaction.response.send_message(embed=footer(e), ephemeral=True)

    await interaction.response.defer()
    amount = max(1, amount)
    sess_val = session.value if session else "sticky"
    proto_val = protocol.value if protocol else "http"
    fmt_val = format.value if format else 2

    proxies = []
    for _ in range(amount):
        sessid = rand8() if sess_val == "sticky" else None
        proxies.append(build_oxylabs_proxy(
            OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD,
            country, state, city, sess_val, sessid, proto_val, fmt_val, "mobile"
        ))

    proxy_text = "\n".join(proxies)
    e = discord.Embed(title="✅ Oxylabs Mobile Proxies Generated", color=CLR_OXYLABS)
    e.add_field(name="Amount", value=str(len(proxies)), inline=True)
    e.add_field(name="Country", value=country, inline=True)
    if state:
        e.add_field(name="State", value=state, inline=True)
    if city:
        e.add_field(name="City", value=city, inline=True)
    e.add_field(name="Session", value=sess_val, inline=True)
    session_summary_fields(e, lifetime, "Oxylabs Mobile")

    if len(proxy_text) <= 1900:
        e.description = f"```\n{proxy_text}\n```"
        await interaction.followup.send(embed=footer(e))
    else:
        buf = io.BytesIO(proxy_text.encode())
        file = discord.File(buf, filename="proxies_oxylabs_mobile.txt")
        await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="oxylabs_mobile_generate_spread", description="Generate Oxylabs Mobile proxies spread across US states")
@app_commands.describe(
    total_amount="Total proxies (no limit)",
    session="Session type",
    protocol="Protocol",
    format="Output format",
    lifetime="Session lifetime in minutes (1-300)",
)
@app_commands.choices(
    session=[app_commands.Choice(name="rotating", value="rotating"), app_commands.Choice(name="sticky", value="sticky")],
    protocol=[app_commands.Choice(name="http", value="http"), app_commands.Choice(name="socks5", value="socks5")],
    format=FORMAT_CHOICES,
)
async def oxylabs_mobile_generate_spread(
    interaction: discord.Interaction,
    total_amount: int = 100,
    session: app_commands.Choice[str] = None,
    protocol: app_commands.Choice[str] = None,
    format: app_commands.Choice[int] = None,
    lifetime: int = 120,
):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    if lifetime > MAX_LIFETIME:
        return await interaction.response.send_message(embed=lifetime_blocked_embed())

    await interaction.response.defer()
    total_amount = max(10, total_amount)
    sess_val = session.value if session else "sticky"
    proto_val = protocol.value if protocol else "http"
    fmt_val = format.value if format else 2
    per_state = -(-total_amount // len(OXYLABS_SPREAD_STATES))

    proxies = []
    import itertools
    state_cycle = itertools.cycle(OXYLABS_SPREAD_STATES)
    for _ in range(total_amount):
        st = next(state_cycle)
        sessid = rand8() if sess_val == "sticky" else None
        proxies.append(build_oxylabs_proxy(
            OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD,
            "US", st, None, sess_val, sessid, proto_val, fmt_val, "mobile"
        ))

    proxy_text = "\n".join(proxies)
    buf = io.BytesIO(proxy_text.encode())
    file = discord.File(buf, filename="proxies_oxylabs_mobile_spread.txt")

    e = discord.Embed(title="✅ Oxylabs Mobile Spread Proxies", color=CLR_OXYLABS)
    e.add_field(name="Total", value=str(len(proxies)), inline=True)
    e.add_field(name="States", value=", ".join(OXYLABS_SPREAD_STATES), inline=False)
    e.add_field(name="Unique Sessions", value=str(len(proxies)), inline=True)
    e.add_field(name="", value="💡 Each proxy has unique state + sessid for maximum pool diversity", inline=False)
    session_summary_fields(e, lifetime, "Oxylabs Mobile")
    await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="oxylabs_mobile_balance", description="Show Oxylabs Mobile balance")
async def oxylabs_mobile_balance(interaction: discord.Interaction):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    await interaction.response.defer()
    await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
    data = await oxylabs_get_stats(oxylabs_mobile_jwt)
    if not data:
        e = discord.Embed(title="❌ Error", description="Could not fetch stats.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    gb_used = float(data.get("traffic", data.get("gb_used", 0)))
    date_from = data.get("date_from", "N/A")
    date_to = data.get("date_to", "N/A")
    caps = load_json("oxylabs_mobile_caps.json")
    cap = caps.get("cap_gb")

    e = discord.Embed(title="💰 Oxylabs Mobile Balance", color=CLR_OXYLABS)
    e.add_field(name="GB Used", value=f"{gb_used:.2f}", inline=True)
    e.add_field(name="Billing Period", value=f"{date_from} → {date_to}", inline=True)
    if cap:
        remaining = max(0, cap - gb_used)
        pct = gb_used / cap * 100 if cap > 0 else 0
        e.add_field(name="Cap", value=f"{progress_bar(gb_used, cap)} {gb_used:.2f} / {cap:.2f} GB ({pct:.1f}%)", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="oxylabs_mobile_set_cap", description="Set Oxylabs Mobile usage cap")
@app_commands.describe(cap_gb="Cap in GB")
async def oxylabs_mobile_set_cap(interaction: discord.Interaction, cap_gb: float):
    caps = load_json("oxylabs_mobile_caps.json")
    caps["cap_gb"] = cap_gb
    save_json("oxylabs_mobile_caps.json", caps)
    e = discord.Embed(title="✅ Cap Set", description=f"Oxylabs Mobile cap: {cap_gb:.2f} GB", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_mobile_set_alert", description="Set Oxylabs Mobile usage alert")
@app_commands.describe(threshold_gb="Alert when GB used reaches this", channel="Channel for alerts")
async def oxylabs_mobile_set_alert(interaction: discord.Interaction, threshold_gb: float, channel: discord.TextChannel):
    alerts = {"threshold_gb": threshold_gb, "channel_id": channel.id, "breached": False}
    save_json("oxylabs_mobile_alerts.json", alerts)
    e = discord.Embed(title="✅ Alert Set", description=f"Alert when usage ≥ {threshold_gb:.2f} GB in {channel.mention}", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_mobile_remove_alert", description="Remove Oxylabs Mobile alert")
async def oxylabs_mobile_remove_alert(interaction: discord.Interaction):
    save_json("oxylabs_mobile_alerts.json", {})
    e = discord.Embed(title="✅ Alert Removed", description="Oxylabs Mobile alert removed.", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_mobile_alerts_list", description="List Oxylabs Mobile alerts")
async def oxylabs_mobile_alerts_list(interaction: discord.Interaction):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    await interaction.response.defer()
    alerts = load_json("oxylabs_mobile_alerts.json")
    e = discord.Embed(title="🔔 Oxylabs Mobile Alerts", color=CLR_OXYLABS)
    if not alerts or "threshold_gb" not in alerts:
        e.description = "No alerts configured."
    else:
        await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_mobile_jwt)
        gb_used = f"{float(data.get('traffic', 0)):.2f} GB" if data else "N/A"
        ch = bot.get_channel(alerts["channel_id"])
        ch_name = ch.mention if ch else f"#{alerts['channel_id']}"
        status = "⚠️ BREACHED" if alerts.get("breached") else "✅ OK"
        e.add_field(name="Alert", value=f"Threshold: {alerts['threshold_gb']:.2f} GB used | Channel: {ch_name}\nCurrent: {gb_used} | {status}", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="oxylabs_mobile_usage_history", description="Show Oxylabs Mobile usage history (last 7 days)")
async def oxylabs_mobile_usage_history(interaction: discord.Interaction):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    history = load_json("oxylabs_mobile_history.json")
    e = discord.Embed(title="📈 Oxylabs Mobile Usage History (7 days)", color=CLR_OXYLABS)
    if not history:
        e.description = "No history yet."
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        entries = [(k, v) for k, v in sorted(history.items()) if k >= cutoff]
        lines = ["Date | GB Used | Daily Change"]
        for i, (date, val) in enumerate(entries[-7:]):
            used = val.get("gb_used", 0)
            change = f"{used - entries[max(0, i-1)][1].get('gb_used', 0):+.2f}" if i > 0 else "—"
            lines.append(f"{date} | {used:.2f} | {change}")
        e.description = "```\n" + "\n".join(lines) + "\n```"
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_mobile_top_up", description="Get Oxylabs Mobile top-up links")
async def oxylabs_mobile_top_up(interaction: discord.Interaction):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    await interaction.response.defer()
    await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
    data = await oxylabs_get_stats(oxylabs_mobile_jwt)
    e = discord.Embed(title="💳 Oxylabs Mobile — Top Up", color=CLR_ERROR)
    if data:
        e.add_field(name="GB Used", value=f"{float(data.get('traffic', 0)):.2f} GB", inline=True)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="💳 Top Up", url="https://dashboard.oxylabs.io"))
    view.add_item(discord.ui.Button(label="📊 Dashboard", url="https://dashboard.oxylabs.io/?route=/overview/MP"))
    await interaction.followup.send(embed=footer(e), view=view)


@bot.tree.command(name="oxylabs_mobile_test", description="Test Oxylabs Mobile proxy connection")
async def oxylabs_mobile_test(interaction: discord.Interaction):
    if not OXYLABS_MOBILE_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Mobile"))
    await interaction.response.defer()
    proxy_url = f"http://customer-{OXYLABS_MOBILE_USERNAME}:{OXYLABS_MOBILE_PASSWORD}@pr.oxylabs.io:7777"
    try:
        async with aiohttp.ClientSession() as s:
            # Try ipinfo.io for reliable geo data
            async with s.get("https://ipinfo.io/json", proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                ip_addr = data.get("ip", "Unknown")
                city = data.get("city", "")
                region = data.get("region", "")
                country = data.get("country", "")
                org = data.get("org", "")
                location = ", ".join(filter(None, [city, region, country]))
                e = discord.Embed(title="✅ Oxylabs Mobile Test Passed", color=CLR_OXYLABS)
                e.add_field(name="Exit IP", value=ip_addr, inline=True)
                e.add_field(name="Location", value=location or "Unknown", inline=True)
                e.add_field(name="ISP", value=org or "Unknown", inline=True)
    except Exception as ex:
        e = discord.Embed(title="❌ Oxylabs Mobile Test Failed", description=str(ex)[:500], color=CLR_ERROR)
    await interaction.followup.send(embed=footer(e))


# ===================================================================
# OXYLABS RESIDENTIAL COMMANDS
# ===================================================================
@bot.tree.command(name="oxylabs_resi_status", description="Show Oxylabs Residential status")
async def oxylabs_resi_status(interaction: discord.Interaction):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    await interaction.response.defer()
    await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
    data = await oxylabs_get_stats(oxylabs_resi_jwt)
    if not data:
        e = discord.Embed(title="🔴 Oxylabs Residential Error", description="Could not fetch stats.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    gb_used = float(data.get("traffic", data.get("gb_used", 0)))
    date_from = data.get("date_from", "N/A")
    date_to = data.get("date_to", "N/A")
    caps = load_json("oxylabs_resi_caps.json")
    cap = caps.get("cap_gb")

    e = discord.Embed(title="🏠 Oxylabs Residential Status", color=CLR_OXYLABS)
    e.add_field(name="🟢 Connected", value="pr.oxylabs.io:7777", inline=False)
    e.add_field(name="GB Used", value=f"{gb_used:.2f} GB", inline=True)
    e.add_field(name="Billing Period", value=f"{date_from} → {date_to}", inline=True)
    e.add_field(name="Protocols", value="HTTP / SOCKS5 (port 7777)", inline=True)
    if cap:
        e.add_field(name="Cap", value=f"{progress_bar(gb_used, cap)} {gb_used:.2f} / {cap:.2f} GB", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="oxylabs_resi_generate", description="Generate Oxylabs Residential proxies")
@app_commands.describe(
    amount="Number of proxies (no limit)", country="Country code", state="US state code",
    city="City name", session="Session type", protocol="Protocol", format="Output format",
    lifetime="Session lifetime in minutes (1-300)",
)
@app_commands.choices(
    session=[app_commands.Choice(name="rotating", value="rotating"), app_commands.Choice(name="sticky", value="sticky")],
    protocol=[app_commands.Choice(name="http", value="http"), app_commands.Choice(name="socks5", value="socks5")],
    format=FORMAT_CHOICES,
)
async def oxylabs_resi_generate(
    interaction: discord.Interaction, amount: int = 10, country: str = "US",
    state: str = None, city: str = None,
    session: app_commands.Choice[str] = None, protocol: app_commands.Choice[str] = None,
    format: app_commands.Choice[int] = None, lifetime: int = 120,
):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    if lifetime > MAX_LIFETIME:
        return await interaction.response.send_message(embed=lifetime_blocked_embed())
    cd = check_cooldown("oxylabs_resi_generate", interaction.user.id, 5)
    if cd > 0:
        e = discord.Embed(title="⏳ Cooldown", description=f"Please wait {cd:.1f}s", color=CLR_ERROR)
        return await interaction.response.send_message(embed=footer(e), ephemeral=True)

    await interaction.response.defer()
    amount = max(1, amount)
    sess_val = session.value if session else "sticky"
    proto_val = protocol.value if protocol else "http"
    fmt_val = format.value if format else 2

    proxies = []
    for _ in range(amount):
        sessid = rand8() if sess_val == "sticky" else None
        proxies.append(build_oxylabs_proxy(
            OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD,
            country, state, city, sess_val, sessid, proto_val, fmt_val, "residential"
        ))

    proxy_text = "\n".join(proxies)
    e = discord.Embed(title="✅ Oxylabs Residential Proxies Generated", color=CLR_OXYLABS)
    e.add_field(name="Amount", value=str(len(proxies)), inline=True)
    e.add_field(name="Country", value=country, inline=True)
    if state:
        e.add_field(name="State", value=state, inline=True)
    if city:
        e.add_field(name="City", value=city, inline=True)
    session_summary_fields(e, lifetime, "Oxylabs Residential")

    if len(proxy_text) <= 1900:
        e.description = f"```\n{proxy_text}\n```"
        await interaction.followup.send(embed=footer(e))
    else:
        buf = io.BytesIO(proxy_text.encode())
        file = discord.File(buf, filename="proxies_oxylabs_resi.txt")
        await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="oxylabs_resi_generate_spread", description="Generate Oxylabs Residential proxies spread across US states")
@app_commands.describe(
    total_amount="Total proxies (no limit)", session="Session type", protocol="Protocol",
    format="Output format", lifetime="Session lifetime in minutes (1-300)",
)
@app_commands.choices(
    session=[app_commands.Choice(name="rotating", value="rotating"), app_commands.Choice(name="sticky", value="sticky")],
    protocol=[app_commands.Choice(name="http", value="http"), app_commands.Choice(name="socks5", value="socks5")],
    format=FORMAT_CHOICES,
)
async def oxylabs_resi_generate_spread(
    interaction: discord.Interaction, total_amount: int = 100,
    session: app_commands.Choice[str] = None, protocol: app_commands.Choice[str] = None,
    format: app_commands.Choice[int] = None, lifetime: int = 120,
):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    if lifetime > MAX_LIFETIME:
        return await interaction.response.send_message(embed=lifetime_blocked_embed())

    await interaction.response.defer()
    total_amount = max(10, total_amount)
    sess_val = session.value if session else "sticky"
    proto_val = protocol.value if protocol else "http"
    fmt_val = format.value if format else 2

    import itertools
    proxies = []
    state_cycle = itertools.cycle(OXYLABS_SPREAD_STATES)
    for _ in range(total_amount):
        st = next(state_cycle)
        sessid = rand8() if sess_val == "sticky" else None
        proxies.append(build_oxylabs_proxy(
            OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD,
            "US", st, None, sess_val, sessid, proto_val, fmt_val, "residential"
        ))

    proxy_text = "\n".join(proxies)
    buf = io.BytesIO(proxy_text.encode())
    file = discord.File(buf, filename="proxies_oxylabs_resi_spread.txt")

    e = discord.Embed(title="✅ Oxylabs Residential Spread Proxies", color=CLR_OXYLABS)
    e.add_field(name="Total", value=str(len(proxies)), inline=True)
    e.add_field(name="States", value=", ".join(OXYLABS_SPREAD_STATES), inline=False)
    e.add_field(name="", value="💡 Each proxy has unique state + sessid for maximum pool diversity", inline=False)
    session_summary_fields(e, lifetime, "Oxylabs Residential")
    await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="oxylabs_resi_balance", description="Show Oxylabs Residential balance")
async def oxylabs_resi_balance(interaction: discord.Interaction):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    await interaction.response.defer()
    await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
    data = await oxylabs_get_stats(oxylabs_resi_jwt)
    if not data:
        e = discord.Embed(title="❌ Error", description="Could not fetch stats.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    gb_used = float(data.get("traffic", data.get("gb_used", 0)))
    date_from = data.get("date_from", "N/A")
    date_to = data.get("date_to", "N/A")
    caps = load_json("oxylabs_resi_caps.json")
    cap = caps.get("cap_gb")

    e = discord.Embed(title="💰 Oxylabs Residential Balance", color=CLR_OXYLABS)
    e.add_field(name="GB Used", value=f"{gb_used:.2f}", inline=True)
    e.add_field(name="Billing Period", value=f"{date_from} → {date_to}", inline=True)
    if cap:
        pct = gb_used / cap * 100 if cap > 0 else 0
        e.add_field(name="Cap", value=f"{progress_bar(gb_used, cap)} {gb_used:.2f} / {cap:.2f} GB ({pct:.1f}%)", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="oxylabs_resi_set_cap", description="Set Oxylabs Residential usage cap")
@app_commands.describe(cap_gb="Cap in GB")
async def oxylabs_resi_set_cap(interaction: discord.Interaction, cap_gb: float):
    caps = load_json("oxylabs_resi_caps.json")
    caps["cap_gb"] = cap_gb
    save_json("oxylabs_resi_caps.json", caps)
    e = discord.Embed(title="✅ Cap Set", description=f"Oxylabs Residential cap: {cap_gb:.2f} GB", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_resi_set_alert", description="Set Oxylabs Residential usage alert")
@app_commands.describe(threshold_gb="Alert when GB used reaches this", channel="Channel for alerts")
async def oxylabs_resi_set_alert(interaction: discord.Interaction, threshold_gb: float, channel: discord.TextChannel):
    alerts = {"threshold_gb": threshold_gb, "channel_id": channel.id, "breached": False}
    save_json("oxylabs_resi_alerts.json", alerts)
    e = discord.Embed(title="✅ Alert Set", description=f"Alert when usage ≥ {threshold_gb:.2f} GB in {channel.mention}", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_resi_remove_alert", description="Remove Oxylabs Residential alert")
async def oxylabs_resi_remove_alert(interaction: discord.Interaction):
    save_json("oxylabs_resi_alerts.json", {})
    e = discord.Embed(title="✅ Alert Removed", description="Oxylabs Residential alert removed.", color=CLR_SUCCESS)
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_resi_alerts_list", description="List Oxylabs Residential alerts")
async def oxylabs_resi_alerts_list(interaction: discord.Interaction):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    await interaction.response.defer()
    alerts = load_json("oxylabs_resi_alerts.json")
    e = discord.Embed(title="🔔 Oxylabs Residential Alerts", color=CLR_OXYLABS)
    if not alerts or "threshold_gb" not in alerts:
        e.description = "No alerts configured."
    else:
        await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_resi_jwt)
        gb_used = f"{float(data.get('traffic', 0)):.2f} GB" if data else "N/A"
        ch = bot.get_channel(alerts["channel_id"])
        ch_name = ch.mention if ch else f"#{alerts['channel_id']}"
        status = "⚠️ BREACHED" if alerts.get("breached") else "✅ OK"
        e.add_field(name="Alert", value=f"Threshold: {alerts['threshold_gb']:.2f} GB used | Channel: {ch_name}\nCurrent: {gb_used} | {status}", inline=False)
    await interaction.followup.send(embed=footer(e))


@bot.tree.command(name="oxylabs_resi_usage_history", description="Show Oxylabs Residential usage history (last 7 days)")
async def oxylabs_resi_usage_history(interaction: discord.Interaction):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    history = load_json("oxylabs_resi_history.json")
    e = discord.Embed(title="📈 Oxylabs Residential Usage History (7 days)", color=CLR_OXYLABS)
    if not history:
        e.description = "No history yet."
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        entries = [(k, v) for k, v in sorted(history.items()) if k >= cutoff]
        lines = ["Date | GB Used | Daily Change"]
        for i, (date, val) in enumerate(entries[-7:]):
            used = val.get("gb_used", 0)
            change = f"{used - entries[max(0, i-1)][1].get('gb_used', 0):+.2f}" if i > 0 else "—"
            lines.append(f"{date} | {used:.2f} | {change}")
        e.description = "```\n" + "\n".join(lines) + "\n```"
    await interaction.response.send_message(embed=footer(e))


@bot.tree.command(name="oxylabs_resi_top_up", description="Get Oxylabs Residential top-up links")
async def oxylabs_resi_top_up(interaction: discord.Interaction):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    await interaction.response.defer()
    await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
    data = await oxylabs_get_stats(oxylabs_resi_jwt)
    e = discord.Embed(title="💳 Oxylabs Residential — Top Up", color=CLR_ERROR)
    if data:
        e.add_field(name="GB Used", value=f"{float(data.get('traffic', 0)):.2f} GB", inline=True)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="💳 Top Up", url="https://dashboard.oxylabs.io"))
    view.add_item(discord.ui.Button(label="📊 Dashboard", url="https://dashboard.oxylabs.io/?route=/overview/RP"))
    await interaction.followup.send(embed=footer(e), view=view)


@bot.tree.command(name="oxylabs_resi_test", description="Test Oxylabs Residential proxy connection")
async def oxylabs_resi_test(interaction: discord.Interaction):
    if not OXYLABS_RESI_ENABLED:
        return await interaction.response.send_message(embed=not_configured_embed("Oxylabs Residential"))
    await interaction.response.defer()
    proxy_url = f"http://customer-{OXYLABS_RESIDENTIAL_USERNAME}:{OXYLABS_RESIDENTIAL_PASSWORD}@pr.oxylabs.io:7777"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://ipinfo.io/json", proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                ip_addr = data.get("ip", "Unknown")
                city = data.get("city", "")
                region = data.get("region", "")
                country = data.get("country", "")
                org = data.get("org", "")
                location = ", ".join(filter(None, [city, region, country]))
                e = discord.Embed(title="✅ Oxylabs Residential Test Passed", color=CLR_OXYLABS)
                e.add_field(name="Exit IP", value=ip_addr, inline=True)
                e.add_field(name="Location", value=location or "Unknown", inline=True)
                e.add_field(name="ISP", value=org or "Unknown", inline=True)
    except Exception as ex:
        e = discord.Embed(title="❌ Oxylabs Residential Test Failed", description=str(ex)[:500], color=CLR_ERROR)
    await interaction.followup.send(embed=footer(e))


# ===================================================================
# PROXY VERIFICATION COMMANDS
# ===================================================================
@bot.tree.command(name="verify_proxies", description="Verify a list of proxies from an uploaded .txt file")
@app_commands.describe(
    attachment="Upload a .txt proxy list file",
    max_test="Max proxies to test (5-50)",
    timeout="Timeout per proxy in seconds (3-15)",
)
async def verify_proxies(interaction: discord.Interaction, attachment: discord.Attachment, max_test: int = 20, timeout: int = 8):
    cd = check_cooldown("verify_proxies", interaction.user.id, 15)
    if cd > 0:
        e = discord.Embed(title="⏳ Cooldown", description=f"Please wait {cd:.1f}s", color=CLR_ERROR)
        return await interaction.response.send_message(embed=footer(e), ephemeral=True)

    await interaction.response.defer()
    max_test = max(5, min(max_test, 50))
    timeout = max(3, min(timeout, 15))

    try:
        content = (await attachment.read()).decode("utf-8", errors="ignore")
    except Exception:
        e = discord.Embed(title="❌ Error", description="Could not read attachment.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if not lines:
        e = discord.Embed(title="❌ Error", description="No proxies found in file.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    sample = random.sample(lines, min(max_test, len(lines)))
    sem = asyncio.Semaphore(10)
    results = []

    async def test_proxy(proxy_line):
        parsed = parse_proxy(proxy_line)
        if not parsed:
            return {"proxy": proxy_line, "status": "PARSE_ERROR"}
        user, pwd, host, port, protocol = parsed

        test_url = "https://ip.oxylabs.io/location" if "oxylabs" in host else "https://ipinfo.io/json"
        proxy_url = f"{'socks5' if protocol == 'socks5' else 'http'}://{user}:{pwd}@{host}:{port}"

        async with sem:
            try:
                if protocol == "socks5":
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(proxy_url)
                    async with aiohttp.ClientSession(connector=connector) as s:
                        async with s.get(test_url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                            data = await resp.json()
                else:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(test_url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                            data = await resp.json()
                return {
                    "proxy": proxy_line, "status": "OK",
                    "ip": data.get("ip", data.get("query", "?")),
                    "country": data.get("country", "?"),
                    "region": data.get("region", data.get("state", "?")),
                    "city": data.get("city", "?"),
                    "isp": data.get("org", data.get("isp", "?")),
                }
            except Exception as ex:
                return {"proxy": proxy_line, "status": f"FAIL: {str(ex)[:80]}"}

    results = await asyncio.gather(*[test_proxy(p) for p in sample])

    success = [r for r in results if r["status"] == "OK"]
    failed = [r for r in results if r["status"] != "OK"]
    unique_ips = set(r["ip"] for r in success)
    unique_states = set(r["region"] for r in success if r.get("region") and r["region"] != "?")
    unique_cities = set(r["city"] for r in success if r.get("city") and r["city"] != "?")
    unique_isps = set(r["isp"] for r in success if r.get("isp") and r["isp"] != "?")
    duplicates = len(success) - len(unique_ips)
    geo_score = min(10, round(len(unique_states) / max(1, len(success)) * 10))

    if geo_score >= 8:
        score_icon = "🟢 Excellent"
    elif geo_score >= 5:
        score_icon = "🟡 Moderate"
    else:
        score_icon = "🔴 Poor"

    color = CLR_EVOMI if geo_score >= 7 else CLR_ERROR
    e = discord.Embed(title="🔍 Proxy Verification Results", color=color)
    e.add_field(name="✅ Successful", value=f"{len(success)} / {len(results)} tested", inline=True)
    e.add_field(name="❌ Failed", value=str(len(failed)), inline=True)
    e.add_field(name="📊 IP Diversity Report", value=(
        f"Unique IPs: **{len(unique_ips)}** of {len(success)} tested\n"
        f"Unique States: **{len(unique_states)}** — {', '.join(sorted(unique_states)[:10]) or 'N/A'}\n"
        f"Unique Cities: **{len(unique_cities)}** — {', '.join(sorted(unique_cities)[:5]) or 'N/A'}\n"
        f"Unique ISPs: **{len(unique_isps)}** — {', '.join(sorted(unique_isps)[:5]) or 'N/A'}\n"
        f"Duplicate IPs: **{duplicates}**"
    ), inline=False)
    e.add_field(name=f"Geographic Spread Score: {geo_score}/10", value=score_icon, inline=False)

    # Build results file
    result_lines = ["proxy_string | exit_ip | country | state | city | isp | status"]
    for r in results:
        if r["status"] == "OK":
            result_lines.append(f"{r['proxy']} | {r['ip']} | {r['country']} | {r['region']} | {r['city']} | {r['isp']} | OK")
        else:
            result_lines.append(f"{r['proxy']} | - | - | - | - | - | {r['status']}")

    buf = io.BytesIO("\n".join(result_lines).encode())
    file = discord.File(buf, filename="verify_results.txt")
    await interaction.followup.send(embed=footer(e), file=file)


@bot.tree.command(name="test_single_proxy", description="Test a single proxy")
@app_commands.describe(proxy_string="Proxy string in any format", protocol="Protocol")
@app_commands.choices(protocol=[app_commands.Choice(name="http", value="http"), app_commands.Choice(name="socks5", value="socks5")])
async def test_single_proxy(interaction: discord.Interaction, proxy_string: str, protocol: app_commands.Choice[str] = None):
    await interaction.response.defer()
    parsed = parse_proxy(proxy_string)
    proto_val = protocol.value if protocol else "http"
    if not parsed:
        e = discord.Embed(title="❌ Parse Error", description="Could not parse proxy string.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    user, pwd, host, port, _ = parsed
    test_url = "https://ip.oxylabs.io/location" if "oxylabs" in host else "https://ipinfo.io/json"
    proxy_url = f"{proto_val}://{user}:{pwd}@{host}:{port}"

    import time
    start = time.monotonic()
    try:
        if proto_val == "socks5":
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy_url)
            async with aiohttp.ClientSession(connector=connector) as s:
                async with s.get(test_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
        else:
            async with aiohttp.ClientSession() as s:
                async with s.get(test_url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()

        latency = int((time.monotonic() - start) * 1000)
        e = discord.Embed(title="✅ Proxy Test Passed", color=CLR_SUCCESS)
        e.add_field(name="Exit IP", value=data.get("ip", data.get("query", "?")), inline=True)
        e.add_field(name="Country", value=data.get("country", "?"), inline=True)
        e.add_field(name="State", value=data.get("region", data.get("state", "?")), inline=True)
        e.add_field(name="City", value=data.get("city", "?"), inline=True)
        e.add_field(name="ISP", value=data.get("org", data.get("isp", "?")), inline=True)
        e.add_field(name="Latency", value=f"{latency} ms", inline=True)
        mobile = data.get("mobile", data.get("hosting", "Unknown"))
        e.add_field(name="Mobile", value=str(mobile), inline=True)
    except Exception as ex:
        e = discord.Embed(title="❌ Proxy Test Failed", description=str(ex)[:500], color=CLR_ERROR)
    await interaction.followup.send(embed=footer(e))


# ===================================================================
# SPREAD ALL / COMBINED COMMANDS
# ===================================================================
@bot.tree.command(name="spread_all", description="Run all configured provider spread commands simultaneously")
async def spread_all(interaction: discord.Interaction):
    cd = check_cooldown("spread_all", interaction.user.id, 10)
    if cd > 0:
        e = discord.Embed(title="⏳ Cooldown", description=f"Please wait {cd:.1f}s", color=CLR_ERROR)
        return await interaction.response.send_message(embed=footer(e), ephemeral=True)

    await interaction.response.defer()
    files = []
    counts = {}

    async def evomi_spread():
        if not EVOMI_ENABLED:
            return
        batches = 5
        all_p = []
        for i in range(batches):
            params = {"apikey": EVOMI_API_KEY, "product": "rp", "amount": 10, "countries": "US",
                      "session": "none", "protocol": "http", "seed": random.randint(1, 999999)}
            try:
                async with http_session.get(f"{EVOMI_BASE}/generate", params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        px = data if isinstance(data, list) else data.get("proxies", [])
                        if isinstance(px, str):
                            px = [p.strip() for p in px.strip().splitlines() if p.strip()]
                        all_p.extend(px)
            except Exception:
                pass
        seen = set()
        unique = [str(p) for p in all_p if str(p).strip() not in seen and not seen.add(str(p).strip())]
        if unique:
            counts["Evomi"] = len(unique)
            files.append(discord.File(io.BytesIO("\n".join(unique).encode()), filename="proxies_evomi_spread.txt"))

    async def proxidize_spread():
        if not PROXIDIZE_ENABLED:
            return
        all_p = []
        for city in PROXIDIZE_SPREAD_CITIES:
            payload = {"amount": 8, "city": city, "carrier": "fastest", "protocol": "http", "mode": "sticky", "lifetime": 120}
            data = await proxidize_post("/pergb/generate", payload)
            if data:
                px = data if isinstance(data, list) else data.get("proxies", [])
                if isinstance(px, str):
                    px = [p.strip() for p in px.strip().splitlines() if p.strip()]
                all_p.extend(px)
        seen = set()
        unique = [str(p) for p in all_p if str(p).strip() not in seen and not seen.add(str(p).strip())]
        if unique:
            counts["Proxidize"] = len(unique)
            files.append(discord.File(io.BytesIO("\n".join(unique).encode()), filename="proxies_proxidize_spread.txt"))

    async def oxy_mobile_spread():
        if not OXYLABS_MOBILE_ENABLED:
            return
        import itertools
        proxies = []
        for st in OXYLABS_SPREAD_STATES:
            for _ in range(7):
                proxies.append(build_oxylabs_proxy(
                    OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD,
                    "US", st, None, "sticky", rand8(), "http", 2, "mobile"
                ))
        counts["Oxylabs Mobile"] = len(proxies)
        files.append(discord.File(io.BytesIO("\n".join(proxies).encode()), filename="proxies_oxylabs_mobile_spread.txt"))

    async def oxy_resi_spread():
        if not OXYLABS_RESI_ENABLED:
            return
        import itertools
        proxies = []
        for st in OXYLABS_SPREAD_STATES:
            for _ in range(7):
                proxies.append(build_oxylabs_proxy(
                    OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD,
                    "US", st, None, "sticky", rand8(), "http", 2, "residential"
                ))
        counts["Oxylabs Residential"] = len(proxies)
        files.append(discord.File(io.BytesIO("\n".join(proxies).encode()), filename="proxies_oxylabs_resi_spread.txt"))

    await asyncio.gather(evomi_spread(), proxidize_spread(), oxy_mobile_spread(), oxy_resi_spread())

    e = discord.Embed(title="🌍 Spread All — Results", color=CLR_EVOMI)
    if counts:
        for provider, count in counts.items():
            e.add_field(name=provider, value=f"{count} proxies", inline=True)
    else:
        e.description = "No providers configured."
    await interaction.followup.send(embed=footer(e), files=files if files else discord.utils.MISSING)


@bot.tree.command(name="status_all", description="Show status for all configured providers")
async def status_all(interaction: discord.Interaction):
    await interaction.response.defer()
    embeds = []
    any_issues = False

    # --- EVOMI ---
    if EVOMI_ENABLED:
        data = await evomi_get()
        if data:
            e = discord.Embed(title="", color=CLR_EVOMI)
            lines = []
            for p in evomi_parse_products(data):
                gb = p["balance_mb"] / 1024
                if gb > 1:
                    icon = "🟢"
                elif gb > 0.1:
                    icon = "🟡"
                else:
                    icon = "🔴"
                    any_issues = True
                lines.append(f"{icon} **{p['name']}** — {gb:.2f} GB")
            e.add_field(name="📘 Evomi", value="\n".join(lines), inline=False)
            embeds.append(e)

    # --- PROXIDIZE ---
    if PROXIDIZE_ENABLED:
        data = await proxidize_get("/pergb/mobile/user-info")
        if data:
            gb_used, gb_rem, gb_total = proxidize_parse_balance(data)
            pct = gb_used / gb_total * 100 if gb_total > 0 else 0
            icon = "🟢" if gb_rem > 5 else "🟡" if gb_rem >= 1 else "🔴"
            if gb_rem < 1:
                any_issues = True
            e = discord.Embed(title="", color=CLR_PROXIDIZE)
            e.add_field(name="📙 Proxidize Mobile", value=(
                f"{icon} **{gb_rem:.2f} GB** remaining\n"
                f"{progress_bar(gb_used, gb_total)} {gb_used:.2f} / {gb_total:.2f} GB ({pct:.1f}% used)"
            ), inline=False)
            embeds.append(e)

    # --- OXYLABS MOBILE ---
    if OXYLABS_MOBILE_ENABLED:
        await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_mobile_jwt)
        if data:
            gb = float(data.get("traffic", 0))
            caps = load_json("oxylabs_mobile_caps.json")
            cap = caps.get("cap_gb")
            e = discord.Embed(title="", color=CLR_OXYLABS)
            if cap:
                rem = max(0, cap - gb)
                pct = gb / cap * 100 if cap > 0 else 0
                icon = "🟢" if rem > 5 else "🟡" if rem >= 1 else "🔴"
                if rem < 1:
                    any_issues = True
                e.add_field(name="📱 Oxylabs Mobile", value=(
                    f"{icon} **{rem:.2f} GB** remaining\n"
                    f"{progress_bar(gb, cap)} {gb:.2f} / {cap:.2f} GB ({pct:.1f}% used)"
                ), inline=False)
            else:
                e.add_field(name="📱 Oxylabs Mobile", value=f"🟢 {gb:.2f} GB used", inline=False)
            embeds.append(e)

    # --- OXYLABS RESIDENTIAL ---
    if OXYLABS_RESI_ENABLED:
        await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_resi_jwt)
        if data:
            gb = float(data.get("traffic", 0))
            caps = load_json("oxylabs_resi_caps.json")
            cap = caps.get("cap_gb")
            e = discord.Embed(title="", color=CLR_OXYLABS)
            if cap:
                rem = max(0, cap - gb)
                pct = gb / cap * 100 if cap > 0 else 0
                icon = "🟢" if rem > 5 else "🟡" if rem >= 1 else "🔴"
                if rem < 1:
                    any_issues = True
                e.add_field(name="🏠 Oxylabs Residential", value=(
                    f"{icon} **{rem:.2f} GB** remaining\n"
                    f"{progress_bar(gb, cap)} {gb:.2f} / {cap:.2f} GB ({pct:.1f}% used)"
                ), inline=False)
            else:
                e.add_field(name="🏠 Oxylabs Residential", value=f"🟢 {gb:.2f} GB used", inline=False)
            embeds.append(e)

    if not embeds:
        e = discord.Embed(title="📊 Provider Status", description="No providers configured.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    # Title on first, health + footer on last
    embeds[0].title = "📊 Provider Status"
    health = "⚠️ Attention needed — check low balances" if any_issues else "✅ All providers healthy"
    embeds[-1].add_field(name="", value=f"─────────────────────\n{health}", inline=False)
    footer(embeds[-1])
    await interaction.followup.send(embeds=embeds)


@bot.tree.command(name="balance_all", description="Show balances for all configured providers")
async def balance_all(interaction: discord.Interaction):
    await interaction.response.defer()
    embeds = []

    # --- EVOMI ---
    if EVOMI_ENABLED:
        data = await evomi_get()
        if data:
            e = discord.Embed(title="", color=CLR_EVOMI)
            lines = []
            for p in evomi_parse_products(data):
                gb = p["balance_mb"] / 1024
                if gb > 1:
                    icon = "🟢"
                elif gb > 0.1:
                    icon = "🟡"
                else:
                    icon = "🔴"
                lines.append(f"{icon} **{p['name']}** — {gb:.2f} GB")
            e.add_field(name="📘 Evomi", value="\n".join(lines) if lines else "No products", inline=False)
            embeds.append(e)

    # --- PROXIDIZE ---
    if PROXIDIZE_ENABLED:
        data = await proxidize_get("/pergb/mobile/user-info")
        if data:
            gb_used, gb_rem, gb_total = proxidize_parse_balance(data)
            pct = gb_used / gb_total * 100 if gb_total > 0 else 0
            icon = "🟢" if gb_rem > 5 else "🟡" if gb_rem >= 1 else "🔴"
            e = discord.Embed(title="", color=CLR_PROXIDIZE)
            e.add_field(name="📙 Proxidize Mobile", value=(
                f"{icon} **{gb_rem:.2f} GB** remaining\n"
                f"{progress_bar(gb_used, gb_total)} {gb_used:.2f} / {gb_total:.2f} GB ({pct:.1f}% used)"
            ), inline=False)
            embeds.append(e)

    # --- OXYLABS MOBILE ---
    if OXYLABS_MOBILE_ENABLED:
        await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_mobile_jwt)
        if data:
            gb_used = float(data.get("traffic", 0))
            caps = load_json("oxylabs_mobile_caps.json")
            cap = caps.get("cap_gb")
            e = discord.Embed(title="", color=CLR_OXYLABS)
            if cap:
                remaining = max(0, cap - gb_used)
                pct = gb_used / cap * 100 if cap > 0 else 0
                icon = "🟢" if remaining > 5 else "🟡" if remaining >= 1 else "🔴"
                e.add_field(name="📱 Oxylabs Mobile", value=(
                    f"{icon} **{remaining:.2f} GB** remaining\n"
                    f"{progress_bar(gb_used, cap)} {gb_used:.2f} / {cap:.2f} GB ({pct:.1f}% used)"
                ), inline=False)
            else:
                e.add_field(name="📱 Oxylabs Mobile", value=f"🟢 {gb_used:.2f} GB used\n*Set cap with `/oxylabs_mobile_set_cap` to see remaining*", inline=False)
            embeds.append(e)

    # --- OXYLABS RESIDENTIAL ---
    if OXYLABS_RESI_ENABLED:
        await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_resi_jwt)
        if data:
            gb_used = float(data.get("traffic", 0))
            caps = load_json("oxylabs_resi_caps.json")
            cap = caps.get("cap_gb")
            e = discord.Embed(title="", color=CLR_OXYLABS)
            if cap:
                remaining = max(0, cap - gb_used)
                pct = gb_used / cap * 100 if cap > 0 else 0
                icon = "🟢" if remaining > 5 else "🟡" if remaining >= 1 else "🔴"
                e.add_field(name="🏠 Oxylabs Residential", value=(
                    f"{icon} **{remaining:.2f} GB** remaining\n"
                    f"{progress_bar(gb_used, cap)} {gb_used:.2f} / {cap:.2f} GB ({pct:.1f}% used)"
                ), inline=False)
            else:
                e.add_field(name="🏠 Oxylabs Residential", value=f"🟢 {gb_used:.2f} GB used\n*Set cap with `/oxylabs_resi_set_cap` to see remaining*", inline=False)
            embeds.append(e)

    if not embeds:
        e = discord.Embed(title="💰 Balance Overview", description="No providers configured.", color=CLR_ERROR)
        return await interaction.followup.send(embed=footer(e))

    # Add title to first embed, footer to last
    embeds[0].title = "💰 Balance Overview"
    footer(embeds[-1])
    await interaction.followup.send(embeds=embeds)


@bot.tree.command(name="proxy_help", description="Full command reference for ProxyBot")
async def proxy_help(interaction: discord.Interaction):
    e = discord.Embed(title="📖 ProxyBot Command Reference", color=CLR_EVOMI)

    e.add_field(name="📘 Evomi Commands", value=(
        "`/evomi_status` — Account status\n"
        "`/evomi_generate` — Generate proxies\n"
        "`/evomi_generate_spread` — Spread proxies across pool\n"
        "`/evomi_rotate` — Rotate session\n"
        "`/evomi_balance` — Check balance\n"
        "`/evomi_set_cap` — Set usage cap\n"
        "`/evomi_set_alert` — Set balance alert\n"
        "`/evomi_remove_alert` — Remove alert\n"
        "`/evomi_alerts_list` — List alerts\n"
        "`/evomi_usage_history` — 7-day history\n"
        "`/evomi_top_up` — Top-up links"
    ), inline=False)

    e.add_field(name="📙 Proxidize Commands", value=(
        "`/proxidize_status` — Account status\n"
        "`/proxidize_generate` — Generate mobile proxies\n"
        "`/proxidize_generate_spread` — Spread across 7 cities\n"
        "`/proxidize_rotate` — Rotate session\n"
        "`/proxidize_balance` — Check balance\n"
        "`/proxidize_set_cap` — Set usage cap\n"
        "`/proxidize_set_alert` — Set balance alert\n"
        "`/proxidize_remove_alert` — Remove alert\n"
        "`/proxidize_alerts_list` — List alerts\n"
        "`/proxidize_usage_history` — 7-day history\n"
        "`/proxidize_top_up` — Top-up links"
    ), inline=False)

    e.add_field(name="📱 Oxylabs Mobile Commands", value=(
        "`/oxylabs_mobile_status` — Account status\n"
        "`/oxylabs_mobile_generate` — Generate proxies\n"
        "`/oxylabs_mobile_generate_spread` — Spread across 15 states\n"
        "`/oxylabs_mobile_balance` — Check balance\n"
        "`/oxylabs_mobile_set_cap` — Set usage cap\n"
        "`/oxylabs_mobile_set_alert` — Set usage alert\n"
        "`/oxylabs_mobile_remove_alert` — Remove alert\n"
        "`/oxylabs_mobile_alerts_list` — List alerts\n"
        "`/oxylabs_mobile_usage_history` — 7-day history\n"
        "`/oxylabs_mobile_top_up` — Top-up links\n"
        "`/oxylabs_mobile_test` — Test connection"
    ), inline=False)

    e.add_field(name="🏠 Oxylabs Residential Commands", value=(
        "`/oxylabs_resi_status` — Account status\n"
        "`/oxylabs_resi_generate` — Generate proxies\n"
        "`/oxylabs_resi_generate_spread` — Spread across 15 states\n"
        "`/oxylabs_resi_balance` — Check balance\n"
        "`/oxylabs_resi_set_cap` — Set usage cap\n"
        "`/oxylabs_resi_set_alert` — Set usage alert\n"
        "`/oxylabs_resi_remove_alert` — Remove alert\n"
        "`/oxylabs_resi_alerts_list` — List alerts\n"
        "`/oxylabs_resi_usage_history` — 7-day history\n"
        "`/oxylabs_resi_top_up` — Top-up links\n"
        "`/oxylabs_resi_test` — Test connection"
    ), inline=False)

    e.add_field(name="🔍 Verification Commands", value=(
        "`/verify_proxies` — Verify proxy list from file\n"
        "`/test_single_proxy` — Test one proxy"
    ), inline=False)

    e.add_field(name="🌍 Spread / Diversity Commands", value="`/spread_all` — Run all spreads simultaneously", inline=False)

    e.add_field(name="🔀 Shared Commands", value=(
        "`/status_all` — All provider statuses\n"
        "`/balance_all` — All balances\n"
        "`/proxy_help` — This help\n"
        "`/proxy_best_practices` — Strategy guide"
    ), inline=False)

    e.set_footer(text="ProxyBot — Evomi + Proxidize + Oxylabs | Commands for unconfigured providers show setup instructions")
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="proxy_best_practices", description="Proxy strategy guide based on real-world testing")
async def proxy_best_practices(interaction: discord.Interaction):
    e = discord.Embed(title="📋 Proxy Strategy — Real World Results", color=CLR_EVOMI)

    e.add_field(name="🧪 What Testing Proved", value=(
        "1440 min sessions → ❌ Stuck on flagged/recycled IPs\n"
        "300 min sessions  → ✅ Much fresher IPs, better results\n"
        "Fraud score filter → ❌ Shrinks pool, no benefit\n"
        "Low latency filter → ❌ Shrinks pool, no benefit\n"
        "Mobile proxies    → ✅ Best trust score for account tasks"
    ), inline=False)

    e.add_field(name="⏱️ Session Length Guide", value=(
        "1-60 min   → Scraping, testing, one-off requests\n"
        "120 min    → ✅ Good default for account tasks\n"
        "180 min    → ✅ Sweet spot — fresh + consistent\n"
        "300 min    → ✅ Max recommended\n"
        "300+ min   → ❌ Hard blocked — proven bad results\n"
        "1440 min   → ❌ Hard blocked — worst results"
    ), inline=False)

    e.add_field(name="🎯 IP Priority Order", value=(
        "1. Fresh IP  (120-300 min session = new IP from rotation)\n"
        "2. Clean IP  (comes naturally with fresh pool)\n"
        "3. Fast IP   (never filter for speed — shrinks pool)\n"
        "4. Long session → ❌ Never prioritize"
    ), inline=False)

    e.add_field(name="🚫 What NOT To Do", value=(
        "Never use sessions over 300 min\n"
        "Never filter by fraud score\n"
        "Never filter by latency\n"
        "Never assume clean label = good results"
    ), inline=False)

    e.add_field(name="✅ Recommended Workflow", value=(
        "1. Generate with 120-300 min sticky session\n"
        "2. Mobile proxies first for Walmart and high-trust tasks\n"
        "3. No pool filters — full pool always\n"
        "4. Use `/spread` commands for geographic diversity\n"
        "5. Run `/verify_proxies` to confirm spread score ≥ 7/10\n"
        "6. Regenerate if score < 7/10"
    ), inline=False)

    e.add_field(name="📱 Mobile Proxy Priority", value=(
        "For Walmart + high-trust platforms:\n"
        "→ Proxidize or Oxylabs Mobile first\n"
        "→ Evomi mp second\n"
        "→ Residential only as fallback"
    ), inline=False)

    await interaction.response.send_message(embed=footer(e))


# ===================================================================
# WALMART MEGA COMMAND
# ===================================================================
EVOMI_US_REGIONS = [
    "California", "Texas", "Florida", "New York", "Illinois",
    "Georgia", "Pennsylvania", "Ohio", "North Carolina", "Michigan",
]


async def _evomi_generate_batch(product: str, amount: int, lifetime: int,
                                 activesince: int = None, spread_regions: bool = False) -> list[str]:
    """Generate Evomi proxies in batches via API, returns list of proxy strings."""
    batch_size = 100
    pwd_suffix = f"_activesince-{activesince}" if activesince else ""

    if spread_regions:
        per_region = -(-amount // len(EVOMI_US_REGIONS))
        batches_list = []
        for region in EVOMI_US_REGIONS:
            region_batches = -(-per_region // batch_size)
            for i in range(region_batches):
                n = min(batch_size, per_region - i * batch_size)
                if n > 0:
                    batches_list.append({"amount": n, "region": region})
    else:
        total_batches = -(-amount // batch_size)
        batches_list = [{"amount": min(batch_size, amount - i * batch_size), "region": None} for i in range(total_batches)]

    all_proxies = []

    for batch_info in batches_list:
        params = {
            "apikey": EVOMI_API_KEY,
            "product": product,
            "amount": batch_info["amount"],
            "countries": "US",
            "session": "sticky",
            "protocol": "http",
            "lifetime": lifetime,
            "prepend_protocol": "false",
            "format": "2",
        }
        if batch_info["region"]:
            params["region"] = batch_info["region"]
        for attempt in range(3):
            try:
                async with http_session.get(f"{EVOMI_BASE}/generate", params=params) as resp:
                    if resp.status == 200:
                        txt = await resp.text()
                        all_proxies.extend([p.strip() for p in txt.strip().splitlines() if p.strip()])
                        break
                    elif resp.status == 429:
                        await asyncio.sleep(2 + attempt)
                    else:
                        break
            except Exception:
                break
        await asyncio.sleep(0.15)

    # Append activesince to password if needed, keep original format from API
    # Format 2: host:port:user:pass — password is the last field
    formatted = []
    for p in all_proxies:
        ps = str(p).strip()
        if not ps:
            continue
        if pwd_suffix:
            # Append suffix to the password (last colon-separated field)
            ps = ps + pwd_suffix
        formatted.append(ps)
    random.shuffle(formatted)
    return formatted


@bot.tree.command(name="walmart", description="Generate optimized proxy lists for Walmart from ALL providers")
@app_commands.describe(
    amount="Proxies per provider (default 10000)",
    lifetime="Session lifetime in minutes (default 180)",
)
async def walmart_generate(interaction: discord.Interaction, amount: int = 5000, lifetime: int = 180):
    if lifetime > MAX_LIFETIME:
        return await interaction.response.send_message(embed=lifetime_blocked_embed())

    await interaction.response.defer()

    files = []
    results = {}
    errors = []

    # --- 1. Evomi Mobile (mp) — no activesince, full US pool ---
    async def gen_evomi_mobile():
        if not EVOMI_ENABLED:
            return
        try:
            proxies = await _evomi_generate_batch("mp", amount, lifetime, activesince=None, spread_regions=False)
            if proxies:
                results["Evomi Mobile"] = len(proxies)
                files.append(("proxies_walmart_evomi_mobile.txt", "\n".join(proxies)))
            else:
                errors.append("Evomi Mobile: no proxies returned")
        except Exception as ex:
            errors.append(f"Evomi Mobile: {str(ex)[:100]}")

    # --- 2. Evomi Core Residential (rpc) — activesince-60, full US pool ---
    async def gen_evomi_core():
        if not EVOMI_ENABLED:
            return
        try:
            proxies = await _evomi_generate_batch("rpc", amount, lifetime, activesince=60, spread_regions=False)
            if proxies:
                results["Evomi Core Resi"] = len(proxies)
                files.append(("proxies_walmart_evomi_core.txt", "\n".join(proxies)))
            else:
                errors.append("Evomi Core: no proxies returned")
        except Exception as ex:
            errors.append(f"Evomi Core: {str(ex)[:100]}")

    # --- 3. Oxylabs Mobile ---
    async def gen_oxylabs_mobile():
        if not OXYLABS_MOBILE_ENABLED:
            return
        try:
            import itertools
            proxies = []
            state_cycle = itertools.cycle(OXYLABS_SPREAD_STATES)
            for _ in range(amount):
                st = next(state_cycle)
                sessid = rand8()
                proxies.append(build_oxylabs_proxy(
                    OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD,
                    "US", st, None, "sticky", sessid, "http", 2, "mobile"
                ))
            random.shuffle(proxies)
            results["Oxylabs Mobile"] = len(proxies)
            files.append(("proxies_walmart_oxylabs_mobile.txt", "\n".join(proxies)))
        except Exception as ex:
            errors.append(f"Oxylabs Mobile: {str(ex)[:100]}")

    # --- 4. Proxidize (24-city spread, local proxy building) ---
    async def gen_proxidize():
        if not PROXIDIZE_ENABLED:
            return
        try:
            aps = await proxidize_get("/pergb/mobile/access-point")
            if not aps or not isinstance(aps, list) or len(aps) == 0:
                errors.append("Proxidize: no access points found")
                return

            ap = next((a for a in aps if a.get("username") and a.get("password")), None)
            if not ap:
                errors.append("Proxidize: no access points with credentials")
                return

            import itertools
            base_user = ap["username"]
            pwd = ap["password"]
            city_cycle = itertools.cycle(PROXIDIZE_CITIES)
            proxies = []
            for _ in range(amount):
                c = next(city_cycle)
                proxies.append(build_proxidize_proxy(base_user, pwd, c["state"], c["city"]))

            random.shuffle(proxies)
            results["Proxidize"] = len(proxies)
            files.append(("proxies_walmart_proxidize.txt", "\n".join(proxies)))
        except Exception as ex:
            errors.append(f"Proxidize: {str(ex)[:100]}")

    # Run all concurrently
    # Run Oxylabs + Proxidize concurrently (local/fast), then Evomi sequentially (API rate limited)
    await asyncio.gather(gen_oxylabs_mobile(), gen_proxidize())
    await gen_evomi_mobile()
    await gen_evomi_core()

    if not results:
        e = discord.Embed(
            title="❌ Walmart Generate Failed",
            description="No providers returned proxies.\n" + "\n".join(errors),
            color=CLR_ERROR,
        )
        return await interaction.followup.send(embed=footer(e))

    # Build embeds
    total = sum(results.values())
    e = discord.Embed(
        title="🛒 Walmart Proxy Lists Generated",
        description=f"**{total:,} total proxies** across {len(results)} providers",
        color=0x0071DC,  # Walmart blue
    )

    for provider, count in results.items():
        e.add_field(name=provider, value=f"**{count:,}** proxies", inline=True)

    e.add_field(name="", value="─────────────────────", inline=False)
    e.add_field(name="Optimization", value=(
        "✅ Sticky sessions on all providers\n"
        "✅ Evomi Mobile — full US pool, no filters\n"
        "✅ Evomi Core — `_activesince-60`, full US pool\n"
        "✅ Oxylabs Mobile — 15-state spread\n"
        "✅ Proxidize — 24-city spread\n"
        f"✅ {lifetime} min session lifetime\n"
        "✅ US only — full pool freshness"
    ), inline=False)

    if errors:
        e.add_field(name="⚠️ Warnings", value="\n".join(errors), inline=False)

    # Upload files
    discord_files = []
    for fname, content in files:
        discord_files.append(discord.File(io.BytesIO(content.encode()), filename=fname))

    await interaction.followup.send(embed=footer(e), files=discord_files)


# ===================================================================
# BACKGROUND TASKS
# ===================================================================
@tasks.loop(minutes=5)
async def evomi_background():
    if not EVOMI_ENABLED:
        return
    await asyncio.sleep(0)  # offset 0 min
    try:
        data = await evomi_get()
        if not data:
            return

        # Snapshot
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        history = load_json("evomi_history.json")
        entry = history.get(today, {})

        for p in evomi_parse_products(data):
            entry[p["code"]] = p["balance_mb"]
        history[today] = entry

        # Prune 30 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        history = {k: v for k, v in history.items() if k >= cutoff}
        save_json("evomi_history.json", history)

        # Check alerts
        alerts = load_json("evomi_alerts.json")
        for prod_code, alert in alerts.items():
            balance = entry.get(prod_code, 99999)
            if balance <= alert["threshold_mb"] and not alert.get("breached"):
                alert["breached"] = True
                ch = bot.get_channel(alert["channel_id"])
                if ch:
                    e = discord.Embed(
                        title=f"🚨 Low Balance Alert — {EVOMI_PRODUCTS.get(prod_code, prod_code)}",
                        description=f"Balance: **{balance:.2f} MB** (threshold: {alert['threshold_mb']} MB)",
                        color=CLR_ERROR,
                    )
                    await ch.send(embed=footer(e))
            elif balance > alert["threshold_mb"] and alert.get("breached"):
                alert["breached"] = False
        save_json("evomi_alerts.json", alerts)
    except Exception:
        pass


@tasks.loop(minutes=5)
async def proxidize_background():
    if not PROXIDIZE_ENABLED:
        return
    await asyncio.sleep(60)  # offset 1 min
    try:
        data = await proxidize_get("/pergb/mobile/user-info")
        if not data:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        history = load_json("proxidize_history.json")
        gb_used, gb_remaining, gb_total = proxidize_parse_balance(data)
        history[today] = {"remaining": gb_remaining, "used": gb_used, "total": gb_total}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        history = {k: v for k, v in history.items() if k >= cutoff}
        save_json("proxidize_history.json", history)

        alerts = load_json("proxidize_alerts.json")
        if "threshold_gb" in alerts:
            remaining = gb_remaining
            if remaining <= alerts["threshold_gb"] and not alerts.get("breached"):
                alerts["breached"] = True
                ch = bot.get_channel(alerts["channel_id"])
                if ch:
                    e = discord.Embed(
                        title="🚨 Proxidize Low Balance Alert",
                        description=f"Remaining: **{remaining:.2f} GB** (threshold: {alerts['threshold_gb']:.2f} GB)",
                        color=CLR_ERROR,
                    )
                    await ch.send(embed=footer(e))
            elif remaining > alerts["threshold_gb"] and alerts.get("breached"):
                alerts["breached"] = False
            save_json("proxidize_alerts.json", alerts)
    except Exception:
        pass


@tasks.loop(minutes=5)
async def oxylabs_mobile_background():
    if not OXYLABS_MOBILE_ENABLED:
        return
    await asyncio.sleep(120)  # offset 2 min
    try:
        await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_mobile_jwt)
        if not data:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        history = load_json("oxylabs_mobile_history.json")
        gb_used = float(data.get("traffic", 0))
        history[today] = {"gb_used": gb_used}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        history = {k: v for k, v in history.items() if k >= cutoff}
        save_json("oxylabs_mobile_history.json", history)

        alerts = load_json("oxylabs_mobile_alerts.json")
        if "threshold_gb" in alerts:
            if gb_used >= alerts["threshold_gb"] and not alerts.get("breached"):
                alerts["breached"] = True
                ch = bot.get_channel(alerts["channel_id"])
                if ch:
                    e = discord.Embed(
                        title="🚨 Oxylabs Mobile Usage Alert",
                        description=f"Usage: **{gb_used:.2f} GB** (threshold: {alerts['threshold_gb']:.2f} GB)",
                        color=CLR_ERROR,
                    )
                    await ch.send(embed=footer(e))
            elif gb_used < alerts["threshold_gb"] and alerts.get("breached"):
                alerts["breached"] = False
            save_json("oxylabs_mobile_alerts.json", alerts)
    except Exception:
        pass


@tasks.loop(minutes=5)
async def oxylabs_resi_background():
    if not OXYLABS_RESI_ENABLED:
        return
    await asyncio.sleep(180)  # offset 3 min
    try:
        await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
        data = await oxylabs_get_stats(oxylabs_resi_jwt)
        if not data:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        history = load_json("oxylabs_resi_history.json")
        gb_used = float(data.get("traffic", 0))
        history[today] = {"gb_used": gb_used}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        history = {k: v for k, v in history.items() if k >= cutoff}
        save_json("oxylabs_resi_history.json", history)

        alerts = load_json("oxylabs_resi_alerts.json")
        if "threshold_gb" in alerts:
            if gb_used >= alerts["threshold_gb"] and not alerts.get("breached"):
                alerts["breached"] = True
                ch = bot.get_channel(alerts["channel_id"])
                if ch:
                    e = discord.Embed(
                        title="🚨 Oxylabs Residential Usage Alert",
                        description=f"Usage: **{gb_used:.2f} GB** (threshold: {alerts['threshold_gb']:.2f} GB)",
                        color=CLR_ERROR,
                    )
                    await ch.send(embed=footer(e))
            elif gb_used < alerts["threshold_gb"] and alerts.get("breached"):
                alerts["breached"] = False
            save_json("oxylabs_resi_alerts.json", alerts)
    except Exception:
        pass


@tasks.loop(minutes=50)
async def jwt_refresh_task():
    """Refresh all Oxylabs JWTs that are set."""
    try:
        if OXYLABS_MOBILE_ENABLED and http_session:
            await ensure_jwt(http_session, oxylabs_mobile_jwt, OXYLABS_MOBILE_USERNAME, OXYLABS_MOBILE_PASSWORD)
        if OXYLABS_RESI_ENABLED and http_session:
            await ensure_jwt(http_session, oxylabs_resi_jwt, OXYLABS_RESIDENTIAL_USERNAME, OXYLABS_RESIDENTIAL_PASSWORD)
    except Exception:
        pass


# Prevent background tasks from starting before bot is ready
@evomi_background.before_loop
async def before_evomi():
    await bot.wait_until_ready()

@proxidize_background.before_loop
async def before_proxidize():
    await bot.wait_until_ready()

@oxylabs_mobile_background.before_loop
async def before_oxy_mobile():
    await bot.wait_until_ready()

@oxylabs_resi_background.before_loop
async def before_oxy_resi():
    await bot.wait_until_ready()

@jwt_refresh_task.before_loop
async def before_jwt():
    await bot.wait_until_ready()


# ===================================================================
# RUN
# ===================================================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("⚠️  DISCORD_TOKEN is not set in .env — bot cannot start.")
        print("   Add your token to C:\\Users\\markp\\Desktop\\ProxyBot\\.env")
    else:
        bot.run(DISCORD_TOKEN)
