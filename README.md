# GemeindeGuess CH

A small Django game for guessing Swiss municipalities on a map.

The app uses GeoDjango, PostGIS, Leaflet, official swisstopo municipality
boundaries, and BFS STATPOP population data.

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
