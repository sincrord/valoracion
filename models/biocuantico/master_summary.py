# -*- coding: utf-8 -*-
"""BioCuanticoMasterSummary — Fase 2.2.

Toma el payload de BioCuanticoParser.parse() y produce:

  * json: dict con la forma EXACTA especificada en Fase 2.2
      {
        "sistemas_afectados": [...],
        "hallazgos_principales": [...],   # severo / crítico
        "hallazgos_secundarios": [...],   # leve / moderado
        "prioridades_funcionales": [...],
        "restricciones_detectadas": [...],
        "senales_metabolicas": [...],
        "parametros_anormales": [...],
        "estadisticas_parser": {...}
      }
  * text: versión compacta y priorizada, capada a 6000 chars, lista para
          inyectarse en el prompt IA reemplazando el resumen heurístico.

available=True sólo si status in ('ok', 'partial'). En 'failed' o
'not_biocuantico' devuelve available=False y el llamador cae al
flujo heurístico normal.
"""
import json as _json
import logging
import re

_logger = logging.getLogger(__name__)


# Pistas léxicas para detectar restricciones/intolerancias y señales metabólicas
_RESTRICCIONES_KWS = (
    'intoleran', 'alergi', 'sensibil', 'evitar', 'contraindic',
    'no consum', 'restricc', 'gluten', 'lactos',
)
_METABOLICAS_KWS = (
    'glucos', 'insulin', 'colesterol', 'triglicer', 'hba1c', 'metabol',
    'ph ', 'mineral', 'oxidativ', 'lipid', 'tiroide', 'cortisol',
    'leptin', 'grelin', 'homeostas',
)


# Mapeo antecedente → sistemas boost (Fase 2.4).
# Cuando el texto del antecedente del cliente contiene alguna de estas
# claves, los sistemas listados reciben un boost multiplicativo en el
# ranking de prioridades. Esto alinea el resumen con el caso clínico
# concreto en lugar de tratar todos los sistemas con el mismo peso.
_ANTECEDENTE_BOOST = (
    ('hipotiroid', ['endocrino']),
    ('tiroides', ['endocrino']),
    ('hipertiroid', ['endocrino']),
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
    ('colesterol', ['cardiovascular', 'endocrino']),
    ('metabolic', ['cardiovascular', 'endocrino']),
    ('diabet', ['endocrino', 'cardiovascular']),
    ('obesidad', ['endocrino', 'cardiovascular']),
    ('riñon', ['urinario']),
    ('renal', ['urinario']),
    ('respirator', ['respiratorio']),
    ('asma', ['respiratorio']),
    ('estres', ['nervioso']),
    ('ansiedad', ['nervioso']),
    ('depresion', ['nervioso']),
    ('insomnio', ['nervioso']),
)

# Pesos por severidad para el score de ranking
_WEIGHT_SEV = {
    5: 7,   # crítico (raro pero más peso aún)
    4: 5,   # severo
    3: 3,   # moderado
    2: 1,   # leve
    1: 0,   # normal — no suma
}

# Boost multiplicativo cuando el antecedente del cliente apunta a este sistema
_ANTECEDENTE_BOOST_FACTOR = 1.6

# Mínimo y máximo de prioridades funcionales (Fase 2.4 spec)
PRIORIDADES_MIN = 3
PRIORIDADES_MAX = 6


class BioCuanticoMasterSummary:
    """Constructor del 'Resumen Maestro Funcional'."""

    PHASE = '2.2'
    MAX_TEXT_CHARS = 6000

    # ===================================================================
    # API pública
    # ===================================================================
    @classmethod
    def build(cls, parsed_payload, env=None, antecedente_text=None):
        """Construye el resumen maestro.

        Args:
            parsed_payload: output de BioCuanticoParser.parse().
            env: Odoo env (opcional, para catálogos en el futuro).
            antecedente_text: texto plano del antecedente clínico del cliente
                (padecimientos + medicamentos + objetivo + alergias). Usado
                para boost del ranking de prioridades funcionales. Si None,
                el ranking funciona pero sin boost por contexto del caso.
        """
        if not parsed_payload:
            return cls._unavailable('no_payload')

        status = parsed_payload.get('status')
        if status == 'not_biocuantico':
            return cls._unavailable('no_biocuantico')
        if status == 'failed':
            return cls._unavailable('parser_failed',
                                    error=parsed_payload.get('error'))
        if status not in ('ok', 'partial'):
            return cls._unavailable('unknown_status:%s' % status)

        if not parsed_payload.get('parsed'):
            return cls._unavailable('parser_not_active')

        # Construir las listas (con boost de antecedente si se pasó)
        json_data = cls._build_json(parsed_payload,
                                    antecedente_text=antecedente_text)
        text = cls._render_text(json_data, status=status,
                                metodo=parsed_payload.get('metodo'))
        # Cap
        if len(text) > cls.MAX_TEXT_CHARS:
            text = cls._safe_truncate(text, cls.MAX_TEXT_CHARS)

        return {
            'available': True,
            'reason': None,
            'json': json_data,
            'text': text,
            'status': status,
            'phase': cls.PHASE,
        }

    # ===================================================================
    # Construcción del JSON estructurado
    # ===================================================================
    @classmethod
    def _build_json(cls, payload, antecedente_text=None):
        mediciones = list(payload.get('mediciones') or [])
        sistemas_in = list(payload.get('sistemas') or [])
        estadisticas = dict(payload.get('estadisticas') or {})

        # Asegurar todas las claves de estadísticas en el orden del spec
        estadisticas_norm = {
            'paginas_procesadas': int(estadisticas.get('paginas_procesadas') or 0),
            'tablas_detectadas': int(estadisticas.get('tablas_detectadas') or 0),
            'filas_utiles': int(estadisticas.get('filas_utiles') or 0),
            'filas_descartadas': int(estadisticas.get('filas_descartadas') or 0),
            'hallazgos_severos': int(estadisticas.get('hallazgos_severos') or 0),
            'hallazgos_moderados': int(estadisticas.get('hallazgos_moderados') or 0),
            'hallazgos_leves': int(estadisticas.get('hallazgos_leves') or 0),
        }

        # =========================================================
        # 1) Separar mediciones por severidad y baja relevancia
        # =========================================================
        principales = []      # severo (4) o crítico (5)
        secundarios = []      # leve (2) o moderado (3)
        parametros_anormales = []
        descartados_low = []  # baja relevancia (downgraded)
        restricciones = []
        metabolicas = []

        # Recuento por sistema (para ranking ponderado).
        # Sólo cuentan mediciones NO low_relevance y con sistema.
        sistema_stats = {}   # code → {sistema_label, severos, moderados, leves}

        for m in mediciones:
            sev = int(m.get('severity') or 9)
            low_rel = bool(m.get('low_relevance'))
            item = {
                'sistema_code': m.get('sistema_code'),
                'sistema_label': m.get('sistema_label'),
                'parametro': m.get('parametro'),
                'valor': m.get('valor'),
                'rango': m.get('rango'),
                'estado_raw': m.get('estado_raw'),
                'estado_norm': m.get('estado_norm'),
                'severity': sev,
                'source': m.get('source'),
                'low_relevance': low_rel,
                # Auditoría Fase 2.6 — visibilidad de cómo se clasificó
                'sistema_source': m.get('sistema_source'),
                'keyword_matched': m.get('keyword_matched'),
                'parametro_normalizado': m.get('parametro_normalizado'),
            }
            # Tagging de restricciones / metabólicas
            tag_source = ' '.join([
                (m.get('parametro') or ''),
                (m.get('estado_raw') or ''),
            ]).lower()
            if any(k in tag_source for k in _RESTRICCIONES_KWS):
                restricciones.append(item)
            if any(k in tag_source for k in _METABOLICAS_KWS):
                metabolicas.append(item)

            # Filtrar baja relevancia: van a descartados, NO al ranking
            if low_rel and sev >= 2:
                item['motivo_descarte'] = 'baja_relevancia_clinica'
                descartados_low.append(item)
                continue

            if sev in (4, 5):
                principales.append(item)
            elif sev in (2, 3):
                secundarios.append(item)
            if sev in (2, 3, 4, 5):
                parametros_anormales.append(item)
                # Acumular en stats por sistema para ranking
                code = m.get('sistema_code')
                if code:
                    entry = sistema_stats.setdefault(code, {
                        'sistema_code': code,
                        'sistema_label': m.get('sistema_label') or code,
                        'severos': 0, 'moderados': 0, 'leves': 0,
                        'sev_max': 0,
                        'top_findings': [],
                    })
                    if sev in (4, 5):
                        entry['severos'] += 1
                    elif sev == 3:
                        entry['moderados'] += 1
                    elif sev == 2:
                        entry['leves'] += 1
                    if sev > entry['sev_max']:
                        entry['sev_max'] = sev
                    entry['top_findings'].append(item)

        # =========================================================
        # 2) Ranking ponderado de sistemas → prioridades_funcionales
        # =========================================================
        boost_codes = cls._compute_antecedente_boost(antecedente_text or '')
        ranked = cls._rank_sistemas(sistema_stats, boost_codes)

        # Top 3-6 prioridades
        prioridades_caso = ranked[:PRIORIDADES_MAX]
        # min 3 si hay info — si ranked tiene menos, dejamos los que hay
        # (el spec dice "mínimo 3 si hay suficiente información").

        # =========================================================
        # 2.bis) Fase 2.10 — VALIDACIÓN ESTRICTA de consistencia
        # =========================================================
        # Cada hallazgo_clave de una prioridad DEBE tener sistema_code
        # exactamente igual al de la prioridad. Si no, es un bug upstream
        # (parser mal clasificó o cache inconsistente) — el hallazgo se
        # mueve a parametros_descartados con motivo y la prioridad no lo
        # muestra. Esto es una capa de defensa en profundidad.
        inconsistentes = cls._validar_consistencia_prioridades(
            prioridades_caso,
        )
        if inconsistentes:
            _logger.warning(
                "BioCuántico master_summary: %d hallazgos inconsistentes "
                "filtrados (motivo='hallazgo_sistema_inconsistente')",
                len(inconsistentes),
            )
            descartados_low.extend(inconsistentes)

        # =========================================================
        # 3) sistemas_afectados (filtrado por hallazgos clínicos)
        # =========================================================
        sistemas_afectados = []
        for s_code, s_stat in sistema_stats.items():
            if s_stat['severos'] + s_stat['moderados'] + s_stat['leves'] == 0:
                continue
            sistemas_afectados.append({
                'sistema_code': s_code,
                'sistema_label': s_stat['sistema_label'],
                'severity_max': s_stat['sev_max'],
                'hallazgos_count': (s_stat['severos'] + s_stat['moderados']
                                    + s_stat['leves']),
                'severos': s_stat['severos'],
                'moderados': s_stat['moderados'],
                'leves': s_stat['leves'],
            })
        # Orden: severidad max desc, luego # hallazgos desc
        sistemas_afectados.sort(
            key=lambda s: (-int(s['severity_max']), -int(s['hallazgos_count']),
                           s['sistema_label']),
        )

        # =========================================================
        # 4) Sistemas secundarios: con hallazgos pero NO entre las
        #    prioridades. Útil para mostrarlos en el resumen sin que
        #    dominen la atención.
        # =========================================================
        prioridad_codes = {p['sistema_code'] for p in prioridades_caso}
        sistemas_secundarios = [
            s for s in sistemas_afectados
            if s['sistema_code'] not in prioridad_codes
        ]

        # =========================================================
        # 5) Ordenar hallazgos siguiendo prioridad → sistema
        # =========================================================
        priority_rank = {p['sistema_code']: i for i, p in enumerate(prioridades_caso)}

        def _sortkey(x):
            code = x.get('sistema_code') or ''
            return (
                priority_rank.get(code, 999),    # prioridades primero
                -int(x.get('severity') or 0),
                (x.get('sistema_label') or ''),
                (x.get('parametro') or ''),
            )

        principales.sort(key=_sortkey)
        secundarios.sort(key=_sortkey)
        parametros_anormales.sort(key=_sortkey)

        return {
            'sistemas_afectados': sistemas_afectados,
            'sistemas_secundarios': sistemas_secundarios,
            'hallazgos_principales': principales,
            'hallazgos_secundarios': secundarios,
            'prioridades_funcionales': prioridades_caso,
            'restricciones_detectadas': restricciones,
            'senales_metabolicas': metabolicas,
            'parametros_anormales': parametros_anormales,
            # Fase 2.7: descartados acumula items de varias fuentes:
            #   - baja_relevancia_clinica (downgrade por low_relevance)
            #   - duplicado_exacto (mismo sistema/parametro/valor/rango/estado)
            #   - duplicado_parametro_mismo_sistema
            #   - duplicado_conflicto_sistema (cross-sistema con keyword wins)
            # Cada item lleva 'motivo_descarte'.
            'parametros_descartados': (
                descartados_low
                + list(payload.get('descartados_dedup') or [])
            ),
            'estadisticas_parser': estadisticas_norm,
            # === Metadatos persistidos ===
            'metodo': payload.get('metodo'),
            'status': payload.get('status'),
            'antecedente_boost_codes': boost_codes,
        }

    # ===================================================================
    # Ranking ponderado de sistemas (Fase 2.4)
    # ===================================================================
    @staticmethod
    def _compute_antecedente_boost(antecedente_text):
        """Devuelve la lista de códigos de sistema que reciben boost,
        derivada del texto del antecedente clínico del cliente.
        """
        if not antecedente_text:
            return []
        norm = antecedente_text.lower()
        # Strip accents lightly (sólo lo común — esto es match por keyword)
        import unicodedata
        nfkd = unicodedata.normalize('NFKD', norm)
        norm = ''.join(c for c in nfkd if not unicodedata.combining(c))
        codes = set()
        for trigger, sistemas in _ANTECEDENTE_BOOST:
            if trigger in norm:
                for code in sistemas:
                    codes.add(code)
        return sorted(codes)

    @classmethod
    def _rank_sistemas(cls, sistema_stats, boost_codes):
        """Calcula score por sistema y devuelve la lista de prioridades
        ordenada por score descendente.

        Score:
            base = severos*5 + moderados*3 + leves*1
            si leves dominan (leves >= 4*severos + 2*moderados): score *= 0.5
            si el sistema está en boost_codes: score *= FACTOR (1.6)

        Cada prioridad lleva:
            sistema_code, sistema_label, severidad (label),
            hallazgos_clave (top 3 más severos), razon_funcional,
            relacion_antecedente, severos, moderados, leves, score.
        """
        if not sistema_stats:
            return []
        boost_set = set(boost_codes or [])
        out = []
        for code, s in sistema_stats.items():
            sev = s['severos']
            mod = s['moderados']
            lev = s['leves']
            base = sev * _WEIGHT_SEV[4] + mod * _WEIGHT_SEV[3] + lev * _WEIGHT_SEV[2]
            # Penalización: muchos leves comparados con severos/moderados
            #   leves dominantes = sistema es ruidoso, baja prioridad
            if lev > 0 and lev >= 4 * sev + 2 * mod:
                base *= 0.5
            boosted = code in boost_set
            score = base * (_ANTECEDENTE_BOOST_FACTOR if boosted else 1.0)

            # Top findings: hasta 3 más severos
            top_sorted = sorted(
                s['top_findings'],
                key=lambda x: (-int(x.get('severity') or 0),
                               x.get('parametro') or ''),
            )
            hallazgos_clave = top_sorted[:3]

            out.append({
                'sistema_code': code,
                'sistema_label': s['sistema_label'],
                'severidad': cls._severity_label(s['sev_max']),
                'severos': sev,
                'moderados': mod,
                'leves': lev,
                'score': round(score, 2),
                'boosted_por_antecedente': boosted,
                'hallazgos_clave': hallazgos_clave,
                'razon_funcional': cls._razon_funcional(
                    s['sistema_label'], sev, mod, lev, hallazgos_clave,
                ),
                'relacion_antecedente': (
                    'Sistema alineado con antecedente del cliente'
                    if boosted else 'Sin relación directa con antecedente declarado'
                ),
            })

        # Sort: score desc, then severos desc, then sistema_label
        out.sort(key=lambda x: (-x['score'], -x['severos'], x['sistema_label']))
        return out

    @staticmethod
    def _validar_consistencia_prioridades(prioridades):
        """Fase 2.10 — defensa en profundidad.

        Para cada prioridad, verifica que cada item en hallazgos_clave
        tenga sistema_code EXACTAMENTE igual al sistema_code de la
        prioridad. Si no, el item se EXCLUYE de la prioridad y se
        devuelve en la lista `inconsistentes` para que el caller lo
        registre en parametros_descartados con
        motivo='hallazgo_sistema_inconsistente'.

        MUTA `prioridades` in-place: cada prioridad.hallazgos_clave se
        reemplaza por la lista filtrada.

        Returns:
            list: items inconsistentes (con campos extra para auditoría).
        """
        inconsistentes = []
        for pr in prioridades:
            pr_sis = pr.get('sistema_code')
            if not pr_sis:
                continue
            hk = pr.get('hallazgos_clave') or []
            valid_hk = []
            for h in hk:
                h_sis = h.get('sistema_code')
                if h_sis == pr_sis:
                    valid_hk.append(h)
                else:
                    d = dict(h)
                    d['motivo_descarte'] = 'hallazgo_sistema_inconsistente'
                    d['prioridad_origen_code'] = pr_sis
                    d['prioridad_origen_label'] = pr.get('sistema_label')
                    d['sistema_real'] = h_sis
                    inconsistentes.append(d)
            pr['hallazgos_clave'] = valid_hk
        return inconsistentes

    @staticmethod
    def _razon_funcional(sistema_label, severos, moderados, leves,
                         hallazgos_clave):
        """Genera una frase corta describiendo por qué apoyar este sistema."""
        partes = []
        if severos:
            partes.append("%d hallazgo%s severo%s" % (
                severos, '' if severos == 1 else 's', '' if severos == 1 else 's',
            ))
        if moderados:
            partes.append("%d moderado%s" % (
                moderados, '' if moderados == 1 else 's',
            ))
        if leves:
            partes.append("%d leve%s" % (leves, '' if leves == 1 else 's'))
        intensidad = ', '.join(partes) or 'sin hallazgos'

        # Ejemplos clave
        ej = [h.get('parametro') for h in hallazgos_clave if h.get('parametro')]
        ej_txt = (' (ej: ' + ', '.join(ej[:3]) + ')') if ej else ''
        return "Apoyar %s: %s%s." % (sistema_label.lower(), intensidad, ej_txt)

    # ===================================================================
    # Render compacto de texto (≤ 6000 chars) — Fase 2.4
    # ===================================================================
    @classmethod
    def _render_text(cls, j, status=None, metodo=None):
        lines = []
        lines.append("=== Análisis BioCuántico Funcional ===")
        meta = []
        if status:
            meta.append("status: %s" % status)
        if metodo:
            meta.append("método: %s" % metodo)
        est = j.get('estadisticas_parser') or {}
        meta.append("filas útiles: %d" % est.get('filas_utiles', 0))
        meta.append("prioridades: %d" % len(j.get('prioridades_funcionales') or []))
        boosted = j.get('antecedente_boost_codes') or []
        if boosted:
            meta.append("antecedente alineado: %s" % ', '.join(boosted))
        lines.append("(" + ", ".join(meta) + ")")
        lines.append("")

        # === PRIORIDADES FUNCIONALES (top 3-6) ===
        prioridades = j.get('prioridades_funcionales') or []
        if prioridades:
            lines.append("Prioridades funcionales (ranking ponderado):")
            for i, p in enumerate(prioridades, 1):
                boost_mark = ' ★' if p.get('boosted_por_antecedente') else ''
                lines.append("  %d. %s [%s]%s" % (
                    i, p['sistema_label'], p.get('severidad', '?'), boost_mark,
                ))
                lines.append("     Razón funcional: %s" % p.get('razon_funcional', ''))
                if p.get('relacion_antecedente'):
                    lines.append("     Relación con antecedente: %s" % p['relacion_antecedente'])
                if p.get('hallazgos_clave'):
                    lines.append("     Hallazgos clave:")
                    for h in p['hallazgos_clave']:
                        lines.append("       - " + cls._format_finding_short(h))
            lines.append("")

        # === SISTEMAS SECUNDARIOS (no priorizados pero con hallazgos) ===
        secundarios_sist = j.get('sistemas_secundarios') or []
        if secundarios_sist:
            lines.append("Sistemas secundarios (con hallazgos pero menor prioridad):")
            for s in secundarios_sist[:8]:
                lines.append("  - %s [%s] — %d hallazgo%s (S:%d M:%d L:%d)" % (
                    s.get('sistema_label') or '?',
                    cls._severity_label(s.get('severity_max') or 0),
                    s.get('hallazgos_count') or 0,
                    '' if (s.get('hallazgos_count') or 0) == 1 else 's',
                    s.get('severos', 0), s.get('moderados', 0), s.get('leves', 0),
                ))
            lines.append("")

        # === Restricciones detectadas ===
        restricciones = j.get('restricciones_detectadas') or []
        if restricciones:
            lines.append("Restricciones / alertas detectadas:")
            for m in restricciones[:10]:
                lines.append("  - " + cls._format_finding(m))
            lines.append("")

        # === Señales metabólicas ===
        metabolicas = j.get('senales_metabolicas') or []
        if metabolicas:
            lines.append("Señales metabólicas:")
            for m in metabolicas[:10]:
                lines.append("  - " + cls._format_finding(m))
            lines.append("")

        # === Parámetros descartados (baja relevancia / duplicados) ===
        descartados = j.get('parametros_descartados') or []
        if descartados:
            # Agrupar por motivo para mostrar el conteo desglosado
            by_motivo = {}
            for m in descartados:
                motivo = m.get('motivo_descarte') or 'sin_motivo'
                by_motivo.setdefault(motivo, []).append(m)
            total = len(descartados)
            counts = ', '.join(
                "%s=%d" % (mot, len(items))
                for mot, items in sorted(by_motivo.items(), key=lambda kv: -len(kv[1]))
            )
            lines.append("Parámetros descartados (%d total — %s):" % (total, counts))
            # Mostrar hasta 8 ejemplos, agrupados por motivo
            shown = 0
            for motivo, items in sorted(by_motivo.items(),
                                         key=lambda kv: -len(kv[1])):
                lines.append("  [motivo=%s]" % motivo)
                for m in items[:3]:
                    suffix = ''
                    if motivo == 'duplicado_conflicto_sistema':
                        suffix = ' (ganó: %s)' % (
                            m.get('ganador_sistema_code') or '?')
                    lines.append("    - " + cls._format_finding_short(m) + suffix)
                    shown += 1
                    if shown >= 8:
                        break
                if len(items) > 3:
                    lines.append("    ...(+%d más en este motivo)" % (len(items) - 3))
                if shown >= 8:
                    break
            lines.append("")

        # Estadísticas del parser (al final, compactas)
        if est:
            lines.append("Parser: %d página(s), %d tabla(s), %d/%d filas útiles, "
                         "hallazgos S/M/L = %d/%d/%d." % (
                             est.get('paginas_procesadas', 0),
                             est.get('tablas_detectadas', 0),
                             est.get('filas_utiles', 0),
                             est.get('filas_utiles', 0) + est.get('filas_descartadas', 0),
                             est.get('hallazgos_severos', 0),
                             est.get('hallazgos_moderados', 0),
                             est.get('hallazgos_leves', 0),
                         ))

        return "\n".join(lines)

    @staticmethod
    def _severity_label(sev):
        return {
            0: 'óptimo', 1: 'normal', 2: 'leve', 3: 'moderado',
            4: 'severo', 5: 'crítico',
        }.get(int(sev or 0), 'desconocido')

    @staticmethod
    def _group_by_sistema(mediciones, cap=25):
        """Agrupa una lista plana de hallazgos por sistema_label, preservando
        el orden de aparición. Devuelve list de (sistema_label, [items]).

        Aplica un cap GLOBAL (no por sistema) para no inflar el texto.
        """
        groups = {}
        order = []
        used = 0
        for m in mediciones:
            if used >= cap:
                break
            label = m.get('sistema_label') or m.get('sistema_code') or '(sin sistema)'
            if label not in groups:
                groups[label] = []
                order.append(label)
            groups[label].append(m)
            used += 1
        return [(lbl, groups[lbl]) for lbl in order]

    @staticmethod
    def _format_finding_short(m):
        """Igual que _format_finding pero sin el prefijo de sistema (ya está
        en el header del grupo)."""
        param = m.get('parametro') or '(parámetro no identificado)'
        valor = m.get('valor')
        rango = m.get('rango')
        estado = m.get('estado_raw') or m.get('estado_norm') or '?'
        extra = []
        if valor:
            extra.append("valor=%s" % valor)
        if rango:
            extra.append("rango=%s" % rango)
        extra_str = (" (" + ", ".join(extra) + ")") if extra else ""
        return "%s — %s%s" % (param, estado, extra_str)

    @staticmethod
    def _format_finding(m):
        sistema = m.get('sistema_label') or m.get('sistema_code') or '?'
        param = m.get('parametro') or '(parámetro no identificado)'
        valor = m.get('valor')
        rango = m.get('rango')
        estado = m.get('estado_raw') or m.get('estado_norm') or '?'
        extra = []
        if valor:
            extra.append("valor=%s" % valor)
        if rango:
            extra.append("rango=%s" % rango)
        extra_str = (" (" + ", ".join(extra) + ")") if extra else ""
        return "%s: %s — %s%s" % (sistema, param, estado, extra_str)

    @staticmethod
    def _safe_truncate(text, cap):
        """Trunca al límite respetando borde de línea, añade marcador."""
        if len(text) <= cap:
            return text
        cut = text.rfind('\n', 0, cap - 50)
        if cut < int(cap * 0.6):
            cut = cap - 50
        return text[:cut].rstrip() + "\n[Resumen truncado a %d chars]" % cap

    # ===================================================================
    # Reconstrucción de payload desde cache JSON (Fase 2.3 bugfix)
    # ===================================================================
    @classmethod
    def payload_from_json(cls, json_data, archivo_status=None,
                          parse_error=None):
        """Reconstruye un payload tipo-parser desde el JSON cacheado en
        archivo.biocuantico_summary_json.

        Esto resuelve el bug donde, cuando el parser NO se re-corre en una
        generación (cache fuerte, status='ok' o 'partial'), la auditoría del
        log IA salía con método=ninguno y contadores=0 porque se construía
        sobre un payload-stub vacío.

        Args:
            json_data: str (JSON) o dict ya parseado.
            archivo_status: fallback para `status` cuando el JSON no lo trae
                (caches antiguos, anteriores al bugfix).
            parse_error: error persistido en archivo.biocuantico_parse_error.

        Returns:
            dict con la forma del payload del parser, o None si el JSON es
            inválido o vacío.
        """
        if not json_data:
            return None
        if isinstance(json_data, str):
            try:
                data = _json.loads(json_data)
            except (ValueError, TypeError):
                _logger.warning(
                    "BioCuántico: biocuantico_summary_json no es JSON válido; "
                    "audit caerá a stub. Considera re-correr el parser."
                )
                return None
        elif isinstance(json_data, dict):
            data = json_data
        else:
            return None

        est = dict(data.get('estadisticas_parser') or {})
        sistemas_afectados = data.get('sistemas_afectados') or []

        # Reconstruir lista 'sistemas' con la misma forma que el parser
        # devuelve en runtime. Lo que se persistió ya es la lista filtrada
        # (sólo sistemas con hallazgos) — el audit muestra ESOS, que es
        # lo clínicamente relevante.
        sistemas = []
        for s in sistemas_afectados:
            if not isinstance(s, dict):
                continue
            sistemas.append({
                'sistema_code': s.get('sistema_code'),
                'sistema_label': s.get('sistema_label') or s.get('sistema_code'),
                'severity_max': int(s.get('severity_max') or 0),
                'hallazgos_count': int(s.get('hallazgos_count') or 0),
            })

        return {
            'is_biocuantico': True,
            'status': data.get('status') or archivo_status or 'ok',
            'metodo': data.get('metodo') or 'cacheado',
            'estadisticas': est,
            'sistemas': sistemas,
            'mediciones': [],          # no se persisten en JSON
            'parametros_anormales': data.get('parametros_anormales') or [],
            'parsed': True,
            'phase': cls.PHASE,
            'error': parse_error,
        }

    # ===================================================================
    # API auxiliar: bloque de auditoría para valoracion.log.ia
    # ===================================================================
    @classmethod
    def render_audit_block(cls, parsed_payload, summary_text=None,
                           archivo_label=None):
        """Bloque legible para append en valoracion.log.ia.truncado_detalle.

        Formato (alineado con el spec del usuario):

            === PARSER BIOCUÁNTICO ===
            Archivo: <nombre>   ← opcional
            Detectado: Sí | No
            Status: ok | partial | failed | not_biocuantico
            Método: tablas | fallback textual | ambos | ninguno
            Páginas procesadas: X
            Tablas detectadas: X
            Filas útiles: X
            Filas descartadas: X
            Sistemas detectados:
              - <Sistema 1>
              - <Sistema 2>
            Hallazgos severos: X
            Hallazgos moderados: X
            Hallazgos leves: X
            Resumen maestro:
              <primeras N líneas del summary_text>
        """
        if not parsed_payload:
            return None
        est = parsed_payload.get('estadisticas') or {}
        sistemas = parsed_payload.get('sistemas') or []
        status = parsed_payload.get('status') or 'unknown'
        is_bq = bool(parsed_payload.get('is_biocuantico'))

        lines = ["=== PARSER BIOCUÁNTICO ==="]
        if archivo_label:
            lines.append("Archivo: %s" % archivo_label)
        lines.append("Detectado: %s" % ("Sí" if is_bq else "No"))
        lines.append("Status: %s" % status)
        metodo = parsed_payload.get('metodo') or 'ninguno'
        metodo_label = {
            'tablas': 'tablas',
            'fallback_textual': 'fallback textual',
            'ambos': 'tablas + fallback textual',
            'ninguno': 'ninguno',
        }.get(metodo, metodo)
        lines.append("Método: %s" % metodo_label)
        lines.append("Páginas procesadas: %d" % int(est.get('paginas_procesadas') or 0))
        lines.append("Tablas detectadas: %d" % int(est.get('tablas_detectadas') or 0))
        lines.append("Filas útiles: %d" % int(est.get('filas_utiles') or 0))
        lines.append("Filas descartadas: %d" % int(est.get('filas_descartadas') or 0))
        if sistemas:
            lines.append("Sistemas detectados:")
            for s in sistemas[:12]:
                lines.append("  - %s" % (s.get('sistema_label') or s.get('sistema_code') or '?'))
        else:
            lines.append("Sistemas detectados: (ninguno)")
        lines.append("Hallazgos severos: %d" % int(est.get('hallazgos_severos') or 0))
        lines.append("Hallazgos moderados: %d" % int(est.get('hallazgos_moderados') or 0))
        lines.append("Hallazgos leves: %d" % int(est.get('hallazgos_leves') or 0))
        if parsed_payload.get('error'):
            lines.append("Error: %s" % parsed_payload['error'])
        if summary_text:
            # Recortar el resumen al primer trozo útil para no inflar el log
            preview = summary_text.strip().splitlines()
            cap = 25
            preview = preview[:cap]
            lines.append("Resumen maestro:")
            for p in preview:
                lines.append("  " + p)
            if len(summary_text.strip().splitlines()) > cap:
                lines.append("  [...]")
        return '\n'.join(lines)

    # ===================================================================
    # Internals
    # ===================================================================
    @classmethod
    def _unavailable(cls, reason, error=None):
        return {
            'available': False,
            'reason': reason,
            'error': error,
            'json': {},
            'text': '',
            'status': None,
            'phase': cls.PHASE,
        }
