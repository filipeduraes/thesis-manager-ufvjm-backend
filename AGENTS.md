# Instructions for AI Agents

## Repository scope

This repository contains only the backend for UFVJM's undergraduate thesis management system. The frontend is maintained in a separate React project. Implement the API, business rules, persistence, authentication, and backend integrations here; do not create React components or pages in this repository. Keep API contracts clear for the frontend.

Before changing code, read the settings and modules related to the task. Respect the existing structure and make small changes that fit the project's current stage. Do not assume that requirements described in the assignment have already been implemented. When a change depends on external services or architectural decisions that are not yet configured, document the configuration requirements and keep component boundaries clear.

## System requirements

- Use Django, Django REST Framework, Python, and PostgreSQL. Read credentials, keys, and other secrets from environment variables loaded through `.env`; never put them in code, logs, or versioned files. Keep `.env` ignored by Git. Update `.env.example` with variable names and fictitious values only.
- Use `django-allauth` for system authentication, following the existing dependencies and configuration.
- Model and enforce permissions for administrators, course coordinators, professors/advisors, students, and external thesis committee members. Temporary access for external members must use tokens with limited lifetime and scope, without requiring a permanent account.
- Restrict administrative and submission operations to authenticated and authorized users. The academic assignment requires the admin area to be restricted to the institutional network via IP/VPN; implement this only with trusted environment configuration and never trust client-supplied IP headers.
- Allow anonymous access to the public repository only for published theses.
- The domain includes students, professors, courses, theses, committees, evaluations, and supplementary documents. Preserve relationships and business rules in the backend, even though the UI is in another repository.
- Theses must support drafts, submission, status tracking, and search by title, author/student, advisor, course, defense year, and keywords. CRUD and filters must enforce permissions and state rules.
- Allow a defense committee to be scheduled only after advisor approval. Validate member availability and time conflicts. Each member records their evaluation; calculate the final result only after all required evaluations have been submitted.
- Publish a thesis to the public repository only after final committee approval, submission of a corrected version when applicable, and recording of a signed publication authorization form. Never allow a published thesis to be deleted through ordinary CRUD operations.
- On upload, accept PDF files only, enforce a configurable size limit (50 MB is the assignment's reference), and calculate SHA-256 for integrity and deduplication. Recheck integrity in workflows where approval or publication depends on the file.
- Process text extraction, indexing, notifications, and other long-running tasks asynchronously through an event queue, never inside the HTTP request. Expected events include submission, scheduling, evaluation recorded, and publication.
- Redis is a stated requirement for session and search caching, in-progress upload tracking, and rate limiting. Use predictable namespaced keys with appropriate expiration; do not store secrets or unnecessary personal data in the cache.
- The assignment proposes RabbitMQ or Kafka for queues and an architecture with authentication, thesis, committee, repository/search, notifications, and reporting services. Preserve these functional boundaries and decoupled APIs. Do not create infrastructure or microservices unless the concrete task requires them; avoid duplicating responsibilities or introducing unavailable dependencies.
- The REST API must return JSON. Expected public resources include published theses with search/filters, courses, and advisors. CRUD for students, theses, committees, and evaluations requires authentication and authorization. Keep names, formats, HTTP status codes, and pagination consistent.
- The system should support dashboards/reports for theses by course, advisor, and defense year, approval rate, and average time between submission and defense. Calculate these on the backend when reporting endpoints are implemented.
- The expected development and deployment environment is Linux.

## Implementation practices

### Python and readability

- Use type hints for new or changed functions and methods, including parameters and return values. Prefer precise types and explicit structures; avoid `Any` when a concrete type is possible.
- Add type hints for local variables when the type is not obvious or to improve clarity.
- Keep function and method parameter declarations on a single line whenever practical. Never break function/method/constructor parameter declarations across multiple lines. If a declaration becomes excessively long, group related parameters into a small object and pass that object instead of breaking parameter lists across multiple lines. Line breaks are allowed in function/method/constructor calls only when there are many arguments.
- Do not chain multiple function calls on the same line or split a single function call across multiple lines unnecessarily. Avoid patterns like `func1(func2(x))` in a single complex statement when clarity suffers, and do not break calls like `parse_datetime(str(x))` across lines. Cache intermediate results in clearly named variables on separate lines.
- Do not chain method calls across multiple lines (e.g., avoid breaking `.filter(...).first()` across lines). Keep method chains on a single line when possible, or assign intermediate objects to variables.
- Write all docstrings in English and follow the Google style (`Args`, `Returns`, `Raises`, and `Examples` where applicable). Document purpose, non-obvious behavior, side effects, and relevant exceptions; do not merely repeat the signature. Docstrings must follow this exact format: opening triple quotes on its own line, documentation on the next line with indentation matching the triple quotes, closing triple quotes on its own line at the same indentation level as opening. Do NOT add blank lines between triple quotes and documentation.
- Write developer-facing documentation and code comments in Portuguese. Add comments only to explain why a particular approach was chosen or to clarify something that is not obvious from the code. The code itself should make clear what it does; do not add comments that merely narrate operations.
- Logging messages may be written in English to keep them consistent across the codebase.
- Use descriptive and consistent names: `PascalCase` for classes, `snake_case` for functions, methods, variables, and modules, and `UPPER_SNAKE_CASE` for constants. In Django, choose clear names for models, fields, serializers, views, permissions, services, and URLs; avoid obscure abbreviations and generic names such as `data`, `obj`, or `helper` when a specific name is practical.
- Prefer small functions, single responsibilities, and explicit business rules. Avoid duplication, mutable global state, domain logic hidden in views/serializers, and abstractions without a concrete use.
- Use `Decimal` for grades or other values that require decimal precision when appropriate. Use timezone-aware dates and keep `USE_TZ` enabled.

### Django and API

- Centralize domain validation so it applies to every write path, not only the UI or a single endpoint.
- Use serializers to validate and transform API data, DRF permissions for authorization, and services or domain methods for multi-step workflows. Do not trust user, role, status, or authorship fields submitted by the client without validation.
- Keep queries efficient: use `select_related`/`prefetch_related` where appropriate, filter in the database, and paginate potentially large lists.
- Create and version migrations alongside model changes. Do not edit migrations that have already been applied as if they were disposable, and do not change existing data without a deliberate migration.
- Do not expose stack traces, private data, local paths, or internal implementation details in HTTP responses. Use consistent status codes and error messages.
- Secure uploads and downloads: validate content and size, generate safe storage names, do not trust client-provided filenames or MIME types, and enforce permissions when accessing files.

### Logging: severity and detail

- Use the Python standard library's `logging` module and obtain a logger per module (`logging.getLogger(__name__)`). Do not use `print` for application diagnostics.
- Choose severity based on the event's importance and impact: `DEBUG` for internal diagnostics; `INFO` for relevant normal events; `WARNING` for unexpected but recoverable conditions; `ERROR` for an operation failure; `CRITICAL` for a failure that threatens service continuity. Do not raise severity merely to get more output.
- Control detail separately from severity: include useful, concise context in log messages (for example, entity identifier, step, and outcome), and let handler configuration determine the level and format for each environment. Never log tokens, passwords, keys, connection URLs, document contents, unnecessary personal data, or complete payloads.
- Log exceptions with `logger.exception(...)` inside an exception handler when a traceback is useful. Do not catch broad exceptions to hide failures; handle only exceptions that can be resolved or meaningfully contextualized.
- For asynchronous workflows, log the event/task identifier and processing outcome without sensitive data. Avoid duplicate logs in layers that only propagate the same exception.

### Security and configuration

- Keep `SECRET_KEY`, PostgreSQL, Redis, broker, email, and integration credentials in environment variables. Document variable names and fictitious examples in `.env.example`.
- Apply least privilege to permissions, tokens, service credentials, and file access. Temporary tokens must expire and allow only their intended actions.
- Avoid raw SQL; if it is necessary, use bound parameters. Never build queries by interpolating user input.
- Consider privacy and auditing: record who performed relevant actions and when, without copying sensitive content into logs. Protect and retain audit records according to project configuration.

### Changes and verification

- When implementing or changing behavior, update affected documentation and configuration examples.
- Keep `FUNCIONALIDADES.md` updated: when implementing, changing, or removing a feature, reflect its status (Implementado, Parcial, Pendente), affected area, and relevant observations/endpoints.
- Create or update tests for domain rules, permissions, file validation, state transitions, and API contracts when the task involves those behaviors.
- Before finishing, run checks appropriate to the change (for example, `python manage.py check` and targeted tests) when the environment allows. Clearly report any check that could not be run and why.
- Do not commit `.env`, local databases, user uploads, secrets, caches, or generated artifacts.


## Authentication (implemented rules)

- Use Django custom user `accounts.User` with UUID PK, `AbstractBaseUser + PermissionsMixin`, `email` unique, `name`, `is_staff`, `is_active`, `date_joined`, `is_superuser`; `USERNAME_FIELD="email"`, `REQUIRED_FIELDS=["name"]`. No `username`, `first_name`, `last_name`. Table `users_user`.
- Business roles stored in `user_profile_role` with history (no deletion on revoke, `revoked_at`). Only one active role per (user, role). The effective role is always derived from the database, following the priority `ADMIN > COORDINATOR > ADVISOR > STUDENT`; there is no client-side profile switching or session-selected role.
- Login only via Google Workspace institutional accounts (`@ufvjm.edu.br`). Use django-allauth in headless/social mode (`SOCIALACCOUNT_ONLY=True`). Validate domain, `email_verified` and `hd`. Provision user on first login and assign default `STUDENT` role. No local signup/password recovery.
- API façade versioned `/api/v1`. `GET /api/v1/session` returns `authenticated`, `authType`, `user` (including `avatarUrl` from the Google profile when available), `role`, `profile`, `roles`, `permissions`, `csrfToken`, `externalInvitation` (200 for anonymous). `DELETE /api/v1/session` logs out and clears the session cookie. `GET /api/v1/auth/google/start?next=` returns `{url}` to start Google auth. The session `role` is read-only and comes from the user's roles in the database.
- External committee members use temporary invitations (`external_invitation`): generate human token `UFVJM-XXXX-XXXX-XXXX-XXXX` (base32-like), store only `SHA-256` in `token_hash` (`BinaryField(max_length=32)`). `POST /api/v1/auth/external-token` exchanges token for session, flushes previous session, rotates keys, sets `external_invitation_id`/expiration. Sessions authenticated via `ExternalInvitationSessionAuthentication` (no permanent User). TTL configurable.
- Sessions use `SESSION_ENGINE="django.contrib.sessions.backends.cached_db"` with Redis cache. Cookies HttpOnly, CSRF handled. CORS/CSRF configured for frontend.
- Audit: `session_audit` tracks sessions (IP, user agent, origin INSTITUTIONAL_NETWORK/VPN/EXTERNAL, login/logout/activity). `audit_log` is append-only with SHA-256 hash chain (`previous_hash`, `row_hash`). Actions include LOGIN, LOGOUT, LOGIN_FAILED, TOKEN_ACCESS, etc.
- Administrative areas (`/admin/`, `/api/v1/admin/`) restricted by IP/VPN via `ADMIN_ALLOWED_CIDRS`; never trust `X-Forwarded-For` without `TRUSTED_PROXY_COUNT>0`. Empty list denies all in production; return 404 on denial. DEBUG allows localhost.
- Users: soft delete only (`is_active=False`, revoke roles). Protect last active admin. Email immutable after creation. Admin endpoints: `GET/POST/PATCH/DELETE /api/v1/admin/users`, `GET /api/v1/admin/audit-logs`.
- Allauth headless configured with browser client; frontend sends `credentials: 'include'`, reads `csrftoken`, sends `X-CSRFToken`.
