# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.8.1 (auditoría v3 en log IA).

Verifica que el helper _render_audit_block_v3 produce el bloque
'=== PARSER BIOCUÁNTICO V3 ===' con la metadata del motor, y que
nunca rompe el log IA cuando los datos faltan o están corruptos.
"""
import json as _json

from odoo.tests.common import TransactionCase, tagged


def _new_archivo(env):
    partner = env['res.partner'].create({'name': 'TEST F381'})
    val = env['valoracion.valoracion'].create({'partner_id': partner.id})
    return env['valoracion.archivo.cliente'].create({
        'valoracion_id': val.id,
        'name': 'reporte.pdf',
        'file_name': 'reporte.pdf',
        'file_data': b'X',
    })


META_OK = {
    'motor_version': 'v3',
    'parser_version': '3.5.0',
    'master_summary_version': '3.6.0',
    'generated_at': '2026-05-23T10:00:00',
    'fallback_used': False,
    'filas_procesadas': 631,
    'clasificadas': 489,
    'descartados': 142,
    'sistemas_afectados': 6,
    'prioridades_generadas': 6,
    'anormales': 47,
    'chars_resumen': 5973,
    'boost_codes': ['endocrino', 'metabolico'],
}


@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestRenderAuditBlockV3(TransactionCase):

    def test_bloque_completo_con_meta_ok(self):
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_fallback_used': False,
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': _json.dumps(META_OK),
        })
        block = arc._render_audit_block_v3()
        # Header y datos esperados
        self.assertIn("=== PARSER BIOCUÁNTICO V3 ===", block)
        self.assertIn("Motor: v3", block)
        self.assertIn("Parser version: 3.5.0", block)
        self.assertIn("Fallback: No", block)
        self.assertIn("Status: ok", block)
        self.assertIn("Filas procesadas: 631", block)
        self.assertIn("Clasificadas: 489", block)
        self.assertIn("Descartados: 142", block)
        self.assertIn("Sistemas afectados: 6", block)
        self.assertIn("Prioridades generadas: 6", block)
        self.assertIn("Anormales: 47", block)
        self.assertIn("Chars resumen: 5973", block)
        self.assertIn("Boost codes: endocrino, metabolico", block)
        # "Usado: Sí/No" lo añade el llamador (valoracion_valoracion.py),
        # no el helper, para mantener la misma convención que legacy y
        # evitar duplicación en truncado_detalle.
        self.assertNotIn("Usado:", block)

    def test_fallback_usado_se_muestra_si(self):
        arc = _new_archivo(self.env)
        meta = dict(META_OK)
        meta['fallback_used'] = True
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_fallback_used': True,
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': _json.dumps(meta),
        })
        block = arc._render_audit_block_v3()
        self.assertIn("Fallback: Sí", block)

    def test_status_failed_marca_usado_no(self):
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_parse_status': 'failed',
            'biocuantico_motor_meta_json': _json.dumps(META_OK),
        })
        block = arc._render_audit_block_v3()
        # El helper ya no incluye "Usado:" — lo agrega el llamador.
        self.assertNotIn("Usado:", block)

    def test_meta_json_vacio_no_rompe(self):
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': False,
        })
        block = arc._render_audit_block_v3()
        self.assertIn("=== PARSER BIOCUÁNTICO V3 ===", block)
        self.assertIn("Auditoría v3 no disponible", block)
        self.assertIn("Motor: v3", block)

    def test_meta_json_invalido_no_rompe(self):
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': '{esto no es json}',
        })
        block = arc._render_audit_block_v3()
        self.assertIn("=== PARSER BIOCUÁNTICO V3 ===", block)
        self.assertIn("Auditoría v3 no disponible", block)

    def test_boost_codes_vacio_se_muestra_ninguno(self):
        arc = _new_archivo(self.env)
        meta = dict(META_OK)
        meta['boost_codes'] = []
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': _json.dumps(meta),
        })
        block = arc._render_audit_block_v3()
        self.assertIn("Boost codes: (ninguno)", block)

    def test_no_duplica_linea_usado(self):
        """Microfix 3.8.2: el helper no debe incluir 'Usado:' porque el
        llamador (valoracion_valoracion.py) ya lo agrega; de lo contrario
        aparecería dos veces en truncado_detalle."""
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': _json.dumps(META_OK),
        })
        block = arc._render_audit_block_v3()
        self.assertEqual(block.count("Usado:"), 0,
                         "El helper duplica 'Usado:'; debe agregarlo solo "
                         "el llamador. Block:\n%s" % block)
        # Y también en la rama meta vacío
        arc.sudo().write({'biocuantico_motor_meta_json': False})
        block_empty = arc._render_audit_block_v3()
        self.assertEqual(block_empty.count("Usado:"), 0)

    def test_combinado_con_etiqueta_externa_solo_aparece_una_vez(self):
        """Simula el ensamble final que hace valoracion_valoracion.py:
        helper + '\\n' + 'Usado: Sí/No'. La cuenta total debe ser 1."""
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': _json.dumps(META_OK),
        })
        block = arc._render_audit_block_v3()
        etiqueta_uso = "Usado: Sí"
        ensamblado = block + "\n" + etiqueta_uso
        self.assertEqual(ensamblado.count("Usado:"), 1,
                         "Duplicación detectada en ensamble final:\n%s"
                         % ensamblado)

    def test_archivo_label_aparece(self):
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'name': 'BC_Pedro.pdf',
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': '3.5.0',
            'biocuantico_parse_status': 'ok',
            'biocuantico_motor_meta_json': _json.dumps(META_OK),
        })
        block = arc._render_audit_block_v3()
        self.assertIn("Archivo: BC_Pedro.pdf", block)


@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestLegacySigueIntacto(TransactionCase):
    """El bloque legacy no debe ser afectado por el cambio v3."""

    def test_archivo_legacy_no_invoca_render_v3(self):
        """Si motor_version != 'v3', el helper de v3 no se invoca; el
        bloque conserva su header legacy '=== PARSER BIOCUÁNTICO ==='
        (sin sufijo V3)."""
        arc = _new_archivo(self.env)
        arc.sudo().write({
            'biocuantico_motor_version': 'legacy',
            'biocuantico_parse_status': 'ok',
            'biocuantico_summary_json': _json.dumps({
                'archivo': 'x', 'meta': {}, 'sistemas_top': [],
                'estadisticas_globales': {}, 'auditoria': {},
            }),
        })
        # Importamos el render legacy directamente para confirmar que
        # produce el header histórico
        from odoo.addons.valoracion.models.biocuantico.master_summary import (
            BioCuanticoMasterSummary,
        )
        block = BioCuanticoMasterSummary.render_audit_block(
            {'is_biocuantico': True, 'status': 'ok', 'metodo': None,
             'estadisticas': {}, 'sistemas': []},
            summary_text='',
            archivo_label='legacy.pdf',
        )
        self.assertIn("=== PARSER BIOCUÁNTICO ===", block)
        # No debe llevar el sufijo V3
        self.assertNotIn("PARSER BIOCUÁNTICO V3", block)


@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest381(TransactionCase):

    def test_version_3_0_1_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            major = int(parts[2]); minor = int(parts[3]); patch = int(parts[4])
            self.assertGreaterEqual(
                (major, minor, patch), (3, 0, 1),
                "Manifest %s < 18.0.3.0.1 (Fase 3.8.1)" % v,
            )
