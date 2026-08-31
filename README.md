# Reddit → Discord notifier

A Discord bot that pings you as soon as a new post appears in a subreddit you
follow. Add subreddits with `/watch`, optionally filter by keyword, flair or
author, and get an embed with a mention in the channel or your DMs.

## How fast is it?

The bot polls Reddit's `/new` listing every **5 seconds** by default. The trick
that keeps that affordable is that Reddit lets you read several subreddits in
one request — `/r/python+rust+go/new` — so the bot batches up to 25 subreddits
per call. Any number of subreddits up to 25 costs the same 12 requests per
minute, out of an OAuth budget of 100.

Typical end-to-end lag is *poll interval / 2 + a second or two*, so **3–4
seconds**. `/status` reports the measured lag for the most recent post.

Five seconds is the floor the config accepts. Going lower buys very little —
you would be spending quota to shave a second or two — and risks tripping
Reddit's rate limiting.

How far the default scales, since each batch of 25 costs 12 requests/min:

| Subreddits watched | Requests/min at 5s | Budget used |
| --- | --- | --- |
| up to 25 | 12 | 12% |
| 50 | 24 | 24% |
| 100 | 48 | 48% |
| 200 | 96 | 96% — too close |

The bot logs a warning once you cross 80% of the budget, naming the two knobs
that fix it. Past roughly 150 subreddits, raise `POLL_INTERVAL`.

Reddit has no push/webhook API for new posts, so polling is the only option; the
batching above is what makes a short interval practical.

## Setup

### 1. Create the Reddit app

Go to <https://www.reddit.com/prefs/apps> → **create another app**:

- **name**: anything
- **type**: `script`
- **redirect uri**: `http://localhost:8080` (unused, but the form requires one)

Copy the client id (the string under the app name) and the secret.

### 2. Create the Discord bot

At <https://discord.com/developers/applications> → **New Application** → **Bot**
→ **Reset Token**, and copy the token.

Then **OAuth2 → URL Generator**, tick:

- scopes: `bot`, `applications.commands`
- bot permissions: **View Channels**, **Send Messages**, **Embed Links**

Open the generated URL to invite the bot to your server. No privileged intents
are needed — the bot never reads message content.

### 3. Configure and run

```bash
git clone https://github.com/qwertyist12/Reddit-notifications
cd Reddit-notifications
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the four required values
.venv/bin/python main.py
```

Set `DISCORD_GUILD_IDS` to your server's id while you are setting things up:
slash commands appear in those servers instantly, whereas a global sync can take
up to an hour to propagate.

## Commands

| Command | What it does |
| --- | --- |
| `/watch subreddit:python` | Ping you here whenever r/python gets a new post |
| `/watch subreddit:rust keywords:async, tokio` | Only ping when the title or body mentions one of those |
| `/watch subreddit:gamedeals flair:expired author:someone` | Filter by flair text and/or poster |
| `/watch subreddit:python dm:True` | Deliver to your DMs instead of a channel |
| `/watch subreddit:python channel:#feeds mention:@Subscribers` | Post elsewhere and ping a role (needs Manage Server) |
| `/unwatch` | Remove a watch (autocompletes with what is active) |
| `/watches` | List the watches in this server |
| `/status` | Poll health: uptime, last poll, detection lag, Reddit quota, last error |
| `/preview subreddit:python` | Render the newest post, to check formatting and permissions |

All filters are optional and case-insensitive. Keywords are OR-ed together, and
match against the title, body and flair; when you combine keywords with a flair
or author filter, all of them have to pass.

By default anyone can create a watch that pings themselves in the channel they
are in. **Manage Server** is required to point a watch at a different channel or
to ping someone else or a role.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **Required.** Bot token |
| `REDDIT_CLIENT_ID` | — | **Required.** Reddit app id |
| `REDDIT_CLIENT_SECRET` | — | Reddit app secret. Omit only for "installed app" credentials |
| `REDDIT_USERNAME` / `REDDIT_PASSWORD` | unset | Optional. Set both to authenticate as a Reddit account instead of app-only |
| `REDDIT_USER_AGENT` | generic | Reddit asks that you identify your client here |
| `DISCORD_GUILD_IDS` | unset | Comma-separated guild ids for instant command sync |
| `POLL_INTERVAL` | `5` | Seconds between polls (5 is the minimum) |
| `SUBREDDITS_PER_REQUEST` | `25` | Subreddits batched into one Reddit call |
| `MAX_POST_AGE` | `3600` | Posts older than this are never announced |
| `DATA_DIR` | `data` | Where `watches.json` and `seen.json` are written |
| `LOG_LEVEL` | `INFO` | Python log level |

### Tuning for very busy subreddits

Each batched request returns at most 100 posts *in total across the batch*. If
the subreddits in one batch collectively produce more than 100 posts within a
single poll interval, the bot can miss some — it logs a warning when it sees a
full page of unseen posts. If that happens, lower `SUBREDDITS_PER_REQUEST` so
busy subreddits get their own request. (`POLL_INTERVAL` is already at its floor
by default, so shortening it is not an option unless you have raised it.)

## Behaviour worth knowing

- **No backlog spam.** The first time a subreddit is polled, its current posts
  are recorded but not announced. Only posts made after that trigger a ping.
- **Restart-safe.** Seen post ids and watches are persisted to `DATA_DIR`, so a
  restart does not re-announce anything. Posts older than `MAX_POST_AGE` are
  skipped, so a restart after downtime does not dump a backlog into a channel.
- **Failures are isolated.** A deleted channel or a revoked permission is logged
  and skipped; it does not stop notifications for other watches.
- **Reddit outages back off.** Consecutive failures back off from 5s up to 5
  minutes, and bad credentials stop the retry storm instead of hammering Reddit.
- **Nothing gets mass-pinged.** Only the mention the bot constructs is allowed;
  `@everyone` and `@here` are never sent.

## Deployment

Docker:

```bash
docker compose up -d --build   # reads .env, keeps state in a named volume
```

systemd: see `reddit-notifier.service` for a unit to adapt.

Keep `DATA_DIR` on a persistent volume. If you lose it, the bot re-seeds each
subreddit on the next poll (so you will not get a flood of old posts, but posts
made during the gap are missed).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

77 tests cover configuration, the store and its filters, the
dedupe/seeding/batching and rate-budget logic in the poll loop, embed
rendering, and the Reddit OAuth and request paths — the last of these run
against a local stand-in server, so the suite needs no credentials and no
network access.
