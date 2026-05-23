# -*- coding: utf-8 -*-
"""Tests BioCuántico — Fase 2.1 (detección) + Fase 2.2 (parser real).

Las dos fases comparten suite porque el contrato público se extendió, no
rompió. Los tests de Fase 2.1 siguen validando `is_biocuantico` / `detect`,
y los nuevos tests de Fase 2.2 validan `parse(tables=...)`, status
ok/partial/failed, y el master_summary.
"""
from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.addons.valoracion.models.biocuantico.parser import BioCuanticoParser
from odoo.addons.valoracion.models.biocuantico.master_summary import (
    BioCuanticoMasterSummary,
)


# =====================================================================
# Detección heurística (Fase 2.1)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestBioCuanticoDetection(TransactionCase):

    def test_detect_explicit_marker(self):
        text = (
            "Informe BIOCUÁNTICO del paciente.\n"
            "Sistema Cardiovascular: Normal.\n"
            "Sistema Digestivo: Levemente alterado.\n"
        )
        self.assertTrue(BioCuanticoParser.is_biocuantico(text))

    def test_detect_resonancia_cuantica(self):
        text = (
            "Reporte de Resonancia Cuántica.\n"
            "Sistema Inmune: Moderadamente alterado.\n"
            "Sistema Nervioso: Normal.\n"
            "Rango normal y valor medido incluidos.\n"
        )
        self.assertTrue(BioCuanticoParser.is_biocuantico(text))

    def test_detect_handles_accents_and_case(self):
        text = (
            "BIOCUÁNTICO — Análisis Cuántico.\n"
            "SISTEMA CARDIOVASCULAR: NORMAL.\n"
            "Valor de referencia incluido.\n"
        )
        self.assertTrue(BioCuanticoParser.is_biocuantico(text))

    def test_detect_two_strong_markers(self):
        text = "Bio-cuántico. Resonancia cuántica."
        self.assertTrue(BioCuanticoParser.is_biocuantico(text))

    def test_not_detect_generic_lab(self):
        text = (
            "Resultado de laboratorio clínico.\n"
            "Hemograma completo. Glucosa: 95 mg/dL (rango normal 70-110).\n"
        )
        self.assertFalse(BioCuanticoParser.is_biocuantico(text))

    def test_not_detect_empty(self):
        self.assertFalse(BioCuanticoParser.is_biocuantico(""))
        self.assertFalse(BioCuanticoParser.is_biocuantico(None))

    def test_detect_returns_stable_shape(self):
        result = BioCuanticoParser.detect("Biocuántico — resonancia cuántica")
        for key in ('is_biocuantico', 'status', 'score',
                    'threshold', 'text_hits', 'table_hits'):
            self.assertIn(key, result)


# =====================================================================
# Parser real con tablas (Fase 2.2)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestBioCuanticoParserPhase22(TransactionCase):

    def _make_full_tables(self):
        """Tablas BC bien formadas — debe dar status='ok'."""
        return [
            [
                ['Item', 'Valor', 'Rango Normal', 'Estado'],
                ['Sistema Cardiovascular', '', '', ''],
                ['Presión arterial', '150/95', '120/80', 'Severo'],
                ['Frecuencia cardíaca', '88', '60-100', 'Normal'],
                ['Sistema Digestivo', '', '', ''],
                ['Helicobacter pylori', 'positivo', 'negativo', 'Severo'],
                ['Acidez gástrica', 'alta', 'normal', 'Moderado'],
                ['Tránsito intestinal', 'lento', 'normal', 'Leve'],
                ['Sistema Endocrino', '', '', ''],
                ['Tiroides TSH', '5.5', '0.5-4.5', 'Moderado'],
                ['Cortisol matutino', 'alto', 'normal', 'Severo'],
                ['Insulina basal', '18', '5-15', 'Moderado'],
                ['Sistema Inmune', '', '', ''],
                ['Defensas globales', 'bajas', 'normal', 'Levemente alterado'],
                ['Sistema Nervioso', '', '', ''],
                ['Equilibrio autonómico', 'alterado', 'normal', 'Severamente alterado'],
            ],
        ]

    def test_status_ok_with_rich_tables(self):
        text = "Informe BIOCUÁNTICO — Resonancia Cuántica."
        payload = BioCuanticoParser.parse(text=text, tables=self._make_full_tables(),
                                          env=self.env)
        self.assertEqual(payload['status'], 'ok')
        self.assertTrue(payload['parsed'])
        # Debe cumplir al menos uno de los tres criterios
        n_useful = payload['estadisticas']['filas_utiles']
        n_sistemas = len(payload['sistemas'])
        n_abnormal = len(payload['parametros_anormales'])
        self.assertTrue(
            n_useful >= 15 or n_sistemas >= 3 or n_abnormal >= 5,
            "Status ok pero ningún criterio se cumple: rows=%d sistemas=%d anormal=%d" % (
                n_useful, n_sistemas, n_abnormal,
            ),
        )

    def test_status_partial_with_minimal_tables(self):
        text = "Reporte BIOCUÁNTICO."
        tables = [
            [
                ['Sistema', 'Estado'],
                ['Sistema digestivo', 'levemente alterado'],
            ],
        ]
        payload = BioCuanticoParser.parse(text=text, tables=tables, env=self.env)
        self.assertEqual(payload['status'], 'partial')

    def test_status_not_biocuantico_for_random_doc(self):
        text = "Receta médica. Tomar dos cápsulas cada 12 horas."
        payload = BioCuanticoParser.parse(text=text, tables=[], env=self.env)
        self.assertEqual(payload['status'], 'not_biocuantico')

    def test_classification_per_sistema(self):
        """Verifica que cada parámetro queda en su sistema correcto."""
        text = "Informe BIOCUÁNTICO — Resonancia Cuántica."
        payload = BioCuanticoParser.parse(text=text, tables=self._make_full_tables(),
                                          env=self.env)
        # Mapeo parametro → sistema esperado
        by_param = {m['parametro']: m for m in payload['mediciones']}
        cases = [
            ('Presión arterial', 'cardiovascular', 'severo'),
            ('Helicobacter pylori', 'digestivo', 'severo'),
            ('Acidez gástrica', 'digestivo', 'moderado'),
            ('Tiroides TSH', 'endocrino', 'moderado'),
            ('Cortisol matutino', 'endocrino', 'severo'),
            ('Defensas globales', 'inmune', 'leve'),
            ('Equilibrio autonómico', 'nervioso', 'severo'),
        ]
        for param, sistema, estado in cases:
            m = by_param.get(param)
            self.assertIsNotNone(m, "Falta el parámetro: %s" % param)
            self.assertEqual(m['sistema_code'], sistema, "%s sistema" % param)
            self.assertEqual(m['estado_norm'], estado, "%s estado" % param)

    # -----------------------------------------------------------------
    # Inferencia numérica de severidad (Fase 2.2 — bugfix usuario)
    # -----------------------------------------------------------------
    def test_numeric_inference_real_user_case(self):
        """Caso real del usuario: valor=1.425 vs rango 0,431 - 1,329 → alterado."""
        e, s = BioCuanticoParser._classify_by_numeric_range('1.425', '0,431 - 1,329')
        # ~7.2% fuera del bound superior → leve
        self.assertEqual(e, 'leve')
        self.assertEqual(s, 2)

    def test_numeric_inference_20pct_above_upper(self):
        """valor=120 vs rango 80-100 → 20% fuera → moderado."""
        e, s = BioCuanticoParser._classify_by_numeric_range('120', '80 - 100')
        self.assertEqual(e, 'moderado')
        self.assertEqual(s, 3)

    def test_numeric_inference_in_range(self):
        """valor=90 vs rango 80-100 → dentro → normal."""
        e, s = BioCuanticoParser._classify_by_numeric_range('90', '80 - 100')
        self.assertEqual(e, 'normal')
        self.assertEqual(s, 1)

    def test_numeric_inference_above_lt_bound(self):
        """valor=160 vs rango <150 → alterado (6.7% out → leve)."""
        e, s = BioCuanticoParser._classify_by_numeric_range('160', '<150')
        self.assertEqual(e, 'leve')
        self.assertEqual(s, 2)

    def test_numeric_inference_below_gt_bound(self):
        """valor=25 vs rango >30 → alterado (16.7% out → moderado)."""
        e, s = BioCuanticoParser._classify_by_numeric_range('25', '>30')
        self.assertEqual(e, 'moderado')
        self.assertEqual(s, 3)

    def test_numeric_inference_comma_decimal_in_value(self):
        """Coma decimal estilo ES/MX en el valor (1,425 = 1.425)."""
        e, s = BioCuanticoParser._classify_by_numeric_range('1,425', '0,431 - 1,329')
        self.assertEqual(e, 'leve')

    def test_numeric_inference_unicode_leq_geq(self):
        """≤ y ≥ Unicode equivalentes a <= y >="""
        e1, _ = BioCuanticoParser._classify_by_numeric_range('200', '≤150')
        e2, _ = BioCuanticoParser._classify_by_numeric_range('20', '≥30')
        self.assertNotEqual(e1, None)
        self.assertNotEqual(e2, None)
        self.assertNotEqual(e1, 'normal')
        self.assertNotEqual(e2, 'normal')

    def test_numeric_inference_severo_when_30pct_plus(self):
        """>30% fuera del rango → severo."""
        e, s = BioCuanticoParser._classify_by_numeric_range('200', '0 - 100')
        # 100% fuera → severo
        self.assertEqual(e, 'severo')
        self.assertEqual(s, 4)

    def test_numeric_inference_handles_invalid_value(self):
        """Si el valor no es numérico, devuelve (None, None) sin romper."""
        e, s = BioCuanticoParser._classify_by_numeric_range('positivo', '0 - 100')
        self.assertIsNone(e)
        self.assertIsNone(s)

    def test_numeric_inference_handles_invalid_range(self):
        """Si el rango no es interpretable, devuelve (None, None)."""
        e, s = BioCuanticoParser._classify_by_numeric_range('50', 'cualitativo')
        self.assertIsNone(e)
        self.assertIsNone(s)

    def test_parser_infers_state_when_no_estado_column(self):
        """Integración: una tabla BC con (param/valor/rango) y SIN columna de
        estado debe producir mediciones con estado_norm asignado por inferencia
        numérica, NO 'desconocido'.
        """
        tables = [
            [
                ['Parámetro', 'Valor', 'Rango Referencia'],
                ['Sistema Inmune', '', ''],
                ['Índice de alergia a fármacos', '1.425', '0,431 - 1,329'],
                ['IgE total', '95', '0 - 100'],
                ['Sistema Endocrino', '', ''],
                ['TSH', '5,5', '0,5 - 4,5'],
                ['Cortisol matutino', '32', '5 - 25'],
            ],
        ]
        payload = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        self.assertEqual(payload['status'], 'ok')
        # NINGUNA medición con valor+rango debe quedar 'desconocido'
        unknowns = [
            m for m in payload['mediciones']
            if m.get('estado_norm') == 'desconocido'
            and m.get('valor') and m.get('rango')
        ]
        self.assertEqual(unknowns, [], "Quedaron mediciones 'desconocido' con valor+rango: %r" % unknowns)
        # Estadísticas deben reflejar hallazgos reales (no 0/0/0)
        est = payload['estadisticas']
        total_anormal = (est['hallazgos_severos'] + est['hallazgos_moderados']
                         + est['hallazgos_leves'])
        self.assertGreater(total_anormal, 0,
                           "Estadísticas hallazgos vacías pese a tener fuera-de-rango")

    def test_master_summary_no_desconocido_in_priorities(self):
        """Master summary no debe listar sistemas con severidad 'desconocido'
        ni 'óptimo' en sistemas_afectados / prioridades_funcionales.
        """
        tables = [
            [
                ['Parámetro', 'Valor', 'Rango'],
                ['Sistema Endocrino', '', ''],
                ['TSH', '5,5', '0,5 - 4,5'],
                ['Cortisol', '32', '5 - 25'],
                ['Sistema Inmune', '', ''],
                ['IgE total', '95', '0 - 100'],   # normal — no debería aparecer
            ],
        ]
        payload = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(payload, env=self.env)
        self.assertTrue(ms['available'])
        # Ningún sistema con severity_max < 2 en sistemas_afectados
        for s in ms['json']['sistemas_afectados']:
            self.assertGreaterEqual(s['severity_max'], 2,
                                    "Sistema %s con severity_max=%d coló en afectados" % (
                                        s['sistema_label'], s['severity_max']))
        # Inmune no debería estar en afectados (todo normal)
        codes = {s['sistema_code'] for s in ms['json']['sistemas_afectados']}
        self.assertNotIn('inmune', codes, "Sistema 'inmune' no debería aparecer si todo es normal")
        # Prioridades funcionales NO deben mencionar 'desconocido' ni 'óptimo'
        for p in ms['json']['prioridades_funcionales']:
            self.assertNotIn('desconocido', p.lower())
            self.assertNotIn('óptimo', p.lower())

    def test_value_cell_rejects_text_with_embedded_digit(self):
        """Parámetros como 'T4 libre', 'Vit B12', 'Omega-3' NO deben tomarse
        como valor por tener un dígito embebido."""
        self.assertFalse(BioCuanticoParser._looks_like_value_cell('t4 libre'))
        self.assertFalse(BioCuanticoParser._looks_like_value_cell('vitamina b12'))
        self.assertFalse(BioCuanticoParser._looks_like_value_cell('omega-3'))
        # Sí valores
        self.assertTrue(BioCuanticoParser._looks_like_value_cell('1.425'))
        self.assertTrue(BioCuanticoParser._looks_like_value_cell('1,425'))
        self.assertTrue(BioCuanticoParser._looks_like_value_cell('150'))
        self.assertTrue(BioCuanticoParser._looks_like_value_cell('-3.14'))

    def test_text_only_fallback(self):
        """Si no hay tablas pero el texto tiene secciones, el parser textual
        produce mediciones."""
        text = (
            "Reporte BIOCUÁNTICO. Resonancia Cuántica.\n"
            "Sistema Digestivo:\n"
            "  Helicobacter: positivo — Severo\n"
            "  Acidez: alta — Moderado\n"
            "  Tránsito: lento — Leve\n"
            "Sistema Endocrino:\n"
            "  TSH: 5.5 — Moderado\n"
            "  Cortisol: alto — Severo\n"
            "Sistema Cardiovascular:\n"
            "  Presión: 150/95 — Severo\n"
        )
        payload = BioCuanticoParser.parse(text=text, env=self.env)
        self.assertIn(payload['status'], ('ok', 'partial'))
        codes = {s['sistema_code'] for s in payload['sistemas']}
        self.assertIn('digestivo', codes)
        self.assertIn('endocrino', codes)


# =====================================================================
# Master Summary (Fase 2.2)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestBioCuanticoMasterSummary(TransactionCase):

    def test_build_returns_full_json_shape(self):
        tables = [
            [
                ['Item', 'Valor', 'Rango', 'Estado'],
                ['Sistema Digestivo', '', '', ''],
                ['Helicobacter', 'pos', 'neg', 'Severo'],
                ['Acidez', 'alta', 'normal', 'Moderado'],
                ['Sistema Endocrino', '', '', ''],
                ['TSH', '5.5', '0.5-4.5', 'Moderado'],
                ['Cortisol', 'alto', 'normal', 'Severo'],
                ['Insulina', '18', '5-15', 'Moderado'],
            ],
        ]
        text = "Reporte BIOCUÁNTICO Resonancia Cuántica."
        payload = BioCuanticoParser.parse(text=text, tables=tables, env=self.env)
        ms = BioCuanticoMasterSummary.build(payload, env=self.env)
        self.assertTrue(ms['available'])
        j = ms['json']
        for key in (
            'sistemas_afectados', 'hallazgos_principales',
            'hallazgos_secundarios', 'prioridades_funcionales',
            'restricciones_detectadas', 'senales_metabolicas',
            'parametros_anormales', 'estadisticas_parser',
        ):
            self.assertIn(key, j)
        # Estadísticas tienen todas las sub-keys del spec
        est = j['estadisticas_parser']
        for k in ('paginas_procesadas', 'tablas_detectadas', 'filas_utiles',
                  'filas_descartadas', 'hallazgos_severos',
                  'hallazgos_moderados', 'hallazgos_leves'):
            self.assertIn(k, est)

    def test_text_capped_at_6000(self):
        # Payload sintético gigante
        payload = {
            'is_biocuantico': True,
            'status': 'ok',
            'parsed': True,
            'metodo': 'tablas',
            'sistemas': [
                {'sistema_code': 'c%d' % i, 'sistema_label': 'Sistema %d' % i,
                 'severity_max': 4, 'hallazgos_count': 5}
                for i in range(40)
            ],
            'mediciones': [
                {'sistema_code': 'c%d' % (i % 5),
                 'sistema_label': 'Sistema %d' % (i % 5),
                 'parametro': 'Param %d con un nombre muy largo y descriptivo' % i,
                 'valor': str(i * 10), 'rango': '0-100',
                 'estado_raw': 'severo', 'estado_norm': 'severo',
                 'severity': 4, 'source': 'tabla'}
                for i in range(400)
            ],
            'parametros_anormales': [],
            'estadisticas': {'paginas_procesadas': 8, 'tablas_detectadas': 20,
                             'filas_utiles': 400, 'filas_descartadas': 30,
                             'hallazgos_severos': 400,
                             'hallazgos_moderados': 0, 'hallazgos_leves': 0},
            'error': None,
        }
        ms = BioCuanticoMasterSummary.build(payload, env=self.env)
        self.assertTrue(ms['available'])
        self.assertLessEqual(len(ms['text']), 6000)

    def test_unavailable_when_not_biocuantico(self):
        payload = BioCuanticoParser.parse(text="Receta médica.", env=self.env)
        ms = BioCuanticoMasterSummary.build(payload, env=self.env)
        self.assertFalse(ms['available'])
        self.assertEqual(ms['reason'], 'no_biocuantico')

    def test_unavailable_when_failed(self):
        payload = {
            'is_biocuantico': True, 'status': 'failed',
            'parsed': False, 'error': 'something',
            'estadisticas': {}, 'sistemas': [], 'mediciones': [],
            'parametros_anormales': [], 'metodo': None,
        }
        ms = BioCuanticoMasterSummary.build(payload, env=self.env)
        self.assertFalse(ms['available'])
        self.assertEqual(ms['reason'], 'parser_failed')

    def test_render_audit_block_format(self):
        text = "Reporte BIOCUÁNTICO Resonancia Cuántica."
        payload = BioCuanticoParser.parse(
            text=text,
            tables=[[['Sistema Digestivo', '', '', ''],
                     ['Helicobacter', 'pos', 'neg', 'Severo']]],
            env=self.env,
        )
        block = BioCuanticoMasterSummary.render_audit_block(
            payload, summary_text='resumen ejemplo', archivo_label='r.pdf',
        )
        self.assertIn("=== PARSER BIOCUÁNTICO ===", block)
        self.assertIn("Archivo: r.pdf", block)
        self.assertIn("Status:", block)
        self.assertIn("Método:", block)
        self.assertIn("Páginas procesadas:", block)
        self.assertIn("Tablas detectadas:", block)
        self.assertIn("Filas útiles:", block)
        self.assertIn("Hallazgos severos:", block)
        self.assertIn("Resumen maestro:", block)


# =====================================================================
# Integración con valoracion.archivo.cliente (Fase 2.2)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestArchivoClienteBioCuanticoFlow(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Test BC'})
        self.valoracion = self.env['valoracion.valoracion'].create({
            'partner_id': self.partner.id,
        })

    def _make_txt_archivo(self, content, name='reporte.txt'):
        import base64
        return self.env['valoracion.archivo.cliente'].create({
            'valoracion_id': self.valoracion.id,
            'file_name': name,
            'file_data': base64.b64encode(content.encode('utf-8')),
            'extracted_text': content,
            'extraction_status': 'ok',
        })

    def test_run_parser_persists_summary_on_ok(self):
        content = (
            "Informe BIOCUÁNTICO — Resonancia Cuántica.\n"
            "Sistema Digestivo:\n"
            "  Helicobacter: positivo — Severo\n"
            "  Acidez: alta — Moderado\n"
            "  Tránsito: lento — Leve\n"
            "Sistema Endocrino:\n"
            "  TSH: 5.5 — Moderado\n"
            "  Cortisol: alto — Severo\n"
            "Sistema Cardiovascular:\n"
            "  Presión: 150/95 — Severo\n"
        )
        arch = self._make_txt_archivo(content)
        arch._run_biocuantico_parser(force=True)
        self.assertIn(arch.biocuantico_parse_status, ('ok', 'partial'))
        self.assertTrue(arch.biocuantico_summary_text)
        self.assertTrue(arch.biocuantico_summary_json)
        self.assertLessEqual(
            len(arch.biocuantico_summary_text), 6000,
            "summary_text excede el cap de 6000 chars",
        )

    def test_run_parser_marks_not_biocuantico_for_generic(self):
        arch = self._make_txt_archivo(
            "Receta médica. Tomar una pastilla cada 8 horas."
        )
        arch._run_biocuantico_parser(force=True)
        self.assertEqual(arch.biocuantico_parse_status, 'not_biocuantico')
        self.assertFalse(arch.biocuantico_summary_text)
        self.assertFalse(arch.biocuantico_summary_json)

    def test_lazy_call_is_idempotent(self):
        """force=False no debe reintentar cuando ya hay status estable."""
        arch = self._make_txt_archivo(
            "Receta médica corta sin marcadores BC."
        )
        arch._run_biocuantico_parser(force=True)
        first_status = arch.biocuantico_parse_status
        first_at = arch.biocuantico_parsed_at
        self.assertEqual(first_status, 'not_biocuantico')
        # Segunda llamada con force=False debe ser no-op
        arch._run_biocuantico_parser(force=False)
        self.assertEqual(arch.biocuantico_parse_status, first_status)
        self.assertEqual(arch.biocuantico_parsed_at, first_at)

    def test_button_runs_full_parser(self):
        arch = self._make_txt_archivo(
            "Informe BIOCUÁNTICO. Resonancia Cuántica.\n"
            "Sistema Digestivo:\n  Helicobacter: positivo — Severo\n"
        )
        arch.action_detectar_biocuantico()
        # Debe estar en alguno de los estados terminales válidos
        self.assertIn(
            arch.biocuantico_parse_status,
            ('ok', 'partial', 'not_biocuantico', 'failed'),
        )

    def test_replace_file_invalidates_cache(self):
        import base64
        arch = self._make_txt_archivo(
            "Informe BIOCUÁNTICO. Resonancia Cuántica.\n"
            "Sistema Digestivo:\n  Helicobacter: positivo — Severo\n"
        )
        arch.action_detectar_biocuantico()
        self.assertIn(arch.biocuantico_parse_status, ('ok', 'partial', 'not_biocuantico'))
        # Reemplazo de archivo
        arch.write({
            'file_name': 'otro.txt',
            'file_data': base64.b64encode(b"sin marcadores BC"),
        })
        self.assertEqual(arch.biocuantico_parse_status, 'not_parsed')
        self.assertFalse(arch.biocuantico_summary_text)


# =====================================================================
# Bugfix Fase 2.3 — auditoría desde cache JSON sin re-correr parser
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestAuditFromCacheJson(TransactionCase):
    """El bug que motivó este test:

    Cuando un archivo ya tiene `biocuantico_parse_status='ok'` y
    `biocuantico_summary_json` poblado, el parser NO se re-corre en la
    siguiente generación (cache fuerte). Pero `_build_archivos_cliente_block`
    construía la auditoría sobre un payload-stub vacío, produciendo:
        Método: ninguno
        Páginas procesadas: 0
        Tablas detectadas: 0
        Filas útiles: 0
        Sistemas detectados: (ninguno)
        Hallazgos severos/moderados/leves: 0
    pese a que biocuantico_summary_text mostraba métricas reales.

    Fix: cuando bq_payload runtime es None, reconstruir el payload desde
    biocuantico_summary_json via BioCuanticoMasterSummary.payload_from_json.
    """

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Test Cache'})
        self.valoracion = self.env['valoracion.valoracion'].create({
            'partner_id': self.partner.id,
        })

    def _make_archivo_with_cached_json(self):
        """Crea un archivo con biocuantico_summary_json poblado, simulando
        que el parser ya corrió en una generación anterior.
        """
        import base64
        import json
        json_payload = json.dumps({
            "sistemas_afectados": [
                {"sistema_code": "cardiovascular",
                 "sistema_label": "Sistema Cardiovascular",
                 "severity_max": 4, "hallazgos_count": 8},
                {"sistema_code": "digestivo",
                 "sistema_label": "Sistema Digestivo",
                 "severity_max": 4, "hallazgos_count": 12},
                {"sistema_code": "endocrino",
                 "sistema_label": "Sistema Endocrino",
                 "severity_max": 3, "hallazgos_count": 5},
            ],
            "hallazgos_principales": [],
            "hallazgos_secundarios": [],
            "prioridades_funcionales": [],
            "restricciones_detectadas": [],
            "senales_metabolicas": [],
            "parametros_anormales": [],
            "estadisticas_parser": {
                "paginas_procesadas": 127,
                "tablas_detectadas": 116,
                "filas_utiles": 395,
                "filas_descartadas": 142,
                "hallazgos_severos": 32,
                "hallazgos_moderados": 78,
                "hallazgos_leves": 145,
            },
            "metodo": "ambos",
            "status": "ok",
        })
        return self.env['valoracion.archivo.cliente'].create({
            'valoracion_id': self.valoracion.id,
            'file_name': 'Gema Completo.pdf',
            'file_data': base64.b64encode(b"%PDF-1.4 dummy bytes"),
            'extracted_text': 'Informe BIOCUÁNTICO Resonancia Cuántica '
                              'Sistema Cardiovascular Sistema Digestivo '
                              'Sistema Endocrino',
            'extraction_status': 'ok',
            'biocuantico_detectado': True,
            'biocuantico_parse_status': 'ok',
            'biocuantico_summary_json': json_payload,
            'biocuantico_summary_text':
                '=== Análisis BioCuántico Funcional ===\n'
                '(status: ok, método: ambos, filas útiles: 395, sistemas: 3)\n'
                'Sistemas afectados: ...',
            'biocuantico_parsed_at': fields.Datetime.now(),
        })

    def test_payload_from_json_reconstructs_metrics(self):
        """API: BioCuanticoMasterSummary.payload_from_json devuelve un
        payload con las métricas reales del cache."""
        arch = self._make_archivo_with_cached_json()
        payload = BioCuanticoMasterSummary.payload_from_json(
            arch.biocuantico_summary_json,
            archivo_status=arch.biocuantico_parse_status,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['metodo'], 'ambos')
        self.assertEqual(payload['estadisticas']['paginas_procesadas'], 127)
        self.assertEqual(payload['estadisticas']['tablas_detectadas'], 116)
        self.assertEqual(payload['estadisticas']['filas_utiles'], 395)
        self.assertEqual(payload['estadisticas']['hallazgos_severos'], 32)
        self.assertEqual(payload['estadisticas']['hallazgos_moderados'], 78)
        self.assertEqual(payload['estadisticas']['hallazgos_leves'], 145)
        sistemas_codes = [s['sistema_code'] for s in payload['sistemas']]
        self.assertIn('cardiovascular', sistemas_codes)
        self.assertIn('digestivo', sistemas_codes)
        self.assertIn('endocrino', sistemas_codes)

    def test_audit_block_uses_cached_json_when_parser_not_rerun(self):
        """Test de regresión exacto del bug del usuario.

        Flujo simulado:
          1. Archivo ya tiene biocuantico_parse_status='ok' y JSON cacheado.
          2. _build_archivos_cliente_block detecta status estable → NO
             re-corre el parser (bq_payload queda None).
          3. La auditoría ahora DEBE construirse desde el JSON cacheado.
          4. truncado_detalle DEBE mostrar las métricas reales, no ceros.
        """
        arch = self._make_archivo_with_cached_json()
        # Ejecutar el flujo real (sin invocar el proveedor IA — solo el
        # constructor del bloque de archivos).
        block, images, aviso, archivos_audit = (
            self.valoracion._build_archivos_cliente_block()
        )

        # 1) Hay UNA entrada de archivo en la auditoría
        self.assertEqual(len(archivos_audit), 1)
        entry = archivos_audit[0]

        # 2) El parser fue marcado como "Usado: Sí" (sustituyó al heurístico)
        bq = entry.get('biocuantico') or {}
        self.assertTrue(bq.get('used'),
                        "Cache 'ok' debería marcarse como usado en prompt")
        audit_block = bq.get('audit_block_text') or ''
        self.assertTrue(audit_block, "audit_block_text no se generó")

        # 3) Métricas REALES del JSON cacheado deben aparecer (no ceros)
        self.assertIn('Páginas procesadas: 127', audit_block)
        self.assertIn('Tablas detectadas: 116', audit_block)
        self.assertIn('Filas útiles: 395', audit_block)
        self.assertIn('Filas descartadas: 142', audit_block)
        self.assertIn('Hallazgos severos: 32', audit_block)
        self.assertIn('Hallazgos moderados: 78', audit_block)
        self.assertIn('Hallazgos leves: 145', audit_block)
        self.assertIn('Sistema Cardiovascular', audit_block)
        self.assertIn('Sistema Digestivo', audit_block)
        self.assertIn('Sistema Endocrino', audit_block)
        self.assertIn('Status: ok', audit_block)
        # Método real ("ambos" se renderiza como "tablas + fallback textual")
        self.assertIn('Método: tablas + fallback textual', audit_block)

        # 4) NO debe haber ceros donde sí hay datos
        self.assertNotIn('Páginas procesadas: 0', audit_block)
        self.assertNotIn('Filas útiles: 0', audit_block)
        self.assertNotIn('Sistemas detectados: (ninguno)', audit_block)
        self.assertNotIn('Método: ninguno', audit_block)

        # 5) El cache NO se invalidó (no hubo re-parse)
        self.assertEqual(arch.biocuantico_parse_status, 'ok')
        self.assertTrue(arch.biocuantico_summary_json)
        self.assertTrue(arch.biocuantico_summary_text)

    def test_audit_block_uses_runtime_when_parser_fresh(self):
        """Cuando el parser SÍ corre en este request, la auditoría sigue
        usando el payload runtime (no leemos del JSON dos veces)."""
        import base64
        arch = self.env['valoracion.archivo.cliente'].create({
            'valoracion_id': self.valoracion.id,
            'file_name': 'fresh.txt',
            'file_data': base64.b64encode(
                b"Informe BIOCUANTICO Resonancia Cuantica\n"
                b"Sistema Digestivo:\n"
                b"  Helicobacter: positivo - Severo\n"
                b"  Acidez: alta - Moderado\n"
                b"  Transito: lento - Leve\n"
            ),
            'extracted_text': (
                'Informe BIOCUANTICO Resonancia Cuantica\n'
                'Sistema Digestivo:\n'
                '  Helicobacter: positivo - Severo\n'
                '  Acidez: alta - Moderado\n'
                '  Transito: lento - Leve\n'
            ),
            'extraction_status': 'ok',
            # status = not_parsed → el flujo SÍ va a correr el parser
            'biocuantico_parse_status': 'not_parsed',
        })
        block, images, aviso, archivos_audit = (
            self.valoracion._build_archivos_cliente_block()
        )
        self.assertEqual(len(archivos_audit), 1)
        entry = archivos_audit[0]
        bq = entry.get('biocuantico') or {}
        audit_block = bq.get('audit_block_text') or ''
        # El parser corrió: status debe ser ok o partial; audit no es cero
        self.assertIn(arch.biocuantico_parse_status, ('ok', 'partial'))
        self.assertIn('Status:', audit_block)
        self.assertNotIn('Método: ninguno', audit_block)

    def test_audit_handles_old_cache_without_metodo(self):
        """Cache antiguo (pre-bugfix) no tenía 'metodo' / 'status' en el
        JSON. La reconstrucción debe usar fallbacks legibles."""
        import json
        old_json = json.dumps({
            "sistemas_afectados": [
                {"sistema_code": "digestivo",
                 "sistema_label": "Sistema Digestivo",
                 "severity_max": 4, "hallazgos_count": 5},
            ],
            "estadisticas_parser": {
                "paginas_procesadas": 50,
                "tablas_detectadas": 30,
                "filas_utiles": 80,
                "filas_descartadas": 20,
                "hallazgos_severos": 5,
                "hallazgos_moderados": 10,
                "hallazgos_leves": 15,
            },
            # SIN 'metodo' ni 'status'
        })
        payload = BioCuanticoMasterSummary.payload_from_json(
            old_json, archivo_status='ok',
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload['status'], 'ok')   # fallback al status del archivo
        self.assertEqual(payload['metodo'], 'cacheado')  # marker para Admin
        self.assertEqual(payload['estadisticas']['filas_utiles'], 80)
        # render produce bloque legible (no rompe)
        block = BioCuanticoMasterSummary.render_audit_block(
            payload, summary_text='resumen viejo', archivo_label='old.pdf',
        )
        self.assertIn('Páginas procesadas: 50', block)
        self.assertIn('Filas útiles: 80', block)
        self.assertIn('Método: cacheado', block)

    def test_audit_handles_invalid_json_gracefully(self):
        """Si el JSON está corrupto, payload_from_json devuelve None y el
        caller cae al stub mínimo sin romper."""
        self.assertIsNone(BioCuanticoMasterSummary.payload_from_json(
            '{not valid', archivo_status='ok',
        ))
        self.assertIsNone(BioCuanticoMasterSummary.payload_from_json(
            None, archivo_status='ok',
        ))
        self.assertIsNone(BioCuanticoMasterSummary.payload_from_json(
            '', archivo_status='ok',
        ))


# =====================================================================
# Fase 2.4 — Refinamiento de calidad del resumen
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestFase24QualityRefinement(TransactionCase):
    """Tests de las 5 mejoras de calidad de Fase 2.4:
      1) Nuevo sistema Toxicidad / Metales pesados.
      2) Dedup exacto preservado.
      3) Ranking ponderado (severos × 5 + moderados × 3 + leves × 1).
      4) Penalización a sistemas con leves dominantes.
      5) Boost por antecedente del cliente.
      6) Max 6 prioridades; mínimo 3 si hay info.
      7) Severidad suavizada (no severo automático en rangos narrow).
    """

    # -----------------------------------------------------------------
    # Item 1 — Toxicidad / Metales pesados
    # -----------------------------------------------------------------
    def test_aluminio_se_clasifica_como_toxicidad(self):
        """Aluminio NO debe quedar en digestivo aunque haya carry-forward
        de un encabezado 'Sistema Digestivo'.
        """
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Digestivo', '', ''],
            ['Aluminio', '85', '0 - 10'],
            ['Mercurio', '0.5', '0 - 0.1'],
            ['Plomo', '50', '0 - 5'],
            ['Helicobacter', '1.5', '0 - 1'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        by_param = {m['parametro']: m for m in p['mediciones']}
        self.assertEqual(by_param['Aluminio']['sistema_code'], 'toxicidad')
        self.assertEqual(by_param['Mercurio']['sistema_code'], 'toxicidad')
        self.assertEqual(by_param['Plomo']['sistema_code'], 'toxicidad')
        # Helicobacter SÍ queda en digestivo (carry-forward intacto)
        self.assertEqual(by_param['Helicobacter']['sistema_code'], 'digestivo')

    def test_otros_metales_pesados_capturados(self):
        """arsénico, cadmio, níquel también capturan."""
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Cardiovascular', '', ''],
            ['Arsénico', '15', '0 - 5'],
            ['Cadmio', '10', '0 - 2'],
            ['Níquel', '20', '0 - 8'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        for m in p['mediciones']:
            self.assertEqual(
                m['sistema_code'], 'toxicidad',
                "%s esperaba toxicidad, got %s" % (m['parametro'], m['sistema_code']),
            )

    # -----------------------------------------------------------------
    # Item 2 — Dedup exacto preservado
    # -----------------------------------------------------------------
    def test_dedup_exacto_mismo_parametro_valor_rango(self):
        """Mismo (parámetro, valor, rango) idéntico repetido en distintas
        tablas → una sola entrada.
        """
        tables = [
            [['Parámetro', 'Valor', 'Rango'],
             ['Sistema Digestivo', '', ''],
             ['Helicobacter', '1.5', '0 - 1']],
            [['Helicobacter', '1.5', '0 - 1']],  # repetido en otra tabla
            [['Helicobacter', '1.5', '0 - 1']],  # de nuevo
        ]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        helic = [m for m in p['mediciones']
                 if (m.get('parametro') or '').lower() == 'helicobacter']
        self.assertEqual(len(helic), 1, "Dedup cross-table falló: %d" % len(helic))

    # -----------------------------------------------------------------
    # Items 3+4 — Ranking ponderado + penalización
    # -----------------------------------------------------------------
    def test_sistema_pocos_severos_supera_a_muchos_leves(self):
        """Un sistema con 2 severos debe rankear más alto que uno con
        10 leves (peso severo × 5 vs leve × 1, además de la penalización
        por leves dominantes).
        """
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            # Cardiovascular: 10 LEVES (ruidoso, debería penalizarse)
            ['Sistema Cardiovascular', '', ''],
        ] + [['param_leve_%d' % i, '105', '0 - 100'] for i in range(10)] + [
            # Endocrino: 2 SEVEROS
            ['Sistema Endocrino', '', ''],
            ['TSH', '15', '0.5 - 4.5'],         # ~233% out → severo
            ['Cortisol', '120', '5 - 25'],      # ~380% out → severo
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(p, env=self.env, antecedente_text=None)
        prios = ms['json']['prioridades_funcionales']
        # Endocrino debe rankear MÁS ALTO que cardiovascular
        endo_idx = next((i for i, pr in enumerate(prios)
                        if pr['sistema_code'] == 'endocrino'), None)
        cardio_idx = next((i for i, pr in enumerate(prios)
                          if pr['sistema_code'] == 'cardiovascular'), None)
        self.assertIsNotNone(endo_idx, "Endocrino no entró en prioridades")
        if cardio_idx is not None:
            self.assertLess(endo_idx, cardio_idx,
                            "Endocrino (2 severos) debe rankear arriba de "
                            "Cardio (muchos leves)")

    # -----------------------------------------------------------------
    # Item 5 — Boost por antecedente
    # -----------------------------------------------------------------
    def test_antecedente_boost_marca_sistemas(self):
        """Cuando el antecedente menciona hipotiroidismo, el sistema
        endocrino debe quedar marcado boosted_por_antecedente=True."""
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Endocrino', '', ''],
            ['TSH', '5.5', '0.5 - 4.5'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(
            p, env=self.env,
            antecedente_text='Paciente con hipotiroidismo controlado.',
        )
        endo = next((pr for pr in ms['json']['prioridades_funcionales']
                     if pr['sistema_code'] == 'endocrino'), None)
        self.assertIsNotNone(endo)
        self.assertTrue(endo['boosted_por_antecedente'])

    # -----------------------------------------------------------------
    # Item 6 — Max 6 prioridades
    # -----------------------------------------------------------------
    def test_maximo_6_prioridades_funcionales(self):
        """Aunque haya más de 6 sistemas con hallazgos clínicos, el output
        de prioridades_funcionales debe tener máximo 6 elementos.
        """
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
        ]]
        # 10 sistemas, cada uno con un severo
        sistemas = [
            ('Sistema Cardiovascular', 'HDL'),
            ('Sistema Digestivo', 'Helicobacter'),
            ('Sistema Endocrino', 'TSH'),
            ('Sistema Inmune', 'Defensas'),
            ('Sistema Nervioso', 'Eq autonómico'),
            ('Sistema Respiratorio', 'CV1'),
            ('Sistema Urinario', 'Creatinina'),
            ('Sistema Reproductor', 'Hormona X'),
            ('Sistema Musculoesquelético', 'Densidad'),
            ('Sistema Tegumentario', 'Param X'),
        ]
        for header, param in sistemas:
            tables[0].append([header, '', ''])
            tables[0].append([param, '999', '0 - 10'])
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(p, env=self.env)
        prios = ms['json']['prioridades_funcionales']
        self.assertLessEqual(len(prios), 6,
                             "Más de 6 prioridades: %d" % len(prios))
        self.assertGreaterEqual(len(prios), 3,
                                "Menos de 3 prioridades con info suficiente: %d"
                                % len(prios))

    def test_cada_prioridad_tiene_estructura_completa(self):
        """Cada prioridad lleva: sistema, severidad, hallazgos clave,
        razón funcional, relación con antecedente."""
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Digestivo', '', ''],
            ['Helicobacter', '1.5', '0 - 1'],
            ['Acidez', '8', '4 - 7'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(p, env=self.env,
                                            antecedente_text='digestivo')
        for pr in ms['json']['prioridades_funcionales']:
            for k in ('sistema_code', 'sistema_label', 'severidad',
                      'hallazgos_clave', 'razon_funcional',
                      'relacion_antecedente'):
                self.assertIn(k, pr, "Falta '%s' en prioridad" % k)

    # -----------------------------------------------------------------
    # Item 7 — Severidad suavizada
    # -----------------------------------------------------------------
    def test_severidad_suavizada_no_severo_automatico_en_rango_narrow(self):
        """Un valor con pequeña desviación absoluta en un rango estrecho
        NO debe darse como severo."""
        # 1.05 vs 0.99-1.00: 5% sobre el bound, pero la desviación absoluta
        # respecto al centro del rango es pequeña → leve, no severo.
        e, s = BioCuanticoParser._classify_by_numeric_range('1.05', '0.99 - 1.00')
        self.assertEqual(e, 'leve')
        self.assertEqual(s, 2)

    def test_severidad_caso_real_usuario(self):
        """1.425 vs 0,431-1,329 (caso real del log de usuario): leve."""
        e, s = BioCuanticoParser._classify_by_numeric_range('1.425', '0,431 - 1,329')
        self.assertEqual(e, 'leve')

    # -----------------------------------------------------------------
    # Item 8 — Low relevance downgrade
    # -----------------------------------------------------------------
    def test_low_relevance_downgrade(self):
        """Parámetros estéticos bajan un nivel su severidad."""
        e, s, d = BioCuanticoParser._downgrade_low_relevance(
            'severo', 4, 'Arrugas profundas',
        )
        self.assertTrue(d)
        self.assertEqual(e, 'moderado')
        self.assertEqual(s, 3)
        # Helicobacter NO debe bajarse
        e2, s2, d2 = BioCuanticoParser._downgrade_low_relevance(
            'severo', 4, 'Helicobacter',
        )
        self.assertFalse(d2)
        self.assertEqual(e2, 'severo')


# =====================================================================
# Fase 2.5 — Clasificación por keyword específica gana sobre carry-forward
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestFase25KeywordSpecificityWinsOverCarryForward(TransactionCase):
    """Regresión exacta del bug del usuario:

    En el log real, Sistema Respiratorio recibía hallazgos que no le
    correspondían ("Actividad celular del ojo", "Aminoácidos", "Demanda de
    Sangre Miocardial") porque el carry-forward de la sección respiratoria
    propagaba a filas sin keyword propia.

    Fase 2.5 resuelve esto con:
      a) Verdadero longest-match GLOBAL en _classify_sistema (sin break
         per-sistema).
      b) Expansión de keywords en parser.py y data XML para que cada
         parámetro tenga su keyword específica.
      c) Nuevo sistema 'metabolico' para aminoácidos, perfil lipídico,
         glucemia, vitaminas.
    """

    # Cada caso: (parámetro_raw, sistema_code_esperado, sección_carry_forward).
    # La sección_carry_forward se prepende como encabezado de sistema antes
    # de la fila — debe ser IGNORADA cuando la fila tiene su propia kw.
    USER_EXAMPLES = [
        # Items explícitamente listados por el usuario
        ('Actividad celular del ojo', 'sensorial', 'Sistema Respiratorio'),
        ('Aminoácidos', 'metabolico', 'Sistema Respiratorio'),
        ('Demanda de Sangre Miocardial', 'cardiovascular', 'Sistema Respiratorio'),
        ('Volumen de Oxígeno en la sangre cerebrovascular',
         'cardiovascular', 'Sistema Respiratorio'),
        ('Capacidad residual funcional (FRC)', 'respiratorio', 'Sistema Respiratorio'),
        # Lipídicos / glucemia → metabolico
        ('Triglicéridos', 'metabolico', 'Sistema Cardiovascular'),
        ('HDL', 'metabolico', 'Sistema Cardiovascular'),
        ('LDL', 'metabolico', 'Sistema Cardiovascular'),
        ('Glucosa basal', 'metabolico', 'Sistema Cardiovascular'),
        # Hormonales → endocrino
        ('T3', 'endocrino', 'Sistema Inmune'),
        ('T4 libre', 'endocrino', 'Sistema Inmune'),
        ('Tiroglobulina', 'endocrino', 'Sistema Inmune'),
        ('Progesterona', 'endocrino', 'Sistema Inmune'),
        ('Prolactina', 'endocrino', 'Sistema Inmune'),
        # Metales pesados → toxicidad (regresión Fase 2.4)
        ('Aluminio', 'toxicidad', 'Sistema Digestivo'),
        ('Arsénico', 'toxicidad', 'Sistema Digestivo'),
        ('Mercurio', 'toxicidad', 'Sistema Digestivo'),
        ('Plomo', 'toxicidad', 'Sistema Digestivo'),
        # Tegumentario / estético
        ('Colágeno', 'tegumentario', 'Sistema Musculoesquelético'),
        ('Humectación cutánea', 'tegumentario', 'Sistema Musculoesquelético'),
        ('Arrugas profundas', 'tegumentario', 'Sistema Sensorial'),
        ('Bolsas en ojos', 'tegumentario', 'Sistema Sensorial'),
    ]

    def test_keyword_specifica_gana_sobre_carry_forward(self):
        """Test parametrizado con TODOS los ejemplos del usuario.
        Cada fila se ubica DESPUÉS de un encabezado de sección que apunta
        a un sistema DIFERENTE al que debe clasificar. Si la fila acaba
        en el sistema de la sección (carry-forward), es BUG.
        """
        # Construir una tabla larga con todas las filas + headers intercalados
        rows = [['Parámetro', 'Valor', 'Rango']]
        for parametro, _expected, seccion in self.USER_EXAMPLES:
            rows.append([seccion, '', ''])  # header de sección "trampa"
            rows.append([parametro, '50', '0 - 100'])

        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=[rows], env=self.env,
        )
        by_param = {m['parametro']: m for m in p['mediciones']}

        failures = []
        for parametro, expected_sis, seccion in self.USER_EXAMPLES:
            m = by_param.get(parametro)
            if m is None:
                failures.append("%s: NO encontrado" % parametro)
                continue
            got_sis = m.get('sistema_code')
            if got_sis != expected_sis:
                failures.append(
                    "'%s' bajo '%s' → %s, esperado %s" % (
                        parametro, seccion, got_sis, expected_sis,
                    )
                )

        self.assertFalse(failures,
                         "Clasificaciones incorrectas:\n  - " +
                         "\n  - ".join(failures))

    def test_metabolico_es_sistema_real_en_bd(self):
        """El nuevo sistema 'metabolico' debe existir en BD tras -u valoracion."""
        sistemas = self.env['valoracion.biocuantico.sistema'].sudo().search([
            ('code', '=', 'metabolico'),
            ('active', '=', True),
        ])
        self.assertEqual(len(sistemas), 1,
                         "Sistema 'metabolico' no existe en BD")
        # Debe tener keywords nutricionales
        kws = sistemas._get_keywords_list()
        for must_have in ('aminoacido', 'hdl', 'glucosa', 'trigliceridos'):
            self.assertIn(must_have, kws,
                          "Keyword '%s' falta en sistema metabolico" % must_have)

    def test_longest_match_global_no_per_sistema_break(self):
        """Cuando un sistema tiene varias keywords que matchean, debe usar
        la MÁS LARGA para competir contra otros sistemas.

        Ejemplo: cardiovascular tiene 'vascular' (8) y 'cerebrovascular' (15).
        Una fila "cerebrovascular" debe ganar con length 15, no 8.
        """
        # Bypass directo del classifier para precisión
        catalog = [
            ('test_a', 'Sistema A', ['vascular', 'cerebrovascular']),
            ('test_b', 'Sistema B', ['vasc']),  # match más corto
        ]
        result = BioCuanticoParser._classify_sistema(
            ['cerebrovascular sangre'], catalog, None,
        )
        self.assertEqual(result, ('test_a', 'Sistema A'),
                         "Longest-match dentro del sistema falló")

    def test_aluminio_en_seccion_digestiva_no_se_pega_a_digestivo(self):
        """Regresión Fase 2.4 reforzada: Aluminio dentro de sección
        digestiva debe ir a toxicidad por kw específica."""
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Digestivo', '', ''],
            ['Helicobacter', '1.5', '0 - 1'],   # digestivo (kw helicobacter)
            ['Aluminio', '85', '0 - 10'],        # toxicidad (kw aluminio)
            ['Acidez gástrica', '8', '4 - 7'],  # digestivo (kw acidez gastrica)
            ['Mercurio', '0.5', '0 - 0.1'],     # toxicidad
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        by = {m['parametro']: m['sistema_code'] for m in p['mediciones']}
        self.assertEqual(by['Aluminio'], 'toxicidad')
        self.assertEqual(by['Mercurio'], 'toxicidad')
        self.assertEqual(by['Helicobacter'], 'digestivo')
        self.assertEqual(by['Acidez gástrica'], 'digestivo')


# =====================================================================
# Fase 2.6 — Normalización fuerte de texto sucio del flujo real
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestFase26DirtyTextNormalization(TransactionCase):
    """Regresión exacta del bug post-2.5 reportado por el usuario:

    Tests aislados clasifican bien, pero el flujo real seguía mostrando
    'Actividad celular del ojo' bajo Sistema Respiratorio. Causa: el texto
    extraído de PDFs llega con saltos de línea, espacios dobles, NBSP y
    caracteres invisibles. Las keywords (ej. 'celular del ojo') no
    matcheaban porque '\\n' rompía el substring.

    Fase 2.6 normaliza todo eso antes de clasificar.
    """

    # ---------------------------------------------------------------
    # Normalización fuerte
    # ---------------------------------------------------------------
    def test_normaliza_newline_a_espacio(self):
        n = BioCuanticoParser._normalize('Actividad celular del\n ojo')
        self.assertEqual(n, 'actividad celular del ojo')

    def test_normaliza_tab_y_cr(self):
        n = BioCuanticoParser._normalize('HDL\r\nbajo')
        self.assertEqual(n, 'hdl bajo')
        n2 = BioCuanticoParser._normalize('  T3 \t total  ')
        self.assertEqual(n2, 't3 total')

    def test_normaliza_espacios_multiples(self):
        n = BioCuanticoParser._normalize('Demanda  de   Sangre    Miocardial')
        self.assertEqual(n, 'demanda de sangre miocardial')

    def test_normaliza_nbsp_a_espacio(self):
        """U+00A0 (non-breaking space) debe convertirse a espacio normal."""
        n = BioCuanticoParser._normalize('Fatiga visual')
        self.assertEqual(n, 'fatiga visual')

    def test_normaliza_caracteres_invisibles_zero_width(self):
        """ZW space / BOM / soft-hyphen deben eliminarse."""
        for inv in ('​', '‌', '‍', '﻿', '­'):
            n = BioCuanticoParser._normalize('HDL%sbajo' % inv)
            self.assertEqual(n, 'hdlbajo',
                             "Invisible %r no eliminado" % inv)

    def test_normaliza_acentos(self):
        n = BioCuanticoParser._normalize('Volumen de Oxígeno cerebrovascular')
        self.assertEqual(n, 'volumen de oxigeno cerebrovascular')

    # ---------------------------------------------------------------
    # Clasificación con texto sucio + carry-forward "trampa"
    # ---------------------------------------------------------------
    def _classify_under_respiratorio(self, raw_param):
        """Construye una tabla con encabezado 'Sistema Respiratorio' y la
        fila del parámetro, parsea, devuelve la medición resultante.
        """
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Respiratorio', '', ''],
            [raw_param, '50', '0 - 100'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        return p['mediciones'][0] if p['mediciones'] else None

    def test_actividad_celular_del_ojo_con_newline(self):
        m = self._classify_under_respiratorio('Actividad celular del\n ojo')
        self.assertIsNotNone(m)
        self.assertEqual(m['sistema_code'], 'sensorial')
        self.assertEqual(m['sistema_source'], 'keyword')
        self.assertEqual(m['keyword_matched'], 'celular del ojo')

    def test_demanda_de_sangre_miocardial_con_newline(self):
        m = self._classify_under_respiratorio('Demanda de Sangre\nMiocardial')
        self.assertIsNotNone(m)
        self.assertEqual(m['sistema_code'], 'cardiovascular')
        self.assertEqual(m['sistema_source'], 'keyword')

    def test_fatiga_visual(self):
        m = self._classify_under_respiratorio('Fatiga visual')
        self.assertIsNotNone(m)
        self.assertEqual(m['sistema_code'], 'sensorial')
        self.assertEqual(m['sistema_source'], 'keyword')

    def test_fatiga_visual_con_nbsp(self):
        m = self._classify_under_respiratorio('Fatiga visual')
        self.assertIsNotNone(m)
        self.assertEqual(m['sistema_code'], 'sensorial')

    def test_volumen_oxigeno_cerebrovascular_con_newline(self):
        m = self._classify_under_respiratorio(
            'Volumen de Oxígeno en la sangre\ncerebrovascular',
        )
        self.assertIsNotNone(m)
        self.assertEqual(m['sistema_code'], 'cardiovascular')
        self.assertEqual(m['sistema_source'], 'keyword')

    def test_parametro_sin_keyword_si_cae_a_carry_forward(self):
        """Control negativo: un parámetro genérico sin keyword propia SÍ
        debe caer al carry-forward de la sección."""
        m = self._classify_under_respiratorio('Parámetro Z sin matching')
        self.assertIsNotNone(m)
        self.assertEqual(m['sistema_code'], 'respiratorio')
        self.assertEqual(m['sistema_source'], 'carry_forward')
        self.assertIsNone(m['keyword_matched'])

    # ---------------------------------------------------------------
    # Auditoría completa: cada hallazgo lleva la traza de cómo se clasificó
    # ---------------------------------------------------------------
    def test_cada_medicion_lleva_campos_auditoria(self):
        """Toda medición debe llevar sistema_source, keyword_matched,
        parametro_normalizado en el dict."""
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Respiratorio', '', ''],
            ['Actividad celular del ojo', '50', '0 - 100'],
            ['Capacidad residual funcional', '1.8', '2.0 - 3.5'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        for m in p['mediciones']:
            for k in ('sistema_source', 'keyword_matched', 'parametro_normalizado'):
                self.assertIn(k, m, "Falta campo de auditoría '%s'" % k)
            self.assertIn(m['sistema_source'],
                          ('keyword', 'carry_forward', 'fallback'))

    def test_master_summary_propaga_auditoria(self):
        """El JSON resultante de MasterSummary.build debe contener los
        campos de auditoría en cada hallazgo."""
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Respiratorio', '', ''],
            ['Actividad celular del ojo', '85', '0 - 50'],  # severo + kw match
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(p, env=self.env)
        self.assertTrue(ms['available'])
        # Buscar el hallazgo en principales o secundarios
        all_items = (ms['json']['hallazgos_principales']
                     + ms['json']['hallazgos_secundarios'])
        target = next((i for i in all_items
                       if i.get('parametro') == 'Actividad celular del ojo'),
                      None)
        self.assertIsNotNone(target)
        self.assertEqual(target.get('sistema_source'), 'keyword')
        self.assertEqual(target.get('keyword_matched'), 'celular del ojo')

    def test_version_manifest_2_0_3(self):
        """Validación de control de versiones: el manifest debe estar al
        menos en 18.0.2.0.3 para este fix."""
        modulo = self.env.ref('base.module_valoracion', raise_if_not_found=False)
        if modulo is None:
            modulo = self.env['ir.module.module'].search(
                [('name', '=', 'valoracion')], limit=1,
            )
        self.assertTrue(modulo)
        # version aquí es la del manifest (Odoo lo lee como latest_version
        # con el prefijo serie). El test acepta versiones >= 2.0.3.
        v = modulo.latest_version or ''
        # latest_version típicamente viene como '18.0.X.Y.Z'
        parts = v.split('.')
        if len(parts) >= 5:
            major, minor, x, y, z = parts[:5]
            self.assertEqual('%s.%s' % (major, minor), '18.0')
            # parsear como tupla numérica
            ver_tuple = (int(x), int(y), int(z))
            self.assertGreaterEqual(ver_tuple, (2, 0, 3),
                                    "Manifest version %s < 18.0.2.0.3" % v)


# =====================================================================
# Fase 2.7 — Dedup CROSS-SISTEMA con resolución de conflicto
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestFase27CrossSistemaDedup(TransactionCase):
    """El bug que motivó este test:

    En logs reales, el mismo parámetro normalizado aparecía en MÁS DE UN
    sistema simultáneamente:
        Sistema Metabólico contiene: Actividad celular del ojo, Fatiga visual
        Sistema Respiratorio contiene: Actividad celular del ojo, Demanda
            de Sangre Miocardial, Fatiga visual

    Causa: cuando el PDF contiene el mismo parámetro en varias secciones,
    algunas instancias caen a carry_forward (sin matchear kw) y mi dedup
    anterior (que incluía sistema_code en la key) NO las consolidaba.

    Fase 2.7 añade una Pasada C que agrupa por parametro_normalizado
    canónico (incluyendo fragmentos prefijo) y resuelve conflictos:
      1) sistema_source='keyword' gana sobre 'carry_forward'
      2) keyword_matched más larga gana
      3) mayor severidad gana
      4) primera aparición gana
    """

    def _build_user_scenario(self):
        """Reproduce el caso exacto del log del usuario: parámetros que
        aparecen en múltiples secciones con texto variable."""
        return [[
            ['Parámetro', 'Valor', 'Rango'],
            # Sección respiratoria con parámetros que NO son respiratorios
            ['Sistema Respiratorio', '', ''],
            ['Actividad celular', '60', '0 - 50'],  # fragmento — carry_forward resp
            ['Fatiga visual', '0.8', '0 - 0.3'],     # kw → sensorial
            ['Demanda de Sangre Miocardial', '80', '0 - 50'],  # kw → cardio
            # Sección metabólica con los mismos parámetros (variantes)
            ['Sistema Metabólico / Nutricional', '', ''],
            ['Actividad celular', '55', '0 - 50'],  # fragmento — carry_forward meta
            ['Fatiga visual', '0.8', '0 - 0.3'],     # dup exacto → descart
            # Sección sensorial donde APARECE el nombre completo
            ['Sistema Sensorial', '', ''],
            ['Actividad celular del ojo', '60', '0 - 50'],  # KW match
            ['Fatiga visual', '0.9', '0 - 0.3'],     # dup mismo sistema → descart
            # Otros sistemas
            ['Sistema Digestivo', '', ''],
            ['Aluminio', '85', '0 - 10'],
            ['Helicobacter', '1.5', '0 - 1'],
        ]]

    def test_actividad_celular_del_ojo_solo_en_sensorial(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        # Solo debe haber UNA entrada cuyo nombre normalizado sea
        # 'actividad celular del ojo' o 'actividad celular' (fragmento).
        # El fragmento debe haberse fusionado con la versión canónica.
        matches = [m for m in p['mediciones']
                   if (m.get('parametro_normalizado') or '').startswith('actividad celular')]
        self.assertEqual(len(matches), 1,
                         "Esperaba 1 entrada para 'actividad celular*', got %d" % len(matches))
        self.assertEqual(matches[0]['sistema_code'], 'sensorial')
        self.assertEqual(matches[0]['sistema_source'], 'keyword')
        # parametro_normalizado debe ser la versión COMPLETA (canónica)
        self.assertEqual(matches[0]['parametro_normalizado'],
                         'actividad celular del ojo')

    def test_fatiga_visual_solo_en_sensorial(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        matches = [m for m in p['mediciones']
                   if m.get('parametro_normalizado') == 'fatiga visual']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['sistema_code'], 'sensorial')

    def test_demanda_de_sangre_miocardial_solo_en_cardiovascular(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        matches = [m for m in p['mediciones']
                   if m.get('parametro_normalizado') == 'demanda de sangre miocardial']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['sistema_code'], 'cardiovascular')

    def test_aluminio_solo_en_toxicidad(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        matches = [m for m in p['mediciones']
                   if m.get('parametro_normalizado') == 'aluminio']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['sistema_code'], 'toxicidad')

    def test_helicobacter_se_preserva_intacto(self):
        """Regresión del bug de variable leftover: helicobacter debe
        sobrevivir a Pasada C, no descartarse por colisión con otro key."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        matches = [m for m in p['mediciones']
                   if m.get('parametro_normalizado') == 'helicobacter']
        self.assertEqual(len(matches), 1,
                         "Helicobacter no debió perderse en Pasada C")
        self.assertEqual(matches[0]['sistema_code'], 'digestivo')

    def test_descartados_tienen_motivo_y_ganador(self):
        """Las versiones perdedoras del conflicto deben quedar en
        descartados_dedup con motivo='duplicado_conflicto_sistema' y el
        sistema/source ganador anotados."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        descartados = p.get('descartados_dedup') or []
        # Debe haber descartados con motivo='duplicado_conflicto_sistema'
        conflictos = [d for d in descartados
                      if d.get('motivo_descarte') == 'duplicado_conflicto_sistema']
        self.assertGreater(len(conflictos), 0,
                           "No hay descartados por conflicto cross-sistema")
        # Cada descartado por conflicto lleva el ganador
        for d in conflictos:
            self.assertIn('ganador_sistema_code', d)
            self.assertIn('ganador_sistema_source', d)
            self.assertTrue(d.get('ganador_sistema_code'))

    def test_auditoria_por_hallazgo(self):
        """Cada medición lleva los 7 campos de auditoría (Fase 2.6+2.7)."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        REQUIRED = (
            'parametro', 'parametro_normalizado', 'sistema_code',
            'sistema_label', 'sistema_source', 'keyword_matched',
            'carry_forward_was', 'pagina', 'tabla_idx', 'row_idx',
        )
        for m in p['mediciones']:
            for k in REQUIRED:
                self.assertIn(k, m,
                              "Falta campo auditoría '%s' en medición %r"
                              % (k, m.get('parametro')))

    def test_master_summary_incluye_descartados_con_motivo(self):
        """parametros_descartados en el JSON resultante debe incluir tanto
        los low_relevance como los cross_sistema, cada uno con motivo."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._build_user_scenario(), env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(p, env=self.env)
        descartados = ms['json'].get('parametros_descartados') or []
        # Cada item debe llevar 'motivo_descarte'
        for d in descartados:
            self.assertIn('motivo_descarte', d,
                          "Descartado sin motivo_descarte: %r" % d)
        # Debe haber al menos uno con motivo='duplicado_conflicto_sistema'
        motivos = {d.get('motivo_descarte') for d in descartados}
        self.assertIn('duplicado_conflicto_sistema', motivos)

    def test_version_manifest_2_0_4(self):
        """Manifest debe estar en versión >= 18.0.2.0.4 para Fase 2.7."""
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = parts[2], parts[3], parts[4]
            self.assertGreaterEqual((int(x), int(y), int(z)), (2, 0, 4),
                                    "Manifest version %s < 18.0.2.0.4" % v)


# =====================================================================
# Fase 2.8 — Ruido no-clínico + contexto-aware
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestFase28NoiseAndContextResolution(TransactionCase):
    """Bug del log del usuario:
        Sistema Endocrino [moderado] muestra:
          - Capacidad residual funcional (FRC)
          - Complacencia
          - Ilustración

    Causa: parámetros respiratorios y de ruido caían en Endocrino por
    carry-forward. Fase 2.8 introduce:
      - NOISE_PATTERNS: filtra parámetros no-clínicos (ilustración,
        figura, imagen, etc.) a parametros_descartados con motivo
        'ruido_no_clinico'.
      - AMBIGUOUS_PATTERNS: parámetros como 'complacencia' se resuelven
        por contexto (texto agregado de la tabla); si la tabla tiene
        contexto pulmonar → respiratorio.
    """

    def _user_real_table(self):
        """Tabla que reproduce el caso real reportado: encabezado endocrino
        seguido de parámetros que no son endocrinos."""
        return [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Endocrino', '', ''],
            ['Capacidad residual funcional (FRC)', '1.8', '2.0 - 3.5'],
            ['Complacencia', '0.05', '0.1 - 0.3'],
            ['Ilustración', '3', '0 - 5'],
            ['Figura 4', '2', '0 - 5'],
            ['Imagen del análisis', '1', '0 - 5'],
            ['Ejemplo: parámetro', '2', '0 - 5'],
            ['TSH', '6.5', '0.5 - 4.5'],
            ['T4 libre', '0.5', '0.8 - 1.8'],
        ]]

    def test_ilustracion_va_a_ruido_no_clinico(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._user_real_table(), env=self.env,
        )
        # Ningún parámetro normalizado en mediciones contiene 'ilustracion'
        for m in p['mediciones']:
            self.assertNotIn(
                'ilustracion', m['parametro_normalizado'] or '',
                "Ilustración llegó a mediciones: %r" % m['parametro'],
            )
        # Debe estar en descartados con motivo='ruido_no_clinico'
        noise = [d for d in p.get('descartados_dedup') or []
                 if d.get('motivo_descarte') == 'ruido_no_clinico'
                 and 'ilustracion' in (d.get('parametro_normalizado') or '')]
        self.assertEqual(len(noise), 1)
        # Lleva sistema_original (el carry-forward que tenía antes)
        self.assertEqual(noise[0]['sistema_original'], 'endocrino')

    def test_figura_imagen_ejemplo_son_descartados(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._user_real_table(), env=self.env,
        )
        noise = [d for d in p.get('descartados_dedup') or []
                 if d.get('motivo_descarte') == 'ruido_no_clinico']
        params_noise = {d['parametro_normalizado'] for d in noise}
        # Cada patrón de ruido debe estar capturado
        self.assertTrue(any('ilustracion' in p_ for p_ in params_noise),
                        "Ilustración no descartada")
        self.assertTrue(any('figura' in p_ for p_ in params_noise),
                        "Figura no descartada")
        self.assertTrue(any('imagen' in p_ for p_ in params_noise),
                        "Imagen no descartada")
        self.assertTrue(any('ejemplo' in p_ for p_ in params_noise),
                        "Ejemplo no descartado")

    def test_capacidad_residual_funcional_va_a_respiratorio(self):
        """FRC siempre debe ir a respiratorio por kw, no a endocrino
        por carry-forward."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._user_real_table(), env=self.env,
        )
        frc = [m for m in p['mediciones']
               if (m['parametro_normalizado'] or '').startswith('capacidad residual')]
        self.assertEqual(len(frc), 1)
        self.assertEqual(frc[0]['sistema_code'], 'respiratorio')
        self.assertEqual(frc[0]['sistema_source'], 'keyword')

    def test_complacencia_va_a_respiratorio_por_contexto(self):
        """Complacencia es ambiguo. En esta tabla hay contexto pulmonar
        (FRC, capacidad residual) → respiratorio. NUNCA endocrino aunque
        el carry-forward sea endocrino."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._user_real_table(), env=self.env,
        )
        comp = [m for m in p['mediciones']
                if m['parametro_normalizado'] == 'complacencia']
        self.assertEqual(len(comp), 1)
        self.assertEqual(comp[0]['sistema_code'], 'respiratorio')
        self.assertEqual(comp[0]['sistema_source'], 'context_resolved')

    def test_endocrino_no_contiene_FRC_complacencia_ilustracion(self):
        """Validación de fondo: el sistema endocrino debe contener SOLO
        parámetros endocrinos reales (TSH, T4 libre)."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._user_real_table(), env=self.env,
        )
        endo_params = sorted(
            m['parametro_normalizado'] for m in p['mediciones']
            if m['sistema_code'] == 'endocrino'
        )
        # SOLO los endocrinos reales
        self.assertEqual(endo_params, ['t4 libre', 'tsh'])
        # FRC, complacencia, ilustración NO deben estar
        for forbidden in ('capacidad residual funcional', 'complacencia',
                          'ilustracion'):
            for p_ in endo_params:
                self.assertNotIn(forbidden, p_)

    def test_descartados_ruido_llevan_sistema_original_y_pagina(self):
        """Auditoría completa de descartados por ruido."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._user_real_table(), env=self.env,
        )
        noise = [d for d in p.get('descartados_dedup') or []
                 if d.get('motivo_descarte') == 'ruido_no_clinico']
        for d in noise:
            self.assertIn('sistema_original', d)
            self.assertIn('pagina', d)
            self.assertIn('parametro', d)
            self.assertIn('parametro_normalizado', d)

    def test_master_summary_excluye_ruido_de_prioridades(self):
        """parametros_descartados en JSON debe incluir los noise; ningún
        ruido debe llegar a prioridades_funcionales o hallazgos."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._user_real_table(), env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(p, env=self.env)
        j = ms['json']
        # Ningún ruido en hallazgos
        all_findings = (j['hallazgos_principales']
                        + j['hallazgos_secundarios']
                        + j['parametros_anormales'])
        for h in all_findings:
            for noise_w in ('ilustracion', 'figura', 'imagen', 'ejemplo'):
                self.assertNotIn(noise_w, h.get('parametro_normalizado') or '',
                                 "Ruido %r filtró a hallazgos: %r"
                                 % (noise_w, h.get('parametro')))
        # parametros_descartados incluye al menos uno con motivo='ruido_no_clinico'
        motivos = {d.get('motivo_descarte') for d in j.get('parametros_descartados') or []}
        self.assertIn('ruido_no_clinico', motivos)

    def test_is_noise_unitario(self):
        """Test directo del classifier de ruido."""
        # Verdaderos noise
        for p_ in ('Ilustración', 'Figura 3', 'Imagen del análisis',
                   'Ejemplo: parámetro X', 'Logo del laboratorio',
                   'Nota: ver detalle', 'Página 5', 'Header del reporte'):
            self.assertTrue(BioCuanticoParser._is_noise_parametro(p_),
                            "Debió ser noise: %r" % p_)
        # Falsos positivos a evitar
        for p_ in ('Helicobacter', 'TSH', 'Aluminio', 'Capacidad residual',
                   'Demanda de Sangre Miocardial', 'Actividad celular del ojo'):
            self.assertFalse(BioCuanticoParser._is_noise_parametro(p_),
                             "NO debió ser noise: %r" % p_)

    def test_resolve_ambiguous_unitario(self):
        """Test directo del resolver de parámetros ambiguos."""
        # Complacencia con contexto pulmonar → respiratorio
        ctx = 'capacidad residual funcional frc pulmonar alveolar diafragma'
        sis = BioCuanticoParser._resolve_ambiguous_parametro('Complacencia', ctx)
        self.assertEqual(sis, 'respiratorio')
        # Complacencia con contexto vascular → cardiovascular
        ctx = 'arteria aortica arterial circulacion hemodinamica'
        sis = BioCuanticoParser._resolve_ambiguous_parametro('Complacencia', ctx)
        self.assertEqual(sis, 'cardiovascular')
        # Complacencia sin contexto → None
        ctx = 'tsh t4 tiroglobulina cortisol insulina'
        sis = BioCuanticoParser._resolve_ambiguous_parametro('Complacencia', ctx)
        self.assertIsNone(sis)
        # Parámetro no ambiguo → None (no se mete)
        sis = BioCuanticoParser._resolve_ambiguous_parametro('Helicobacter', ctx)
        self.assertIsNone(sis)

    def test_version_manifest_2_0_5(self):
        """Manifest debe estar en versión >= 18.0.2.0.5 para Fase 2.8."""
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = parts[2], parts[3], parts[4]
            self.assertGreaterEqual((int(x), int(y), int(z)), (2, 0, 5),
                                    "Manifest version %s < 18.0.2.0.5" % v)


# =====================================================================
# Fase 2.9 — CRITICAL_PARAM_OVERRIDES
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestFase29CriticalOverrides(TransactionCase):
    """Bug exacto del log del usuario:
        Sistema Respiratorio contiene:
          - Demanda de Sangre Miocardial
          - La tiroglobulina
          - Ojo

    Causa: aunque las keywords expandidas (Fase 2.5) cubrían estos casos,
    en la práctica algunas instancias del PDF tenían texto demasiado corto
    o con prefijo de artículo ('La tiroglobulina') que perdían contra el
    longest-match o no llegaban a registrarse en flujos extremos. La fix
    es una tabla EXPLÍCITA de overrides con prioridad MÁXIMA, antes de
    todo lo demás.
    """

    def _respiratorio_trap_table(self):
        """Tabla con encabezado respiratorio + parámetros que NO son
        respiratorios, mezclados con respiratorios reales."""
        return [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Respiratorio', '', ''],
            ['Ojo', '1.2', '0 - 1'],
            ['La tiroglobulina', '120', '0 - 55'],
            ['Demanda de Sangre Miocardial', '85', '0 - 50'],
            ['Fatiga visual', '0.8', '0 - 0.3'],
            ['Actividad celular del ojo', '60', '0 - 50'],
            # Respiratorios reales (control negativo)
            ['Capacidad residual funcional (FRC)', '1.8', '2.0 - 3.5'],
            ['FEV1', '2.5', '3.0 - 4.5'],
        ]]

    def test_ojo_va_a_sensorial_via_critical_override(self):
        """'Ojo' (3 chars) bajo sección Respiratorio debe ir a sensorial
        por override, NO heredar carry-forward."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._respiratorio_trap_table(), env=self.env,
        )
        ojo = [m for m in p['mediciones'] if m['parametro_normalizado'] == 'ojo']
        self.assertEqual(len(ojo), 1)
        self.assertEqual(ojo[0]['sistema_code'], 'sensorial')
        self.assertEqual(ojo[0]['sistema_source'], 'critical_override')
        self.assertEqual(ojo[0]['keyword_matched'], 'ojo')

    def test_la_tiroglobulina_va_a_endocrino_via_override(self):
        """'La tiroglobulina' (con artículo) bajo Respiratorio → endocrino."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._respiratorio_trap_table(), env=self.env,
        )
        tg = [m for m in p['mediciones']
              if m['parametro_normalizado'] == 'la tiroglobulina']
        self.assertEqual(len(tg), 1)
        self.assertEqual(tg[0]['sistema_code'], 'endocrino')
        self.assertEqual(tg[0]['sistema_source'], 'critical_override')

    def test_demanda_de_sangre_miocardial_va_a_cardiovascular(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._respiratorio_trap_table(), env=self.env,
        )
        d = [m for m in p['mediciones']
             if m['parametro_normalizado'] == 'demanda de sangre miocardial']
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]['sistema_code'], 'cardiovascular')

    def test_fatiga_visual_y_actividad_celular_van_a_sensorial(self):
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._respiratorio_trap_table(), env=self.env,
        )
        by = {m['parametro_normalizado']: m for m in p['mediciones']}
        self.assertEqual(by['fatiga visual']['sistema_code'], 'sensorial')
        self.assertEqual(by['actividad celular del ojo']['sistema_code'],
                         'sensorial')

    def test_respiratorio_contiene_solo_parametros_respiratorios(self):
        """Después del override, Respiratorio SOLO contiene FRC y FEV1.
        Demanda de Sangre Miocardial, Ojo, La tiroglobulina, Fatiga visual,
        Actividad celular del ojo NO deben estar."""
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=self._respiratorio_trap_table(), env=self.env,
        )
        resp = sorted(m['parametro_normalizado']
                      for m in p['mediciones']
                      if m['sistema_code'] == 'respiratorio')
        self.assertEqual(
            resp,
            sorted(['capacidad residual funcional (frc)', 'fev1']),
        )
        # Negativos explícitos
        for forbidden in ('ojo', 'la tiroglobulina',
                          'demanda de sangre miocardial',
                          'fatiga visual', 'actividad celular del ojo'):
            self.assertNotIn(forbidden, resp,
                             "%r coló en respiratorio" % forbidden)

    def test_overrides_unitario_classify_sistema_detail(self):
        """Test directo del classifier con parámetros override.

        Aún con carry-forward de respiratorio activo y celdas que no
        contienen keywords de otros sistemas, el override gana.
        """
        catalog = [
            ('respiratorio', 'Sistema Respiratorio', ['frc']),
            ('sensorial', 'Sistema Sensorial', ['ojo']),
            ('endocrino', 'Sistema Endocrino', ['tiroglobulina']),
        ]
        cf = ('respiratorio', 'Sistema Respiratorio')

        # 'Ojo' aislado → override (en parametro)
        d = BioCuanticoParser._classify_sistema_detail(
            ['ojo', '1.2', '0 - 1'], catalog, cf, parametro='Ojo',
        )
        self.assertEqual(d['sistema_code'], 'sensorial')
        self.assertEqual(d['sistema_source'], 'critical_override')

        # 'La tiroglobulina' → override
        d = BioCuanticoParser._classify_sistema_detail(
            ['la tiroglobulina', '120', '0 - 55'],
            catalog, cf, parametro='La tiroglobulina',
        )
        self.assertEqual(d['sistema_code'], 'endocrino')
        self.assertEqual(d['sistema_source'], 'critical_override')

        # Parámetro NO en override → cae al longest-match normal
        d = BioCuanticoParser._classify_sistema_detail(
            ['frc', '1.8', '2.0 - 3.5'], catalog, cf, parametro='FRC',
        )
        self.assertEqual(d['sistema_code'], 'respiratorio')
        self.assertEqual(d['sistema_source'], 'keyword')

    def test_override_gana_sobre_context_resolved(self):
        """Si un parámetro está en CRITICAL_OVERRIDES, no debe ser
        sobreescrito por context_resolved después."""
        # 'Ojo' está en CRITICAL_OVERRIDES → sensorial.
        # Aunque la tabla tenga contexto pulmonar, debe seguir siendo
        # sensorial.
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Capacidad pulmonar', '4.5', '4.0 - 6.0'],
            ['FRC', '2.5', '2.0 - 3.5'],
            ['Bronquios', 'normal', 'normal'],
            ['Ojo', '1.2', '0 - 1'],  # rodeado de contexto pulmonar
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ojo = [m for m in p['mediciones']
               if m['parametro_normalizado'] == 'ojo']
        self.assertEqual(len(ojo), 1)
        self.assertEqual(ojo[0]['sistema_code'], 'sensorial')
        self.assertEqual(ojo[0]['sistema_source'], 'critical_override')

    def test_critical_overrides_es_dict_publico(self):
        """La tabla está accesible como atributo de clase para auditar."""
        self.assertIsInstance(BioCuanticoParser.CRITICAL_PARAM_OVERRIDES, dict)
        # Las keys mínimas que el spec del usuario exige
        keys = BioCuanticoParser.CRITICAL_PARAM_OVERRIDES
        self.assertEqual(keys.get('demanda de sangre miocardial'), 'cardiovascular')
        self.assertEqual(keys.get('demanda miocardial'), 'cardiovascular')
        self.assertEqual(keys.get('miocardial'), 'cardiovascular')
        self.assertEqual(keys.get('tiroglobulina'), 'endocrino')
        self.assertEqual(keys.get('la tiroglobulina'), 'endocrino')
        self.assertEqual(keys.get('ojo'), 'sensorial')
        self.assertEqual(keys.get('actividad celular del ojo'), 'sensorial')
        self.assertEqual(keys.get('fatiga visual'), 'sensorial')

    def test_version_manifest_2_0_6(self):
        """Manifest debe estar en versión >= 18.0.2.0.6 para Fase 2.9."""
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = parts[2], parts[3], parts[4]
            self.assertGreaterEqual((int(x), int(y), int(z)), (2, 0, 6),
                                    "Manifest version %s < 18.0.2.0.6" % v)
