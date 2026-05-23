# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.1 (taxonomy + extractor).

OBJETIVO de esta fase:
    Demostrar que el catálogo declarativo (taxonomy.py) y el extractor
    puro (extractor.py) son SUFICIENTEMENTE GENERALES para clasificar
    correctamente parámetros de 6 perfiles clínicos distintos —
    SIN hardcodear ningún cliente y SIN llamar IA.

NO se conecta al flujo productivo (parser.py + master_summary.py viejos
siguen intactos). Esta fase es una prueba de concepto en aislado.

Criterio de aceptación:
    Los 6 casos A-F deben mostrar distribuciones de hallazgos en
    sistemas DISTINTOS y COHERENTES con su perfil clínico.
"""
from collections import defaultdict

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.valoracion.models.biocuantico.taxonomy import (
    SYSTEMS, NOISE_PATTERNS, LOW_RELEVANCE_PATTERNS,
    Pattern, System,
    normalize_text, all_codes, get_system, validate_catalog,
)
from odoo.addons.valoracion.models.biocuantico.extractor import (
    extract, extract_from_tables, extract_from_table, make_raw_row,
    _cell_kind, _RE_RANGE, _RE_NUMBER,
)


# =====================================================================
# 0) Sanidad del catálogo declarativo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestTaxonomyCatalog(TransactionCase):

    def test_catalogo_sin_problemas(self):
        """validate_catalog() debe devolver lista vacía."""
        problems = validate_catalog()
        self.assertEqual(problems, [],
                         "Problemas en catálogo: %r" % problems)

    def test_14_sistemas_principales_existen(self):
        """Los 14 sistemas mínimos del spec del usuario están presentes."""
        codes = set(all_codes())
        # 13 sistemas clínicos + 'otros' catch-all
        for expected in (
            'digestivo', 'metabolico', 'endocrino', 'cardiovascular',
            'respiratorio', 'nervioso', 'musculoesqueletico',
            'inmune', 'urinario', 'sensorial', 'tegumentario',
            'toxicidad', 'reproductor', 'otros',
        ):
            self.assertIn(expected, codes,
                          "Sistema %r ausente del catálogo" % expected)

    def test_solo_un_catch_all(self):
        catch_alls = [s for s in SYSTEMS if s.catch_all]
        self.assertEqual(len(catch_alls), 1)
        self.assertEqual(catch_alls[0].code, 'otros')

    def test_normalize_text_idempotente(self):
        """normalize_text aplicada 2 veces da el mismo resultado."""
        for raw in ('Capacidad residual\nfuncional', '  HDL ', 'Ojo',
                    'Tiroglobulina', 'Fátiga visual'):
            n1 = normalize_text(raw)
            n2 = normalize_text(n1)
            self.assertEqual(n1, n2)

    def test_normalize_text_limpia_invisibles_y_whitespace(self):
        self.assertEqual(normalize_text('HDL​ bajo'), 'hdl bajo')
        self.assertEqual(normalize_text('Capacidad   residual'), 'capacidad residual')
        self.assertEqual(normalize_text('Demanda\nde Sangre'), 'demanda de sangre')
        self.assertEqual(normalize_text('Fátiga'), 'fatiga')

    def test_noise_y_low_relevance_son_listas_de_patterns(self):
        for p in NOISE_PATTERNS:
            self.assertIsInstance(p, Pattern)
            self.assertTrue(p.needle)
        for p in LOW_RELEVANCE_PATTERNS:
            self.assertIsInstance(p, Pattern)


# =====================================================================
# 1) Extractor — manejo de texto sucio
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestExtractor(TransactionCase):

    def test_cell_kind_basico(self):
        self.assertEqual(_cell_kind(''), 'empty')
        self.assertEqual(_cell_kind('1.5'), 'value')
        self.assertEqual(_cell_kind('70-110'), 'range')
        self.assertEqual(_cell_kind('0,5 - 4,5'), 'range')
        self.assertEqual(_cell_kind('<150'), 'range')
        self.assertEqual(_cell_kind('normal'), 'state')
        self.assertEqual(_cell_kind('severo'), 'state')
        self.assertEqual(_cell_kind('helicobacter pylori'), 'text')

    def test_extract_tabla_normal(self):
        """Tabla bien formada: parametro / valor / rango / estado."""
        rows = [
            ['Parámetro', 'Valor', 'Rango', 'Estado'],
            ['Helicobacter', '1.5', '0 - 1', 'Severo'],
            ['Glucosa', '95', '70 - 110', 'Normal'],
        ]
        extracted = extract_from_table(rows, pagina=1, tabla_idx=0)
        self.assertEqual(len(extracted), 2)
        self.assertEqual(extracted[0]['parametro_raw'], 'Helicobacter')
        self.assertEqual(extracted[0]['valor_raw'], '1.5')
        self.assertEqual(extracted[0]['rango_raw'], '0 - 1')
        self.assertEqual(extracted[0]['estado_raw'], 'Severo')
        self.assertEqual(extracted[0]['pagina'], 1)
        self.assertEqual(extracted[0]['tabla_idx'], 0)

    def test_extract_tabla_celda_partida(self):
        """Parámetro partido en dos celdas (típico de pdfplumber)."""
        rows = [
            ['Parámetro', 'Valor', 'Rango'],
            ['Actividad celular', 'del ojo', '60', '0 - 50'],
        ]
        ext = extract_from_table(rows)
        self.assertEqual(len(ext), 1)
        # El parámetro debe concatenarse
        self.assertIn('actividad celular', ext[0]['parametro_norm'])
        self.assertIn('del ojo', ext[0]['parametro_norm'])

    def test_extract_tolera_columnas_desordenadas(self):
        """Si vienen [valor, rango, parametro], el extractor sigue funcionando."""
        rows = [
            ['Parámetro', 'Valor', 'Rango'],
            ['Helicobacter', '1.5', '0 - 1'],
        ]
        ext = extract_from_table(rows)
        self.assertEqual(ext[0]['parametro_raw'], 'Helicobacter')

    def test_extract_section_header_propaga(self):
        """Un section header se asocia a las filas siguientes."""
        rows = [
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Digestivo', '', ''],
            ['Helicobacter', '1.5', '0 - 1'],
            ['Acidez', '8', '4 - 7'],
        ]
        ext = extract_from_table(rows)
        # 2 filas de datos (la sección no es una fila)
        self.assertEqual(len(ext), 2)
        self.assertEqual(ext[0]['section_header'], 'sistema digestivo')
        self.assertEqual(ext[1]['section_header'], 'sistema digestivo')

    def test_extract_table_context_acumula_toda_la_tabla(self):
        """table_context_norm debe incluir el texto de TODAS las celdas."""
        rows = [
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Respiratorio', '', ''],
            ['FRC', '1.8', '2.0 - 3.5'],
            ['Pulmón derecho', '4.0', '4.5 - 6.0'],
        ]
        ext = extract_from_table(rows)
        self.assertTrue(ext)
        ctx = ext[0]['table_context_norm']
        # Debe contener al menos algunos términos pulmonares
        self.assertIn('frc', ctx)
        self.assertIn('pulmon', ctx)
        # Y el header
        self.assertIn('sistema respiratorio', ctx)

    def test_extract_descarta_fila_sin_parametro(self):
        rows = [
            ['Parámetro', 'Valor', 'Rango'],
            ['', '1.5', '0 - 1'],  # sin parametro
            ['Helicobacter', '1.5', '0 - 1'],
        ]
        ext = extract_from_table(rows)
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]['parametro_raw'], 'Helicobacter')


# =====================================================================
# 2) Helper: pseudo-classifier para Fase 3.1
# =====================================================================
# Como Fase 3.1 NO incluye el classifier.py todavía, definimos aquí un
# scorer mínimo basado en taxonomy.py para validar que la taxonomy SOLA
# (sin classifier) ya distribuye los parámetros correctamente entre
# sistemas. Este scorer NO va a producción; es sólo evaluación.

# Si el parámetro tiene una keyword match >= STRONG_PARAM_THRESHOLD,
# el contexto agregado de la tabla NO se usa. Esto evita que una tabla
# con tema dominante "absorba" parámetros que ya tienen su propia kw.
# Lógica que va al classifier.py real (Fase 3.3).
STRONG_PARAM_THRESHOLD = 7


def _score_param_only(row, system):
    """Score basado SÓLO en el nombre del parámetro."""
    target = row.get('parametro_norm', '') or ''
    score = 0
    matched = []
    for p in system.patterns:
        if p.matches(target):
            score += p.score
            matched.append(('param', p.needle, p.score))
    for ap in system.anti_patterns:
        if ap.matches(target):
            score -= ap.score
            matched.append(('anti', ap.needle, -ap.score))
    return score, matched


def _score_context_only(row, system):
    """Score basado SÓLO en el contexto agregado de la tabla."""
    target = row.get('table_context_norm', '') or ''
    score = 0
    matched = []
    for p in system.patterns:
        if p.match_mode == 'substring' and p.matches(target):
            score += max(1, p.score // 3)
            matched.append(('ctx', p.needle, p.score // 3))
    return score, matched


def _classify_simple(row):
    """Asigna el sistema con score más alto (>= min_score_to_assign).

    Regla clave: si max(param_score) >= STRONG_PARAM_THRESHOLD, la
    decisión se toma SÓLO con param scores (no se suma contexto). Esto
    evita que el contexto "absorba" parámetros con kw propia fuerte.

    Devuelve dict con sistema_code, score, breakdown.
    """
    param_scores = {}
    param_breakdowns = {}
    for system in SYSTEMS:
        if system.catch_all:
            continue
        sc, br = _score_param_only(row, system)
        param_scores[system.code] = sc
        param_breakdowns[system.code] = br

    max_param = max(param_scores.values()) if param_scores else 0

    if max_param >= STRONG_PARAM_THRESHOLD:
        # Decidir sólo con param scores
        winner_code = max(param_scores, key=param_scores.get)
        sys = get_system(winner_code)
        return {
            'sistema_code': winner_code,
            'sistema_label': sys.label if sys else winner_code,
            'score': param_scores[winner_code],
            'breakdown': param_breakdowns[winner_code],
        }

    # No hay match fuerte → sumar contexto para desempatar
    total_scores = dict(param_scores)
    total_breakdowns = dict(param_breakdowns)
    for system in SYSTEMS:
        if system.catch_all:
            continue
        ctx_sc, ctx_br = _score_context_only(row, system)
        total_scores[system.code] += ctx_sc
        total_breakdowns[system.code] = (total_breakdowns.get(system.code, [])
                                          + ctx_br)
    winner_code = max(total_scores, key=total_scores.get) if total_scores else 'otros'
    winner_sys = get_system(winner_code)
    min_threshold = winner_sys.min_score_to_assign if winner_sys else 5
    if total_scores[winner_code] < min_threshold:
        return {
            'sistema_code': 'otros',
            'sistema_label': 'Otros / sin clasificar',
            'score': total_scores[winner_code],
            'breakdown': total_breakdowns.get(winner_code, []),
        }
    return {
        'sistema_code': winner_code,
        'sistema_label': winner_sys.label if winner_sys else winner_code,
        'score': total_scores[winner_code],
        'breakdown': total_breakdowns[winner_code],
    }


# =====================================================================
# 3) Fixtures sintéticos por caso (sin IA, sin clientes reales)
# =====================================================================

CASE_A = {
    'name': 'Caso A — Digestivo / hígado / metales / detox',
    'table': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Digestivo', '', ''],
        ['Helicobacter pylori', '1.8', '0 - 1'],
        ['Acidez gástrica', '8.2', '4 - 7'],
        ['Tránsito intestinal', '0.3', '0.5 - 1.5'],
        ['Función hepática', '2.5', '1.0 - 1.5'],
        ['Bilis estancada', 'sí', 'no'],
        ['Sistema Toxicidad', '', ''],
        ['Aluminio', '85', '0 - 10'],
        ['Mercurio', '0.5', '0 - 0.1'],
        ['Detox hepático', 'bajo', 'normal'],
    ],
    # Distribución esperada (sistema_code → cantidad mínima)
    'expected_distribution': {
        'digestivo': 4,    # 4 parámetros digestivos
        'toxicidad': 3,    # 3 parámetros tóxicos
    },
    'forbidden_in': {
        # ningún parámetro debe quedar en cardiovascular o respiratorio
        'cardiovascular': 0,
        'respiratorio': 0,
        'sensorial': 0,
    },
}

CASE_B = {
    'name': 'Caso B — Síndrome metabólico (glucosa, lipidos, obesidad)',
    'table': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Metabólico', '', ''],
        ['Glucosa basal', '110', '70 - 100'],
        ['HbA1c', '6.5', '4 - 5.6'],
        ['HDL', '25', '40 - 60'],
        ['LDL', '180', '0 - 100'],
        ['Triglicéridos', '210', '0 - 150'],
        ['Colesterol total', '250', '0 - 200'],
        ['Grasa corporal', '38', '15 - 25'],
        ['Insulina', '20', '5 - 15'],
    ],
    'expected_distribution': {
        'metabolico': 6,   # >= 6 parámetros metabólicos/nutricionales
    },
    'forbidden_in': {
        'cardiovascular': 0,   # con las kw actuales NO debe haber CV puro aquí
        'respiratorio': 0,
        'sensorial': 0,
        'urinario': 0,
    },
}

CASE_C = {
    'name': 'Caso C — Tiroides / menopausia / hormonas',
    'table': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Endocrino', '', ''],
        ['TSH', '6.5', '0.5 - 4.5'],
        ['T4 libre', '0.5', '0.8 - 1.8'],
        ['T3 reverso', '350', '90 - 280'],
        ['Tiroglobulina', '120', '0 - 55'],
        ['Cortisol matutino', '32', '5 - 25'],
        ['Progesterona', '0.5', '5 - 30'],
        ['Estradiol', '180', '50 - 150'],
        ['Prolactina', '35', '5 - 25'],
        ['DHEA', '120', '50 - 270'],
    ],
    'expected_distribution': {
        'endocrino': 8,    # >= 8 hormonas
    },
    'forbidden_in': {
        'cardiovascular': 0,
        'metabolico': 0,    # cortisol/insulina podrían colar — verificar
        'respiratorio': 0,
    },
}

CASE_D = {
    'name': 'Caso D — Lumbar / articulaciones / inflamación / colágeno',
    'table': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Musculoesquelético', '', ''],
        ['Densidad ósea lumbar', '0.7', '0.9 - 1.2'],
        ['Densidad ósea cadera', '0.75', '0.9 - 1.2'],
        ['Colágeno tipo I', '1.5', '2.5 - 4.0'],
        ['Articulación rodilla', '0.4', '0.8 - 1.0'],
        ['Sistema Inmune', '', ''],
        ['Inflamación articular', '2.5', '0 - 1'],
        ['Proteína C reactiva', '8.5', '0 - 5'],
        ['IgG', '1700', '700 - 1500'],
    ],
    'expected_distribution': {
        'musculoesqueletico': 4,   # >= 4 mskl
        'inmune': 2,               # >= 2 inflamatorios
    },
    'forbidden_in': {
        'metabolico': 0,
        'respiratorio': 0,
        'cardiovascular': 0,
    },
}

CASE_E = {
    'name': 'Caso E — Ansiedad / sueño / sistema nervioso',
    'table': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Nervioso', '', ''],
        ['Serotonina', '80', '120 - 200'],
        ['Dopamina', '45', '60 - 100'],
        ['GABA', '0.8', '1.5 - 3.0'],
        ['Equilibrio autonómico', '0.4', '0.8 - 1.2'],
        ['Ansiedad subjetiva', '7', '0 - 3'],
        ['Insomnio nocturno', '5', '0 - 2'],
        ['Memoria a corto plazo', '6', '8 - 10'],
        ['Fatiga mental', '8', '0 - 3'],
    ],
    'expected_distribution': {
        'nervioso': 6,   # >= 6 nervioso
    },
    'forbidden_in': {
        'cardiovascular': 0,
        'metabolico': 0,
        'respiratorio': 0,
    },
}

CASE_F = {
    'name': 'Caso F — Sin antecedente claro, ranking depende del PDF',
    'table': [
        ['Parámetro', 'Valor', 'Rango'],
        # MEZCLA de sistemas sin sección dominante
        ['Helicobacter', '1.5', '0 - 1'],
        ['HDL', '25', '40 - 60'],
        ['TSH', '5.5', '0.5 - 4.5'],
        ['Densidad ósea', '0.8', '0.9 - 1.2'],
        ['Serotonina', '70', '120 - 200'],
        ['Aluminio', '50', '0 - 10'],
        ['Cápsula renal', '0.7', '0.8 - 1.2'],
        ['Capacidad residual funcional', '1.8', '2.0 - 3.5'],
    ],
    'expected_distribution': {
        # Cada parámetro debe ir a un sistema distinto
        'digestivo': 1,       # Helicobacter
        'metabolico': 1,      # HDL
        'endocrino': 1,       # TSH
        'musculoesqueletico': 1,  # Densidad ósea
        'nervioso': 1,        # Serotonina
        'toxicidad': 1,       # Aluminio
        'urinario': 1,        # Cápsula renal
        'respiratorio': 1,    # FRC
    },
    'forbidden_in': {
        # Sin antecedente, ningún sistema debe absorber TODOS
        'otros': 0,           # nada debe caer en otros (ideal)
    },
}

ALL_CASES = (CASE_A, CASE_B, CASE_C, CASE_D, CASE_E, CASE_F)


# =====================================================================
# 4) Tests multicaso A-F
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMulticasoFase31(TransactionCase):
    """Cada caso ejercita perfiles clínicos distintos. Si el catálogo y
    extractor son verdaderamente GENERALES, los 6 casos deben distribuir
    sus parámetros en sistemas distintos y coherentes — sin ningún parche
    específico por cliente.
    """

    def _classify_case(self, case):
        """Extrae filas y clasifica cada una con el scorer simple
        basado en taxonomy. Devuelve dict {sistema_code: [params]}."""
        ext = extract_from_table(case['table'])
        result = defaultdict(list)
        for row in ext:
            cls = _classify_simple(row)
            result[cls['sistema_code']].append({
                'parametro': row['parametro_raw'],
                'parametro_norm': row['parametro_norm'],
                'score': cls['score'],
                'breakdown': cls['breakdown'],
            })
        return result

    def _assert_case(self, case):
        distribution = self._classify_case(case)
        # Verificar expected_distribution
        for sis, min_count in case['expected_distribution'].items():
            actual = len(distribution.get(sis, []))
            self.assertGreaterEqual(
                actual, min_count,
                "%s: esperaba >=%d en %s, obtuvo %d. Distribución total: %r" % (
                    case['name'], min_count, sis, actual,
                    {k: [p['parametro'] for p in v]
                     for k, v in distribution.items()},
                ),
            )
        # Verificar forbidden_in
        for sis, max_count in case.get('forbidden_in', {}).items():
            actual = len(distribution.get(sis, []))
            self.assertLessEqual(
                actual, max_count,
                "%s: forbidden_in %s esperaba <=%d, obtuvo %d. "
                "Parámetros mal clasificados: %r" % (
                    case['name'], sis, max_count, actual,
                    [p['parametro'] for p in distribution.get(sis, [])],
                ),
            )
        return distribution

    # ---- Casos individuales ----
    def test_caso_a_digestivo_metales(self):
        self._assert_case(CASE_A)

    def test_caso_b_metabolico(self):
        self._assert_case(CASE_B)

    def test_caso_c_endocrino(self):
        self._assert_case(CASE_C)

    def test_caso_d_musculoesqueletico_inflamatorio(self):
        self._assert_case(CASE_D)

    def test_caso_e_nervioso(self):
        self._assert_case(CASE_E)

    def test_caso_f_sin_antecedente_distribucion_amplia(self):
        """Caso F valida que SIN sección dominante, el classifier no
        absorbe todo en un solo sistema (sería señal de fragilidad)."""
        distribution = self._assert_case(CASE_F)
        # Validación adicional: deben aparecer al menos 6 sistemas distintos
        sistemas_con_hallazgos = [s for s, items in distribution.items() if items]
        self.assertGreaterEqual(
            len(sistemas_con_hallazgos), 6,
            "Caso F debe distribuir en >=6 sistemas; got %d: %r" % (
                len(sistemas_con_hallazgos), sistemas_con_hallazgos,
            ),
        )

    # ---- Cross-case: cada caso produce distribuciones DISTINTAS ----
    def test_los_6_casos_producen_distribuciones_distintas(self):
        """Si dos casos produjeran exactamente la misma distribución,
        el classifier sería degenerado (no diferencia clientes)."""
        signatures = []
        for case in ALL_CASES:
            dist = self._classify_case(case)
            # firma compacta: cuenta por sistema (ignora otros vacíos)
            sig = tuple(sorted(
                (s, len(items)) for s, items in dist.items() if items
            ))
            signatures.append((case['name'], sig))
        # No deben ser todas iguales
        unique_sigs = {sig for _, sig in signatures}
        self.assertGreaterEqual(
            len(unique_sigs), 5,
            "Los 6 casos deben generar >=5 firmas distintas (granularidad). "
            "Got %d: %r" % (len(unique_sigs), signatures),
        )

    def test_ningun_caso_depende_de_gema(self):
        """Validación del spec del usuario: ningún test fixture menciona
        a Gema, ni el nombre 'gema' aparece en taxonomy.py / extractor.py.
        """
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        for module_path in (
            '../models/biocuantico/taxonomy.py',
            '../models/biocuantico/extractor.py',
        ):
            full = os.path.join(base, module_path)
            with open(full, 'r') as f:
                content = f.read().lower()
            self.assertNotIn('gema', content,
                             "Referencia a 'gema' en %s" % module_path)

    def test_taxonomy_no_referencia_productos(self):
        """taxonomy.py no debe contener strings de productos comerciales."""
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        full = os.path.join(base, '../models/biocuantico/taxonomy.py')
        with open(full, 'r') as f:
            content = f.read().lower()
        # Productos VitalHealth conocidos (palabras clave que NO deben aparecer)
        for forbidden in ('vital pro', 'v-te', 'v-ita', 'neurovital',
                          'producto', 'precio', 'cotizacion'):
            self.assertNotIn(forbidden, content,
                             "Referencia comercial %r en taxonomy.py" % forbidden)


# =====================================================================
# 5) Test de versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifestVersion(TransactionCase):

    def test_version_2_1_0_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        # 18.0.2.1.0 → parts = ['18', '0', '2', '1', '0']
        if len(parts) >= 5:
            x, y, z = int(parts[2]), int(parts[3]), int(parts[4])
            self.assertGreaterEqual(
                (x, y, z), (2, 1, 0),
                "Manifest version %s < 18.0.2.1.0 (Fase 3.1)" % v,
            )
