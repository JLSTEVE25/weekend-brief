# Passkey Auth Worker — Setup Walkthrough

This is a one-time setup. Everything after deployment runs automatically.

All commands below are run from this folder:
```bash
cd "~/Desktop/Claude Workspace/code/weekend-brief/auth-worker"
```

---

## 1. Create a Cloudflare account

Go to https://cloudflare.com and sign up (free tier is plenty). Verify your email.

## 2. Install Wrangler (Cloudflare's CLI)

```bash
npm install -g wrangler
```

If `npm` isn't found, install Node.js first: https://nodejs.org (pick the LTS version).

## 3. Install this Worker's dependencies

```bash
cd "~/Desktop/Claude Workspace/code/weekend-brief/auth-worker"
npm install
```

## 4. Log in to Cloudflare from the terminal

```bash
wrangler login
```

This opens a browser — click "Allow" to authorize Wrangler.

## 5. Create the KV namespace

```bash
wrangler kv:namespace create "PASSKEY_STORE"
```

It prints something like:
```
🌀 Creating namespace with title "weekend-brief-auth-PASSKEY_STORE"
✨ Success!
Add the following to your configuration file:
[[kv_namespaces]]
binding = "PASSKEY_STORE"
id = "abc123def456..."
```

**Copy the `id` value.** Open `wrangler.toml` and replace `REPLACE_WITH_KV_NAMESPACE_ID` with that id.

## 6. Set the secrets

Pick a registration code (you'll type this once on your phone when registering) and a JWT signing secret (random string, you never see it again).

Good JWT secret command:
```bash
openssl rand -hex 32
```

Set both:
```bash
wrangler secret put REGISTRATION_SECRET
# Paste your chosen code (e.g. "Charlotte2026!") and press enter

wrangler secret put JWT_SECRET
# Paste the openssl output and press enter
```

## 7. Deploy the Worker

```bash
wrangler deploy
```

It prints the Worker URL, something like:
```
Published weekend-brief-auth (1.23 sec)
  https://weekend-brief-auth.your-subdomain.workers.dev
```

**Copy that URL.**

## 8. Add the URL to GitHub and the register page

### GitHub secret
1. Go to https://github.com/JLSTEVE25/weekend-brief/settings/secrets/actions
2. Click "New repository secret"
3. Name: `PASSKEY_AUTH_URL`
4. Value: the Worker URL from step 7
5. Save

### Register page
Open `register.html` in the repo root and replace:
```js
const AUTH_API = 'https://weekend-brief-auth.REPLACE_ME.workers.dev';
```
with your actual Worker URL.

Commit and push both changes.

## 9. Register passkeys

1. On John's iPhone, open: `https://jlsteve25.github.io/weekend-brief/register.html`
2. Enter "John" and the registration code from step 6
3. Tap "Register passkey" → Face ID prompt → done
4. Same on Sara's iPhone with name "Sara"

iCloud Keychain syncs the passkey to any other Apple device signed into the same Apple ID, so each person registers once.

## 10. Test the main brief

Open `https://jlsteve25.github.io/weekend-brief/` — you should see the login screen. Tap "Sign in with Face ID / Touch ID". If a brief hasn't been regenerated since these changes landed, trigger the workflow manually: GitHub → Actions → "Generate Weekend Brief" → "Run workflow".

---

## Changing things later

- **Rotate the JWT secret** (invalidates all sessions): `wrangler secret put JWT_SECRET`
- **Change the registration code**: `wrangler secret put REGISTRATION_SECRET`
- **Remove a credential** (e.g. lost phone): `wrangler kv:key list --binding=PASSKEY_STORE` then `wrangler kv:key delete --binding=PASSKEY_STORE "credential:<id>"` and remove it from the `credential_index` key too
- **View Worker logs**: `wrangler tail`
