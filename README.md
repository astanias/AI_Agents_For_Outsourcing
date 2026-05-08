# AI_Agents_For_Outsourcing

This repo currently includes:

- Postgres via `docker-compose.yml`
- Database schema in `db/schema.sql`
- FastAPI backend (authentication + authorization helpers) under `app/`
- Server-rendered web pages served by FastAPI at `/`

The active UI is the FastAPI/Jinja application served by `uvicorn` on port `8000`.
The old React/Vite and Flask prototype UIs have been removed from this branch.

Deployment instructions for DigitalOcean live hosting are in [DEPLOYMENT.md](DEPLOYMENT.md).

## Local Setup

1) Start Postgres

`docker compose up -d`

2) Install Python deps

`pip install -r requirements.txt`

3) Configure env

- Copy `.env.example` to `.env`
- Fill in `JWT_SECRET`
- (Optional, development only) Fill in `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to make the local Google login button complete the OAuth flow
- (Optional) Fill in `OPENROUTESERVICE_API_KEY` to enable meeting travel-time warnings
- (Optional) Fill in `ORGANIZATION_DEFAULT_LOCATION` and coordinates for the final travel-origin fallback

4) Run API

`uvicorn app.main:app --reload`

API docs: `http://127.0.0.1:8000/docs`
Web login page: `http://127.0.0.1:8000/`

## Tests And Database Safety

The pytest suite intentionally truncates its database between tests. To protect real user data, tests use `TEST_DATABASE_URL` when set, or automatically derive a database ending in `_test` from `DATABASE_URL` such as `appdb_test`.

The test cleanup code refuses to truncate any database whose name does not end in `_test`. Do not point `TEST_DATABASE_URL` at a development or production database.

## Production Notes

For a public deployment, set:

- `APP_ENV=production`
- a strong `JWT_SECRET` of at least 32 characters
- `COOKIE_SECURE=true`
- `CSRF_PROTECTION_ENABLED=true`
- `APP_BASE_URL` to the public HTTPS URL

The app refuses to start in production if these safety settings are missing or still set to local-development defaults.

For local production-like UI testing, use `APP_ENV=staging`. Staging hides development-only Google/Microsoft login buttons like production, but it does not enforce production-only security settings such as `COOKIE_SECURE=true` or `CSRF_PROTECTION_ENABLED=true`.

PowerShell example:

```powershell
$env:APP_ENV="staging"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## Travel-Time Warnings

The meetings page can now evaluate travel feasibility between meetings using `openrouteservice` routing and OpenStreetMap-based geocoding.

Relevant environment variables:

- `OPENROUTESERVICE_API_KEY`
- `OPENROUTESERVICE_BASE_URL` (defaults to `https://api.openrouteservice.org`)
- `OPENROUTESERVICE_TIMEOUT_SECONDS`
- `OPENROUTESERVICE_PROFILE` (defaults to `driving-car`)
- `TRAVEL_WARNING_BUFFER_MINUTES`
- `TRAVEL_WARNING_TIGHT_WINDOW_MINUTES`
- `ORGANIZATION_DEFAULT_LOCATION`
- `ORGANIZATION_DEFAULT_LOCATION_LATITUDE`
- `ORGANIZATION_DEFAULT_LOCATION_LONGITUDE`

If the ORS provider is unavailable or a meeting location cannot be resolved, the page continues rendering without travel warnings.

## Auth Endpoints

- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/refresh` (rotates refresh token cookie)
- `POST /auth/logout`
- `GET /auth/me`
- `POST /auth/google/exchange`
- `POST /auth/link/google`

## Future Optional Google Login

Google login is identity-only. It requests `openid email profile` and does not request Google Calendar access. The backend and web OAuth callback code are present, but the production UI currently hides Google login because the Google Cloud Console OAuth setup has not been completed and approved for the deployed app.

In development, the Google and Microsoft buttons appear on the login page. Google redirects into the OAuth flow only when both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set; otherwise it shows a configuration message. Microsoft remains a local stub. In production, the visible login UI is email/password only.

In Google Cloud Console:

- Create or select a project.
- Configure the OAuth consent screen.
- Create an OAuth 2.0 Client ID for a Web application.
- Add authorized redirect URIs:
  - `http://127.0.0.1:8000/web/auth/google/callback`
  - `https://schedulerai.tech/web/auth/google/callback`
- Copy the client ID and client secret into `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
- Set `APP_BASE_URL=http://127.0.0.1:8000` locally and `APP_BASE_URL=https://schedulerai.tech` in production.

When a verified Google email matches an existing password account, the app links that Google identity automatically and signs the user in.

## Recommendation Endpoint

- `POST /recommendations/meeting-times`
  - Finds optimal meeting slots for attendees using:
  - Existing meeting conflicts (owned calendar + invited/accepted meetings)
  - `time_slot_preferences` windows

Example payload:

```json
{
  "attendee_emails": ["alice.demo@example.com", "bob.demo@example.com", "carol.demo@example.com"],
  "window_start": "2026-03-10T08:00:00Z",
  "window_end": "2026-03-10T18:00:00Z",
  "duration_minutes": 60,
  "slot_interval_minutes": 30,
  "max_results": 5,
  "include_current_user": true
}
```

Optional demo seed data:

```bash
psql -U appuser -d appdb -f db/seed_recommendation_demo.sql
```

If `psql` is not on your PATH, run the Python seed runner instead:

```bash
py -3.14 db/seed_recommendation_demo.py
```

## Notes

- Refresh token is stored in an `HttpOnly` cookie (path `/auth`).
- Access token is returned in JSON and should be sent as `Authorization: Bearer <token>`.
- Google login requires `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a redirect URI that exactly matches Google Cloud Console.
