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
    st.info("PyMuPDF (fitz) not found. PDF extraction will be disabled.")

# ================= 1. CONFIG (STREAMLIT SECRETS) =================
# Deployment ke liye st.secrets use karna zaroori hai
MYSQL_USER = st.secrets["MYSQL_USER"]
MYSQL_PASSWORD = st.secrets["MYSQL_PASSWORD"]
MYSQL_HOST = st.secrets["MYSQL_HOST"]
MYSQL_DBNAME = st.secrets["MYSQL_DBNAME"]
MYSQL_PORT = int(st.secrets["MYSQL_PORT"])

EXPORT_DIR = os.path.join(os.getcwd(), "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

# ================= 2. DB HELPERS (AIVEN FIX) =================
def get_server_conn():
    return mysql.connector.connect(
        host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASSWORD, port=MYSQL_PORT,
        ssl_ca=None, ssl_verify_cert=False
    )

def get_db_conn():
    return mysql.connector.connect(
        host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASSWORD, 
        database=MYSQL_DBNAME, port=MYSQL_PORT,
        ssl_ca=None, ssl_verify_cert=False
    )

def init_db_and_seed():
    try:
        srv = get_server_conn()
        srv.autocommit = True
        cur_srv = srv.cursor()
        cur_srv.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_DBNAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        cur_srv.close(); srv.close()

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
        CREATE TABLE IF NOT EXISTS penalties (
            penalty_id INT AUTO_INCREMENT PRIMARY KEY,
            law_section VARCHAR(64),
            imprisonment VARCHAR(128),
            fine VARCHAR(128),
            severity VARCHAR(32),
            notes TEXT,
            FOREIGN KEY (law_section) REFERENCES laws(section) ON DELETE CASCADE
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
        st.error(f"DB Init Error: {e}")
        return False

# ================= 3. SEEDING & MATCHING LOGIC =================
DEFAULT_USERS = [("admin","admin123","advocate"),("client","client123","client")]
BUNDLED_LAWS = [
    ("302","Murder","Punishment for murder","IPC","murder,kill,homicide,assault,stab",""),
    ("304","Culpable Homicide not Amounting to Murder","Culpable homicide","IPC","culpable homicide,death,assault",""),
    ("304B","Dowry Death","Dowry death case","IPC","dowry,dowry death",""),
    ("307","Attempt to Murder","Attempt to cause death","IPC","attempt murder,assault",""),
    ("376","Rape","Rape & punishment","IPC","rape,sexual assault",""),
    ("379","Theft","Taking movable property","IPC","theft,steal",""),
    ("420","Cheating","Fraud and cheating","IPC","cheat,fraud",""),
    ("498A","Cruelty by Husband","Cruelty to married woman","IPC","cruelty,498a",""),
    ("138","Dishonour of Cheque","Cheque bounce offence","NIA","cheque,bounce",""),
    ("66C","Identity Theft","Identity theft offences","Cyber","identity,personation",""),
    ("66D","Cheating by Personation","Online fraud","Cyber","personation,online fraud",""),
    ("2(1)(g)","Deficiency in Service","Consumer Protection Act","Consumer","consumer,refund",""),
]

def seed_default_if_empty():
   try:
        conn = get_db_conn(); cur = conn.cursor()
        # Default users
        cur.execute("INSERT IGNORE INTO users (username, password, role) VALUES ('admin', 'advocate_secure_2024', 'advocate'), ('client', 'client_secure_pass', 'client')")
        cur.execute("SELECT COUNT(1) FROM laws")
        if cur.fetchone()[0] == 0:
            bundled = [("302","Murder","Punishment for murder","IPC","murder,kill,stab",""), ("420","Cheating","Fraud cases","IPC","cheat,fraud",""), ("138","Cheque Bounce","NI Act","NIA","cheque,bounce","")]
            for sec, tit, desc, cat, kws, txt in bundled:
                cur.execute("INSERT IGNORE INTO laws (section, title, short_desc, category, keywords, official_text) VALUES (%s,%s,%s,%s,%s,%s)", (sec, tit, desc, cat, kws, txt))
        conn.commit(); cur.close(); conn.close()
    except: pass

SYNONYMS = {'murder':'302','kill':'302','homicide':'304','cheat':'420','fraud':'420','theft':'379','cheque':'138'}
def tokenize(text): return re.findall(r"[a-zA-Z0-9]+", (text or "").lower())

def score_law_match(text):
    try:
        conn = get_db_conn(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM laws"); laws = cur.fetchall(); cur.close(); conn.close()
        tokens = tokenize(text); matched = []
        for law in laws:
            score = 0; matched_kws = set(); kws = [k.strip() for k in (law['keywords'] or "").split(",") if k.strip()]
            for t in tokens:
                if t in SYNONYMS and SYNONYMS[t] == law['section']: score += 3; matched_kws.add(t)
                if t in kws: score += 2; matched_kws.add(t)
                if t in (law['title'] or "").lower(): score += 1; matched_kws.add(t)
            if law['section'] in tokens: score += 5; matched_kws.add(law['section'])
            if score > 0: matched.append({"law_id":law['law_id'], "section":law['section'], "title":law['title'], "short_desc":law['short_desc'], "score":score, "matched_keywords":list(matched_kws)})
        matched.sort(key=lambda x:-x['score']); return matched
    except: return []

def extract_text_from_pdf_bytes(bytestream):
    if fitz is None: return ""
    doc = fitz.open(stream=bytestream, filetype="pdf")
    return "\n".join([page.get_text("text") for page in doc])

def log_query(user_text, matched_section, matched_law_id, score, metadata=None):
    try:
        conn = get_db_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO user_queries (user_text, matched_section, matched_law_id, score, metadata) VALUES (%s,%s,%s,%s,%s)", (user_text, matched_section, matched_law_id, score, json.dumps(metadata)))
        conn.commit(); cur.close(); conn.close()
    except: pass

# ================= 4. UI FUNCTIONS (FULL ORIGINAL) =================
def render_law_search_tab():
    st.header("Search Laws & Penalties")
    conn = get_db_conn()
    laws_df = pd.read_sql("SELECT section, title, short_desc, category FROM laws", conn)
    st.dataframe(laws_df, use_container_width=True, hide_index=True)
    conn.close()

def render_document_checklist():
    st.header("📋 Document Checklist Assistant")
    case_docs = {
        "Mutual Consent Divorce": ["Marriage Certificate", "Wedding Photos", "Aadhar Card", "Separation Proof"],
        "Cheque Bounce (Sec 138)": ["Original Cheque", "Bank Return Memo", "Legal Demand Notice", "Postal Receipt"],
        "Cyber Fraud": ["Screenshots of Chat", "Bank Statement", "Email Headers", "ID Proof"]
    }
    sel = st.selectbox("Select Case Category:", ["-- Choose --"] + list(case_docs.keys()))
    if sel != "-- Choose --":
        st.write(f"### Required for {sel}:")
        for doc in case_docs[sel]: st.checkbox(doc, key=f"chk_{doc}")

def render_home_query_tab():
    st.header(f"⚖️ Welcome, {st.session_state['username']}")
    query_text = st.text_area("Describe your case here:", height=150)
    
    # CSS target for the Uploader Box to make it dark teal/navy
    st.markdown('<div class="dark-uploader">', unsafe_allow_html=True)
    uploaded_file = st.file_uploader("Upload PDF Evidence", type=["pdf"])
    st.markdown('</div>', unsafe_allow_html=True)

    if st.button("Find Matching Law Section(s)", use_container_width=True):
        if query_text:
            matches = score_law_match(query_text)
            if matches:
                st.success(f"Best Match: Section {matches[0]['section']}")
                st.info(f"**{matches[0]['title']}**: {matches[0]['short_desc']}")
            else: st.warning("No direct match found. Try more specific keywords.")

def render_admin_tab():
    st.header("Advocate Dashboard")
    with st.form("add_law"):
        st.subheader("Add New Law Entry")
        c1, c2 = st.columns(2)
        sec = c1.text_input("Section")
        cat = c2.text_input("Category")
        tit = st.text_input("Title")
        desc = st.text_area("Short Description")
        if st.form_submit_button("Save Law"):
            try:
                conn = get_db_conn(); cur = conn.cursor()
                cur.execute("INSERT INTO laws (section, title, short_desc, category) VALUES (%s,%s,%s,%s)", (sec, tit, desc, cat))
                conn.commit(); cur.close(); conn.close()
                st.success("Law Added!")
            except Exception as e: st.error(f"Error: {e}")

# ================= 5. LOGIN / SIGNUP =================
def render_auth():
    st.markdown("<div class='app-header'><h1>Virtual Lawyer ⚖️</h1></div>", unsafe_allow_html=True)
    auth_tabs = st.tabs(["Login", "Sign Up"])
    with auth_tabs[0]:
        r = st.radio("Role", ["Advocate", "Client"], horizontal=True)
        u = st.text_input("Username", key="login_u")
        p = st.text_input("Password", type="password", key="login_p")
        if st.button("Login"):
            conn = get_db_conn(); cur = conn.cursor()
            cur.execute("SELECT username, role FROM users WHERE username=%s AND password=%s", (u,p))
            res = cur.fetchone(); cur.close(); conn.close()
            if res and res[1].lower() == r.lower():
                st.session_state["role"], st.session_state["username"] = res[1], res[0]
                st.rerun()
            else: st.error("Login Failed.")
    with auth_tabs[1]:
        nu = st.text_input("New Username")
        np = st.text_input("New Password", type="password")
        nr = st.selectbox("I am a:", ["Client", "Advocate"])
        if st.button("Register"):
            try:
                conn = get_db_conn(); cur = conn.cursor()
                cur.execute("INSERT INTO users (username, password, role) VALUES (%s,%s,%s)", (nu,np,nr))
                conn.commit(); cur.close(); conn.close()
                st.success("Registered! Go to Login.")
            except: st.error("User already exists.")

# ================= 6. STYLING & MAIN =================
st.set_page_config(page_title="Virtual Lawyer ⚖️", layout="wide")

st.markdown("""
<style>
/* Main App Background */
.stApp { background-color: #001f3f; color: #FFFFFF; }

/* Sidebar Styling */
[data-testid="stSidebar"] { background-color: #003366 !important; border-right: 1px solid #4CC5B3; }
[data-testid="stSidebar"] * { color: #FFFFFF !important; }

/* Input Fields (Ghost Text Fix) */
input, textarea { 
    background-color: #002b55 !important; 
    color: white !important; 
    border: 1px solid #4CC5B3 !important;
    -webkit-text-fill-color: white !important;
}

/* FILE UPLOADER DARK FIX */
[data-testid="stFileUploader"] {
    background-color: #002b55 !important;
    border: 2px dashed #4CC5B3 !important;
    border-radius: 10px;
    padding: 10px;
}
[data-testid="stFileUploaderText"] > span { color: #FFFFFF !important; }
[data-testid="stFileUploader"] section { background-color: transparent !important; }

/* Headers and Buttons */
h1, h2, h3 { color: #4CC5B3 !important; }
.stButton>button { 
    background-color: #008080 !important; 
    color: white !important; 
    border-radius: 8px;
    border: none;
    font-weight: bold;
}

/* Tabs Styling */
.stTabs [data-baseweb="tab"] { color: #FFFFFF !important; }
.stTabs [aria-selected="true"] { color: #4CC5B3 !important; border-bottom-color: #4CC5B3 !important; }
</style>
""", unsafe_allow_html=True)

if init_db_and_seed(): seed_default_if_empty()

if "role" not in st.session_state or st.session_state["role"] is None:
    st.markdown("<h1 style='text-align: center;'>Virtual Lawyer ⚖️</h1>", unsafe_allow_html=True)
    t1, t2 = st.tabs(["Login", "Create Account"])
    with t1:
        u = st.text_input("Username", key="l_u")
        p = st.text_input("Password", type="password", key="l_p")
        r = st.radio("Role", ["Advocate", "Client"], horizontal=True)
        if st.button("Login"):
            conn = get_db_conn(); cur = conn.cursor()
            cur.execute("SELECT username, role FROM users WHERE username=%s AND password=%s", (u,p))
            res = cur.fetchone(); cur.close(); conn.close()
            if res and res[1].lower() == r.lower():
                st.session_state["role"], st.session_state["username"] = res[1], res[0]
                st.rerun()
            else: st.error("Invalid Username/Password or Role mismatch")
    with t2:
        nu = st.text_input("New Username")
        np = st.text_input("New Password (Use 8+ chars)", type="password")
        nr = st.selectbox("Register as", ["Client", "Advocate"])
        if st.button("Sign Up"):
            try:
                conn = get_db_conn(); cur = conn.cursor()
                cur.execute("INSERT INTO users (username, password, role) VALUES (%s,%s,%s)", (nu,np,nr))
                conn.commit(); cur.close(); conn.close()
                st.success("Registered! Now Login.")
            except: st.error("Username already taken.")
else:
    st.sidebar.subheader(f"User: {st.session_state['username']}")
    if st.sidebar.button("Logout"): 
        st.session_state["role"] = None
        st.rerun()

    titles = ["Home: Query Law", "Document Checklist", "Law Database"]
    if st.session_state["role"].lower() == "advocate": titles.append("Admin Dashboard")
    
    tabs = st.tabs(titles)
    with tabs[0]: render_home_query_tab()
    with tabs[1]: render_document_checklist()
    with tabs[2]:
        st.header("Law Database")
        conn = get_db_conn()
        df = pd.read_sql("SELECT section, title, category FROM laws", conn)
        st.dataframe(df, use_container_width=True, hide_index=True)
        conn.close()
    if len(tabs) > 3:
        with tabs[3]: render_admin_tab()

