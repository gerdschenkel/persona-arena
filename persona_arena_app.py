"""
Persona Arena — AI-powered multi-perspective thinking tool.
Six customisable AI personas debate any topic.
Run: python persona_arena_app.py  |  Open: http://localhost:5004
"""

from flask import Flask, Response, render_template_string, request, jsonify, redirect, session
import anthropic
import json
import os
import re
import smtplib
import threading
import time
import webbrowser
from io import BytesIO
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'persona-arena-dev-key-change-in-prod')

MODEL = "claude-opus-4-6"

# ── Persona defaults ───────────────────────────────────────────────────────────

PERSONA_DEFAULTS = [
    {'id': 'p1', 'gender': 'Man',        'age': 35, 'mindset': 'Scared'},
    {'id': 'p2', 'gender': 'Woman',      'age': 22, 'mindset': 'Creative'},
    {'id': 'p3', 'gender': 'Man',        'age': 75, 'mindset': 'Nostalgic'},
    {'id': 'p4', 'gender': 'Woman',      'age': 45, 'mindset': 'Empathetic'},
    {'id': 'p5', 'gender': 'Non-binary', 'age': 45, 'mindset': 'Disappointed'},
    {'id': 'p6', 'gender': 'Cat',        'age':  5, 'mindset': 'Playful'},
]

PERSONA_PDF_COLORS = {
    'p1': '#a78bfa', 'p2': '#22d3ee', 'p3': '#fb923c',
    'p4': '#f472b6', 'p5': '#818cf8', 'p6': '#a3e635',
}

JUDGE_SYSTEM = (
    "You are a synthesis facilitator for a multi-persona thinking session. "
    "Your role is to evaluate all perspectives and synthesise them into clear, "
    "actionable insights. Be decisive, specific, and genuinely helpful. "
    "Identify the most valuable contributions without bias."
)

SOURCE_RULE = (
    "After your point, on a new line write **Sources:** followed by 1–2 markdown links "
    "to real, verifiable sources: [Description](https://url.com). "
    "Only use URLs from reputable sites (Reuters, BBC, Nature, WHO, government sites, Wikipedia). "
    "If unsure of a specific article URL, link to the publication's homepage instead."
)

# ── Persona helpers ────────────────────────────────────────────────────────────

def persona_avatar(gender, age):
    g = str(gender).lower().strip()
    try:
        a = int(age)
    except (ValueError, TypeError):
        a = 30
    if 'cat' in g:
        return '🐱'
    if 'dog' in g:
        return '🐶'
    if 'non' in g or 'enby' in g or 'nb' == g:
        return '🧓' if a >= 65 else ('🧒' if a < 13 else '🧑')
    if 'woman' in g or 'female' in g or 'girl' in g:
        return '👵' if a >= 65 else ('👧' if a < 13 else '👩')
    if 'man' in g or 'male' in g or 'boy' in g:
        return '👴' if a >= 65 else ('👦' if a < 13 else '👨')
    return '🧑'

def persona_system(gender, age, mindset):
    g = str(gender).strip()
    m = str(mindset).strip()
    try:
        a = int(age)
    except (ValueError, TypeError):
        a = age
    if 'cat' in g.lower():
        return (
            f"You are a {a}-year-old cat with a {m} personality. "
            "You see the world through feline instincts — self-interested, curious, occasionally profound. "
            "Respond in short, direct, instinct-driven observations. Stay in character as a cat."
        )
    return (
        f"You are a {a}-year-old {g} with a {m} mindset. "
        "Let your age, gender identity, and emotional state genuinely shape how you see this topic. "
        "Be specific, personal, and authentic to this persona."
    )

def persona_label(gender, age, mindset):
    return f"{gender}, {age}, {mindset}"

def persona_prompt(question, round_num, gender, age, mindset, prev_points=None, peer_points=None):
    extra = (
        "Share your opening perspective on this topic."
        if round_num == 1
        else "Share a completely new and distinct perspective — different from everything you have already said."
    )
    prev_block = ""
    if prev_points:
        prev_block = (
            f"\n\nYour PREVIOUS points in this session:\n{prev_points}\n\n"
            "You MUST share a substantively different perspective — "
            "do NOT repeat, rephrase, or echo any of the above."
        )
    peer_block = ""
    if peer_points:
        peer_block = (
            f"\n\nOther perspectives already shared this round:\n{peer_points}\n\n"
            "You may acknowledge, build on, or contrast with these — "
            "or take an entirely independent direction."
        )
    return (
        f'Topic: "{question}"\n\n'
        f"Round {round_num}: {extra}{prev_block}{peer_block}\n\n"
        f"State your perspective in 60 words or fewer, then add sources.\n{SOURCE_RULE}"
    )

def build_judge_prompt(question, transcript_data, active_personas):
    sections = []
    total_rounds = max((t.get("round", 0) for t in transcript_data), default=0)
    for p in active_personas:
        pid   = p['id']
        label = persona_label(p['gender'], p['age'], p['mindset'])
        avatar = persona_avatar(p['gender'], p['age'])
        turns = [t for t in transcript_data if t.get("persona_id") == pid]
        if turns:
            points = "\n".join(f"  Round {t.get('round','?')}: {t['text']}" for t in turns)
            sections.append(f"=== {avatar} {label} ({pid}) ===\n{points}")
    pid_list = ' / '.join(p['id'] for p in active_personas)
    return (
        f'Topic: "{question}"\n\n'
        f"A multi-persona session ran {total_rounds} round(s) with {len(active_personas)} persona(s):\n\n"
        + "\n\n".join(sections)
        + "\n\nIn 2–3 sentences each, provide:\n"
        "1. The single strongest insight from each persona\n"
        "2. One overall recommendation from the combined perspectives\n"
        "3. Which voice was most decisive or insightful, and why (one sentence)\n\n"
        "Be concise. Then on the very last line write exactly:\n"
        f"MVP: persona_id\n"
        f"(Replace persona_id with one of: {pid_list})"
    )

# ── PDF ────────────────────────────────────────────────────────────────────────

GDRIVE_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gdrive_token.json')
GDRIVE_SCOPES     = ['https://www.googleapis.com/auth/drive.file']

def _strip_md(text):
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    text = re.sub(
        r'\[(.+?)\]\((https?://[^\)]+)\)',
        r'<a href="\2" color="#3b82f6">\1</a>', text
    )
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*]\s+', '\u2022 ', text, flags=re.MULTILINE)
    return text.strip()

def build_pdf_bytes(question, rounds, active_personas, mvp_id, verdict_text, transcript):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.colors import HexColor

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2.5*cm, rightMargin=2.5*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    def sty(name, **kw):
        defaults = dict(fontName='Helvetica', fontSize=9.5, leading=14,
                        textColor=HexColor('#333333'), spaceAfter=4)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    s_title  = sty('T', fontSize=20, leading=26, fontName='Helvetica-Bold',
                   textColor=HexColor('#1a1a2e'), spaceAfter=3)
    s_sub    = sty('S', fontSize=8,  textColor=HexColor('#888888'), spaceAfter=10)
    s_label  = sty('L', fontSize=7,  textColor=HexColor('#888888'),
                   fontName='Helvetica', spaceAfter=2, leading=9)
    s_topic  = sty('Q', fontSize=13, leading=17, fontName='Helvetica-Bold',
                   textColor=HexColor('#1a1a2e'), spaceAfter=6)
    s_meta   = sty('M', fontSize=8.5, textColor=HexColor('#555555'), spaceAfter=8)
    s_body   = sty('B', fontSize=9.5, leading=14, spaceAfter=5)
    s_round  = sty('R', fontSize=7.5, textColor=HexColor('#888888'), spaceAfter=2, leading=10)
    s_footer = sty('F', fontSize=7.5, textColor=HexColor('#aaaaaa'),
                   fontName='Helvetica-Oblique')

    story = []
    hr  = lambda: HRFlowable(width='100%', thickness=0.8, color=HexColor('#dddddd'), spaceAfter=8)
    sp  = lambda n=6: Spacer(1, n)
    add = story.append

    add(Paragraph('Persona Arena', s_title))
    add(Paragraph('\u00a9 2026 BGAD Consulting \u00b7 bgadconsulting.com', s_sub))
    add(hr())
    add(Paragraph('TOPIC', s_label))
    add(Paragraph(question or '\u2014', s_topic))

    persona_names = ', '.join(
        persona_label(p['gender'], p['age'], p['mindset']) for p in active_personas
    )
    add(Paragraph(
        f"{rounds} round{'s' if rounds != 1 else ''} \u00b7 {len(active_personas)} personas",
        s_meta
    ))

    mvp_persona = next((p for p in active_personas if p['id'] == mvp_id), None)
    if mvp_persona:
        color  = HexColor(PERSONA_PDF_COLORS.get(mvp_id, '#888888'))
        avatar = persona_avatar(mvp_persona['gender'], mvp_persona['age'])
        label  = persona_label(mvp_persona['gender'], mvp_persona['age'], mvp_persona['mindset'])
        s_mvp  = sty('MVP', fontSize=10, fontName='Helvetica-Bold',
                     textColor=color, spaceAfter=8,
                     borderColor=color, borderWidth=1, borderPadding=6)
        add(Paragraph(f"Most Insightful Voice: {avatar} {label}", s_mvp))

    add(sp(8)); add(hr())
    add(Paragraph('SYNTHESIS', s_label))
    for para in _strip_md(verdict_text).split('\n\n'):
        para = para.strip()
        if para:
            add(Paragraph(para.replace('\n', ' '), s_body))

    add(sp(12)); add(hr())
    add(Paragraph('FULL TRANSCRIPT', s_label))
    add(sp(6))

    for p in active_personas:
        pid   = p['id']
        turns = [t for t in transcript if t.get('persona_id') == pid]
        if not turns:
            continue
        color  = HexColor(PERSONA_PDF_COLORS.get(pid, '#888888'))
        avatar = persona_avatar(p['gender'], p['age'])
        label  = persona_label(p['gender'], p['age'], p['mindset'])
        s_ph   = sty('P' + pid, fontSize=11, fontName='Helvetica-Bold',
                     textColor=color, spaceAfter=4, leading=14)
        add(Paragraph(f"{avatar} {label}", s_ph))
        for t in turns:
            add(Paragraph(f"Round {t.get('round', '?')}", s_round))
            for line in _strip_md(t.get('text', '')).split('\n'):
                line = line.strip()
                if line:
                    add(Paragraph(line, s_body))
            add(sp(4))
        add(sp(8))

    add(hr())
    add(Paragraph(
        'Verify all cited sources independently. '
        'Generated by Persona Arena \u00b7 bgadconsulting.com', s_footer))

    doc.build(story)
    buf.seek(0)
    return buf.read()

# ── Google Drive helpers ───────────────────────────────────────────────────────

def _gdrive_config():
    cid  = os.environ.get('GOOGLE_CLIENT_ID',     '')
    csec = os.environ.get('GOOGLE_CLIENT_SECRET', '')
    if cid and csec:
        return {"web": {"client_id": cid, "client_secret": csec,
                        "auth_uri":  "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token"}}
    cf = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'google_credentials.json')
    if os.path.exists(cf):
        with open(cf) as f:
            return json.load(f)
    return None

def _gdrive_redirect_uri():
    base = os.environ.get('BASE_URL', 'http://localhost:5004')
    return base.rstrip('/') + '/gdrive/callback'

def _load_gdrive_creds():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        if not os.path.exists(GDRIVE_TOKEN_FILE):
            return None
        creds = Credentials.from_authorized_user_file(GDRIVE_TOKEN_FILE, GDRIVE_SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
            with open(GDRIVE_TOKEN_FILE, 'w') as f:
                f.write(creds.to_json())
            return creds
    except Exception:
        pass
    return None

# ── Routes ─────────────────────────────────────────────────────────────────────

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE,
                                  defaults=json.dumps(PERSONA_DEFAULTS))

@app.route("/logo")
def serve_logo():
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if not os.path.exists(logo_path):
        return "", 404
    with open(logo_path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "max-age=86400"})

@app.route("/recreate_persona", methods=["POST"])
def recreate_persona():
    body       = request.get_json(force=True, silent=True) or {}
    all_p      = body.get("personas",   [])
    target_id  = body.get("target_id", "")

    others = [p for p in all_p if p.get("id") != target_id]
    others_desc = "\n".join(
        f"- {persona_label(p['gender'], p['age'], p['mindset'])}" for p in others
    ) if others else "None yet."

    prompt = (
        "You are designing a diverse AI persona for a multi-perspective thinking tool.\n\n"
        f"Existing personas in this session:\n{others_desc}\n\n"
        "Create ONE new persona that maximises diversity relative to the existing ones. "
        "Vary gender, age (range 1–95), and mindset — all three should contrast with what already exists.\n\n"
        "Gender can be anything — examples: Man, Woman, Non-binary, Cat, Dog, Bird, Horse, "
        "Rabbit, Fox, Bear, Parrot, Dolphin, Dragon, Robot, Alien, Child, Teen, Elder.\n\n"
        "Mindset examples: Optimistic, Cynical, Anxious, Curious, Nostalgic, Angry, Pragmatic, "
        "Idealistic, Overwhelmed, Joyful, Suspicious, Empathetic, Detached, Revolutionary, "
        "Conservative, Dreamy, Analytical, Impulsive, Stoic, Rebellious, Wistful, Paranoid, Zen.\n\n"
        'Respond with ONLY a JSON object, no other text:\n{"gender": "...", "age": <number>, "mindset": "..."}'
    )

    try:
        response = client.messages.create(
            model=MODEL, max_tokens=120,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        m = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            return jsonify({
                "gender":  str(data.get("gender",  "Person")),
                "age":     int(data.get("age",     30)),
                "mindset": str(data.get("mindset", "Curious")),
            })
        return jsonify({"error": "Could not parse persona from response"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/think", methods=["POST"])
def think():
    body            = request.get_json(force=True, silent=True) or {}
    question        = body.get("question",        "").strip()
    round_num       = int(body.get("round",       1))
    personas        = body.get("personas",        [])
    transcript      = body.get("transcript",      [])
    cross_pollinate = body.get("cross_pollinate", True)

    def generate():
        try:
            round_so_far = []  # (persona_id, label, text) for cross-pollination

            for p in personas:
                pid     = p.get('id', '')
                gender  = p.get('gender', '')
                age     = p.get('age',    30)
                mindset = p.get('mindset', '')
                label   = persona_label(gender, age, mindset)

                prev_pts_list = [t["text"] for t in transcript if t.get("persona_id") == pid]
                prev_pts = "\n\n---\n\n".join(prev_pts_list) if prev_pts_list else None

                peer_pts = None
                if cross_pollinate and round_so_far:
                    peer_pts = "\n\n".join(
                        f"{pl}: {text}" for _, pl, text in round_so_far
                    )

                prompt = persona_prompt(question, round_num, gender, age, mindset,
                                        prev_pts, peer_pts)
                sys    = persona_system(gender, age, mindset)

                yield f"data: {json.dumps({'type':'turn_start','persona_id':pid,'round':round_num})}\n\n"

                full_text = ""
                try:
                    with client.messages.stream(
                        model=MODEL, max_tokens=350,
                        system=sys,
                        messages=[{"role": "user", "content": prompt}],
                    ) as stream:
                        for text in stream.text_stream:
                            full_text += text
                            yield f"data: {json.dumps({'type':'chunk','persona_id':pid,'text':text})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"
                    return

                yield f"data: {json.dumps({'type':'turn_end','persona_id':pid,'round':round_num,'text':full_text})}\n\n"
                round_so_far.append((pid, label, full_text))

            yield f"data: {json.dumps({'type':'round_done','round':round_num})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/judge_personas", methods=["POST"])
def judge_personas():
    body            = request.get_json(force=True, silent=True) or {}
    question        = body.get("question",   "").strip()
    transcript_data = body.get("transcript", [])
    active_personas = body.get("personas",   [])
    prompt          = build_judge_prompt(question, transcript_data, active_personas)

    def generate():
        try:
            full_text = ""
            with client.messages.stream(
                model=MODEL, max_tokens=1200,
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    yield f"data: {json.dumps({'type':'chunk','text':text})}\n\n"

            # Extract MVP persona ID from last line
            mvp_id = ''
            valid_ids = {p['id'] for p in active_personas}
            for line in reversed(full_text.strip().splitlines()):
                clean = line.strip().lower()
                if clean.startswith('mvp:'):
                    candidate = clean[4:].strip()
                    if candidate in valid_ids:
                        mvp_id = candidate
                    break

            lines = full_text.strip().splitlines()
            if lines and lines[-1].strip().lower().startswith('mvp:'):
                display_text = '\n'.join(lines[:-1]).strip()
            else:
                display_text = full_text.strip()

            yield f"data: {json.dumps({'type':'mvp','persona_id':mvp_id,'display':display_text})}\n\n"
            yield f"data: {json.dumps({'type':'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/generate_pdf', methods=['POST'])
def generate_pdf_route():
    body = request.get_json(force=True, silent=True) or {}
    try:
        pdf = build_pdf_bytes(
            body.get('question', ''), body.get('rounds', 0),
            body.get('personas', []), body.get('mvpId', ''),
            body.get('verdictText', ''), body.get('transcript', [])
        )
        fname = re.sub(r'[^\w\s-]', '', body.get('question', 'session'))[:40].strip() + '.pdf'
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except ImportError:
        return jsonify({'error': 'reportlab not installed — run: pip install reportlab'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/gdrive/status')
def gdrive_status():
    cfg = _gdrive_config()
    if not cfg:
        return jsonify({'configured': False, 'authenticated': False})
    return jsonify({'configured': True, 'authenticated': _load_gdrive_creds() is not None})

@app.route('/gdrive/auth')
def gdrive_auth():
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return 'google-auth-oauthlib not installed', 500
    cfg = _gdrive_config()
    if not cfg:
        return 'Google Drive not configured', 400
    flow = Flow.from_client_config(cfg, scopes=GDRIVE_SCOPES,
                                    redirect_uri=_gdrive_redirect_uri())
    auth_url, state = flow.authorization_url(access_type='offline', prompt='consent')
    session['gdrive_state'] = state
    return redirect(auth_url)

@app.route('/gdrive/callback')
def gdrive_callback():
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return 'google-auth-oauthlib not installed', 500
    cfg = _gdrive_config()
    if not cfg:
        return 'Not configured', 400
    flow = Flow.from_client_config(cfg, scopes=GDRIVE_SCOPES,
                                    redirect_uri=_gdrive_redirect_uri(),
                                    state=session.get('gdrive_state'))
    flow.fetch_token(authorization_response=request.url)
    with open(GDRIVE_TOKEN_FILE, 'w') as f:
        f.write(flow.credentials.to_json())
    return (
        '<html><body>'
        '<script>if(window.opener){window.opener.postMessage("gdrive_ok","*");window.close();}</script>'
        '<p style="font-family:sans-serif;padding:20px">&#10003; Authenticated! You can close this window.</p>'
        '</body></html>'
    )

@app.route('/gdrive/upload', methods=['POST'])
def gdrive_upload():
    body = request.get_json(force=True, silent=True) or {}
    try:
        from googleapiclient.discovery import build as gbuild
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError:
        return jsonify({'error': 'Run: pip install google-api-python-client google-auth-oauthlib'}), 500
    creds = _load_gdrive_creds()
    if not creds:
        return jsonify({'error': 'Not authenticated with Google Drive', 'need_auth': True}), 401
    try:
        pdf = build_pdf_bytes(
            body.get('question', ''), body.get('rounds', 0),
            body.get('personas', []), body.get('mvpId', ''),
            body.get('verdictText', ''), body.get('transcript', [])
        )
        fname   = re.sub(r'[^\w\s-]', '', body.get('question', 'session'))[:40].strip() + '.pdf'
        service = gbuild('drive', 'v3', credentials=creds)
        meta    = {'name': fname, 'mimeType': 'application/pdf'}
        media   = MediaIoBaseUpload(BytesIO(pdf), mimetype='application/pdf')
        f       = service.files().create(body=meta, media_body=media,
                                         fields='id,webViewLink').execute()
        return jsonify({'ok': True, 'link': f.get('webViewLink', ''), 'name': fname})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/send_email", methods=["POST"])
def send_email():
    body        = request.get_json(force=True, silent=True) or {}
    to_email    = body.get("to_email",    "").strip()
    question    = body.get("question",    "").strip()
    rounds      = body.get("rounds",      0)
    mvp_id      = body.get("mvpId",       "")
    verdict_txt = body.get("verdictText", "")
    transcript  = body.get("transcript",  [])
    personas    = body.get("personas",    [])

    if not to_email:
        return jsonify({"error": "No recipient email provided"}), 400

    smtp_user = body.get("smtp_user", "").strip() or os.environ.get("SMTP_USER", "")
    smtp_pass = body.get("smtp_pass", "").strip() or os.environ.get("SMTP_PASS", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not smtp_user or not smtp_pass:
        return jsonify({"error": "Please enter your Gmail address and App Password."}), 400

    mvp_persona = next((p for p in personas if p['id'] == mvp_id), None)
    mvp_label   = (
        f"{persona_avatar(mvp_persona['gender'], mvp_persona['age'])} "
        f"{persona_label(mvp_persona['gender'], mvp_persona['age'], mvp_persona['mindset'])}"
        if mvp_persona else "N/A"
    )

    PERSONA_COLORS_EMAIL = {
        'p1': '#a78bfa', 'p2': '#22d3ee', 'p3': '#fb923c',
        'p4': '#f472b6', 'p5': '#818cf8', 'p6': '#a3e635',
    }

    def esc(s):
        return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br>")

    trans_lines = []
    for p in personas:
        pid   = p['id']
        turns = [t for t in transcript if t.get("persona_id") == pid]
        if turns:
            label = persona_label(p['gender'], p['age'], p['mindset'])
            trans_lines.append(f"\n=== {persona_avatar(p['gender'], p['age'])} {label} ===")
            for t in turns:
                trans_lines.append(f"  Round {t['round']}: {t['text']}")

    plain_body = (
        f"PERSONA ARENA — SESSION SUMMARY\n"
        f"© 2026 BGAD Consulting · bgadconsulting.com\n\n"
        f"Topic:  {question}\n"
        f"Rounds: {rounds}\n"
        f"Most Insightful Voice: {mvp_label}\n\n"
        f"{'─'*50}\nSYNTHESIS\n{'─'*50}\n{verdict_txt}\n\n"
        f"{'─'*50}\nFULL TRANSCRIPT\n{'─'*50}\n" + "\n".join(trans_lines) + "\n\n"
        f"Verify all cited sources independently.\n"
        f"Sent by Persona Arena · bgadconsulting.com"
    )

    trans_html = []
    for p in personas:
        pid   = p['id']
        turns = [t for t in transcript if t.get("persona_id") == pid]
        if turns:
            color  = PERSONA_COLORS_EMAIL.get(pid, '#888')
            avatar = persona_avatar(p['gender'], p['age'])
            label  = persona_label(p['gender'], p['age'], p['mindset'])
            trans_html.append(
                f'<h3 style="color:{color};margin:16px 0 8px">{avatar}&nbsp;{label}</h3>'
            )
            for t in turns:
                trans_html.append(
                    f'<div style="margin-bottom:8px;padding:10px 14px;background:#111124;'
                    f'border-radius:7px;border-left:3px solid {color}">'
                    f'<div style="font-size:.68rem;color:{color};font-weight:700;margin-bottom:4px">'
                    f'Round {t["round"]}</div>'
                    f'<div style="font-size:.85rem;color:#c8c8d8">{esc(t["text"])}</div></div>'
                )

    mvp_color = PERSONA_COLORS_EMAIL.get(mvp_id, '#888')
    r_label   = f"{rounds} round{'s' if rounds != 1 else ''}"
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="background:#0d0d1a;color:#e0e0e0;font-family:Segoe UI,Arial,sans-serif;margin:0;padding:20px">
<div style="max-width:660px;margin:0 auto">
  <div style="background:linear-gradient(135deg,#12122a,#1a1a3a);padding:16px 22px;
              border-radius:12px 12px 0 0;border:1px solid #2a2a4a;
              display:flex;align-items:center;justify-content:space-between">
    <div>
      <div style="font-size:1.05rem;font-weight:800;color:#e0e0e0">Persona Arena</div>
      <div style="font-size:.68rem;color:#555;margin-top:2px">Session Summary</div>
    </div>
    <a href="https://www.bgadconsulting.com" style="color:#60a5fa;font-size:.72rem;text-decoration:none">
      © 2026 BGAD Consulting</a>
  </div>
  <div style="background:#13131f;padding:20px 22px;border:1px solid #2a2a4a;border-top:none;
              border-radius:0 0 12px 12px">
    <p style="font-size:.72rem;color:#555;margin:0 0 4px">TOPIC</p>
    <h2 style="color:#e0e0e0;font-size:1rem;margin:0 0 14px">{esc(question)}</h2>
    <div style="margin-bottom:18px">
      <span style="background:#1e1e30;color:#888;padding:4px 12px;border-radius:9px;
                   font-size:.72rem;margin-right:10px">{r_label}</span>
      <span style="color:{mvp_color};font-weight:800;font-size:.85rem">
        Most Insightful Voice: {esc(mvp_label)}</span>
    </div>
    <div style="background:#0f0f1e;border:1px solid #2a2a40;border-radius:9px;
                padding:16px;margin-bottom:20px">
      <p style="font-size:.68rem;font-weight:800;letter-spacing:2px;color:#fbbf24;
                text-transform:uppercase;margin:0 0 10px">Synthesis</p>
      <div style="font-size:.85rem;color:#c8c8d8;line-height:1.65">{esc(verdict_txt)}</div>
    </div>
    <p style="font-size:.68rem;font-weight:800;letter-spacing:2px;color:#93c5fd;
              text-transform:uppercase;margin:0 0 10px">Session Transcript</p>
    {''.join(trans_html)}
    <p style="font-size:.65rem;color:#3a3a5a;margin-top:16px;font-style:italic">
      Verify all cited sources independently.
      Sent by Persona Arena ·
      <a href="https://www.bgadconsulting.com" style="color:#60a5fa">bgadconsulting.com</a></p>
  </div>
</div>
</body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Persona Arena: {question[:75]}"
        msg["From"]    = smtp_user
        msg["To"]      = to_email
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body,  "html"))
        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.ehlo(); srv.starttls(); srv.login(smtp_user, smtp_pass)
            srv.sendmail(smtp_user, to_email, msg.as_string())
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ── HTML Template ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Persona Arena</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: #0d0d1a; color: #e0e0e0;
    height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}

/* ── Persona CSS variables ── */
[data-persona="p1"] { --hc:#c4b5fd; --hbg:#08001a; --ha:#a78bfa; --hbd:#3b1f8a; --hhdr:#0d0022; }
[data-persona="p2"] { --hc:#67e8f9; --hbg:#001418; --ha:#22d3ee; --hbd:#0e5f6e; --hhdr:#001c22; }
[data-persona="p3"] { --hc:#fdba74; --hbg:#180800; --ha:#fb923c; --hbd:#7c2d12; --hhdr:#200a00; }
[data-persona="p4"] { --hc:#f9a8d4; --hbg:#180010; --ha:#f472b6; --hbd:#831843; --hhdr:#220018; }
[data-persona="p5"] { --hc:#a5b4fc; --hbg:#05081a; --ha:#818cf8; --hbd:#1e1b4b; --hhdr:#080c22; }
[data-persona="p6"] { --hc:#bef264; --hbg:#081400; --ha:#a3e635; --hbd:#365314; --hhdr:#0c1a00; }

/* ── Header ── */
header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 18px; background: #0d0d1a;
    border-bottom: 1px solid #1a1a2e; flex-shrink: 0;
}
.logo     { font-size: 1.05rem; font-weight: 800; color: #e0e0e0; letter-spacing: -0.3px; }
.logo-sub { font-size: 0.62rem; color: #555; margin-top: 1px; }
.model-badge {
    font-size: 0.62rem; color: #4a8adc; background: #0d1a2e;
    padding: 2px 9px; border-radius: 9px; border: 1px solid #1a3a5c;
}
.brand-link { font-size: 0.7rem; color: #4a8adc; text-decoration: none; opacity: 0.7; transition: opacity 0.15s; }
.brand-link:hover { opacity: 1; }
.brand-logo { height: 28px; width: auto; opacity: 0.9; }

/* ── Persona setup ── */
.persona-setup {
    padding: 10px 14px 8px; background: #0a0a18;
    border-bottom: 1px solid #1a1a2e; flex-shrink: 0;
}
.setup-hint {
    font-size: 0.65rem; color: #555; margin-bottom: 8px; text-align: center;
}
.persona-grid {
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px;
}
@media (max-width: 900px) {
    .persona-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 600px) {
    .persona-grid { grid-template-columns: repeat(2, 1fr); }
}

/* ── Persona card ── */
.persona-card {
    border: 2px solid var(--hbd, #2a2a40); border-radius: 10px;
    background: var(--hbg, #111); padding: 8px;
    opacity: 0.45; transition: all 0.18s; cursor: default;
}
.persona-card.on { opacity: 1; }
.persona-card.locked .pfield-input { pointer-events: none; opacity: 0.7; }
.pcard-top {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 6px;
}
.pcard-avatar { font-size: 1.5rem; line-height: 1; }
.pcard-toggle-btn {
    font-size: 0.58rem; font-weight: 800; letter-spacing: 0.5px;
    padding: 2px 7px; border-radius: 5px; cursor: pointer;
    background: #2a2a40; color: #555; user-select: none;
    border: 1px solid #3a3a50; transition: all 0.15s;
}
.persona-card.on .pcard-toggle-btn {
    background: var(--ha, #4a8adc); color: #0a0a18;
}
.pcard-recreate-btn {
    font-size: 0.58rem; font-weight: 700; letter-spacing: 0.3px;
    padding: 3px 8px; border-radius: 5px; cursor: pointer;
    background: #1a1a30; color: #666; user-select: none;
    border: 1px solid #2a2a40; transition: all 0.15s; width: 100%; text-align: center;
    margin-top: 5px; display: block; font-family: inherit;
}
.pcard-recreate-btn:hover:not(:disabled) { background: #252540; color: #a78bfa; border-color: #a78bfa; }
.pcard-recreate-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.pfield { margin-bottom: 5px; }
.pfield:last-child { margin-bottom: 0; }
.pfield-label {
    font-size: 0.55rem; color: #555; text-transform: uppercase;
    letter-spacing: 0.5px; display: block; margin-bottom: 2px;
}
.pfield-input {
    width: 100%; padding: 4px 7px; border-radius: 6px;
    border: 1px solid #2a2a40; background: #111128; color: var(--hc, #e0e0e0);
    font-size: 0.75rem; outline: none; transition: border-color 0.15s;
}
.pfield-input:focus { border-color: var(--ha, #4a8adc); }
.pfield-input[type="number"] { -moz-appearance: textfield; }
.pfield-input[type="number"]::-webkit-inner-spin-button,
.pfield-input[type="number"]::-webkit-outer-spin-button { -webkit-appearance: none; }

/* ── Options bar ── */
.options-bar {
    display: flex; align-items: center; gap: 8px; padding: 5px 14px;
    background: #0d0d1a; border-bottom: 1px solid #1a1a2e; flex-shrink: 0;
}
.cross-toggle {
    display: flex; align-items: center; gap: 8px; cursor: pointer;
    padding: 5px 11px; border-radius: 10px; border: 2px solid #2a2a40;
    background: #111128; transition: all 0.18s; user-select: none;
    font-size: 0.7rem; color: #555; white-space: nowrap;
}
.cross-toggle:hover { border-color: #4a5568; color: #888; }
.cross-toggle.on { border-color: #f59e0b; background: #1a1400; color: #fde68a; }
.cross-pill {
    font-size: 0.58rem; font-weight: 800; letter-spacing: 0.5px;
    padding: 2px 6px; border-radius: 4px;
    background: #2a2a40; color: #444; transition: all 0.18s;
}
.cross-toggle.on .cross-pill { background: #f59e0b; color: #1a0f00; }

/* ── Input bar ── */
.input-bar {
    padding: 8px 16px; border-bottom: 1px solid #1a1a2e;
    background: #0d0d1a; flex-shrink: 0;
}
.input-row { display: flex; gap: 8px; max-width: 960px; margin: 0 auto; align-items: center; }
.input-row input[type="text"] {
    flex: 1; padding: 9px 14px; border-radius: 10px;
    border: 1px solid #2a2a40; background: #111128; color: #e0e0e0;
    font-size: 0.88rem; outline: none;
}
.input-row input[type="text"]:focus { border-color: #4a8adc; }
.rapid-rounds-input {
    width: 52px; padding: 6px 8px; border-radius: 8px;
    border: 1px solid #3a3a5c; background: #1a1a2e; color: #e0e0e0;
    font-size: 0.85rem; text-align: center;
}
.rapid-rounds-input:focus { outline: none; border-color: #6d28d9; }
.rapid-rounds-label { font-size: 0.78rem; color: #888; white-space: nowrap; }

/* ── Buttons ── */
.btn {
    padding: 8px 16px; border-radius: 10px; border: none; cursor: pointer;
    font-size: 0.8rem; font-weight: 600; transition: all 0.15s; white-space: nowrap;
}
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.btn-primary { background: linear-gradient(135deg,#1a3a6a,#2563eb); color: #bfdbfe; }
.btn-rapid   { background: linear-gradient(135deg,#4c1d95,#6d28d9); color: #ddd6fe; }
.btn-next    { background: linear-gradient(135deg,#064e3b,#059669); color: #a7f3d0; }
.btn-judge   { background: linear-gradient(135deg,#78350f,#d97706); color: #fef3c7; }
.btn-reset   { background: #1e1e30; color: #a0a0b0; border: 1px solid #2a2a40; }
.btn-rejudge { background: linear-gradient(135deg,#1e1b4b,#4338ca); color: #c7d2fe; }
.btn-email   { background: linear-gradient(135deg,#1a3a2a,#15803d); color: #bbf7d0; }
.btn-hist    { background: #1e1e30; color: #a0aec0; border: 1px solid #2a2a40; font-size: 0.78rem; }
.btn-pdf     { background: linear-gradient(135deg,#1a1a3a,#7c3aed); color: #ddd6fe; }
.btn-donate  { background: linear-gradient(135deg,#4a0a1a,#be123c); color: #fda4af;
               text-decoration: none; display: inline-flex; align-items: center; }
.hidden { display: none !important; }
.rapid-disabled { opacity: 0.35 !important; pointer-events: none !important; }

/* ── PDF / Drive modal ── */
.pdf-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 200;
}
.pdf-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 14px;
    width: min(420px, 95vw); box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.pdf-hdr   { padding: 16px 20px 8px; border-bottom: 1px solid #1a1a3a; }
.pdf-title { font-size: 0.9rem; font-weight: 700; color: #e0e0e0; }
.pdf-desc  { font-size: 0.72rem; color: #555; margin-top: 3px; }
.pdf-body  { padding: 16px 20px; display: flex; flex-direction: column; gap: 10px; }
.pdf-option {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px; border-radius: 10px; background: #0d0d1a;
    border: 1px solid #2a2a40;
}
.pdf-opt-info { display: flex; flex-direction: column; gap: 2px; }
.pdf-opt-name { font-size: 0.85rem; font-weight: 600; color: #e0e0e0; }
.pdf-opt-desc { font-size: 0.68rem; color: #666; }
.pdf-status   { font-size: 0.72rem; min-height: 1.2em; padding: 0 4px; }
.pdf-status.ok  { color: #4ade80; }
.pdf-status.err { color: #f87171; }
.pdf-status.inf { color: #60a5fa; }
.pdf-footer { padding: 10px 20px; border-top: 1px solid #1a1a3a; display: flex; justify-content: flex-end; }
.drive-auth-row { font-size: 0.68rem; color: #888; display: flex; align-items: center; gap: 6px; }
.drive-auth-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }

/* ── Controls bar ── */
.controls-bar {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    padding: 5px 14px; background: #0a0a16; border-bottom: 1px solid #1a1a2e;
    flex-shrink: 0;
}
.ctrl-question { font-size: 0.75rem; color: #888; flex: 1; min-width: 0;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctrl-question strong { color: #c0c0d8; }
.rapid-badge {
    font-size: 0.6rem; font-weight: 800; letter-spacing: 1px; padding: 2px 9px;
    border-radius: 9px; background: #2d1a4a; color: #c084fc; text-transform: uppercase;
    flex-shrink: 0; white-space: nowrap;
}
.ctrl-status { font-size: 0.72rem; padding: 2px 10px; border-radius: 8px; white-space: nowrap; flex-shrink: 0; }
.ctrl-status.idle { background: #1a1a2e; color: #6060a0; }
.ctrl-status.done { background: #0f2a0f; color: #4ade80; }
.ctrl-status.p1 { background: #0d0022; color: #c4b5fd; }
.ctrl-status.p2 { background: #001c22; color: #67e8f9; }
.ctrl-status.p3 { background: #200a00; color: #fdba74; }
.ctrl-status.p4 { background: #220018; color: #f9a8d4; }
.ctrl-status.p5 { background: #080c22; color: #a5b4fc; }
.ctrl-status.p6 { background: #0c1a00; color: #bef264; }
.progress-wrap {
    height: 4px; flex: 1; min-width: 60px; background: #1a1a2e;
    border-radius: 2px; overflow: hidden;
}
.progress-fill { height: 100%; background: linear-gradient(90deg,#2563eb,#10b981); transition: width 0.3s; }
.round-counter { font-size: 0.72rem; color: #4a5568; white-space: nowrap; }

/* ── Arena ── */
.arena {
    flex: 1; display: grid; gap: 8px; padding: 8px;
    overflow-y: auto; align-content: start; min-height: 0;
}
.panel {
    display: flex; flex-direction: column;
    background: var(--hbg, #111); border: 1px solid var(--hbd, #2a2a40);
    border-radius: 12px; overflow: hidden;
    transition: box-shadow 0.25s; min-height: 180px;
}
.panel.speaking {
    box-shadow: 0 0 0 2px var(--ha), 0 4px 24px color-mix(in srgb, var(--ha) 25%, transparent);
}
.panel-header {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 13px; background: var(--hhdr, var(--hbg));
    border-bottom: 1px solid var(--hbd); flex-shrink: 0;
}
.panel-avatar { font-size: 1.35rem; flex-shrink: 0; }
.panel-label  { font-size: 0.78rem; font-weight: 700; color: var(--hc); }
.panel-desc   { font-size: 0.58rem; color: var(--ha); }
.panel-body   { flex: 1; overflow-y: auto; padding: 8px; }
.speaking-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--ha, #888); margin-left: auto;
    opacity: 0; transition: opacity 0.2s;
}
.panel.speaking .speaking-dot { opacity: 1; animation: pulse-dot 1s infinite; }
@keyframes pulse-dot {
    0%,100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.4; transform: scale(0.7); }
}
.placeholder { color: #3a3a5a; font-size: 0.78rem; font-style: italic; padding: 8px; text-align: center; }

/* ── Turn cards ── */
.turn-card {
    background: color-mix(in srgb, var(--ha, #4a8adc) 7%, #0d0d1a);
    border: 1px solid var(--hbd, #2a2a40); border-radius: 8px;
    padding: 9px 11px; margin-bottom: 6px; font-size: 0.82rem;
    line-height: 1.55; color: #c8c8d8;
}
.turn-card.active { border-color: var(--ha, #4a8adc); }
.card-header { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.round-tag {
    font-size: 0.6rem; font-weight: 800; color: var(--ha, #4a8adc);
    background: color-mix(in srgb, var(--ha, #4a8adc) 15%, transparent);
    padding: 1px 6px; border-radius: 5px;
}
.card-body a { color: var(--ha, #4a8adc); }
.card-waiting { display: flex; align-items: center; gap: 4px; color: #4a4a6a; font-size: 0.78rem; font-style: italic; }
.dot-anim { display: flex; gap: 2px; }
.dot-anim span { animation: bounce 1.2s infinite; font-style: normal; }
.dot-anim span:nth-child(2) { animation-delay: 0.2s; }
.dot-anim span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce { 0%,80%,100%{transform:translateY(0)} 40%{transform:translateY(-4px)} }
.error-msg { color: #f87171; font-size: 0.78rem; padding: 6px 10px; }
.round-sep {
    text-align: center; font-size: 0.6rem; color: #3a3a5a; letter-spacing: 2px;
    padding: 6px 0; text-transform: uppercase;
}

/* ── Verdict overlay ── */
.verdict-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 100;
}
.verdict-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 16px;
    width: min(680px, 95vw); max-height: 88vh; display: flex; flex-direction: column;
    box-shadow: 0 24px 80px rgba(0,0,0,0.7);
}
.verdict-header { padding: 18px 22px 12px; border-bottom: 1px solid #1a1a3a; flex-shrink: 0; }
.verdict-title { font-size: 0.65rem; font-weight: 800; letter-spacing: 2px; color: #4a5568; text-transform: uppercase; margin-bottom: 10px; }
.mvp-card {
    display: flex; align-items: center; gap: 14px; padding: 12px 16px;
    border-radius: 10px; background: var(--hbg, #0d0d1a); border: 2px solid var(--ha, #888);
    transition: all 0.3s;
}
.mvp-card.pending { background: #0d0d1a; border-color: #2a2a40; }
.mvp-emoji-large { font-size: 2rem; }
.mvp-name  { font-size: 0.95rem; font-weight: 800; color: var(--hc, #e0e0e0); }
.mvp-desc  { font-size: 0.65rem; color: var(--ha, #888); }
.mvp-crown { font-size: 0.6rem; font-weight: 800; letter-spacing: 1px; color: #fbbf24; text-transform: uppercase; margin-left: auto; }
.verdict-body { flex: 1; overflow-y: auto; padding: 16px 22px; font-size: 0.85rem; line-height: 1.65; color: #c0c0d8; }
.verdict-body a { color: #60a5fa; }
.verdict-body p  { margin-bottom: 1em; }
.verdict-body h1,.verdict-body h2,.verdict-body h3 { margin-top:1.2em; margin-bottom:0.5em; color:#e0e0e0; }
.verdict-body ul,.verdict-body ol { margin: 0.5em 0 1em 1.4em; }
.verdict-body li { margin-bottom: 0.4em; }
.verdict-body strong { color: #e8e8f0; }
.verdict-footer { padding: 12px 22px; border-top: 1px solid #1a1a3a; display: flex; gap: 8px; flex-shrink: 0; flex-wrap: wrap; }

/* ── Email modal ── */
.email-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 200;
}
.email-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 14px;
    width: min(420px, 95vw); box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.email-hdr   { padding: 16px 20px 8px; border-bottom: 1px solid #1a1a3a; }
.email-title { font-size: 0.9rem; font-weight: 700; color: #e0e0e0; }
.email-desc  { font-size: 0.72rem; color: #555; margin-top: 3px; }
.email-body  { padding: 14px 20px; display: flex; flex-direction: column; gap: 10px; }
.email-label { font-size: 0.72rem; color: #888; margin-bottom: -4px; }
.email-input-field {
    padding: 8px 12px; border-radius: 8px; border: 1px solid #2a2a40;
    background: #0d0d1a; color: #e0e0e0; font-size: 0.85rem; width: 100%; outline: none;
}
.email-input-field:focus { border-color: #4a8adc; }
.email-remember { display: flex; align-items: center; gap: 8px; font-size: 0.75rem; color: #888; cursor: pointer; }
.email-status { font-size: 0.75rem; min-height: 1.2em; }
.email-status.ok  { color: #4ade80; }
.email-status.err { color: #f87171; }
.email-footer { padding: 10px 20px; border-top: 1px solid #1a1a3a; display: flex; justify-content: flex-end; gap: 8px; }

/* ── History modal ── */
.history-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 200;
}
.history-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 14px;
    width: min(620px, 95vw); max-height: 85vh; display: flex; flex-direction: column;
    box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.history-hdr {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px; border-bottom: 1px solid #1a1a3a; flex-shrink: 0;
}
.history-title { font-size: 0.9rem; font-weight: 700; color: #e0e0e0; }
.history-body  { flex: 1; overflow-y: auto; padding: 12px 20px; }
.history-empty { color: #3a3a5a; font-size: 0.82rem; font-style: italic; text-align: center; padding: 20px; }
.hist-entry { background: #0d0d1a; border: 1px solid #1a1a2e; border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
.hist-header {
    display: flex; align-items: flex-start; justify-content: space-between;
    padding: 10px 14px; gap: 10px; cursor: pointer; transition: background 0.15s;
}
.hist-header:hover { background: #111128; }
.hist-q    { font-size: 0.82rem; color: #c0c0d8; font-weight: 600; flex: 1; }
.hist-meta { font-size: 0.65rem; color: #555; flex-shrink: 0; text-align: right; }
.hist-mvp  { font-size: 0.68rem; font-weight: 700; padding: 1px 8px; border-radius: 6px; margin-top: 3px; }
.hist-body { padding: 0 14px 12px; display: none; }
.hist-body.open { display: block; }
.hist-verdict { font-size: 0.78rem; color: #a0a0b8; line-height: 1.55; }
.hist-actions { display: flex; gap: 6px; margin-top: 8px; }
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;gap:13px;">
    <a href="https://www.bgadconsulting.com" target="_blank" rel="noopener noreferrer">
      <img src="/logo" alt="BGAD Consulting" class="brand-logo" onerror="this.style.display='none'" />
    </a>
    <div>
      <div class="logo">Persona Arena</div>
      <div class="logo-sub">Six AI voices &bull; customisable perspectives &bull; structured synthesis</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:12px;">
    <a href="https://www.bgadconsulting.com" target="_blank" rel="noopener noreferrer" class="brand-link">&copy; 2026 BGAD Consulting</a>
    <button class="btn btn-hist hidden" id="historyBtn" onclick="showHistory()">History</button>
    <a class="btn btn-donate" href="https://buy.stripe.com/aFa14n8pf2Gf4oO7OTgbm02"
       target="_blank" rel="noopener noreferrer">&#9829; Donate</a>
    <div class="model-badge">claude-opus-4-6</div>
  </div>
</header>

<!-- Persona setup -->
<div class="persona-setup" id="personaSetup">
  <div class="setup-hint">Define your personas — edit any field before starting. Click a card to toggle it on / off.</div>
  <div class="persona-grid" id="personaGrid"></div>
</div>

<!-- Options bar -->
<div class="options-bar" id="optionsBar">
  <div class="cross-toggle on" id="crossToggle" onclick="toggleCross()">
    🔗 Personas listen to each other
    <span class="cross-pill" id="crossPill">ON</span>
  </div>
</div>

<!-- Input bar -->
<div class="input-bar">
  <div class="input-row">
    <input type="text" id="question"
           placeholder="Enter a topic or question — e.g. 'Should we move to a four-day work week?'" />
    <button class="btn btn-primary" id="startBtn"    onclick="startSession(false)">Start</button>
    <button class="btn btn-reset   hidden" id="restartBtn" onclick="restartSession()">Restart</button>
    <button class="btn btn-rapid"  id="rapidBtn"   onclick="startSession(true)">Auto</button>
    <input  type="number" id="rapidRoundsInput" class="rapid-rounds-input" value="3" min="1" max="20" title="Rounds for Auto mode" />
    <label  class="rapid-rounds-label" for="rapidRoundsInput">rounds</label>
  </div>
</div>

<!-- Controls bar -->
<div class="controls-bar hidden" id="controlsBar">
  <div class="ctrl-question" id="ctrlQuestion"></div>
  <span class="rapid-badge hidden" id="rapidBadge">Auto</span>
  <div class="ctrl-status idle" id="ctrlStatus">Ready</div>
  <div class="progress-wrap"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  <div class="round-counter" id="roundCounter">Round 0</div>
  <button class="btn btn-next  hidden" id="nextBtn"  onclick="nextRound()">Next Round</button>
  <button class="btn btn-judge hidden" id="judgeBtn" onclick="stopAndSynthesize()">Synthesise</button>
  <button class="btn btn-reset hidden" id="resetBtn" onclick="resetSession()">New Session</button>
</div>

<!-- Arena -->
<div class="arena hidden" id="arena"></div>

<!-- Verdict overlay -->
<div class="verdict-overlay hidden" id="verdictOverlay">
  <div class="verdict-modal">
    <div class="verdict-header">
      <div class="verdict-title">Session Synthesis</div>
      <div class="mvp-card pending" id="mvpCard" data-persona="">
        <span class="mvp-emoji-large" id="mvpEmoji">🤔</span>
        <div>
          <div class="mvp-name" id="mvpName">Deliberating…</div>
          <div class="mvp-desc" id="mvpDesc"></div>
        </div>
        <div class="mvp-crown hidden" id="mvpCrown">Most Insightful Voice</div>
      </div>
    </div>
    <div class="verdict-body" id="verdictBody">
      <div class="card-waiting">Analysing the session
        <div class="dot-anim"><span>.</span><span>.</span><span>.</span></div>
      </div>
    </div>
    <div class="verdict-footer">
      <button class="btn btn-rejudge" onclick="reJudge()">Re-Synthesise</button>
      <button class="btn btn-email"   onclick="showEmailModal(-1)">Email</button>
      <button class="btn btn-pdf"     onclick="showPdfModal(-1)">PDF / Drive</button>
      <button class="btn btn-hist"    onclick="showHistory()">History</button>
      <button class="btn btn-reset"   onclick="resetSession()">New Session</button>
      <a class="btn btn-donate" href="https://buy.stripe.com/aFa14n8pf2Gf4oO7OTgbm02"
         target="_blank" rel="noopener noreferrer">&#9829; Donate</a>
    </div>
  </div>
</div>

<!-- PDF / Drive modal -->
<div class="pdf-overlay hidden" id="pdfOverlay">
  <div class="pdf-modal">
    <div class="pdf-hdr">
      <div class="pdf-title">PDF &amp; Google Drive</div>
      <div class="pdf-desc" id="pdfDesc"></div>
    </div>
    <div class="pdf-body">
      <div class="pdf-option">
        <div class="pdf-opt-info">
          <div class="pdf-opt-name">⬇ Download PDF</div>
          <div class="pdf-opt-desc">Save the session as a formatted PDF file</div>
        </div>
        <button class="btn btn-pdf" id="pdfDownloadBtn" onclick="downloadPdf()" style="font-size:0.75rem;padding:6px 14px">Download</button>
      </div>
      <div class="pdf-option">
        <div class="pdf-opt-info">
          <div class="pdf-opt-name">&#9729; Upload to Google Drive</div>
          <div class="pdf-opt-desc">Generate PDF and save directly to your Drive</div>
          <div class="drive-auth-row" id="driveAuthRow">
            <div class="drive-auth-dot" id="driveAuthDot" style="background:#555"></div>
            <span id="driveAuthLabel">Checking…</span>
            <a href="#" id="driveAuthLink" onclick="connectDrive();return false;"
               style="color:#60a5fa;font-size:0.68rem;display:none">Connect Google Drive</a>
          </div>
        </div>
        <button class="btn btn-pdf" id="pdfDriveBtn" onclick="uploadToDrive()" style="font-size:0.75rem;padding:6px 14px" disabled>Upload</button>
      </div>
      <div class="pdf-status" id="pdfStatus"></div>
    </div>
    <div class="pdf-footer">
      <button class="btn btn-reset" onclick="hidePdfModal()">Close</button>
    </div>
  </div>
</div>

<!-- Email modal -->
<div class="email-overlay hidden" id="emailOverlay">
  <div class="email-modal">
    <div class="email-hdr">
      <div class="email-title">Send Session by Email</div>
      <div class="email-desc" id="emailDesc"></div>
    </div>
    <div class="email-body">
      <div class="email-label">From (Gmail address)</div>
      <input class="email-input-field" type="email" id="emailFrom" placeholder="you@gmail.com" />
      <div class="email-label">App Password <span style="font-size:0.65rem;color:#555">(myaccount.google.com → Security → App Passwords)</span></div>
      <input class="email-input-field" type="password" id="emailAppPass" placeholder="xxxx xxxx xxxx xxxx" />
      <div class="email-label">Send to</div>
      <input class="email-input-field" type="email" id="emailTo" placeholder="recipient@example.com"
             onkeypress="if(event.key==='Enter') doSendEmail()" />
      <label class="email-remember">
        <input type="checkbox" id="emailRemember" checked />
        Remember sender credentials
      </label>
      <div class="email-status" id="emailStatus"></div>
    </div>
    <div class="email-footer">
      <button class="btn btn-reset" onclick="hideEmailModal()">Cancel</button>
      <button class="btn btn-email" id="emailSendBtn" onclick="doSendEmail()">Send</button>
    </div>
  </div>
</div>

<!-- History modal -->
<div class="history-overlay hidden" id="historyOverlay">
  <div class="history-modal">
    <div class="history-hdr">
      <div class="history-title">Session History</div>
      <button class="btn btn-reset" style="padding:5px 14px;font-size:0.75rem" onclick="hideHistory()">Close</button>
    </div>
    <div class="history-body" id="historyBody"></div>
  </div>
</div>

<script>
// ── marked config ─────────────────────────────────────────────────────────────
const renderer = new marked.Renderer();
const _link = renderer.link.bind(renderer);
renderer.link = (href, title, text) =>
    _link(href, title, text).replace('<a ', '<a target="_blank" rel="noopener noreferrer" ');
marked.setOptions({ renderer, breaks: true, gfm: true });

// ── Persona color palette ─────────────────────────────────────────────────────
const PERSONA_COLORS = {
    p1: { color:'#c4b5fd', accent:'#a78bfa' },
    p2: { color:'#67e8f9', accent:'#22d3ee' },
    p3: { color:'#fdba74', accent:'#fb923c' },
    p4: { color:'#f9a8d4', accent:'#f472b6' },
    p5: { color:'#a5b4fc', accent:'#818cf8' },
    p6: { color:'#bef264', accent:'#a3e635' },
};

// ── State ─────────────────────────────────────────────────────────────────────
const PERSONA_IDS = ['p1','p2','p3','p4','p5','p6'];

// personas[] mirrors the live card state
let personas = {{ defaults | safe }};
let activePersonas  = [];    // filtered personas (on=true) when session starts
let question        = '';
let crossPollinate  = true;
let currentRound    = 0;
let roundInProgress = false;
let judging         = false;
let rapidMode       = false;
let rapidRounds     = 3;
let abortCtrl       = null;
let judgeVer        = 0;
let transcript      = [];
let currentMvpId    = '';
let currentVerdictText = '';
let emailTargetIdx  = -1;
let sessionHistory  = [];

// per-persona streaming state
let currentPid  = null;
let currentCard = null;
let currentText = '';

// which personas are toggled ON in setup
const personaOn = { p1:true, p2:true, p3:true, p4:true, p5:true, p6:true };

// ── Avatar helper ─────────────────────────────────────────────────────────────
function personaAvatar(gender, age) {
    const g = String(gender).toLowerCase().trim();
    const a = parseInt(age) || 30;
    if (g.includes('cat'))    return '🐱';
    if (g.includes('dog'))    return '🐶';
    if (g.includes('non') || g === 'nb' || g.includes('enby'))
        return a >= 65 ? '🧓' : (a < 13 ? '🧒' : '🧑');
    if (g.includes('woman') || g.includes('female') || g.includes('girl'))
        return a >= 65 ? '👵' : (a < 13 ? '👧' : '👩');
    if (g.includes('man')  || g.includes('male')   || g.includes('boy'))
        return a >= 65 ? '👴' : (a < 13 ? '👦' : '👨');
    return '🧑';
}

function personaLabel(p) {
    return `${p.gender}, ${p.age}, ${p.mindset}`;
}

// ── Diversity tables ──────────────────────────────────────────────────────────
const GENDER_OPTIONS = [
    'Man', 'Woman', 'Non-binary',
    'Teen Boy', 'Teen Girl', 'Elder Man', 'Elder Woman', 'Child',
    'Cat', 'Dog', 'Bird', 'Horse', 'Rabbit', 'Fox', 'Bear', 'Parrot',
    'Dolphin', 'Elephant', 'Wolf', 'Owl',
    'Robot', 'Alien',
];
const ANIMAL_GENDERS   = new Set(['Cat','Dog','Bird','Horse','Rabbit','Fox','Bear','Parrot','Dolphin','Elephant','Wolf','Owl']);
const NONBINARY_GENDERS = new Set(['Non-binary']);

// [min, max] age buckets — one per slot to force spread
const AGE_BUCKETS = [
    [2,  9],   // very young
    [10, 17],  // teen
    [18, 28],  // young adult
    [29, 40],  // adult
    [41, 55],  // middle-aged
    [56, 70],  // mature
    [71, 90],  // senior
];

const MINDSET_OPTIONS = [
    'Optimistic', 'Cynical', 'Anxious', 'Curious', 'Nostalgic', 'Angry',
    'Pragmatic', 'Idealistic', 'Overwhelmed', 'Joyful', 'Suspicious',
    'Empathetic', 'Detached', 'Revolutionary', 'Conservative', 'Dreamy',
    'Analytical', 'Impulsive', 'Stoic', 'Rebellious', 'Wistful', 'Zen',
    'Excited', 'Determined', 'Playful', 'Scared', 'Creative', 'Disappointed',
    'Hopeful', 'Restless', 'Melancholic', 'Fiery', 'Gentle', 'Stubborn',
    'Bewildered', 'Serene', 'Frantic', 'Tender', 'Bitter', 'Mischievous',
];

function _pick(pool, usedValues) {
    const unused = pool.filter(v => !usedValues.includes(v));
    const source  = unused.length > 0 ? unused : pool;
    return source[Math.floor(Math.random() * source.length)];
}

function _pickGender(otherGenders) {
    const animalCount    = otherGenders.filter(g => ANIMAL_GENDERS.has(g)).length;
    const nonbinaryCount = otherGenders.filter(g => NONBINARY_GENDERS.has(g)).length;
    let pool = [...GENDER_OPTIONS];
    if (animalCount    >= 2) pool = pool.filter(g => !ANIMAL_GENDERS.has(g));
    if (nonbinaryCount >= 1) pool = pool.filter(g => !NONBINARY_GENDERS.has(g));
    const unused = pool.filter(g => !otherGenders.includes(g));
    const source  = unused.length > 0 ? unused : pool;
    return source[Math.floor(Math.random() * source.length)];
}

function _ageBucket(age) {
    for (const [lo, hi] of AGE_BUCKETS) {
        if (age >= lo && age <= hi) return `${lo}-${hi}`;
    }
    return 'other';
}

function _pickAge(usedAges) {
    const usedBuckets = usedAges.map(_ageBucket);
    const unusedBuckets = AGE_BUCKETS.filter(([lo, hi]) =>
        !usedBuckets.includes(`${lo}-${hi}`)
    );
    const [lo, hi] = unusedBuckets.length > 0
        ? unusedBuckets[Math.floor(Math.random() * unusedBuckets.length)]
        : AGE_BUCKETS[Math.floor(Math.random() * AGE_BUCKETS.length)];
    return lo + Math.floor(Math.random() * (hi - lo + 1));
}

function generateDiversePersona(targetPid) {
    // Read current live values for all OTHER personas
    const others = PERSONA_IDS
        .filter(id => id !== targetPid)
        .map(id => ({
            gender:  (document.getElementById('gender-'  + id)?.value || '').trim(),
            age:     parseInt(document.getElementById('age-' + id)?.value) || 30,
            mindset: (document.getElementById('mindset-' + id)?.value || '').trim(),
        }));

    const usedGenders  = others.map(p => p.gender);
    const usedAges     = others.map(p => p.age);
    const usedMindsets = others.map(p => p.mindset);

    return {
        gender:  _pickGender(usedGenders),
        age:     _pickAge(usedAges),
        mindset: _pick(MINDSET_OPTIONS, usedMindsets),
    };
}

// ── Persona setup UI ──────────────────────────────────────────────────────────
function buildPersonaSetup() {
    const grid = document.getElementById('personaGrid');
    grid.innerHTML = '';
    PERSONA_IDS.forEach(pid => {
        const p = personas.find(x => x.id === pid);
        const avatar = personaAvatar(p.gender, p.age);
        const on = personaOn[pid];
        const card = document.createElement('div');
        card.className = 'persona-card' + (on ? ' on' : '');
        card.id = 'pcard-' + pid;
        card.dataset.persona = pid;
        card.innerHTML = `
          <div class="pcard-top">
            <span class="pcard-avatar" id="avatar-${pid}">${avatar}</span>
            <span class="pcard-toggle-btn" onclick="togglePersona('${pid}')"
                  id="ptoggle-${pid}">${on ? 'ON' : 'OFF'}</span>
          </div>
          <div class="pfield">
            <label class="pfield-label">Gender</label>
            <input class="pfield-input" id="gender-${pid}" value="${escHtml(p.gender)}"
                   oninput="updateAvatar('${pid}')" />
          </div>
          <div class="pfield">
            <label class="pfield-label">Age</label>
            <input class="pfield-input" id="age-${pid}" type="number" value="${p.age}" min="0" max="200"
                   oninput="updateAvatar('${pid}')" />
          </div>
          <div class="pfield">
            <label class="pfield-label">Mindset</label>
            <input class="pfield-input" id="mindset-${pid}" value="${escHtml(p.mindset)}" />
          </div>
          <button class="pcard-recreate-btn" id="recreate-${pid}"
                  type="button">↺ Recreate</button>`;
        grid.appendChild(card);

        // Attach event listener after element is in the DOM
        document.getElementById('recreate-' + pid)
            .addEventListener('click', () => recreatePersona(pid));
    });
}

function updateAvatar(pid) {
    const gender = document.getElementById('gender-' + pid)?.value || '';
    const age    = document.getElementById('age-'    + pid)?.value || 30;
    const el     = document.getElementById('avatar-' + pid);
    if (el) el.textContent = personaAvatar(gender, age);
}

function recreatePersona(pid) {
    const btn = document.getElementById('recreate-' + pid);
    if (!btn || btn.disabled) return;

    const p = generateDiversePersona(pid);

    document.getElementById('gender-'  + pid).value = p.gender;
    document.getElementById('age-'     + pid).value = p.age;
    document.getElementById('mindset-' + pid).value = p.mindset;
    updateAvatar(pid);

    btn.textContent = '✓ Done';
    setTimeout(() => { btn.textContent = '↺ Recreate'; }, 900);
}

function togglePersona(pid) {
    if (roundInProgress || judging) return;
    personaOn[pid] = !personaOn[pid];
    const card = document.getElementById('pcard-' + pid);
    const btn  = document.getElementById('ptoggle-' + pid);
    if (card) card.classList.toggle('on', personaOn[pid]);
    if (btn)  btn.textContent = personaOn[pid] ? 'ON' : 'OFF';
}

function readPersonasFromSetup() {
    return PERSONA_IDS
        .filter(pid => personaOn[pid])
        .map(pid => ({
            id:      pid,
            gender:  (document.getElementById('gender-'  + pid)?.value  || '').trim(),
            age:     parseInt(document.getElementById('age-' + pid)?.value || 30),
            mindset: (document.getElementById('mindset-' + pid)?.value || '').trim(),
        }));
}

function lockSetup(locked) {
    PERSONA_IDS.forEach(pid => {
        const card = document.getElementById('pcard-' + pid);
        if (card) card.classList.toggle('locked', locked);
        ['gender-','age-','mindset-'].forEach(prefix => {
            const inp = document.getElementById(prefix + pid);
            if (inp) inp.disabled = locked;
        });
        const toggleBtn = document.getElementById('ptoggle-'   + pid);
        if (toggleBtn) toggleBtn.style.pointerEvents = locked ? 'none' : '';
        const recreateBtn = document.getElementById('recreate-' + pid);
        if (recreateBtn) {
            recreateBtn.style.display = locked ? 'none' : '';
            if (!locked) recreateBtn.disabled = false;
        }
    });
    const ct = document.getElementById('crossToggle');
    if (ct) ct.style.pointerEvents = locked ? 'none' : '';
}

// ── Cross-pollinate ───────────────────────────────────────────────────────────
function toggleCross() {
    if (document.getElementById('crossToggle').style.pointerEvents === 'none') return;
    crossPollinate = !crossPollinate;
    document.getElementById('crossToggle').classList.toggle('on', crossPollinate);
    document.getElementById('crossPill').textContent = crossPollinate ? 'ON' : 'OFF';
}

// ── Arena builder ─────────────────────────────────────────────────────────────
function buildArena() {
    const arena = document.getElementById('arena');
    arena.innerHTML = '';
    const n    = activePersonas.length;
    const cols = n <= 3 ? n : n === 4 ? 2 : 3;
    arena.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;

    activePersonas.forEach(p => {
        const avatar = personaAvatar(p.gender, p.age);
        const label  = personaLabel(p);
        const panel  = document.createElement('div');
        panel.className  = 'panel';
        panel.id         = 'panel-' + p.id;
        panel.dataset.persona = p.id;
        panel.innerHTML  =
            '<div class="panel-header">'
          + '<span class="panel-avatar">' + avatar + '</span>'
          + '<div><div class="panel-label">' + escHtml(p.gender) + ', ' + p.age + '</div>'
          + '<div class="panel-desc">' + escHtml(p.mindset) + ' mindset</div></div>'
          + '<div class="speaking-dot"></div>'
          + '</div>'
          + '<div class="panel-body" id="body-' + p.id + '">'
          + '<div class="placeholder">Perspectives will appear here…</div>'
          + '</div>';
        arena.appendChild(panel);
    });
}

// ── Session flow ──────────────────────────────────────────────────────────────
function startSession(rapid) {
    question = document.getElementById('question').value.trim();
    if (!question) { alert('Please enter a topic or question!'); return; }

    activePersonas = readPersonasFromSetup();
    if (activePersonas.length < 2) { alert('Please enable at least 2 personas.'); return; }

    // Randomise order once — retained for all subsequent rounds
    for (let i = activePersonas.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [activePersonas[i], activePersonas[j]] = [activePersonas[j], activePersonas[i]];
    }

    rapidMode       = !!rapid;
    rapidRounds     = rapidMode
        ? Math.max(1, parseInt(document.getElementById('rapidRoundsInput').value) || 3)
        : 3;
    roundInProgress = false;
    currentRound    = 0;
    transcript      = [];
    judging         = false;
    currentMvpId    = '';
    currentVerdictText = '';

    buildArena();
    lockSetup(true);

    document.getElementById('personaSetup').classList.add('hidden');
    document.getElementById('optionsBar').classList.add('hidden');
    document.getElementById('verdictOverlay').classList.add('hidden');
    document.getElementById('arena').classList.remove('hidden');
    document.getElementById('ctrlQuestion').innerHTML =
        'Topic: <strong>' + escHtml(question) + '</strong>';
    document.getElementById('controlsBar').classList.remove('hidden');

    setBtn('startBtn', false, 'Running…');
    setBtn('rapidBtn', false, 'Auto');
    show('restartBtn');
    hide('nextBtn'); hide('resetBtn');
    document.getElementById('nextBtn').textContent = 'Next Round';

    if (rapidMode) {
        document.getElementById('rapidBadge').textContent = 'Auto \u2022 ' + rapidRounds + ' rounds';
        show('rapidBadge');
    } else {
        hide('rapidBadge');
    }

    show('judgeBtn');
    setStatus('idle', 'Starting…');
    nextRound();
}

function nextRound() {
    if (judging || roundInProgress) return;
    roundInProgress = true;
    hide('nextBtn'); hide('resetBtn');
    show('judgeBtn');

    currentRound++;
    currentPid  = null;
    currentCard = null;
    currentText = '';

    if (currentRound > 1) {
        activePersonas.forEach(p => {
            const body = document.getElementById('body-' + p.id);
            if (body) {
                const sep = document.createElement('div');
                sep.className = 'round-sep';
                sep.textContent = '— Round ' + currentRound + ' —';
                body.appendChild(sep);
            }
        });
    }

    setRoundCounter();
    setStatus('idle', 'Round ' + currentRound + ' — starting…');
    setProgress(0, activePersonas.length);

    if (abortCtrl) abortCtrl.abort();
    abortCtrl = new AbortController();

    fetch('/think', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            question, round: currentRound,
            personas: activePersonas,
            transcript, cross_pollinate: crossPollinate
        }),
        signal: abortCtrl.signal,
    }).then(resp => {
        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        function read() {
            reader.read().then(({ done, value }) => {
                if (done) return;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    let d;
                    try { d = JSON.parse(line.slice(6)); } catch { continue; }
                    handleThinkEvent(d);
                }
                read();
            }).catch(err => {
                if (err.name !== 'AbortError' && !judging) {
                    setStatus('idle', 'Connection error');
                    roundInProgress = false;
                    show('resetBtn');
                }
            });
        }
        read();
    }).catch(err => {
        if (err.name !== 'AbortError' && !judging) {
            setStatus('idle', 'Connection error');
            roundInProgress = false;
            show('resetBtn');
        }
    });
}

function stopAndSynthesize() {
    if (judging) return;
    judging         = true;
    roundInProgress = false;
    finaliseCurrentCard();
    cleanup();
    hide('judgeBtn'); hide('nextBtn');
    show('resetBtn');
    setBtn('startBtn', true, 'Start');
    hide('restartBtn');
    setBtn('rapidBtn', true, 'Auto');

    if (transcript.length === 0) {
        alert('No points yet — let at least one round complete first.');
        judging = false;
        show('judgeBtn');
        setBtn('startBtn', false, 'Running…');
        setBtn('rapidBtn', false, 'Auto');
        return;
    }
    runJudge();
}

function resetSession() {
    judging         = false;
    rapidMode       = false;
    roundInProgress = false;
    crossPollinate  = true;
    document.getElementById('crossToggle').classList.add('on');
    document.getElementById('crossPill').textContent = 'ON';
    cleanup();
    currentRound       = 0;
    transcript         = [];
    currentMvpId       = '';
    currentVerdictText = '';

    document.getElementById('verdictOverlay').classList.add('hidden');
    document.getElementById('controlsBar').classList.add('hidden');
    document.getElementById('arena').innerHTML = '';
    document.getElementById('arena').classList.add('hidden');
    document.getElementById('personaSetup').classList.remove('hidden');
    document.getElementById('optionsBar').classList.remove('hidden');
    setProgress(0, 1);
    setStatus('idle', 'Ready');
    hide('judgeBtn'); hide('nextBtn'); hide('resetBtn'); hide('rapidBadge');
    setBtn('startBtn', true, 'Start');
    hide('restartBtn');
    setBtn('rapidBtn', true, 'Auto');
    lockSetup(false);
    document.getElementById('question').focus();
}

function restartSession() {
    const savedQuestion = document.getElementById('question').value;
    resetSession();
    document.getElementById('question').value = savedQuestion;
    document.getElementById('question').focus();
    document.getElementById('question').select();
}

function cleanup() {
    if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; }
}

// ── Think events ──────────────────────────────────────────────────────────────
let personasDoneThisRound = 0;

function handleThinkEvent(data) {
    if (judging) return;
    switch (data.type) {

        case 'turn_start': {
            currentPid  = data.persona_id;
            currentText = '';
            const panel = document.getElementById('panel-' + data.persona_id);
            const body  = document.getElementById('body-'  + data.persona_id);
            if (!panel || !body) break;

            const ph = body.querySelector('.placeholder');
            if (ph) ph.remove();

            document.querySelectorAll('.panel').forEach(p => p.classList.remove('speaking'));
            panel.classList.add('speaking');

            const p = activePersonas.find(x => x.id === data.persona_id) || {};
            setStatus(data.persona_id,
                personaAvatar(p.gender, p.age) + ' ' + (p.gender || data.persona_id) + ' thinking…');

            const card = document.createElement('div');
            card.className = 'turn-card active';
            card.innerHTML =
                '<div class="card-header">'
              + '<span class="round-tag">R' + data.round + '</span>'
              + '</div>'
              + '<div class="card-body">'
              + '<div class="card-waiting">Thinking'
              + '<div class="dot-anim"><span>.</span><span>.</span><span>.</span></div>'
              + '</div></div>';
            body.appendChild(card);
            body.scrollTop = body.scrollHeight;
            currentCard = card;

            personasDoneThisRound = activePersonas.findIndex(x => x.id === data.persona_id);
            setProgress(personasDoneThisRound, activePersonas.length);
            break;
        }

        case 'chunk':
            if (!currentCard || judging) break;
            currentText += data.text;
            renderCard(currentCard, currentText, true);
            const body2 = document.getElementById('body-' + data.persona_id);
            if (body2) body2.scrollTop = body2.scrollHeight;
            break;

        case 'turn_end':
            if (currentCard) {
                renderCard(currentCard, currentText, false);
                currentCard.classList.remove('active');
                const p = activePersonas.find(x => x.id === data.persona_id) || {};
                transcript.push({
                    round: data.round,
                    persona_id: data.persona_id,
                    gender: p.gender, age: p.age, mindset: p.mindset,
                    text: currentText
                });
                currentCard = null;
                currentText = '';
            }
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('speaking'));
            personasDoneThisRound++;
            setProgress(personasDoneThisRound, activePersonas.length);
            break;

        case 'round_done':
            roundInProgress = false;
            cleanup();
            setProgress(activePersonas.length, activePersonas.length);
            if (rapidMode) {
                if (currentRound < rapidRounds) {
                    setStatus('idle', 'Round ' + currentRound + ' / ' + rapidRounds + ' done — next starting…');
                    setTimeout(nextRound, 1200);
                } else {
                    setStatus('done', 'All ' + rapidRounds + ' rounds done — synthesising…');
                    setTimeout(stopAndSynthesize, 900);
                }
            } else {
                setStatus('done', 'Round ' + currentRound + ' done');
                document.getElementById('nextBtn').textContent =
                    'Next Round (' + (currentRound + 1) + ')';
                show('nextBtn');
            }
            break;

        case 'error':
            activePersonas.forEach(p => {
                const b = document.getElementById('body-' + p.id);
                if (b) b.innerHTML += '<div class="error-msg">Error: ' + escHtml(data.message) + '</div>';
            });
            roundInProgress = false;
            cleanup();
            hide('judgeBtn'); show('resetBtn');
            setStatus('idle', 'Error');
            break;
    }
}

// ── Judge ─────────────────────────────────────────────────────────────────────
async function runJudge() {
    const myVer = ++judgeVer;
    setStatus('done', 'Synthesising…');
    document.getElementById('verdictOverlay').classList.remove('hidden');

    const mvpCard = document.getElementById('mvpCard');
    mvpCard.className   = 'mvp-card pending';
    mvpCard.dataset.persona = '';
    document.getElementById('mvpEmoji').textContent = '🤔';
    document.getElementById('mvpName').textContent  = 'Deliberating…';
    document.getElementById('mvpDesc').textContent  = '';
    document.getElementById('mvpCrown').classList.add('hidden');
    document.getElementById('verdictBody').innerHTML =
        '<div class="card-waiting">Synthesising'
      + '<div class="dot-anim"><span>.</span><span>.</span><span>.</span></div></div>';

    let judgeText  = '';
    let sseBuffer  = '';
    let firstChunk = true;

    try {
        const resp = await fetch('/judge_personas', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, transcript, personas: activePersonas }),
        });

        if (myVer !== judgeVer) return;

        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (myVer !== judgeVer) { reader.cancel(); return; }
            if (done) break;

            sseBuffer += decoder.decode(value, { stream: true });
            const lines = sseBuffer.split('\n');
            sseBuffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                let d;
                try { d = JSON.parse(line.slice(6)); } catch { continue; }

                if (d.type === 'chunk') {
                    if (firstChunk) {
                        document.getElementById('verdictBody').innerHTML = '';
                        firstChunk = false;
                    }
                    judgeText += d.text;
                    document.getElementById('verdictBody').innerHTML =
                        marked.parse(judgeText) + '<span class="cursor">▍</span>';

                } else if (d.type === 'mvp') {
                    currentMvpId = d.persona_id;
                    const mvpP   = activePersonas.find(x => x.id === d.persona_id);
                    const avatar = mvpP ? personaAvatar(mvpP.gender, mvpP.age) : '🏆';
                    const label  = mvpP ? personaLabel(mvpP) : 'Most Insightful';

                    mvpCard.dataset.persona = d.persona_id || '';
                    mvpCard.className       = d.persona_id ? 'mvp-card' : 'mvp-card pending';
                    // Apply color via inline style since persona_id may not be in CSS
                    const pc = PERSONA_COLORS[d.persona_id] || {};
                    mvpCard.style.borderColor = pc.accent || '#888';
                    mvpCard.style.background  = pc.accent
                        ? `color-mix(in srgb, ${pc.accent} 6%, #0d0d1a)` : '';

                    document.getElementById('mvpEmoji').textContent = avatar;
                    document.getElementById('mvpName').textContent  = label;
                    document.getElementById('mvpDesc').textContent  = mvpP ? mvpP.mindset + ' mindset' : '';
                    document.getElementById('mvpName').style.color  = pc.color || '#e0e0e0';
                    document.getElementById('mvpDesc').style.color  = pc.accent || '#888';
                    if (d.persona_id) document.getElementById('mvpCrown').classList.remove('hidden');

                    const displayText = d.display || judgeText;
                    document.getElementById('verdictBody').innerHTML = marked.parse(displayText);
                    currentVerdictText = displayText;

                } else if (d.type === 'done') {
                    sessionHistory.push({
                        question, rounds: currentRound,
                        personas: [...activePersonas],
                        mvpId: currentMvpId,
                        verdictText: currentVerdictText,
                        transcript: [...transcript],
                    });
                    show('historyBtn');

                } else if (d.type === 'error') {
                    document.getElementById('verdictBody').innerHTML =
                        '<div class="error-msg">Error: ' + escHtml(d.message) + '</div>';
                }
            }
        }
    } catch (err) {
        if (myVer !== judgeVer) return;
        document.getElementById('verdictBody').innerHTML =
            '<div class="error-msg">Error: ' + escHtml(err.message) + '</div>';
    }
}

function reJudge() { runJudge(); }

// ── PDF / Drive ───────────────────────────────────────────────────────────────
let pdfTargetIdx = -1;

function _pdfPayload(idx) {
    if (idx >= 0) {
        const e = sessionHistory[idx];
        return { question: e.question, rounds: e.rounds, personas: e.personas,
                 mvpId: e.mvpId, verdictText: e.verdictText, transcript: e.transcript };
    }
    return { question, rounds: currentRound, personas: activePersonas,
             mvpId: currentMvpId, verdictText: currentVerdictText,
             transcript: [...transcript] };
}

async function showPdfModal(idx) {
    pdfTargetIdx = idx;
    const q = idx >= 0 ? sessionHistory[idx].question : question;
    document.getElementById('pdfDesc').textContent = q.length > 65 ? q.slice(0,62)+'…' : q;
    document.getElementById('pdfStatus').textContent = '';
    document.getElementById('pdfStatus').className = 'pdf-status';
    setBtn('pdfDownloadBtn', true, 'Download');
    document.getElementById('pdfOverlay').classList.remove('hidden');
    await refreshDriveStatus();
}

function hidePdfModal() {
    document.getElementById('pdfOverlay').classList.add('hidden');
}

async function refreshDriveStatus() {
    const dot   = document.getElementById('driveAuthDot');
    const label = document.getElementById('driveAuthLabel');
    const link  = document.getElementById('driveAuthLink');
    const btn   = document.getElementById('pdfDriveBtn');
    label.textContent = 'Checking…'; dot.style.background = '#555';
    try {
        const r = await fetch('/gdrive/status');
        const d = await r.json();
        if (!d.configured) {
            dot.style.background = '#f59e0b';
            label.textContent = 'Not configured — see setup guide';
            link.style.display = 'none'; btn.disabled = true;
        } else if (d.authenticated) {
            dot.style.background = '#10b981';
            label.textContent = 'Connected to Google Drive';
            link.style.display = 'none'; btn.disabled = false;
        } else {
            dot.style.background = '#ef4444';
            label.textContent = 'Not connected — ';
            link.style.display = ''; btn.disabled = true;
        }
    } catch {
        label.textContent = 'Status check failed'; dot.style.background = '#555';
    }
}

function connectDrive() {
    const win = window.open('/gdrive/auth', '_blank', 'width=520,height=620');
    window.addEventListener('message', async function handler(e) {
        if (e.data === 'gdrive_ok') {
            window.removeEventListener('message', handler);
            if (win && !win.closed) win.close();
            await refreshDriveStatus();
        }
    });
}

async function downloadPdf() {
    const st = document.getElementById('pdfStatus');
    setBtn('pdfDownloadBtn', false, 'Generating…');
    st.textContent = ''; st.className = 'pdf-status';
    try {
        const resp = await fetch('/generate_pdf', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_pdfPayload(pdfTargetIdx)),
        });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            st.textContent = d.error || 'PDF generation failed.'; st.className = 'pdf-status err';
        } else {
            const blob = await resp.blob();
            const url  = URL.createObjectURL(blob);
            const a    = document.createElement('a');
            const cd   = resp.headers.get('Content-Disposition') || '';
            const m    = cd.match(/filename="(.+?)"/);
            a.href = url; a.download = m ? m[1] : 'session.pdf';
            document.body.appendChild(a); a.click();
            document.body.removeChild(a); URL.revokeObjectURL(url);
            st.textContent = '✓ PDF downloaded'; st.className = 'pdf-status ok';
        }
    } catch (err) {
        st.textContent = 'Error: ' + err.message; st.className = 'pdf-status err';
    }
    setBtn('pdfDownloadBtn', true, 'Download');
}

async function uploadToDrive() {
    const st = document.getElementById('pdfStatus');
    setBtn('pdfDriveBtn', false, 'Uploading…');
    st.textContent = 'Generating PDF…'; st.className = 'pdf-status inf';
    try {
        const resp = await fetch('/gdrive/upload', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_pdfPayload(pdfTargetIdx)),
        });
        const d = await resp.json();
        if (d.ok) {
            st.innerHTML = `✓ Uploaded: <a href="${d.link}" target="_blank" rel="noopener"
                style="color:#60a5fa">${d.name}</a>`;
            st.className = 'pdf-status ok';
        } else if (d.need_auth) {
            st.textContent = 'Not authenticated — connect Google Drive first.';
            st.className = 'pdf-status err';
            await refreshDriveStatus();
        } else {
            st.textContent = d.error || 'Upload failed.'; st.className = 'pdf-status err';
        }
    } catch (err) {
        st.textContent = 'Error: ' + err.message; st.className = 'pdf-status err';
    }
    setBtn('pdfDriveBtn', true, 'Upload');
}

// ── Email ─────────────────────────────────────────────────────────────────────
function showEmailModal(idx) {
    emailTargetIdx = idx;
    const q = idx >= 0 ? sessionHistory[idx].question : question;
    document.getElementById('emailDesc').textContent = q.length > 65 ? q.slice(0,62)+'…' : q;
    document.getElementById('emailFrom').value    = localStorage.getItem('persona_smtp_user') || '';
    document.getElementById('emailAppPass').value = localStorage.getItem('persona_smtp_pass') || '';
    document.getElementById('emailTo').value      = '';
    const st = document.getElementById('emailStatus');
    st.textContent = ''; st.className = 'email-status';
    setBtn('emailSendBtn', true, 'Send');
    document.getElementById('emailOverlay').classList.remove('hidden');
    setTimeout(() => {
        const f = document.getElementById('emailFrom').value ? 'emailTo' : 'emailFrom';
        document.getElementById(f).focus();
    }, 60);
}

function hideEmailModal() {
    document.getElementById('emailOverlay').classList.add('hidden');
}

async function doSendEmail() {
    const fromEmail = document.getElementById('emailFrom').value.trim();
    const appPass   = document.getElementById('emailAppPass').value.trim();
    const toEmail   = document.getElementById('emailTo').value.trim();
    const remember  = document.getElementById('emailRemember').checked;
    const st        = document.getElementById('emailStatus');

    if (!fromEmail || !appPass || !toEmail) {
        st.textContent = 'Please fill in all three fields.';
        st.className = 'email-status err'; return;
    }

    if (remember) {
        localStorage.setItem('persona_smtp_user', fromEmail);
        localStorage.setItem('persona_smtp_pass', appPass);
    } else {
        localStorage.removeItem('persona_smtp_user');
        localStorage.removeItem('persona_smtp_pass');
    }

    let payload;
    if (emailTargetIdx >= 0) {
        const e = sessionHistory[emailTargetIdx];
        payload = { smtp_user: fromEmail, smtp_pass: appPass, to_email: toEmail,
                    question: e.question, rounds: e.rounds, personas: e.personas,
                    mvpId: e.mvpId, verdictText: e.verdictText, transcript: e.transcript };
    } else {
        payload = { smtp_user: fromEmail, smtp_pass: appPass, to_email: toEmail,
                    question, rounds: currentRound, personas: activePersonas,
                    mvpId: currentMvpId, verdictText: currentVerdictText,
                    transcript: [...transcript] };
    }

    setBtn('emailSendBtn', false, 'Sending…');
    st.textContent = ''; st.className = 'email-status';
    try {
        const resp = await fetch('/send_email', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.ok) {
            st.textContent = '✓ Sent successfully!'; st.className = 'email-status ok';
            setBtn('emailSendBtn', true, 'Send');
            setTimeout(hideEmailModal, 1800);
        } else {
            st.textContent = data.error || 'Send failed.'; st.className = 'email-status err';
            setBtn('emailSendBtn', true, 'Send');
        }
    } catch (err) {
        st.textContent = 'Network error: ' + err.message; st.className = 'email-status err';
        setBtn('emailSendBtn', true, 'Send');
    }
}

// ── History ───────────────────────────────────────────────────────────────────
function showHistory() {
    const body = document.getElementById('historyBody');
    if (sessionHistory.length === 0) {
        body.innerHTML = '<div class="history-empty">No sessions yet.</div>';
    } else {
        body.innerHTML = sessionHistory.slice().reverse().map((e, ri) => {
            const idx    = sessionHistory.length - 1 - ri;
            const mvpP   = e.personas.find(x => x.id === e.mvpId);
            const color  = PERSONA_COLORS[e.mvpId]?.accent || '#888';
            const mvpAvatar = mvpP ? personaAvatar(mvpP.gender, mvpP.age) : '';
            const mvpLbl    = mvpP ? personaLabel(mvpP) : '';
            const icons  = e.personas.map(p => personaAvatar(p.gender, p.age)).join(' ');
            return `<div class="hist-entry">
                <div class="hist-header" onclick="toggleHistEntry(${idx})">
                  <div>
                    <div class="hist-q">${escHtml(e.question)}</div>
                    <div style="font-size:0.65rem;color:#555;margin-top:3px">
                      ${icons} &bull; ${e.rounds} round${e.rounds!==1?'s':''}
                    </div>
                    ${e.mvpId ? `<div class="hist-mvp" style="color:${color};background:color-mix(in srgb,${color} 15%,transparent)">${mvpAvatar} ${escHtml(mvpLbl)} — Most Insightful Voice</div>` : ''}
                  </div>
                </div>
                <div class="hist-body" id="hb-${idx}">
                  <div class="hist-verdict">${marked.parse(e.verdictText || '')}</div>
                  <div class="hist-actions">
                    <button class="btn btn-email" style="font-size:0.72rem;padding:5px 12px"
                            onclick="showEmailModal(${idx});hideHistory()">Email</button>
                    <button class="btn btn-pdf" style="font-size:0.72rem;padding:5px 12px"
                            onclick="showPdfModal(${idx});hideHistory()">PDF / Drive</button>
                  </div>
                </div>
              </div>`;
        }).join('');
    }
    document.getElementById('historyOverlay').classList.remove('hidden');
}

function hideHistory() {
    document.getElementById('historyOverlay').classList.add('hidden');
}

function toggleHistEntry(idx) {
    const el = document.getElementById('hb-' + idx);
    if (el) el.classList.toggle('open');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function finaliseCurrentCard() {
    if (currentCard) {
        renderCard(currentCard, currentText, false);
        currentCard.classList.remove('active');
        currentCard = null;
        currentText = '';
    }
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('speaking'));
}

function renderCard(card, text, streaming) {
    const body = card.querySelector('.card-body');
    if (!body) return;
    if (!text && streaming) return;
    const html = marked.parse(text || '');
    body.innerHTML = streaming ? html + '<span class="cursor">▍</span>' : html;
}

function setRoundCounter() {
    const label = rapidMode
        ? 'Round ' + currentRound + ' / ' + rapidRounds
        : 'Round ' + currentRound;
    document.getElementById('roundCounter').textContent = label;
}

function setProgress(done, total) {
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    document.getElementById('progressFill').style.width = pct + '%';
}

function setStatus(cls, text) {
    const el = document.getElementById('ctrlStatus');
    el.className = 'ctrl-status ' + cls;
    el.textContent = text;
}

function setBtn(id, enabled, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = !enabled;
    if (text) el.textContent = text;
}

function show(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hide(id) { document.getElementById(id)?.classList.add('hidden'); }

function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                    .replace(/"/g,'&quot;');
}

// ── Init ──────────────────────────────────────────────────────────────────────
buildPersonaSetup();
document.getElementById('question').focus();
</script>
</body>
</html>
"""

# ── Launch ─────────────────────────────────────────────────────────────────────

def _launch_chrome(url):
    time.sleep(1.5)
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in chrome_paths:
        if os.path.exists(exe):
            webbrowser.register("chrome", None, webbrowser.BackgroundBrowser(exe))
            webbrowser.get("chrome").open(url)
            return
    webbrowser.open(url)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5004))
    url  = f"http://localhost:{port}"
    print(f"[*] Starting Persona Arena on {url}")
    if not os.environ.get("PORT"):
        threading.Thread(target=_launch_chrome, args=(url,), daemon=True).start()
    app.run(debug=False, port=port, threaded=True)
