import streamlit as st
import requests
import json
import re
import sqlite3
import time
import html
import hashlib
import pandas as pd
import numpy as np
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


require_access_code()


st.set_page_config(
    page_title="Minero Multinicho Pro v4.0",
    page_icon="brain",
    layout="wide"
)


# =========================================================
# MEMORIA IA
# ESTO SIRVE PARA GUARDAR PATRONES DE NICHOS BUENOS EN SQLITE.
# LA APP USA ESTO PARA COMPARAR NUEVAS BUSQUEDAS CON LO APRENDIDO.
# =========================================================

class PatternMemory:
    def __init__(self, db_path="data/pattern_memory.sqlite"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.setup()

    def setup(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                seeds TEXT,
                final_score REAL,
                auto_score REAL,
                reading TEXT,
                color_rgb TEXT,
                total_videos INTEGER,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS pattern_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER,
                pattern_type TEXT,
                pattern_value TEXT,
                weight REAL,
                created_at TEXT
            );
        """)
        self.conn.commit()

    def save_analysis(self, seeds, df_total, final_score, auto_score, reading, color_rgb, ideas=None, notes=""):
        if df_total is None or df_total.empty:
            return None

        now = datetime.utcnow().isoformat(timespec="seconds")
        seeds_text = ", ".join(seeds) if isinstance(seeds, list) else str(seeds)

        cur = self.conn.execute("""
            INSERT INTO analyses
            (created_at, seeds, final_score, auto_score, reading, color_rgb, total_videos, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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

        analysis_id = cur.lastrowid

        for _, row in df_total.iterrows():
            weight = self.video_weight(row)
            for pattern_type, pattern_value in self.extract_video_patterns(row):
                self.conn.execute("""
                    INSERT INTO pattern_events
                    (analysis_id, pattern_type, pattern_value, weight, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (analysis_id, pattern_type, pattern_value, weight, now))

        for idea in ideas or []:
            for pattern_type, pattern_value in self.extract_title_patterns(str(idea)):
                self.conn.execute("""
                    INSERT INTO pattern_events
                    (analysis_id, pattern_type, pattern_value, weight, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (analysis_id, "idea_" + pattern_type, pattern_value, final_score / 100, now))

        self.conn.commit()
        return analysis_id

    def predict_opportunity(self, seeds, df_total, color_rgb=None, ideas=None):
        history_count = self.conn.execute("SELECT COUNT(*) AS total FROM analyses").fetchone()["total"]

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
        return pd.read_sql_query("""
            SELECT pattern_type, pattern_value, COUNT(*) AS uses, AVG(weight) AS avg_weight
            FROM pattern_events
            GROUP BY pattern_type, pattern_value
            HAVING uses >= 2
            ORDER BY avg_weight DESC, uses DESC
            LIMIT ?
        """, self.conn, params=(limit,))

    def recent_analyses(self, limit=10):
        return pd.read_sql_query("""
            SELECT created_at, seeds, final_score, auto_score, reading, total_videos
            FROM analyses
            ORDER BY id DESC
            LIMIT ?
        """, self.conn, params=(limit,))

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
        rows = self.conn.execute("""
            SELECT pattern_type, pattern_value, COUNT(*) AS uses, AVG(weight) AS avg_weight
            FROM pattern_events
            GROUP BY pattern_type, pattern_value
        """).fetchall()

        stats = {}
        for row in rows:
            stats[(row["pattern_type"], row["pattern_value"])] = {
                "uses": int(row["uses"]),
                "avg_weight": float(row["avg_weight"])
            }

        return stats

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
            "esta", "este", "pero", "porque", "todo", "algo", "nada"
        }

        clean = self.clean(text)
        return [w for w in clean.split() if w not in stopwords and len(w) > 3]

    def clean(self, text):
        text = str(text).lower()
        text = re.sub(r"[^\w\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()


def format_pattern(pattern_item):
    pattern_type, pattern_value = pattern_item["pattern"]
    return f"{pattern_type}: {pattern_value}"


memoria = PatternMemory("data/pattern_memory.sqlite")


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
    # NO GUARDA IP, EMAIL, NOMBRE NI DATOS PERSONALES.
    if not AUTO_SAVE_SEARCHES_TO_MEMORY:
        return None

    if df_total is None or df_total.empty:
        return None

    signature = build_analysis_signature(seeds, df_total)
    saved = st.session_state.get("saved_analysis_signatures", set())

    if signature in saved:
        return None

    analysis_id = memoria.save_analysis(
        seeds,
        df_total,
        final_score,
        auto_score,
        reading,
        color_rgb,
        ideas,
        notes="Guardado automatico sin datos personales"
    )

    saved.add(signature)
    st.session_state.saved_analysis_signatures = saved
    st.session_state.last_saved_analysis_id = analysis_id
    return analysis_id


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
    def __init__(self):
        self.subs_cache = {}
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }

    def get_youtube_suggestions(self, keyword):
        url = f"https://suggestqueries.google.com/complete/search?client=youtube&hl=en&ds=yt&q={quote(keyword)}"
        try:
            response = requests.get(url, headers=self.headers, timeout=7)
            if response.status_code == 200:
                clean_text = response.text
                if "(" in clean_text:
                    clean_text = clean_text[clean_text.find("(")+1:clean_text.rfind(")")]
                data = json.loads(clean_text)
                return [item[0] for item in data[1] if item[0].lower() != keyword.lower()]
        except Exception:
            pass
        return []

    def scrape_keyword_videos(self, keyword):
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

        return videos

    def extract_script_keywords(self, video_id):
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
            return full_text

        except Exception:
            return ""

    def get_video_subscribers(self, video_id):
        if not video_id:
            return "Unknown"

        if video_id in self.subs_cache:
            return self.subs_cache[video_id]

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
                    return subs

            self.subs_cache[video_id] = "Hidden/Unknown"
            return "Hidden/Unknown"

        except Exception:
            self.subs_cache[video_id] = "Unknown"
            return "Unknown"

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


def run_mining(input_raw, outlier_factor, max_ciclos, max_guiones, max_keywords_expansion, profundidad_keywords):
    st.session_state.outliers_data = []
    st.session_state.guiones_data = []
    st.session_state.nichos_similares = []
    st.session_state.keywords_generadas = []

    historico_outliers = []
    keywords_procesadas = set()
    guiones_acumulados = []

    miner = YouTubeHyperMiner()
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

st.sidebar.markdown("### MEMORIA")
guardar_en_memoria = st.sidebar.checkbox("Guardar analisis en memoria IA", value=False, help="Si la app es publica, activa esto solo si quieres guardar este analisis en la memoria compartida de la app.")
ver_memoria = st.sidebar.checkbox("Ver memoria aprendida", value=False)
st.sidebar.caption("Auto-memoria activa: cada busqueda guarda patrones anonimos una vez.")

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
            profundidad_keywords
        )

if "keywords_generadas" in st.session_state and st.session_state.keywords_generadas:
    with st.expander(" Keywords generadas por la expansion en red", expanded=False):
        st.write(f"Total generadas: {len(st.session_state.keywords_generadas)}")
        st.text_area("Keywords encontradas:", value=", ".join(st.session_state.keywords_generadas), height=180)

if not st.session_state.get("outliers_data"):
    st.info("Escribe un nicho en el buscador de arriba y pulsa Buscar nicho. Los videos que salgan aqui seran outliers.")
    st.stop()

df_total = pd.DataFrame(st.session_state.outliers_data)
df_total = df_total.drop_duplicates(subset=["URL"]).sort_values(by="Multiplicador", ascending=False)

if "Subscribers" not in df_total.columns:
    df_total["Subscribers"] = "Unknown"

df_total["Subscribers"] = df_total["Subscribers"].fillna("Unknown")

df_total_sin_filtros = df_total.copy()
df_total = filtrar_outliers_por_sidebar(df_total, periodo_publicacion, rango_subs)

if df_total.empty:
    st.warning(
        f"Hay {len(df_total_sin_filtros)} outliers encontrados, pero ninguno pasa los filtros actuales: "
        f"{periodo_publicacion} - {rango_subs}. Abre el filtro de meses/subs para verlos."
    )
    st.stop()

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

if autosaved_id:
    st.success(f"Busqueda guardada automaticamente en memoria IA con ID {autosaved_id}.")

tab_outliers, tab_nicho, tab_ideas, tab_canales, tab_memoria = st.tabs([
    "Videos outliers",
    "Nicho",
    "Ideas validadas",
    "Canales validados",
    "Memoria IA"
])

with tab_outliers:
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

with tab_nicho:
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

    st.markdown("### ")
    st.write(f"Color dominante detectado: RGB {color_patron}")

    for instruccion in generar_direccion_miniatura(df_total, color_patron):
        st.write(f"- {instruccion}")

    st.markdown("### ")

    if st.button("Analizar miniaturas ganadoras y generar base"):
        patron = analizar_patron_ganador(df_total.head(12)["Thumbnail"].tolist())
        st.write(f"Patron de color detectado: {patron}")
        base_img = crear_base_miniatura(patron)
        st.image(base_img, caption="Miniatura base generada con el color dominante del nicho")

with tab_ideas:
    st.markdown("## Ideas validadas")

    st.text_area("Ideas listas para adaptar:", value="\n".join(ideas), height=260)

    st.markdown("## brain Cajas de extraccion rapida")

    c1, c2 = st.columns(2)
    semilla_referencia = df_total["Keyword_Origen"].iloc[0] if not df_total.empty else "video"

    with c1:
        st.markdown("### ")

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

        st.text_area("", value=", ".join(list(dict.fromkeys(sug_titulos_caja))), height=100)

    with c2:
        st.markdown("### Basado en lo hablado en guiones")

        if "guiones_data" in st.session_state and st.session_state.guiones_data:
            texto_global = " ".join([g["Texto"] for g in st.session_state.guiones_data])
            conceptos_guion = extraer_conceptos_de_texto(texto_global, limite=10)
            sug_guion_caja = [f"{semilla_referencia} {palabra}" for palabra, _ in conceptos_guion]

            st.text_area("", value=", ".join(list(dict.fromkeys(sug_guion_caja))), height=100)
        else:
            st.info("No hay suficientes transcripciones procesadas aun en esta tanda.")

    st.markdown("##  Radar de transcripciones")

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

with tab_canales:
    st.markdown("## Canales validados")

    if tabla_canales.empty:
        st.info("Aun no hay canales validados.")
    else:
        st.dataframe(
            tabla_canales,
            use_container_width=True,
            column_config={"Link": st.column_config.LinkColumn("Link")}
        )

with tab_memoria:
    st.markdown("## Memoria IA")

    semillas_para_memoria = st.session_state.get(
        "semillas_iniciales",
        [k.strip().lower() for k in input_raw.split(",") if k.strip()]
    )

    prediccion_memoria = memoria.predict_opportunity(
        semillas_para_memoria,
        df_total,
        color_patron,
        ideas
    )

    if prediccion_memoria["score"] is None:
        st.info(prediccion_memoria["motives"][0])
    else:
        c_mem1, c_mem2, c_mem3 = st.columns(3)

        c_mem1.metric("Score memoria", f'{prediccion_memoria["score"]}/100')
        c_mem2.metric("Lectura memoria", prediccion_memoria["label"])
        c_mem3.metric("Confianza", prediccion_memoria["confidence"])

        for motivo in prediccion_memoria["motives"]:
            st.write(f"- {motivo}")

        if prediccion_memoria["winning_patterns"]:
            st.markdown("**Patrones historicos que empujan a favor:**")

            for p in prediccion_memoria["winning_patterns"]:
                st.write(f'- {format_pattern(p)} | peso {p["avg_weight"]:.2f} | usos {p["uses"]}')

    if guardar_en_memoria:
        if st.button(" Guardar resultado en memoria IA"):
            score_final_mem, lectura_final_mem, _ = resumen_validacion_final(tabla_referencias, tabla_validacion)

            analysis_id = memoria.save_analysis(
                semillas_para_memoria,
                df_total,
                score_final_mem,
                score_auto,
                lectura_final_mem,
                color_patron,
                ideas,
                notes="Guardado desde Streamlit"
            )

            st.success(f"Analisis guardado en memoria IA con ID {analysis_id}")

    if ver_memoria:
        st.markdown("### Ultimos analisis guardados")
        st.dataframe(memoria.recent_analyses(10), use_container_width=True)

        st.markdown("### Patrones ganadores historicos")
        st.dataframe(memoria.leaderboard(30), use_container_width=True)


