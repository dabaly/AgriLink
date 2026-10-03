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

## Commands

```sh
flask --app wsgi.py --help
flask --app wsgi.py routes
flask --app wsgi.py db upgrade
flask --app wsgi.py seed
flask --app wsgi.py jobs run-maintenance
```

`flask db upgrade` uses Flask-Migrate. Create an initial migration with `flask --app wsgi.py db init` and `flask --app wsgi.py db migrate` when the first domain models are introduced.

## Checks

```sh
pytest
ruff check .
bandit -r app
```

For deployment, set `AGRI_LINK_CONFIG=production` and provide a unique secret key and production database URL through the environment. HTTPS is required for secure production session cookies.
