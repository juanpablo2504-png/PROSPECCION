# Prospección en Frío 📧

Plataforma de envío de correos de prospección con campañas, seguimientos y dashboard.

## Cómo correrla

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Credenciales iniciales
- Usuario: `admin`  
- Contraseña: `admin123`
- Contraseña de administrador (sección Admin): `admin123`

**Cambia ambas contraseñas en cuanto entres por primera vez.**

## Configuración del correo
En Administración → Correo, ingresa:
- **Correo remitente**: tu cuenta de Gmail
- **Contraseña de aplicación**: generada en myaccount.google.com/apppasswords

O agrega en Secrets de Streamlit (recomendado para producción):
```toml
[email]
remitente = "tucorreo@gmail.com"
password = "xxxx xxxx xxxx xxxx"
```

## Flujo principal

1. **Envío masivo**: sube un Excel con las columnas configuradas (por defecto: Nombre, Empresa, Correo), escribe el asunto y cuerpo con placeholders `(Nombre)`, `(Empresa)`, etc., adjunta un archivo si quieres, revisa la vista previa y envía.
2. **Envío individual**: formulario con la plantilla pre-cargada, editable por correo.
3. **Dashboard**: métricas generales, por campaña y detalle por destinatario.
4. **Seguimientos**: elige una campaña anterior, selecciona destinatarios (todos o individualmente) y manda un follow-up personalizado con los mismos placeholders.

## Multi-destinatario en una fila
Si un prospecto tiene varios contactos, separa nombres y correos con `;`:
- Nombre: `Juan; Ana`  
- Correo: `juan@x.com; ana@x.com`  
El saludo se genera automáticamente: `Hola Juan y Ana,`  
Para 3+: `Hola Juan, Ana y Pedro,`

## Administración
- **Correo**: configura las credenciales de Gmail.
- **Plantilla**: asunto y cuerpo por defecto para todos los envíos.
- **Columnas del Excel**: agrega, quita o reasigna la columna de correo.
- **Usuarios**: crea, cambia contraseña, activa/desactiva cuentas.
- **Seguridad**: cambia la contraseña de la sección Admin.
- **Respaldo**: descarga/restaura la base de datos completa.
