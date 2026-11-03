# Passwords and sign-in

How sign-in works in Abteilo, how to set passwords for the demo company, and what to do when someone
cannot sign in. No real password is written in this file or anywhere else in the repository.

## The essentials

- **Every account has its own password.** Resetting one account changes only that account.
- Passwords are stored only as **Argon2 hashes** in the `core.users` table. Nobody can read a password
  back: not the owner, not an administrator, not someone with database access. A password can only be
  **replaced**.
- Passwords have **at least 12 characters**.
- The app must be running (`run-dev.cmd`, terminal left open) for any sign-in to work.

## Demo accounts (BINA GmbH)

| Email | Role | Where to try the AI |
|---|---|---|
| `owner@bina.example` | Owner | Owner console → AI briefing |
| `lena.weber@bina.example` | Engineering Lead | Engineering → Assistant |
| `jonas.becker@bina.example` | Mechanical Engineer | Engineering → Assistant |
| `sara.koch@bina.example` | Finance Analyst | Finance → Assistant |
| `mila.hoffmann@bina.example` | HR Specialist | Human Resources → Assistant |
| `tim.schulz@bina.example` | IT Administrator | IT & Security → Assistant |
| `eva.richter@bina.example` | Security Officer | IT & Security → Assistant |

When the demo company is created (`run-dev.cmd -Seed`), every account gets the password in `.env`:

```
ABTEILO_DEMO_PASSWORD=...your chosen password...
```

Changing that line later does **not** change existing accounts. Use one of the resets below.

## Give every demo account the same password (recommended for demos)

1. Put the password you want into `.env` as `ABTEILO_DEMO_PASSWORD` (at least 12 characters).
2. From the `Abteilo` folder run:

   ```
   .\reset-password.cmd --demo
   ```

   or start the app with `.\run-dev.cmd -ResetDemoPassword`.

All seven accounts (owner included) now use that password. Their open sessions are signed out.

## Who can change which password

| Situation | Where | How |
|---|---|---|
| I know my password and want a new one | In the app | **Account & privacy** in the left menu → Password. Needs the current password. Your other devices are signed out; this one stays signed in. |
| An employee forgot their password | In the app | The owner or an administrator: **Owner console → People & roles** → set a new password for that person, then tell them. They are signed out everywhere. |
| The owner forgot their password | Terminal (server access needed) | `.\reset-password.cmd owner@bina.example`. Nobody ranks above the owner, so this break-glass path exists for that case. |
| One specific account, from the terminal | Terminal | `.\reset-password.cmd lena.weber@bina.example`. You type the new password twice; nothing is shown while you type. |

Every password change and reset is written to the audit log (`auth.password_changed` or
`member.password_reset`). The password itself is never logged.

There is no "email me a reset link" yet: that needs an outgoing mail service and is a planned feature.

## Sign-in messages and what they mean

| Message | Meaning | Fix |
|---|---|---|
| *Email or password is incorrect.* | Wrong password for **that** account (each account has its own). | Check the account; reset it as above. |
| *Too many attempts. Try again later.* | 5 failed attempts for that email within 15 minutes. | Wait 15 minutes, or reset the password (a reset also clears the lock). |
| *Cannot reach the Abteilo server…* | The API or web app is not running. | Start `.\run-dev.cmd` and keep that terminal open. |

## For developers

- The test suite and the evaluation use their own disposable database with fixed test passwords; they
  never touch the development database.
- Self-service change: `POST /api/me/password`. Admin reset: `POST /api/admin/members/{id}/password`.
  Operator reset: `python -m abteilo.reset_password` (inside `backend`). Code: `backend/abteilo/security.py`,
  `backend/abteilo/reset_password.py`.
- Never commit `.env`; it holds the database URLs and the demo password.
