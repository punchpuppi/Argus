
# Argus

## Project Overview

Argus is an in-progress Python email monitoring and notification bot. Its goal
is to monitor connected email accounts, match relevant messages against
user-defined tracking rules, detect meaningful status changes, and notify the
user through Telegram.

Development is incremental. The current codebase implements Gmail OAuth
authentication, an authenticated profile check, and read-only Gmail message
retrieval, incremental synchronization, tracking rules, and status detection.

## Current Implementation

Implemented and tested:

- Typed application configuration with `pydantic-settings`.
- SQLAlchemy database schema with an initial Alembic migration.
- Gmail OAuth 2.0 desktop authentication using only the read-only Gmail scope.
- Reuse of valid stored tokens and automatic refresh of expired access tokens.
- An authenticated Gmail profile check that verifies the connected account.
- Read-only Gmail message retrieval with search queries, pagination, and
  normalized message metadata.
- Gmail sync backfills existing mail, including Spam and Trash, on first use
  and when Gmail expires a saved cursor. It then follows incremental history.
  Messages that disappear before metadata retrieval are skipped and counted.
- Rule matching and status-event recording.

Not implemented yet:

- Automatic Gmail polling or monitoring.
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

Argus runs locally in one process. Gmail accounts share one SQLite database and use independent OAuth token files. Telegram and scheduling dependencies will be added with those features.

## Project Structure

```text
Argus/
├── app/
│   ├── providers/gmail/   # Gmail OAuth, profile client, and CLI
│   ├── database/          # SQLAlchemy models, types, and session management
│   ├── bot/               # Future Telegram layer
│   ├── email/             # Provider-neutral message schemas
│   ├── services/          # Incremental sync and checkpoint services
│   └── tracking/          # Tracking rules and status detection
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
| `GMAIL_PAGE_SIZE` | `100` | Gmail message page size (1–500) |

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

After applying the migrations, connect each mailbox once and sync it on demand:

```bat
python -m app.providers.gmail --account first@example.com
python -m app.providers.gmail --account second@example.com
```

Select the requested mailbox in Google's consent screen. Argus verifies its
address before saving new credentials and registering the account. Each token
lives in an `accounts/` directory beside `GMAIL_TOKEN_FILE`, under a filename
derived from the normalized address. The no-argument command still checks the
legacy token. Use `--import-legacy EMAIL` to copy a verified legacy credential
to a mailbox without a scoped token. Normal synchronization uses only that
mailbox's scoped credentials. Use `--reauthorize EMAIL` to request fresh consent
and atomically replace the selected token after verifying the Google address.

Configure and activate tracking rules **before the first sync**. Run
`python -m app.providers.gmail --sync EMAIL` when the rules are ready. The first
sync backfills the entire mailbox, including Spam and Trash. Every processed
message receives a permanent account-specific claim, even if no rule matches.
Adding or reactivating a rule later does not reevaluate those messages.

When Gmail expires a history cursor, Argus backfills from the current mailbox
and resumes incremental history from the position observed before listing.
Changes during the scan are picked up on a subsequent sync while that history
is available. Deleted messages can be unrecoverable, and a cursor that expires
during recovery requires another full scan. Argus is intended for one local
process. Overlap protection is process-local; separate CLI processes can contend
for the shared SQLite writer. Run one sync per mailbox at a time.

The CLI writes application logs to `logs/argus.log` and retains three rotated
files of up to 5 MiB each. Sync summaries use database account IDs.

## Running Tests

```bat
python -m pytest
```

The test suite uses temporary SQLite databases and mocked Google clients. It
does not require network access, real Gmail credentials, or the application
database.

To exercise the 10,000-message backfill against temporary SQLite, run:

```bat
set ARGUS_TEST_BACKLOG=1
python -m pytest tests/test_gmail_sync.py -k ten_thousand
```

## Security Notes

- Never commit `.env`, OAuth client secrets, token files, or local databases.
  `.gitignore` excludes them, and only `credentials/.gitkeep` is intended to be
  committed from the credentials directory.
- Gmail access uses only the read-only scope.
- On Windows, `chmod(0o600)` is best-effort and does not establish a restrictive
  ACL. Keep `credentials/` in a private location or apply an appropriate ACL.

## Roadmap

Future milestones:

- Scheduled Gmail monitoring
- Telegram notifications
- Outlook / Microsoft Graph support
- Optional AI-assisted command interpretation
