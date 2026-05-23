# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.3 (severity puro).

REGLAS:
    El PDF clasifica → classifier.py (Fase 3.2)
    El PDF tiene severidad → severity.py (Fase 3.3) ← este módulo
    El antecedente prioriza → ranker.py (Fase 3.4, pendiente)
    La IA interpreta → no se toca

Los tests validan que severity.py:
  * usa estado explícito cuando existe,
  * infiere severidad por valor vs rango con suavizado,
  * tolera coma decimal, operadores <>≤≥, miles,
  * NO infla pequeñas desviaciones,
  * marca 'desconocida' cuando faltan datos,
  * pasa los 6 casos A-F end-to-end con classifier + severity,
  * NO está conectado al flujo productivo.
"""
from collections import defaultdict

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.valoracion.models.biocuantico.extractor import (
    extract_from_table, make_raw_row,
)
from odoo.addons.valoracion.models.biocuantico.classifier import classify
from odoo.addons.valoracion.models.biocuantico.severity import (
    assign_severity, assign_severity_row,
    severity_from_estado, infer_severity_from_value_range,
    parse_float, parse_range,
    SEVERITY_SCORE, THRESHOLD_LEVE, THRESHOLD_MODERADO,
    DEFAULT_ESTADOS_LOOKUP, all_levels, severity_stats,
)


# =====================================================================
# 0) Parsers numéricos
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestParsers(TransactionCase):

    def test_parse_float_entero(self):
        self.assertEqual(parse_float('120'), 120.0)
        self.assertEqual(parse_float('-3'), -3.0)

    def test_parse_float_punto_decimal(self):
        self.assertEqual(parse_float('1.425'), 1.425)
        self.assertEqual(parse_float('-3.14'), -3.14)

    def test_parse_float_coma_decimal_mx_es(self):
        """Coma decimal estilo Mexico/España: '1,425' → 1.425."""
        self.assertEqual(parse_float('1,425'), 1.425)
        self.assertEqual(parse_float('0,5'), 0.5)

    def test_parse_float_miles_con_punto_decimal(self):
        """Cuando hay AMBOS: coma = miles, punto = decimal (US style)."""
        self.assertEqual(parse_float('1,329.50'), 1329.50)

    def test_parse_float_con_unidades(self):
        """Tolera unidades adheridas: '120 mg/dL'."""
        self.assertEqual(parse_float('120 mg/dL'), 120.0)

    def test_parse_float_presion_arterial(self):
        """'150/95' → 150 (primer número)."""
        self.assertEqual(parse_float('150/95'), 150.0)

    def test_parse_float_texto_no_numerico(self):
        self.assertIsNone(parse_float('positivo'))
        self.assertIsNone(parse_float(''))
        self.assertIsNone(parse_float(None))

    # --- Rangos ---
    def test_parse_range_bilateral(self):
        self.assertEqual(parse_range('70-110'), (70.0, 110.0))
        self.assertEqual(parse_range('70 - 110'), (70.0, 110.0))
        self.assertEqual(parse_range('0,5 - 4,5'), (0.5, 4.5))
        self.assertEqual(parse_range('0,431 - 1,329'), (0.431, 1.329))

    def test_parse_range_acotado_superior(self):
        self.assertEqual(parse_range('<150'), (None, 150.0))
        self.assertEqual(parse_range('<= 150'), (None, 150.0))
        self.assertEqual(parse_range('≤ 150'), (None, 150.0))

    def test_parse_range_acotado_inferior(self):
        self.assertEqual(parse_range('>30'), (30.0, None))
        self.assertEqual(parse_range('>= 30'), (30.0, None))
        self.assertEqual(parse_range('≥ 30'), (30.0, None))

    def test_parse_range_a_to(self):
        self.assertEqual(parse_range('70 a 110'), (70.0, 110.0))
        self.assertEqual(parse_range('70 to 110'), (70.0, 110.0))

    def test_parse_range_invalido(self):
        self.assertIsNone(parse_range('rango cualitativo'))
        self.assertIsNone(parse_range(''))
        self.assertIsNone(parse_range(None))


# =====================================================================
# 1) Lookup de estado explícito
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestEstadoExplicito(TransactionCase):

    def test_estado_normal(self):
        level, score = severity_from_estado('Normal')
        self.assertEqual(level, 'normal')
        self.assertEqual(score, 1)

    def test_estado_severo(self):
        level, score = severity_from_estado('Severo')
        self.assertEqual(level, 'severo')
        self.assertEqual(score, 4)

    def test_estado_critico(self):
        level, score = severity_from_estado('Crítico')
        self.assertEqual(level, 'critico')
        self.assertEqual(score, 5)

    def test_estado_optimo(self):
        level, score = severity_from_estado('Óptimo')
        self.assertEqual(level, 'optimo')
        self.assertEqual(score, 0)

    def test_estado_partial_match(self):
        """'moderadamente alterado' debe ganar sobre 'alterado'."""
        level, _ = severity_from_estado('Moderadamente alterado')
        self.assertEqual(level, 'moderado')

    def test_estado_desconocido_devuelve_none(self):
        self.assertEqual(severity_from_estado('texto raro'), (None, None))
        self.assertEqual(severity_from_estado(''), (None, None))
        self.assertEqual(severity_from_estado(None), (None, None))


# =====================================================================
# 2) Inferencia numérica — casos del spec
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestInferenciaNumerica(TransactionCase):

    def test_valor_dentro_de_rango_normal(self):
        level, score, bd = infer_severity_from_value_range('90', '80 - 100')
        self.assertEqual(level, 'normal')
        self.assertEqual(score, 1)
        self.assertTrue(bd['in_range'])

    def test_valor_apenas_fuera_leve(self):
        """Caso real del usuario: 1.425 vs 0,431-1,329 → debe ser leve,
        no severo, gracias al suavizado bilateral."""
        level, score, bd = infer_severity_from_value_range('1.425', '0,431 - 1,329')
        self.assertEqual(level, 'leve')
        self.assertEqual(score, 2)
        # Debe haber usado suavizado (pct_central < pct_bound)
        self.assertIsNotNone(bd.get('pct_central'))

    def test_valor_moderado(self):
        """120 vs 80-100 → 20% fuera → moderado."""
        level, score, bd = infer_severity_from_value_range('120', '80 - 100')
        self.assertEqual(level, 'moderado')
        self.assertEqual(score, 3)

    def test_valor_severo_alto_pct(self):
        """200 vs 80-100 → 100% fuera → severo."""
        level, score, bd = infer_severity_from_value_range('200', '80 - 100')
        self.assertEqual(level, 'severo')
        self.assertEqual(score, 4)

    def test_acotado_superior(self):
        """160 vs <150 → 6.7% fuera → leve."""
        level, _, _ = infer_severity_from_value_range('160', '<150')
        self.assertEqual(level, 'leve')

    def test_acotado_inferior(self):
        """25 vs >30 → 16.7% fuera → moderado."""
        level, _, _ = infer_severity_from_value_range('25', '>30')
        self.assertEqual(level, 'moderado')

    def test_unicode_leq_geq(self):
        level1, _, _ = infer_severity_from_value_range('200', '≤150')
        level2, _, _ = infer_severity_from_value_range('20', '≥30')
        self.assertIsNotNone(level1)
        self.assertIsNotNone(level2)
        self.assertNotEqual(level1, 'normal')
        self.assertNotEqual(level2, 'normal')

    def test_coma_decimal_en_valor(self):
        """'1,425' como valor con coma decimal."""
        level, _, bd = infer_severity_from_value_range('1,425', '0,431 - 1,329')
        self.assertEqual(level, 'leve')
        self.assertAlmostEqual(bd['valor_float'], 1.425)

    def test_no_inflar_rango_estrecho(self):
        """Rango muy estrecho (0.99-1.00): un valor 5% arriba (1.05) NO
        debe ser severo. El suavizado bilateral protege."""
        level, _, bd = infer_severity_from_value_range('1.05', '0.99 - 1.00')
        # Sin suavizado: pct_bound = 5/100 = 5% → leve (igual)
        # Con suavizado: pct_central ≈ 5.5% → leve (igual)
        # Si el ancho fuera mayor, el suavizado tendría más efecto.
        self.assertEqual(level, 'leve')

    def test_pequena_desviacion_no_se_infla(self):
        """1.5 vs 0.5-4.5 → dentro del rango → normal."""
        level, _, _ = infer_severity_from_value_range('1.5', '0.5 - 4.5')
        self.assertEqual(level, 'normal')

    def test_valor_no_numerico(self):
        """'positivo' no es número → no se puede inferir."""
        level, score, bd = infer_severity_from_value_range('positivo', '0 - 1')
        self.assertIsNone(level)
        self.assertIsNone(score)
        self.assertEqual(bd['motivo'], 'valor_no_parseable')

    def test_rango_invalido(self):
        """Rango cualitativo no parseable."""
        level, score, bd = infer_severity_from_value_range('120', 'cualitativo')
        self.assertIsNone(level)
        self.assertIsNone(score)
        self.assertEqual(bd['motivo'], 'rango_no_parseable')


# =====================================================================
# 3) assign_severity_row — flujo completo por fila
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestAssignSeverityRow(TransactionCase):

    def test_estado_explicito_gana(self):
        """Si la fila trae estado_raw, ese gana sobre la inferencia."""
        row = make_raw_row(parametro_raw='Glucosa', valor_raw='150',
                           rango_raw='70 - 110', estado_raw='Severo')
        result = assign_severity_row(row)
        self.assertEqual(result['severity_level'], 'severo')
        self.assertEqual(result['severity_score'], 4)
        self.assertEqual(result['severity_source'], 'estado_explicito')

    def test_inferencia_cuando_no_hay_estado(self):
        row = make_raw_row(parametro_raw='Glucosa', valor_raw='150',
                           rango_raw='70 - 110', estado_raw=None)
        result = assign_severity_row(row)
        # 150 vs 70-110: bound=110, abs_dev=40, pct_bound=36.4%
        # pct_central = 40/90 = 44.4%
        # min = 36.4% → severo
        self.assertEqual(result['severity_level'], 'severo')
        self.assertEqual(result['severity_source'], 'inferencia_numerica')

    def test_desconocida_sin_datos(self):
        row = make_raw_row(parametro_raw='X', valor_raw='', rango_raw='',
                           estado_raw=None)
        result = assign_severity_row(row)
        self.assertEqual(result['severity_level'], 'desconocida')
        self.assertEqual(result['severity_source'], 'desconocida')
        self.assertEqual(result['severity_score'], 9)

    def test_desconocida_estado_no_resuelve_y_no_hay_valor(self):
        """Estado raro como 'observar' no resuelve a un level conocido."""
        row = make_raw_row(parametro_raw='X', valor_raw='', rango_raw='',
                           estado_raw='observar')
        result = assign_severity_row(row)
        self.assertEqual(result['severity_level'], 'desconocida')

    def test_breakdown_contiene_auditoria(self):
        row = make_raw_row(parametro_raw='Glucosa', valor_raw='95',
                           rango_raw='70 - 110')
        result = assign_severity_row(row)
        bd = result['severity_breakdown']
        self.assertEqual(bd['valor_float'], 95.0)
        self.assertEqual(bd['rango_lo'], 70.0)
        self.assertEqual(bd['rango_hi'], 110.0)
        self.assertTrue(bd['in_range'])


# =====================================================================
# 4) End-to-end: classifier + severity con casos A-F
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


@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMulticasoConSeverity(TransactionCase):
    """Cada caso A-F debe pasar end-to-end con classifier + severity:
    sistema_code + severity_level asignados a cada hallazgo, con audit."""

    def _process(self, case_key):
        rows = extract_from_table(CASE_TABLES[case_key])
        classified = classify(rows)['kept']
        with_sev = assign_severity(classified)
        return with_sev

    def test_caso_a_todos_tienen_severidad(self):
        results = self._process('A')
        for r in results:
            self.assertIn(r['severity_level'], all_levels())
            self.assertIn(r['severity_source'],
                          ('estado_explicito', 'inferencia_numerica',
                           'desconocida'))

    def test_caso_b_metabolico_tiene_severos_y_moderados(self):
        """Caso B (síndrome metabólico) tiene varios valores muy fuera de
        rango → debe haber al menos 1 severo y varios moderados."""
        results = self._process('B')
        sev_count = severity_stats(results)
        # >= 1 severo (ej. HDL 25 vs 40-60)
        self.assertGreaterEqual(sev_count.get('severo', 0), 1,
                                "Caso B: esperaba >=1 severo. Stats: %r"
                                % sev_count)
        # Mínimo algunos hallazgos anormales en total
        anormal = sum(sev_count.get(lev, 0)
                      for lev in ('leve', 'moderado', 'severo'))
        self.assertGreaterEqual(anormal, 5,
                                "Caso B: esperaba >=5 anormales. Stats: %r"
                                % sev_count)

    def test_caso_c_endocrino_tiene_severos(self):
        """TSH 6.5 vs 0.5-4.5 → severo. Tiroglobulina 120 vs 0-55 → severo."""
        results = self._process('C')
        sev_count = severity_stats(results)
        self.assertGreaterEqual(sev_count.get('severo', 0), 1)

    def test_caso_d_inflamatorios_severos(self):
        """Inflamación articular 2.5 vs 0-1 → severo."""
        results = self._process('D')
        sev_count = severity_stats(results)
        anormal = sum(sev_count.get(lev, 0)
                      for lev in ('leve', 'moderado', 'severo'))
        self.assertGreaterEqual(anormal, 3,
                                "Caso D anormales: %r" % sev_count)

    def test_caso_e_nervioso_anormales(self):
        results = self._process('E')
        sev_count = severity_stats(results)
        anormal = sum(sev_count.get(lev, 0)
                      for lev in ('leve', 'moderado', 'severo'))
        self.assertGreaterEqual(anormal, 4)

    def test_caso_f_distribucion_de_severidades(self):
        """Caso F (mezcla sin antecedente): cada parámetro debe recibir
        severity_level distinto al random — algunos serán anormales."""
        results = self._process('F')
        sev_count = severity_stats(results)
        # Al menos 4 hallazgos no-normal (anormales o desconocidos)
        non_normal = sum(v for lev, v in sev_count.items()
                         if lev not in ('normal',))
        self.assertGreaterEqual(non_normal, 4,
                                "Caso F: esperaba >=4 no-normales. Stats: %r"
                                % sev_count)

    def test_cada_row_lleva_severity_breakdown(self):
        """Auditoría: cada hallazgo trae su breakdown para que el admin
        pueda entender el razonamiento."""
        results = self._process('A')
        for r in results:
            self.assertIn('severity_breakdown', r)
            self.assertIsInstance(r['severity_breakdown'], dict)


# =====================================================================
# 5) Aislamiento del flujo productivo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestSeverityAislamiento(TransactionCase):

    def test_severity_no_importa_odoo(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/severity.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        for forbidden in ('from odoo', 'import odoo', 'self.env',
                          'sudo()', 'ir.model'):
            self.assertNotIn(forbidden, content,
                             "severity.py importa Odoo: %r" % forbidden)

    def test_severity_no_es_usado_por_parser_productivo(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/parser.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('from .severity', content)
        self.assertNotIn('import severity', content)

    def test_severity_no_es_usado_por_master_summary(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/master_summary.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('from .severity', content)

    def test_severity_no_es_usado_por_archivo_cliente(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/valoracion_archivo_cliente.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        # severity_score y severity_level YA existían como conceptos del
        # parser viejo. Pero el módulo severity.py NEW no debe ser importado.
        self.assertNotIn('from .biocuantico.severity', content)
        self.assertNotIn('from .biocuantico import severity', content)

    def test_severity_no_referencia_clientes_o_productos(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/severity.py',
        )
        with open(path, 'r') as f:
            content = f.read().lower()
        for forbidden in ('gema', 'openai', 'anthropic', 'claude',
                          'producto', 'cotizacion', 'sale_order',
                          'vital pro', 'antecedente', 'paciente'):
            # 'antecedente' y 'paciente' confirman que severity NO usa
            # contexto del cliente.
            self.assertNotIn(forbidden, content,
                             "severity.py contiene %r" % forbidden)


# =====================================================================
# 6) Versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest312(TransactionCase):

    def test_version_2_1_2_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = int(parts[2]), int(parts[3]), int(parts[4])
            self.assertGreaterEqual(
                (x, y, z), (2, 1, 2),
                "Manifest %s < 18.0.2.1.2 (Fase 3.3)" % v,
            )
