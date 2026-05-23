# -*- coding: utf-8 -*-
"""ContextBuilder — selección y resumen inteligente de contexto para IA.

Estrategia de cuatro fases (sin usar IA externa para resumir, todo heurístico
local para no inflar costos):

  FASE 1 — extract_keywords(case_text):
      Tokeniza el caso del cliente (antecedente, objetivo, archivos cliente)
      en lowercase, filtra stopwords español, devuelve set de palabras > 3
      caracteres con peso ajustado por frecuencia.

  FASE 2 — score_relevance(source_text, keywords):
      Cuenta apariciones de cada keyword en el texto. Cero hits → 0 score.

  FASE 3 — summarize_by_keywords(text, keywords, max_chars):
      Si el texto cabe entero, devuelve original. Si no:
        a) Divide en párrafos (separadores: \\n\\n, == HEADER ==, etc.).
        b) Puntea cada párrafo por keyword hits.
        c) Selecciona en orden de score descendente hasta agotar presupuesto.
        d) Re-ordena por orden original para preservar coherencia narrativa.
        e) Anexa metadato "[resumido X/Y chars]" al final.
      Garantiza que el contenido devuelto NO supere max_chars.

  FASE 4 — select_relevant_sources(sources, keywords, ...):
      Aplica score por fuente, ordena, selecciona hasta total_budget,
      resume cada una con su sub-presupuesto. Devuelve junto con metadatos
      de auditoría: cuáles se incluyeron completas, resumidas u omitidas.

PUERTO DE ENTRADA del orquestador: build_relevant_context() en
valoracion_valoracion._build_fuentes_block / _build_archivos_cliente_block.
"""
import re
import logging

_logger = logging.getLogger(__name__)


# =====================================================================
# Stopwords español (lista corta, suficiente para limpieza de keywords).
# No incluye términos clínicos (los queremos como keywords).
# =====================================================================
STOPWORDS_ES = frozenset([
    'que', 'con', 'por', 'para', 'una', 'uno', 'las', 'los', 'del', 'esta',
    'este', 'estos', 'estas', 'pero', 'como', 'son', 'sus', 'sin', 'sobre',
    'todo', 'toda', 'todos', 'todas', 'mas', 'más', 'muy', 'donde', 'cuando',
    'porque', 'desde', 'hasta', 'entre', 'durante', 'también', 'cada', 'otro',
    'otra', 'otros', 'otras', 'mismo', 'misma', 'algunos', 'algunas', 'tener',
    'haber', 'hacer', 'puede', 'pueden', 'debe', 'deben', 'estar', 'esto',
    'eso', 'aqui', 'aquí', 'alli', 'allí', 'siempre', 'nunca', 'tanto',
    'mucho', 'poco', 'segun', 'según', 'cual', 'cuál', 'cuales', 'cuáles',
    'quien', 'quién', 'esta', 'están', 'estaba', 'estaban', 'fueron', 'sera',
    'será', 'fue', 'sido', 'siendo', 'sea', 'seas', 'sean', 'eres', 'somos',
    'soy', 'fui', 'fuimos', 'porque', 'cómo', 'qué', 'pues', 'aún', 'aun',
    'ahora', 'luego', 'antes', 'después', 'dentro', 'fuera', 'arriba',
    'abajo', 'menos', 'tanto', 'tanta', 'tantos', 'tantas',
    # Conectores genéricos que no aportan
    'caso', 'casos', 'tipo', 'tipos', 'forma', 'formas', 'parte', 'partes',
    'nivel', 'niveles', 'manera', 'maneras', 'momento', 'momentos',
    'persona', 'personas', 'cliente', 'clientes', 'paciente', 'pacientes',
    'producto', 'productos',  # demasiado genéricos en este dominio
])


# =====================================================================
# Presupuestos por defecto (caracteres aproximados).
# Conversión heurística: 1 token ≈ 3.5 caracteres en español.
# Objetivo del prompt total: 8k-20k tokens = 28k-70k chars.
# =====================================================================
class Limits(object):
    """Presupuestos en caracteres por sección. Modificable desde el caller."""
    # Por archivo individual del cliente (PDFs de estudios, etc.)
    MAX_CHARS_PER_CLIENT_FILE = 6000
    # Total de archivos del cliente combinados
    MAX_CHARS_CLIENT_FILES_TOTAL = 18000
    # Por fuente individual (catálogo, fichas, etc.)
    MAX_CHARS_PER_SOURCE = 8000
    # Total de fuentes combinadas
    MAX_CHARS_SOURCES_TOTAL = 28000
    # Tamaño mínimo a partir del cual se resume (debajo, se incluye completo)
    SMALL_TEXT_THRESHOLD = 3000
    # Si la cantidad disponible para una fuente es menor a esto, se omite
    MIN_USEFUL_BUDGET = 600
    # Catálogo estructurado VitalHealth (Fase A): bloque oficial enviado
    # SIEMPRE al prompt, separado de las fuentes. ~4.300 tokens de margen.
    MAX_CHARS_CATALOGO = 15000


# =====================================================================
# Funciones puras (sin Odoo)
# =====================================================================

# Regex tokenizador: palabras alfabéticas con acentos, longitud >= 4.
_RE_WORD = re.compile(r"\b[a-záéíóúñüA-ZÁÉÍÓÚÑÜ]{4,}\b", re.UNICODE)
# Regex para detectar separadores de párrafo robusto.
_RE_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+|\n=+[^\n]*=+\n|\n-{3,}\n", re.MULTILINE)


def extract_keywords(case_text, max_keywords=80):
    """Extrae keywords del texto del caso (antecedente, objetivo, archivos).

    Devuelve dict {keyword: weight} con weight = frecuencia normalizada.
    Las keywords con peso > 1 son las que más se repiten en el caso.
    """
    if not case_text:
        return {}
    words = _RE_WORD.findall(case_text.lower())
    freq = {}
    for w in words:
        # Quitar acentos para matching más permisivo (resfriado vs resfríado, etc.)
        w_norm = _strip_accents(w)
        if w_norm in STOPWORDS_ES or len(w_norm) < 4:
            continue
        freq[w_norm] = freq.get(w_norm, 0) + 1
    # Ordenar por frecuencia descendente y truncar
    sorted_kw = sorted(freq.items(), key=lambda x: -x[1])
    return dict(sorted_kw[:max_keywords])


def _strip_accents(s):
    """Lowercase + sin acentos. Igual que en otros helpers del módulo."""
    import unicodedata
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def score_relevance(text, keywords_dict):
    """Cuenta hits ponderados de keywords en el texto.

    Args:
        text: str a evaluar.
        keywords_dict: {keyword: weight_int} producido por extract_keywords.

    Returns:
        int — score acumulado (0 si ninguna keyword aparece).
    """
    if not text or not keywords_dict:
        return 0
    text_norm = _strip_accents(text)
    score = 0
    for kw, weight in keywords_dict.items():
        # count() es O(n*m); aceptable para textos < 100KB.
        hits = text_norm.count(kw)
        if hits:
            score += hits * max(weight, 1)
    return score


def summarize_by_keywords(text, keywords_dict, max_chars):
    """Resume un texto manteniendo los párrafos más relevantes a las keywords.

    Garantías:
      * len(retorno) <= max_chars + ~80 (margen para metadato).
      * Conserva orden original de los párrafos seleccionados.
      * Si len(text) <= max_chars, devuelve text intacto.
      * Si no hay keywords, hace truncado simple al final.

    Devuelve (texto_resumido, info_dict) donde info_dict contiene:
        original_chars, kept_chars, paragraphs_total, paragraphs_kept,
        method ('full'|'summarized'|'truncated').
    """
    if not text:
        return '', {'original_chars': 0, 'kept_chars': 0, 'method': 'empty',
                    'paragraphs_total': 0, 'paragraphs_kept': 0}

    original_chars = len(text)
    # Caso 1: cabe completo
    if original_chars <= max_chars:
        return text, {
            'original_chars': original_chars,
            'kept_chars': original_chars,
            'method': 'full',
            'paragraphs_total': 1,
            'paragraphs_kept': 1,
        }

    # Caso 2: sin keywords → truncado simple al final
    if not keywords_dict:
        truncated = text[:max_chars - 60].rstrip()
        meta = '\n[Truncado %d/%d chars]' % (len(truncated), original_chars)
        return truncated + meta, {
            'original_chars': original_chars,
            'kept_chars': len(truncated),
            'method': 'truncated',
            'paragraphs_total': 1,
            'paragraphs_kept': 1,
        }

    # Caso 3: resumen por relevancia
    paragraphs = _RE_PARAGRAPH_SPLIT.split(text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    total_paragraphs = len(paragraphs)
    if total_paragraphs <= 1:
        # No hay separadores claros → recorrer por líneas
        paragraphs = [ln.strip() for ln in text.split('\n') if ln.strip()]
        total_paragraphs = len(paragraphs)

    # Score cada párrafo
    scored = []
    for idx, p in enumerate(paragraphs):
        s = score_relevance(p, keywords_dict)
        scored.append((s, idx, p))

    # Orden: por score desc, luego por posición original asc
    scored.sort(key=lambda x: (-x[0], x[1]))

    # Selección con presupuesto
    budget = max_chars - 80  # margen para metadato final
    selected_indices = []
    used = 0
    for s, idx, p in scored:
        plen = len(p) + 2  # +2 por '\n\n' separador
        if used + plen > budget:
            # ¿Vale la pena meter una versión recortada?
            remaining = budget - used
            if s > 0 and remaining > 300:
                # Recortar este párrafo a remaining chars
                snippet = p[:remaining - 20].rstrip() + '...'
                selected_indices.append((idx, snippet))
                used += len(snippet) + 2
            # Cortar selección
            break
        if s == 0 and used > budget * 0.4:
            # Ya tenemos suficiente sin párrafos irrelevantes
            continue
        selected_indices.append((idx, p))
        used += plen

    # Re-ordenar por posición original para mantener narrativa
    selected_indices.sort(key=lambda x: x[0])
    body = '\n\n'.join(p for _, p in selected_indices)
    meta = '\n\n[Resumido por relevancia: %d/%d chars, %d/%d párrafos]' % (
        len(body), original_chars, len(selected_indices), total_paragraphs,
    )
    result = body + meta
    return result, {
        'original_chars': original_chars,
        'kept_chars': len(result),
        'method': 'summarized',
        'paragraphs_total': total_paragraphs,
        'paragraphs_kept': len(selected_indices),
    }


def select_and_summarize_sources(sources, keywords_dict,
                                 max_chars_per_source=Limits.MAX_CHARS_PER_SOURCE,
                                 max_chars_total=Limits.MAX_CHARS_SOURCES_TOTAL,
                                 min_useful_budget=Limits.MIN_USEFUL_BUDGET):
    """Selecciona, resume y combina N fuentes en un único bloque de texto.

    Args:
        sources: lista de dicts [{name, tipo, text, sequence}] (sequence opcional).
        keywords_dict: dict de keywords del caso.
        max_chars_per_source: cap individual por fuente.
        max_chars_total: presupuesto total combinado.
        min_useful_budget: si lo que queda es menor que esto, omitir el resto.

    Returns:
        (texto_combinado, lista_metadatos)
        donde lista_metadatos = [
            {name, tipo, score, original_chars, kept_chars, method, included},
            ...
        ]
        included puede ser: 'completa', 'resumida', 'recortada', 'omitida_irrelevante',
                            'omitida_sin_presupuesto'.
    """
    if not sources:
        return '', []

    # 1. Score cada fuente
    scored_sources = []
    for src in sources:
        text = src.get('text') or ''
        s = score_relevance(text, keywords_dict)
        scored_sources.append({
            'src': src,
            'score': s,
            'text': text,
            'name': src.get('name') or '(sin nombre)',
            'tipo': src.get('tipo') or '',
            'sequence': src.get('sequence', 9999),
        })

    # 2. Ordenar: primero por score desc, luego por sequence (prioridad asc)
    scored_sources.sort(key=lambda x: (-x['score'], x['sequence']))

    # 3. Construir bloques con presupuesto
    chunks = []
    used_total = 0
    metadatos = []
    for entry in scored_sources:
        name = entry['name']
        tipo = entry['tipo']
        text = entry['text']
        score = entry['score']
        original_len = len(text)

        # Presupuesto restante
        remaining_total = max_chars_total - used_total
        if remaining_total < min_useful_budget:
            metadatos.append({
                'name': name,
                'tipo': tipo,
                'score': score,
                'original_chars': original_len,
                'kept_chars': 0,
                'method': 'omitted',
                'included': 'omitida_sin_presupuesto',
            })
            continue

        # Si el score es 0 y ya tenemos contenido, posiblemente omitir
        if score == 0 and used_total > max_chars_total * 0.6:
            metadatos.append({
                'name': name,
                'tipo': tipo,
                'score': 0,
                'original_chars': original_len,
                'kept_chars': 0,
                'method': 'omitted',
                'included': 'omitida_irrelevante',
            })
            continue

        # Cap individual: el menor entre per_source y remaining_total
        budget_for_this = min(max_chars_per_source, remaining_total)

        summary, info = summarize_by_keywords(text, keywords_dict, budget_for_this)
        # Header de la fuente
        tipo_label = (' (%s)' % tipo) if tipo else ''
        header = "=== Fuente: %s%s ===\n" % (name, tipo_label)
        chunk = header + summary
        chunks.append(chunk)
        used_total += len(chunk) + 2  # +2 por '\n\n' separador

        included_label = {
            'full': 'completa',
            'summarized': 'resumida',
            'truncated': 'recortada',
        }.get(info.get('method'), 'parcial')
        metadatos.append({
            'name': name,
            'tipo': tipo,
            'score': score,
            'original_chars': original_len,
            'kept_chars': info.get('kept_chars', len(chunk)),
            'method': info.get('method'),
            'included': included_label,
            'paragraphs_total': info.get('paragraphs_total'),
            'paragraphs_kept': info.get('paragraphs_kept'),
        })

    body = '\n\n'.join(chunks) if chunks else ''
    return body, metadatos


def format_audit_summary(metadatos, etiqueta='Fuentes'):
    """Devuelve un resumen legible para el log IA.

    Ejemplo de output:
        Fuentes (3 procesadas, 1 omitida):
          ✓ Catálogo VitalHealth (catalogo)
              score=24, completa, 2800/2800 chars
          ↓ Fichas técnicas (fichas_tecnicas)
              score=12, resumida, 6000/18500 chars (3/12 párrafos)
          ✗ Lista precios v2 (lista_precios)
              score=0, omitida_irrelevante
    """
    if not metadatos:
        return etiqueta + ': (ninguna procesada)'

    incluidas = [m for m in metadatos if m.get('kept_chars', 0) > 0]
    omitidas = [m for m in metadatos if m.get('kept_chars', 0) == 0]

    lines = ['%s (%d procesadas, %d omitidas):' % (
        etiqueta, len(incluidas), len(omitidas),
    )]

    for m in metadatos:
        kept = m.get('kept_chars', 0)
        original = m.get('original_chars', 0)
        if kept > 0:
            if m.get('included') == 'completa':
                marker = '✓'
            else:
                marker = '↓'
        else:
            marker = '✗'
        tipo = (' (%s)' % m['tipo']) if m.get('tipo') else ''
        lines.append('  %s %s%s' % (marker, m.get('name', '?'), tipo))
        if kept > 0:
            extra = ''
            if m.get('paragraphs_total') and m.get('paragraphs_total') > 1:
                extra = ' (%d/%d párrafos)' % (
                    m.get('paragraphs_kept', 0), m.get('paragraphs_total', 0),
                )
            lines.append('      score=%d, %s, %d/%d chars%s' % (
                m.get('score', 0), m.get('included', '?'), kept, original, extra,
            ))
        else:
            lines.append('      score=%d, %s' % (
                m.get('score', 0), m.get('included', 'omitida'),
            ))
    return '\n'.join(lines)
