# Vizja Course Schedule Monitor

Python automation that reads an authenticated **Vizja / Verbis** course schedule and synchronizes it with an existing, dedicated Google Calendar. Intended to run using GitHub Actions approximately every 30 minutes without leaving a personal computer on.

**Status:** Browser-based sign-in and four-week schedule retrieval were validated on Windows. GitHub-hosted Linux sign-in and live Google Calendar sync are **not yet verified**. The scheduled job is disabled unless explicitly enabled.

## How it works

1. Opens a temporary Chrome browser and signs into the university Extranet using repository secrets.
2. Opens the new SIS/Verbis system through the Extranet's own link; reads four weeks of lessons through the authenticated AJAX endpoint.
3. Reads the dedicated `Vizja University` Google Calendar and reconciles events by Verbis lesson ID. Existing events from earlier local versions can be matched through their description.
4. Handles added, modified, removed, cancelled and makeup classes; optional Telegram messages notify about detected changes.

**Every run starts from a fresh session.** If the university enforces CAPTCHA, extra verification, rate limits or changes its sign-in form, the automation may need updating. A GitHub scheduled workflow is not guaranteed to run at an exact minute.

## Protecting private information in a public repository

The repository holds **source code only**, no credentials or personal schedule data. Add these through GitHub's encrypted repository secrets; **never commit them** or paste their contents into Issues, Actions logs, or chat:

| Repository secret | Value |
|---|---|
| `VIZJA_USERNAME` | Extranet login |
| `VIZJA_PASSWORD` | Extranet password |
| `VIZJA_PERSON_ID` | Your Verbis `idOsoby` value (numeric; visible in a schedule AJAX request) |
| `VIZJA_SEMESTER_ID` | Your Verbis `idSemestru` value (numeric) |
| `GOOGLE_TOKEN_JSON` | Entire local `google_token.json` file content from the working desktop integration; must contain a usable OAuth refresh token |
| `GOOGLE_CALENDAR_ID` | `calendar_id` from local `google_calendar_config.json`; identifies the existing separate **Vizja University** calendar, not `primary` |

Optional notification secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

On Windows, Google auth files from the prior local version are normally located inside `%USERPROFILE%\VizjaMonitor`. Copy values into **Settings → Secrets and variables → Actions → Secrets → New repository secret**. Do **not** upload either JSON file. Secrets may be accessed by the workflows that run on your behalf; review proposed code changes before accepting them. Workflow logs from this public repository are publicly viewable, so avoid printing personal details.

## First cloud test (does not modify Calendar)

1. Add the four `VIZJA_...` repository secrets listed above. You can leave both Google secrets empty for the sign-in test.
2. Open **Actions → Vizja Monitor — cloud → Run workflow**.
3. Leave **`login_test_only` checked (`true`)** and run it on `main`.
4. A successful run ends with a message similar to `[Test] ... başarılı. Google Calendar değiştirilmedi.`

If it fails, review the `[SSO]`, `[Vizja]`, and `[HATA]` lines. Avoid pasting full authenticated URLs, cookies, tokens, or private identifying details.

## First live Google Calendar sync

1. Add `GOOGLE_TOKEN_JSON` and `GOOGLE_CALENDAR_ID` secrets.
2. Run **Actions → Vizja Monitor — cloud → Run workflow** with `login_test_only = false`.
3. Verify the separate Google Calendar: no duplicates, correct times, rooms, class status.
4. Run again and verify the second sync reports **0 new events** when nothing changed.

The program refuses to write into `primary`; it only writes into the separate existing calendar whose name is exactly `Vizja University`.

## Turn on the 30-minute schedule

After both cloud tests pass, add a **repository variable**, not a secret: `VIZJA_ENABLED` = `true` under **Settings → Secrets and variables → Actions → Variables**.

The workflow is configured with `7,37 * * * *` in UTC (about every 30 minutes). GitHub may delay or skip scheduled runs. Remove `VIZJA_ENABLED`, change its value, or disable the workflow to stop automatic jobs.

Once cloud sync works reliably, stop the old local monitor to avoid two simultaneous writers. The Google OAuth application's *Testing* status may cause its refresh token to expire in seven days; resolve this in Google Cloud before relying on long-term unattended syncing.

## Local tests

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The real `--login-test` requires your personal environment variables `VIZJA_PERSON_ID` and `VIZJA_SEMESTER_ID`. Locally, `VIZJA_USERNAME` and `VIZJA_PASSWORD` can instead be typed at runtime:

```bash
python cloud_monitor.py --login-test
```

The login test fetches lessons but does not modify Google Calendar.

## Security and limitations

- No password, session cookies, or Google OAuth token should be added to commits. The `.gitignore` is preventative, not a replacement for review.
- The code doesn't implement CAPTCHA bypass or two-factor authentication bypass.
- A valid connection is required on **every** scheduled run. If authentication fails, no calendar changes are made.
- Only events carrying this project's Verbis ID are automatically deleted during reconciliation.
- Avoid granting repository write access to untrusted parties. Public GitHub Actions workflows and their logs are visible to others, and workflow authors can access repository secrets at runtime.
- Check the university's applicable terms before operating unattended polling.

Technical details: [GitHub Actions schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [GitHub Actions secrets](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions), [Google Calendar event IDs](https://developers.google.com/workspace/calendar/api/v3/reference/events).