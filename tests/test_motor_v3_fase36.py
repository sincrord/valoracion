# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.6 (master_summary_v3).

Valida que master_summary_v3 transforma el output de parse_v3 en:
  * JSON canónico con shape estable.
  * Texto compacto <= 6000 chars, sin lenguaje diagnóstico.
  * Auditoría estructurada con validaciones invariantes.

No conecta al flujo productivo. No invoca IA. No referencia productos.
"""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.valoracion.models.biocuantico.parser_v3 import parse_v3
from odoo.addons.valoracion.models.biocuantico.master_summary_v3 import (
    build_json, build_compact_text, summarize,
    MASTER_SUMMARY_V3_VERSION, DEFAULT_MAX_CHARS, MAX_PRIORITIES_HARD_CAP,
    DIAGNOSTIC_FORBIDDEN,
)


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

ANTECEDENTES = {
    'A': 'detox metales pesados',
    'B': 'diabetes tipo 2',
    'C': 'hipotiroidismo',
    'D': 'artritis crónica',
    'E': 'ansiedad e insomnio',
    'F': '',
}


def _run(case_key):
    return parse_v3(
        table=CASE_TABLES[case_key],
        antecedente_text=ANTECEDENTES[case_key],
    )


# =====================================================================
# 1) JSON shape
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestJsonShape(TransactionCase):

    def test_keys_obligatorias(self):
        out = _run('A')
        js = build_json(out)['analisis_biocuantico']
        for k in ('prioridades_funcionales', 'hallazgos_principales',
                  'hallazgos_secundarios', 'parametros_descartados',
                  'restricciones_detectadas', 'auditoria', 'metadata_motor'):
            self.assertIn(k, js, "Falta key %r en analisis_biocuantico" % k)

    def test_metadata_contiene_versiones(self):
        out = _run('B')
        meta = build_json(out)['analisis_biocuantico']['metadata_motor']
        self.assertEqual(meta['master_summary_v3_version'],
                         MASTER_SUMMARY_V3_VERSION)
        self.assertEqual(meta['max_priorities_hard_cap'],
                         MAX_PRIORITIES_HARD_CAP)
        self.assertFalse(meta['ia_invocada'])
        self.assertFalse(meta['productos_referenciados'])

    def test_prioridades_respetan_hard_cap(self):
        # Caso D + extras para forzar muchos sistemas
        out = _run('B')
        js = build_json(out)
        self.assertLessEqual(
            len(js['analisis_biocuantico']['prioridades_funcionales']),
            MAX_PRIORITIES_HARD_CAP,
        )

    def test_hallazgos_principales_solo_severo_critico(self):
        out = _run('B')
        js = build_json(out)
        for h in js['analisis_biocuantico']['hallazgos_principales']:
            self.assertIn(h['severity_level'], ('severo', 'critico'))

    def test_hallazgos_secundarios_solo_leve_moderado(self):
        out = _run('B')
        js = build_json(out)
        for h in js['analisis_biocuantico']['hallazgos_secundarios']:
            self.assertIn(h['severity_level'], ('leve', 'moderado'))

    def test_input_vacio_produce_metadata_faltante(self):
        out = parse_v3()
        js = build_json(out)
        falt = js['analisis_biocuantico']['metadata_motor'][
            'informacion_faltante']
        self.assertIn('sin_filas_extraidas', falt)
        self.assertIn('sin_prioridades', falt)

    def test_descartados_se_listan(self):
        """Caso A trae secciones "Sistema Digestivo" como descartadas
        por ser headers de sección."""
        out = _run('A')
        js = build_json(out)
        descartes = js['analisis_biocuantico']['parametros_descartados']
        # Esperamos al menos los headers de sección descartados
        params_descartados = [d['parametro'].lower() for d in descartes]
        encontrado = any('sistema digestivo' in p or 'sistema toxicidad' in p
                         for p in params_descartados)
        self.assertTrue(encontrado,
                        "No se encontraron headers descartados: %r"
                        % params_descartados)


# =====================================================================
# 2) Texto compacto
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestTextoCompacto(TransactionCase):

    def test_no_excede_6000_chars(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            out = _run(k)
            txt = build_compact_text(out)
            self.assertLessEqual(
                len(txt), DEFAULT_MAX_CHARS,
                "Caso %s texto=%d chars" % (k, len(txt)),
            )

    def test_max_chars_personalizado(self):
        out = _run('B')
        txt = build_compact_text(out, max_chars=500)
        self.assertLessEqual(len(txt), 500)
        self.assertIn('truncado', txt.lower())

    def test_prioridades_aparecen_primero(self):
        out = _run('C')
        txt = build_compact_text(out)
        idx_prio = txt.find('PRIORIDADES FUNCIONALES')
        idx_aud = txt.find('AUDITORIA')
        idx_desc = txt.find('DESCARTADOS')
        self.assertGreater(idx_prio, 0)
        self.assertGreater(idx_aud, idx_prio)
        self.assertGreater(idx_desc, idx_prio)

    def test_no_lenguaje_diagnostico(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            out = _run(k)
            txt = build_compact_text(out).lower()
            for forbidden in DIAGNOSTIC_FORBIDDEN:
                self.assertNotIn(
                    forbidden, txt,
                    "Caso %s contiene lenguaje diagnostico: %r" % (k, forbidden),
                )

    def test_texto_contiene_prioridad_top(self):
        out = _run('C')
        txt = build_compact_text(out)
        # endocrino debe aparecer en el texto
        self.assertIn('ndocrino', txt)  # tolera con/sin acento

    def test_texto_caso_sin_input(self):
        out = parse_v3()
        txt = build_compact_text(out)
        self.assertLessEqual(len(txt), DEFAULT_MAX_CHARS)
        self.assertIn('sin prioridades', txt.lower())


# =====================================================================
# 3) Validaciones invariantes
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestValidacionesInvariantes(TransactionCase):

    def test_ningun_hallazgo_clave_fuera_de_sistema(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            out = _run(k)
            js = build_json(out)
            vals = js['analisis_biocuantico']['auditoria'][
                'validaciones_summary']
            self.assertTrue(
                vals['ningun_hallazgo_clave_fuera_de_sistema'],
                "Caso %s: hallazgos fuera de sistema: %r"
                % (k, vals['hallazgos_fuera_de_sistema_evidencia']),
            )

    def test_ningun_parametro_duplicado_cross_sistema(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            out = _run(k)
            js = build_json(out)
            vals = js['analisis_biocuantico']['auditoria'][
                'validaciones_summary']
            self.assertTrue(
                vals['ningun_parametro_duplicado_cross_sistema'],
                "Caso %s duplicados: %r"
                % (k, vals['duplicados_cross_sistema_evidencia']),
            )

    def test_noise_no_filtrado_a_prioridades(self):
        for k in ('A', 'D'):
            out = _run(k)
            js = build_json(out)
            vals = js['analisis_biocuantico']['auditoria'][
                'validaciones_summary']
            self.assertTrue(
                vals['noise_no_filtrado_a_prioridades'],
                "Caso %s noise en prioridades: %r"
                % (k, vals['noise_en_prioridades_evidencia']),
            )

    def test_validaciones_marcan_falta_info_si_input_vacio(self):
        out = parse_v3()
        js = build_json(out)
        vals = js['analisis_biocuantico']['auditoria'][
            'validaciones_summary']
        self.assertFalse(vals['hay_rows_extraidas'])
        self.assertFalse(vals['hay_classified'])
        self.assertFalse(vals['hay_severity'])

    def test_no_inventa_datos(self):
        """Cada parámetro reportado en el JSON debe existir en rows_raw."""
        out = _run('E')
        js = build_json(out)
        # Unión de todos los parametro_raw realmente extraídos
        raw_params = {(r.get('parametro_raw') or '').strip()
                      for r in out['rows_raw']}
        # Verificar principales + secundarios + hallazgos_clave de prioridades
        reported = set()
        for h in js['analisis_biocuantico']['hallazgos_principales']:
            reported.add(h['parametro'].strip())
        for h in js['analisis_biocuantico']['hallazgos_secundarios']:
            reported.add(h['parametro'].strip())
        for p in js['analisis_biocuantico']['prioridades_funcionales']:
            for h in p.get('hallazgos_clave', []):
                reported.add(h['parametro'].strip())
        invented = reported - raw_params
        invented.discard('')
        self.assertEqual(invented, set(),
                         "Parámetros inventados (no en raw): %r" % invented)


# =====================================================================
# 4) summarize() — atajo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestSummarizeShortcut(TransactionCase):

    def test_summarize_devuelve_json_text_metadata(self):
        out = _run('A')
        s = summarize(out)
        self.assertIn('json', s)
        self.assertIn('text', s)
        self.assertIn('metadata', s)
        self.assertEqual(s['metadata']['master_summary_v3_version'],
                         MASTER_SUMMARY_V3_VERSION)
        self.assertLessEqual(len(s['text']), DEFAULT_MAX_CHARS)


# =====================================================================
# 5) Restricciones detectadas
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestRestriccionesDetectadas(TransactionCase):

    def test_detecta_embarazo_si_aparece_en_input(self):
        """Si una fila menciona explícitamente 'embarazo', debe aparecer
        en restricciones_detectadas. Si no aparece, NO se inventa."""
        tabla = [
            ['Parámetro', 'Valor', 'Rango'],
            ['Estado especial', 'embarazo confirmado', 'n/a'],
            ['HDL', '25', '40 - 60'],
        ]
        out = parse_v3(table=tabla)
        js = build_json(out)
        restrs = js['analisis_biocuantico']['restricciones_detectadas']
        tipos = {r['tipo'] for r in restrs}
        self.assertIn('embarazo', tipos)

    def test_no_inventa_restricciones(self):
        out = _run('B')
        js = build_json(out)
        # Caso B no menciona embarazo/anticoagulantes
        for r in js['analisis_biocuantico']['restricciones_detectadas']:
            self.assertIn(r['tipo'], {t for _, t in [
                ('embarazo', 'embarazo'),
                ('lactancia', 'lactancia'),
                ('anticoagulante', 'anticoagulante'),
                ('alergia', 'alergia'),
                ('intolerancia', 'intolerancia'),
                ('marcapasos', 'marcapasos'),
                ('quimioterapia', 'quimioterapia'),
                ('inmunosupresion', 'inmunosupresion'),
                ('contraindicacion', 'contraindicacion'),
            ]})


# =====================================================================
# 6) Multicaso A-F end-to-end
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMulticasoSummary(TransactionCase):

    def test_caso_a_summary_funciona(self):
        s = summarize(_run('A'))
        self.assertGreater(len(s['text']), 100)
        self.assertGreaterEqual(
            len(s['json']['analisis_biocuantico']['prioridades_funcionales']),
            1)

    def test_caso_b_metabolico_en_top(self):
        s = summarize(_run('B'))
        prios = s['json']['analisis_biocuantico']['prioridades_funcionales']
        self.assertEqual(prios[0]['sistema_code'], 'metabolico')

    def test_caso_c_endocrino_en_top(self):
        s = summarize(_run('C'))
        prios = s['json']['analisis_biocuantico']['prioridades_funcionales']
        self.assertEqual(prios[0]['sistema_code'], 'endocrino')

    def test_caso_e_nervioso_en_top(self):
        s = summarize(_run('E'))
        prios = s['json']['analisis_biocuantico']['prioridades_funcionales']
        self.assertEqual(prios[0]['sistema_code'], 'nervioso')

    def test_caso_f_sin_boost(self):
        s = summarize(_run('F'))
        self.assertEqual(
            s['json']['analisis_biocuantico']['auditoria'][
                'ranker_stats']['boost_codes'], [])

    def test_todos_casos_validaciones_ok(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            s = summarize(_run(k))
            vals = s['json']['analisis_biocuantico']['auditoria'][
                'validaciones_summary']
            self.assertTrue(vals['ningun_hallazgo_clave_fuera_de_sistema'])
            self.assertTrue(vals['ningun_parametro_duplicado_cross_sistema'])
            self.assertTrue(vals['noise_no_filtrado_a_prioridades'])
            self.assertTrue(vals['prioridades_respetan_max_hard_cap'])


# =====================================================================
# 7) Aislamiento del flujo productivo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMasterSummaryV3Aislamiento(TransactionCase):

    def _read(self, rel):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), rel,
        )
        with open(path, 'r') as f:
            return f.read()

    def test_no_importa_odoo(self):
        content = self._read('../models/biocuantico/master_summary_v3.py')
        for forbidden in ('from odoo', 'import odoo', 'self.env',
                          'sudo()', 'ir.model'):
            self.assertNotIn(forbidden, content,
                             "master_summary_v3 acopla Odoo: %r" % forbidden)

    def test_no_es_importado_por_init_biocuantico(self):
        content = self._read('../models/biocuantico/__init__.py')
        self.assertNotIn('master_summary_v3', content,
                         "master_summary_v3 ya enchufado en __init__")

    def test_no_es_usado_por_master_summary_productivo(self):
        content = self._read('../models/biocuantico/master_summary.py')
        self.assertNotIn('master_summary_v3', content)

    def test_no_es_usado_por_parser_productivo(self):
        content = self._read('../models/biocuantico/parser.py')
        self.assertNotIn('master_summary_v3', content)

    def test_no_es_usado_por_archivo_cliente(self):
        content = self._read('../models/valoracion_archivo_cliente.py')
        self.assertNotIn('master_summary_v3', content)

    def test_no_referencia_ia_ni_productos(self):
        content = self._read(
            '../models/biocuantico/master_summary_v3.py'
        ).lower()
        for forbidden in ('openai', 'anthropic', 'claude api',
                          'sale_order', 'cotizacion', 'product.template',
                          'vital pro', 'gema'):
            self.assertNotIn(forbidden, content,
                             "master_summary_v3 contiene %r" % forbidden)


# =====================================================================
# 8) Versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest316(TransactionCase):

    def test_version_2_1_5_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = int(parts[2]), int(parts[3]), int(parts[4])
            self.assertGreaterEqual(
                (x, y, z), (2, 1, 5),
                "Manifest %s < 18.0.2.1.5 (Fase 3.6)" % v,
            )
