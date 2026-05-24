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


VERSION = '4.2.0'


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
    """Saneador wellness genérico.

    Aplica, en orden:
      1) BLACKLIST_PHRASES (multi-palabra primero).
      2) BLACKLIST_WORDS (palabras sueltas con boundary).
      3) LABEL_REWRITES (labels técnicos visibles → wellness).

    Idempotente: humanize_text(humanize_text(x)) == humanize_text(x).
    Determinístico: mismo input → mismo output.
    Preserva tags HTML simples (no se tocan, sólo el texto entre tags).
    """
    if not text:
        return text or ''
    out = text
    out = _apply_replacements(out, BLACKLIST_PHRASES)
    out = _apply_replacements(out, BLACKLIST_WORDS)
    # LABEL_REWRITES son strings literales (no regex). Conservamos case.
    for legacy, wellness in LABEL_REWRITES:
        if not wellness:
            continue  # eliminación se maneja en renderers, no aquí
        out = out.replace(legacy, wellness)
    return out


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
    parts.append(
        '<h4 style="color:#714B67;margin-bottom:6px;">'
        'Lo que cuenta tu estudio</h4>'
    )

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
            '<p>De la lectura de tu estudio se observan algunas señales '
            'que vale la pena acompañar:</p>'
        )

    def _bullets(items, intro=None):
        if not items:
            return
        if intro:
            parts.append('<p style="margin-bottom:2px;">%s</p>'
                         % _html_escape(intro))
        parts.append('<ul style="margin-top:0;">')
        for it in items:
            parts.append('<li>%s</li>'
                         % humanize_text(_html_escape(str(it))))
        parts.append('</ul>')

    # Sin labels técnicos: cada bloque va con su intro narrativa
    _bullets(hp, 'Lo más relevante:')
    _bullets(hs, 'Y como complemento, también se observa:')
    _bullets(sf, 'En cómo se siente el cuerpo, se nota:')
    if rd:
        _bullets(rd, 'Aspectos a tener presentes para tu acompañamiento:')
    if pd_:
        _bullets(pd_, 'Áreas funcionales que pediría apoyo integral:')

    parts.append(
        '<hr style="border:none;border-top:1px dashed #ccc;margin:8px 0;"/>'
    )
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

        parts.append('<li style="margin-bottom:10px;">')
        parts.append('<b>%s</b>' % titulo)

        # Hilamos los tres subcampos en prosa fluida con conectores
        # naturales — sin "Evidencia:", "Relación con antecedentes:",
        # "Importancia:".
        oraciones = []
        if evidencia and evidencia.strip() not in ('[Sin archivo aprovechable]',):
            oraciones.append('en tu estudio se observa que ' + evidencia[:1].lower() + evidencia[1:])
        if relacion and relacion.lower().strip() not in (
            'sin relación directa con antecedente declarado',
        ):
            oraciones.append('se conecta con tu historia porque ' + relacion[:1].lower() + relacion[1:])
        if importancia:
            oraciones.append(importancia[:1].lower() + importancia[1:])

        if oraciones:
            parts.append('<br/><span style="color:#555;">')
            parts.append('. '.join(o.rstrip('.') for o in oraciones) + '.')
            parts.append('</span>')

        parts.append('</li>')

    parts.append('</ol>')
    return ''.join(parts)
