# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ValoracionLogIA(models.Model):
    """Bitácora de cada llamada al proveedor IA.

    Funciones:
      * Auditoría: quién disparó qué, cuándo, qué modelo, qué costo.
      * Diagnóstico: errores, timeouts, parseos fallidos.
      * Control de costos (riesgo #3): tokens consumidos por llamada.
      * Re-generación (decisión D): los logs históricos NO se eliminan al
        re-generar; se acumulan para auditoría completa.

    Acceso:
      * Usuario: solo lee los suyos.
      * Super Usuario: lee todos.
      * Administrador: CRUD total (puede borrar logs antiguos si es necesario).
    """
    _name = 'valoracion.log.ia'
    _description = 'Bitácora de llamada a IA'
    _order = 'create_date desc, id desc'
    _rec_name = 'display_name'

    valoracion_id = fields.Many2one(
        'valoracion.valoracion',
        string='Valoración',
        ondelete='cascade',
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        string='Usuario',
        default=lambda self: self.env.user,
        required=True,
        index=True,
    )
    proveedor = fields.Selection(
        selection=[
            ('claude', 'Claude (Anthropic)'),
            ('openai', 'OpenAI'),
            ('otro', 'Otro'),
        ],
        string='Proveedor',
        default='claude',
        required=True,
    )
    modelo = fields.Char(string='Modelo')
    tokens_input = fields.Integer(string='Tokens entrada')
    tokens_output = fields.Integer(string='Tokens salida')
    tokens_total = fields.Integer(
        string='Tokens totales',
        compute='_compute_tokens_total',
        store=True,
    )
    costo_estimado = fields.Float(
        string='Costo estimado (USD)',
        digits=(12, 6),
        help='Calculado a partir de los costos por 1M tokens configurados '
             'en res.config.settings.',
    )
    duracion_ms = fields.Integer(string='Duración (ms)')
    estado = fields.Selection(
        selection=[
            ('exito', 'Éxito'),
            ('error_api', 'Error de API'),
            ('timeout', 'Timeout'),
            ('error_parsing', 'Error de parseo'),
            ('error_validacion', 'Error de validación'),
            ('error_config', 'Error de configuración'),
        ],
        string='Estado',
        required=True,
        index=True,
    )
    error_message = fields.Text(string='Mensaje de error')
    request_size_bytes = fields.Integer(string='Tamaño request (bytes)')
    response_size_bytes = fields.Integer(string='Tamaño respuesta (bytes)')
    truncado = fields.Boolean(
        string='Prompt truncado',
        help='Verdadero si fue necesario truncar fuentes y/o archivos por '
             'exceder el límite de tokens del prompt.',
    )
    truncado_detalle = fields.Text(string='Detalle de truncado')

    display_name = fields.Char(compute='_compute_display_name', store=True)
    company_id = fields.Many2one(
        related='valoracion_id.company_id',
        store=True,
        string='Compañía',
    )

    # ====================================================================
    # Computeds
    # ====================================================================
    @api.depends('tokens_input', 'tokens_output')
    def _compute_tokens_total(self):
        for rec in self:
            rec.tokens_total = (rec.tokens_input or 0) + (rec.tokens_output or 0)

    @api.depends('valoracion_id.name', 'estado', 'create_date')
    def _compute_display_name(self):
        for rec in self:
            val_name = rec.valoracion_id.name if rec.valoracion_id else '-'
            estado_label = dict(rec._fields['estado'].selection).get(rec.estado, rec.estado or '-')
            rec.display_name = "[%s] %s — %s" % (val_name, estado_label, rec.create_date or '')
