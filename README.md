# Policy Opportunity Agent

A Raspberry Pi–friendly collector that monitors official Canadian and British Columbia policy sources, normalizes upcoming and newly published events, assigns deterministic op-ed opportunity signals, writes static JSON, and pushes the updated data to GitHub every 12 hours.

This repository covers **opportunity identification only**. It contains no author roster, outlet matching, drafting workflow, pitching, approvals, or publication management. The separate HTML application can consume the files in `docs/data/`.

## What it produces

Each successful or partially successful run atomically writes:

| File | Purpose |
|---|---|
| `docs/data/manifest.json` | Entry point, schema version, run status, file paths, hashes, counts, and horizons |
| `docs/data/events.json` | Normalized official-source events and deadlines |
| `docs/data/opportunities.json` | Ranked opportunity signals, “why now,” hook type, and angle prompts |
| `docs/data/changes.json` | New, changed, and removed event records since the preceding run |
| `docs/data/source-status.json` | Health, errors, warnings, event counts, and stale-data indicators by source |
| `docs/data/heartbeat.json` | Last run time and aggregate run status |

The frontend should fetch `manifest.json` first and then use the relative paths in that file. It should display a stale-data warning whenever the manifest is old or a source in `source-status.json` has `stale: true`.

The packaged repository includes an intentionally failed bootstrap manifest so these URLs exist before first deployment. The first successful Pi run replaces it with live data.

## Opportunity horizons

The deterministic engine assigns one of six horizons:

- `react_now`: a new or changed official publication detected within roughly three days.
- `execution`: an event or deadline within 14 days.
- `preparation`: 15–30 days away.
- `scouting`: 31–90 days away.
- `second_wave`: a publication up to 14 days old that remains usable for follow-up analysis.
- `monitor`: retained for search or continued monitoring but outside the active windows.

Scores are transparent. Each opportunity includes `score_components`; weights are editable in `config/rules.yml`.

## Included source families

The default configuration covers:

- Bank of Canada upcoming events, press releases, and speeches.
- Statistics Canada’s key-indicator schedule and The Daily feed.
- BCUC anticipated filings, filing deadlines, public proceeding docket metadata, orders, and decisions.
- Canada Gazette Parts I, II, and III.
- Federal and B.C. consultation registries.
- B.C. Regulations Bulletin.
- House of Commons committee meetings and the B.C. legislative calendar.
- Elections Canada political-finance deadlines.
- Selected federal fiscal, regulatory-plan, PBO, Canada Energy Regulator, and municipal calendar signals.

All source definitions are in `config/sources.yml`; a source can be disabled with `enabled: false`.

### BCUC boundary

The BCUC proceeding collector reads public page metadata and public timetable rows. It **does not download any linked docket document**. A row marked confidential is recorded only with metadata already visible on the public proceeding page, with `metadata_only: true`, and the event points back to the proceeding page rather than a document URL.

## Raspberry Pi requirements

Recommended baseline:

- Raspberry Pi OS Bookworm or another Linux distribution with systemd.
- Python 3.11 or later.
- Git and OpenSSH client.
- A public or private GitHub repository that the Pi can push to.

Install operating-system prerequisites if needed:

```bash
sudo apt update
sudo apt install -y git openssh-client python3 python3-venv
```

## Initial repository setup

Create an empty GitHub repository, place this project in it, and use an SSH remote:

```bash
git init
git add .
git commit -m "Initial policy opportunity agent"
git branch -M main
git remote add origin git@github.com:YOUR_ACCOUNT/YOUR_REPOSITORY.git
git push -u origin main
```

On the Pi, clone it and install the Python environment:

```bash
git clone git@github.com:YOUR_ACCOUNT/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
./scripts/bootstrap-pi.sh
```

Edit `.env` and replace the contact address. It is included in the scraper’s User-Agent so source operators have a contact if the collector causes a problem.

```bash
nano .env
```

## Repository-scoped GitHub write access

A dedicated deploy key limits the Pi’s Git access to this repository. Generate one and configure this local clone to use it:

```bash
./scripts/configure-github-key.sh
```

Copy the printed public key into the repository’s **Settings → Deploy keys**, and enable write access. Do not put a private key, personal access token, `.env`, or `.state` contents into the repository.

Test the remote after adding the key:

```bash
git push --dry-run origin HEAD:main
```

## Run once before scheduling

A collection-only run writes JSON without committing it:

```bash
.venv/bin/policy-agent \
  --config config/sources.yml \
  --rules config/rules.yml \
  --output docs/data \
  --state-dir .state \
  run
```

Validate the manifest:

```bash
.venv/bin/policy-agent --output docs/data validate
```

Run the complete pull–collect–commit–push path:

```bash
./scripts/run-once.sh
```

The pull is best-effort by default: a temporary GitHub outage does not prevent local collection. A push conflict triggers one pull/rebase and retry. Overlapping manual and timer runs are prevented by `.state/publish.lock`.

## Enable the 12-hour systemd timer

Install the system service and timer from the repository root:

```bash
./scripts/install-systemd.sh
```

The default timer runs at 00:15 and 12:15 in `America/Vancouver`, with up to ten minutes of randomized delay and catch-up after downtime. Run it immediately and inspect the logs:

```bash
sudo systemctl start policy-opportunity-agent.service
journalctl -u policy-opportunity-agent.service -n 200 --no-pager
systemctl list-timers policy-opportunity-agent.timer
```

Other useful commands:

```bash
sudo systemctl status policy-opportunity-agent.service
sudo systemctl status policy-opportunity-agent.timer
sudo systemctl disable --now policy-opportunity-agent.timer
```

## Publish the JSON through GitHub Pages

In the GitHub repository, select **Settings → Pages → Deploy from a branch**, choose `main`, and choose `/docs` as the folder. The data base URL will be:

```text
https://YOUR_ACCOUNT.github.io/YOUR_REPOSITORY/data/
```

The separate HTML app should begin with:

```text
https://YOUR_ACCOUNT.github.io/YOUR_REPOSITORY/data/manifest.json
```

A raw-GitHub fallback is also available:

```text
https://raw.githubusercontent.com/YOUR_ACCOUNT/YOUR_REPOSITORY/main/docs/data/manifest.json
```

No HTML application is included in this project.

## Data contract

Dates are ISO-8601 timestamps in UTC. The display timezone is declared in the manifest as `America/Vancouver`.

A representative opportunity record is:

```json
{
  "id": "opp:...",
  "event_id": "statcan-key-indicators:...",
  "event_title": "Consumer Price Index",
  "source_id": "statcan-key-indicators",
  "event_type": "economic_release",
  "hook_type": "fresh_evidence",
  "horizon": "execution",
  "opportunity_score": 80,
  "score_components": [
    {"name": "event_type", "points": 52, "reason": "Base weight for economic_release."}
  ],
  "why_now": "The event is scheduled for Monday, July 20, 2026, approximately 7 days away.",
  "angle_prompts": ["Does the new evidence confirm or contradict the prevailing policy narrative?"],
  "relevant_at": "2026-07-20T12:30:00Z",
  "confidence": "confirmed",
  "change_type": "new"
}
```

The opportunity schema deliberately excludes implementation fields such as authors, outlets, owners, approvals, drafts, and pitch status.

## Reliability behaviour

- HTTP requests use a descriptive User-Agent, host pacing, redirects, retries, and conditional requests through `ETag` and `Last-Modified` when supported.
- Files are written atomically; `manifest.json` is written last.
- A failed or partially parsed source retains its previous good events and is marked stale rather than being replaced by an empty list.
- The Pi archives only small HTTP validator state, page hashes, and locks in `.state/`; raw documents are not committed.
- Every scheduled run updates the heartbeat, so the repository normally receives two data commits per day even when no policy event changes.
- Parsers are intentionally deterministic. No language-model call is required to collect or rank opportunities.

## Configuration

### Priorities

Edit `config/rules.yml` to change:

- minimum score;
- event-type base scores;
- policy-topic bonuses;
- source bonuses; and
- priority keyword bonuses.

### Sources

Edit `config/sources.yml` to change horizons, source URLs, lookback windows, BCUC application IDs, or enabled sources. To monitor an additional known BCUC proceeding even if the listing parser fails, add its public application ID:

```yaml
application_ids: [1391, 1234]
```

List the effective source configuration:

```bash
.venv/bin/policy-agent --config config/sources.yml --rules config/rules.yml list-sources
```

## Operational cautions

Official websites can change markup, reject automated requests, or have intermittent outages. Review `source-status.json` and the systemd journal. A `partial` run can still contain valid data, but a persistent parser warning should be investigated before relying on that source.

Keep the 12-hour schedule unless a source operator authorizes more frequent access. The BCUC proceeding collector is capped at 25 listed proceedings per run by default and pauses between requests to the same host.

## Development and tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check src tests
```

The tests use local fixtures and do not make live network requests.
