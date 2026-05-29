import os
import json
import time
import secrets
import threading
from datetime import datetime
from email.message import EmailMessage
import smtplib
import ssl
from google import genai
from google.genai import types
from flask import Flask, render_template_string, request, redirect, url_for, session, send_file, abort
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth
from werkzeug.utils import secure_filename

# --- CONFIGURATION ---
app = Flask(__name__) 

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///surfboard_ai.db'

# 🚀 FORCE 500MB UPLOAD LIMIT
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

# SECURITY: Cookie Settings
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False 

# --- 🔑 CREDENTIALS ---
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", "CHANGE ME"))
GOOGLE_CLIENT_ID = None
GOOGLE_CLIENT_SECRET = None
CREDENTIALS_PATH = "CHANGE ME"

try:
    with open(CREDENTIALS_PATH, 'r') as f:
        creds = json.load(f)
        data = creds.get('web', creds.get('installed', {}))
        GOOGLE_CLIENT_ID = data.get('client_id')
        GOOGLE_CLIENT_SECRET = data.get('client_secret')
        print(f"✅ Loaded Google Client ID: ...{GOOGLE_CLIENT_ID[-10:]}")
except Exception as e:
    print(f"❌ ERROR LOADING KEYS: {e}")
    GOOGLE_CLIENT_ID = "MISSING"

# --- ADMIN SETTINGS ---
SUPER_ADMIN_EMAIL = "CHANGE ME"

# --- EMAIL SETTINGS ---
EMAIL_SENDER = "CHANGE ME" 
EMAIL_PASSWORD = "CHANGE ME" 

db = SQLAlchemy(app)
oauth = OAuth(app)

google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
    authorize_params={'prompt': 'select_account'} 
)

# --- SECURITY MIDDLEWARE ---
@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get('_csrf_token')
        if not token or token != request.form.get('_csrf_token'):
            abort(403) 

def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']

app.jinja_env.globals['csrf_token'] = generate_csrf_token

@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.errorhandler(413)
def request_entity_too_large(error):
    return render_template_string(CSS + '''
        <div class="container" style="text-align:center;">
            <div style="font-size:4em;">🦕</div>
            <h1>File Too Big!</h1>
            <p>The limit is 500MB.</p>
            <a href="/dashboard" class="btn">Try a smaller file</a>
        </div>
    '''), 413

# --- DB MODEL ---
class Surfer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(100), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    height_cm = db.Column(db.Float, nullable=False)
    weight_kg = db.Column(db.Float, nullable=False)
    skill_level = db.Column(db.String(50), nullable=False) 
    is_pro = db.Column(db.Boolean, default=False) 
    bundle_used = db.Column(db.String(50), default="ai") 
    
    ai_rec_liters = db.Column(db.Float, nullable=True) 
    ai_rec_feet = db.Column(db.Integer, nullable=True)
    ai_rec_inches = db.Column(db.Float, nullable=True)
    ai_rec_message = db.Column(db.Text, nullable=True)
    
    video_motion_score = db.Column(db.Float, default=0.0) 
    videos = db.relationship('SurfVideo', backref='surfer', lazy=True)
    
    rec_liters = db.Column(db.Float, nullable=True) 
    rec_feet = db.Column(db.Integer, nullable=True)
    rec_inches = db.Column(db.Float, nullable=True)
    rec_message = db.Column(db.Text, nullable=True)

class SurfVideo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    surfer_id = db.Column(db.Integer, db.ForeignKey('surfer.id'), nullable=False)
    file_path = db.Column(db.String(255))
    motion_score = db.Column(db.Float)

class Inventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(100), unique=True, nullable=False)
    ai_bundles = db.Column(db.Integer, default=1)
    coach_bundles = db.Column(db.Integer, default=0)
    zoom_bundles = db.Column(db.Integer, default=0)

# NEW DB MODEL FOR ZOOM CHAT
class ZoomMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(100), nullable=False)
    sender = db.Column(db.String(10), nullable=False) # 'user' or 'admin'
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

def get_inventory(email):
    inv = Inventory.query.filter_by(user_email=email).first()
    if not inv:
        inv = Inventory(user_email=email)
        db.session.add(inv)
        db.session.commit()
    return inv

# --- LOGIC ---
def send_email(to_email, subject, body):
    if "your_google" in EMAIL_PASSWORD:
        print(f"\n[SIMULATION] Email to {to_email}\n{body}\n")
        return
    try:
        msg = EmailMessage()
        msg.set_content(body); msg['Subject'] = subject
        msg['From'] = EMAIL_SENDER; msg['To'] = to_email
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ctx) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD); s.send_message(msg)
    except: pass

def get_ai_prediction(weight, height, motion_score):
    return "Handled by Gemini Vision"

app.jinja_env.globals.update(get_ai_prediction=get_ai_prediction)

def analyze_surf_video_with_ai(filepath, weight_kg, height_cm, declared_skill, training_data):
    print(f"Uploading {filepath} to AI...")
    try:
        video_file = client.files.upload(file=filepath)
        
        while video_file.state.name == "PROCESSING":
            time.sleep(2)
            video_file = client.files.get(name=video_file.name)
            
        if video_file.state.name == "FAILED":
            return {"error": "Video processing failed on AI side."}

        prompt = f"""
        You are an expert surfboard shaper. Watch this video. 
        The user claims their skill level is '{declared_skill}'. They weigh {weight_kg}kg and are {height_cm}cm tall.
        
        First, verify if this is actually a video of someone surfing or attempting to surf in the water.
        If it is NOT surfing, set "is_surfing" false and leave the rest blank.
        
        If it IS surfing, calculate the ideal surfboard volume (Liters) and length (Feet and Inches).
        
        CRITICAL INSTRUCTIONS:
        Here are examples of how the head coach previously sized boards based on weight, skill, and technique. 
        Study these past decisions and mimic this exact logic and writing style in your assessment:
        {training_data}
        
        You MUST respond ONLY with a valid JSON object in this exact format:
        {{
            "is_surfing": true,
            "skill_assessment_text": "A paragraph explaining their technique, matching the head coach's style.",
            "rec_liters": 32.5,
            "rec_feet": 6,
            "rec_inches": 2
        }}
        """

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[video_file, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        client.files.delete(name=video_file.name)
        result_data = json.loads(response.text)
        return result_data

    except Exception as e:
        print(f"AI Analysis Error: {e}")
        return {"error": str(e)}

def process_videos_background(app_context, surfer_id, filepaths, weight, height, skill, email):
    with app_context:
        s = Surfer.query.get(surfer_id)
        inv = get_inventory(email)
        
        recent_pros = Surfer.query.filter(Surfer.rec_liters != None, Surfer.is_pro == True).order_by(Surfer.timestamp.desc()).limit(5).all()
        training_data = ""
        for r in recent_pros:
            training_data += f"- Surfer Stats: {r.weight_kg}kg, {r.height_cm}cm, {r.skill_level} level. You recommended: {r.rec_feet}'{r.rec_inches}\", {r.rec_liters}L. Your reasoning: {r.rec_message}\n"
        
        if not training_data:
            training_data = "No historical data yet. Use your best judgment."

        valid_videos = 0
        refunded_bundles = 0
        last_ai_result = None

        for filepath in filepaths:
            ai_result = analyze_surf_video_with_ai(filepath, weight, height, skill, training_data)
            
            if ai_result.get("error") or not ai_result.get("is_surfing", False):
                refunded_bundles += 1
                if os.path.exists(filepath):
                    os.remove(filepath)
            else:
                vid = SurfVideo(surfer_id=s.id, file_path=filepath, motion_score=100.0)
                db.session.add(vid)
                valid_videos += 1
                last_ai_result = ai_result
        
        # Safe refund logic
        if refunded_bundles > 0:
            if s.bundle_used == 'coach':
                inv.coach_bundles += refunded_bundles
            else:
                inv.ai_bundles += refunded_bundles
            
        if valid_videos > 0 and last_ai_result:
            lit = last_ai_result.get("rec_liters", 0)
            ft = last_ai_result.get("rec_feet", 0)
            inc = last_ai_result.get("rec_inches", 0)

            safe_sk = s.skill_level.replace('/', '-').replace(' ', '_')
            for vid in s.videos:
                old_path = vid.file_path
                ext = os.path.splitext(old_path)[1]
                new_name = f"{s.weight_kg}kg_{s.height_cm}cm_{safe_sk}_{ft}ft_{inc}in_{lit}L_{secrets.token_hex(4)}{ext}"
                new_path = os.path.join(app.config['UPLOAD_FOLDER'], new_name)
                try:
                    if os.path.exists(old_path):
                        os.rename(old_path, new_path)
                        vid.file_path = new_path
                except Exception as e:
                    print("Rename error:", e)

            if s.bundle_used == 'coach':
                s.ai_rec_liters = lit
                s.ai_rec_feet = ft
                s.ai_rec_inches = inc
                s.ai_rec_message = last_ai_result.get("skill_assessment_text")
            else:
                s.rec_liters = lit
                s.rec_feet = ft
                s.rec_inches = inc
                s.rec_message = last_ai_result.get("skill_assessment_text")
                
                msg_body = f"Hi!\nYour AI analysis is complete.\n\nRecommended Volume: {s.rec_liters}L\nLength: {s.rec_feet}'{s.rec_inches}\"\n\nNotes: {s.rec_message}\n\nSee your dashboard for more details."
                send_email(email, "Your Perfect Surfboard Dimensions!", msg_body)
            
        elif valid_videos == 0:
            db.session.delete(s)
            
        db.session.commit()
        print(f"Background analysis for {email} complete!")

# --- 🎨 CSS STYLES ---
CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;800&display=swap');
    :root { --bg-color: #0f172a; --card-bg: rgba(30, 41, 59, 0.6); --accent: #38bdf8; --text: #f1f5f9; --text-muted: #94a3b8; }
    
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: 'Poppins', sans-serif; background: linear-gradient(135deg, #020617 0%, #1e1b4b 100%); color: var(--text); margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; overflow-x: hidden; }
    body::before { content: ""; position: fixed; top: -50%; left: -50%; width: 200%; height: 200%; background: radial-gradient(circle, rgba(56, 189, 248, 0.05) 0%, transparent 60%); animation: float 20s infinite linear; z-index: -1; }
    @keyframes float { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    .container { width: 100%; max-width: 850px; padding: 40px 20px; box-sizing: border-box; animation: fadeIn 0.8s ease-out; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
    .card { background: var(--card-bg); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); padding: 35px; border-radius: 20px; margin-bottom: 25px; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.4); transition: transform 0.3s ease; }
    .card:hover { transform: translateY(-5px); box-shadow: 0 15px 50px rgba(56, 189, 248, 0.1); border-color: rgba(56, 189, 248, 0.3); }
    h1, h2, h3 { background: linear-gradient(90deg, #fff, #38bdf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 800; margin-top: 0; }
    input[type="number"], input[type="text"], select { width: 100%; padding: 16px; margin: 10px 0 25px; background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 12px; color: white; font-size: 16px; font-family: 'Poppins', sans-serif; box-sizing: border-box; }
    input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2); }
    label { font-size: 0.9em; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .btn { display: block; width: 100%; padding: 16px; background: linear-gradient(135deg, #0ea5e9, #3b82f6); color: white; border-radius: 12px; text-decoration: none; font-weight: 700; font-size: 1.1em; border: none; cursor: pointer; transition: all 0.3s ease; text-align: center; box-shadow: 0 4px 15px rgba(14, 165, 233, 0.3); }
    .btn:hover { background: linear-gradient(135deg, #38bdf8, #60a5fa); box-shadow: 0 8px 25px rgba(14, 165, 233, 0.5); transform: translateY(-2px); }
    .btn-secondary { background: rgba(255,255,255,0.1); box-shadow: none; width: auto; display: inline-block; padding: 10px 20px; font-size: 0.9em; }
    .btn-secondary:hover { background: rgba(255,255,255,0.2); }
    .g-btn { background: white; color: #333; display: flex; align-items: center; justify-content: center; gap: 15px; transition: 0.2s; }
    .g-btn:hover { background: #f8fafc; transform: scale(1.02); }
    .file-drop-zone { border: 2px dashed rgba(255, 255, 255, 0.2); border-radius: 16px; padding: 50px 20px; text-align: center; cursor: pointer; position: relative; transition: 0.3s; background: rgba(0,0,0,0.2); }
    .file-drop-zone:hover { border-color: var(--accent); background: rgba(56, 189, 248, 0.05); }
    .file-drop-zone input { position: absolute; top: 0; left: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
    .skill-container { display: grid; gap: 15px; margin-bottom: 25px; }
    .skill-option { display: none; }
    .skill-card { display: flex; align-items: center; gap: 15px; background: rgba(15, 23, 42, 0.5); border: 1px solid rgba(255, 255, 255, 0.1); padding: 20px; border-radius: 12px; cursor: pointer; transition: 0.2s; }
    .skill-card:hover { background: rgba(255, 255, 255, 0.05); }
    .skill-option:checked + .skill-card { border-color: var(--accent); background: rgba(56, 189, 248, 0.1); box-shadow: 0 0 20px rgba(56, 189, 248, 0.15); }
    .status-badge { display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 0.75em; font-weight: 800; text-transform: uppercase; }
    .status-badge.ready { background: rgba(52, 211, 153, 0.2); color: #34d399; }
    .status-badge.pending { background: rgba(251, 191, 36, 0.2); color: #fbbf24; }
    footer { text-align: center; padding: 20px; color: var(--text-muted); font-size: 0.8em; margin-top: auto; }
    footer a { color: var(--accent); text-decoration: none; }
    
    .modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); backdrop-filter: blur(8px); display: none; justify-content: center; align-items: center; z-index: 1000; opacity: 0; transition: opacity 0.3s ease; }
    .modal-overlay.active { display: flex; opacity: 1; }
    .modal-box { background: var(--card-bg); border: 1px solid var(--accent); padding: 40px; border-radius: 20px; text-align: center; max-width: 450px; box-shadow: 0 15px 50px rgba(56, 189, 248, 0.2); transform: scale(0.9); transition: transform 0.3s ease; }
    .modal-overlay.active .modal-box { transform: scale(1); }
    .modal-buttons { display: flex; gap: 15px; margin-top: 30px; }
</style>
"""

# --- ROUTES ---

@app.route('/')
def home():
    if session.get('user'): return redirect('/dashboard')
    err_msg = ""
    if GOOGLE_CLIENT_ID == "MISSING":
        err_msg = "<div style='background:rgba(239, 68, 68, 0.2); border:1px solid #ef4444; color:#fca5a5; padding:15px; border-radius:10px; margin-bottom:20px;'>⚠️ <strong>System Error:</strong> Google Credentials JSON missing.</div>"

    return render_template_string(CSS + f'''
        <div class="container" style="display:flex; flex-direction:column; justify-content:center; height:85vh; text-align:center;">
            <div style="font-size:4em; margin-bottom:20px;">🏄‍♂️</div>
            <h1 style="font-size: 3.5em; margin-bottom: 10px;">Surfing AI</h1>
            <p style="font-size: 1.2em; color: var(--text-muted); margin-bottom: 40px; max-width:600px; margin-left:auto; margin-right:auto;">
                Advanced computer vision analysis for your perfect surfboard dimensions.
            </p>
            {err_msg}
            <div style="max-width:350px; margin:0 auto;">
                <a href="/login" class="btn g-btn">
                    <img src="https://www.svgrepo.com/show/475656/google-color.svg" width="24" height="24">
                    Continue with Google
                </a>
            </div>
        </div>
        <footer><a href="/privacy">Privacy Policy</a> • Secure Login via OAuth 2.0</footer>
    ''')

@app.route('/privacy')
def privacy():
    return render_template_string(CSS + '''
        <div class="container">
            <a href="/" style="color:var(--text-muted); text-decoration:none;">← Back</a>
            <h2 style="margin-top:20px;">Privacy Policy</h2>
            <div class="card">
                <h3>1. Data Collection</h3>
                <p>We collect your email, surfing videos, and biometric data (height/weight) solely for the purpose of analyzing your surfing technique and recommending equipment.</p>
                <h3>2. Security</h3>
                <p>All data is encrypted in transit using SSL. Your videos are stored securely and analyzed by our AI system. We implement strict CSRF protection and secure session management.</p>
                <h3>3. Usage</h3>
                <p>We do not share your personal data with third parties. Your video data is used exclusively for analysis and then deleted if invalid.</p>
                <h3>4. Your Rights</h3>
                <p>You may request deletion of your account and all associated data at any time by contacting support.</p>
            </div>
        </div>
    ''')

@app.route('/login')
def login():
    if GOOGLE_CLIENT_ID == "MISSING": return redirect('/')
    return google.authorize_redirect(url_for('authorize', _external=True))

@app.route('/authorize')
def authorize():
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo') or google.get('https://openidconnect.googleapis.com/v1/userinfo').json()
        session['user'] = user_info
        return redirect('/dashboard')
    except Exception as e: return f"Login Failed: {e}"

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

@app.route('/dashboard')
def dashboard():
    user = session.get('user')
    if not user: return redirect('/')
    if user['email'].lower().strip() == SUPER_ADMIN_EMAIL.lower().strip(): return redirect('/admin')
    
    inv = get_inventory(user['email'])
    history = Surfer.query.filter_by(user_email=user['email']).order_by(Surfer.timestamp.desc()).all()
    zoom_messages = ZoomMessage.query.filter_by(user_email=user['email']).order_by(ZoomMessage.timestamp.asc()).all()
    
    return render_template_string(CSS + '''
        <div class="container">
            <div class="header" style="display:flex; justify-content:space-between; align-items:center; margin-bottom:30px; border-bottom:1px solid rgba(255,255,255,0.1); padding-bottom:20px;">
                <h2>My Dashboard</h2>
                <div style="display:flex; align-items:center; gap:15px;">
                    <a href="/shop" style="background:rgba(56, 189, 248, 0.1); padding:8px 15px; border-radius:50px; border:1px solid var(--accent); color:white; font-weight:bold; text-decoration:none;">
                        🛒 My Bundles ({{ inv.ai_bundles }} AI | {{ inv.coach_bundles }} Pro | {{ inv.zoom_bundles }} Zoom)
                    </a>
                    <a href="/logout" class="btn btn-secondary">Logout</a>
                </div>
            </div>

            {% if inv.zoom_bundles > 0 %}
                <div class="card" style="border: 1px solid #34d399; background: rgba(52, 211, 153, 0.05); text-align: left;">
                    <h3 style="color:#34d399; margin-bottom: 10px;">📹 VIP Zoom Consultation Active</h3>
                    <p style="color:var(--text-muted); font-size: 0.95em; line-height: 1.5; margin-bottom: 15px;">
                        You have {{ inv.zoom_bundles }} Zoom bundle(s) available! <strong>You will receive an email personally with time ranges for your time zone to choose when to do it.</strong> Please keep an eye on your inbox. You can also chat directly with the coaching team below to discuss availability or ask questions beforehand.
                    </p>
                    <div style="background: rgba(0,0,0,0.3); padding: 15px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.05);">
                        <label style="color: var(--accent); margin-bottom: 10px; display: block;">Direct Chat (Coach Team)</label>
                        
                        <div style="max-height: 200px; overflow-y: auto; margin-bottom: 15px;">
                            {% if not zoom_messages %}<div style="color:var(--text-muted); font-size:0.9em;">Send a message to start the conversation...</div>{% endif %}
                            {% for msg in zoom_messages %}
                                <div style="margin-bottom: 10px; text-align: {% if msg.sender == 'user' %}right{% else %}left{% endif %};">
                                    <span style="display:inline-block; padding: 8px 12px; border-radius: 12px; font-size: 0.9em; background: {% if msg.sender == 'user' %}#3b82f6; color: white;{% else %}#34d399; color: #0f172a;{% endif %}; max-width: 80%;">
                                        {{ msg.message }}
                                    </span>
                                </div>
                            {% endfor %}
                        </div>

                        <form action="/send_zoom_message" method="POST" style="display:flex; gap:10px;">
                            <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
                            <input type="text" name="message" required placeholder="Type a message..." style="flex:1; margin-bottom:0; padding:10px; border-radius:8px; background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255,255,255,0.1); color: white;">
                            <button class="btn btn-secondary" style="margin:0; background: #34d399; color: #0f172a; font-weight: bold; border:none;">Send</button>
                        </form>
                    </div>
                </div>
            {% endif %}

            <div class="card" style="text-align:center; border: 1px dashed var(--accent); background: rgba(56, 189, 248, 0.05);">
                <div style="font-size:3em; margin-bottom:10px;">🌊</div><h3 style="color:var(--accent);">Ready for a new board?</h3>
                <p style="color:var(--text-muted); margin-bottom:25px;">Upload a new video to get updated stats.</p>
                <div style="max-width:300px; margin:0 auto;"><a href="/new_analysis" class="btn">Start New Analysis</a></div>
            </div>
            
            <h3 style="margin-top:50px; margin-bottom:20px;">Recent Sessions</h3>
            {% if not surfs %}<div style="text-align:center; padding:40px; color:var(--text-muted); background:rgba(255,255,255,0.02); border-radius:15px;">No history found. Start your first analysis above!</div>{% endif %}
            
            {% for s in surfs %}
                <div class="card" style="border-left: 4px solid {% if s.rec_liters %}#34d399{% else %}#fbbf24{% endif %};">
                    <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:15px;">
                        <div><div style="font-weight:bold; font-size:1.1em; color:white;">{{ s.timestamp.strftime('%B %d, %Y') }}</div><div style="font-size:0.9em; color:var(--text-muted);">{{ s.skill_level }} • {{ s.height_cm }}cm • {{ s.weight_kg }}kg</div></div>
                        {% if s.rec_liters %}<span class="status-badge ready">Complete</span>{% else %}<span class="status-badge pending">Processing</span>{% endif %}
                    </div>
                    {% if s.rec_message %}
                        {% if s.bundle_used == 'coach' %}
                            <div style="margin-top:15px; background:linear-gradient(135deg, rgba(168, 85, 247, 0.1), rgba(0,0,0,0.4)); border: 1px solid #a855f7; padding:20px; border-radius:12px; box-shadow: 0 4px 20px rgba(168, 85, 247, 0.15);">
                                <div style="display:flex; align-items:center; gap:8px; color:#c084fc; font-weight:900; margin-bottom:10px; text-transform:uppercase; letter-spacing: 1px; font-size: 0.85em;">
                                    💎 Pro Expert Recommendation
                                </div>
                                <div style="font-size:1.5em; font-weight:bold; color:white; margin-bottom:12px;">
                                    {{ s.rec_feet }}'{{ s.rec_inches }}" | {{ s.rec_liters }}L
                                </div>
                                <label style="color:#c084fc;">Expert Notes</label>
                                <p style="font-size:0.95em; line-height:1.6; color:#f1f5f9; margin-bottom:0; white-space: pre-wrap;">{{ s.rec_message }}</p>
                            </div>
                        {% else %}
                            <div style="margin-top:15px; background:rgba(0,0,0,0.3); border: 1px solid rgba(56, 189, 248, 0.2); padding:15px; border-radius:10px;">
                                <div style="display:flex; align-items:center; gap:8px; color:var(--accent); font-weight:bold; margin-bottom:8px;">
                                    🤖 AI Recommendation: {{ s.rec_feet }}'{{ s.rec_inches }}" | {{ s.rec_liters }}L
                                </div>
                                <label>Analysis Details</label>
                                <p style="font-size:0.9em; line-height:1.5; color:var(--text-muted); margin-bottom:0; white-space: pre-wrap;">{{ s.rec_message }}</p>
                            </div>
                        {% endif %}
                    {% endif %}
                </div>
            {% endfor %}
        </div>
    ''', user=user, surfs=history, inv=inv, zoom_messages=zoom_messages)

@app.route('/send_zoom_message', methods=['POST'])
def send_zoom_message():
    user = session.get('user')
    if not user: return redirect('/')
    msg_text = request.form.get('message')
    if msg_text:
        db.session.add(ZoomMessage(user_email=user['email'], sender='user', message=msg_text))
        db.session.commit()
    return redirect('/dashboard')

@app.route('/admin_reply_zoom', methods=['POST'])
def admin_reply_zoom():
    u = session.get('user')
    if not u or u['email'].lower().strip() != SUPER_ADMIN_EMAIL.lower().strip(): return redirect('/')
    target_email = request.form.get('target_email')
    msg_text = request.form.get('message')
    if target_email and msg_text:
        db.session.add(ZoomMessage(user_email=target_email, sender='admin', message=msg_text))
        db.session.commit()
    return redirect('/admin')

@app.route('/new_analysis', methods=['GET', 'POST'])
def new_analysis():
    if not session.get('user'): return redirect('/')
    
    if request.method == 'POST':
        sys_type = request.form.get('unit_sys')
        
        if sys_type == 'imperial':
            h_feet = float(request.form.get('height_ft', 0) or 0)
            h_inches = float(request.form.get('height_in', 0) or 0)
            w_lbs = float(request.form.get('weight_lbs', 0) or 0)
            final_h_cm = round((h_feet * 30.48) + (h_inches * 2.54), 1)
            final_w_kg = round(w_lbs * 0.453592, 1)
        else:
            final_h_cm = float(request.form.get('height_cm', 0) or 0)
            final_w_kg = float(request.form.get('weight_kg', 0) or 0)

        chosen_skill = request.form.get('skill')
        if not chosen_skill:
            chosen_skill = "Unspecified"
            
        session['temp_surfer'] = {
            'height': final_h_cm,
            'weight': final_w_kg,
            'skill': chosen_skill
        }
        return redirect(url_for('upload_video_page'))
    
    return render_template_string(CSS + '''
        <div class="container"><a href="/dashboard" style="color:var(--text-muted); text-decoration:none;">← Back</a><div class="card">
            <h2 style="margin-bottom:30px; text-align:center;">Step 1: Your Stats</h2>
            
            <form method="POST"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
                
                <div style="display:flex; justify-content:center; gap:20px; margin-bottom:20px;">
                    <label style="cursor:pointer; display:flex; align-items:center; gap:8px;">
                        <input type="radio" name="unit_sys" value="metric" checked onchange="toggleUnits()"> Metric (cm/kg)
                    </label>
                    <label style="cursor:pointer; display:flex; align-items:center; gap:8px;">
                        <input type="radio" name="unit_sys" value="imperial" onchange="toggleUnits()"> Imperial (ft/lbs)
                    </label>
                </div>
                
                <div id="metric-inputs" style="display:grid; grid-template-columns: 1fr 1fr; gap:20px;">
                    <div><label>Height (cm)</label><input type="number" step="0.1" name="height_cm" id="h_cm" placeholder="e.g. 180"></div>
                    <div><label>Weight (kg)</label><input type="number" step="0.1" name="weight_kg" id="w_kg" placeholder="e.g. 75"></div>
                </div>

                <div id="imperial-inputs" style="display:none; grid-template-columns: 1fr 1fr; gap:20px;">
                    <div>
                        <label>Height (ft & in)</label>
                        <div style="display:flex; gap:10px;">
                            <input type="number" name="height_ft" id="h_ft" placeholder="Ft">
                            <input type="number" name="height_in" id="h_in" max="11" placeholder="In">
                        </div>
                    </div>
                    <div><label>Weight (lbs)</label><input type="number" step="0.1" name="weight_lbs" id="w_lbs" placeholder="e.g. 165"></div>
                </div>
                
                <div style="margin-top: 25px; margin-bottom: 15px;">
                    <label style="display:flex; align-items:center; gap:12px; cursor:pointer; font-weight:bold; color:var(--text); background: rgba(255,255,255,0.05); padding: 15px 20px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.1); transition: 0.2s;">
                        <input type="checkbox" id="skill_toggle" onchange="document.getElementById('skill_dropdown').style.display = this.checked ? 'block' : 'none';" style="width: 20px; height: 20px; margin: 0; cursor: pointer;">
                        Provide Experience Level (Optional)
                    </label>
                </div>
                
                <div id="skill_dropdown" style="display:none; padding-top: 10px; animation: fadeIn 0.3s ease-out;">
                    <div class="skill-container">
                        <input type="radio" name="skill" value="Beginner" id="sk1" class="skill-option">
                        <label for="sk1" class="skill-card"><div style="font-size:1.8em;">🐣</div><div><h4 style="margin:0;">Beginner</h4><p style="margin:0; font-size:0.8em; color:#94a3b8;">Whitewater, foam boards.</p></div></label>
                        
                        <input type="radio" name="skill" value="Intermediate" id="sk2" class="skill-option">
                        <label for="sk2" class="skill-card"><div style="font-size:1.8em;">🌊</div><div><h4 style="margin:0;">Intermediate</h4><p style="margin:0; font-size:0.8em; color:#94a3b8;">Green waves, turns.</p></div></label>
                        
                        <input type="radio" name="skill" value="Expert" id="sk3" class="skill-option">
                        <label for="sk3" class="skill-card"><div style="font-size:1.8em;">⚡</div><div><h4 style="margin:0;">Expert</h4><p style="margin:0; font-size:0.8em; color:#94a3b8;">Aerials, critical waves.</p></div></label>
                    </div>
                </div>
                
                <button class="btn" style="margin-top:20px;" onclick="return validateStats()">Continue to Upload →</button>
            </form>
        </div></div>
        
        <script>
            function toggleUnits() {
                const isMetric = document.querySelector('input[name="unit_sys"]:checked').value === 'metric';
                document.getElementById('metric-inputs').style.display = isMetric ? 'grid' : 'none';
                document.getElementById('imperial-inputs').style.display = isMetric ? 'none' : 'grid';
            }
            
            function validateStats() {
                const isMetric = document.querySelector('input[name="unit_sys"]:checked').value === 'metric';
                if (isMetric) {
                    if(!document.getElementById('h_cm').value || !document.getElementById('w_kg').value) {
                        alert("Please enter height and weight."); return false;
                    }
                } else {
                    if(!document.getElementById('h_ft').value || !document.getElementById('w_lbs').value) {
                        alert("Please enter height (ft) and weight (lbs)."); return false;
                    }
                }
                return true;
            }
        </script>
    ''')

@app.route('/upload_video', methods=['GET', 'POST'])
def upload_video_page():
    if not session.get('user'): return redirect('/')
    if 'temp_surfer' not in session: return redirect('/new_analysis')
    
    email = session['user']['email']
    inv = get_inventory(email)
    
    if 'secure_upload_token' not in session:
        session['secure_upload_token'] = secrets.token_hex(16)
    
    if request.method == 'POST':
        submitted_token = request.form.get('secure_upload_token')
        used_tokens = session.get('used_tokens', [])
        
        if submitted_token in used_tokens:
            return render_template_string(CSS + '''<div class="container" style="text-align:center;"><h2>Upload already processing!</h2><p>Your video was safely received.</p><a href="/dashboard" class="btn">Go to Dashboard</a></div>''')

        files = request.files.getlist('video')
        files = [f for f in files if f.filename != '']
        
        tier = request.form.get('analysis_tier')
        is_pro = (tier == 'pro')
        required_bundles = len(files)

        if required_bundles == 0:
            return redirect('/upload_video')

        # CHECK INVENTORY
        if is_pro and inv.coach_bundles < required_bundles:
            return redirect('/shop')
        elif not is_pro and inv.ai_bundles < required_bundles:
            return redirect('/shop') 
            
        # CONSUME BUNDLE
        if is_pro:
            inv.coach_bundles -= required_bundles
            bundle_str = "coach"
        else:
            inv.ai_bundles -= required_bundles
            bundle_str = "ai"
            
        used_tokens.append(submitted_token)
        session['used_tokens'] = used_tokens
        session.pop('secure_upload_token', None)
        
        temp = session['temp_surfer']
        
        s = Surfer(
            user_email=email, 
            height_cm=temp['height'], 
            weight_kg=temp['weight'], 
            skill_level=temp['skill'], 
            is_pro=is_pro, 
            bundle_used=bundle_str
        )
        db.session.add(s)
        db.session.commit()
        
        filepaths = []
        for f in files:
            filename = secure_filename(f.filename)
            unique_filename = f"{secrets.token_hex(8)}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            f.save(filepath)
            filepaths.append(filepath)

        app_context = app.app_context()
        thread = threading.Thread(
            target=process_videos_background,
            args=(app_context, s.id, filepaths, s.weight_kg, s.height_cm, s.skill_level, email)
        )
        thread.start()

        session.pop('temp_surfer', None)
        return redirect('/dashboard')

    return render_template_string(CSS + '''
    <div class="container">
        <h2 style="text-align:center; margin-bottom:30px;">Step 2: Video Analysis</h2>
        <div class="card">
            <form method="POST" enctype="multipart/form-data" id="uploadForm">
                <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
                <input type="hidden" name="secure_upload_token" value="{{ secure_token }}">
                
                <label style="display:block; margin-bottom:15px; text-align:center;">Select Analysis Tier</label>
                <div class="skill-container">
                    <input type="radio" name="analysis_tier" value="standard" id="tier1" class="skill-option" checked>
                    <label for="tier1" class="skill-card">
                        <div style="font-size:1.8em;">🤖</div>
                        <div><h4 style="margin:0;">Standard AI</h4><p style="margin:0; font-size:0.8em; color:#94a3b8;">Uses 1 AI Bundle</p></div>
                    </label>
                    
                    <input type="radio" name="analysis_tier" value="pro" id="tier2" class="skill-option">
                    <label for="tier2" class="skill-card">
                        <div style="font-size:1.8em;">👨‍🔬</div>
                        <div><h4 style="margin:0;">Olympic Coach</h4><p style="margin:0; font-size:0.8em; color:#94a3b8;">Uses 1 Coach Bundle</p></div>
                    </label>
                </div>

                <div class="file-drop-zone" onclick="document.getElementById('real-file').click()" style="margin-top:25px;">
                    <span style="font-size:3em; display:block; margin-bottom:10px;">📹</span>
                    <div style="font-weight:600;">Click to Add Videos</div>
                    <div style="font-size:0.8em; color:#94a3b8; margin-top:5px;">(Available: {{ inv.ai_bundles }} AI | {{ inv.coach_bundles }} Coach)</div>
                </div>

                <input type="file" id="real-file" name="video" accept="video/*" multiple style="display:none;" onchange="addFiles(this)">
                <div id="preview-area" style="display:grid; grid-template-columns:1fr 1fr; gap:15px; margin-top:20px;"></div>
                <div id="file-count" style="text-align:center; margin-top:15px; color:#34d399; font-weight:bold;"></div>

                <button type="button" onclick="validateAndSubmit()" class="btn" style="margin-top:30px;">Analyze Footage ⚡</button>
            </form>
        </div>
    </div>

    <div class="modal-overlay" id="custom-modal"><div class="modal-box" id="modal-content"></div></div>

    <script>
    const dt = new DataTransfer(); 

    function addFiles(input) {
        const files = input.files;
        for (let i = 0; i < files.length; i++) {
            let isDuplicate = false;
            for (let j = 0; j < dt.files.length; j++) {
                if (dt.files[j].name === files[i].name && dt.files[j].size === files[i].size) {
                    isDuplicate = true; break;
                }
            }
            if (!isDuplicate) dt.items.add(files[i]);
        }
        updatePreview();
        input.value = ''; 
    }

    function removeFile(index) {
        dt.items.remove(index); 
        updatePreview(); 
    }

    function updatePreview() {
        const area = document.getElementById('preview-area');
        const count = document.getElementById('file-count');
        area.innerHTML = ""; 
        if (dt.files.length === 0) { count.innerText = ""; return; }
        count.innerText = "✅ " + dt.files.length + " video(s) ready to upload";
        for (let i = 0; i < dt.files.length; i++) {
            const file = dt.files[i];
            const url = URL.createObjectURL(file);
            const wrapper = document.createElement('div');
            wrapper.style.position = 'relative';
            const vid = document.createElement('video');
            vid.src = url; vid.controls = true; vid.style.width = "100%"; vid.style.borderRadius = "8px"; vid.style.border = "1px solid #38bdf8";
            const btn = document.createElement('div');
            btn.innerHTML = "✖"; btn.style.position = "absolute"; btn.style.top = "-10px"; btn.style.right = "-10px";
            btn.style.background = "#ef4444"; btn.style.color = "white"; btn.style.width = "25px"; btn.style.height = "25px";
            btn.style.borderRadius = "50%"; btn.style.textAlign = "center"; btn.style.lineHeight = "25px"; btn.style.cursor = "pointer"; btn.style.fontWeight = "bold";
            btn.onclick = function() { removeFile(i); };
            wrapper.appendChild(vid); wrapper.appendChild(btn); area.appendChild(wrapper);
        }
    }

    function validateAndSubmit() {
        const isPro = document.getElementById('tier2').checked;
        const requiredBundles = dt.files.length;
        
        let availableBundles = {{ inv.ai_bundles }};
        let bundleName = "AI Bundle(s)";
        if (isPro) { availableBundles = {{ inv.coach_bundles }}; bundleName = "Coach Bundle(s)"; }
        
        const modalBox = document.getElementById('modal-content');

        if (dt.files.length === 0) {
            alert("Please select at least one video!");
            return;
        }
        
        if (availableBundles < requiredBundles) {
            modalBox.innerHTML = `
                <div style="font-size:4em; margin-bottom:15px;">⚠️</div>
                <h2 style="margin-bottom:10px;">Out of Bundles</h2>
                <p style="color:var(--text-muted); font-size:1.1em; margin-bottom:5px;">You need <strong>${requiredBundles} ${bundleName}</strong>, but you only have <strong>${availableBundles}</strong>.</p>
                <div class="modal-buttons">
                    <button type="button" onclick="closeModal()" class="btn btn-secondary" style="width:50%;">Cancel</button>
                    <a href="/shop" class="btn" style="width:50%; text-decoration:none;">Go to Shop 🛒</a>
                </div>
            `;
        } else {
            modalBox.innerHTML = `
                <div style="font-size:4em; margin-bottom:15px;">🏄‍♂️</div>
                <h2 style="margin-bottom:10px;">Ready to Analyze!</h2>
                <p style="color:var(--text-muted); font-size:1.1em; margin-bottom:15px;">
                    We will use <strong>${requiredBundles} ${bundleName}</strong>. 
                    If the video isn't actually surfing, your bundle will be automatically refunded!
                </p>
                <div class="modal-buttons">
                    <button type="button" onclick="closeModal()" class="btn btn-secondary" style="width:50%;">Cancel</button>
                    <button type="button" onclick="confirmUpload(this)" class="btn" style="width:50%;">Upload & Analyze 🚀</button>
                </div>
            `;
        }

        document.getElementById('custom-modal').classList.add('active');
    }

    function closeModal() { document.getElementById('custom-modal').classList.remove('active'); }

    function confirmUpload(btnElement) {
        document.getElementById('real-file').files = dt.files;
        document.getElementById('uploadForm').submit();
        btnElement.innerHTML = "Processing... ⌛";
        btnElement.style.opacity = "0.7";
        btnElement.disabled = true;
    }
    </script>
    ''', csrf_token=generate_csrf_token, inv=inv, secure_token=session['secure_upload_token'])

@app.route('/admin')
def admin_panel():
    u = session.get('user')
    if not u or u['email'].lower().strip() != SUPER_ADMIN_EMAIL.lower().strip(): return redirect('/')
    
    pending = Surfer.query.filter_by(rec_liters=None).all()
    inventories = Inventory.query.all()
    
    # Compile zoom chats for the admin dashboard
    zoom_emails = set([i.user_email for i in inventories if i.zoom_bundles > 0])
    zoom_emails.update([m.user_email for m in ZoomMessage.query.all()])
    zoom_chats = {}
    for email in zoom_emails:
        zoom_chats[email] = ZoomMessage.query.filter_by(user_email=email).order_by(ZoomMessage.timestamp.asc()).all()

    return render_template_string(CSS + '''
        <div class="container" style="max-width: 900px;">
            <div class="header" style="display:flex; justify-content:space-between; align-items:center; margin-bottom:30px;">
                <h2>🛡️ Admin Command</h2>
                <a href="/logout" class="btn btn-secondary">Logout</a>
            </div>

            <div class="card" style="border:1px solid #a855f7; margin-bottom:40px;">
                <h3 style="color:#a855f7; margin-bottom:15px;">💳 Manage User Bundles</h3>
                
                <div style="background:rgba(0,0,0,0.3); padding:15px; border-radius:10px; max-height: 200px; overflow-y: auto; margin-bottom: 20px; border: 1px solid rgba(255,255,255,0.05);">
                    <table style="width:100%; text-align:left; color:white; border-collapse: collapse;">
                        <tr><th style="padding-bottom:10px; color:var(--text-muted);">Email</th><th style="padding-bottom:10px; color:var(--text-muted);">AI Packs</th><th style="padding-bottom:10px; color:var(--text-muted);">Coach Packs</th><th style="padding-bottom:10px; color:var(--text-muted);">Zoom Calls</th></tr>
                        {% for i in inventories %}
                        <tr style="border-top:1px solid rgba(255,255,255,0.05);">
                            <td style="padding:10px 0; font-size: 0.9em;">{{ i.user_email }}</td>
                            <td style="padding:10px 0; font-weight:bold; color:var(--accent);">{{ i.ai_bundles }}</td>
                            <td style="padding:10px 0; font-weight:bold; color:#a855f7;">{{ i.coach_bundles }}</td>
                            <td style="padding:10px 0; font-weight:bold; color:#34d399;">{{ i.zoom_bundles }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>

                <form action="/admin_update_inventory" method="POST" onsubmit="return confirmBundleUpdate(this);" style="display:flex; gap:15px; align-items:flex-end;">
                    <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
                    <div style="flex:2;">
                        <label>User Email</label>
                        <input type="text" name="email" placeholder="user@gmail.com" required style="margin-bottom:0;">
                    </div>
                    <div style="flex:1;">
                        <label>Type</label>
                        <select name="bundle_type" style="margin-bottom:0; padding:15px;">
                            <option value="ai">AI Bundle</option>
                            <option value="coach">Coach Bundle</option>
                            <option value="zoom">Zoom Call</option>
                        </select>
                    </div>
                    <div style="flex:1;">
                        <label>New Amount</label>
                        <input type="number" name="amount" placeholder="e.g. 1" required style="margin-bottom:0;">
                    </div>
                    <button class="btn" style="flex:1; background:linear-gradient(135deg, #9333ea, #c084fc); box-shadow:0 4px 15px rgba(168, 85, 247, 0.3);">Set Value</button>
                </form>
            </div>

            <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom:20px;">
                <h3 style="margin:0;">Pending Action Hub</h3>
                <div style="display: flex; gap: 15px; background: rgba(0,0,0,0.3); padding: 10px 15px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.05);">
                    <label style="cursor:pointer; display:flex; align-items:center; gap:5px; font-size:0.85em; color:var(--text);"><input type="checkbox" id="mod_ai" checked onchange="filterAdminMods()"> 🤖 AI</label>
                    <label style="cursor:pointer; display:flex; align-items:center; gap:5px; font-size:0.85em; color:var(--text);"><input type="checkbox" id="mod_pro" checked onchange="filterAdminMods()"> 👨‍🔬 Olympic</label>
                    <label style="cursor:pointer; display:flex; align-items:center; gap:5px; font-size:0.85em; color:var(--text);"><input type="checkbox" id="mod_zoom" checked onchange="filterAdminMods()"> 📹 Zoom Chats</label>
                </div>
            </div>

            {% if not pending and not zoom_chats %}
                <div style="text-align:center; padding:60px; color:var(--text-muted); background:var(--card-bg); border-radius:20px; border: 1px solid rgba(255,255,255,0.05);">
                    <h3>All caught up!</h3>
                    <p>No pending reviews or active chats.</p>
                </div>
            {% endif %}

            <div id="admin-cards-container">
            
            {% for email, msgs in zoom_chats.items() %}
                <div class="card admin-card" data-mod="zoom" style="border:1px solid #34d399;">
                    <div style="display:flex; justify-content:space-between; margin-bottom:20px;">
                        <div>
                            <h3 style="margin:0; color:white;">{{ email }}</h3>
                            <div style="color:var(--text-muted);">VIP Zoom Chat Channel</div>
                        </div>
                        <div class="status-badge ready" style="height:fit-content; background:rgba(52, 211, 153, 0.2); color:#34d399;">📹 ZOOM CHAT</div>
                    </div>
                    
                    <div style="background:rgba(0,0,0,0.3); padding:15px; border-radius:10px; max-height: 250px; overflow-y: auto; margin-bottom: 15px; border: 1px solid rgba(255,255,255,0.05);">
                        {% if not msgs %}<div style="color:var(--text-muted); text-align:center; font-size:0.9em;">No messages yet. User hasn't replied.</div>{% endif %}
                        {% for msg in msgs %}
                            <div style="margin-bottom: 10px; text-align: {% if msg.sender == 'admin' %}right{% else %}left{% endif %};">
                                <span style="display:inline-block; padding: 8px 12px; border-radius: 12px; font-size: 0.9em; background: {% if msg.sender == 'admin' %}#34d399; color: #0f172a;{% else %}rgba(56, 189, 248, 0.2); color: white; border: 1px solid rgba(56,189,248,0.3);{% endif %}; max-width: 80%;">
                                    {{ msg.message }}
                                </span>
                            </div>
                        {% endfor %}
                    </div>
                    
                    <form action="/admin_reply_zoom" method="POST" style="display:flex; gap:10px;">
                        <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
                        <input type="hidden" name="target_email" value="{{ email }}">
                        <input type="text" name="message" required placeholder="Type your reply to {{ email }}..." style="flex:1; margin-bottom:0; padding:12px; border-radius:8px; background:rgba(15, 23, 42, 0.8); border:1px solid #34d399; color:white;">
                        <button class="btn" style="width:auto; background:#34d399; color:#0f172a;">Reply</button>
                    </form>
                </div>
            {% endfor %}

            {% for s in pending %}
                <div class="card admin-card" data-mod="{{ s.bundle_used }}" style="border:1px solid {% if s.bundle_used == 'coach' %}#a855f7{% else %}#fbbf24{% endif %};">
                    <div style="display:flex; justify-content:space-between; margin-bottom:20px;">
                        <div>
                            <h3 style="margin:0; color:white;">{{ s.user_email }}</h3>
                            <div style="color:var(--text-muted);">{{ s.timestamp.strftime('%Y-%m-%d %H:%M') }}</div>
                        </div>
                        {% if s.bundle_used == 'coach' %}
                            <div class="status-badge ready" style="height:fit-content; background:rgba(168, 85, 247, 0.2); color:#c084fc;">💎 OLYMPIC COACH</div>
                        {% else %}
                            <div class="status-badge pending" style="height:fit-content;">Pending AI</div>
                        {% endif %}
                    </div>

                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px; margin-bottom:20px;">
                        <div style="background:rgba(0,0,0,0.3); padding:15px; border-radius:10px;">
                            <label>Biometrics</label>
                            <div style="font-size:1.1em; color:white;">{{ s.height_cm }}cm / {{ s.weight_kg }}kg</div>
                            <div style="color:var(--accent);">Level: {{ s.skill_level }}</div>
                        </div>

                        <div style="background:rgba(56, 189, 248, 0.1); border:1px solid var(--accent); padding:15px; border-radius:10px;">
                            <label style="color:var(--accent);">Initial Assessment</label>
                            {% if s.ai_rec_liters %}
                                <div style="font-size:1.3em; font-weight:bold; color:white; margin-bottom: 5px;">
                                    {{ s.ai_rec_feet }}'{{ s.ai_rec_inches }}" | {{ s.ai_rec_liters }}L
                                </div>
                                <div style="font-size:0.85em; color:var(--text-muted); line-height:1.4; white-space: pre-wrap;">{{ s.ai_rec_message }}</div>
                            {% else %}
                                <div style="font-size:1.1em; font-weight:bold; color:white;">Data missing or still processing...</div>
                            {% endif %}
                        </div>
                    </div>

                    <div style="background:black; border-radius:12px; padding:10px; margin-bottom:25px;">
                        <label style="color:white; margin-bottom:10px; display:block;">Session Videos</label>
                        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px;">
                            {% for vid in s.videos %}
                                <div style="text-align:center;">
                                    <video width="100%" height="200" controls src="/video_serve/{{vid.id}}"></video>
                                </div>
                            {% endfor %}
                        </div>
                    </div>

                    <div style="display:flex; gap:15px; align-items:stretch;">
                        
                        {% if s.bundle_used == 'coach' %}
                            <form action="/admin_decide/{{s.id}}" method="POST" style="flex:3;">
                                <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
                                <label style="color:#c084fc;">Your Decision & Coaching Notes</label>
                                <div style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap:15px; margin-bottom:10px;">
                                    <input type="number" name="ft" value="{{ s.ai_rec_feet or '' }}" placeholder="Feet (6)" required style="margin-bottom:0;">
                                    <input type="number" step="0.5" name="in" max="11.9" value="{{ s.ai_rec_inches or '' }}" placeholder="Inches (2)" required style="margin-bottom:0;">
                                    <input type="number" step="0.1" name="lit" value="{{ s.ai_rec_liters or '' }}" placeholder="Liters (32.5)" required style="margin-bottom:0;">
                                </div>
                                <textarea name="admin_notes" rows="3" placeholder="Explain your sizing logic here. (The AI will read this to learn your style!)" required style="width:100%; padding:12px; border-radius:8px; background:rgba(15, 23, 42, 0.8); color:white; border:1px solid #a855f7; font-family:inherit; margin-bottom:15px; resize:vertical;"></textarea>
                                <button class="btn">Send Results & Email 🚀</button>
                            </form>
                        {% else %}
                            <div style="flex:3; background:rgba(56, 189, 248, 0.05); border: 1px dashed rgba(56, 189, 248, 0.3); padding: 15px; border-radius: 8px; display:flex; flex-direction:column; justify-content:center;">
                                <label style="color:var(--accent); margin-bottom:8px;">🤖 AI Session Control</label>
                                <p style="color:var(--text-muted); margin:0; font-size:0.95em; line-height: 1.5;">
                                    This is an automated AI analysis. Manual overrides and sending are disabled. You can review the footage or delete the session to refund the user's bundle.
                                </p>
                            </div>
                        {% endif %}
                        
                        <form action="/admin_delete/{{s.id}}" method="POST" style="flex:1; display:flex;">
                            <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
                            <button class="btn" style="background:#ef4444; box-shadow:0 4px 15px rgba(239, 68, 68, 0.3); padding: 0; width:100%; align-self:stretch;" onclick="return confirm('Delete this session and refund their bundle?');">🗑️ Delete</button>
                        </form>

                    </div>
                </div>
            {% endfor %}
            </div>
        </div>

        <script>
            // JS Dictionary mapping emails to their exact bundle balances for fast checking
            const userInventory = {
                {% for i in inventories %}
                "{{ i.user_email.lower() }}": { ai: {{ i.ai_bundles }}, coach: {{ i.coach_bundles }}, zoom: {{ i.zoom_bundles }} },
                {% endfor %}
            };

            function confirmBundleUpdate(form) {
                const email = form.email.value.trim().toLowerCase();
                const type = form.bundle_type.value;
                const newAmount = parseInt(form.amount.value, 10);
                
                if (userInventory[email]) {
                    const currentAmount = userInventory[email][type];
                    if (newAmount < currentAmount) {
                        return confirm(`Are you sure you want to subtract? Changing ${email}'s ${type} bundles from ${currentAmount} down to ${newAmount}.`);
                    }
                }
                return true;
            }

            function filterAdminMods() {
                const showAI = document.getElementById('mod_ai').checked;
                const showPro = document.getElementById('mod_pro').checked;
                const showZoom = document.getElementById('mod_zoom').checked;
                
                document.querySelectorAll('.admin-card').forEach(card => {
                    const mod = card.getAttribute('data-mod');
                    if (mod === 'ai' && showAI) card.style.display = 'block';
                    else if (mod === 'coach' && showPro) card.style.display = 'block';
                    else if (mod === 'zoom' && showZoom) card.style.display = 'block';
                    else card.style.display = 'none';
                });
            }
        </script>
    ''', pending=pending, inventories=inventories, zoom_chats=zoom_chats)

@app.route('/admin_decide/<int:sid>', methods=['POST'])
def admin_decide(sid):
    if session.get('user')['email'].lower().strip() != SUPER_ADMIN_EMAIL.lower().strip(): return redirect('/')
    
    s = Surfer.query.get_or_404(sid)
    ft, inc, lit = request.form['ft'], request.form['in'], request.form['lit']
    notes = request.form['admin_notes'] 
    
    s.rec_feet = int(ft)
    s.rec_inches = float(inc)
    s.rec_liters = float(lit)
    s.rec_message = notes 
    db.session.commit()
    
    msg = f"Hi there!\nOur expert has manually analyzed your video.\n\n🌊 Volume: {lit}L\n📏 Length: {ft}'{inc}\"\n\nExpert Notes:\n{notes}\n\nGo shred!\n- The Surfing AI Team"
    send_email(s.user_email, "Your Pro Surf Analysis Results", msg)
    
    return redirect('/admin')

@app.route('/admin_update_inventory', methods=['POST'])
def admin_update_inventory():
    u = session.get('user')
    if not u or u['email'].lower().strip() != SUPER_ADMIN_EMAIL.lower().strip(): return redirect('/')
    target_email = request.form.get('email').strip()
    bundle_type = request.form.get('bundle_type')
    amount = int(request.form.get('amount'))
    
    inv = get_inventory(target_email)
    
    # NOW SETTING INSTEAD OF ADDING
    if bundle_type == 'ai': inv.ai_bundles = amount
    elif bundle_type == 'coach': inv.coach_bundles = amount
    elif bundle_type == 'zoom': inv.zoom_bundles = amount
        
    db.session.commit()
    return redirect('/admin')

@app.route('/admin_delete/<int:sid>', methods=['POST'])
def admin_delete(sid):
    u = session.get('user')
    if not u or u['email'].lower().strip() != SUPER_ADMIN_EMAIL.lower().strip(): return redirect('/')
    
    s = Surfer.query.get_or_404(sid)
    inv = get_inventory(s.user_email)
    
    if s.bundle_used == 'coach':
        inv.coach_bundles += 1
    else:
        inv.ai_bundles += 1
    
    SurfVideo.query.filter_by(surfer_id=s.id).delete()
    db.session.delete(s)
    db.session.commit()
    
    return redirect('/admin')

@app.route('/video_serve/<int:vid_id>')
def video_serve(vid_id):
    v = SurfVideo.query.get_or_404(vid_id)
    if not v.file_path or not os.path.exists(v.file_path): 
        return "No Data"
    return send_file(v.file_path, mimetype='video/mp4')

@app.route('/shop')
def shop():
    if not session.get('user'): return redirect('/')
    
    email = session['user']['email']
    inv = get_inventory(email)
    
    return render_template_string(CSS + '''
        <div class="container" style="max-width: 1000px;">
            <div class="header" style="display:flex; justify-content:space-between; align-items:center; margin-bottom:30px;">
                <a href="/dashboard" style="color:var(--text-muted); text-decoration:none;">← Back to Dashboard</a>
            </div>
            
            <h2 style="text-align:center; margin-bottom:10px;">Upgrade Your Surfing</h2>
            <p style="text-align:center; color:var(--text-muted); margin-bottom:10px;">Choose a bundle below to get tailored recommendations.</p>
            
            <div style="background:rgba(52, 211, 153, 0.1); border: 1px dashed #34d399; padding:15px 20px; border-radius:12px; max-width: 700px; margin: 0 auto 40px auto; text-align:center;">
                <div style="display:flex; justify-content:center; align-items:center; gap:10px; color:#34d399; font-weight:bold; margin-bottom:5px;">
                    <span>🛡️</span> Protected by SecureToken™
                </div>
                <p style="color:var(--text-muted); font-size:0.85em; margin:0;">
                    Your bundle purchases are locked to a cryptographic one-time token. If your internet crashes, you accidentally refresh the page, or the video drops during upload, <strong>your purchase is entirely safe</strong>. A bundle is ONLY consumed when our servers successfully process your footage.
                </p>
            </div>
            
            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:25px; align-items: stretch;">
                
                <div class="card" style="text-align:center; padding:35px 20px; display: flex; flex-direction: column;">
                    <div style="font-size:3em; margin-bottom:15px;">🤖</div>
                    <h3 style="font-size:1.4em; margin-bottom:5px;">AI Analysis</h3>
                    <p style="color:var(--text-muted); font-size:0.9em; margin-bottom:20px;">Instant AI equipment sizing</p>
                    <div style="font-size:2.5em; font-weight:800; color:white; margin-top: auto; margin-bottom:25px;">
                        <span style="font-size:0.6em; color:#ef4444; text-decoration:line-through; margin-right:8px;">$15</span>$10
                    </div>
                    <a href="#" class="btn btn-secondary" style="width:100%;">Buy AI Bundle</a>
                </div>
                
                <div class="card" style="text-align:center; padding:35px 20px; border: 1px solid var(--accent); background: rgba(56, 189, 248, 0.05); position:relative; display: flex; flex-direction: column; box-shadow: 0 0 15px rgba(56, 189, 248, 0.1);">
                    <div style="position:absolute; top:-15px; left:50%; transform:translateX(-50%); background:var(--accent); color:#0f172a; font-weight:bold; padding:5px 20px; border-radius:20px; font-size:0.75em; text-transform:uppercase; letter-spacing:1px; white-space:nowrap;">Expert Level</div>
                    <div style="font-size:3em; margin-bottom:15px;">👨‍🔬</div>
                    <h3 style="font-size:1.4em; margin-bottom:5px;">Olympic Coach</h3>
                    <p style="color:var(--text-muted); font-size:0.9em; margin-bottom:10px;">Manual video review & sizing</p>
                    <div style="font-size:2.5em; font-weight:800; color:white; margin-top: auto; margin-bottom:25px;">
                        <span style="font-size:0.6em; color:#ef4444; text-decoration:line-through; margin-right:8px;">$50</span>$35
                    </div>
                    <a href="#" class="btn" style="width:100%;">Buy Coach Bundle</a>
                </div>

                <div class="card" style="text-align:center; padding:35px 20px; border: 2px solid #a855f7; background: rgba(168, 85, 247, 0.05); position:relative; transform: scale(1.05); display: flex; flex-direction: column; box-shadow: 0 0 25px rgba(168, 85, 247, 0.2);">
                    <div style="position:absolute; top:-15px; left:50%; transform:translateX(-50%); background:#a855f7; color:white; font-weight:bold; padding:5px 20px; border-radius:20px; font-size:0.75em; text-transform:uppercase; letter-spacing:1px; white-space:nowrap;">Ultimate Access</div>
                    <div style="font-size:3em; margin-bottom:15px;">📹</div>
                    <h3 style="font-size:1.4em; margin-bottom:5px;">1-Hour Zoom</h3>
                    <p style="color:var(--text-muted); font-size:0.9em; margin-bottom:10px;">Live meeting with the coach</p>
                    <div style="font-size:2.5em; font-weight:800; color:white; margin-top: auto; margin-bottom:25px;">
                        <span style="font-size:0.6em; color:#ef4444; text-decoration:line-through; margin-right:8px;">$150</span>$99
                    </div>
                    <a href="#" class="btn" style="width:100%; background:linear-gradient(135deg, #9333ea, #c084fc); box-shadow: 0 4px 15px rgba(168, 85, 247, 0.3);">Book Zoom Call</a>
                </div>

            </div>
            
        </div>
    ''')

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(debug=True, port=5000)