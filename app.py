#!/usr/bin/env python3
"""Cámaras IP - Control ligero"""

import os
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
from onvif import ONVIFCamera
from motion_detector import motion_pool

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

DB_PATH = os.getenv('DB_PATH', 'data/cameras.db')
ADMIN_USER = os.getenv('ADMIN_USER')
ADMIN_PASS = os.getenv('ADMIN_PASS')
RTSP_PORT = int(os.getenv('RTSP_PORT', '554'))
GO2RTC_PORT = int(os.getenv('GO2RTC_PORT', '1984'))
DATA_DIR = os.path.dirname(DB_PATH)

NTFY_TOPIC = os.getenv('NTFY_TOPIC', 'motion-alerts')
NTFY_SERVER = os.getenv('NTFY_SERVER', 'https://ntfy.sh')
MOTION_THRESHOLD = int(os.getenv('MOTION_THRESHOLD', '500'))
DETECTION_INTERVAL = int(os.getenv('DETECTION_INTERVAL', '3'))
MIN_MOTION_AREA = float(os.getenv('MIN_MOTION_AREA', '1.5'))
NOTIFICATION_COOLDOWN = int(os.getenv('NOTIFICATION_COOLDOWN', '10'))

if not ADMIN_USER or not ADMIN_PASS:
    raise RuntimeError(
        'ADMIN_USER and ADMIN_PASS environment variables are required. '
        'Copy .env.example to .env and configure your credentials.'
    )


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS cameras (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        ip TEXT NOT NULL,
        port INTEGER DEFAULT 8899,
        user TEXT NOT NULL,
        password TEXT NOT NULL,
        rtsp_port INTEGER DEFAULT 554,
        enabled INTEGER DEFAULT 1,
        motion_enabled INTEGER DEFAULT 0
    )''')
    try:
        conn.execute('ALTER TABLE cameras ADD COLUMN motion_enabled INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def generate_go2rtc_config():
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras WHERE enabled=1').fetchall()
    conn.close()

    streams = {}
    for cam in cameras:
        rtsp_port = cam['rtsp_port'] or RTSP_PORT
        key = f"cam{cam['id']}"
        rtsp_url = f"rtsp://{cam['user']}:{cam['password']}@{cam['ip']}:{rtsp_port}/stream1"
        streams[key] = f"ffmpeg:{rtsp_url}#video=copy#audio=copy"
        streams[f"{key}_sub"] = f"rtsp://{cam['user']}:{cam['password']}@{cam['ip']}:{rtsp_port}/stream2"

    config = {
        'api': {'listen': ':1984'},
        'rtsp': {'listen': ':8554'},
        'webrtc': {'listen': ':8555'},
        'streams': streams,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    config_path = os.path.join(DATA_DIR, 'go2rtc', 'go2rtc.yaml')
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    import yaml
    with open(config_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['user'] == ADMIN_USER and request.form['pass'] == ADMIN_PASS:
            session['logged_in'] = True
            return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras WHERE enabled=1').fetchall()
    conn.close()
    return render_template('index.html', cameras=cameras)


@app.route('/cameras')
@login_required
def cameras():
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras').fetchall()
    conn.close()
    return render_template('cameras.html', cameras=cameras)


@app.route('/api/cameras', methods=['GET'])
@login_required
def api_cameras():
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras').fetchall()
    conn.close()
    return jsonify([dict(c) for c in cameras])


@app.route('/api/cameras', methods=['POST'])
@login_required
def api_add_camera():
    data = request.json
    conn = get_db()
    conn.execute(
        'INSERT INTO cameras (name, ip, port, user, password, rtsp_port) VALUES (?, ?, ?, ?, ?, ?)',
        (data['name'], data['ip'], data.get('port', 8899),
         data['user'], data['password'], data.get('rtsp_port', RTSP_PORT))
    )
    conn.commit()
    conn.close()
    generate_go2rtc_config()
    return jsonify({'ok': True})


@app.route('/api/cameras/<int:id>', methods=['DELETE'])
@login_required
def api_delete_camera(id):
    motion_pool.stop(id)
    conn = get_db()
    conn.execute('DELETE FROM cameras WHERE id=?', (id,))
    conn.commit()
    conn.close()
    generate_go2rtc_config()
    return jsonify({'ok': True})


@app.route('/api/cameras/<int:id>/toggle', methods=['POST'])
@login_required
def api_toggle_camera(id):
    conn = get_db()
    conn.execute('UPDATE cameras SET enabled = NOT enabled WHERE id=?', (id,))
    conn.commit()
    conn.close()
    generate_go2rtc_config()
    return jsonify({'ok': True})


@app.route('/api/ptz/<int:id>/<action>')
@login_required
def api_ptz(id, action):
    conn = get_db()
    cam_data = conn.execute('SELECT * FROM cameras WHERE id=?', (id,)).fetchone()
    conn.close()

    if not cam_data:
        return jsonify({'error': 'Cámara no encontrada'}), 404

    try:
        cam = ONVIFCamera(cam_data['ip'], cam_data['port'], cam_data['user'], cam_data['password'])
        media = cam.create_media_service()
        ptz = cam.create_ptz_service()
        profile = media.GetProfiles()[0]

        speed = float(request.args.get('speed', '0.5'))

        actions = {
            'up': lambda: move(ptz, profile, 0, speed, 0),
            'down': lambda: move(ptz, profile, 0, -speed, 0),
            'left': lambda: move(ptz, profile, -speed, 0, 0),
            'right': lambda: move(ptz, profile, speed, 0, 0),
            'zoom_in': lambda: move(ptz, profile, 0, 0, speed),
            'zoom_out': lambda: move(ptz, profile, 0, 0, -speed),
            'home': lambda: go_home(ptz, profile),
        }

        if action in actions:
            actions[action]()
            return jsonify({'ok': True})
        return jsonify({'error': 'Acción desconocida'}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ptz/<int:id>/status')
@login_required
def api_ptz_status(id):
    conn = get_db()
    cam_data = conn.execute('SELECT * FROM cameras WHERE id=?', (id,)).fetchone()
    conn.close()

    if not cam_data:
        return jsonify({'error': 'Cámara no encontrada'}), 404

    try:
        cam = ONVIFCamera(cam_data['ip'], cam_data['port'], cam_data['user'], cam_data['password'])
        media = cam.create_media_service()
        ptz = cam.create_ptz_service()
        profile = media.GetProfiles()[0]
        status = ptz.GetStatus({'ProfileToken': profile.token})

        pos = status.Position
        pan = pos.PanTilt.x if pos.PanTilt else 0.0
        tilt = pos.PanTilt.y if pos.PanTilt else 0.0
        zoom = pos.Zoom.x if pos.Zoom else 0.0
        moving = status.MoveStatus

        return jsonify({
            'pan': pan, 'tilt': tilt, 'zoom': zoom,
            'pan_tilt_moving': moving.PanTilt if moving else 'IDLE',
            'zoom_moving': moving.Zoom if moving else 'IDLE',
            'limits': {
                'left': pan <= -0.95,
                'right': pan >= 0.95,
                'up': tilt >= 0.95,
                'down': tilt <= -0.95,
                'zoom_in': zoom >= 0.95,
                'zoom_out': zoom <= 0.05,
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rtsp/<int:id>')
@login_required
def api_rtsp(id):
    conn = get_db()
    cam_data = conn.execute('SELECT * FROM cameras WHERE id=?', (id,)).fetchone()
    conn.close()

    if not cam_data:
        return jsonify({'error': 'Cámara no encontrada'}), 404

    rtsp_port = cam_data['rtsp_port'] or RTSP_PORT
    urls = {
        'ch00_0': f"rtsp://{cam_data['ip']}:{rtsp_port}/live/ch00_0",
        'ch00_1': f"rtsp://{cam_data['ip']}:{rtsp_port}/live/ch00_1",
    }
    return jsonify(urls)


@app.route('/api/cameras/<int:id>/motion-status')
@login_required
def api_motion_status(id):
    conn = get_db()
    cam_data = conn.execute('SELECT * FROM cameras WHERE id=?', (id,)).fetchone()
    conn.close()

    if not cam_data:
        return jsonify({'error': 'Cámara no encontrada'}), 404

    enabled = bool(cam_data['motion_enabled'])
    detector_status = motion_pool.get_status(id)

    return jsonify({
        'enabled': enabled,
        'running': motion_pool.is_running(id),
        'detecting': detector_status.get('detecting', False),
        'connected': detector_status.get('connected', False),
        'frame_count': detector_status.get('frame_count', 0),
        'last_motion': detector_status.get('last_motion'),
    })


@app.route('/api/cameras/<int:id>/motion', methods=['POST'])
@login_required
def api_toggle_motion(id):
    conn = get_db()
    cam_data = conn.execute('SELECT * FROM cameras WHERE id=?', (id,)).fetchone()

    if not cam_data:
        conn.close()
        return jsonify({'error': 'Cámara no encontrada'}), 404

    enable = request.json.get('enable', True) if request.json else True

    try:
        if enable:
            config = {
                'ntfy_topic': NTFY_TOPIC,
                'ntfy_server': NTFY_SERVER,
                'motion_threshold': MOTION_THRESHOLD,
                'detection_interval': DETECTION_INTERVAL,
                'min_motion_area': MIN_MOTION_AREA,
                'notification_cooldown': NOTIFICATION_COOLDOWN,
            }
            success = motion_pool.start(id, cam_data, config)
            if not success:
                conn.close()
                return jsonify({'error': 'No se pudo iniciar el detector'}), 500
        else:
            motion_pool.stop(id)

        conn.execute('UPDATE cameras SET motion_enabled = ? WHERE id = ?', (1 if enable else 0, id))
        conn.commit()
        conn.close()

        return jsonify({'ok': True, 'enabled': enable})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


def move(ptz, profile, x, y, zoom, duration=0.3):
    import time
    req = ptz.create_type('ContinuousMove')
    req.ProfileToken = profile.token
    req.Velocity = {'PanTilt': {'x': x, 'y': y}, 'Zoom': {'x': zoom}}
    ptz.ContinuousMove(req)
    time.sleep(duration)
    ptz.Stop({'ProfileToken': profile.token})


def go_home(ptz, profile):
    req = ptz.create_type('AbsoluteMove')
    req.ProfileToken = profile.token
    req.Position = {'PanTilt': {'x': 0, 'y': 0}, 'Zoom': {'x': 0}}
    ptz.AbsoluteMove(req)


def start_enabled_detectors():
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras WHERE motion_enabled=1 AND enabled=1').fetchall()
    conn.close()
    config = {
        'ntfy_topic': NTFY_TOPIC,
        'ntfy_server': NTFY_SERVER,
        'motion_threshold': MOTION_THRESHOLD,
        'detection_interval': DETECTION_INTERVAL,
        'min_motion_area': MIN_MOTION_AREA,
        'notification_cooldown': NOTIFICATION_COOLDOWN,
    }
    for cam in cameras:
        print(f"Iniciando detector para cam {cam['id']} ({cam['name']})")
        motion_pool.start(cam['id'], cam, config)


if __name__ == '__main__':
    init_db()
    generate_go2rtc_config()
    start_enabled_detectors()
    app.run(host='0.0.0.0', port=8080)
