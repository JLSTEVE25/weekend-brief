// Weekend Brief — WebAuthn Passkey Auth Worker
// =============================================
// Endpoints:
//   POST /register/begin     — start passkey registration (requires REGISTRATION_SECRET)
//   POST /register/complete  — finish registration, store public key in KV
//   POST /login/begin        — start passkey auth, return challenge + allowCredentials
//   POST /login/complete     — verify signature, issue JWT session (30 days)
//   GET  /verify             — check session cookie validity
//
// KV keys:
//   credential:<credentialIdBase64url> → { publicKey, counter, userId, userName, createdAt }
//   challenge:<challengeHash>          → { type: 'reg'|'auth', userName? }  (5 min TTL)
//   credential_index                    → [ credentialIdBase64url, ... ]     (for allowCredentials)
//
// Sessions are JWT (HS256), not stored in KV — stateless and cheap.

import {
  generateRegistrationOptions,
  verifyRegistrationResponse,
  generateAuthenticationOptions,
  verifyAuthenticationResponse,
} from '@simplewebauthn/server';

// ── Helpers ─────────────────────────────────────────────────────────────

function corsHeaders(env) {
  return {
    'Access-Control-Allow-Origin': env.RP_ORIGIN,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    'Access-Control-Allow-Credentials': 'true',
    'Vary': 'Origin',
  };
}

function json(data, status, env, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      ...corsHeaders(env),
      ...extraHeaders,
    },
  });
}

function b64urlEncode(bytes) {
  let s = '';
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (const b of arr) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlDecode(str) {
  const pad = '='.repeat((4 - (str.length % 4)) % 4);
  const s = (str + pad).replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function sha256Hex(input) {
  const data = typeof input === 'string' ? new TextEncoder().encode(input) : input;
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// ── JWT (HS256) ─────────────────────────────────────────────────────────

async function hmacKey(secret) {
  return crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign', 'verify'],
  );
}

async function signJWT(payload, secret) {
  const header = { alg: 'HS256', typ: 'JWT' };
  const h = b64urlEncode(new TextEncoder().encode(JSON.stringify(header)));
  const p = b64urlEncode(new TextEncoder().encode(JSON.stringify(payload)));
  const signingInput = `${h}.${p}`;
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(signingInput));
  return `${signingInput}.${b64urlEncode(new Uint8Array(sig))}`;
}

async function verifyJWT(token, secret) {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const [h, p, s] = parts;
  const key = await hmacKey(secret);
  const ok = await crypto.subtle.verify(
    'HMAC',
    key,
    b64urlDecode(s),
    new TextEncoder().encode(`${h}.${p}`),
  );
  if (!ok) return null;
  const payload = JSON.parse(new TextDecoder().decode(b64urlDecode(p)));
  if (payload.exp && Date.now() / 1000 > payload.exp) return null;
  return payload;
}

// ── Cookie helpers ──────────────────────────────────────────────────────

const COOKIE_NAME = 'wb_session';
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 30; // 30 days

function sessionCookie(token) {
  // SameSite=None required for cross-site (GitHub Pages → workers.dev). Needs Secure.
  return `${COOKIE_NAME}=${token}; Path=/; Max-Age=${SESSION_TTL_SECONDS}; Secure; HttpOnly; SameSite=None`;
}

function clearSessionCookie() {
  return `${COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=None`;
}

function readCookie(request, name) {
  const header = request.headers.get('Cookie') || '';
  const match = header.split(/;\s*/).find(c => c.startsWith(name + '='));
  return match ? match.slice(name.length + 1) : null;
}

// ── KV credential index ─────────────────────────────────────────────────

async function getCredentialIndex(env) {
  const raw = await env.PASSKEY_STORE.get('credential_index');
  return raw ? JSON.parse(raw) : [];
}

async function addToCredentialIndex(env, credentialId) {
  const list = await getCredentialIndex(env);
  if (!list.includes(credentialId)) {
    list.push(credentialId);
    await env.PASSKEY_STORE.put('credential_index', JSON.stringify(list));
  }
}

// ── Route handlers ──────────────────────────────────────────────────────

async function handleRegisterBegin(request, env) {
  const body = await request.json().catch(() => ({}));
  const { secret, userName } = body;

  if (!env.REGISTRATION_SECRET || secret !== env.REGISTRATION_SECRET) {
    return json({ error: 'Invalid registration secret' }, 403, env);
  }
  if (!userName || typeof userName !== 'string') {
    return json({ error: 'userName required' }, 400, env);
  }

  const userId = new TextEncoder().encode(userName); // stable per-name user handle
  const existingIds = await getCredentialIndex(env);
  const excludeCredentials = existingIds.map(id => ({ id })); // v10: base64url string

  const options = await generateRegistrationOptions({
    rpName: env.RP_NAME,
    rpID: env.RP_ID,
    userID: userId,
    userName,
    userDisplayName: userName,
    attestationType: 'none',
    authenticatorSelection: {
      residentKey: 'preferred',
      userVerification: 'preferred',
    },
    excludeCredentials,
  });

  // Persist challenge for 5 min, keyed by its hash.
  const key = `challenge:${await sha256Hex(options.challenge)}`;
  await env.PASSKEY_STORE.put(
    key,
    JSON.stringify({ type: 'reg', userName, challenge: options.challenge }),
    { expirationTtl: 300 },
  );

  return json(options, 200, env);
}

async function handleRegisterComplete(request, env) {
  const body = await request.json();
  const { userName, credential } = body;
  if (!userName || !credential) {
    return json({ error: 'userName and credential required' }, 400, env);
  }

  // clientDataJSON contains the challenge — look it up.
  const clientData = JSON.parse(
    new TextDecoder().decode(b64urlDecode(credential.response.clientDataJSON)),
  );
  const challengeKey = `challenge:${await sha256Hex(clientData.challenge)}`;
  const stored = await env.PASSKEY_STORE.get(challengeKey);
  if (!stored) return json({ error: 'Challenge expired or not found' }, 400, env);
  const { challenge: expectedChallenge } = JSON.parse(stored);

  let verification;
  try {
    verification = await verifyRegistrationResponse({
      response: credential,
      expectedChallenge,
      expectedOrigin: env.RP_ORIGIN,
      expectedRPID: env.RP_ID,
    });
  } catch (err) {
    return json({ error: 'Verification failed', detail: String(err) }, 400, env);
  }

  if (!verification.verified || !verification.registrationInfo) {
    return json({ error: 'Verification failed' }, 400, env);
  }

  const { credentialID, credentialPublicKey, counter } = verification.registrationInfo;
  // @simplewebauthn v10: credentialID is already a base64url string; credentialPublicKey is Uint8Array.
  await env.PASSKEY_STORE.put(
    `credential:${credentialID}`,
    JSON.stringify({
      credentialID,
      publicKey: b64urlEncode(credentialPublicKey),
      counter,
      userName,
      createdAt: Date.now(),
    }),
  );
  await addToCredentialIndex(env, credentialID);
  await env.PASSKEY_STORE.delete(challengeKey);

  return json({ verified: true, userName }, 200, env);
}

async function handleLoginBegin(request, env) {
  const existingIds = await getCredentialIndex(env);
  if (existingIds.length === 0) {
    return json({ error: 'No registered passkeys' }, 400, env);
  }

  const allowCredentials = existingIds.map(id => ({ id })); // v10: base64url string

  const options = await generateAuthenticationOptions({
    rpID: env.RP_ID,
    allowCredentials,
    userVerification: 'preferred',
  });

  const key = `challenge:${await sha256Hex(options.challenge)}`;
  await env.PASSKEY_STORE.put(
    key,
    JSON.stringify({ type: 'auth', challenge: options.challenge }),
    { expirationTtl: 300 },
  );

  return json(options, 200, env);
}

async function handleLoginComplete(request, env) {
  const credential = await request.json();

  const clientData = JSON.parse(
    new TextDecoder().decode(b64urlDecode(credential.response.clientDataJSON)),
  );
  const challengeKey = `challenge:${await sha256Hex(clientData.challenge)}`;
  const stored = await env.PASSKEY_STORE.get(challengeKey);
  if (!stored) return json({ authenticated: false, error: 'Challenge expired' }, 400, env);
  const { challenge: expectedChallenge } = JSON.parse(stored);

  const credIdB64 = credential.id;
  const credRaw = await env.PASSKEY_STORE.get(`credential:${credIdB64}`);
  if (!credRaw) return json({ authenticated: false, error: 'Unknown credential' }, 400, env);
  const credRecord = JSON.parse(credRaw);

  let verification;
  try {
    verification = await verifyAuthenticationResponse({
      response: credential,
      expectedChallenge,
      expectedOrigin: env.RP_ORIGIN,
      expectedRPID: env.RP_ID,
      authenticator: {
        credentialID: credRecord.credentialID, // v10: base64url string
        credentialPublicKey: b64urlDecode(credRecord.publicKey),
        counter: credRecord.counter,
      },
    });
  } catch (err) {
    return json({ authenticated: false, error: String(err) }, 400, env);
  }

  if (!verification.verified) {
    return json({ authenticated: false }, 401, env);
  }

  // Update counter.
  credRecord.counter = verification.authenticationInfo.newCounter;
  await env.PASSKEY_STORE.put(`credential:${credIdB64}`, JSON.stringify(credRecord));
  await env.PASSKEY_STORE.delete(challengeKey);

  // Issue JWT session.
  const now = Math.floor(Date.now() / 1000);
  const token = await signJWT(
    { sub: credRecord.userName, iat: now, exp: now + SESSION_TTL_SECONDS },
    env.JWT_SECRET,
  );

  return json(
    { authenticated: true, user: credRecord.userName },
    200,
    env,
    { 'Set-Cookie': sessionCookie(token) },
  );
}

async function handleVerify(request, env) {
  const token = readCookie(request, COOKIE_NAME);
  if (!token) return json({ authenticated: false }, 200, env);
  const payload = await verifyJWT(token, env.JWT_SECRET);
  if (!payload) {
    return json({ authenticated: false }, 200, env, { 'Set-Cookie': clearSessionCookie() });
  }
  return json({ authenticated: true, user: payload.sub }, 200, env);
}

// ── Entrypoint ──────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    try {
      if (url.pathname === '/register/begin' && request.method === 'POST') {
        return await handleRegisterBegin(request, env);
      }
      if (url.pathname === '/register/complete' && request.method === 'POST') {
        return await handleRegisterComplete(request, env);
      }
      if (url.pathname === '/login/begin' && request.method === 'POST') {
        return await handleLoginBegin(request, env);
      }
      if (url.pathname === '/login/complete' && request.method === 'POST') {
        return await handleLoginComplete(request, env);
      }
      if (url.pathname === '/verify' && request.method === 'GET') {
        return await handleVerify(request, env);
      }
      return json({ error: 'Not found' }, 404, env);
    } catch (err) {
      return json({ error: 'Internal error', detail: String(err) }, 500, env);
    }
  },
};
