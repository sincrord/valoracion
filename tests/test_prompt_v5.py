# -*- coding: utf-8 -*-
"""Tests Prompt IA v5 — contenido oficial + acción de aplicación.

Valida que el Prompt v5:
  * Mantiene los campos required del schema v4.1 (compat. legacy).
  * Añade programas_funcionales como campo OPCIONAL (no en required).
  * Conserva restricciones legales (no diagnóstico, advertencias por
    medicamento/padecimiento/alergia, supervisión de menores, productos
    sólo del catálogo).
  * Es invocable vía action_aplicar_prompt_v5 sin romper la plantilla
    activa.
  * Convive con action_restaurar_v4_oficial (rollback disponible).
"""
import json as _json

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'valoracion', 'prompt_v5')
class TestPromptV5Content(TransactionCase):

    def _load(self):
        from odoo.addons.valoracion.models.prompt_v5_content import (
            SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, OUTPUT_SCHEMA, VERSION,
        )
        return SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, OUTPUT_SCHEMA, VERSION

    def test_version_es_5_0(self):
        _, _, _, version = self._load()
        self.assertEqual(version, '5.0')

    def test_schema_es_json_valido(self):
        _, _, schema, _ = self._load()
        try:
            parsed = _json.loads(schema)
        except Exception as e:
            self.fail("Schema v5 no es JSON válido: %s" % e)
        self.assertEqual(parsed.get('type'), 'object')

    def test_schema_mantiene_required_v4_1(self):
        """v5 NO puede romper compatibilidad legacy: todos los campos que
        eran required en v4.1 siguen siendo required en v5."""
        _, _, schema, _ = self._load()
        parsed = _json.loads(schema)
        required = set(parsed.get('required') or [])
        required_v4_1 = {
            'analisis_archivo_cliente',
            'prioridades_caso',
            'resultados_valoracion',
            'plan_estrategico',
            'productos_recomendados',
            'habitos_complementarios',
            'resumen_estrategico',
            'resultados_esperados',
            'advertencias',
        }
        missing = required_v4_1 - required
        self.assertFalse(
            missing,
            "v5 rompió compatibilidad: faltan required %r" % sorted(missing),
        )

    def test_programas_funcionales_es_opcional(self):
        """programas_funcionales es nuevo en v5 y NO está en required."""
        _, _, schema, _ = self._load()
        parsed = _json.loads(schema)
        props = parsed.get('properties') or {}
        self.assertIn('programas_funcionales', props,
                      "Falta property 'programas_funcionales' en v5")
        self.assertNotIn('programas_funcionales',
                         set(parsed.get('required') or []),
                         "programas_funcionales NO debe ser required")

    def test_programas_funcionales_estructura(self):
        _, _, schema, _ = self._load()
        parsed = _json.loads(schema)
        pf = parsed['properties']['programas_funcionales']
        self.assertEqual(pf.get('type'), 'array')
        items = pf.get('items') or {}
        self.assertEqual(items.get('type'), 'object')
        item_props = items.get('properties') or {}
        for k in ('momento_dia', 'duracion_semanas', 'intencion', 'acciones'):
            self.assertIn(k, item_props, "Falta key %r en programa item" % k)
        # `intencion` y `acciones` deben ser required del item
        self.assertEqual(
            set(items.get('required') or []),
            {'intencion', 'acciones'},
        )

    def test_system_prompt_sin_palabras_diagnosticas(self):
        """v5 sube el tono pero mantiene cero lenguaje diagnóstico."""
        system_prompt, _, _, _ = self._load()
        low = system_prompt.lower()
        for forbidden in ('padece', 'diagnostica enfermedad',
                          'cura ', 'sufre de'):
            self.assertNotIn(forbidden, low,
                             "v5 contiene lenguaje diagnóstico: %r"
                             % forbidden)

    def test_system_prompt_mantiene_restricciones_legales(self):
        system_prompt, _, _, _ = self._load()
        low = system_prompt.lower()
        # Algunas anchors que confirman las restricciones siguen presentes
        anchors = [
            'no diagnost',          # "nunca diagnosticas enfermedades"
            'no sustituyes',        # tratamiento médico
            'catalogo' if 'catalogo' in low else 'catálogo',
            'menor de edad',
            'medicamento',
            'alergia',
        ]
        for a in anchors:
            self.assertIn(a, low,
                          "v5 perdió restricción/anchor: %r" % a)

    def test_system_prompt_tono_humano_wellness(self):
        """Confirma vocabulario wellness/humano nuevo en v5."""
        system_prompt, _, _, _ = self._load()
        low = system_prompt.lower()
        # Al menos 3 anclas de tono wellness deben aparecer
        anchors_wellness = (
            'acompañ', 'funcional', 'ritual', 'cuerpo',
            'bienestar', 'cálid',
        )
        hits = sum(1 for a in anchors_wellness if a in low)
        self.assertGreaterEqual(hits, 3,
                                "v5 no es lo suficientemente wellness "
                                "(hits=%d): %r" % (hits, anchors_wellness))

    def test_user_prompt_menciona_programas_funcionales(self):
        _, user_prompt, _, _ = self._load()
        self.assertIn('programas_funcionales', user_prompt)

    def test_user_prompt_mantiene_placeholders(self):
        """Los placeholders {{...}} usados por el render legacy deben
        seguir existiendo en v5 para no romper el ensamble del prompt."""
        _, user_prompt, _, _ = self._load()
        for ph in (
            '{{CONTENIDO_ARCHIVOS_CLIENTE}}',
            '{{DATOS_CONTACTO}}',
            '{{ANTECEDENTE_CLINICO}}',
            '{{CATALOGO_VITALHEALTH}}',
            '{{FUENTES_DEL_MODULO}}',
        ):
            self.assertIn(ph, user_prompt,
                          "v5 perdió placeholder %r" % ph)

    def test_user_prompt_menciona_advertencias_minimas(self):
        """Mínimos exigibles de v4.1 siguen mencionados en el user prompt."""
        _, user_prompt, _, _ = self._load()
        low = user_prompt.lower()
        for must in ('medicamento', 'padecimiento', 'alergia',
                     'menor de edad', 'archivo insuficiente'):
            self.assertIn(must, low,
                          "v5 perdió advertencia mínima: %r" % must)


@tagged('post_install', '-at_install', 'valoracion', 'prompt_v5')
class TestAccionAplicarV5(TransactionCase):

    def _get_template(self):
        Template = self.env['valoracion.prompt.template']
        rec = Template.search([], limit=1)
        if not rec:
            rec = Template.create({
                'name': 'TEST v5',
                'system_prompt': 'old system',
                'user_prompt_template': 'old user',
                'output_schema': '{}',
                'version': '0.0',
                'is_default': False,
                'active': True,
            })
        return rec

    def test_aplicar_v5_sobreescribe_contenido(self):
        rec = self._get_template()
        rec.action_aplicar_prompt_v5()
        from odoo.addons.valoracion.models.prompt_v5_content import (
            SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, OUTPUT_SCHEMA, VERSION,
        )
        self.assertEqual(rec.version, VERSION)
        self.assertEqual(rec.system_prompt, SYSTEM_PROMPT)
        self.assertEqual(rec.user_prompt_template, USER_PROMPT_TEMPLATE)
        self.assertEqual(rec.output_schema, OUTPUT_SCHEMA)

    def test_aplicar_v5_preserva_name_y_flags(self):
        rec = self._get_template()
        rec.write({
            'name': 'Mi plantilla custom',
            'is_default': False,
            'active': True,
            'notes': 'notas internas',
        })
        rec.action_aplicar_prompt_v5()
        self.assertEqual(rec.name, 'Mi plantilla custom')
        self.assertFalse(rec.is_default)
        self.assertTrue(rec.active)
        self.assertEqual(rec.notes, 'notas internas')

    def test_aplicar_v5_no_falla_sin_mail_thread(self):
        """Microfix 3.1.1: el modelo no hereda mail.thread; la acción
        debe completar sin lanzar AttributeError por message_post."""
        rec = self._get_template()
        try:
            rec.action_aplicar_prompt_v5()
        except AttributeError as e:
            if 'message_post' in str(e):
                self.fail("message_post sin guardia rompió la acción: %s" % e)
            raise
        self.assertEqual(rec.version, '5.0')

    def test_output_schema_persistido_contiene_programas_funcionales(self):
        """Microfix 3.1.1: tras aplicar v5, el output_schema persistido
        en BD DEBE contener programas_funcionales. Si message_post lanzaba
        excepción, la transacción hacía rollback y este campo no quedaba."""
        rec = self._get_template()
        rec.action_aplicar_prompt_v5()
        self.assertTrue(rec.output_schema,
                        "output_schema vacío tras aplicar v5")
        try:
            parsed = _json.loads(rec.output_schema)
        except Exception as e:
            self.fail("output_schema persistido no es JSON válido: %s" % e)
        self.assertIn('programas_funcionales',
                      (parsed.get('properties') or {}),
                      "BD no recibió programas_funcionales — posible rollback "
                      "por message_post")
        # No debe ser required (es opcional)
        self.assertNotIn('programas_funcionales',
                         set(parsed.get('required') or []))

    def test_aplicar_v5_persiste_los_4_campos(self):
        """Confirma que todos los campos del write se persistieron en BD."""
        from odoo.addons.valoracion.models.prompt_v5_content import (
            SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, OUTPUT_SCHEMA, VERSION,
        )
        rec = self._get_template()
        rec.write({
            'system_prompt': 'X', 'user_prompt_template': 'X',
            'output_schema': '{}', 'version': '0.0',
        })
        rec.action_aplicar_prompt_v5()
        self.assertEqual(rec.version, VERSION)
        self.assertEqual(rec.system_prompt, SYSTEM_PROMPT)
        self.assertEqual(rec.user_prompt_template, USER_PROMPT_TEMPLATE)
        self.assertEqual(rec.output_schema, OUTPUT_SCHEMA)

    def test_restaurar_v4_1_no_falla_sin_mail_thread(self):
        """Mismo microfix aplicado a la acción v4.1."""
        rec = self._get_template()
        try:
            rec.action_restaurar_v4_oficial()
        except AttributeError as e:
            if 'message_post' in str(e):
                self.fail("v4.1 message_post sin guardia rompe: %s" % e)
            raise
        from odoo.addons.valoracion.models.prompt_v4_1_content import (
            VERSION as V41_VERSION,
        )
        self.assertEqual(rec.version, V41_VERSION)

    def test_rollback_v4_1_despues_de_v5(self):
        """Tras aplicar v5, el rollback a v4.1 debe funcionar y dejar la
        plantilla en el wording oficial v4.1."""
        rec = self._get_template()
        rec.action_aplicar_prompt_v5()
        self.assertEqual(rec.version, '5.0')
        rec.action_restaurar_v4_oficial()
        from odoo.addons.valoracion.models.prompt_v4_1_content import (
            VERSION as V41_VERSION,
        )
        self.assertEqual(rec.version, V41_VERSION)


@tagged('post_install', '-at_install', 'valoracion', 'prompt_v5')
class TestManifest311(TransactionCase):

    def test_version_3_1_1_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            major = int(parts[2]); minor = int(parts[3]); patch = int(parts[4])
            self.assertGreaterEqual(
                (major, minor, patch), (3, 1, 1),
                "Manifest %s < 18.0.3.1.1 (microfix v5 message_post)" % v,
            )
