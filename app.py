from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from typing import Optional, Any

import subprocess
import requests
import json
import time
import re
import os


# ============================================================
# CONFIG
# ============================================================

app = FastAPI(title="AVR Voice Agent Control")

AVR_INFRA = "/root/avr-infra"
COMPOSE = "/root/avr-infra/docker-compose-weeam.yml"
AVR_ENV_FILE = Path("/root/avr-infra/.env")

CONTROL_ENV_FILE = Path("/root/avr-control/.env")
LEADS_FILE = Path("/root/avr-control/leads.json")


# ============================================================
# CONTROL ENV
# ============================================================

def load_control_env():
    if not CONTROL_ENV_FILE.exists():
        return

    for line in CONTROL_ENV_FILE.read_text().splitlines():
        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)

        if key not in os.environ:
            os.environ[key.strip()] = value.strip()


load_control_env()


ARI_BASE = os.environ.get(
    "ARI_BASE",
    "http://127.0.0.1:8088/ari"
)

ARI_USER = os.environ.get(
    "ARI_USER",
    "aiagent"
)

ARI_PASSWORD = os.environ.get(
    "ARI_PASSWORD",
    ""
)


# ============================================================
# PYDANTIC MODELS
# ============================================================

class CallRequest(BaseModel):
    number: str


class AgentSettings(BaseModel):
    model: str
    voice: str
    turn_detection: str
    max_tokens: int
    instructions: str


class LeadUpdate(BaseModel):
    call_id: str

    phone: Optional[str] = None
    name: Optional[str] = None

    intent: Optional[str] = None
    purpose: Optional[str] = None

    area: Optional[str] = None
    property_type: Optional[str] = None

    bedrooms: Optional[Any] = None
    budget_aed: Optional[Any] = None

    payment_method: Optional[str] = None
    property_status: Optional[str] = None

    timeline: Optional[str] = None
    location: Optional[str] = None

    follow_up: Optional[str] = None
    notes: Optional[str] = None

    status: Optional[str] = "in_progress"


class EndCallRequest(BaseModel):
    call_id: str
    reason: Optional[str] = "qualification_complete"


# ============================================================
# COMMAND EXECUTION
# ============================================================

def run(cmd, cwd=None, timeout=30):

    try:

        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        return {
            "code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()
        }

    except subprocess.TimeoutExpired:

        return {
            "code": 124,
            "stdout": "",
            "stderr": "Command timed out"
        }


def container_status(name):

    result = run([
        "docker",
        "inspect",
        "-f",
        "{{.State.Status}}",
        name
    ])

    if result["code"] != 0:
        return "missing"

    return result["stdout"]


# ============================================================
# AVR .ENV MANAGEMENT
# ============================================================

def read_avr_env():

    values = {}

    if not AVR_ENV_FILE.exists():
        return values

    for line in AVR_ENV_FILE.read_text().splitlines():

        stripped = line.strip()

        if not stripped:
            continue

        if stripped.startswith("#"):
            continue

        if "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)

        values[key.strip()] = value

    return values


def update_avr_env(updates):

    if not AVR_ENV_FILE.exists():
        raise RuntimeError(
            "/root/avr-infra/.env does not exist"
        )

    original_lines = AVR_ENV_FILE.read_text().splitlines()

    output = []
    updated = set()

    for line in original_lines:

        stripped = line.strip()

        if "=" in stripped and not stripped.startswith("#"):

            key = stripped.split("=", 1)[0].strip()

            if key in updates:

                value = str(updates[key])

                value = value.replace(
                    "\r",
                    " "
                ).replace(
                    "\n",
                    " "
                )

                output.append(
                    f"{key}={value}"
                )

                updated.add(key)

                continue

        output.append(line)

    for key, value in updates.items():

        if key not in updated:

            value = str(value)

            value = value.replace(
                "\r",
                " "
            ).replace(
                "\n",
                " "
            )

            output.append(
                f"{key}={value}"
            )

    AVR_ENV_FILE.write_text(
        "\n".join(output) + "\n"
    )


# ============================================================
# LEAD STORAGE
# ============================================================

def load_leads():

    if not LEADS_FILE.exists():
        return {}

    try:

        data = json.loads(
            LEADS_FILE.read_text()
        )

        if isinstance(data, dict):
            return data

        return {}

    except Exception:

        return {}


def save_leads(data):

    tmp_file = Path(
        str(LEADS_FILE) + ".tmp"
    )

    tmp_file.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        )
    )

    tmp_file.replace(
        LEADS_FILE
    )


# ============================================================
# ASTERISK ARI
# ============================================================

def ari_request(
    method,
    path,
    **kwargs
):

    if not ARI_PASSWORD:

        raise RuntimeError(
            "ARI_PASSWORD is not configured"
        )

    url = (
        ARI_BASE.rstrip("/")
        + "/"
        + path.lstrip("/")
    )

    response = requests.request(
        method,
        url,
        auth=(
            ARI_USER,
            ARI_PASSWORD
        ),
        timeout=10,
        **kwargs
    )

    return response


def get_ari_channels():

    r = ari_request(
        "GET",
        "/channels"
    )

    if r.status_code != 200:

        raise RuntimeError(
            f"ARI returned HTTP {r.status_code}: {r.text}"
        )

    return r.json()


def find_active_gsm_channels():

    channels = get_ari_channels()

    return [
        channel
        for channel in channels
        if "PJSIP/4001-" in channel.get(
            "name",
            ""
        )
    ]


def find_active_channel_for_call(
    call_id
):

    channels = get_ari_channels()

    #
    # Exact matching first.
    #
    for channel in channels:

        channel_id = str(
            channel.get(
                "id",
                ""
            )
        )

        channel_name = str(
            channel.get(
                "name",
                ""
            )
        )

        if call_id:

            if call_id in channel_id:
                return channel

            if call_id in channel_name:
                return channel

    #
    # Current testing fallback.
    #
    # If there is exactly ONE active GoIP 4001 channel,
    # it is safe enough for single-call testing.
    #
    gsm_channels = [
        channel
        for channel in channels
        if "PJSIP/4001-" in channel.get(
            "name",
            ""
        )
    ]

    if len(gsm_channels) == 1:
        return gsm_channels[0]

    return None


def hangup_channel(
    channel_id
):

    r = ari_request(
        "DELETE",
        f"/channels/{channel_id}"
    )

    if r.status_code not in (
        204,
        404
    ):

        raise RuntimeError(
            f"ARI hangup returned "
            f"HTTP {r.status_code}: {r.text}"
        )

    return True


# ============================================================
# SYSTEM STATUS
# ============================================================

@app.get("/api/status")
def status():

    ast = run([
        "asterisk",
        "-rx",
        "core show version"
    ])

    ari_ok = False

    try:

        r = ari_request(
            "GET",
            "/asterisk/info"
        )

        ari_ok = (
            r.status_code == 200
        )

    except Exception:

        ari_ok = False

    return {

        "asterisk": {
            "online":
                ast["code"] == 0,

            "version":
                ast["stdout"],

            "ari":
                ari_ok
        },

        "avr_core":
            container_status(
                "avr-core"
            ),

        "avr_sts_openai":
            container_status(
                "avr-sts-openai"
            )
    }


# ============================================================
# ACTIVE CALLS
# ============================================================

@app.get("/api/calls/active")
def active_calls():

    try:

        channels = get_ari_channels()

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    result = []

    for channel in channels:

        result.append({

            "id":
                channel.get("id"),

            "name":
                channel.get("name"),

            "state":
                channel.get("state"),

            "caller":
                channel.get(
                    "caller",
                    {}
                ),

            "connected":
                channel.get(
                    "connected",
                    {}
                ),

            "created_at":
                channel.get(
                    "creationtime"
                )
        })

    return {
        "count": len(result),
        "channels": result
    }


# ============================================================
# OUTBOUND CALL
# ============================================================

@app.post("/api/call")
def call(req: CallRequest):

    number = req.number.strip()

    if not re.fullmatch(
        r"00[0-9]{8,16}",
        number
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Use international format "
                "starting with 00"
            )
        )

    dial = (
        f"PJSIP/7{number}@4001"
    )

    asterisk_cmd = (
        f"channel originate "
        f"{dial} "
        f"extension s@avr-ai-test"
    )

    result = run([
        "asterisk",
        "-rx",
        asterisk_cmd
    ])

    if result["code"] != 0:

        raise HTTPException(
            status_code=500,
            detail=(
                result["stderr"]
                or
                result["stdout"]
            )
        )

    return {
        "ok": True,
        "number": number,
        "dial": dial,
        "command": asterisk_cmd,
        "result": result["stdout"]
    }


# ============================================================
# MANUAL HANGUP
# ============================================================

@app.post(
    "/api/channel/{channel_id}/hangup"
)
def manual_hangup(
    channel_id: str
):

    try:

        hangup_channel(
            channel_id
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    return {
        "ok": True,
        "channel_id": channel_id
    }


# ============================================================
# LEAD UPDATE
# ============================================================

@app.post("/api/lead/update")
def update_lead(
    req: LeadUpdate
):

    leads = load_leads()

    existing = leads.get(
        req.call_id,
        {}
    )

    incoming = req.model_dump()

    for key, value in incoming.items():

        if value is not None:

            existing[key] = value

    existing["updated_at"] = int(
        time.time()
    )

    if "created_at" not in existing:

        existing["created_at"] = int(
            time.time()
        )

    leads[
        req.call_id
    ] = existing

    save_leads(
        leads
    )

    return {
        "ok": True,
        "lead": existing
    }


# ============================================================
# GET ONE LEAD
# ============================================================

@app.get(
    "/api/lead/{call_id}"
)
def get_lead(
    call_id: str
):

    leads = load_leads()

    if call_id not in leads:

        raise HTTPException(
            status_code=404,
            detail="Lead not found"
        )

    return leads[
        call_id
    ]


# ============================================================
# GET ALL LEADS
# ============================================================

@app.get("/api/leads")
def get_all_leads():

    leads = load_leads()

    items = list(
        leads.values()
    )

    items.sort(
        key=lambda item:
        item.get(
            "updated_at",
            0
        ),
        reverse=True
    )

    return {
        "count": len(items),
        "leads": items
    }


# ============================================================
# AUTOMATIC END CALL
# ============================================================

@app.post("/api/call/end")
def end_call(
    req: EndCallRequest
):

    leads = load_leads()

    channel = None

    try:

        channel = (
            find_active_channel_for_call(
                req.call_id
            )
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    if not channel:

        raise HTTPException(
            status_code=409,
            detail=(
                "Could not uniquely identify "
                "the active GSM channel. "
                "Automatic hangup was NOT performed."
            )
        )

    channel_id = channel.get(
        "id"
    )

    channel_name = channel.get(
        "name"
    )

    try:

        hangup_channel(
            channel_id
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    if req.call_id in leads:

        leads[
            req.call_id
        ][
            "status"
        ] = "completed"

        leads[
            req.call_id
        ][
            "end_reason"
        ] = req.reason

        leads[
            req.call_id
        ][
            "ended_at"
        ] = int(
            time.time()
        )

        save_leads(
            leads
        )

    return {
        "ok": True,
        "ended": True,
        "call_id": req.call_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "reason": req.reason
    }


# ============================================================
# AGENT SETTINGS
# ============================================================

@app.get("/api/settings")
def get_settings():

    env = read_avr_env()

    try:

        max_tokens = int(
            env.get(
                "OPENAI_MAX_TOKENS",
                "200"
            )
        )

    except Exception:

        max_tokens = 200

    return {

        "model":
            env.get(
                "OPENAI_MODEL",
                "gpt-realtime-2"
            ),

        "voice":
            env.get(
                "OPENAI_VOICE",
                "alloy"
            ),

        "turn_detection":
            env.get(
                "OPENAI_TURN_DETECTION",
                "server_vad"
            ),

        "max_tokens":
            max_tokens,

        "instructions":
            env.get(
                "OPENAI_INSTRUCTIONS",
                ""
            )
    }


@app.post("/api/settings")
def save_settings(
    settings: AgentSettings
):

    if (
        settings.max_tokens < 50
        or
        settings.max_tokens > 4096
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "max_tokens must be "
                "between 50 and 4096"
            )
        )

    if not settings.model.strip():

        raise HTTPException(
            status_code=400,
            detail="Model cannot be empty"
        )

    if not settings.voice.strip():

        raise HTTPException(
            status_code=400,
            detail="Voice cannot be empty"
        )

    if not settings.instructions.strip():

        raise HTTPException(
            status_code=400,
            detail="Instructions cannot be empty"
        )

    update_avr_env({

        "OPENAI_MODEL":
            settings.model.strip(),

        "OPENAI_VOICE":
            settings.voice.strip(),

        "OPENAI_TURN_DETECTION":
            settings.turn_detection.strip(),

        "OPENAI_MAX_TOKENS":
            settings.max_tokens,

        "OPENAI_INSTRUCTIONS":
            settings.instructions.strip()
    })

    result = run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE,
            "up",
            "-d",
            "--force-recreate",
            "avr-sts-openai"
        ],
        cwd=AVR_INFRA,
        timeout=120
    )

    if result["code"] != 0:

        raise HTTPException(
            status_code=500,
            detail=(
                result["stderr"]
                or
                result["stdout"]
            )
        )

    return {
        "ok": True,
        "message": (
            "Settings saved and "
            "OpenAI STS recreated"
        )
    }


# ============================================================
# RESTART AI
# ============================================================

@app.post("/api/restart-ai")
def restart_ai():

    result = run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE,
            "restart",
            "avr-core",
            "avr-sts-openai"
        ],
        cwd=AVR_INFRA,
        timeout=120
    )

    return result


# ============================================================
# LOGS
# ============================================================

@app.get("/api/logs/{service}")
def logs(
    service: str
):

    allowed = {
        "core":
            "avr-core",

        "openai":
            "avr-sts-openai"
    }

    if service not in allowed:

        raise HTTPException(
            status_code=400,
            detail="Invalid service"
        )

    result = run([
        "docker",
        "logs",
        "--tail",
        "250",
        allowed[service]
    ])

    combined = (
        result["stdout"]
        + "\n"
        + result["stderr"]
    ).strip()

    return {
        "service": service,
        "logs": combined
    }


# ============================================================
# WEB UI
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def index():

    return """
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<title>
AVR Voice Agent
</title>

<style>

body {
    margin: 0;
    background: #0f172a;
    color: #e2e8f0;
    font-family: Arial, sans-serif;
}

.container {
    max-width: 1150px;
    margin: 30px auto;
    padding: 20px;
}

.card {
    background: #1e293b;
    border-radius: 12px;
    padding: 22px;
    margin-bottom: 18px;
}

h1 {
    margin-bottom: 4px;
}

h2 {
    margin-top: 0;
}

.muted {
    color: #94a3b8;
}

.status {
    display: flex;
    gap: 15px;
    flex-wrap: wrap;
}

.status-item {
    background: #0f172a;
    min-width: 180px;
    padding: 14px;
    border-radius: 8px;
}

.ok {
    color: #4ade80;
}

.bad {
    color: #f87171;
}

input,
textarea,
select {
    width: 100%;
    box-sizing: border-box;
    padding: 11px;
    margin-top: 5px;
    margin-bottom: 12px;
    color: white;
    background: #0f172a;
    border: 1px solid #475569;
    border-radius: 8px;
}

textarea {
    height: 260px;
    resize: vertical;
}

button {
    border: 0;
    border-radius: 8px;
    padding: 11px 16px;
    margin-right: 6px;
    cursor: pointer;
    font-weight: bold;
}

.primary {
    background: #2563eb;
    color: white;
}

.success {
    background: #059669;
    color: white;
}

.secondary {
    background: #475569;
    color: white;
}

.danger {
    background: #dc2626;
    color: white;
}

.grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 15px;
}

pre {
    background: #020617;
    padding: 15px;
    border-radius: 8px;
    white-space: pre-wrap;
    max-height: 450px;
    overflow: auto;
}

table {
    width: 100%;
    border-collapse: collapse;
}

td,
th {
    padding: 10px;
    border-bottom: 1px solid #334155;
    text-align: left;
}

.message {
    color: #94a3b8;
    margin-top: 8px;
}

@media(max-width:700px) {
    .grid {
        grid-template-columns: 1fr;
    }
}

</style>

</head>


<body>

<div class="container">

<h1>
AVR Voice Agent
</h1>

<div class="muted">
Asterisk + AVR Core + OpenAI Realtime
</div>


<div class="card">

<h2>
System Status
</h2>

<div class="status">

<div class="status-item">
Asterisk<br>
<b id="asterisk">
Checking...
</b>
</div>

<div class="status-item">
Asterisk ARI<br>
<b id="ari">
Checking...
</b>
</div>

<div class="status-item">
AVR Core<br>
<b id="core">
Checking...
</b>
</div>

<div class="status-item">
OpenAI Realtime<br>
<b id="openai">
Checking...
</b>
</div>

</div>

</div>


<div class="card">

<h2>
Outbound GSM Call
</h2>

<input
id="number"
placeholder="00923041349020"
>

<button
class="primary"
onclick="makeCall()"
>
Call
</button>

<button
class="secondary"
onclick="loadCalls()"
>
Refresh Calls
</button>

<div
id="callResult"
class="message"
>
</div>

</div>


<div class="card">

<h2>
Active Asterisk Channels
</h2>

<div id="calls">
Loading...
</div>

</div>


<div class="card">

<h2>
Agent Configuration
</h2>

<div class="grid">

<div>

<label>
OpenAI Model
</label>

<input
id="model"
>

</div>


<div>

<label>
Voice
</label>

<input
id="voice"
>

</div>


<div>

<label>
Turn Detection
</label>

<select
id="turn_detection"
>

<option
value="server_vad"
>
server_vad
</option>

<option
value="semantic_vad"
>
semantic_vad
</option>

</select>

</div>


<div>

<label>
Max Tokens
</label>

<input
id="max_tokens"
type="number"
min="50"
max="4096"
>

</div>

</div>


<label>
Agent Instructions
</label>

<textarea
id="instructions"
></textarea>


<button
class="success"
onclick="saveSettings()"
>
Save & Apply
</button>

<span
id="settingsResult"
class="message"
></span>

</div>


<div class="card">

<h2>
Collected Leads
</h2>

<button
class="secondary"
onclick="loadLeads()"
>
Refresh Leads
</button>

<pre id="leads">
No data loaded.
</pre>

</div>


<div class="card">

<h2>
AI Runtime
</h2>

<button
class="secondary"
onclick="restartAI()"
>
Restart AI
</button>

<button
class="secondary"
onclick="showLogs('core')"
>
AVR Core Logs
</button>

<button
class="secondary"
onclick="showLogs('openai')"
>
OpenAI Logs
</button>

</div>


<div class="card">

<h2>
Logs
</h2>

<pre id="logs">
Select a log source.
</pre>

</div>


</div>


<script>


async function refreshStatus() {

    try {

        const r =
            await fetch('/api/status');

        const d =
            await r.json();


        document.getElementById(
            'asterisk'
        ).innerHTML =
            d.asterisk.online
            ? '<span class="ok">● Online</span>'
            : '<span class="bad">● Offline</span>';


        document.getElementById(
            'ari'
        ).innerHTML =
            d.asterisk.ari
            ? '<span class="ok">● Connected</span>'
            : '<span class="bad">● Offline</span>';


        document.getElementById(
            'core'
        ).innerHTML =
            d.avr_core === 'running'
            ? '<span class="ok">● Running</span>'
            : '<span class="bad">● '
              + d.avr_core
              + '</span>';


        document.getElementById(
            'openai'
        ).innerHTML =
            d.avr_sts_openai === 'running'
            ? '<span class="ok">● Running</span>'
            : '<span class="bad">● '
              + d.avr_sts_openai
              + '</span>';

    }

    catch(e) {

        console.error(e);

    }
}


async function makeCall() {

    const number =
        document.getElementById(
            'number'
        ).value;


    const result =
        document.getElementById(
            'callResult'
        );


    result.innerText =
        'Starting call...';


    const r =
        await fetch(
            '/api/call',
            {
                method: 'POST',

                headers: {
                    'Content-Type':
                        'application/json'
                },

                body:
                    JSON.stringify({
                        number
                    })
            }
        );


    const d =
        await r.json();


    if (!r.ok) {

        result.innerText =
            'Error: '
            + (
                d.detail
                ||
                'Call failed'
            );

        return;
    }


    result.innerText =
        'Calling '
        + d.number;


    setTimeout(
        loadCalls,
        1500
    );
}


async function loadCalls() {

    const holder =
        document.getElementById(
            'calls'
        );


    const r =
        await fetch(
            '/api/calls/active'
        );


    const d =
        await r.json();


    if (!r.ok) {

        holder.innerText =
            'Error loading calls';

        return;
    }


    if (!d.channels.length) {

        holder.innerHTML =
            '<span class="muted">'
            + 'No active channels'
            + '</span>';

        return;
    }


    let html =
        '<table>'
        + '<tr>'
        + '<th>Channel</th>'
        + '<th>State</th>'
        + '<th>Action</th>'
        + '</tr>';


    for (
        const channel
        of d.channels
    ) {

        html +=
            '<tr>'
            + '<td>'
            + channel.name
            + '</td>'
            + '<td>'
            + channel.state
            + '</td>'
            + '<td>'
            + '<button '
            + 'class="danger" '
            + 'onclick="hangupChannel(\\''
            + channel.id
            + '\\')">'
            + 'Hangup'
            + '</button>'
            + '</td>'
            + '</tr>';
    }


    html +=
        '</table>';


    holder.innerHTML =
        html;
}


async function hangupChannel(
    channelId
) {

    const r =
        await fetch(
            '/api/channel/'
            + encodeURIComponent(
                channelId
            )
            + '/hangup',
            {
                method: 'POST'
            }
        );


    const d =
        await r.json();


    if (!r.ok) {

        alert(
            d.detail
            ||
            'Hangup failed'
        );

        return;
    }


    setTimeout(
        loadCalls,
        500
    );
}


async function loadSettings() {

    const r =
        await fetch(
            '/api/settings'
        );


    const d =
        await r.json();


    document.getElementById(
        'model'
    ).value =
        d.model;


    document.getElementById(
        'voice'
    ).value =
        d.voice;


    document.getElementById(
        'turn_detection'
    ).value =
        d.turn_detection;


    document.getElementById(
        'max_tokens'
    ).value =
        d.max_tokens;


    document.getElementById(
        'instructions'
    ).value =
        d.instructions;
}


async function saveSettings() {

    const result =
        document.getElementById(
            'settingsResult'
        );


    result.innerText =
        'Saving...';


    const payload = {

        model:
            document.getElementById(
                'model'
            ).value,

        voice:
            document.getElementById(
                'voice'
            ).value,

        turn_detection:
            document.getElementById(
                'turn_detection'
            ).value,

        max_tokens:
            Number(
                document.getElementById(
                    'max_tokens'
                ).value
            ),

        instructions:
            document.getElementById(
                'instructions'
            ).value
    };


    const r =
        await fetch(
            '/api/settings',
            {
                method: 'POST',

                headers: {
                    'Content-Type':
                        'application/json'
                },

                body:
                    JSON.stringify(
                        payload
                    )
            }
        );


    const d =
        await r.json();


    if (!r.ok) {

        result.innerText =
            'Error: '
            + (
                d.detail
                ||
                'Save failed'
            );

        return;
    }


    result.innerText =
        '✓ Saved and applied';
}


async function loadLeads() {

    const r =
        await fetch(
            '/api/leads'
        );


    const d =
        await r.json();


    document.getElementById(
        'leads'
    ).innerText =
        JSON.stringify(
            d,
            null,
            2
        );
}


async function restartAI() {

    document.getElementById(
        'logs'
    ).innerText =
        'Restarting...';


    const r =
        await fetch(
            '/api/restart-ai',
            {
                method: 'POST'
            }
        );


    const d =
        await r.json();


    document.getElementById(
        'logs'
    ).innerText =
        JSON.stringify(
            d,
            null,
            2
        );


    setTimeout(
        refreshStatus,
        2000
    );
}


async function showLogs(
    service
) {

    const r =
        await fetch(
            '/api/logs/'
            + service
        );


    const d =
        await r.json();


    document.getElementById(
        'logs'
    ).innerText =
        d.logs;
}


refreshStatus();

loadSettings();

loadCalls();

loadLeads();


setInterval(
    refreshStatus,
    5000
);


setInterval(
    loadCalls,
    3000
);


</script>

</body>

</html>
"""


# ============================================================
# END
# ============================================================
