# -*- coding: utf-8 -*-
"""BioCuanticoParser — Fase 2.2.

Pipeline completo:
  1. Detección heurística (heredada de Fase 2.1).
  2. Extracción tabular vía pdfplumber.page.extract_tables() — ruta principal.
  3. Si tablas insuficientes / falla pdfplumber: fallback a
     page.extract_text() + regex por línea.
  4. Si ambos fallan: status='failed'. El llamador cae al flujo heurístico
     (summarize_by_keywords) — el parser NUNCA debe romper la valoración.

Contrato público:

    BioCuanticoParser.is_biocuantico(text, tables=None) -> bool
    BioCuanticoParser.detect(text, tables=None) -> dict
    BioCuanticoParser.parse(text=None, tables=None, pdf_b64=None, env=None) -> dict

NO se usa OCR, embeddings, IA adicional ni async. Todo local con pdfplumber.
"""
import base64
import io
import logging
import re
import unicodedata

_logger = logging.getLogger(__name__)


class BioCuanticoParser:
    # ===================================================================
    # Marcadores de detección (heredados Fase 2.1, sin cambios)
    # ===================================================================
    MARKERS_STRONG = (
        'biocuantic', 'bio-cuantic', 'bio cuantic',
        'resonancia cuantica', 'analizador cuantico', 'analisis cuantico',
        'magnetic resonance quantum analyzer',
        'quantum resonance magnetic analyzer', 'quantum analyzer',
        'informe biocuantico', 'reporte biocuantico', 'amra',
    )
    MARKERS_MEDIUM = (
        'sistema cardiovascular', 'sistema digestivo', 'sistema endocrino',
        'sistema inmune', 'sistema inmunologico', 'sistema nervioso',
        'sistema respiratorio', 'sistema musculoesqueletico',
        'sistema linfatico', 'sistema urinario', 'sistema reproductor',
        'sistema tegumentario',
        'rango normal', 'valor de referencia', 'valor medido', 'valor actual',
        'estado de salud', 'indice de salud', 'ph y minerales',
        'metales pesados', 'minerales humanos', 'reflexologia',
    )
    POINTS_STRONG = 3
    POINTS_MEDIUM = 1
    POINTS_TABLE = 2
    DEFAULT_THRESHOLD = 5

    # ===================================================================
    # Umbrales de éxito (Fase 2.2)
    # ===================================================================
    OK_MIN_USEFUL_ROWS = 15
    OK_MIN_SISTEMAS = 3
    OK_MIN_ABNORMAL = 5

    # Tope a filas útiles a procesar por archivo (evita PDFs gigantes).
    MAX_USEFUL_ROWS = 600

    # ===================================================================
    # Patrones de extracción
    # ===================================================================
    _RE_RANGE = re.compile(
        r'(?:'
        r'(?P<lo1>-?\d+(?:[.,]\d+)?)\s*(?:-|–|—|a|to)\s*(?P<hi1>-?\d+(?:[.,]\d+)?)'
        r'|<\s*(?P<hi2>-?\d+(?:[.,]\d+)?)'
        r'|>\s*(?P<lo2>-?\d+(?:[.,]\d+)?)'
        r'|≤\s*(?P<hi3>-?\d+(?:[.,]\d+)?)'
        r'|≥\s*(?P<lo3>-?\d+(?:[.,]\d+)?)'
        r')'
    )
    _RE_NUMBER = re.compile(r'-?\d+(?:[.,]\d+)?')

    # Patrones específicos para parseo de rango robusto.
    # Aceptan coma decimal (estilo ES/MX) y punto decimal.
    _RE_BOUND_LT = re.compile(r'^<=?\s*(-?\d[\d.,]*)\s*$')
    _RE_BOUND_GT = re.compile(r'^>=?\s*(-?\d[\d.,]*)\s*$')
    # Captura dos números separados por -, –, —, "a", "to".
    # NO usa "," como separador (sería ambiguo con coma decimal).
    _RE_BOUND_RANGE = re.compile(
        r'(-?\d[\d.,]*)\s*(?:-|–|—|\s+a\s+|\s+to\s+)\s*(-?\d[\d.,]*)'
    )

    # Umbrales (en %) de desviación fuera del rango → estado normalizado.
    # Fase 2.4: suavizado contra falsos severos.
    #
    # Cuando el rango tiene dos bordes (lo, hi), se calcula:
    #   pct_bound   = |val - bound| / |bound|
    #   pct_central = |val - bound| / |(lo+hi)/2|
    # y se usa la MENOR de las dos como score efectivo. Esto evita que
    # rangos donde el borde es pequeño (cerca de cero) generen severos
    # artificiales por la división por un valor pequeño.
    NUMERIC_THRESHOLD_LEVE = 0.10       # ≤ 10% → leve
    NUMERIC_THRESHOLD_MODERADO = 0.30   # ≤ 30% → moderado
    # > 30% → severo

    # Patrones de parámetros de BAJA RELEVANCIA funcional/clínica.
    # Si el parámetro matchea aquí, su severidad se baja UN nivel:
    #   severo → moderado, moderado → leve, leve → normal.
    # Motivación: muchos reportes BC incluyen indicadores estéticos o
    # señales de tracking que no deben sobre-influir la priorización
    # clínica. Esta lista es conservadora; se puede extender vía Admin
    # en futuro.
    LOW_RELEVANCE_PATTERNS = (
        'arrugas', 'rugas', 'manchas cutaneas', 'celulitis',
        'estetic', 'cosmetic', 'apariencia',
        'cabello (estetico)', 'piel (estetico)',
        'olor corporal', 'sudoracion menor',
    )

    # Fase 2.8 — Patrones de RUIDO NO-CLÍNICO.
    # Si el parámetro contiene alguno de estos términos, NO es un hallazgo
    # clínico real — es texto de layout (figuras, tablas, ilustraciones,
    # cabeceras, notas), ejemplos o referencias documentales del propio PDF.
    # Estos parámetros van a `parametros_descartados` con motivo
    # 'ruido_no_clinico' y NUNCA entran en ranking ni hallazgos.
    NOISE_PATTERNS = (
        'ilustracion', 'ilustraciones',
        'imagen', 'imagenes',
        'figura', 'figuras',
        'grafico', 'graficos', 'grafica',
        'tabla siguiente', 'tabla anterior', 'tabla resumen',
        'page', 'pagina ', 'pag.',
        'logo', 'logotipo',
        'nota:', 'nota al pie', 'pie de pagina', 'pie de página',
        'ejemplo', 'ejemplos', 'ejemplo:',
        'explicacion', 'explicaciones',
        'introduccion', 'introductorio',
        'descripcion del grafico', 'descripcion de la tabla',
        'descripcion general',
        'titulo', 'subtitulo', 'encabezado',
        'header', 'footer',
        'anexo', 'apendice', 'glosario',
        'referencias bibliograficas', 'bibliografia',
        'indice general', 'tabla de contenido', 'contenido del informe',
    )

    # Fase 2.9 — Overrides CRÍTICOS de clasificación.
    # Mapa explícito parametro_normalizado → sistema_code con prioridad
    # MÁXIMA. Se consulta ANTES de longest-match y antes de carry_forward.
    # Pensado para frases cortas o con prefijos que matcheaban débilmente
    # y terminaban heredando carry_forward (ej. "Ojo" dentro de una sección
    # respiratoria iba a respiratorio porque la kw 'ojo' = 3 chars perdía
    # contra cualquier otra keyword del catálogo o no llegaba a registrarse
    # en flujos extremos).
    #
    # Reglas:
    #   - Match exacto (parametro_normalizado == key) → override directo.
    #   - sistema_source resultante = 'critical_override'.
    #   - Override gana sobre keyword genérica, carry_forward y context.
    #
    # Las KEYS deben estar normalizadas (lower + sin acentos + sin extra space).
    CRITICAL_PARAM_OVERRIDES = {
        # Cardiovascular — variantes de "Demanda de Sangre Miocardial"
        'demanda de sangre miocardial': 'cardiovascular',
        'demanda miocardial': 'cardiovascular',
        'miocardial': 'cardiovascular',
        # Endocrino — variantes de "Tiroglobulina"
        'tiroglobulina': 'endocrino',
        'la tiroglobulina': 'endocrino',
        # Sensorial — variantes oculares cortas
        'ojo': 'sensorial',
        'ojos': 'sensorial',
        'actividad celular del ojo': 'sensorial',
        'fatiga visual': 'sensorial',
        # Fase 2.10 — Respiratorio: garantizar FRC y variantes
        'capacidad residual funcional': 'respiratorio',
        'capacidad residual funcional (frc)': 'respiratorio',
        'frc': 'respiratorio',
        'capacidad pulmonar': 'respiratorio',
        'capacidad vital': 'respiratorio',
        'capacidad vital forzada': 'respiratorio',
        'fev1': 'respiratorio',
        'volumen tidal': 'respiratorio',
        'volumen residual': 'respiratorio',
    }

    # Etiquetas legibles por sistema_code (para devolver sistema_label
    # cuando un override matchea sin pasar por el catálogo). Coincide con
    # los labels de _SISTEMAS_FALLBACK.
    _SISTEMA_LABELS = {
        'cardiovascular': 'Sistema Cardiovascular',
        'digestivo': 'Sistema Digestivo',
        'endocrino': 'Sistema Endocrino',
        'inmune': 'Sistema Inmune',
        'linfatico': 'Sistema Linfático',
        'nervioso': 'Sistema Nervioso',
        'respiratorio': 'Sistema Respiratorio',
        'urinario': 'Sistema Urinario',
        'reproductor': 'Sistema Reproductor',
        'musculoesqueletico': 'Sistema Musculoesquelético',
        'tegumentario': 'Sistema Tegumentario',
        'sensorial': 'Sistema Sensorial',
        'toxicidad': 'Toxicidad / Metales pesados',
        'metabolico': 'Sistema Metabólico / Nutricional',
    }

    # Fase 2.8 — Parámetros AMBIGUOS que requieren contexto para clasificar.
    # "Complacencia" en BC reports puede referirse a:
    #   - Complacencia pulmonar (respiratoria — comportamiento elástico
    #     del pulmón ante presión), o
    #   - Complacencia vascular (cardiovascular), o
    #   - Complacencia psicológica (no clínica BC).
    # Sin contexto cercano, NO debe ir a Endocrino ni absorberse por
    # carry-forward — debe etiquetarse 'contexto_insuficiente' y caer a
    # baja prioridad o descartarse según el caller.
    AMBIGUOUS_PATTERNS = {
        # parametro_norm → contexto-windows: si el text de la ventana
        # de la fila contiene alguna de las palabras, ir a ese sistema
        'complacencia': {
            'respiratorio': [
                'pulmonar', 'pulmon', 'pulmones', 'frc',
                'capacidad residual', 'capacidad vital', 'alveolo', 'alveolar',
                'bronquio', 'bronquios', 'vias respiratorias', 'diafragma',
                'espirometr',
            ],
            'cardiovascular': [
                'vascular', 'arteria', 'arterial', 'aortico', 'aorta',
                'circulacion', 'hemodinamic',
            ],
        },
    }

    # ===================================================================
    # Helpers básicos
    # ===================================================================

    # Caracteres invisibles que deben eliminarse antes de matching.
    # Vienen muy seguido del texto extraído de PDFs (especialmente
    # cuando hay tablas con bordes ricos o fuentes embebidas).
    _INVISIBLE_CHARS = (
        '​',   # ZERO WIDTH SPACE
        '‌',   # ZERO WIDTH NON-JOINER
        '‍',   # ZERO WIDTH JOINER
        '⁠',   # WORD JOINER
        '﻿',   # ZERO WIDTH NO-BREAK SPACE / BOM
        '­',   # SOFT HYPHEN
    )

    @classmethod
    def _normalize(cls, text):
        """Normalización fuerte para matching robusto contra texto sucio
        extraído de PDFs.

        Fase 2.6 — pipeline en orden:
          1) Quitar caracteres invisibles (ZW space, BOM, soft-hyphen, etc.)
          2) Reemplazar non-breaking spaces (U+00A0) por espacio normal.
          3) Lowercase.
          4) NFKD + strip combining (quita acentos).
          5) Reemplazar TODO whitespace (newlines, tabs, CR) por espacio
             normal y colapsar múltiples espacios.

        El paso (5) es CRÍTICO: sin él, una celda como
        "Actividad celular del\n ojo" no matchea la keyword
        "celular del ojo" porque el `\n` rompe el substring.
        """
        if not text:
            return ''
        t = str(text)
        # 1) Invisibles
        for inv in cls._INVISIBLE_CHARS:
            if inv in t:
                t = t.replace(inv, '')
        # 2) NBSP → espacio
        t = t.replace(' ', ' ')
        # 3) lower
        t = t.lower()
        # 4) NFKD + strip acentos
        nfkd = unicodedata.normalize('NFKD', t)
        cleaned = ''.join(c for c in nfkd if not unicodedata.combining(c))
        # 5) Colapsar TODO whitespace en un único espacio. str.split() sin args
        #    parte por cualquier secuencia de whitespace, ' '.join une con
        #    espacios simples. Esto reemplaza \n, \t, \r, espacios múltiples,
        #    todo de una sola pasada.
        cleaned = ' '.join(cleaned.split())
        return cleaned

    @classmethod
    def _score_text(cls, normalized_text):
        score, hits = 0, []
        for m in cls.MARKERS_STRONG:
            n = normalized_text.count(m)
            if n:
                score += cls.POINTS_STRONG * min(n, 3)
                hits.append({'marker': m, 'count': n, 'weight': 'strong'})
        for m in cls.MARKERS_MEDIUM:
            if m in normalized_text:
                score += cls.POINTS_MEDIUM
                hits.append({'marker': m, 'count': 1, 'weight': 'medium'})
        return score, hits

    @classmethod
    def _score_tables(cls, tables):
        if not tables:
            return 0, []
        combos = (
            ('item', 'valor', 'rango'),
            ('analisis', 'valor', 'rango'),
            ('analisis', 'resultado', 'rango'),
            ('parametro', 'valor', 'referencia'),
            ('sistema', 'estado'),
            ('sistema', 'indicador', 'estado'),
            ('prueba', 'valor', 'rango'),
        )
        score, hits = 0, []
        for idx, table in enumerate(tables):
            if not table:
                continue
            try:
                first = list(table[0])
            except (IndexError, TypeError):
                continue
            cells = [cls._normalize(str(c or '')) for c in first]
            if not any(cells):
                continue
            for combo in combos:
                if all(any(p in c for c in cells) for p in combo):
                    score += cls.POINTS_TABLE
                    hits.append({'table_index': idx, 'combo': combo, 'header': cells})
                    break
        return score, hits

    # ===================================================================
    # API de detección (sin cambios respecto a Fase 2.1)
    # ===================================================================
    @classmethod
    def is_biocuantico(cls, text, tables=None, threshold=None):
        return cls.detect(text, tables=tables, threshold=threshold)['is_biocuantico']

    @classmethod
    def detect(cls, text, tables=None, threshold=None):
        thr = cls.DEFAULT_THRESHOLD if threshold is None else int(threshold)
        normalized = cls._normalize(text or '')
        ts, th = cls._score_text(normalized)
        bs, bh = cls._score_tables(tables or [])
        total = ts + bs
        is_bq = total >= thr
        return {
            'is_biocuantico': is_bq,
            'status': 'detected' if is_bq else 'not_biocuantico',
            'score': total, 'threshold': thr,
            'text_hits': th, 'table_hits': bh,
        }

    # ===================================================================
    # API principal Fase 2.2: parse() unificado
    # ===================================================================
    @classmethod
    def parse(cls, text=None, tables=None, pdf_b64=None, env=None,
              threshold=None):
        """Parser completo. Devuelve payload estable.

        Args:
            text: texto plano del archivo (preferido si no hay PDF).
            tables: tablas pre-extraídas (lista de listas de filas).
            pdf_b64: base64 del PDF crudo. Si está presente, se usa
                pdfplumber para extracción tabular real.
            env: Odoo env (opcional). Si se pasa, se usan los catálogos
                valoracion.biocuantico.sistema y .estado.mapeo para
                clasificación; si no, se cae a una lista mínima embebida.
            threshold: umbral entero para is_biocuantico (default 5).

        Returns dict con:
            * is_biocuantico (bool)
            * status: 'ok' | 'partial' | 'failed' | 'not_biocuantico'
            * sistemas: list of {sistema_code, sistema_label, severity_max,
                                 hallazgos_count}
            * mediciones: list of {sistema, parametro, valor, rango,
                                   estado_raw, estado_norm, severity}
            * parametros_anormales: list (subset of mediciones)
            * estadisticas: {paginas, tablas, filas_utiles, filas_descartadas,
                             hallazgos_severos, hallazgos_moderados, hallazgos_leves}
            * metodo: 'tablas' | 'fallback_textual' | 'ambos' | 'ninguno'
            * parsed (bool): True si construyó estructura (status ok/partial)
            * phase: '2.2'
            * error (str|None): si falló duro
        """
        stats = {
            'paginas_procesadas': 0,
            'tablas_detectadas': 0,
            'filas_utiles': 0,
            'filas_descartadas': 0,
            'hallazgos_severos': 0,
            'hallazgos_moderados': 0,
            'hallazgos_leves': 0,
        }

        # === Paso 1: extracción cruda según fuente disponible ===
        # extracted_tables_meta: [{pagina, tabla_idx, rows}, ...] para
        #   mediciones con auditoría de posición (Fase 2.7).
        # extracted_tables: [rows, rows, ...] sin metadata, para
        #   detect() y _score_tables() que sólo necesitan filas.
        extracted_tables_meta = []
        extracted_tables = []
        extracted_text = text or ''
        metodo_usado = 'ninguno'
        try:
            if pdf_b64:
                pdf_tables_meta, pdf_text, pdf_stats = cls._extract_from_pdf_b64(pdf_b64)
                stats['paginas_procesadas'] = pdf_stats.get('paginas', 0)
                stats['tablas_detectadas'] = pdf_stats.get('tablas', 0)
                extracted_tables_meta = pdf_tables_meta
                extracted_tables = [t['rows'] for t in pdf_tables_meta]
                if pdf_text and not extracted_text:
                    extracted_text = pdf_text
                if extracted_tables and pdf_text:
                    metodo_usado = 'ambos'
                elif extracted_tables:
                    metodo_usado = 'tablas'
                elif pdf_text:
                    metodo_usado = 'fallback_textual'
            elif tables:
                # Legacy: tables= arg (lista de listas de filas).
                # Sin metadata real de pagina/tabla_idx.
                extracted_tables = list(tables)
                extracted_tables_meta = [
                    {'pagina': 0, 'tabla_idx': i, 'rows': t}
                    for i, t in enumerate(extracted_tables)
                ]
                stats['tablas_detectadas'] = len(extracted_tables)
                metodo_usado = 'tablas'
            elif extracted_text:
                metodo_usado = 'fallback_textual'
        except Exception as e:
            _logger.exception("BioCuántico: error extrayendo PDF")
            return cls._failed_payload(stats, error='extract_failed: %s' % e)

        # === Paso 2: detección ===
        det = cls.detect(extracted_text, tables=extracted_tables, threshold=threshold)
        if not det['is_biocuantico']:
            return {
                'is_biocuantico': False,
                'status': 'not_biocuantico',
                'sistemas': [],
                'mediciones': [],
                'parametros_anormales': [],
                'estadisticas': stats,
                'metodo': metodo_usado,
                'parsed': False,
                'phase': '2.2',
                'detection_score': det['score'],
                'detection_threshold': det['threshold'],
                'error': None,
            }

        # === Paso 3: clasificación ===
        try:
            sistemas_catalog = cls._load_sistemas_catalog(env)
            estados_lookup = cls._build_estados_lookup(env)

            mediciones = []
            # 3a) Filas desde tablas (con metadata de posición Fase 2.7)
            if extracted_tables_meta:
                mediciones.extend(cls._mediciones_from_tables(
                    extracted_tables_meta, sistemas_catalog, estados_lookup, stats,
                ))
            # 3b) Si pocas mediciones desde tablas, intentar texto como fallback
            if len(mediciones) < cls.OK_MIN_USEFUL_ROWS and extracted_text:
                txt_med = cls._mediciones_from_text(
                    extracted_text, sistemas_catalog, estados_lookup, stats,
                )
                # Marcar fallback textual usado
                if txt_med:
                    if metodo_usado == 'tablas':
                        metodo_usado = 'ambos'
                    elif metodo_usado == 'ninguno':
                        metodo_usado = 'fallback_textual'
                mediciones.extend(txt_med)

            # === Post-procesamiento (Fase 2.2 — calidad de señal) ===
            # 1) Descartar mediciones sin sistema_code (carry-forward falló).
            #    Estas se mostraban como "?" en el resumen — ruido puro.
            # 2) Deduplicar:
            #    a) hallazgos idénticos: misma (sistema, parámetro, valor,
            #       rango, estado_norm) → conservar el primero.
            #    b) parámetros repetidos: misma (sistema, parámetro) →
            #       conservar la entrada con MÁS información (mayor severidad,
            #       o con valor/rango si la otra no los tiene).
            antes = len(mediciones)

            # === Fase 2.8: separar ruido NO-CLÍNICO antes del dedup ===
            # Estos parámetros (ilustración, figura, encabezado, etc.) no
            # son hallazgos clínicos reales y NO deben rankearse.
            descartados_noise = []
            mediciones_clinicas = []
            for m in mediciones:
                if m.get('is_noise'):
                    d = dict(m)
                    d['motivo_descarte'] = 'ruido_no_clinico'
                    d['sistema_original'] = m.get('sistema_code')
                    descartados_noise.append(d)
                else:
                    mediciones_clinicas.append(m)
            mediciones = mediciones_clinicas

            # Filtrar sin sistema_code (carry-forward falló)
            mediciones = [m for m in mediciones if m.get('sistema_code')]

            # Dedup cross-sistema (Pasada A/B/C de Fase 2.7)
            mediciones, descartados_dedup = cls._dedup_mediciones(mediciones)

            # Mezclar todos los descartados (noise primero — más visibles
            # en la auditoría)
            descartados_dedup = descartados_noise + descartados_dedup

            descartadas_post = antes - len(mediciones)
            stats['filas_descartadas'] += descartadas_post
            stats['filas_utiles'] = len(mediciones)

            # Cap por seguridad
            if len(mediciones) > cls.MAX_USEFUL_ROWS:
                mediciones = mediciones[:cls.MAX_USEFUL_ROWS]

            # Stats: contar hallazgos por severidad
            sistemas_set = set()
            abnormal = []
            for m in mediciones:
                sev = m.get('severity', 9)
                if m.get('sistema_code'):
                    sistemas_set.add(m['sistema_code'])
                if sev == 5:
                    stats['hallazgos_severos'] += 1  # crítico contado como severo+
                    abnormal.append(m)
                elif sev == 4:
                    stats['hallazgos_severos'] += 1
                    abnormal.append(m)
                elif sev == 3:
                    stats['hallazgos_moderados'] += 1
                    abnormal.append(m)
                elif sev == 2:
                    stats['hallazgos_leves'] += 1
                    abnormal.append(m)

            # Resumen por sistema
            sistemas_dict = {}
            for m in mediciones:
                code = m.get('sistema_code')
                if not code:
                    continue
                sev = int(m.get('severity') or 0)
                entry = sistemas_dict.setdefault(code, {
                    'sistema_code': code,
                    'sistema_label': m.get('sistema_label') or code,
                    'severity_max': 0,
                    'hallazgos_count': 0,
                })
                # severity_max: sólo considerar severidades clínicas reales
                # (1=normal, 2=leve, 3=moderado, 4=severo, 5=crítico).
                # Excluir 9 (desconocido) — si no, un sistema con todo
                # 'desconocido' aparece como [desconocido] en prioridades.
                if 1 <= sev <= 5 and sev > entry['severity_max']:
                    entry['severity_max'] = sev
                if sev in (2, 3, 4, 5):
                    entry['hallazgos_count'] += 1
            sistemas_list = sorted(
                sistemas_dict.values(),
                key=lambda s: (-s['severity_max'], -s['hallazgos_count'], s['sistema_label']),
            )

            # === Paso 4: determinar status ===
            n_useful = stats['filas_utiles']
            n_sistemas = len(sistemas_list)
            n_abnormal = len(abnormal)
            ok = (
                n_useful >= cls.OK_MIN_USEFUL_ROWS
                or n_sistemas >= cls.OK_MIN_SISTEMAS
                or n_abnormal >= cls.OK_MIN_ABNORMAL
            )
            status = 'ok' if ok else 'partial'

            return {
                'is_biocuantico': True,
                'status': status,
                'sistemas': sistemas_list,
                'mediciones': mediciones,
                'parametros_anormales': abnormal,
                'descartados_dedup': descartados_dedup,
                'estadisticas': stats,
                'metodo': metodo_usado,
                'parsed': True,
                'phase': '2.7',
                'detection_score': det['score'],
                'detection_threshold': det['threshold'],
                'error': None,
            }
        except Exception as e:
            _logger.exception("BioCuántico: error clasificando")
            return cls._failed_payload(stats, error='classify_failed: %s' % e,
                                       metodo=metodo_usado)

    # ===================================================================
    # Extracción desde PDF
    # ===================================================================
    @classmethod
    def _extract_from_pdf_b64(cls, pdf_b64):
        """Abre el PDF con pdfplumber y devuelve (tablas, texto_total, stats).

        tablas: lista de tablas, cada tabla = lista de filas (lista de str).
        texto_total: '\\n' join de page.extract_text() (fallback textual).
        stats: {'paginas': N, 'tablas': N}
        """
        # Normalizar a bytes
        if isinstance(pdf_b64, str):
            data_b64 = pdf_b64.encode('ascii')
        else:
            data_b64 = pdf_b64
        raw = base64.b64decode(data_b64)
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            raise RuntimeError(
                "BioCuántico requiere pdfplumber. Instala: pip3 install pdfplumber"
            )

        # Fase 2.7: tablas se devuelven con metadata para auditoría.
        # tablas_meta: lista de dicts {pagina, tabla_idx, rows}.
        # tablas_meta no es exactamente backward-compatible: el callsite
        # detecta el formato (dict vs lista) y se adapta.
        tablas_meta = []
        texto_chunks = []
        n_pages = 0
        global_table_idx = 0
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                n_pages += 1
                # Tablas — ruta principal
                try:
                    page_tables = page.extract_tables() or []
                except Exception as e:
                    _logger.warning("page.extract_tables falló (p%d): %s", n_pages, e)
                    page_tables = []
                for t in page_tables:
                    # Filtrar filas completamente vacías
                    rows = [
                        [('' if c is None else str(c)) for c in row]
                        for row in t
                        if row and any((c is not None and str(c).strip()) for c in row)
                    ]
                    if rows:
                        tablas_meta.append({
                            'pagina': n_pages,
                            'tabla_idx': global_table_idx,
                            'rows': rows,
                        })
                        global_table_idx += 1
                # Texto — fallback / contexto adicional
                try:
                    txt = page.extract_text() or ''
                except Exception:
                    txt = ''
                if txt:
                    texto_chunks.append(txt)
        return tablas_meta, '\n'.join(texto_chunks), {
            'paginas': n_pages,
            'tablas': len(tablas_meta),
        }

    # ===================================================================
    # Catálogos (sistemas y estados) con fallback embebido
    # ===================================================================
    # Fallback usado cuando no hay env (tests aislados / extracción offline).
    # IMPORTANTE: estas keywords deben mantenerse en sincronía con el seed
    # data/biocuantico_data.xml. Si se cambian aquí, actualizar el seed XML
    # y dar al admin el SQL/UI para refrescar registros existentes en BD
    # (noupdate=1 evita auto-update al actualizar el módulo).
    #
    # Fase 2.5: expansión de keywords para corregir mis-clasificaciones
    # reportadas por el usuario (ej. "Demanda de Sangre Miocardial" iba a
    # respiratorio por carry-forward al no encontrar keyword específica).
    _SISTEMAS_FALLBACK = (
        ('cardiovascular', 'Sistema Cardiovascular',
         ['cardiovascular', 'corazon', 'presion arterial', 'ritmo cardiaco',
          'circulacion', 'vascular', 'miocardio', 'miocardial',
          'demanda miocardial', 'demanda de sangre', 'cerebrovascular',
          'sangre', 'hemoglobina', 'hematocrito', 'plaquetas', 'arteria',
          'arteriosclerosis', 'aterosclerosis', 'ateromatosis', 'ateroma',
          'infarto', 'angina', 'taquicardia', 'bradicardia', 'arritmia',
          'oxigenacion sanguinea', 'oxigeno en la sangre',
          'volumen de oxigeno']),
        ('digestivo', 'Sistema Digestivo',
         ['digestivo', 'gastrointestinal', 'estomago', 'intestino',
          'higado', 'vesicula', 'pancreas', 'bilis', 'biliar',
          'estreñimiento', 'microbiota', 'flora intestinal',
          'gastritis', 'ulcera gastrica', 'reflujo gastroesofagico',
          'colon', 'duodeno', 'helicobacter', 'acidez gastrica']),
        ('endocrino', 'Sistema Endocrino',
         ['endocrino', 'tiroides', 'tiroideo', 'suprarrenal', 'glandula',
          'hormonal', 'hipofisis', 'hipofisiario', 't3', 't4',
          'tiroglobulina', 'tiroxina', 'triyodotironina', 'tsh',
          'progesterona', 'prolactina', 'estrogeno', 'estradiol',
          'testosterona', 'fsh', 'lh', 'cortisol', 'dhea',
          'melatonina', 'insulina', 'oxitocina', 'aldosterona',
          'hormona de crecimiento']),
        ('inmune', 'Sistema Inmune',
         ['inmune', 'inmunologico', 'defensas', 'inflamacion',
          'autoinmune', 'iga secretora', 'igg', 'igm', 'ige',
          'linfocitos', 'leucocitos', 'neutrofilos']),
        ('linfatico', 'Sistema Linfático',
         ['linfatico', 'linfa', 'ganglios', 'bazo', 'drenaje linfatico']),
        ('nervioso', 'Sistema Nervioso',
         ['nervioso', 'neurologico', 'cerebro', 'nervios', 'snc',
          'neuronal', 'sinapsis', 'neurotransmisor', 'serotonina',
          'dopamina', 'gaba', 'acetilcolina', 'noradrenalina',
          'sistema nervioso autonomo', 'equilibrio autonomico']),
        ('respiratorio', 'Sistema Respiratorio',
         ['respiratorio', 'pulmonar', 'pulmones', 'bronquios',
          'capacidad pulmonar', 'capacidad vital', 'capacidad residual',
          'capacidad residual funcional', 'frc', 'fev1', 'espirometr',
          'alveolo', 'alveolos', 'alveolar', 'diafragma',
          'vias respiratorias']),
        ('urinario', 'Sistema Urinario',
         ['urinario', 'renal', 'riñon', 'vejiga', 'creatinina',
          'urea', 'nefron', 'filtrado glomerular', 'acido urico']),
        ('reproductor', 'Sistema Reproductor',
         ['reproductor', 'ginecologico', 'prostata', 'ovarios',
          'testiculos', 'utero', 'endometrio', 'menstrual',
          'fertilidad', 'libido']),
        ('musculoesqueletico', 'Sistema Musculoesquelético',
         ['musculoesqueletico', 'oseo', 'huesos', 'articulaciones',
          'columna', 'densidad osea', 'lumbar', 'cervical', 'dorsal',
          'sacro', 'rodilla', 'cadera', 'hombro', 'tendon', 'tendinitis',
          'osteoporosis', 'osteopenia', 'calcio osea', 'colageno tipo i']),
        ('tegumentario', 'Sistema Tegumentario',
         ['tegumentario', 'piel', 'cabello', 'uñas', 'dermatologico',
          'colageno', 'humectacion cutanea', 'hidratacion cutanea',
          'dermis', 'epidermis', 'sebo', 'sebaceo', 'arruga', 'arrugas',
          'bolsas en ojos', 'manchas cutaneas', 'celulitis',
          'elasticidad cutanea', 'flacidez', 'estria']),
        ('sensorial', 'Sistema Sensorial',
         ['sensorial', 'vision', 'visual', 'ocular', 'audicion', 'oido',
          'equilibrio', 'olfato', 'gusto', 'ojo', 'ojos',
          'celular del ojo', 'celulas oculares', 'retina', 'retiniano',
          'cornea', 'cristalino', 'macula', 'agudeza visual',
          'nervio optico', 'lacrimal', 'pupila', 'iris', 'tímpano',
          'coclea', 'auditivo', 'audicion alterada']),
        # Toxicidad / Metales pesados (Fase 2.4)
        ('toxicidad', 'Toxicidad / Metales pesados',
         ['aluminio', 'arsenico', 'mercurio', 'plomo', 'cadmio', 'niquel',
          'manganeso', 'cromo', 'antimonio', 'bario', 'berilio', 'talio',
          'cobre', 'zinc', 'metales pesados', 'metal pesado', 'toxicidad',
          'toxico', 'carga toxica', 'intoxicacion', 'contaminantes',
          'xenobioticos', 'exposicion ambiental', 'biotransformacion',
          'desintoxicacion', 'detox hepatico']),
        # Metabólico / Nutricional (Fase 2.5 — NUEVO)
        # Captura macronutrientes, perfil lipídico, glucemia y vitaminas.
        # Triglicéridos/HDL/LDL/glucosa van aquí por decisión clínica:
        # el caso suele apoyarse con productos metabólicos antes que CV.
        ('metabolico', 'Sistema Metabólico / Nutricional',
         ['metabolico', 'metabolismo', 'metabolica', 'nutricional',
          'nutricion', 'aminoacido', 'aminoacidos', 'proteina', 'proteinas',
          'carbohidrato', 'carbohidratos', 'lipido', 'lipidos', 'lipidico',
          'perfil lipidico', 'glucido', 'glucidos', 'vitamina',
          'mineral nutricional', 'balance nutricional', 'sintesis proteica',
          'masa magra', 'masa muscular', 'grasa corporal',
          'indice metabolico', 'hdl', 'ldl', 'vldl', 'lipoproteina',
          'trigliceridos', 'triglicerido', 'colesterol total', 'colesterol',
          'glucosa', 'glucemia', 'hba1c', 'glicada', 'hemoglobina glicada',
          'acido folico', 'b12', 'vitamina d', 'vitamina e', 'vitamina c',
          'omega 3', 'omega 6']),
    )

    _ESTADOS_FALLBACK = {
        # raw_label normalizado -> (normalized, severity)
        'optimo': ('optimo', 0), 'excelente': ('optimo', 0),
        'normal': ('normal', 1), 'equilibrado': ('normal', 1),
        'sano': ('normal', 1), 'dentro de rango': ('normal', 1),
        'leve': ('leve', 2), 'levemente alterado': ('leve', 2),
        'ligero': ('leve', 2),
        'moderado': ('moderado', 3), 'moderadamente alterado': ('moderado', 3),
        'severo': ('severo', 4), 'severamente alterado': ('severo', 4),
        'alterado': ('severo', 4),
        'critico': ('critico', 5), 'grave': ('critico', 5),
    }

    @classmethod
    def _load_sistemas_catalog(cls, env):
        """Devuelve list of (code, label, [keywords_normalizados])."""
        if env is None:
            return [
                (c, lbl, [cls._normalize(k) for k in kws])
                for c, lbl, kws in cls._SISTEMAS_FALLBACK
            ]
        out = []
        try:
            sistemas = env['valoracion.biocuantico.sistema'].sudo().search(
                [('active', '=', True)], order='sequence, id',
            )
            for s in sistemas:
                kws = s._get_keywords_list() or []
                # Si no hay keywords configurados, usar el nombre + código
                if not kws:
                    kws = [s.name or '', s.code or '']
                kws_norm = [cls._normalize(k) for k in kws if k]
                # Añadir code y label como keywords adicionales para robustez
                kws_norm.extend([cls._normalize(s.code or ''),
                                 cls._normalize(s.name or '')])
                kws_norm = [k for k in kws_norm if k]
                out.append((s.code, s.name, list(set(kws_norm))))
            return out
        except Exception:
            _logger.exception("BioCuántico: no se pudo cargar catálogo sistemas, usando fallback")
            return [
                (c, lbl, [cls._normalize(k) for k in kws])
                for c, lbl, kws in cls._SISTEMAS_FALLBACK
            ]

    @classmethod
    def _build_estados_lookup(cls, env):
        """Devuelve dict {raw_label_normalizado: (normalized, severity)}."""
        if env is None:
            return dict(cls._ESTADOS_FALLBACK)
        try:
            Mapeo = env['valoracion.biocuantico.estado.mapeo'].sudo()
            mapeos = Mapeo.search([('active', '=', True)])
            out = dict(cls._ESTADOS_FALLBACK)  # base + override
            for m in mapeos:
                key = Mapeo._normalize_label(m.raw_label or '')
                if key:
                    out[key] = (m.normalized, m.severity)
            return out
        except Exception:
            _logger.exception("BioCuántico: no se pudo cargar mapeos estado, usando fallback")
            return dict(cls._ESTADOS_FALLBACK)

    # ===================================================================
    # Clasificación de filas
    # ===================================================================
    @classmethod
    def _classify_sistema_detail(cls, normalized_cells, sistemas_catalog,
                                  current_sistema, parametro=None):
        """Versión enriquecida del clasificador (Fase 2.6+ — auditoría).

        Devuelve siempre un dict con:
            sistema_code (str|None)
            sistema_label (str|None)
            sistema_source: 'critical_override' | 'keyword' |
                            'carry_forward' | 'fallback'
            keyword_matched: str|None  (kw del catálogo o key del override)

        Comportamiento (en orden de PRIORIDAD):
          0) Fase 2.9 — Si `parametro` (normalizado) está en
             CRITICAL_PARAM_OVERRIDES, gana TODO. source='critical_override'.
          1) Escanea TODAS las kw de TODOS los sistemas y elige el match con
             la kw más larga (longest-match GLOBAL — sin break per-sistema).
          2) Si hay match → source='keyword'.
          3) Si no hay match pero current_sistema existe → source='carry_forward'.
          4) Si tampoco hay carry-forward → source='fallback', codes None.

        REGLA CRÍTICA: si una keyword específica matcheó (o el override de
        Fase 2.9), NUNCA se usa carry-forward, incluso si el carry-forward
        apuntaba al "mismo" sistema. Esto facilita auditar por qué cada
        hallazgo cayó en su sistema.

        Fase 2.7 — el separador entre celdas es espacio SIMPLE (no ' | '),
        porque pdfplumber a veces parte el nombre del parámetro en dos
        celdas (ej. "Actividad celular" | "del ojo"). Con ' | ' como
        separador, la keyword 'celular del ojo' no matcheaba; con espacio
        simple sí.
        """
        non_empty = [c for c in normalized_cells if c]
        joined = ' '.join(non_empty)

        # 'carry_forward_was' siempre se reporta para auditoría: muestra
        # qué sistema HUBIERA ganado por carry-forward incluso cuando la
        # keyword (o el override) ganó.
        carry_forward_was = None
        if isinstance(current_sistema, tuple) and len(current_sistema) >= 2:
            carry_forward_was = current_sistema[0]

        # === Paso 0: CRITICAL_PARAM_OVERRIDES (Fase 2.9) ===
        # Prioridad MÁXIMA. Match exacto del parametro_normalizado contra
        # la tabla de overrides. Gana sobre keyword genérica, carry_forward
        # y context. Útil para parámetros cortos o frases con artículo que
        # matcheaban débilmente.
        if parametro:
            param_norm = cls._normalize(parametro)
            if param_norm and param_norm in cls.CRITICAL_PARAM_OVERRIDES:
                code = cls.CRITICAL_PARAM_OVERRIDES[param_norm]
                label = cls._SISTEMA_LABELS.get(code, code)
                return {
                    'sistema_code': code,
                    'sistema_label': label,
                    'sistema_source': 'critical_override',
                    'keyword_matched': param_norm,
                    'carry_forward_was': carry_forward_was,
                }

        best = None  # (len_kw, code, label, kw_match)
        for code, label, keywords in sistemas_catalog:
            for kw in keywords:
                if kw and kw in joined:
                    if best is None or len(kw) > best[0]:
                        best = (len(kw), code, label, kw)
                    # NO break — seguimos buscando kw más largas.

        if best:
            return {
                'sistema_code': best[1],
                'sistema_label': best[2],
                'sistema_source': 'keyword',
                'keyword_matched': best[3],
                'carry_forward_was': carry_forward_was,
            }
        # No matchea ninguna kw — usar carry-forward si existe
        if isinstance(current_sistema, tuple) and len(current_sistema) >= 2:
            return {
                'sistema_code': current_sistema[0],
                'sistema_label': current_sistema[1],
                'sistema_source': 'carry_forward',
                'keyword_matched': None,
                'carry_forward_was': carry_forward_was,
            }
        return {
            'sistema_code': None,
            'sistema_label': None,
            'sistema_source': 'fallback',
            'keyword_matched': None,
            'carry_forward_was': carry_forward_was,
        }

    @classmethod
    def _classify_sistema(cls, normalized_cells, sistemas_catalog, current_sistema):
        """Wrapper compat: devuelve sólo (code, label) o el carry-forward.

        Para auditoría detallada usar _classify_sistema_detail.
        """
        d = cls._classify_sistema_detail(normalized_cells, sistemas_catalog,
                                          current_sistema)
        if d['sistema_code']:
            return (d['sistema_code'], d['sistema_label'])
        return current_sistema

    @classmethod
    def _extract_estado(cls, normalized_cells, estados_lookup):
        """Busca el estado en las celdas. Devuelve (estado_raw, normalized, severity)
        o (None, None, None).

        Estrategia (en este orden):
          1. Match exacto de celda completa, recorriendo de DERECHA a IZQUIERDA
             — en reportes BC la columna 'Estado' suele ser la última.
          2. Match parcial (substring), también de derecha a izquierda.

        Sesgo: una celda que sea 'normal' compitiendo con otra que sea
        'moderado' pierde, porque 'moderado' es más informativo y está en
        una posición más a la derecha que 'normal' cuando la columna rango
        viene antes que estado.
        """
        cells_rev = list(reversed(normalized_cells))
        # 1) Exact match (derecha → izquierda)
        for cell in cells_rev:
            cell_strip = cell.strip()
            if not cell_strip:
                continue
            if cell_strip in estados_lookup:
                norm, sev = estados_lookup[cell_strip]
                return cell_strip, norm, sev
        # 2) Match parcial (derecha → izquierda), preferir matches "más severos"
        best = None  # (severity, raw, norm)
        for cell in cells_rev:
            for key, (norm, sev) in estados_lookup.items():
                if key and key in cell:
                    if best is None or sev > best[0]:
                        best = (sev, key, norm)
        if best:
            return best[1], best[2], best[0]
        return None, None, None

    # -------------------------------------------------------------------
    # Helpers numéricos (Fase 2.2 — inferencia por valor vs rango)
    # -------------------------------------------------------------------
    @classmethod
    def _to_float(cls, s):
        """Convierte un string a float aceptando coma decimal y punto decimal.

        Reglas:
          * Sólo coma → coma es decimal (estilo MX/ES): "1,329" → 1.329
          * Sólo punto → punto es decimal: "1.329" → 1.329
          * Punto y coma → coma se asume separador de miles: "1,329.50" → 1329.50
          * Sólo dígitos → entero: "150" → 150.0
          * Si hay texto adicional ("alto", "positivo"), devuelve None.

        Para celdas tipo "150/95" (presión arterial) toma el primer número.
        """
        if s is None:
            return None
        text = str(s).strip()
        if not text:
            return None
        # Buscar el primer trozo numérico (admite signo, dígitos, . y ,)
        m = re.search(r'-?\d[\d.,]*', text)
        if not m:
            return None
        num = m.group(0).strip().strip('.,')
        if not num:
            return None
        has_dot = '.' in num
        has_comma = ',' in num
        if has_dot and has_comma:
            # Coma = miles, punto = decimal
            num = num.replace(',', '')
        elif has_comma and not has_dot:
            # Coma decimal estilo MX/ES
            num = num.replace(',', '.')
        try:
            return float(num)
        except (ValueError, TypeError):
            return None

    @classmethod
    def _parse_range_bounds(cls, s):
        """Devuelve (lo, hi) con None en bordes no acotados.

        Soporta:
          * "0,431 - 1,329", "0.431 - 1.329"
          * "70-110", "70 - 110", "70 a 110", "70 to 110"
          * "<150", "<= 150", "≤ 150"
          * ">30", ">= 30", "≥ 30"

        Devuelve None si no puede interpretar.
        """
        if s is None:
            return None
        text = str(s).strip()
        if not text:
            return None
        # Normalizar ≤/≥ y =< => para que regex únicos cubran todo
        t = (text
             .replace('≤', '<=')
             .replace('≥', '>=')
             .replace('=<', '<=')
             .replace('=>', '>='))

        # Acotado superior: < X o <= X
        m = cls._RE_BOUND_LT.match(t)
        if m:
            hi = cls._to_float(m.group(1))
            return (None, hi) if hi is not None else None

        # Acotado inferior: > X o >= X
        m = cls._RE_BOUND_GT.match(t)
        if m:
            lo = cls._to_float(m.group(1))
            return (lo, None) if lo is not None else None

        # Rango bilateral
        m = cls._RE_BOUND_RANGE.search(t)
        if m:
            lo = cls._to_float(m.group(1))
            hi = cls._to_float(m.group(2))
            if lo is not None and hi is not None:
                if lo > hi:
                    lo, hi = hi, lo
                return (lo, hi)
        return None

    @classmethod
    def _classify_by_numeric_range(cls, valor_str, rango_str):
        """Compara un valor numérico contra un rango y devuelve
        (estado_norm, severity) o (None, None) si no puede comparar.

        Reglas Fase 2.4 (suavizadas):
          * Dentro del rango → ('normal', 1).
          * Fuera: usar el MENOR entre:
              - pct_bound:   |val - borde_violado| / |borde_violado|
              - pct_central: |val - borde_violado| / |(lo+hi)/2|
                             (sólo si el rango tiene dos bordes)
            Esto evita "severo automático" cuando el borde es chico
            o el rango es estrecho — el desvío en términos de la
            posición central del rango compensa el cálculo por borde.
          * Para rangos de un solo borde (<X o >X), se usa pct_bound
            directamente.
          * ≤ 10 % → leve; ≤ 30 % → moderado; > 30 % → severo.
        """
        val = cls._to_float(valor_str)
        if val is None:
            return None, None
        bounds = cls._parse_range_bounds(rango_str)
        if not bounds:
            return None, None
        lo, hi = bounds
        if lo is None and hi is None:
            return None, None

        violated_bound = None
        if lo is not None and val < lo:
            violated_bound = lo
        elif hi is not None and val > hi:
            violated_bound = hi
        else:
            return 'normal', 1  # in range

        abs_dev = abs(val - violated_bound)
        ref_bound = abs(violated_bound) if violated_bound != 0 else (
            abs(val) if val != 0 else 1.0
        )
        pct_bound = abs_dev / ref_bound

        # Suavizado: si hay dos bordes, usar también pct_central
        if lo is not None and hi is not None:
            center = (lo + hi) / 2.0
            ref_center = abs(center) if center != 0 else ref_bound
            pct_central = abs_dev / ref_center if ref_center > 0 else pct_bound
            # Conservador: usar el menor
            effective_pct = min(pct_bound, pct_central)
        else:
            effective_pct = pct_bound

        if effective_pct <= cls.NUMERIC_THRESHOLD_LEVE:
            return 'leve', 2
        if effective_pct <= cls.NUMERIC_THRESHOLD_MODERADO:
            return 'moderado', 3
        return 'severo', 4

    @classmethod
    def _is_low_relevance(cls, parametro):
        """True si el parámetro coincide con un patrón de baja relevancia
        clínica. Comparación normalizada (lower + sin acentos).
        """
        if not parametro:
            return False
        norm = cls._normalize(parametro)
        for pat in cls.LOW_RELEVANCE_PATTERNS:
            if pat in norm:
                return True
        return False

    @classmethod
    def _is_noise_parametro(cls, parametro):
        """True si el parámetro es ruido no-clínico (texto de layout,
        ilustraciones, encabezados, etc.). Fase 2.8.

        Comparación normalizada (lower + sin acentos). Si el parametro
        ES EXACTAMENTE el ruido (o lo CONTIENE como substring), True.

        Cuidado: 'figura' es bastante genérico, así que el parametro
        debe ser corto y dominado por el ruido. Para evitar falsos
        positivos en parámetros legítimos largos que pueden incluir
        'figura' como palabra (raro pero posible), exigimos que el ruido
        sea la palabra principal o dominante.
        """
        if not parametro:
            return False
        norm = cls._normalize(parametro)
        if not norm:
            return False
        # Match estricto: el parametro DEBE empezar con el ruido, ser
        # exactamente el ruido, o ser muy corto y contenerlo.
        # Esto evita que un parámetro clínico que casualmente mencione
        # "figura" como adjetivo (raro) se descarte.
        for pat in cls.NOISE_PATTERNS:
            if norm == pat:
                return True
            if norm.startswith(pat + ' ') or norm.startswith(pat + ':'):
                return True
            # Si el parametro es corto (< 25 chars) y contiene el ruido
            if len(norm) < 25 and pat in norm:
                return True
        return False

    @classmethod
    def _resolve_ambiguous_parametro(cls, parametro, context_text):
        """Si el parámetro está en AMBIGUOUS_PATTERNS, resuelve por contexto.

        Args:
            parametro: nombre del parámetro (raw)
            context_text: texto del contexto (ej. todas las celdas
                normalizadas de la fila, o de la tabla, unidas en una
                sola cadena ya normalizada)

        Returns:
            (sistema_code, is_ambiguous):
                - (sistema_code, True): resolvió por contexto. Usar este sistema.
                - (None, True): es ambiguo PERO no hay contexto resolutorio.
                  El caller debe marcar la fila para descarte
                  (motivo='ambiguo_sin_contexto') porque NO debe heredar
                  carry-forward de otro sistema (ej. endocrino).
                - (None, False): no es ambiguo. Continuar flujo normal.
        """
        if not parametro:
            return None, False
        norm = cls._normalize(parametro)
        if not norm:
            return None, False
        # Match parametro contra ambiguous patterns (normalizado, exact o startswith)
        for amb_param, sis_map in cls.AMBIGUOUS_PATTERNS.items():
            if norm == amb_param or norm.startswith(amb_param + ' '):
                # Buscar el primer sistema cuyo contexto matchee
                for sis_code, ctx_words in sis_map.items():
                    for w in ctx_words:
                        if w in context_text:
                            return sis_code, True
                # Ambiguo sin contexto resolutorio
                # Fase 2.10: NO debe heredar carry-forward.
                # Caller responsable de descartar la fila.
                return None, True
        return None, False

    @classmethod
    def _downgrade_low_relevance(cls, estado_norm, severity, parametro):
        """Si el parámetro es de baja relevancia, baja UN nivel su severidad.

        Mapping: severo(4) → moderado(3), moderado(3) → leve(2),
                 leve(2) → normal(1), normal(1) → normal(1).
        Marca el cambio devolviendo tupla (estado_norm, severity, downgraded_bool).
        """
        if not cls._is_low_relevance(parametro):
            return estado_norm, severity, False
        downgrade_map = {
            4: ('moderado', 3),
            3: ('leve', 2),
            2: ('normal', 1),
        }
        if severity in downgrade_map:
            new_norm, new_sev = downgrade_map[severity]
            return new_norm, new_sev, True
        return estado_norm, severity, False

    @classmethod
    def _looks_like_value_cell(cls, cell):
        """True si la celda parece un valor numérico (no un nombre de parámetro
        con dígito embebido como 'T4 libre', 'Vit B12', 'Omega-3').

        Criterio: la celda, tras strip(), debe empezar por dígito, signo o
        separador decimal, Y debe ser convertible a float.
        """
        if not cell:
            return False
        s = cell.strip()
        if not s:
            return False
        first = s[0]
        if not (first.isdigit() or first in '-+.,'):
            return False
        return cls._to_float(s) is not None

    @classmethod
    def _extract_valor_rango(cls, normalized_cells):
        """Heurística: la celda con un rango (ej. '70-110') es el rango.
        La celda con un número solo es el valor.
        Devuelve (valor_str, rango_str) — pueden ser None.

        Nota: una celda solo se acepta como `valor` si parece numérica
        de verdad (empieza por dígito/signo). Esto evita que parámetros
        con dígito embebido (T4, B12, Omega-3) terminen como `valor`.
        """
        valor, rango = None, None
        for cell in normalized_cells:
            if not cell:
                continue
            mr = cls._RE_RANGE.search(cell)
            if mr and rango is None:
                rango = cell.strip()
                continue
            if valor is None and cls._looks_like_value_cell(cell):
                # Evitar que tomemos como valor algo que también es rango
                if mr is None:
                    valor = cell.strip()
        return valor, rango

    @classmethod
    def _looks_like_header(cls, normalized_cells):
        """Detecta filas-encabezado típicas para descartarlas."""
        joined = ' '.join(normalized_cells)
        header_words = ('item', 'parametro', 'analisis', 'prueba', 'valor',
                        'rango', 'referencia', 'estado', 'resultado',
                        'sistema', 'indicador')
        hits = sum(1 for w in header_words if w in joined)
        return hits >= 2

    @classmethod
    def _row_is_useful(cls, cells):
        """Una fila es útil si tiene >=2 celdas con texto no trivial."""
        non_empty = [c for c in cells if c and len(c.strip()) >= 2]
        return len(non_empty) >= 2

    @classmethod
    def _extract_parametro(cls, raw_cells, normalized_cells, estado_raw,
                           valor_str, rango_str):
        """El 'parámetro' es la primera celda con texto que NO es estado/valor/rango."""
        for raw, norm in zip(raw_cells, normalized_cells):
            if not raw or not raw.strip():
                continue
            sn = norm.strip()
            if estado_raw and sn == estado_raw:
                continue
            if valor_str and norm.strip() == cls._normalize(valor_str):
                continue
            if rango_str and norm.strip() == cls._normalize(rango_str):
                continue
            # Saltar celdas que son solo número (probable valor)
            if cls._RE_NUMBER.fullmatch(sn.replace(' ', '')):
                continue
            return raw.strip()
        return None

    @classmethod
    def _is_sistema_header_row(cls, cells_norm, sistemas_catalog):
        """Detecta filas-encabezado tipo ['Sistema Digestivo', '', '', ''] que
        anuncian la sección pero no contienen datos. Devuelve (code, label)
        o None.

        Criterio: una celda menciona un sistema (vía keywords) y a lo más
        otra celda tiene contenido trivial.

        Fase 2.6 — usa longest-match igual que _classify_sistema para evitar
        que un header "Sistema Cardiovascular" caiga accidentalmente en
        otro sistema por kw genérica.
        """
        non_empty_cells = [c for c in cells_norm if c and c.strip()]
        if len(non_empty_cells) > 2:
            return None
        joined = ' '.join(non_empty_cells)
        best = None  # (len_kw, code, label)
        for code, label, keywords in sistemas_catalog:
            for kw in keywords:
                if kw and kw in joined:
                    if best is None or len(kw) > best[0]:
                        best = (len(kw), code, label)
        if best:
            return best[1], best[2]
        return None

    # -------------------------------------------------------------------
    # Deduplicación de mediciones (Fase 2.2 + 2.7 cross-sistema)
    # -------------------------------------------------------------------
    @classmethod
    def _dedup_mediciones(cls, mediciones):
        """Tres pasadas de deduplicación.

        Devuelve (kept, descartados) donde descartados llevan
        'motivo_descarte' explicando por qué fueron filtrados.

        Pasada A — duplicado exacto (motivo='duplicado_exacto'):
          Misma (sistema, parametro_norm, valor, rango, estado_norm).

        Pasada B — duplicado mismo parámetro mismo sistema
        (motivo='duplicado_parametro_mismo_sistema'):
          Conservar la entrada con MÁS señal (severidad, source_estado,
          valor+rango). Resto descartado.

        Pasada C — CONFLICTO entre sistemas
        (motivo='duplicado_conflicto_sistema', Fase 2.7):
          Mismo parametro_normalizado en sistemas distintos.
          Resolución del conflicto:
            1) sistema_source='keyword' gana sobre 'carry_forward'
            2) si ambos keyword: kw_matched más larga gana
            3) si empate: mayor severidad
            4) si empate: primera aparición
          Las versiones perdedoras van a descartados.
        """
        if not mediciones:
            return [], []

        descartados = []

        # === Pasada A: exact dedup ===
        seen_exact = set()
        unique_exact = []
        for m in mediciones:
            key = (
                (m.get('sistema_code') or '').lower(),
                cls._normalize(m.get('parametro') or ''),
                cls._normalize(str(m.get('valor') or '')),
                cls._normalize(str(m.get('rango') or '')),
                m.get('estado_norm') or '',
            )
            if key in seen_exact:
                d = dict(m)
                d['motivo_descarte'] = 'duplicado_exacto'
                descartados.append(d)
                continue
            seen_exact.add(key)
            unique_exact.append(m)

        # === Pasada B: mismo (sistema, parámetro) conservando la mejor ===
        SEV_RANK = {5: 5, 4: 4, 3: 3, 2: 2, 1: 1, 9: 0, 0: 0, None: 0}
        SRC_RANK = {'lookup': 2, 'numeric': 2, 'none': 0, None: 0}

        def _quality_score(m):
            sev_score = SEV_RANK.get(int(m.get('severity') or 0), 0)
            has_data = (1 if (m.get('valor') and m.get('rango')) else 0)
            src_score = SRC_RANK.get(m.get('source_estado'), 0)
            return (sev_score, has_data, src_score)

        best_by_param_sis = {}    # (sistema_code, parametro_norm) → (idx, m)
        order = []                # preserva orden de primera aparición
        for idx, m in enumerate(unique_exact):
            param = cls._normalize(m.get('parametro') or '')
            sistema = (m.get('sistema_code') or '').lower()
            if not param:
                order.append(('keep', idx, m))
                continue
            key = (sistema, param)
            existing = best_by_param_sis.get(key)
            if existing is None:
                best_by_param_sis[key] = (idx, m)
                order.append(('grp_sis_param', key))
            else:
                if _quality_score(m) > _quality_score(existing[1]):
                    # Nuevo gana, viejo a descartados
                    d = dict(existing[1])
                    d['motivo_descarte'] = 'duplicado_parametro_mismo_sistema'
                    descartados.append(d)
                    best_by_param_sis[key] = (idx, m)
                else:
                    # Viejo gana
                    d = dict(m)
                    d['motivo_descarte'] = 'duplicado_parametro_mismo_sistema'
                    descartados.append(d)

        # Reconstruir lista tras Pasada B
        unique_b = []
        for entry in order:
            if entry[0] == 'keep':
                unique_b.append(entry[2])
            else:
                _, key = entry
                if key in best_by_param_sis:
                    unique_b.append(best_by_param_sis[key][1])

        # === Pasada C: CROSS-SISTEMA conflict resolution (Fase 2.7) ===
        # Agrupa por parametro_normalizado, sin importar sistema_code.
        # Fase 2.7+: además de match exacto, agrupa FRAGMENTOS:
        # si "actividad celular" (carry_forward) y "actividad celular del
        # ojo" (keyword) coexisten, el corto es un fragmento extraído mal
        # del PDF y se agrupa con el largo (el de keyword gana).
        SOURCE_RANK = {'keyword': 2, 'carry_forward': 1, 'fallback': 0,
                       None: 0, '': 0}

        # Construir set de "parámetros canónicos" (los que tienen al menos
        # un sistema_source='keyword' — texto extraído correctamente).
        canonical_params = set()
        for m in unique_b:
            if m.get('sistema_source') == 'keyword':
                pn = cls._normalize(m.get('parametro') or '')
                if pn:
                    canonical_params.add(pn)

        def _canonical_key(m):
            """Resuelve la clave de agrupación para Pasada C, expandiendo
            fragmentos a su versión canónica si existe."""
            pn = cls._normalize(m.get('parametro') or '')
            if not pn:
                return None
            # Match exacto a un canónico
            if pn in canonical_params:
                return pn
            # Es prefijo de algún canónico → usar el canónico (versión completa)
            for cp in canonical_params:
                if cp.startswith(pn + ' '):
                    return cp
            # Es sufijo/contiene-canónico al revés? (caso muy raro, ignorar)
            return pn

        def _resolution_score(m, order_idx):
            """Tupla mayor → más prioritario."""
            return (
                # 1) sistema_source: keyword > carry_forward
                SOURCE_RANK.get(m.get('sistema_source'), 0),
                # 2) kw_matched más larga
                len(m.get('keyword_matched') or ''),
                # 3) mayor severidad (excluyendo 9=desconocido)
                SEV_RANK.get(int(m.get('severity') or 0), 0),
                # 4) menor índice (primera aparición) → negativo
                -order_idx,
            )

        best_by_param = {}    # canonical_key → (idx, m)
        order_c = []
        for idx, m in enumerate(unique_b):
            key = _canonical_key(m)
            if not key:
                order_c.append(('keep', m))
                continue
            existing = best_by_param.get(key)
            if existing is None:
                best_by_param[key] = (idx, m)
                order_c.append(('grp', key))
            else:
                # Resolver conflicto
                if _resolution_score(m, idx) > _resolution_score(existing[1],
                                                                  existing[0]):
                    # Nuevo gana → viejo a descartados
                    d = dict(existing[1])
                    d['motivo_descarte'] = 'duplicado_conflicto_sistema'
                    d['ganador_sistema_code'] = m.get('sistema_code')
                    d['ganador_sistema_source'] = m.get('sistema_source')
                    descartados.append(d)
                    best_by_param[key] = (idx, m)
                else:
                    # Viejo gana → nuevo a descartados
                    d = dict(m)
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
                if key in best_by_param:
                    out.append(best_by_param[key][1])

        return out, descartados

    @classmethod
    def _mediciones_from_tables(cls, tables_meta, sistemas_catalog, estados_lookup,
                                stats):
        """Itera tablas con metadata (Fase 2.7).

        Cada elemento de tables_meta es un dict {pagina, tabla_idx, rows}.
        Se acepta también el formato legacy (lista de listas de filas)
        para back-compat con tests viejos.
        """
        out = []
        # IMPORTANTE: carry-forward de sistema PERSISTE entre tablas.
        current_sistema = None
        current_sistema_label = None
        for tbl_meta in tables_meta:
            # Acepto dos formatos
            if isinstance(tbl_meta, dict):
                pagina = tbl_meta.get('pagina', 0)
                tabla_idx = tbl_meta.get('tabla_idx', 0)
                rows_iter = tbl_meta.get('rows', [])
            else:
                # Legacy: la tabla es directamente una lista de filas
                pagina = 0
                tabla_idx = 0
                rows_iter = tbl_meta

            # Fase 2.8: contexto agregado de la tabla, para resolver
            # parámetros AMBIGUOS (ej. "Complacencia" en proximidad a
            # FRC/pulmón → respiratorio).
            table_context_norm = ' '.join(
                cls._normalize(' '.join(str(c) for c in row if c))
                for row in rows_iter
            )

            for row_idx, row in enumerate(rows_iter):
                cells_raw = [str(c) if c is not None else '' for c in row]
                cells_norm = [cls._normalize(c) for c in cells_raw]

                # 1) Fila-encabezado de sección "Sistema X" — actualiza
                #    carry-forward y se descarta como dato.
                section = cls._is_sistema_header_row(cells_norm, sistemas_catalog)
                if section:
                    current_sistema, current_sistema_label = section
                    stats['filas_descartadas'] += 1
                    continue

                if not cls._row_is_useful(cells_norm):
                    stats['filas_descartadas'] += 1
                    continue
                if cls._looks_like_header(cells_norm):
                    stats['filas_descartadas'] += 1
                    continue

                # 2) Extraer estado/valor/rango/parametro PRIMERO. Fase 2.9
                #    necesita el `parametro` extraído para consultar
                #    CRITICAL_PARAM_OVERRIDES antes de classify.
                estado_raw, estado_norm, sev = cls._extract_estado(
                    cells_norm, estados_lookup,
                )
                valor, rango = cls._extract_valor_rango(cells_norm)
                parametro = cls._extract_parametro(
                    cells_raw, cells_norm, estado_raw, valor, rango,
                )

                # 3) Resolver sistema con AUDITORÍA. Orden de prioridad
                #    (de mayor a menor):
                #      a) CRITICAL_PARAM_OVERRIDES (Fase 2.9)
                #      b) longest-match contra catálogo (Fase 2.5)
                #      c) carry_forward de la sección (Fase 2.6)
                #      d) fallback (None)
                #
                #    REGLA CRÍTICA: si override o kw específica matcheó,
                #    NUNCA se usa carry-forward.
                cf_arg = (current_sistema, current_sistema_label) if current_sistema else None
                row_detail = cls._classify_sistema_detail(
                    cells_norm, sistemas_catalog, cf_arg, parametro=parametro,
                )
                sistema_code = row_detail['sistema_code']
                sistema_label = row_detail['sistema_label']
                sistema_source = row_detail['sistema_source']
                keyword_matched = row_detail['keyword_matched']
                carry_forward_was = row_detail.get('carry_forward_was')

                # Fase 2.8 — Filtro de RUIDO NO-CLÍNICO.
                # Marca la medición con is_noise=True; el dedup la mandará
                # a parametros_descartados con motivo='ruido_no_clinico'.
                # Se conserva la fila para auditoría, no se descarta aquí.
                is_noise = cls._is_noise_parametro(parametro)

                # Fase 2.8/2.10 — Resolución de parámetros AMBIGUOS por contexto.
                # Ejemplo: "Complacencia" sin contexto → no debe heredar
                # carry-forward endocrino. Si la TABLA tiene contexto
                # pulmonar (FRC, pulmón, alvéolo) → respiratorio.
                # NO se aplica si critical_override ya ganó (Fase 2.9).
                if (parametro and not is_noise
                        and sistema_source != 'critical_override'):
                    ctx_sistema, is_ambiguous = cls._resolve_ambiguous_parametro(
                        parametro, table_context_norm,
                    )
                    if ctx_sistema:
                        # Resolvió por contexto: cambiar sistema
                        new_label = None
                        for code, label, _kws in sistemas_catalog:
                            if code == ctx_sistema:
                                new_label = label
                                break
                        if new_label:
                            sistema_code = ctx_sistema
                            sistema_label = new_label
                            sistema_source = 'context_resolved'
                            keyword_matched = None
                    elif is_ambiguous:
                        # Fase 2.10: parámetro ambiguo SIN contexto resolutorio.
                        # NO debe heredar carry-forward (endocrino, etc.) —
                        # mejor marcarlo para descarte con motivo claro.
                        is_noise = True   # reutilizamos el flujo noise
                        # Sobreescribimos sistema_original con lo que
                        # HUBIERA tenido (carry_forward) para auditoría
                        sistema_source = 'ambiguo_sin_contexto'

                # Inferencia numérica: si no hay estado explícito (o quedó
                # 'desconocido') pero sí valor + rango, comparar para asignar
                # severidad. Esto cubre reportes BC que tabulan
                # parámetro / valor / rango sin columna "Estado".
                source_estado = 'lookup' if estado_norm else None
                if (estado_norm is None
                        or estado_norm == 'desconocido') and valor and rango:
                    inf_norm, inf_sev = cls._classify_by_numeric_range(valor, rango)
                    if inf_norm is not None:
                        estado_norm = inf_norm
                        sev = inf_sev
                        source_estado = 'numeric'
                        if not estado_raw:
                            estado_raw = '(inferido por valor/rango)'

                # Downgrade Fase 2.4: parámetros de baja relevancia clínica
                # bajan un nivel su severidad para no inflar prioridades.
                downgraded = False
                if estado_norm and parametro:
                    estado_norm, sev, downgraded = (
                        cls._downgrade_low_relevance(estado_norm, sev, parametro)
                    )

                # Fila aprovechable si tiene al menos parámetro o estado
                if not parametro and not estado_norm:
                    stats['filas_descartadas'] += 1
                    continue

                stats['filas_utiles'] += 1
                out.append({
                    'sistema_code': sistema_code,
                    'sistema_label': sistema_label,
                    'parametro': parametro,
                    'valor': valor,
                    'rango': rango,
                    'estado_raw': estado_raw,
                    'estado_norm': estado_norm or 'desconocido',
                    'severity': sev if sev is not None else 9,
                    'source': 'tabla',
                    'source_estado': source_estado or 'none',
                    'low_relevance': downgraded,
                    # Auditoría Fase 2.6/2.7/2.8
                    'sistema_source': sistema_source,
                    'keyword_matched': keyword_matched,
                    'parametro_normalizado': cls._normalize(parametro or ''),
                    'carry_forward_was': carry_forward_was,
                    'pagina': pagina,
                    'tabla_idx': tabla_idx,
                    'row_idx': row_idx,
                    'is_noise': is_noise,
                })
                if len(out) >= cls.MAX_USEFUL_ROWS:
                    return out
        return out

    @classmethod
    def _mediciones_from_text(cls, text, sistemas_catalog, estados_lookup,
                              stats):
        """Fallback textual: parsea línea por línea buscando patrones
        '<parámetro> ... <valor> ... <estado>'.
        """
        out = []
        if not text:
            return out
        # Cada línea es un candidato a fila
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        current_sistema = None
        current_sistema_label = None
        for line in lines:
            norm = cls._normalize(line)

            # ¿Es un encabezado de sección "Sistema X" o variación?
            section = cls._is_sistema_header_row([norm], sistemas_catalog)
            if section:
                current_sistema, current_sistema_label = section
                continue

            # Pseudo-fila: usar separadores típicos (tab, dos espacios, |, :, ;)
            parts = [p.strip() for p in re.split(r'\t|\s{2,}|\||:|;', line) if p.strip()]
            if len(parts) < 2:
                stats['filas_descartadas'] += 1
                continue
            cells_raw = parts
            cells_norm = [cls._normalize(c) for c in cells_raw]

            if not cls._row_is_useful(cells_norm):
                stats['filas_descartadas'] += 1
                continue
            if cls._looks_like_header(cells_norm):
                stats['filas_descartadas'] += 1
                continue

            # Fase 2.9: extraer parametro PRIMERO para que CRITICAL_PARAM_OVERRIDES
            # pueda consultarse al classify.
            estado_raw, estado_norm, sev = cls._extract_estado(cells_norm, estados_lookup)
            valor, rango = cls._extract_valor_rango(cells_norm)
            parametro = cls._extract_parametro(
                cells_raw, cells_norm, estado_raw, valor, rango,
            )

            # Fase 2.6+2.9: classify con auditoría + override crítico.
            # NO se actualiza current_sistema desde una fila de datos
            # (sólo headers lo hacen).
            cf_arg = (current_sistema, current_sistema_label) if current_sistema else None
            row_detail = cls._classify_sistema_detail(
                cells_norm, sistemas_catalog, cf_arg, parametro=parametro,
            )
            sistema_code = row_detail['sistema_code']
            sistema_label = row_detail['sistema_label']
            sistema_source = row_detail['sistema_source']
            keyword_matched = row_detail['keyword_matched']
            carry_forward_was = row_detail.get('carry_forward_was')

            # Inferencia numérica: idéntico al path tabular.
            source_estado = 'lookup' if estado_norm else None
            if (estado_norm is None
                    or estado_norm == 'desconocido') and valor and rango:
                inf_norm, inf_sev = cls._classify_by_numeric_range(valor, rango)
                if inf_norm is not None:
                    estado_norm = inf_norm
                    sev = inf_sev
                    source_estado = 'numeric'
                    if not estado_raw:
                        estado_raw = '(inferido por valor/rango)'

            # Downgrade Fase 2.4: parámetros de baja relevancia clínica
            downgraded = False
            if estado_norm and parametro:
                estado_norm, sev, downgraded = (
                    cls._downgrade_low_relevance(estado_norm, sev, parametro)
                )

            if not parametro and not estado_norm:
                stats['filas_descartadas'] += 1
                continue
            stats['filas_utiles'] += 1
            out.append({
                'sistema_code': sistema_code,
                'sistema_label': sistema_label,
                'parametro': parametro,
                'valor': valor,
                'rango': rango,
                'estado_raw': estado_raw,
                'estado_norm': estado_norm or 'desconocido',
                'severity': sev if sev is not None else 9,
                'source': 'texto',
                'source_estado': source_estado or 'none',
                'low_relevance': downgraded,
                # Auditoría Fase 2.6/2.7/2.8
                'sistema_source': sistema_source,
                'keyword_matched': keyword_matched,
                'parametro_normalizado': cls._normalize(parametro or ''),
                'carry_forward_was': carry_forward_was,
                'pagina': 0,        # texto plano: no hay paginación
                'tabla_idx': None,
                'row_idx': None,
                'is_noise': cls._is_noise_parametro(parametro),
            })
            if len(out) >= cls.MAX_USEFUL_ROWS:
                break
        return out

    # ===================================================================
    # Helper failed
    # ===================================================================
    @classmethod
    def _failed_payload(cls, stats, error=None, metodo='ninguno'):
        return {
            'is_biocuantico': True,  # detectado pero parser falló
            'status': 'failed',
            'sistemas': [],
            'mediciones': [],
            'parametros_anormales': [],
            'estadisticas': stats,
            'metodo': metodo,
            'parsed': False,
            'phase': '2.2',
            'error': error or 'unknown',
        }
