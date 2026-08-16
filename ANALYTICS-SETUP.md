# Rollcfluence page analytics — what it is and how to turn it on

You now have three new things: a collector in the backend, a snippet for client
pages, and a dashboard. Nothing that already worked has changed.

---

## 1. What went into `app.py`

All of it is inside the file you already deploy. No new dependencies, still pure
standard library.

| Added | What it does |
|---|---|
| `events` table | one row per thing a visitor did. Created automatically on next start. |
| `POST /api/track/<slug>` | swallows batches of events from client pages. Open like `/api/book/`, because pages on Vercel have to reach it. |
| `GET /api/stats/<slug>?key=…&days=30` | all the numbers as JSON. **Key-protected** — this is client business data. |
| `GET /analytics?key=…&slug=…` | the dashboard. Same `ADMIN_KEY` as `/dashboard`. |
| Analytics link | new last column in the `/dashboard` business table. |

Three env knobs, all optional:

- `TZ_OFFSET_HOURS` — default `3`. Only affects the "what time of day" charts.
  Change to `2` in winter if you care about the hour being exact.
- `EVENT_RETENTION_DAYS` — default `90`. Older raw events are deleted automatically
  on roughly 1 in 200 writes, so the disk never fills and you never run a cron job.
- `MAX_EVENTS_PER_SESSION` — default `300`. One visitor can never write more than
  this. A bot or a stuck page cannot flood your database.

Your `render.yaml` already mounts a persistent disk at `/var/data`, so the events
survive redeploys the same way bookings do. Nothing to change there.

**Deploying it:** same as always — GitHub Desktop, commit, Push origin, wait for
Render to redeploy. The new table is created on startup; existing data is untouched.

---

## 2. Turning it on for a client page

Paste `tracker-snippet.html` into the client's page **just before `</head>`**.
That's the whole job — one paste, no configuration, no editing of the booking code.

It has to sit above the page's own `<script>`, because it works by wrapping
`fetch()` before the page uses it. That's how it manages to be zero-config:

- it learns the business **slug** from the `/api/register` response the page
  already makes on load,
- it detects a **booking** by watching the `/api/book/` call and its reply.

If you ever have to put it lower in the page, set `window.RC_SLUG = "the-slug"`
and it will pick that up instead.

For new pages: add the block to the templates inside your
`rollcfluence-client-launch` and `rollcfluence-3d-booking` skills, and every
client you launch from then on ships with analytics already on.

### What it records

view · click (with position) · which field was focused · scroll depth · submit ·
booking confirmed · how long the visit lasted.

### What it deliberately does not record

No cookie. No IP address. No user agent. No name, phone or anything else typed
into the form — only *which* field was touched, never its contents. The session
id is a random string that dies when the tab closes.

That is a decision, not an oversight: it keeps the data anonymous, which means
your clients need **no cookie banner and no consent popup**. Say that out loud
when you sell this — it's a real advantage over Google Analytics, and it costs
you nothing. If you ever add an IP column, you lose it.

---

## 3. Reading the dashboard

`https://rollcfluence.onrender.com/analytics?key=YOUR_ADMIN_KEY`

Pick a business, pick a range. The **Demo data** button fills it with realistic
fake traffic — useful for showing a prospect what they'd be getting before they
have any traffic of their own. It's labelled as demo on screen so you can't
confuse the two.

**The numbers, and what each one is for:**

| Number | Means | Use it for |
|---|---|---|
| Conversion % | visits that became a booking | the one number you sell on |
| Clicked something | visits that interacted at all | tells you if the page holds attention |
| Finished the form | of those who started it | separates a page problem from a form problem |
| Typical time to book | median, page open → confirmed | "your customers book in 40 seconds" |
| Funnel | how many survived each step | shows *where* you're losing people |
| Where they give up | last field touched before quitting | **the most valuable panel here** |
| Heatmap | click density as % of page | shows what people try to tap that isn't a button |
| Time / day charts | when bookings actually happen | "half your bookings arrive after 9pm" |

The "where they give up" panel is the one that earns you money. When you can tell
a client *"63% of the people who start your form quit at the phone number field —
I removed it and bookings went up"*, that's a monthly retainer, not a one-off sale.

The heatmap accepts a page URL: paste the client's live link and their real page
loads faintly behind the click map, so you can see exactly what people were
reaching for. Some sites refuse to be embedded — if it stays blank, that's why,
and the map still works on its own.

---

## 4. Things worth knowing

**It cannot break a booking.** Every part of the collector is wrapped so that a
failure logs and returns OK. The snippet swallows its own errors too. If
analytics dies, the booking form still works.

**First 50 seconds after a cold start.** Render's free tier sleeps. The first
visitor of the day may have some early events land late or not at all, exactly
like the register call already does. Not worth fixing.

**Numbers are per-visit, not per-person.** Someone who visits twice counts twice.
That's the honest thing to report, and it's what "conversion rate" means anyway.

**Verification done before delivery:** 400 simulated visits with a known correct
answer were pushed through the live API, and every funnel count, percentage and
drop-off figure was checked against an independent recount straight from SQL —
all matched. The snippet was then driven through a real browser (register, click,
fill four fields, book) and produced the full correct event chain with the page's
own code untouched. Flood cap, bad payloads, wrong keys and unknown slugs were all
tested and behave.
