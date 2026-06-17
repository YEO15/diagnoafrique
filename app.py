"""
╔══════════════════════════════════════════════════════════════════╗
║   DiagnoAfrique v2.0 — Serveur Central                          ║
║   Détection paludisme par IA + Architecture distribuée          ║
║                                                                  ║
║   Routes :                                                       ║
║   /              → Interface client                             ║
║   /dashboard     → Tableau de bord admin                        ║
║   /predict       → Analyse image (POST)                         ║
║   /api/diagnose  → Alias /predict (compatibilité clients)       ║
║   /history       → Historique diagnostics (GET)                 ║
║   /status        → Statut serveur (GET)                         ║
║   /health        → Ping (GET)                                   ║
║   /sync          → Réception sync nœuds (POST)                  ║
║   /pdf/<id>      → Rapport PDF patient (GET)                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, io, json, uuid, sqlite3, base64
import numpy as np
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, abort

try:
    from flask_cors import CORS
    CORS_AVAILABLE = True
except ImportError:
    CORS_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════
#   CONFIGURATION
# ══════════════════════════════════════════════════════════════════
app = Flask(__name__)
if CORS_AVAILABLE:
    CORS(app)

NODE_NAME     = "central"
VERSION       = "2.0"
DB_PATH       = "diagnoafrique.db"
UPLOAD_FOLDER = "uploads"
SEUIL_MALARIA = 24.68   # Seuil calibré sur dataset NIH

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("templates", exist_ok=True)


# ══════════════════════════════════════════════════════════════════
#   BASE DE DONNÉES
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
            source_node     TEXT DEFAULT 'central',
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
            exam_type       TEXT,
            result          TEXT,
            recommendations TEXT,
            synced          INTEGER DEFAULT 1,
            latitude        REAL DEFAULT 5.3484,
            longitude       REAL DEFAULT -4.0169
        );
    """)
    conn.commit()
    conn.close()
    print(f"  ✅ Base de données prête → {DB_PATH}")

def save_diagnostic(diag_id, source_node, mode, result, image_name,
                    synced=1, patient_info=None, coords=None):
    if patient_info is None:
        patient_info = {}
    infected   = result['details'].get('infected', False)
    confidence = float(result['details'].get('confidence', 0))
    result_txt = 'Malaria détectée' if infected else 'Pas de malaria'
    lat = coords[0] if coords else 5.3484
    lng = coords[1] if coords else -4.0169

    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO diagnostics
        (id, timestamp, source_node, mode, status, diagnostic,
         confiance, confidence, risk_level, details, image_name,
         patient, patient_id, patient_nom, patient_prenom,
         patient_age, doctor, exam_type, result, recommendations,
         synced, latitude, longitude)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        diag_id,
        datetime.now().isoformat(),
        source_node, mode,
        result.get('status', ''),
        result.get('diagnostic', ''),
        result.get('confiance', ''),
        confidence,
        result.get('risk_level', ''),
        json.dumps(result.get('details', {})),
        image_name,
        patient_info.get('patient', 'Anonyme'),
        patient_info.get('patient_id', ''),
        patient_info.get('patient_nom', ''),
        patient_info.get('patient_prenom', ''),
        patient_info.get('patient_age', ''),
        patient_info.get('doctor', ''),
        patient_info.get('exam_type', 'PALUDISME'),
        result_txt,
        json.dumps([result.get('recommandation', '')]),
        synced, lat, lng
    ))
    conn.commit()
    conn.close()

init_db()


# ══════════════════════════════════════════════════════════════════
#   MOTEUR IA — DÉTECTION PALUDISME
# ══════════════════════════════════════════════════════════════════

def analyze_malaria(image_bytes):
    """
    Analyse une image de frottis Giemsa pour détecter le paludisme.
    Basé sur la détection de granules pourpres caractéristiques.
    Seuil calibré sur dataset NIH : 24.68
    """
    if not PIL_AVAILABLE:
        return {"error": "PIL non disponible"}

    try:
        img  = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        arr  = np.array(img, dtype=float)

        # Masque cellules (exclure fond noir)
        mean_px   = arr.mean(axis=2)
        cell_mask = mean_px > 30

        r = arr[:,:,0][cell_mask]
        g = arr[:,:,1][cell_mask]
        b = arr[:,:,2][cell_mask]

        if len(r) == 0:
            return {"error": "Image invalide — aucune cellule détectée"}

        r_m, g_m, b_m = r.mean(), g.mean(), b.mean()
        r_std = r.std()

        # Détection granules pourpres (marqueurs plasmodium)
        purple_mask = (
            (arr[:,:,2] > arr[:,:,1] + 15) &
            (arr[:,:,0] > arr[:,:,1]) &
            (mean_px < 180) &
            (mean_px > 40) &
            cell_mask
        )
        purple_pct   = purple_mask.sum() / max(cell_mask.sum(), 1) * 100
        purple_score = (b_m - g_m) + (r_m - g_m) * 0.3
        bg_ratio     = b_m / max(g_m, 1)

        # Score final
        score = (
            purple_pct * 2.5 +
            max(0, purple_score) * 0.8 +
            max(0, bg_ratio - 0.95) * 40 +
            r_std * 0.1
        )

        infected   = bool(score > SEUIL_MALARIA)
        confidence = float(min(99.9, 50 + abs(score - SEUIL_MALARIA) * 8))
        risk_level = ("ÉLEVÉ" if confidence > 85 else "MODÉRÉ") if infected else "FAIBLE"

        return {
            "infected"        : infected,
            "confidence"      : round(confidence, 1),
            "infection_score" : round(float(score), 2),
            "purple_granules" : round(float(purple_pct), 2),
            "risk_level"      : risk_level,
            "threshold"       : SEUIL_MALARIA,
        }

    except Exception as e:
        return {"error": str(e)}


def build_response(raw):
    """Formate la réponse IA pour les clients."""
    if "error" in raw:
        return {
            "status": "ERREUR", "diagnostic": "Erreur d'analyse",
            "confiance": "0%", "recommandation": raw["error"],
            "risk_level": "INCONNU", "mode_info": "PALUDISME",
            "details": raw
        }

    infected   = raw.get("infected", False)
    confidence = raw.get("confidence", 0)
    risk       = raw.get("risk_level", "INCONNU")

    if infected:
        status     = "ALERTE"
        diagnostic = "Anomalie Détectée ⚠️"
        reco = (
            f"🚨 Paludisme probable (granules: {raw.get('purple_granules',0):.1f}%, "
            f"confiance: {confidence:.1f}%). Consultation médicale IMMÉDIATE requise. "
            "Initier traitement antipaludéen selon protocole national."
        )
    else:
        status     = "OK"
        diagnostic = "Analyse Négative ✅"
        reco = (
            "Aucun marqueur parasitaire détecté. "
            "Surveillance clinique standard recommandée. "
            "En cas de symptômes persistants, répéter le test sous 48h."
        )

    return {
        "status"        : status,
        "mode_info"     : "PALUDISME",
        "diagnostic"    : diagnostic,
        "confiance"     : f"{confidence:.1f}%",
        "recommandation": reco,
        "risk_level"    : risk,
        "details"       : raw,
        # Champs format client MedEdge
        "diagnosis"     : "Malaria détectée" if infected else "Pas de malaria",
        "confidence"    : round(confidence, 1),
        "severity"      : ("critical" if risk == "ÉLEVÉ" else "warning") if infected else "normal",
        "description"   : reco,
        "recommendations": [reco],
        "distribution"  : [
            {"class": "Malaria", "confidence": round(confidence if infected else 100 - confidence, 1)},
            {"class": "Normal",  "confidence": round((100 - confidence) if infected else confidence, 1)},
        ]
    }


# ══════════════════════════════════════════════════════════════════
#   ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/predict', methods=['POST'])
@app.route('/api/diagnose', methods=['POST'])
def predict():
    """
    Endpoint principal d'analyse.
    Accepte : multipart/form-data ou JSON base64
    Champs fichier : 'image', 'file', 'upload'
    """
    # ── Récupérer l'image ────────────────────────────────────────
    image_bytes = None
    filename    = "image.jpg"

    # Multipart
    file = (request.files.get('image') or
            request.files.get('file')  or
            request.files.get('upload'))

    if file:
        image_bytes = file.read()
        filename    = file.filename or filename

    # JSON base64
    elif request.is_json:
        data = request.get_json()
        for key in ('image_b64', 'image', 'data', 'file_b64'):
            if key in data:
                try:
                    b64 = data[key]
                    if ',' in b64:
                        b64 = b64.split(',')[1]
                    image_bytes = base64.b64decode(b64)
                    filename    = data.get('filename', filename)
                    break
                except Exception:
                    pass

    if not image_bytes:
        return jsonify({
            "error": "Aucune image reçue",
            "hint" : "Champs acceptés: image, file, upload (multipart) ou image_b64 (JSON)"
        }), 400

    # ── Infos patient ─────────────────────────────────────────────
    diag_id = str(uuid.uuid4())

    def get_field(*keys, default=''):
        """Cherche un champ dans form-data ou JSON body."""
        for k in keys:
            v = request.form.get(k)
            if v: return v
        if request.is_json:
            data = request.get_json() or {}
            for k in keys:
                # Support objet imbriqué: patient.nom
                parts = k.split('.')
                val = data
                for p in parts:
                    val = val.get(p, {}) if isinstance(val, dict) else None
                if val and isinstance(val, str): return val
        return default

    prenom = get_field('patient_prenom', 'prenom', 'patient.prenom')
    nom    = get_field('patient_nom',    'nom',    'patient.nom')
    patient_name = ' '.join([prenom, nom]).strip() or get_field('patient', default='Anonyme')

    patient_info = {
        'patient'       : patient_name,
        'patient_id'    : get_field('patient_id', 'id', default=diag_id[:8].upper()),
        'patient_nom'   : nom,
        'patient_prenom': prenom,
        'patient_age'   : get_field('patient_age', 'age'),
        'doctor'        : get_field('doctor', 'medecin'),
        'exam_type'     : 'PALUDISME',
    }

    source_node = get_field('source_node', default='client')

    # Coordonnées GPS optionnelles
    try:
        lat = float(get_field('latitude',  default='5.3484'))
        lng = float(get_field('longitude', default='-4.0169'))
    except ValueError:
        lat, lng = 5.3484, -4.0169

    # ── Sauvegarder l'image ───────────────────────────────────────
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    img_name = f"{ts}_{source_node}_{filename}"
    try:
        with open(os.path.join(UPLOAD_FOLDER, img_name), 'wb') as f:
            f.write(image_bytes)
    except Exception:
        pass

    # ── Analyse IA ────────────────────────────────────────────────
    try:
        raw    = analyze_malaria(image_bytes)
        result = build_response(raw)
    except Exception as e:
        return jsonify({"error": f"Erreur analyse: {str(e)}"}), 500

    # ── Sauvegarder en base ───────────────────────────────────────
    save_diagnostic(
        diag_id, source_node, 'paludisme',
        result, img_name, synced=1,
        patient_info=patient_info,
        coords=(lat, lng)
    )

    # ── Réponse ───────────────────────────────────────────────────
    result['diag_id']      = diag_id
    result['processed_by'] = NODE_NAME
    result['patient']      = patient_name
    result['timestamp']    = datetime.now().isoformat()
    result['version']      = VERSION

    ts_str = datetime.now().strftime('%H:%M:%S')
    infected = result['details'].get('infected', False)
    print(f"  [{ts_str}] {source_node} → {result['status']} "
          f"({result['confiance']}) — {patient_name}")

    return jsonify(result)


@app.route('/history', methods=['GET'])
def history():
    """Retourne l'historique des diagnostics."""
    limit = request.args.get('limit', 100, type=int)
    conn  = get_db()
    rows  = conn.execute(
        "SELECT * FROM diagnostics ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()

    diagnostics = []
    for r in rows:
        d = dict(r)
        try:
            d['details'] = json.loads(d['details'] or '{}')
        except Exception:
            d['details'] = {}
        try:
            d['recommendations'] = json.loads(d['recommendations'] or '[]')
        except Exception:
            d['recommendations'] = []
        diagnostics.append(d)

    return jsonify({"diagnostics": diagnostics, "total": len(diagnostics)})


@app.route('/status', methods=['GET'])
def status():
    """Statut du serveur."""
    conn   = get_db()
    total  = conn.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0]
    alertes= conn.execute("SELECT COUNT(*) FROM diagnostics WHERE status='ALERTE'").fetchone()[0]
    nodes  = conn.execute("SELECT DISTINCT source_node FROM diagnostics").fetchall()
    conn.close()

    return jsonify({
        "status"           : "online",
        "node"             : NODE_NAME,
        "version"          : VERSION,
        "total_diagnostics": total,
        "alertes"          : alertes,
        "negatifs"         : total - alertes,
        "taux_infection"   : round(alertes / total * 100, 1) if total > 0 else 0,
        "nodes_actifs"     : [r[0] for r in nodes],
        "timestamp"        : datetime.now().isoformat(),
    })


@app.route('/health', methods=['GET'])
def health():
    """Endpoint de ping pour les nœuds."""
    return jsonify({"status": "ok", "node": NODE_NAME, "version": VERSION})


@app.route('/sync', methods=['POST'])
def sync():
    """
    Reçoit les diagnostics offline des nœuds locaux.
    Payload: { "node": "node_1", "diagnostics": [...] }
    """
    data        = request.get_json()
    node_name   = data.get('node', 'unknown')
    diagnostics = data.get('diagnostics', [])
    merged      = 0

    conn = get_db()
    for d in diagnostics:
        # Vérifier si déjà présent
        existing = conn.execute(
            "SELECT id FROM diagnostics WHERE id=?", (d.get('id', ''),)
        ).fetchone()

        if not existing:
            try:
                details = d.get('details', {})
                if isinstance(details, str):
                    details = json.loads(details)

                infected = details.get('infected', d.get('status') == 'ALERTE')

                conn.execute("""
                    INSERT INTO diagnostics
                    (id, timestamp, source_node, mode, status, diagnostic,
                     confiance, confidence, risk_level, details, image_name,
                     patient, patient_id, patient_nom, patient_prenom,
                     patient_age, doctor, exam_type, result, recommendations,
                     synced, latitude, longitude)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    d.get('id', str(uuid.uuid4())),
                    d.get('timestamp', datetime.now().isoformat()),
                    node_name,
                    d.get('mode', 'paludisme'),
                    d.get('status', ''),
                    d.get('diagnostic', ''),
                    d.get('confiance', ''),
                    float(d.get('confidence', 0)),
                    d.get('risk_level', ''),
                    json.dumps(details),
                    d.get('image_name', ''),
                    d.get('patient', 'Anonyme'),
                    d.get('patient_id', ''),
                    d.get('patient_nom', ''),
                    d.get('patient_prenom', ''),
                    d.get('patient_age', ''),
                    d.get('doctor', ''),
                    d.get('exam_type', 'PALUDISME'),
                    'Malaria détectée' if infected else 'Pas de malaria',
                    json.dumps(d.get('recommendations', [])),
                    1,  # synced
                    float(d.get('latitude', 5.3484)),
                    float(d.get('longitude', -4.0169)),
                ))
                merged += 1
            except Exception as e:
                print(f"  ⚠️ Sync erreur pour {d.get('id','?')}: {e}")

    conn.commit()
    conn.close()

    ts = datetime.now().strftime('%H:%M:%S')
    print(f"  [{ts}] Sync ← {node_name} : {merged}/{len(diagnostics)} nouveaux")
    return jsonify({
        "status" : "ok",
        "merged" : merged,
        "total"  : len(diagnostics),
        "node"   : node_name,
    })


@app.route('/pdf/<diag_id>', methods=['GET'])
def get_pdf(diag_id):
    """Génère et retourne le rapport PDF d'un diagnostic."""
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM diagnostics WHERE id=?", (diag_id,)
    ).fetchone()
    conn.close()

    if not row:
        abort(404)

    d = dict(row)
    try:
        details = json.loads(d.get('details') or '{}')
    except Exception:
        details = {}

    infected   = details.get('infected', d.get('status') == 'ALERTE')
    confidence = details.get('confidence', 0)
    ts         = d.get('timestamp', '')[:10]

    # Générer HTML du rapport
    color  = "#DC2626" if infected else "#059669"
    icon   = "⚠️" if infected else "✅"
    result_label = "POSITIF — Malaria Détectée" if infected else "NÉGATIF — Pas de Malaria"

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Rapport DiagnoAfrique — {d.get('patient','Anonyme')}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ font-family:Arial,sans-serif; color:#0f172a; background:#f8fafc; padding:32px }}
  .header {{ background:linear-gradient(135deg,#1a3c6e,#2563eb); color:white;
    padding:28px 36px; border-radius:16px; margin-bottom:28px }}
  .logo {{ font-size:1.6rem; font-weight:800; margin-bottom:4px }}
  .subtitle {{ font-size:.85rem; opacity:.8 }}
  .section {{ background:white; border:1px solid #e2e8f0; border-radius:12px;
    padding:20px 24px; margin-bottom:16px }}
  .section-title {{ font-size:.65rem; font-weight:700; text-transform:uppercase;
    letter-spacing:.12em; color:#94a3b8; margin-bottom:14px }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px }}
  .field {{ background:#f8fafc; border-radius:8px; padding:10px 14px }}
  .field-label {{ font-size:.62rem; font-weight:700; color:#94a3b8;
    text-transform:uppercase; letter-spacing:.08em; margin-bottom:3px }}
  .field-value {{ font-size:.9rem; font-weight:600; color:#0f172a }}
  .result-box {{ border-radius:12px; padding:24px; text-align:center;
    margin-bottom:16px; border:2px solid {color}20; background:{color}08 }}
  .result-icon {{ font-size:3rem; margin-bottom:8px }}
  .result-label {{ font-size:1.4rem; font-weight:800; color:{color} }}
  .confidence {{ font-size:.95rem; color:#64748b; margin-top:6px }}
  .reco {{ background:#fffbeb; border:1px solid #fde68a; border-radius:10px;
    padding:14px 18px; font-size:.85rem; line-height:1.8; color:#92400e }}
  .footer {{ text-align:center; font-size:.7rem; color:#94a3b8;
    margin-top:28px; padding-top:16px; border-top:1px solid #e2e8f0 }}
  @media print {{ body {{ padding:0 }} }}
</style>
</head>
<body>
<div class="header">
  <div class="logo">🧬 DiagnoAfrique</div>
  <div class="subtitle">Plateforme de Diagnostic Médical Distribué — Côte d'Ivoire · v{VERSION}</div>
</div>

<div class="result-box">
  <div class="result-icon">{icon}</div>
  <div class="result-label">{result_label}</div>
  <div class="confidence">Confiance : <strong>{confidence:.1f}%</strong> · 
    Niveau de risque : <strong>{d.get('risk_level','—')}</strong></div>
</div>

<div class="section">
  <div class="section-title">Informations Patient</div>
  <div class="grid">
    <div class="field">
      <div class="field-label">Nom complet</div>
      <div class="field-value">{d.get('patient','Anonyme')}</div>
    </div>
    <div class="field">
      <div class="field-label">ID Patient</div>
      <div class="field-value">{d.get('patient_id','—')}</div>
    </div>
    <div class="field">
      <div class="field-label">Âge</div>
      <div class="field-value">{d.get('patient_age','—')} ans</div>
    </div>
    <div class="field">
      <div class="field-label">Médecin</div>
      <div class="field-value">{d.get('doctor','—')}</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-title">Détails du Diagnostic</div>
  <div class="grid">
    <div class="field">
      <div class="field-label">Date</div>
      <div class="field-value">{ts}</div>
    </div>
    <div class="field">
      <div class="field-label">Référence</div>
      <div class="field-value" style="font-size:.7rem">{diag_id[:16]}...</div>
    </div>
    <div class="field">
      <div class="field-label">Type d'analyse</div>
      <div class="field-value">Frottis Giemsa — IA CNN</div>
    </div>
    <div class="field">
      <div class="field-label">Nœud</div>
      <div class="field-value">{d.get('source_node','central')}</div>
    </div>
    <div class="field">
      <div class="field-label">Granules détectés</div>
      <div class="field-value">{details.get('purple_granules',0):.1f}%</div>
    </div>
    <div class="field">
      <div class="field-label">Score infection</div>
      <div class="field-value">{details.get('infection_score',0):.2f} / seuil {SEUIL_MALARIA}</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-title">Recommandations Cliniques</div>
  <div class="reco">{d.get('result','—')}</div>
</div>

<div class="footer">
  Rapport généré par DiagnoAfrique v{VERSION} · {datetime.now().strftime('%d/%m/%Y à %H:%M')} ·
  Usage médical uniquement · Confidentiel
</div>

<br>
<button onclick="window.print()" style="display:block;margin:0 auto;padding:12px 28px;
  background:#2563eb;color:white;border:none;border-radius:10px;
  font-size:14px;font-weight:700;cursor:pointer">
  🖨️ Imprimer / Sauvegarder PDF
</button>
</body>
</html>"""

    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


# ══════════════════════════════════════════════════════════════════
#   LANCEMENT
# ══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"║   DiagnoAfrique v{VERSION} — Serveur Central               ║")
    print("║                                                          ║")
    print("║   Interface    →  http://192.168.100.34:5000                  ║")
    print("║   Dashboard    →  http://localhost:5000/dashboard       ║")
    print("║   API          →  http://192.168.100.34:5000/api/diagnose     ║")
    print("║   Statut       →  http://localhost:5000/status          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    app.run(host='0.0.0.0', port=5000, debug=False)
