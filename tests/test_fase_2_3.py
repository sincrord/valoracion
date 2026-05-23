# -*- coding: utf-8 -*-
"""Tests Fase 2.3 — Plantilla v4.1, parser BioCuántico (dedup/drop ?),
extractor de catálogo desde fuentes.

Cobertura:
  * Parser: deduplicación exact y por (sistema, parámetro); drop "?".
  * Parser: carry-forward de sistema entre tablas (no se resetea).
  * Master summary: hallazgos agrupados por sistema en text rendering.
  * Plantilla v4.1: action_restaurar_v4_oficial aplica wording + schema.
  * Schema v4.1: minItems, required y minLength refuerzan los mínimos.
  * Seed XML ↔ Python: contenido sincronizado.
  * Catalog enricher: extracción de Beneficio / Modo / Apoya desde fuentes.
"""
import json

from odoo.tests.common import TransactionCase, tagged
from odoo.addons.valoracion.models.biocuantico.parser import BioCuanticoParser
from odoo.addons.valoracion.models.biocuantico.master_summary import (
    BioCuanticoMasterSummary,
)
from odoo.addons.valoracion.models import prompt_v4_1_content as v41
from odoo.addons.valoracion.models.ia.catalog_enricher import (
    extract_product_sections_from_fuentes,
)


# =====================================================================
# Parser BioCuántico — dedup, drop "?", carry-forward
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestParserPhase23(TransactionCase):

    def test_dedup_exact_findings(self):
        """Hallazgos idénticos (mismo sistema/parámetro/valor/rango/estado)
        se deduplican a 1.
        """
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Digestivo', '', ''],
            ['Helicobacter', '1.5', '0 - 1'],
            ['Helicobacter', '1.5', '0 - 1'],   # exact duplicate
            ['Helicobacter', '1.5', '0 - 1'],   # exact duplicate
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        helic = [m for m in p['mediciones']
                 if (m.get('parametro') or '').lower() == 'helicobacter']
        self.assertEqual(len(helic), 1, "Duplicados exactos no dedup: %d" % len(helic))

    def test_dedup_same_param_keeps_richest(self):
        """Mismo (sistema, parámetro) con variantes incompletas:
        debe conservarse la entrada con MÁS información (valor+rango).
        """
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Digestivo', '', ''],
            ['Acidez', '6,0', ''],        # sin rango
            ['Acidez', '6,0', '4 - 7'],   # con rango — esta debe ganar
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        acidez = [m for m in p['mediciones']
                  if (m.get('parametro') or '').lower() == 'acidez']
        self.assertEqual(len(acidez), 1, "No se fusionaron variantes del mismo parámetro")
        self.assertEqual(acidez[0].get('rango'), '4 - 7',
                         "Se conservó la variante sin rango")

    def test_drop_sistema_unknown(self):
        """Filas sin sistema_code (carry-forward falló) NO deben aparecer."""
        # Caso adversarial: primera tabla sin encabezado de sistema en absoluto.
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            # Esto NO es un header de sistema reconocible. No hay carry-forward.
            ['Foo', '50', '0 - 100'],
            ['Bar', '200', '0 - 100'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        # Ninguna medición debe tener sistema_code vacío
        for m in p['mediciones']:
            self.assertTrue(
                m.get('sistema_code'),
                "Medición sin sistema coló: %r" % m,
            )

    def test_carry_forward_across_tables(self):
        """Si la primera tabla anuncia un sistema y la siguiente continúa
        sin encabezado, las filas heredan el sistema.
        """
        tables = [
            [['Parámetro', 'Valor', 'Rango'],
             ['Sistema Digestivo', '', ''],
             ['Helicobacter', '1.5', '0 - 1']],
            # Segunda tabla sin encabezado: debe heredar 'digestivo'
            [['Tránsito intestinal', '0,3', '0,5 - 1,5']],
        ]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        transito = [m for m in p['mediciones']
                    if (m.get('parametro') or '').lower().startswith('tránsito')]
        self.assertEqual(len(transito), 1)
        self.assertEqual(
            transito[0]['sistema_code'], 'digestivo',
            "Tránsito esperaba digestivo (carry-forward), got %s"
            % transito[0]['sistema_code'],
        )


# =====================================================================
# Master summary — agrupación por sistema
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestMasterSummaryGrouping(TransactionCase):

    def test_findings_rendered_grouped_by_sistema(self):
        tables = [[
            ['Parámetro', 'Valor', 'Rango'],
            ['Sistema Digestivo', '', ''],
            ['Helicobacter', '1.5', '0 - 1'],
            ['Acidez', '8', '4 - 7'],
            ['Sistema Endocrino', '', ''],
            ['TSH', '5,5', '0,5 - 4,5'],
            ['Cortisol', '50', '5 - 25'],
        ]]
        p = BioCuanticoParser.parse(
            text='Informe BIOCUÁNTICO Resonancia Cuántica',
            tables=tables, env=self.env,
        )
        ms = BioCuanticoMasterSummary.build(p, env=self.env)
        self.assertTrue(ms['available'])
        text = ms['text']
        # Headers de sistema en el render
        self.assertIn('[Sistema Digestivo]', text)
        self.assertIn('[Sistema Endocrino]', text)
        # Hallazgos del mismo sistema deben quedar contiguos (no intercalados)
        idx_digestivo_helic = text.find('Helicobacter')
        idx_digestivo_acidez = text.find('Acidez')
        idx_endocrino_tsh = text.find('TSH')
        idx_endocrino_cort = text.find('Cortisol')
        # Hallazgos digestivos antes que cualquier endocrino
        self.assertLess(max(idx_digestivo_helic, idx_digestivo_acidez),
                        min(idx_endocrino_tsh, idx_endocrino_cort),
                        "Hallazgos intercalados entre sistemas (no agrupados)")


# =====================================================================
# Plantilla v4.1 — action restaurar
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestPromptV41Restore(TransactionCase):

    def test_action_restaurar_overwrites_three_fields(self):
        tmpl = self.env['valoracion.prompt.template'].create({
            'name': 'Test custom',
            'version': '0.1',
            'system_prompt': 'wording viejo: el archivo es referencia complementaria',
            'user_prompt_template': '{{CONTENIDO_ARCHIVOS_CLIENTE}}',
            'output_schema': '{"type": "object", "properties": {}}',
            'is_default': False,
            'active': True,
            'notes': 'NOTA CUSTOM',
        })
        tmpl.action_restaurar_v4_oficial()
        self.assertEqual(tmpl.version, v41.VERSION)
        self.assertEqual(tmpl.system_prompt, v41.SYSTEM_PROMPT)
        self.assertEqual(tmpl.user_prompt_template, v41.USER_PROMPT_TEMPLATE)
        self.assertEqual(tmpl.output_schema, v41.OUTPUT_SCHEMA)
        # name y notes se preservan
        self.assertEqual(tmpl.name, 'Test custom')
        self.assertEqual(tmpl.notes, 'NOTA CUSTOM')
        # El wording viejo desapareció
        self.assertNotIn('referencia complementaria', tmpl.system_prompt)
        # El wording fuerte está presente
        self.assertIn('FUENTE PRINCIPAL', tmpl.system_prompt)

    def test_schema_v41_minimums(self):
        schema = json.loads(v41.OUTPUT_SCHEMA)
        # prioridades_caso minItems=1
        self.assertEqual(schema['properties']['prioridades_caso']['minItems'], 1)
        # prioridades_caso items.required incluye evidencia_archivo
        pr_required = schema['properties']['prioridades_caso']['items']['required']
        for k in ('prioridad', 'titulo', 'evidencia_archivo',
                  'relacion_antecedente', 'importancia'):
            self.assertIn(k, pr_required, "prioridades_caso.items.required falta %s" % k)
        # productos_recomendados items.required incluye prioridad_que_apoya
        prod_required = schema['properties']['productos_recomendados']['items']['required']
        for k in ('orden_importancia', 'nombre', 'prioridad_que_apoya', 'razon'):
            self.assertIn(k, prod_required,
                          "productos_recomendados.items.required falta %s" % k)
        # advertencias minLength
        self.assertGreaterEqual(
            schema['properties']['advertencias'].get('minLength', 0), 20,
            "advertencias.minLength debe ser >= 20",
        )

    def test_seed_xml_matches_python_constants(self):
        """El contenido del seed XML debe estar sincronizado con el módulo
        Python para garantizar que install + acción de restauración produzcan
        el mismo wording.
        """
        import os
        import xml.etree.ElementTree as ET
        # Localizar el archivo seed relativo a este test
        seed_path = os.path.join(
            os.path.dirname(__file__), '..', 'data',
            'valoracion_prompt_default.xml',
        )
        tree = ET.parse(seed_path)
        root = tree.getroot()
        record = root.find(".//record[@id='prompt_template_default']")
        self.assertIsNotNone(record, "No se encontró prompt_template_default en seed")

        def _field(name):
            field = record.find("field[@name='%s']" % name)
            return (field.text or '') if field is not None else None

        # version
        self.assertEqual(_field('version'), v41.VERSION,
                         "version del seed no coincide con Python")
        # Comparar strings completos (whitespace incluido)
        self.assertEqual(_field('system_prompt'), v41.SYSTEM_PROMPT,
                         "system_prompt del seed no sincronizado")
        self.assertEqual(_field('user_prompt_template'), v41.USER_PROMPT_TEMPLATE,
                         "user_prompt_template del seed no sincronizado")
        self.assertEqual(_field('output_schema'), v41.OUTPUT_SCHEMA,
                         "output_schema del seed no sincronizado")


# =====================================================================
# Catalog enricher — extracción de Beneficio / Modo / Apoya
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico')
class TestCatalogEnricher(TransactionCase):

    def setUp(self):
        super().setUp()

        class _P:
            def __init__(self, id_, name):
                self.id = id_
                self.name = name

        self._P = _P

    def test_extract_three_sections_per_product(self):
        text = """\
CATÁLOGO VITALHEALTH

=================================
VITAL PRO PLUS

Beneficio: Apoya el equilibrio metabólico y la energía celular.

Modo de empleo: 1 cápsula con cada comida principal.

Puede apoyar a personas con: cansancio crónico, baja energía.

Ingredientes: cúrcuma, vitamina B12.

=================================
V-TE DETOX

Beneficios: Apoya la función hepática.
Forma de uso: 1 sobre en ayunas.
Apoya a personas con: digestión lenta.
"""
        products = [self._P(1, 'Vital Pro Plus'), self._P(2, 'V-TE Detox')]
        out = extract_product_sections_from_fuentes(text, products)
        self.assertIn(1, out)
        self.assertIn(2, out)
        self.assertIn('equilibrio metabólico', out[1]['beneficio'])
        self.assertIn('cápsula', out[1]['modo_empleo'])
        self.assertIn('cansancio', out[1]['apoya_a'])
        self.assertIn('hepática', out[2]['beneficio'])
        self.assertIn('sobre', out[2]['modo_empleo'])
        self.assertIn('digestión', out[2]['apoya_a'])
        # No debe contaminarse con terminadores
        for k, v_ in out[1].items():
            if v_:
                self.assertNotIn('Ingredientes', v_)
                self.assertNotIn('===', v_)

    def test_extract_handles_missing_product(self):
        text = """\
VITAL PRO PLUS
Beneficio: foo.
"""
        out = extract_product_sections_from_fuentes(
            text,
            [self._P(1, 'Vital Pro Plus'),
             self._P(99, 'Producto Inexistente')],
        )
        self.assertIn(1, out)
        self.assertNotIn(99, out)

    def test_extract_empty_returns_empty_dict(self):
        self.assertEqual(extract_product_sections_from_fuentes('', []), {})
        self.assertEqual(extract_product_sections_from_fuentes('texto', []), {})
        self.assertEqual(
            extract_product_sections_from_fuentes('', [self._P(1, 'X')]), {},
        )

    def test_extract_label_variations(self):
        """Tolerancia a 'Beneficios' (plural), 'Modo de uso' (vs 'empleo'),
        'Apoya a personas con' (vs 'Puede apoyar...').
        """
        text = """\
ALPHA
Beneficios: foo bar.
Modo de uso: tomar 1.
Apoya a personas con: estrés.
"""
        out = extract_product_sections_from_fuentes(text, [self._P(1, 'Alpha')])
        self.assertIn(1, out)
        self.assertIn('foo', out[1]['beneficio'])
        self.assertIn('tomar', out[1]['modo_empleo'])
        self.assertIn('estrés', out[1]['apoya_a'])
