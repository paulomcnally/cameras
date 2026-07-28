# Cámaras IP - Control ligero

Sistema minimalista para controlar cámaras IP (V380) vía red local.

## Inicio rápido

```bash
# 1. Copiar variables de entorno
cp .env.example .env

# 2. Editar credenciales
nano .env

# 3. Levantar
docker compose up -d

# 4. Abrir http://localhost:8080
```

## Variables de entorno

| Variable | Descripción | Default |
|----------|-------------|---------|
| `ADMIN_USER` | Usuario de acceso | admin |
| `ADMIN_PASS` | Contraseña de acceso | admin123 |
| `DB_PATH` | Ruta de SQLite | /app/data/cameras.db |
| `RTSP_PORT` | Puerto RTSP por defecto | 554 |

## Comandos PTZ soportados

- `up` / `down` / `left` / `right` - Movimiento
- `zoom_in` / `zoom_out` - Zoom
- `home` - Posición inicial

## URLs RTSP típicas V380

```
rtsp://user:pass@ip:554/live/ch00_0
rtsp://user:pass@ip:554/live/ch00_1
rtsp://user:pass@ip:554/onvif1
```
