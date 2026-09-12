# Gmail AI Sorter

A self-hosted, free-tier daily Gmail sorter + digest with its own web
dashboard, built to run as a Docker/Portainer stack on your Asustor
AS1102T NAS (ARM64). It replicates (and is meant to eventually replace)
the Gmail-sorting digest currently running as a Claude Cowork scheduled
task, but running entirely under your own control with no per-message
cost.

**Deploy once, then everything else happens in the browser.** Deploy the
stack with no environment variables set at all if you like - the
dashboard boots anyway and walks you through a one-time **Setup page**
for the five secrets it needs (Gemini key, Google OAuth client, public
URL, dashboard password) right there in the browser. Prefer to set them
in Portainer's stack environment variables up front instead? That works
too - either way, once they're in place you open the dashboard and click
"Connect a Gmail account". No scripts to run, no files to copy onto the
NAS, no `.env` file to hunt for.

## What it does, once a day (or once a week - see below), per connected Gmail account

1. Looks at everything currently in the inbox and asks Gemini to match
   each email to one of the labels **already in your account** (it never
   creates new labels). Confident matches get that label applied and are
   archived out of the inbox. Low-confidence ones are left in the inbox.
2. Sweeps every other label ("folder") for new mail - unread mail only if
   this account runs daily, or everything from the last 7 days regardless
   of read state if it runs weekly (see "Daily vs. weekly" below) - reads
   it, and marks it read.
3. Flags anything that needs a personal reply. For each one, it checks
   Gmail for an existing draft on that thread first (never creates a
   duplicate); if none exists, it drafts a reply for you.
4. Picks the 5 most important emails from everything reviewed (excluding
   anything that went to the School section below, so nothing shows up
   twice) and summarises them. If one describes a dated event, the digest
   includes a real "Add to Calendar" link - tapping it opens the native
   calendar app on whatever device you're reading the digest on (iPhone's
   Calendar included), rather than a Google-Calendar-specific web page.
5. Separately, runs its own recent-window search of any label whose name
   *contains* one of your configured School keywords (default just
   `school`, matched case-insensitively anywhere in the label's full
   name/path - so a nested label like `Family/School` or a differently
   worded one like `School - Yeomoor Wood` is picked up automatically,
   not just a label named exactly "School") and picks the top 3 from
   those, same summary/event/calendar-link treatment as above - so school
   mail always
   gets its own spotlight rather than competing for a slot in step 4's
   general importance ranking. This is a "what's coming up" reminder, not
   part of the sort/triage above - the messages it surfaces aren't marked
   read or re-labelled just for showing up here. How far back it looks is
   its own setting (`school_lookback_days`, default 14) and doesn't change
   with the daily/weekly setting below - a weekly account still gets the
   same School lookback window it's configured for.
6. Emails you (or whoever you set as the recipient) one HTML digest with:
   boxed counts up top, **Needs a reply** (with draft links), **Good to
   know** (top 5 + calendar links), **School** (top 3 + calendar links),
   **Sorted**, and **Inbox — no good label match**.

### Daily vs. weekly

Run time, frequency, and weekly run day are all set **per Gmail account**,
on that account's own card on the dashboard - there's no global default to
fall back to. A newly-connected account starts out daily at 07:00; change
it to **weekly** and a day-of-week picker appears right there on the card.
Set the run time to `08:00` and the weekly day to `Monday`, and that
account's sort+digest fires every Monday at 8am, not "roughly once a week
since it last ran".
When it does fire, its labelled-folder sweep (step 2 above) looks at a
full week of mail regardless of read state, rather than only what's
unread, since a weekly account isn't checked in between runs. The School
section (step 5) is deliberately **not** affected by any of this - it's a
reminder feature, not part of the sort/triage sweep, so its own lookback
window (`school_lookback_days` in Settings) stays whatever you've set it
to regardless of daily/weekly or which day is chosen.

### Add to Calendar links (iPhone-friendly)

Any event the AI finds - in "Good to know" or "School" - gets an "Add to
Calendar" link built from a real `.ics` file rather than a
`calendar.google.com` web link (what earlier versions of this project
used). The dashboard serves that `.ics` file from a small, unauthenticated
`/ics/<token>` link - deliberately reachable without logging into the
dashboard first, since the person tapping it is reading an email, not
using the dashboard. Tapping it opens the native "Add to Calendar" sheet
on the device you're reading it on: Apple's Calendar app on iPhone/iPad/
Mac, or whatever calendar app is registered on Android/desktop. The token
in the link is an unguessable random ID (not sequential, not derived from
anything else) - anyone who has that exact link can see that one event's
title/time/location, which is the same practical exposure as the old
Google Calendar link already had (it put those same details directly in
a public URL). Event files are kept for 90 days and cleaned up
automatically after that.

## The dashboard (port 4568)

- **Connect / disconnect Gmail accounts** - click a button, sign in to
  Google, done. No manual token files.
- **Run now** - trigger an immediate sort+digest for one account or all of
  them, without waiting for the schedule.
- **Per-account settings, one Save button** - each Gmail account's own
  card has its digest recipient, run time, daily/weekly frequency, and
  (only shown once you pick Weekly) which day of the week it fires on, all
  in one form with a single Save button at the bottom. No global schedule
  to keep in sync - just set each account the way you want it. The button
  reads "Saved" in green until you change something on that card, then
  turns orange as a reminder there are unsaved changes - press it again to
  save and it goes back to green.
- **Settings** - label-match confidence threshold, a list of
  labels the AI should never sort into, a list of keywords that route a
  label to its own
  dedicated "School" digest section (defaults to `School`, matched as a
  substring anywhere in a label's full name/path - so it also catches a
  nested label or a differently-worded one, not just a label named
  exactly "School" - add more keywords, comma-separated, for anything else
  that should get the same own-section treatment rather than competing in
  the general "Good to know" ranking), and how many days back the School
  section itself checks (default 14 -
  set it to whatever window makes sense as a reminder, e.g. 21 for three
  weeks, independent of each account's own run frequency above).
- **Last run status** per account (counts, or an error if something went
  wrong).

## Why this needs one piece of setup outside the NAS: HTTPS

Being fully honest about the one thing that can't be automated away:
Google **requires OAuth sign-ins to redirect back over HTTPS**, to a real
domain name - it will not send you back to a plain `http://` address on
your home network. That's a Google security rule, not a limitation of
this project, and it's why "Connect a Gmail account" has to happen via a
public HTTPS URL rather than your NAS's local IP.

The good news: this is a one-time setup, not an ongoing chore, and
section 3 below covers three ways to do it:

- **Tailscale Funnel (recommended, £0)** - `tailscale`/`tailscaled` are
  bundled into this project's own image and run as background processes
  the app manages itself, making an outbound-only connection so there's no
  router port-forwarding, no domain to buy, and no certificate to issue or
  renew - Tailscale handles all of that, and the auth key that turns it on
  can be typed straight into the dashboard's Setup page, same as every
  other secret. This is the option that needed the least fighting with
  routers and DNS in practice.
- **Cloudflare Tunnel** - `cloudflared` is bundled the same way. Also no
  router port-forwarding and no certificate to manage, but it needs a
  domain name you own (a cheap one is fine, a few dollars a year) and its
  DNS pointed at Cloudflare (free, no card needed) - worth it if you
  already have a domain, or want a URL under your own name rather than a
  `.ts.net` one.
- **Asustor's own EZ-Connect + Certificate Manager + Reverse Proxy** - no
  domain purchase needed (uses a free DDNS subdomain instead), but you're
  managing the certificate and a router port forward yourself, the Let's
  Encrypt step there can be finicky, and EZ-Connect's DNS can end up
  pointing at Asustor's own relay servers rather than your home IP
  depending on how it auto-configures - see Route B below for the details
  and workaround.

Once whichever route is set up, the dashboard itself works fine over
plain `http://<nas-ip>:4568/` on your home network for everyday use
(settings, run-now, status) - the public HTTPS URL is only strictly
needed for the "Connect a Gmail account" button.

**Security note**: either route means your NAS dashboard becomes
reachable from the public internet. It's password-protected
(`DASHBOARD_PASSWORD`) specifically because of this - use a long, random,
unique password. Everything else on your NAS stays exactly as exposed (or
not) as it is today; only this one container is affected.

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

## 3. Expose the NAS over HTTPS

Three routes - pick one. **Tailscale Funnel (Route C) is recommended**: £0,
no router changes, no domain to buy, no certificate to issue or renew, and
it sidesteps the Asustor Certificate Manager's Let's Encrypt errors
entirely (a known rough edge - "ACME Client has encountered an issue" /
"Ref. 5401"/"Ref. 5056" are all the same underlying HTTP-validation
problem, not something wrong with your NAS - see Route B below if you want
the details anyway).

### Route A: Cloudflare Tunnel (needs a domain you own)

**What you need first**: a domain name you own. If you don't have one
already, the simplest path is buying one directly through Cloudflare
itself (typically $4-15/year depending on the extension - `.com` is
around $10) so registration and DNS live in the same place with nothing
extra to configure. If you already own a domain anywhere else (Namecheap,
GoDaddy, etc.), you can use that instead - step 2 below covers it.

1. **Create a free Cloudflare account** at <https://dash.cloudflare.com/sign-up>
   - no credit card needed for this.
2. **Add your domain to Cloudflare**:
   - If you just bought the domain through Cloudflare Registrar, this is
     automatic - skip to step 3.
   - If you already own a domain elsewhere: in the Cloudflare dashboard,
     **Add a site**, enter your domain, choose the Free plan. Cloudflare
     shows you two nameservers (e.g. `aida.ns.cloudflare.com`). Log into
     wherever you registered the domain, find its DNS/nameserver
     settings, and replace the existing nameservers with the two
     Cloudflare gives you. You're only moving DNS management, not the
     registration itself. This can take anywhere from a few minutes to a
     few hours to take effect - Cloudflare's dashboard shows the domain
     as "Active" once it has.
3. **Open Zero Trust** (left sidebar of the Cloudflare dashboard, or
   <https://one.dash.cloudflare.com/>) - free, no card needed for this
   either. Go to **Networks > Tunnels > Create a tunnel**, choose
   **Cloudflared**, and name it (e.g. `gmail-ai-sorter`).
4. On the next screen, under **Install and run a connector**, ignore the
   install commands shown (those are for installing `cloudflared`
   directly on a machine) - you just need the **tunnel token**, a long
   string shown further down the page or under the tunnel's "Configure"
   tab afterwards. Copy it.
5. Still in that tunnel's settings, go to the **Public Hostname** tab and
   add a route:
   - Subdomain: anything you like, e.g. `gmail`
   - Domain: pick your domain from the dropdown
   - Type: `HTTP`, URL: `localhost:4568` - `cloudflared` runs as a
     background process directly inside the same container as the
     dashboard (see "How the tunnel actually runs" below), not as a
     separate one, so it reaches the dashboard on `localhost` rather than
     over Docker's inter-container networking.
   - Save.
6. Paste that tunnel token into `CLOUDFLARE_TUNNEL_TOKEN` - either in
   Portainer's stack environment variables (deploy the stack first if you
   haven't yet - see section 4 - then edit the environment variables and
   redeploy), **or** into the dashboard's own **Setup page** at `/setup`
   (reachable anytime, even after first-run setup is done) if the stack's
   already running. The Setup page route takes effect within about 15
   seconds with no redeploy at all - it's the easier option if you're
   just adding or changing the tunnel token later.
7. Your `PUBLIC_BASE_URL` is `https://gmail.yourdomain.com` (whatever
   subdomain/domain you chose in step 5, no trailing slash). Test it in a
   browser after redeploying (or after ~15 seconds if you used the Setup
   page) - it should load the dashboard's Setup or login page directly
   over HTTPS, with a valid certificate Cloudflare issued automatically.
   No router port-forwarding was needed anywhere in this process.

**How the tunnel actually runs**: rather than a second Docker container,
`cloudflared` is bundled into this project's own image and runs as a
background process supervised from inside `main.py` (see
`tunnel_manager.py`) - the moment `CLOUDFLARE_TUNNEL_TOKEN` is present
(from either source in step 6), it starts that process automatically; if
you clear the token later, it stops it. This is also why the "Save and
continue"/Setup page can control it directly with no Docker socket access
and no redeploy: it's just another background thread in the same
container as the dashboard, reacting to the same `/data/bootstrap.json`
file the rest of the Setup page already writes to.

### Route B: Asustor's own EZ-Connect + Certificate Manager + Reverse Proxy

Skip this if you've done Route A or C. **This route turned out to be the
most fiddly in practice** - the notes below include the real problems hit
while setting this up, not just the happy path.

1. **ADM > EZ-Connect** (or **Settings > EZ-Connect**): register a free
   DDNS subdomain if you don't already have one. The exact domain suffix
   Asustor hands out has changed over time and can vary by account/region
   (`.myasustor.com` in older docs, `.ezconnect.to` currently seen) - check
   your own EZ-Connect settings page for the exact hostname it actually
   gives you (shown next to "ADM:", e.g. `http://yourcloudid.ezconnect.to`)
   rather than assuming either suffix. This is your DDNS hostname - it's
   meant to follow your home IP even if it changes.
   - **Known issue**: if the EZ-Connect wizard's "EZ-Router" step fails
     (e.g. "No UPnP/NAT-PMP router found") but "Internet Passthrough"
     reports success anyway, EZ-Connect may fall back to routing your
     hostname through **Asustor's own cloud relay** rather than pointing
     DNS directly at your home IP. You can check this yourself: look up
     your hostname on <https://dnschecker.org> (record type A) and compare
     the IP it shows against your own public IP (check
     <https://whatismyip.com> on mobile data, not home WiFi). If they
     don't match - especially if the resolved IP belongs to a cloud
     provider like AWS rather than your ISP - the certificate/port-80
     validation steps below cannot work no matter how correctly your
     router is configured, because requests to that hostname never reach
     your NAS at all. If you hit this, either try enabling UPnP on your
     router and re-running the EZ-Connect wizard (so it can set up direct
     mode instead of relay mode), or switch to a plain third-party DDNS
     provider instead - **DuckDNS** (duckdns.org, free, no relay) is a
     simple option: create a subdomain there, then check whether your
     NAS's DDNS settings screen (**Settings > EZ-Connect > DDNS** or
     **Settings > Network > DDNS**, depending on ADM version) lists it (or
     "Custom") as a provider so the NAS keeps it updated automatically.
     Use whichever hostname you end up with (`....ezconnect.to` or
     `....duckdns.org`) for every step below.
2. **ADM > Settings > Security > Certificate Manager** (menu wording
   varies slightly by ADM version): add a new certificate, choose
   **Let's Encrypt**, and issue it for your hostname from step 1. It
   auto-renews (tick "Update automatically when certificates expire").
   - **Domain name field**: enter the *bare full hostname* only - no
     `http://` prefix, no path, no trailing slash (e.g.
     `yourcloudid.ezconnect.to`, not `http://yourcloudid`) - a stray
     prefix or a truncated entry are both common causes of "Unable to
     apply settings (Ref. 5401)" / "...is invalid... (Ref. 5056)".
   - **Port 80 must be forwarded** from your router to the NAS's LAN IP
     before you click Finish - Let's Encrypt briefly connects over plain
     HTTP to verify you own the domain, and this fails (producing the
     same Ref. 5401/5056 errors) if port 80 isn't reachable from the
     internet at that moment, or if your home network has a double-NAT
     setup (e.g. an ISP modem/router in front of your own router, both
     doing NAT) - in that case port forwarding on just one of them isn't
     enough; either put the ISP device into "modem"/bridge mode so only
     your own router does NAT, or forward the port on both hops. It needs
     to stay forwarded afterwards too, since the certificate renews
     itself the same way periodically.
   - Testing port 80 directly (from outside your own network) with
     <https://canyouseeme.org> is a fast way to check this in isolation
     from the certificate wizard: "Connection refused" there means
     traffic *is* reaching your NAS but nothing's listening on port 80 at
     that exact moment - normal outside an active certificate request,
     since Asustor only opens port 80 briefly during validation itself.
     "Connection timed out" instead usually means the traffic never
     leaves your network at all (check port forwarding, double-NAT, or
     ask your ISP whether you have a genuine static public IP vs. a
     shared/CGNAT one, which cannot be port-forwarded).
   - If it still fails after all of the above check out, confirm the
     hostname actually resolves to your real public IP (see the DNS check
     under step 1) and that it attempts a connection (rather than a
     DNS/"server not found" error) when opened from your phone on mobile
     data.
3. **ADM > Settings > Services > Reverse Proxy**: add a new proxy domain:
   - Protocol: HTTPS, Server name: your hostname from step 1, Port: an
     unused external port (e.g. `8443` - avoid `443` if something else
     already uses it), certificate: the one from step 2.
   - Under that proxy domain, add a rule routing to: Protocol HTTP,
     Hostname/IP: the NAS's own LAN IP (or `localhost`), Port `4568`.
   - This is exactly the pattern Asustor's own docs use for exposing
     Docker apps like Jellyfin/Nextcloud - see
     <https://www.asustor.com/en/online/online_help?id=75>.
4. **Router**: forward the external port you chose (e.g. `8443`, TCP) to
   the NAS's LAN IP on that same port, so traffic from the internet
   actually reaches the NAS.
5. Your `PUBLIC_BASE_URL` is now `https://<your hostname>:8443` (no
   trailing slash) - use this exact value in both the Google Cloud
   redirect URI (step 2.5 above) and the stack's environment variables.
   Test it in a browser before deploying the stack - you should get a
   connection refused/404 (nothing's listening on 4568 yet), not a
   certificate warning or DNS failure.

### Route C: Tailscale Funnel (recommended, £0, no router changes)

1. **Create a free Tailscale account** at <https://login.tailscale.com/start>
   (sign in with Google/GitHub/Microsoft - no card needed) if you don't
   already have one.
2. **Generate a reusable auth key**: go to
   <https://login.tailscale.com/admin/settings/keys> > **Generate auth
   key**. Turn **Reusable** on (so the same key still works if the
   container ever needs to re-authenticate) and leave **Ephemeral** off
   (so the node - and its URL - stays permanent rather than disappearing
   when offline). Copy the key (starts `tskey-auth-...`) - it's only shown
   once.
3. Paste that key into `TAILSCALE_AUTH_KEY` - either in Portainer's stack
   environment variables (deploy the stack first if you haven't yet - see
   section 4 - then edit the environment variables and redeploy), **or**
   into the dashboard's own **Setup page** at `/setup` (reachable anytime)
   if the stack's already running. The Setup page route takes effect
   within about 15 seconds with no redeploy needed. Optionally also set
   `TAILSCALE_HOSTNAME` (defaults to `gmail-ai-sorter` if left blank) - it
   becomes part of your public URL.
4. Within about 15-30 seconds of the key being picked up, check the
   container's logs (Portainer > Stacks > gmail-ai-sorter > Logs) for a
   line like `Tailscale Funnel is live: https://gmail-ai-sorter.<your-
   tailnet>.ts.net` - that's your public URL.
5. Your `PUBLIC_BASE_URL` is that exact URL (no trailing slash, no port
   needed - Funnel serves over standard HTTPS). Use it in both the Google
   Cloud redirect URI (step 2.5 above) and `PUBLIC_BASE_URL`. No router
   port-forwarding, no certificate, no domain purchase, and the URL stays
   the same across container restarts as long as the `/data` volume
   persists (see section 7).

**How this actually runs**: `tailscale`/`tailscaled` are bundled into this
project's own image and run as background processes supervised from
inside `main.py` (see `tailscale_manager.py`) - the moment
`TAILSCALE_AUTH_KEY` is present, it starts the daemon, joins your tailnet,
and turns on Funnel for the dashboard's port automatically; clearing the
key later stops it. `tailscaled` runs in Tailscale's "userspace
networking" mode specifically, which needs no special container
permissions or devices at all - real kernel-mode networking would need a
`/dev/net/tun` device on the NAS's own kernel, which many NAS/embedded
Linux builds (the AS1102T included) simply don't provide, so it's not an
option here. The one thing userspace mode does need is for the container
to run as root rather than a dropped-privilege user - some Tailscale
versions have a known bug where Funnel's TLS handshake silently fails in
userspace mode under a non-root user, so this image stays root to avoid
it. A reasonable trade-off for a single-purpose container on your own
private NAS.

## 4. Deploy the stack - entirely from Portainer, no SSH

**Why this needs a small detour through GitHub**: Portainer's "Web
editor" and "Upload" stack methods only ever see the one
`docker-compose.yml` file you paste or select - they never receive the
`Dockerfile`/`.py` files sitting next to it, so `build:` always fails
that way (`open Dockerfile: no such file or directory`), no matter how
you submit it. Portainer's **"Repository"** method is different: point it
at a git repo URL and it clones the *whole* folder onto the NAS itself
before building - Dockerfile, every `.py` file, everything - so `build:`
works correctly, and the whole thing happens by clicking through the
Stacks UI. No terminal, no `docker build` command. The only extra step is
getting this folder into a repo first, which is also just a website.

Every `.py` file in this project sits flat at the top level (no
subfolder) specifically so this upload step can't go wrong: GitHub's
drag-and-drop web uploader doesn't reliably preserve subfolders (an
earlier version of this project had an `app/` subfolder, which is exactly
what broke last time - the files landed at the repo root instead, and the
build failed looking for a folder that wasn't there). With everything
flat, there's no nesting for the upload to lose.

**Step A - put the project on GitHub (web only, no git command line).**
1. Go to <https://github.com/new>, create a free account if you don't
   have one, and create a new repository (e.g. `gmail-ai-sorter`). Public
   is fine and simplest - nothing secret is in this code; every secret
   (API keys, password) is entered separately in Portainer, never
   committed to the repo.
2. On the new repo's page, click **"uploading an existing file"** (or
   **Add file > Upload files**). Drag in *every file* from the extracted
   `gmail-ai-sorter/` folder - all the `.py` files, `Dockerfile`,
   `requirements.txt`, `README.md`, `docker-compose.yml`, `.env.example`
   - and commit. They should all land at the repo's top level, sitting
   next to each other (not inside any subfolder) - if your repo already
   has files from an earlier attempt in a subfolder, delete those first
   (select them, then the "..." menu > Delete files) so old and new
   copies don't end up side by side.
3. Copy the repo's URL from your browser's address bar, e.g.
   `https://github.com/yourusername/gmail-ai-sorter`.

**Step B - deploy the stack in Portainer, "Repository" method.**
1. **Stacks > Add stack**, name it, and under **Build method** choose
   **Repository** (not Web editor/Upload).
2. **Repository URL**: paste the GitHub URL from Step A. **Repository
   reference**: leave as the default branch (usually `main`).
   **Compose path**: `docker-compose.yml`.
3. **Environment variables**: optional. Leave this empty and deploy right
   away if you'd rather configure things afterwards from the browser (see
   the Setup page below) - the stack deploys fine either way. If you'd
   prefer to set them here up front instead, switch to "Advanced mode" (a
   plain textarea) and paste in the lines from `.env.example` you want
   filled in: `GEMINI_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
   `PUBLIC_BASE_URL`, `DASHBOARD_PASSWORD`, and (if using Route C in
   section 3) `TAILSCALE_AUTH_KEY`/`TAILSCALE_HOSTNAME`, or (if using
   Route A) `CLOUDFLARE_TUNNEL_TOKEN`.
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
   `https://gmail-ai-sorter.yourtailnet.ts.net/` if you used Route C).
   - **If you left the environment variables blank in Step B**, you'll
     land on a **Setup page** instead of the login screen. Fill in the
     Gemini key, Google OAuth client ID/secret, public base URL, and a
     dashboard password, and submit - this is saved straight away
     (persisted on the NAS, no redeploy needed) and you're taken to the
     login screen.
   - **If you set them in Portainer already**, you'll go straight to the
     login screen.
   Log in with `DASHBOARD_PASSWORD`.
2. Click **+ Connect a Gmail account**, sign in with the first account,
   approve access. You're bounced back to the dashboard showing it
   connected. Repeat for the second account (sign out of Google or use an
   incognito window so the picker offers the other account).
3. On each account's card, set its run time, daily/weekly frequency (pick
   Weekly to reveal a day-of-week picker), and digest recipient if you
   want the digest to land somewhere other than the account's own inbox.
   Adjust **Settings** if you want (confidence threshold, ignore list,
   School-section labels and lookback days).
4. Click **Run now** on an account rather than waiting for the schedule,
   then refresh in a minute or two. Check: did the digest email arrive?
   Did a couple of inbox emails get labelled and archived? Does a "Needs
   a reply" item show a working draft link?

From here on, day-to-day use of the dashboard (status, settings, run-now)
works equally well at `http://<nas-lan-ip>:4568/` on your home network -
only connecting a *new* Gmail account needs the public HTTPS URL.

## 6. Known caveats

- **`cloudflared` and `tailscale`/`tailscaled` being bundled into the
  image are harmless if you never use one or both of them** - with
  `CLOUDFLARE_TUNNEL_TOKEN` and/or `TAILSCALE_AUTH_KEY` left blank (e.g.
  you went with Route B in section 3 instead), the background thread(s)
  that would manage them just never start a process. No extra resource
  use, no log spam, nothing to disable.
- **This image runs as root inside the container** (not a non-root user)
  - a deliberate trade-off so Tailscale Funnel (Route C) can use real
    kernel networking rather than a less reliable fallback mode. Fine for
    a single-purpose container on your own private NAS; worth knowing if
    you're used to non-root-by-default images.
- **The Setup page is always reachable at `/setup`**, even after it's
  been completed once - handy if a value changes (a rotated Gemini key,
  a new NAS domain) and you'd rather update it from the browser than
  redeploy the stack. A saved change there takes effect immediately.
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

Everything sits flat at the top level - no subfolders - specifically so a
GitHub drag-and-drop upload can't lose any nesting (see section 4):

```
gmail-ai-sorter/
  Dockerfile
  docker-compose.yml          # Portainer stack
  requirements.txt            # container runtime deps
  .env.example                # template for Portainer's Environment variables box
  main.py                     # entry point: scheduler + tunnel manager threads, dashboard server
  config.py                   # bootstrap secrets (env vars, or entered via the Setup page)
  tunnel_manager.py           # supervises the optional Cloudflare Tunnel subprocess
  tailscale_manager.py        # supervises the optional Tailscale Funnel processes
  settings_store.py           # everything the dashboard edits, persisted to /data
  oauth_web.py                # in-app Google OAuth connect flow
  web_app.py                  # Flask dashboard (port 4568)
  pipeline.py                 # the actual sort+digest run for one account
  gmail_client.py             # Gmail REST API wrapper + OAuth refresh
  ai_client.py                 # Gemini REST API wrapper
  digest_builder.py           # HTML digest matching the spec sections
  ics_builder.py               # Google Calendar link builder
  state_store.py              # once-per-day guard across restarts
```
