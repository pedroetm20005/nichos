import streamlit as st
import requests
import json
import re
import pandas as pd
from urllib.parse import quote
from collections import Counter
import time
from youtube_transcript_api import YouTubeTranscriptApi
from PIL import Image
from io import BytesIO
from colorthief import ColorThief
import numpy as np

st.set_page_config(page_title="Minero Multinicho Pro v4.0", page_icon="🧠", layout="wide")


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
                html = response.text
                json_match = re.search(r'var ytInitialData = ({.*?});</script>', html)

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

                            if "year" in time_text.lower() or "año" in time_text.lower():
                                continue

                            title = v_renderer.get("title", {}).get("runs", [{}])[0].get("text", "Sin título")
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

        return [] if not videos else videos

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

            html = response.text

            patterns = [
                r'"ownerSubCountText":\{"simpleText":"([^"]+)"\}',
                r'"ownerSubCountText":\{"runs":\[\{"text":"([^"]+)"\}',
                r'"subscriberCountText":\{"simpleText":"([^"]+)"\}',
                r'"subscriberCountText":\{"runs":\[\{"text":"([^"]+)"\}'
            ]

            for pattern in patterns:
                match = re.search(pattern, html)
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
        clean = re.sub(r'[^\d\.KMBkmb]', '', text)
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


def fecha_es_buena(published):
    if not isinstance(published, str):
        return False

    p = published.lower()

    if "hour" in p or "day" in p or "week" in p:
        return True

    match = re.search(r"(\d+)\s+month", p)
    if match:
        return int(match.group(1)) <= 6

    return False


def recency_points(published):
    if not isinstance(published, str):
        return 0

    p = published.lower()

    if "hour" in p or "day" in p:
        return 20
    if "week" in p:
        return 18
    if "month" in p:
        return 12
    return 5


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

    palabras = texto.split()
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

    destaca = "Sí" if motivos else "No"
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
            "¿Destaca?": destaca,
            "¿Por qué llama la atención?": razon
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

            if destaca == "Sí":
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
            "¿Hay 3 videos ref.?": "Sí" if hay_3 else "No",
            "¿Alguno es de un canal pequeño?": canal_pequeno,
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
        destaca = str(row.get("¿Destaca?", "")).lower()
        subs = parse_number_label(row.get("Subs canal", ""))
        fecha = row.get("Fecha", "")

        if "sí" in destaca or "si" in destaca:
            destacados += 1

        if subs == 0:
            subs_desconocidos += 1

        if 0 < subs <= 100000:
            canales_pequenos_refs += 1

        if fecha_es_buena(str(fecha)):
            refs_recientes += 1

    if total_refs >= 3:
        score += 10
        motivos.append("Hay suficientes vídeos de referencia para comparar.")

    if destacados >= 5:
        score += 25
        motivos.append(f"Hay {destacados} vídeos marcados como destacados.")
    elif destacados >= 3:
        score += 18
        motivos.append(f"Hay {destacados} vídeos destacados.")
    elif destacados >= 1:
        score += 8
        motivos.append("Hay al menos un vídeo destacado.")

    if canales_pequenos_refs >= 2:
        score += 25
        motivos.append(f"Hay {canales_pequenos_refs} canales pequeños con señales fuertes.")
    elif canales_pequenos_refs >= 1:
        score += 15
        motivos.append("Hay al menos un canal pequeño validando la oportunidad.")

    if refs_recientes >= 3:
        score += 15
        motivos.append("Hay varias referencias recientes.")
    elif refs_recientes >= 1:
        score += 7
        motivos.append("Hay alguna referencia reciente.")

    validaciones_fuertes = 0

    if not tabla_validacion.empty:
        altas = len(tabla_validacion[tabla_validacion["Prioridad"] == "Alta"])
        medias = len(tabla_validacion[tabla_validacion["Prioridad"] == "Media"])

        for _, row in tabla_validacion.iterrows():
            hay_3 = str(row.get("¿Hay 3 videos ref.?", "")).lower()
            pequeno = bool(row.get("¿Alguno es de un canal pequeño?", False))
            prioridad = row.get("Prioridad", "")

            if ("sí" in hay_3 or "si" in hay_3) and pequeno and prioridad == "Alta":
                validaciones_fuertes += 1

        if validaciones_fuertes >= 2:
            score += 25
            motivos.append("Hay varias ideas con 3 referencias, canal pequeño y prioridad alta.")
        elif validaciones_fuertes >= 1:
            score += 18
            motivos.append("Hay una idea clara con 3 referencias, canal pequeño y prioridad alta.")
        elif altas >= 1:
            score += 12
            motivos.append("Hay al menos una idea en prioridad alta.")
        elif medias >= 2:
            score += 8
            motivos.append("Hay varias ideas en prioridad media.")

    if subs_desconocidos > 0:
        motivos.append(f"{subs_desconocidos} referencias tienen subs desconocidos: conviene revisarlos a mano.")

    score = min(score, 100)

    if score >= 75:
        lectura = "🔥 Nicho rentable / Alta prioridad"
    elif score >= 55:
        lectura = "📈 Nicho prometedor"
    elif score >= 35:
        lectura = "🧪 Nicho testeable"
    else:
        lectura = "🧊 Señal débil"

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
        instrucciones.append("El nicho tira a tonos cálidos: rojo/naranja puede funcionar para peligro, emoción o urgencia.")
    elif b > r and b > g:
        instrucciones.append("El nicho tira a tonos fríos: azul/cian puede funcionar para misterio, tecnología o fantasía.")
    elif g > r and g > b:
        instrucciones.append("El nicho tira a tonos verdes: úsalo para naturaleza, juego, crecimiento, rareza o toxicidad.")
    else:
        instrucciones.append("Color dominante neutro: apóyate más en caras, flechas, círculos y contraste.")

    if top:
        instrucciones.append(f"Elementos a probar en miniatura: {', '.join(top)}.")

    instrucciones.append("Texto recomendado: 2 a 4 palabras, grande, sin frases largas.")
    instrucciones.append("Promesa visual: antes/después, secreto, peligro, reto extremo o comparación clara.")

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
        motivos.append(f"Hay señales moderadas de outlier con {max_multi:.1f}x.")

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
        motivos.append("Hay algunas señales recientes.")

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


def generar_ideas_ataque(df_total, guiones_data, nichos_similares, limite=14):
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
        "100_days": [
            "I Spent 100 Days Inside {}",
            "100 Days Trying to Master {}",
            "I Survived 100 Days With Only {}"
        ],
        "build": [
            "I Built a {} That Should Not Exist",
            "I Built the Most Dangerous {}",
            "I Built {} and Instantly Regretted It"
        ],
        "survival": [
            "I Tried to Survive {}",
            "Surviving the Hardest {} Challenge",
            "{} Survival Gets Worse Every Minute"
        ],
        "secret": [
            "The Hidden Truth About {}",
            "The Secret Side of {}",
            "What Nobody Tells You About {}"
        ],
        "why": [
            "Why {} Is Taking Over YouTube",
            "Why Everyone Suddenly Cares About {}",
            "Why {} Works So Well"
        ],
        "versus": [
            "{} vs The Most Impossible Challenge",
            "I Compared {} With Its Biggest Rival",
            "{} vs Everything That Tries to Stop It"
        ],
        "escalation": [
            "{} Gets Harder Every Minute",
            "{} But Every Step Makes It Worse",
            "I Tried {}, But It Kept Escalating"
        ],
        "ignored": [
            "Nobody Talks About {}",
            "The {} Everyone Ignored",
            "No One Expected {} To Work"
        ],
        "story": [
            "The Complete Story of {}",
            "The Rise and Fall of {}",
            "The Strange History of {}"
        ],
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

    for b in top_bigramas:
        temas_finales.append(b)

    for t in top_temas:
        temas_finales.append(t)

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


st.title("♾️ Minero Pro v4.0: Motor Universal Multi-Nicho")
st.markdown("Busca keywords en red, detecta outliers, valida nichos, analiza guiones, miniaturas e ideas de ataque.")

st.sidebar.header("⚙️ Configuración")
input_raw = st.sidebar.text_area("Palabras Semilla (Sepáralas con comas):", value="how to train your dragon")
outlier_factor = st.sidebar.slider("Sensibilidad del Filtro (x)", 1.1, 5.0, 1.2, step=0.1)
max_ciclos = st.sidebar.slider("Tope maximo de ramas totales:", 2, 120, 50)
max_guiones = st.sidebar.slider("Videos buenos para analizar guion:", 1, 10, 5)
max_keywords_expansion = st.sidebar.slider("Keywords reales máximas:", 50, 500, 250)
profundidad_keywords = st.sidebar.slider("Profundidad de red:", 1, 4, 3)

if st.sidebar.button("⚡ Iniciar Bucle de Profundidad Absoluta", use_container_width=True):
    st.session_state.outliers_data = []
    st.session_state.guiones_data = []
    st.session_state.nichos_similares = []
    st.session_state.keywords_generadas = []

    historico_outliers = []
    keywords_procesadas = set()
    guiones_acumulados = []

    miner = YouTubeHyperMiner()
    semillas_iniciales = [k.strip().lower() for k in input_raw.split(",") if k.strip()]

    status_box = st.empty()
    progress_bar = st.progress(0)

    status_box.info("🧬 Expandiendo keywords en red con autocomplete de YouTube...")

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

        status_box.info(f"🔮 Excavando en rama [{ciclo_actual}/{total_ramas_encontradas}]: **{kw_actual}**")
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
            status_box.text(f"📖 [{index + 1}/{len(top_videos)}] Extrayendo guion de: {row['Title'][:60]}...")
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

    status_box.success("🎉 ¡Mapeo completado con exito!")
    st.session_state.outliers_data = historico_outliers
    st.session_state.guiones_data = guiones_acumulados
    st.session_state.nichos_similares = nichos_similares


if "keywords_generadas" in st.session_state and st.session_state.keywords_generadas:
    with st.expander("🧬 Keywords generadas por la expansión en red", expanded=False):
        st.write(f"Total generadas: {len(st.session_state.keywords_generadas)}")
        st.text_area(
            "Keywords encontradas:",
            value=", ".join(st.session_state.keywords_generadas),
            height=180
        )


if "outliers_data" in st.session_state and st.session_state.outliers_data:
    df_total = pd.DataFrame(st.session_state.outliers_data)
    df_total = df_total.drop_duplicates(subset=["URL"]).sort_values(by="Multiplicador", ascending=False)

    if "Subscribers" not in df_total.columns:
        df_total["Subscribers"] = "Unknown"

    df_total["Subscribers"] = df_total["Subscribers"].fillna("Unknown")

    def clasificar(m):
        if m >= 3.0:
            return "💥 BOMBAZO"
        elif m >= 2.0:
            return "🔥 MINA DE ORO"
        return "📈 PROMETEDOR"

    df_total["Potencial"] = df_total["Multiplicador"].apply(clasificar)

    st.markdown("## 📈 Videos Outliers Detectados")

    render_df = df_total.copy()
    render_df["Subscribers"] = render_df["Subscribers"].fillna("Unknown")
    render_df["Views"] = render_df["Views"].map('{:,.0f}'.format)
    render_df["Multiplicador"] = render_df["Multiplicador"].map('{:,.1f}x'.format)

    columnas_tabla = [
        "Potencial", "Keyword_Origen", "Title", "Channel", "Subscribers",
        "Views", "Published", "Multiplicador", "URL"
    ]

    st.dataframe(render_df[columnas_tabla], use_container_width=True)

    st.markdown("---")
    st.markdown("## 🧠 Inteligencia de Oportunidad")

    score_auto, motivos_auto = calcular_score_oportunidad(df_total)

    tabla_referencias = crear_tabla_referencias(df_total)
    tabla_validacion = crear_tabla_validacion(df_total)

    with st.expander("📋 Ver / editar validacion real del nicho", expanded=True):
        st.warning("Si ves Subs canal como Hidden/Unknown, puedes escribirlos a mano. Ejemplo: 25K, 80K, 1.2M. La nota final usara lo que edites aqui.")

        st.markdown("### Tabla de ideas de los canales referencia")
        tabla_referencias_editada = st.data_editor(
            tabla_referencias,
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "¿Destaca?": st.column_config.SelectboxColumn(
                    "¿Destaca?",
                    options=["Sí", "No"],
                    required=True
                ),
                "Link": st.column_config.LinkColumn("Link"),
                "Subs canal": st.column_config.TextColumn("Subs canal"),
                "¿Por qué llama la atención?": st.column_config.TextColumn("¿Por qué llama la atención?")
            },
            key="tabla_referencias_editor"
        )

        st.markdown("### Tabla de validacion de ideas")
        tabla_validacion_editada = st.data_editor(
            tabla_validacion,
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "¿Hay 3 videos ref.?": st.column_config.SelectboxColumn(
                    "¿Hay 3 videos ref.?",
                    options=["Sí", "No"],
                    required=True
                ),
                "Prioridad": st.column_config.SelectboxColumn(
                    "Prioridad",
                    options=["Alta", "Media", "Baja"],
                    required=True
                ),
                "¿Alguno es de un canal pequeño?": st.column_config.CheckboxColumn(
                    "¿Alguno es de un canal pequeño?"
                )
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
        with st.expander("Ver señales automaticas extra"):
            for m in motivos_auto:
                st.write(f"- {m}")

    ideas = generar_ideas_ataque(
        df_total,
        st.session_state.get("guiones_data", []),
        st.session_state.get("nichos_similares", []),
        limite=14
    )

    st.markdown("### 🎯 Ideas de video")
    st.text_area("Ideas listas para adaptar:", value="\n".join(ideas), height=220)

    st.markdown("### 🖼️ Direccion de miniatura")
    color_patron = analizar_patron_ganador(df_total.head(12)["Thumbnail"].tolist())
    st.write(f"Color dominante detectado: RGB {color_patron}")

    for instruccion in generar_direccion_miniatura(df_total, color_patron):
        st.write(f"- {instruccion}")

    st.markdown("---")
    st.markdown("## 🧠 Cajas de Extraccion Rapida")

    c1, c2 = st.columns(2)
    semilla_referencia = df_total["Keyword_Origen"].iloc[0] if not df_total.empty else "video"

    with c1:
        st.markdown("### 🔗 Basado en Titulos Ganadores")
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
        st.text_area("📋 Patrones de Titulos:", value=", ".join(list(dict.fromkeys(sug_titulos_caja))), height=100)

    with c2:
        st.markdown("### 📖 Basado en lo Hablado en Guiones")

        if "guiones_data" in st.session_state and st.session_state.guiones_data:
            texto_global = " ".join([g["Texto"] for g in st.session_state.guiones_data])
            conceptos_guion = extraer_conceptos_de_texto(texto_global, limite=10)
            sug_guion_caja = [f"{semilla_referencia} {palabra}" for palabra, _ in conceptos_guion]
            st.text_area("📋 Palabras clave del Guion:", value=", ".join(list(dict.fromkeys(sug_guion_caja))), height=100)
        else:
            st.info("No hay suficientes transcripciones procesadas aun en esta tanda.")

    st.markdown("---")
    st.markdown("## 🧭 Nichos Similares Para Seguir Excavando")

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
        st.info("Aun no hay suficientes señales para sugerir nichos similares.")

    st.markdown("---")
    st.markdown("## 🖼️ Miniaturas Ganadoras del Nicho")

    thumbs_df = df_total.head(12).copy()
    cols = st.columns(3)

    for i, (_, row) in enumerate(thumbs_df.iterrows()):
        with cols[i % 3]:
            subs = row["Subscribers"] if "Subscribers" in row and pd.notna(row["Subscribers"]) else "Unknown"

            st.image(row["Thumbnail"], use_container_width=True)
            st.markdown(f"**{row['Title']}**")
            st.caption(f"{row['Channel']} · {subs} subs · {row['Views']:,.0f} views · {row['Multiplicador']:.1f}x · {row['Published']}")
            st.markdown(f"[Abrir video]({row['URL']})")

    st.markdown("---")
    st.markdown("## 📖 Radar de Transcripciones")

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

    st.markdown("---")
    st.markdown("## 🎨 Generador de Base de Miniatura Basado en Datos")

    if st.button("Analizar miniaturas ganadoras y generar base"):
        urls = df_total.head(12)["Thumbnail"].tolist()
        patron = analizar_patron_ganador(urls)

        st.write(f"Patron de color detectado: {patron}")

        base_img = crear_base_miniatura(patron)
        st.image(base_img, caption="Miniatura base generada con el color dominante del nicho")

else:
    st.info("Introduce una palabra raiz a la izquierda y arranca el motor profundo.")
