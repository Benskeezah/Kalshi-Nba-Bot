# Turning on the daily props report

What you get: twice a day, GitHub's servers run the analysis for free, publish
a web page with today's picks, and send a notification to your phone. Nothing
runs on your computer, and nothing can place a trade.

You need about 20 minutes, a free GitHub account, and the free ntfy app.

## 1. Put the code on GitHub
1. Create a free account at github.com.
2. Create a new repository named `Kalshi-Nba-Bot`. Choose **Public**: the free plan only
   publishes web pages from public repositories, so the report page will be visible
   to anyone who has the link.
3. Upload every file from this folder, including the hidden `.github` folder.
   Or have Claude push it for you if your GitHub account is connected.

## 2. Phone notifications (ntfy)
1. Install **ntfy** from the App Store or Google Play (free, no account).
2. Tap **+** and subscribe to a topic name nobody would guess, such as
   `logan-props-7Hq2xT`. Anyone who knows the name can read the alerts.
3. In the repository: **Settings → Secrets and variables → Actions → New repository
   secret**. Name: `NTFY_TOPIC`. Value: your topic name.

## 3. Sportsbook lines (optional, recommended)
1. Get a free key at the-odds-api.com (500 credits per month).
2. Add another secret: `ODDS_API_KEY`.
3. Each run checks lines for at most 2 games (about 6 credits), roughly 360 credits a
   month at two runs a day, inside the free 500. Change `sportsbook_games_per_run`
   in `picks_settings.toml` to adjust. Without a key, picks use the stats model alone.

## Changing what counts as a good prop
Open `picks_settings.toml` on GitHub, tap the pencil icon, change a number, and tap
**Commit changes**. The next run uses it. Each setting is explained in the file.

## 4. Turn on the web page
**Settings → Pages → Build and deployment → Source: GitHub Actions.**
The report lives at `https://benskeezah.github.io/Kalshi-Nba-Bot/`.

## 5. First run
**Actions → Daily NBA props → Run workflow.** The first run downloads last season's
box scores and takes about 20 minutes. Later runs take a few minutes.

## Schedule
7:00 am and 11:30 am Hawaii time (17:00 and 21:30 UTC). Edit the two `cron` lines in
`.github/workflows/daily.yml` to change them. GitHub sometimes starts scheduled runs
10-30 minutes late.

## Reading the report
* **High-probability picks** at the top: sides the model gives at least a 70% chance
  that still cost less than that chance (after fees), even at the cautious end of its range.
* **Track record** at the bottom: how often past picks actually won compared with what the
  model said. Until it shows 50+ settled predictions, treat the probabilities as unproven.
* Injury news after the 11:30 am run is not in the report. Check before you trade.
