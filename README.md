# Rollcfluence Booking Hub

Branded with your real logo and gradient (blue → pink, pulled straight from the mark). See the cost section below for what this actually costs to run.

A tiny multi-tenant appointment booking system, built as your first portfolio piece:

- Businesses register at `/register` and get their own public booking link (e.g. `/book/sofia-dental`).
- Their customers book appointments on that page — no login needed for the customer.
- **Every registration and every booking is visible to you only**, on a private dashboard at `/dashboard`, and can also ping you instantly on Telegram and/or by email.

No external libraries required — just Python 3 (standard library only), so it runs on any machine with Python installed, with zero `pip install`.

## Run it

```bash
cd booking_hub
python3 -u app.py
```

Then open `http://localhost:8000`. Your dashboard is at `http://localhost:8000/dashboard` — it will ask for an access key.

**Change the access key before showing this to anyone.** By default it is `rollcfluence`, which is not a real secret. Run it like this instead:

```bash
ADMIN_KEY=pick-something-only-you-know python3 -u app.py
```

The `-u` flag matters — it stops Python from buffering console output, so notification log lines and requests show up immediately instead of only when the process stops.

## How it works, in one paragraph

Everything is stored in a single SQLite file (`booking_hub.db`, created automatically next to `app.py`) — no database server to install. When a business registers, a row is added to a `businesses` table and a unique slug is generated for their booking link. When a customer books through that link, a row is added to a `bookings` table linked to that business. The dashboard just queries both tables and shows counts and recent activity. `notify()` is called on both events and pushes a message to Telegram and/or email if you've configured them — if you haven't, it just prints to the console, so nothing ever crashes for lack of config.

## Turning on instant alerts

Both are optional and independent — turn on either, both, or neither.

### Telegram (recommended — free, instant, works from your phone)

1. Message **@BotFather** on Telegram, send `/newbot`, follow the prompts. You'll get a **bot token** (looks like `123456:ABC-DEF...`).
2. Message your new bot anything (so it can find your chat), then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser — your **chat ID** is in the JSON response under `message.chat.id`.
3. Run the app with both set:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF... TELEGRAM_CHAT_ID=987654321 ADMIN_KEY=yourkey python3 -u app.py
```

### Email

You'll need an SMTP account — a Gmail account with an **app password** (not your normal password) is the easiest free option.

```bash
SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@gmail.com SMTP_PASS=your-app-password \
ALERT_EMAIL_TO=you@gmail.com ADMIN_KEY=yourkey python3 -u app.py
```

## What it costs to run — month by month

Building it cost nothing but time — the code is free, standard-library-only Python, and I wrote it with AI help (this conversation) rather than paying a developer. What actually costs money is keeping it live and reachable by real customers. Three separate line items, each optional and each swappable:

| Line item | Cheapest real option | Typical cost | Why you'd pay for it |
|---|---|---|---|
| **Domain name** | A `.com` or `.bg` through any registrar (Namecheap, Porkbun, etc.) | ~$10–15/year (≈ €1/month) | So the booking link is `rollcfluence.com/book/...` instead of a raw server address. Skip this entirely at first — you don't need a domain for a free pilot. |
| **Hosting** (to keep it live 24/7, not just on your laptop) | Render or Railway free/starter tier | $0–7/month | Free tiers exist but "sleep" the app when idle, causing a slow first load — fine for a pilot, not for a paying client. A small always-on instance (Railway ~$5/mo, a Hetzner VPS ~€4.5/mo) fixes that. |
| **AI API usage** (only if you add AI features — this current version doesn't call any AI) | Claude or OpenAI API, pay-as-you-go | $0 now; a few dollars/month once added | The booking hub itself is plain logic, no AI calls, so it's free to run today. The moment you add the AI qualification/auto-reply layer from the wider plan, this becomes usage-based — at pilot volume (tens of conversations/month) it's typically single-digit dollars. |
| **Everything else** (SSL, email alerts, Telegram alerts) | Included / free | $0 | Every host above issues free HTTPS automatically; Telegram alerts are free; email alerts are free if you use an existing Gmail account. |

**Honest total: $0/month while it's just you testing and running free pilots. Roughly $5–20/month once it's live full-time for real clients, before you've added any AI features. Add another few dollars a month per active AI-powered conversation flow once you build those in.**

That is deliberately not comparable to what an agency or freelance developer would charge to build this from scratch (typically four figures for something equivalent) — the entire cost here is infrastructure, not labor, because the labor was AI-assisted and done by you.

I have not registered a domain or deployed this anywhere on your behalf — both involve real payment, so they're your call. Say the word if you want help picking a host and walking through the deploy step by step when you're ready.

## The animated background (Dither effect)

The home page now has an animated, dithered wave background behind the hero text, in your brand blue. It's a vanilla Three.js port of the React Bits "Dither" component's shader — same visual effect, but without pulling React, `@react-three/fiber`, or `@react-three/postprocessing` into a project that isn't a React app. Files:

- `static/dither-bg.js` — the effect itself. Self-contained, reads its settings from `data-*` attributes on its container `<div>`.
- Three.js itself loads from a CDN (`cdnjs`) via a `<script>` tag on the home page only — no npm install needed.

**To tune it**, edit the `data-*` attributes on `<div id="dither-canvas-wrap">` in `route_home()` in `app.py`:

| Attribute | Effect |
|---|---|
| `data-color` | RGB 0–1, e.g. `"0.24,0.39,0.87"` for blue, `"0.84,0.21,0.56"` for pink |
| `data-pixel-size` | Bigger = chunkier, more retro. 2 is subtle, 6+ is very blocky. |
| `data-color-num` | Fewer colors = more graphic/poster-like. Try 3–6. |
| `data-mouse-interaction` | `"true"`/`"false"` — the wave dents around your cursor when true. |
| `data-wave-speed` | How fast the pattern drifts. |

**To reuse it elsewhere** (e.g. behind the register or dashboard header instead), copy the same `<div id="dither-canvas-wrap" data-...></div>` markup plus the two `<script>` tags into that page's body — the script self-initializes wherever it finds that container ID. If you want it in more than one place on the same page at once, the script would need a small tweak to support multiple containers — ask and I'll extend it.

It degrades safely: if Three.js fails to load (offline, ad blocker, etc.) or WebGL isn't supported, the container just shows its CSS gradient fallback — nothing breaks.

## What this is, and isn't, ready for

This is a working prototype to demo, to use as a portfolio piece, and to run a real free pilot on — not a production system for paying clients yet. Before you use it with a real client's real customer data:

- **Move off SQLite-on-a-laptop.** It's fine for a demo or a single pilot; it is not how you'd run this for multiple paying clients reliably. Deploying to a small always-on host (Render, Railway, Fly.io, or a cheap VPS all have free or near-free tiers) is the natural next step, and this code will run on any of them unmodified — it just needs `python3 -u app.py` and the environment variables above.
- **Replace the access-key dashboard auth** with something stronger before it holds real client data — this was kept simple on purpose for a prototype.
- **Add HTTPS** once this is public — none of the platforms above require you to set this up manually, they handle it for you.
- **Collect only what you have consent to collect**, and say so on the booking page, once real customer phone numbers and emails are flowing through this — this is the same compliance point from the operating plan (TCPA-style rules apply the moment this touches real people, even before you're doing outbound texting).

## Extending it

The natural next features, in the order they'd matter to a real client:
1. Business-side login so each client can see only their own bookings (right now, only you see everything — that's what you asked for first).
2. Editable time slots / availability instead of a free-text preferred time.
3. Automatic confirmation message back to the customer (email or SMS) — this is exactly the "missed-message recovery" and "speed-to-lead" pattern from the wider plan.
4. Calendar sync (Google Calendar) so a booking here creates a real calendar event.

None of these are required to demo this or run a first free pilot — build them only once a real client's workflow actually needs them.
