# GemeindeGuess CH

Local Django project for guessing Swiss municipalities.

## Local Setup
GeoDjango needs native GDAL and GEOS libraries. On Windows, Django may not find
the DLLs automatically. Set them for the current terminal session before running
Django commands:

```cmd
for /f "delims=" %i in ('dir /b /s .venv\Lib\site-packages\pyogrio.libs\gdal*.dll') do set "GDAL_LIBRARY_PATH=%i"
for /f "delims=" %i in ('dir /b /s .venv\Lib\site-packages\shapely.libs\geos_c*.dll') do set "GEOS_LIBRARY_PATH=%i"
```

## Common Commands

Start the local PostGIS database:

```cmd
docker compose up -d db
```

Create or update database tables:

```cmd
python manage.py migrate
```

Download official municipality boundaries and population data:

```cmd
python manage.py setup_geodata
```

Create an admin user:

```cmd
python manage.py createsuperuser
```

Seed five dummy municipalities for local development:

```cmd
python manage.py seed_dev_geodata
```

Import only the latest official swissBOUNDARIES3D canton and municipality boundaries:

```cmd
python manage.py import_swissboundaries3d
```

Import only official BFS STATPOP population values for existing municipalities:

```cmd
python manage.py import_statpop_population
```

Start the development server:

```cmd
python manage.py runserver
```

Run Django configuration checks:

```cmd
python manage.py check
```

Run the test suite:

```cmd
python manage.py test
```

The app is available at `http://127.0.0.1:8000/`.

## Local Database

PostgreSQL data is stored in the Docker volume
`swiss-municipality-guess_postgres_data`.

Stop the database while keeping its data:

```cmd
docker compose down
```

Stop the database and delete all local data:

```cmd
docker compose down -v
```
