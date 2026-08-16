#!/usr/bin/env python3
"""
Rollcfluence Booking Hub — prototype

A tiny multi-tenant appointment booking system:
  - Businesses register at /register and get their own public booking link.
  - Their customers book appointments at /book/<slug>.
  - Every registration and every booking is logged to a private dashboard
    at /dashboard, and (optionally) pushed to you instantly via Telegram
    and/or email.

Zero external dependencies — Python standard library only, so it runs
anywhere with just `python3 app.py`. See README.md for configuration
(admin password, Telegram bot, email alerts) and for what to change
before using this with real client data.
"""

import html
import json
import os
import random
import re
import smtplib
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────────────────────── Configuration
# Override any of these with environment variables before running, e.g.:
#   ADMIN_KEY=something-only-you-know python3 app.py
PORT = int(os.environ.get("PORT", 8000))
# Your real public address once the site is online, e.g. https://rollcfluence.com
# Link-preview cards on WhatsApp/Instagram/Facebook only work when this is set,
# because those services fetch the image from the internet, not from your laptop.
SITE_URL = os.environ.get("SITE_URL", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "rollcfluence")  # CHANGE THIS before real use
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "booking_hub.db"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")

# ── Analytics knobs ──
# Hours to add to stored UTC timestamps when showing "what time of day".
# Bulgaria is +3 in summer, +2 in winter. Only affects display, never storage.
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", 3))
# One visitor can never write more than this many rows. Stops a bot or a stuck
# page from filling the database in a night.
MAX_EVENTS_PER_SESSION = int(os.environ.get("MAX_EVENTS_PER_SESSION", 300))
MAX_EVENTS_PER_REQUEST = 60
# Raw events older than this are deleted. Ninety days is plenty to show a
# client a trend, and keeps the free-tier disk from ever filling up.
EVENT_RETENTION_DAYS = int(os.environ.get("EVENT_RETENTION_DAYS", 90))

BRAND_PURPLE = "#4B1E73"
BRAND_PURPLE_LIGHT = "#6A2C8C"
BRAND_ACCENT = "#C9A0DC"
BRAND_BLUE = "#3E63DD"
BRAND_PINK = "#D6368F"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# ─────────────────────────────────────────── Database

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            contact_email TEXT NOT NULL,
            service_type TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            customer_contact TEXT NOT NULL,
            requested_date TEXT,
            requested_time TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (business_id) REFERENCES businesses (id)
        );

        /* One row per thing a visitor did on a client's page. Deliberately
           anonymous: no cookie, no IP address, no user agent. session_id is a
           random string the page invents on load and forgets when the tab
           closes — enough to group one visit together and nothing more. That
           is what keeps this outside GDPR's definition of personal data, so
           clients need no consent banner. Do not add an IP column later
           without understanding what it costs you. */
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            element TEXT,
            x REAL,
            y REAL,
            ms INTEGER,
            viewport_w INTEGER,
            viewport_h INTEGER,
            device TEXT,
            referrer TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (business_id) REFERENCES businesses (id)
        );
        CREATE INDEX IF NOT EXISTS ix_events_biz_time ON events (business_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_events_session ON events (session_id);
        """
    )
    conn.commit()
    conn.close()


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "business"
    conn = get_db()
    slug = base
    n = 1
    while conn.execute("SELECT 1 FROM businesses WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    conn.close()
    return slug


def get_or_create_business(name: str, email: str, service_type: str = "") -> dict:
    """Idempotent registration: if a business with this exact name already
    exists, return its existing slug instead of creating a duplicate. This
    lets a landing page safely call the API on every page load without
    creating a new row each time someone visits."""
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM businesses WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        conn.close()
        return {"slug": existing["slug"], "name": existing["name"], "created": False}
    slug = slugify(name)
    conn.execute(
        "INSERT INTO businesses (slug, name, contact_email, service_type, created_at) VALUES (?, ?, ?, ?, ?)",
        (slug, name, email, service_type, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return {"slug": slug, "name": name, "created": True}


# ─────────────────────────────────────────── Notifications

def notify(subject: str, message: str):
    """Best-effort push to Telegram and/or email. Never raises — a missing
    or wrong config must not take down registration/booking for the client."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"*{subject}*\n{message}",
                "parse_mode": "Markdown",
            }).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5)
        except Exception as e:
            print(f"[notify] Telegram send failed: {e}")

    if SMTP_HOST and SMTP_USER and SMTP_PASS and ALERT_EMAIL_TO:
        try:
            msg = MIMEText(message)
            msg["Subject"] = subject
            msg["From"] = SMTP_USER
            msg["To"] = ALERT_EMAIL_TO
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        except Exception as e:
            print(f"[notify] Email send failed: {e}")

    # Always log locally too, so nothing is ever silently lost.
    print(f"[notify] {subject} — {message}", flush=True)


# ─────────────────────────────────────────── HTML layout

def page(title: str, body: str, description: str = None, share_path: str = "/") -> bytes:
    desc = description or (
        "Give your customers a booking page they can use in seconds — "
        "and see every booking land in one place, the moment it happens."
    )
    # SITE_URL must be the real public address for link previews to work on
    # WhatsApp/Instagram/Facebook — those crawlers cannot read localhost.
    abs_share = SITE_URL.rstrip("/") + "/static/share.jpg" if SITE_URL else "/static/share.jpg"
    abs_url = SITE_URL.rstrip("/") + share_path if SITE_URL else share_path
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Rollcfluence</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="icon" type="image/png" href="/static/favicon.png">
<meta name="theme-color" content="#4B1E73">

<!-- Link preview card (WhatsApp, Instagram DM, Facebook, LinkedIn, iMessage) -->
<meta property="og:type" content="website">
<meta property="og:site_name" content="Rollcfluence">
<meta property="og:title" content="{html.escape(title)} · Rollcfluence">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:image" content="{html.escape(abs_share)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="{html.escape(abs_url)}">

<!-- Link preview card (X / Twitter) -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{html.escape(title)} · Rollcfluence">
<meta name="twitter:description" content="{html.escape(desc)}">
<meta name="twitter:image" content="{html.escape(abs_share)}">
<style>
  :root {{
    --purple: {BRAND_PURPLE};
    --purple-light: {BRAND_PURPLE_LIGHT};
    --accent: {BRAND_ACCENT};
    --blue: {BRAND_BLUE};
    --pink: {BRAND_PINK};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    margin: 0; background: #f7f7f9; color: #242424;
  }}
  header {{
    background: linear-gradient(120deg, var(--blue) 0%, var(--pink) 100%);
    color: white; padding: 22px 20px;
  }}
  .header-inner {{
    max-width: 760px; margin: 0 auto; display: flex; align-items: center; gap: 14px;
  }}
  header a {{ color: white; text-decoration: none; display: flex; align-items: center; gap: 14px; }}
  .logo-mark {{
    width: 44px; height: 44px; border-radius: 10px; background: white;
    display: flex; align-items: center; justify-content: center; overflow: hidden;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15); flex-shrink: 0;
  }}
  .logo-mark img {{ width: 100%; height: 100%; object-fit: cover; }}
  header h1 {{ margin: 0; font-size: 21px; letter-spacing: 0.3px; }}
  header p {{ margin: 3px 0 0; opacity: 0.92; font-size: 13px; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 28px 20px 60px; }}
  .hero-bg-wrap {{
    position: relative; border-radius: 20px; overflow: hidden;
    min-height: 560px; margin-bottom: 30px;
    background: #08060f;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 18px 54px rgba(30,18,70,0.28);
    perspective: 1000px;   /* gives the tilt real 3D depth */
  }}
  .hero-video {{
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: cover; z-index: 0; opacity: 0.55;
    transition: transform 0.5s cubic-bezier(.2,.7,.3,1);
  }}
  /* Ferrofluid sits above the video and screen-blends, so the glowing rims
     add light on top of the footage instead of hiding it. */
  .hero-ferro {{
    position: absolute; inset: 0; z-index: 1; pointer-events: auto;
    mix-blend-mode: screen; opacity: 0.95;
  }}
  /* ElasticMesh — the warping sheet used behind booking pages */
  .hero-mesh {{
    position: absolute; inset: 0; z-index: 1; touch-action: none;
  }}
  .hero-mesh canvas {{ display: block; width: 100%; height: 100%; }}

  /* ── DepthText ── */
  .depth-text {{ display: inline-block; perspective: 900px; perspective-origin: 50% 48%; isolation: isolate; }}
  .depth-stage {{
    position: relative; display: inline-grid; place-items: center;
    transform-style: preserve-3d; transform-origin: 50% 50%; will-change: transform;
  }}
  .depth-layer, .depth-face {{
    grid-area: 1 / 1; display: inline-block;
    font-size: clamp(2.6rem, 13vw, 4.4rem); font-weight: 900; line-height: 0.86;
    letter-spacing: -0.065em; white-space: nowrap; user-select: none;
    transform-style: preserve-3d; backface-visibility: hidden; text-rendering: geometricPrecision;
  }}
  .depth-layer {{
    position: absolute; inset: 0; z-index: 0;
    filter: saturate(0.95) brightness(0.92); pointer-events: none;
  }}
  .depth-face {{
    position: relative; z-index: 1; color: #f8fafc; transform: translateZ(0.6px);
    text-shadow: 0 22px 34px rgba(124,58,237,0.36), 0 4px 8px rgba(0,0,0,0.3);
  }}
  .who {{
    margin: 14px 0 6px; font-size: 21px; font-weight: 600;
    letter-spacing: -0.3px; color: #fff; text-shadow: 0 2px 14px rgba(0,0,0,0.45);
  }}
  /* Darkened + vignetted so light text reads cleanly on top */
  .hero-scrim {{
    position: absolute; inset: 0; z-index: 2; pointer-events: none;
    background:
      radial-gradient(ellipse at 50% 45%, rgba(8,5,18,0.05) 0%, rgba(8,5,18,0.66) 74%),
      linear-gradient(180deg, rgba(8,5,18,0.28) 0%, rgba(8,5,18,0.60) 100%);
  }}
  .hero {{
    position: relative; z-index: 3; text-align: center;
    padding: 8px 30px 12px; margin: 26px; max-width: 520px;
    transform-style: preserve-3d;
    transition: transform 0.35s cubic-bezier(.2,.7,.3,1);
    will-change: transform;
  }}
  .hero img {{
    width: 56px; height: 56px; border-radius: 14px; margin-bottom: 16px;
    box-shadow: 0 8px 26px rgba(0,0,0,0.45);
    transform: translateZ(60px);
    mix-blend-mode: screen;   /* lets the light logo backdrop melt into the dark hero */
  }}
  .hero h2 {{
    font-size: 40px; line-height: 1.08; margin: 0 0 14px;
    letter-spacing: -1.1px; font-weight: 700; color: #fff;
    transform: translateZ(46px);
    text-shadow: 0 2px 18px rgba(0,0,0,0.45), 0 1px 3px rgba(0,0,0,0.4);
  }}
  .hero h2 .grad {{
    background: linear-gradient(100deg, #7aa2ff 0%, #c77dff 45%, #ff5fa8 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
    text-shadow: none;
  }}
  .hero p {{
    color: rgba(255,255,255,0.86); font-size: 15px; line-height: 1.6;
    margin: 0 auto 22px; max-width: 380px;
    transform: translateZ(30px);
    text-shadow: 0 1px 10px rgba(0,0,0,0.5);
  }}
  .hero-cta {{
    display: inline-block; background: linear-gradient(120deg, #4f7cff, #ff4fa0);
    color: white; text-decoration: none; font-weight: 600; font-size: 15px;
    padding: 13px 30px; border-radius: 10px;
    box-shadow: 0 8px 26px rgba(120,60,220,0.45);
    transform: translateZ(56px);
    transition: filter 0.15s ease, box-shadow 0.2s ease;
  }}
  .hero-cta:hover {{ filter: brightness(1.1); box-shadow: 0 12px 34px rgba(140,70,240,0.6); }}
  .hero-hint {{
    position: absolute; bottom: 12px; left: 0; right: 0; z-index: 3;
    text-align: center; font-size: 11px; letter-spacing: 0.4px;
    color: rgba(255,255,255,0.4); pointer-events: none;
  }}

  /* ── Seamless in-hero form ─────────────────────────────────────────
     Instead of a solid card pasted onto the background, the inputs are
     translucent panes that let the fluid show through — so the text,
     the form and the background read as one surface. */
  .hero-curved {{ margin-top: 22px; transform: translateZ(38px); }}
  /* Before curved-input.js enhances it, this is a plain usable form —
     so it still works if the script fails or JS is disabled. */
  .hero-curved input {{
    width: 100%; padding: 13px 15px; font-size: 15px; font-family: inherit; color: #fff;
    background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.18);
    border-radius: 12px;
  }}
  .hero-curved input::placeholder {{ color: rgba(255,255,255,0.5); }}
  .hero-curved button {{
    margin-top: 10px; width: 100%; padding: 13px; border: none; cursor: pointer;
    font-size: 15px; font-weight: 600; font-family: inherit; color: #fff; border-radius: 12px;
    background: linear-gradient(120deg, #4f7cff, #ff4fa0);
  }}
  /* Once enhanced, the SVG replaces the native controls entirely. */
  .ci-host input {{ border: none !important; border-radius: 0 !important; }}

  /* ── Booking page: avatar, day pills, time slots ────────────────── */
  .biz-avatar {{
    width: 64px; height: 64px; border-radius: 20px; margin: 0 auto 14px;
    display: flex; align-items: center; justify-content: center;
    font-size: 26px; font-weight: 700; color: #fff; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #4f7cff, #ff4fa0);
    box-shadow: 0 10px 30px rgba(120,60,220,0.4);
    transform: translateZ(56px);
  }}
  .step-label {{
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase;
    color: rgba(255,255,255,0.55); margin: 20px 0 10px; text-align: left;
  }}
  .step-label span.n {{
    width: 19px; height: 19px; border-radius: 50%; flex-shrink: 0;
    background: rgba(255,255,255,0.14); color: #fff;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 11px; letter-spacing: 0;
  }}
  .step-label.done span.n {{ background: linear-gradient(135deg,#4f7cff,#ff4fa0); }}

  .day-scroll {{
    display: flex; gap: 8px; overflow-x: auto; padding: 2px 2px 6px;
    scrollbar-width: none; -ms-overflow-style: none;
  }}
  .day-scroll::-webkit-scrollbar {{ display: none; }}
  .day-pill {{
    flex: 0 0 auto; min-width: 60px; padding: 10px 6px; border-radius: 14px;
    background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.14);
    color: #fff; cursor: pointer; text-align: center; font-family: inherit;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease;
  }}
  .day-pill:hover {{ background: rgba(255,255,255,0.13); }}
  .day-pill .dow {{ display: block; font-size: 10.5px; opacity: 0.66; letter-spacing: 0.5px; text-transform: uppercase; }}
  .day-pill .dnum {{ display: block; font-size: 18px; font-weight: 600; margin-top: 3px; }}
  .day-pill.sel {{
    background: linear-gradient(135deg, rgba(79,124,255,0.9), rgba(255,79,160,0.9));
    border-color: rgba(255,255,255,0.5); transform: translateY(-2px);
    box-shadow: 0 8px 22px rgba(120,60,220,0.4);
  }}

  .slot-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }}
  .slot {{
    padding: 11px 4px; border-radius: 11px; font-size: 13.5px; font-weight: 500;
    background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.14);
    color: #fff; cursor: pointer; font-family: inherit;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease;
  }}
  .slot:hover {{ background: rgba(255,255,255,0.13); }}
  .slot.sel {{
    background: linear-gradient(135deg, rgba(79,124,255,0.9), rgba(255,79,160,0.9));
    border-color: rgba(255,255,255,0.5); transform: translateY(-1px);
    box-shadow: 0 6px 18px rgba(120,60,220,0.38);
  }}
  @media (max-width: 420px) {{ .slot-grid {{ grid-template-columns: repeat(3, 1fr); }} }}

  .hero-cta:disabled {{ opacity: 0.4; cursor: not-allowed; box-shadow: none; filter: none; }}

  /* Staggered entrance so the page assembles itself rather than snapping in */
  .rise {{ opacity: 0; transform: translateY(14px); animation: rise 0.65s cubic-bezier(.2,.7,.3,1) forwards; }}
  @keyframes rise {{ to {{ opacity: 1; transform: translateY(0); }} }}
  @media (prefers-reduced-motion: reduce) {{ .rise {{ animation: none; opacity: 1; transform: none; }} }}

  .done-check {{
    width: 62px; height: 62px; border-radius: 50%; margin: 0 auto 16px;
    background: linear-gradient(135deg, #35d07f, #16a765);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 10px 30px rgba(30,180,110,0.4);
    animation: pop 0.5s cubic-bezier(.2,1.4,.4,1) forwards;
  }}
  @keyframes pop {{ from {{ transform: scale(0.4); opacity: 0; }} to {{ transform: scale(1); opacity: 1; }} }}

  .hero-form {{
    margin-top: 20px; transform: translateZ(34px);
    display: flex; flex-direction: column; gap: 10px;
  }}
  .hero-form .row {{ display: flex; gap: 10px; }}
  .hero-form .row > * {{ flex: 1; min-width: 0; }}
  .glass-field {{
    width: 100%; padding: 13px 15px; font-size: 15px; font-family: inherit;
    color: #fff; background: rgba(255,255,255,0.07);
    border: 1px solid rgba(255,255,255,0.16); border-radius: 12px;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    transition: background 0.2s ease, border-color 0.2s ease;
  }}
  .glass-field::placeholder {{ color: rgba(255,255,255,0.5); }}
  .glass-field:focus {{
    outline: none; background: rgba(255,255,255,0.12);
    border-color: rgba(255,255,255,0.4);
  }}
  /* date/time inputs render their icons dark by default — invert on dark glass */
  .glass-field[type="date"], .glass-field[type="time"] {{ color-scheme: dark; }}

  /* ── Specular edge highlight (see static/specular.js) ──────────────
     --spec-angle points the streak at the cursor; --spec-on fades the
     whole effect in as the cursor gets close. Both are set from JS. */
  .spec {{
    --spec-angle: 0deg; --spec-on: 0;
    position: relative; isolation: isolate;
  }}
  .spec::after {{
    content: ""; position: absolute; inset: 0; border-radius: inherit;
    padding: 1px; pointer-events: none; opacity: var(--spec-on);
    background: conic-gradient(from var(--spec-angle),
      transparent 0deg, rgba(255,255,255,0.95) 8deg,
      transparent 26deg, transparent 180deg,
      rgba(255,255,255,0.65) 188deg, transparent 206deg, transparent 360deg);
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude;
    transition: opacity 0.25s ease;
  }}
  @media (prefers-reduced-motion: reduce) {{
    .hero-video {{ display: none; }}
    .hero, .hero-video {{ transition: none !important; transform: none !important; }}
  }}
  @media (max-width: 560px) {{
    .hero-bg-wrap {{ min-height: 400px; }}
    .hero {{ margin: 16px; padding: 6px 14px 10px; }}
    .hero h2 {{ font-size: 28px; letter-spacing: -0.6px; }}
    .hero p {{ font-size: 14px; }}
    .hero-hint {{ display: none; }}
  }}
  .card {{
    background: white; border: 1px solid #eee; border-radius: 10px;
    padding: 20px 22px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  h2 {{ color: var(--purple-light); font-size: 18px; margin-top: 0; }}
  label {{ display: block; font-size: 13px; font-weight: 600; margin: 14px 0 5px; color: #444; }}
  input, select, textarea {{
    width: 100%; padding: 9px 10px; border: 1px solid #ddd; border-radius: 6px;
    font-size: 14px; font-family: inherit;
  }}
  input:focus, select:focus, textarea:focus {{
    outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(62,99,221,0.12);
  }}
  textarea {{ min-height: 70px; }}
  button {{
    margin-top: 18px; background: linear-gradient(120deg, var(--blue), var(--pink));
    color: white; border: none;
    padding: 11px 22px; border-radius: 7px; font-size: 14px; font-weight: 600;
    cursor: pointer; box-shadow: 0 2px 10px rgba(90,60,180,0.2);
  }}
  button:hover {{ filter: brightness(1.06); }}
  a.link-btn {{
    display: inline-block; margin-top: 10px; color: var(--blue);
    font-weight: 600; text-decoration: none;
  }}
  a.link-btn:hover {{ color: var(--pink); }}
  .stats {{ display: flex; gap: 14px; flex-wrap: wrap; }}
  .stat {{
    flex: 1; min-width: 140px; background: white; border: 1px solid #eee;
    border-radius: 10px; padding: 16px; text-align: center;
  }}
  .stat .n {{ font-size: 26px; font-weight: 700; background: linear-gradient(120deg, var(--blue), var(--pink)); -webkit-background-clip: text; background-clip: text; color: transparent; }}
  .stat .l {{ font-size: 12px; color: #777; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; background: #2b2b2b; color: white; padding: 8px 10px; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #eee; }}
  tr:nth-child(even) td {{ background: #f7f7f9; }}
  .muted {{ color: #888; font-size: 13px; }}
  .success {{ background: #eef8f0; border: 1px solid #cdeed6; padding: 16px; border-radius: 8px; }}
  .booking-link {{ font-family: monospace; background: #f2f2f2; padding: 8px 10px; border-radius: 6px; word-break: break-all; }}
  code {{ background: #f2f2f2; padding: 2px 5px; border-radius: 4px; }}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <a href="/">
      <span class="logo-mark"><img src="/static/logo.jpg" alt="Rollcfluence"></span>
      <span>
        <h1>Rollcfluence</h1>
        <p>Appointment booking, with every lead flowing to one place.</p>
      </span>
    </a>
  </div>
</header>
<main>
{body}
</main>
<script src="/static/ferrofluid-bg.js"></script>
<script src="/static/elastic-mesh.js"></script>
<script src="/static/depth-text.js"></script>
<script src="/static/specular.js"></script>
<script src="/static/curved-input.js"></script>
<script>
(function () {{
  // 3D tilt: any .hero inside a .hero-bg-wrap leans toward the cursor, with
  // its children at different translateZ depths so they part like real layers.
  var wrap = document.getElementById('heroTilt');
  if (!wrap) return;
  var card = wrap.querySelector('.hero');
  var video = wrap.querySelector('.hero-video');
  if (!card) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var tx = 0, ty = 0, cx = 0, cy = 0, raf = null;
  function loop() {{
    cx += (tx - cx) * 0.09;
    cy += (ty - cy) * 0.09;
    card.style.transform =
      'rotateY(' + (cx * 11).toFixed(2) + 'deg) rotateX(' + (-cy * 9).toFixed(2) + 'deg)';
    if (video) {{
      video.style.transform =
        'scale(1.06) translate(' + (-cx * 12).toFixed(1) + 'px,' + (-cy * 12).toFixed(1) + 'px)';
    }}
    if (Math.abs(tx - cx) > 0.001 || Math.abs(ty - cy) > 0.001) raf = requestAnimationFrame(loop);
    else raf = null;
  }}
  function kick() {{ if (!raf) raf = requestAnimationFrame(loop); }}

  wrap.addEventListener('pointermove', function (e) {{
    // Don't tilt while someone is aiming at a form field — a moving target
    // is genuinely annoying to tap, especially on a phone.
    if (e.target.closest('input, textarea, button, a')) {{ tx = 0; ty = 0; kick(); return; }}
    var r = wrap.getBoundingClientRect();
    tx = ((e.clientX - r.left) / r.width - 0.5) * 2;
    ty = ((e.clientY - r.top) / r.height - 0.5) * 2;
    kick();
  }});
  wrap.addEventListener('pointerleave', function () {{ tx = 0; ty = 0; kick(); }});
}})();
</script>
</body>
</html>"""
    return html_doc.encode("utf-8")


def redirect(location: str):
    return 303, {"Location": location}, b""


# ─────────────────────────────────────────── Route handlers

def route_home(handler):
    body = """
    <div class="hero-bg-wrap" id="heroTilt">
      <video class="hero-video" autoplay loop muted playsinline poster="/static/hero-poster.jpg">
        <source src="/static/hero.webm" type="video/webm">
        <source src="/static/hero.mp4" type="video/mp4">
      </video>
      <div class="hero-ferro" data-ferrofluid
           data-colors="#7aa2ff,#c77dff,#ff5fa8"
           data-speed="0.4" data-scale="1.7" data-glow="2.1"
           data-rim-width="0.22" data-sharpness="2.6" data-shimmer="1.4"
           data-flow-direction="down" data-mouse-radius="0.3"></div>
      <div class="hero-scrim"></div>
      <div class="hero">
        <img src="/static/logo.jpg" alt="Rollcfluence">
        <h2>Never miss<br><span class="grad">another lead.</span></h2>
        <p>Give your customers a booking page they can use in seconds — and see every booking land in one place, the moment it happens.</p>
        <form class="hero-curved" data-curved method="get" action="/register"
              data-bend="24" data-height="60" data-button-color="#4f7cff">
          <input name="name" placeholder="Your business name" maxlength="120" aria-label="Your business name">
          <button type="submit">Get my link</button>
        </form>
      </div>
      <div class="hero-hint">move your cursor</div>
    </div>
    <div class="card">
      <h2>For businesses</h2>
      <p class="muted">Register your business and get a shareable booking link your customers can use.</p>
      <a class="link-btn" href="/register">Register your business &rarr;</a>
    </div>
    <div class="card">
      <h2>Rollcfluence dashboard</h2>
      <p class="muted">Private view of every business and every booking. Requires an access key.</p>
      <a class="link-btn" href="/dashboard">Go to dashboard &rarr;</a>
    </div>
    """
    return page("Home", body)


def route_register_form(handler, error=None, prefill_name=None):
    err_html = f'<p style="color:#b00020;">{html.escape(error)}</p>' if error else ""
    # The hero's curved input sends the business name here via ?name=…, so
    # someone who started typing on the home page doesn't have to retype it.
    prefill = html.escape(prefill_name or "")
    body = f"""
    <div class="card">
      <h2>Register your business</h2>
      {err_html}
      <form method="post" action="/register">
        <label>Business name</label>
        <input name="name" required maxlength="120" value="{prefill}">
        <label>Contact email</label>
        <input name="email" type="email" required maxlength="160">
        <label>Service type</label>
        <input name="service_type" maxlength="120" placeholder="e.g. roofing, dental, driving school">
        <button type="submit">Create my booking link</button>
      </form>
    </div>
    """
    return page("Register", body)


def route_register_submit(handler, fields):
    name = fields.get("name", "").strip()
    email = fields.get("email", "").strip()
    service_type = fields.get("service_type", "").strip()

    if not name or not email:
        return route_register_form(handler, error="Business name and contact email are required.")

    slug = slugify(name)
    conn = get_db()
    conn.execute(
        "INSERT INTO businesses (slug, name, contact_email, service_type, created_at) VALUES (?, ?, ?, ?, ?)",
        (slug, name, email, service_type, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()

    notify(
        "New business registered",
        f"{name} ({email}) — service: {service_type or 'n/a'} — booking link: /book/{slug}",
    )

    booking_url = f"/book/{slug}"
    body = f"""
    <div class="card success">
      <h2>You're set up</h2>
      <p>Share this link with your customers so they can book appointments directly:</p>
      <p class="booking-link">{html.escape(booking_url)}</p>
      <a class="link-btn" href="{html.escape(booking_url)}">Preview your booking page &rarr;</a>
    </div>
    """
    return page("Registered", body)


def route_booking_form(handler, slug, error=None):
    conn = get_db()
    biz = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    if not biz:
        return page("Not found", '<div class="card"><h2>No such business</h2><p class="muted">This booking link does not exist.</p></div>'), 404

    err_html = (
        f'<p style="color:#ffb4b4;text-shadow:0 1px 8px rgba(0,0,0,.5);">{html.escape(error)}</p>'
        if error else ""
    )
    # The booking page is what a client's own customers see, so it gets the
    # full treatment: fluid background, and the heading + fields as one
    # continuous surface rather than a form boxed inside a card.
    initial = html.escape(biz["name"].strip()[:1].upper() or "?")
    body = f"""
    <div class="hero-bg-wrap" id="heroTilt">
      <div class="hero-mesh" data-elastic-mesh
           data-color1="#4f7cff" data-color2="#ff4fa0"
           data-grid-density="18" data-grid-opacity="0.26" data-shading="0.75"
           data-tilt="12" data-pull="0.55" data-wobble="5.5" data-border-radius="26"></div>
      <div class="hero-scrim"></div>
      <div class="hero">
        <div class="biz-avatar rise" style="animation-delay:.05s">{initial}</div>
        <span class="rise" data-depth-text="Book" data-face="#f8fafc" data-deep="#7c3aed"
              data-layers="28" data-depth="2.4" data-tilt="7.5" style="animation-delay:.12s"></span>
        <h2 class="who rise" style="animation-delay:.18s">{html.escape(biz["name"])}</h2>
        <p class="rise" style="animation-delay:.24s">Pick a day and a time — it takes about thirty seconds.</p>
        {err_html}
        <form method="post" action="/book/{html.escape(slug)}" class="hero-form" id="bookForm">
          <div class="rise" style="animation-delay:.28s">
            <div class="step-label" id="lbl1"><span class="n">1</span> Choose a day</div>
            <div class="day-scroll" id="dayScroll"></div>
          </div>
          <div class="rise" style="animation-delay:.36s">
            <div class="step-label" id="lbl2"><span class="n">2</span> Choose a time</div>
            <div class="slot-grid" id="slotGrid"></div>
          </div>
          <div class="rise" style="animation-delay:.44s">
            <div class="step-label" id="lbl3"><span class="n">3</span> Your details</div>
            <input class="glass-field spec" name="customer_name" id="cname" required maxlength="120" placeholder="Your name">
            <input class="glass-field spec" name="customer_contact" id="ccontact" required maxlength="160" placeholder="Phone or email" style="margin-top:10px;">
            <input class="glass-field spec" name="note" maxlength="500" placeholder="Anything we should know? (optional)" style="margin-top:10px;">
          </div>
          <input type="hidden" name="requested_date" id="reqDate">
          <input type="hidden" name="requested_time" id="reqTime">
          <button class="hero-cta spec rise" id="submitBtn" type="submit" disabled
                  style="animation-delay:.52s;border:none;width:100%;margin-top:14px;">Request appointment</button>
        </form>
      </div>
      <script>
      (function () {{
        // Days and slots are generated in the visitor's own timezone, so
        // "Tue 12" always means their Tuesday, not the server's.
        var DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
        var dayScroll = document.getElementById('dayScroll');
        var slotGrid  = document.getElementById('slotGrid');
        var reqDate   = document.getElementById('reqDate');
        var reqTime   = document.getElementById('reqTime');
        var btn       = document.getElementById('submitBtn');
        var cname     = document.getElementById('cname');
        var ccontact  = document.getElementById('ccontact');
        if (!dayScroll) return;

        function pad(n) {{ return (n < 10 ? '0' : '') + n; }}

        // Next 14 days, skipping today once it's late enough that same-day
        // booking is unrealistic for most businesses.
        var start = new Date();
        if (start.getHours() >= 17) start.setDate(start.getDate() + 1);
        for (var i = 0; i < 14; i++) {{
          var d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'day-pill';
          b.dataset.date = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
          b.innerHTML = '<span class="dow">' + DOW[d.getDay()] + '</span><span class="dnum">' + d.getDate() + '</span>';
          b.addEventListener('click', function () {{
            var prev = dayScroll.querySelector('.sel');
            if (prev) prev.classList.remove('sel');
            this.classList.add('sel');
            reqDate.value = this.dataset.date;
            document.getElementById('lbl1').classList.add('done');
            check();
          }});
          dayScroll.appendChild(b);
        }}

        // 09:00–18:00, every 30 minutes.
        for (var h = 9; h <= 17; h++) {{
          for (var m = 0; m < 60; m += 30) {{
            var t = pad(h) + ':' + pad(m);
            var s = document.createElement('button');
            s.type = 'button';
            s.className = 'slot';
            s.dataset.time = t;
            s.textContent = t;
            s.addEventListener('click', function () {{
              var prev = slotGrid.querySelector('.sel');
              if (prev) prev.classList.remove('sel');
              this.classList.add('sel');
              reqTime.value = this.dataset.time;
              document.getElementById('lbl2').classList.add('done');
              check();
            }});
            slotGrid.appendChild(s);
          }}
        }}

        function check() {{
          var ok = reqDate.value && reqTime.value &&
                   cname.value.trim() && ccontact.value.trim();
          btn.disabled = !ok;
          document.getElementById('lbl3').classList.toggle('done',
            !!(cname.value.trim() && ccontact.value.trim()));
        }}
        cname.addEventListener('input', check);
        ccontact.addEventListener('input', check);

        if (window.__specBind) window.__specBind();
      }})();
      </script>
    </div>
    """
    return page(
        f"Book with {biz['name']}",
        body,
        description=f"Book an appointment with {biz['name']} online in under a minute.",
        share_path=f"/book/{slug}",
    ), 200


def route_booking_submit(handler, slug, fields):
    conn = get_db()
    biz = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
    if not biz:
        conn.close()
        return page("Not found", '<div class="card"><h2>No such business</h2></div>'), 404

    customer_name = fields.get("customer_name", "").strip()
    customer_contact = fields.get("customer_contact", "").strip()
    requested_date = fields.get("requested_date", "").strip()
    requested_time = fields.get("requested_time", "").strip()
    note = fields.get("note", "").strip()

    if not customer_name or not customer_contact:
        conn.close()
        result = route_booking_form(handler, slug, error="Name and phone/email are required.")
        return result

    conn.execute(
        """INSERT INTO bookings
           (business_id, customer_name, customer_contact, requested_date, requested_time, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (biz["id"], customer_name, customer_contact, requested_date, requested_time, note,
         datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()

    notify(
        f"New booking — {biz['name']}",
        f"{customer_name} ({customer_contact}) requested {requested_date or 'no date'} {requested_time or ''}\nNote: {note or '—'}",
    )

    # Confirmation gets the same fluid treatment — the last thing a customer
    # sees shouldn't drop back to a plain white box.
    when = ""
    if requested_date:
        when = requested_date + (f" at {requested_time}" if requested_time else "")
    when_html = (
        f'<p class="rise" style="animation-delay:.24s"><strong>{html.escape(when)}</strong></p>'
        if when else ""
    )
    body = f"""
    <div class="hero-bg-wrap" id="heroTilt">
      <div class="hero-ferro" data-ferrofluid
           data-colors="#7aa2ff,#c77dff,#ff5fa8"
           data-speed="0.3" data-scale="1.7" data-glow="2.0"
           data-rim-width="0.22" data-sharpness="2.6" data-shimmer="1.4"
           data-flow-direction="down" data-mouse-radius="0.3"></div>
      <div class="hero-scrim"></div>
      <div class="hero">
        <div class="done-check">
          <svg width="30" height="30" viewBox="0 0 24 24" fill="none"
               stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 6L9 17l-5-5"/>
          </svg>
        </div>
        <h2 class="rise" style="animation-delay:.1s">Request<br><span class="grad">received.</span></h2>
        {when_html}
        <p class="rise" style="animation-delay:.32s">{html.escape(biz['name'])} will confirm your appointment shortly.</p>
      </div>
    </div>
    """
    return page("Booked", body), 200


# ─────────────────────────────────────────── JSON API
# These exist so an *external* page — a branded landing page hosted anywhere
# else on the internet, like Vercel — can register a business and take
# bookings straight from the visitor's own browser via fetch(), without a
# human filling in the HTML form on this site by hand. CORS headers (added
# in the Handler below) are what make that cross-site fetch() call legal;
# without them, browsers block it silently for security reasons.

def api_register(fields: dict) -> bytes:
    name = (fields.get("name") or "").strip()
    email = (fields.get("email") or "").strip()
    service_type = (fields.get("service_type") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"}).encode()
    result = get_or_create_business(name, email, service_type)
    if result["created"]:
        notify("New business registered (via API)", f"{name} ({email or 'no email'}) — slug: {result['slug']}")
    return json.dumps(result).encode()


def api_book(slug: str, fields: dict) -> tuple:
    conn = get_db()
    biz = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
    if not biz:
        conn.close()
        return json.dumps({"error": "no such business"}).encode(), 404

    customer_name = (fields.get("customer_name") or "").strip()
    customer_contact = (fields.get("customer_contact") or "").strip()
    requested_date = (fields.get("requested_date") or "").strip()
    requested_time = (fields.get("requested_time") or "").strip()
    note = (fields.get("note") or "").strip()

    if not customer_name or not customer_contact:
        conn.close()
        return json.dumps({"error": "customer_name and customer_contact are required"}).encode(), 400

    conn.execute(
        """INSERT INTO bookings
           (business_id, customer_name, customer_contact, requested_date, requested_time, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (biz["id"], customer_name, customer_contact, requested_date, requested_time, note,
         datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()

    notify(
        f"New booking (via API) — {biz['name']}",
        f"{customer_name} ({customer_contact}) requested {requested_date or 'no date'} {requested_time or ''}\nNote: {note or '—'}",
    )
    return json.dumps({"ok": True, "business": biz["name"]}).encode(), 200


# ─────────────────────────────────────────── Analytics: collecting
# The page a client's customer sees fires small batches of events at
# /api/track/<slug>. Everything here is written to be cheap and to fail
# quietly: analytics must never be able to break a booking.

FUNNEL_STAGES = [
    ("view", "Opened the page"),
    ("click", "Clicked something"),
    ("focus", "Started the form"),
    ("submit", "Pressed Book"),
    ("success", "Booking confirmed"),
]
ALLOWED_EVENT_TYPES = {"view", "click", "scroll", "focus", "submit", "success", "exit"}


def _device_from_width(w) -> str:
    """Device class from viewport width alone — no user agent, so nothing
    here can be used to fingerprint a person."""
    try:
        w = int(w or 0)
    except (TypeError, ValueError):
        return "unknown"
    if w <= 0:
        return "unknown"
    if w < 700:
        return "mobile"
    if w < 1100:
        return "tablet"
    return "desktop"


def _referrer_host(ref: str) -> str:
    """Keep only where they came from, never the full URL (which can carry
    search terms and personal identifiers)."""
    ref = (ref or "").strip()
    if not ref:
        return "direct"
    try:
        host = urllib.parse.urlparse(ref).netloc.lower()
    except ValueError:
        return "unknown"
    if not host:
        return "direct"
    if host.startswith("www."):
        host = host[4:]
    if "instagram" in host:
        return "instagram"
    if "facebook" in host or host == "m.me":
        return "facebook"
    if "google" in host:
        return "google"
    if "tiktok" in host:
        return "tiktok"
    return host[:60]


def prune_events():
    """Delete raw events past the retention window. Called on a small random
    share of writes, so it costs nothing on average and needs no scheduler."""
    cutoff = (datetime.utcnow() - timedelta(days=EVENT_RETENTION_DAYS)).isoformat(timespec="seconds")
    try:
        conn = get_db()
        conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[analytics] prune failed: {e}", flush=True)


def api_track(slug: str, payload: dict) -> tuple:
    """Swallow a batch of events. Always answers fast and never raises."""
    try:
        conn = get_db()
        biz = conn.execute("SELECT id FROM businesses WHERE slug = ?", (slug,)).fetchone()
        if not biz:
            conn.close()
            return json.dumps({"error": "no such business"}).encode(), 404

        session_id = str(payload.get("sid") or "")[:40]
        events = payload.get("events") or []
        if not session_id or not isinstance(events, list):
            conn.close()
            return json.dumps({"ok": True, "stored": 0}).encode(), 200

        already = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()["c"]
        room = max(0, MAX_EVENTS_PER_SESSION - already)
        if room == 0:
            conn.close()
            return json.dumps({"ok": True, "stored": 0}).encode(), 200

        device = _device_from_width(payload.get("w"))
        referrer = _referrer_host(payload.get("ref"))
        vw, vh = payload.get("w"), payload.get("h")
        now = datetime.utcnow().isoformat(timespec="seconds")

        rows = []
        for ev in events[:MAX_EVENTS_PER_REQUEST][:room]:
            if not isinstance(ev, dict):
                continue
            etype = str(ev.get("type") or "")[:20]
            if etype not in ALLOWED_EVENT_TYPES:
                continue

            def num(v, lo, hi):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    return None
                return max(lo, min(hi, v))

            rows.append((
                biz["id"], session_id, etype,
                (str(ev.get("el") or "")[:40] or None),
                num(ev.get("x"), 0, 100), num(ev.get("y"), 0, 100),
                int(num(ev.get("ms"), 0, 86_400_000) or 0),
                int(num(vw, 0, 20000) or 0), int(num(vh, 0, 20000) or 0),
                device, referrer, now,
            ))

        if rows:
            conn.executemany(
                """INSERT INTO events
                   (business_id, session_id, type, element, x, y, ms,
                    viewport_w, viewport_h, device, referrer, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
        conn.close()

        if random.random() < 0.005:   # ~1 in 200 writes does the housekeeping
            prune_events()

        return json.dumps({"ok": True, "stored": len(rows)}).encode(), 200
    except Exception as e:
        print(f"[analytics] track failed: {e}", flush=True)
        return json.dumps({"ok": False}).encode(), 200


# ─────────────────────────────────────────── Analytics: reading

def _pct(part, whole) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def stats_for(slug: str, days: int = 30) -> dict:
    """Everything the dashboard needs, in one pass over the events table."""
    conn = get_db()
    biz = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
    if not biz:
        conn.close()
        return {"error": "no such business"}

    bid = biz["id"]
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    where = "business_id = ? AND created_at >= ?"
    args = (bid, cutoff)
    tz = f"{TZ_OFFSET_HOURS:+d} hours"

    # ── How many distinct visits reached each stage
    per_type = {
        r["type"]: r["n"]
        for r in conn.execute(
            f"SELECT type, COUNT(DISTINCT session_id) AS n FROM events WHERE {where} GROUP BY type",
            args,
        )
    }
    views = per_type.get("view", 0)

    funnel, prev = [], None
    for key, label in FUNNEL_STAGES:
        n = per_type.get(key, 0)
        funnel.append({
            "key": key,
            "label": label,
            "sessions": n,
            "pct_of_views": _pct(n, views),
            "pct_of_prev": 100.0 if prev is None else _pct(n, prev),
            "lost": 0 if prev is None else max(0, prev - n),
        })
        prev = n

    # ── When: hour of day and day of week, in local time
    by_hour = [{"hour": f"{h:02d}", "views": 0, "bookings": 0} for h in range(24)]
    for r in conn.execute(
        f"""SELECT CAST(strftime('%H', created_at, '{tz}') AS INTEGER) AS h, type,
                   COUNT(DISTINCT session_id) AS n
            FROM events WHERE {where} AND type IN ('view','success')
            GROUP BY h, type""", args):
        slot = by_hour[r["h"]]
        slot["bookings" if r["type"] == "success" else "views"] = r["n"]

    names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    by_weekday = [{"day": n, "views": 0, "bookings": 0} for n in names]
    for r in conn.execute(
        f"""SELECT CAST(strftime('%w', created_at, '{tz}') AS INTEGER) AS d, type,
                   COUNT(DISTINCT session_id) AS n
            FROM events WHERE {where} AND type IN ('view','success')
            GROUP BY d, type""", args):
        slot = by_weekday[r["d"]]
        slot["bookings" if r["type"] == "success" else "views"] = r["n"]

    by_day = {}
    for r in conn.execute(
        f"""SELECT date(created_at, '{tz}') AS d, type, COUNT(DISTINCT session_id) AS n
            FROM events WHERE {where} AND type IN ('view','success')
            GROUP BY d, type ORDER BY d""", args):
        slot = by_day.setdefault(r["d"], {"date": r["d"], "views": 0, "bookings": 0})
        slot["bookings" if r["type"] == "success" else "views"] = r["n"]

    # ── Which form field they reached, in the order the form asks for it
    fields = [
        {"element": r["element"], "reached": r["n"], "order": r["first_ms"]}
        for r in conn.execute(
            f"""SELECT element, COUNT(DISTINCT session_id) AS n, MIN(ms) AS first_ms
                FROM events WHERE {where} AND type = 'focus' AND element IS NOT NULL
                GROUP BY element ORDER BY first_ms""", args)
    ]
    focus_sessions = per_type.get("focus", 0)
    for f in fields:
        f["pct_of_starters"] = _pct(f["reached"], focus_sessions)

    # ── The money question: for visits that started the form and never booked,
    #    which field were they last touching when they gave up?
    abandon = [
        {"element": r["element"], "sessions": r["n"]}
        for r in conn.execute(
            f"""SELECT e.element, COUNT(*) AS n
                FROM events e
                JOIN (SELECT session_id, MAX(ms) AS m FROM events
                      WHERE {where} AND type = 'focus' GROUP BY session_id) last
                  ON last.session_id = e.session_id AND last.m = e.ms
                WHERE e.business_id = ? AND e.type = 'focus' AND e.element IS NOT NULL
                  AND e.session_id NOT IN (
                      SELECT session_id FROM events WHERE {where} AND type = 'success')
                GROUP BY e.element ORDER BY n DESC""",
            args + (bid,) + args)
    ]
    abandoned_total = sum(a["sessions"] for a in abandon)
    for a in abandon:
        a["pct"] = _pct(a["sessions"], abandoned_total)

    # ── Where they clicked (capped so the dashboard stays fast)
    clicks = [
        {"x": r["x"], "y": r["y"], "el": r["element"] or "", "device": r["device"]}
        for r in conn.execute(
            f"""SELECT x, y, element, device FROM events
                WHERE {where} AND type = 'click' AND x IS NOT NULL
                ORDER BY id DESC LIMIT 3000""", args)
    ]
    top_elements = [
        {"element": r["element"] or "(background)", "clicks": r["n"],
         "sessions": r["s"]}
        for r in conn.execute(
            f"""SELECT element, COUNT(*) AS n, COUNT(DISTINCT session_id) AS s
                FROM events WHERE {where} AND type = 'click'
                GROUP BY element ORDER BY n DESC LIMIT 12""", args)
    ]

    # ── Who and where from
    devices = [
        {"device": r["device"], "sessions": r["n"]}
        for r in conn.execute(
            f"""SELECT device, COUNT(DISTINCT session_id) AS n FROM events
                WHERE {where} AND type = 'view' GROUP BY device ORDER BY n DESC""", args)
    ]
    for d in devices:
        d["pct"] = _pct(d["sessions"], views)
    referrers = [
        {"source": r["referrer"], "sessions": r["n"]}
        for r in conn.execute(
            f"""SELECT referrer, COUNT(DISTINCT session_id) AS n FROM events
                WHERE {where} AND type = 'view' GROUP BY referrer ORDER BY n DESC LIMIT 10""", args)
    ]
    for s in referrers:
        s["pct"] = _pct(s["sessions"], views)

    # ── Speed and depth
    secs = [r["ms"] / 1000.0 for r in conn.execute(
        f"SELECT ms FROM events WHERE {where} AND type = 'success'", args) if r["ms"]]
    dwell = [r["m"] / 1000.0 for r in conn.execute(
        f"SELECT MAX(ms) AS m FROM events WHERE {where} GROUP BY session_id", args) if r["m"]]
    scroll = _median([r["y"] for r in conn.execute(
        f"SELECT y FROM events WHERE {where} AND type = 'scroll'", args)])

    totals = conn.execute(
        f"""SELECT COUNT(*) AS events, COUNT(DISTINCT session_id) AS sessions,
                   MIN(created_at) AS first_seen, MAX(created_at) AS last_seen
            FROM events WHERE {where}""", args).fetchone()
    bookings_recorded = conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE business_id = ? AND created_at >= ?",
        args).fetchone()["c"]
    conn.close()

    return {
        "business": biz["name"],
        "slug": slug,
        "days": days,
        "tz_offset": TZ_OFFSET_HOURS,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "funnel": funnel,
        "views": views,
        "bookings": per_type.get("success", 0),
        "bookings_recorded": bookings_recorded,
        "conversion_pct": _pct(per_type.get("success", 0), views),
        "engagement_pct": _pct(per_type.get("click", 0), views),
        "form_completion_pct": _pct(per_type.get("success", 0), focus_sessions),
        "median_seconds_to_book": round(_median(secs), 1) if secs else None,
        "median_seconds_on_page": round(_median(dwell), 1) if dwell else None,
        "median_scroll_pct": round(scroll, 1) if scroll is not None else None,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "by_day": sorted(by_day.values(), key=lambda d: d["date"]),
        "fields": fields,
        "abandon": abandon,
        "clicks": clicks,
        "top_elements": top_elements,
        "devices": devices,
        "referrers": referrers,
        "totals": dict(totals) if totals else {},
    }


def api_stats(slug: str, params: dict) -> tuple:
    """Read-only stats feed. Key-protected — this is client business data,
    not something to leave open to the world like /api/book."""
    if params.get("key", [""])[0] != ADMIN_KEY:
        return json.dumps({"error": "unauthorized"}).encode(), 401
    try:
        days = max(1, min(365, int(params.get("days", ["30"])[0])))
    except ValueError:
        days = 30
    data = stats_for(slug, days)
    status = 404 if data.get("error") else 200
    return json.dumps(data).encode(), status


# ─────────────────────────────────────────── Analytics: the dashboard
# Served whole from here rather than from /static so the file stays a single
# deployable app.py, exactly like every other page on this site.

ANALYTICS_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analytics · Rollcfluence</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<meta name="robots" content="noindex">
<style>
  :root {
    color-scheme: dark;
    --surface-0:#0d0a17; --surface-1:#141024; --surface-2:#1c1732;
    --line:#2b2447; --line-soft:#221c3b;
    --text-1:#f4f2fb; --text-2:#b9b2d6; --text-3:#7d769b;
    /* Rollcfluence brand, validated on this surface:
       CVD ΔE 12.9, normal-vision ΔE 27.6, both ≥3:1 contrast. */
    --series-1:#3E63DD;   /* visits  */
    --series-2:#D6368F;   /* bookings */
    --accent:#C9A0DC;
    --good:#2fa96b; --warn:#e0a13a; --bad:#e05c5c;
    --r:14px;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--surface-0); color:var(--text-1);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    font-size:14px; line-height:1.5;
  }
  a { color:var(--accent); }
  header {
    padding:18px 24px; border-bottom:1px solid var(--line);
    background:linear-gradient(120deg,#3E63DD22,#D6368F22),var(--surface-1);
    position:sticky; top:0; z-index:20; backdrop-filter:blur(6px);
  }
  .bar { max-width:1200px; margin:0 auto; display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
  .bar h1 { font-size:17px; margin:0; letter-spacing:-.2px; flex:1 1 200px; }
  .bar h1 span { color:var(--text-3); font-weight:400; }
  select, input[type=text], button {
    background:var(--surface-2); color:var(--text-1); border:1px solid var(--line);
    border-radius:9px; padding:8px 11px; font:inherit; font-size:13px;
  }
  button { cursor:pointer; }
  button:hover, select:hover { border-color:var(--accent); }
  button.on { background:var(--series-1); border-color:var(--series-1); color:#fff; }
  main { max-width:1200px; margin:0 auto; padding:22px 24px 80px; }
  .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(270px,1fr)); }
  .card {
    background:var(--surface-1); border:1px solid var(--line);
    border-radius:var(--r); padding:18px 18px 16px;
  }
  .card h2 { font-size:13px; margin:0 0 2px; letter-spacing:.02em; text-transform:uppercase; color:var(--text-2); font-weight:600; }
  .card .sub { font-size:12px; color:var(--text-3); margin:0 0 14px; }
  .wide { grid-column:1/-1; }
  /* stat tiles — a hero number is a form, not a chart */
  .kpis { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); margin-bottom:16px; }
  .kpi { background:var(--surface-1); border:1px solid var(--line); border-radius:var(--r); padding:16px 18px; }
  .kpi .n { font-size:33px; font-weight:700; letter-spacing:-1.2px; line-height:1.05; }
  .kpi .l { font-size:12px; color:var(--text-2); margin-top:5px; }
  .kpi .h { font-size:11px; color:var(--text-3); margin-top:3px; }
  .kpi.hero { background:linear-gradient(140deg,#3E63DD26,#D6368F26),var(--surface-1); border-color:#3E63DD66; }
  /* funnel */
  .fstage { margin-bottom:11px; }
  .fhead { display:flex; justify-content:space-between; align-items:baseline; font-size:13px; margin-bottom:5px; gap:10px; }
  .fhead .v { color:var(--text-2); font-variant-numeric:tabular-nums; }
  .ftrack { height:26px; background:var(--surface-2); border-radius:6px; overflow:hidden; }
  .ffill { height:100%; border-radius:6px; background:var(--series-1); transition:width .5s cubic-bezier(.2,.7,.3,1); }
  .flost { font-size:11.5px; color:var(--text-3); margin-top:4px; }
  .flost b { color:var(--bad); font-weight:600; }
  /* charts */
  svg { display:block; width:100%; overflow:visible; }
  .gridline { stroke:var(--line-soft); stroke-width:1; }
  .axis { fill:var(--text-3); font-size:10.5px; }
  .mark { transition:opacity .12s; }
  .mark:hover { opacity:.75; }
  .legend { display:flex; gap:16px; font-size:12px; color:var(--text-2); margin-bottom:10px; flex-wrap:wrap; }
  .legend i { width:10px; height:10px; border-radius:3px; display:inline-block; margin-right:6px; vertical-align:-1px; }
  /* heatmap */
  .hmwrap { position:relative; background:var(--surface-2); border-radius:10px; overflow:hidden; border:1px solid var(--line); }
  .hmwrap iframe { width:100%; height:100%; border:0; position:absolute; inset:0; opacity:.55; }
  .hmwrap canvas { position:relative; display:block; width:100%; }
  .hmempty { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--text-3); font-size:13px; text-align:center; padding:20px; }
  .hmscale { display:flex; align-items:center; gap:10px; font-size:11.5px; color:var(--text-3); margin-top:10px; }
  .hmramp { height:9px; flex:1; border-radius:5px; background:linear-gradient(90deg,#2a0f21,#5b1740,#D6368F,#ffc2e0); }
  /* tables */
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--text-3); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; padding:0 8px 8px 0; }
  td { padding:7px 8px 7px 0; border-top:1px solid var(--line-soft); font-variant-numeric:tabular-nums; }
  td.el { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; color:var(--text-1); }
  .minibar { height:7px; border-radius:4px; background:var(--series-1); min-width:2px; }
  .muted { color:var(--text-3); }
  .empty { color:var(--text-3); font-size:13px; padding:14px 0; }
  #tip {
    position:fixed; pointer-events:none; z-index:60; opacity:0; transition:opacity .1s;
    background:#080611f2; border:1px solid var(--line); border-radius:9px; padding:8px 11px;
    font-size:12.5px; box-shadow:0 10px 30px #0009; max-width:240px;
  }
  #tip b { display:block; margin-bottom:3px; font-size:12px; color:var(--text-2); font-weight:600; }
  .note { font-size:12px; color:var(--text-3); margin-top:16px; line-height:1.6; }
  .pill { display:inline-block; font-size:11px; padding:2px 8px; border-radius:20px; background:var(--surface-2); color:var(--text-2); border:1px solid var(--line); }
  @media (max-width:640px){ main{padding:16px 14px 60px;} header{padding:14px;} .kpi .n{font-size:27px;} }
</style>
</head>
<body>

<header>
  <div class="bar">
    <h1>Analytics <span id="bizname">— pick a business</span></h1>
    <select id="biz"></select>
    <select id="days">
      <option value="7">Last 7 days</option>
      <option value="30" selected>Last 30 days</option>
      <option value="90">Last 90 days</option>
      <option value="365">Last year</option>
    </select>
    <button id="reload">Refresh</button>
    <button id="demo" title="See what the dashboard looks like with traffic">Demo data</button>
  </div>
</header>

<main>
  <div class="kpis" id="kpis"></div>

  <div class="grid">
    <div class="card wide">
      <h2>The funnel</h2>
      <p class="sub">Every bar is a share of visits. The grey number on the right is how many of the
         previous step survived — that is where you fix things.</p>
      <div id="funnel"></div>
    </div>

    <div class="card wide">
      <h2>Where they click</h2>
      <p class="sub">Brighter = more clicks. Positions are stored as a percentage of the page, so
         phones and laptops land on the same map.</p>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">
        <input type="text" id="pageurl" placeholder="Client page URL (optional — shows the real page underneath)" style="flex:1 1 260px">
        <button data-dev="all" class="devbtn on">All devices</button>
        <button data-dev="mobile" class="devbtn">Mobile</button>
        <button data-dev="desktop" class="devbtn">Desktop</button>
      </div>
      <div class="hmwrap" id="hmwrap">
        <iframe id="hmframe" sandbox="allow-scripts" title="client page preview" style="display:none"></iframe>
        <canvas id="heat" width="1000" height="640"></canvas>
        <div class="hmempty" id="hmempty">No clicks recorded yet.</div>
      </div>
      <div class="hmscale"><span>fewer clicks</span><div class="hmramp"></div><span>more</span></div>
    </div>

    <div class="card wide">
      <h2>What time they book</h2>
      <p class="sub">Local time (UTC<span id="tzoff">+3</span>).</p>
      <div class="legend">
        <span><i style="background:var(--series-1)"></i>Visits</span>
        <span><i style="background:var(--series-2)"></i>Bookings</span>
      </div>
      <div id="hours"></div>
    </div>

    <div class="card wide">
      <h2>Which day they book</h2>
      <p class="sub">Same two measures, by weekday.</p>
      <div class="legend">
        <span><i style="background:var(--series-1)"></i>Visits</span>
        <span><i style="background:var(--series-2)"></i>Bookings</span>
      </div>
      <div id="weekdays"></div>
    </div>

    <div class="card wide">
      <h2>Day by day</h2>
      <div class="legend">
        <span><i style="background:var(--series-1)"></i>Visits</span>
        <span><i style="background:var(--series-2)"></i>Bookings</span>
      </div>
      <div id="trend"></div>
    </div>

    <div class="card">
      <h2>How far into the form they get</h2>
      <p class="sub">Share of everyone who started filling anything in.</p>
      <div id="fields"></div>
    </div>

    <div class="card">
      <h2>Where they give up</h2>
      <p class="sub">Last field touched by visits that never booked. The top row is the field
         costing you the most money.</p>
      <div id="abandon"></div>
    </div>

    <div class="card">
      <h2>Most clicked</h2>
      <div id="elements"></div>
    </div>

    <div class="card">
      <h2>Who and where from</h2>
      <div id="audience"></div>
    </div>
  </div>

  <p class="note" id="note"></p>
</main>

<div id="tip"></div>

<script>
(function () {
  "use strict";

  var KEY  = "__KEY__";
  var SLUG = "__SLUG__";
  var BUSINESSES = __BUSINESSES__;   // [{slug,name,bookings}]
  var S1 = "#3E63DD", S2 = "#D6368F";
  var data = null, demoMode = false, deviceFilter = "all";

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var fmt = function (n) { return (n == null ? "—" : n.toLocaleString()); };
  var pct = function (n) { return (n == null ? "—" : n.toFixed(1).replace(/\.0$/, "") + "%"); };
  var dur = function (s) {
    if (s == null) return "—";
    if (s < 60) return Math.round(s) + "s";
    var m = Math.floor(s / 60);
    return m + "m " + Math.round(s - m * 60) + "s";
  };

  // ── tooltip ─────────────────────────────────────────────
  var tip = $("tip");
  function bindTip(el, title, lines) {
    el.addEventListener("mousemove", function (e) {
      tip.innerHTML = "<b>" + esc(title) + "</b>" + lines.map(esc).join("<br>");
      tip.style.opacity = 1;
      var x = Math.min(e.clientX + 14, innerWidth - 250);
      tip.style.left = x + "px";
      tip.style.top = Math.max(8, e.clientY - 46) + "px";
    });
    el.addEventListener("mouseleave", function () { tip.style.opacity = 0; });
  }

  // ── grouped bar chart: 1 or 2 series, one shared y-axis ──
  function groupedBars(mount, rows, labelKey, series, opts) {
    opts = opts || {};
    var W = opts.W || 520, H = opts.height || 190, padL = W * 0.06, padB = 26, padT = 8;
    var fs = opts.font || 13;
    var max = 0;
    rows.forEach(function (r) { series.forEach(function (s) { max = Math.max(max, r[s.key] || 0); }); });
    max = max || 1;
    var innerW = W - padL, innerH = H - padB - padT;
    var slot = innerW / rows.length;
    var gap = 2;                                  // 2px surface gap between adjacent fills
    var bw = Math.max(2, (slot - gap * 2) / series.length - gap);

    var svg = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img">'];
    [0, 0.5, 1].forEach(function (t) {
      var y = padT + innerH - innerH * t;
      svg.push('<line class="gridline" x1="' + padL + '" x2="' + W + '" y1="' + y + '" y2="' + y + '"/>');
      svg.push('<text class="axis" font-size="' + fs + '" x="0" y="' + (y + fs / 3) + '">' + Math.round(max * t) + '</text>');
    });
    rows.forEach(function (r, i) {
      series.forEach(function (s, j) {
        var v = r[s.key] || 0;
        var h = v / max * innerH;
        var x = padL + i * slot + gap + j * (bw + gap);
        var y = padT + innerH - h;
        svg.push('<rect class="mark" data-i="' + i + '" data-j="' + j + '" x="' + x + '" y="' + y +
                 '" width="' + bw + '" height="' + Math.max(h, v > 0 ? 1.5 : 0) +
                 '" rx="3" fill="' + s.color + '"/>');
      });
      if (!opts.everyOther || i % opts.everyOther === 0) {
        svg.push('<text class="axis" font-size="' + fs + '" text-anchor="middle" x="' +
                 (padL + i * slot + slot / 2) + '" y="' + (H - 8) + '">' +
                 esc(String(r[labelKey]).slice(0, opts.labelChars || 3)) + '</text>');
      }
    });
    svg.push("</svg>");
    mount.innerHTML = svg.join("");
    mount.querySelectorAll("rect.mark").forEach(function (el) {
      var r = rows[+el.dataset.i], s = series[+el.dataset.j];
      bindTip(el, r[labelKey], series.map(function (ss) { return ss.label + ": " + fmt(r[ss.key] || 0); }));
    });
  }

  // ── line chart: 2 series, shared axis, crosshair ─────────
  function lineChart(mount, rows, labelKey, series) {
    if (!rows.length) { mount.innerHTML = '<p class="empty">No days with traffic yet.</p>'; return; }
    var W = 1000, H = 210, padL = 38, padB = 24, padT = 10;
    var fs = 12;
    var max = 0;
    rows.forEach(function (r) { series.forEach(function (s) { max = Math.max(max, r[s.key] || 0); }); });
    max = max || 1;
    var innerW = W - padL - 6, innerH = H - padB - padT;
    var X = function (i) { return padL + (rows.length === 1 ? innerW / 2 : i / (rows.length - 1) * innerW); };
    var Y = function (v) { return padT + innerH - (v || 0) / max * innerH; };

    var svg = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img">'];
    [0, 0.5, 1].forEach(function (t) {
      var y = padT + innerH - innerH * t;
      svg.push('<line class="gridline" x1="' + padL + '" x2="' + W + '" y1="' + y + '" y2="' + y + '"/>');
      svg.push('<text class="axis" font-size="' + fs + '" x="0" y="' + (y + fs / 3) + '">' + Math.round(max * t) + '</text>');
    });
    series.forEach(function (s) {
      var d = rows.map(function (r, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(r[s.key]).toFixed(1); }).join(" ");
      svg.push('<path d="' + d + '" fill="none" stroke="' + s.color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>');
      var last = rows.length - 1;
      svg.push('<circle cx="' + X(last) + '" cy="' + Y(rows[last][s.key]) + '" r="4" fill="' + s.color +
               '" stroke="var(--surface-1)" stroke-width="2"/>');
    });
    var step = Math.max(1, Math.ceil(rows.length / 8));
    rows.forEach(function (r, i) {
      if (i % step === 0) {
        svg.push('<text class="axis" text-anchor="middle" x="' + X(i) + '" y="' + (H - 6) + '">' +
                 esc(String(r[labelKey]).slice(5)) + "</text>");
      }
      svg.push('<rect class="hit" x="' + (X(i) - innerW / rows.length / 2) + '" y="0" width="' +
               (innerW / rows.length) + '" height="' + H + '" fill="transparent" data-i="' + i + '"/>');
    });
    svg.push("</svg>");
    mount.innerHTML = svg.join("");
    mount.querySelectorAll("rect.hit").forEach(function (el) {
      var r = rows[+el.dataset.i];
      bindTip(el, r[labelKey], series.map(function (s) { return s.label + ": " + fmt(r[s.key] || 0); }));
    });
  }

  // ── click heatmap: single-hue sequential ramp ────────────
  function ramp(t) {
    var stops = [[0, [42, 15, 33]], [0.35, [91, 23, 64]], [0.72, [214, 54, 143]], [1, [255, 194, 224]]];
    for (var i = 1; i < stops.length; i++) {
      if (t <= stops[i][0]) {
        var a = stops[i - 1], b = stops[i], k = (t - a[0]) / (b[0] - a[0]);
        return [0, 1, 2].map(function (c) { return Math.round(a[1][c] + (b[1][c] - a[1][c]) * k); });
      }
    }
    return stops[stops.length - 1][1];
  }

  function drawHeat(clicks) {
    var cv = $("heat"), ctx = cv.getContext("2d");
    var pts = clicks.filter(function (c) { return deviceFilter === "all" || c.device === deviceFilter; });
    ctx.clearRect(0, 0, cv.width, cv.height);
    $("hmempty").style.display = pts.length ? "none" : "flex";
    if (!pts.length) return;

    var r = cv.width * 0.032;
    ctx.globalCompositeOperation = "lighter";
    pts.forEach(function (c) {
      var x = c.x / 100 * cv.width, y = c.y / 100 * cv.height;
      var g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, "rgba(255,255,255,0.17)");
      g.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();
    });
    ctx.globalCompositeOperation = "source-over";

    var img = ctx.getImageData(0, 0, cv.width, cv.height), d = img.data;
    for (var i = 0; i < d.length; i += 4) {
      var a = d[i + 3] / 255;
      if (a < 0.01) { d[i + 3] = 0; continue; }
      var col = ramp(Math.min(1, a * 1.6));
      d[i] = col[0]; d[i + 1] = col[1]; d[i + 2] = col[2];
      d[i + 3] = Math.min(235, 40 + a * 520);
    }
    ctx.putImageData(img, 0, 0);
  }

  // ── renderers ───────────────────────────────────────────
  function renderKpis(d) {
    var tiles = [
      ["hero", pct(d.conversion_pct), "Visits that became a booking",
       fmt(d.bookings) + " of " + fmt(d.views) + " visits"],
      ["", fmt(d.views), "Visits", d.days + "-day window"],
      ["", pct(d.engagement_pct), "Clicked something", "of everyone who opened the page"],
      ["", pct(d.form_completion_pct), "Finished the form", "of everyone who started it"],
      ["", dur(d.median_seconds_to_book), "Typical time to book", "from opening the page"],
      ["", dur(d.median_seconds_on_page), "Typical time on page", "median across all visits"]
    ];
    $("kpis").innerHTML = tiles.map(function (t) {
      return '<div class="kpi ' + t[0] + '"><div class="n">' + esc(t[1]) + '</div>' +
             '<div class="l">' + esc(t[2]) + '</div><div class="h">' + esc(t[3]) + "</div></div>";
    }).join("");
  }

  function renderFunnel(d) {
    var views = d.views || 0;
    $("funnel").innerHTML = d.funnel.map(function (f, i) {
      var lost = i > 0 && f.lost > 0
        ? '<div class="flost"><b>' + fmt(f.lost) + "</b> dropped out here — " +
          pct(100 - f.pct_of_prev) + " of the step before</div>"
        : "";
      return '<div class="fstage">' +
        '<div class="fhead"><span>' + esc(f.label) + "</span>" +
        '<span class="v">' + fmt(f.sessions) + " · " + pct(f.pct_of_views) + " of visits</span></div>" +
        '<div class="ftrack"><div class="ffill" style="width:' +
        (views ? Math.max(f.pct_of_views, f.sessions ? 1 : 0) : 0) + '%"></div></div>' + lost + "</div>";
    }).join("");
  }

  function barTable(mount, rows, nameKey, valKey, pctKey, emptyMsg) {
    if (!rows.length) { mount.innerHTML = '<p class="empty">' + esc(emptyMsg) + "</p>"; return; }
    var max = Math.max.apply(null, rows.map(function (r) { return r[valKey]; })) || 1;
    mount.innerHTML = "<table><tr><th>" + esc(nameKey === "element" ? "Field / element" : "Source") +
      "</th><th>Visits</th><th style='width:38%'></th></tr>" +
      rows.map(function (r) {
        return "<tr><td class='el'>" + esc(r[nameKey] || "—") + "</td>" +
          "<td>" + fmt(r[valKey]) + (pctKey && r[pctKey] != null ? " <span class='muted'>· " + pct(r[pctKey]) + "</span>" : "") + "</td>" +
          "<td><div class='minibar' style='width:" + (r[valKey] / max * 100) + "%'></div></td></tr>";
      }).join("") + "</table>";
  }

  function render(d) {
    data = d;
    $("bizname").textContent = "— " + d.business;
    $("tzoff").textContent = (d.tz_offset >= 0 ? "+" : "") + d.tz_offset;
    renderKpis(d);
    renderFunnel(d);
    var series = [{ key: "views", label: "Visits", color: S1 }, { key: "bookings", label: "Bookings", color: S2 }];
    groupedBars($("hours"), d.by_hour, "hour", series, { W: 1000, height: 200, labelChars: 2 });
    groupedBars($("weekdays"), d.by_weekday, "day", series, { W: 1000, height: 190, labelChars: 3 });
    lineChart($("trend"), d.by_day, "date", series);
    barTable($("fields"), d.fields, "element", "reached", "pct_of_starters", "Nobody has touched the form yet.");
    barTable($("abandon"), d.abandon, "element", "sessions", "pct", "Nobody has abandoned the form — or nobody has started it.");
    barTable($("elements"), d.top_elements, "element", "clicks", null, "No clicks recorded yet.");
    $("audience").innerHTML =
      "<table><tr><th>Device</th><th>Visits</th></tr>" +
      (d.devices.map(function (x) {
        return "<tr><td>" + esc(x.device) + "</td><td>" + fmt(x.sessions) + " <span class='muted'>· " + pct(x.pct) + "</span></td></tr>";
      }).join("") || "<tr><td colspan=2 class='muted'>—</td></tr>") +
      "</table><table style='margin-top:16px'><tr><th>Came from</th><th>Visits</th></tr>" +
      (d.referrers.map(function (x) {
        return "<tr><td>" + esc(x.source) + "</td><td>" + fmt(x.sessions) + " <span class='muted'>· " + pct(x.pct) + "</span></td></tr>";
      }).join("") || "<tr><td colspan=2 class='muted'>—</td></tr>") + "</table>";
    drawHeat(d.clicks);
    $("note").innerHTML = fmt((d.totals && d.totals.events) || 0) + " events from " +
      fmt((d.totals && d.totals.sessions) || 0) + " visits" +
      (demoMode ? ' <span class="pill">demo data — not real</span>' : "") +
      ". No cookies, no IP addresses, no device fingerprints are stored — which is why your clients need no consent banner." +
      " Bookings actually saved in the database for this window: " + fmt(d.bookings_recorded) + ".";
  }

  // ── demo data, so the dashboard is readable before real traffic ──
  function demo() {
    var rnd = function (a, b) { return a + Math.random() * (b - a); };
    var views = 840, click = 604, focus = 296, submit = 121, ok = 98;
    var stages = [["view", "Opened the page", views], ["click", "Clicked something", click],
                  ["focus", "Started the form", focus], ["submit", "Pressed Book", submit],
                  ["success", "Booking confirmed", ok]];
    var prev = null;
    var funnel = stages.map(function (s) {
      var f = { key: s[0], label: s[1], sessions: s[2],
                pct_of_views: +(s[2] / views * 100).toFixed(1),
                pct_of_prev: prev === null ? 100 : +(s[2] / prev * 100).toFixed(1),
                lost: prev === null ? 0 : prev - s[2] };
      prev = s[2]; return f;
    });
    var hours = [], shape = [1,1,1,1,1,2,4,9,14,17,16,15,14,16,18,19,21,26,31,34,30,22,12,5];
    for (var h = 0; h < 24; h++) {
      hours.push({ hour: ("0" + h).slice(-2), views: Math.round(shape[h] * rnd(.8, 1.2)),
                   bookings: Math.round(shape[h] * rnd(.06, 0.16)) });
    }
    var names = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
    var wshape = [70,132,141,128,150,169,50];
    var week = names.map(function (n, i) {
      return { day: n, views: wshape[i], bookings: Math.round(wshape[i] * rnd(.07, .15)) };
    });
    var days = [], t = new Date();
    for (var i = 29; i >= 0; i--) {
      var dd = new Date(t - i * 864e5).toISOString().slice(0, 10);
      var v = Math.round(rnd(14, 42));
      days.push({ date: dd, views: v, bookings: Math.round(v * rnd(.04, .18)) });
    }
    var clicks = [];
    var hot = [[50, 34, 180], [50, 62, 240], [50, 71, 120], [50, 78, 90], [50, 86, 260], [30, 12, 40], [72, 12, 30]];
    hot.forEach(function (h) {
      for (var i = 0; i < h[2]; i++) {
        clicks.push({ x: Math.max(0, Math.min(100, h[0] + rnd(-7, 7))),
                      y: Math.max(0, Math.min(100, h[1] + rnd(-3.5, 3.5))),
                      el: "", device: Math.random() < .64 ? "mobile" : "desktop" });
      }
    });
    for (var i = 0; i < 120; i++) {
      clicks.push({ x: rnd(2, 98), y: rnd(2, 98), el: "", device: Math.random() < .64 ? "mobile" : "desktop" });
    }
    return {
      business: "Demo Barbershop", slug: "demo", days: 30, tz_offset: 3,
      funnel: funnel, views: views, bookings: ok, bookings_recorded: ok,
      conversion_pct: +(ok / views * 100).toFixed(1),
      engagement_pct: +(click / views * 100).toFixed(1),
      form_completion_pct: +(ok / focus * 100).toFixed(1),
      median_seconds_to_book: 71, median_seconds_on_page: 46, median_scroll_pct: 62,
      by_hour: hours, by_weekday: week, by_day: days,
      fields: [{ element: "customer_name", reached: 296, pct_of_starters: 100 },
               { element: "customer_contact", reached: 231, pct_of_starters: 78 },
               { element: "requested_date", reached: 178, pct_of_starters: 60.1 },
               { element: "requested_time", reached: 151, pct_of_starters: 51 },
               { element: "note", reached: 66, pct_of_starters: 22.3 }],
      abandon: [{ element: "customer_contact", sessions: 74, pct: 37.4 },
                { element: "requested_date", sessions: 51, pct: 25.8 },
                { element: "customer_name", sessions: 44, pct: 22.2 },
                { element: "requested_time", sessions: 29, pct: 14.6 }],
      clicks: clicks,
      top_elements: [{ element: "book-btn", clicks: 388, sessions: 301 },
                     { element: "customer_name", clicks: 296, sessions: 296 },
                     { element: "requested_date", clicks: 212, sessions: 178 },
                     { element: "(background)", clicks: 164, sessions: 120 },
                     { element: "services", clicks: 121, sessions: 98 }],
      devices: [{ device: "mobile", sessions: 538, pct: 64 }, { device: "desktop", sessions: 244, pct: 29 },
                { device: "tablet", sessions: 58, pct: 6.9 }],
      referrers: [{ source: "instagram", sessions: 421, pct: 50.1 }, { source: "direct", sessions: 232, pct: 27.6 },
                  { source: "google", sessions: 121, pct: 14.4 }, { source: "facebook", sessions: 66, pct: 7.9 }],
      totals: { events: 11840, sessions: 840 }
    };
  }

  // ── wiring ──────────────────────────────────────────────
  function load() {
    if (demoMode) { render(demo()); return; }
    var slug = $("biz").value, days = $("days").value;
    if (!slug) { $("note").textContent = "No businesses registered yet."; return; }
    fetch("/api/stats/" + encodeURIComponent(slug) + "?key=" + encodeURIComponent(KEY) + "&days=" + days)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) { $("note").textContent = "Could not load: " + d.error; return; }
        render(d);
      })
      .catch(function (e) { $("note").textContent = "Could not load: " + e; });
  }

  $("biz").innerHTML = BUSINESSES.map(function (b) {
    return '<option value="' + esc(b.slug) + '"' + (b.slug === SLUG ? " selected" : "") + ">" +
      esc(b.name) + "</option>";
  }).join("");
  $("biz").addEventListener("change", function () { demoMode = false; $("demo").classList.remove("on"); load(); });
  $("days").addEventListener("change", load);
  $("reload").addEventListener("click", load);
  $("demo").addEventListener("click", function () {
    demoMode = !demoMode;
    this.classList.toggle("on", demoMode);
    load();
  });
  document.querySelectorAll(".devbtn").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".devbtn").forEach(function (o) { o.classList.remove("on"); });
      b.classList.add("on");
      deviceFilter = b.dataset.dev;
      if (data) drawHeat(data.clicks);
    });
  });
  $("pageurl").addEventListener("change", function () {
    var u = this.value.trim(), f = $("hmframe");
    if (!u) { f.style.display = "none"; return; }
    f.src = u; f.style.display = "block";
    f.style.height = $("heat").clientHeight + "px";
  });

  load();
})();
</script>
</body>
</html>
"""


def route_analytics(handler, params):
    """Ilko-only view of one client's page performance."""
    key = params.get("key", [""])[0]
    if key != ADMIN_KEY:
        body = """
        <div class="card">
          <h2>Analytics access</h2>
          <p class="muted">Enter your access key (the same <code>ADMIN_KEY</code> as the dashboard).</p>
          <form method="get" action="/analytics">
            <label>Access key</label>
            <input name="key" type="password">
            <button type="submit">View analytics</button>
          </form>
        </div>
        """
        return page("Analytics", body)

    conn = get_db()
    businesses = [
        {"slug": r["slug"], "name": r["name"]}
        for r in conn.execute("SELECT slug, name FROM businesses ORDER BY name")
    ]
    conn.close()
    slug = params.get("slug", [""])[0] or (businesses[0]["slug"] if businesses else "")

    return (ANALYTICS_HTML
            .replace("__BUSINESSES__", json.dumps(businesses))
            .replace("__KEY__", json.dumps(key)[1:-1])
            .replace("__SLUG__", json.dumps(slug)[1:-1])
            ).encode()

def route_dashboard(handler, params):
    key = params.get("key", [""])[0]
    if key != ADMIN_KEY:
        body = f"""
        <div class="card">
          <h2>Dashboard access</h2>
          <p class="muted">Enter your access key (set via the <code>ADMIN_KEY</code> environment variable).</p>
          <form method="get" action="/dashboard">
            <label>Access key</label>
            <input name="key" type="password">
            <button type="submit">View dashboard</button>
          </form>
        </div>
        """
        return page("Dashboard", body)

    conn = get_db()
    businesses = conn.execute(
        """SELECT b.*, (SELECT COUNT(*) FROM bookings k WHERE k.business_id = b.id) AS booking_count
           FROM businesses b ORDER BY b.created_at DESC"""
    ).fetchall()
    bookings = conn.execute(
        """SELECT k.*, b.name AS business_name FROM bookings k
           JOIN businesses b ON b.id = k.business_id
           ORDER BY k.created_at DESC LIMIT 100"""
    ).fetchall()
    today = date.today().isoformat()
    bookings_today = conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE substr(created_at, 1, 10) = ?", (today,)
    ).fetchone()["c"]
    businesses_today = conn.execute(
        "SELECT COUNT(*) AS c FROM businesses WHERE substr(created_at, 1, 10) = ?", (today,)
    ).fetchone()["c"]
    conn.close()

    biz_rows = "".join(
        f"""<tr>
              <td>{html.escape(b['name'])}</td>
              <td>{html.escape(b['contact_email'])}</td>
              <td>{html.escape(b['service_type'] or '—')}</td>
              <td><code>/book/{html.escape(b['slug'])}</code></td>
              <td>{b['booking_count']}</td>
              <td>{b['created_at'][:16].replace('T',' ')}</td>
              <td><a href="/analytics?key={urllib.parse.quote(key)}&slug={urllib.parse.quote(b['slug'])}">Analytics</a></td>
            </tr>"""
        for b in businesses
    ) or '<tr><td colspan="7" class="muted">No businesses registered yet.</td></tr>'

    booking_rows = "".join(
        f"""<tr>
              <td>{html.escape(k['business_name'])}</td>
              <td>{html.escape(k['customer_name'])}</td>
              <td>{html.escape(k['customer_contact'])}</td>
              <td>{html.escape(k['requested_date'] or '—')} {html.escape(k['requested_time'] or '')}</td>
              <td>{k['created_at'][:16].replace('T',' ')}</td>
            </tr>"""
        for k in bookings
    ) or '<tr><td colspan="5" class="muted">No bookings yet.</td></tr>'

    body = f"""
    <div class="stats">
      <div class="stat"><div class="n">{len(businesses)}</div><div class="l">Businesses registered</div></div>
      <div class="stat"><div class="n">{len(bookings)}</div><div class="l">Total bookings (last 100 shown)</div></div>
      <div class="stat"><div class="n">{bookings_today}</div><div class="l">Bookings today</div></div>
      <div class="stat"><div class="n">{businesses_today}</div><div class="l">New businesses today</div></div>
    </div>

    <div class="card">
      <h2>Businesses</h2>
      <table>
        <tr><th>Name</th><th>Email</th><th>Service</th><th>Booking link</th><th>Bookings</th><th>Registered</th><th></th></tr>
        {biz_rows}
      </table>
    </div>

    <div class="card">
      <h2>Recent bookings</h2>
      <table>
        <tr><th>Business</th><th>Customer</th><th>Contact</th><th>Requested</th><th>Submitted</th></tr>
        {booking_rows}
      </table>
    </div>
    """
    return page("Dashboard", body)


# ─────────────────────────────────────────── HTTP plumbing

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, content: bytes, status=200, extra_headers=None, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, content: bytes, status=200):
        # Wide-open CORS on purpose: /api/* only ever registers a business or
        # takes a booking — both safe, low-risk, rate-limitable-later actions
        # — and this API is designed to be called from landing pages hosted
        # on other domains (e.g. a client's Vercel site), which is the whole
        # point of it existing.
        self._send(content, status, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }, content_type="application/json; charset=utf-8")

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {}
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    def _read_body_json(self):
        """Parse the raw body as JSON whatever the Content-Type says.
        navigator.sendBeacon() sends text/plain (on purpose — any other type
        triggers a CORS preflight that unload-time beacons often lose), so
        _read_json's Content-Type sniffing would silently throw the data away."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send_static(self, filename: str):
        # Only ever serve a small allow-listed set of files from STATIC_DIR —
        # never build a generic file server out of this.
        safe_name = os.path.basename(filename)
        path = os.path.join(STATIC_DIR, safe_name)
        if not os.path.isfile(path):
            self._send(page("Not found", '<div class="card">404</div>'), 404)
            return
        ext = safe_name.rsplit(".", 1)[-1].lower()
        content_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "svg": "image/svg+xml", "gif": "image/gif",
            "js": "application/javascript; charset=utf-8",
            "css": "text/css; charset=utf-8",
            "mp4": "video/mp4", "webm": "video/webm",
        }.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self._send(data, 200, {"Cache-Control": "public, max-age=86400"}, content_type)

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        pathname = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if pathname.startswith("/static/"):
            self._send_static(pathname[len("/static/"):])
        elif pathname == "/":
            self._send(route_home(self))
        elif pathname == "/register":
            self._send(route_register_form(self, prefill_name=params.get("name", [""])[0]))
        elif pathname.startswith("/book/"):
            slug = pathname[len("/book/"):]
            content, status = route_booking_form(self, slug)
            self._send(content, status)
        elif pathname == "/dashboard":
            self._send(route_dashboard(self, params))
        elif pathname == "/analytics":
            self._send(route_analytics(self, params))
        elif pathname.startswith("/api/stats/"):
            slug = pathname[len("/api/stats/"):]
            content, status = api_stats(slug, params)
            self._send_json(content, status)
        else:
            self._send(page("Not found", '<div class="card">404</div>'), 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        pathname = parsed.path

        # JSON API — used by externally-hosted branded pages, not by the
        # HTML forms on this site (those use do_POST's form branches below).
        if pathname == "/api/register":
            self._send_json(api_register(self._read_json()))
            return
        elif pathname.startswith("/api/book/"):
            slug = pathname[len("/api/book/"):]
            content, status = api_book(slug, self._read_json())
            self._send_json(content, status)
            return
        elif pathname.startswith("/api/track/"):
            # Analytics beacons. sendBeacon() only avoids a CORS preflight if
            # the body is text/plain, so this route parses the raw body as JSON
            # itself instead of trusting the Content-Type header.
            slug = pathname[len("/api/track/"):]
            content, status = api_track(slug, self._read_body_json())
            self._send_json(content, status)
            return

        fields = self._read_form()
        if pathname == "/register":
            self._send(route_register_submit(self, fields))
        elif pathname.startswith("/book/"):
            slug = pathname[len("/book/"):]
            content, status = route_booking_submit(self, slug, fields)
            self._send(content, status)
        else:
            self._send(page("Not found", '<div class="card">404</div>'), 404)

    def do_OPTIONS(self):
        # Browsers send this "preflight" check before a cross-site fetch()
        # POST is allowed to actually go through. Without answering it,
        # every /api/ call from an external landing page would silently fail.
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._send_json(b"")
        else:
            self.send_response(204)
            self.end_headers()


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Rollcfluence Booking Hub running at http://localhost:{PORT}", flush=True)
    print(f"Dashboard: http://localhost:{PORT}/dashboard?key={ADMIN_KEY}", flush=True)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Telegram alerts: not configured (see README.md)", flush=True)
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and ALERT_EMAIL_TO):
        print("Email alerts: not configured (see README.md)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
