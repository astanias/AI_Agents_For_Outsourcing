# Deployment Guide

This guide documents how to deploy the `prod` branch of this project to DigitalOcean App Platform with:

- FastAPI + Jinja UI served on the root domain
- DigitalOcean Managed PostgreSQL
- Resend for production email notifications
- HTTPS on `https://schedulerai.tech`
- `https://www.schedulerai.tech` redirecting to the root domain

This project does not use the removed React/Vite frontend on this branch. The live UI is the FastAPI application served by `uvicorn`.

## Architecture

- App Platform Web Service
- Managed PostgreSQL database
- Custom domain: `schedulerai.tech`
- Redirect domain: `www.schedulerai.tech`
- Resend for outbound email

## Expected Cost

At a small launch size, expect roughly:

- App Platform web service: about `$5/month`
- Managed PostgreSQL: about `$15/month`

This is usually a good fit for the DigitalOcean student credit, but always confirm your remaining credit balance in the DigitalOcean billing page before creating resources.

## Before You Start

You need:

- A DigitalOcean account with the GitHub Student credit already applied
- Access to the GitHub repository
- Access to DNS settings for `schedulerai.tech`
- Access to the Resend account that will send mail for `schedulerai.tech`
- The `prod` branch ready in GitHub

## Production Environment Values

Use these production settings in DigitalOcean App Platform:

```text
APP_ENV=production
DATABASE_URL=<managed-postgres-connection-string>
JWT_SECRET=<strong-random-secret-at-least-32-characters>
FRONTEND_ORIGIN=
COOKIE_SECURE=true
COOKIE_SAMESITE=lax
COOKIE_DOMAIN=schedulerai.tech
CSRF_PROTECTION_ENABLED=true
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
RESEND_API_KEY=<your-resend-api-key>
EMAIL_FROM_ADDRESS=notifications@schedulerai.tech
EMAIL_FROM_NAME=AI Scheduler
APP_BASE_URL=https://schedulerai.tech
LOG_LEVEL=INFO
OPENROUTESERVICE_API_KEY=
OPENROUTESERVICE_BASE_URL=https://api.openrouteservice.org
OPENROUTESERVICE_TIMEOUT_SECONDS=10
OPENROUTESERVICE_PROFILE=driving-car
TRAVEL_WARNING_BUFFER_MINUTES=10
TRAVEL_WARNING_TIGHT_WINDOW_MINUTES=15
ORGANIZATION_DEFAULT_LOCATION=
ORGANIZATION_DEFAULT_LOCATION_LATITUDE=
ORGANIZATION_DEFAULT_LOCATION_LONGITUDE=
```

Notes:

- Leave `FRONTEND_ORIGIN` empty unless you add a separate frontend later.
- `COOKIE_DOMAIN=schedulerai.tech` allows cookies to work consistently for both the root domain and `www`.
- Google OAuth support exists in the codebase, but the production UI hides Google login while the Google Cloud Console setup remains incomplete. Leave `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` empty for the current production launch.

## Owner Setup

These steps are intended for the person who owns the DigitalOcean account and DNS.

### 1. Confirm local readiness

Before deploying, confirm the app works locally from the `prod` branch:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d
python -m uvicorn app.main:app --reload
```

Then open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/healthz`

### 2. Push the `prod` branch

Make sure the exact branch you want to deploy is pushed:

```powershell
git push origin prod
```

### 3. Create a Managed PostgreSQL database

In DigitalOcean:

1. Create a PostgreSQL database cluster.
2. Choose the smallest production-appropriate size to start.
3. Keep automated backups enabled.
4. Create or note the database name, username, password, host, port, and SSL mode.

You will use the resulting connection string as `DATABASE_URL`.

### 4. Create the App Platform app

In DigitalOcean App Platform:

1. Create a new app from GitHub.
2. Choose this repository.
3. Choose the `prod` branch.
4. Disable auto-deploy for now because deploys will be manual before the exposition.
5. Select a Web Service component.

Use these service settings:

- Source directory: repo root
- Build command: leave blank
- Run command:

```text
uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
```

- HTTP port: App Platform injects `PORT`
- Instance size: smallest paid web service to start

### 5. Add environment variables

In the App Platform app settings, add all production variables from the list above.

Important values to set correctly:

- `APP_ENV=production`
- `JWT_SECRET=<strong random value>`
- `COOKIE_SECURE=true`
- `CSRF_PROTECTION_ENABLED=true`
- `COOKIE_DOMAIN=schedulerai.tech`
- `APP_BASE_URL=https://schedulerai.tech`
- `EMAIL_FROM_ADDRESS=notifications@schedulerai.tech`

Future optional Google login: if the team decides to enable Google OAuth in production later, create a Web application OAuth client in Google Cloud Console and make the redirect URI exactly match:

```text
https://schedulerai.tech/web/auth/google/callback
```

Then set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, and remove or adjust the production UI guard.

### 6. Add a health check

Configure the health check path as:

```text
/healthz
```

This application exposes a lightweight health endpoint specifically for deployment checks.

### 7. First deploy

Run the initial deployment in App Platform and wait for it to finish.

If startup fails:

- verify `DATABASE_URL`
- verify `JWT_SECRET`
- verify `COOKIE_SECURE=true`
- verify `CSRF_PROTECTION_ENABLED=true`

The app intentionally refuses to start in unsafe production mode.

### 8. Attach the custom domain

In App Platform domain settings:

1. Add `schedulerai.tech`
2. Add `www.schedulerai.tech`
3. Set `schedulerai.tech` as the primary domain
4. Configure `www.schedulerai.tech` to redirect to the primary domain if App Platform offers a redirect option

If App Platform does not expose a redirect toggle for the subdomain in your current UI, keep both domains attached temporarily and add the redirect at your DNS or edge layer later.

### 9. Update DNS at get.tech

At your DNS provider:

1. Create or update the records DigitalOcean tells you to use for `schedulerai.tech`
2. Create or update the records for `www.schedulerai.tech`
3. Wait for DNS propagation
4. Wait for DigitalOcean to issue HTTPS certificates

After DNS settles, test:

- `https://schedulerai.tech`
- `https://www.schedulerai.tech`

The goal is for `www` to end up redirecting to the root domain.

### 10. Configure Resend

In Resend:

1. Verify `schedulerai.tech` if not already verified
2. Make sure `notifications@schedulerai.tech` is an approved sender
3. Confirm the DNS records Resend requires are still present
4. Put the production `RESEND_API_KEY` into App Platform

### 11. Smoke test the live app

After the app is live, verify:

1. Home page loads over HTTPS
2. Sign up works
3. Email/password login works
4. Session persists after refresh
5. Logout works
6. Meeting creation works
7. Invite notifications create in-app notifications
8. Email notifications send from Resend
9. Reminder scheduler works for near-future meetings

### 12. Manual deploy flow before the exposition

Because auto-deploy is disabled, use this process:

1. Merge or commit to `prod`
2. Push to GitHub
3. Open DigitalOcean App Platform
4. Trigger a manual redeploy
5. Wait for health checks to pass
6. Run smoke tests on the live domain

## Teammate Guide

Teammates do not need direct DigitalOcean access if one person owns deployments.

Their workflow:

1. Branch from the latest `prod`
2. Make changes
3. Test locally
4. Open a PR
5. Get approval
6. Merge into `prod`
7. Notify the deployment owner that `prod` is ready for manual deploy

Recommended local verification before asking for deploy:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d
python -m uvicorn app.main:app --reload
```

Then test:

- `http://127.0.0.1:8000/`
- login
- signup
- meetings flow
- notifications flow if the change touches it

## Security Checklist

Never commit:

- `.env`
- production API keys
- JWT secrets
- database passwords

Always confirm in production:

- `APP_ENV=production`
- `COOKIE_SECURE=true`
- `CSRF_PROTECTION_ENABLED=true`
- strong `JWT_SECRET`
- HTTPS working on the final domain

## Post-Expo Improvements

After the exposition, good next steps are:

1. Add a formal deployment checklist in PRs
2. Consider enabling auto-deploy after the team is comfortable
3. Add uptime monitoring and error alerting
