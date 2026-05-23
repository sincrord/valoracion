# -*- coding: utf-8 -*-
"""Motor BioCuántico v3 — Master Summary aislado (Fase 3.6).

Convierte el output de parser_v3.parse_v3() en:
  * JSON canónico (estructura estable y auditable).
  * Texto compacto para inyección en prompt IA (<=6000 chars).
  * Metadata + auditoría estructurada.

Es un módulo PURO. No depende de Odoo, no llama a IA, no toca productos
ni cotización. Existe en paralelo con `master_summary.py` (productivo)
hasta la Fase 3.8.

Reglas:
  * Nunca inventar datos. Lo que no esté en el input, no aparece.
  * No usar lenguaje médico diagnóstico. Usar fraseo funcional.
  * No reclasificar parámetros: el sistema_code que trae cada hallazgo
    es ley.

API principal:
    build_json(parse_out)            → dict canónico
    build_compact_text(parse_out, max_chars=6000) → str
    summarize(parse_out, max_chars=6000) → dict combinado

donde `parse_out` es el dict devuelto por parser_v3.parse_v3().
"""
from collections import OrderedDict


MASTER_SUMMARY_V3_VERSION = '3.6.0'
DEFAULT_MAX_CHARS = 6000
MAX_PRIORITIES_HARD_CAP = 6


# =====================================================================
# Restricciones — triggers declarativos (puramente lectores)
# =====================================================================
# Las "restricciones" no se inventan: sólo se reportan cuando aparecen
# explícitamente en el texto de algún parámetro/valor/rango o cuando
# vienen embebidas en una fila como flag. Generalista, no por cliente.
RESTRICTION_TRIGGERS = (
    ('embarazo', 'embarazo'),
    ('lactancia', 'lactancia'),
    ('anticoagul', 'anticoagulante'),
    ('alergia', 'alergia'),
    ('intoleran', 'intolerancia'),
    ('marcapaso', 'marcapasos'),
    ('quimio', 'quimioterapia'),
    ('inmunosup', 'inmunosupresion'),
    ('contraindicad', 'contraindicacion'),
)


# Verbos / términos diagnósticos que NO deben aparecer en el texto
# compacto. Si alguno se filtra accidentalmente, los tests fallarán.
DIAGNOSTIC_FORBIDDEN = (
    'padece', 'padeces', 'diagnostica', 'diagnosticad',
    'enfermedad de', 'sufre de',
)


# Términos funcionales aceptados (para documentar el fraseo)
FUNCTIONAL_PHRASING = {
    'severo': 'valor muy fuera del rango funcional',
    'critico': 'valor crítico fuera de rango funcional',
    'moderado': 'valor moderadamente fuera de rango',
    'leve': 'valor levemente fuera de rango',
    'normal': 'valor dentro de rango',
    'optimo': 'valor en rango óptimo',
    'desconocida': 'sin datos suficientes',
}


# =====================================================================
# Helpers internos
# =====================================================================

def _norm(text):
    return (text or '').strip().lower()


def _row_brief(row):
    """Versión mínima de un hallazgo (para JSON / texto). Solo campos
    que ya existen en el row — no inventamos ninguno."""
    return OrderedDict((
        ('parametro', row.get('parametro_raw') or ''),
        ('valor', row.get('valor_raw') or ''),
        ('rango', row.get('rango_raw') or ''),
        ('sistema_code', row.get('sistema_code') or ''),
        ('sistema_label', row.get('sistema_label') or ''),
        ('severity_level', row.get('severity_level') or 'desconocida'),
        ('severity_score', row.get('severity_score') or 0),
    ))


def _is_principal(row):
    return row.get('severity_level') in ('severo', 'critico')


def _is_secundario(row):
    return row.get('severity_level') in ('leve', 'moderado')


def _detect_restricciones(rows_raw):
    """Escanea las filas RAW por triggers de restricción. Generalista:
    cualquier substring registrado en RESTRICTION_TRIGGERS.

    Devuelve lista ordenada de dicts {tipo, evidencia, parametro}."""
    out = []
    seen = set()
    for r in rows_raw or []:
        # Concatenamos los campos textuales para buscar
        haystack = ' '.join([
            _norm(r.get('parametro_raw')),
            _norm(r.get('valor_raw')),
            _norm(r.get('rango_raw')),
            _norm(r.get('estado_raw')),
        ])
        if not haystack.strip():
            continue
        for trigger, label in RESTRICTION_TRIGGERS:
            if trigger in haystack and (label, r.get('parametro_raw')) not in seen:
                seen.add((label, r.get('parametro_raw')))
                out.append(OrderedDict((
                    ('tipo', label),
                    ('parametro', r.get('parametro_raw') or ''),
                    ('evidencia', haystack[:120]),
                )))
    return out


def _descartados_resumen(descartados):
    """Resumen breve: parametro + motivo. Sin inventar nada."""
    out = []
    for d in descartados or []:
        out.append(OrderedDict((
            ('parametro', d.get('parametro_raw') or ''),
            ('motivo', d.get('motivo_descarte') or ''),
            ('sistema_original', d.get('sistema_code') or ''),
        )))
    return out


def _validate_summary(parse_out, prioridades):
    """Validaciones invariantes que el summary expone al usuario."""
    flags = OrderedDict()

    # 1. Ningún hallazgo clave fuera de su sistema
    fuera_sistema = []
    for p in prioridades:
        for h in p.get('hallazgos_clave') or []:
            if h.get('sistema_code') and h['sistema_code'] != p['sistema_code']:
                fuera_sistema.append({
                    'parametro': h.get('parametro_raw'),
                    'esperaba': p['sistema_code'],
                    'tiene': h.get('sistema_code'),
                })
    flags['ningun_hallazgo_clave_fuera_de_sistema'] = (
        len(fuera_sistema) == 0)
    flags['hallazgos_fuera_de_sistema_evidencia'] = fuera_sistema

    # 2. Ningún parámetro duplicado cross-sistema (en classified)
    seen = {}
    duplicados = []
    for r in parse_out.get('classified') or []:
        key = _norm(r.get('parametro_raw'))
        sis = r.get('sistema_code')
        if not key or not sis:
            continue
        if key in seen and seen[key] != sis:
            duplicados.append({
                'parametro': key,
                'sistemas': sorted({seen[key], sis}),
            })
        else:
            seen[key] = sis
    flags['ningun_parametro_duplicado_cross_sistema'] = (
        len(duplicados) == 0)
    flags['duplicados_cross_sistema_evidencia'] = duplicados

    # 3. Noise no aparece en prioridades
    noise_params = set()
    for d in parse_out.get('descartados') or []:
        if d.get('motivo_descarte') in (
                'noise_no_clinico', 'noise_seccion', 'noise_header'):
            p = _norm(d.get('parametro_raw'))
            if p:
                noise_params.add(p)
    noise_en_prio = []
    for p in prioridades:
        for h in p.get('hallazgos_clave') or []:
            if _norm(h.get('parametro_raw')) in noise_params:
                noise_en_prio.append(h.get('parametro_raw'))
    flags['noise_no_filtrado_a_prioridades'] = (len(noise_en_prio) == 0)
    flags['noise_en_prioridades_evidencia'] = noise_en_prio

    # 4. Tope max prioridades
    flags['prioridades_respetan_max_hard_cap'] = (
        len(prioridades) <= MAX_PRIORITIES_HARD_CAP)

    # 5. Información mínima presente
    flags['hay_rows_extraidas'] = bool(parse_out.get('rows_raw'))
    flags['hay_classified'] = bool(parse_out.get('classified'))
    flags['hay_severity'] = bool(parse_out.get('severity'))

    return flags


# =====================================================================
# JSON canónico
# =====================================================================

def build_json(parse_out):
    """Construye el JSON canónico a partir del output de parse_v3.

    Estructura raíz:
      analisis_biocuantico:
        prioridades_funcionales: [Priority slim]
        hallazgos_principales:   [row brief, severo/crítico]
        hallazgos_secundarios:   [row brief, moderado/leve]
        parametros_descartados:  [{parametro, motivo, sistema_original}]
        restricciones_detectadas:[{tipo, parametro, evidencia}]
        auditoria:               dict con stats + contrato + validaciones
        metadata_motor:          version + flags
    """
    parse_out = parse_out or {}
    prioridades = (parse_out.get('prioridades') or [])[:MAX_PRIORITIES_HARD_CAP]
    severity_rows = parse_out.get('severity') or []
    descartados = parse_out.get('descartados') or []
    rows_raw = parse_out.get('rows_raw') or []

    principales = [_row_brief(r) for r in severity_rows if _is_principal(r)]
    secundarios = [_row_brief(r) for r in severity_rows if _is_secundario(r)]

    # Slim de cada prioridad (sin filtrar nada del original)
    prioridades_slim = []
    for p in prioridades:
        prioridades_slim.append(OrderedDict((
            ('sistema_code', p.get('sistema_code')),
            ('sistema_label', p.get('sistema_label')),
            ('severidad', p.get('severidad')),
            ('severos', p.get('severos', 0)),
            ('moderados', p.get('moderados', 0)),
            ('leves', p.get('leves', 0)),
            ('score', p.get('score')),
            ('score_base', p.get('score_base')),
            ('penalty_applied', p.get('penalty_applied')),
            ('boosted_por_antecedente', p.get('boosted_por_antecedente')),
            ('hallazgos_clave', [_row_brief(h)
                                 for h in (p.get('hallazgos_clave') or [])]),
        )))

    restricciones = _detect_restricciones(rows_raw)

    validacion = _validate_summary(parse_out, prioridades)

    auditoria_in = parse_out.get('auditoria') or {}
    auditoria_out = OrderedDict((
        ('parser_v3_version', auditoria_in.get('parser_v3_version')),
        ('master_summary_v3_version', MASTER_SUMMARY_V3_VERSION),
        ('contrato_parser', auditoria_in.get('contrato') or {}),
        ('validaciones_summary', validacion),
        ('extractor_stats', auditoria_in.get('extractor') or {}),
        ('classifier_stats', auditoria_in.get('classifier') or {}),
        ('severity_distribucion',
         auditoria_in.get('severity_distribucion') or {}),
        ('ranker_stats', auditoria_in.get('ranker') or {}),
    ))

    info_missing = []
    if not rows_raw:
        info_missing.append('sin_filas_extraidas')
    if not parse_out.get('classified'):
        info_missing.append('sin_clasificacion')
    if not severity_rows:
        info_missing.append('sin_severidad')
    if not prioridades:
        info_missing.append('sin_prioridades')

    metadata_motor = OrderedDict((
        ('master_summary_v3_version', MASTER_SUMMARY_V3_VERSION),
        ('parser_v3_version', auditoria_in.get('parser_v3_version')),
        ('max_priorities_hard_cap', MAX_PRIORITIES_HARD_CAP),
        ('max_chars_texto_compacto', DEFAULT_MAX_CHARS),
        ('ia_invocada', False),
        ('productos_referenciados', False),
        ('informacion_faltante', info_missing),
    ))

    return OrderedDict((
        ('analisis_biocuantico', OrderedDict((
            ('prioridades_funcionales', prioridades_slim),
            ('hallazgos_principales', principales),
            ('hallazgos_secundarios', secundarios),
            ('parametros_descartados', _descartados_resumen(descartados)),
            ('restricciones_detectadas', restricciones),
            ('auditoria', auditoria_out),
            ('metadata_motor', metadata_motor),
        ))),
    ))


# =====================================================================
# Texto compacto para prompt
# =====================================================================

def _line_prioridad(prio, idx):
    label = prio.get('sistema_label') or prio.get('sistema_code') or '?'
    sev = prio.get('severidad') or 'desconocida'
    score = prio.get('score')
    sev_count = prio.get('severos', 0)
    mod_count = prio.get('moderados', 0)
    lev_count = prio.get('leves', 0)
    boost = ' [boost-antecedente]' if prio.get('boosted_por_antecedente') else ''
    penalty = ' [leves-dominan]' if prio.get('penalty_applied') else ''
    return ('{idx}. {label} | severidad max: {sev} | score={score} '
            '(sev={s} mod={m} leve={l}){boost}{penalty}').format(
        idx=idx, label=label, sev=sev, score=score,
        s=sev_count, m=mod_count, l=lev_count,
        boost=boost, penalty=penalty,
    )


def _line_hallazgo(row):
    p = row.get('parametro_raw') or ''
    v = row.get('valor_raw') or ''
    rg = row.get('rango_raw') or ''
    lev = row.get('severity_level') or 'desconocida'
    return '   - {p}: valor={v} | rango={r} | nivel={lev}'.format(
        p=p, v=v, r=rg, lev=lev,
    )


def build_compact_text(parse_out, max_chars=DEFAULT_MAX_CHARS):
    """Texto plano compacto para inyectar en un prompt IA.

    Estructura:
      [HEADER]
      [PRIORIDADES]
      [HALLAZGOS POR SISTEMA]
      [RESTRICCIONES]
      [DESCARTADOS - resumen]
      [AUDITORIA - resumen]

    Si el contenido excede max_chars, truncamos preservando prioridades
    completas y agregamos un marcador "[...truncado...]".
    """
    parse_out = parse_out or {}
    prioridades = (parse_out.get('prioridades') or [])[:MAX_PRIORITIES_HARD_CAP]
    severity_rows = parse_out.get('severity') or []
    descartados = parse_out.get('descartados') or []
    auditoria_in = parse_out.get('auditoria') or {}
    rows_raw = parse_out.get('rows_raw') or []

    lines = []
    lines.append('=== ANALISIS BIOCUANTICO FUNCIONAL ===')
    lines.append('motor: parser_v3 %s | master_summary_v3 %s' % (
        auditoria_in.get('parser_v3_version') or '?',
        MASTER_SUMMARY_V3_VERSION,
    ))
    lines.append('filas: %d | clasificadas: %d | descartadas: %d' % (
        len(rows_raw), len(parse_out.get('classified') or []),
        len(descartados),
    ))
    sev_dist = auditoria_in.get('severity_distribucion') or {}
    lines.append('severidad: severo=%d | moderado=%d | leve=%d | '
                 'normal=%d | desconocida=%d' % (
        sev_dist.get('severo', 0) + sev_dist.get('critico', 0),
        sev_dist.get('moderado', 0),
        sev_dist.get('leve', 0),
        sev_dist.get('normal', 0),
        sev_dist.get('desconocida', 0),
    ))
    boost = auditoria_in.get('ranker', {}).get('boost_codes') or []
    if boost:
        lines.append('boost antecedente: ' + ', '.join(boost))
    lines.append('')

    # PRIORIDADES
    lines.append('--- PRIORIDADES FUNCIONALES (max %d) ---'
                 % MAX_PRIORITIES_HARD_CAP)
    if not prioridades:
        lines.append('(sin prioridades — revisar entrada)')
    for i, p in enumerate(prioridades, 1):
        lines.append(_line_prioridad(p, i))
        for h in (p.get('hallazgos_clave') or [])[:3]:
            lines.append(_line_hallazgo(h))
    lines.append('')

    # HALLAZGOS PRINCIPALES POR SISTEMA (severo/crítico)
    principales = [r for r in severity_rows if _is_principal(r)]
    lines.append('--- HALLAZGOS PRINCIPALES (severo/critico) ---')
    if not principales:
        lines.append('(ninguno)')
    else:
        by_sis = OrderedDict()
        for r in principales:
            by_sis.setdefault(
                r.get('sistema_label') or r.get('sistema_code') or '?', []
            ).append(r)
        for label, rs in by_sis.items():
            lines.append('* ' + label)
            for r in rs:
                lines.append(_line_hallazgo(r))
    lines.append('')

    # HALLAZGOS SECUNDARIOS (leve/moderado) — resumen
    secundarios = [r for r in severity_rows if _is_secundario(r)]
    lines.append('--- HALLAZGOS SECUNDARIOS (leve/moderado) ---')
    if not secundarios:
        lines.append('(ninguno)')
    else:
        by_sis = OrderedDict()
        for r in secundarios:
            by_sis.setdefault(
                r.get('sistema_label') or r.get('sistema_code') or '?', []
            ).append(r)
        for label, rs in by_sis.items():
            lines.append('* %s (%d)' % (label, len(rs)))
            for r in rs[:4]:
                lines.append(_line_hallazgo(r))
            if len(rs) > 4:
                lines.append('   - (+%d hallazgos adicionales)' % (len(rs) - 4))
    lines.append('')

    # RESTRICCIONES
    restricciones = _detect_restricciones(rows_raw)
    lines.append('--- RESTRICCIONES DETECTADAS ---')
    if not restricciones:
        lines.append('(ninguna detectada en el PDF)')
    else:
        for r in restricciones:
            lines.append('* %s (en %s)' % (r['tipo'], r['parametro']))
    lines.append('')

    # DESCARTADOS — resumen por motivo
    lines.append('--- PARAMETROS DESCARTADOS (resumen) ---')
    if not descartados:
        lines.append('(ninguno)')
    else:
        from collections import Counter
        motivos = Counter(
            d.get('motivo_descarte') or 'sin_motivo' for d in descartados
        )
        for motivo, n in motivos.most_common():
            lines.append('* %s: %d' % (motivo, n))
    lines.append('')

    # AUDITORIA
    lines.append('--- AUDITORIA ---')
    contrato = auditoria_in.get('contrato') or {}
    lines.append('antecedente_no_reclasifica: %s'
                 % contrato.get('antecedente_no_reclasifica'))
    lines.append('parametro_unico_por_sistema: %s'
                 % contrato.get('ningun_parametro_en_multiples_sistemas'))
    lines.append('ia_invocada: %s'
                 % contrato.get('ia_no_invocada', True))

    full = '\n'.join(lines)
    if len(full) <= max_chars:
        return full

    # Truncado preservando cabecera + prioridades completas
    truncated = full[:max_chars - 50].rsplit('\n', 1)[0]
    return truncated + '\n[...truncado por max_chars=%d...]' % max_chars


# =====================================================================
# Orquestador del summary
# =====================================================================

def summarize(parse_out, max_chars=DEFAULT_MAX_CHARS):
    """Atajo: devuelve {'json': ..., 'text': ..., 'metadata': ...}."""
    js = build_json(parse_out)
    txt = build_compact_text(parse_out, max_chars=max_chars)
    return OrderedDict((
        ('json', js),
        ('text', txt),
        ('metadata', js['analisis_biocuantico']['metadata_motor']),
    ))
