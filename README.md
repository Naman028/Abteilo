# Abteilo

**A department-aware AI workspace for companies.** Each department (Engineering,
Finance, Human Resources, IT & Security, plus any department you add) gets its own
workspace, data scope and assistant. The owner console manages departments,
people, AI models, data retention and audit. It runs on free, local open-source
models by default.

*Abteilo* comes from the German *Abteilung*, "department".

## What it does

- **Four working departments.** Engineering projects and tasks. Finance budgets and
  purchase approvals with budget limits. HR leave requests with balance checks. IT
  role changes that need a second person to approve.
- **An assistant in every workspace.** It answers only from data the user is allowed
  to see. Each claim and the summary must cite evidence. Numbers, project codes and
  statuses are checked against that evidence, so a claim like "A17 is complete" fails
  when the record says blocked. Each claim is shown as verified, cited or unsupported.
- **An owner console.**
  - **Overview:** decisions waiting on you, blocked work, budget pressure, each
    department's status, AI health and today's activity, all computed directly
    from company records.
  - **Departments:** add, rename, archive, switch modules on or off, see who is in
    each one.
  - **People & roles:** add people, edit them, reset passwords, disable accounts,
    assign or revoke department roles.
  - **AI models:** add, edit, test and route models per department.
  - **Also:** an AI briefing, AI usage per department, a data inventory with
    retention, an audit log with CSV export, and company settings.
  - **Elevated roles** (owner, security admin) always need an owner's approval.
- **Data protection built into the database.** Tenant and chat isolation use
  PostgreSQL Row-Level Security. The application role cannot delete business data at
  all. Users can delete their own chats, and the owner can set chats to be deleted
  automatically after 30 or 60 days. Users can export their data (GDPR Art. 15/20).
- **Hybrid document search.** Keyword and semantic search (pgvector + local bge-m3 embeddings, English
  and German) are combined with Reciprocal Rank Fusion. Every candidate passes the user's permission
  rules inside the database query.
  - Measured on the included evaluation set (`python -m eval.retrieval`): Recall@5 1.0, with zero
    unauthorised results.
  - If the embedding model is down, search falls back to keyword-only.
- **One local model by default.** Every department and the owner use Qwen 2.5 7B through
  Ollama, with bge-m3 for document search embeddings. Departments differ by permissions,
  tools and data, not by model weights. Other profiles (Qwen 2.5 Coder 7B, Llama 3.2) can
  be routed to a department explicitly. A hosted provider (Claude, or an OpenAI-compatible
  API) can be added for a department only after the owner opts in; there is never a
  silent fallback to it.

## Stack

FastAPI · PostgreSQL 17/18 (RLS) · SQLAlchemy Core · Alembic · React + TypeScript
(Vite) · Ollama · optional Anthropic / OpenAI-compatible APIs · Docker Compose ·
pytest against real PostgreSQL.

## Quick start (Docker)

```bash
cp .env.docker.example deploy/.env.docker      # set two long random passwords
ollama pull qwen2.5:7b && ollama pull qwen2.5-coder:7b && ollama pull llama3.2 && ollama pull bge-m3
docker compose --env-file deploy/.env.docker -p abteilo up -d --build
docker compose --env-file deploy/.env.docker -p abteilo run --rm -e ABTEILO_DEMO_PASSWORD='choose-a-demo-password' api python -m abteilo.seed
```

Then open http://127.0.0.1:8000 (or `ABTEILO_HTTP_PORT`). Over plain http on
localhost, set `ABTEILO_COOKIE_SECURE=false` in `deploy/.env.docker`. In
production, keep it `true` and put HTTPS in front of the API. `docker compose
down` keeps the database volume. Removing the volume (`down -v`) deletes all
company data.

Demo accounts (all use the demo password you chose):

| Account | Role |
|---|---|
| `owner@bina.example` | Company owner: owner console, executive briefing |
| `lena.weber@bina.example` | Engineering lead |
| `jonas.becker@bina.example` | Engineer |
| `sara.koch@bina.example` | Finance analyst (approves purchases) |
| `mila.hoffmann@bina.example` | HR specialist |
| `tim.schulz@bina.example` | IT access administrator (proposes role changes) |
| `eva.richter@bina.example` | Security officer (approves role changes) |

All company data in the demo is fictional.

### Day-to-day Docker commands

```bash
docker compose --env-file deploy/.env.docker ps        # what is running
docker compose --env-file deploy/.env.docker stop      # pause; containers and data stay
docker compose --env-file deploy/.env.docker start     # resume
docker compose --env-file deploy/.env.docker logs --tail=100
```

Abteilo needs only its own PostgreSQL container. It does not use Neo4j, Redis
or MongoDB.

## Local development

```bash
# PostgreSQL with pgvector and two roles: abteilo_owner (owns schema) and abteilo_app (runtime, no BYPASSRLS).
# A superuser runs `CREATE EXTENSION vector;` once per database (Docker does this automatically).
cp .env.example .env                  # fill in the database URLs and a demo password
cd backend
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"    # use .venv/bin/... on macOS/Linux
python -m alembic upgrade head
python -m abteilo.seed
python -m uvicorn abteilo.main:app --reload
python -m pytest                      # needs the ABTEILO_TEST_* database URLs
ruff check .                          # lint

cd ../frontend && npm install && npm run dev   # http://localhost:5173, proxies /api to :8000
```

On Windows with PostgreSQL in WSL, `run-dev.cmd` (or `scripts\dev.ps1`) does all of this in one step:
it starts PostgreSQL and Ollama, applies migrations, starts the API and the web app, and opens the
browser. API and web logs appear in the same terminal; in VS Code, run it from the integrated terminal
or via *Tasks: Run Task → Abteilo: run dev stack*. It prints the demo accounts; the password is
`ABTEILO_DEMO_PASSWORD` from `.env`. Pass `-Seed` on the first run against an empty database. Press
Enter in that terminal to stop.

Passwords are stored only as Argon2 hashes in `core.users`; nobody can read them back. If sign-in is
rejected, run `run-dev.cmd -ResetDemoPassword` to set every demo account to the current `.env` value.
Signed-in users change their own password under *Account & privacy* (current password required; their
other sessions are signed out). A forgotten password is reset by the owner or an administrator under
People & roles in the console. If the owner is locked out, someone with server access runs
`reset-password.cmd <email>` from the project folder (or `python -m abteilo.reset_password <email>` inside
`backend`), which prompts for the new password, signs that person out everywhere and is audited. Five failed
sign-ins for one email within 15 minutes lock it for 15 minutes; a reset clears the lock. There is no
self-service email reset yet: that needs an outgoing mail service.

## Quality gates and CI/CD

`.github/workflows/ci.yml` runs on every push to `main` and every pull request:

| Job | Checks |
|---|---|
| backend | `ruff check`, `pip-audit`, the full pytest suite against PostgreSQL 17 + pgvector as the restricted runtime role |
| frontend | `npm audit --omit=dev --audit-level=high`, strict TypeScript check, production build |
| container | builds the Docker image, migrates an empty database with it, starts the API and checks `/readyz`, `/healthz`, production mode (no API docs, 401 without a session) and security headers |

Pushing a version tag (`git tag v0.1.0 && git push origin v0.1.0`) runs `.github/workflows/release.yml`:
the same gates, then the image is published to `ghcr.io/<owner>/abteilo`. Nothing is deployed
automatically. Third-party actions are pinned to commit SHAs, and Dependabot proposes weekly updates
for pip, npm, Actions and the Docker base images.

## Things to try

**Reports.** In any assistant, ask for a document, e.g. *"Make me a PDF report of the progress of all
projects"* or *"…as a Word report"*. The assistant gathers the live data, writes the report and checks
every statement; the answer ends with a **Report ready** card (Download PDF / Word). Only verified or
cited statements go into the file; unverified ones are left out and counted. Any earlier answer can
also be saved as PDF or Word from the line under it. Downloads are private to the person who asked
and are recorded in the audit log (`assistant.report_exported`). Light, dark and system themes are
switched at the bottom of the left menu.

1. Sign in as Jonas and ask *"What is the status of project A17?"*. Open the
   evidence chips.
2. Ask Jonas's assistant for salary bands. That document is confidential to HR,
   so it is never retrieved, and any invented figures are marked unverified.
3. Have Jonas submit a purchase request, then approve it as Sara. Try approving
   more than the budget allows.
4. As the owner, add a "Quality" department, add a person to it, register a new
   Ollama model and route the department's heavy work to it.
5. Set chat retention to 30 days under Settings. Then look at the AI usage tab,
   which reports per department and never per person.

## Documentation

- [Architecture](docs/ARCHITECTURE.md): components, data planes, request flow, extending
- [Privacy and GDPR](docs/PRIVACY_AND_GDPR.md): GDPR, BDSG, BetrVG, AI Act mapping
- [Reports](docs/REPORTS.md): AI-authored PDF/Word reports and answer exports

## Limits

- Retrieval quality was measured on a small synthetic corpus; re-run `python -m eval.retrieval --model bge-m3`
  on representative documents before relying on it. Graph relationships suggested from documents need a reviewer.
  Combined answers across documents, the graph and live records are developer-tested; questions that need
  several reasoning steps (multi-hop) are not yet answered reliably.
- Claim verification checks sources, numbers, identifiers and statuses
  deterministically. It cannot prove that a narrative sentence is true. Such
  sentences are labelled "cited", never "verified".
- Built-in business modules cover Engineering, Finance, HR and IT & Security.
  Further departments get documents and the assistant until their own modules
  are built.
- The demo uses password sign-in. For a real organisation, connect an OIDC
  identity provider.
- This is not legal advice. A real deployment needs the company's data
  protection officer.

## Sharing the code

Never zip the working folder directly: it contains your local `.env`, virtual
environment and `node_modules`. After committing, create a clean archive from
Git, which leaves out everything in `.gitignore`:

```bash
git archive --format=zip -o abteilo.zip HEAD
```

## License

MIT
