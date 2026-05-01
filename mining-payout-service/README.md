# Mining Payout Service

Initial scaffold for reward collection and payout settlement service.

## Quickstart

1. Create virtual environment:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Create env file:
   - `touch .env` (if missing) and set required values
4. Start API:
   - `uvicorn app.main:app --reload`

Health check:
- `curl http://127.0.0.1:8000/health`

## Continuous Scheduler Mode

The service can run payout cycles continuously in-process using APScheduler.

Enable in `.env`:

```bash
SCHEDULER_ENABLED=true
SCHEDULER_INTERVAL_SECONDS=60
```

Then start API as usual:

```bash
uvicorn app.main:app --reload
```

While scheduler is running:
- each interval executes the same logic as `POST /settlements/run`,
- snapshots are polled (`poll_channels_once_with_blocks` when channels endpoint is configured),
- settlement and sender flow run,
- payout audit log keeps appending to `PAYOUT_AUDIT_LOG_PATH` (default `./logs/payout_audit.jsonl`).

## End-to-End Demo Run

Run a complete local demo that:
- maps channel_id to downstream user identity,
- writes two snapshots for translator channels,
- computes interval deltas,
- creates settlement + user payout rows,
- prints a payout-ready table.

```bash
python scripts/demo_interval_run.py
```

Optional args:

```bash
python scripts/demo_interval_run.py --db-path ./demo_payouts.db --interval-minutes 90 --reward-btc 0.01000000
```

### Live API Mode

To poll fresh translator data every run (instead of static embedded payloads):

```bash
python scripts/demo_interval_run.py \
   --mode live \
   --db-path ./demo_live.db \
   --interval-minutes 4 \
   --reward-btc 0.01000000 \
   --upstream-url http://192.168.38.155:8080/v1/translator/upstream/channels \
   --downstream-url http://192.168.38.155:8080/v1/translator/downstreams
```

Run it again after 2-4 minutes to see new snapshots and updated deltas/payout rows.

### Env-Driven Cadence (Demo)

You can control demo snapshot and payout cadence from environment values:

- `DEMO_PAYOUT_INTERVAL_MINUTES` (for example `2`)
- `DEMO_SNAPSHOT_INTERVAL_SECONDS` (for example `120`)
- `DEMO_LOOP_CYCLES` (how many live cycles in one run)
- `DEMO_DB_PATH`
- `DEMO_REWARD_BTC`

Example run using env defaults:

```bash
python scripts/demo_interval_run.py --mode live
```

Example explicit 2-minute payout with 2-minute snapshots:

```bash
python scripts/demo_interval_run.py --mode live --interval-minutes 2 --snapshot-interval-seconds 120 --loop-cycles 5
```

The output includes:
- settlement_id and interval window,
- user_share_delta and user_work_delta,
- payout_fraction and amount_btc,
- translator_total_work for that payout interval.
