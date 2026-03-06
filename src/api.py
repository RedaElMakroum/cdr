"""
Simple Flask API to run orchestrator from web interface.
"""
from flask import Flask, request, jsonify, Response, stream_with_context, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import subprocess
import shlex
import os
import json
from pathlib import Path
from datetime import datetime

# Project root (one level up from src/)
PROJECT_ROOT = str(Path(__file__).parent.parent)

app = Flask(__name__)
CORS(app)  # Enable CORS for local development

# Rate limiting configuration
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
    strategy="fixed-window"
)

@app.route('/')
@limiter.exempt
def serve_dashboard():
    """Serve the dashboard HTML file."""
    return send_file(os.path.join(PROJECT_ROOT, 'dashboard.html'))


@app.route('/api/run', methods=['POST'])
@limiter.limit("20 per minute")  # Expensive LLM orchestration
def run_orchestrator():
    """Run the orchestrator with given prompt."""
    data = request.json
    prompt = data.get('prompt', 'Schedule all flexible loads')

    try:
        # Set working directory (container-aware)
        work_dir = os.environ.get('HEMS_WORK_DIR') or PROJECT_ROOT

        # Virtual environment activation (only for local development)
        venv_path = os.environ.get('VENV_PATH', '')
        venv_activate = f'source {venv_path}/bin/activate && ' if os.path.exists(venv_path) else ''

        # Run orchestrator
        result = subprocess.run(
            [
                'bash', '-c',
                f'{venv_activate}python src/orchestrator_agent_react.py {shlex.quote(prompt)}'
            ],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        return jsonify({
            'success': True,
            'prompt': prompt,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode
        })

    except subprocess.TimeoutExpired:
        return jsonify({
            'success': False,
            'error': 'Orchestrator timed out after 5 minutes'
        }), 408

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/run/stream', methods=['POST'])
@limiter.limit("20 per minute")  # Expensive LLM orchestration
def run_orchestrator_stream():
    """Run the orchestrator with real-time streaming output."""
    data = request.json
    prompt = data.get('prompt', 'Schedule all flexible loads')
    model = data.get('model', 'llama3.1-8b')

    def generate():
        try:
            # Set working directory (container-aware)
            work_dir = os.environ.get('HEMS_WORK_DIR') or PROJECT_ROOT

            # Virtual environment activation (only for local development)
            venv_path = os.environ.get('VENV_PATH', '')
            venv_activate = f'source {venv_path}/bin/activate && ' if os.path.exists(venv_path) else ''

            # Set model as environment variable so orchestrator can use it
            env = os.environ.copy()
            env['CEREBRAS_MODEL_OVERRIDE'] = model

            process = subprocess.Popen(
                [
                    'bash', '-c',
                    f'{venv_activate}python -u src/orchestrator_agent_react.py {shlex.quote(prompt)}'
                ],
                env=env,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            # Stream stdout
            for line in iter(process.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'type': 'stdout', 'content': line})}\n\n"

            # Wait for process to complete
            process.wait(timeout=300)

            # Send completion message
            yield f"data: {json.dumps({'type': 'done', 'returncode': process.returncode})}\n\n"

        except subprocess.TimeoutExpired:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Timeout after 5 minutes'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/api/models', methods=['GET'])
@limiter.limit("30 per minute")  # Allow frequent model list fetches
def get_models():
    """
    Fetch available models from LLM provider API.

    Supports:
    - OpenAI-compatible APIs (Cerebras, OpenRouter, etc.)
    - Custom provider-specific endpoints

    Returns categorized list of available models.
    """
    try:
        import requests
        import os

        # Get recently used models from query parameter (sent from frontend)
        recently_used = request.args.get('recently_used', '').split(',') if request.args.get('recently_used') else []
        recently_used = [m for m in recently_used if m]  # Remove empty strings

        # Get API configuration from environment
        api_key = os.getenv('CEREBRAS_API_KEY') or os.getenv('OPENAI_API_KEY') or os.getenv('LLM_API_KEY')
        api_base = os.getenv('LLM_API_BASE', 'https://api.cerebras.ai')
        provider = os.getenv('LLM_PROVIDER', 'cerebras')

        if not api_key:
            return jsonify({
                'success': False,
                'error': 'No API key found. Set CEREBRAS_API_KEY, OPENAI_API_KEY, or LLM_API_KEY'
            }), 500

        # Build API endpoint
        models_endpoint = f'{api_base}/v1/models'

        # Fetch models from API
        response = requests.get(
            models_endpoint,
            headers={
                'Authorization': f'Bearer {api_key}'
            },
            timeout=10
        )

        if response.status_code == 200:
            models_data = response.json()

            # Parse models (OpenAI-compatible format)
            all_models = []

            # Handle different response formats
            if 'data' in models_data:
                # OpenAI/Cerebras format: { "data": [...] }
                all_models = models_data['data']
            elif isinstance(models_data, list):
                # Direct list format
                all_models = models_data

            # Convert all models to standardized format
            model_list = []
            for model in all_models:
                if isinstance(model, dict):
                    model_id = model.get('id', '')
                    model_name = model.get('name', model_id)
                    model_desc = model.get('description', '')
                    created = model.get('created', 0)  # Timestamp for recency
                else:
                    model_id = model_name = str(model)
                    model_desc = ''
                    created = 0

                model_list.append({
                    'id': model_id,
                    'name': model_name,
                    'description': model_desc,
                    'created': created
                })

            # Filter text-only models (exclude image/audio models for HEMS use case)
            text_models = [m for m in model_list if not any(x in m['id'].lower() for x in ['image', 'audio', 'vision', 'whisper', 'tts', 'dall-e'])]

            # Categorize models into three groups
            most_used = []
            newest = []
            other = []

            # 1. Most Used - models that appear in recently_used list
            if recently_used:
                for model_id in recently_used:
                    model = next((m for m in text_models if m['id'] == model_id), None)
                    if model and model not in most_used:
                        most_used.append(model)

            # 2. Newest - top 4 newest models (excluding those in most_used)
            sorted_models = sorted(text_models, key=lambda x: x['created'], reverse=True)
            newest_count = 0
            for model in sorted_models:
                if model not in most_used and newest_count < 4:
                    newest.append(model)
                    newest_count += 1

            # 3. Other - remaining models
            for model in sorted_models:
                if model not in most_used and model not in newest:
                    other.append(model)

            return jsonify({
                'success': True,
                'provider': provider,
                'most_used': most_used,
                'newest': newest,
                'other': other
            })
        else:
            return jsonify({
                'success': False,
                'error': f'API returned status {response.status_code}',
                'provider': provider
            }), 500

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/health', methods=['GET'])
@limiter.exempt  # Health check should not be rate limited
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok'})


# =============================================================================
# AGGREGATOR ENDPOINTS
# =============================================================================

@app.route('/api/aggregator/stream', methods=['POST'])
@limiter.limit("20 per minute")
def run_aggregator_stream():
    """Run the aggregator agent with real-time streaming output."""
    data = request.json
    prompt = data.get('prompt', 'Fulfill today\'s market obligation')
    model = data.get('model', 'llama3.1-8b')

    def generate():
        try:
            work_dir = os.environ.get('HEMS_WORK_DIR') or PROJECT_ROOT
            venv_path = os.environ.get('VENV_PATH', '')
            venv_activate = f'source {venv_path}/bin/activate && ' if os.path.exists(venv_path) else ''

            env = os.environ.copy()
            env['CEREBRAS_MODEL_OVERRIDE'] = model

            process = subprocess.Popen(
                [
                    'bash', '-c',
                    f'{venv_activate}python -u src/orchestrator_agent_react.py --aggregator {shlex.quote(prompt)}'
                ],
                env=env,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            for line in iter(process.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'type': 'stdout', 'content': line})}\n\n"

            process.wait(timeout=300)
            yield f"data: {json.dumps({'type': 'done', 'returncode': process.returncode})}\n\n"

        except subprocess.TimeoutExpired:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Timeout after 5 minutes'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/dr-events', methods=['GET'])
@limiter.limit("30 per minute")
def list_dr_events():
    """List all DR events with their current status."""
    import glob
    events_dir = os.path.join(PROJECT_ROOT, 'data', 'dr_events')

    if not os.path.exists(events_dir):
        return jsonify({'success': True, 'events': []})

    events = []
    for event_file in glob.glob(os.path.join(events_dir, '*.json')):
        with open(event_file, 'r') as f:
            events.append(json.load(f))

    # Sort by created_at descending (most recent first)
    events.sort(key=lambda e: e.get('created_at', ''), reverse=True)

    return jsonify({'success': True, 'events': events})


@app.route('/api/dr-event', methods=['POST'])
@limiter.limit("20 per minute")
def create_dr_event():
    """Create and dispatch a DR event directly (without aggregator agent)."""
    from aggregator_tools import dispatch_dr_event
    data = request.json

    result = dispatch_dr_event(
        household_id=data.get('household_id', 'HH-001'),
        window_start=data.get('window_start'),
        window_end=data.get('window_end'),
        target_kw=float(data.get('target_kw', 0)),
        compensation_eur_kwh=float(data.get('compensation_eur_kwh', 0))
    )

    if result['success']:
        return jsonify(result)
    return jsonify(result), 400


@app.route('/api/dr-event/<event_id>/response', methods=['GET'])
@limiter.limit("30 per minute")
def get_dr_event_response(event_id):
    """Get the household response for a specific DR event."""
    from aggregator_tools import collect_response
    result = collect_response(event_id)
    return jsonify(result)


@app.route('/api/dr-event/<event_id>/log', methods=['GET'])
@limiter.limit("30 per minute")
def get_dr_event_log(event_id):
    """Get the lifecycle event log for a specific DR event."""
    log_path = os.path.join(PROJECT_ROOT, 'data', 'event_logs', f'{event_id}.jsonl')
    if not os.path.exists(log_path):
        return jsonify({'success': False, 'error': 'No event log found', 'entries': []})
    entries = []
    with open(log_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return jsonify({'success': True, 'entries': entries})


@app.route('/api/dr-events/pending', methods=['GET'])
@limiter.limit("60 per minute")
def get_pending_dr_events():
    """Poll for pending DR events for a household (prosumer-side)."""
    from aggregator_tools import get_pending_dr_events as _get_pending
    household_id = request.args.get('household_id', 'HH-001')
    result = _get_pending(household_id)
    return jsonify(result)


@app.route('/api/dr-event/<event_id>/respond/stream', methods=['POST'])
@limiter.limit("20 per minute")
def respond_to_dr_event_stream(event_id):
    """Prosumer responds to a DR event. Triggers HEMS DR handler agent with streaming."""
    data = request.json
    prosumer_message = data.get('message', '')
    model = data.get('model', 'llama3.1-8b')

    def generate():
        try:
            work_dir = os.environ.get('HEMS_WORK_DIR') or PROJECT_ROOT
            venv_path = os.environ.get('VENV_PATH', '')
            venv_activate = f'source {venv_path}/bin/activate && ' if os.path.exists(venv_path) else ''

            env = os.environ.copy()
            env['CEREBRAS_MODEL_OVERRIDE'] = model

            # Pass event_id and optional prosumer follow-up message
            cmd = f'{venv_activate}python -u src/orchestrator_agent_react.py --dr-handler {shlex.quote(event_id)}'
            if prosumer_message:
                cmd += f' --followup {shlex.quote(prosumer_message)}'

            process = subprocess.Popen(
                ['bash', '-c', cmd],
                env=env,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            for line in iter(process.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'type': 'stdout', 'content': line})}\n\n"

            process.wait(timeout=300)
            yield f"data: {json.dumps({'type': 'done', 'returncode': process.returncode})}\n\n"

        except subprocess.TimeoutExpired:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Timeout after 5 minutes'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/dr-event/<event_id>/submit-response', methods=['POST'])
@limiter.limit("20 per minute")
def submit_prosumer_response(event_id):
    """Submit prosumer's decision for a DR event (accept/reject)."""
    from aggregator_tools import submit_dr_response
    data = request.json

    result = submit_dr_response(
        event_id=event_id,
        accepted=data.get('accepted', False),
        commitment_kw=float(data.get('commitment_kw', 0)),
        accepted_appliances=data.get('accepted_appliances', []),
        reasoning=data.get('reasoning', ''),
        conversation_summary=data.get('conversation_summary', '')
    )

    if result['success']:
        return jsonify(result)
    return jsonify(result), 400


@app.route('/api/household-request', methods=['POST'])
@limiter.limit("20 per minute")
def create_household_request():
    """Create a bottom-up request from household to aggregator."""
    from aggregator_tools import create_household_request as _create_request
    data = request.json

    result = _create_request(
        household_id=data.get('household_id', 'HH-001'),
        request_type=data.get('type', 'unknown'),
        message=data.get('message', ''),
        details=data.get('details', {})
    )

    if result['success']:
        return jsonify(result)
    return jsonify(result), 400


@app.route('/api/household-requests', methods=['GET'])
@limiter.limit("30 per minute")
def list_household_requests():
    """List all household requests (for aggregator inbox)."""
    import glob
    requests_dir = os.path.join(PROJECT_ROOT, 'data', 'household_requests')

    if not os.path.exists(requests_dir):
        return jsonify({'success': True, 'requests': []})

    reqs = []
    for req_file in sorted(glob.glob(os.path.join(requests_dir, '*.json')), reverse=True):
        with open(req_file, 'r') as f:
            reqs.append(json.load(f))

    return jsonify({'success': True, 'requests': reqs})


@app.route('/api/household-requests/<request_id>/acknowledge', methods=['POST'])
@limiter.limit("30 per minute")
def acknowledge_household_request(request_id):
    """Mark a household request as acknowledged."""
    requests_dir = os.path.join(PROJECT_ROOT, 'data', 'household_requests')
    req_file = os.path.join(requests_dir, f'{request_id}.json')

    if not os.path.exists(req_file):
        return jsonify({'success': False, 'error': 'Request not found'}), 404

    with open(req_file, 'r') as f:
        req_data = json.load(f)

    req_data['status'] = 'acknowledged'
    req_data['acknowledged_at'] = datetime.now().isoformat()

    with open(req_file, 'w') as f:
        json.dump(req_data, f, indent=2)

    return jsonify({'success': True, 'request_id': request_id})


# =============================================================================
# PROSUMER-TO-AGGREGATOR COMMUNICATION
# =============================================================================

@app.route('/api/prosumer-message', methods=['POST'])
@limiter.limit("10 per minute")
def prosumer_message():
    """Process a free-text prosumer message via single LLM classification call."""
    from aggregator_tools import process_prosumer_message
    data = request.json

    result = process_prosumer_message(
        household_id=data.get('household_id', 'HH-001'),
        message=data.get('message', ''),
        sandbox=data.get('sandbox', True)
    )

    if result.get('success'):
        return jsonify(result)
    return jsonify(result), 400


@app.route('/api/portfolio/reset-sandbox', methods=['POST'])
@limiter.limit("10 per minute")
def reset_sandbox():
    """Reset sandbox portfolio to base state."""
    from aggregator_tools import reset_sandbox_portfolio
    result = reset_sandbox_portfolio()
    if result.get('success'):
        return jsonify(result)
    return jsonify(result), 400


@app.route('/api/portfolio', methods=['GET'])
@limiter.limit("30 per minute")
def get_portfolio():
    """Get portfolio status. Use ?sandbox=true for sandbox version."""
    from aggregator_tools import get_portfolio_status
    sandbox = request.args.get('sandbox', 'false').lower() == 'true'
    result = get_portfolio_status(sandbox=sandbox)
    return jsonify(result)


@app.route('/api/runs', methods=['GET'])
@limiter.limit("30 per minute")
def list_all_runs():
    """List all agent runs (prosumer + aggregator) with metadata."""
    import glob
    base_dir = PROJECT_ROOT
    runs = []

    # Prosumer (HEMS) runs: data/runs/<model>/*.json
    hems_dir = os.path.join(base_dir, 'data', 'runs')
    if os.path.exists(hems_dir):
        for model_dir in os.listdir(hems_dir):
            model_path = os.path.join(hems_dir, model_dir)
            if model_dir in ('aggregator', 'dr_handler') or not os.path.isdir(model_path):
                continue
            for run_file in glob.glob(os.path.join(model_path, 'run_*.json')):
                try:
                    with open(run_file, 'r') as f:
                        data = json.load(f)
                    runs.append({
                        'agent_type': data.get('agent_type', 'hems'),
                        'model': data.get('model', model_dir),
                        'timestamp': data.get('timestamp', ''),
                        'user_request': data.get('user_request', ''),
                        'success': data.get('success', False),
                        'iterations': data.get('iterations', 0),
                        'duration_seconds': data.get('duration_seconds', 0),
                        'total_tokens': data.get('total_tokens', 0),
                        'actions_taken': data.get('actions_taken', []),
                        'final_summary': data.get('final_summary', ''),
                        'file': os.path.basename(run_file),
                    })
                except Exception:
                    continue

    # Aggregator runs: data/runs/aggregator/<model>/*.json
    agg_dir = os.path.join(hems_dir, 'aggregator')
    if os.path.exists(agg_dir):
        for model_dir in os.listdir(agg_dir):
            model_path = os.path.join(agg_dir, model_dir)
            if not os.path.isdir(model_path):
                continue
            for run_file in glob.glob(os.path.join(model_path, 'run_*.json')):
                try:
                    with open(run_file, 'r') as f:
                        data = json.load(f)
                    runs.append({
                        'agent_type': data.get('agent_type', 'aggregator'),
                        'model': data.get('model', model_dir),
                        'timestamp': data.get('timestamp', ''),
                        'user_request': data.get('user_request', ''),
                        'success': data.get('success', False),
                        'iterations': data.get('iterations', 0),
                        'duration_seconds': data.get('duration_seconds', 0),
                        'total_tokens': data.get('total_tokens', 0),
                        'actions_taken': data.get('actions_taken', []),
                        'final_summary': data.get('final_summary', ''),
                        'dispatched_events': data.get('dispatched_events', []),
                        'file': os.path.basename(run_file),
                    })
                except Exception:
                    continue

    # DR handler runs: data/runs/dr_handler/<model>/*.json
    dr_dir = os.path.join(hems_dir, 'dr_handler')
    if os.path.exists(dr_dir):
        for model_dir in os.listdir(dr_dir):
            model_path = os.path.join(dr_dir, model_dir)
            if not os.path.isdir(model_path):
                continue
            for run_file in glob.glob(os.path.join(model_path, 'run_*.json')):
                try:
                    with open(run_file, 'r') as f:
                        data = json.load(f)
                    runs.append({
                        'agent_type': data.get('agent_type', 'dr_handler'),
                        'model': data.get('model', model_dir),
                        'timestamp': data.get('timestamp', ''),
                        'user_request': data.get('user_request', ''),
                        'success': data.get('success', False),
                        'exit_reason': data.get('exit_reason', ''),
                        'iterations': data.get('iterations', 0),
                        'duration_seconds': data.get('duration_seconds', 0),
                        'total_tokens': data.get('total_tokens', 0),
                        'actions_taken': data.get('actions_taken', []),
                        'final_summary': data.get('final_summary', ''),
                        'file': os.path.basename(run_file),
                    })
                except Exception:
                    continue

    # Sort by timestamp descending
    runs.sort(key=lambda r: r.get('timestamp', ''), reverse=True)

    return jsonify({'success': True, 'runs': runs, 'total': len(runs)})



if __name__ == '__main__':
    print("=" * 80)
    print("AGENTIC HEMS + AGGREGATOR API SERVER")
    print("=" * 80)
    print("Starting Flask API on http://localhost:5001")
    print("")
    print("HEMS Endpoints:")
    print("  POST /api/run/stream          - Streaming orchestrator (20 req/min)")
    print("  POST /api/run                 - Orchestrator (20 req/min)")
    print("  GET  /api/models              - Available LLM models (30 req/min)")
    print("")
    print("Aggregator Endpoints:")
    print("  POST /api/aggregator/stream   - Aggregator agent (20 req/min)")
    print("  POST /api/dr-event            - Create DR event (20 req/min)")
    print("  GET  /api/dr-events           - List DR events (30 req/min)")
    print("  GET  /api/dr-event/<id>/response - Event response (30 req/min)")
    print("")
    print("Prosumer Endpoints:")
    print("  GET  /api/dr-events/pending   - Poll pending events (60 req/min)")
    print("  POST /api/dr-event/<id>/respond/stream - DR handler (20 req/min)")
    print("  POST /api/dr-event/<id>/submit-response - Submit decision (20 req/min)")
    print("")
    print("Battery Lab:")
    print("")
    print("Bottom-Up:")
    print("  POST /api/household-request   - Household message (20 req/min)")
    print("  GET  /api/household-requests  - List requests (30 req/min)")
    print("")
    print("Prosumer Communication:")
    print("  POST /api/prosumer-message       - Process prosumer message (10 req/min)")
    print("  POST /api/portfolio/reset-sandbox - Reset sandbox (10 req/min)")
    print("  GET  /api/portfolio              - Portfolio status (30 req/min)")
    print("")
    print("Dashboard: http://localhost:5001?role=aggregator")
    print("           http://localhost:5001?role=prosumer")
    print("")
    print("  GET  /api/health              - Health check (no limit)")
    print("=" * 80)
    app.run(host='0.0.0.0', port=5001, debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
