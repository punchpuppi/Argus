# Argus

## Project Overview

Argus is an in-progress Python email monitoring and notification bot. Its goal
is to monitor connected email accounts, match relevant messages against
user-defined tracking rules, detect meaningful status changes, and notify the
user through Telegram.

Development is incremental. The current codebase implements Gmail OAuth
authentication and an authenticated profile check. Email monitoring, tracking
rules, and notifications are not implemented yet.

## Current Implementation

Implemented and tested:

- Typed application configuration with `pydantic-settings`.
- SQLAlchemy database schema with an initial Alembic migration.
- Gmail OAuth 2.0 desktop authentication using only the read-only Gmail scope.
- Reuse of valid stored tokens and automatic refresh of expired access tokens.
- An authenticated Gmail profile check that verifies the connected account.

Not implemented yet:

- Gmail message retrieval, listing, or monitoring.
- Tracking rules or status detection.
- Telegram notifications or command handling.
- Outlook or Microsoft Graph support.
- AI-assisted command interpretation.

## Technology Stack

- Python 3.14.7
- Google Gmail API v1, using `google-api-python-client`, `google-auth`, and
  `google-auth-oauthlib`
- SQLAlchemy 2.x and Alembic
- Pydantic Settings
- Pytest

`python-telegram-bot` is included in the dependency set for a future Telegram
milestone. No Telegram bot is implemented or operational yet.

## Project Structure

```text
Argus/
├── app/
│   ├── providers/gmail/   # Gmail OAuth, profile client, and CLI
│   ├── database/          # SQLAlchemy models, types, and session management
│   ├── bot/               # Future Telegram layer
│   ├── email/             # Future provider-neutral message schemas
│   ├── services/          # Future monitoring and notification services
│   └── tracking/          # Future tracking rules and status detection
├── alembic/               # Database migrations
├── credentials/           # Local OAuth files; only .gitkeep is committed
├── tests/
├── alembic.ini
├── config.py
├── pytest.ini
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Requirements and Setup

The commands below assume Windows Command Prompt. Run them from the Argus
project root:

```bat
python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` locally. The important Gmail settings are:

| Variable | Default | Purpose |
|---|---|---|
| `GMAIL_CLIENT_SECRET_FILE` | `credentials/google_client_secret.json` | Google OAuth client secret file |
| `GMAIL_TOKEN_FILE` | `credentials/gmail_token.json` | Generated OAuth token file |

Relative credential paths resolve against the project root. Project-local
credential files must stay under `credentials/`. Paths explicitly outside the
project are also supported for OS-level credential storage.

`.env.example` documents the full set of supported settings. Do not commit
`.env` or any credential file.

## Gmail Authentication Setup

1. Open the Google Cloud Console and create or select a project.
2. Enable the **Gmail API** for that project.
3. Configure the **OAuth consent screen**. While the app is in testing, add your
   Google account as a test user.
4. Create an **OAuth client ID** of type **Desktop app**.
5. Download the client JSON and save it as
   `credentials/google_client_secret.json`, or set `GMAIL_CLIENT_SECRET_FILE` to
   its location.
6. From the project root, run:

```bat
venv\Scripts\activate
python -m app.providers.gmail
```

The command opens the system browser and requests only
`https://www.googleapis.com/auth/gmail.readonly`. After consent, it verifies the
account through the Gmail profile endpoint and prints the authenticated email
address.

The generated token is written to `credentials/gmail_token.json` by default.
Subsequent runs reuse the token or refresh it automatically. To force
reauthorization after a revoked grant, delete the token file and run the command
again.

## Database and Migrations

The default database is a local SQLite file named `argus.db` in the project
root. Apply the schema with:

```bat
alembic upgrade head
```

After changing the models, create a new migration with:

```bat
alembic revision --autogenerate -m "describe change"
```

The database file and SQLite sidecar files are ignored by Git. Set
`DATABASE_URL` in `.env` to use a different database URL.

## Running the Application

The only current application entry point is the Gmail authorization and profile
check:

```bat
python -m app.providers.gmail
```

This authorizes Argus if needed, verifies the connected Gmail account, and prints
its email address. The email monitoring bot itself is not operational yet.

## Running Tests

```bat
python -m pytest
```

The test suite uses temporary SQLite databases and mocked Google clients. It
does not require network access, real Gmail credentials, or the application
database.

## Security Notes

- Never commit `.env`, OAuth client secrets, token files, or local databases.
  `.gitignore` excludes them, and only `credentials/.gitkeep` is intended to be
  committed from the credentials directory.
- Gmail access uses only the read-only scope.
- On Windows, `chmod(0o600)` is best-effort and does not establish a restrictive
  ACL. Keep `credentials/` in a private location or apply an appropriate ACL.

## Roadmap

Future milestones:

- Gmail email retrieval and monitoring
- Tracking rules and status detection
- Telegram notifications
- Outlook / Microsoft Graph support
- Optional AI-assisted command interpretation
