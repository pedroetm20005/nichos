import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd


class PatternMemory:
    def __init__(self, db_path="data/pattern_memory.sqlite"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                seeds TEXT NOT NULL,
                final_score REAL NOT NULL,
                auto_score REAL NOT NULL,
                reading TEXT NOT NULL,
                color_rgb TEXT NOT NULL,
                total_videos INTEGER NOT NULL,
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL,
                keyword TEXT,
                title TEXT,
                channel TEXT,
                url TEXT UNIQUE,
                views INTEGER DEFAULT 0,
                subscribers TEXT DEFAULT 'Unknown',
                published TEXT,
                multiplier REAL DEFAULT 0,
                thumbnail TEXT,
                is_winner INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pattern_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL,
                video_url TEXT,
                pattern_type TEXT NOT NULL,
                pattern_value TEXT NOT NULL,
                weight REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def save_analysis(self, seeds, df_total, final_score, auto_score, reading, color_rgb, ideas=None, notes=""):
        if df_total is None or df_total.empty:
            return None

        now = datetime.utcnow().isoformat(timespec="seconds")
        seeds_text = ", ".join(seeds) if isinstance(seeds, (list, tuple, set)) else str(seeds)

        cur = self.conn.execute(
            """
            INSERT INTO analyses
            (created_at, seeds, final_score, auto_score, reading, color_rgb, total_videos, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                seeds_text,
                float(final_score),
                float(auto_score),
                str(reading),
                json.dumps(tuple(int(x) for x in color_rgb)),
                int(len(df_total)),
                notes,
            ),
        )
        analysis_id = cur.lastrowid

        for _, row in df_total.iterrows():
            url = str(row.get("URL", ""))
            multiplier = float(row.get("Multiplicador", 0) or 0)
            views = int(row.get("Views", 0) or 0)
            is_winner = int(multiplier >= 2 or views >= 100000)

            self.conn.execute(
                """
                INSERT OR IGNORE INTO videos
                (analysis_id, keyword, title, channel, url, views, subscribers, published,
                 multiplier, thumbnail, is_winner)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    str(row.get("Keyword_Origen", "")),
                    str(row.get("Title", "")),
                    str(row.get("Channel", "")),
                    url,
                    views,
                    str(row.get("Subscribers", "Unknown")),
                    str(row.get("Published", "")),
                    multiplier,
                    str(row.get("Thumbnail", "")),
                    is_winner,
                ),
            )

            weight = self._video_weight(row)

            for pattern_type, pattern_value in self.extract_video_patterns(row):
                self.conn.execute(
                    """
                    INSERT INTO pattern_events
                    (analysis_id, video_url, pattern_type, pattern_value, weight, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (analysis_id, url, pattern_type, pattern_value, weight, now),
                )

        for idea in ideas or []:
            for pattern_type, pattern_value in self.extract_title_patterns(str(idea)):
                self.conn.execute(
                    """
                    INSERT INTO pattern_events
                    (analysis_id, video_url, pattern_type, pattern_value, weight, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (analysis_id, "", f"idea_{pattern_type}", pattern_value, final_score / 100, now),
                )

        self.conn.commit()
        return analysis_id

    def predict_opportunity(self, seeds, df_total, color_rgb=None, ideas=None):
        history_count = self.conn.execute("SELECT COUNT(*) AS total FROM analyses").fetchone()["total"]

        if history_count < 3:
            return {
                "score": None,
                "label": "Memoria insuficiente",
                "confidence": "Baja",
                "motives": [
                    f"Hay {history_count} analisis guardados. Guarda al menos 3 para empezar a predecir."
                ],
                "winning_patterns": [],
                "weak_patterns": [],
            }

        current_patterns = Counter()

        if df_total is not None and not df_total.empty:
            for _, row in df_total.head(20).iterrows():
                for key in self.extract_video_patterns(row):
                    current_patterns[key] += 1

        for idea in ideas or []:
            for key in self.extract_title_patterns(str(idea)):
                current_patterns[key] += 1

        if color_rgb is not None:
            current_patterns[("color_family", self.color_family(color_rgb))] += 2

        pattern_stats = self._pattern_stats()
        matched = []

        for pattern, count in current_patterns.items():
            stats = pattern_stats.get(pattern)
            if not stats or stats["uses"] < 2:
                continue

            matched.append({
                "pattern": pattern,
                "count": count,
                "avg_weight": stats["avg_weight"],
                "uses": stats["uses"],
                "impact": stats["avg_weight"] * min(count, 3),
            })

        if not matched:
            return {
                "score": 45,
                "label": "Nicho testeable",
                "confidence": "Baja",
                "motives": ["No hay patrones historicos parecidos suficientes."],
                "winning_patterns": [],
                "weak_patterns": [],
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
                f"Confianza {confidence.lower()} basada en {history_count} analisis guardados.",
            ],
            "winning_patterns": sorted(matched, key=lambda x: x["impact"], reverse=True)[:8],
            "weak_patterns": sorted(matched, key=lambda x: x["avg_weight"])[:5],
        }

    def leaderboard(self, limit=20):
        query = """
            SELECT pattern_type, pattern_value, COUNT(*) AS uses, AVG(weight) AS avg_weight
            FROM pattern_events
            GROUP BY pattern_type, pattern_value
            HAVING uses >= 2
            ORDER BY avg_weight DESC, uses DESC
            LIMIT ?
        """
        return pd.read_sql_query(query, self.conn, params=(limit,))

    def recent_analyses(self, limit=10):
        query = """
            SELECT created_at, seeds, final_score, auto_score, reading, total_videos
            FROM analyses
            ORDER BY id DESC
            LIMIT ?
        """
        return pd.read_sql_query(query, self.conn, params=(limit,))

    def extract_video_patterns(self, row):
        title = str(row.get("Title", ""))
        keyword = str(row.get("Keyword_Origen", ""))
        published = str(row.get("Published", "")).lower()

        patterns = []
        patterns.extend(self.extract_title_patterns(title))

        for token in self._important_tokens(keyword):
            patterns.append(("keyword_token", token))

        if "hour" in published or "day" in published:
            patterns.append(("recency", "fresh_48h"))
        elif "week" in published:
            patterns.append(("recency", "fresh_weeks"))
        elif "month" in published:
            patterns.append(("recency", "recent_months"))

        return patterns

    def extract_title_patterns(self, title):
        clean = self._clean(title)
        words = self._important_tokens(clean)
        patterns = []

        for word in words[:12]:
            patterns.append(("title_token", word))

        for i in range(len(words) - 1):
            patterns.append(("title_bigram", f"{words[i]} {words[i + 1]}"))

        format_rules = {
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

        for name, pattern in format_rules.items():
            if re.search(pattern, clean):
                patterns.append(("title_format", name))

        return patterns

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

    def _pattern_stats(self):
        rows = self.conn.execute(
            """
            SELECT pattern_type, pattern_value, COUNT(*) AS uses, AVG(weight) AS avg_weight
            FROM pattern_events
            GROUP BY pattern_type, pattern_value
            """
        ).fetchall()

        stats = {}
        for row in rows:
            stats[(row["pattern_type"], row["pattern_value"])] = {
                "uses": int(row["uses"]),
                "avg_weight": float(row["avg_weight"]),
            }

        return stats

    def _video_weight(self, row):
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

    def _important_tokens(self, text):
        stopwords = {
            "the", "and", "for", "with", "from", "this", "that", "your", "you",
            "how", "why", "what", "when", "where", "who", "are", "was", "were",
            "into", "about", "video", "videos", "official", "full", "new", "best",
            "top", "review", "analysis", "explained", "breakdown", "para", "como",
            "esta", "este", "pero", "porque", "todo", "algo", "nada"
        }

        clean = self._clean(text)
        return [w for w in clean.split() if w not in stopwords and len(w) > 3]

    def _clean(self, text):
        text = str(text).lower()
        text = re.sub(r"[^\w\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()


def format_pattern(pattern_item):
    pattern_type, pattern_value = pattern_item["pattern"]
    return f"{pattern_type}: {pattern_value}"
