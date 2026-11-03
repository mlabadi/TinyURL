# m.rta.ae link shortener

A small TinyURL-style service that turns long URLs into `https://m.rta.ae/<code>` links.
Flask + SQLite, no other dependencies.

## Run

```bash
pip install -r requirements.txt
API_KEY=change-me flask --app app run          # dev, http://127.0.0.1:5000
# or
docker build -t m-rta-ae . && docker run -p 8000:8000 -e API_KEY=change-me -v $PWD/data:/data m-rta-ae
```

Tests: `pip install pytest && pytest -q`

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/shorten` | `X-API-Key` | Create a link. Body: `{"url", "alias"?, "expires_in_days"?}` |
| `GET` | `/<code>` | none | 302 redirect to the target; counts the click |
| `GET` | `/api/links/<code>` | `X-API-Key` | Target, created/expiry, click count |
| `DELETE` | `/api/links/<code>` | `X-API-Key` | Remove a link |
| `GET` | `/` | none | Web form |
| `GET` | `/health` | none | Health check |

```bash
curl -X POST https://m.rta.ae/api/shorten -H 'X-API-Key: change-me' \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://www.rta.ae/wps/portal/rta/ae/home","alias":"home"}'
# {"code":"home","short_url":"https://m.rta.ae/home", ...}
```

Status codes: `400` invalid input, `401` bad/missing key, `404` unknown code, `409` alias taken, `410` expired.

## Configuration (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `SHORT_DOMAIN` | `m.rta.ae` | Domain used in generated links |
| `SHORT_SCHEME` | `https` | |
| `API_KEY` | *(empty)* | Empty = anyone can create links. Set it in production. |
| `ALLOWED_TARGET_DOMAINS` | `rta.ae` | Comma list; subdomains included. `*` allows any destination. |
| `CODE_LENGTH` | `7` | 62^7 ≈ 3.5 trillion codes |
| `DATABASE` | `tinyurl.db` | SQLite file path |

## Design notes

- **Codes** are random base62 (via `secrets`), not sequential IDs, so links can't be enumerated. Collisions are retried.
- **Destination allowlist** defaults to `rta.ae` and its subdomains. An open shortener on a government domain would be an easy phishing tool, so widening this should be a deliberate decision.
- **302, not 301**, so browsers don't cache the redirect permanently and click counts stay accurate.
- Shortening the same non-expiring URL twice returns the existing code.

## Deploying on m.rta.ae

1. Point DNS for `m.rta.ae` at the host/load balancer running the container.
2. Terminate TLS in front of it (nginx, ALB, etc.) and proxy to port 8000.
3. Mount `/data` on persistent storage and back up `tinyurl.db`.
4. SQLite is fine for one instance. For multiple replicas, move to PostgreSQL and add a cache (e.g. Redis) in front of redirects.
