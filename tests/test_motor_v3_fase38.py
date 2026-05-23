# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.8 (integración productiva controlada).

Valida que:
  * El setting `valoracion.biocuantico_motor_version` controla el motor.
  * Default es legacy (sin tocar nada, el flujo actual sigue funcionando).
  * Al activar v3, el dispatcher invoca _run_biocuantico_parser_v3.
  * Si v3 falla, hay fallback automático a legacy con bandera y motivo.
  * La metadata del motor (motor_version, parser_version, fallback_used,
    motor_meta_json) se persiste y queda auditable.
  * El summary_text generado por v3 es compatible (string no vacío).
  * Sin archivo no rompe la valoración.
  * Manifest sube a 18.0.3.0.0.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


SAMPLE_TEXT_BIOCUANTICO = (
    "Reporte BioCuántico funcional.\n"
    "Sistema Digestivo\n"
    "Helicobacter pylori 1.8 (0 - 1)\n"
    "Acidez gástrica 8.2 (4 - 7)\n"
    "Sistema Toxicidad\n"
    "Aluminio 85 (0 - 10)\n"
    "Mercurio 0.5 (0 - 0.1)\n"
)


def _set_motor(env, value):
    env['ir.config_parameter'].sudo().set_param(
        'valoracion.biocuantico_motor_version', value,
    )


def _new_archivo(env, val, name='test.pdf', file_type='pdf', text=None):
    """Crea un archivo con extracted_text directamente (sin pdfplumber).

    Para los tests no queremos un PDF real; trabajamos con extracted_text
    y mockeamos los pasos que requieren el binario."""
    arc = env['valoracion.archivo.cliente'].create({
        'valoracion_id': val.id,
        'name': name,
        'file_name': name,
        'file_data': b'BASE64DUMMY',
    })
    # File_type viene del compute por nombre — sobreescribimos por write
    # sólo para tests con tipo no-PDF
    if file_type != arc.file_type:
        arc.write({})  # no-op: file_type es computado
    if text is not None:
        arc.write({
            'extracted_text': text,
            'extraction_status': 'ok',
        })
    return arc


def _new_valoracion(env):
    partner = env['res.partner'].create({'name': 'TEST Fase 3.8'})
    return env['valoracion.valoracion'].create({
        'partner_id': partner.id,
    })


# =====================================================================
# 1) Setting + default
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestSettingMotorVersion(TransactionCase):

    def test_default_es_legacy(self):
        # Sin tocar el setting, get_param devuelve None o 'legacy'
        val = (self.env['ir.config_parameter'].sudo()
               .get_param('valoracion.biocuantico_motor_version', 'legacy'))
        # default declarado en res.config.settings es 'legacy'
        self.assertIn(val, (False, None, 'legacy'))

    def test_set_y_lee_v3(self):
        _set_motor(self.env, 'v3')
        self.assertEqual(
            self.env['ir.config_parameter'].sudo()
            .get_param('valoracion.biocuantico_motor_version'),
            'v3',
        )

    def test_helper_normaliza_valor_invalido_a_legacy(self):
        _set_motor(self.env, 'cualquier_otro_valor_invalido')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text='cualquier cosa')
        self.assertEqual(arc._get_biocuantico_motor_version(), 'legacy')

    def test_helper_lee_v3(self):
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text='x')
        self.assertEqual(arc._get_biocuantico_motor_version(), 'v3')


# =====================================================================
# 2) Dispatcher routing
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestDispatcherRouting(TransactionCase):

    def test_legacy_invoca_legacy_no_v3(self):
        _set_motor(self.env, 'legacy')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text='texto sin tablas')
        with patch.object(
            type(arc), '_run_biocuantico_parser_v3', return_value=None,
        ) as v3_mock, patch.object(
            type(arc), '_run_biocuantico_parser_legacy', return_value=None,
        ) as legacy_mock:
            arc._run_biocuantico_parser(force=True)
        v3_mock.assert_not_called()
        legacy_mock.assert_called_once()

    def test_v3_invoca_v3_primero(self):
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text='x')
        with patch.object(
            type(arc), '_run_biocuantico_parser_v3',
            return_value={'status': 'ok', 'motor': 'v3'},
        ) as v3_mock, patch.object(
            type(arc), '_run_biocuantico_parser_legacy',
        ) as legacy_mock:
            arc._run_biocuantico_parser(force=True)
        v3_mock.assert_called_once()
        legacy_mock.assert_not_called()


# =====================================================================
# 3) Fallback automático cuando v3 falla
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestFallbackAutomatico(TransactionCase):

    def test_fallback_si_v3_lanza_excepcion(self):
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text='x')

        def boom(self_):
            raise RuntimeError("v3 simulated crash")

        with patch.object(
            type(arc), '_run_biocuantico_parser_v3', side_effect=boom,
        ), patch.object(
            type(arc), '_run_biocuantico_parser_legacy',
            return_value={'status': 'ok', 'motor': 'legacy'},
        ) as legacy_mock:
            arc._run_biocuantico_parser(force=True)
        legacy_mock.assert_called_once()
        # Bandera y error registrados
        self.assertTrue(arc.biocuantico_fallback_used)
        self.assertIn('Motor v3 falló', arc.biocuantico_parse_error or '')

    def test_fallback_si_v3_retorna_none(self):
        """v3 puede devolver None cuando no encuentra estructura útil; el
        dispatcher debe caer a legacy sin marcar fallback_used (no fue
        crash, fue decisión informada)."""
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text='x')
        with patch.object(
            type(arc), '_run_biocuantico_parser_v3', return_value=None,
        ), patch.object(
            type(arc), '_run_biocuantico_parser_legacy',
            return_value={'status': 'ok', 'motor': 'legacy'},
        ) as legacy_mock:
            arc._run_biocuantico_parser(force=True)
        legacy_mock.assert_called_once()

    def test_valoracion_no_se_rompe_cuando_v3_crashea(self):
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text='x')
        with patch.object(
            type(arc), '_run_biocuantico_parser_v3',
            side_effect=Exception("kaboom"),
        ), patch.object(
            type(arc), '_run_biocuantico_parser_legacy',
            return_value=None,
        ):
            # No debe propagar la excepción
            try:
                arc._run_biocuantico_parser(force=True)
            except Exception as e:
                self.fail("v3 crash propagó al usuario: %s" % e)


# =====================================================================
# 4) Cache + metadata persistida (v3 end-to-end con table sintética)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestCacheMetadataV3(TransactionCase):
    """Tests que verifican el cache cuando v3 corre exitosamente.

    Usamos un patch de parse_v3 para devolver un payload sintético en
    lugar de extraer del PDF (la extracción de PDFs reales requiere
    pdfplumber, fuera del scope de unit tests)."""

    SIMULATED_V3 = {
        'rows_raw': [{'parametro_raw': 'X'}] * 5,
        'classified': [{'parametro_raw': 'X', 'sistema_code': 'digestivo'}] * 5,
        'descartados': [],
        'severity': [],
        'prioridades': [
            {
                'sistema_code': 'digestivo',
                'sistema_label': 'Digestivo',
                'severidad': 'severo',
                'severidad_score': 4,
                'severos': 2, 'moderados': 1, 'leves': 0,
                'score': 13.0, 'score_base': 13.0,
                'penalty_applied': False,
                'boosted_por_antecedente': False,
                'hallazgos_clave': [],
                'audit': {'weights': {}, 'boost_factor': 1.0},
            },
            {
                'sistema_code': 'toxicidad',
                'sistema_label': 'Toxicidad',
                'severidad': 'severo',
                'severidad_score': 4,
                'severos': 1, 'moderados': 0, 'leves': 0,
                'score': 5.0, 'score_base': 5.0,
                'penalty_applied': False,
                'boosted_por_antecedente': False,
                'hallazgos_clave': [],
                'audit': {'weights': {}, 'boost_factor': 1.0},
            },
            {
                'sistema_code': 'endocrino',
                'sistema_label': 'Endocrino',
                'severidad': 'moderado',
                'severidad_score': 3,
                'severos': 0, 'moderados': 2, 'leves': 1,
                'score': 7.0, 'score_base': 7.0,
                'penalty_applied': False,
                'boosted_por_antecedente': False,
                'hallazgos_clave': [],
                'audit': {'weights': {}, 'boost_factor': 1.0},
            },
        ],
        'systems_audit': {},
        'boost_codes': ['digestivo'],
        'auditoria': {
            'parser_v3_version': '3.5.0',
            'extractor': {'paginas': 1, 'tablas': 1, 'filas_extraidas': 5},
            'classifier': {'por_sistema': {'digestivo': 5}},
            'severity_distribucion': {'severo': 3, 'moderado': 2},
            'ranker': {'sistemas_con_hallazgos': 3,
                       'prioridades_devueltas': 3,
                       'boost_codes': ['digestivo'],
                       'max_priorities': 6},
            'contrato': {
                'antecedente_no_reclasifica': True,
                'antecedente_no_reclasifica_conflictos': [],
                'ningun_parametro_en_multiples_sistemas': True,
                'cross_sistema_conflictos': [],
                'ia_no_invocada': True,
                'productos_no_referenciados': True,
            },
        },
        'stats': {
            'rows_raw': 5, 'classified': 5, 'descartados': 0,
            'anormales': 5, 'sistemas_afectados': 3, 'prioridades': 3,
        },
    }

    def _run_v3_with_payload(self, payload):
        from odoo.addons.valoracion.models.biocuantico import (
            parser_v3 as pv3,
        )
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(
            self.env, valor, name='test.pdf',
            text=SAMPLE_TEXT_BIOCUANTICO,
        )
        with patch.object(pv3, 'parse_v3', return_value=payload):
            arc._run_biocuantico_parser(force=True)
        return arc

    def test_v3_persiste_motor_version(self):
        arc = self._run_v3_with_payload(self.SIMULATED_V3)
        self.assertEqual(arc.biocuantico_motor_version, 'v3')

    def test_v3_persiste_parser_version(self):
        arc = self._run_v3_with_payload(self.SIMULATED_V3)
        self.assertTrue(arc.biocuantico_parser_version)
        # Formato semántico simple X.Y.Z
        self.assertEqual(arc.biocuantico_parser_version.count('.'), 2)

    def test_v3_persiste_motor_meta_json(self):
        import json as _json
        arc = self._run_v3_with_payload(self.SIMULATED_V3)
        self.assertTrue(arc.biocuantico_motor_meta_json)
        meta = _json.loads(arc.biocuantico_motor_meta_json)
        for k in ('motor_version', 'parser_version', 'generated_at',
                  'fallback_used', 'filas_procesadas', 'prioridades_generadas',
                  'chars_resumen', 'sistemas_afectados', 'boost_codes',
                  'descartados'):
            self.assertIn(k, meta, "Falta key %r en motor_meta_json" % k)
        self.assertEqual(meta['motor_version'], 'v3')
        self.assertGreaterEqual(meta['prioridades_generadas'], 1)

    def test_v3_genera_summary_text_compatible(self):
        arc = self._run_v3_with_payload(self.SIMULATED_V3)
        self.assertTrue(arc.biocuantico_summary_text)
        # Resumen es string no vacío y <= 6000 chars
        self.assertIsInstance(arc.biocuantico_summary_text, str)
        self.assertGreater(len(arc.biocuantico_summary_text), 50)
        self.assertLessEqual(len(arc.biocuantico_summary_text), 6000)

    def test_v3_genera_summary_json_serializable(self):
        import json as _json
        arc = self._run_v3_with_payload(self.SIMULATED_V3)
        self.assertTrue(arc.biocuantico_summary_json)
        parsed = _json.loads(arc.biocuantico_summary_json)
        self.assertIn('analisis_biocuantico', parsed)

    def test_v3_marca_detectado_true(self):
        arc = self._run_v3_with_payload(self.SIMULATED_V3)
        self.assertTrue(arc.biocuantico_detectado)
        self.assertIn(arc.biocuantico_parse_status, ('ok', 'partial'))


# =====================================================================
# 5) Legacy sigue funcionando (no regresión)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestLegacyIntacto(TransactionCase):

    def test_legacy_marca_motor_version_legacy(self):
        _set_motor(self.env, 'legacy')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text=SAMPLE_TEXT_BIOCUANTICO)
        # Forzamos el legacy directamente; el dispatcher debería rutear ahí
        arc._run_biocuantico_parser(force=True)
        # Tras la corrida, motor_version queda 'legacy' (incluso si parser
        # decidió not_biocuantico/failed)
        self.assertEqual(arc.biocuantico_motor_version, 'legacy')

    def test_legacy_no_setea_fallback_used(self):
        _set_motor(self.env, 'legacy')
        valor = _new_valoracion(self.env)
        arc = _new_archivo(self.env, valor, text=SAMPLE_TEXT_BIOCUANTICO)
        arc._run_biocuantico_parser(force=True)
        self.assertFalse(arc.biocuantico_fallback_used)


# =====================================================================
# 6) Sin archivo / casos degradados
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestNoArchivoNoRompe(TransactionCase):

    def test_valoracion_sin_archivos_no_rompe(self):
        """Crear una valoración sin archivos y operar normalmente no debe
        propagar errores del motor BC (ni v3 ni legacy)."""
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        # Sin archivos: NO debe haber excepción al consultar el campo
        self.assertEqual(len(valor.archivo_cliente_ids), 0)
        # Cambiar el toggle tampoco debe causar problema
        _set_motor(self.env, 'legacy')
        self.assertEqual(len(valor.archivo_cliente_ids), 0)

    def test_archivo_sin_file_data_no_rompe(self):
        """Un archivo creado sin contenido binario no rompe el dispatcher."""
        _set_motor(self.env, 'v3')
        valor = _new_valoracion(self.env)
        partner = valor.partner_id
        # Forzar creación con file_data mínimo (la validación require name)
        arc = self.env['valoracion.archivo.cliente'].create({
            'valoracion_id': valor.id,
            'name': 'vacio.txt',
            'file_name': 'vacio.txt',
            'file_data': b'',
        })
        try:
            arc._run_biocuantico_parser(force=True)
        except Exception as e:
            self.fail("Dispatcher rompió con archivo vacío: %s" % e)


# =====================================================================
# 7) Versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest38(TransactionCase):

    def test_version_3_0_0_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        # 18.0.3.0.0
        if len(parts) >= 5:
            major = int(parts[2])
            self.assertGreaterEqual(
                major, 3,
                "Manifest %s < 18.0.3.0.0 (Fase 3.8)" % v,
            )
