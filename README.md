# Gmail AI Sorter

A self-hosted, free-tier daily Gmail sorter + digest with its own web
dashboard, built to run as a Docker/Portainer stack on your Asustor
AS1102T NAS (ARM64). It replicates (and is meant to eventually replace)
the Gmail-sorting digest currently running as a Claude Cowork scheduled
task, but running entirely under your own control with no per-message
cost.

**Deploy once, then everything else happens in the browser.** You set
five secrets in Portainer's stack environment variables (once), deploy,
then open the dashboard and click "Connect a Gmail account" - no scripts
to run, no files to copy onto the NAS, no `.env` file to hunt for.

## What it does, once a day, per connected Gmail account

1. Looks at everything currently in the inbox and asks Gemini to match
   each email to one of the labels **already in your account** (it never
   creates new labels). Confident matches get that label applied and are
   archived out of the inbox. Low-confidence ones are left in the inbox.
2. Sweeps every other label ("folder") for unread mail, reads it, and
   marks it read.
3. Flags anything that needs a personal reply. For each one, it checks
   Gmail for an existing draft on that thread first (never creates a
   duplicate); if none exists, it drafts a reply for you.
4. Picks the 5 most important emails from everything reviewed and
   summarises them. If one describes a dated event, the digest includes
   an "Add to Calendar" link that opens Google Calendar with the event
   pre-filled, one click from saved.
5. Emails you (or whoever you set as the recipient) one HTML digest with:
   boxed counts up top, **Needs a reply** (with draft links), **Good to
   know** (top 5 + calendar links), **Sorted**, and **Inbox — no good
   label match**.

## The dashboard (port 4568)

- **Connect / disconnect Gmail accounts** - click a button, sign in to
  Google, done. No manual token files.
- **Per-account digest recipient**, editable any time.
- **Run now** - trigger an immediate sort+digest for one account or all of
  them, without waiting for the schedule.
- **Settings** - daily run time, label-match confidence threshold, and a
  list of labels the AI should never sort into.
- **Last run status** per account (counts, or an error if something went
  wrong).

## Why this needs one piece of setup outside the NAS: HTTPS

Being fully honest about the one thing that can't be automated away:
Google **requires OAuth sign-ins to redirect back over HTTPS**, to a real
domain name - it will not send you back to a plain `http://` address on
your home network. That's a Google security rule, not a limitation of
this project, and it's why "Connect a Gmail account" has to happen via a
public HTTPS URL rather than your NAS's local IP.

The good news: Asustor NAS have this built in for free (EZ-Connect +
Certificate Manager + Reverse Proxy - see section 3 below), so it's a
one-time, ~15 minute setup, not an ongoing chore. Once it's done, the
dashboard itself works fine over plain `http://<nas-ip>:4568/` on your
home network for everyday use (settings, run-now, status) - the public
HTTPS URL is only strictly needed for the "Connect a Gmail account"
button.

**Security note**: this means your NAS becomes reachable from the public
internet on whatever port you choose in the Reverse Proxy step. The
dashboard is password-protected (`DASHBOARD_PASSWORD`) specifically
because of this - use a long, random, unique password. Everything else on
your NAS stays exactly as exposed (or not) as it is today; only the one
container/port you explicitly proxy is affected.

---

## 1. Choosing an AI provider (you asked for free, no card required)

**Recommendation: Google Gemini API free tier.** Unlike Anthropic's and
OpenAI's free allowances (one-time trial credits that expire), Gemini's
free tier is ongoing and rate-limited rather than credit-limited, and
signup genuinely doesn't require a card. Get a key at
<https://aistudio.google.com/apikey>.

**Trade-off worth knowing**: Google's terms state that free-tier API
traffic (unlike paid-tier) may be used to improve their products - and
the input here is your email content. If that bothers you, two options
without changing any code:
- Enable billing on the same Google Cloud project and use a paid-tier key
  instead (cheap at this volume - a few cents/month) to get the
  no-training-on-your-data terms.
- Swap in a different `AiClient` implementation later (Groq's free tier
  is the next-best free option, using open models like Llama; happy to
  wire that in if you'd rather).

## 2. Google Cloud OAuth setup (one client, used for every account)

1. Go to <https://console.cloud.google.com/>, create a project (or reuse
   one) - e.g. "gmail-ai-sorter".
2. **APIs & Services > Library**: search for "Gmail API" and enable it.
3. **APIs & Services > OAuth consent screen**: choose "External", fill in
   the required fields (app name, your email). Leave it in "Testing"
   status - you don't need Google's review for personal use. Under "Test
   users", add every Gmail address you plan to connect (Google restricts
   sign-in to listed test users while a consent screen is in Testing
   status).
4. **APIs & Services > Credentials > Create Credentials > OAuth client
   ID**. Application type: **Web application** (not Desktop app - the
   in-app connect flow needs a proper redirect URI).
5. Under **Authorized redirect URIs**, add exactly:
   `<your public HTTPS URL>/oauth/callback` - e.g.
   `https://joe123.myasustor.com:8443/oauth/callback`. This must match
   `PUBLIC_BASE_URL` (below) exactly, including the port.
6. Save, then copy the **Client ID** and **Client secret** - these go into
   `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in the stack's environment
   variables. One client covers both Gmail accounts - you'll click
   "Connect a Gmail account" twice from the dashboard, once signed into
   each.

## 3. Expose the NAS over HTTPS (Asustor EZ-Connect + Reverse Proxy)

Skip this if you already have a domain/HTTPS route to the NAS - just use
that instead of `myasustor.com` below.

1. **ADM > EZ-Connect** (or **Settings > EZ-Connect**): register a free
   `myasustor.com` subdomain if you don't already have one, e.g.
   `joe123.myasustor.com`. This is your DDNS hostname - it follows your
   home IP even if it changes.
2. **ADM > Settings > Security > Certificate Manager** (menu wording
   varies slightly by ADM version): add a new certificate, choose
   **Let's Encrypt**, and issue it for your `myasustor.com` hostname. It
   auto-renews.
3. **ADM > Settings > Services > Reverse Proxy**: add a new proxy domain:
   - Protocol: HTTPS, Server name: your `myasustor.com` hostname, Port:
     an unused external port (e.g. `8443` - avoid `443` if something else
     already uses it), certificate: the one from step 2.
   - Under that proxy domain, add a rule routing to: Protocol HTTP,
     Hostname/IP: the NAS's own LAN IP (or `localhost`), Port `4568`.
   - This is exactly the pattern Asustor's own docs use for exposing
     Docker apps like Jellyfin/Nextcloud - see
     <https://www.asustor.com/en/online/online_help?id=75>.
4. **Router**: forward the external port you chose (e.g. `8443`, TCP) to
   the NAS's LAN IP on that same port, so traffic from the internet
   actually reaches the NAS.
5. Your `PUBLIC_BASE_URL` is now `https://joe123.myasustor.com:8443` (no
   trailing slash) - use this exact value in both the Google Cloud
   redirect URI (step 2.5 above) and the stack's environment variables.
   Test it in a browser before deploying the stack - you should get a
   connection refused/404 (nothing's listening on 4568 yet), not a
   certificate warning or DNS failure.

## 4. Deploy the stack - entirely from Portainer, no SSH

**Why this needs a small detour through GitHub**: Portainer's "Web
editor" and "Upload" stack methods only ever see the one
`docker-compose.yml` file you paste or select - they never receive the
`Dockerfile`/`app/` source sitting next to it, so `build:` always fails
that way (`open Dockerfile: no such file or directory`), no matter how
you submit it. Portainer's **"Repository"** method is different: point it
at a git repo URL and it clones the *whole* folder onto the NAS itself
before building - Dockerfile, `app/`, everything - so `build:` works
correctly, and the whole thing happens by clicking through the Stacks UI.
No terminal, no `docker build` command. The only extra step is getting
this folder into a repo first, which is also just a website.

**Step A - put the project on GitHub (web only, no git command line).**
1. Go to <https://github.com/new>, create a free account if you don't
   have one, and create a new repository (e.g. `gmail-ai-sorter`). Public
   is fine and simplest - nothing secret is in this code; every secret
   (API keys, password) is entered separately in Portainer, never
   committed to the repo.
2. On the new repo's page, click **"uploading an existing file"** (or
   **Add file > Upload files**). Drag in every file and folder from the
   extracted `gmail-ai-sorter/` folder (`Dockerfile`, `requirements.txt`,
   `README.md`, `docker-compose.yml`, `.env.example`, and the `app/`
   folder with its `.py` files) and commit. GitHub's uploader preserves
   the `app/` folder structure as long as you drag the folder itself
   rather than picking files one by one.
3. Copy the repo's URL from your browser's address bar, e.g.
   `https://github.com/yourusername/gmail-ai-sorter`.

**Step B - deploy the stack in Portainer, "Repository" method.**
1. **Stacks > Add stack**, name it, and under **Build method** choose
   **Repository** (not Web editor/Upload).
2. **Repository URL**: paste the GitHub URL from Step A. **Repository
   reference**: leave as the default branch (usually `main`).
   **Compose path**: `docker-compose.yml`.
3. Scroll to **Environment variables**, switch to "Advanced mode" (a
   plain textarea), and paste in every line from `.env.example` with real
   values filled in: `GEMINI_API_KEY`, `GOOGLE_CLIENT_ID`,
   `GOOGLE_CLIENT_SECRET`, `PUBLIC_BASE_URL`, `DASHBOARD_PASSWORD`. That's
   the entire deploy-time configuration.
4. Deploy. Portainer clones the repo onto the NAS and builds the image
   there - since that's the NAS's own ARM64 engine, the image comes out
   ARM64 automatically, no cross-build flags needed. The first build
   takes a minute or two; watch progress under the stack's logs.

**Updating later**: change files in the GitHub repo (edit directly on
github.com, or re-upload changed files the same drag-and-drop way), then
in Portainer go to **Stacks > gmail-ai-sorter > Pull and redeploy** (or
"Update the stack" with "re-pull image" / "re-clone" ticked, wording
varies slightly by version) - it re-clones the repo and rebuilds, still
with no terminal involved.

## 5. First run

1. Open `PUBLIC_BASE_URL` in a browser (e.g.
   `https://joe123.myasustor.com:8443/`), log in with
   `DASHBOARD_PASSWORD`.
2. Click **+ Connect a Gmail account**, sign in with the first account,
   approve access. You're bounced back to the dashboard showing it
   connected. Repeat for the second account (sign out of Google or use an
   incognito window so the picker offers the other account).
3. Adjust **Settings** if you want (run time, confidence threshold,
   ignore list), and set each account's digest recipient if you want the
   digest to land somewhere other than the account's own inbox.
4. Click **Run now** on an account rather than waiting for the schedule,
   then refresh in a minute or two. Check: did the digest email arrive?
   Did a couple of inbox emails get labelled and archived? Does a "Needs
   a reply" item show a working draft link?

From here on, day-to-day use of the dashboard (status, settings, run-now)
works equally well at `http://<nas-lan-ip>:4568/` on your home network -
only connecting a *new* Gmail account needs the public HTTPS URL.

## 6. Known caveats

- **Gmail permalinks** (`.../#all/<id>` and `.../#drafts/<id>`) open in
  browser account slot `u/0` by default. If a link opens the wrong signed-
  in Google account in your browser, edit the `0` in the URL to match
  which slot that address actually is for you.
- **"Add to Calendar" links** open `calendar.google.com` with the event
  pre-filled, ready for one click to save - a plain ordinary hyperlink,
  so it needs no server hosting and isn't stripped by Gmail's sanitizer
  the way a `data:` link would be. This assumes you use Google Calendar;
  say the word if you'd like an alternative for a different calendar app.
- **Two accounts share one Gemini free-tier quota** (one `GEMINI_API_KEY`
  configures the whole stack) - fine for typical personal mailbox
  volumes, but if you hit rate limits, ask and I'll add support for a
  per-account key.
- **Internet exposure**: the dashboard is reachable from the public
  internet once `PUBLIC_BASE_URL` is live. It's password-gated, but treat
  that password like any other credential - long, random, not reused.
- **Relationship to the existing Cowork digest**: leave that scheduled
  task running until you've confirmed this one works reliably for a few
  days, then let me know and I'll turn the Cowork one off so you're not
  getting two digests.

## 7. Project layout

```
gmail-ai-sorter/
  Dockerfile
  docker-compose.yml          # Portainer stack
  requirements.txt            # container runtime deps
  .env.example                # template for Portainer's Environment variables box
  app/
    main.py                   # entry point: scheduler thread + dashboard server
    config.py                 # bootstrap secrets (env vars, set once)
    settings_store.py         # everything the dashboard edits, persisted to /data
    oauth_web.py               # in-app Google OAuth connect flow
    web_app.py                 # Flask dashboard (port 4568)
    pipeline.py                # the actual sort+digest run for one account
    gmail_client.py            # Gmail REST API wrapper + OAuth refresh
    ai_client.py                # Gemini REST API wrapper
    digest_builder.py          # HTML digest matching the spec sections
    ics_builder.py              # Google Calendar link builder
    state_store.py             # once-per-day guard across restarts
```
