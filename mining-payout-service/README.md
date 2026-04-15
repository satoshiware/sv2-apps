# Mining Payout Service

Initial scaffold for reward collection and payout settlement service.

## Quickstart

1. Create virtual environment:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Create env file:
   - `cp .env.example .env`
4. Start API:
   - `uvicorn app.main:app --reload`

Health check:
- `curl http://127.0.0.1:8000/health`
