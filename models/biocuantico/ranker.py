# -*- coding: utf-8 -*-
"""Motor BioCuántico v3 — Ranker puro (Fase 3.4).

Convierte una lista de hallazgos (rows con sistema_code + severity_level)
en un ranking de prioridades funcionales.

REGLA ARQUITECTÓNICA:
    El PDF clasifica → classifier.py (Fase 3.2)
    El PDF tiene severidad → severity.py (Fase 3.3)
    El antecedente PRIORIZA → ranker.py (Fase 3.4) ← este módulo
    La IA interpreta → prompt + providers (no se toca)

El antecedente del cliente SOLO afecta el ranking del sistema YA
clasificado. NUNCA reclasifica parámetros. La asignación parámetro →
sistema es responsabilidad exclusiva del classifier.

API principal:
    rank(rows_with_severity, antecedente_text='', max_priorities=6)
        → dict con keys:
            'prioridades': list[Priority] (top-N)
            'systems_audit': dict completo por sistema
            'boost_codes': lista de sistemas boosteados por antecedente
"""
import logging
import unicodedata

from .taxonomy import get_system


_logger = logging.getLogger(__name__)


# =====================================================================
# Pesos de scoring
# =====================================================================
# severo / crítico cuentan como severo (peso 5)
WEIGHTS = {
    'critico': 5,
    'severo': 5,
    'moderado': 3,
    'leve': 1,
    # 'normal', 'optimo', 'desconocida' = peso 0
}


# Si los leves dominan (mucho ruido), penalizar el sistema.
# Aplica cuando: leves > 4*severos + 2*moderados.
LEVES_DOMINANT_PENALTY = 0.5

# Boost multiplicativo cuando el antecedente del cliente apunta al sistema.
ANTECEDENTE_BOOST_FACTOR = 1.6

# Cap default de prioridades funcionales en el output
DEFAULT_MAX_PRIORITIES = 6


# =====================================================================
# Mapeo antecedente → sistemas (boost-only, no reclasifica)
# =====================================================================
# Cada (trigger_substring, [sistema_codes]) — si trigger aparece en el
# antecedente normalizado, los sistemas listados reciben boost.
ANTECEDENTE_TRIGGERS = (
    ('hipotiroid', ['endocrino']),
    ('hipertiroid', ['endocrino']),
    ('tiroides', ['endocrino']),
    ('lumbar', ['musculoesqueletico']),
    ('lumbalgia', ['musculoesqueletico']),
    ('cervical', ['musculoesqueletico']),
    ('dolor cronico', ['musculoesqueletico', 'nervioso']),
    ('artritis', ['musculoesqueletico', 'inmune']),
    ('metales', ['toxicidad']),
    ('detox', ['toxicidad', 'digestivo']),
    ('desintox', ['toxicidad', 'digestivo']),
    ('intoxicacion', ['toxicidad']),
    ('inflamacion', ['inmune', 'digestivo']),
    ('inflamatori', ['inmune', 'digestivo']),
    ('autoinmune', ['inmune']),
    ('digestiv', ['digestivo']),
    ('hepatic', ['digestivo']),
    ('higado', ['digestivo']),
    ('intestin', ['digestivo']),
    ('reflujo', ['digestivo']),
    ('cardiovasc', ['cardiovascular']),
    ('hipertens', ['cardiovascular']),
    ('colesterol', ['metabolico', 'cardiovascular']),
    ('metabolic', ['metabolico', 'cardiovascular']),
    ('diabet', ['metabolico', 'endocrino']),
    ('obesidad', ['metabolico', 'cardiovascular']),
    ('riñon', ['urinario']),
    ('renal', ['urinario']),
    ('respirator', ['respiratorio']),
    ('asma', ['respiratorio']),
    ('estres', ['nervioso']),
    ('ansiedad', ['nervioso']),
    ('depresion', ['nervioso']),
    ('insomnio', ['nervioso']),
    ('menopausia', ['endocrino', 'reproductor']),
)


def _strip_accents_lower(text):
    """Normaliza para match antecedente (lower + sin acentos)."""
    if not text:
        return ''
    nfkd = unicodedata.normalize('NFKD', str(text).lower())
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def compute_antecedente_boost(antecedente_text):
    """Devuelve lista ordenada de sistema_codes que recibirán boost.

    Es una función pura. No muta nada. Si no hay antecedente, lista vacía.
    """
    if not antecedente_text:
        return []
    norm = _strip_accents_lower(antecedente_text)
    codes = set()
    for trigger, sistemas in ANTECEDENTE_TRIGGERS:
        if trigger in norm:
            codes.update(sistemas)
    return sorted(codes)


# =====================================================================
# Agregación por sistema
# =====================================================================

def _row_weight(row):
    """Peso de una row según su severity_level."""
    return WEIGHTS.get(row.get('severity_level'), 0)


def aggregate_by_system(rows):
    """Agrupa rows por sistema_code y calcula stats clínicos.

    Devuelve dict {sistema_code: {
        'sistema_code', 'sistema_label',
        'severos', 'moderados', 'leves',
        'sev_max_level', 'sev_max_score',
        'hallazgos': [rows del sistema, ordenados por severidad desc],
    }}.

    Sólo cuenta hallazgos con severity >= leve. Normales / desconocidos
    aparecen en hallazgos pero no suman al weighted score.
    """
    by_sis = {}
    for row in rows or []:
        code = row.get('sistema_code')
        if not code:
            continue
        # Saltar la categoría 'otros' (catch-all del classifier) — no es
        # un sistema clínico real, no debe rankearse como prioridad.
        if code == 'otros':
            continue
        entry = by_sis.setdefault(code, {
            'sistema_code': code,
            'sistema_label': row.get('sistema_label') or code,
            'severos': 0,
            'moderados': 0,
            'leves': 0,
            'sev_max_level': 'normal',
            'sev_max_score': 1,
            'hallazgos': [],
        })
        level = row.get('severity_level') or 'desconocida'
        if level in ('critico', 'severo'):
            entry['severos'] += 1
        elif level == 'moderado':
            entry['moderados'] += 1
        elif level == 'leve':
            entry['leves'] += 1
        # Actualizar max severity_score visto
        sc = int(row.get('severity_score') or 0)
        if 1 <= sc <= 5 and sc > entry['sev_max_score']:
            entry['sev_max_score'] = sc
            entry['sev_max_level'] = level
        entry['hallazgos'].append(row)
    # Ordenar hallazgos por severity desc
    for entry in by_sis.values():
        entry['hallazgos'].sort(
            key=lambda r: -int(r.get('severity_score') or 0),
        )
    return by_sis


# =====================================================================
# Scoring por sistema
# =====================================================================

def score_system(stats, boost_codes=None):
    """Calcula el score final de un sistema.

    Formula:
        base = severos*5 + moderados*3 + leves*1
        si leves dominan (leves > 4*severos + 2*moderados): base *= 0.5
        si sistema en boost_codes: base *= 1.6 (ANTECEDENTE_BOOST_FACTOR)

    Devuelve dict con score y breakdown auditable.
    """
    sev = stats['severos']
    mod = stats['moderados']
    lev = stats['leves']
    boost_set = set(boost_codes or [])

    base = sev * WEIGHTS['severo'] + mod * WEIGHTS['moderado'] + lev * WEIGHTS['leve']

    penalty_applied = False
    if lev > 0 and lev > 4 * sev + 2 * mod:
        base *= LEVES_DOMINANT_PENALTY
        penalty_applied = True

    boosted = stats['sistema_code'] in boost_set
    final = base * (ANTECEDENTE_BOOST_FACTOR if boosted else 1.0)

    return {
        'score_base': sev * WEIGHTS['severo'] + mod * WEIGHTS['moderado']
                       + lev * WEIGHTS['leve'],
        'score_final': round(final, 2),
        'penalty_applied': penalty_applied,
        'boosted_por_antecedente': boosted,
        'boost_factor': ANTECEDENTE_BOOST_FACTOR if boosted else 1.0,
        'weights': {
            'severos': sev * WEIGHTS['severo'],
            'moderados': mod * WEIGHTS['moderado'],
            'leves': lev * WEIGHTS['leve'],
        },
    }


# =====================================================================
# Construcción de prioridades
# =====================================================================

SEVERITY_LABEL_FOR_SCORE = {
    0: 'optimo', 1: 'normal', 2: 'leve', 3: 'moderado',
    4: 'severo', 5: 'critico',
}


def _build_priority(stats, score_info, hallazgos_clave_n=3):
    """Construye un objeto Priority a partir de stats y score_info."""
    halls = stats['hallazgos'][:hallazgos_clave_n]
    return {
        'sistema_code': stats['sistema_code'],
        'sistema_label': stats['sistema_label'],
        'severidad': stats['sev_max_level'],
        'severidad_score': stats['sev_max_score'],
        'severos': stats['severos'],
        'moderados': stats['moderados'],
        'leves': stats['leves'],
        'score': score_info['score_final'],
        'score_base': score_info['score_base'],
        'penalty_applied': score_info['penalty_applied'],
        'boosted_por_antecedente': score_info['boosted_por_antecedente'],
        'hallazgos_clave': halls,
        'audit': {
            'weights': score_info['weights'],
            'boost_factor': score_info['boost_factor'],
        },
    }


# =====================================================================
# API principal
# =====================================================================

def rank(rows_with_severity, antecedente_text='',
         max_priorities=DEFAULT_MAX_PRIORITIES):
    """Ranking funcional de sistemas a partir de hallazgos clasificados.

    Args:
        rows_with_severity: lista de rows (output de severity.assign_severity).
            Cada row debe tener al menos: sistema_code, sistema_label,
            severity_level, severity_score.
        antecedente_text: texto plano del antecedente del cliente
            (padecimientos + medicamentos + objetivo). Si vacío, no hay
            boost. NUNCA reclasifica parámetros.
        max_priorities: tope de prioridades en el output (default 6).

    Returns:
        dict con:
            'prioridades': list[Priority] — top-N ordenada por score desc.
                Cada Priority tiene sistema_code, sistema_label, severidad,
                severos/moderados/leves, score, score_base, penalty_applied,
                boosted_por_antecedente, hallazgos_clave, audit.
            'systems_audit': dict completo {sistema_code: Priority} con
                TODOS los sistemas con hallazgos (no sólo los top-N).
            'boost_codes': lista de sistemas que recibieron boost.
    """
    boost_codes = compute_antecedente_boost(antecedente_text)
    aggregated = aggregate_by_system(rows_with_severity)

    # Construir prioridades con scoring
    all_priorities = []
    for code, stats in aggregated.items():
        # Sistema sólo entra al ranking si tiene >=1 hallazgo clínico
        # (leve/moderado/severo). Sistemas con SOLO normales/desconocidas
        # no son "afectados".
        if stats['severos'] + stats['moderados'] + stats['leves'] == 0:
            continue
        score_info = score_system(stats, boost_codes=boost_codes)
        prio = _build_priority(stats, score_info)
        all_priorities.append(prio)

    # Orden determinístico:
    #   1. score desc
    #   2. severidad_score desc (severo gana en empate)
    #   3. total hallazgos clínicos desc
    #   4. sistema_label asc (estable)
    all_priorities.sort(key=lambda p: (
        -p['score'],
        -p['severidad_score'],
        -(p['severos'] + p['moderados'] + p['leves']),
        p['sistema_label'],
    ))

    top_n = all_priorities[:max_priorities]
    systems_audit = {p['sistema_code']: p for p in all_priorities}

    return {
        'prioridades': top_n,
        'systems_audit': systems_audit,
        'boost_codes': boost_codes,
    }
