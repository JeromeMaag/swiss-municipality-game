# GemeindeGuess CH

A small Django game for guessing Swiss municipalities on a map.

The app uses GeoDjango, PostGIS, Leaflet, official swisstopo municipality
boundaries, and BFS STATPOP population data.

## Gameplay

Start a game as a guest or with an account, then guess where the shown Swiss
municipality is located. Place a pin on the map and confirm the guess. The game
then reveals the target municipality, the distance, the score, the canton, and
the population when it is available.

A game has five turns. After the last turn, the summary page shows all guesses
and the total score.

## Setup

Create and activate a virtual environment, then install the dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copy the example environment file:

```powershell
Copy-Item .env.example .env
```

Then open `.env` and replace `SECRET_KEY` with a real local value. You can
generate one with:

```powershell
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

The app refuses to start with the example placeholder key.

Start the local PostGIS database and apply migrations:

```powershell
docker compose up -d db
python manage.py migrate
```

The included `docker-compose.yml` is for local development only. It uses default
database credentials and exposes Postgres on your machine; do not use it as a
production deployment file.

## Data

For real local gameplay, import official boundaries and population data:

```powershell
python manage.py setup_geodata
```

For a quick smoke test without downloading official data:

```powershell
python manage.py seed_dev_geodata
```

## Run

Start the development server:

```powershell
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. You can play without an account. Create a
superuser only when you need the Django admin:

```powershell
python manage.py createsuperuser
```

## Checks

```powershell
python manage.py check
python manage.py test
```

## Database

PostgreSQL data is stored in the Docker volume created by Compose for the `db`
service, typically `<compose-project>_postgres_data`. Use `docker volume ls` to
confirm the exact name in your environment.

Stop the database:

```powershell
docker compose down
```

Delete the local database data:

```powershell
docker compose down -v
```

## Docs

- [Gameplay](docs/gameplay.md)
- [Data import](docs/data_import.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)

## License

This project is licensed under the MIT License.
