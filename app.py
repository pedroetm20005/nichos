import streamlit as st
import requests
import json
import re
import psycopg2
import psycopg2.extras
import time
import html
import hashlib
import pandas as pd
import numpy as np
import streamlit.components.v1 as components
from urllib.parse import quote
from collections import Counter
from pathlib import Path
from datetime import datetime
from io import BytesIO
from PIL import Image
from colorthief import ColorThief
from youtube_transcript_api import YouTubeTranscriptApi


PUBLIC_MODE = True
MAX_SAFE_KEYWORDS = 150
MAX_SAFE_DEPTH = 2
MAX_SAFE_BRANCHES = 50
RATE_LIMIT_SECONDS = 20
AUTO_SAVE_SEARCHES_TO_MEMORY = True


# =========================================================
# ACCESO PRIVADO
# ESTO SIRVE PARA QUE SOLO ENTREN PERSONAS CON CODIGO.
# EN STREAMLIT CLOUD LO IDEAL ES PONER LOS CODIGOS EN SECRETS.
# =========================================================

ACCESS_CONTROL_ENABLED = True


def get_allowed_access_codes():
    try:
        codes = st.secrets.get("ACCESS_CODES", [])
        return [str(code).strip() for code in codes if str(code).strip()]
    except Exception:
        return []


def require_access_code():
    if not ACCESS_CONTROL_ENABLED:
        return

    if st.session_state.get("access_granted"):
        return

    st.title("Acceso privado")
    st.caption("Introduce tu codigo de acceso para usar la app.")

    code = st.text_input("Codigo de acceso", type="password")
    entrar = st.button("Entrar")

    allowed_codes = get_allowed_access_codes()

    if not allowed_codes:
        st.warning(
            "No hay codigos configurados. En Streamlit Cloud anade ACCESS_CODES en Secrets."
        )
        st.stop()

    if entrar:
        if code.strip() in allowed_codes:
            st.session_state.access_granted = True
            st.rerun()
        else:
            st.error("Codigo incorrecto.")

    st.stop()


st.set_page_config(
    page_title="Minero Multinicho Pro v4.0",
    page_icon="brain",
    layout="wide"
)

require_access_code()


# =========================================================
# MEMORIA IA
# ESTO SIRVE PARA GUARDAR PATRONES DE NICHOS BUENOS EN POSTGRESQL.
# LA APP USA ESTO PARA COMPARAR NUEVAS BUSQUEDAS CON LO APRENDIDO.
# =========================================================

@st.cache_resource
def _initialize_postgresql_database():
    import psycopg2
    import streamlit as st
    creds = st.secrets["postgres"]
    conn = psycopg2.connect(
        host=creds["host"],
        database=creds["database"],
        user=creds["user"],
        password=creds["password"],
        port=creds.get("port", 5432)
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS analyses (
                        id SERIAL PRIMARY KEY,
                        created_at TEXT,
                        seeds TEXT,
                        final_score DOUBLE PRECISION,
                        auto_score DOUBLE PRECISION,
                        reading TEXT,
                        color_rgb TEXT,
                        total_videos INTEGER,
                        notes TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pattern_events (
                        id SERIAL PRIMARY KEY,
                        analysis_id INTEGER REFERENCES analyses(id) ON DELETE CASCADE,
                        pattern_type TEXT,
                        pattern_value TEXT,
                        weight DOUBLE PRECISION,
                        created_at TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS graph_edges (
                        id SERIAL PRIMARY KEY,
                        analysis_id INTEGER REFERENCES analyses(id) ON DELETE CASCADE,
                        source_type TEXT,
                        source_value TEXT,
                        target_type TEXT,
                        target_value TEXT,
                        weight DOUBLE PRECISION,
                        created_at TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS request_cache (
                        cache_key TEXT PRIMARY KEY,
                        payload TEXT,
                        created_at DOUBLE PRECISION
                    );
                """)
    finally:
        conn.close()
    return True

@st.cache_data
def get_cached_analyses_count():
    mem = PatternMemory()
    conn = mem.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total FROM analyses")
                return cur.fetchone()[0]
    finally:
        conn.close()

@st.cache_data
def get_cached_pattern_stats():
    mem = PatternMemory()
    conn = mem.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT pattern_type, pattern_value, COUNT(*) AS uses, AVG(weight) AS avg_weight
                    FROM pattern_events
                    GROUP BY pattern_type, pattern_value
                """)
                rows = cur.fetchall()
    finally:
        conn.close()

    stats = {}
    for row in rows:
        stats[(row[0], row[1])] = {
            "uses": int(row[2]),
            "avg_weight": float(row[3])
        }
    return stats

@st.cache_data
def get_cached_leaderboard(limit):
    mem = PatternMemory()
    conn = mem.get_conn()
    try:
        return pd.read_sql_query("""
            SELECT pattern_type, pattern_value, COUNT(*) AS uses, AVG(weight) AS avg_weight
            FROM pattern_events
            GROUP BY pattern_type, pattern_value
            HAVING COUNT(*) >= 2
            ORDER BY avg_weight DESC, uses DESC
            LIMIT %s
        """, conn, params=(limit,))
    finally:
        conn.close()

@st.cache_data
def get_cached_recent_analyses(limit):
    mem = PatternMemory()
    conn = mem.get_conn()
    try:
        return pd.read_sql_query("""
            SELECT created_at, seeds, final_score, auto_score, reading, total_videos
            FROM analyses
            ORDER BY id DESC
            LIMIT %s
        """, conn, params=(limit,))
    finally:
        conn.close()

@st.cache_data
def get_cached_graph_edges(limit_edges, min_edge_weight):
    mem = PatternMemory()
    conn = mem.get_conn()
    try:
        return pd.read_sql_query("""
            SELECT
                source_type,
                source_value,
                target_type,
                target_value,
                COUNT(*) AS uses,
                SUM(weight) AS total_weight,
                AVG(weight) AS avg_weight
            FROM graph_edges
            GROUP BY source_type, source_value, target_type, target_value
            HAVING SUM(weight) >= %s
            ORDER BY total_weight DESC, uses DESC
            LIMIT %s
        """, conn, params=(min_edge_weight, limit_edges))
    finally:
        conn.close()

GLOBAL_MEMORY_CACHE = {}
GLOBAL_CACHE_LOADED = False

def load_global_cache_if_needed():
    global GLOBAL_CACHE_LOADED, GLOBAL_MEMORY_CACHE
    if GLOBAL_CACHE_LOADED:
        return
    try:
        import psycopg2
        import streamlit as st
        creds = st.secrets["postgres"]
        conn = psycopg2.connect(
            host=creds["host"],
            database=creds["database"],
            user=creds["user"],
            password=creds["password"],
            port=creds.get("port", 5432)
        )
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT cache_key, payload, created_at FROM request_cache")
                    rows = cur.fetchall()
                    for row in rows:
                        try:
                            GLOBAL_MEMORY_CACHE[row[0]] = (json.loads(row[1]), float(row[2] or 0))
                        except Exception:
                            pass
        finally:
            conn.close()
        GLOBAL_CACHE_LOADED = True
    except Exception:
        pass

class PatternMemory:
    def __init__(self):
        _initialize_postgresql_database()
        load_global_cache_if_needed()

    def get_conn(self):
        import psycopg2
        import streamlit as st
        creds = st.secrets["postgres"]
        return psycopg2.connect(
            host=creds["host"],
            database=creds["database"],
            user=creds["user"],
            password=creds["password"],
            port=creds.get("port", 5432)
        )

    def _execute(self, query, params=None, fetch=None):
        conn = self.get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one":
                        return cur.fetchone()
                    elif fetch == "all":
                        return cur.fetchall()
        finally:
            conn.close()

    def setup(self):
        pass

    def cache_get(self, cache_key, ttl_seconds=None):
        # 1. Check global in-memory cache first
        if cache_key in GLOBAL_MEMORY_CACHE:
            payload, created_at = GLOBAL_MEMORY_CACHE[cache_key]
            if ttl_seconds is not None and time.time() - float(created_at or 0) > ttl_seconds:
                del GLOBAL_MEMORY_CACHE[cache_key]
            else:
                return payload

        # 2. Fallback to PostgreSQL request_cache
        try:
            conn = self.get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT payload, created_at FROM request_cache WHERE cache_key = %s",
                            (cache_key,)
                        )
                        row = cur.fetchone()
            finally:
                conn.close()

            if not row:
                return None

            payload_str, created_at = row
            payload = json.loads(payload_str)
            GLOBAL_MEMORY_CACHE[cache_key] = (payload, created_at)

            if ttl_seconds is not None and time.time() - float(created_at or 0) > ttl_seconds:
                return None

            return payload
        except Exception:
            return None

    def cache_set(self, cache_key, payload):
        now_ts = time.time()
        GLOBAL_MEMORY_CACHE[cache_key] = (payload, now_ts)

        # Write to PostgreSQL request_cache in background
        def write_db_cache():
            try:
                conn = self.get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO request_cache (cache_key, payload, created_at)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (cache_key)
                                DO UPDATE SET payload = EXCLUDED.payload, created_at = EXCLUDED.created_at
                            """, (cache_key, json.dumps(payload), now_ts))
                finally:
                    conn.close()
            except Exception:
                pass

        import threading
        t = threading.Thread(target=write_db_cache, daemon=True)
        t.start()

    def save_analysis(self, seeds, df_total, final_score, auto_score, reading, color_rgb, ideas=None, notes=""):
        if df_total is None or df_total.empty:
            return None

        now = datetime.utcnow().isoformat(timespec="seconds")
        seeds_text = ", ".join(seeds) if isinstance(seeds, list) else str(seeds)

        conn = self.get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO analyses
                        (created_at, seeds, final_score, auto_score, reading, color_rgb, total_videos, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        now,
                        seeds_text,
                        float(final_score),
                        float(auto_score),
                        str(reading),
                        json.dumps(tuple(int(x) for x in color_rgb)),
                        int(len(df_total)),
                        notes
                    ))
                    analysis_id = cur.fetchone()[0]
                    graph_patterns = Counter()

                    # Collect pattern events for batch insert
                    events_to_insert = []

                    for _, row_video in df_total.iterrows():
                        weight = self.video_weight(row_video)
                        for pattern_type, pattern_value in self.extract_video_patterns(row_video):
                            events_to_insert.append((
                                analysis_id,
                                pattern_type,
                                pattern_value,
                                weight,
                                now
                            ))
                            graph_patterns[(pattern_type, pattern_value)] += weight

                    for idea in ideas or []:
                        for pattern_type, pattern_value in self.extract_title_patterns(str(idea)):
                            events_to_insert.append((
                                analysis_id,
                                "idea_" + pattern_type,
                                pattern_value,
                                final_score / 100,
                                now
                            ))
                            graph_patterns[("idea_" + pattern_type, pattern_value)] += final_score / 100

                    if events_to_insert:
                        import psycopg2.extras
                        psycopg2.extras.execute_values(
                            cur,
                            """
                            INSERT INTO pattern_events
                            (analysis_id, pattern_type, pattern_value, weight, created_at)
                            VALUES %s
                            """,
                            events_to_insert
                        )

                    self._save_graph_edges_with_cursor(cur, analysis_id, graph_patterns, now)

            # Clear specific caches so the UI updates on next rerun
            get_cached_analyses_count.clear()
            get_cached_pattern_stats.clear()
            get_cached_leaderboard.clear()
            get_cached_recent_analyses.clear()
            get_cached_graph_edges.clear()

            return analysis_id
        finally:
            conn.close()

    def _save_graph_edges_with_cursor(self, cur, analysis_id, graph_patterns, created_at):
        if not graph_patterns:
            return

        top_patterns = graph_patterns.most_common(35)
        edges_to_insert = []

        for i in range(len(top_patterns)):
            (source_type, source_value), source_weight = top_patterns[i]

            for j in range(i + 1, min(i + 12, len(top_patterns))):
                (target_type, target_value), target_weight = top_patterns[j]

                if source_type == target_type and source_value == target_value:
                    continue

                edge_weight = float(min(source_weight, target_weight))
                edges_to_insert.append((
                    analysis_id,
                    source_type,
                    source_value,
                    target_type,
                    target_value,
                    edge_weight,
                    created_at
                ))

        if edges_to_insert:
            import psycopg2.extras
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO graph_edges
                (analysis_id, source_type, source_value, target_type, target_value, weight, created_at)
                VALUES %s
                """,
                edges_to_insert
            )

    def predict_opportunity(self, seeds, df_total, color_rgb=None, ideas=None):
        history_count = get_cached_analyses_count()

        if history_count < 3:
            return {
                "score": None,
                "label": "Memoria insuficiente",
                "confidence": "Baja",
                "motives": [f"Hay {history_count} analisis guardados. Guarda al menos 3 para empezar a predecir."],
                "winning_patterns": []
            }

        current = Counter()

        if df_total is not None and not df_total.empty:
            for _, row in df_total.head(20).iterrows():
                for key in self.extract_video_patterns(row):
                    current[key] += 1

        for idea in ideas or []:
            for key in self.extract_title_patterns(str(idea)):
                current[key] += 1

        if color_rgb:
            current[("color_family", self.color_family(color_rgb))] += 2

        stats = self.pattern_stats()
        matched = []

        for pattern, count in current.items():
            s = stats.get(pattern)
            if not s or s["uses"] < 2:
                continue

            matched.append({
                "pattern": pattern,
                "count": count,
                "avg_weight": s["avg_weight"],
                "uses": s["uses"],
                "impact": s["avg_weight"] * min(count, 3)
            })

        if not matched:
            return {
                "score": 45,
                "label": "Nicho testeable",
                "confidence": "Baja",
                "motives": ["No hay patrones historicos parecidos suficientes."],
                "winning_patterns": []
            }

        impact = sum(m["impact"] for m in matched)
        coverage = min(len(matched) / 10, 1)
        score = int(max(0, min(100, 35 + impact * 18 + coverage * 20)))

        if score >= 75:
            label = "Alta probabilidad segun memoria"
        elif score >= 58:
            label = "Prometedor segun memoria"
        elif score >= 42:
            label = "Testeable segun memoria"
        else:
            label = "Debil segun memoria"

        confidence = "Alta" if len(matched) >= 8 else "Media" if len(matched) >= 4 else "Baja"

        return {
            "score": score,
            "label": label,
            "confidence": confidence,
            "motives": [
                f"Coinciden {len(matched)} patrones con la memoria historica.",
                f"Confianza {confidence.lower()} basada en {history_count} analisis guardados."
            ],
            "winning_patterns": sorted(matched, key=lambda x: x["impact"], reverse=True)[:8]
        }

    def leaderboard(self, limit=30):
        return get_cached_leaderboard(limit)

    def recent_analyses(self, limit=10):
        return get_cached_recent_analyses(limit)

    def graph_data(self, limit_edges=220, min_edge_weight=0.08):
        edges_raw = get_cached_graph_edges(limit_edges, min_edge_weight)

        if edges_raw.empty:
            return pd.DataFrame(), pd.DataFrame()

        consolidated_edges = {}
        node_weights = Counter()
        node_types = {}

        for _, row in edges_raw.iterrows():
            # Filter out recency type nodes from mind map
            if row["source_type"] == "recency" or row["target_type"] == "recency":
                continue

            src_val = clean_and_singularize_label(row["source_value"])
            tgt_val = clean_and_singularize_label(row["target_value"])
            src_type = row["source_type"]
            tgt_type = row["target_type"]
            weight = float(row["total_weight"])
            uses = int(row["uses"])

            if not src_val or not tgt_val or src_val == tgt_val:
                continue

            # Consolidated undirected edges (alphabetical sorting to avoid bidirectional duplicate paths)
            edge_key = tuple(sorted([src_val, tgt_val]))
            
            if edge_key not in consolidated_edges:
                consolidated_edges[edge_key] = {
                    "from": edge_key[0],
                    "to": edge_key[1],
                    "uses": 0,
                    "total_weight": 0.0,
                    "count": 0
                }
            
            consolidated_edges[edge_key]["uses"] += uses
            consolidated_edges[edge_key]["total_weight"] += weight
            consolidated_edges[edge_key]["count"] += 1

            node_weights[src_val] += weight
            node_weights[tgt_val] += weight

            # Retain the most descriptive type (prioritizing non-generic types)
            if src_val not in node_types or src_type != "title_token":
                node_types[src_val] = src_type
            if tgt_val not in node_types or tgt_type != "title_token":
                node_types[tgt_val] = tgt_type

        if not consolidated_edges:
            return pd.DataFrame(), pd.DataFrame()

        edges_list = []
        for edge in consolidated_edges.values():
            edges_list.append({
                "from": edge["from"],
                "to": edge["to"],
                "uses": edge["uses"],
                "total_weight": edge["total_weight"],
                "avg_weight": edge["total_weight"] / max(1, edge["count"])
            })
        edges_df = pd.DataFrame(edges_list)

        nodes_list = []
        for node_id, weight in node_weights.items():
            nodes_list.append({
                "id": node_id,
                "label": node_id,
                "type": node_types.get(node_id, "unknown"),
                "weight": weight
            })
        nodes_df = pd.DataFrame(nodes_list)

        return nodes_df, edges_df

    def extract_video_patterns(self, row):
        title = str(row.get("Title", ""))
        keyword = str(row.get("Keyword_Origen", ""))
        published = str(row.get("Published", "")).lower()

        patterns = []
        patterns.extend(self.extract_title_patterns(title))

        for token in self.important_tokens(keyword):
            patterns.append(("keyword_token", token))

        if "hour" in published or "day" in published:
            patterns.append(("recency", "fresh_48h"))
        elif "week" in published:
            patterns.append(("recency", "fresh_weeks"))
        elif "month" in published:
            patterns.append(("recency", "recent_months"))

        return patterns

    def extract_title_patterns(self, title):
        clean = self.clean(title)
        words = self.important_tokens(clean)
        patterns = []

        for word in words[:12]:
            patterns.append(("title_token", word))

        for i in range(len(words) - 1):
            patterns.append(("title_bigram", f"{words[i]} {words[i + 1]}"))

        rules = {
            "why": r"\bwhy\b",
            "secret": r"\b(secret|hidden|truth)\b",
            "versus": r"\bvs\b|\bversus\b",
            "story": r"\b(story|history|rise|fall)\b",
            "challenge": r"\b(challenge|hardest|impossible)\b",
            "survival": r"\b(survive|survived|survival)\b",
            "ranking": r"\b(best|top|ranking|tier)\b",
            "explainer": r"\b(explained|analysis|breakdown)\b",
            "curiosity_gap": r"\b(nobody|no one|unexpected|weird|strange)\b",
        }

        for name, pattern in rules.items():
            if re.search(pattern, clean):
                patterns.append(("title_format", name))

        return patterns

    def pattern_stats(self):
        return get_cached_pattern_stats()

    def video_weight(self, row):
        multiplier = float(row.get("Multiplicador", 0) or 0)
        views = int(row.get("Views", 0) or 0)
        published = str(row.get("Published", "")).lower()

        weight = min(multiplier / 5, 1)

        if views >= 500000:
            weight += 0.25
        elif views >= 100000:
            weight += 0.15
        elif views >= 30000:
            weight += 0.07

        if "hour" in published or "day" in published or "week" in published:
            weight += 0.15

        return max(0.05, min(weight, 1.5))

    def color_family(self, rgb):
        r, g, b = [int(x) for x in rgb]
        brightness = (r + g + b) / 3

        if brightness < 70:
            tone = "dark"
        elif brightness > 180:
            tone = "light"
        else:
            tone = "mid"

        if r >= g and r >= b:
            hue = "warm"
        elif b >= r and b >= g:
            hue = "cold"
        elif g >= r and g >= b:
            hue = "green"
        else:
            hue = "neutral"

        return f"{tone}_{hue}"

    def important_tokens(self, text):
        stopwords = {
            "the", "and", "for", "with", "from", "this", "that", "your", "you",
            "how", "why", "what", "when", "where", "who", "are", "was", "were",
            "into", "about", "video", "videos", "official", "full", "new", "best",
            "top", "review", "analysis", "explained", "breakdown", "para", "como",
            "esta", "este", "pero", "porque", "todo", "algo", "nada", "youtube",
            "channel", "channels", "subscribe", "subscribers", "views", "shorts",
            "grow", "growth", "viral", "canal", "canales", "vistas", "suscribete",
            "reproducciones", "como", "para", "todo", "todos", "toda", "todas"
        }

        clean = self.clean(text)
        tokens = []
        for w in clean.split():
            if w in stopwords or len(w) <= 2:
                continue
            if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
                w = w[:-1]
            if w not in stopwords and len(w) >= 3:
                tokens.append(w)
        return tokens

    def clean(self, text):
        text = str(text).lower()
        text = re.sub(r"[^\w\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()


def format_pattern(pattern_item):
    pattern_type, pattern_value = pattern_item["pattern"]
    return f"{pattern_type}: {pattern_value}"


memoria = PatternMemory()


def neural_node_color(node_type):
    if "title_token" in node_type:
        return "#3f7df4"
    if "title_bigram" in node_type:
        return "#7c4dff"
    if "title_format" in node_type:
        return "#ff4f7b"
    if "keyword" in node_type:
        return "#00b894"
    if "recency" in node_type:
        return "#ffb703"
    if "idea" in node_type:
        return "#f97316"
    if "color" in node_type:
        return "#22c55e"
    return "#94a3b8"


def clean_and_singularize_label(label):
    label = str(label).lower().strip()
    label = re.sub(r"[^\w\s]", " ", label)
    label = re.sub(r"\s+", " ", label)
    words = label.split()
    clean_words = []
    for w in words:
        if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        clean_words.append(w)
    return " ".join(clean_words).strip()


def render_neural_graph(nodes_df, edges_df, videos_list=None, height=720):
    # ESTO PINTA EL MAPA TIPO OBSIDIAN Y MUESTRA LOS VIDEOS EN UN PANEL INTERACTIVO AL PINCHAR.
    if nodes_df.empty or edges_df.empty:
        st.info("Aun no hay conexiones suficientes. Haz varias busquedas y deja que la memoria IA guarde patrones.")
        return

    nodes_payload = []
    allowed_ids = set(nodes_df["id"].tolist())

    for _, row in nodes_df.iterrows():
        size = 12 + min(float(row["weight"]) * 2.3, 34)
        nodes_payload.append({
            "id": row["id"],
            "label": str(row["label"])[:34],
            "title": f"{row['type']}: {row['label']} | peso {float(row['weight']):.2f}",
            "value": float(row["weight"]),
            "color": neural_node_color(str(row["type"])),
            "font": {"color": "#f8fafc", "size": 18 if size > 24 else 14},
            "shape": "dot",
            "size": size
        })

    edges_payload = []
    for _, row in edges_df.iterrows():
        source_id = row["from"]
        target_id = row["to"]

        if source_id not in allowed_ids or target_id not in allowed_ids:
            continue

        weight = float(row["total_weight"])
        edges_payload.append({
            "from": source_id,
            "to": target_id,
            "value": weight,
            "width": 1 + min(weight, 8),
            "title": f"Conexion: {int(row['uses'])} usos | peso {weight:.2f}",
            "color": {"color": "rgba(148, 163, 184, 0.42)"}
        })

    html_doc = f"""
    <html>
    <head>
      <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
      <style>
        body {{
          margin: 0;
          background: #080d16;
          color: #f8fafc;
          font-family: Inter, Arial, sans-serif;
        }}
        #container {{
          display: flex;
          width: 100%;
          height: {height}px;
          border: 1px solid rgba(255,255,255,.12);
          border-radius: 10px;
          background: #080d16;
          overflow: hidden;
        }}
        #network-canvas {{
          width: 70%;
          height: 100%;
          position: relative;
          background: radial-gradient(circle at 50% 0%, rgba(63,125,244,.15), transparent 60%);
        }}
        #details-panel {{
          width: 30%;
          height: 100%;
          background: rgba(11, 16, 27, 0.96);
          border-left: 1px solid rgba(255, 255, 255, 0.1);
          display: flex;
          flex-direction: column;
          color: #f8fafc;
          overflow-y: auto;
          padding: 16px;
          box-sizing: border-box;
        }}
        .panel-header {{
          font-size: 16px;
          font-weight: 850;
          margin-bottom: 12px;
          padding-bottom: 8px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.15);
          color: #3f7df4;
          text-transform: capitalize;
        }}
        .empty-state {{
          display: flex;
          align-items: center;
          justify-content: center;
          height: 100%;
          text-align: center;
          color: #8d98aa;
          font-size: 13px;
          padding: 16px;
        }}
        .video-item {{
          display: flex;
          gap: 10px;
          background: rgba(255, 255, 255, 0.03);
          padding: 8px;
          border-radius: 8px;
          margin-bottom: 10px;
          text-decoration: none;
          color: inherit;
          transition: background 0.2s, transform 0.2s;
          border: 1px solid rgba(255, 255, 255, 0.05);
        }}
        .video-item:hover {{
          background: rgba(255, 255, 255, 0.08);
          transform: translateY(-2px);
          border-color: rgba(63, 125, 244, 0.4);
        }}
        .video-thumb {{
          width: 80px;
          aspect-ratio: 16 / 9;
          border-radius: 4px;
          object-fit: cover;
          background: #141b29;
        }}
        .video-info {{
          display: flex;
          flex-direction: column;
          justify-content: space-between;
          min-width: 0;
        }}
        .video-title {{
          font-size: 12px;
          font-weight: 700;
          line-height: 1.25;
          margin-bottom: 4px;
          overflow: hidden;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          color: #f8fafc;
        }}
        .video-meta {{
          font-size: 9px;
          color: #8d98aa;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }}
        .video-badge {{
          display: inline-block;
          padding: 1px 4px;
          font-size: 8px;
          font-weight: 900;
          color: white;
          border-radius: 4px;
          margin-right: 4px;
        }}
        .score-hot {{ background: #ff2f63; }}
        .score-mid {{ background: #a24be8; }}
        .score-low {{ background: #3f7df4; }}
        
        @media (max-width: 768px) {{
          #container {{
            flex-direction: column;
            height: auto;
          }}
          #network-canvas {{
            width: 100%;
            height: 450px;
          }}
          #details-panel {{
            width: 100%;
            height: 350px;
            border-left: none;
            border-top: 1px solid rgba(255, 255, 255, 0.1);
          }}
        }}
      </style>
    </head>
    <body>
      <div id="container">
        <div id="network-canvas"></div>
        <div id="details-panel"></div>
      </div>
      <script>
        const nodes = new vis.DataSet({json.dumps(nodes_payload)});
        const edges = new vis.DataSet({json.dumps(edges_payload)});
        const videos = {json.dumps(videos_list or [])};

        const container = document.getElementById("network-canvas");
        const data = {{ nodes, edges }};
        const options = {{
          interaction: {{
            hover: true,
            tooltipDelay: 80,
            navigationButtons: true,
            keyboard: true
          }},
          physics: {{
            enabled: true,
            solver: "forceAtlas2Based",
            forceAtlas2Based: {{
              gravitationalConstant: -100,
              centralGravity: 0.01,
              springLength: 130,
              springConstant: 0.08,
              damping: 0.6
            }},
            stabilization: {{
              enabled: true,
              iterations: 150
            }}
          }},
          edges: {{
            smooth: {{ type: "continuous" }},
            scaling: {{ min: 1, max: 8 }}
          }},
          nodes: {{
            borderWidth: 1,
            borderWidthSelected: 3,
            scaling: {{ min: 10, max: 45 }}
          }}
        }};
        
        const network = new vis.Network(container, data, options);
        const panel = document.getElementById("details-panel");
        
        function formatNumber(num) {{
            if (num >= 1000000) return (num / 1000000).toFixed(1) + "M";
            if (num >= 1000) return (num / 1000).toFixed(0) + "K";
            return num;
        }}
        
        function getBadgeClass(multiplier) {{
            if (multiplier >= 8) return "score-hot";
            if (multiplier >= 4) return "score-mid";
            return "score-low";
        }}

        function showVideosForNode(nodeLabel) {{
            panel.innerHTML = "";
            
            const header = document.createElement("div");
            header.className = "panel-header";
            header.innerText = "Videos: \"" + nodeLabel + "\"";
            panel.appendChild(header);
            
            const matches = videos.filter(v => {{
                const title = (v.Title || "").toLowerCase();
                const keyword = (v.Keyword_Origen || "").toLowerCase();
                const cleanLabel = nodeLabel.toLowerCase();
                return title.includes(cleanLabel) || keyword.includes(cleanLabel);
            }});
            
            if (matches.length === 0) {{
                const empty = document.createElement("div");
                empty.className = "empty-state";
                empty.innerText = "No se encontraron videos activos de esta tanda que contengan esta palabra.";
                panel.appendChild(empty);
                return;
            }}
            
            // Sort matches by outlier multiplier descending
            matches.sort((a, b) => b.Multiplicador - a.Multiplicador);
            
            matches.forEach(v => {{
                const item = document.createElement("a");
                item.className = "video-item";
                item.href = v.URL;
                item.target = "_blank";
                
                const badgeClass = getBadgeClass(v.Multiplicador);
                
                item.innerHTML = `
                    <img class="video-thumb" src="${v.Thumbnail}" alt="">
                    <div class="video-info" style="flex: 1; min-width: 0;">
                        <div class="video-title">${v.Title}</div>
                        <div style="display: flex; align-items: center; margin-bottom: 2px;">
                            <span class="video-badge ${badgeClass}">${v.Multiplicador.toFixed(1)}x</span>
                            <span class="video-meta" style="flex: 1;">${v.Channel}</span>
                        </div>
                        <div class="video-meta">${formatNumber(v.Views)} vistas - ${v.Published}</div>
                    </div>
                `;
                panel.appendChild(item);
            }});
        }}
        
        network.on("selectNode", function (params) {{
            const selectedNodeId = params.nodes[0];
            const nodeData = nodes.get(selectedNodeId);
            if (nodeData) {{
                showVideosForNode(nodeData.label);
            }}
        }});
        
        network.on("deselectNode", function () {{
            showDefaultPanel();
        }});
        
        function showDefaultPanel() {{
            panel.innerHTML = "";
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.innerHTML = videos.length > 0 
                ? "👈 Haz clic en cualquier bola del mapa para ver los videos outliers de esta tanda."
                : "Inicia una busqueda de nicho arriba para ver y explorar los videos outliers.";
            panel.appendChild(empty);
        }}
        
        showDefaultPanel();
      </script>
    </body>
    </html>
    """

    components.html(html_doc, height=height + 20, scrolling=False)


def build_analysis_signature(seeds, df_total):
    # ESTO SIRVE PARA NO GUARDAR LA MISMA BUSQUEDA 20 VECES CUANDO STREAMLIT RECARGA.
    urls = []
    if df_total is not None and not df_total.empty and "URL" in df_total.columns:
        urls = sorted([str(u) for u in df_total["URL"].dropna().head(50).tolist()])

    raw = json.dumps(
        {
            "seeds": seeds,
            "urls": urls,
            "total": 0 if df_total is None else int(len(df_total))
        },
        sort_keys=True
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def autosave_analysis_once(seeds, df_total, final_score, auto_score, reading, color_rgb, ideas):
    # ESTO GUARDA CADA BUSQUEDA EN LA MEMORIA IA UNA SOLA VEZ.
    if not AUTO_SAVE_SEARCHES_TO_MEMORY:
        return None

    if df_total is None or df_total.empty:
        return None

    signature = build_analysis_signature(seeds, df_total)
    saved = st.session_state.get("saved_analysis_signatures", set())

    if signature in saved:
        return None

    # Guardar firma localmente para no repetir
    saved.add(signature)
    st.session_state.saved_analysis_signatures = saved

    # Guardar en base de datos en segundo plano sin bloquear la UI
    def save_background():
        try:
            mem = PatternMemory()
            mem.save_analysis(
                seeds,
                df_total,
                final_score,
                auto_score,
                reading,
                color_rgb,
                ideas,
                notes="Guardado automatico en segundo plano"
            )
        except Exception:
            pass

    import threading
    t = threading.Thread(target=save_background, daemon=True)
    t.start()

    return "background"


# =========================================================
# ESTILO VISUAL
# ESTO SIRVE PARA CAMBIAR EL ASPECTO DE LA APP:
# COLORES, TARJETAS, MINIATURAS, TITULOS Y DISTRIBUCION.
# =========================================================

def inject_css():
    st.markdown("""
    <style>
    .stApp {
        background: radial-gradient(circle at 50% 0%, #171f31 0, #0b101b 38%, #080d16 100%);
        color: #f7f8fc;
    }

    .block-container {
        max-width: 1700px;
        padding-top: 12px;
        padding-left: 28px;
        padding-right: 28px;
    }

    [data-testid="stSidebar"] {
        background: #080d16;
        border-right: 1px solid rgba(255,255,255,.10);
    }

    [data-testid="stSidebar"] * {
        color: #f7f8fc;
    }

    .top-banner {
        background: #ffd21f;
        color: #05070d;
        font-weight: 900;
        text-align: center;
        padding: 12px 18px;
        border-radius: 8px;
        margin-bottom: 24px;
    }

    .page-title {
        font-size: 34px;
        line-height: 1;
        font-weight: 950;
        margin: 28px 0 22px;
    }

    .chips {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin: 18px 0 34px;
    }

    .chip {
        background: #427cf4;
        color: white;
        border-radius: 999px;
        padding: 8px 13px;
        font-size: 15px;
        font-weight: 800;
    }

    .video-card-fixed {
        width: 100%;
        min-width: 0;
        margin-bottom: 28px;
    }

    .thumb-wrap-fixed {
        position: relative;
        display: block;
        width: 100%;
        aspect-ratio: 16 / 9;
        border-radius: 10px;
        overflow: hidden;
        background: #141b29;
        box-shadow: 0 14px 36px rgba(0,0,0,.28);
        text-decoration: none;
    }

    .thumb-wrap-fixed img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
    }

    .duration-fixed {
        position: absolute;
        right: 7px;
        bottom: 7px;
        background: rgba(0,0,0,.88);
        color: white;
        border-radius: 6px;
        padding: 3px 6px;
        font-size: 12px;
        font-weight: 900;
    }

    .card-title-row-fixed {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr);
        gap: 9px;
        align-items: start;
        margin-top: 12px;
    }

    .score-badge {
        color: white;
        border-radius: 8px;
        padding: 5px 8px;
        font-size: 14px;
        line-height: 1;
        font-weight: 950;
        white-space: nowrap;
    }

    .score-hot { background: #ff2f63; }
    .score-mid { background: #a24be8; }
    .score-low { background: #3f7df4; }

    .video-title-fixed {
        color: #f7f8fc;
        font-size: 17px;
        line-height: 1.22;
        font-weight: 950;
        overflow: hidden;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
    }

    .meta-fixed {
        color: #8d98aa;
        font-size: 13px;
        line-height: 1.35;
        margin-top: 7px;
    }
    </style>
    """, unsafe_allow_html=True)


def compact_number(value):
    try:
        value = float(value)
    except Exception:
        return "0"

    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(int(value))


def score_class(multiplier):
    try:
        multiplier = float(multiplier)
    except Exception:
        multiplier = 0

    if multiplier >= 8:
        return "score-hot"
    if multiplier >= 4:
        return "score-mid"
    return "score-low"


def render_video_card(row):
    title = html.escape(str(row.get("Title", "Sin titulo")))
    channel = html.escape(str(row.get("Channel", "Canal")))
    subs = html.escape(str(row.get("Subscribers", "Unknown")))
    url = html.escape(str(row.get("URL", "#")))
    thumb = html.escape(str(row.get("Thumbnail", "")))
    published = html.escape(str(row.get("Published", "")))
    views = compact_number(row.get("Views", 0))
    multiplier = float(row.get("Multiplicador", 0) or 0)
    badge_class = score_class(multiplier)

    st.markdown(f"""
    <div class="video-card-fixed">
        <a class="thumb-wrap-fixed" href="{url}" target="_blank">
            <img src="{thumb}" alt="{title}">
            <span class="duration-fixed">outlier</span>
        </a>
        <div class="card-title-row-fixed">
            <span class="score-badge {badge_class}">{multiplier:.1f}x</span>
            <div class="video-title-fixed">{title}</div>
        </div>
        <div class="meta-fixed">
            {channel} - {subs} subs<br>
            {views} views - {published}
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_outlier_cards(df_total):
    df = df_total.sort_values("Multiplicador", ascending=False).head(30)

    for start in range(0, len(df), 5):
        cols = st.columns(5, gap="large")
        bloque = df.iloc[start:start + 5]

        for col, (_, row) in zip(cols, bloque.iterrows()):
            with col:
                render_video_card(row)


# =========================================================
# MINERO YOUTUBE
# ESTO SIRVE PARA PEDIR SUGERENCIAS, BUSCAR VIDEOS,
# SACAR VISTAS, CANALES, MINIATURAS, SUBS Y TRANSCRIPCIONES.
# =========================================================

class YouTubeHyperMiner:
    def __init__(self, memory=None):
        self.memory = memory
        self.subs_cache = {}
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }

    def get_youtube_suggestions(self, keyword):
        cache_key = f"suggestions:v1:{keyword.lower().strip()}"
        cached = self._cache_get(cache_key, ttl_seconds=7 * 24 * 3600)
        if cached is not None:
            return cached

        url = f"https://suggestqueries.google.com/complete/search?client=youtube&hl=en&ds=yt&q={quote(keyword)}"
        try:
            response = requests.get(url, headers=self.headers, timeout=7)
            if response.status_code == 200:
                clean_text = response.text
                if "(" in clean_text:
                    clean_text = clean_text[clean_text.find("(")+1:clean_text.rfind(")")]
                data = json.loads(clean_text)
                suggestions = [item[0] for item in data[1] if item[0].lower() != keyword.lower()]
                self._cache_set(cache_key, suggestions)
                return suggestions
        except Exception:
            pass
        return []

    def scrape_keyword_videos(self, keyword):
        cache_key = f"search:v2:{keyword.lower().strip()}"
        cached = self._cache_get(cache_key, ttl_seconds=24 * 3600)
        if cached is not None:
            return cached

        search_url = f"https://www.youtube.com/results?search_query={quote(keyword)}&sp=CAI%253D"
        videos = []

        try:
            response = requests.get(search_url, headers=self.headers, timeout=12)
            if response.status_code == 200:
                html_text = response.text
                json_match = re.search(r'var ytInitialData = ({.*?});</script>', html_text)

                if json_match:
                    yt_data = json.loads(json_match.group(1))
                    contents = yt_data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]

                    video_items = []
                    for item in contents:
                        if "itemSectionRenderer" in item:
                            video_items.extend(item["itemSectionRenderer"].get("contents", []))

                    for item in video_items:
                        if "videoRenderer" in item:
                            v_renderer = item["videoRenderer"]
                            time_text = v_renderer.get("publishedTimeText", {}).get("simpleText", "Unknown")

                            if "year" in time_text.lower() or "ano" in time_text.lower():
                                continue

                            title = v_renderer.get("title", {}).get("runs", [{}])[0].get("text", "Sin titulo")
                            v_id = v_renderer.get("videoId", "")

                            byline_run = v_renderer.get("longBylineText", {}).get("runs", [{}])[0]
                            channel_name = byline_run.get("text", "Canal")

                            channel_endpoint = byline_run.get("navigationEndpoint", {})
                            channel_id = channel_endpoint.get("browseEndpoint", {}).get("browseId", "")

                            channel_path = (
                                channel_endpoint
                                .get("commandMetadata", {})
                                .get("webCommandMetadata", {})
                                .get("url", "")
                            )
                            channel_url = f"https://www.youtube.com{channel_path}" if channel_path else ""

                            views_text = v_renderer.get("viewCountText", {}).get("simpleText", "0")
                            views = self._parse_views(views_text)

                            if v_id and views > 0:
                                videos.append({
                                    "Title": title,
                                    "Channel": channel_name,
                                    "Channel_ID": channel_id,
                                    "Channel_URL": channel_url,
                                    "Views": views,
                                    "Published": time_text,
                                    "URL": f"https://www.youtube.com/watch?v={v_id}",
                                    "ID": v_id,
                                    "Thumbnail": f"https://img.youtube.com/vi/{v_id}/hqdefault.jpg"
                                })
        except Exception:
            pass

        if videos:
            self._cache_set(cache_key, videos)

        return videos

    def scrape_recommended_videos(self, video_id, source_title="", limit=12):
        # ESTO SIRVE PARA MIRAR EL FEED RECOMENDADO DE UN VIDEO OUTLIER.
        # ES UNA SENAL MUY BUENA PORQUE YOUTUBE RELACIONA ESOS VIDEOS CON EL NICHO.
        if not video_id:
            return []

        cache_key = f"recommended:v2:{video_id}"
        cached = self._cache_get(cache_key, ttl_seconds=24 * 3600)
        if cached is not None:
            return cached[:limit]

        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            response = requests.get(url, headers=self.headers, timeout=12)

            if response.status_code != 200:
                return []

            yt_data = self._extract_yt_initial_data(response.text)
            if not yt_data:
                return []

            recommended = []
            seen = set()

            for renderer in self._find_renderers(yt_data, "compactVideoRenderer"):
                rec_id = renderer.get("videoId", "")

                if not rec_id or rec_id == video_id or rec_id in seen:
                    continue

                seen.add(rec_id)
                title = self._text_from_runs(renderer.get("title", {}))
                channel = self._text_from_runs(renderer.get("shortBylineText", {})) or self._text_from_runs(renderer.get("longBylineText", {}))
                views_text = self._text_from_runs(renderer.get("viewCountText", {}))
                published = self._text_from_runs(renderer.get("publishedTimeText", {})) or "Unknown"

                recommended.append({
                    "Title": title or "Sin titulo",
                    "Channel": channel or "Canal",
                    "Views": self._parse_views(views_text),
                    "Published": published,
                    "URL": f"https://www.youtube.com/watch?v={rec_id}",
                    "ID": rec_id,
                    "Thumbnail": f"https://img.youtube.com/vi/{rec_id}/hqdefault.jpg",
                    "Source_Video_ID": video_id,
                    "Source_Title": source_title,
                    "Signal": "Feed recomendado"
                })

                if len(recommended) >= limit:
                    break

            self._cache_set(cache_key, recommended)
            return recommended
        except Exception:
            return []

    def extract_script_keywords(self, video_id):
        cache_key = f"transcript:v1:{video_id}"
        cached = self._cache_get(cache_key, ttl_seconds=14 * 24 * 3600)
        if cached is not None:
            return cached

        try:
            transcript = None

            try:
                ytt_api = YouTubeTranscriptApi()
                fetched = ytt_api.fetch(video_id, languages=['en', 'es'])
                transcript = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else fetched
            except Exception:
                transcript = None

            if transcript is None:
                try:
                    transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'es'])
                except Exception:
                    transcript = None

            if transcript is None:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                try:
                    transcript_obj = transcript_list.find_transcript(['en', 'es'])
                except Exception:
                    transcript_obj = transcript_list.find_generated_transcript(['en', 'es'])

                transcript = transcript_obj.fetch()
                if hasattr(transcript, "to_raw_data"):
                    transcript = transcript.to_raw_data()

            textos = []
            for t in transcript:
                if isinstance(t, dict):
                    textos.append(t.get("text", ""))
                else:
                    textos.append(getattr(t, "text", ""))

            full_text = " ".join(textos).lower()
            full_text = re.sub(r'[^\w\s]', ' ', full_text)
            full_text = re.sub(r'\s+', ' ', full_text).strip()
            self._cache_set(cache_key, full_text)
            return full_text

        except Exception:
            return ""

    def get_video_subscribers(self, video_id):
        if not video_id:
            return "Unknown"

        if video_id in self.subs_cache:
            return self.subs_cache[video_id]

        cache_key = f"subs:v1:{video_id}"
        cached = self._cache_get(cache_key, ttl_seconds=7 * 24 * 3600)
        if cached is not None:
            self.subs_cache[video_id] = cached
            return cached

        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            response = requests.get(url, headers=self.headers, timeout=10)

            if response.status_code != 200:
                self.subs_cache[video_id] = "Unknown"
                return "Unknown"

            html_text = response.text

            patterns = [
                r'"ownerSubCountText":\{"simpleText":"([^"]+)"\}',
                r'"ownerSubCountText":\{"runs":\[\{"text":"([^"]+)"\}',
                r'"subscriberCountText":\{"simpleText":"([^"]+)"\}',
                r'"subscriberCountText":\{"runs":\[\{"text":"([^"]+)"\}'
            ]

            for pattern in patterns:
                match = re.search(pattern, html_text)
                if match:
                    subs = match.group(1)
                    self.subs_cache[video_id] = subs
                    self._cache_set(cache_key, subs)
                    return subs

            self.subs_cache[video_id] = "Hidden/Unknown"
            self._cache_set(cache_key, "Hidden/Unknown")
            return "Hidden/Unknown"

        except Exception:
            self.subs_cache[video_id] = "Unknown"
            return "Unknown"

    def _cache_get(self, cache_key, ttl_seconds=None):
        if not self.memory:
            return None
        return self.memory.cache_get(cache_key, ttl_seconds=ttl_seconds)

    def _cache_set(self, cache_key, payload):
        if self.memory:
            self.memory.cache_set(cache_key, payload)

    def _extract_yt_initial_data(self, html_text):
        match = re.search(r'var ytInitialData = ({.*?});</script>', html_text, re.DOTALL)
        if not match:
            match = re.search(r'ytInitialData"\]\s*=\s*({.*?});', html_text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except Exception:
            return None

    def _find_renderers(self, data, renderer_name):
        if isinstance(data, dict):
            if renderer_name in data and isinstance(data[renderer_name], dict):
                yield data[renderer_name]
            for value in data.values():
                yield from self._find_renderers(value, renderer_name)
        elif isinstance(data, list):
            for item in data:
                yield from self._find_renderers(item, renderer_name)

    def _text_from_runs(self, obj):
        if not isinstance(obj, dict):
            return ""
        if "simpleText" in obj:
            return str(obj.get("simpleText", ""))
        runs = obj.get("runs", [])
        if isinstance(runs, list):
            return "".join([str(run.get("text", "")) for run in runs if isinstance(run, dict)]).strip()
        return ""

    def _parse_views(self, text):
        clean = re.sub(r'[^\d\.KMBkmb]', '', str(text))
        if not clean:
            return 0
        try:
            if 'K' in clean or 'k' in clean:
                return int(float(clean.lower().replace('k', '')) * 1000)
            if 'M' in clean or 'm' in clean:
                return int(float(clean.lower().replace('m', '')) * 1000000)
            return int(clean)
        except ValueError:
            return 0


# =========================================================
# FUNCIONES DE ANALISIS
# ESTO SIRVE PARA EXPANDIR KEYWORDS, FILTRAR OUTLIERS,
# CALCULAR NOTAS, CREAR IDEAS Y VALIDAR SI UN NICHO ES BUENO.
# =========================================================

def generar_keywords_red_autocomplete(miner, semillas_iniciales, max_keywords=250, profundidad=3):
    keywords = []
    vistos = set()
    pendientes = []

    stopwords = {
        "the", "and", "for", "with", "from", "this", "that", "your", "you",
        "how", "why", "what", "when", "where", "who", "is", "are", "was",
        "were", "to", "in", "of", "a", "an", "on", "by", "about", "all",
        "full", "video", "videos", "official", "new", "best", "top"
    }

    expansores_base = list("abcdefghijklmnopqrstuvwxyz") + [
        "2024", "2025", "2026",
        "explained", "theory", "story", "ending", "secret", "secrets",
        "characters", "timeline", "facts", "hidden details", "things you missed",
        "review", "breakdown", "analysis", "vs", "ranking", "truth"
    ]

    def limpiar_kw(kw):
        return re.sub(r"\s+", " ", str(kw).lower().strip())

    def add_keyword(kw):
        kw = limpiar_kw(kw)
        if kw and kw not in vistos and len(kw) > 2:
            vistos.add(kw)
            keywords.append(kw)
            pendientes.append(kw)

    def extraer_conceptos(frase):
        frase = limpiar_kw(frase)
        palabras = re.findall(r"\w+", frase)
        palabras_filtradas = [p for p in palabras if p not in stopwords and len(p) > 2]

        conceptos = []
        conceptos.extend(palabras_filtradas)

        for i in range(len(palabras_filtradas) - 1):
            conceptos.append(f"{palabras_filtradas[i]} {palabras_filtradas[i + 1]}")

        for i in range(len(palabras_filtradas) - 2):
            conceptos.append(f"{palabras_filtradas[i]} {palabras_filtradas[i + 1]} {palabras_filtradas[i + 2]}")

        return conceptos

    for semilla in semillas_iniciales:
        semilla = limpiar_kw(semilla)
        add_keyword(semilla)

        for exp in expansores_base:
            add_keyword(f"{semilla} {exp}")

    nivel = 0

    while pendientes and len(keywords) < max_keywords and nivel < profundidad:
        ronda = pendientes[:]
        pendientes = []

        for kw in ronda:
            if len(keywords) >= max_keywords:
                break

            consultas = [kw]

            for exp in expansores_base[:35]:
                consultas.append(f"{kw} {exp}")

            for consulta in consultas:
                if len(keywords) >= max_keywords:
                    break

                sugerencias = miner.get_youtube_suggestions(consulta)

                for sug in sugerencias:
                    if len(keywords) >= max_keywords:
                        break

                    sug = limpiar_kw(sug)
                    add_keyword(sug)
                    conceptos = extraer_conceptos(sug)

                    for concepto in conceptos:
                        if len(keywords) >= max_keywords:
                            break

                        add_keyword(concepto)

                        for semilla in semillas_iniciales:
                            semilla = limpiar_kw(semilla)
                            add_keyword(f"{semilla} {concepto}")
                            add_keyword(f"{concepto} explained")
                            add_keyword(f"{concepto} theory")
                            add_keyword(f"{concepto} story")

                time.sleep(0.03)

        nivel += 1

    return keywords[:max_keywords]


def parse_number_label(texto):
    if texto is None:
        return 0

    if isinstance(texto, (int, float)):
        return int(texto)

    texto = str(texto).lower().replace(",", ".")
    texto = texto.replace("subscribers", "").replace("subs", "").strip()

    match = re.search(r"(\d+(?:\.\d+)?)\s*([kmb]?)", texto)
    if not match:
        return 0

    num = float(match.group(1))
    suf = match.group(2)

    if suf == "k":
        num *= 1_000
    elif suf == "m":
        num *= 1_000_000
    elif suf == "b":
        num *= 1_000_000_000

    return int(num)


def published_age_days(published):
    if not isinstance(published, str):
        return None

    p = published.lower().strip()
    match = re.search(r"(\d+)", p)
    n = int(match.group(1)) if match else 1

    if "hour" in p or "hora" in p:
        return 0
    if "day" in p or "dia" in p or "dia" in p:
        return n
    if "week" in p or "semana" in p:
        return n * 7
    if "month" in p or "mes" in p:
        return n * 30
    if "year" in p or "ano" in p:
        return n * 365

    return None


def filtrar_outliers_por_sidebar(df_total, periodo_publicacion, rango_subs):
    df = df_total.copy()

    if periodo_publicacion == "Ultimos 3 meses":
        df = df[df["Published"].apply(lambda p: (published_age_days(p) or 99999) <= 90)]
    elif periodo_publicacion == "Ultimos 6 meses":
        df = df[df["Published"].apply(lambda p: (published_age_days(p) or 99999) <= 180)]
    elif periodo_publicacion == "Cualquier reciente":
        df = df[df["Published"].apply(lambda p: (published_age_days(p) or 99999) <= 365)]

    if rango_subs == "Canales pequenos 0 - 100K":
        df = df[df["Subscribers"].apply(lambda s: 0 < parse_number_label(s) <= 100000)]
    elif rango_subs == "0 - 500K":
        df = df[df["Subscribers"].apply(lambda s: 0 < parse_number_label(s) <= 500000)]

    return df.sort_values(by="Multiplicador", ascending=False)


def fecha_es_buena(published):
    age = published_age_days(published)
    return age is not None and age <= 180


def recency_points(published):
    age = published_age_days(published)

    if age is None:
        return 0
    if age <= 2:
        return 20
    if age <= 14:
        return 18
    if age <= 90:
        return 12
    if age <= 180:
        return 7

    return 3


def extraer_conceptos_de_texto(texto, limite=12):
    stopwords = {
        'the', 'and', 'you', 'that', 'this', 'with', 'they', 'have', 'what', 'about',
        'just', 'like', 'your', 'here', 'there', 'from', 'then', 'gonna', 'wanna',
        'know', 'back', 'down', 'into', 'them', 'for', 'are', 'was', 'were', 'when',
        'where', 'why', 'how', 'can', 'could', 'would', 'should', 'their', 'these',
        'those', 'thing', 'things', 'really', 'actually', 'going', 'make', 'made',
        'much', 'many', 'more', 'most', 'very', 'some', 'also', 'because', 'first',
        'video', 'videos', 'course', 'email', 'people', 'channel',
        'para', 'como', 'esta', 'este', 'bueno', 'bien', 'pero', 'porque',
        'entonces', 'hacer', 'todo', 'algo', 'nada', 'cuando'
    }

    palabras = str(texto).split()
    palabras_filtradas = [p for p in palabras if p not in stopwords and len(p) > 4]
    return Counter(palabras_filtradas).most_common(limite)


def evaluar_destaca(row):
    views = row.get("Views", 0)
    subs = parse_number_label(row.get("Subscribers", ""))
    multi = row.get("Multiplicador", 0)
    published = row.get("Published", "")

    motivos = []

    if multi >= 3:
        motivos.append(f"multiplicador muy alto ({multi:.1f}x)")
    elif multi >= 2:
        motivos.append(f"multiplicador fuerte ({multi:.1f}x)")

    if subs > 0 and views >= subs:
        motivos.append("mas vistas que subs del canal")

    if subs > 0 and views >= subs * 2:
        motivos.append("duplica o supera los subs")

    if fecha_es_buena(published):
        motivos.append("video reciente")

    if views >= 100000:
        motivos.append("buen volumen de vistas")

    destaca = "Si" if motivos else "No"
    razon = ", ".join(motivos) if motivos else "No destaca con las reglas actuales"

    return destaca, razon


def crear_tabla_referencias(df_total):
    filas = []

    for _, row in df_total.head(20).iterrows():
        destaca, razon = evaluar_destaca(row)

        filas.append({
            "Idea del video": row.get("Title", ""),
            "Canal": row.get("Channel", ""),
            "Link": row.get("URL", ""),
            "Vistas": int(row.get("Views", 0)),
            "Subs canal": row.get("Subscribers", "Unknown"),
            "Fecha": row.get("Published", "Unknown"),
            "Destaca?": destaca,
            "Por que llama la atencion?": razon
        })

    return pd.DataFrame(filas)


def crear_tabla_validacion(df_total):
    filas = []

    for idea, grupo in df_total.groupby("Keyword_Origen"):
        grupo = grupo.sort_values(by="Multiplicador", ascending=False).head(3)

        refs = grupo["Title"].tolist()
        while len(refs) < 3:
            refs.append("")

        hay_3 = len(grupo) >= 3
        canal_pequeno = False
        destacados = 0
        max_multi = grupo["Multiplicador"].max() if not grupo.empty else 0

        for _, row in grupo.iterrows():
            subs = parse_number_label(row.get("Subscribers", ""))
            destaca, _ = evaluar_destaca(row)

            if 0 < subs <= 100000:
                canal_pequeno = True

            if destaca == "Si":
                destacados += 1

        if hay_3 and canal_pequeno and destacados >= 2:
            prioridad = "Alta"
        elif hay_3 or canal_pequeno or max_multi >= 3:
            prioridad = "Media"
        else:
            prioridad = "Baja"

        filas.append({
            "Idea": idea,
            "Video ref. 1": refs[0],
            "Video ref. 2": refs[1],
            "Video ref. 3": refs[2],
            "Hay 3 videos ref.?": "Si" if hay_3 else "No",
            "Alguno es de un canal pequeno?": canal_pequeno,
            "Prioridad": prioridad
        })

    return pd.DataFrame(filas)


def resumen_validacion_final(tabla_referencias, tabla_validacion):
    if tabla_referencias.empty and tabla_validacion.empty:
        return 0, "Sin datos", []

    motivos = []
    score = 0

    total_refs = len(tabla_referencias)
    destacados = 0
    canales_pequenos_refs = 0
    subs_desconocidos = 0
    refs_recientes = 0

    for _, row in tabla_referencias.iterrows():
        destaca = str(row.get("Destaca?", "")).lower()
        subs = parse_number_label(row.get("Subs canal", ""))
        fecha = row.get("Fecha", "")

        if "si" in destaca or "si" in destaca:
            destacados += 1

        if subs == 0:
            subs_desconocidos += 1

        if 0 < subs <= 100000:
            canales_pequenos_refs += 1

        if fecha_es_buena(str(fecha)):
            refs_recientes += 1

    if total_refs >= 3:
        score += 10
        motivos.append("Hay suficientes videos de referencia para comparar.")

    if destacados >= 5:
        score += 25
        motivos.append(f"Hay {destacados} videos marcados como destacados.")
    elif destacados >= 3:
        score += 18
        motivos.append(f"Hay {destacados} videos destacados.")
    elif destacados >= 1:
        score += 8
        motivos.append("Hay al menos un video destacado.")

    if canales_pequenos_refs >= 2:
        score += 25
        motivos.append(f"Hay {canales_pequenos_refs} canales pequenos con senales fuertes.")
    elif canales_pequenos_refs >= 1:
        score += 15
        motivos.append("Hay al menos un canal pequeno validando la oportunidad.")

    if refs_recientes >= 3:
        score += 15
        motivos.append("Hay varias referencias recientes.")
    elif refs_recientes >= 1:
        score += 7
        motivos.append("Hay alguna referencia reciente.")

    if not tabla_validacion.empty:
        altas = len(tabla_validacion[tabla_validacion["Prioridad"] == "Alta"])
        medias = len(tabla_validacion[tabla_validacion["Prioridad"] == "Media"])

        if altas >= 1:
            score += 12
            motivos.append("Hay al menos una idea en prioridad alta.")
        elif medias >= 2:
            score += 8
            motivos.append("Hay varias ideas en prioridad media.")

    if subs_desconocidos > 0:
        motivos.append(f"{subs_desconocidos} referencias tienen subs desconocidos: conviene revisarlos a mano.")

    score = min(score, 100)

    if score >= 75:
        lectura = "FUEGO Nicho rentable / Alta prioridad"
    elif score >= 55:
        lectura = "PROMETEDOR Nicho prometedor"
    elif score >= 35:
        lectura = "TEST Nicho testeable"
    else:
        lectura = "FRIO Senal debil"

    return score, lectura, motivos


def generar_nichos_similares(semillas_iniciales, historico_outliers, guiones_acumulados, limite=40):
    stopwords = {
        'the', 'and', 'you', 'that', 'this', 'with', 'they', 'have', 'what', 'about',
        'just', 'like', 'your', 'here', 'there', 'from', 'then', 'for', 'are', 'was',
        'were', 'when', 'where', 'why', 'how', 'can', 'could', 'would', 'should',
        'video', 'videos', 'explained', 'analysis', 'review', 'theory', 'story',
        'secret', 'ending', 'facts', 'breakdown', 'official', 'trailer', 'movie',
        'full', 'new', 'best', 'top'
    }

    semillas_texto = " ".join(semillas_iniciales).lower()
    palabras_semilla = set(semillas_texto.split())
    candidatos = []

    if historico_outliers:
        df_temp = pd.DataFrame(historico_outliers).drop_duplicates(subset=["URL"])
        textos_fuente = []
        textos_fuente.extend(df_temp["Title"].tolist())
        textos_fuente.extend(df_temp["Keyword_Origen"].tolist())

        texto = re.sub(r'[^\w\s]', ' ', " ".join(textos_fuente).lower())
        palabras = [p for p in texto.split() if p not in stopwords and p not in palabras_semilla and len(p) > 3]

        for palabra, _ in Counter(palabras).most_common(30):
            candidatos.append(palabra)

    nichos = []
    plantillas_vecinas = [
        "{} explained", "{} theory", "{} secrets", "{} story", "{} analysis",
        "{} ending explained", "{} hidden details", "{} things you missed",
        "{} similar topics", "{} characters explained", "{} lore", "{} timeline"
    ]

    for candidato in candidatos:
        candidato = candidato.strip()
        if candidato:
            for plantilla in plantillas_vecinas:
                nichos.append(plantilla.format(candidato))

    nichos_limpios = []
    vistos = set()

    for n in nichos:
        n = re.sub(r'\s+', ' ', n).strip().lower()
        if n not in vistos and len(n) > 5:
            vistos.add(n)
            nichos_limpios.append(n)

    return nichos_limpios[:limite]


@st.cache_data
def analizar_patron_ganador(urls_miniaturas):
    colores_dominantes = []

    for url in urls_miniaturas:
        try:
            response = requests.get(url, timeout=10)
            color_thief = ColorThief(BytesIO(response.content))
            colores_dominantes.append(color_thief.get_color(quality=1))
        except Exception:
            continue

    if not colores_dominantes:
        return (30, 30, 30)

    promedio_rgb = np.mean(colores_dominantes, axis=0).astype(int)
    return (int(promedio_rgb[0]), int(promedio_rgb[1]), int(promedio_rgb[2]))


def crear_base_miniatura(color_base):
    return Image.new('RGB', (1280, 720), color=color_base)


def generar_direccion_miniatura(df_total, color_rgb):
    r, g, b = color_rgb
    brillo = (r + g + b) / 3
    textos = " ".join(df_total["Title"].head(12).tolist()).lower()

    tokens = re.findall(r'\w{4,}', textos)
    stop = {"with", "from", "this", "that", "your", "video", "official", "movie", "full", "explained"}
    temas = [t for t in tokens if t not in stop]
    top = [t for t, _ in Counter(temas).most_common(3)]

    instrucciones = []

    if brillo < 85:
        instrucciones.append("Base visual oscura: usa texto claro, borde grueso y un elemento muy iluminado.")
    elif brillo > 170:
        instrucciones.append("Base visual clara: usa texto oscuro o rojo/negro para contraste fuerte.")
    else:
        instrucciones.append("Base visual media: conviene exagerar contraste con sombras y contornos.")

    if r > g and r > b:
        instrucciones.append("El nicho tira a tonos calidos: rojo/naranja puede funcionar para peligro, emocion o urgencia.")
    elif b > r and b > g:
        instrucciones.append("El nicho tira a tonos frios: azul/cian puede funcionar para misterio, tecnologia o fantasia.")
    elif g > r and g > b:
        instrucciones.append("El nicho tira a tonos verdes: usalo para naturaleza, juego, crecimiento, rareza o toxicidad.")
    else:
        instrucciones.append("Color dominante neutro: apoyate mas en caras, flechas, circulos y contraste.")

    if top:
        instrucciones.append(f"Elementos a probar en miniatura: {', '.join(top)}.")

    instrucciones.append("Texto recomendado: 2 a 4 palabras, grande, sin frases largas.")
    instrucciones.append("Promesa visual: antes/despues, secreto, peligro, reto extremo o comparacion clara.")

    return instrucciones


def calcular_score_oportunidad(df_total):
    if df_total.empty:
        return 0, []

    motivos = []
    score = 0

    max_multi = df_total["Multiplicador"].max()
    outliers_2x = len(df_total[df_total["Multiplicador"] >= 2.0])
    outliers_3x = len(df_total[df_total["Multiplicador"] >= 3.0])

    if max_multi >= 5:
        score += 25
        motivos.append(f"Hay un video muy fuerte con {max_multi:.1f}x sobre la mediana.")
    elif max_multi >= 3:
        score += 18
        motivos.append(f"Hay un video potente con {max_multi:.1f}x sobre la mediana.")
    elif max_multi >= 2:
        score += 12
        motivos.append(f"Hay senales moderadas de outlier con {max_multi:.1f}x.")

    if outliers_3x >= 3:
        score += 20
        motivos.append(f"Hay {outliers_3x} videos por encima de 3x.")
    elif outliers_2x >= 4:
        score += 15
        motivos.append(f"Hay {outliers_2x} videos por encima de 2x.")

    recencia = sum(recency_points(p) for p in df_total["Published"].head(10))
    if recencia >= 120:
        score += 20
        motivos.append("Varios outliers son recientes.")
    elif recencia >= 60:
        score += 12
        motivos.append("Hay algunas senales recientes.")

    views_medias = df_total["Views"].head(10).mean()
    if views_medias >= 500000:
        score += 15
        motivos.append("Hay volumen alto de visitas en los videos ganadores.")
    elif views_medias >= 100000:
        score += 10
        motivos.append("Hay volumen real de visitas.")
    elif views_medias >= 30000:
        score += 6
        motivos.append("Hay volumen inicial suficiente para testear.")

    return min(score, 100), motivos


def generar_ideas_ataque(df_total, guiones_data, nichos_similares, limite=20):
    textos_titulos = df_total["Title"].tolist()
    texto = " ".join(textos_titulos).lower()

    if guiones_data:
        texto += " " + " ".join([g["Texto"][:1800] for g in guiones_data])

    if nichos_similares:
        texto += " " + " ".join(nichos_similares[:20])

    stopwords = {
        "the", "and", "for", "with", "from", "this", "that", "your", "you",
        "how", "why", "what", "when", "video", "videos", "explained", "review",
        "analysis", "official", "trailer", "movie", "full", "new", "best",
        "minecraft"
    }

    tokens = re.findall(r'\w{4,}', texto)
    temas = [t for t in tokens if t not in stopwords]
    top_temas = [t for t, _ in Counter(temas).most_common(25)]

    bigramas = []
    for titulo in textos_titulos:
        clean = re.sub(r'[^\w\s]', ' ', titulo.lower())
        words = [w for w in clean.split() if w not in stopwords and len(w) > 3]
        for i in range(len(words) - 1):
            bigramas.append(f"{words[i]} {words[i + 1]}")

    top_bigramas = [b for b, _ in Counter(bigramas).most_common(15)]

    patrones_detectados = []
    for titulo in textos_titulos:
        t = titulo.lower()

        if "100 days" in t:
            patrones_detectados.append("100_days")
        if "i built" in t or "built" in t:
            patrones_detectados.append("build")
        if "survive" in t or "survived" in t:
            patrones_detectados.append("survival")
        if "secret" in t or "hidden" in t:
            patrones_detectados.append("secret")
        if "why" in t:
            patrones_detectados.append("why")
        if " vs " in t:
            patrones_detectados.append("versus")
        if "every" in t:
            patrones_detectados.append("escalation")
        if "no one" in t or "nobody" in t:
            patrones_detectados.append("ignored")
        if "story" in t:
            patrones_detectados.append("story")

    formatos_prioritarios = [p for p, _ in Counter(patrones_detectados).most_common()]

    bancos_formatos = {
        "100_days": ["I Spent 100 Days Inside {}", "100 Days Trying to Master {}", "I Survived 100 Days With Only {}"],
        "build": ["I Built a {} That Should Not Exist", "I Built the Most Dangerous {}", "I Built {} and Instantly Regretted It"],
        "survival": ["I Tried to Survive {}", "Surviving the Hardest {} Challenge", "{} Survival Gets Worse Every Minute"],
        "secret": ["The Hidden Truth About {}", "The Secret Side of {}", "What Nobody Tells You About {}"],
        "why": ["Why {} Is Taking Over YouTube", "Why Everyone Suddenly Cares About {}", "Why {} Works So Well"],
        "versus": ["{} vs The Most Impossible Challenge", "I Compared {} With Its Biggest Rival", "{} vs Everything That Tries to Stop It"],
        "escalation": ["{} Gets Harder Every Minute", "{} But Every Step Makes It Worse", "I Tried {}, But It Kept Escalating"],
        "ignored": ["Nobody Talks About {}", "The {} Everyone Ignored", "No One Expected {} To Work"],
        "story": ["The Complete Story of {}", "The Rise and Fall of {}", "The Strange History of {}"],
        "default": [
            "I Tested {} So You Don't Have To",
            "The Truth About {}",
            "{} Explained Through Real Examples",
            "I Tried the Most Viral {}",
            "The Most Underrated {} Right Now",
            "I Found a Weird Pattern in {}",
            "This {} Strategy Should Not Work",
            "I Copied the Best {} Ideas and Made My Own"
        ]
    }

    temas_finales = []
    temas_finales.extend(top_bigramas)
    temas_finales.extend(top_temas)

    temas_limpios = []
    vistos = set()

    for t in temas_finales:
        t = re.sub(r'\s+', ' ', t).strip()
        if t and t not in vistos:
            vistos.add(t)
            temas_limpios.append(t)

    ideas = []
    usados = set()
    formatos = []

    for f in formatos_prioritarios:
        formatos.extend(bancos_formatos.get(f, []))

    formatos.extend(bancos_formatos["default"])

    for tema in temas_limpios:
        tema_titulo = tema.title()
        for formato in formatos:
            idea = formato.format(tema_titulo)
            if idea not in usados:
                usados.add(idea)
                ideas.append(idea)

            if len(ideas) >= limite:
                return ideas

    return ideas[:limite]


def crear_tabla_canales_validados(df_total):
    filas = []

    for canal, grupo in df_total.groupby("Channel"):
        grupo = grupo.sort_values("Multiplicador", ascending=False)
        subs = grupo["Subscribers"].iloc[0] if "Subscribers" in grupo else "Unknown"
        subs_num = parse_number_label(subs)
        max_multi = float(grupo["Multiplicador"].max())
        total_views = int(grupo["Views"].sum())
        videos = int(len(grupo))

        if 0 < subs_num <= 100000 and max_multi >= 3:
            prioridad = "Alta"
        elif max_multi >= 2 or total_views >= 100000:
            prioridad = "Media"
        else:
            prioridad = "Baja"

        filas.append({
            "Canal": canal,
            "Subs": subs,
            "Videos outlier": videos,
            "Mejor multiplicador": round(max_multi, 1),
            "Vistas outlier": total_views,
            "Prioridad": prioridad,
            "Mejor video": grupo["Title"].iloc[0],
            "Link": grupo["URL"].iloc[0]
        })

    if not filas:
        return pd.DataFrame()

    tabla = pd.DataFrame(filas)
    prioridad_order = {"Alta": 0, "Media": 1, "Baja": 2}
    tabla["_orden"] = tabla["Prioridad"].map(prioridad_order).fillna(9)
    tabla = tabla.sort_values(["_orden", "Mejor multiplicador", "Vistas outlier"], ascending=[True, False, False])
    return tabla.drop(columns=["_orden"])


def run_mining(input_raw, outlier_factor, max_ciclos, max_guiones, max_keywords_expansion, profundidad_keywords, max_recommended_sources=4):
    st.session_state.outliers_data = []
    st.session_state.guiones_data = []
    st.session_state.nichos_similares = []
    st.session_state.keywords_generadas = []
    st.session_state.recommended_data = []

    historico_outliers = []
    keywords_procesadas = set()
    guiones_acumulados = []
    recomendados_acumulados = []

    miner = YouTubeHyperMiner(memoria)
    semillas_iniciales = [k.strip().lower() for k in input_raw.split(",") if k.strip()]
    st.session_state.semillas_iniciales = semillas_iniciales

    status_box = st.empty()
    progress_bar = st.progress(0)
    status_box.info(" Expandiendo keywords en red con autocomplete de YouTube...")

    cola_keywords = generar_keywords_red_autocomplete(
        miner,
        semillas_iniciales,
        max_keywords=max_keywords_expansion,
        profundidad=profundidad_keywords
    )

    st.session_state.keywords_generadas = cola_keywords

    ciclo_actual = 0
    total_ramas_encontradas = min(len(cola_keywords), max_ciclos)

    while cola_keywords and ciclo_actual < max_ciclos:
        kw_actual = cola_keywords.pop(0)

        if kw_actual in keywords_procesadas:
            continue

        keywords_procesadas.add(kw_actual)
        ciclo_actual += 1
        progress_bar.progress(ciclo_actual / max_ciclos)

        status_box.info(f" Excavando en rama [{ciclo_actual}/{total_ramas_encontradas}]: **{kw_actual}**")
        videos = miner.scrape_keyword_videos(kw_actual)

        if videos:
            df = pd.DataFrame(videos)
            median_views = df["Views"].median()

            if median_views > 0:
                df["Multiplicador"] = df["Views"] / median_views
                outliers = df[df["Views"] >= (median_views * outlier_factor)].copy()

                if not outliers.empty:
                    outliers["Keyword_Origen"] = kw_actual

                    for _, row in outliers.iterrows():
                        row_dict = row.to_dict()
                        row_dict["Subscribers"] = miner.get_video_subscribers(row_dict.get("ID", ""))
                        historico_outliers.append(row_dict)

        time.sleep(0.3)

    if historico_outliers:
        df_ordenado = (
            pd.DataFrame(historico_outliers)
            .drop_duplicates(subset=["URL"])
            .sort_values(by="Multiplicador", ascending=False)
        )

        top_videos = df_ordenado.head(max_guiones).to_dict(orient="records")

        top_recommended_sources = (
            df_ordenado[df_ordenado["Multiplicador"] >= 3.0]
            .head(max_recommended_sources)
            .to_dict(orient="records")
        )

        if top_recommended_sources:
            for index, row in enumerate(top_recommended_sources):
                status_box.text(f" [{index + 1}/{len(top_recommended_sources)}] Leyendo feed recomendado de: {row['Title'][:60]}...")
                recomendados = miner.scrape_recommended_videos(
                    row.get("ID", ""),
                    source_title=row.get("Title", ""),
                    limit=12
                )

                for rec in recomendados:
                    rec["Keyword_Origen"] = f"feed de {row.get('Keyword_Origen', '')}"
                    rec["Source_Multiplicador"] = row.get("Multiplicador", 0)
                    recomendados_acumulados.append(rec)

        for index, row in enumerate(top_videos):
            status_box.text(f" [{index + 1}/{len(top_videos)}] Extrayendo guion de: {row['Title'][:60]}...")
            texto_guion = miner.extract_script_keywords(row["ID"])

            if texto_guion:
                guiones_acumulados.append({
                    "Title": row["Title"],
                    "Channel": row["Channel"],
                    "URL": row["URL"],
                    "ID": row["ID"],
                    "Keyword_Origen": row["Keyword_Origen"],
                    "Texto": texto_guion,
                    "Conceptos": extraer_conceptos_de_texto(texto_guion, limite=12)
                })

    nichos_similares = generar_nichos_similares(semillas_iniciales, historico_outliers, guiones_acumulados, limite=40)

    status_box.success(" Mapeo completado con exito!")
    st.session_state.outliers_data = historico_outliers
    st.session_state.guiones_data = guiones_acumulados
    st.session_state.nichos_similares = nichos_similares
    if recomendados_acumulados:
        st.session_state.recommended_data = (
            pd.DataFrame(recomendados_acumulados)
            .drop_duplicates(subset=["URL"])
            .to_dict(orient="records")
        )
    else:
        st.session_state.recommended_data = []


# =========================================================
# APP
# ESTO ES LA INTERFAZ: SIDEBAR, BUSCADOR, BOTONES,
# PESTANAS, TABLAS, TARJETAS DE VIDEOS Y RESULTADOS.
# =========================================================

inject_css()

st.sidebar.markdown("## Minero Pro")
st.sidebar.caption("Filtros del radar")

st.sidebar.markdown("### OUTLIERS")
outlier_factor = st.sidebar.slider("Outlier score minimo", 1.1, 5.0, 1.2, step=0.1)
periodo_publicacion = st.sidebar.selectbox("Publication date", ["Ultimos 3 meses", "Ultimos 6 meses", "Cualquier reciente"])
rango_subs = st.sidebar.selectbox("Subscribers", ["Canales pequenos 0 - 100K", "0 - 500K", "Cualquiera"])

st.sidebar.markdown("### RAMAS / RED")
max_ciclos = st.sidebar.slider("Ramas totales", 2, MAX_SAFE_BRANCHES, 40)
max_keywords_expansion = st.sidebar.slider("Keywords reales maximas", 50, MAX_SAFE_KEYWORDS, 120)
profundidad_keywords = st.sidebar.slider("Profundidad de red", 1, MAX_SAFE_DEPTH, 2)
max_guiones = st.sidebar.slider("Videos buenos para guion", 1, 10, 5)
max_recommended_sources = st.sidebar.slider("Videos fuente para feed recomendado", 0, 8, 4)



st.markdown("""
<div class="top-banner">
    Minero Multinicho Pro: encuentra outliers, valida nichos y guarda patrones en memoria IA
</div>
""", unsafe_allow_html=True)

nicho_default = st.session_state.get("nicho_buscado", "how to train your dragon")

input_raw = st.text_input(
    "Buscador de nicho",
    value=nicho_default,
    placeholder="Escribe el nicho: how to train your dragon, roblox horror, shorts ai..."
)

st.caption("Privacidad: esta app no pide datos personales. Las busquedas se procesan desde el servidor de la app. Si activas la memoria IA, solo se guardan patrones del analisis, no datos personales.")
st.caption("Memoria IA: cada busqueda completada guarda automaticamente patrones anonimos para mejorar futuras lecturas del nicho.")

st.markdown(f"""
<div class="chips">
    <span class="chip">Outlier Score {outlier_factor:.1f}x+ x</span>
    <span class="chip">Ramas red {max_ciclos} x</span>
    <span class="chip">Keywords {max_keywords_expansion} x</span>
    <span class="chip">Profundidad {profundidad_keywords} x</span>
    <span class="chip">Guiones {max_guiones} x</span>
</div>
""", unsafe_allow_html=True)

col_run, col_random, col_saved = st.columns([1.2, 1, 4])

with col_run:
    buscar = st.button("Buscar nicho", use_container_width=True)

with col_random:
    random_click = st.button("Random", use_container_width=True)

with col_saved:
    st.caption(f"Filtros activos: {periodo_publicacion} - {rango_subs}")

if random_click:
    input_raw = "viral ai shorts, roblox horror, minecraft survival, dragon theory"
    st.session_state.nicho_buscado = input_raw

def check_rate_limit(seconds=RATE_LIMIT_SECONDS):
    now = time.time()
    last_run = st.session_state.get("last_run_ts", 0)

    if now - last_run < seconds:
        wait = int(seconds - (now - last_run))
        st.warning(f"Espera {wait}s antes de lanzar otra busqueda.")
        return False

    st.session_state.last_run_ts = now
    return True

if buscar or random_click:
    if check_rate_limit():
        st.session_state.nicho_buscado = input_raw
        run_mining(
            input_raw,
            outlier_factor,
            max_ciclos,
            max_guiones,
            max_keywords_expansion,
            profundidad_keywords,
            max_recommended_sources
        )

if "keywords_generadas" in st.session_state and st.session_state.keywords_generadas:
    with st.expander(" Keywords generadas por la expansion en red", expanded=False):
        st.write(f"Total generadas: {len(st.session_state.keywords_generadas)}")
        st.text_area("Keywords encontradas:", value=", ".join(st.session_state.keywords_generadas), height=180)

# Helper to extract graph in-memory for current search
def extract_current_search_graph(seeds, df_total, ideas, final_score):
    if df_total is None or df_total.empty:
        return pd.DataFrame(), pd.DataFrame()
    
    graph_patterns = Counter()
    pattern_types = {}

    for _, row in df_total.iterrows():
        weight = memoria.video_weight(row)
        for pattern_type, pattern_value in memoria.extract_video_patterns(row):
            if pattern_type == "recency":
                continue
            val = clean_and_singularize_label(pattern_value)
            if val:
                graph_patterns[val] += weight
                pattern_types[val] = pattern_type

    for idea in ideas or []:
        for pattern_type, pattern_value in memoria.extract_title_patterns(str(idea)):
            val = clean_and_singularize_label(pattern_value)
            if val:
                graph_patterns[val] += final_score / 100
                pattern_types[val] = "idea_" + pattern_type

    edges_payload = []
    top_patterns = graph_patterns.most_common(35)
    
    for i in range(len(top_patterns)):
        source_val, source_weight = top_patterns[i]

        for j in range(i + 1, min(i + 12, len(top_patterns))):
            target_val, target_weight = top_patterns[j]

            if source_val == target_val:
                continue

            edge_weight = float(min(source_weight, target_weight))
            from_node, to_node = sorted([source_val, target_val])
            edges_payload.append({
                "from": from_node,
                "to": to_node,
                "uses": 1,
                "total_weight": edge_weight,
                "avg_weight": edge_weight
            })

    nodes_payload = []
    for node_val, weight in graph_patterns.items():
        nodes_payload.append({
            "id": node_val,
            "label": node_val,
            "type": pattern_types.get(node_val, "unknown"),
            "weight": weight
        })

    return pd.DataFrame(nodes_payload), pd.DataFrame(edges_payload)


# Define tabs immediately
tab_outliers, tab_nicho, tab_ideas, tab_canales, tab_mapa = st.tabs([
    "Videos outliers",
    "Nicho",
    "Ideas validadas",
    "Canales validados",
    "Mapa neural"
])

# Check if search is active
search_active = False
if st.session_state.get("outliers_data"):
    df_total = pd.DataFrame(st.session_state.outliers_data)
    df_total = df_total.drop_duplicates(subset=["URL"]).sort_values(by="Multiplicador", ascending=False)

    if "Subscribers" not in df_total.columns:
        df_total["Subscribers"] = "Unknown"
    df_total["Subscribers"] = df_total["Subscribers"].fillna("Unknown")

    df_total_sin_filtros = df_total.copy()
    df_total = filtrar_outliers_por_sidebar(df_total, periodo_publicacion, rango_subs)

    if not df_total.empty:
        search_active = True
        
        # Calculate search metrics
        score_auto, motivos_auto = calcular_score_oportunidad(df_total)
        tabla_referencias = crear_tabla_referencias(df_total)
        tabla_validacion = crear_tabla_validacion(df_total)
        tabla_canales = crear_tabla_canales_validados(df_total)

        ideas = generar_ideas_ataque(
            df_total,
            st.session_state.get("guiones_data", []),
            st.session_state.get("nichos_similares", []),
            limite=20
        )

        color_patron = analizar_patron_ganador(df_total.head(12)["Thumbnail"].tolist())
        semillas_para_autoguardado = st.session_state.get(
            "semillas_iniciales",
            [k.strip().lower() for k in input_raw.split(",") if k.strip()]
        )
        score_memoria_auto, lectura_memoria_auto, _ = resumen_validacion_final(
            tabla_referencias,
            tabla_validacion
        )
        autosaved_id = autosave_analysis_once(
            semillas_para_autoguardado,
            df_total,
            score_memoria_auto,
            score_auto,
            lectura_memoria_auto,
            color_patron,
            ideas
        )

        if autosaved_id == "background":
            st.toast("💾 Guardando análisis en la base de datos en segundo plano...", icon="☁️")
        elif autosaved_id:
            st.success(f"Búsqueda guardada en memoria con ID {autosaved_id}.")

# Render tabs
with tab_outliers:
    if search_active:
        st.markdown('<div class="page-title">All outliers</div>', unsafe_allow_html=True)
        render_outlier_cards(df_total)

        with st.expander("Ver tabla completa de outliers"):
            st.dataframe(
                df_total[
                    [
                        "Keyword_Origen",
                        "Title",
                        "Channel",
                        "Subscribers",
                        "Views",
                        "Published",
                        "Multiplicador",
                        "URL"
                    ]
                ],
                use_container_width=True
            )
    else:
        st.info("Escribe un nicho en el buscador de arriba y pulsa Buscar nicho para activar esta pestaña.")

with tab_nicho:
    if search_active:
        st.markdown("## Validacion del nicho")
        st.warning("Si ves Subs canal como Hidden/Unknown, puedes escribirlos a mano. Ejemplo: 25K, 80K, 1.2M.")

        st.markdown("### Tabla de ideas de los canales referencia")

        tabla_referencias_editada = st.data_editor(
            tabla_referencias,
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "Destaca?": st.column_config.SelectboxColumn("Destaca?", options=["Si", "No"], required=True),
                "Link": st.column_config.LinkColumn("Link"),
                "Subs canal": st.column_config.TextColumn("Subs canal"),
                "Por que llama la atencion?": st.column_config.TextColumn("Por que llama la atencion?")
            },
            key="tabla_referencias_editor"
        )

        st.markdown("### Tabla de validacion de ideas")

        tabla_validacion_editada = st.data_editor(
            tabla_validacion,
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "Hay 3 videos ref.?": st.column_config.SelectboxColumn("Hay 3 videos ref.?", options=["Si", "No"], required=True),
                "Prioridad": st.column_config.SelectboxColumn("Prioridad", options=["Alta", "Media", "Baja"], required=True),
                "Alguno es de un canal pequeno?": st.column_config.CheckboxColumn("Alguno es de un canal pequeno?")
            },
            key="tabla_validacion_editor"
        )

        score_final, lectura_final, motivos_finales = resumen_validacion_final(
            tabla_referencias_editada,
            tabla_validacion_editada
        )

        col_score1, col_score2, col_score3 = st.columns(3)
        col_score1.metric("Nota final del nicho", f"{score_final}/100")
        col_score2.metric("Resultado", lectura_final)
        col_score3.metric("Score automatico bruto", f"{score_auto}/100")

        st.markdown("**Motivos de la nota final:**")
        for m in motivos_finales:
            st.write(f"- {m}")

        if motivos_auto:
            st.markdown("**Senales automaticas extra:**")
            for m in motivos_auto:
                st.write(f"- {m}")

        st.markdown("###  Nichos similares para seguir excavando")

        if "nichos_similares" in st.session_state and st.session_state.nichos_similares:
            st.text_area(
                "Copia estos nichos/ramas para una nueva busqueda:",
                value=", ".join(st.session_state.nichos_similares),
                height=130
            )

            cols_nichos = st.columns(3)
            for i, nicho in enumerate(st.session_state.nichos_similares[:15]):
                with cols_nichos[i % 3]:
                    st.code(nicho)
        else:
            st.info("Aun no hay suficientes senales para sugerir nichos similares.")

        st.markdown("### Direccion de miniatura")
        st.write(f"Color dominante detectado: RGB {color_patron}")

        for instruccion in generar_direccion_miniatura(df_total, color_patron):
            st.write(f"- {instruccion}")

        st.markdown("### Generador de base de miniatura basado en datos")

        if st.button("Analizar miniaturas ganadoras y generar base"):
            patron = analizar_patron_ganador(df_total.head(12)["Thumbnail"].tolist())
            st.write(f"Patron de color detectado: {patron}")
            base_img = crear_base_miniatura(patron)
            st.image(base_img, caption="Miniatura base generada con el color dominante del nicho")
    else:
        st.info("Escribe un nicho en el buscador de arriba y pulsa Buscar nicho para activar esta pestaña.")

with tab_ideas:
    if search_active:
        st.markdown("## Ideas validadas")

        st.text_area("Ideas listas para adaptar:", value="\n".join(ideas), height=260)

        st.markdown("## Cajas de extraccion rapida")

        c1, c2 = st.columns(2)
        semilla_referencia = df_total["Keyword_Origen"].iloc[0] if not df_total.empty else "video"

        with c1:
            st.markdown("### Basado en titulos ganadores")

            titulos_buenos = df_total["Title"].tolist()
            stopwords_titulos = {
                'the', 'is', 'and', 'to', 'in', 'of', 'a', 'for', 'with', 'on',
                'how', 'video', 'but', 'why', 'what', 'when', 'from', 'this',
                'that', 'you', 'your', 'about', 'into', 'after'
            }

            bigramas = []
            for t in titulos_buenos:
                clean_t = re.sub(r'[^\w\s]', '', t.lower())
                palabras = [p for p in clean_t.split() if p not in stopwords_titulos and len(p) > 2]

                for i in range(len(palabras) - 1):
                    bigramas.append(f"{palabras[i]} {palabras[i + 1]}")

            top_bi = Counter(bigramas).most_common(8)
            sug_titulos_caja = [f"{semilla_referencia} {token}" for token, _ in top_bi]

            st.text_area(
                "Patrones de titulos",
                value=", ".join(list(dict.fromkeys(sug_titulos_caja))),
                height=100,
                label_visibility="collapsed"
            )

        with c2:
            st.markdown("### Basado en lo hablado en guiones")

            if "guiones_data" in st.session_state and st.session_state.guiones_data:
                texto_global = " ".join([g["Texto"] for g in st.session_state.guiones_data])
                conceptos_guion = extraer_conceptos_de_texto(texto_global, limite=10)
                sug_guion_caja = [f"{semilla_referencia} {palabra}" for palabra, _ in conceptos_guion]

                st.text_area(
                    "Palabras clave del guion",
                    value=", ".join(list(dict.fromkeys(sug_guion_caja))),
                    height=100,
                    label_visibility="collapsed"
                )
            else:
                st.info("No hay suficientes transcripciones procesadas aun en esta tanda.")

        st.markdown("## Radar de transcripciones")

        if "guiones_data" in st.session_state and st.session_state.guiones_data:
            for i, guion in enumerate(st.session_state.guiones_data, start=1):
                with st.expander(f"{i}. {guion['Title']}"):
                    st.markdown(f"**Canal:** {guion['Channel']}")
                    st.markdown(f"**Keyword origen:** {guion['Keyword_Origen']}")
                    st.markdown(f"[Abrir video]({guion['URL']})")

                    conceptos = guion["Conceptos"]

                    if conceptos:
                        st.markdown("**Conceptos fuertes detectados:**")
                        st.write(", ".join([f"{palabra} ({count})" for palabra, count in conceptos]))

                        keyword_base_guion = guion["Keyword_Origen"]
                        nuevas_busquedas = [f"{keyword_base_guion} {palabra}" for palabra, _ in conceptos[:8]]

                        st.text_area(
                            "Nuevas busquedas sugeridas desde esta transcripcion:",
                            value=", ".join(nuevas_busquedas),
                            height=80,
                            key=f"ideas_guion_{i}"
                        )

                    st.text_area(
                        "Fragmento de transcripcion:",
                        value=guion["Texto"][:2500],
                        height=180,
                        key=f"preview_guion_{i}"
                    )
        else:
            st.info("No hay transcripciones disponibles en esta tanda.")
    else:
        st.info("Escribe un nicho en el buscador de arriba y pulsa Buscar nicho para activar esta pestaña.")

with tab_canales:
    if search_active:
        st.markdown("## Canales validados")

        if tabla_canales.empty:
            st.info("Aun no hay canales validados.")
        else:
            st.dataframe(
                tabla_canales,
                use_container_width=True,
                column_config={"Link": st.column_config.LinkColumn("Link")}
            )
    else:
        st.info("Escribe un nicho en el buscador de arriba y pulsa Buscar nicho para activar esta pestaña.")

with tab_mapa:
    st.markdown("## Mapa neural de memoria IA")
    st.caption(
        "Este mapa conecta patrones que aparecen juntos en las busquedas: tokens de titulo, formatos, keywords, ideas y recencia. "
        "Cuanto mas grande el nodo, mas fuerte aparece en la memoria."
    )

    c_graph1, c_graph2, c_graph3 = st.columns([1, 1, 2])

    with c_graph1:
        graph_limit = st.slider("Conexiones maximas", 50, 500, 220, step=25)

    with c_graph2:
        min_weight = st.slider("Peso minimo", 0.01, 1.0, 0.08, step=0.01)

    # Load database graph
    nodes_db, edges_db = memoria.graph_data(
        limit_edges=graph_limit,
        min_edge_weight=min_weight
    )
    
    # Merge with current search in-memory if active
    if search_active:
        nodes_curr, edges_curr = extract_current_search_graph(
            semillas_para_autoguardado,
            df_total,
            ideas,
            score_memoria_auto
        )
        
        if not nodes_curr.empty and not edges_curr.empty:
            if nodes_db.empty:
                nodes_graph = nodes_curr
            else:
                combined_nodes = pd.concat([nodes_db, nodes_curr])
                nodes_graph = combined_nodes.groupby(["id", "label", "type"], as_index=False).sum()
                
            if edges_db.empty:
                edges_graph = edges_curr
            else:
                combined_edges = pd.concat([edges_db, edges_curr])
                grouped = combined_edges.groupby(["from", "to"]).agg({
                    "uses": "sum",
                    "total_weight": "sum",
                    "avg_weight": "mean"
                }).reset_index()
                edges_graph = grouped
        else:
            nodes_graph, edges_graph = nodes_db, edges_db
    else:
        nodes_graph, edges_graph = nodes_db, edges_db

    # ONLY KEEP NODES THAT HAVE AT LEAST ONE ACTIVE CONNECTION (Filters out disconnected outer ring nodes)
    if not nodes_graph.empty and not edges_graph.empty:
        connected_nodes = set(edges_graph["from"].tolist() + edges_graph["to"].tolist())
        nodes_graph = nodes_graph[nodes_graph["id"].isin(connected_nodes)]

    # Prepare videos list from current search to pass to interactive graph
    videos_payload = []
    if search_active and not df_total.empty:
        for _, row in df_total.iterrows():
            videos_payload.append({
                "Title": str(row.get("Title", "")),
                "Channel": str(row.get("Channel", "")),
                "Subscribers": str(row.get("Subscribers", "")),
                "Views": int(row.get("Views", 0)),
                "Published": str(row.get("Published", "")),
                "URL": str(row.get("URL", "")),
                "Thumbnail": str(row.get("Thumbnail", "")),
                "Multiplicador": float(row.get("Multiplicador", 0.0)),
                "Keyword_Origen": str(row.get("Keyword_Origen", ""))
            })

    with c_graph3:
        st.metric("Nodos", 0 if nodes_graph.empty else len(nodes_graph))
        st.metric("Conexiones", 0 if edges_graph.empty else len(edges_graph))

    render_neural_graph(nodes_graph, edges_graph, videos_payload, height=720)

    with st.expander("Ver datos del mapa"):
        st.markdown("### Nodos mas fuertes")
        if nodes_graph.empty:
            st.info("Todavia no hay nodos.")
        else:
            st.dataframe(
                nodes_graph.sort_values("weight", ascending=False).head(80),
                use_container_width=True
            )

        st.markdown("### Conexiones mas fuertes")
        if edges_graph.empty:
            st.info("Todavia no hay conexiones.")
        else:
            st.dataframe(edges_graph.head(120), use_container_width=True)

