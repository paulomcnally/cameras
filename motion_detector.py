import cv2
import numpy as np
import requests
import time
import os
import threading
from datetime import datetime
from collections import deque


class MotionDetector:
    FRAME_BUFFER_SIZE = 6

    def __init__(self, cam_id, camera_url, ntfy_topic='motion-alerts',
                 ntfy_server='https://ntfy.sh', motion_threshold=300,
                 detection_interval=2, min_motion_area=2000,
                 notification_cooldown=10):
        self.cam_id = cam_id
        self.camera_url = camera_url
        self.ntfy_topic = ntfy_topic
        self.ntfy_server = ntfy_server
        self.motion_threshold = motion_threshold
        self.detection_interval = detection_interval
        self.min_motion_area = min_motion_area
        self.notification_cooldown = notification_cooldown

        self.running = False
        self.thread = None
        self.cap = None
        self.frame_buffer = deque(maxlen=self.FRAME_BUFFER_SIZE)
        self.last_notification_time = 0
        self.frame_count = 0

        self.status = {
            'detecting': False,
            'last_motion': None,
            'frame_count': 0,
            'connected': False,
        }

    def connect_camera(self):
        try:
            rtsp_url = self.camera_url
            if '?' not in rtsp_url:
                rtsp_url += '?rtsp_transport=tcp'
            self.cap = cv2.VideoCapture(rtsp_url)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ret, frame = self.cap.read()
            if ret and frame is not None:
                self.status['connected'] = True
                print(f"[{datetime.now()}] [Cam {self.cam_id}] Conectada a {self.camera_url}")
                return True
            else:
                print(f"[{datetime.now()}] [Cam {self.cam_id}] No se pudo leer frame")
                self.cap.release()
        except Exception as e:
            print(f"[{datetime.now()}] [Cam {self.cam_id}] Error conexion: {e}")
        self.status['connected'] = False
        return False

    def send_notification(self, title, message, priority='default', tags='warning', image_path=None):
        url = f"{self.ntfy_server}/{self.ntfy_topic}"
        try:
            if image_path and os.path.exists(image_path):
                frame = cv2.imread(image_path)
                if frame is not None:
                    h, w = frame.shape[:2]
                    new_w = 640
                    new_h = int(h * new_w / w)
                    resized = cv2.resize(frame, (new_w, new_h))
                    _, img_encoded = cv2.imencode('.jpg', resized, [cv2.IMWRITE_JPEG_QUALITY, 50])
                    image_data = img_encoded.tobytes()
                else:
                    with open(image_path, 'rb') as f:
                        image_data = f.read()
                requests.put(
                    url, data=image_data,
                    headers={"Title": title, "Priority": priority, "Tags": tags,
                             "Filename": "motion.jpg", "Content-Type": "image/jpeg"},
                    timeout=15
                )
            else:
                requests.post(
                    url, data=message.encode('utf-8'),
                    headers={"Title": title, "Priority": priority, "Tags": tags},
                    timeout=10
                )
            print(f"[{datetime.now()}] [Cam {self.cam_id}] Notificacion: {title}")
        except Exception as e:
            print(f"[{datetime.now()}] [Cam {self.cam_id}] Error notificacion: {e}")

    def process_frame(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (15, 15), 0)
        return gray

    def detect_motion(self, current_frame):
        self.frame_buffer.append(current_frame)

        if len(self.frame_buffer) < self.FRAME_BUFFER_SIZE:
            return False

        old_frame = self.frame_buffer[0]
        frame_diff = cv2.absdiff(old_frame, current_frame)
        mean_diff = float(frame_diff.mean())
        max_diff = int(frame_diff.max())

        if self.frame_count % 5 == 0:
            print(f"[{datetime.now()}] [Cam {self.cam_id}] mean_diff={mean_diff:.1f} max_diff={max_diff} mov={mean_diff > self.min_motion_area}")

        return mean_diff > self.min_motion_area

    def _loop(self):
        print(f"[{datetime.now()}] [Cam {self.cam_id}] Loop iniciado")
        while self.running:
            try:
                self.cap.grab()
                time.sleep(0.3)
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    print(f"[{datetime.now()}] [Cam {self.cam_id}] Frame no leido, reconectando...")
                    self.status['connected'] = False
                    if self.cap:
                        self.cap.release()
                    if not self.connect_camera():
                        time.sleep(10)
                    continue

                self.status['connected'] = True
                current_frame = self.process_frame(frame)
                motion = self.detect_motion(current_frame)
                self.frame_count += 1
                self.status['frame_count'] = self.frame_count

                if motion:
                    self.status['detecting'] = True
                    self.status['last_motion'] = datetime.now().isoformat()
                    current_time = time.time()
                    if current_time - self.last_notification_time > self.notification_cooldown:
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        img_path = f"/tmp/motion_cam{self.cam_id}.jpg"
                        cv2.imwrite(img_path, frame)
                        self.send_notification(
                            f"Movimiento Cam {self.cam_id}!",
                            f"Movimiento detectado a las {ts}",
                            priority='high', tags='rotating_light',
                            image_path=img_path
                        )
                        self.last_notification_time = current_time
                else:
                    self.status['detecting'] = False

                if self.frame_count % 10 == 0:
                    print(f"[{datetime.now()}] [Cam {self.cam_id}] Frames procesados: {self.frame_count}, moviendo: {motion}")

            except Exception as e:
                print(f"[{datetime.now()}] [Cam {self.cam_id}] Error: {e}")
                time.sleep(1)

    def start(self):
        if self.running:
            return True
        if not self.connect_camera():
            return False
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        print(f"[{datetime.now()}] [Cam {self.cam_id}] Detector iniciado")
        return True

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        if self.cap:
            self.cap.release()
            self.cap = None
        self.status = {'detecting': False, 'last_motion': None, 'frame_count': 0, 'connected': False}
        print(f"[{datetime.now()}] [Cam {self.cam_id}] Detector detenido")

    def get_status(self):
        return dict(self.status)


class MotionDetectorPool:
    def __init__(self):
        self.detectors = {}

    def start(self, cam_id, cam_data, config):
        if cam_id in self.detectors and self.detectors[cam_id].running:
            return True

        rtsp_port = cam_data['rtsp_port'] or 554
        camera_url = f"rtsp://{cam_data['user']}:{cam_data['password']}@{cam_data['ip']}:{rtsp_port}/stream1"

        detector = MotionDetector(
            cam_id=cam_id,
            camera_url=camera_url,
            ntfy_topic=config.get('ntfy_topic', 'motion-alerts'),
            ntfy_server=config.get('ntfy_server', 'https://ntfy.sh'),
            motion_threshold=config.get('motion_threshold', 300),
            detection_interval=config.get('detection_interval', 2),
            min_motion_area=config.get('min_motion_area', 2000),
            notification_cooldown=config.get('notification_cooldown', 10),
        )

        if detector.start():
            self.detectors[cam_id] = detector
            return True
        return False

    def stop(self, cam_id):
        if cam_id in self.detectors:
            self.detectors[cam_id].stop()
            del self.detectors[cam_id]

    def get_status(self, cam_id):
        if cam_id in self.detectors:
            return self.detectors[cam_id].get_status()
        return {'detecting': False, 'connected': False, 'frame_count': 0, 'last_motion': None}

    def is_running(self, cam_id):
        return cam_id in self.detectors and self.detectors[cam_id].running

    def stop_all(self):
        for cam_id in list(self.detectors.keys()):
            self.stop(cam_id)


motion_pool = MotionDetectorPool()
