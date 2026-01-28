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

# ================= 1. CONFIG (STRICTLY FOR DEPLOYMENT) =================
try:
    MYSQL_USER = st.secrets["MYSQL_USER"]
    MYSQL_PASSWORD = st.secrets["MYSQL_PASSWORD"]
    MYSQL_HOST = st.secrets["MYSQL_HOST"]
    MYSQL_DBNAME = st.secrets["MYSQL_DBNAME"]
    MYSQL_PORT = int(st.secrets["MYSQL_PORT"])
except Exception as e:
    st.error("Secrets missing! Add them in Streamlit Cloud settings.")
    st.stop()

# Local drive ki jagah current cloud folder
EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)

# ================= 2. DB HELPERS (Aiven SSL Fix) =================
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
        cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(128) UNIQUE, password VARCHAR(128), role VARCHAR(32)) ENGINE=InnoDB;")
        cur.execute("CREATE TABLE IF NOT EXISTS laws (law_id INT AUTO_INCREMENT PRIMARY KEY, section VARCHAR(64) UNIQUE, title VARCHAR(255), short_desc TEXT, official_text LONGTEXT, source_url VARCHAR(512), category VARCHAR(64), keywords TEXT) ENGINE=InnoDB;")
        cur.execute("CREATE TABLE IF NOT EXISTS penalties (penalty_id INT AUTO_INCREMENT PRIMARY KEY, law_section VARCHAR(64), imprisonment VARCHAR(128), fine VARCHAR(128), severity VARCHAR(32), notes TEXT, FOREIGN KEY (law_section) REFERENCES laws(section) ON DELETE CASCADE) ENGINE=InnoDB;")
        cur.execute("CREATE TABLE IF NOT EXISTS user_queries (qid INT AUTO_INCREMENT PRIMARY KEY, user_text LONGTEXT, matched_section VARCHAR(64), matched_law_id INT, score FLOAT, metadata JSON, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (matched_law_id) REFERENCES laws(law_id) ON DELETE SET NULL) ENGINE=InnoDB;")
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        st.error(f"DB Error: {e}")
        return False

# ================= 3. LOGIC FUNCTIONS (SAME AS YOURS) =================
DEFAULT_USERS = [("admin","admin123","advocate"),("client","client123","client")]
BUNDLED_LAWS = [
    ("302","Murder","Punishment for murder","IPC","murder,kill,homicide,assault,stab",""),
    ("304","Culpable Homicide not Amounting to Murder","Culpable homicide (lesser than murder)","IPC","culpable homicide,death,assault",""),
    ("304B","Dowry Death","Dowry death: married woman dies within seven years","IPC","dowry,dowry death",""),
    ("307","Attempt to Murder","Attempt to cause death","IPC","attempt murder,attempted,assault",""),
    ("376","Rape","Rape & punishment (various sub-sections)","IPC","rape,sexual assault",""),
    ("379","Theft","Dishonest taking of movable property","IPC","theft,steal",""),
    ("420","Cheating","Cheating and dishonestly inducing delivery of property","IPC","cheat,fraud",""),
    ("498A","Cruelty by Husband/Relatives","Cruelty to married woman by husband/relatives","IPC","cruelty,498a",""),
    ("138","Dishonour of Cheque","Dishonour of cheque offence under Negotiable Instruments Act","NIA","cheque,bounce",""),
    ("66C","Identity Theft (IT Act)","Identity theft offences under IT Act","Cyber","identity,personation",""),
    ("66D","Cheating by Personation","Cheating by personation via electronic means","Cyber","personation,online fraud",""),
    ("2(1)(g)","Deficiency in Service (Consumer)","Deficiency in service under Consumer Protection Act","Consumer","consumer,refund",""),
]

def seed_default_if_empty():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        for u,p,r in DEFAULT_USERS:
            cur.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE password=VALUES(password)", (u, p, r))
        cur.execute("SELECT COUNT(1) FROM laws")
        if cur.fetchone()[0]==0:
            for sec,title,short,cat,kws,text in BUNDLED_LAWS:
                cur.execute("INSERT IGNORE INTO laws (section,title,short_desc,category,keywords,official_text) VALUES (%s,%s,%s,%s,%s,%s)", (sec,title,short,cat,kws,text))
        conn.commit(); cur.close(); conn.close()
    except: pass

SYNONYMS = {'murder':'302','kill':'302','homicide':'304','assault':'307','stab':'307','dowry':'304B','rape':'376','cheat':'420','fraud':'420','theft':'379','phish':'66D','identity':'66C','cheque':'138','consumer':'2(1)(g)'}

def tokenize(text): return re.findall(r"[a-zA-Z0-9]+", (text or "").lower())

def score_law_match(text):
    try:
        conn = get_db_conn(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT law_id, section, title, short_desc, keywords FROM laws")
        laws = cur.fetchall(); cur.close(); conn.close()
    except: return []
    tokens = tokenize(text); matched=[]
    for law in laws:
        score=0; matched_tokens=set(); kws=[k.strip() for k in (law['keywords'] or "").split(",") if k.strip()]
        for t in tokens:
            if t in SYNONYMS and SYNONYMS[t]==law['section']: score+=3; matched_tokens.add(t)
            if t in kws: score+=2; matched_tokens.add(t)
            if t in (law['title'] or "").lower() or t in (law['short_desc'] or "").lower(): score+=1; matched_tokens.add(t)
        if law['section'] in tokens: score+=5; matched_tokens.add(law['section'])
        if score>0: matched.append({"law_id":law['law_id'], "section":law['section'], "title":law['title'], "short_desc":law['short_desc'], "score":score, "matched_keywords":list(matched_tokens)})
    matched.sort(key=lambda x:-x['score']); return matched

def extract_text_from_pdf_bytes(bytestream):
    doc = fitz.open(stream=bytestream, filetype="pdf")
    return "\n".join([page.get_text("text") for page in doc])

def log_query(user_text, matched_section, matched_law_id, score, metadata=None):
    try:
        conn = get_db_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO user_queries (user_text, matched_section, matched_law_id, score, metadata) VALUES (%s,%s,%s,%s,%s)", (user_text, matched_section, matched_law_id, score, json.dumps(metadata)))
        conn.commit(); cur.close(); conn.close()
    except: pass

# ================= 4. UI FUNCTIONS (YOUR ORIGINAL ONES) =================
def render_law_search_tab():
    st.header("Search for Laws & Penalties")
    conn = get_db_conn()
    laws_df = pd.read_sql("SELECT section, title, short_desc, category FROM laws", conn)
    st.dataframe(laws_df, use_container_width=True, hide_index=True)
    conn.close()

def render_document_checklist():
    st.subheader("Document Checklist Assistant")
    case_docs = {
        "Mutual Consent Divorce": ["Marriage Certificate", "Wedding Photos", "Aadhar Card", "IT Returns", "Separation Proof"],
        "Domestic Violence (DV Case)": ["Incident List", "Medical Reports", "FIR/NC Copy", "Ownership Proof", "Recordings"],
        "Cheque Bounce (Sec 138 NI Act)": ["Original Cheque", "Return Memo", "Legal Notice Copy", "Postal Receipt", "Bill/Invoice"],
        "Cyber Fraud": ["Screenshots", "Bank Statement", "Email Header", "Identity Proof"]
    }
    selected_case = st.selectbox("Select Case Category:", ["-- Choose --"] + list(case_docs.keys()))
    if selected_case != "-- Choose --":
        for doc in case_docs[selected_case]: st.checkbox(doc)

def render_home_query_tab():
    st.header(f"Query the Law Database, {st.session_state['username']}")
    query_text = st.text_area("Describe your case:", placeholder="Type here...")
    uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])
    if st.button("Find Matching Law Section(s)", use_container_width=True):
        full_q = query_text
        if uploaded_file and fitz:
            full_q += "\n" + extract_text_from_pdf_bytes(uploaded_file.read())
        matches = score_law_match(full_q)
        if matches:
            st.success(f"Best Match: Section {matches[0]['section']}")
            st.info(f"{matches[0]['title']} - {matches[0]['short_desc']}")
            log_query(full_q, matches[0]['section'], matches[0]['law_id'], matches[0]['score'])
        else: st.warning("No matches found.")

def render_admin_tab():
    st.header("Admin Dashboard")
    conn = get_db_conn()
    q_df = pd.read_sql("SELECT created_at, user_text, matched_section, score FROM user_queries ORDER BY created_at DESC LIMIT 10", conn)
    st.table(q_df)
    conn.close()

# ================= 5. MAIN UI START =================
st.set_page_config(page_title="Virtual Lawyer ⚖️", layout="wide")

st.markdown("""
<style>
.stApp { background-color: #001f3f; color: white; }
h1, h2, h3, h4 { color: #4CC5B3 !important; }
[data-testid="stSidebar"] { background-color: #003366; color: white; }
.stButton>button { background-color: #008080; color: white; border: none; }

/* Input Boxes Fix: Dark background with White text inside */
input, textarea { 
    background-color: #002b55 !important; 
    color: white !important; 
    -webkit-text-fill-color: white !important; 
    border: 1px solid #008080 !important;
}

/* Checklist and other text forced to white */
.stApp p, .stApp label, .stApp span { color: white !important; }

.app-header { background: linear-gradient(90deg, #004d40, #008080); padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 20px; }
</style>
<div class="app-header"><h1>Virtual Lawyer ⚖️</h1><p>Educational Content Referenced from IndiaKanoon</p></div>
""", unsafe_allow_html=True)

if init_db_and_seed(): seed_default_if_empty()
else: st.stop()

if "role" not in st.session_state: st.session_state["role"] = None

if st.session_state["role"] is None:
    with st.container(border=True):
        role_selection = st.radio("Role", ["Advocate", "Client"], horizontal=True)
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button(f"Login as {role_selection}"):
            conn = get_db_conn(); cur = conn.cursor()
            cur.execute("SELECT username, role FROM users WHERE username=%s AND password=%s", (u,p))
            res = cur.fetchone(); cur.close(); conn.close()
            if res and res[1].lower() == role_selection.lower():
                st.session_state["role"], st.session_state["username"] = res[1], res[0]
                st.rerun()
            else: st.error("Login Failed.")
else:
    st.sidebar.write(f"Logged in as: {st.session_state['username']}")
    if st.sidebar.button("Logout"): st.session_state["role"] = None; st.rerun()
    
    titles = ["Home: Query Law", "Document Checklist", "Law Database"]
    if st.session_state["role"].lower() == "advocate": titles.append("Admin Dashboard")
    tabs = st.tabs(titles)
    with tabs[0]: render_home_query_tab()
    with tabs[1]: render_document_checklist()
    with tabs[2]: render_law_search_tab()
    if len(tabs) > 3:
        with tabs[3]: render_admin_tab()
