# Airport Investment Intelligence Agent

A chat system for analyzing US airports and identifying signs of a need for terminal expansion. The system combines FAA and BTS data, calculations performed in code and explanations from a language model. Results include tables, data sources, observation periods and limitations.

## Project documentation

- [Design and architecture document](docs/architecture.md) — the primary submission document: scoring methodology, AI usage, key tradeoffs and limitations.
- [Detailed decisions and tradeoffs](docs/key-tradeoffs.md) — an optional appendix with the rationale and alternatives considered.

## Quick start with Docker

### Mandatory prerequisites

**Complete both requirements below before running the project:**

1. **Docker with Docker Compose should be installed, and Docker must be running.** Start Docker Desktop, or make sure the Docker service is running.
2. **A valid OpenAI API key should be configured in `.env`.** For a new installation, copy `.env.example` to `.env`. If `.env` already exists, use it. Set the key inside it:

   ```dotenv
   OPENAI_API_KEY=your_api_key_here
   ```

You do not need to install Python or Node.js on your computer to run the application with Docker.

All commands in this document are run from the project directory unless stated otherwise.

### Run the project

1. Run:

   ```bash
   docker compose up -d --build
   ```

2. Open **[http://localhost:8000](http://localhost:8000)** in your browser.


### Sign in with the demo account

The following user is created automatically on a new installation:

| Field | Value |
| --- | --- |
| Username | `demo` |
| Password | `AirportDemo123!` |

Once the service is running, sign in at [http://localhost:8000](http://localhost:8000) with these credentials. There is no need to run commands to create a user or load data.

On the first run, a bundle of real, preprocessed FAA and BTS data is loaded automatically: BTS years 2024–2025 and FAA TAF forecasts from the 2025 edition. The bundle is included in the project as a compressed file of approximately 52MB and lets you start analyzing without downloading and processing the source files again. Data preparation occurs before the server starts accepting requests and may take a little time.

Subsequent runs use the saved data without loading it again. Docker stores the database and data files in persistent storage named `airport-intelligence-data`, accessible to the container at `/data`. Existing users, passwords and conversations are retained after stopping and rebuilding. If a user named `demo` already exists, its password remains unchanged and is not replaced with the demo password.

The bundle contains only processed aviation data and source metadata; it does not contain conversations, user accounts or API keys. Raw source files are not included. To update to newer data, use **Check & update data** in the interface; the update downloads source files as needed and requires an internet connection.

## Access URLs

| URL | Purpose | Availability |
| --- | --- | --- |
| [http://localhost:8000](http://localhost:8000) | User interface: sign-up, login, chat and results | When running with Docker or a server that serves the built frontend |
| [http://localhost:8000/api/health](http://localhost:8000/api/health) | Server health and the active data version id | No login required |

When running with Docker, use port **8000**. All server endpoints start with `/api`; the `/api` URL itself is not an application page.

The health check returns `status: ok` and `data_version_id`. A `null` version id indicates that there is no active data version yet. The check does not validate the OpenAI key or the availability of the language model.

## Available features

| Capability | What you get |
| --- | --- |
| Ranking airports for further expansion assessment | Ranking by forecast passenger growth, capacity-pressure metrics and documented constraints. You can select a region, a US state or a group of airports |
| Congestion comparison | Comparison of 2–6 airports by delays, cancellations, taxi times and delay causes |
| Long-haul flight share | Share of departures above a distance threshold, a breakdown by aircraft configuration and the distance distribution. The default threshold is 3,452 miles |
| Signs of unmet demand | Classification based on passenger growth versus seat growth, load factor, congestion and constraints. The system does not calculate the number of missing flights |
| Follow-up questions | Retained conversation context: selected airports, year, analysis type and parameters |
| Saved conversations | Creating a new conversation and opening previous conversations belonging to the signed-in user |
| Result explanations | Tables, sources, periods, limitations, calculation details and versions under `Calculation details` |
| Data updates | Starting a refresh from the interface, tracking progress and viewing each source's status |
| Login and retries | Sign-up, login and logout, error messages and a `Retry` option for requests that can be retried |

Calculations and rankings are performed in code. The language model selects the analysis tools and writes the explanation. The system is intended for initial screening of capacity-pressure signals; it does not calculate profitability, ticket prices or real-time flight activity.

## Using the interface

After signing in, click **New conversation**, type a question and click **Send** or press Enter. Shift+Enter adds a new line. You can ask questions in Hebrew or English.

Examples:

- "Rank the airports in New England by their potential for terminal expansion."
- "Compare congestion at LAX and SNA in 2025."
- "What is the share of long-haul flights departing from ANC?"
- "Are there signs of unmet demand at SFO?"
- A follow-up in the same conversation: "And what about the previous year?"

Next to the answer, you can open **Sources** and **Calculation details** to check sources, periods, parameters and calculation versions. When data is missing, the result is marked accordingly. The analysis year must be covered by the loaded data.

Click **Check & update data** to update the data. The **Details** button shows progress and source status. A refresh reuses existing files when nothing has changed; if a refresh fails, the previous active data version is retained.

## Data sources

| Source | Use in the system |
| --- | --- |
| FAA Terminal Area Forecast — TAF | Historical passenger and activity data and forecasts for future years |
| BTS T-100 Segment | Departures, seats, passengers, distance and aircraft configuration |
| BTS On-Time Performance | Delays, cancellations, taxi times and delay causes for reporting carriers |
| BTS T-100 Summary API | Discovering the available data period, detecting changes and checking completeness |
| [`backend/app/data/reference/`](backend/app/data/reference/) | Airport identification, aliases, regions, constraints and evidence from documented sources |

The default is to load two full years of BTS data alongside the TAF edition selected during refresh. Data is stored in local versions. Each result includes the sources and periods it used; BTS data by calendar year and TAF forecasts by fiscal year are shown separately.

## API endpoints

The base URL when running locally is `http://localhost:8000`. Requests with a JSON body are sent with `Content-Type: application/json`.

| Method | Path | Purpose | Authorization and successful response |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | Create an account with `username` (3–150 letters, digits, `.`, `_` or `-`) and `password` (at least 10 characters) and sign it in | Allowed Origin; returns `200` like login, `409` if the username is taken; limited to 5 per hour per client address |
| `POST` | `/api/auth/login` | Login with `username` and `password` | Allowed Origin; returns `200`, user details, `csrf_token` and a session cookie |
| `GET` | `/api/auth/me` | Current user details and `csrf_token` | Login required; `200` |
| `POST` | `/api/auth/logout` | Invalidate the session and log out | Login and CSRF required; `204` |
| `POST` | `/api/chat` | Send a new question or a follow-up | Login and CSRF required; a complete answer with `200`, or an active request status with `202` |
| `GET` | `/api/chat/requests/{request_id}` | Chat request status and the saved answer when completed | Login required; `200`, for a request belonging to the user |
| `GET` | `/api/conversations` | List the user's conversations | Login required; `200` |
| `GET` | `/api/conversations/{conversation_id}` | Conversation details, messages and saved results | Login required; `200`, for a conversation belonging to the user |
| `POST` | `/api/data/refresh` | Start a data refresh or join an active refresh | Login and CSRF required; `202` with job details |
| `GET` | `/api/data/refresh/{job_id}` | Refresh-job status and source details | Login required; `200` |
| `GET` | `/api/health` | Server health and the active data version | No login required; `200` |

### Login and direct API usage

The interface handles login and headers automatically. When using an API client:

1. Send a request to `POST /api/auth/login` with a body containing `username` and `password`, and the header `Origin: http://localhost:8000`.
2. Save the `aiia_session` session cookie and include it in subsequent requests.
3. For `POST` requests after login, add `X-CSRF-Token` with the received `csrf_token` value and an allowed Origin. You can also retrieve the token through `/api/auth/me`.

Example body for `POST /api/chat`:

```json
{
  "request_id": "a9ec9752-5daa-43ec-8175-953ebecae98c",
  "message": "Compare congestion at LAX and SNA",
  "conversation_id": null
}
```

Create a new UUID in `request_id` for each new message. For a new conversation, send `conversation_id: null` or omit the field; to continue a conversation, send the id received in the response. Retrying the same request with the same id and body prevents duplicate messages.

A complete response includes `conversation_id`, `answer`, `results`, `sources` and `limitations`. For `202`, the response includes a `status_url` for tracking. Chat request states are `processing`, `completed`, `failed` and `interrupted`.

Errors are returned in a uniform structure:

```json
{
  "error": {
    "code": "not_authenticated",
    "message": "Sign in to continue."
  }
}
```

Common status codes: `401` for no valid session, `403` for an invalid Origin or CSRF token, `404` for a resource not found, `409` for a request conflict, `422` for invalid input, `429` for exceeding limits, and `503`/`504` for unavailability or exceeding the processing time limit.

The full contracts are in [`backend/app/api/schemas.py`](backend/app/api/schemas.py). Airport analyses are run through chat or the CLI; there is no separate endpoint for each analysis.

## Administration and useful commands

The following commands are run from the project directory. The container must be running for `exec` commands.

| Action | Command |
| --- | --- |
| Build and start | `docker compose up -d --build` |
| Container status | `docker compose ps` |
| View logs | `docker compose logs -f app` |
| Stop and remove the container while retaining data | `docker compose down` |
| Create a user | `docker compose exec app python -m app.cli create-user analyst` |
| Change or reset a password | `docker compose exec app python -m app.cli set-password analyst` |
| Disable a user | `docker compose exec app python -m app.cli disable-user analyst` |
| Re-enable a user | `docker compose exec app python -m app.cli enable-user analyst` |
| Refresh data | `docker compose exec app python -m app.cli refresh` |
| Back up the database and source files | `docker compose exec app python -m app.cli backup --dest /data/backups` |

Replace `analyst` with the relevant username. Resetting a password and disabling a user invalidate that user's existing sessions. Backups in this example are stored in `/data/backups` in Docker's persistent storage; the backup contains a consistent copy of the database, available source files and a manifest. You can copy it to your computer with `docker compose cp app:/data/backups ./backups`.

Anyone who can reach the interface can create an account from the login screen (`Sign up`); operators can also create accounts with the command above. Password changes, disabling users and backups are available only through commands. The interface has no user management screen, conversation deletion, conversation renaming or dedicated file export.

### Direct analysis without a language model

After loading the data, you can run calculations and receive JSON without using an OpenAI key:

```bash
docker compose exec app python -m app.cli analyze rank "New England"
docker compose exec app python -m app.cli analyze compare LAX SNA
docker compose exec app python -m app.cli analyze long-haul ANC --threshold 3452
docker compose exec app python -m app.cli analyze unmet SFO
```

You can add `--year` for the analysis year, `--horizon` for the ranking forecast horizon, and `--threshold` for the distance threshold in long-haul flight analysis.

## Local development and testing — optional

This section is for editing the code. For normal application usage, running Docker at `http://localhost:8000` is sufficient.

The frontend development server is a ready-made Vite tool, started with `npm run dev` at **[http://localhost:5173](http://localhost:5173)**. It shows the same interface and features as the regular application, and adds automatic updates after code changes and development error displays. This lets you edit the interface without rebuilding Docker after every change.

Requests to `/api` are forwarded from the frontend to the server on port 8000. **This is not an isolated data environment:** the frontend uses the users, conversations and database of the server it connects to.

Running the backend and frontend locally requires Python 3.12 and Node.js 22.12 or later with npm. Stop the container to free port 8000 and use the project's `.env` file. In this setup, the server stores its data in the local `data/` directory, separately from Docker's storage.

In PowerShell, from the `backend` directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:AUTO_BOOTSTRAP = "true"
.\.venv\Scripts\python.exe -m uvicorn app.main:app_factory --factory --reload --port 8000
```

In another window, from the `frontend` directory:

```powershell
npm ci
npm run dev
```

After starting both servers, open the development URL above. On macOS/Linux, you can create the environment with `python3.12 -m venv .venv` and use paths under `.venv/bin/` instead of `.venv/Scripts/`.

To test the interface with mock data, open [http://localhost:5173/?demo](http://localhost:5173/?demo). Demo mode is an addition built into the project code and available only in development: it simulates the API and returns mock responses instead of contacting the server and running real analyses.

Backend tests, from `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check app tests scripts
.\.venv\Scripts\mypy.exe
```

Frontend tests and build, from `frontend`:

```powershell
npm run typecheck
npm test
npm run build
```
