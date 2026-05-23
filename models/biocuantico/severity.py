# -*- coding: utf-8 -*-
"""Motor BioCuántico v3 — Severity puro (Fase 3.3).

Asigna severidad (level + numeric) a ClassifiedRows. Funciones puras —
no toca Odoo, no toca BD, no llama IA.

REGLA arquitectónica:
    El PDF clasifica → classifier.py (Fase 3.2)
    El PDF tiene severidad → severity.py (Fase 3.3, este módulo)
    El antecedente prioriza → ranker.py (Fase 3.4, pendiente)
    La IA interpreta → prompt + providers (no se toca en refactor)

NO se hace aquí:
    - Boost por antecedente del cliente — eso es ranker.
    - Dedup — eso ya pasó en classifier.
    - Ranking funcional / top-N prioridades — eso es ranker.
    - Render / output — eso es Fase 3.5.

API principal:
    assign_severity_to_rows(classified_rows, estados_lookup=None)
        → cada ClassifiedRow recibe campos extra:
            'severity_level': 'normal' | 'leve' | 'moderado' | 'severo'
                              | 'critico' | 'optimo' | 'desconocida'
            'severity_score': int 0-5 (correlativo del level)
            'severity_source': 'estado_explicito' | 'inferencia_numerica'
                               | 'desconocida'
            'severity_breakdown': dict de auditoría
"""
import logging
import re
import unicodedata

from .taxonomy import normalize_text


_logger = logging.getLogger(__name__)


# =====================================================================
# Escala canónica de severidad
# =====================================================================
# level → score numérico (0-5, 9 para desconocida)
SEVERITY_SCORE = {
    'optimo': 0,
    'normal': 1,
    'leve': 2,
    'moderado': 3,
    'severo': 4,
    'critico': 5,
    'desconocida': 9,
}

# Inversa para resolución desde score
LEVEL_FROM_SCORE = {v: k for k, v in SEVERITY_SCORE.items()}


# =====================================================================
# Lookup de estados explícitos (default)
# =====================================================================
# Mapa de label normalizado → (level, score). El llamador puede pasar
# su propio lookup si tiene customizaciones (ej. desde Odoo).
DEFAULT_ESTADOS_LOOKUP = {
    'optimo': ('optimo', 0),
    'excelente': ('optimo', 0),
    'normal': ('normal', 1),
    'equilibrado': ('normal', 1),
    'sano': ('normal', 1),
    'dentro de rango': ('normal', 1),
    'leve': ('leve', 2),
    'levemente alterado': ('leve', 2),
    'ligero': ('leve', 2),
    'moderado': ('moderado', 3),
    'moderadamente alterado': ('moderado', 3),
    'severo': ('severo', 4),
    'severamente alterado': ('severo', 4),
    'alterado': ('severo', 4),
    'critico': ('critico', 5),
    'grave': ('critico', 5),
}


# =====================================================================
# Umbrales de inferencia numérica
# =====================================================================
# Cuando se infiere severidad a partir de valor vs rango, se calcula el
# porcentaje fuera del bound violado y se compara con estos umbrales.
# Diseño suavizado: pequeñas desviaciones NO inflan a severo.
THRESHOLD_LEVE = 0.10      # ≤ 10% → leve
THRESHOLD_MODERADO = 0.30  # ≤ 30% → moderado
# > 30% → severo

# Si el rango es BILATERAL [lo, hi], además del % fuera del bound, se
# considera la desviación relativa al ANCHO del rango. Esto suaviza
# rangos estrechos: si la diferencia es pequeña respecto al ancho del
# rango, no se infla a severo.
# Se usa el MENOR entre pct_bound y pct_central (centro del rango).
USE_SOFTENING_FOR_BILATERAL = True


# =====================================================================
# Parsers numéricos (tolerantes a coma decimal, miles, símbolos)
# =====================================================================

# Detecta operadores (<, >, ≤, ≥) seguidos de un número.
# También variantes ASCII: <=, >=, =<, =>.
_RE_BOUND_LT = re.compile(r'^\s*(?:<=|≤|=<)\s*(-?\d[\d.,]*)\s*$')
_RE_BOUND_LT2 = re.compile(r'^\s*<\s*(-?\d[\d.,]*)\s*$')
_RE_BOUND_GT = re.compile(r'^\s*(?:>=|≥|=>)\s*(-?\d[\d.,]*)\s*$')
_RE_BOUND_GT2 = re.compile(r'^\s*>\s*(-?\d[\d.,]*)\s*$')
# Rango bilateral: número - número (con guión, em-dash, "a", "to")
_RE_BOUND_RANGE = re.compile(
    r'(-?\d[\d.,]*)\s*(?:-|–|—|\s+a\s+|\s+to\s+)\s*(-?\d[\d.,]*)'
)


def parse_float(text):
    """Convierte un string a float. Tolera:
        * coma decimal estilo MX/ES: '1,425' → 1.425
        * punto decimal estilo US:  '1.425' → 1.425
        * separador de miles ambiguo: '1,329.50' → 1329.50
        * signos: '-3.14' → -3.14
        * unidades adheridas: '120 mg/dL' → 120
        * presión arterial: '150/95' → 150 (primer número)
        * texto: 'positivo' → None

    Devuelve float o None.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # Buscar el primer trozo numérico (signo + dígitos + . , )
    m = re.search(r'-?\d[\d.,]*', s)
    if not m:
        return None
    num = m.group(0).strip().strip('.,')
    if not num:
        return None
    has_dot = '.' in num
    has_comma = ',' in num
    if has_dot and has_comma:
        # Coma = miles, punto = decimal (estilo US)
        num = num.replace(',', '')
    elif has_comma and not has_dot:
        # Coma decimal estilo MX/ES
        num = num.replace(',', '.')
    try:
        return float(num)
    except (ValueError, TypeError):
        return None


def parse_range(text):
    """Parsea un texto de rango y devuelve (lo, hi).
        lo o hi puede ser None si el rango es de un solo lado.
        Devuelve None si no se puede interpretar.

    Soporta:
        '70-110', '70 - 110', '0,5 - 4,5', '0.5 a 4.5'
        '<150', '<= 150', '≤ 150'
        '>30', '>= 30', '≥ 30'
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # Normalizar (≤/≥ Unicode quedan claros, no necesitan normalize_text)
    # Probar acotado superior
    for pat in (_RE_BOUND_LT, _RE_BOUND_LT2):
        m = pat.match(s)
        if m:
            hi = parse_float(m.group(1))
            return (None, hi) if hi is not None else None
    # Acotado inferior
    for pat in (_RE_BOUND_GT, _RE_BOUND_GT2):
        m = pat.match(s)
        if m:
            lo = parse_float(m.group(1))
            return (lo, None) if lo is not None else None
    # Rango bilateral
    m = _RE_BOUND_RANGE.search(s)
    if m:
        lo = parse_float(m.group(1))
        hi = parse_float(m.group(2))
        if lo is not None and hi is not None:
            if lo > hi:
                lo, hi = hi, lo
            return (lo, hi)
    return None


# =====================================================================
# Inferencia de severidad por valor vs rango
# =====================================================================

def infer_severity_from_value_range(valor_text, rango_text):
    """Compara un valor numérico contra un rango y devuelve
    (level, score, breakdown) o (None, None, breakdown) si no se puede.

    breakdown: dict con detalles para auditoría:
        {
            'valor_float': 1.425,
            'rango_lo': 0.431,
            'rango_hi': 1.329,
            'in_range': False,
            'violated_bound': 1.329,
            'pct_bound': 0.072,
            'pct_central': 0.108,
            'effective_pct': 0.072,
            'thresholds': {leve: 0.10, moderado: 0.30},
        }

    Reglas:
      * Si valor o rango no parsean → (None, None, {motivo: ...}).
      * Si valor está dentro del rango → ('normal', 1).
      * Si valor está fuera:
            effective_pct = min(pct_bound, pct_central) cuando hay 2 bounds
                            (suavizado contra rangos estrechos).
            effective_pct = pct_bound cuando hay un solo bound.
        Mapeo a severidad:
            ≤ 0.10 → leve
            ≤ 0.30 → moderado
            > 0.30 → severo
    """
    val = parse_float(valor_text)
    if val is None:
        return None, None, {'motivo': 'valor_no_parseable',
                            'valor_text': valor_text}
    bounds = parse_range(rango_text)
    if bounds is None:
        return None, None, {'motivo': 'rango_no_parseable',
                            'rango_text': rango_text, 'valor_float': val}
    lo, hi = bounds
    if lo is None and hi is None:
        return None, None, {'motivo': 'rango_sin_bounds',
                            'valor_float': val}
    breakdown = {
        'valor_float': val,
        'rango_lo': lo,
        'rango_hi': hi,
    }
    # Determinar el bound violado
    violated = None
    if lo is not None and val < lo:
        violated = lo
    elif hi is not None and val > hi:
        violated = hi
    if violated is None:
        # Dentro del rango
        breakdown.update({'in_range': True, 'effective_pct': 0.0})
        return 'normal', SEVERITY_SCORE['normal'], breakdown

    abs_dev = abs(val - violated)
    ref_bound = abs(violated) if violated != 0 else (
        abs(val) if val != 0 else 1.0
    )
    pct_bound = abs_dev / ref_bound

    # Suavizado bilateral
    effective_pct = pct_bound
    pct_central = None
    if USE_SOFTENING_FOR_BILATERAL and lo is not None and hi is not None:
        center = (lo + hi) / 2.0
        ref_center = abs(center) if center != 0 else ref_bound
        if ref_center > 0:
            pct_central = abs_dev / ref_center
            effective_pct = min(pct_bound, pct_central)

    breakdown.update({
        'in_range': False,
        'violated_bound': violated,
        'abs_dev': abs_dev,
        'pct_bound': pct_bound,
        'pct_central': pct_central,
        'effective_pct': effective_pct,
        'thresholds': {
            'leve': THRESHOLD_LEVE,
            'moderado': THRESHOLD_MODERADO,
        },
    })

    if effective_pct <= THRESHOLD_LEVE:
        return 'leve', SEVERITY_SCORE['leve'], breakdown
    if effective_pct <= THRESHOLD_MODERADO:
        return 'moderado', SEVERITY_SCORE['moderado'], breakdown
    return 'severo', SEVERITY_SCORE['severo'], breakdown


# =====================================================================
# Estado explícito lookup
# =====================================================================

def severity_from_estado(estado_text, lookup=None):
    """Resuelve un estado textual (ej. 'Severo', 'Levemente alterado')
    contra el lookup de estados. Devuelve (level, score) o (None, None).
    """
    if not estado_text:
        return None, None
    norm = normalize_text(estado_text)
    if not norm:
        return None, None
    if lookup is None:
        lookup = DEFAULT_ESTADOS_LOOKUP
    # Match exacto
    if norm in lookup:
        level, score = lookup[norm]
        return level, score
    # Match parcial (substring): permite "alterado" dentro de
    # "moderadamente alterado", pero priorizando matches más específicos.
    best = None
    best_len = 0
    for key, (level, score) in lookup.items():
        if key in norm and len(key) > best_len:
            best = (level, score)
            best_len = len(key)
    if best:
        return best
    return None, None


# =====================================================================
# API principal
# =====================================================================

def assign_severity_row(row, estados_lookup=None):
    """Asigna severidad a una ClassifiedRow. NO muta — devuelve copia.

    Lógica:
      1. Si row['estado_norm'] existe y es resolvible → severity_source
         = 'estado_explicito'.
      2. Si no, intentar inferencia numérica con val/rango. Si OK →
         severity_source = 'inferencia_numerica'.
      3. Si nada funciona → severity_level = 'desconocida'.

    Devuelve copia de la row con campos extra:
        severity_level, severity_score, severity_source, severity_breakdown.
    """
    out = dict(row)

    # 1. Estado explícito (tomar de estado_raw que llega con texto crudo)
    estado_raw = row.get('estado_raw')
    if estado_raw:
        level, score = severity_from_estado(estado_raw, lookup=estados_lookup)
        if level:
            out['severity_level'] = level
            out['severity_score'] = score
            out['severity_source'] = 'estado_explicito'
            out['severity_breakdown'] = {
                'estado_text': estado_raw,
                'matched_level': level,
            }
            return out

    # 2. Inferencia numérica
    valor_text = row.get('valor_raw') or ''
    rango_text = row.get('rango_raw') or ''
    if valor_text and rango_text:
        level, score, breakdown = infer_severity_from_value_range(
            valor_text, rango_text,
        )
        if level:
            out['severity_level'] = level
            out['severity_score'] = score
            out['severity_source'] = 'inferencia_numerica'
            out['severity_breakdown'] = breakdown
            return out

    # 3. Desconocida — datos insuficientes
    out['severity_level'] = 'desconocida'
    out['severity_score'] = SEVERITY_SCORE['desconocida']
    out['severity_source'] = 'desconocida'
    out['severity_breakdown'] = {
        'motivo': 'datos_insuficientes',
        'tenia_estado': bool(estado_raw),
        'tenia_valor': bool(valor_text),
        'tenia_rango': bool(rango_text),
    }
    return out


def assign_severity(classified_rows, estados_lookup=None):
    """API principal: asigna severidad a una lista de ClassifiedRows.

    Args:
        classified_rows: lista de ClassifiedRow (output de classifier.classify).
        estados_lookup: dict opcional {label_norm: (level, score)} para
            overrides de estado. Si None, usa DEFAULT_ESTADOS_LOOKUP.

    Returns:
        list — cada ClassifiedRow con campos extra de severidad.
    """
    return [
        assign_severity_row(row, estados_lookup=estados_lookup)
        for row in classified_rows or []
    ]


# =====================================================================
# Helpers de introspección
# =====================================================================

def severity_stats(rows_with_severity):
    """Devuelve conteo por severity_level."""
    from collections import Counter
    return dict(Counter(r.get('severity_level') for r in rows_with_severity))


def all_levels():
    """Devuelve los levels canónicos en orden de severidad ascendente."""
    return ('optimo', 'normal', 'leve', 'moderado', 'severo', 'critico',
            'desconocida')
