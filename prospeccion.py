import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
import json
import hashlib
import re
import io
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from datetime import datetime
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Prospección en Frío", page_icon="📧", layout="wide")

ZONA_CDMX = ZoneInfo("America/Mexico_City")
DB_PATH = "prospeccion.db"

COLUMNAS_DEFAULT = ["Nombre", "Empresa", "Correo"]
ASUNTO_DEFAULT = "Oportunidad de colaboración con (Empresa)"
CUERPO_DEFAULT = (
    "Hola (Nombre),\n\n"
    "Espero que te encuentres muy bien.\n\n"
    "Me pongo en contacto contigo de parte de [Tu empresa].\n\n"
    "[Escribe aquí tu propuesta de valor...]\n\n"
    "Quedamos a tus órdenes,\n"
    "[Tu nombre y cargo]"
)
PAGINAS = ["Inicio", "Envío masivo", "Envío individual", "Dashboard",
           "Seguimientos", "Mi configuración", "Administración"]


def hoy_cdmx():
    return datetime.now(ZONA_CDMX).date()


def ahora_cdmx():
    return datetime.now(ZONA_CDMX).isoformat()


def hash_pw(p):
    return hashlib.sha256(p.encode()).hexdigest()


# ─────────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────────

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _add_col(conn, table, col, coltype):
    c = conn.cursor()
    c.execute(f"PRAGMA table_info({table})")
    if col not in [r[1] for r in c.fetchall()]:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        conn.commit()


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        nombre TEXT,
        activo INTEGER DEFAULT 1,
        creado_en TEXT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS configuracion (
        clave TEXT PRIMARY KEY,
        valor TEXT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS campanas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        asunto_plantilla TEXT NOT NULL,
        cuerpo_plantilla TEXT NOT NULL,
        adjunto_nombre TEXT,
        tipo TEXT NOT NULL,
        usuario TEXT NOT NULL,
        creado_en TEXT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS correos_enviados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campana_id INTEGER NOT NULL,
        fila_datos TEXT,
        nombres TEXT NOT NULL,
        correos TEXT NOT NULL,
        asunto_final TEXT NOT NULL,
        cuerpo_final TEXT NOT NULL,
        estado TEXT NOT NULL,
        error TEXT,
        enviado_en TEXT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS seguimientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        correo_enviado_id INTEGER NOT NULL,
        asunto TEXT NOT NULL,
        cuerpo TEXT NOT NULL,
        estado TEXT NOT NULL,
        error TEXT,
        enviado_en TEXT NOT NULL
    )""")

    conn.commit()

    # Migraciones: columnas por usuario
    for col, tipo in [
        ("email_remitente", "TEXT DEFAULT ''"),
        ("email_password", "TEXT DEFAULT ''"),
        ("asunto_plantilla", "TEXT DEFAULT ''"),
        ("cuerpo_plantilla", "TEXT DEFAULT ''"),
    ]:
        _add_col(conn, "usuarios", col, tipo)

    # Usuario admin por defecto
    c.execute("SELECT COUNT(*) FROM usuarios")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO usuarios (username, password_hash, nombre, creado_en) VALUES (?,?,?,?)",
            ("admin", hash_pw("admin123"), "Administrador", ahora_cdmx())
        )

    # Config global por defecto
    defaults = {
        "columnas_excel": json.dumps(COLUMNAS_DEFAULT),
        "columna_correo": "Correo",
        "admin_password": hash_pw("admin123"),
        # Credenciales globales de respaldo (opcionales)
        "email_remitente": "",
        "email_password": "",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO configuracion (clave, valor) VALUES (?,?)", (k, v))

    # Migración: columna respondido para bases de datos existentes
    c.execute("PRAGMA table_info(correos_enviados)")
    cols = [r[1] for r in c.fetchall()]
    if "respondido" not in cols:
        c.execute("ALTER TABLE correos_enviados ADD COLUMN respondido INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def get_config(clave, default=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT valor FROM configuracion WHERE clave=?", (clave,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default


def set_config(clave, valor):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO configuracion (clave, valor) VALUES (?,?) "
        "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
        (clave, str(valor))
    )
    conn.commit()
    conn.close()


def get_columnas_excel():
    raw = get_config("columnas_excel")
    if not raw:
        return list(COLUMNAS_DEFAULT)
    try:
        return json.loads(raw)
    except Exception:
        return list(COLUMNAS_DEFAULT)


# ── Datos por usuario ──────────────────────────

def get_usuario_row(username):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE username=?", (username,))
    row = c.fetchone()
    cols = [d[0] for d in c.description] if c.description else []
    conn.close()
    if row and cols:
        return dict(zip(cols, row))
    return {}


def set_usuario_email(username, remitente, password):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE usuarios SET email_remitente=?, email_password=? WHERE username=?",
        (remitente.strip(), password.strip(), username)
    )
    conn.commit()
    conn.close()


def set_usuario_plantilla(username, asunto, cuerpo):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE usuarios SET asunto_plantilla=?, cuerpo_plantilla=? WHERE username=?",
        (asunto, cuerpo, username)
    )
    conn.commit()
    conn.close()


def get_email_credentials_usuario(username):
    """Credenciales del usuario. Si no tiene, usa las globales o secrets."""
    row = get_usuario_row(username)
    rem = row.get("email_remitente", "").strip()
    pwd = row.get("email_password", "").strip()
    if rem and pwd:
        return rem, pwd
    # Fallback: config global
    rem_g = get_config("email_remitente", "").strip()
    pwd_g = get_config("email_password", "").strip()
    if rem_g and pwd_g:
        return rem_g, pwd_g
    # Fallback: secrets
    try:
        return st.secrets["email"]["remitente"], st.secrets["email"]["password"]
    except Exception:
        return "", ""


def get_plantilla_usuario(username):
    """Plantilla del usuario. Si no tiene, usa los valores por defecto globales."""
    row = get_usuario_row(username)
    asunto = (row.get("asunto_plantilla") or "").strip()
    cuerpo = (row.get("cuerpo_plantilla") or "").strip()
    return (
        asunto if asunto else ASUNTO_DEFAULT,
        cuerpo if cuerpo else CUERPO_DEFAULT,
    )


# ── Usuarios CRUD ──────────────────────────────

def verificar_usuario(username, password):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, nombre FROM usuarios WHERE username=? AND password_hash=? AND activo=1",
        (username.strip(), hash_pw(password))
    )
    row = c.fetchone()
    conn.close()
    return row


def get_usuarios():
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT id, username, nombre, activo, email_remitente, creado_en FROM usuarios ORDER BY username",
        conn
    )
    conn.close()
    return df


def crear_usuario(username, password, nombre):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO usuarios (username, password_hash, nombre, creado_en) VALUES (?,?,?,?)",
            (username.strip(), hash_pw(password), nombre.strip(), ahora_cdmx())
        )
        conn.commit()
        conn.close()
        return True, "Usuario creado."
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Ya existe un usuario con ese nombre."


def cambiar_password_usuario(user_id, nueva_password):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE usuarios SET password_hash=? WHERE id=?", (hash_pw(nueva_password), user_id))
    conn.commit()
    conn.close()


def toggle_usuario_activo(user_id, activo):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE usuarios SET activo=? WHERE id=?", (int(activo), user_id))
    conn.commit()
    conn.close()


# ── Campañas ───────────────────────────────────

def crear_campana(nombre, asunto, cuerpo, tipo, usuario, adjunto_nombre=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO campanas (nombre, asunto_plantilla, cuerpo_plantilla, adjunto_nombre, tipo, usuario, creado_en) "
        "VALUES (?,?,?,?,?,?,?)",
        (nombre, asunto, cuerpo, adjunto_nombre, tipo, usuario, ahora_cdmx())
    )
    campana_id = c.lastrowid
    conn.commit()
    conn.close()
    return campana_id


def registrar_correo(campana_id, nombres, correos, asunto_final, cuerpo_final, estado, fila_datos=None, error=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO correos_enviados "
        "(campana_id, fila_datos, nombres, correos, asunto_final, cuerpo_final, estado, error, enviado_en) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            campana_id,
            json.dumps(fila_datos, ensure_ascii=False) if fila_datos else None,
            nombres, correos, asunto_final, cuerpo_final, estado, error, ahora_cdmx()
        )
    )
    conn.commit()
    conn.close()


def registrar_seguimiento(correo_enviado_id, asunto, cuerpo, estado, error=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO seguimientos (correo_enviado_id, asunto, cuerpo, estado, error, enviado_en) "
        "VALUES (?,?,?,?,?,?)",
        (correo_enviado_id, asunto, cuerpo, estado, error, ahora_cdmx())
    )
    conn.commit()
    conn.close()


def get_campanas(solo_usuario=None):
    """Si solo_usuario=None trae todas; si es un username filtra por ese usuario."""
    conn = get_conn()
    where = "WHERE c.usuario=?" if solo_usuario else ""
    params = (solo_usuario,) if solo_usuario else ()
    df = pd.read_sql_query(
        f"""SELECT c.id, c.nombre, c.tipo, c.usuario, c.creado_en,
            COUNT(ce.id) AS total,
            SUM(CASE WHEN ce.estado='enviado' THEN 1 ELSE 0 END) AS exitosos,
            SUM(CASE WHEN ce.estado='error' THEN 1 ELSE 0 END) AS errores
           FROM campanas c
           LEFT JOIN correos_enviados ce ON c.id = ce.campana_id
           {where}
           GROUP BY c.id ORDER BY c.creado_en DESC""",
        conn, params=params
    )
    conn.close()
    return df


def get_correos_campana(campana_id, solo_exitosos=False):
    conn = get_conn()
    q = "SELECT * FROM correos_enviados WHERE campana_id=?"
    if solo_exitosos:
        q += " AND estado='enviado'"
    q += " ORDER BY enviado_en"
    df = pd.read_sql_query(q, conn, params=(campana_id,))
    conn.close()
    return df


def marcar_respondido(correo_id, respondido):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE correos_enviados SET respondido=? WHERE id=?", (int(respondido), correo_id))
    conn.commit()
    conn.close()


def get_seguimientos_por_correo(correo_enviado_id):
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM seguimientos WHERE correo_enviado_id=? ORDER BY enviado_en",
        conn, params=(correo_enviado_id,)
    )
    conn.close()
    return df


def borrar_campana(campana_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM seguimientos WHERE correo_enviado_id IN "
        "(SELECT id FROM correos_enviados WHERE campana_id=?)", (campana_id,)
    )
    c.execute("DELETE FROM correos_enviados WHERE campana_id=?", (campana_id,))
    c.execute("DELETE FROM campanas WHERE id=?", (campana_id,))
    conn.commit()
    conn.close()


def borrar_correo_enviado(correo_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM seguimientos WHERE correo_enviado_id=?", (correo_id,))
    c.execute("DELETE FROM correos_enviados WHERE id=?", (correo_id,))
    conn.commit()
    conn.close()


def leer_respaldo_db():
    with open(DB_PATH, "rb") as f:
        return f.read()


# ─────────────────────────────────────────────
# CORREO Y PLACEHOLDERS
# ─────────────────────────────────────────────

def formatear_multi(valor_str):
    partes = [p.strip() for p in str(valor_str).split(';') if p.strip()]
    if not partes:
        return str(valor_str)
    if len(partes) == 1:
        return partes[0]
    if len(partes) == 2:
        return f"{partes[0]} y {partes[1]}"
    return ", ".join(partes[:-1]) + f" y {partes[-1]}"


def aplicar_placeholders(texto, fila_dict, columnas):
    for col in columnas:
        valor_raw = str(fila_dict.get(col, ''))
        valor = formatear_multi(valor_raw) if ';' in valor_raw else valor_raw
        texto = texto.replace(f"({col})", valor)
    return texto


def validar_placeholders(asunto, cuerpo, columnas):
    encontrados = set(re.findall(r'\(([^)]+)\)', asunto + cuerpo))
    return [p for p in encontrados if p not in columnas]


def generar_plantilla_excel(columnas):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Prospectos"
    ws.append(list(columnas))
    ejemplo = []
    for col in columnas:
        col_lower = str(col).lower()
        if any(x in col_lower for x in ["correo", "email", "mail"]):
            ejemplo.append("ejemplo@empresa.com")
        elif any(x in col_lower for x in ["nombre", "name"]):
            ejemplo.append("Juan Pérez")
        elif any(x in col_lower for x in ["empresa", "company"]):
            ejemplo.append("Empresa Ejemplo")
        else:
            ejemplo.append(f"Ejemplo {col}")
    ws.append(ejemplo)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()


def enviar_correo(destinatarios, asunto, cuerpo, adjuntos=None, username=None):
    remitente, password = get_email_credentials_usuario(username or "")
    if not remitente or not password:
        return False, "Sin credenciales de correo. Configúralas en Mi configuración → Correo."
    try:
        msg = MIMEMultipart()
        msg['From'] = remitente
        msg['To'] = ', '.join(destinatarios)
        msg['Subject'] = asunto
        msg.attach(MIMEText(cuerpo, 'plain', 'utf-8'))
        if adjuntos:
            for adj_bytes, adj_nombre in adjuntos:
                if not adj_bytes or not adj_nombre:
                    continue
                part = MIMEApplication(adj_bytes, Name=adj_nombre)
                part['Content-Disposition'] = f'attachment; filename="{adj_nombre}"'
                msg.attach(part)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as srv:
            srv.login(remitente, password)
            srv.sendmail(remitente, destinatarios, msg.as_string())
        return True, "Enviado."
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

def login_form():
    col_l, col_c, col_r = st.columns([1, 1.2, 1])
    with col_c:
        st.markdown("<br><br><br>", unsafe_allow_html=True)
        st.markdown("""
        <div style="text-align:center; margin-bottom:2.5rem;">
            <div style="
                display:inline-block;
                background:#111111; color:#ff3c00;
                width:52px; height:52px; line-height:52px;
                font-size:24px; margin-bottom:1.2rem;
                font-weight:700;
            ">✉</div>
            <h1 style="
                font-family:'Space Grotesk',sans-serif;
                font-size:2rem; font-weight:700; text-transform:uppercase;
                color:#111111; margin:0 0 0.4rem 0;
                letter-spacing:-0.04em; border:none; padding:0;
            ">Prospección en Frío</h1>
            <p style="font-family:'Space Mono',monospace; color:#999999;
                      font-size:0.7rem; margin:0; letter-spacing:0.1em; text-transform:uppercase;">
                Ingresa tus credenciales
            </p>
        </div>
        """, unsafe_allow_html=True)
        with st.form("form_login"):
            username = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            st.markdown("<br>", unsafe_allow_html=True)
            if st.form_submit_button("Entrar →", type="primary"):
                resultado = verificar_usuario(username, password)
                if resultado:
                    st.session_state.logged_in = True
                    st.session_state.username = username.strip()
                    st.session_state.user_nombre = resultado[1] or username
                    st.session_state.pagina = "Inicio"
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")


def verificar_admin():
    if "is_admin" not in st.session_state:
        st.session_state.is_admin = False
    if not st.session_state.is_admin:
        st.subheader("🔒 Acceso de administrador")
        pwd = st.text_input("Contraseña de administrador", type="password", key="admin_pwd_input")
        if st.button("Entrar como administrador"):
            if hash_pw(pwd) == get_config("admin_password", hash_pw("admin123")):
                st.session_state.is_admin = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
        st.stop()


# ─────────────────────────────────────────────
# INIT
# ─────────────────────────────────────────────

init_db()

# ─── Tema visual Cultural / Experimental ─────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 400;
}

/* ── Fondo: blanco roto con textura ── */
.stApp {
    background-color: #f2f0eb;
    color: #111111;
}

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background-color: #111111 !important;
    border-right: none !important;
}
[data-testid="stSidebar"] * { color: #f2f0eb !important; }
[data-testid="stSidebar"] .stRadio label {
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    letter-spacing: 0.04em !important;
    color: #aaaaaa !important;
}

/* ── Títulos ── */
h1 {
    font-family: 'Space Grotesk', sans-serif !important;
    font-weight: 700 !important;
    font-size: 2.4rem !important;
    color: #111111 !important;
    letter-spacing: -0.04em;
    border-bottom: 3px solid #111111;
    padding-bottom: 0.6rem;
    margin-bottom: 1.4rem !important;
    text-transform: uppercase;
}
h2 {
    font-weight: 700 !important;
    color: #111111 !important;
    text-transform: uppercase;
    letter-spacing: -0.02em;
}
h3 {
    font-family: 'Space Mono', monospace !important;
    font-size: 0.75rem !important;
    font-weight: 400 !important;
    color: #888888 !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
}

/* ── Botón primario: negro total, hover con acento ── */
.stButton > button[kind="primary"] {
    background: #111111 !important;
    color: #f2f0eb !important;
    border: 2px solid #111111 !important;
    border-radius: 0px !important;
    font-weight: 700 !important;
    font-size: 0.78rem !important;
    letter-spacing: 0.12em !important;
    text-transform: uppercase !important;
    padding: 0.55rem 1.6rem !important;
    transition: all 0.15s ease !important;
}
.stButton > button[kind="primary"]:hover {
    background: #ff3c00 !important;
    border-color: #ff3c00 !important;
    color: #ffffff !important;
    transform: none !important;
}

/* ── Botón secundario ── */
.stButton > button[kind="secondary"] {
    background: transparent !important;
    color: #555555 !important;
    border: 2px solid #cccccc !important;
    border-radius: 0px !important;
    font-size: 0.78rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    transition: all 0.15s ease !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color: #111111 !important;
    color: #111111 !important;
}

/* ── Inputs ── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div > div,
.stNumberInput > div > div > input {
    background: #ffffff !important;
    border: 2px solid #cccccc !important;
    border-radius: 0px !important;
    color: #111111 !important;
    font-family: 'Space Grotesk', sans-serif !important;
    font-size: 0.9rem !important;
    transition: border-color 0.15s ease !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {
    border-color: #ff3c00 !important;
    box-shadow: none !important;
}

/* ── Labels ── */
.stTextInput label, .stTextArea label, .stSelectbox label,
.stNumberInput label, .stCheckbox label, .stFileUploader label {
    font-family: 'Space Mono', monospace !important;
    color: #888888 !important;
    font-size: 0.7rem !important;
    font-weight: 400 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
}

/* ── Containers ── */
[data-testid="stVerticalBlock"] > div > div[data-testid="stVerticalBlockBorderWrapper"] {
    background: #ffffff !important;
    border: 2px solid #111111 !important;
    border-radius: 0px !important;
    padding: 1.4rem !important;
}

/* ── Métricas ── */
[data-testid="metric-container"] {
    background: #111111 !important;
    border: none !important;
    border-radius: 0px !important;
    padding: 1.2rem !important;
}
[data-testid="metric-container"] label {
    font-family: 'Space Mono', monospace !important;
    color: #666666 !important;
    font-size: 0.68rem !important;
    text-transform: uppercase !important;
    letter-spacing: 0.1em !important;
}
[data-testid="stMetricValue"] {
    color: #ff3c00 !important;
    font-weight: 700 !important;
    font-size: 2rem !important;
    letter-spacing: -0.03em;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: transparent !important;
    border-bottom: 2px solid #111111 !important;
    gap: 0 !important;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'Space Mono', monospace !important;
    color: #aaaaaa !important;
    font-size: 0.72rem !important;
    font-weight: 400 !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    padding: 0.65rem 1.2rem !important;
    border-bottom: 3px solid transparent !important;
    background: transparent !important;
}
.stTabs [aria-selected="true"] {
    color: #111111 !important;
    border-bottom: 3px solid #ff3c00 !important;
    background: transparent !important;
}

/* ── Dataframes ── */
[data-testid="stDataFrame"] {
    border: 2px solid #111111 !important;
    border-radius: 0px !important;
    overflow: hidden;
}

/* ── Divider ── */
hr {
    border: none !important;
    border-top: 2px solid #dddddd !important;
    margin: 1.8rem 0 !important;
}

/* ── Caption ── */
.stCaption, [data-testid="stCaptionContainer"] {
    font-family: 'Space Mono', monospace !important;
    color: #999999 !important;
    font-size: 0.72rem !important;
}

/* ── File uploader ── */
[data-testid="stFileUploaderDropzone"] {
    background: #ffffff !important;
    border: 2px dashed #cccccc !important;
    border-radius: 0px !important;
}

/* ── Progress bar ── */
.stProgress > div > div > div {
    background: #ff3c00 !important;
    border-radius: 0px !important;
}

/* ── Expander ── */
[data-testid="stExpander"] {
    background: #ffffff !important;
    border: 2px solid #dddddd !important;
    border-radius: 0px !important;
}
[data-testid="stExpander"] summary {
    font-family: 'Space Mono', monospace !important;
    color: #555555 !important;
    font-size: 0.75rem !important;
    text-transform: uppercase !important;
    letter-spacing: 0.08em !important;
}

/* ── Checkbox ── */
[data-testid="stCheckbox"] span {
    color: #333333 !important;
    font-size: 0.88rem !important;
}

/* ── Alertas ── */
[data-testid="stAlert"] {
    border-radius: 0px !important;
    border-left: 3px solid #ff3c00 !important;
}
</style>
""", unsafe_allow_html=True)

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    login_form()
    st.stop()

username_actual = st.session_state.username
is_admin = st.session_state.get("is_admin", False)

# ─────────────────────────────────────────────
# NAVEGACIÓN
# ─────────────────────────────────────────────

if "pagina" not in st.session_state:
    st.session_state.pagina = "Inicio"

st.sidebar.title("📧 Prospección en Frío")
st.sidebar.caption(f"👤 {st.session_state.get('user_nombre', username_actual)}")

pagina_sel = st.sidebar.radio("", PAGINAS, index=PAGINAS.index(st.session_state.pagina))
if pagina_sel != st.session_state.pagina:
    st.session_state.pagina = pagina_sel
pagina = st.session_state.pagina

st.sidebar.divider()
if st.sidebar.button("Cerrar sesión"):
    for k in ["logged_in", "username", "user_nombre", "is_admin", "pagina"]:
        st.session_state.pop(k, None)
    st.rerun()


# ═══════════════════════════════════════════════
# INICIO
# ═══════════════════════════════════════════════

if pagina == "Inicio":
    nombre_display = st.session_state.get('user_nombre', username_actual)
    st.markdown(f"""
    <div style="margin-bottom:0.5rem;">
        <h1 style="margin-bottom:0.2rem;">Bienvenido, {nombre_display}</h1>
        <p style="color:#606080; font-size:0.9rem; margin:0;">
            ¿Qué quieres hacer hoy?
        </p>
    </div>
    """, unsafe_allow_html=True)

    rem, _ = get_email_credentials_usuario(username_actual)
    if not rem:
        st.warning("⚠️ Aún no has configurado tu correo. Ve a **Mi configuración → Correo** para poder enviar.")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("""
        <div style="
            background:#111111; border:none;
            padding:2rem 1.6rem 1.4rem;
        ">
            <div style="font-family:'Space Mono',monospace; font-size:0.65rem;
                        letter-spacing:0.14em; text-transform:uppercase;
                        color:#ff3c00; margin-bottom:0.8rem;">
                01 — Campaña
            </div>
            <div style="font-family:'Space Grotesk',sans-serif; font-weight:700;
                        font-size:1.4rem; color:#f2f0eb; margin-bottom:0.6rem;
                        text-transform:uppercase; letter-spacing:-0.02em;">
                Envío masivo
            </div>
            <div style="color:#666666; font-size:0.84rem; line-height:1.6;">
                Sube un Excel con tus prospectos y envía correos personalizados a todos de un solo clic.
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        if st.button("Ir a Envío masivo →", type="primary", key="btn_masivo"):
            st.session_state.pagina = "Envío masivo"
            st.rerun()
    with col2:
        st.markdown("""
        <div style="
            background:#f2f0eb; border:2px solid #111111;
            padding:2rem 1.6rem 1.4rem;
        ">
            <div style="font-family:'Space Mono',monospace; font-size:0.65rem;
                        letter-spacing:0.14em; text-transform:uppercase;
                        color:#888888; margin-bottom:0.8rem;">
                02 — Prospecto
            </div>
            <div style="font-family:'Space Grotesk',sans-serif; font-weight:700;
                        font-size:1.4rem; color:#111111; margin-bottom:0.6rem;
                        text-transform:uppercase; letter-spacing:-0.02em;">
                Envío individual
            </div>
            <div style="color:#666666; font-size:0.84rem; line-height:1.6;">
                Envía un correo personalizado a un prospecto específico con la plantilla activa.
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        if st.button("Ir a Envío individual →", type="primary", key="btn_individual"):
            st.session_state.pagina = "Envío individual"
            st.rerun()
