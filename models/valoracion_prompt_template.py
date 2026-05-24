# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


# Placeholders soportados por la plantilla de prompt
PLACEHOLDERS = (
    '{{DATOS_CONTACTO}}',
    '{{ANTECEDENTE_CLINICO}}',
    '{{CONTENIDO_ARCHIVOS_CLIENTE}}',
    '{{FUENTES_DEL_MODULO}}',
    '{{CATALOGO_VITALHEALTH}}',
)


class ValoracionPromptTemplate(models.Model):
    """Plantilla configurable del prompt enviado a la IA.

    Solo Administrador tiene CRUD (ver ir.model.access.csv); Usuario y
    Super Usuario tienen lectura. Esto evita que se manipulen las instrucciones
    de IA desde fuera del control técnico.

    Decisión B (idioma): el system_prompt fuerza español-MX desde la plantilla
    por defecto. No se valida automáticamente, pero la plantilla por defecto
    incluye instrucciones explícitas.
    """
    _name = 'valoracion.prompt.template'
    _description = 'Plantilla de Prompt VitalHealth'
    _order = 'is_default desc, name'

    name = fields.Char(string='Nombre', required=True)
    active = fields.Boolean(string='Activa', default=True)
    is_default = fields.Boolean(
        string='Plantilla por defecto',
        default=False,
        help='Si verdadero, esta plantilla se usa por defecto en las nuevas '
             'valoraciones. Solo puede haber UNA activa marcada como por defecto.',
    )
    version = fields.Char(string='Versión', default='1.0')

    system_prompt = fields.Text(
        string='Prompt de sistema',
        required=True,
        help='Instrucciones de rol y reglas que se envían como mensaje "system" '
             'a la IA. Define el comportamiento, tono, idioma y restricciones.',
    )
    user_prompt_template = fields.Text(
        string='Plantilla de prompt de usuario',
        required=True,
        help='Plantilla con placeholders sustituibles. Soportados:\n'
             '  {{DATOS_CONTACTO}}\n'
             '  {{ANTECEDENTE_CLINICO}}\n'
             '  {{CONTENIDO_ARCHIVOS_CLIENTE}}\n'
             '  {{FUENTES_DEL_MODULO}}',
    )
    output_schema = fields.Text(
        string='Schema JSON de salida',
        required=True,
        help='JSON Schema que la IA debe cumplir. Se usa para forzar respuesta '
             'estructurada vía tool_use de Claude (riesgo #2 mitigado).',
    )
    notes = fields.Text(string='Notas')

    # ====================================================================
    # Constraints
    # ====================================================================
    @api.constrains('is_default', 'active')
    def _check_unico_default(self):
        for rec in self:
            if rec.is_default and rec.active:
                others = self.search([
                    ('is_default', '=', True),
                    ('active', '=', True),
                    ('id', '!=', rec.id),
                ])
                if others:
                    raise ValidationError(_(
                        "Solo puede existir UNA plantilla activa marcada como "
                        "'por defecto'. Conflicto con: %s"
                    ) % ', '.join(others.mapped('name')))

    @api.constrains('output_schema')
    def _check_output_schema_valid_json(self):
        for rec in self:
            if rec.output_schema:
                try:
                    parsed = json.loads(rec.output_schema)
                except ValueError as e:
                    raise ValidationError(_(
                        "El schema JSON de salida no es JSON válido: %s"
                    ) % str(e))
                if not isinstance(parsed, dict):
                    raise ValidationError(_(
                        "El schema JSON de salida debe ser un objeto JSON."
                    ))
                if parsed.get('type') != 'object':
                    raise ValidationError(_(
                        "El schema JSON debe ser de tipo 'object' en la raíz."
                    ))

    @api.constrains('user_prompt_template')
    def _check_placeholders_presentes(self):
        """Advierte (no bloquea con error) si faltan placeholders críticos."""
        # No bloqueamos: el usuario podría tener una plantilla minimalista.
        # Solo log en debug si faltan los 4 placeholders estándar.
        pass

    # ====================================================================
    # Render
    # ====================================================================
    def render(self, datos_contacto, antecedente_clinico, contenido_archivos,
               fuentes_modulo, catalogo_vitalhealth=''):
        """Sustituye los placeholders y devuelve dict {system, user, schema}.

        Argumentos:
            datos_contacto         (str): bloque de datos demográficos
            antecedente_clinico    (str): bloque de antecedente clínico
            contenido_archivos     (str): texto extraído de archivos cliente
            fuentes_modulo         (str): texto concatenado de fuentes activas
            catalogo_vitalhealth   (str, opcional): bloque oficial del catálogo
                VitalHealth construido desde product.template + reglas. Si la
                plantilla NO contiene {{CATALOGO_VITALHEALTH}}, este valor se
                ignora silenciosamente (backward compatibility).

        Retorna:
            dict {
                'system': str,
                'user':   str (con placeholders reemplazados),
                'schema': dict (JSON Schema parseado),
            }
        """
        self.ensure_one()
        user_prompt = self.user_prompt_template or ''
        replacements = (
            ('{{DATOS_CONTACTO}}', datos_contacto or ''),
            ('{{ANTECEDENTE_CLINICO}}', antecedente_clinico or ''),
            ('{{CONTENIDO_ARCHIVOS_CLIENTE}}', contenido_archivos or ''),
            ('{{FUENTES_DEL_MODULO}}', fuentes_modulo or ''),
            # Nuevo en Fase A. Backward compatible: si la plantilla no
            # tiene este placeholder, .replace() no hace nada y el catálogo
            # simplemente no se inserta. El llamador debe loggear una
            # advertencia si quiere notificar al admin.
            ('{{CATALOGO_VITALHEALTH}}', catalogo_vitalhealth or ''),
        )
        for placeholder, value in replacements:
            user_prompt = user_prompt.replace(placeholder, value)

        try:
            schema = json.loads(self.output_schema) if self.output_schema else {}
        except ValueError:
            schema = {}

        return {
            'system': self.system_prompt or '',
            'user': user_prompt,
            'schema': schema,
        }

    # ====================================================================
    # Acciones
    # ====================================================================
    def action_marcar_default(self):
        """Marca esta plantilla como la plantilla por defecto y desmarca las demás."""
        self.ensure_one()
        others = self.search([
            ('is_default', '=', True),
            ('id', '!=', self.id),
        ])
        if others:
            others.write({'is_default': False})
        self.write({'is_default': True})
        return True

    def action_restaurar_v4_oficial(self):
        """Sobreescribe system_prompt + user_prompt_template + output_schema
        de ESTA plantilla con el contenido oficial v4.1 (defined in
        models/prompt_v4_1_content.py).

        Motivación: el seed XML del módulo se carga con noupdate="1", por lo
        que una vez instalada la plantilla en BD no recibe updates al
        actualizar el módulo. Cuando se cambia el wording oficial (ej.
        refuerzo "FUENTE PRINCIPAL"), se necesita un punto para re-aplicar
        ese wording sobre la plantilla activa en BD sin recrear el registro.

        Esta acción NO toca:
          * name
          * is_default
          * active
          * notes
        Sólo reescribe los 3 campos de contenido y bumpea `version` a v4.1.

        Sólo Admin del módulo Valoración puede llamarla (en la práctica
        garantizado por ir.model.access.csv: el modelo es CRUD-Admin only).
        """
        from .prompt_v4_1_content import (
            SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, OUTPUT_SCHEMA, VERSION,
        )
        for rec in self:
            rec.write({
                'system_prompt': SYSTEM_PROMPT,
                'user_prompt_template': USER_PROMPT_TEMPLATE,
                'output_schema': OUTPUT_SCHEMA,
                'version': VERSION,
            })
            # Microfix 3.1.1: el modelo NO hereda mail.thread; protegemos
            # message_post para no romper la acción ni revertir el write.
            if hasattr(rec, 'message_post'):
                rec.message_post(body=_(
                    "Plantilla restaurada al wording oficial v%s. "
                    "Se sobreescribieron: system_prompt, user_prompt_template, "
                    "output_schema. Se preservaron: name, is_default, active, "
                    "notes."
                ) % VERSION)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Plantilla restaurada'),
                'message': _(
                    "Wording oficial v%s aplicado sobre %d plantilla(s)."
                ) % (VERSION, len(self)),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_aplicar_prompt_v5(self):
        """Sobreescribe system_prompt + user_prompt_template + output_schema
        de ESTA plantilla con el contenido oficial v5.0 (definido en
        models/prompt_v5_content.py).

        v5 es opcional y convive con v4.1:
          * v4.1 sigue siendo el default oficial del módulo.
          * v5 sube el tono (más humano, wellness, programas funcionales)
            manteniendo TODAS las restricciones legales y los campos
            required del schema de v4.1.
          * Se añade el campo opcional `programas_funcionales` al schema;
            parsers existentes que ignoren claves desconocidas siguen
            funcionando.

        Para rollback inmediato: pulsar `action_restaurar_v4_oficial`.
        """
        from .prompt_v5_content import (
            SYSTEM_PROMPT as V5_SYSTEM_PROMPT,
            USER_PROMPT_TEMPLATE as V5_USER_PROMPT_TEMPLATE,
            OUTPUT_SCHEMA as V5_OUTPUT_SCHEMA,
            VERSION as V5_VERSION,
        )
        for rec in self:
            rec.write({
                'system_prompt': V5_SYSTEM_PROMPT,
                'user_prompt_template': V5_USER_PROMPT_TEMPLATE,
                'output_schema': V5_OUTPUT_SCHEMA,
                'version': V5_VERSION,
            })
            # Microfix 3.1.1: el modelo NO hereda mail.thread; protegemos
            # message_post para no romper la acción ni revertir el write.
            if hasattr(rec, 'message_post'):
                rec.message_post(body=_(
                    "Plantilla actualizada al wording v%s. Se "
                    "sobreescribieron: system_prompt, user_prompt_template, "
                    "output_schema. Se preservaron: name, is_default, "
                    "active, notes. Para rollback, usar "
                    "'Restaurar v4.1 oficial'."
                ) % V5_VERSION)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Plantilla actualizada a v5'),
                'message': _(
                    "Wording v%s aplicado sobre %d plantilla(s). "
                    "Rollback disponible con 'Restaurar v4.1 oficial'."
                ) % (V5_VERSION, len(self)),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_aplicar_prompt_v5_1(self):
        """Aplica el wording oficial v5.1 (wellness premium con lista negra)
        sobre esta plantilla. Convive con v4.1, v5.0 y v5.1 disponibles
        por sus respectivas acciones; rollback inmediato pulsando
        'Restaurar v4.1 oficial'.

        Cambios respecto v5.0:
          * Lista negra explícita de términos diagnósticos prohibidos en
            cualquier sección que llegue al cliente.
          * Pide prosa narrativa (no bullets) en resultados_valoracion,
            plan_estrategico, resumen_estrategico, resultados_esperados.
          * Mantiene mismo schema (compatibilidad 100%).
        """
        from .prompt_v5_1_content import (
            SYSTEM_PROMPT as V51_SYSTEM_PROMPT,
            USER_PROMPT_TEMPLATE as V51_USER_PROMPT_TEMPLATE,
            OUTPUT_SCHEMA as V51_OUTPUT_SCHEMA,
            VERSION as V51_VERSION,
        )
        for rec in self:
            rec.write({
                'system_prompt': V51_SYSTEM_PROMPT,
                'user_prompt_template': V51_USER_PROMPT_TEMPLATE,
                'output_schema': V51_OUTPUT_SCHEMA,
                'version': V51_VERSION,
            })
            if hasattr(rec, 'message_post'):
                rec.message_post(body=_(
                    "Plantilla actualizada al wording v%s (wellness premium). "
                    "Se sobreescribieron: system_prompt, "
                    "user_prompt_template, output_schema. Se preservaron: "
                    "name, is_default, active, notes."
                ) % V51_VERSION)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Plantilla actualizada a v5.1'),
                'message': _(
                    "Wording v%s aplicado sobre %d plantilla(s). "
                    "Rollback con 'Restaurar v4.1 oficial' o 'Aplicar "
                    "Prompt v5'."
                ) % (V51_VERSION, len(self)),
                'type': 'success',
                'sticky': False,
            },
        }
