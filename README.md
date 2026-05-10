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

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Start the local PostGIS database and apply migrations:

```cmd
docker compose up -d db
python manage.py migrate
```

On Windows, GeoDjango may need explicit GDAL/GEOS paths before running Django
commands:

```cmd
for /f "delims=" %i in ('dir /b /s .venv\Lib\site-packages\pyogrio.libs\gdal*.dll') do set "GDAL_LIBRARY_PATH=%i"
for /f "delims=" %i in ('dir /b /s .venv\Lib\site-packages\shapely.libs\geos_c*.dll') do set "GEOS_LIBRARY_PATH=%i"
```

## Data

For real local gameplay, import official boundaries and population data:

```cmd
python manage.py setup_geodata
```

For a quick smoke test without downloading official data:

```cmd
python manage.py seed_dev_geodata
```

## Run

Create a user and start the development server:

```cmd
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`.

## Checks

```cmd
python manage.py check
python manage.py test
```

## Database

PostgreSQL data is stored in the Docker volume
`swiss-municipality-guess_postgres_data`.

Stop the database:

```cmd
docker compose down
```

Delete the local database data:

```cmd
docker compose down -v
```

## Docs

- [Gameplay](docs/gameplay.md)
- [Data import](docs/data_import.md)
- [Architecture](docs/architecture.md)

## Planned

- Switchable background maps
- Play without an account
- Statistics and history
- Visual update; the current UI is still rough
- More game modes:
  - learn cantons
  - only municipalities from one canton
  - places instead of municipalities
  - multiplayer options
