# Privacy, GDPR and German employment-data rules

Abteilo processes employee data inside a company, so the GDPR and German law
apply: the BDSG (Bundesdatenschutzgesetz), the TDDDG for cookies, and the
BetrVG where a works council exists. This document maps those rules to what the
software does. It is engineering documentation, not legal advice. A real
deployment needs the company's data protection officer and, for a works
council, a works agreement (Betriebsvereinbarung).

## Technical and organisational measures (Art. 25 and 32 GDPR)

| Principle | How Abteilo implements it |
|---|---|
| Data minimisation (Art. 5(1)(c)) | Directory shows name and title only. Usage tracking stores token counts and latency, never prompts or answers. Login throttling stores a hash of the email, not the email. |
| Purpose limitation (Art. 5(1)(b)) | Department data is readable only inside its department. The owner role manages the company but has no automatic access to department records, HR data or chats. |
| Storage limitation (Art. 5(1)(e)) | Chats can be deleted by the user at any time. The owner can set automatic chat deletion after 30 or 60 days. Old usage records lose their link to the person (pseudonymisation) under the same window. |
| Integrity and confidentiality (Art. 5(1)(f), Art. 32) | PostgreSQL Row-Level Security per company and per chat owner; argon2id password hashing; server-side sessions stored as hashes; HttpOnly SameSite=Strict cookies; CSRF header on writes; strict security headers; no secrets in errors or logs. |
| Accountability (Art. 5(2)) | Every consequential action writes an audit event in the same transaction; audit events are append-only for the application. |
| Privacy by default (Art. 25(2)) | External AI providers are off by default. New companies keep data on local open-source models. Documents default to department visibility. |

## Data subject rights

| Right | Implementation |
|---|---|
| Access (Art. 15) and portability (Art. 20) | `GET /api/me/export`, button "Download my data" on the Privacy page. Machine-readable JSON with profile, roles, leave, purchase requests, chats, AI usage and the user's own audit trail. |
| Erasure (Art. 17) | Users delete chats themselves, one by one or all at once. Business records the user created (requests, approvals, documents, audit) are kept, because the company has legal retention duties (HGB §257, AO §147: 6 to 10 years) and Art. 17(3)(b) applies. Removing an account's remaining personal data is a controller task, done through a documented procedure, not self-service. |
| Rectification (Art. 16) | Owner/admin manage names and titles; HR manages leave entitlements. |
| Objection and information (Art. 13, 21) | The privacy page explains what is stored, who can see it and how long it is kept. |

## Employee monitoring (BDSG §26, BetrVG §87(1) no. 6)

Tools that can monitor employee performance or behaviour need a legal basis
and, with a works council, co-determination. Abteilo is designed to avoid
covert monitoring:

- Chat content is private to the employee. Row-Level Security in the `conv`
  schema means not even the owner's database session can read another user's
  chats.
- The owner console reports AI usage aggregated by department and model, never
  per person.
- The graph-read log exists for incident reconstruction only. There is no dashboard and there are no per-person
  statistics. Only holders of `audit.read` can export it, by time window and resource, and every export is
  itself audited.
- Suspended or disabled members:
  - The relationship graph hides them at once (read-time check).
  - The assistant never names them in evidence or answers; it says "an inactive member". Modules return a typed
    active flag from live membership, and the assistant tools decide from that flag.
  - The Projects view, which is the system of record, keeps the historical assignment with an "inactive" marker
    for those already allowed to read the project.
- Assistant chats store evidence references only (source type, ids, versions), never copies of source content.
  A stored chat therefore cannot outlive a revocation: sources re-open through permission checks.
- No productivity scores, rankings or automated decisions about employees.
  Leave and role changes are always decided by a human.

## AI-specific rules

- **EU AI Act:** Abteilo is an assistant for information retrieval and
  workflow support. It does not make hiring, promotion, termination or
  performance decisions, which would fall under the high-risk category in
  Annex III. Keep it that way, or do a separate risk assessment.
- **External providers (Art. 28, Chapter V):** before the owner can enable a
  hosted model, they must confirm a data processing agreement exists and, if
  data leaves the EU/EEA, a transfer assessment. The confirmation is stored
  with the owner's id and a timestamp. Only the minimum evidence for one
  question is sent to a provider, and never credentials.
- **Transparency (AI Act Art. 50):** every answer shows which model produced
  it, which tools were used and whether each claim is verified against company
  data. Generated reports state the model, label every written statement
  (verified / from source), mark recommended next steps as AI suggestions, and
  say which statements were left out. Reports name people only as the business
  screens of that area do, and never name suspended or disabled members.

## Cookies (TDDDG §25)

Abteilo sets one cookie, the session cookie, which is strictly necessary for
the service the user requested. No analytics, no tracking and no third-party
cookies, so no consent banner is required.

## Retention summary

| Data | Default retention | Deletion |
|---|---|---|
| Chats and chat messages | Kept until the user deletes them, or 30/60 days if the owner sets it | User, retention worker |
| Generated reports (typed content in `conv.reports`; private to the person who generated them) | 30 days, or the chat retention if shorter; enforced by a database check (at most 90 days) | Deleted with the chat that produced them; retention worker after expiry. Downloads re-check permissions, so a revoked permission blocks an old report. |
| AI usage metadata | Kept; user link removed after the chat retention window | Retention worker pseudonymises |
| Business records and documents | Kept (legal retention duties) | Never by the application; archive only |
| Audit events | Kept (accountability) | Never by the application |
| Sessions | 12 hours | Expire; revoked on logout or account disablement |
| Login throttle records | 15 minutes | Pruned automatically |
| Graph-read log (who read which graph resource: actor, resource reference, depth, outcome, node count; no labels or content) | 90 days (development default; a real deployment needs its own retention decision and, with a works council, agreement) | Retention worker. The database refuses earlier deletion, and refuses any deletion outside the retention job. |

## Backups

Development can drill with `pg_dump` / `pg_restore`. Production needs
automated encrypted backups, retention, point-in-time recovery where available,
monitoring and scheduled restore tests. A backup counts only after a
successful restore. Note that deleted chats remain in backups until those
backups expire; set backup retention accordingly and document it in the
records of processing activities (Art. 30).
