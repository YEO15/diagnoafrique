"""
╔══════════════════════════════════════════════════════════════════╗
║   DiagnoAfrique v2.0 — Nœud Local (Équipier 1)                 ║
║                                                                  ║
║   Rôle double :                                                  ║
║   1. Relaie vers le serveur central si disponible               ║
║   2. Traite localement (offline) si central hors ligne          ║
║   3. Synchronise automatiquement dès reconnexion                ║
║                                                                  ║
║   Lancement : python app_node.py                                ║
║   Port local : 5001                                             ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, io, json, uuid, sqlite3, threading, time, base64
import numpy as np
from datetime import datetime
from flask import Flask, request, jsonify, render_template

try:
    from flask_cors import CORS
    CORS_AVAILABLE = True
except ImportError:
    CORS_AVAILABLE = False

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False
    print("  ⚠️  'requests' non installé — sync désactivée. pip install requests")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

app = Flask(__name__, template_folder='templates')
if CORS_AVAILABLE:
    CORS(app)

# ══════════════════════════════════════════════════════════════════
#   ⚙️  CONFIGURATION
# ══════════════════════════════════════════════════════════════════
CENTRAL_URL    = "http://192.168.100.34:5000"   # ← IP serveur central
NODE_NAME      = "node_1"
MY_PORT        = 5001
VERSION        = "2.0"
DB_PATH        = "node_diagnoafrique.db"
UPLOAD_FOLDER  = "uploads_node"
SYNC_INTERVAL  = 10    # secondes entre chaque sync
PING_INTERVAL  = 5     # secondes entre chaque ping central
SEUIL_MALARIA  = 24.68

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("templates", exist_ok=True)

# État connexion centrale (thread-safe)
_central_online = False
_central_lock   = threading.Lock()


def is_central_online():
    with _central_lock:
        return _central_online

def set_central_online(val):
    global _central_online
    with _central_lock:
        _central_online = val


# ══════════════════════════════════════════════════════════════════
#   BASE DE DONNÉES LOCALE
# ══════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS diagnostics (
            id              TEXT PRIMARY KEY,
            timestamp       TEXT NOT NULL,
            mode            TEXT DEFAULT 'paludisme',
            status          TEXT NOT NULL,
            diagnostic      TEXT NOT NULL,
            confiance       TEXT,
            confidence      REAL,
            risk_level      TEXT,
            details         TEXT,
            image_name      TEXT,
            patient         TEXT DEFAULT 'Anonyme',
            patient_id      TEXT,
            patient_nom     TEXT,
            patient_prenom  TEXT,
            patient_age     TEXT,
            doctor          TEXT,
            synced          INTEGER DEFAULT 0,
            sync_attempts   INTEGER DEFAULT 0,
            latitude        REAL DEFAULT 5.3484,
            longitude       REAL DEFAULT -4.0169
        );
    """)
    conn.commit()
    conn.close()
    print(f"  ✅ DB locale prête → {DB_PATH}")

def save_local(diag_id, result, image_name, patient_info, coords=None):
    infected   = result['details'].get('infected', False)
    confidence = float(result['details'].get('confidence', 0))
    lat = coords[0] if coords else 5.3484
    lng = coords[1] if coords else -4.0169

    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO diagnostics
        (id, timestamp, mode, status, diagnostic, confiance, confidence,
         risk_level, details, image_name, patient, patient_id, patient_nom,
         patient_prenom, patient_age, doctor, synced, latitude, longitude)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
    """, (
        diag_id, datetime.now().isoformat(), 'paludisme',
        result.get('status', ''), result.get('diagnostic', ''),
        result.get('confiance', ''), confidence,
        result.get('risk_level', ''),
        json.dumps(result.get('details', {})), image_name,
        patient_info.get('patient', 'Anonyme'),
        patient_info.get('patient_id', ''),
        patient_info.get('patient_nom', ''),
        patient_info.get('patient_prenom', ''),
        patient_info.get('patient_age', ''),
        patient_info.get('doctor', ''),
        lat, lng
    ))
    conn.commit()
    conn.close()

def get_pending():
    conn  = get_db()
    rows  = conn.execute(
        "SELECT * FROM diagnostics WHERE synced=0 ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_synced(ids):
    conn = get_db()
    for i in ids:
        conn.execute("UPDATE diagnostics SET synced=1 WHERE id=?", (i,))
    conn.commit()
    conn.close()

init_db()


# ══════════════════════════════════════════════════════════════════
#   MOTEUR IA (identique au serveur central)
# ══════════════════════════════════════════════════════════════════

def analyze_malaria(image_bytes):
    if not PIL_AVAILABLE:
        return {"error": "PIL non disponible"}
    try:
        img      = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        arr      = np.array(img, dtype=float)
        mean_px  = arr.mean(axis=2)
        cell_mask = mean_px > 30
        r = arr[:,:,0][cell_mask]; g = arr[:,:,1][cell_mask]; b = arr[:,:,2][cell_mask]
        if len(r) == 0:
            return {"error": "Image invalide"}
        r_m,g_m,b_m = r.mean(),g.mean(),b.mean(); r_std = r.std()
        purple_mask = ((arr[:,:,2]>arr[:,:,1]+15)&(arr[:,:,0]>arr[:,:,1])&
                       (mean_px<180)&(mean_px>40)&cell_mask)
        purple_pct   = purple_mask.sum()/max(cell_mask.sum(),1)*100
        purple_score = (b_m-g_m)+(r_m-g_m)*0.3
        bg_ratio     = b_m/max(g_m,1)
        score        = purple_pct*2.5+max(0,purple_score)*0.8+max(0,bg_ratio-0.95)*40+r_std*0.1
        infected     = bool(score > SEUIL_MALARIA)
        confidence   = float(min(99.9, 50+abs(score-SEUIL_MALARIA)*8))
        risk_level   = ("ÉLEVÉ" if confidence>85 else "MODÉRÉ") if infected else "FAIBLE"
        return {"infected":infected,"confidence":round(confidence,1),
                "infection_score":round(float(score),2),
                "purple_granules":round(float(purple_pct),2),
                "risk_level":risk_level,"threshold":SEUIL_MALARIA}
    except Exception as e:
        return {"error": str(e)}

def build_response(raw):
    if "error" in raw:
        return {"status":"ERREUR","diagnostic":"Erreur","confiance":"0%",
                "recommandation":raw["error"],"risk_level":"INCONNU",
                "mode_info":"PALUDISME","details":raw}
    infected   = raw.get("infected", False)
    confidence = raw.get("confidence", 0)
    risk       = raw.get("risk_level", "INCONNU")
    if infected:
        status="ALERTE"; diagnostic="Anomalie Détectée ⚠️"
        reco=(f"🚨 Paludisme probable (granules: {raw.get('purple_granules',0):.1f}%). "
              "Consultation médicale IMMÉDIATE requise.")
    else:
        status="OK"; diagnostic="Analyse Négative ✅"
        reco="Aucun marqueur parasitaire détecté. Surveillance standard."
    return {
        "status":status,"mode_info":"PALUDISME","diagnostic":diagnostic,
        "confiance":f"{confidence:.1f}%","recommandation":reco,
        "risk_level":risk,"details":raw,
        "diagnosis":"Malaria détectée" if infected else "Pas de malaria",
        "confidence":round(confidence,1),
        "severity":("critical" if risk=="ÉLEVÉ" else "warning") if infected else "normal",
        "description":reco,"recommendations":[reco],
        "distribution":[
            {"class":"Malaria","confidence":round(confidence if infected else 100-confidence,1)},
            {"class":"Normal","confidence":round((100-confidence) if infected else confidence,1)},
        ]
    }


# ══════════════════════════════════════════════════════════════════
#   WATCHDOG — Surveille le serveur central
# ══════════════════════════════════════════════════════════════════

def watchdog():
    while True:
        try:
            r = requests.get(f"{CENTRAL_URL}/health", timeout=3)
            was_offline = not is_central_online()
            set_central_online(r.status_code == 200)
            if was_offline and is_central_online():
                print(f"  🟢 [{datetime.now().strftime('%H:%M:%S')}] "
                      f"Central de nouveau en ligne — sync immédiate")
                threading.Thread(target=sync_pending, daemon=True).start()
        except Exception:
            if is_central_online():
                print(f"  🔴 [{datetime.now().strftime('%H:%M:%S')}] "
                      f"Central hors ligne — mode offline activé")
            set_central_online(False)
        time.sleep(PING_INTERVAL)


# ══════════════════════════════════════════════════════════════════
#   SYNC — Envoie les diagnostics offline au central
# ══════════════════════════════════════════════════════════════════

def sync_pending():
    if not REQUESTS_OK or not is_central_online():
        return

    pending = get_pending()
    if not pending:
        return

    print(f"  🔄 [{datetime.now().strftime('%H:%M:%S')}] "
          f"Sync → {len(pending)} diagnostic(s) en attente")

    try:
        # Enrichir les données avant envoi
        payload = []
        for d in pending:
            d_copy = dict(d)
            if isinstance(d_copy.get('details'), str):
                try:
                    d_copy['details'] = json.loads(d_copy['details'])
                except Exception:
                    d_copy['details'] = {}
            payload.append(d_copy)

        r = requests.post(
            f"{CENTRAL_URL}/sync",
            json={"node": NODE_NAME, "diagnostics": payload},
            timeout=15
        )
        if r.status_code == 200:
            data   = r.json()
            merged = data.get('merged', 0)
            ids    = [d['id'] for d in pending]
            mark_synced(ids)
            print(f"  ✅ Sync réussie → {merged}/{len(pending)} envoyé(s)")
        else:
            print(f"  ⚠️ Sync échouée → HTTP {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ Sync impossible → {e}")


def sync_loop():
    """Boucle de sync périodique."""
    while True:
        time.sleep(SYNC_INTERVAL)
        if is_central_online():
            pending = get_pending()
            if pending:
                sync_pending()

# Démarrer les threads de surveillance
if REQUESTS_OK:
    threading.Thread(target=watchdog,  daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════
#   ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/predict', methods=['POST'])
@app.route('/api/diagnose', methods=['POST'])
def predict():
    """
    Logique de routage agnostique :
    1. Central en ligne  → relai direct (résultat temps réel)
    2. Central hors ligne → traitement local + stockage pour sync
    """
    # Récupérer l'image
    file = (request.files.get('image') or
            request.files.get('file')  or
            request.files.get('upload'))

    image_bytes = None
    filename    = "image.jpg"

    if file:
        image_bytes = file.read()
        filename    = file.filename or filename
    elif request.is_json:
        data = request.get_json()
        for key in ('image_b64', 'image', 'data', 'file_b64'):
            if key in data:
                try:
                    b64 = data[key]
                    if ',' in b64: b64 = b64.split(',')[1]
                    image_bytes = base64.b64decode(b64)
                    filename    = data.get('filename', filename)
                    break
                except Exception:
                    pass

    if not image_bytes:
        return jsonify({"error": "Aucune image reçue"}), 400

    # Infos patient
    diag_id = str(uuid.uuid4())
    def gf(*keys, default=''):
        for k in keys:
            v = request.form.get(k)
            if v: return v
        if request.is_json:
            d = request.get_json() or {}
            for k in keys:
                parts = k.split('.'); val = d
                for p in parts:
                    val = val.get(p,{}) if isinstance(val,dict) else None
                if val and isinstance(val,str): return val
        return default

    prenom = gf('patient_prenom','prenom')
    nom    = gf('patient_nom','nom')
    patient_name = ' '.join([prenom,nom]).strip() or gf('patient', default='Anonyme')

    patient_info = {
        'patient'       : patient_name,
        'patient_id'    : gf('patient_id', default=diag_id[:8].upper()),
        'patient_nom'   : nom,
        'patient_prenom': prenom,
        'patient_age'   : gf('patient_age','age'),
        'doctor'        : gf('doctor','medecin'),
    }

    try:
        lat = float(gf('latitude',  default='5.3484'))
        lng = float(gf('longitude', default='-4.0169'))
    except ValueError:
        lat, lng = 5.3484, -4.0169

    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    img_name = f"{ts}_{NODE_NAME}_{filename}"

    # ── OPTION 1 : Relai vers central ────────────────────────────
    if is_central_online() and REQUESTS_OK:
        try:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                  f"▶ Relai central ({patient_name})")

            fd = {
                'image'          : (filename, io.BytesIO(image_bytes), 'image/jpeg'),
                'mode'           : (None, 'paludisme'),
                'source_node'    : (None, NODE_NAME),
                'diag_id'        : (None, diag_id),
                'patient'        : (None, patient_name),
                'patient_prenom' : (None, prenom),
                'patient_nom'    : (None, nom),
                'patient_age'    : (None, patient_info['patient_age']),
                'doctor'         : (None, patient_info['doctor']),
                'latitude'       : (None, str(lat)),
                'longitude'      : (None, str(lng)),
            }
            r = requests.post(f"{CENTRAL_URL}/predict", files=fd, timeout=12)
            if r.status_code == 200:
                data = r.json()
                data['processed_by'] = 'central'
                data['routed_via']   = NODE_NAME
                print(f"  ✅ Réponse centrale → {data.get('status')}")
                return jsonify(data)
        except Exception as e:
            print(f"  ⚠️ Relai central échoué ({e}) → traitement local")
            set_central_online(False)

    # ── OPTION 2 : Traitement LOCAL offline ──────────────────────
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
          f"💾 Traitement LOCAL offline ({patient_name})")

    try:
        with open(os.path.join(UPLOAD_FOLDER, img_name), 'wb') as f:
            f.write(image_bytes)
    except Exception:
        pass

    try:
        raw    = analyze_malaria(image_bytes)
        result = build_response(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Stocker pour sync ultérieure
    save_local(diag_id, result, img_name, patient_info, coords=(lat, lng))

    infected   = result['details'].get('infected', False)
    confidence = result['details'].get('confidence', 0)

    response = {
        **result,
        "diag_id"     : diag_id,
        "processed_by": NODE_NAME,
        "routed_via"  : NODE_NAME,
        "offline_mode": True,
        "sync_status" : "⏳ Sera synchronisé au prochain reconnexion centrale",
        "patient"     : patient_name,
        "timestamp"   : datetime.now().isoformat(),
        "version"     : VERSION,
    }
    print(f"  💾 Stocké localement (sera syncé) → "
          f"{result['status']} ({result['confiance']})")
    return jsonify(response)


@app.route('/health', methods=['GET'])
def health():
    conn  = get_db()
    total = conn.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0]
    pend  = conn.execute("SELECT COUNT(*) FROM diagnostics WHERE synced=0").fetchone()[0]
    conn.close()
    return jsonify({
        "status"      : "online",
        "node"        : NODE_NAME,
        "version"     : VERSION,
        "port"        : MY_PORT,
        "central"     : "online" if is_central_online() else "offline",
        "total_diags" : total,
        "pending_sync": pend,
        "timestamp"   : datetime.now().isoformat(),
    })


@app.route('/status', methods=['GET'])
def status():
    conn  = get_db()
    total = conn.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0]
    pend  = conn.execute("SELECT COUNT(*) FROM diagnostics WHERE synced=0").fetchone()[0]
    conn.close()
    return jsonify({
        "node"             : NODE_NAME,
        "central"          : "online" if is_central_online() else "offline",
        "total_diagnostics": total,
        "pending_sync"     : pend,
        "central_url"      : CENTRAL_URL,
    })


@app.route('/force-sync', methods=['POST'])
def force_sync():
    """Forcer une synchronisation manuelle."""
    if not REQUESTS_OK:
        return jsonify({"error": "requests non installé"}), 500
    if not is_central_online():
        return jsonify({"error": "Central hors ligne"}), 503
    threading.Thread(target=sync_pending, daemon=True).start()
    return jsonify({"status": "sync lancée"})


# ══════════════════════════════════════════════════════════════════
#   LANCEMENT
# ══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"║   DiagnoAfrique v{VERSION} — Nœud Local                    ║")
    print("║                                                          ║")
    print(f"║   Port local   →  http://localhost:{MY_PORT}               ║")
    print(f"║   Central      →  {CENTRAL_URL:<33}   ║")
    print(f"║   Sync auto    →  toutes les {SYNC_INTERVAL}s                      ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    app.run(host='0.0.0.0', port=MY_PORT, debug=False)
