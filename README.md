# PokéDrop

Watches Target for Pokémon TCG drops and posts to your Discord — with a focus
on telling you **before** something goes live, not just when it sells out.

**No server. No credit card. Nothing to install or leave running.** GitHub wakes
it up every 10 minutes for free, it checks Target, posts anything new, and goes
back to sleep.

---

## What it tells you

| Alert | What just happened | Why you care |
|---|---|---|
| 🆕 **New SKU listed** | A product ID appeared that's never been seen | Target creates the product page — **TCIN and DPCI** — days or weeks before you can buy it. This is the earliest possible warning that something is coming. |
| 📅 **Dropping soon** | A known item's release date is within 48 hours | Be at the keyboard for it. |
| 📦 **Preorders open** | You can now place an order ahead of release | Often the real drop moment for hyped sets. |
| 🔥 **In stock** | Orderable for shipping, or your local store has units | The restock ping. |

Every alert shows both SKU numbers:

- **TCIN** — the online ID (the digits after `A-` in a target.com URL)
- **DPCI** — the in-store ID, like `087-06-1234`. This is the number you can
  read to a Target employee to have them check the stockroom.

---

## Setup

Roughly 10 minutes, and you don't need to know how to code. You'll do three
things: make a webhook, make a repo, paste one secret.

### Step 1 — Get a Discord webhook (30 seconds)

A webhook is just a URL that lets something post into one channel. No bot
account, no developer portal.

1. In Discord, **right-click the channel** you want alerts in → **Edit Channel**
2. Left sidebar → **Integrations**
3. **Webhooks** → **New Webhook**
4. Click the new webhook → **Copy Webhook URL**

Keep that URL handy. It looks like
`https://discord.com/api/webhooks/1357.../abcXYZ...`

> **Treat it like a password.** Anyone with it can post in that channel. You'll
> paste it into GitHub's encrypted secrets in Step 3 — never into a file. To
> revoke it, hit **Delete Webhook** on that same screen.

### Step 2 — Put this project on GitHub

1. Make a free account at [github.com](https://github.com) if you don't have one
2. Click **+** (top right) → **New repository**
3. Name it `pokedrop`
4. **Choose Public.** This matters — scheduled jobs are silently disabled on
   free *private* repos, so a private repo will look fine and simply never run.
   Public is also what makes the compute unlimited and free. Your webhook goes
   in encrypted secrets, not in the code, so nothing sensitive is exposed.
5. Click **Create repository**
6. On the next page click **uploading an existing file**
7. Drag in **everything** from this project folder, including the hidden
   `.github` folder — that folder *is* the scheduler, and without it nothing
   will ever run
8. Click **Commit changes**

> On Windows, if you can't see `.github` when dragging: in File Explorer go to
> **View → Show → Hidden items**.

### Step 3 — Paste in your webhook

1. In your repo, click **Settings** (top bar)
2. Left sidebar → **Secrets and variables** → **Actions**
3. **New repository secret**
4. Name: `DISCORD_WEBHOOK_URL` — exactly that, capitals and underscores
5. Secret: paste the webhook URL from Step 1
6. **Add secret**

### Step 4 — Point it at your Target

Same page, click the **Variables** tab → **New repository variable**. Add these
two (they're not secret, just settings):

| Name | Value |
|---|---|
| `TARGET_ZIP` | your ZIP code |
| `TARGET_STORE_ID` | your store's ID |

To find your store ID: go to target.com, pick your store, and look at the URL —
`target.com/sl/brooklyn/**2779**` means your store ID is `2779`.

Skip this step and it'll watch a Minneapolis store, which still catches every
online drop but gives you useless in-store pickup alerts.

### Step 5 — Start it

1. Click the **Actions** tab
2. If it asks, click **I understand my workflows, go ahead and enable them**
3. Click **Watch Target for Pokémon drops** on the left
4. Click **Run workflow** → **Run workflow**

**The first run posts nothing on purpose.** It's recording every Pokémon product
Target already sells, so you don't get 200 notifications about things that have
been on shelves for months. From the second run on, you only hear about what's
actually new.

That's it. It now runs every 10 minutes forever.

---

## Checking on it

**Is it working?** Actions tab → you'll see a run every ~10 minutes. Green check
= fine. Click any run for a summary of how many SKUs it's tracking.

**What does it know about?** Open `state/pokedrop.json` in your repo. Every
product it's found, with TCINs, DPCIs and release dates, updated automatically.

**GitHub's timing is approximate.** Scheduled jobs are best-effort and often run
a few minutes late when GitHub is busy. That's fine for drop warnings, which
give you hours or days of notice. It is *not* fast enough to snipe a surprise
restock down to the second — nothing free is.

---

## Changing what it watches

Settings → Secrets and variables → Actions → **Variables** tab.

| Variable | Default | What it does |
|---|---|---|
| `WATCH_CATEGORIES` | `4yka5,x6ax5,5xtg5` | Target category IDs to sweep. `4yka5` is Pokémon, `x6ax5` is Pokémon Toys, `5xtg5` is Trading Cards. |
| `TITLE_FILTER` | `pokemon\|pokémon` | Only alert on products whose name matches this. |
| `DISCOVERY_INTERVAL` | `1800` | Seconds between sweeps for brand-new SKUs. |
| `STREET_DATE_WARN_HOURS` | `48` | How far ahead of a release date to warn you. |

**Only want sealed TCG product**, not plush and t-shirts? Set `TITLE_FILTER` to:

```
pokemon.*(booster|elite trainer|etb|tin|bundle|collection|deck|blister)
```

**Want a different category?** Browse to any Target category page and read the
ID off the URL: `target.com/c/pokemon/-/N-**4yka5**`.

Changes take effect on the next run — no need to restart anything.

---

## If something goes wrong

**No runs happening at all.** The repo is private (make it public), or the
`.github` folder didn't upload, or workflows were never enabled in the Actions
tab.

**Runs are green but Discord is silent.** Normal for the first run. After that,
check the run summary for "Alerts sent" — if it's genuinely 0, nothing has
changed at Target yet. Also confirm the secret is named exactly
`DISCORD_WEBHOOK_URL`.

**Runs failing with `403`.** Target rotated their public API key. Open
target.com, press **F12** → **Network** tab, reload, click any request to
`redsky.target.com`, and copy the `key=` value out of its URL. Add it as a new
secret named `REDSKY_KEY`.

**It went quiet after a couple of months.** GitHub disables scheduled jobs on
repos with no activity for 60 days. This one commits its own state file
whenever anything changes, which normally keeps it alive — but if it does stop,
open Actions and hit **Run workflow** once to wake it up.

**Alerts about things you don't care about.** Tighten `TITLE_FILTER` above.

---

## Advanced: run it as a real bot instead

If you'd rather have slash commands (`/check`, `/upcoming`, `/track`), the full
Discord bot is included. It needs somewhere to run 24/7 — your own PC, a
Raspberry Pi, or a VPS.

```bash
cp .env.example .env      # then fill in DISCORD_TOKEN and DISCORD_CHANNEL_ID
docker compose up -d
```

Commands: `/track sku`, `/track keyword`, `/track list`, `/track remove`,
`/check`, `/upcoming`, `/recent`, `/find`, `/pokedrop channel`, `/pokedrop role`,
`/pokedrop status`, `/pokedrop scan`.

Three ways to run, same core:

| Mode | Command | Needs |
|---|---|---|
| One-shot scan | `python -m pokedrop scan` | A webhook URL. This is what GitHub Actions runs. |
| Webhook daemon | `python -m pokedrop webhook` | A webhook URL, and a machine that stays on. |
| Full bot | `python -m pokedrop` | A bot token, and a machine that stays on. |

---

## Tests

```bash
python tests/test_pokedrop.py
```

146 assertions covering payload parsing, the search-redirect handling, both
storage backends, the event diff engine, and embed rendering. No network or
Discord account needed.

---

## How it works

```
pokedrop/
  config.py       settings, all from environment variables
  target_api.py   Target's RedSky API client + the Product model
  db.py           SQLite storage (bot mode)
  jsonstore.py    JSON storage (GitHub Actions mode — commits to your repo)
  monitor.py      discovery / street-date / status passes, and the diff engine
  formatting.py   Discord embeds
  oneshot.py      run one pass and exit
  webhook.py      webhook daemon
  bot.py          full bot with slash commands
.github/workflows/watch.yml    the every-10-minutes scheduler
```

It reads Target's RedSky API — the same public JSON endpoints target.com's own
website calls from your browser. Nothing is logged into, and there is
deliberately no cart or checkout functionality. It watches and it tells you.

A note on the search logic, since it's counterintuitive: searching RedSky for
`"pokemon"` returns **zero products** and instead hands back a redirect to
category `4yka5`. So discovery browses category nodes rather than searching
keywords, and any keyword that redirects is followed to its category
automatically.
