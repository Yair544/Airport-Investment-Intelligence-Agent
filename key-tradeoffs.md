# Key Tradeoffs — Architectural decisions and tradeoffs

This is an optional detailed appendix to the [primary design document](architecture.md).

This document presents the architectural and methodological decisions I made in the project, the alternatives I considered, and the benefits and limitations of each choice.

## AI provider: direct integration with OpenAI

I chose to integrate directly with OpenAI through the [Responses API](https://developers.openai.com/api/docs/guides/function-calling). As an alternative, I considered [OpenRouter](https://openrouter.ai/docs/quickstart), which provides access to models from different providers through one interface and makes switching between them easier.

For an assignment of approximately one day, I preferred a focused integration with one provider so I could spend development time on data quality, analysis and reliable answers. A direct integration reduces the number of services involved and allows me to work with the provider's own interface and documentation.

The tradeoff is dependency on OpenAI: switching to another provider requires adapting the communication layer and repeating tests. I therefore decided to isolate provider communication in a separate component to limit the changes needed when switching providers.

I decided to keep the model name in the system configuration and separate it from the provider communication code.

## Communication library and agent orchestration

I chose the official [openai](https://developers.openai.com/api/reference/python) Python library. I decided to manage the process in which the model requests calls to system functions and receives their results myself.

I considered using [LangChain](https://docs.langchain.com/oss/python/langchain/overview), which offers ready-made infrastructure for agents and integrations. For one agent and a defined set of tools, I preferred direct code that is easy to follow, test and maintain.

The tradeoff I accepted was responsibility for implementing conversation management, tool-request validation, execution limits and error handling myself.

## Data sources: three public datasets

I chose to base the system on three core datasets from two organizations: BTS T-100 for activity, passengers, seats and distances; BTS Reporting Carrier On-Time Performance for delays, cancellations and taxi times; and FAA Terminal Area Forecast for passenger and operation forecasts. I chose sources accessible without payment, registration or a data API key to reduce setup barriers for the evaluator.

I decided to combine the public T-100 summary API with the detailed data files. I retained the API's role of summarizing activity by airport and month, and chose the detailed data for calculations requiring destinations and service types, including the long-haul flight share from Anchorage. I decided to separate data ingestion and refresh from conversation management.

I assigned the summary API three roles: discovering the latest published month, providing a low-cost change fingerprint for a T-100 year, and checking the completeness of the detailed data against totals by airport and month. As an alternative, I considered downloading the T-100 file again on every refresh. I preferred the fingerprint because downloading the national file through the TranStats form is slow. Summary data does not participate in calculations and is therefore not counted twice. The tradeoff is dependency on an additional service for change detection.

I chose to download On-Time data from the official monthly PREZIP files and T-100 through the TranStats download form, because it has no static file. I considered downloading On-Time through the form with selected fields as well to save space. I preferred the static files because their URLs are stable and allow inexpensive change checks using ETag and Last-Modified. The tradeoff is an initial download of approximately 700 megabytes for two years.

As an alternative, I considered adding OurAirports, ticket-price data, and live flight and weather services. I preferred to limit the number of sources for an assignment of approximately one day to reduce integration work, reconciliation of definitions and dependency on external services. I relied on the basic information in the BTS and FAA datasets for airport identification and geographic filtering.

The tradeoff I accepted was a limitation in identifying unmet demand from the core datasets alone. I chose to use activity metrics and indirect signals to support the analysis, and to quantify a gap only with matching evidence of demand and capacity. I decided to present the assessment alongside its assumptions and limitations, and clarified that a delayed flight is not, by itself, evidence of a missing flight. I also accounted for the coverage limitations of delay data and the lack of a uniform measure of terminal capacity in the core datasets.

I decided to collect official FAA or airport-operator evidence on demand, capacity, constraints and expansion plans in advance for the airports to be assessed. I chose to store it as structured reference data with a precise source, date and applicable period, distinguishing between the existing situation, the future without the project and the future with it. I specified that the evidence would be shown alongside ranking results without changing the classification, so the constraints table remained the only manual input to the ranking. I specified that when suitable infrastructure information was missing to support a terminal-expansion recommendation, the gap would be shown and the airport marked as a candidate for further review, without asserting a proven need for expansion.

## Airport identification and data integration

I chose a stable internal id for each airport and an explicit mapping to BTS and FAA identifiers. I decided to resolve requests using codes, names and predefined aliases in Hebrew and English, using conversation context to resolve ambiguous names. I preferred a verified mapping over matching by code or name similarity, and specified that any remaining ambiguity would lead to a clarifying question. The tradeoff I accepted was the need to maintain the mappings and alias list.

I chose to convert the sources into five shared record types: airports, monthly activity, flight performance, annual forecasts, and demand and infrastructure evidence. As an alternative, I considered calculating directly against each source's structure. I preferred a shared structure to keep definitions and units consistent and reduce calculation dependencies on provider column names. The tradeoff was adaptation work for each source while preserving the detail and classifications required for calculations.

I chose to store On-Time data as airport-month aggregates rather than individual flight rows: counts, histograms of taxi times in whole minutes, and delay minutes by cause. As an alternative, I considered storing every flight, approximately seven million rows per year. I preferred the aggregates because they preserve exact medians and percentiles while greatly reducing database size. The tradeoff is that a new analysis at individual-flight level would require re-ingestion from the source files, which are retained for this purpose.

I limited historical analysis periods in the first version to full calendar years. I considered allowing month ranges, but preferred full years because national references, growth calculations and rankings require complete, comparable periods. The limitation is that this version does not support seasonal or partial-year analysis.

I specified that when the user does not select a period, the default would be the latest full calendar year available for the data required by the analysis. I preferred a complete comparison period over automatically using the newest months, at the cost of less current data. I decided to show the period explicitly and keep fiscal-year forecasts separate from calendar-year history.

I decided to retain the source, observation period, retrieval time and dataset version, when available, with the records. I specified that missing values would remain marked as missing, duplicates would be examined according to record characteristics, and aggregates and their underlying data would not be counted together. I preferred to reflect coverage gaps rather than fill values without support from the source.

## Metrics and methodology for ranking expansion candidates

I chose deterministic, rule-based ranking, calculated in code from the three core datasets.

- **The primary metric** is the five-year increase in passenger enplanements forecast by TAF, `ΔE`.
- **Three pressure signals:** forecast demand growth, seat load factor and taxi-out excess.
- **A flag** for a documented constraint outside the terminal.

The classification rules assign each airport to a group, and airports within each group are sorted by `ΔE`. The language model explains the result rather than determining it. I defined the full methodology in [section 4 of the architecture document](architecture.md#4-scoring-methodology).

### Alternatives I rejected

**A weighted score from 0 to 100.** I considered a weighted sum of historical growth, forecast growth, size and load factor, and rejected it for several reasons:
- The weights were arbitrary, without calibration.
- Normalization hides the size of the opportunity.
- Each airport's score changes depending on the other airports selected.
- Such a combination can reward an airport that is already growing and penalize one whose growth has stopped because of a constraint.

Instead, I chose threshold rules against national references and sorting by one metric in physical units.

**A demand–capacity gap model.** I considered ranking by the additional activity enabled by expansion:

`ΔActivity = min(D, C1) − min(D, C0)`

Here, `D` is demand, `C0` is capacity without the project and `C1` is capacity with the project. The model compares the situation with the project to the situation without it, as in the [FAA benefit–cost analysis guidance](https://www.faa.gov/regulations_policies/policy_guidance/benefit_cost/FAA-Airport-Benefit-Cost-Guidance.pdf).

Conceptually, this is the more appropriate model:
- It distinguishes weak demand from blocked growth.
- It does not credit an expansion with capacity for which there is no demand.
- It does not attribute flights to a terminal expansion when a runway constraint prevents them.

I nevertheless rejected it for the first version for three reasons:
- The public datasets do not contain uniform, comparable terminal-capacity data. The inputs would require manually reading each airport's master plans.
- Even after that collection effort, most airports would lack a matching estimate, leaving the ranking nearly empty.
- The inputs would reflect the judgment of whoever read the documents, whether a person or a language model. The rules would be deterministic, but the actual decision would not be based on the data.

I retained the model as a direction for the next version, together with structured ingestion of master plans.

### Decisions on metrics

| Decision | Rationale and tradeoff |
|---|---|
| Absolute `ΔE` as the primary metric | The metric expresses the additional passenger volume the terminal would need to serve. It is the closest publicly available proxy for the opportunity defined by the assignment. The tradeoff: it favors large airports and does not measure return per unit of investment. `CAGR_5` is shown alongside it and used in signal S1. |
| TAF forecasts rather than historical growth | At a constrained airport, historical growth reflects the constraint rather than demand. TAF is a consistent FAA forecast across all airports. The tradeoff: past constraints are reflected in the forecast through its baseline data ([TAF methodology documentation](https://taf.faa.gov/downloads/finalforecastprocessfor2024taf.pdf)), so it may understate demand at a constrained airport. Signals S2 and S3 and the constraint flag partially compensate for this. |
| Thresholds against national references | The reference is calculated from all the data in the source, so adding an airport to a query does not change the results of other airports. The tradeoff: "above the national average" is a sign of pressure rather than a proven need. |
| Taxi-Out excess rather than raw taxi time | The difference between the median and the 10th percentile at the same airport separates congestion from airport size and layout. The 10th percentile approximates unimpeded taxi time; it is not the official FAA metric. |
| A manual constraint flag | Constraints such as a night curfew, legal cap or runway limitation do not appear in the core datasets. I chose a small table with a source link in every row. The flag moves an airport to a separate group and does not change the sorting. The tradeoff: the absence of a row does not prove the absence of a constraint. |
| Handling missing data | The rules are evaluated for every possible completion of an unknown signal. When those completions lead to different groups, the airport is classified as "Information needed", so missing data does not count for or against it. The tradeoff: more small airports, which have limited On-Time coverage, end up in this group. |
| National LF from US-origin segments | T-100 also includes segments originating abroad. I limited the reference to segments departing from the United States so it compares the same population as the airport LF. The tradeoff: inbound international segments do not affect the reference. |
| Percentiles using grouped-data interpolation | Taxi times are reported in whole minutes, so ordinary percentiles converge on the same integer values at many airports. Interpolation distinguishes airports with close values. The tradeoff: the value is an estimate within a one-minute interval rather than an observed time. |
| Only binding constraints in the constraints table | I included night curfews, legal caps, runway limitations and binding FAA operating limits. I did not include voluntary schedule facilitation (Level 2) because it does not limit activity. The tradeoff: a congested airport without a binding constraint does not move to the constraint group. |
| Completing missing inputs for unmet demand as well | I applied the same completion logic as in the ranking, so a missing input prevents a decision only when it could change the result. The tradeoff: explanations of blocking inputs are longer. |

### Supporting metrics

I retained the arithmetic definitions of the supporting metrics:
- **Load factor** is calculated as a ratio of sums rather than an average of percentages. For example, 90 passengers out of 100 seats plus 10 out of 1,000 yield a combined load factor of approximately 9.09%, rather than 45.5%.
- **Departure delays** are compared separately from arrival delay causes.
- **Long-haul flights** are counted by departures performed rather than the number of destinations.

For long-haul flights, I chose an adjustable threshold of more than 3,000 nautical miles. This is a threshold I chose rather than a single accepted definition, so the result also includes the distribution by distance bands. At Anchorage, the result is split into passenger and freight operations. The calculation includes all service types, and segments with missing or zero distance are excluded and shown as a separate share.

For unmet demand, I chose a deterministic classification of signals into three groups: constrained demand, weak demand or undetermined. I rejected quantifying missing flights or passengers because the available data provides no basis for doing so. I did not infer profitability or returns from any metric.

## Agent tools and conversation context management

I chose five task-level tools: resolving airports, ranking candidates, comparing airports, calculating the long-haul flight share, and examining signs of unmet demand. As an alternative, I considered exposing data tables and unrestricted queries to the model. I preferred defined tools so each tool would contain its analysis rules and keep calculations consistent and executable without a language model. The tradeoff I accepted was less flexibility for questions not covered by the tools, and the need to add or extend a tool to support a new capability.

I specified that a ranking by region or state would receive a region id from the resolver tool, and the server would expand it to all airports in the region using reference data. As an alternative, I considered accepting the airport list and region name from the model. I preferred server-side expansion so the model could not omit airports from a region or label a partial selection with the name of an entire region. For the same reason, I bounded the output sent to the model: in a large ranking, full details are sent for the leading airports and a shortened row for all others, while the full table is shown to the user from the validated result. The tradeoff is a tool with two input forms, and the model sees less detail about airports at the bottom of a large ranking.

I decided that the model would identify user intent, choose tools and explain their output, while code would validate inputs and calculate metrics, classifications and sorting. I specified that tables and sources would come from structured, validated output. I preferred this division over calculation or ranking by the model to make results reproducible and reduce numerical errors and changes to classifications when wording the answer. The tradeoff I accepted was the need to define and maintain input and output contracts for each tool.

I chose to retain the airports, period, parameters and latest result as structured context alongside a bounded conversation history. I preferred this over relying solely on the conversation transcript so follow-up questions would preserve previous choices and update the details the user changed. I specified that material ambiguity would lead to a clarifying question and that defaults would be shown in the answer. The tradeoff I accepted was the need to update the context explicitly whenever the analysis changed.

I set limits on model rounds, tool calls and execution time, and decided to show failures and missing data explicitly. I preferred ending a request with a partial result and its limitations over continuing tool calls indefinitely or stating an unsupported conclusion. The tradeoff I accepted was that a complex request might not finish within the limits. I implemented the tools and loop and tested them with automated tests using a mock model.

I chose to send the conversation to the model provider without provider-side storage (`store=false`) and return the encrypted reasoning items produced by the model in each round. As an alternative, I considered referring to the previous response through an id stored by the provider. I preferred a conversation without provider-side state to reduce the information stored outside the system; the authoritative conversation history is already retained in the database. The tradeoff is that each round resends the required context.

I specified that if the model explanation failed after an analysis had completed and been validated, the server would return the results with a fixed summary built from them and an explicit limitation about the missing explanation. As an alternative, I considered returning an error for the entire request. I preferred to preserve the supported information, and the tradeoff is a less detailed answer.

## API contract and response delivery

I chose HTTP communication and JSON, with endpoints for login, identifying the current user and logout, a chat endpoint for sending messages, endpoints for reading conversations and checking request status, and endpoints for starting and monitoring data refreshes. As an alternative, I considered exposing a separate endpoint for each analysis type. I preferred to centralize analysis requests in the chat endpoint so the agent would choose tools and retain conversation context in the same process. The tradeoff I accepted was dependency on the chat endpoint to run analyses from the user interface, while retaining the calculations as independent operations in the analysis layer.

I specified that the server would create an id on the first message and load the stored context for follow-up messages. I preferred this over resending the conversation transcript and analysis context from the interface with every request, to centralize context management on the server. The tradeoff I accepted was responsibility for server-side conversation persistence and loading. I specified that a nonexistent id would lead to an explicit error to avoid accidentally starting a new conversation and losing the context the user intended to continue.

I chose a fixed final response envelope with `conversation_id`, `answer`, `results`, `sources` and `limitations`. As an alternative, I considered returning text alone. I preferred to separate the explanation from structured results so the interface would display tables and sources from validated data and retain the parameters and versions needed for reproduction. The tradeoff I accepted was the need to maintain a shared contract between the server and user interface.

I chose to return a complete final chat response in the first version. I considered streaming the answer incrementally, but preferred to reduce the complexity of presenting results, sources and errors before the analysis had finished. I decided that a duplicate request still being processed would return `202` with a link to the request status, and that refresh-job status would be read separately. The tradeoff I accepted was waiting for the analysis to finish before showing response content and the need to monitor request status when no final answer is available.

I specified that the interface would create a `request_id` for each message and reuse it for retries, and that reading status would not run the agent again. I decided to interpret the id within the authenticated account at all times and check ownership when returning a saved response as well. I preferred this over sending a new message after every disconnection to allow retrieval of an already saved response and prevent duplicates without exposing another account's data. The tradeoff I accepted was the need to store the request id and original content, and distinguish active, completed and interrupted requests in the interface.

I decided to distinguish a technical error from a processed request that ended with a clarification, missing data or a partial result. I specified a uniform error structure and empty arrays when there are no items to display. I preferred this over treating every data gap as a service failure so the interface could explain the result and its limitations to the user. The tradeoff I accepted was the need to handle valid responses with no analysis results as well.

I set a maximum of three processing attempts per request and a stable error code for each `409` condition: `request_id_conflict`, `conversation_busy` and `stale_request`. I preferred explicit codes over status alone so the interface could choose the appropriate action. The tradeoff is a broader error contract to maintain. I implemented the endpoints and tested them with automated tests using a mock agent.

## User interface and result presentation

I chose one workspace screen after login, containing the user's conversations, chat, results, sources and limitations. I decided to attach each table to the answer it belongs to. As an alternative, I considered separate screens for conversation and analysis. I preferred to keep the request, result and follow-up questions in the same place to make the context easier to understand. The tradeoff I accepted was the need to scroll through long conversations containing several analyses.

I chose tables suited to each analysis type, with the classifications and ranking order returned by the server, alongside periods and measurement units. I preferred these over text-only findings to allow direct comparison between airports and keep validated values visible. I specified that historical data and forecasts would be clearly labeled. The tradeoff I accepted was the need to adapt the table structure to each analysis type.

I decided to show the assumptions and limitations affecting the conclusion next to the result, and allow users to open the full source and calculation details as needed. I considered displaying every detail in full in every answer, but preferred to reduce visual clutter while retaining access to the values, parameters and versions needed to understand and reproduce the result. The tradeoff I accepted was an additional user action to see the full details.

I specified separate displays for waiting, errors, missing data and partial results, as well as preserving typed text when an error occurs. I decided to prevent another submission while a request was processing and display a missing value as "No data". I preferred to make the request status and result boundaries clear to the user rather than show an empty or ambiguous display. The tradeoff I accepted was allowing one message at a time until the request finished. I implemented the interface and tested it with unit tests and in the browser against the server, real data and language model.

I chose to add a login screen and logout button and clear private interface state when switching accounts. I specified that drafts, late results and retries from a previous account would not carry over to the new account. I preferred this over automatically preserving screen state to prevent content exposure between users in the same browser. The tradeoff I accepted was losing an unsent draft when switching accounts; information already saved on the server remained associated with its owner.

## Persistence, recovery and data refresh

I chose persistent storage as the authoritative source for conversations, results and analysis context, with memory used for processing and a rebuildable cache. I retained SQLite as the engine selected for a single-server deployment and centralized access in `backend/app/storage.py`. As alternatives, I considered memory alone and JSON or JSONL files. I preferred a database with transactions, uniqueness constraints and queries over implementing consistency between messages, requests and context myself. The tradeoff I accepted was writing to persistent storage and responsibility for backing it up. I based the separation of cache and storage on [Microsoft's Cache-Aside pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/cache-aside).

I decided to save the user's question and request status before processing began, and to save the answer, results and context together before returning a successful response. I specified that model and tool execution would occur outside an open database transaction. I preferred two short transactions at request boundaries over saving only at the end or accumulating several messages in memory before saving them. This separated durable acceptance of the question from completion of the answer, while preventing partial persistence of the result and context. The tradeoff I accepted was write latency at both boundaries and, on failure, an additional status update; I did not specify that the number of database operations per request would always be two. I based the principle of saving related data together on [atomic transactions](https://www.sqlite.org/atomiccommit.html).

I chose a stable request id across retries, database-enforced uniqueness of the `(owner_id, request_id)` pair, and validation that the original content matches. I decided to return a saved answer to its owner rather than add a duplicate message or answer. I based the choice on [Amazon's guidance on safe retries](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/). As an alternative, I considered relying only on disabling the send button. I preferred server-side protection to cover disconnections and concurrent requests as well. The tradeoff I accepted was retaining request records and execution states alongside messages.

I specified one active request per conversation and a time-limited lease for each processing attempt. I decided that a result would be saved only if the attempt id and lease still matched, and that a request without a valid lease would be marked as interrupted before a new attempt. I specified that resuming a failed request would be rejected if a newer request had already been accepted in the conversation, to avoid updating new context with an old message. I preferred this over an in-memory lock in a single process or marking all active requests as failed on every restart. The tradeoff I accepted was the need to manage lease expiry and set appropriate execution times. I chose to preserve history and completed results without adding a checkpoint for every internal agent step. I explicitly accepted that a calculation or external call whose result had not been saved might run again; I did not guarantee that every model call would execute only once after a crash.

I chose a prebuilt local data version and an explicit refresh button, with refresh running as a background job separate from the conversation. I decided to check corrections to existing periods as well, retain content fingerprints and avoid reprocessing unchanged files. I chose this because BTS sources are published monthly and include historical revisions, as shown in the [BTS release schedule](https://transtats.bts.gov/releaseinfo.asp). I accounted for TAF's annual cycle described in the [FAA document](https://www.faa.gov/media/6186) and the possibility of changes between editions noted on the [official TAF page](https://www.faa.gov/data_research/aviation/taf). As an alternative, I considered retrieving data as part of every conversation. I preferred to separate ingestion to preserve a consistent version for analysis and keep the previous version available during refresh. The tradeoff I accepted was dependency on triggering refresh and the need to show data periods and retrieval times explicitly.

I chose to associate each processed record with its source file and identify files by a SHA-256 hash. A data version is a list of files, one per source and period, together with the national references calculated from them. As an alternative, I considered copying every record for each version. I preferred file association because an unchanged file can serve several versions without reprocessing or duplication. The tradeoff is that every query must filter by the version's files.

I decided to prepare a new version separately and activate it only after ingestion and preparation of the national reference had finished. I specified that each analysis would use an identified version, that a follow-up repeating the same analysis type would retain the previous analysis version when it covered the request, that a question of another type would use the active version, and that old results would not be replaced by a refresh. When a follow-up stays on an old version while a newer version is active, the answer states this explicitly. I preferred this over gradually updating active data while analyses read it. The tradeoff I accepted was retaining several versions and source files needed for reproduction, at the cost of additional storage.

I chose persistent storage and a consistent backup of the database and required data files to a separate destination, and distinguished recovery from a process crash from restoration after storage loss. I specified that storage performance would be assessed by response time, write load and lock waits. I rejected the number of calls alone as a reason for the choice, and reliance on the assignment's size or the machine running it as a basis for durability. I accounted for SQLite allowing one writer at a time and for deployments with many concurrent writers requiring consideration of a server database, in line with [SQLite's guidance](https://www.sqlite.org/whentouse.html). I did not change the engine choice as part of this decision. I implemented the persistence, backup and refresh mechanisms and tested them with automated tests and runs on real data.

## Reliability, private accounts and agent boundaries

I chose separate accounts and private conversations for each user rather than a shared workspace for everyone with access to the system. I decided to identify the user from a server-validated login rather than an id sent in the request body or chosen by the model. I specified ownership checks on every read and change of private information, including status reads, retries and cached output. I preferred to return `404` when a private resource did not belong to the user so its existence would not be disclosed. The tradeoff I accepted was adding user association, filtering and constraints throughout the request flow. I based the choice on [OWASP's authorization guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html).

I chose username-and-password login, with self-service sign-up from the login screen alongside operator-created accounts, and password resets and account disabling kept to an authorized operational procedure. I limited sign-up per client address and required the same minimum password length as the operator command. I considered an external identity provider and preferred not to make running the system depend on setting up another identity-service integration. I chose Argon2id hashing with a random salt through a maintained library, following [OWASP's password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html). The tradeoffs I accepted were that anyone who can reach the deployment can open an account, so a deployment exposed beyond a trusted network should sit behind network-level access control, and operational responsibility for password resets and authentication-library updates.

I chose server-side sessions identified by an `HttpOnly` cookie rather than storing a JavaScript-accessible authentication token in browser storage. I specified expiry, server-side invalidation on logout, a new random id on every login, and storing a hash of the id in the database. I decided to combine CSRF protection and origin checks for state-changing session-based requests, and use HTTPS and a `Secure` cookie for remote deployment. The tradeoff I accepted was server-side session management and additional handling of state-changing requests. I relied on [session management guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) and [OWASP's CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

I decided to share public aviation datasets and refresh-job status across accounts while separating conversation content, user context and private analysis outputs. I specified that a private analysis cache key would also include the account owner. As an alternative, I considered a shared analysis cache keyed only by parameters. I preferred clear separation to prevent returning another user's private content. The tradeoff I accepted was possible repeated calculations and storage of similar results for several users. I specified that the refresh-monitoring interface would not expose account details.

I chose time budgets, round and tool limits, input and rate limits, and retries restricted to transient failures. I specified that retries would be managed in one layer per integration and that library-internal retries would count toward the budget as well. I preferred this over retries at every layer or unbounded waits to prevent multiplying load and costs during a failure. The tradeoff I accepted was the possibility of ending a request with a controlled failure even when another attempt might have succeeded. I based increasing waits and random jitter on [Amazon's timeout and retry guidance](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/).

I chose to run the server in a single process and keep rate limits in process memory. As an alternative, I considered a shared store for limits, such as Redis. I preferred the simple solution because the deployment consists of one container and SQLite allows one writer at a time. The tradeoff is that limits reset on restart, and a deployment with several processes would require a shared store.

I chose to allow an independent analysis that had already completed and been validated to be shown with a fixed explanation when model explanation generation failed. As an alternative, I considered returning an error for the entire request even when results were available. I preferred to preserve the supported information and show the limitation explicitly. I specified that an error would be returned when there was no valid result, and that using a previous data version as a fallback would include its source, period, retrieval time and the reason. The tradeoff I accepted was a less detailed answer while preserving the distinction between a complete result, a partial result and a failure.

I decided to limit the agent to preregistered tools and enforce arguments, entities and permissions in code. I chose to treat user content and external sources as information that does not grant authority to act. I preferred this over relying on model instructions alone to reduce the effect of injected instructions in sources and incorrect output. I specified that data refresh would be triggered through the authorized interface and that the agent would not receive code execution, unrestricted SQL or arbitrary access to files and URLs. The tradeoff I accepted was limited flexibility and the need to extend tools explicitly when adding a capability. I relied on [OWASP's prompt injection prevention guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html).

I chose to keep keys on the server, limit context sent to the provider to information required for the request, and display content without executing HTML or scripts. I decided to use parameterized queries, restrict download destinations and file ingestion, and prevent public serving of database files, data and secrets. I specified that logs would record identifiers, durations and failures without duplicating conversation content or retaining secrets, and that user-facing errors would not expose internal details. I preferred focused operational logging over logging full requests and responses, at the cost of less detail in the logs when investigating a failure. I implemented the authentication, authorization and protection mechanisms and tested them with automated tests.
