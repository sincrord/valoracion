# -*- coding: utf-8 -*-
"""Narrative Renderer wellness (Fase 4.2).

Capa Python pura que humaniza la salida IA antes de que llegue al
PDF cliente. No depende de Odoo, no toca BD, no llama IA, no consume
tokens — es post-procesado local 100% determinístico.

Reglas arquitectónicas (CLAUDE.md):
  * NO modificar el motor v3 (extractor/classifier/severity/ranker).
  * NO alterar productos recomendados, cantidades, ni reglas comerciales.
  * NO alterar advertencias (requisito legal: menciones explícitas).
  * NO inventar contenido — sólo reescribir labels técnicos y aplicar
    blacklist/wellness sobre prosa visible para el cliente.
  * Tono profesional, wellness premium, humano, legalmente seguro.

API pública:
    humanize_text(text)
        Saneador genérico: aplica blacklist + reemplazos wellness +
        normalización ligera. Preserva tags HTML simples.
    render_analisis_narrative(analisis)
        Reemplaza _render_analisis_archivo_html legacy con prosa
        narrativa (sin labels "Hallazgos principales/secundarios/
        Restricciones detectadas").
    render_prioridades_narrative(prioridades)
        Reemplaza _render_prioridades_caso_html legacy con prosa
        wellness (sin labels "Evidencia/Relación/Importancia").

Las funciones legacy se preservan en valoracion_valoracion.py; el toggle
`valoracion.narrative_mode` (legacy|wellness, default legacy) decide
cuál se usa. Default legacy → producción no cambia hasta activar.

Compat: v4.1, v5.0, v5.1 — todas devuelven el mismo shape de keys.
"""
import re
import unicodedata


VERSION = '4.4.0'


# =====================================================================
# Vocabulario declarativo (auditable, testeable)
# =====================================================================

# Frases multi-palabra a vetar — se procesan ANTES que palabras sueltas
# para evitar falsos positivos (ej. "crónico severo" ≠ "crónico" aislado).
BLACKLIST_PHRASES = (
    # (regex_pattern, reemplazo_wellness)
    (r'\bcr[oó]nico\s+severo\b', 'persistente'),
    (r'\bdisfunci[oó]n\s+severa\b', 'desbalance funcional'),
    (r'\bsistema\s+comprometido\b', 'eje funcional con necesidad de soporte'),
    (r'\bsistema\s+comprometida\b', 'área funcional con necesidad de soporte'),
    (r'\benfermedad\s+degenerativa\b', 'proceso funcional que requiere acompañamiento'),
    (r'\benfermedad\s+cr[oó]nica\b', 'condición persistente'),
)

# Palabras sueltas a vetar — se procesan tras las frases, con word-boundary.
BLACKLIST_WORDS = (
    (r'\benfermedad(es)?\b',   'condición funcional'),
    (r'\bpatolog[ií]a(s)?\b',  'señal funcional'),
    (r'\bs[ií]ndrome(s)?\b',   'patrón funcional'),
    (r'\bdiagn[oó]stico(s)?\b', 'observación funcional'),
    (r'\bdiagn[oó]sticar\b',   'observar'),
    (r'\bdisfunci[oó]n(es)?\b', 'desbalance funcional'),
    (r'\bcomprometid[oa]s?\b',  'con necesidad de soporte'),
    (r'\binsuficiencia(s)?\b',  'soporte funcional disminuido'),
    (r'\bdegenerativ[oa]s?\b',  'progresivo'),
    (r'\bpadece(s|n)?\b',       'presenta'),
    (r'\bpadecimientos?\b',     'antecedente'),
    (r'\bsufre(s|n)?\s+de\b',   'presenta'),
    (r'\btrastorn[oa]s?\b',     'desbalance funcional'),
)

# Labels parser-style que aparecen en los renderers legacy.
# Se reemplazan por encabezados wellness o se eliminan según el contexto.
LABEL_REWRITES = (
    # (label_legacy, label_wellness)
    ('Hallazgos principales',          'Señales que destacan'),
    ('Hallazgos secundarios',          'Señales complementarias'),
    ('Señales funcionales',            'Lo que el cuerpo está expresando'),
    ('Restricciones detectadas',       'Aspectos a tener presentes'),
    ('Prioridades funcionales detectadas', 'Áreas que pediría acompañamiento'),
    ('Calidad del archivo',            ''),  # se elimina del cliente
    ('Análisis del archivo del cliente', 'Lo que cuenta tu estudio'),
    ('Evidencia (archivo)',            'Lo que se observa en el estudio'),
    ('Evidencia',                      'Lo que se observa'),
    ('Relación con antecedentes',      'Cómo se conecta con tu historia'),
    ('Importancia',                    'Por qué nos importa'),
)


# Vocabulario wellness preferido — referencia documental (no se inyecta,
# es la guía para los reemplazos arriba).
WELLNESS_VOCAB = (
    'equilibrio', 'acompañamiento', 'bienestar', 'soporte funcional',
    'recuperación', 'procesos naturales', 'vitalidad', 'armonía',
    'apoyo integral', 'eje funcional', 'ritmo natural del cuerpo',
)


# =====================================================================
# Fase 4.3 — reescrituras de sección + suavizado + flujo emocional
# =====================================================================

# Mapeo de SECCIÓN: nombres que la IA podría haber embebido directamente
# en HTML narrativo (resultados_valoracion, plan_estrategico, etc.) o
# que produce el render legacy. Cubre además acentos y variantes con
# dos puntos al final.
HUMAN_SECTION_MAP = (
    # Bloque "analisis_archivo_cliente"
    ('Hallazgos principales',          'Aspectos observados durante la valoración'),
    ('Hallazgos secundarios',          'Aspectos complementarios observados'),
    ('Señales funcionales',            'Áreas que podrían beneficiarse de apoyo'),
    ('Senales funcionales',            'Áreas que podrían beneficiarse de apoyo'),
    ('Restricciones detectadas',       'Aspectos a tener presentes en el acompañamiento'),
    ('Prioridades funcionales detectadas', 'Objetivos funcionales prioritarios'),
    ('Prioridades detectadas',         'Objetivos funcionales prioritarios'),
    ('Análisis del archivo del cliente', 'Lo que cuenta tu estudio'),
    ('Analisis del archivo del cliente', 'Lo que cuenta tu estudio'),
    ('Calidad del archivo',            ''),  # eliminado en renderers
    # Bloque "prioridades_caso"
    ('Evidencia (archivo)',            'Lo que se observa en el estudio'),
    ('Evidencia',                      'Lo que se observa'),
    ('Relación con antecedentes',      'Cómo se conecta con tu historia'),
    ('Relacion con antecedentes',      'Cómo se conecta con tu historia'),
    ('Importancia',                    'Por qué nos importa'),
    # Encabezados de auditoría que la IA no debería emitir, pero por
    # si filtra:
    ('archivo_fue_analizado',          ''),
    ('archivo NO analizado',           ''),
    ('Archivo NO analizado',           ''),
)


# Reglas de suavizado del lenguaje IA (muletillas robóticas → naturales).
# Se aplica DESPUÉS de la blacklist y los rewrites de label.
# Cada par (regex, reemplazo); regex case-insensitive con boundary.
SOFTENING_RULES = (
    (r'\ben funci[oó]n de los datos analizados\b', 'según lo observado'),
    (r'\bdatos analizados\b',          'lo observado'),
    (r'\bbasado en el an[aá]lisis\b',  'a partir de lo observado'),
    (r'\bcabe (destacar|mencionar|se[ñn]alar)\b', 'vale la pena destacar'),
    (r'\bes importante notar que\b',   'también observamos que'),
    (r'\bes importante destacar que\b', 'también vale la pena destacar que'),
    # OJO: el reemplazo NO incluye coma — el regex consume la coma del
    # original si está presente, así no se duplica.
    (r'\ben conclusi[oó]n,?\s*',       'en suma, '),
    (r'\bpor lo tanto,?\s*',           'por eso, '),
    (r'\bdicho (esto|lo anterior)\b',  'con eso en mente'),
    (r'\bse (recomienda|sugiere) (encarecidamente )?\b', 'te invitamos a '),
    (r'\bal paciente\b',               'a ti'),
    (r'\bel paciente\b',               'el cliente'),
    (r'\bla paciente\b',               'la cliente'),
    (r'\bcaso cl[ií]nico\b',           'acompañamiento personalizado'),
    (r'\bcuadro cl[ií]nico\b',         'perfil funcional'),
    # Conectores típicos IA
    (r'\bcomo se mencion[oó] anteriormente\b', 'como vimos'),
    (r'\bsuper[ií]or al rango normal\b',   'por encima del rango funcional'),
    (r'\binferior al rango normal\b',      'por debajo del rango funcional'),
    (r'\bfuera del rango normal\b',        'fuera del rango funcional'),
)


# Reglas de flujo emocional: normalizan el HTML/texto para que el PDF
# se sienta más fluido y menos "QWeb administrativo".
# Aplica al final del pipeline (después de humanize).
EMOTIONAL_FLOW_RULES = (
    # Triple+ salto de línea → doble (uniforme)
    (r'\n{3,}', '\n\n'),
    # Espacios múltiples NO dentro de tags → un solo espacio
    (r' {2,}', ' '),
    # "  ," / "  ." → ", " / ". "
    (r'\s+([,.;:])', r'\1'),
    # Dos puntos seguidos de salto y bullet → mantener prosa
    (r':\s*<br/?>\s*', ': '),
)


# Tags HTML permitidos para preservar (resto se conserva textualmente
# también — el renderer NO sanitiza HTML, sólo reemplaza tokens).
_TAG_PATTERN = re.compile(r'<[^>]+>')


# =====================================================================
# Helpers internos
# =====================================================================

def _apply_replacements(text, pairs):
    """Aplica una lista de (regex, replacement) sobre `text`, en orden,
    case-insensitive, preservando la primera letra mayúscula si la
    coincidencia original empezaba en mayúscula."""
    if not text:
        return text
    out = text
    for pattern, replacement in pairs:
        rgx = re.compile(pattern, re.IGNORECASE)

        def _repl(m, _rep=replacement):
            orig = m.group(0)
            if orig and orig[0].isupper():
                return _rep[:1].upper() + _rep[1:] if _rep else _rep
            return _rep

        out = rgx.sub(_repl, out)
    return out


def _strip_outer_whitespace(text):
    if text is None:
        return ''
    return re.sub(r'\s{2,}', ' ', text).strip()


def _html_escape(text):
    """Escape mínimo seguro para inyectar texto plano dentro de HTML."""
    if text is None:
        return ''
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


# =====================================================================
# API pública
# =====================================================================

def humanize_text(text):
    """Saneador wellness genérico (Fase 4.2 + ampliado 4.3).

    Aplica, en orden:
      1) BLACKLIST_PHRASES (multi-palabra primero).
      2) BLACKLIST_WORDS (palabras sueltas con boundary).
      3) LABEL_REWRITES (labels técnicos visibles → wellness, Fase 4.2).
      4) HUMAN_SECTION_MAP (secciones IA → wellness, Fase 4.3).
      5) SOFTENING_RULES (muletillas IA → naturales, Fase 4.3).
      6) EMOTIONAL_FLOW_RULES (normalización de whitespace, Fase 4.3).

    Idempotente: humanize_text(humanize_text(x)) == humanize_text(x).
    Determinístico: mismo input → mismo output.
    Preserva tags HTML simples (no parsea DOM; sólo reemplaza tokens).
    """
    if not text:
        return text or ''
    out = text
    out = _apply_replacements(out, BLACKLIST_PHRASES)
    out = _apply_replacements(out, BLACKLIST_WORDS)
    # LABEL_REWRITES y HUMAN_SECTION_MAP son strings literales.
    for legacy, wellness in LABEL_REWRITES:
        if not wellness:
            continue
        out = out.replace(legacy, wellness)
    for legacy, wellness in HUMAN_SECTION_MAP:
        if not wellness:
            # Eliminamos también el ':' colgante si existía
            out = out.replace(legacy + ':', '')
            out = out.replace(legacy, '')
            continue
        out = out.replace(legacy, wellness)
    # SOFTENING_RULES son regex
    out = _apply_replacements(out, SOFTENING_RULES)
    # EMOTIONAL_FLOW_RULES son regex; reemplazo posicional sin case
    for pattern, replacement in EMOTIONAL_FLOW_RULES:
        out = re.sub(pattern, replacement, out)
    return out


def humanize_narrative_field(html):
    """Versión específica para campos HTML del cliente
    (resultados_valoracion, plan_estrategico, resumen_estrategico,
    resultados_esperados). Es equivalente a `humanize_text` pero la API
    explícita ayuda a auditar las llamadas en `valoracion_valoracion.py`.

    NO debe usarse sobre:
      * advertencias (requisito legal: menciones literales).
      * productos / dosis / cantidades (datos comerciales/numéricos).
    """
    return humanize_text(html or '')


def humanize_html(html):
    """Como humanize_text, pero pensado para HTML — preserva tags y
    sólo transforma los nodos de texto. Implementación simple: procesa
    el string completo, las regex no matchean dentro de tags porque los
    tokens (palabras castellanas) no aparecen como nombres de tag."""
    return humanize_text(html or '')


def render_analisis_narrative(analisis):
    """Reescribe el bloque `analisis_archivo_cliente` como prosa wellness.

    Acepta el mismo dict que `_render_analisis_archivo_html` legacy:
      {archivo_fue_analizado, calidad_del_archivo, hallazgos_principales,
       hallazgos_secundarios, senales_funcionales, restricciones_detectadas,
       prioridades_detectadas}

    Devuelve HTML simple con prosa cálida en lugar de bullets con labels.

    Reglas:
      * Si archivo_fue_analizado=False: mensaje cálido pidiendo estudio.
      * Si hay hallazgos: párrafo conector + bullets sin labels visibles
        (más fáciles de leer en PDF que un párrafo larguísimo).
      * Calidad del archivo: se omite (auditoría interna).
      * NO inventa contenido; sólo reformula labels y aplica blacklist.
    """
    if not isinstance(analisis, dict):
        return ''

    parts = []
    # Fase 4.4.3: el header de sección lo pone el contenedor (template
    # legacy/premium). El renderer NO emite su propio <h4> para evitar
    # duplicación visual ("Lo que cuenta tu estudio" no debe aparecer
    # dos veces seguidas).

    analizado = analisis.get('archivo_fue_analizado', True)
    if analizado is False:
        parts.append(
            '<p style="color:#714B67;">'
            'Para acompañarte con la mayor precisión posible, '
            'necesitamos que cargues un estudio o reporte legible. '
            'En cuanto tengamos esa información volveremos contigo '
            'con una valoración funcional completa.'
            '</p>'
        )
        return ''.join(parts)

    # Conector narrativo breve cuando hay hallazgos
    hp = [h for h in (analisis.get('hallazgos_principales') or []) if h]
    hs = [h for h in (analisis.get('hallazgos_secundarios') or []) if h]
    sf = [h for h in (analisis.get('senales_funcionales') or []) if h]
    rd = [h for h in (analisis.get('restricciones_detectadas') or []) if h]
    pd_ = [h for h in (analisis.get('prioridades_detectadas') or []) if h]

    if hp or hs or sf:
        parts.append(
            '<p style="margin-top:0;">El estudio deja ver algunas '
            'señales que vale la pena acompañar:</p>'
        )

    def _bullets(items, intro=None):
        if not items:
            return
        if intro:
            parts.append('<p style="margin:10px 0 2px 0;color:#5C5752;'
                         'font-style:italic;">%s</p>'
                         % _html_escape(intro))
        parts.append('<ul style="margin-top:0;margin-bottom:6px;">')
        for it in items:
            parts.append('<li>%s</li>'
                         % humanize_text(_html_escape(str(it))))
        parts.append('</ul>')

    # Intros más cálidas y variadas (menos plantilla)
    _bullets(hp, 'Lo más destacado')
    _bullets(hs, 'También llaman la atención')
    _bullets(sf, 'Cómo se está sintiendo el cuerpo')
    if rd:
        _bullets(rd, 'A tener presente en el acompañamiento')
    if pd_:
        _bullets(pd_, 'Áreas que pediría apoyo integral')

    return ''.join(parts)


def render_prioridades_narrative(prioridades):
    """Reescribe `prioridades_caso` como prosa wellness sin labels
    parser-style.

    Acepta:
      * list[dict] (v3+): se renderiza como <ol> de párrafos cálidos.
      * str (formato anterior): se devuelve tras humanize_text.

    Cada prioridad se cuenta como un mini-párrafo:
        "1. <titulo>. <evidencia> · <relacion> · <importancia>"
    donde cada subsección entra como prosa fluida, no como label/value.
    """
    if isinstance(prioridades, str):
        return humanize_text(prioridades)
    if not isinstance(prioridades, list) or not prioridades:
        return ''

    parts = ['<ol style="padding-left:18px;">']
    for entry in prioridades:
        if not isinstance(entry, dict):
            parts.append('<li>%s</li>' % humanize_text(_html_escape(
                str(entry))))
            continue
        titulo = humanize_text(_html_escape(
            entry.get('titulo') or '(sin título)'))
        evidencia = humanize_text(_html_escape(
            entry.get('evidencia_archivo') or ''))
        relacion = humanize_text(_html_escape(
            entry.get('relacion_antecedente') or ''))
        importancia = humanize_text(_html_escape(
            entry.get('importancia') or ''))

        parts.append('<li style="margin-bottom:12px;">')
        parts.append('<b style="color:#2C2A29;">%s</b>' % titulo)

        # Fase 4.4.3: prosa más natural y menos plantilla.
        # Evitamos conectores rígidos repetitivos ("en tu estudio se
        # observa que…", "se conecta con tu historia porque…").
        # Cada oración se imprime tal cual (capitalizada y terminada en
        # punto), separadas por ' · ' que el ojo lee como continuidad.
        oraciones = []

        def _clean(frase):
            """Capitaliza y limpia un fragmento del payload IA."""
            if not frase:
                return ''
            txt = frase.strip().rstrip('.')
            if not txt:
                return ''
            return txt[:1].upper() + txt[1:] + '.'

        if evidencia and evidencia.strip() not in (
            '[Sin archivo aprovechable]',
        ):
            oraciones.append(_clean(evidencia))
        if relacion and relacion.lower().strip() not in (
            'sin relación directa con antecedente declarado',
        ):
            oraciones.append(_clean(relacion))
        if importancia:
            oraciones.append(_clean(importancia))

        oraciones = [o for o in oraciones if o]
        if oraciones:
            parts.append('<br/><span style="color:#5C5752;'
                         'line-height:1.55;">')
            parts.append(' · '.join(oraciones))
            parts.append('</span>')

        parts.append('</li>')

    parts.append('</ol>')
    return ''.join(parts)
