import os, re, io, csv, time, shutil, threading, sqlite3, base64
from datetime import datetime, date
from collections import deque

import cv2
import numpy as np
from flask import Flask, render_template, jsonify, request, Response, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "database.db")
FACES_DIR = os.path.join(BASE, "data", "faces")
ATT_DIR = os.path.join(BASE, "attendance")
os.makedirs(FACES_DIR, exist_ok=True)
os.makedirs(ATT_DIR, exist_ok=True)

# 0 = laptop webcam. For CCTV use an RTSP URL, e.g.
#   Windows: set CAMERA_SOURCE=rtsp://user:pass@192.168.1.10:554/stream1
#   Linux/Mac: export CAMERA_SOURCE=rtsp://user:pass@192.168.1.10:554/stream1
CAMERA_SOURCE = os.environ.get("CAMERA_SOURCE", "0")
MATCH_THRESHOLD = 70.0   # LBPH distance: lower = stricter
CONFIRM_FRAMES = 5       # consecutive recognitions needed before marking
SAMPLES = 30             # face photos captured per student on registration

app = Flask(__name__)


# ----------------------------------------------------------------- database
def q(sql, args=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def run(sql, args=()):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def init_db():
    run("""CREATE TABLE IF NOT EXISTS students (
            student_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    run("""CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            confidence INTEGER,
            snapshot TEXT,
            UNIQUE(student_id, date))""")  # UNIQUE = one record per student per day


# ---------------------------------------------------------------- AI engine
class FaceEngine:
    """Detection + recognition (OpenCV Haar + LBPH, runs on any laptop).
    To upgrade to a stronger model (ArcFace / dlib / InsightFace) just replace
    this class, keeping the same methods: detect(), train(), identify()."""

    def __init__(self):
        if not hasattr(cv2, "face"):
            raise RuntimeError("cv2.face missing. Run: pip uninstall opencv-python -y && pip install opencv-contrib-python")
        self.detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        self.model = None
        self.labels = {}
        self.lock = threading.Lock()

    def detect(self, gray):
        return self.detector.detectMultiScale(gray, 1.2, 5, minSize=(70, 70))

    @staticmethod
    def prep(face):
        return cv2.equalizeHist(cv2.resize(face, (200, 200)))

    def train(self):
        imgs, lbls, labels = [], [], {}
        for i, sid in enumerate(sorted(os.listdir(FACES_DIR))):
            folder = os.path.join(FACES_DIR, sid)
            if not os.path.isdir(folder):
                continue
            labels[i] = sid
            for f in os.listdir(folder):
                img = cv2.imread(os.path.join(folder, f), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    imgs.append(self.prep(img))
                    lbls.append(i)
        model = None
        if imgs:
            model = cv2.face.LBPHFaceRecognizer_create(radius=1, neighbors=8, grid_x=8, grid_y=8)
            model.train(imgs, np.array(lbls))
        with self.lock:
            self.model, self.labels = model, labels
        print(f"[AI] Trained on {len(imgs)} face images, {len(labels)} students")

    def identify(self, face_gray):
        """Returns (student_id or None, confidence 0-100)."""
        with self.lock:
            model, labels = self.model, self.labels
        if model is None:
            return None, 0
        label, dist = model.predict(self.prep(face_gray))
        if dist <= MATCH_THRESHOLD:
            return labels.get(label), int(max(0, min(100, 100 - dist * 0.5)))
        return None, 0


# ------------------------------------------------------------ camera worker
GREEN, RED, BLUE = (60, 160, 40), (50, 50, 220), (200, 120, 30)


def draw_box(frame, box, lines, color):
    x, y, w, h = [int(v) for v in box]
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    lh = 20
    bh = lh * len(lines) + 6
    top = y - bh if y - bh > 0 else y + h
    cv2.rectangle(frame, (x, top), (x + max(w, 190), top + bh), color, -1)
    for i, t in enumerate(lines):
        cv2.putText(frame, t, (x + 5, top + lh * (i + 1) - 4), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1, cv2.LINE_AA)


def make_offline_jpeg():
    img = np.full((540, 960, 3), 25, np.uint8)
    cv2.putText(img, "Camera Offline", (270, 260), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(img, "Connect a camera to start attendance detection.", (190, 310),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (180, 180, 180), 2, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


OFFLINE_JPEG = make_offline_jpeg()


class CameraWorker(threading.Thread):
    def __init__(self, engine):
        super().__init__(daemon=True)
        self.engine = engine
        self.lock = threading.Lock()
        self.jpeg = None
        self.online = False
        self.counts = (0, 0, 0)          # detected, recognized, unknown
        self.detections = []
        self.events = deque(maxlen=40)
        self.streak = {}
        self.last_logged = {}
        self.marked, self.marked_date = set(), None
        self.names = {}
        self.capture = None
        self.reload_names()

    # ---- helpers
    def reload_names(self):
        self.names = {r["student_id"]: r["name"] for r in q("SELECT student_id, name FROM students")}

    def refresh_marked(self):
        today = date.today().isoformat()
        if self.marked_date != today:
            self.marked = {r["student_id"] for r in q("SELECT student_id FROM attendance WHERE date=?", (today,))}
            self.marked_date = today

    def log(self, msg, kind="info"):
        self.events.append({"time": datetime.now().strftime("%H:%M:%S"), "message": msg, "type": kind})

    def log_throttled(self, key, msg, kind, every=10):
        if time.time() - self.last_logged.get(key, 0) > every:
            self.last_logged[key] = time.time()
            self.log(msg, kind)

    def set_offline(self):
        with self.lock:
            if self.online:
                self.log("Camera disconnected", "warning")
            self.online, self.jpeg = False, None
            self.counts, self.detections = (0, 0, 0), []

    # ---- attendance
    def mark(self, sid, name, conf, face_bgr):
        today, now = date.today().isoformat(), datetime.now().strftime("%H:%M:%S")
        fn = f"{today}_{sid}.jpg"
        inserted = run("INSERT OR IGNORE INTO attendance(student_id,date,time,confidence,snapshot) VALUES (?,?,?,?,?)",
                       (sid, today, now, conf, fn))
        self.marked.add(sid)
        if inserted:
            cv2.imwrite(os.path.join(ATT_DIR, fn), face_bgr)
            self.log(f"{name} recognized → Attendance marked", "success")
        else:
            self.log_throttled(sid, f"{name} recognized → Already marked", "info")

    def handle_capture(self, gray, faces):
        c = self.capture
        if not c or not c["active"] or len(faces) != 1 or time.time() - c["last"] < 0.2:
            return
        x, y, w, h = faces[0]
        n = c["total"] - c["remaining"] + 1
        cv2.imwrite(os.path.join(FACES_DIR, c["id"], f"{n}.jpg"), gray[y:y + h, x:x + w])
        c["remaining"] -= 1
        c["last"] = time.time()
        if c["remaining"] <= 0:
            self.engine.train()
            c["active"] = False
            self.reload_names()
            self.log(f"{c['name']} registered successfully", "success")

    # ---- frame processing
    def process(self, frame):
        self.refresh_marked()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self.engine.detect(gray)
        self.handle_capture(gray, faces)

        seen, dets, rec, unk = set(), [], 0, 0
        for (x, y, w, h) in faces:
            sid, conf = self.engine.identify(gray[y:y + h, x:x + w])
            if sid and sid in self.names:
                name = self.names[sid]
                safe = name.encode("ascii", "ignore").decode() or sid
                rec += 1
                seen.add(sid)
                self.streak[sid] = self.streak.get(sid, 0) + 1
                if sid in self.marked:
                    status, kind, color = "Present", "recognized", GREEN
                    self.log_throttled(sid, f"{name} recognized → Already marked", "info")
                elif self.streak[sid] >= CONFIRM_FRAMES:
                    self.mark(sid, name, conf, frame[y:y + h, x:x + w])
                    status, kind, color = "Present", "recognized", GREEN
                else:
                    status, kind, color = "Verifying...", "verifying", BLUE
                head = "[OK] " if kind == "recognized" else ""
                draw_box(frame, (x, y, w, h), [f"{head}{safe}", f"ID: {sid}", status, f"Confidence: {conf}%"], color)
                dets.append({"type": kind, "name": name, "student_id": sid, "confidence": conf, "status": status,
                             "box": [int(x), int(y), int(w), int(h)]})
            else:
                unk += 1
                draw_box(frame, (x, y, w, h), ["[!] Unknown Person", "Attendance not marked"], RED)
                dets.append({"type": "unknown", "name": "Unknown Person", "student_id": "", "confidence": 0,
                             "status": "Attendance not marked", "box": [int(x), int(y), int(w), int(h)]})
                self.log_throttled("unknown", "Unknown person detected → Attendance not marked", "warning", 15)

        for sid in list(self.streak):          # decay streaks of people no longer seen
            if sid not in seen:
                self.streak[sid] -= 1
                if self.streak[sid] <= 0:
                    del self.streak[sid]

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        with self.lock:
            self.jpeg = buf.tobytes() if ok else None
            self.counts = (len(faces), rec, unk)
            self.detections = dets

    def open_cam(self):
        cap = cv2.VideoCapture(int(CAMERA_SOURCE) if CAMERA_SOURCE.isdigit() else CAMERA_SOURCE)
        if cap.isOpened():
            return cap
        cap.release()
        return None

    def run(self):
        cap = None
        while True:
            try:
                if cap is None:
                    cap = self.open_cam()
                    if cap is None:
                        self.set_offline()
                        time.sleep(3)
                        continue
                ok, frame = cap.read()
                if not ok:
                    cap.release()
                    cap = None
                    self.set_offline()
                    time.sleep(1)
                    continue
                if not self.online:
                    self.online = True
                    self.log("Camera connected", "success")
                if frame.shape[1] > 960:
                    s = 960 / frame.shape[1]
                    frame = cv2.resize(frame, None, fx=s, fy=s)
                self.process(frame)
                time.sleep(0.01)
            except Exception as e:
                print("[Camera error]", e)
                if cap is not None:
                    cap.release()
                    cap = None
                self.set_offline()
                time.sleep(1)


init_db()
engine = FaceEngine()
engine.train()
cam = CameraWorker(engine)
cam.start()


# ------------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/video_feed")
def video_feed():
    def gen():
        while True:
            jpg = cam.jpeg or OFFLINE_JPEG
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(0.04)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/stats")
def stats():
    today = date.today().isoformat()
    total = q("SELECT COUNT(*) c FROM students")[0]["c"]
    present = q("SELECT COUNT(*) c FROM attendance a JOIN students s ON s.student_id=a.student_id WHERE a.date=?",
                (today,))[0]["c"]
    with cam.lock:
        det, rec, unk = cam.counts
        dets = list(cam.detections)
    c = cam.capture
    return jsonify(
        camera_online=cam.online, detected=det, recognized=rec, unknown=unk, detections=dets,
        total=total, present=present, absent=total - present,
        percent=round(present / total * 100) if total else 0,
        events=list(cam.events)[::-1],
        capture=None if not c else {"active": c["active"], "done": c["total"] - c["remaining"],
                                    "total": c["total"], "id": c["id"], "name": c["name"]})


@app.route("/api/students")
def students():
    today = date.today().isoformat()
    rows = q("""SELECT s.student_id, s.name,
                EXISTS(SELECT 1 FROM attendance a WHERE a.student_id=s.student_id AND a.date=?) AS present
                FROM students s ORDER BY s.name""", (today,))
    for r in rows:
        folder = os.path.join(FACES_DIR, r["student_id"])
        r["samples"] = len(os.listdir(folder)) if os.path.isdir(folder) else 0
    return jsonify(rows)


@app.route("/api/register", methods=["POST"])
def register():
    d = request.get_json(force=True)
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(d.get("student_id", "")).strip())
    name = str(d.get("name", "")).strip()
    if not sid or not name:
        return jsonify(error="Student ID and name are required"), 400
    if not cam.online:
        return jsonify(error="Camera is offline. Connect a camera first."), 400
    folder = os.path.join(FACES_DIR, sid)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    run("""INSERT INTO students(student_id,name) VALUES(?,?)
           ON CONFLICT(student_id) DO UPDATE SET name=excluded.name""", (sid, name))
    cam.reload_names()
    cam.capture = {"id": sid, "name": name, "total": SAMPLES, "remaining": SAMPLES, "last": 0, "active": True}
    return jsonify(ok=True)


@app.route("/api/students/<sid>", methods=["DELETE"])
def delete_student(sid):
    run("DELETE FROM attendance WHERE student_id=?", (sid,))
    run("DELETE FROM students WHERE student_id=?", (sid,))
    shutil.rmtree(os.path.join(FACES_DIR, re.sub(r"[^A-Za-z0-9_-]", "", sid)), ignore_errors=True)
    engine.train()
    cam.reload_names()
    cam.marked_date = None
    return jsonify(ok=True)


def fetch_history(d, status, text):
    like = f"%{text}%"
    if status == "absent":
        d = d or date.today().isoformat()
        rows = q("""SELECT student_id, name FROM students
                    WHERE (student_id LIKE ? OR name LIKE ?)
                    AND student_id NOT IN (SELECT student_id FROM attendance WHERE date=?)
                    ORDER BY name""", (like, like, d))
        for r in rows:
            r.update(date=d, time="-", confidence=None, snapshot=None, status="Absent")
        return rows
    sql = """SELECT a.date, a.time, a.confidence, a.snapshot, s.student_id, s.name
             FROM attendance a JOIN students s ON s.student_id=a.student_id
             WHERE (s.student_id LIKE ? OR s.name LIKE ?)"""
    args = [like, like]
    if d:
        sql += " AND a.date=?"
        args.append(d)
    rows = q(sql + " ORDER BY a.date DESC, a.time DESC LIMIT 500", args)
    for r in rows:
        r["status"] = "Present"
    return rows


@app.route("/api/history")
def history():
    return jsonify(fetch_history(request.args.get("date", "").strip(),
                                 request.args.get("status", "present"),
                                 request.args.get("q", "").strip()))


@app.route("/api/export")
def export():
    rows = fetch_history(request.args.get("date", "").strip(), request.args.get("status", "present"),
                         request.args.get("q", "").strip())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Student ID", "Name", "Date", "Time", "Status", "Confidence"])
    for r in rows:
        w.writerow([r["student_id"], r["name"], r["date"], r["time"], r["status"], r["confidence"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=attendance.csv"})


@app.route("/attendance/<path:filename>")
def snapshot(filename):
    return send_from_directory(ATT_DIR, filename)


# -------------------------------------------------------- mobile / browser camera
@app.route("/api/process_frame", methods=["POST"])
def process_client_frame():
    d = request.get_json(silent=True) or {}
    image_data = d.get("image", "")
    if not image_data:
        return jsonify(error="No image provided"), 400

    if "," in image_data:
        image_data = image_data.split(",", 1)[1]
    try:
        img_bytes = base64.b64decode(image_data)
        nparr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    except Exception as e:
        return jsonify(error=f"Decode error: {str(e)}"), 400

    if frame is None:
        return jsonify(error="Invalid image format"), 400

    cam.refresh_marked()
    h_orig, w_orig = frame.shape[:2]
    if w_orig > 960:
        scale = 960 / w_orig
        frame_proc = cv2.resize(frame, None, fx=scale, fy=scale)
    else:
        scale = 1.0
        frame_proc = frame

    gray = cv2.cvtColor(frame_proc, cv2.COLOR_BGR2GRAY)
    faces = engine.detect(gray)

    seen, dets, rec, unk = set(), [], 0, 0
    for (x, y, w, h) in faces:
        sid, conf = engine.identify(gray[y:y + h, x:x + w])
        orig_box = [int(x / scale), int(y / scale), int(w / scale), int(h / scale)]
        if sid and sid in cam.names:
            name = cam.names[sid]
            rec += 1
            seen.add(sid)
            cam.streak[sid] = cam.streak.get(sid, 0) + 1
            if sid in cam.marked:
                status, kind = "Present", "recognized"
                cam.log_throttled(sid, f"{name} recognized → Already marked", "info")
            elif cam.streak[sid] >= CONFIRM_FRAMES:
                ox, oy, ow, oh = orig_box
                cam.mark(sid, name, conf, frame[oy:oy + oh, ox:ox + ow])
                status, kind = "Present", "recognized"
            else:
                status, kind = "Verifying...", "verifying"
            dets.append({
                "type": kind,
                "name": name,
                "student_id": sid,
                "confidence": conf,
                "status": status,
                "box": orig_box
            })
        else:
            unk += 1
            dets.append({
                "type": "unknown",
                "name": "Unknown Person",
                "student_id": "",
                "confidence": 0,
                "status": "Attendance not marked",
                "box": orig_box
            })
            cam.log_throttled("unknown", "Unknown person detected → Attendance not marked", "warning", 15)

    for sid in list(cam.streak):
        if sid not in seen:
            cam.streak[sid] -= 1
            if cam.streak[sid] <= 0:
                del cam.streak[sid]

    today = date.today().isoformat()
    total = q("SELECT COUNT(*) c FROM students")[0]["c"]
    present = q("SELECT COUNT(*) c FROM attendance a JOIN students s ON s.student_id=a.student_id WHERE a.date=?", (today,))[0]["c"]

    return jsonify(
        success=True,
        frame_w=w_orig,
        frame_h=h_orig,
        detected=len(faces),
        recognized=rec,
        unknown=unk,
        detections=dets,
        total=total,
        present=present,
        absent=total - present,
        percent=round(present / total * 100) if total else 0,
        events=list(cam.events)[::-1][:20]
    )


@app.route("/api/register_init", methods=["POST"])
def register_init():
    d = request.get_json(silent=True) or {}
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(d.get("student_id", "")).strip())
    name = str(d.get("name", "")).strip()
    if not sid or not name:
        return jsonify(error="Student ID and name are required"), 400
    folder = os.path.join(FACES_DIR, sid)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder, exist_ok=True)
    run("""INSERT INTO students(student_id,name) VALUES(?,?)
           ON CONFLICT(student_id) DO UPDATE SET name=excluded.name""", (sid, name))
    cam.reload_names()
    return jsonify(ok=True, student_id=sid, name=name, total=SAMPLES)


@app.route("/api/register_sample", methods=["POST"])
def register_sample():
    d = request.get_json(silent=True) or {}
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(d.get("student_id", "")).strip())
    image_data = d.get("image", "")
    sample_index = int(d.get("index", 1))

    if not sid or not image_data:
        return jsonify(error="Missing student ID or image data"), 400

    folder = os.path.join(FACES_DIR, sid)
    if not os.path.isdir(folder):
        return jsonify(error="Registration not initialized for this ID"), 400

    if "," in image_data:
        image_data = image_data.split(",", 1)[1]
    try:
        img_bytes = base64.b64decode(image_data)
        nparr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    except Exception as e:
        return jsonify(error="Decode error"), 400

    if frame is None:
        return jsonify(error="Invalid image"), 400

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = engine.detect(gray)
    if len(faces) == 0:
        return jsonify(ok=False, error="No face detected. Look directly at the camera."), 400
    if len(faces) > 1:
        return jsonify(ok=False, error="Multiple faces detected. Ensure only 1 person is in frame."), 400

    x, y, w, h = faces[0]
    face_roi = gray[y:y + h, x:x + w]
    cv2.imwrite(os.path.join(folder, f"{sample_index}.jpg"), face_roi)
    return jsonify(ok=True, sample=sample_index, total=SAMPLES)


@app.route("/api/register_complete", methods=["POST"])
def register_complete():
    d = request.get_json(silent=True) or {}
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(d.get("student_id", "")).strip())
    if not sid:
        return jsonify(error="Missing student ID"), 400
    folder = os.path.join(FACES_DIR, sid)
    count = len(os.listdir(folder)) if os.path.isdir(folder) else 0
    if count == 0:
        return jsonify(error="No face samples were collected"), 400
    engine.train()
    cam.reload_names()
    name = cam.names.get(sid, sid)
    cam.log(f"{name} registered successfully ({count} samples)", "success")
    return jsonify(ok=True, name=name, samples=count)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Open http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)