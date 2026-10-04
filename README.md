# AgriLink

AgriLink is a mobile-responsive agricultural marketplace connecting farmers and buyers. This repository currently contains the runnable Flask foundation; domain features will be added in later implementation batches.

## Requirements

- Python 3.14 or newer
- Git

## Development setup

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
flask --app wsgi.py run
```

The default development database is SQLite in `instance/agri_link.sqlite3`. SQLite foreign keys, WAL journaling, and a busy timeout are configured by the application. The `instance/` directory is created at startup and is ignored by Git.

## Authentication and phone verification

Registration accepts a phone number and password. Phone numbers are normalized to E.164 (national-format input is interpreted as Kenya by default). New accounts verify the phone using a six-digit code, then choose Farmer or Buyer once. A correct password on an unverified account starts a restricted verification flow; it does not establish a logged-in session. Normal login requires a verified phone and selected role. Passwords are hashed, and OTP challenges store only a challenge-specific HMAC. OTPs expire after five minutes, allow at most five attempts, and have a 60-second resend cooldown and five-send-per-hour phone limit.

Development and tests use `OTP_PROVIDER=mock` by default. The development provider writes codes to `instance/dev_otp_outbox.log`; this file is ignored by Git and must only be used in a local development environment. Tests capture messages in the mock provider in memory. Mock OTP delivery is rejected when running with the production configuration. Set `OTP_PEPPER` to a unique random value for production. The optional `OTP_PROVIDER=africastalking` adapter reads `AT_USERNAME`, `AT_API_KEY`, and `AT_SENDER_ID`; it has not been live-tested without provider credentials.

Apply the database schema before running the application:

```sh
flask --app wsgi.py db upgrade
```

Run authentication and foundation tests with `pytest`. No real SMS provider is needed for local development or testing.

## Commands

```sh
flask --app wsgi.py --help
flask --app wsgi.py routes
flask --app wsgi.py db upgrade
flask --app wsgi.py create-admin
flask --app wsgi.py seed
flask --app wsgi.py jobs run-maintenance
```

`flask db upgrade` applies the checked-in authentication migrations. Use `flask --app wsgi.py db migrate -m "describe change"` to generate later schema revisions. The `create-admin` command prompts for a phone number and password in the terminal; administrator accounts cannot be created through public forms.

## Checks

```sh
pytest
ruff check .
bandit -r app
```

For deployment, set `AGRI_LINK_CONFIG=production` and provide a unique secret key and production database URL through the environment. HTTPS is required for secure production session cookies.
