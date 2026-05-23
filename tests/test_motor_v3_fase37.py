# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.7 (validación integral aislada).

Pipeline validado end-to-end:
    extractor → classifier → severity → ranker → master_summary_v3

Cubre:
  * Multicaso A-F con summarize().
  * Firmas distintas por caso.
  * Texto <= 6000 chars.
  * JSON shape estable.
  * Sin duplicados cross-sistema.
  * Noise fuera de prioridades.
  * Antecedente cambia ranking pero NO clasificación.
  * Sin antecedente funciona.
  * Caso vacío/ilegible reporta metadata faltante.
  * Determinismo (mismo input → mismo output).
  * Auditoría suficiente para explicar clasificación/severidad/ranking.
  * Sin referencias comerciales (Gema, OpenAI, productos, cotización).
  * Sin imports accidentales del motor productivo.
  * Performance básica en dataset sintético grande.
"""
import os
import time
import json as _json

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.valoracion.models.biocuantico.parser_v3 import parse_v3
from odoo.addons.valoracion.models.biocuantico.master_summary_v3 import (
    summarize, build_json, build_compact_text,
    MASTER_SUMMARY_V3_VERSION, DEFAULT_MAX_CHARS, MAX_PRIORITIES_HARD_CAP,
)


# Mismas tablas multicaso que Fases 3.3-3.6 (no drift)
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


def _summary(case_key, ant=None):
    return summarize(parse_v3(
        table=CASE_TABLES[case_key],
        antecedente_text=ANTECEDENTES[case_key] if ant is None else ant,
    ))


def _validaciones(summary_out):
    return summary_out['json']['analisis_biocuantico']['auditoria'][
        'validaciones_summary']


# =====================================================================
# 1) Multicaso A-F end-to-end (summarize)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMulticasoIntegral(TransactionCase):

    def test_todos_casos_producen_summary_valido(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            s = _summary(k)
            self.assertIn('json', s)
            self.assertIn('text', s)
            self.assertIn('metadata', s)
            self.assertGreater(len(s['text']), 50,
                               "Caso %s texto muy corto" % k)

    def test_firmas_distintas(self):
        """A-E deben tener >=4 sistemas top distintos."""
        tops = {}
        for k in ('A', 'B', 'C', 'D', 'E'):
            prios = _summary(k)['json']['analisis_biocuantico'][
                'prioridades_funcionales']
            tops[k] = prios[0]['sistema_code']
        self.assertGreaterEqual(
            len(set(tops.values())), 4,
            "Firmas no diferenciables: %r" % tops,
        )

    def test_caso_b_metabolico(self):
        self.assertEqual(
            _summary('B')['json']['analisis_biocuantico'][
                'prioridades_funcionales'][0]['sistema_code'],
            'metabolico',
        )

    def test_caso_c_endocrino(self):
        self.assertEqual(
            _summary('C')['json']['analisis_biocuantico'][
                'prioridades_funcionales'][0]['sistema_code'],
            'endocrino',
        )

    def test_caso_e_nervioso(self):
        self.assertEqual(
            _summary('E')['json']['analisis_biocuantico'][
                'prioridades_funcionales'][0]['sistema_code'],
            'nervioso',
        )


# =====================================================================
# 2) Shape e invariantes globales
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestInvariantesGlobales(TransactionCase):

    def test_texto_compacto_no_excede_6000(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            txt = _summary(k)['text']
            self.assertLessEqual(len(txt), DEFAULT_MAX_CHARS,
                                 "Caso %s texto=%d" % (k, len(txt)))

    def test_json_serializable(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            js = _summary(k)['json']
            try:
                _json.dumps(js, default=str)
            except Exception as e:
                self.fail("Caso %s JSON no serializable: %s" % (k, e))

    def test_json_shape_estable(self):
        keys_top = set()
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            js = _summary(k)['json']['analisis_biocuantico']
            keys_top.add(tuple(sorted(js.keys())))
        # Misma forma para todos los casos
        self.assertEqual(len(keys_top), 1,
                         "Shape inestable entre casos: %r" % keys_top)
        # Y contiene las 7 keys requeridas
        for key in ('prioridades_funcionales', 'hallazgos_principales',
                    'hallazgos_secundarios', 'parametros_descartados',
                    'restricciones_detectadas', 'auditoria',
                    'metadata_motor'):
            self.assertIn(key, list(keys_top)[0])

    def test_sin_duplicados_cross_sistema(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            v = _validaciones(_summary(k))
            self.assertTrue(
                v['ningun_parametro_duplicado_cross_sistema'],
                "Caso %s duplicados: %r"
                % (k, v['duplicados_cross_sistema_evidencia']),
            )

    def test_noise_fuera_de_prioridades(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            v = _validaciones(_summary(k))
            self.assertTrue(
                v['noise_no_filtrado_a_prioridades'],
                "Caso %s noise filtrado: %r"
                % (k, v['noise_en_prioridades_evidencia']),
            )

    def test_max_6_prioridades(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            prios = _summary(k)['json']['analisis_biocuantico'][
                'prioridades_funcionales']
            self.assertLessEqual(len(prios), MAX_PRIORITIES_HARD_CAP)


# =====================================================================
# 3) Antecedente: cambia ranking, NO clasificación
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestAntecedenteVsClasificacion(TransactionCase):

    def test_classified_identico_con_y_sin_antecedente(self):
        for k in ('A', 'B', 'C', 'D', 'E'):
            sin = parse_v3(table=CASE_TABLES[k], antecedente_text='')
            con = parse_v3(table=CASE_TABLES[k],
                           antecedente_text=ANTECEDENTES[k])
            m_sin = {(r['parametro_raw'], r['sistema_code'])
                     for r in sin['classified']}
            m_con = {(r['parametro_raw'], r['sistema_code'])
                     for r in con['classified']}
            self.assertEqual(
                m_sin, m_con,
                "Caso %s: antecedente alteró clasificación" % k,
            )

    def test_severity_identico_con_y_sin_antecedente(self):
        for k in ('B', 'C', 'E'):
            sin = parse_v3(table=CASE_TABLES[k], antecedente_text='')
            con = parse_v3(table=CASE_TABLES[k],
                           antecedente_text=ANTECEDENTES[k])
            sin_map = {r['parametro_raw']: r['severity_level']
                       for r in sin['severity']}
            con_map = {r['parametro_raw']: r['severity_level']
                       for r in con['severity']}
            self.assertEqual(sin_map, con_map,
                             "Caso %s severidad alterada por antecedente" % k)

    def test_ranking_si_cambia_con_antecedente_apropiado(self):
        """Caso A: con antecedente 'detox metales', toxicidad debe estar
        en top-2 (puede no estar primero pero debe aparecer)."""
        sin = _summary('A', ant='')
        con = _summary('A', ant='detox metales pesados')
        codes_sin = [p['sistema_code'] for p in
                     sin['json']['analisis_biocuantico'][
                         'prioridades_funcionales']]
        codes_con = [p['sistema_code'] for p in
                     con['json']['analisis_biocuantico'][
                         'prioridades_funcionales']]
        # toxicidad aparece en con; boost lo subió
        self.assertIn('toxicidad', codes_con[:2])
        # Y los boost codes lo reflejan
        boost = con['json']['analisis_biocuantico']['auditoria'][
            'ranker_stats']['boost_codes']
        self.assertIn('toxicidad', boost)
        # Sin antecedente, boost_codes vacío
        self.assertEqual(
            sin['json']['analisis_biocuantico']['auditoria'][
                'ranker_stats']['boost_codes'], [])

    def test_sin_antecedente_funciona(self):
        """Pipeline sin antecedente sigue produciendo prioridades."""
        out = parse_v3(table=CASE_TABLES['B'], antecedente_text='')
        self.assertGreater(len(out['prioridades']), 0)
        self.assertEqual(out['boost_codes'], [])


# =====================================================================
# 4) Caso vacío / ilegible
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestCasosDegradados(TransactionCase):

    def test_input_totalmente_vacio(self):
        out = parse_v3()
        s = summarize(out)
        meta = s['json']['analisis_biocuantico']['metadata_motor']
        self.assertIn('sin_filas_extraidas', meta['informacion_faltante'])
        self.assertIn('sin_prioridades', meta['informacion_faltante'])
        # No crashea, texto generado bajo el cap
        self.assertLessEqual(len(s['text']), DEFAULT_MAX_CHARS)

    def test_tabla_solo_headers(self):
        tabla = [['Parámetro', 'Valor', 'Rango']]
        out = parse_v3(table=tabla)
        s = summarize(out)
        meta = s['json']['analisis_biocuantico']['metadata_motor']
        self.assertIn('sin_prioridades', meta['informacion_faltante'])

    def test_tabla_basura(self):
        """Filas con strings vacíos / valores ilegibles."""
        tabla = [
            ['Parámetro', 'Valor', 'Rango'],
            ['', '', ''],
            ['xxx', 'yyy', 'zzz'],
            ['?', '?', '?'],
        ]
        out = parse_v3(table=tabla)
        s = summarize(out)
        # Debe ejecutarse sin error; las filas terminarán en descartados
        # o con severidad desconocida
        self.assertTrue(isinstance(s['text'], str))
        self.assertLessEqual(len(s['text']), DEFAULT_MAX_CHARS)


# =====================================================================
# 5) Determinismo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestDeterminismo(TransactionCase):

    def _firma(self, case_key):
        s = _summary(case_key)
        prios = s['json']['analisis_biocuantico']['prioridades_funcionales']
        return tuple(
            (p['sistema_code'], p['score'], p['severos'],
             p['moderados'], p['leves'])
            for p in prios
        )

    def test_mismo_input_mismo_output(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            f1 = self._firma(k)
            f2 = self._firma(k)
            f3 = self._firma(k)
            self.assertEqual(f1, f2,
                             "Caso %s no determinístico (1 vs 2)" % k)
            self.assertEqual(f2, f3,
                             "Caso %s no determinístico (2 vs 3)" % k)

    def test_texto_determinístico(self):
        for k in ('B', 'C', 'E'):
            t1 = _summary(k)['text']
            t2 = _summary(k)['text']
            self.assertEqual(t1, t2,
                             "Caso %s texto no determinístico" % k)


# =====================================================================
# 6) Auditoría suficiente para explicar el motor
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestAuditoriaExplicable(TransactionCase):

    def test_auditoria_contiene_clasificacion(self):
        for k in ('B', 'C'):
            aud = _summary(k)['json']['analisis_biocuantico']['auditoria']
            self.assertIn('classifier_stats', aud)
            cs = aud['classifier_stats']
            self.assertIn('por_sistema', cs)
            self.assertIn('por_source', cs)
            self.assertIn('por_motivo_descarte', cs)

    def test_auditoria_contiene_severidad(self):
        for k in ('B', 'C'):
            aud = _summary(k)['json']['analisis_biocuantico']['auditoria']
            sev = aud['severity_distribucion']
            # Al menos uno de severo/moderado/leve > 0
            anormales = (sev.get('severo', 0) + sev.get('moderado', 0)
                         + sev.get('leve', 0) + sev.get('critico', 0))
            self.assertGreater(anormales, 0,
                               "Caso %s sin anormales auditables" % k)

    def test_auditoria_contiene_ranker(self):
        for k in ('A', 'B', 'C'):
            aud = _summary(k)['json']['analisis_biocuantico']['auditoria']
            rk = aud['ranker_stats']
            self.assertIn('sistemas_con_hallazgos', rk)
            self.assertIn('prioridades_devueltas', rk)
            self.assertIn('boost_codes', rk)
            self.assertIn('max_priorities', rk)

    def test_cada_prioridad_tiene_breakdown_auditable(self):
        for k in ('B', 'C'):
            prios = _summary(k)['json']['analisis_biocuantico'][
                'prioridades_funcionales']
            for p in prios:
                # severos/moderados/leves explican la severidad agregada
                self.assertIn('severos', p)
                self.assertIn('moderados', p)
                self.assertIn('leves', p)
                self.assertIn('score', p)
                self.assertIn('score_base', p)
                self.assertIn('penalty_applied', p)
                self.assertIn('boosted_por_antecedente', p)
                # Y al menos un hallazgo clave existe
                self.assertGreaterEqual(len(p['hallazgos_clave']), 1)

    def test_contrato_arquitectonico_expuesto(self):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            aud = _summary(k)['json']['analisis_biocuantico']['auditoria']
            cp = aud['contrato_parser']
            self.assertTrue(cp.get('antecedente_no_reclasifica'))
            self.assertTrue(
                cp.get('ningun_parametro_en_multiples_sistemas'))
            self.assertTrue(cp.get('ia_no_invocada'))


# =====================================================================
# 7) Cero referencias comerciales / IA en los módulos v3
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestSinReferenciasComerciales(TransactionCase):

    MODULES_V3 = (
        'taxonomy.py',
        'extractor.py',
        'classifier.py',
        'severity.py',
        'ranker.py',
        'parser_v3.py',
        'master_summary_v3.py',
    )

    FORBIDDEN = (
        'gema', 'openai', 'anthropic', 'claude api',
        'sale_order', 'cotizacion', 'product.template',
        'vital pro', 'vitalhealth',
    )

    def _read(self, rel):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico', rel,
        )
        with open(path, 'r') as f:
            return f.read().lower()

    def test_modulos_v3_sin_referencias(self):
        for mod in self.MODULES_V3:
            content = self._read(mod)
            for f in self.FORBIDDEN:
                self.assertNotIn(
                    f, content,
                    "%s contiene referencia prohibida %r" % (mod, f),
                )


# =====================================================================
# 8) Aislamiento — motor v3 no enchufado a producción
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestAislamientoMotorV3(TransactionCase):

    MODULES_V3_NAMES = (
        'taxonomy', 'extractor', 'classifier', 'severity',
        'ranker', 'parser_v3', 'master_summary_v3',
    )
    PRODUCCION = (
        '../models/biocuantico/__init__.py',
        '../models/biocuantico/parser.py',
        '../models/biocuantico/master_summary.py',
        '../models/valoracion_archivo_cliente.py',
    )

    def _read(self, rel):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), rel,
        )
        with open(path, 'r') as f:
            return f.read()

    def test_motor_v3_no_referenciado_en_produccion(self):
        """Ningún archivo productivo importa los módulos v3."""
        for prod_file in self.PRODUCCION:
            content = self._read(prod_file)
            for mod_name in self.MODULES_V3_NAMES:
                # Forma "from .X import" o "import X"
                forbidden_imports = (
                    'from .%s' % mod_name,
                    'from .biocuantico.%s' % mod_name,
                    'from .biocuantico import %s' % mod_name,
                )
                for fi in forbidden_imports:
                    self.assertNotIn(
                        fi, content,
                        "%s importa motor v3 (%s): %r"
                        % (prod_file, mod_name, fi),
                    )

    def test_modulos_v3_no_importan_odoo(self):
        for mod_name in self.MODULES_V3_NAMES:
            path = '../models/biocuantico/%s.py' % mod_name
            content = self._read(path)
            for forbidden in ('from odoo', 'import odoo', 'self.env',
                              'sudo()', 'ir.model.access'):
                self.assertNotIn(
                    forbidden, content,
                    "Módulo v3 %s acopla Odoo: %r" % (mod_name, forbidden),
                )


# =====================================================================
# 9) Performance básica en dataset sintético grande
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestPerformanceSintetica(TransactionCase):

    def _build_big_table(self, n_param=200):
        """Genera ~n_param filas variadas distribuidas entre sistemas.

        Reusa parámetros conocidos para que el classifier los reconozca
        y la severidad sea calculable."""
        plantilla = [
            ('Helicobacter pylori', '1.8', '0 - 1'),       # digestivo
            ('Acidez gástrica', '8.2', '4 - 7'),           # digestivo
            ('HDL', '25', '40 - 60'),                      # metabolico
            ('LDL', '180', '0 - 100'),                     # metabolico
            ('TSH', '6.5', '0.5 - 4.5'),                   # endocrino
            ('T4 libre', '0.5', '0.8 - 1.8'),              # endocrino
            ('Serotonina', '80', '120 - 200'),             # nervioso
            ('Densidad ósea', '0.7', '0.9 - 1.2'),         # musculo
            ('Aluminio', '85', '0 - 10'),                  # toxicidad
            ('Inflamación articular', '2.5', '0 - 1'),     # inmune
            ('Cápsula renal', '0.7', '0.8 - 1.2'),         # urinario
            ('Capacidad residual funcional', '1.8', '2.0 - 3.5'),
        ]
        rows = [['Parámetro', 'Valor', 'Rango']]
        for i in range(n_param):
            p, v, r = plantilla[i % len(plantilla)]
            rows.append(['%s_%d' % (p, i // len(plantilla)) if i >= len(plantilla) else p,
                         v, r])
        return rows

    def test_pipeline_grande_termina_rapido(self):
        """200 filas sintéticas → summary debe completarse en <2s."""
        tabla = self._build_big_table(n_param=200)
        t0 = time.time()
        out = parse_v3(table=tabla, antecedente_text='diabetes')
        s = summarize(out)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0,
                        "Pipeline tomó %.2fs (>2s) con 200 filas" % elapsed)
        self.assertLessEqual(len(s['text']), DEFAULT_MAX_CHARS)
        # Debe seguir respetando hard cap
        self.assertLessEqual(
            len(s['json']['analisis_biocuantico'][
                'prioridades_funcionales']),
            MAX_PRIORITIES_HARD_CAP,
        )

    def test_pipeline_grande_no_rompe_invariantes(self):
        tabla = self._build_big_table(n_param=200)
        out = parse_v3(table=tabla)
        s = summarize(out)
        v = _validaciones(s)
        self.assertTrue(v['ningun_hallazgo_clave_fuera_de_sistema'])
        self.assertTrue(v['ningun_parametro_duplicado_cross_sistema'])
        self.assertTrue(v['noise_no_filtrado_a_prioridades'])
        self.assertTrue(v['prioridades_respetan_max_hard_cap'])


# =====================================================================
# 10) Versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest317(TransactionCase):

    def test_version_2_1_6_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = int(parts[2]), int(parts[3]), int(parts[4])
            self.assertGreaterEqual(
                (x, y, z), (2, 1, 6),
                "Manifest %s < 18.0.2.1.6 (Fase 3.7)" % v,
            )
