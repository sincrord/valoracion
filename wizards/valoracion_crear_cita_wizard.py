# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class ValoracionCrearCitaWizard(models.TransientModel):
    """Asistente para crear una calendar.event desde una valoración.

    Defaults aplicados (decisiones de la propuesta):
      * Asunto = nombre del contacto.
      * Duración = 30 minutos (0.5 horas).
      * Asistente = usuario actual (puede cambiarlo).
      * Inicio = ahora redondeado al próximo cuarto de hora.

    Validaciones (decisión 10):
      * Pre-validación de solapamiento ANTES de crear la cita (mejor UX
        que esperar a que la constraint del modelo lo bloquee).
      * La constraint en calendar_event._check_solapamiento_usuario actúa
        como segunda barrera (defensa en profundidad).

    Tras crear la cita:
      * Se enlaza con la valoración vía valoracion_id.
      * Se publica mensaje en el chatter de la valoración.
      * Se redirige al form de la cita creada.
    """
    _name = 'valoracion.crear.cita.wizard'
    _description = 'Asistente para crear cita desde valoración'

    valoracion_id = fields.Many2one(
        'valoracion.valoracion',
        string='Valoración',
        required=True,
        ondelete='cascade',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Contacto',
        related='valoracion_id.partner_id',
        readonly=True,
    )
    user_id = fields.Many2one(
        'res.users',
        string='Asistente / Responsable',
        default=lambda self: self.env.user,
        required=True,
        help='Usuario de Odoo responsable de la cita. La validación de '
             'solapamiento se hace contra todas las citas de este usuario.',
    )
    name = fields.Char(string='Asunto', required=True)
    start = fields.Datetime(
        string='Inicio',
        required=True,
        default=lambda self: self._default_start(),
    )
    duration = fields.Float(
        string='Duración (horas)',
        default=0.5,
        required=True,
        help='Duración en horas. 0.5 = 30 minutos.',
    )
    stop = fields.Datetime(
        string='Fin',
        compute='_compute_stop',
        store=False,
    )
    description = fields.Text(string='Descripción')

    # ====================================================================
    # Defaults dinámicos
    # ====================================================================
    @api.model
    def _default_start(self):
        """Próximo múltiplo de 15 minutos respecto a 'ahora' (UTC)."""
        now = fields.Datetime.now()
        # Redondear al siguiente cuarto de hora
        minute_block = (now.minute // 15) + 1
        new_minute = minute_block * 15
        if new_minute >= 60:
            new = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            new = now.replace(minute=new_minute, second=0, microsecond=0)
        return new

    @api.model
    def default_get(self, fields_list):
        """Si viene default_valoracion_id en contexto, deriva el asunto desde
        el partner de la valoración cuando 'name' no fue forzado por contexto.
        """
        defaults = super().default_get(fields_list)
        ctx = self.env.context
        if 'name' in fields_list and not defaults.get('name'):
            valoracion_id = ctx.get('default_valoracion_id')
            if valoracion_id:
                val = self.env['valoracion.valoracion'].browse(valoracion_id)
                if val.exists() and val.partner_id:
                    defaults['name'] = val.partner_id.name or _('Cita')
        if 'name' in fields_list and not defaults.get('name'):
            defaults['name'] = _('Cita')
        return defaults

    # ====================================================================
    # Computed
    # ====================================================================
    @api.depends('start', 'duration')
    def _compute_stop(self):
        for rec in self:
            if rec.start and rec.duration and rec.duration > 0:
                rec.stop = rec.start + timedelta(hours=rec.duration)
            else:
                rec.stop = rec.start

    # ====================================================================
    # Constraints del wizard (validación local antes de crear)
    # ====================================================================
    @api.constrains('duration')
    def _check_duration(self):
        for rec in self:
            if rec.duration is not None and rec.duration <= 0:
                raise ValidationError(_("La duración debe ser mayor a cero."))

    # ====================================================================
    # Acción principal
    # ====================================================================
    def action_crear_cita(self):
        """Valida solapamiento y crea la cita ligada a la valoración."""
        self.ensure_one()

        # Validaciones básicas
        if not self.valoracion_id:
            raise UserError(_("No hay valoración asociada al wizard."))
        if not self.partner_id:
            raise UserError(_("La valoración no tiene contacto asignado."))
        if not self.user_id:
            raise UserError(_("Debes especificar el usuario asistente."))
        if not self.start:
            raise UserError(_("Debes especificar fecha y hora de inicio."))
        if self.duration <= 0:
            raise UserError(_("La duración debe ser mayor a cero."))

        stop = self.start + timedelta(hours=self.duration)

        # Pre-validación de solapamiento (mejor UX que dejar caer la constraint)
        Event = self.env['calendar.event']
        conflictos = Event.search([
            ('user_id', '=', self.user_id.id),
            ('active', '=', True),
            ('start', '<', stop),
            ('stop', '>', self.start),
        ], limit=1)

        if conflictos:
            conflicto = conflictos[0]
            other_partners = ', '.join(
                p.name for p in conflicto.partner_ids if p.name
            ) or _('sin contactos')
            raise UserError(_(
                "El usuario %(user)s ya tiene una cita que se solapa con el "
                "horario propuesto:\n\n"
                "  • Asunto: %(name)s\n"
                "  • Horario: %(start)s al %(stop)s\n"
                "  • Contactos: %(partners)s\n\n"
                "Elige otro horario."
            ) % {
                'user': self.user_id.display_name,
                'name': conflicto.name or _('(sin asunto)'),
                'start': fields.Datetime.context_timestamp(
                    self, conflicto.start
                ).strftime('%d/%m/%Y %H:%M'),
                'stop': fields.Datetime.context_timestamp(
                    self, conflicto.stop
                ).strftime('%d/%m/%Y %H:%M'),
                'partners': other_partners,
            })

        # Crear la cita
        event_vals = {
            'name': self.name,
            'start': self.start,
            'stop': stop,
            'duration': self.duration,
            'partner_ids': [(4, self.partner_id.id)],
            'user_id': self.user_id.id,
            'valoracion_id': self.valoracion_id.id,
            'description': self.description or '',
        }
        event = Event.create(event_vals)

        # Aviso en chatter de la valoración
        self.valoracion_id.message_post(body=_(
            "Cita programada: <b>%(name)s</b> — del %(start)s al %(stop)s "
            "con %(user)s."
        ) % {
            'name': event.name,
            'start': fields.Datetime.context_timestamp(
                self, event.start
            ).strftime('%d/%m/%Y %H:%M'),
            'stop': fields.Datetime.context_timestamp(
                self, event.stop
            ).strftime('%d/%m/%Y %H:%M'),
            'user': self.user_id.display_name,
        })

        # Redirigir al form del evento creado
        return {
            'type': 'ir.actions.act_window',
            'name': _('Cita'),
            'res_model': 'calendar.event',
            'res_id': event.id,
            'view_mode': 'form',
            'target': 'current',
        }
