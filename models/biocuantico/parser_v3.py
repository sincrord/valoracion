# -*- coding: utf-8 -*-
"""Motor BioCuántico v3 — Orquestador delgado (Fase 3.5).

Encadena:
    extractor → classifier → severity → ranker

Es un módulo PURO. No depende de Odoo, no toca BD, no invoca IA, no
modifica el flujo productivo. Existe en paralelo con `parser.py`
(productivo) hasta que la Fase 3.8 lo conecte.

API principal:
    parse_v3(pdf_b64=None, tables=None, text=None, table=None,
             antecedente_text='', max_priorities=6)
        → dict con:
            'rows_raw', 'classified', 'descartados',
            'severity', 'prioridades', 'systems_audit',
            'boost_codes', 'auditoria', 'stats'

Contrato verificado por auditoria (validado en runtime y en tests):
    * El antecedente NO reclasifica parámetros.
    * Cada parámetro queda en UN solo sistema (post-dedup).
    * IA no se invoca aquí.
    * Productos / cotización no se referencian.
"""
import logging

from .extractor import extract, extract_from_table
from .classifier import classify
from .severity import assign_severity, severity_stats
from .ranker import rank, DEFAULT_MAX_PRIORITIES


_logger = logging.getLogger(__name__)


PARSER_V3_VERSION = '3.5.0'


# =====================================================================
# Helpers internos (puros)
# =====================================================================

def _extract_rows(pdf_b64=None, tables=None, text=None, table=None):
    """Resuelve la entrada heterogénea a una lista de RawRow.

    Acepta:
      * pdf_b64: PDF crudo en base64 (preferido en producción).
      * tables: lista de tablas-meta (output de pdfplumber-like).
      * table: una tabla suelta (list[list[str]]) — atajo para tests.
      * text: texto plano (fallback).
    """
    if table is not None:
        return extract_from_table(table), {
            'paginas': 0, 'tablas': 1, 'filas_extraidas': None,
        }
    out = extract(pdf_b64=pdf_b64, tables=tables, text=text)
    return out['rows'], {
        'paginas': out['stats'].get('paginas', 0),
        'tablas': out['stats'].get('tablas', 0),
        'filas_extraidas': out['stats'].get('filas', len(out['rows'])),
    }


def _validate_no_param_cross_sistemas(classified_rows):
    """Confirma que ningún (parametro_raw normalizado) aparece en >1
    sistema_code después del dedup. Devuelve (ok, conflictos)."""
    seen = {}
    conflictos = []
    for r in classified_rows or []:
        key = (r.get('parametro_raw') or '').strip().lower()
        if not key:
            continue
        sis = r.get('sistema_code')
        if not sis:
            continue
        prev = seen.get(key)
        if prev and prev != sis:
            conflictos.append({
                'parametro': key, 'sistemas': sorted({prev, sis}),
            })
        else:
            seen[key] = sis
    return (len(conflictos) == 0), conflictos


def _validate_antecedente_no_reclasifica(classified_before, prioridades):
    """Verifica que para cada hallazgo_clave en prioridades, el
    sistema_code coincide con su clasificación pre-ranker.

    Esto blinda la regla arquitectónica: el antecedente solo prioriza.
    """
    # Mapa parametro_raw → sistema_code asignado por el classifier
    by_param = {}
    for r in classified_before or []:
        key = (r.get('parametro_raw') or '').strip().lower()
        if key:
            by_param[key] = r.get('sistema_code')

    conflictos = []
    for prio in prioridades or []:
        sis_prio = prio.get('sistema_code')
        for h in prio.get('hallazgos_clave', []):
            key = (h.get('parametro_raw') or '').strip().lower()
            if not key:
                continue
            sis_classifier = by_param.get(key)
            if sis_classifier and sis_classifier != sis_prio:
                conflictos.append({
                    'parametro': key,
                    'sistema_classifier': sis_classifier,
                    'sistema_en_prioridad': sis_prio,
                })
    return (len(conflictos) == 0), conflictos


# =====================================================================
# API principal
# =====================================================================

def parse_v3(pdf_b64=None, tables=None, text=None, table=None,
             antecedente_text='', max_priorities=DEFAULT_MAX_PRIORITIES):
    """Pipeline puro v3: extractor → classifier → severity → ranker.

    Args:
        pdf_b64: base64 del PDF (productivo).
        tables: lista de tablas-meta (alternativa a pdf_b64).
        table: tabla única (list[list[str]]) — atajo para tests.
        text: texto plano (fallback, sólo para audit/log).
        antecedente_text: texto del antecedente clínico. JAMÁS reclasifica;
            sólo afecta ranking via boost.
        max_priorities: tope de prioridades en el output (default 6).

    Returns:
        dict con:
          'rows_raw': list[RawRow] extraídas.
          'classified': list[ClassifiedRow] conservadas (kept).
          'descartados': list[ClassifiedRow] descartadas + motivo.
          'severity': list[Row+severity_level/score/breakdown].
          'prioridades': list[Priority] (top-N).
          'systems_audit': dict completo {sistema_code: Priority}.
          'boost_codes': list[str] sistemas boosteados por antecedente.
          'auditoria': dict con stats y contrato validado.
          'stats': dict combinado (extractor + classifier + severity).
    """
    # 1. Extractor
    rows_raw, extract_stats = _extract_rows(
        pdf_b64=pdf_b64, tables=tables, text=text, table=table,
    )

    # 2. Classifier (PDF clasifica — no usa antecedente)
    cls = classify(rows_raw)
    classified = cls['kept']
    descartados = cls['descartados']
    classifier_stats = cls['stats']

    # 3. Severity (PDF define severidad — no usa antecedente)
    with_severity = assign_severity(classified)
    sev_stats = severity_stats(with_severity)

    # 4. Ranker (antecedente PRIORIZA — único módulo que lo consume)
    rk = rank(
        with_severity,
        antecedente_text=antecedente_text,
        max_priorities=max_priorities,
    )

    # 5. Validar contrato arquitectónico
    ok_dedup, conflictos_dedup = _validate_no_param_cross_sistemas(classified)
    ok_no_recl, conflictos_recl = _validate_antecedente_no_reclasifica(
        classified, rk['prioridades'],
    )

    auditoria = {
        'parser_v3_version': PARSER_V3_VERSION,
        'extractor': extract_stats,
        'classifier': classifier_stats,
        'severity_distribucion': sev_stats,
        'ranker': {
            'sistemas_con_hallazgos': len(rk['systems_audit']),
            'prioridades_devueltas': len(rk['prioridades']),
            'boost_codes': rk['boost_codes'],
            'max_priorities': max_priorities,
        },
        'contrato': {
            'antecedente_no_reclasifica': ok_no_recl,
            'antecedente_no_reclasifica_conflictos': conflictos_recl,
            'ningun_parametro_en_multiples_sistemas': ok_dedup,
            'cross_sistema_conflictos': conflictos_dedup,
            'ia_no_invocada': True,
            'productos_no_referenciados': True,
        },
    }

    return {
        'rows_raw': rows_raw,
        'classified': classified,
        'descartados': descartados,
        'severity': with_severity,
        'prioridades': rk['prioridades'],
        'systems_audit': rk['systems_audit'],
        'boost_codes': rk['boost_codes'],
        'auditoria': auditoria,
        'stats': {
            'rows_raw': len(rows_raw),
            'classified': len(classified),
            'descartados': len(descartados),
            'anormales': sum(sev_stats.get(lev, 0)
                             for lev in ('leve', 'moderado', 'severo',
                                         'critico')),
            'sistemas_afectados': len(rk['systems_audit']),
            'prioridades': len(rk['prioridades']),
        },
    }
