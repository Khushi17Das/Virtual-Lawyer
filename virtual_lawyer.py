import os
import io
import json
import re
import pandas as pd
import streamlit as st
import mysql.connector

# --- Import PyMuPDF (fitz) ---
try:
    import fitz 
except ImportError:
    fitz = None
    st.info("PyMuPDF (fitz) not found. PDF text extraction disabled.")

# ================= 1. CONFIGURATION (STRICT SECRETS) =================
try:
    MYSQL_USER = st.secrets["MYSQL_USER"]
    MYSQL_PASSWORD = st.secrets["MYSQL_PASSWORD"]
    MYSQL_HOST = st.secrets["MYSQL_HOST"]
    MYSQL_DBNAME = st.secrets["MYSQL_DBNAME"]
    MYSQL_PORT = int(st.secrets["MYSQL_PORT"])
except Exception as e:
    st.error("Missing Secrets! Add MYSQL_USER, PASSWORD, HOST, DBNAME, PORT in Streamlit Cloud.")
    st.stop()

EXPORT_DIR = os.path.join(os.getcwd(), "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

# ================= 2. DATABASE HELPERS (AIVEN CLOUD FIX) =================
def get_server_conn():
    return mysql.connector.connect(
        host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASSWORD, port=MYSQL_PORT,
        ssl_ca=None, ssl_verify_cert=False, autocommit=True
    )

def get_db_conn():
    return mysql.connector.connect(
        host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASSWORD, 
        database=MYSQL_DBNAME, port=MYSQL_PORT,
        ssl_ca=None, ssl_verify_cert=False, autocommit=True
    )

def init_db_and_seed():
    try:
        # Step A: Create DB if not exists
        srv = get_server_conn()
        cur_srv = srv.cursor()
        cur_srv.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_DBNAME} CHARACTER SET utf8mb4;")
        cur_srv.close(); srv.close()

        # Step B: Create Tables
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(128) UNIQUE,
                password VARCHAR(128),
                role VARCHAR(32)
            ) ENGINE=InnoDB;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS laws (
                law_id INT AUTO_INCREMENT PRIMARY KEY,
                section VARCHAR(64) UNIQUE,
                title VARCHAR(255),
                short_desc TEXT,
                official_text LONGTEXT,
                source_url VARCHAR(512),
                category VARCHAR(64),
                keywords TEXT
            ) ENGINE=InnoDB;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_queries (
                qid INT AUTO_INCREMENT PRIMARY KEY,
                user_text LONGTEXT,
                matched_section VARCHAR(64),
                matched_law_id INT,
                score FLOAT,
                metadata JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB;
        """)
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        st.error(f"Database Initialization Failed: {e}")
        return False

# ================= 3. CORE LOGIC (NLP & SEEDING) =================
DEFAULT_USERS = [("admin", "admin@2026", "advocate"), ("client", "client@2026", "client")]

BUNDLED_LAWS = [
    ("302", "Murder", "Punishment for murder (Death/Life Imprisonment)", "IPC", "murder, kill, death, homicide, killing", ""),
    ("304", "Culpable Homicide", "Not amounting to murder", "IPC", "culpable, homicide, death, unintentional", ""),
    ("304B", "Dowry Death", "Death related to dowry harassment", "IPC", "dowry, marriage, harassment, death", ""),
    ("307", "Attempt to Murder", "Punishment for attempt to cause death", "IPC", "attempt, attack, murder, shooting, stabbing", ""),
    ("323", "Voluntarily Causing Hurt", "Minor physical injury", "IPC", "hurt, slap, minor injury, assault", ""),
    ("376", "Rape", "Sexual assault and punishment", "IPC", "rape, sexual assault, non-consensual", ""),
    ("379", "Theft", "Punishment for stealing movable property", "IPC", "theft, steal, stolen, robbery", ""),
    ("420", "Cheating", "Fraud and dishonestly inducing delivery of property", "IPC", "cheat, fraud, scam, money, forgery", ""),
    ("498A", "Cruelty to Wife", "Harassment for dowry by husband/relatives", "IPC", "cruelty, harassment, dowry, domestic violence", ""),
    ("138", "Cheque Bounce", "Dishonour of cheque for insufficiency of funds", "NIA", "cheque, bank, bounce, payment, dishonour", ""),
    ("66C", "Identity Theft", "Using password/ID of another", "Cyber", "password, identity, hacking, impersonation", ""),
    ("66D", "Cheating by Personation", "Online fraud using computer resource", "Cyber", "online fraud, personation, computer, phishing", ""),
    ("2(1)(g)", "Deficiency in Service", "Consumer Protection Act relief", "Consumer", "service, refund, consumer, faulty", "")
]

def seed_default_data():
    try:
        conn = get_db_conn(); cur = conn.cursor()
        # Seed Users
        for u, p, r in DEFAULT_USERS:
            cur.execute("INSERT IGNORE INTO users (username, password, role) VALUES (%s, %s, %s)", (u, p, r))
        # Seed Laws
        cur.execute("SELECT COUNT(1) FROM laws")
        if cur.fetchone()[0] == 0:
            for sec, tit, desc, cat, kws, txt in BUNDLED_LAWS:
                cur.execute("INSERT INTO laws (section, title, short_desc, category, keywords, official_text) VALUES (%s,%s,%s,%s,%s,%s)", (sec, tit, desc, cat, kws, txt))
        conn.commit(); cur.close(); conn.close()
    except: pass

SYNONYMS = {'murder':'302', 'kill':'302', 'death':'302', 'fraud':'420', 'scam':'420', 'stolen':'379', 'theft':'379', 'bank':'138', 'bounce':'138'}

def tokenize(text): return re.findall(r"[a-zA-Z0-9]+", (text or "").lower())

def score_law_match(text):
    try:
        conn = get_db_conn(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM laws"); laws = cur.fetchall(); cur.close(); conn.close()
        tokens = tokenize(text); matched = []
        for law in laws:
            score = 0; matched_tokens = set(); kws = [k.strip() for k in (law['keywords'] or "").split(",") if k.strip()]
            for t in tokens:
                if t in SYNONYMS and SYNONYMS[t] == law['section']: score += 3; matched_tokens.add(t)
                if t in kws: score += 2; matched_tokens.add(t)
                if t in (law['title'] or "").lower(): score += 1; matched_tokens.add(t)
            if law['section'] in tokens: score += 5; matched_tokens.add(law['section'])
            if score > 0:
                matched.append({"law_id": law['law_id'], "section": law['section'], "title": law['title'], "short_desc": law['short_desc'], "score": score, "matched_keywords": list(matched_tokens)})
        matched.sort(key=lambda x: -x['score']); return matched
    except: return []

def extract_pdf_text(bytestream):
    if fitz is None: return ""
    doc = fitz.open(stream=bytestream, filetype="pdf")
    return "\n".join([page.get_text("text") for page in doc])

# ================= 4. STYLING & UI (WHITE BOX FIX) =================
st.set_page_config(page_title="Virtual Lawyer ⚖️", layout="wide")

st.markdown("""
<style>
    .stApp { background-color: #001f3f; color: white; }
    [data-testid="stSidebar"] { background-color: #003366 !important; border-right: 1px solid #4CC5B3; }
    
    /* INPUT & TEXTAREA FIX */
    input, textarea { 
        background-color: #002b55 !important; 
        color: white !important; 
        border: 1px solid #4CC5B3 !important;
        -webkit-text-fill-color: white !important;
    }
    
    /* PDF UPLOADER BOX (GHOST BOX FIX) */
    [data-testid="stFileUploader"] { 
        background-color: #002b55 !important; 
        border: 2px dashed #008080 !important;
        border-radius: 10px;
        padding: 10px;
    }
    [data-testid="stFileUploaderText"] > span { color: white !important; font-weight: bold; }
    [data-testid="stFileUploader"] section { background-color: #002b55 !important; }

    /* BUTTONS & HEADERS */
    h1, h2, h3 { color: #4CC5B3 !important; font-family: 'Segoe UI', sans-serif; }
    .stButton>button { 
        background-color: #008080; color: white; font-weight: bold;
        border: none; border-radius: 8px; width: 100%; transition: 0.3s;
    }
    .stButton>button:hover { background-color: #4CC5B3; transform: scale(1.02); }
    
    /* LABELS */
    label, .stMarkdown p { color: white !important; }
    .app-header { background: linear-gradient(90deg, #004d40, #008080); padding: 25px; border-radius: 12px; text-align: center; margin-bottom: 20px; }
</style>
<div class="app-header"><h1>Virtual Lawyer ⚖️</h1><p>Smart Legal Assistant for Indian Laws</p></div>
""", unsafe_allow_html=True)

# ================= 5. MAIN APPLICATION FLOW =================
if init_db_and_seed(): seed_default_data()

if "role" not in st.session_state: st.session_state["role"] = None

# --- AUTH SECTION ---
if st.session_state["role"] is None:
    auth_tabs = st.tabs(["Login Portal", "New Registration"])
    with auth_tabs[0]:
        st.subheader("Secure Login")
        role_sel = st.radio("Access Level", ["Advocate", "Client"], horizontal=True)
        u_in = st.text_input("Username")
        p_in = st.text_input("Password", type="password")
        if st.button("Enter Dashboard"):
            conn = get_db_conn(); cur = conn.cursor()
            cur.execute("SELECT username, role FROM users WHERE username=%s AND password=%s", (u_in, p_in))
            res = cur.fetchone(); cur.close(); conn.close()
            if res and res[1].lower() == role_sel.lower():
                st.session_state["role"], st.session_state["username"] = res[1], res[0]
                st.rerun()
            else: st.error("Login Failed. Please check your credentials.")
    with auth_tabs[1]:
        st.subheader("Create New Account")
        nu = st.text_input("Choose Username")
        np = st.text_input("Choose Password (8+ characters)", type="password")
        nr = st.selectbox("I am an:", ["Client", "Advocate"])
        if st.button("Register Account"):
            try:
                conn = get_db_conn(); cur = conn.cursor()
                cur.execute("INSERT INTO users (username, password, role) VALUES (%s,%s,%s)", (nu, np, nr))
                conn.commit(); cur.close(); conn.close()
                st.success("Registration Successful! Now go to 'Login Portal'.")
            except: st.error("Username already exists.")

# --- APP CONTENT ---
else:
    st.sidebar.markdown(f"### 👋 Welcome, {st.session_state['username']}")
    st.sidebar.info(f"Role: {st.session_state['role']}")
    if st.sidebar.button("Secure Logout"):
        st.session_state["role"] = None; st.rerun()

    menu = ["🔍 Case Analysis", "📋 Document Checklist", "📚 Law Library"]
    if st.session_state["role"].lower() == "advocate": menu.append("🛠️ Admin Tools")
    
    app_tabs = st.tabs(menu)

    with app_tabs[0]: # Case Analysis
        st.header("Analyze Your Case")
        case_desc = st.text_area("Provide incident details (e.g., 'Someone stole my car'):", height=150)
        pdf_file = st.file_uploader("Upload Legal Notice or Complaint (PDF)", type=["pdf"])
        if st.button("Start Analysis", use_container_width=True):
            full_text = case_desc
            if pdf_file and fitz:
                full_text += "\n" + extract_pdf_text(pdf_file.read())
            
            matches = score_law_match(full_text)
            if matches:
                st.success(f"Matched Section: {matches[0]['section']}")
                st.subheader(matches[0]['title'])
                st.write(matches[0]['short_desc'])
                # Logging
                conn = get_db_conn(); cur = conn.cursor()
                cur.execute("INSERT INTO user_queries (user_text, matched_section, score) VALUES (%s,%s,%s)", (case_desc[:500], matches[0]['section'], matches[0]['score']))
                conn.commit(); cur.close(); conn.close()
            else: st.warning("No specific law matched. Please provide more details.")

    with app_tabs[1]: # Checklist
        st.header("Required Evidence Checklist")
        cases = {"Theft": ["FIR Copy", "Ownership Proof"], "Cheque Bounce": ["Original Cheque", "Return Memo", "Legal Notice"], "Murder": ["Post Mortem Report", "Weapon Details"]}
        choice = st.selectbox("Select Case Category:", list(cases.keys()))
        for item in cases[choice]: st.checkbox(item, key=f"chk_{item}")

    with app_tabs[2]: # Database
        st.header("Indian Law Repository")
        conn = get_db_conn()
        df = pd.read_sql("SELECT section, title, category, short_desc FROM laws", conn)
        st.dataframe(df, use_container_width=True, hide_index=True)
        conn.close()

    if len(app_tabs) > 3: # Admin Tools
        with app_tabs[3]:
            st.header("Manage Law Data")
            with st.form("admin_form"):
                ns = st.text_input("Law Section (e.g. 506)")
                nt = st.text_input("Title")
                nd = st.text_area("Description")
                nk = st.text_input("Keywords")
                if st.form_submit_button("Update Database"):
                    conn = get_db_conn(); cur = conn.cursor()
                    cur.execute("INSERT IGNORE INTO laws (section, title, short_desc, keywords) VALUES (%s,%s,%s,%s)", (ns, nt, nd, nk))
                    conn.commit(); cur.close(); conn.close()
                    st.success("Database Updated!")
