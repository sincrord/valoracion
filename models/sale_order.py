# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    """Extensión de sale.order para enlazar la orden con la valoración
    funcional VitalHealth que la originó.

    Casos de uso:
      * El método valoracion.action_crear_cotizacion crea automáticamente
        sale.order con valoracion_id = self.id y líneas a partir de
        valoracion.linea.producto.
      * El smart button "Valoración" en el form de la orden permite navegar
        de regreso al expediente clínico.
      * El smart button "Ventas" en la valoración consulta tanto las órdenes
        ligadas como las del mismo contacto (criterio OR).
    """
    _inherit = 'sale.order'

    valoracion_id = fields.Many2one(
        'valoracion.valoracion',
        string='Valoración VitalHealth',
        ondelete='set null',
        copy=False,
        index=True,
        help='Valoración funcional VitalHealth que originó esta orden.',
    )

    # ====================================================================
    # Smart button: ir a la valoración
    # ====================================================================
    def action_view_valoracion(self):
        """Abre la valoración ligada a esta orden de venta."""
        self.ensure_one()
        if not self.valoracion_id:
            raise UserError(_(
                "Esta orden no está ligada a ninguna valoración VitalHealth."
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Valoración'),
            'res_model': 'valoracion.valoracion',
            'res_id': self.valoracion_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
