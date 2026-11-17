# Shipping changes without breaking the live site

Every change goes through four safety nets before and after it reaches people:

1. **CI on every pull request.** The unit tests run, then the site is started the way Render starts it
   (gunicorn and Postgres) and `tools/smoke_test.py` opens every key page, signs up a throwaway
   account, opens the signed-in pages and the Admin Console, and deletes the account.
2. **A staging site.** Changes land on a separate copy of the site with its own database first.
3. **Health checks.** Render only switches traffic to a new version once `/healthz` answers 200.
   A version that can't start never replaces the one that works.
4. **Feature switches and rollback.** A broken feature can be switched off from the Admin Console
   in seconds, and Render can roll back to any earlier deploy in one click.

## The everyday workflow

```
feature branch ──PR──▶ staging ──PR──▶ main
                 CI       │       CI     │
                          ▼              ▼
                  staging site      live site
            (checked after deploy)
```

1. **Work on a branch** made from `staging`.
2. **Open a pull request into `staging`.** Wait for both CI checks (`test` and `smoke`) to go green, then merge.
3. **Render deploys the staging site.** The "Check staging after deploy" workflow waits until staging
   is running that exact commit, then opens its key pages. Click around staging yourself for anything visual.
4. **Promote to live:** open a pull request from `staging` into `main` (title it "Release: ..." with the
   list of changes). When CI is green, merge it. Render deploys the live site, and the health check
   keeps the old version serving until the new one answers.
5. **Check the live site.** Open `/healthz` on it: `version` shows the commit that's now live.

Small fixes can wait on staging and go live together in one release.

## Shipping a new feature dark

New features should ship switched to **Admin only**, then be turned on once they've been seen working:

1. Add an entry to `FLAGS` in `flags.py` with `default="admin"` and the URL prefixes the feature owns.
   Its pages and APIs are then blocked for everyone but the admin.
2. Hide its links in templates with `{% if feature('your_key') %} ... {% endif %}`.
   In Python, use `flags.enabled('your_key', is_admin())`.
3. After it's live, sign in as admin and try it on the real site.
4. Admin Console > **Feature switches** > **Everyone**. It reaches every visitor within about 10 seconds.

`FLAG_<KEY>` environment variables (for example `FLAG_LEARN=off`) set the default for a site until
someone picks a state in the console. The console's choice is saved in that site's database, so
staging and live are switched separately.

Today's switches: Sunny (board assistant), Career Switch, Learn, and Calls.

## When something breaks on the live site

Pick the fastest fix that works:

| What broke | Do this | How long |
|---|---|---|
| One feature (Sunny, Learn, Calls, Career Switch, or a new flagged feature) | Admin Console > Feature switches > **Off** | Seconds |
| Anything else after a deploy | Render > the live service > **Events** > find the last good deploy > **Rollback** | About a minute |
| The new version won't start | Nothing to do: the health check keeps the old version serving. Read the deploy log in Render | |

After a rollback, Render turns off auto-deploy for that service so the broken commit isn't redeployed.
Fix the problem with a normal pull request (or revert the bad commit on `staging` and promote), then
turn **Auto-Deploy** back on in the service's Settings.

### Keep database changes safe to roll back

A rollback swaps the code, not the database. So a release should only **add** things to the database
(new tables, new columns with defaults). Rename or drop a column in a later release, once no live code
uses it. The startup migrations in `db.py` follow this already: they create what's missing and never
remove anything.

## One-time setup

### 1. Live site: turn on the health check

Render > the live service (`pm-job-hunter`) > **Settings** > **Health Check Path** > `/healthz` > Save.

While you're there, under **Build & Deploy**, if **Auto-Deploy** offers **After CI checks pass**, pick it.
Render then waits for the GitHub checks on `main` before deploying.

### 2. Create the staging site

1. Render > **New** > **Blueprint** > pick the `sunny-side` repository. Render reads `render.yaml`
   and offers to create `sunny-side-staging`. It does not touch the live site.
2. Fill in the values it asks for:
   - `ADMIN_PASSWORD`: a password for staging's admin (different from live's).
   - `DATABASE_URL`: a **separate** Postgres database, never the live one. A free
     [Neon](https://neon.tech) project works. If you leave it empty, staging uses a SQLite file that
     starts fresh on every deploy, which is fine for clicking around.
   - `GEMINI_API_KEY`, `LIVEKIT_*`: copy them from the live service if you want Sunny's voice and AI
     scoring on staging. Leave them empty otherwise.
3. Apply. The staging address will look like `https://sunny-side-staging.onrender.com`.

Staging deliberately has no `CRON_SECRET` or email settings, so it never emails real people.
The phone apps keep pointing at the live site.

### 3. GitHub

- **Settings > Secrets and variables > Actions > Variables**: add `STAGING_URL` with the staging address.
- **Settings > Secrets and variables > Actions > Secrets**: add `STAGING_ADMIN_PASSWORD` (optional;
  the staging check then also opens the Admin Console).
- **Settings > Branches**: add a protection rule for `main` and one for `staging`: require a pull
  request and require the `test` and `smoke` status checks to pass.

## Running the checks yourself

```bash
python -m pytest -q tests                                # unit tests
python tools/smoke_test.py https://sunny-side-staging.onrender.com   # open the key pages on any copy
```

`curl https://<site>/healthz` answers `{"ok": true, "env": "staging", "version": "abc1234"}`.
