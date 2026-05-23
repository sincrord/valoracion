# -*- coding: utf-8 -*-
"""
Extensión del modelo nativo calendar.event de Odoo Community 18.

==============================================================================
IMPORTANTE — INHERIT LIMPIO, NO REDEFINICIÓN
==============================================================================
Este archivo SÓLO usa _inherit (atributo de clase). NUNCA define _name
apuntando al modelo nativo, porque eso lo redefiniría y borraría todos
sus métodos (get_recurrent_dates, get_display_time, _compute_attendee, etc.).

Con _inherit (sin _name), todos los métodos nativos de calendar.event
de Odoo Community 18 se conservan intactos automáticamente.

==============================================================================
NOTA SOBRE get_unusual_days
==============================================================================
El widget de calendario del frontend (calendar view) hace una llamada RPC
a `calendar.event.get_unusual_days` para pintar fines de semana / días
no laborables. Este método está definido en el módulo `hr_calendar`
(parte de Enterprise / opcional). Si la instalación de Odoo Community 18
NO tiene hr_calendar instalado, la llamada falla con:

    AttributeError: The method 'calendar.event.get_unusual_days' does not exist

Este archivo añade el método como parte de la herencia, usando el
calendario de recursos por defecto de la compañía actual
(env.company.resource_calendar_id) — que sí está disponible en Community 18
gracias al módulo `resource` (dependencia transitiva de `calendar`).

NO se añade dependencia a hr_calendar (que es Enterprise).

==============================================================================
APORTES DE ESTE ARCHIVO
==============================================================================
  1. Campo valoracion_id (Many2one a valoracion.valoracion).
  2. Método público get_unusual_days (compat Community 18 sin hr_calendar).
  3. Constraint Python _check_solapamiento_usuario (decisión 10).
  4. Acción action_view_valoracion (smart button).
"""
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class CalendarEvent(models.Model):
    # ──────────────────────────────────────────────────────────────────
    # HERENCIA PURA. NO TOCAR _name.
    # ──────────────────────────────────────────────────────────────────
    _inherit = 'calendar.event'

    # ==================================================================
    # Enlace opcional con valoración VitalHealth
    # ==================================================================
    valoracion_id = fields.Many2one(
        comodel_name='valoracion.valoracion',
        string='Valoración VitalHealth',
        ondelete='set null',
        copy=False,
        index=True,
        help=(
            'Valoración funcional VitalHealth a la que está asociada esta '
            'cita. Se llena automáticamente al crear la cita desde el wizard '
            '"Crear Cita" en una valoración. Si la valoración se elimina, '
            'este campo se anula pero la cita se conserva.'
        ),
    )

    # ==================================================================
    # SHIM defensivo: unavailable_partner_ids
    # ==================================================================
    # Algunas vistas/widgets de calendario en Odoo 18 (especialmente cuando
    # módulos como `appointment` están parcialmente referenciados o cuando
    # el widget many2many de attendees evolucionó entre patches) intentan
    # leer el campo `unavailable_partner_ids` en `calendar.event`. Este
    # campo NO existe en Community 18 base, lo que produce el error:
    #
    #     "calendar.event"."unavailable_partner_ids" field is undefined
    #
    # Para absorber esa referencia sin efectos secundarios, declaramos el
    # campo como Many2many computed que SIEMPRE devuelve recordset vacío.
    # No se almacena en BD, no se muestra en ninguna vista nuestra, y no
    # afecta la lógica de solapamiento ni de creación de citas.
    # ==================================================================
    unavailable_partner_ids = fields.Many2many(
        comodel_name='res.partner',
        relation='valoracion_calendar_unavailable_partner_rel',
        column1='event_id',
        column2='partner_id',
        string='Asistentes no disponibles (compat shim)',
        compute='_compute_unavailable_partner_ids',
        store=False,
        help=(
            'Campo defensivo de compatibilidad con vistas/widgets de Odoo 18 '
            'que referencian unavailable_partner_ids cuando el módulo nativo '
            'que normalmente lo provee no está instalado. Siempre vacío.'
        ),
    )

    @api.depends('partner_ids')
    def _compute_unavailable_partner_ids(self):
        """Devuelve recordset vacío para todos los registros."""
        empty = self.env['res.partner']
        for rec in self:
            rec.unavailable_partner_ids = empty

    # ==================================================================
    # get_unusual_days
    # ==================================================================
    # Compatibilidad Community 18 sin hr_calendar.
    # El widget de calendario del frontend invoca este método vía RPC
    # para resaltar días no laborables. Si no existe, la vista calendario
    # falla con: AttributeError: The method 'calendar.event.get_unusual_days'
    # does not exist.
    #
    # Firma estándar esperada por el frontend:
    #   get_unusual_days(date_from, date_to=None) -> dict
    # Retorna: {'YYYY-MM-DD': True/False, ...}
    # ==================================================================
    @api.model
    def get_unusual_days(self, date_from, date_to=None):
        """Devuelve los días inusuales (no laborables) en el rango dado,
        consultando el resource_calendar_id de la compañía actual.

        Implementación:
          1. Toma la compañía del entorno (env.company).
          2. Obtiene su calendario de recursos por defecto.
          3. Si no hay calendario configurado, retorna dict vacío.
          4. Convierte fechas string a datetime si es necesario.
          5. Invoca _get_unusual_days del calendario, probando primero la
             firma de 3 argumentos (date_from, date_to, company) y cayendo
             a la firma de 2 argumentos si la primera no aplica.

        Args:
            date_from: fecha/datetime de inicio (str o datetime).
            date_to:   fecha/datetime de fin (str o datetime, opcional).

        Returns:
            dict {fecha_str: bool} marcando días inusuales. {} si no hay
            calendario en la compañía.
        """
        company = self.env.company
        calendar = company.resource_calendar_id
        if not calendar:
            return {}

        # Normalizar fechas: si vienen como string, convertirlas a datetime
        if isinstance(date_from, str):
            date_from = fields.Datetime.from_string(date_from)
        if date_to and isinstance(date_to, str):
            date_to = fields.Datetime.from_string(date_to)

        # Llamar al método del calendario de recursos.
        # Probamos primero la firma de 3 argumentos (algunas variantes
        # de Odoo 18 la aceptan), y caemos a la de 2 si TypeError.
        try:
            return calendar._get_unusual_days(date_from, date_to, company)
        except TypeError:
            try:
                return calendar._get_unusual_days(date_from, date_to)
            except Exception:
                return {}
        except Exception:
            # Cualquier otro error: degradar a {} para no romper la UI
            return {}

    # ==================================================================
    # Validación de solapamiento por usuario responsable
    # ==================================================================
    @api.constrains('start', 'stop', 'user_id', 'active', 'allday')
    def _check_solapamiento_usuario(self):
        """Bloquea la creación/edición de una cita cuyo rango [start, stop)
        se solape con otra cita activa del mismo user_id.

        Fórmula de solapamiento (rangos abiertos por la derecha):
            other.start < self.stop  AND  other.stop > self.start

        Skips defensivos:
          * Sin user_id, start o stop → no se puede evaluar; se omite.
          * Evento archivado (active=False) → no aplica.
          * stop <= start → Odoo nativo ya valida; no nos compete.
        """
        for event in self:
            if not event.user_id or not event.start or not event.stop:
                continue
            if not event.active:
                continue
            if event.stop <= event.start:
                continue  # Odoo nativo lanza error específico para esto

            domain = [
                ('id', '!=', event.id),
                ('user_id', '=', event.user_id.id),
                ('active', '=', True),
                ('start', '<', event.stop),
                ('stop', '>', event.start),
            ]
            conflicto = self.search(domain, limit=1)
            if not conflicto:
                continue

            # Helper: formatear datetime en TZ del usuario, con fallback
            def _fmt(dt):
                if not dt:
                    return ''
                try:
                    return fields.Datetime.context_timestamp(
                        event, dt
                    ).strftime('%d/%m/%Y %H:%M')
                except Exception:
                    return str(dt)

            partners_label = ', '.join(
                p.name for p in conflicto.partner_ids if p.name
            ) or _('sin contactos')

            raise ValidationError(_(
                "Conflicto de calendario para %(user)s.\n\n"
                "Esta cita ('%(this)s') del %(this_start)s al %(this_stop)s "
                "se solapa con la cita existente:\n"
                "  • Asunto: %(other_name)s\n"
                "  • Horario: %(other_start)s al %(other_stop)s\n"
                "  • Contactos: %(partners)s\n\n"
                "Elige otro horario o reagenda la cita existente."
            ) % {
                'user': event.user_id.display_name,
                'this': event.name or _('(sin asunto)'),
                'this_start': _fmt(event.start),
                'this_stop': _fmt(event.stop),
                'other_name': conflicto.name or _('(sin asunto)'),
                'other_start': _fmt(conflicto.start),
                'other_stop': _fmt(conflicto.stop),
                'partners': partners_label,
            })

    # ==================================================================
    # Smart button: ir a la valoración asociada
    # ==================================================================
    def action_view_valoracion(self):
        """Abre la valoración asociada a esta cita (botón en el form)."""
        self.ensure_one()
        if not self.valoracion_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _('Valoración'),
            'res_model': 'valoracion.valoracion',
            'res_id': self.valoracion_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
