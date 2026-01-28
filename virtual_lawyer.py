import os
import streamlit as st
import mysql.connector
import pandas as pd
import json
import re

# fitz (PyMuPDF) text extraction ke liye
try:
    import fitz 
except ImportError:
    fitz = None

# ================= 1. CONFIG (Streamlit Secrets for Deployment) =================
# Ye details aapko Streamlit Cloud ke "Secrets" box mein daalni hongi
try:
    MYSQL_USER = st.secrets["MYSQL_USER"]
    MYSQL_PASSWORD = st.secrets["MYSQL_PASSWORD"]
    MYSQL_HOST = st.secrets["MYSQL_HOST"]
    MYSQL_DBNAME = st.secrets["MYSQL_DBNAME"]
    MYSQL_PORT = int(st.secrets["MYSQL_PORT"])
except Exception as e:
    st.error("Secrets not found! Make sure to add MYSQL_USER, PASSWORD, etc. in Streamlit Cloud Settings.")
    st.stop()

# Cloud folder setup
EXPORT_DIR = "exports"
if not os.path.exists(EXPORT_DIR):
    os.makedirs(EXPORT_DIR)

# ================= 2. DB HELPERS (Aiven SSL Fix Included) =================
def get_server_conn():
    return mysql.connector.connect(
        host=MYSQL_HOST, 
        user=MYSQL_USER, 
        password=MYSQL_PASSWORD, 
        port=MYSQL_PORT,
        ssl_ca=None,             # Aiven requirements
        ssl_verify_cert=False    # Basic cloud connection fix
    )

def get_db_conn():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DBNAME,
        port=MYSQL_PORT,
        ssl_ca=None,
        ssl_verify_cert=False
    )

def init_db_and_seed():
    try:
        srv = get_server_conn()
        srv.autocommit = True
        cur_srv = srv.cursor()
        cur_srv.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_DBNAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        cur_srv.close()
        srv.close()

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(128) UNIQUE,
            password VARCHAR(128),
            role VARCHAR(32)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_queries (
            qid INT AUTO_INCREMENT PRIMARY KEY,
            user_text LONGTEXT,
            matched_section VARCHAR(64),
            matched_law_id INT,
            score FLOAT,
            metadata JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (matched_law_id) REFERENCES laws(law_id) ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except mysql.connector.Error as err:
        st.error(f"Database Initialization Error: {err}")
        return False

# ================= 3. SEEDING & MATCHING =================
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
        cur.execute("SELECT COUNT(1) FROM users")
        if cur.fetchone()[0]==0:
            for u,p,r in DEFAULT_USERS:
                cur.execute("INSERT IGNORE INTO users (username,password,role) VALUES (%s,%s,%s)",(u,p,r))
        cur.execute("SELECT COUNT(1) FROM laws")
        if cur.fetchone()[0]==0:
            for sec,title,short,cat,kws,text in BUNDLED_LAWS:
                cur.execute("INSERT IGNORE INTO laws (section,title,short_desc,category,keywords,official_text) VALUES (%s,%s,%s,%s,%s,%s)",
                             (sec,title,short,cat,kws,text))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        pass

SYNONYMS = {'murder':'302','kill':'302','homicide':'304','assault':'307','stab':'307','dowry':'304B','rape':'376','cheat':'420','fraud':'420','theft':'379','phish':'66D','identity':'66C','cheque':'138','consumer':'2(1)(g)'}

def tokenize(text):
    return re.findall(r"[a-zA-Z0-9]+", (text or "").lower())

def score_law_match(text):
    try:
        conn = get_db_conn()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT law_id, section, title, short_desc, keywords FROM laws")
        laws = cur.fetchall()
        cur.close(); conn.close()
    except: return []
    tokens = tokenize(text)
    matched=[]
    for law in laws:
        score=0
        kws=[k.strip() for k in (law['keywords'] or "").split(",") if k.strip()]
        for t in tokens:
            if t in SYNONYMS and SYNONYMS[t]==law['section']: score+=3
            if t in kws: score+=2
            if t in (law['title'] or "").lower() or t in (law['short_desc'] or "").lower(): score+=1
        if law['section'] in tokens: score+=5
        if score>0:
            matched.append({"law_id":law['law_id'], "section":law['section'], "title":law['title'], "short_desc":law['short_desc'], "score":score})
    matched.sort(key=lambda x:-x['score'])
    return matched

def extract_text_from_pdf_bytes(bytestream, max_pages=50):
    if fitz is None: raise RuntimeError("PyMuPDF not installed.")
    doc = fitz.open(stream=bytestream, filetype="pdf")
    return "\n".join([page.get_text("text") for i,page in enumerate(doc) if i<max_pages])

def log_query(user_text, matched_section, matched_law_id, score, metadata=None):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO user_queries (user_text, matched_section, matched_law_id, score, metadata) VALUES (%s,%s,%s,%s,%s)",
                    (user_text, matched_section, matched_law_id, score, json.dumps(metadata)))
        conn.commit(); cur.close(); conn.close()
    except: pass

def export_csvs():
    try:
        conn = get_db_conn()
        laws = pd.read_sql("SELECT * FROM laws", conn)
        queries = pd.read_sql("SELECT * FROM user_queries", conn)
        conn.close()
        laws.to_csv(os.path.join(EXPORT_DIR,"laws_export.csv"), index=False)
        queries.to_csv(os.path.join(EXPORT_DIR,"queries_export.csv"), index=False)
        return os.path.abspath(EXPORT_DIR)
    except: return None

# ================= 4. UI RENDERING =================
def render_law_search_tab():
    st.header("Search Laws & Penalties")
    try:
        conn = get_db_conn()
        laws_df = pd.read_sql("SELECT section, title, category FROM laws", conn)
        conn.close()
        st.dataframe(laws_df, use_container_width=True)
    except: st.error("Database connection error.")

def render_admin_tab():
    if st.session_state["role"].lower() != "advocate":
        st.error("Access Denied.")
        return
    st.header("Admin Management")
    if st.button("Export CSVs"):
        path = export_csvs()
        st.success(f"Saved to {path}")

def render_home_query_tab():
    st.header(f"Query Database, {st.session_state['username']}")
    query_text = st.text_area("Describe your case:")
    uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])
    
    if st.button("Search"):
        full_query = query_text
        if uploaded_file:
            full_query += "\n" + extract_text_from_pdf_bytes(uploaded_file.read())
        matches = score_law_match(full_query)
        if matches:
            st.success(f"Match: Section {matches[0]['section']}")
            log_query(full_query, matches[0]['section'], matches[0]['law_id'], matches[0]['score'])
        else:
            st.warning("No match found.")

# ================= 5. MAIN APP START =================
st.set_page_config(page_title="Virtual Lawyer ⚖️", layout="wide")

# Dark Teal CSS
st.markdown("""
<style>
.stApp { background-color: #001f3f; color: white; }
h1, h2, h3 { color: #4CC5B3 !important; }
.stButton>button { background-color: #008080; color: white; }
[data-testid="stSidebar"] { background-color: #003366; }
</style>
""", unsafe_allow_html=True)

if init_db_and_seed():
    seed_default_if_empty()
else:
    st.stop()

if "role" not in st.session_state: st.session_state["role"] = None
if "username" not in st.session_state: st.session_state["username"] = None

if st.session_state["role"] is None:
    st.subheader("Login")
    role_selection = st.radio("Role", ["Advocate", "Client"], horizontal=True)
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")
    if st.button("Login"):
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE username=%s AND password=%s",(username,password))
        res = cur.fetchone()
        cur.close(); conn.close()
        if res and res[0].lower() == role_selection.lower():
            st.session_state["role"], st.session_state["username"] = res[0], username
            st.rerun()
        else: st.error("Invalid Login.")
else:
    st.sidebar.write(f"User: {st.session_state['username']} ({st.session_state['role']})")
    if st.sidebar.button("Logout"):
        st.session_state["role"] = None
        st.rerun()
    
    tabs = st.tabs(["Home", "Database", "Admin"] if st.session_state["role"]=="advocate" else ["Home", "Database"])
    with tabs[0]: render_home_query_tab()
    with tabs[1]: render_law_search_tab()
    if st.session_state["role"]=="advocate":
        with tabs[2]: render_admin_tab()