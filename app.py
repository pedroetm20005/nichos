import streamlit as st
import requests
import json
import re
import pandas as pd
from urllib.parse import quote
from collections import Counter
import time
from youtube_transcript_api import YouTubeTranscriptApi

st.set_page_config(page_title="Minero Multinicho Pro v3.0", page_icon="🧠", layout="wide")


class YouTubeHyperMiner:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }

    def get_youtube_suggestions(self, keyword):
        url = f"https://suggestqueries.google.com/complete/search?client=youtube&hl=en&ds=yt&q={quote(keyword)}"
        try:
            response = requests.get(url, headers=self.headers, timeout=7)
            if response.status_code == 200:
                clean_text = response.text
                if "(" in clean_text:
                    clean_text = clean_text[clean_text.find("(")+1 : clean_text.rfind(")")]
                data = json.loads(clean_text)
                return [item[0] for item in data[1] if item[0] != keyword]
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
                            channel_name = v_renderer.get("longBylineText", {}).get("runs", [{}])[0].get("text", "Canal")
                            views_text = v_renderer.get("viewCountText", {}).get("simpleText", "0")
                            views = self._parse_views(views_text)

                            if v_id and views > 0:
                                videos.append({
                                    "Title": title,
                                    "Channel": channel_name,
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
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'es'])
            except Exception:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

                try:
                    transcript_obj = transcript_list.find_transcript(['en', 'es'])
                except Exception:
                    transcript_obj = transcript_list.find_generated_transcript(['en', 'es'])

                transcript = transcript_obj.fetch()

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


def extraer_conceptos_de_texto(texto, limite=12):
    stopwords = {
        'the', 'and', 'you', 'that', 'this', 'with', 'they', 'have', 'what', 'about',
        'just', 'like', 'your', 'here', 'there', 'from', 'then', 'gonna', 'wanna',
        'know', 'back', 'down', 'into', 'them', 'for', 'are', 'was', 'were', 'when',
        'where', 'why', 'how', 'can', 'could', 'would', 'should', 'there', 'their',
        'these', 'those', 'thing', 'things', 'really', 'actually', 'going', 'make',
        'made', 'much', 'many', 'more', 'most', 'very', 'some', 'also', 'because',
        'para', 'como', 'esta', 'este', 'bueno', 'bien', 'pero', 'porque',
        'entonces', 'hacer', 'todo', 'algo', 'nada', 'cuando'
    }

    palabras = texto.split()
    palabras_filtradas = [
        p for p in palabras
        if p not in stopwords and len(p) > 4
    ]

    return Counter(palabras_filtradas).most_common(limite)


st.title("♾️ Minero Pro v3.0: Motor Universal Multi-Nicho")
st.markdown("Válido para cualquier temática. Busca en mercado inglés, detecta outliers, analiza guiones y muestra miniaturas ganadoras.")

st.sidebar.header("⚙️ Configuración")
input_raw = st.sidebar.text_area("Palabras Semilla (Sepáralas con comas):", value="how to train your dragon")
outlier_factor = st.sidebar.slider("Sensibilidad del Filtro (x)", 1.1, 5.0, 1.2, step=0.1)
max_ciclos = st.sidebar.slider("Tope máximo de ramas totales:", 2, 40, 15)
max_guiones = st.sidebar.slider("Videos buenos para analizar guion:", 1, 10, 5)

if st.sidebar.button("⚡ Iniciar Bucle de Profundidad Absoluta", use_container_width=True):
    historico_outliers = []
    keywords_procesadas = set()
    guiones_acumulados = []

    miner = YouTubeHyperMiner()

    semillas_iniciales = [k.strip().lower() for k in input_raw.split(",") if k.strip()]

    cola_keywords = []
    for semilla in semillas_iniciales:
        if semilla not in cola_keywords:
            cola_keywords.append(semilla)

        sugerencias_base = miner.get_youtube_suggestions(semilla)
        for sug in sugerencias_base:
            if sug not in cola_keywords:
                cola_keywords.append(sug)

        mods_universales = [
            " theory",
            " story",
            " secret",
            " analysis",
            " 2026",
            " explained",
            " ending",
            " facts",
            " breakdown",
            " review"
        ]

        for mod in mods_universales:
            frase_mod = f"{semilla}{mod}"
            if frase_mod not in cola_keywords:
                cola_keywords.append(frase_mod)

    status_box = st.empty()
    progress_bar = st.progress(0)
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
                        historico_outliers.append(row.to_dict())

        time.sleep(0.3)

    if historico_outliers:
        df_ordenado = (
            pd.DataFrame(historico_outliers)
            .drop_duplicates(subset=["URL"])
            .sort_values(by="Multiplicador", ascending=False)
        )

        top_videos = df_ordenado.head(max_guiones).to_dict(orient="records")

        for index, row in enumerate(top_videos):
            status_box.text(f"📖 [{index+1}/{len(top_videos)}] Extrayendo guion de: {row['Title'][:60]}...")
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

    status_box.success("🎉 ¡Mapeo completado con éxito!")
    st.session_state.outliers_data = historico_outliers
    st.session_state.guiones_data = guiones_acumulados


if "outliers_data" in st.session_state and st.session_state.outliers_data:
    df_total = pd.DataFrame(st.session_state.outliers_data)
    df_total = df_total.drop_duplicates(subset=["URL"]).sort_values(by="Multiplicador", ascending=False)

    def clasificar(m):
        if m >= 3.0:
            return "💥 BOMBAZO"
        elif m >= 2.0:
            return "🔥 MINA DE ORO"
        else:
            return "📈 PROMETEDOR"

    df_total["Potencial"] = df_total["Multiplicador"].apply(clasificar)

    st.markdown("## 📈 Vídeos Outliers Detectados")
    render_df = df_total.copy()
    render_df["Views"] = render_df["Views"].map('{:,.0f}'.format)
    render_df["Multiplicador"] = render_df["Multiplicador"].map('{:,.1f}x'.format)

    st.dataframe(
        render_df[["Potencial", "Keyword_Origen", "Title", "Channel", "Views", "Published", "Multiplicador", "URL"]],
        use_container_width=True
    )

    st.markdown("---")
    st.markdown("## 🧠 Cajas de Extracción Rápida (Copia y Pega Masivo)")

    c1, c2 = st.columns(2)

    semilla_referencia = df_total["Keyword_Origen"].iloc[0] if not df_total.empty else "video"

    with c1:
        st.markdown("### 🔗 Basado en Títulos Ganadores")
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
                bigramas.append(f"{palabras[i]} {palabras[i+1]}")

        top_bi = Counter(bigramas).most_common(8)
        sug_titulos_caja = []
        for token, count in top_bi:
            sug_titulos_caja.append(f"{semilla_referencia} {token}")

        txt_titulos_final = ", ".join(list(dict.fromkeys(sug_titulos_caja)))
        st.text_area("📋 Patrones de Títulos:", value=txt_titulos_final, height=100)

    with c2:
        st.markdown("### 📖 Basado en lo Hablado en Guiones")
        if "guiones_data" in st.session_state and st.session_state.guiones_data:
            texto_global = " ".join([g["Texto"] for g in st.session_state.guiones_data])

            conceptos_guion = extraer_conceptos_de_texto(texto_global, limite=10)

            sug_guion_caja = []
            for palabra, count in conceptos_guion:
                sug_guion_caja.append(f"{semilla_referencia} {palabra}")

            txt_guion_final = ", ".join(list(dict.fromkeys(sug_guion_caja)))
            st.text_area("📋 Palabras clave del Guion:", value=txt_guion_final, height=100)
        else:
            st.info("No hay suficientes transcripciones procesadas aún en esta tanda.")

    st.markdown("---")
    st.markdown("## 🖼️ Miniaturas Ganadoras del Nicho")
    st.caption("Miniaturas de los vídeos outlier. Mira patrones de color, caras, texto grande, contraste, composición y promesa visual.")

    thumbs_df = df_total.head(12).copy()
    cols = st.columns(3)

    for i, (_, row) in enumerate(thumbs_df.iterrows()):
        with cols[i % 3]:
            st.image(row["Thumbnail"], use_container_width=True)
            st.markdown(f"**{row['Title']}**")
            st.caption(f"{row['Channel']} · {row['Views']:,.0f} views · {row['Multiplicador']:.1f}x · {row['Published']}")
            st.markdown(f"[Abrir vídeo]({row['URL']})")

    st.markdown("---")
    st.markdown("## 📖 Radar de Transcripciones")
    st.caption("Aquí ves qué temas aparecen dentro de los vídeos buenos, no solo en sus títulos.")

    if "guiones_data" in st.session_state and st.session_state.guiones_data:
        for i, guion in enumerate(st.session_state.guiones_data, start=1):
            with st.expander(f"{i}. {guion['Title']}"):
                st.markdown(f"**Canal:** {guion['Channel']}")
                st.markdown(f"**Keyword origen:** {guion['Keyword_Origen']}")
                st.markdown(f"[Abrir vídeo]({guion['URL']})")

                conceptos = guion["Conceptos"]

                if conceptos:
                    st.markdown("**Conceptos fuertes detectados:**")
                    st.write(", ".join([f"{palabra} ({count})" for palabra, count in conceptos]))

                    nuevas_busquedas = [
                        f"{semilla_referencia} {palabra}"
                        for palabra, count in conceptos[:8]
                    ]

                    st.text_area(
                        "Nuevas búsquedas sugeridas desde esta transcripción:",
                        value=", ".join(nuevas_busquedas),
                        height=80,
                        key=f"ideas_guion_{i}"
                    )

                preview = guion["Texto"][:2500]

                st.text_area(
                    "Fragmento de transcripción:",
                    value=preview,
                    height=180,
                    key=f"preview_guion_{i}"
                )
    else:
        st.info("No hay transcripciones disponibles en esta tanda.")

else:
    st.info("Introduce una palabra raíz a la izquierda y arranca el motor profundo.")