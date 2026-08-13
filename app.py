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
import re
import smtplib
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, date
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
            </tr>"""
        for b in businesses
    ) or '<tr><td colspan="6" class="muted">No businesses registered yet.</td></tr>'

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
        <tr><th>Name</th><th>Email</th><th>Service</th><th>Booking link</th><th>Bookings</th><th>Registered</th></tr>
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
            "Access-Control-Allow-Methods": "POST, OPTIONS",
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
