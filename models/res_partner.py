# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    """Extiende res.partner para soportar antecedente clínico funcional,
    datos demográficos VitalHealth y smart buttons hacia valoraciones y citas.

    Decisión de diseño:
    - No se modifica la vista global de res.partner (decisión 3).
    - Los campos se añaden al modelo de forma global, pero su despliegue se
      controla en una vista específica para el flujo VitalHealth (Etapa 5).
    - 'objetivo_principal' es un campo nuevo (decisión 7); NO se renombra
      ni se altera el campo nativo category_id (Etiquetas).
    """
    _inherit = 'res.partner'

    # ====================================================================
    # Demográficos
    # ====================================================================
    birthdate = fields.Date(
        string='Fecha de nacimiento',
        tracking=True,
        help='Se utiliza para calcular la edad de forma automática.',
    )
    age = fields.Integer(
        string='Edad',
        compute='_compute_age',
        store=False,
        help='Edad calculada a partir de la fecha de nacimiento. '
             'No se almacena para evitar desactualización con el paso del tiempo.',
    )
    sexo = fields.Selection(
        selection=[
            ('masculino', 'Masculino'),
            ('femenino', 'Femenino'),
            ('otro', 'Otro'),
            ('no_especificado', 'No especificado'),
        ],
        string='Sexo',
        tracking=True,
    )
    estatura_cm = fields.Integer(
        string='Estatura (cm)',
        help='Estatura del contacto en centímetros.',
    )
    peso_kg = fields.Float(
        string='Peso (kg)',
        digits=(6, 2),
        help='Peso del contacto en kilogramos.',
    )
    objetivo_principal = fields.Char(
        string='Objetivo principal',
        tracking=True,
        help='Objetivo principal del contacto en el plan de bienestar VitalHealth. '
             'Campo independiente; no afecta a las etiquetas (category_id).',
    )

    # ====================================================================
    # Antecedente Clínico
    # ====================================================================
    antecedente_medicamentos = fields.Text(
        string='¿Tomas algún medicamento actualmente? ¿Cuál y dosis?',
    )
    antecedente_padecimientos = fields.Text(
        string='¿Tienes algún padecimiento diagnosticado?',
    )
    antecedente_suplementos = fields.Text(
        string='¿Tomas suplementos actualmente? ¿Cuáles?',
    )
    antecedente_alergias = fields.Text(
        string='¿Tienes alguna alergia o restricción?',
    )
    antecedente_completo = fields.Boolean(
        string='Antecedente clínico completo',
        compute='_compute_antecedente_completo',
        store=True,
        help='Verdadero si los cuatro campos del antecedente clínico están llenos.',
    )

    # ====================================================================
    # Smart buttons (counts)
    # ====================================================================
    valoracion_ids = fields.One2many(
        'valoracion.valoracion',
        'partner_id',
        string='Valoraciones',
    )
    valoracion_count = fields.Integer(
        string='Total de valoraciones',
        compute='_compute_valoracion_count',
    )
    valoracion_cita_count = fields.Integer(
        string='Total de citas',
        compute='_compute_valoracion_cita_count',
    )

    # ====================================================================
    # Computeds
    # ====================================================================
    @api.depends('birthdate')
    def _compute_age(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.birthdate:
                age = today.year - rec.birthdate.year
                # Resta 1 si aún no ha pasado el cumpleaños este año
                if (today.month, today.day) < (rec.birthdate.month, rec.birthdate.day):
                    age -= 1
                rec.age = max(age, 0)
            else:
                rec.age = 0

    @api.depends(
        'antecedente_medicamentos',
        'antecedente_padecimientos',
        'antecedente_suplementos',
        'antecedente_alergias',
    )
    def _compute_antecedente_completo(self):
        for rec in self:
            rec.antecedente_completo = bool(
                (rec.antecedente_medicamentos or '').strip()
                and (rec.antecedente_padecimientos or '').strip()
                and (rec.antecedente_suplementos or '').strip()
                and (rec.antecedente_alergias or '').strip()
            )

    def _compute_valoracion_count(self):
        # Defensivo: si el usuario no tiene acceso al modelo (por ejemplo
        # un usuario sin grupo VitalHealth abriendo el partner desde CRM
        # vía export o mass action), devolvemos 0 sin lanzar AccessError.
        Val = self.env['valoracion.valoracion']
        has = Val.has_access('read')
        for rec in self:
            rec.valoracion_count = (
                Val.search_count([('partner_id', '=', rec.id)]) if has else 0
            )

    def _compute_valoracion_cita_count(self):
        Event = self.env['calendar.event']
        has = Event.has_access('read')
        for rec in self:
            rec.valoracion_cita_count = (
                Event.search_count([('partner_ids', 'in', rec.id)]) if has else 0
            )

    # ====================================================================
    # Constraints
    # ====================================================================
    @api.constrains('birthdate')
    def _check_birthdate_not_future(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.birthdate and rec.birthdate > today:
                raise ValidationError(_(
                    "La fecha de nacimiento de '%s' no puede estar en el futuro."
                ) % rec.display_name)

    @api.constrains('estatura_cm')
    def _check_estatura_positive(self):
        for rec in self:
            if rec.estatura_cm and rec.estatura_cm <= 0:
                raise ValidationError(_(
                    "La estatura debe ser mayor a 0 cm para '%s'."
                ) % rec.display_name)

    @api.constrains('peso_kg')
    def _check_peso_positive(self):
        for rec in self:
            if rec.peso_kg and rec.peso_kg <= 0:
                raise ValidationError(_(
                    "El peso debe ser mayor a 0 kg para '%s'."
                ) % rec.display_name)

    # ====================================================================
    # Acciones (smart buttons)
    # ====================================================================
    def action_view_valoraciones(self):
        """Abre la lista de valoraciones del contacto."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Valoraciones de %s') % self.name,
            'res_model': 'valoracion.valoracion',
            'view_mode': 'list,form',
            'domain': [('partner_id', '=', self.id)],
            'context': {
                'default_partner_id': self.id,
                'search_default_partner_id': self.id,
            },
        }

    def action_view_valoracion_citas(self):
        """Abre las citas de calendario donde participa el contacto."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Citas de %s') % self.name,
            'res_model': 'calendar.event',
            'view_mode': 'calendar,list,form',
            'domain': [('partner_ids', 'in', self.id)],
            'context': {
                'default_partner_ids': [(4, self.id)],
                'default_name': self.name or '',
                'default_duration': 0.5,
            },
        }
