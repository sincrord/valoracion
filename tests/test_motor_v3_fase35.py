# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.5 (orquestador parser_v3).

Valida que parser_v3.parse_v3() encadena correctamente
extractor → classifier → severity → ranker, mantiene la regla
arquitectónica y produce auditoría coherente para los 6 casos A-F.

Reglas validadas:
  * El antecedente NO reclasifica parámetros (solo prioriza).
  * Cada parámetro aparece en UN solo sistema (post-dedup).
  * IA no se invoca dentro del parser_v3.
  * parser_v3.py NO está conectado al flujo productivo.
"""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.valoracion.models.biocuantico.parser_v3 import (
    parse_v3, PARSER_V3_VERSION,
)


# Tablas multicaso (mismas de Fase 3.3/3.4 para no drift)
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


# =====================================================================
# 1) Shape del output
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestParseV3Shape(TransactionCase):

    def test_keys_obligatorias(self):
        out = parse_v3(table=CASE_TABLES['A'])
        for k in ('rows_raw', 'classified', 'descartados', 'severity',
                  'prioridades', 'systems_audit', 'boost_codes',
                  'auditoria', 'stats'):
            self.assertIn(k, out, "Falta key %r" % k)

    def test_input_vacio_no_crashea(self):
        out = parse_v3()
        self.assertEqual(out['rows_raw'], [])
        self.assertEqual(out['prioridades'], [])
        self.assertEqual(out['boost_codes'], [])

    def test_input_via_text_no_rompe(self):
        out = parse_v3(text='texto suelto sin tabla')
        # Sin tabla → 0 filas extraídas
        self.assertEqual(out['stats']['rows_raw'], 0)

    def test_auditoria_contiene_contrato(self):
        out = parse_v3(table=CASE_TABLES['A'], antecedente_text='detox')
        contrato = out['auditoria']['contrato']
        self.assertTrue(contrato['antecedente_no_reclasifica'])
        self.assertTrue(contrato['ningun_parametro_en_multiples_sistemas'])
        self.assertTrue(contrato['ia_no_invocada'])
        self.assertTrue(contrato['productos_no_referenciados'])

    def test_version_expuesta(self):
        out = parse_v3(table=CASE_TABLES['A'])
        self.assertEqual(out['auditoria']['parser_v3_version'],
                         PARSER_V3_VERSION)

    def test_max_priorities_default_6(self):
        out = parse_v3(table=CASE_TABLES['B'])
        self.assertLessEqual(len(out['prioridades']), 6)

    def test_max_priorities_custom(self):
        out = parse_v3(table=CASE_TABLES['B'], max_priorities=2)
        self.assertLessEqual(len(out['prioridades']), 2)


# =====================================================================
# 2) Contrato arquitectónico — antecedente no reclasifica
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestContratoArquitectonico(TransactionCase):

    def test_antecedente_no_cambia_clasificacion(self):
        """La clasificación pre-ranker no debe verse alterada por el
        antecedente. Lo verificamos comparando classified con y sin
        antecedente: deben ser idénticos (mismo sistema_code por
        parametro_raw)."""
        sin = parse_v3(table=CASE_TABLES['C'], antecedente_text='')
        con = parse_v3(table=CASE_TABLES['C'],
                       antecedente_text='hipotiroidismo')
        # Mapas parametro_raw → sistema_code
        m_sin = {r['parametro_raw']: r['sistema_code']
                 for r in sin['classified']}
        m_con = {r['parametro_raw']: r['sistema_code']
                 for r in con['classified']}
        self.assertEqual(m_sin, m_con,
                         "Antecedente alteró clasificación")

    def test_antecedente_cambia_ranking_no_clasificacion(self):
        """Caso A: sin antecedente, digestivo puede ganar; con
        antecedente 'detox metales', toxicidad sube."""
        sin = parse_v3(table=CASE_TABLES['A'], antecedente_text='')
        con = parse_v3(table=CASE_TABLES['A'],
                       antecedente_text='detox metales pesados')
        self.assertEqual(sin['boost_codes'], [])
        self.assertIn('toxicidad', con['boost_codes'])
        # toxicidad debe estar entre top-2 con antecedente
        codes_con_top2 = [p['sistema_code']
                          for p in con['prioridades'][:2]]
        self.assertIn('toxicidad', codes_con_top2)

    def test_ningun_parametro_en_multiples_sistemas(self):
        """Post-dedup, cada (parametro_raw lower) está en UN sistema."""
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            out = parse_v3(table=CASE_TABLES[k],
                           antecedente_text=ANTECEDENTES[k])
            self.assertTrue(
                out['auditoria']['contrato'][
                    'ningun_parametro_en_multiples_sistemas'],
                "Caso %s: parámetro duplicado entre sistemas: %r"
                % (k, out['auditoria']['contrato'][
                    'cross_sistema_conflictos']),
            )

    def test_hallazgos_clave_conservan_sistema(self):
        """Para cada caso, cada hallazgo_clave en prioridades debe tener
        el mismo sistema_code que la prioridad."""
        for k in ('A', 'C', 'E'):
            out = parse_v3(table=CASE_TABLES[k],
                           antecedente_text=ANTECEDENTES[k])
            for prio in out['prioridades']:
                for h in prio['hallazgos_clave']:
                    self.assertEqual(
                        h['sistema_code'], prio['sistema_code'],
                        "Caso %s: hallazgo reclasificado %r"
                        % (k, h),
                    )


# =====================================================================
# 3) Multicaso A-F end-to-end — firmas distintas
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMulticasoEndToEnd(TransactionCase):

    def _run(self, key, ant=None):
        return parse_v3(
            table=CASE_TABLES[key],
            antecedente_text=ANTECEDENTES[key] if ant is None else ant,
        )

    def test_caso_a_top_contiene_toxicidad_o_digestivo(self):
        out = self._run('A')
        top2 = [p['sistema_code'] for p in out['prioridades'][:2]]
        self.assertTrue('toxicidad' in top2 or 'digestivo' in top2,
                        "Caso A top2=%r" % top2)

    def test_caso_b_top_metabolico(self):
        out = self._run('B')
        self.assertEqual(out['prioridades'][0]['sistema_code'], 'metabolico')

    def test_caso_c_top_endocrino(self):
        out = self._run('C')
        self.assertEqual(out['prioridades'][0]['sistema_code'], 'endocrino')

    def test_caso_d_top_musculo_o_inmune(self):
        out = self._run('D')
        top2 = [p['sistema_code'] for p in out['prioridades'][:2]]
        self.assertTrue('musculoesqueletico' in top2 or 'inmune' in top2,
                        "Caso D top2=%r" % top2)

    def test_caso_e_top_nervioso(self):
        out = self._run('E')
        self.assertEqual(out['prioridades'][0]['sistema_code'], 'nervioso')

    def test_caso_f_sin_antecedente_no_boost(self):
        out = self._run('F')
        self.assertEqual(out['boost_codes'], [])
        # Aun así debe haber prioridades (hay anormales en F)
        self.assertGreater(len(out['prioridades']), 0)

    def test_firmas_distintas_entre_casos(self):
        """A,B,C,D,E con antecedentes deben producir >=4 tops distintos."""
        tops = {}
        for k in ('A', 'B', 'C', 'D', 'E'):
            out = self._run(k)
            tops[k] = out['prioridades'][0]['sistema_code']
        self.assertGreaterEqual(
            len(set(tops.values())), 4,
            "Esperaba >=4 tops distintos; got %r" % tops,
        )

    def test_anormales_se_cuentan(self):
        """stats.anormales debe ser >=1 para casos con anomalías."""
        for k in ('A', 'B', 'C', 'D', 'E'):
            out = self._run(k)
            self.assertGreaterEqual(
                out['stats']['anormales'], 1,
                "Caso %s sin anormales" % k,
            )


# =====================================================================
# 4) Sin antecedentes vs con antecedentes (mismo PDF)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestSinVsConAntecedente(TransactionCase):

    def test_caso_b_diabetes_refuerza_metabolico(self):
        sin = parse_v3(table=CASE_TABLES['B'], antecedente_text='')
        con = parse_v3(table=CASE_TABLES['B'],
                       antecedente_text='diabetes tipo 2')
        # Mismo top, pero score mayor con antecedente
        self.assertEqual(sin['prioridades'][0]['sistema_code'],
                         con['prioridades'][0]['sistema_code'])
        self.assertGreater(con['prioridades'][0]['score'],
                           sin['prioridades'][0]['score'])
        self.assertTrue(con['prioridades'][0]['boosted_por_antecedente'])

    def test_classified_identico_independiente_de_antecedente(self):
        sin = parse_v3(table=CASE_TABLES['E'], antecedente_text='')
        con = parse_v3(table=CASE_TABLES['E'],
                       antecedente_text='ansiedad e insomnio')
        # Misma cantidad de filas clasificadas, misma asignación
        self.assertEqual(len(sin['classified']), len(con['classified']))
        for r_sin, r_con in zip(sin['classified'], con['classified']):
            self.assertEqual(r_sin['sistema_code'], r_con['sistema_code'])
            self.assertEqual(r_sin['parametro_raw'], r_con['parametro_raw'])

    def test_severity_identico_independiente_de_antecedente(self):
        sin = parse_v3(table=CASE_TABLES['C'], antecedente_text='')
        con = parse_v3(table=CASE_TABLES['C'],
                       antecedente_text='hipotiroidismo')
        for r_sin, r_con in zip(sin['severity'], con['severity']):
            self.assertEqual(r_sin['severity_level'], r_con['severity_level'])
            self.assertEqual(r_sin['severity_score'], r_con['severity_score'])


# =====================================================================
# 5) Ruido fuera de prioridades + límite max 6
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestNoiseYLimites(TransactionCase):

    def test_descartados_no_aparecen_en_prioridades(self):
        """Filas descartadas no deben filtrarse hacia el ranking."""
        out = parse_v3(table=CASE_TABLES['A'], antecedente_text='detox')
        # parametro_raw de descartados
        descartados_params = {(d.get('parametro_raw') or '').strip().lower()
                              for d in out['descartados']}
        # Hallazgos clave de prioridades
        en_prio = set()
        for prio in out['prioridades']:
            for h in prio['hallazgos_clave']:
                en_prio.add((h.get('parametro_raw') or '').strip().lower())
        intersect = descartados_params & en_prio
        # Filtramos vacíos
        intersect.discard('')
        self.assertEqual(intersect, set(),
                         "Descartados filtrados a prioridades: %r"
                         % intersect)

    def test_max_6_prioridades_por_default(self):
        """Construyo input grande artificial para garantizar >6 sistemas."""
        big_table = [['Parámetro', 'Valor', 'Rango']]
        # Inyecto parámetros que activan sistemas variados
        big_table += [
            ['HDL', '25', '40 - 60'],          # metabolico
            ['LDL', '180', '0 - 100'],         # metabolico
            ['Glucosa', '150', '70 - 100'],    # metabolico
            ['TSH', '6.5', '0.5 - 4.5'],       # endocrino
            ['Cortisol', '35', '5 - 25'],      # endocrino
            ['Serotonina', '70', '120 - 200'], # nervioso
            ['Dopamina', '45', '60 - 100'],    # nervioso
            ['Helicobacter', '1.5', '0 - 1'],  # digestivo
            ['Aluminio', '85', '0 - 10'],      # toxicidad
            ['Densidad ósea', '0.7', '0.9 - 1.2'],  # musculoesqueletico
            ['Inflamación articular', '2.5', '0 - 1'],  # inmune
            ['Cápsula renal', '0.7', '0.8 - 1.2'],     # urinario
            ['Capacidad residual funcional', '1.8', '2.0 - 3.5'],  # respiratorio
        ]
        out = parse_v3(table=big_table)
        self.assertLessEqual(len(out['prioridades']), 6)
        # systems_audit puede contener más
        self.assertGreaterEqual(len(out['systems_audit']),
                                len(out['prioridades']))


# =====================================================================
# 6) Aislamiento del flujo productivo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestParserV3Aislamiento(TransactionCase):

    def _read(self, rel):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), rel,
        )
        with open(path, 'r') as f:
            return f.read()

    def test_no_importa_odoo(self):
        content = self._read('../models/biocuantico/parser_v3.py')
        for forbidden in ('from odoo', 'import odoo', 'self.env',
                          'sudo()', 'ir.model'):
            self.assertNotIn(forbidden, content,
                             "parser_v3 importa Odoo: %r" % forbidden)

    def test_no_es_importado_por_init_biocuantico(self):
        """parser_v3 NO debe estar registrado en biocuantico/__init__.py
        hasta la fase 3.8."""
        content = self._read('../models/biocuantico/__init__.py')
        self.assertNotIn('parser_v3', content,
                         "parser_v3 conectado al __init__ productivo")

    def test_no_es_usado_por_parser_productivo(self):
        content = self._read('../models/biocuantico/parser.py')
        self.assertNotIn('from .parser_v3', content)
        self.assertNotIn('import parser_v3', content)

    def test_no_es_usado_por_master_summary(self):
        content = self._read('../models/biocuantico/master_summary.py')
        self.assertNotIn('from .parser_v3', content)
        self.assertNotIn('parser_v3', content)

    def test_no_es_usado_por_archivo_cliente(self):
        content = self._read('../models/valoracion_archivo_cliente.py')
        self.assertNotIn('parser_v3', content)

    def test_no_referencia_ia_ni_productos(self):
        content = self._read('../models/biocuantico/parser_v3.py').lower()
        for forbidden in ('openai', 'anthropic', 'claude api', 'gpt',
                          'sale_order', 'cotizacion', 'product.template',
                          'vital pro', 'gema'):
            self.assertNotIn(forbidden, content,
                             "parser_v3 contiene %r" % forbidden)


# =====================================================================
# 7) Versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest315(TransactionCase):

    def test_version_2_1_4_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = int(parts[2]), int(parts[3]), int(parts[4])
            self.assertGreaterEqual(
                (x, y, z), (2, 1, 4),
                "Manifest %s < 18.0.2.1.4 (Fase 3.5)" % v,
            )
