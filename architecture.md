# Airport Investment Intelligence Agent — Design and Architecture

This is the primary design document: it explains the architecture, scoring methodology,
AI usage and key tradeoffs. The [optional detailed appendix](key-tradeoffs.md) expands on
the decisions and alternatives.

## 1. What the system does

Analysts at a firm that invests in US airport terminal modernization ask questions in natural
language, for example:

- *Which airports in New England are strong candidates for terminal expansion?*
- *Compare LA and Santa Ana airport congestion levels.*
- *What is the percentage of long haul flights out of Anchorage airport?*
- *What is the unmet flight demand in SFO airport and why?*

Each answer combines a short explanation written by the language model with structured result
tables, data sources, periods and limitations that come from deterministic calculations over
public FAA and BTS data. Follow-up questions ("and the previous year?", "use 2,500 miles")
reuse the stored analysis context of the conversation.

## 2. Architecture

```
 Browser (React + TypeScript, single screen)
   │  HTTP/JSON, same origin, session cookie + CSRF header
   ▼
 FastAPI  (app/api)  ── auth, validation, rate limits, idempotent chat requests
   │
   ├── Agent (app/agent) ── bounded loop: OpenAI Responses API ⇄ 5 registered tools
   │        │                (model chooses tools and explains; never computes)
   │        ▼
   ├── Analysis (app/analysis) ── deterministic metrics, signals, classification, ranking
   │        │
   │        ▼
   ├── Data (app/data) ── versioned, normalized records + reference catalog (airports, aliases,
   │        │              regions, constraints, infrastructure evidence)
   │        ▼
   └── Storage (app/storage.py) ── SQLite: accounts, sessions, conversations, requests,
                                   analysis context, data files, data versions, refresh jobs

 Refresh job (background thread / CLI) ── downloads FAA TAF, BTS T-100, BTS On-Time and the
                                          T-100 summary API, then activates a new data version
```

The backend is one modular FastAPI application. Requests flow API → agent → analysis → data,
and all SQL lives in `app/storage.py`. Data ingestion runs separately from conversations, so a
chat never waits for a download and a refresh never changes data in the middle of a calculation.

### Request lifecycle

Each message has a stable `request_id`. The server saves the question before processing,
runs the agent outside a database transaction, then saves the answer, results and updated
context together before returning them. Retries reuse saved answers, and time-limited
leases prevent outdated attempts from overwriting newer results.

## 3. Data

| Source | Use | Access |
|---|---|---|
| FAA Terminal Area Forecast (TAF) 2025 | Passenger enplanements and operations, historical and forecast fiscal years | Annual ZIP of Excel files |
| BTS T-100 Segment (All Carriers) | Departures, seats, passengers, distance, service class, aircraft configuration | TranStats download form, one file per calendar year |
| BTS Reporting Carrier On-Time Performance | Delays, cancellations, taxi times, delay causes (domestic, reporting carriers) | Monthly PREZIP files |
| BTS T-100 Segment summary API | Discovery of the latest published month, change fingerprint and integrity check of T-100 totals | JSON API, no key |
| Curated reference records | Airport identity mapping, aliases (English/Hebrew), regions, documented constraints, infrastructure evidence | Version-controlled files with official source links |

- **Versions.** Data versions pin source files and national references. Refreshes prepare a
  new version separately and activate it atomically; failures preserve the previous version.
  Results retain data, rules and reference-data versions for reproduction.
- **On-Time aggregation.** Airport-month aggregates retain counts, taxi-time histograms and
  delay-cause minutes, preserving exact medians and percentiles while keeping the database small.
- **Identity.** Verified mappings connect internal airport ids to FAA and BTS ids; matching
  never relies on name or code similarity alone.
- **Missing data stays missing.** Nulls are never replaced by zero; a zero denominator only
  removes the metric that depends on it.
- **Reference provenance.** Curated constraints and evidence were checked against official
  publications, with source links, check dates, periods and caveats retained in the records.
  A check date does not make an old plan current, and missing records do not prove the absence
  of a constraint or an expansion need.

## 4. Scoring methodology

The ranking is a deterministic screening of capacity pressure. It does not estimate returns.

**Cohort.** Airports in the requested region or state with at least 10,000 enplanements in the
TAF base year (FAA primary airport threshold).

**Primary metric — forecast passenger increase.** `ΔE = E(b+5) − E(b)`, where `E` is total TAF
enplanements (domestic air carrier, air taxi, commuter and international enplanements) and `b`
is the last historical fiscal year of the edition (FY2024 in TAF 2025). It measures the
additional volume a terminal would need to serve. `CAGR_5` is shown next to it, and the
change in commercial operations is shown separately.

**Three pressure signals**, each `pass`, `fail` or `unknown`, against national references
computed from the full source data (so adding an airport to a question never changes the
results of the others):

| Signal | Calculation | Passes when |
|---|---|---|
| S1 demand growth | Airport `CAGR_5` (TAF) | ≥ `CAGR_5` of all TAF airports combined |
| S2 tight supply | Load factor = 100 · Σpassengers / Σseats, outbound scheduled passenger service (class F), year `t` | ≥ national load factor (US-origin segments) |
| S3 operational congestion | Taxi-out excess = median − 10th percentile of taxi-out minutes (On-Time), year `t` | ≥ national median of the excess (airports with ≥ 1,000 departures) |

The 10th percentile approximates unimpeded taxi time, so S3 reflects congestion rather than
airport size or layout. Percentiles use grouped-data interpolation because taxi times are
reported in whole minutes.

**Documented constraint flag.** A small manual table of official, linked records of limits
outside the terminal (runway/airspace, night curfew, legal passenger caps, slot control).
It is the only manual input to the ranking and only moves an airport to a separate group.

**Classification** (first matching rule wins):

1. Documented constraint → *Known constraint outside the terminal*
2. S1 passes and S2 or S3 passes → *Strong candidate*
3. At least one signal passes → *Watch*
4. No signal passes → *Low pressure*

If a signal is unknown, the rules are evaluated for every possible completion; if the group
could change, the airport is classified *Information needed*. Within each group airports are
sorted by `ΔE`, then by load factor. Strong or watch airports without terminal-capacity
evidence on file are marked *candidate for further review*.

**Other analyses.**

- *Congestion comparison* — On-Time metrics side by side (departures per day, departure delay
  rate, cancellation rate, taxi-out and taxi-in times, delay-cause shares). No combined score.
- *Long-haul share* — share of departures performed above a distance threshold (default
  3,452 statute miles ≈ 3,000 nautical miles) out of departures with a valid distance, split by
  aircraft configuration (passenger/freight) and shown with the full distance distribution.
- *Unmet-demand signs* — constrained if S2 passes, passenger growth exceeds seat growth and S3
  passes or a constraint exists; weak if S2 fails and passengers do not outgrow seats; otherwise
  undetermined. The "why" comes from monthly delay causes and documented constraints. It
  classifies signs and never counts "missing flights".

## 5. Where and how AI is used

| Responsibility | Implementation |
|---|---|
| Understanding the question, choosing tools and parameters, and asking clarifying questions | A language model through the OpenAI Responses API. The model is configurable; the default is `gpt-6-luna`. |
| Resolving airport and region names | A deterministic resolver based on reference records collected in advance. |
| Calculating numerical values, pressure signals, classifications and ranking order | Deterministic Python code in the analysis layer (`app/analysis`). |
| Result tables, sources and limitations shown to the user | Assembled from validated tool outputs. The content is not taken from model-generated text. |
| Explaining the results in the user's language | A language model that grounds the explanation in tool outputs. |

- The model can call only five predefined tools. Pydantic validates arguments, and the server
  checks airport ids against the reference catalog.
- Region/state rankings are expanded on the server from resolved region ids, preventing airport
  omissions or a partial selection being labeled as a complete region. Model input is
  size-bounded; users receive the full validated tables.
- Model rounds, tool calls and total runtime are bounded. Transient provider failures use
  centrally managed retries with backoff, jitter and `Retry-After`.
- If explanation generation fails after a successful analysis, the system returns validated
  results with a deterministic summary and an explicit limitation.
- User text, tool outputs and source data are treated as data rather than instructions. The
  model cannot execute code or SQL, access files or URLs, or trigger refreshes.
- Each request sends bounded history and structured analysis context with `store=false`;
  conversation state is managed by the application.
- The analyses also run without a language model: `python -m app.cli analyze rank "New England"`.

## 6. Key tradeoffs

| Decision | Benefit | Cost or limitation |
|---|---|---|
| Rule-based classification against national references instead of a weighted 0–100 score | Transparent, no arbitrary weights, results independent of the selected airports | Coarser ordering; "above the national average" is a signal, not proof of need |
| Absolute `ΔE` as the primary metric | Expresses the extra volume a terminal must serve | Favors large airports; not a return per dollar |
| TAF forecast growth instead of historical growth | Historical growth at a constrained airport reflects the constraint, not demand | TAF itself starts from constrained bases and may understate demand |
| Demand–capacity gap model deferred | No comparable public terminal-capacity data exists | Unmet demand is classified, not quantified |
| Curated constraints and evidence with official links | Captures limits the core datasets do not contain | Absence of a record does not prove absence of a constraint |
| Direct OpenAI SDK and a hand-written tool loop instead of a framework | Small, testable, easy to follow | Provider switch requires adapting one module |
| Pre-built, versioned local data with an explicit refresh | Consistent, reproducible analyses; chats never wait for downloads | Data is as fresh as the last refresh; periods and retrieval times are disclosed |
| SQLite with short transactions and leases | Simple single-container deployment with transactional guarantees | One writer at a time; horizontal scaling would need a server database |

Further reasoning, alternatives and limitations are in the [optional detailed appendix](key-tradeoffs.md).

## 7. Assumptions, uncertainty and scope

- This is an initial screening, not an engineering, regulatory or financial assessment.
- On-Time covers domestic flights of reporting carriers only, so S3 can be unknown at small
  airports; load factor reflects airline supply decisions rather than terminal utilization.
- NAS delay includes weather and air traffic control effects and is not evidence of a terminal
  shortage; a delayed flight is not proof of a missing flight.
- Historical analyses use full calendar years; TAF uses federal fiscal years and its values after
  the base year are forecasts. The two are labeled separately everywhere.
- Ticket prices, live weather and live operations are out of scope for this version.

## 8. Reliability and security

- Operator-managed accounts use Argon2id hashes and server-side sessions in
  `HttpOnly`/`SameSite=Lax` cookies. CSRF/origin checks and ownership checks protect private
  data; requests for other users' data return `404`.
- Limits cover input size, request rate, concurrent work and agent execution. Errors omit
  internal details; logs exclude conversation content and secrets.
- Downloads use allowed official hosts, size and redirect checks, and safe ZIP handling.

## 9. Verification

- **Automated checks:** 274 backend tests covering calculations, data, refresh, API and the
  agent loop; 69 frontend unit tests. Ruff, mypy and the TypeScript build passed.
- **Real data:** TAF 2025, T-100 Segment 2024–2025 and On-Time 2024-01 to 2025-12.
  Anchorage departures were reconciled with the BTS summary API.
- **Exam scenarios:** browser checks with `gpt-5.6-luna` covered all four exam questions,
  follow-ups by year and distance threshold, and Hebrew ambiguity handling. The default
  `gpt-6-luna` was checked through the agent service; responses took 4–10 seconds.
  In both runs every number matched the deterministic results.
- **Docker:** the image was built and run with persistent storage, a non-root user and one
  origin for UI/API; database files, data files and `.env` were not accessible over HTTP.
