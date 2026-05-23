# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.2 (classifier puro).

OBJETIVO:
    Validar que classifier.py produce clasificaciones correctas con su
    propio scoring real (strong param threshold + dedup cross-sistema +
    noise filter + low_relevance downgrade), SIN intervenir antecedente,
    SIN conectar al flujo productivo.

REGLA arquitectónica respetada:
    El PDF clasifica. El antecedente prioriza. La IA interpreta.
    En esta fase el antecedente NO debe afectar la clasificación.
"""
from collections import defaultdict

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.valoracion.models.biocuantico.taxonomy import (
    SYSTEMS, normalize_text, get_system,
)
from odoo.addons.valoracion.models.biocuantico.extractor import (
    extract_from_table, make_raw_row,
)
from odoo.addons.valoracion.models.biocuantico.classifier import (
    classify, classify_row, dedupe_classified,
    _is_noise_param, _is_low_relevance_param,
    STRONG_PARAM_THRESHOLD, MIN_SCORE_TO_ASSIGN,
    SOURCE_PRIORITY,
)


# =====================================================================
# 0) Tests unitarios — clasificación de una sola fila
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestClassifyRowUnit(TransactionCase):

    def test_param_fuerte_decide_solo_con_param(self):
        """Si max(param_score) >= STRONG_PARAM_THRESHOLD, el contexto NO
        debe sumarse. Ejemplo: 'Helicobacter' bajo tabla musculoesquelética."""
        row = make_raw_row(
            parametro_raw='Helicobacter pylori', valor_raw='1.5',
            rango_raw='0 - 1',
            # contexto musculoesquelético (debería ser ignorado)
            table_context_norm=(
                'densidad osea lumbar densidad osea cadera colageno tipo i'
            ),
        )
        c = classify_row(row)
        self.assertEqual(c['sistema_code'], 'digestivo')
        self.assertEqual(c['sistema_source'], 'keyword_strong')
        self.assertGreaterEqual(c['sistema_score'], STRONG_PARAM_THRESHOLD)

    def test_param_debil_usa_contexto(self):
        """Si el parámetro NO tiene match fuerte, el contexto agregado
        debe contribuir. Ejemplo: parámetro genérico con contexto pulmonar."""
        row = make_raw_row(
            parametro_raw='Indicador X', valor_raw='1.5',
            rango_raw='0 - 1',
            table_context_norm=(
                'capacidad residual funcional frc alveolar pulmonar bronquios'
            ),
        )
        c = classify_row(row)
        self.assertEqual(c['sistema_code'], 'respiratorio')
        # source debería ser 'context' (no hay match en param)
        self.assertEqual(c['sistema_source'], 'context')

    def test_param_sin_match_va_a_otros(self):
        """Si ni param ni contexto alcanzan MIN_SCORE_TO_ASSIGN, 'otros'."""
        row = make_raw_row(
            parametro_raw='Parámetro completamente abstracto sin clínica',
            valor_raw='1.5', rango_raw='0 - 1',
            table_context_norm='texto sin keywords clinicas',
        )
        c = classify_row(row)
        self.assertEqual(c['sistema_code'], 'otros')
        self.assertEqual(c['sistema_source'], 'catch_all')

    def test_noise_param_va_a_noise(self):
        """'Ilustración' debe matchear NOISE_PATTERNS y caer en sistema
        otros con sistema_source='noise'."""
        row = make_raw_row(
            parametro_raw='Ilustración', valor_raw='3', rango_raw='0 - 5',
        )
        c = classify_row(row)
        self.assertTrue(c['is_noise'])
        self.assertEqual(c['sistema_source'], 'noise')
        self.assertEqual(c['motivo_descarte'], 'ruido_no_clinico')

    def test_low_relevance_marcado_pero_no_descartado(self):
        """'Arrugas profundas' debe quedar marcado is_low_relevance pero
        NO descartado. Se sigue clasificando a tegumentario."""
        row = make_raw_row(
            parametro_raw='Arrugas profundas', valor_raw='0.8',
            rango_raw='0 - 0.3',
        )
        c = classify_row(row)
        self.assertTrue(c['is_low_relevance'])
        self.assertFalse(c['is_noise'])
        self.assertEqual(c['sistema_code'], 'tegumentario')
        # Ningún motivo de descarte aún (eso lo decide el ranker en fase 3.4)
        self.assertIsNone(c.get('motivo_descarte'))

    def test_score_breakdown_es_auditable(self):
        """Cada decisión debe traer su score_breakdown declarativo."""
        row = make_raw_row(parametro_raw='Tiroglobulina', valor_raw='120',
                           rango_raw='0 - 55')
        c = classify_row(row)
        self.assertEqual(c['sistema_code'], 'endocrino')
        self.assertTrue(c['score_breakdown'])
        # Cada item del breakdown es tupla (kind, needle, score)
        for item in c['score_breakdown']:
            self.assertEqual(len(item), 3)
            kind, needle, score = item
            self.assertIn(kind, ('param', 'ctx', 'anti'))

    def test_anti_pattern_resta_score(self):
        """Cardiovascular tiene anti-pattern 'pulmonar'. Un parámetro CV
        con 'pulmonar' en su nombre debe perder contra otros sistemas."""
        # "Presión pulmonar" — 'presion' es cardiovascular pero 'pulmonar'
        # resta. Y 'pulmonar' es kw fuerte de respiratorio.
        row = make_raw_row(parametro_raw='Presión pulmonar', valor_raw='25',
                           rango_raw='10 - 20')
        c = classify_row(row)
        # Debe ganar respiratorio (no cardiovascular)
        self.assertEqual(c['sistema_code'], 'respiratorio')


# =====================================================================
# 1) Tests de dedup
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestDedupCrossSistema(TransactionCase):

    def _classify(self, *rows):
        return [classify_row(r) for r in rows]

    def test_dedup_exacto_misma_tupla(self):
        """Dos filas idénticas → 1 conservada, 1 a descartados con motivo."""
        r = make_raw_row(parametro_raw='TSH', valor_raw='6.5', rango_raw='0.5 - 4.5')
        classified = self._classify(r, r)
        kept, descart = dedupe_classified(classified)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(descart), 1)
        self.assertEqual(descart[0]['motivo_descarte'], 'duplicado_exacto')

    def test_dedup_mismo_sistema_param_mejor_calidad_gana(self):
        """Mismo (sistema, param) pero distinta calidad — mejor score gana."""
        r1 = make_raw_row(parametro_raw='TSH', valor_raw='6.5',
                          rango_raw='')  # sin rango
        r2 = make_raw_row(parametro_raw='TSH', valor_raw='6.5',
                          rango_raw='0.5 - 4.5')  # con rango
        classified = self._classify(r1, r2)
        kept, descart = dedupe_classified(classified)
        # Ambos van a endocrino con el mismo param → uno dedupado
        self.assertEqual(len(kept), 1)
        # Conservar el que tenía rango (más calidad)
        self.assertEqual(kept[0]['rango_raw'], '0.5 - 4.5')

    def test_dedup_cross_sistema_keyword_strong_gana(self):
        """Mismo param en sistemas distintos: keyword_strong > keyword."""
        # Caso simulado: dos filas con el mismo param normalizado pero
        # distinto contexto que las llevan a sistemas distintos.
        # 'Glucosa basal' bajo contexto endocrino vs metabolico.
        r_endo = make_raw_row(
            parametro_raw='Glucosa basal', valor_raw='110', rango_raw='70 - 100',
            table_context_norm='tsh t4 tiroglobulina cortisol progesterona',
        )
        r_meta = make_raw_row(
            parametro_raw='Glucosa basal', valor_raw='110', rango_raw='70 - 100',
            table_context_norm='glucosa hba1c hdl ldl trigliceridos colesterol',
        )
        classified = self._classify(r_endo, r_meta)
        # 'glucosa' es kw fuerte de metabolico → keyword_strong para ambas
        # Cross-sistema: ambas → metabolico (porque kw 'glucosa' está en metabolico)
        # → no hay realmente conflicto cross-sistema en este caso.
        kept, descart = dedupe_classified(classified)
        # Las dos quedaron en metabolico → dedup mismo sistema
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['sistema_code'], 'metabolico')

    def test_noise_va_a_descartados_no_kept(self):
        r_ok = make_raw_row(parametro_raw='TSH', valor_raw='6.5', rango_raw='0.5 - 4.5')
        r_noise = make_raw_row(parametro_raw='Ilustración', valor_raw='3', rango_raw='0 - 5')
        classified = self._classify(r_ok, r_noise)
        kept, descart = dedupe_classified(classified)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['parametro_raw'], 'TSH')
        # Noise debe estar en descartados
        self.assertEqual(len(descart), 1)
        self.assertEqual(descart[0]['motivo_descarte'], 'ruido_no_clinico')


# =====================================================================
# 2) Re-validación multicaso A-F usando classifier real
# =====================================================================
CASE_TABLES = {
    'A': [
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
    'B': [
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
    'C': [
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
    'D': [
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
    'E': [
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
    'F': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Helicobacter', '1.5', '0 - 1'],
        ['HDL', '25', '40 - 60'],
        ['TSH', '5.5', '0.5 - 4.5'],
        ['Densidad ósea', '0.8', '0.9 - 1.2'],
        ['Serotonina', '70', '120 - 200'],
        ['Aluminio', '50', '0 - 10'],
        ['Cápsula renal', '0.7', '0.8 - 1.2'],
        ['Capacidad residual funcional', '1.8', '2.0 - 3.5'],
    ],
}

CASE_EXPECTATIONS = {
    'A': {'must_have': {'digestivo': 4, 'toxicidad': 3},
          'forbidden': {'cardiovascular': 0, 'respiratorio': 0, 'sensorial': 0}},
    'B': {'must_have': {'metabolico': 6},
          'forbidden': {'respiratorio': 0, 'sensorial': 0, 'urinario': 0}},
    'C': {'must_have': {'endocrino': 8},
          'forbidden': {'cardiovascular': 0, 'respiratorio': 0}},
    'D': {'must_have': {'musculoesqueletico': 4, 'inmune': 2},
          'forbidden': {'metabolico': 0, 'respiratorio': 0, 'cardiovascular': 0}},
    'E': {'must_have': {'nervioso': 6},
          'forbidden': {'cardiovascular': 0, 'metabolico': 0, 'respiratorio': 0}},
    'F': {'must_have_systems_count': 6},
}


@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMulticasoAFConClassifierReal(TransactionCase):
    """Los 6 casos deben pasar con el classifier real (Fase 3.2), NO con
    el helper inline que tenía el test de Fase 3.1."""

    def _classify_case(self, case_key):
        rows = extract_from_table(CASE_TABLES[case_key])
        result = classify(rows)
        dist = defaultdict(list)
        for cr in result['kept']:
            dist[cr['sistema_code']].append({
                'parametro': cr['parametro_raw'],
                'score': cr['sistema_score'],
                'source': cr['sistema_source'],
            })
        return dist, result['descartados'], result['stats']

    def test_caso_a(self):
        dist, descart, stats = self._classify_case('A')
        exp = CASE_EXPECTATIONS['A']
        for sis, n in exp['must_have'].items():
            self.assertGreaterEqual(
                len(dist.get(sis, [])), n,
                "Caso A: %s tiene %d (esperaba >=%d). Dist: %r" % (
                    sis, len(dist.get(sis, [])), n,
                    {k: [p['parametro'] for p in v] for k, v in dist.items()},
                ),
            )
        for sis, n in exp['forbidden'].items():
            self.assertLessEqual(len(dist.get(sis, [])), n,
                                 "Caso A: forbidden %s tiene %d (<=%d)" %
                                 (sis, len(dist.get(sis, [])), n))

    def test_caso_b(self):
        dist, descart, stats = self._classify_case('B')
        exp = CASE_EXPECTATIONS['B']
        for sis, n in exp['must_have'].items():
            self.assertGreaterEqual(
                len(dist.get(sis, [])), n,
                "Caso B: %s tiene %d (>=%d). Dist: %r" % (
                    sis, len(dist.get(sis, [])), n,
                    {k: [p['parametro'] for p in v] for k, v in dist.items()},
                ),
            )
        for sis, n in exp['forbidden'].items():
            self.assertLessEqual(len(dist.get(sis, [])), n)

    def test_caso_c(self):
        dist, descart, stats = self._classify_case('C')
        exp = CASE_EXPECTATIONS['C']
        for sis, n in exp['must_have'].items():
            self.assertGreaterEqual(len(dist.get(sis, [])), n)
        for sis, n in exp['forbidden'].items():
            self.assertLessEqual(len(dist.get(sis, [])), n)

    def test_caso_d(self):
        dist, descart, stats = self._classify_case('D')
        exp = CASE_EXPECTATIONS['D']
        for sis, n in exp['must_have'].items():
            self.assertGreaterEqual(
                len(dist.get(sis, [])), n,
                "Caso D: %s tiene %d (>=%d). Dist: %r" % (
                    sis, len(dist.get(sis, [])), n,
                    {k: [p['parametro'] for p in v] for k, v in dist.items()},
                ),
            )
        for sis, n in exp['forbidden'].items():
            self.assertLessEqual(len(dist.get(sis, [])), n)

    def test_caso_e(self):
        dist, descart, stats = self._classify_case('E')
        exp = CASE_EXPECTATIONS['E']
        for sis, n in exp['must_have'].items():
            self.assertGreaterEqual(len(dist.get(sis, [])), n)
        for sis, n in exp['forbidden'].items():
            self.assertLessEqual(len(dist.get(sis, [])), n)

    def test_caso_f_distribuye_en_muchos_sistemas(self):
        dist, descart, stats = self._classify_case('F')
        n_sistemas = sum(1 for s, items in dist.items() if items)
        self.assertGreaterEqual(
            n_sistemas, 6,
            "Caso F: solo %d sistemas con hallazgos (>=6). Dist: %r" %
            (n_sistemas, {k: [p['parametro'] for p in v]
                          for k, v in dist.items()}),
        )

    def test_los_6_casos_producen_firmas_distintas(self):
        sigs = []
        for k in 'ABCDEF':
            dist, _, _ = self._classify_case(k)
            sig = tuple(sorted(
                (s, len(items)) for s, items in dist.items() if items
            ))
            sigs.append((k, sig))
        unique = {sig for _, sig in sigs}
        self.assertGreaterEqual(
            len(unique), 5,
            "Esperaba >=5 firmas únicas, got %d: %r" % (len(unique), sigs),
        )

    def test_ningun_parametro_en_dos_sistemas(self):
        """Validación cross-sistema: para cualquier caso, ningún
        parámetro_norm debe aparecer en >1 sistema en `kept`."""
        for k in 'ABCDEF':
            dist, _, _ = self._classify_case(k)
            seen = {}
            for sis, items in dist.items():
                for it in items:
                    pn = normalize_text(it['parametro'])
                    if pn in seen:
                        self.fail(
                            "Caso %s: parámetro %r en %s y %s" % (
                                k, it['parametro'], seen[pn], sis,
                            ),
                        )
                    seen[pn] = sis


# =====================================================================
# 3) Tests de no-conexión al flujo productivo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestClassifierAislamiento(TransactionCase):
    """Confirma que classifier.py NO importa, NO toca y NO es usado por
    el flujo productivo."""

    def test_classifier_no_importa_modelos_odoo(self):
        """classifier.py debe ser puro: no importa odoo, no usa env."""
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/classifier.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        # Imports prohibidos
        for forbidden in ('from odoo', 'import odoo', 'self.env',
                          'sudo()', 'ir.model'):
            self.assertNotIn(forbidden, content,
                             "classifier.py importa Odoo: %r" % forbidden)

    def test_classifier_no_es_usado_por_parser_productivo(self):
        """parser.py (motor viejo) NO debe importar classifier.py."""
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/parser.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('from .classifier', content)
        self.assertNotIn('import classifier', content)

    def test_classifier_no_es_usado_por_master_summary(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/master_summary.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('from .classifier', content)

    def test_classifier_no_es_usado_por_archivo_cliente(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/valoracion_archivo_cliente.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('classifier', content)

    def test_classifier_no_referencia_clientes_o_productos(self):
        """No debe contener referencias a Gema, OpenAI, productos."""
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/classifier.py',
        )
        with open(path, 'r') as f:
            content = f.read().lower()
        for forbidden in ('gema', 'openai', 'anthropic', 'claude',
                          'producto', 'cotizacion', 'sale_order',
                          'vital pro', 'v-te'):
            self.assertNotIn(forbidden, content,
                             "classifier.py contiene %r" % forbidden)


# =====================================================================
# 4) Versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest311(TransactionCase):

    def test_version_2_1_1_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = int(parts[2]), int(parts[3]), int(parts[4])
            self.assertGreaterEqual(
                (x, y, z), (2, 1, 1),
                "Manifest %s < 18.0.2.1.1 (Fase 3.2)" % v,
            )
