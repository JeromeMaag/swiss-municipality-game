# 🇨🇭 Find the Municipality! 🇨🇭

Find Swiss municipalities on the map.

Find the Municipality! is a small geography game for Switzerland. Play a
five-round game, place your pin on the map, reveal the municipality, and compare
your distance and score. You can play without an account, or sign in to keep
history and personal statistics.

<p align="center">
  <img src="docs/assets/screenshots/game.png" alt="Game screen with the map and sidebar" width="49%">
  <img src="docs/assets/screenshots/reveal.png" alt="Reveal screen with score and distance" width="49%">
</p>

## Features

- Five-round Swiss municipality guessing game
- Switzerland-wide and single-canton modes
- Guest play without registration
- Account history and personal statistics
- Map reveal with score, distance, pins, and reveal lines
- Finished-game summary and replayable history maps
- Switchable map backgrounds and outline styles
- English, German, and French interface
- Official swisstopo municipality boundaries and BFS STATPOP population data

## Quick Start

This setup is intended for local development and self-hosted evaluation, not as
a production deployment recipe.

Create a virtual environment, install dependencies, and copy the local
environment file:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set a real `SECRET_KEY` in `.env`. A local key can be generated with:

```powershell
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Start PostGIS and apply migrations:

```powershell
docker compose up -d db
python manage.py migrate
```

Choose a geodata setup:

```powershell
# Quick demo data for local smoke tests
python manage.py seed_dev_geodata
```

```powershell
# Official Swiss boundaries and population data
python manage.py setup_geodata
```

The official import downloads swissBOUNDARIES3D municipality boundaries and BFS
STATPOP population data. It can take a while.

Start the server:

```powershell
python manage.py runserver
```

Open `http://127.0.0.1:8000/`.

## Geodata Setup

Detailed import notes are available in [Data import](docs/data_import.md). The
Django admin geodata setup screen is an operator tool and requires a staff
account; it is not part of the public player experience.

## Self-Hosting Notes

- The app expects PostgreSQL with PostGIS.
- `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, and `DATABASE_URL` are configured via
  environment variables.
- The included `docker-compose.yml` is intended for local development only.
- Official geodata can be imported from the Django admin or with
  `python manage.py setup_geodata`.
- Create a superuser only when you need admin access:

```powershell
python manage.py createsuperuser
```

## Useful Commands

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
docker compose down
docker compose down -v
```

## Documentation

- [Gameplay](docs/gameplay.md)
- [Data import](docs/data_import.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)

## License

This project is licensed under the [MIT License](LICENSE).
