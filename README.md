# GemeindeGuess CH

A small Django game for guessing Swiss municipalities on a map.

The app uses GeoDjango, PostGIS, Leaflet, official swisstopo municipality
boundaries, and BFS STATPOP population data.

## Gameplay

Log in, start a game, and guess where the shown Swiss municipality is located.
Place a pin on the map and confirm the guess. The game then reveals the target
municipality, the distance, the score, the canton, and the population when it is
available.

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

On Windows, set the GeoDjango library paths before running Django commands:

```powershell
$env:GDAL_LIBRARY_PATH = (Get-ChildItem .\.venv\Lib\site-packages\pyogrio.libs -Filter "gdal*.dll" | Select-Object -First 1 -ExpandProperty FullName)
$env:GEOS_LIBRARY_PATH = (Get-ChildItem .\.venv\Lib\site-packages\shapely.libs -Filter "geos_c*.dll" | Select-Object -First 1 -ExpandProperty FullName)
```

Start the local PostGIS database and apply migrations:

```powershell
docker compose up -d db
python manage.py migrate
```

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

Create a user and start the development server:

```powershell
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`.

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

## Planned

- Switchable background maps
- Play without an account
- Google login
- Email verification
- Email password reset
- Statistics and history
- Admin statistics dashboard
- Visual update; the current UI is still rough
- More game modes:
  - learn cantons
  - only municipalities from one canton
  - places instead of municipalities
  - historical municipality datasets
  - multiplayer options
