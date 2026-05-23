# -*- coding: utf-8 -*-
"""Reglas comerciales por producto VitalHealth.

Modelo configurable que reemplaza el archivo observaciones.txt como fuente
de cálculo de cantidad mensual de paquetes. Cada producto puede tener una
regla activa por compañía con:

  * duracion_paquete_dias: cuántos días dura un paquete del producto.
  * cantidad_mensual_manual + usar_cantidad_manual: override explícito.
  * cantidad_mensual: calculado = ceil(30 / duracion_paquete_dias) si no se
    usa el override manual.
  * dosis_sugerida: texto informativo (no afecta cantidad de compra).
  * observaciones: notas internas.

Si no existe regla para un producto, el cálculo cae a 1 paquete/mes (default).
"""
import math

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ValoracionProductoRegla(models.Model):
    _name = 'valoracion.producto.regla'
    _description = 'Regla de Producto VitalHealth'
    _order = 'prioridad desc, id'
    _rec_name = 'product_id'

    product_id = fields.Many2one(
        'product.template',
        string='Producto',
        required=True,
        ondelete='cascade',
        index=True,
        domain="[('is_vitalhealth_effective', '=', True)]",
    )
    active = fields.Boolean(default=True)

    duracion_paquete_dias = fields.Integer(
        string='Duración del paquete (días)',
        default=30,
        required=True,
        help='Cuántos días dura UN paquete del producto. Ej: 30 = mensual; '
             '15 = quincenal (VITAL PRO); 7 = semanal (V-TE DETOX).',
    )
    usar_cantidad_manual = fields.Boolean(
        string='Usar cantidad manual',
        default=False,
        help='Si está activado, ignora el cálculo automático y usa '
             '"Cantidad mensual manual".',
    )
    cantidad_mensual_manual = fields.Float(
        string='Cantidad mensual manual',
        digits=(12, 2),
        default=1.0,
        help='Solo se aplica cuando "Usar cantidad manual" está activado.',
    )
    cantidad_mensual = fields.Float(
        string='Cantidad mensual (paquetes)',
        compute='_compute_cantidad_mensual',
        store=True,
        digits=(12, 2),
        help='Calculada como ceil(30 / duracion_paquete_dias). '
             'Si "Usar cantidad manual" está activo, se usa el valor manual.',
    )

    dosis_sugerida = fields.Text(
        string='Dosis sugerida',
        help='Dosis o forma de consumo sugerida. Se usa como fallback '
             'informativo cuando la IA no devuelve dosis.',
    )
    observaciones = fields.Text(
        help='Notas internas sobre el producto (no aparecen en el PDF).',
    )
    prioridad = fields.Integer(
        default=10,
        help='Cuando hay varias reglas activas para el mismo producto, '
             'gana la de mayor prioridad.',
    )
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        help='Si vacía, la regla aplica a todas las compañías.',
    )

    _sql_constraints = [
        (
            'duracion_positive',
            'CHECK (duracion_paquete_dias > 0)',
            'La duración del paquete debe ser mayor a 0 días.',
        ),
        (
            'cantidad_manual_positive',
            'CHECK (cantidad_mensual_manual > 0)',
            'La cantidad mensual manual debe ser mayor a 0.',
        ),
    ]

    @api.depends('duracion_paquete_dias', 'cantidad_mensual_manual',
                 'usar_cantidad_manual')
    def _compute_cantidad_mensual(self):
        for rec in self:
            if rec.usar_cantidad_manual:
                rec.cantidad_mensual = rec.cantidad_mensual_manual or 1.0
            elif rec.duracion_paquete_dias and rec.duracion_paquete_dias > 0:
                rec.cantidad_mensual = float(
                    math.ceil(30.0 / rec.duracion_paquete_dias)
                )
            else:
                rec.cantidad_mensual = 1.0

    @api.constrains('product_id', 'company_id', 'active')
    def _check_unique_active_per_product(self):
        """Solo puede haber una regla activa por (product_id, company_id)."""
        for rec in self:
            if not rec.active:
                continue
            domain = [
                ('product_id', '=', rec.product_id.id),
                ('active', '=', True),
                ('id', '!=', rec.id),
            ]
            if rec.company_id:
                domain += ['|',
                           ('company_id', '=', rec.company_id.id),
                           ('company_id', '=', False)]
            else:
                domain.append(('company_id', '=', False))
            others = self.search(domain, limit=1)
            if others:
                raise ValidationError(_(
                    "Ya existe otra regla activa para el producto '%s'%s. "
                    "Desactiva la otra o ajusta la prioridad."
                ) % (
                    rec.product_id.display_name,
                    " en la compañía " + rec.company_id.name if rec.company_id else "",
                ))

    # ====================================================================
    # API pública usada por el orquestador y por valoracion.linea.producto
    # ====================================================================
    @api.model
    def _get_regla_for_product(self, product_template):
        """Devuelve la regla activa más prioritaria para un product.template,
        respetando company_id (regla de la compañía actual o global).

        Args:
            product_template: recordset de product.template (1 registro).

        Returns:
            recordset de valoracion.producto.regla (vacío o 1 registro).
        """
        if not product_template:
            return self.browse()
        company_id = self.env.company.id
        return self.search([
            ('product_id', '=', product_template.id),
            ('active', '=', True),
            '|',
            ('company_id', '=', False),
            ('company_id', '=', company_id),
        ], order='prioridad desc, id', limit=1)

    @api.model
    def _get_cantidad_mensual_for_product(self, product_template):
        """Devuelve cantidad mensual aplicable para un product.template.

        Si no existe regla activa, devuelve 1.0 (default).
        """
        regla = self._get_regla_for_product(product_template)
        if not regla:
            return 1.0
        return regla.cantidad_mensual or 1.0
