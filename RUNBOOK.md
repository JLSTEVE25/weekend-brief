# Weekend Brief — Runbook

Plain-English instructions for the things that go wrong, in the order you'll
care about them. Ignore the rest of the repo; just read this when something
breaks.

---

## "I got an email saying a workflow failed"

1. Open the link in the email — it lands you on the failed GitHub Actions run.
2. Click the red ❌ step. The error is usually in the last few lines.
3. Match it to one of the sections below.

If no email arrived but you suspect something's wrong, go to
`https://github.com/JLSTEVE25/weekend-brief/actions` and look at the most
recent run for either workflow.

---

## "The Monday brief didn't show up"

**First check:** open `https://jlsteve25.github.io/weekend-brief/`. If the page
is there but stale (last week's date), the workflow ran but the publish step
failed. If the page won't load at all, GitHub Pages itself is the issue —
check `https://www.githubstatus.com`.

**If the workflow failed:**
1. Go to Actions → "Generate Weekend Brief" → most recent run.
2. Read the error (see error sections below).
3. Re-run after fixing: hit the **Run workflow** button on the workflow page.
   Manual runs ignore the DST gate, so they'll execute immediately.

---

## "The Thursday recap email didn't arrive"

It might be working as designed: if there's no feedback at all from the past
7 days, the script now sends a "no feedback this week" email. Check spam.

If the workflow itself failed, follow the same steps as the Monday brief.

---

## Common errors and how to fix them

### `KeyError: 'GOOGLE_REFRESH_TOKEN'` or `invalid_grant`

The Google refresh token has been revoked or expired. To fix:

1. On your Mac, in this repo:
   ```bash
   cd ~/Desktop/Claude\ Workspace/code/weekend-brief
   python get_google_token.py
   ```
2. It opens a browser → sign in → approve → it prints a new refresh token.
3. Go to GitHub → repo Settings → Secrets and variables → Actions →
   update `GOOGLE_REFRESH_TOKEN` with the new value.
4. Re-run the failed workflow.

### `anthropic.AuthenticationError` or `401`

Your Anthropic API key was revoked or rotated.

1. Sign in to `https://console.anthropic.com`.
2. Go to API Keys, generate a new one.
3. Update the `ANTHROPIC_API_KEY` GitHub secret.
4. Re-run.

### `requests.HTTPError: 401` from Airtable

The Airtable Personal Access Token is bad.

1. Sign in to `https://airtable.com/create/tokens`.
2. Either rotate the existing token or create a new one with the same scopes
   (data.records:read on the four bases this project uses).
3. Update the `AIRTABLE_API_KEY` GitHub secret.
4. Re-run.

### `Cache_control` or model-not-found from Claude

Anthropic deprecated or renamed the model. Set `CLAUDE_MODEL` as a repo
variable (Settings → Secrets and variables → Actions → Variables tab) to a
current model id (e.g. `claude-sonnet-4-7`). The code reads it at runtime.

---

## Costs to keep an eye on

Anthropic is the only paid service. Default model is **Sonnet 4.6** with prompt
caching turned on. Expected spend: roughly **$1–2/month**.

To check actual spend: console.anthropic.com → Usage. If it's drifting above
$5/month, something has gone wrong (probably a runaway prompt). Look at the
"Tokens" line in recent workflow logs and compare to baseline (~10K in / ~5K out
on the Monday brief; tiny on the Thursday recap).

---

## Manual operations

### Trigger a brief right now (without waiting for Monday)

GitHub → Actions → "Generate Weekend Brief" → **Run workflow** button.
Manual runs skip the DST hour-gate.

### Trigger a recap right now

Same idea: Actions → "Thursday Recap Email" → **Run workflow**.

### Run locally (dry-run on your laptop)

You'll need the same env vars set in your shell. Easiest: copy them from
GitHub Secrets into a local `.env` you don't commit, then:
```bash
cd ~/Desktop/Claude\ Workspace/code/weekend-brief
set -a && source .env && set +a
python generate_brief.py
```
That writes a fresh `index.html` you can open directly in a browser. Don't
commit the `.env`.

---

## Configuration overrides (advanced)

Everything below is set via env vars. Set them in GitHub repo Settings →
Secrets and variables → Actions → Variables tab (not Secrets — Variables are
for non-sensitive overrides).

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Which Anthropic model to call. |
| `WB_RADAR_CAP` | `40` | Max "Coming Up" events sent to Claude. |
| `WB_CAL_JOHN` | john's gmail | Override John's calendar id. |
| `WB_CAL_SARA` | sara's gmail | Override Sara's calendar id. |
| `WB_CAL_FAMILY` | family cal | Override family calendar id. |
| `WB_AT_RESTAURANTS_BASE` | `appyUA9SEI4R0grrH` | Restaurants Airtable base. |
| `WB_AT_EVENTS_BASE` | `appQEVLUQt03RUIgE` | Events base. |
| `WB_AT_FRIENDS_BASE` | `appTGMNTmT9weRbjL` | Friends base. |
| `WB_AT_FEEDBACK_BASE` | `appvI8vByeBsxegHZ` | Feedback log base. |
| `WB_AT_TABLE_NAME` | `Imported table` | Table name inside the three content bases. |
| `WB_AT_FEEDBACK_TABLE` | `Feedback Log` | Table name inside the feedback base. |
| `ALERT_RECIPIENT` | `jlstevenson2@gmail.com` | Who gets the failure email. |
