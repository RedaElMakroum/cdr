# Setup Guide

## Prerequisites

- Python 3.10 or higher
- A Cerebras API key (for LLM inference)
- An ENTSO-E API key (for day-ahead electricity prices)

## 1. Clone and Install

```bash
git clone https://github.com/redaelmakroum/cdr.git
cd cdr

python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

pip install -r requirements.txt -r requirements-api.txt
```

## 2. Configure API Keys

```bash
cp .env.example .env
```

Edit `.env` and add your keys:

### Cerebras API Key

1. Create an account at [cloud.cerebras.ai](https://cloud.cerebras.ai/)
2. Generate an API key from the dashboard
3. Set `CEREBRAS_API_KEY=your-key-here` in `.env`

The default model is `gpt-oss-120b`. You can change this with the `CEREBRAS_MODEL` variable.

### ENTSO-E API Key

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu/)
2. Request an API token via email (usually approved within 1-2 days)
3. Set `ENTSOE_API_KEY=your-key-here` in `.env`
4. Set `BIDDING_ZONE` to your country code (default: `AT` for Austria)

Supported bidding zones: AT, DE_LU, FR, NL, BE, CH, IT_NORTH, ES, PT, DK_1, DK_2, NO_1 through NO_5, SE_1 through SE_4, FI, PL, CZ, HU, RO, BG, GR, IE_SEM.

## 3. Verify Setup

```bash
source venv/bin/activate
python -c "from config import *; print('Config OK:', CEREBRAS_MODEL)"
```

If this prints the model name without errors, your configuration is correct.

## 4. Run

```bash
# Start the API server
python api.py

# Open in browser:
# Aggregator view: http://localhost:5001/?role=aggregator
# Prosumer view:   http://localhost:5001/?role=prosumer
```

## Docker Setup

If you prefer Docker:

```bash
cp .env.example .env
# Edit .env with your API keys

docker compose up --build
```

- API: http://localhost:5001
- Dashboard: http://localhost:8000

## Security Notes

- Never commit your `.env` file (it is in `.gitignore`)
- The API server includes rate limiting and input validation
- For production deployment, place behind a reverse proxy with HTTPS
