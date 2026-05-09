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

Run Django configuration checks:

```cmd
python manage.py check
```

Run the test suite:

```cmd
python manage.py test
```

Start the local PostGIS database:

```cmd
docker compose up -d db
```

Create or update database tables:

```cmd
python manage.py migrate
```

Start the development server:

```cmd
python manage.py runserver
```

The app is available at `http://127.0.0.1:8000/`.
