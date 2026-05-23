# -*- coding: utf-8 -*-
"""Motor BioCuántico v3 — Classifier puro (Fase 3.2).

Consume RawRows del extractor (Fase 3.1) y los clasifica contra el
catálogo declarativo de taxonomy.py. Salida: ClassifiedRows con
sistema_code asignado + audit declarativo.

PRINCIPIOS:
  * Función pura. No toca Odoo, no toca BD, no llama IA.
  * El PDF clasifica. El antecedente NO interviene aquí — eso será
    trabajo del ranker (Fase 3.4). Esta capa es estrictamente sobre el
    contenido del documento.
  * Auditoría declarativa: cada decisión queda con su score_breakdown
    para que el admin pueda entender por qué cada hallazgo cayó donde
    cayó.
  * No conecta al flujo productivo. parser.py / master_summary.py
    siguen intactos.

CAPAS implementadas:
  1. Noise filter — descarta parámetros que no son hallazgos clínicos
     (figuras, ilustraciones, encabezados, ejemplos).
  2. Scoring real — para cada parámetro, calcula score por sistema
     sumando matches en su nombre normalizado + (opcionalmente)
     contexto agregado de la tabla.
  3. Strong parameter threshold — si el parámetro tiene match propio
     ≥ STRONG_PARAM_THRESHOLD, el contexto NO se acumula. Evita que
     una tabla con tema dominante "absorba" parámetros con kw propia.
  4. Dedup cross-sistema — el mismo parámetro normalizado sólo puede
     pertenecer a UN sistema. Resolución: source_priority > kw_length
     > severity. Las versiones perdedoras van a parametros_descartados.
  5. Low-relevance downgrade — parámetros estéticos (arrugas, manchas)
     bajan un nivel de severidad.

NO se hace aquí:
  * Severity assignment / numeric inference → Fase 3.3 (severity.py).
  * Ranking funcional → Fase 3.4 (ranker.py).
  * Boost por antecedente → Fase 3.4.
  * Render text/JSON → Fase 3.5 (master_summary_v3.py).
"""
import logging

from .taxonomy import (
    SYSTEMS, NOISE_PATTERNS, LOW_RELEVANCE_PATTERNS,
    Pattern, System,
    normalize_text, get_system,
)


_logger = logging.getLogger(__name__)


# =====================================================================
# Constantes de scoring
# =====================================================================

# Umbral de "strong param": si max(param_score por sistema) >= este valor,
# el classifier IGNORA el contexto agregado de la tabla y decide sólo
# con el match propio del parámetro. Esto evita que una tabla con tema
# dominante (ej. sección musculoesquelética) absorba un parámetro
# inmunológico (ej. "IgG") que tiene su propia kw fuerte.
STRONG_PARAM_THRESHOLD = 7

# Si el parámetro NO alcanza STRONG_PARAM_THRESHOLD, se acumula score
# del contexto agregado. El peso del contexto es score_kw // 3 (3x menos
# que un match en el parámetro mismo).
CONTEXT_DIVISOR = 3

# Score mínimo absoluto para asignar un sistema. Si ni siquiera con
# contexto se alcanza, el parámetro va a 'otros' (catch-all).
MIN_SCORE_TO_ASSIGN = 5


# =====================================================================
# Resultado de clasificación por fila
# =====================================================================

CLASSIFIED_ROW_KEYS = (
    # Heredados del extractor (RawRow)
    'parametro_raw', 'parametro_norm',
    'valor_raw', 'valor_norm',
    'rango_raw', 'rango_norm',
    'estado_raw', 'estado_norm',
    'pagina', 'tabla_idx', 'row_idx',
    'section_header', 'table_context_norm',
    # Nuevos del classifier
    'sistema_code',        # string code del sistema asignado (o 'otros')
    'sistema_label',       # label legible
    'sistema_source',      # 'keyword' | 'keyword_strong' | 'context' |
                           # 'catch_all' | 'noise' | 'discarded_dedup'
    'sistema_score',       # int — score total final del sistema ganador
    'score_breakdown',     # list de tuplas (kind, needle, score) para auditoría
    'is_noise',            # bool — True si matchea NOISE_PATTERNS
    'is_low_relevance',    # bool — True si matchea LOW_RELEVANCE_PATTERNS
    'motivo_descarte',     # str opcional cuando el row va a descartados
)


def _make_classified_row(row, sistema_code=None, sistema_label=None,
                         sistema_source='catch_all', sistema_score=0,
                         score_breakdown=None, is_noise=False,
                         is_low_relevance=False, motivo_descarte=None):
    """Construye ClassifiedRow a partir de un RawRow.
    Conserva todos los campos del RawRow y añade los del classifier.
    """
    out = dict(row)   # copia shallow del RawRow
    out.update({
        'sistema_code': sistema_code or 'otros',
        'sistema_label': sistema_label or 'Otros / sin clasificar',
        'sistema_source': sistema_source,
        'sistema_score': int(sistema_score or 0),
        'score_breakdown': list(score_breakdown or []),
        'is_noise': bool(is_noise),
        'is_low_relevance': bool(is_low_relevance),
        'motivo_descarte': motivo_descarte,
    })
    return out


# =====================================================================
# Detectores de noise y low-relevance
# =====================================================================

def _is_noise_param(parametro_norm):
    """True si el parámetro (ya normalizado) matchea cualquier
    NOISE_PATTERN. Usa la misma lógica de matches() del Pattern.

    Conservador: el parámetro debe MATCHEAR el pattern de forma
    significativa — si la kw está al inicio o como palabra dominante.
    """
    if not parametro_norm:
        return False
    for pat in NOISE_PATTERNS:
        if pat.matches(parametro_norm):
            # Para evitar falsos positivos en parámetros legítimos largos,
            # si el patrón es substring genérico, exigimos que el parámetro
            # sea relativamente corto o que el match esté al inicio.
            if pat.match_mode == 'substring':
                needle = str(pat.needle)
                # Match seguro si el parámetro empieza por la kw o es <25 chars
                if (parametro_norm.startswith(needle) or
                    parametro_norm == needle or
                    parametro_norm.startswith(needle + ' ') or
                    parametro_norm.startswith(needle + ':') or
                    len(parametro_norm) < 25):
                    return True
            else:
                return True
    return False


def _is_low_relevance_param(parametro_norm):
    """True si el parámetro matchea cualquier LOW_RELEVANCE_PATTERN.
    Estos no se descartan — siguen su sistema natural pero bajan un
    nivel de severidad en el ranker.
    """
    if not parametro_norm:
        return False
    for pat in LOW_RELEVANCE_PATTERNS:
        if pat.matches(parametro_norm):
            return True
    return False


# =====================================================================
# Scoring por sistema
# =====================================================================

def _score_param_against_system(parametro_norm, system):
    """Suma los matches de `parametro_norm` contra los patterns de
    `system`. Resta los anti_patterns. Devuelve (score, breakdown).

    breakdown: lista de tuplas (kind, needle, score_contributed):
        ('param', 'tsh', 8)
        ('anti', 'pulmonar', -3)
    """
    score = 0
    breakdown = []
    if not parametro_norm:
        return 0, []
    for p in system.patterns:
        if p.matches(parametro_norm):
            score += p.score
            breakdown.append(('param', str(p.needle), p.score))
    for ap in system.anti_patterns:
        if ap.matches(parametro_norm):
            score -= ap.score
            breakdown.append(('anti', str(ap.needle), -ap.score))
    return score, breakdown


def _score_context_against_system(table_context_norm, system):
    """Score basado en contexto agregado de la tabla. Sólo cuenta para
    patterns de tipo 'substring' (los whole-word/exact son demasiado
    específicos para contexto).
    """
    score = 0
    breakdown = []
    if not table_context_norm:
        return 0, []
    for p in system.patterns:
        if p.match_mode != 'substring':
            continue
        if p.matches(table_context_norm):
            weight = max(1, p.score // CONTEXT_DIVISOR)
            score += weight
            breakdown.append(('ctx', str(p.needle), weight))
    return score, breakdown


# =====================================================================
# API principal: classify_row (una fila a la vez)
# =====================================================================

def classify_row(row):
    """Clasifica una RawRow.

    Reglas:
      1. Si el parámetro matchea NOISE_PATTERNS → noise (catch-all
         con sistema_source='noise').
      2. Calcular score por sistema usando SOLO el parámetro.
      3. Si max(param_scores) >= STRONG_PARAM_THRESHOLD → decide sólo
         con param. source='keyword_strong'.
      4. Si no, sumar contexto y decidir con la suma. source='keyword'
         (cuando hay match en param) o 'context' (cuando sólo contexto).
      5. Si total < MIN_SCORE_TO_ASSIGN → 'otros' (catch-all).

    Args:
        row: RawRow (dict con shape de extractor.make_raw_row).

    Returns:
        ClassifiedRow (dict).
    """
    parametro_norm = row.get('parametro_norm', '') or ''
    table_ctx = row.get('table_context_norm', '') or ''

    # === Paso 1: Noise filter ===
    if _is_noise_param(parametro_norm):
        return _make_classified_row(
            row,
            sistema_code='otros',
            sistema_label='Otros / sin clasificar',
            sistema_source='noise',
            is_noise=True,
            motivo_descarte='ruido_no_clinico',
        )

    is_lr = _is_low_relevance_param(parametro_norm)

    # === Paso 2: score por sistema usando SOLO parámetro ===
    param_scores = {}      # code → (score, breakdown)
    for sys in SYSTEMS:
        if sys.catch_all:
            continue
        sc, bd = _score_param_against_system(parametro_norm, sys)
        param_scores[sys.code] = (sc, bd)

    max_param_score = max((sc for sc, _ in param_scores.values()), default=0)

    # === Paso 3: Strong threshold — si max_param fuerte, decidir aquí ===
    if max_param_score >= STRONG_PARAM_THRESHOLD:
        winner_code = max(param_scores, key=lambda c: param_scores[c][0])
        winner_score, winner_bd = param_scores[winner_code]
        winner_sys = get_system(winner_code)
        return _make_classified_row(
            row,
            sistema_code=winner_code,
            sistema_label=winner_sys.label if winner_sys else winner_code,
            sistema_source='keyword_strong',
            sistema_score=winner_score,
            score_breakdown=winner_bd,
            is_low_relevance=is_lr,
        )

    # === Paso 4: sumar contexto y decidir ===
    total_scores = {}
    for sys in SYSTEMS:
        if sys.catch_all:
            continue
        param_sc, param_bd = param_scores[sys.code]
        ctx_sc, ctx_bd = _score_context_against_system(table_ctx, sys)
        total_scores[sys.code] = (param_sc + ctx_sc, param_bd + ctx_bd)

    if not total_scores:
        return _make_classified_row(
            row, sistema_code='otros',
            sistema_source='catch_all', is_low_relevance=is_lr,
        )

    winner_code = max(total_scores, key=lambda c: total_scores[c][0])
    winner_score, winner_bd = total_scores[winner_code]

    # === Paso 5: si no alcanza umbral mínimo, catch-all ===
    if winner_score < MIN_SCORE_TO_ASSIGN:
        return _make_classified_row(
            row, sistema_code='otros',
            sistema_label='Otros / sin clasificar',
            sistema_source='catch_all',
            sistema_score=winner_score,
            score_breakdown=winner_bd,
            is_low_relevance=is_lr,
        )

    # Diferencia con keyword_strong: aquí necesitamos contexto para llegar.
    # source='keyword' si hay AL MENOS un match de tipo 'param' en breakdown,
    # 'context' si todo el score viene del contexto.
    has_param_match = any(k == 'param' for k, _, _ in winner_bd)
    source = 'keyword' if has_param_match else 'context'
    winner_sys = get_system(winner_code)
    return _make_classified_row(
        row,
        sistema_code=winner_code,
        sistema_label=winner_sys.label if winner_sys else winner_code,
        sistema_source=source,
        sistema_score=winner_score,
        score_breakdown=winner_bd,
        is_low_relevance=is_lr,
    )


# =====================================================================
# Dedup cross-sistema
# =====================================================================
# Reglas de resolución (en orden de prioridad):
#   1) keyword_strong  > keyword > context > catch_all > noise
#   2) entre empate de source: kw_length (suma de score_breakdown 'param')
#   3) entre empate: mayor score total
#   4) entre empate: orden de aparición (primero gana)

SOURCE_PRIORITY = {
    'keyword_strong': 4,
    'keyword': 3,
    'context': 2,
    'catch_all': 1,
    'noise': 0,
    None: 0,
}


def _resolution_score(classified_row, order_idx):
    """Tupla mayor → más prioritario.

    Componentes:
      a) source_priority (keyword_strong > keyword > context > ...)
      b) suma de score de tipo 'param' en el breakdown (longest/strongest match)
      c) score total
      d) -order_idx (primera aparición desempata)
    """
    source_p = SOURCE_PRIORITY.get(classified_row.get('sistema_source'), 0)
    param_score_sum = sum(
        s for k, _, s in classified_row.get('score_breakdown') or []
        if k == 'param'
    )
    total = classified_row.get('sistema_score', 0)
    return (source_p, param_score_sum, total, -order_idx)


def dedupe_classified(classified_rows):
    """Aplica dedup en 3 pasadas.

    Pasada A — duplicado exacto:
        Misma tupla (sistema_code, parametro_norm, valor_norm, rango_norm,
        estado_norm) → conservar el primero. Otros → descartados con
        motivo='duplicado_exacto'.

    Pasada B — duplicado mismo sistema + parámetro:
        Mismo (sistema_code, parametro_norm) → conservar el de mayor
        resolution_score. Otros → motivo='duplicado_parametro_mismo_sistema'.

    Pasada C — duplicado cross-sistema:
        Mismo parametro_norm en sistemas distintos → conservar el de
        mayor resolution_score. Esto resuelve los casos donde un mismo
        parámetro caía simultáneamente en respiratorio y endocrino por
        carry_forward inconsistente entre instancias.

    Args:
        classified_rows: lista de ClassifiedRow.

    Returns:
        (kept, descartados)
        kept: lista de ClassifiedRow conservadas
        descartados: lista de ClassifiedRow descartadas con motivo_descarte
                     y campos extra (ganador_sistema_code, etc.)
    """
    if not classified_rows:
        return [], []

    descartados = []

    # Las noise rows son descartadas inmediatamente (no participan en dedup
    # contra clínicas — su motivo es ruido_no_clinico, ya marcado por classify).
    clinical = []
    for cr in classified_rows:
        if cr.get('is_noise') or cr.get('sistema_source') == 'noise':
            d = dict(cr)
            if not d.get('motivo_descarte'):
                d['motivo_descarte'] = 'ruido_no_clinico'
            descartados.append(d)
        else:
            clinical.append(cr)

    if not clinical:
        return [], descartados

    # === Pasada A: dedup exacto ===
    seen_exact = set()
    unique_exact = []
    for cr in clinical:
        key = (
            cr.get('sistema_code') or '',
            cr.get('parametro_norm') or '',
            cr.get('valor_norm') or '',
            cr.get('rango_norm') or '',
            cr.get('estado_norm') or '',
        )
        if key in seen_exact:
            d = dict(cr)
            d['motivo_descarte'] = 'duplicado_exacto'
            descartados.append(d)
            continue
        seen_exact.add(key)
        unique_exact.append(cr)

    # === Pasada B: dedup (sistema, parametro) — mejor calidad gana ===
    best_per_sis_param = {}   # (sis, param) → (idx, row)
    order_b = []
    for idx, cr in enumerate(unique_exact):
        key = (cr.get('sistema_code') or '', cr.get('parametro_norm') or '')
        if not key[1]:
            # Sin parametro_norm → conservar como está, no dedup
            order_b.append(('keep', cr))
            continue
        existing = best_per_sis_param.get(key)
        if existing is None:
            best_per_sis_param[key] = (idx, cr)
            order_b.append(('grp', key))
        else:
            if _resolution_score(cr, idx) > _resolution_score(existing[1], existing[0]):
                d = dict(existing[1])
                d['motivo_descarte'] = 'duplicado_parametro_mismo_sistema'
                descartados.append(d)
                best_per_sis_param[key] = (idx, cr)
            else:
                d = dict(cr)
                d['motivo_descarte'] = 'duplicado_parametro_mismo_sistema'
                descartados.append(d)

    unique_b = []
    for entry in order_b:
        if entry[0] == 'keep':
            unique_b.append(entry[1])
        else:
            _, key = entry
            if key in best_per_sis_param:
                unique_b.append(best_per_sis_param[key][1])

    # === Pasada C: dedup cross-sistema ===
    # Mismo parametro_norm en sistemas distintos → keyword_strong > keyword
    # > context > catch_all. Las versiones perdedoras se mandan a descartados
    # con motivo='duplicado_conflicto_sistema' y ganador_sistema_code anotado.
    best_per_param = {}   # param_norm → (idx, row)
    order_c = []
    for idx, cr in enumerate(unique_b):
        param = cr.get('parametro_norm') or ''
        if not param:
            order_c.append(('keep', cr))
            continue
        existing = best_per_param.get(param)
        if existing is None:
            best_per_param[param] = (idx, cr)
            order_c.append(('grp', param))
        else:
            if _resolution_score(cr, idx) > _resolution_score(existing[1], existing[0]):
                # Nuevo gana → viejo a descartados
                d = dict(existing[1])
                d['motivo_descarte'] = 'duplicado_conflicto_sistema'
                d['ganador_sistema_code'] = cr.get('sistema_code')
                d['ganador_sistema_source'] = cr.get('sistema_source')
                descartados.append(d)
                best_per_param[param] = (idx, cr)
            else:
                # Viejo gana → nuevo a descartados
                d = dict(cr)
                d['motivo_descarte'] = 'duplicado_conflicto_sistema'
                d['ganador_sistema_code'] = existing[1].get('sistema_code')
                d['ganador_sistema_source'] = existing[1].get('sistema_source')
                descartados.append(d)

    out = []
    for entry in order_c:
        if entry[0] == 'keep':
            out.append(entry[1])
        else:
            _, key = entry
            if key in best_per_param:
                out.append(best_per_param[key][1])

    return out, descartados


# =====================================================================
# Orquestador de la capa de clasificación
# =====================================================================

def classify(raw_rows):
    """API principal del classifier.

    Args:
        raw_rows: lista de RawRow (output de extractor.extract()).

    Returns:
        dict con:
          'kept': lista de ClassifiedRow conservadas
          'descartados': lista de ClassifiedRow descartadas con motivo
          'stats': dict con conteos por motivo, por sistema, etc.

    NO produce ranking ni severity numérica. Eso es Fase 3.3+.
    """
    # 1. Clasificar cada fila individualmente
    classified = [classify_row(r) for r in raw_rows or []]

    # 2. Dedup (incluye separación de noise)
    kept, descartados = dedupe_classified(classified)

    # 3. Stats
    from collections import Counter
    sistemas_count = Counter(cr.get('sistema_code') for cr in kept)
    sources_count = Counter(cr.get('sistema_source') for cr in kept)
    motivos_count = Counter(d.get('motivo_descarte') for d in descartados)

    return {
        'kept': kept,
        'descartados': descartados,
        'stats': {
            'total_in': len(classified),
            'total_kept': len(kept),
            'total_descartados': len(descartados),
            'por_sistema': dict(sistemas_count),
            'por_source': dict(sources_count),
            'por_motivo_descarte': dict(motivos_count),
        },
    }
