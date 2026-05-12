# Architecture

GemeindeGuess CH is a small Django app with server-side game logic and a thin
Leaflet frontend.

## Apps

`accounts`
: Registration, login, and logout using Django's built-in auth.

`geo`
: Imported canton and municipality data, GeoJSON endpoints, and data import
commands.

`game`
: Game sessions, turns, guesses, scoring, page views, and summary pages.

`tracking`
: Persisted gameplay events such as game start, map clicks, reveals, and game
finish.

## Data Flow

1. A user starts a game.
2. The backend creates five turns with unique active municipalities.
3. The game page loads neutral municipality boundaries and canton boundaries.
4. The user places a pin and submits a guess.
5. Django validates the turn, calculates distance and score in PostGIS, stores
   the guess, and reveals the turn.
6. The reveal view highlights the target municipality and can load municipality
   labels.
7. After five turns, the game is marked as finished and the summary page shows
   the result.

## Geodata

The database is the source of truth for gameplay:

- target municipalities
- boundaries
- guesses
- distances
- scores
- tracking events

swisstopo tiles are only used as visual map background. Guess validation and
distance calculation never depend on a live map service.

## GeoJSON Endpoints

The game uses three GeoJSON endpoint types:

- canton boundaries with canton names
- municipality boundaries without names
- municipality labels after reveal

Municipality names are deliberately withheld during the guessing phase. Label
access is tied to a revealed turn for the current session.

## Frontend

The frontend is plain Django templates, CSS, and a small `game_map.js` file.
Leaflet handles the map interaction. The browser only places pins and renders
layers; the backend owns validation, scoring, and persistence.

## Internationalization

User-facing UI strings use Django's translation system. English is the source
language, and supported languages are configured in `LANGUAGES` in
`config/settings.py`.

Templates use `{% trans %}` or `{% blocktrans %}`. Python code uses
`gettext()` at the point where user-facing messages are created. Profile
language choices are built from `settings.LANGUAGES` so language names stay in
their native form.

When adding or changing UI strings, update the relevant
`locale/<language>/LC_MESSAGES/django.po` files and commit the compiled
`django.mo` files as well, so deployments do not depend on gettext tooling
being available at runtime.

## Tracking

Tracking events are stored from both backend actions and lightweight frontend
events. Tracking failures must not block gameplay.
