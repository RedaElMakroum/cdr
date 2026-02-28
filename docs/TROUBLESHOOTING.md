# Troubleshooting

## API Key Issues

### "CEREBRAS_API_KEY not found"
- Ensure `.env` exists in the project root (copy from `.env.example`)
- Check that the variable is set: `grep CEREBRAS_API_KEY .env`
- Ensure no trailing whitespace in the key value

### "ENTSOE_API_KEY not found"
- Same as above. ENTSO-E keys are typically 36-character UUIDs
- New ENTSO-E accounts may take 1-2 days for API access activation

## Rate Limiting

### HTTP 429 from Cerebras
- The benchmark script handles this automatically with retries
- For manual usage, wait 60 seconds and retry
- Cerebras free tier has a tokens-per-minute quota. Paid plans have higher limits

### HTTP 429 from the API server
- The Flask API has per-endpoint rate limits (see [API Reference](API.md))
- Wait for the `Retry-After` header duration

## Docker Issues

### Container fails to start
- Check that `.env` exists and has valid keys: `docker compose config`
- Check logs: `docker compose logs cdr-api`
- Ensure port 5001 is not already in use

### Health check failing
```bash
docker compose exec cdr-api curl http://localhost:5001/api/health
```

## Dashboard Issues

### Page loads but nothing is clickable
- Check the browser console for JavaScript errors (F12)
- Ensure the API server is running on port 5001
- Try hard-refreshing (Ctrl+Shift+R)

### SSE streaming not working
- Verify the API server is accessible: `curl http://localhost:5001/api/health`
- Check that no proxy is buffering SSE responses
- In Docker, ensure the port mapping is correct

## ENTSO-E Issues

### No price data returned
- ENTSO-E publishes day-ahead prices around 12:00-14:00 CET
- Check your bidding zone code is valid (see [Setup Guide](SETUP.md))
- ENTSO-E has occasional outages. Check [transparency.entsoe.eu](https://transparency.entsoe.eu/)

### Wrong prices or timezone
- All prices are in the configured bidding zone's local time
- The system uses 15-minute resolution (96 slots/day)

## Orchestrator Issues

### Orchestrator timeout (300s)
- Complex scenarios may hit the iteration limit (15 iterations)
- Check the run log for the specific failure point
- Consider simplifying the request or checking battery state configuration

### Unexpected DR evaluation results
- Verify `data/battery_state.json` has sensible values
- `min_soc_pct` should match your intended minimum (default: 20%)
- The MILP optimizer considers PV forecast for pre-charging -- a high PV forecast can enable larger commitments than the current SoC alone would suggest

## Import Errors

### "ModuleNotFoundError: No module named 'config'"
- Ensure you are running from the project root directory
- The script should be executed as `python src/api.py`, not from a subdirectory

### Missing dependencies
```bash
pip install -r requirements.txt -r requirements-api.txt
```

## Getting Help

If you encounter issues not covered here, open an issue on GitHub with:
1. The error message (full traceback if available)
2. Your Python version (`python --version`)
3. Your OS
4. Steps to reproduce
