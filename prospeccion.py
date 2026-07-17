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
from email.mime.base import MIMEBase
from email.mime.application import MIMEApplication
from email import encoders
from datetime import date, datetime, timedelta
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
PAGINAS = ["Inicio", "Envío masivo", "Envío individual", "Dashboard", "Seguimientos", "Administración"]


def hoy_cdmx():
    return datetime.now(ZONA_CDMX).date()


def ahora_cdmx():
    return datetime.now(ZONA_CDMX).isoformat()


def hash_pw(password):
    return hashlib.sha256(password.encode()).hexdigest()


# ─────────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────────

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


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

    # Usuario admin por defecto
    c.execute("SELECT COUNT(*) FROM usuarios")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO usuarios (username, password_hash, nombre, creado_en) VALUES (?,?,?,?)",
            ("admin", hash_pw("admin123"), "Administrador", ahora_cdmx())
        )

    # Configuración por defecto
    defaults = {
        "email_remitente": "",
        "email_password": "",
        "columnas_excel": json.dumps(COLUMNAS_DEFAULT),
        "columna_correo": "Correo",
        "asunto_plantilla": ASUNTO_DEFAULT,
        "cuerpo_plantilla": CUERPO_DEFAULT,
        "admin_password": hash_pw("admin123"),
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO configuracion (clave, valor) VALUES (?,?)", (k, v))

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
        "SELECT id, username, nombre, activo, creado_en FROM usuarios ORDER BY username", conn
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


def get_campanas():
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT c.id, c.nombre, c.tipo, c.usuario, c.creado_en,
            COUNT(ce.id) AS total,
            SUM(CASE WHEN ce.estado='enviado' THEN 1 ELSE 0 END) AS exitosos,
            SUM(CASE WHEN ce.estado='error' THEN 1 ELSE 0 END) AS errores
           FROM campanas c
           LEFT JOIN correos_enviados ce ON c.id = ce.campana_id
           GROUP BY c.id ORDER BY c.creado_en DESC""",
        conn
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


def get_seguimientos_por_correo(correo_enviado_id):
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM seguimientos WHERE correo_enviado_id=? ORDER BY enviado_en",
        conn, params=(correo_enviado_id,)
    )
    conn.close()
    return df


def generar_plantilla_excel(columnas):
    """Genera un Excel con los encabezados de las columnas configuradas y una fila de ejemplo."""
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


def leer_respaldo_db():
    with open(DB_PATH, "rb") as f:
        return f.read()


def borrar_campana(campana_id):
    """Borra una campaña y todos sus correos enviados y seguimientos asociados."""
    conn = get_conn()
    c = conn.cursor()
    # Primero borrar seguimientos de los correos de esta campaña
    c.execute(
        "DELETE FROM seguimientos WHERE correo_enviado_id IN "
        "(SELECT id FROM correos_enviados WHERE campana_id=?)",
        (campana_id,)
    )
    c.execute("DELETE FROM correos_enviados WHERE campana_id=?", (campana_id,))
    c.execute("DELETE FROM campanas WHERE id=?", (campana_id,))
    conn.commit()
    conn.close()


def borrar_correo_enviado(correo_id):
    """Borra un correo individual y sus seguimientos."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM seguimientos WHERE correo_enviado_id=?", (correo_id,))
    c.execute("DELETE FROM correos_enviados WHERE id=?", (correo_id,))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# CORREO Y PLACEHOLDERS
# ─────────────────────────────────────────────

def get_email_credentials():
    """Prueba primero secrets de Streamlit, luego la configuración guardada en la DB."""
    try:
        remitente = st.secrets["email"]["remitente"]
        password = st.secrets["email"]["password"]
        if remitente and password:
            return remitente, password
    except Exception:
        pass
    return get_config("email_remitente", ""), get_config("email_password", "")


def generar_plantilla_excel():
    """Genera un Excel vacío con las columnas configuradas listo para rellenar."""
    columnas = get_columnas_excel()
    col_correo = get_config("columna_correo", "Correo")
    df = pd.DataFrame({col: [""] * 5 for col in columnas})
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Prospectos")
        # Ajustar ancho de columnas automáticamente
        ws = writer.sheets["Prospectos"]
        for i, col in enumerate(columnas, start=1):
            ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(i)].width = max(20, len(col) + 5)
        # Agregar nota en la columna de correo
        from openpyxl.styles import PatternFill, Font
        for cell in ws[1]:
            cell.font = Font(bold=True)
        if col_correo in columnas:
            idx = columnas.index(col_correo) + 1
            col_letra = __import__('openpyxl').utils.get_column_letter(idx)
            ws[f"{col_letra}1"].comment = __import__('openpyxl').comments.Comment(
                "Separa múltiples correos con ; (punto y coma)", "Sistema"
            )
    buffer.seek(0)
    return buffer.getvalue()


def formatear_multi(valor_str):
    """Convierte 'Juan; Ana; Pedro' en 'Juan, Ana y Pedro'."""
    partes = [p.strip() for p in str(valor_str).split(';') if p.strip()]
    if not partes:
        return str(valor_str)
    if len(partes) == 1:
        return partes[0]
    if len(partes) == 2:
        return f"{partes[0]} y {partes[1]}"
    return ", ".join(partes[:-1]) + f" y {partes[-1]}"


def aplicar_placeholders(texto, fila_dict, columnas):
    """Reemplaza (Columna) con el valor real de esa columna; si tiene ; los formatea con 'y'."""
    for col in columnas:
        valor_raw = str(fila_dict.get(col, ''))
        valor = formatear_multi(valor_raw) if ';' in valor_raw else valor_raw
        texto = texto.replace(f"({col})", valor)
    return texto


def validar_placeholders(asunto, cuerpo, columnas):
    """Retorna lista de placeholders en el texto que no corresponden a ninguna columna."""
    encontrados = set(re.findall(r'\(([^)]+)\)', asunto + cuerpo))
    return [p for p in encontrados if p not in columnas]


def enviar_correo(destinatarios, asunto, cuerpo, adjuntos=None):
    """Envía un correo de prospección.
    destinatarios: lista de strings de email.
    adjuntos: lista de tuplas (bytes, nombre_archivo) o None.
    """
    remitente, password = get_email_credentials()
    if not remitente or not password:
        return False, "Sin credenciales de correo. Configúralas en Administración → Correo."
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
                # Usar MIMEApplication con Name= para que el nombre del archivo
                # viaje correctamente en todos los clientes (evita el problema de "noname")
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
    col_l, col_c, col_r = st.columns([1, 1.4, 1])
    with col_c:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown("## 📧 Prospección en Frío")
        st.markdown("Ingresa tus credenciales para continuar.")
        with st.form("form_login"):
            username = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Entrar", type="primary"):
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
# INICIALIZAR
# ─────────────────────────────────────────────

init_db()

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    login_form()
    st.stop()

# ─────────────────────────────────────────────
# NAVEGACIÓN
# ─────────────────────────────────────────────

if "pagina" not in st.session_state:
    st.session_state.pagina = "Inicio"

st.sidebar.title("📧 Prospección en Frío")
st.sidebar.caption(f"👤 {st.session_state.get('user_nombre', st.session_state.username)}")

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
# PÁGINA: INICIO
# ═══════════════════════════════════════════════

if pagina == "Inicio":
    st.title(f"Bienvenido, {st.session_state.get('user_nombre', '')} 👋")
    st.markdown("¿Qué quieres hacer hoy?")

    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.markdown("### 📤 Envío masivo")
            st.write("Sube un Excel con tus prospectos y envía correos personalizados a todos de un solo clic.")
            if st.button("Ir a Envío masivo →", type="primary"):
                st.session_state.pagina = "Envío masivo"
                st.rerun()
    with col2:
        with st.container(border=True):
            st.markdown("### ✉️ Envío individual")
            st.write("Envía un correo personalizado a un prospecto específico usando la plantilla activa.")
            if st.button("Ir a Envío individual →", type="primary"):
                st.session_state.pagina = "Envío individual"
                st.rerun()

    st.divider()

    campanas_df = get_campanas()
    if not campanas_df.empty:
        total_enviados = int(campanas_df['total'].sum())
        total_exitosos = int(campanas_df['exitosos'].sum())
        total_campanas = len(campanas_df)
        tasa = (total_exitosos / total_enviados * 100) if total_enviados else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Campañas", total_campanas)
        m2.metric("Correos enviados", total_enviados)
        m3.metric("Exitosos", total_exitosos)
        m4.metric("Tasa de éxito", f"{tasa:.1f}%")

        st.caption("Última campaña: " + campanas_df.iloc[0]['nombre'])
    else:
        st.info("Todavía no hay campañas registradas. ¡Manda tu primer correo!")


# ═══════════════════════════════════════════════
# PÁGINA: ENVÍO MASIVO
# ═══════════════════════════════════════════════

elif pagina == "Envío masivo":
    st.title("📤 Envío masivo")

    columnas = get_columnas_excel()
    col_correo = get_config("columna_correo", "Correo")
    asunto_plantilla = get_config("asunto_plantilla", ASUNTO_DEFAULT)
    cuerpo_plantilla = get_config("cuerpo_plantilla", CUERPO_DEFAULT)
    cols_sin_correo = [c for c in columnas if c != col_correo]

    st.info(
        f"El Excel debe tener las columnas: **{', '.join(columnas)}**. "
        f"La columna **{col_correo}** es la dirección de destino. "
        "Si un prospecto tiene varios contactos, separa nombres y correos con `;` en la misma celda."
    )

    # Botón para descargar la plantilla
    plantilla_bytes = generar_plantilla_excel()
    st.download_button(
        "⬇️ Descargar plantilla Excel",
        plantilla_bytes,
        file_name="plantilla_prospectos.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Descarga un Excel con las columnas ya listas para rellenar con tus prospectos."
    )

    # ── 1. Nombre de campaña ──
    st.subheader("1. Nombre de la campaña")
    nombre_campana = st.text_input("", placeholder="Ej: Prospección Julio 2026 — PYMES CDMX")

    # ── 2. Excel ──
    st.subheader("2. Excel de prospectos")

    col_desc, col_btn = st.columns([3, 1])
    with col_desc:
        st.caption(f"Columnas requeridas: **{', '.join(columnas)}**. ¿No tienes el Excel listo? Descarga la plantilla →")
    with col_btn:
        st.download_button(
            "⬇️ Plantilla Excel",
            generar_plantilla_excel(columnas),
            file_name="plantilla_prospectos.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    archivo_excel = st.file_uploader("Sube tu Excel lleno", type=["xlsx"])

    df_prospectos = None
    if archivo_excel:
        try:
            df_prospectos = pd.read_excel(archivo_excel, engine="openpyxl")
            df_prospectos.columns = [str(c).strip() for c in df_prospectos.columns]
            df_prospectos = df_prospectos.dropna(how="all").reset_index(drop=True)
            faltantes = [c for c in columnas if c not in df_prospectos.columns]
            if faltantes:
                st.error(f"Al Excel le faltan las columnas: **{', '.join(faltantes)}**. Ajústalas en Administración → Columnas o revisa el archivo.")
                df_prospectos = None
            else:
                st.success(f"✅ Excel cargado: **{len(df_prospectos)}** prospecto(s).")
                if len(df_prospectos) > 400:
                    st.warning(f"⚠️ {len(df_prospectos)} filas — Gmail permite ~500 correos diarios. Considera dividir este envío en varias campañas.")
                st.dataframe(df_prospectos[columnas].head(5))
        except Exception as e:
            st.error(f"No pude leer el archivo: {e}")

    # ── 3. Asunto y cuerpo ──
    st.subheader("3. Asunto y cuerpo")
    st.caption(f"Placeholders disponibles: {' · '.join(f'**({c})**' for c in cols_sin_correo)}")
    asunto = st.text_input("Asunto", value=asunto_plantilla)
    cuerpo = st.text_area("Cuerpo del correo", value=cuerpo_plantilla, height=260)

    invalidos = validar_placeholders(asunto, cuerpo, columnas)
    if invalidos:
        st.warning(f"Estos placeholders no coinciden con ninguna columna: {', '.join(f'({p})' for p in invalidos)}. Verifica que estén bien escritos.")

    # ── 4. Adjuntos ──
    st.subheader("4. Adjuntos (opcional)")
    adjuntos_masivo = st.file_uploader(
        "Puedes subir uno o varios archivos — se envían iguales a todos",
        type=None, key="adjunto_masivo", accept_multiple_files=True
    )

    # ── 5. Preview + Enviar ──
    if df_prospectos is not None and nombre_campana.strip() and not invalidos:
        st.subheader("5. Vista previa y envío")
        primera_fila = df_prospectos.iloc[0].to_dict()
        asunto_prev = aplicar_placeholders(asunto, primera_fila, columnas)
        cuerpo_prev = aplicar_placeholders(cuerpo, primera_fila, columnas)

        with st.expander(f"👁 Preview — primer prospecto: {formatear_multi(str(primera_fila.get(cols_sin_correo[0] if cols_sin_correo else col_correo, '')))}", expanded=True):
            st.markdown(f"**Asunto:** {asunto_prev}")
            if adjuntos_masivo:
                st.caption(f"📎 {len(adjuntos_masivo)} adjunto(s): {', '.join(a.name for a in adjuntos_masivo)}")
            st.divider()
            st.text(cuerpo_prev)

        rem, _ = get_email_credentials()
        if not rem:
            st.error("No hay credenciales de correo configuradas. Ve a **Administración → Correo** para configurarlas.")
        else:
            if st.button(f"🚀 Enviar a {len(df_prospectos)} prospecto(s)", type="primary"):
                lista_adjuntos = [(a.read(), a.name) for a in adjuntos_masivo] if adjuntos_masivo else []
                nombres_adj = ', '.join(a.name for a in adjuntos_masivo) if adjuntos_masivo else None
                campana_id = crear_campana(
                    nombre_campana.strip(), asunto, cuerpo, "masivo",
                    st.session_state.username, adjunto_nombre=nombres_adj
                )
                barra = st.progress(0, text="Iniciando envío...")
                enviados, errores = 0, 0
                for i, (_, fila) in enumerate(df_prospectos.iterrows()):
                    fila_dict = fila.to_dict()
                    correos_str = str(fila_dict.get(col_correo, ""))
                    correos_lista = [c.strip() for c in correos_str.split(';') if c.strip()]
                    if not correos_lista:
                        registrar_correo(campana_id, "", correos_str, "", "", "error",
                                        fila_datos=fila_dict, error="Sin correo en esta fila")
                        errores += 1
                        continue
                    asunto_f = aplicar_placeholders(asunto, fila_dict, columnas)
                    cuerpo_f = aplicar_placeholders(cuerpo, fila_dict, columnas)
                    nombres_f = formatear_multi(str(fila_dict.get(cols_sin_correo[0], ""))) if cols_sin_correo else correos_str
                    ok, msg = enviar_correo(correos_lista, asunto_f, cuerpo_f, adjuntos=lista_adjuntos)
                    registrar_correo(campana_id, nombres_f, correos_str, asunto_f, cuerpo_f,
                                    "enviado" if ok else "error", fila_datos=fila_dict, error=None if ok else msg)
                    enviados += (1 if ok else 0)
                    errores += (0 if ok else 1)
                    barra.progress((i + 1) / len(df_prospectos), text=f"Enviando {i+1}/{len(df_prospectos)}…")
                    time.sleep(0.3)
                barra.empty()
                if errores == 0:
                    st.success(f"✅ Campaña completada. {enviados} correo(s) enviado(s) exitosamente.")
                else:
                    st.warning(f"Campaña completada. ✅ {enviados} enviados · ❌ {errores} con error. Revisa el Dashboard para detalles.")
    elif df_prospectos is not None and not nombre_campana.strip():
        st.info("Escribe un nombre para la campaña antes de continuar.")


# ═══════════════════════════════════════════════
# PÁGINA: ENVÍO INDIVIDUAL
# ═══════════════════════════════════════════════

elif pagina == "Envío individual":
    st.title("✉️ Envío individual")

    columnas = get_columnas_excel()
    col_correo = get_config("columna_correo", "Correo")
    cols_sin_correo = [c for c in columnas if c != col_correo]
    asunto_plantilla = get_config("asunto_plantilla", ASUNTO_DEFAULT)
    cuerpo_plantilla = get_config("cuerpo_plantilla", CUERPO_DEFAULT)

    st.caption(f"Placeholders disponibles: {' · '.join(f'**({c})**' for c in cols_sin_correo)}")

    st.subheader("Datos del prospecto")
    campos = {}
    n_cols = min(len(columnas), 3)
    col_inputs = st.columns(n_cols)
    for idx, col in enumerate(columnas):
        with col_inputs[idx % n_cols]:
            if col == col_correo:
                campos[col] = st.text_input(f"{col} (separa con ; si son varios)", key=f"ind_{col}")
            else:
                campos[col] = st.text_input(col, key=f"ind_{col}")

    st.subheader("Asunto y cuerpo")
    asunto_ind = st.text_input("Asunto", value=asunto_plantilla, key="asunto_ind")
    cuerpo_ind = st.text_area("Cuerpo", value=cuerpo_plantilla, height=250, key="cuerpo_ind")
    adjuntos_ind = st.file_uploader("Adjunto(s) (opcional)", key="adjunto_ind", accept_multiple_files=True)

    correo_ingresado = campos.get(col_correo, "").strip()
    if correo_ingresado:
        asunto_prev = aplicar_placeholders(asunto_ind, campos, columnas)
        cuerpo_prev = aplicar_placeholders(cuerpo_ind, campos, columnas)
        nombre_prev = formatear_multi(campos.get(cols_sin_correo[0], "")) if cols_sin_correo else correo_ingresado

        with st.expander(f"👁 Vista previa — {nombre_prev}", expanded=True):
            st.markdown(f"**Asunto:** {asunto_prev}")
            if adjuntos_ind:
                st.caption(f"📎 {len(adjuntos_ind)} adjunto(s): {', '.join(a.name for a in adjuntos_ind)}")
            st.divider()
            st.text(cuerpo_prev)

        rem, _ = get_email_credentials()
        if not rem:
            st.error("Sin credenciales de correo. Ve a Administración → Correo.")
        else:
            if st.button("📤 Enviar correo", type="primary"):
                correos_lista = [c.strip() for c in correo_ingresado.split(';') if c.strip()]
                lista_adj = [(a.read(), a.name) for a in adjuntos_ind] if adjuntos_ind else []
                nombres_adj = ', '.join(a.name for a in adjuntos_ind) if adjuntos_ind else None
                campana_id = crear_campana(
                    f"Individual — {nombre_prev} — {hoy_cdmx()}",
                    asunto_ind, cuerpo_ind, "individual",
                    st.session_state.username, adjunto_nombre=nombres_adj
                )
                ok, msg = enviar_correo(correos_lista, asunto_prev, cuerpo_prev, adjuntos=lista_adj)
                registrar_correo(
                    campana_id, nombre_prev, correo_ingresado,
                    asunto_prev, cuerpo_prev,
                    "enviado" if ok else "error",
                    fila_datos=campos, error=None if ok else msg
                )
                if ok:
                    st.success(f"✅ Correo enviado a {', '.join(correos_lista)}.")
                else:
                    st.error(f"❌ Error al enviar: {msg}")
    else:
        st.info(f"Llena el campo **{col_correo}** para ver la vista previa y enviar.")


# ═══════════════════════════════════════════════
# PÁGINA: DASHBOARD
# ═══════════════════════════════════════════════

elif pagina == "Dashboard":
    st.title("📊 Dashboard")

    campanas_df = get_campanas()

    if campanas_df.empty:
        st.info("Todavía no hay campañas registradas.")
        st.stop()

    total_campanas = len(campanas_df)
    total_enviados = int(campanas_df['total'].sum())
    total_exitosos = int(campanas_df['exitosos'].sum())
    total_errores = int(campanas_df['errores'].sum())
    tasa = (total_exitosos / total_enviados * 100) if total_enviados else 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Campañas", total_campanas)
    m2.metric("Correos enviados", total_enviados)
    m3.metric("Exitosos", total_exitosos)
    m4.metric("Con error", total_errores)
    m5.metric("Tasa de éxito", f"{tasa:.1f}%")

    # Gráfica por día
    conn = get_conn()
    df_por_dia = pd.read_sql_query(
        """SELECT DATE(enviado_en) AS dia,
           COUNT(*) AS total,
           SUM(CASE WHEN estado='enviado' THEN 1 ELSE 0 END) AS exitosos
           FROM correos_enviados
           GROUP BY dia ORDER BY dia""",
        conn
    )
    conn.close()

    if not df_por_dia.empty:
        st.subheader("Envíos por día")
        fig = px.bar(df_por_dia, x="dia", y="exitosos",
                     labels={"dia": "Día", "exitosos": "Correos enviados"},
                     color_discrete_sequence=["#1f77b4"])
        st.plotly_chart(fig)

    # Tabla de campañas
    st.subheader("Campañas")
    tabla = campanas_df[["nombre", "tipo", "usuario", "total", "exitosos", "errores", "creado_en"]].rename(columns={
        "nombre": "Campaña", "tipo": "Tipo", "usuario": "Usuario",
        "total": "Total", "exitosos": "✅ Exitosos", "errores": "❌ Errores", "creado_en": "Fecha"
    })
    st.dataframe(tabla)

    # Detalle por campaña
    st.subheader("Detalle de una campaña")
    campana_id_sel = st.selectbox(
        "Selecciona campaña",
        campanas_df["id"].tolist(),
        format_func=lambda x: campanas_df.loc[campanas_df["id"] == x, "nombre"].iloc[0]
    )
    df_correos = get_correos_campana(campana_id_sel)
    if not df_correos.empty:
        mostrar = df_correos[["nombres", "correos", "estado", "enviado_en", "error"]].rename(columns={
            "nombres": "Nombre", "correos": "Correo(s)", "estado": "Estado",
            "enviado_en": "Enviado", "error": "Error"
        })
        st.dataframe(mostrar)

        # Seguimientos de esta campaña
        total_seg = 0
        for _, row in df_correos.iterrows():
            segs = get_seguimientos_por_correo(int(row["id"]))
            total_seg += len(segs)
        if total_seg > 0:
            st.caption(f"Esta campaña tiene {total_seg} seguimiento(s) registrado(s).")
    else:
        st.info("Esta campaña no tiene correos registrados.")


# ═══════════════════════════════════════════════
# PÁGINA: SEGUIMIENTOS
# ═══════════════════════════════════════════════

elif pagina == "Seguimientos":
    st.title("🔄 Seguimientos")

    campanas_df = get_campanas()
    if campanas_df.empty:
        st.info("Todavía no hay campañas para hacer seguimiento.")
        st.stop()

    campanas_con_enviados = campanas_df[campanas_df["exitosos"] > 0]
    if campanas_con_enviados.empty:
        st.info("No hay campañas con correos enviados exitosamente todavía.")
        st.stop()

    campana_id_sel = st.selectbox(
        "Campaña de origen",
        campanas_con_enviados["id"].tolist(),
        format_func=lambda x: campanas_con_enviados.loc[campanas_con_enviados["id"] == x, "nombre"].iloc[0]
    )

    df_exitosos = get_correos_campana(campana_id_sel, solo_exitosos=True)

    if df_exitosos.empty:
        st.info("No hay correos exitosos en esta campaña.")
        st.stop()

    # Marcar cuáles ya tienen seguimiento
    df_exitosos = df_exitosos.copy()
    df_exitosos["n_seguimientos"] = df_exitosos["id"].apply(
        lambda i: len(get_seguimientos_por_correo(int(i)))
    )

    st.subheader("Destinatarios")
    seleccionar_todos = st.checkbox("Seleccionar todos", value=True)

    if seleccionar_todos:
        ids_sel = df_exitosos["id"].tolist()
    else:
        def label_correo(row_id):
            row = df_exitosos[df_exitosos["id"] == row_id].iloc[0]
            seg_txt = f" · {row['n_seguimientos']} seg." if row['n_seguimientos'] > 0 else ""
            return f"{row['nombres']} — {row['correos']}{seg_txt}"

        ids_sel = st.multiselect(
            "Destinatarios",
            options=df_exitosos["id"].tolist(),
            default=df_exitosos["id"].tolist(),
            format_func=label_correo
        )

    st.caption(f"{len(ids_sel)} destinatario(s) seleccionado(s).")

    # Resumen de quiénes ya tienen seguimiento
    ya_con_seg = df_exitosos[df_exitosos["n_seguimientos"] > 0]
    if not ya_con_seg.empty:
        st.info(f"ℹ️ {len(ya_con_seg)} de ellos ya recibieron al menos un seguimiento antes.")

    columnas = get_columnas_excel()
    col_correo = get_config("columna_correo", "Correo")
    cols_sin_correo = [c for c in columnas if c != col_correo]

    st.subheader("Correo de seguimiento")
    st.caption(f"Placeholders disponibles: {' · '.join(f'**({c})**' for c in cols_sin_correo)}")

    asunto_seg = st.text_input("Asunto", placeholder="Ej: Seguimiento a mi correo sobre (Empresa)", key="asunto_seg")
    cuerpo_seg = st.text_area("Cuerpo", height=220, key="cuerpo_seg",
                              placeholder="Hola (Nombre),\n\nQuería hacer seguimiento a mi correo anterior...")
    adjuntos_seg = st.file_uploader("Adjunto(s) (opcional)", key="adj_seg", accept_multiple_files=True)

    invalidos_seg = validar_placeholders(asunto_seg, cuerpo_seg, columnas) if asunto_seg and cuerpo_seg else []
    if invalidos_seg:
        st.warning(f"Placeholders no reconocidos: {', '.join(f'({p})' for p in invalidos_seg)}")

    if ids_sel and asunto_seg and cuerpo_seg and not invalidos_seg:
        if st.button(f"📤 Enviar seguimiento a {len(ids_sel)} destinatario(s)", type="primary"):
            lista_adj_seg = [(a.read(), a.name) for a in adjuntos_seg] if adjuntos_seg else []
            barra = st.progress(0)
            enviados, errores = 0, 0
            for i, id_correo in enumerate(ids_sel):
                fila_correo = df_exitosos[df_exitosos["id"] == id_correo].iloc[0]
                fila_datos = {}
                raw_datos = fila_correo.get("fila_datos")
                if isinstance(raw_datos, str) and raw_datos.strip():
                    try:
                        fila_datos = json.loads(raw_datos)
                    except Exception:
                        fila_datos = {}
                if not fila_datos:
                    fila_datos = {"Nombre": fila_correo["nombres"], col_correo: fila_correo["correos"]}
                asunto_f = aplicar_placeholders(asunto_seg, fila_datos, columnas)
                cuerpo_f = aplicar_placeholders(cuerpo_seg, fila_datos, columnas)
                correos_lista = [c.strip() for c in fila_correo["correos"].split(';') if c.strip()]
                ok, msg = enviar_correo(correos_lista, asunto_f, cuerpo_f, adjuntos=lista_adj_seg)
                registrar_seguimiento(int(id_correo), asunto_f, cuerpo_f, "enviado" if ok else "error", None if ok else msg)
                enviados += (1 if ok else 0)
                errores += (0 if ok else 1)
                barra.progress((i + 1) / len(ids_sel))
                time.sleep(0.3)
            barra.empty()
            if errores == 0:
                st.success(f"✅ {enviados} seguimiento(s) enviado(s) correctamente.")
            else:
                st.warning(f"✅ {enviados} enviados · ❌ {errores} con error.")


# ═══════════════════════════════════════════════
# PÁGINA: ADMINISTRACIÓN
# ═══════════════════════════════════════════════

elif pagina == "Administración":
    verificar_admin()
    st.title("⚙️ Administración")

    tab_correo, tab_plantilla, tab_columnas, tab_usuarios, tab_seguridad, tab_limpiar, tab_respaldo = st.tabs([
        "Correo", "Plantilla", "Columnas del Excel", "Usuarios", "Seguridad", "Limpiar datos", "Respaldo"
    ])

    # ── Correo ──
    with tab_correo:
        st.subheader("Configuración de correo saliente (Gmail)")
        st.caption(
            "Para Gmail, usa la dirección de tu cuenta y una **Contraseña de aplicación** "
            "(no tu contraseña normal). Activa la verificación en 2 pasos y genera una en "
            "myaccount.google.com/apppasswords. También puedes configurarlo en Secrets de Streamlit "
            "bajo [email] / remitente / password."
        )
        remitente_actual = get_config("email_remitente", "")
        password_actual = get_config("email_password", "")
        nuevo_remitente = st.text_input("Correo remitente", value=remitente_actual)
        nuevo_password = st.text_input("Contraseña de aplicación", value=password_actual, type="password")
        if st.button("Guardar configuración de correo", key="btn_guardar_correo"):
            set_config("email_remitente", nuevo_remitente.strip())
            set_config("email_password", nuevo_password.strip())
            st.success("Configuración guardada.")

    # ── Plantilla ──
    with tab_plantilla:
        st.subheader("Plantilla de correo por defecto")
        columnas = get_columnas_excel()
        col_correo = get_config("columna_correo", "Correo")
        cols_sin_correo = [c for c in columnas if c != col_correo]
        st.caption(
            "Esta plantilla se carga automáticamente en los envíos. Puedes editarla en cada campaña "
            "sin que cambie la guardada aquí.\n\n"
            f"Placeholders disponibles: {' · '.join(f'**({c})**' for c in cols_sin_correo)}"
        )
        nuevo_asunto = st.text_input("Asunto por defecto", value=get_config("asunto_plantilla", ASUNTO_DEFAULT))
        nuevo_cuerpo = st.text_area("Cuerpo por defecto", value=get_config("cuerpo_plantilla", CUERPO_DEFAULT), height=300)
        if st.button("Guardar plantilla", type="primary", key="btn_guardar_plantilla"):
            set_config("asunto_plantilla", nuevo_asunto)
            set_config("cuerpo_plantilla", nuevo_cuerpo)
            st.success("Plantilla guardada.")

    # ── Columnas del Excel ──
    with tab_columnas:
        st.subheader("Columnas del Excel de prospectos")
        columnas = get_columnas_excel()
        col_correo = get_config("columna_correo", "Correo")
        st.dataframe(
            pd.DataFrame({
                "Columna": columnas,
                "Es columna de correo": [c == col_correo for c in columnas]
            }),
            
        )
        st.markdown("**Agregar columna**")
        c1, c2 = st.columns([3, 1])
        with c1:
            nueva_col = st.text_input("Nombre", key="nueva_col_excel")
        with c2:
            st.write("")
            if st.button("Agregar", key="btn_agregar_col"):
                nc = nueva_col.strip()
                if nc and nc not in columnas:
                    columnas.append(nc)
                    set_config("columnas_excel", json.dumps(columnas))
                    st.success("Columna agregada.")
                    st.rerun()
                elif nc in columnas:
                    st.error("Ya existe.")
        if len(columnas) > 1:
            st.markdown("**Quitar columna**")
            c1, c2 = st.columns([3, 1])
            with c1:
                col_quitar = st.selectbox("Columna a quitar", columnas, key="col_quitar")
            with c2:
                st.write("")
                if st.button("Quitar", type="secondary", key="btn_quitar_col"):
                    if col_quitar == col_correo:
                        st.error("No puedes quitar la columna de correo. Asigna primero otra como columna de correo.")
                    else:
                        columnas.remove(col_quitar)
                        set_config("columnas_excel", json.dumps(columnas))
                        st.success("Columna eliminada.")
                        st.rerun()
        st.markdown("**Columna que contiene el correo de destino**")
        c1, c2 = st.columns([3, 1])
        with c1:
            nueva_col_correo = st.selectbox(
                "Columna de correo",
                columnas,
                index=columnas.index(col_correo) if col_correo in columnas else 0,
                key="sel_col_correo"
            )
        with c2:
            st.write("")
            if st.button("Guardar", key="btn_col_correo"):
                set_config("columna_correo", nueva_col_correo)
                st.success(f"'{nueva_col_correo}' es ahora la columna de correo.")
                st.rerun()

    # ── Usuarios ──
    with tab_usuarios:
        st.subheader("Usuarios")
        usuarios_df = get_usuarios()
        st.dataframe(
            usuarios_df[["username", "nombre", "activo", "creado_en"]].rename(columns={
                "username": "Usuario", "nombre": "Nombre", "activo": "Activo", "creado_en": "Creado"
            }),
            
        )
        st.markdown("**Crear nuevo usuario**")
        c1, c2, c3 = st.columns(3)
        with c1:
            new_username = st.text_input("Usuario", key="new_username")
        with c2:
            new_nombre = st.text_input("Nombre completo", key="new_nombre")
        with c3:
            new_password = st.text_input("Contraseña inicial", type="password", key="new_password")
        if st.button("Crear usuario", key="btn_crear_usuario"):
            if new_username.strip() and new_password.strip():
                ok, msg = crear_usuario(new_username, new_password, new_nombre)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
            else:
                st.error("Usuario y contraseña son obligatorios.")
        st.markdown("**Cambiar contraseña**")
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            user_pwd = st.selectbox("Usuario", usuarios_df["username"].tolist(), key="sel_user_pwd")
        with c2:
            nueva_pwd_u = st.text_input("Nueva contraseña", type="password", key="nueva_pwd_u")
        with c3:
            st.write("")
            if st.button("Cambiar", key="btn_cambiar_pwd"):
                if nueva_pwd_u.strip():
                    uid = int(usuarios_df.loc[usuarios_df["username"] == user_pwd, "id"].iloc[0])
                    cambiar_password_usuario(uid, nueva_pwd_u)
                    st.success("Contraseña actualizada.")
                else:
                    st.error("Escribe la nueva contraseña.")
        st.markdown("**Activar / Desactivar usuario**")
        c1, c2 = st.columns([3, 1])
        with c1:
            user_toggle = st.selectbox("Usuario", usuarios_df["username"].tolist(), key="sel_user_toggle")
        with c2:
            activo_actual = bool(usuarios_df.loc[usuarios_df["username"] == user_toggle, "activo"].iloc[0])
            st.write("")
            if st.button("Desactivar" if activo_actual else "Activar", key="btn_toggle_user"):
                uid = int(usuarios_df.loc[usuarios_df["username"] == user_toggle, "id"].iloc[0])
                toggle_usuario_activo(uid, not activo_actual)
                st.success(f"Usuario {'desactivado' if activo_actual else 'activado'}.")
                st.rerun()

    # ── Seguridad ──
    with tab_seguridad:
        st.subheader("Contraseña de Administración")
        nueva_admin_pwd = st.text_input("Nueva contraseña de administración", type="password")
        if st.button("Actualizar", key="btn_admin_pwd"):
            if nueva_admin_pwd.strip():
                set_config("admin_password", hash_pw(nueva_admin_pwd.strip()))
                st.success("Contraseña de administración actualizada.")
            else:
                st.error("La contraseña no puede estar vacía.")
        st.divider()
        if st.button("Cerrar sesión de administrador", key="btn_cerrar_admin"):
            st.session_state.is_admin = False
            st.rerun()

    # ── Limpiar datos ──
    with tab_limpiar:
        st.subheader("Borrar campañas o correos")
        st.caption(
            "Útil para eliminar pruebas o campañas que ya no necesitas. "
            "Al borrar una campaña se eliminan también todos sus correos enviados y seguimientos."
        )

        campanas_df_admin = get_campanas()

        if campanas_df_admin.empty:
            st.info("No hay campañas registradas todavía.")
        else:
            st.markdown("**Borrar una campaña completa**")
            campana_borrar_id = st.selectbox(
                "Campaña a borrar",
                campanas_df_admin["id"].tolist(),
                format_func=lambda x: (
                    f"{campanas_df_admin.loc[campanas_df_admin['id']==x,'nombre'].iloc[0]} "
                    f"({int(campanas_df_admin.loc[campanas_df_admin['id']==x,'total'].iloc[0])} correos)"
                ),
                key="sel_campana_borrar"
            )
            nombre_sel = campanas_df_admin.loc[campanas_df_admin["id"] == campana_borrar_id, "nombre"].iloc[0]
            total_sel = int(campanas_df_admin.loc[campanas_df_admin["id"] == campana_borrar_id, "total"].iloc[0])

            st.warning(
                f"Esto borrará la campaña **{nombre_sel}** y sus {total_sel} correo(s) "
                "enviados (y sus seguimientos). No se puede deshacer."
            )
            confirmar_borrar = st.checkbox(f"Confirmo que quiero borrar '{nombre_sel}'", key="chk_borrar_campana")
            if st.button("🗑️ Borrar campaña", type="secondary", disabled=not confirmar_borrar, key="btn_borrar_campana"):
                borrar_campana(int(campana_borrar_id))
                st.success(f"Campaña '{nombre_sel}' eliminada.")
                st.rerun()

            st.divider()

            st.markdown("**Borrar un correo individual de una campaña**")
            campana_detalle_id = st.selectbox(
                "Campaña",
                campanas_df_admin["id"].tolist(),
                format_func=lambda x: campanas_df_admin.loc[campanas_df_admin["id"]==x, "nombre"].iloc[0],
                key="sel_campana_detalle_borrar"
            )
            df_correos_admin = get_correos_campana(campana_detalle_id)
            if df_correos_admin.empty:
                st.info("Esta campaña no tiene correos registrados.")
            else:
                correo_borrar_id = st.selectbox(
                    "Correo a borrar",
                    df_correos_admin["id"].tolist(),
                    format_func=lambda x: (
                        f"{df_correos_admin.loc[df_correos_admin['id']==x,'nombres'].iloc[0]} — "
                        f"{df_correos_admin.loc[df_correos_admin['id']==x,'correos'].iloc[0]} "
                        f"({df_correos_admin.loc[df_correos_admin['id']==x,'estado'].iloc[0]})"
                    ),
                    key="sel_correo_borrar"
                )
                if st.button("🗑️ Borrar este correo", type="secondary", key="btn_borrar_correo"):
                    borrar_correo_enviado(int(correo_borrar_id))
                    st.success("Correo eliminado.")
                    st.rerun()

    # ── Respaldo ──
    with tab_respaldo:
        st.subheader("Respaldo de la base de datos")
        st.caption("Descarga una copia de toda la información: campañas, correos enviados, seguimientos, usuarios y configuración.")
        try:
            st.download_button(
                "⬇️ Descargar respaldo completo (.db)",
                leer_respaldo_db(),
                file_name=f"respaldo_prospeccion_{hoy_cdmx().isoformat()}.db",
                mime="application/octet-stream"
            )
        except FileNotFoundError:
            st.info("La base de datos todavía no existe.")
        st.divider()
        st.subheader("Restaurar desde respaldo")
        st.warning("Esto reemplaza todos los datos actuales.")
        archivo_respaldo = st.file_uploader("Sube el archivo .db de respaldo", type=["db"], key="subir_respaldo")
        if archivo_respaldo:
            if st.checkbox("Entiendo que esto reemplaza todos los datos actuales."):
                if st.button("Restaurar", type="primary", key="btn_restaurar"):
                    try:
                        contenido = archivo_respaldo.read()
                        with open(DB_PATH, "wb") as f:
                            f.write(contenido)
                        get_conn().execute("SELECT COUNT(*) FROM correos_enviados")
                        st.success("Respaldo restaurado correctamente.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"No pude restaurar: {e}")
