# Survey Tool 1.0

A single-file, self-hosted survey engine. One Python file, SQLite storage, no external services.

Powered by Zinca Inc. · An OpenZinca Project

## Features

- **Question types**: single choice, multi-choice (with cap and "pick the ONE" follow-up), 1–5 scale, dropdown, free text, validated email field, tap-to-rank
- **Flow control**: conditional questions (`show_if` / `show_if_any` / `show_any`), screening answers that end the survey politely (`end_if`), conditionally-required fields (`req_if_shown`)
- **Bilingual**: English canonical with a per-question overlay for a second language (French included as the example)
- **Auto-save**: answers persist as a "partial" response while the visitor types; a submit upgrades it to "complete"
- **Funnel counters**: views → starts → completes, shown on the dashboard
- **Anonymous respondent codes**: `YYYYMMDD-HHMM-REGION-SEQ`, easy to reference without identifying anyone
- **Admin dashboard**: totals, funnel, per-question distributions (counts, scale averages, rank average positions), latest respondent codes
- **CSV export**: one row per respondent, headers carry the full question text, answers exported as full option text, per-question notes included, UTF-8 BOM for spreadsheet apps
- **Login protection**: admin password with per-IP lockout after 3 consecutive failures (5 minutes)
- **Duplicate guard**: one submission per device (localStorage token)

## Quick start

```bash
pip install fastapi uvicorn
export SURVEY_ADMIN_PASS='choose-a-strong-password'
uvicorn app:app --host 127.0.0.1 --port 8800
```

- Survey: `http://127.0.0.1:8800/`
- Admin: `http://127.0.0.1:8800/admin`

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `SURVEY_ADMIN_PASS` | yes | Admin dashboard password |
| `SURVEY_HOST` | no | Public hostname of the survey (host-based routing) |
| `SURVEY_ADMIN_HOST` | no | Hostname for the admin dashboard. When set, admin is served on this host instead of `/admin` |
| `SURVEY_DB` | no | Path to the SQLite database file |

## Writing your questionnaire

Edit the `Q` list in `app.py`. Every field is documented in the comment block above it, and the shipped demo questions cover each type and flow feature. Put second-language text in the `FR` overlay (any missing field falls back to English). Replace `INTRO_EN` / `INTRO_FR` with your study information and consent wording.

## Deployment notes

- Run behind any reverse proxy or tunnel that terminates TLS; the admin cookie is marked `secure`.
- Respondent region in the code comes from the `cf-ipcountry` header when present (Cloudflare); otherwise it is recorded as `??`.
- SQLite runs in WAL mode with a busy timeout, which is sufficient for survey-scale concurrency on a single host.

## License

MIT — see [LICENSE](LICENSE). © 2026 Zinca Inc.
