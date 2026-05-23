# -*- coding: utf-8 -*-
import unicodedata

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


# ============================================================
# Excepciones de cantidad mensual de COMPRA
# ============================================================
# Reglas según observaciones.txt del catálogo VitalHealth:
#   * VITAL PRO     → un paquete dura 15 días → 2 paquetes/mes
#   * V-TE DETOX    → un paquete dura 7 días  → 4 paquetes/mes
#   * Cualquier otro→ 1 paquete/mes (independiente de la dosis diaria)
#
# La clave es el nombre del producto NORMALIZADO con _normalize_compact():
# lowercase + sin acentos + sin guiones/espacios/puntos.
# Esto hace robusto el matching contra variaciones como "VITAL PRO",
# "Vital-Pro", "vital pro" → todas mapean a "vitalpro".
# ============================================================
CANTIDAD_MENSUAL_EXCEPCIONES = {
    'vitalpro': 2.0,
    'vtedetox': 4.0,
}
CANTIDAD_MENSUAL_DEFAULT = 1.0


def _normalize_compact(value):
    """Normaliza un nombre para hacer matching robusto con las excepciones.

    Aplica: lowercase + quita acentos (NFKD) + quita guiones, espacios,
    underscore y puntos. Devuelve string vacío si entrada es falsy.

    Ejemplo:
        'VITAL PRO'     -> 'vitalpro'
        'Vital-Pro'     -> 'vitalpro'
        'V-TE DETOX'    -> 'vtedetox'
        'V Te Detox'    -> 'vtedetox'
        'V Té Detox'    -> 'vtedetox'
    """
    if not value:
        return ''
    s = unicodedata.normalize('NFKD', value)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    for ch in ('-', '_', ' ', '.', '\t'):
        s = s.replace(ch, '')
    return s


class ValoracionLineaProducto(models.Model):
    """Producto VitalHealth recomendado por la IA dentro de una valoración.

    Estructura paralela al campo HTML 'plan_estrategico': el HTML es la
    narrativa legible (que va al PDF), y este modelo es la representación
    estructurada que permite crear cotizaciones de venta automáticamente
    (action_crear_cotizacion en valoracion.valoracion).

    'product_id' es product.product (variante) y se filtra por
    is_vitalhealth=True a nivel template. La IA puede devolver un nombre que
    no coincida exactamente con un producto existente: en ese caso se guarda
    en 'product_name_ia' y match_status='no_encontrado'. El usuario puede
    asignar manualmente el producto correcto.
    """
    _name = 'valoracion.linea.producto'
    _description = 'Línea de Producto VitalHealth (Valoración)'
    _order = 'sequence, id'

    # ====================================================================
    # Identificación y relación
    # ====================================================================
    valoracion_id = fields.Many2one(
        'valoracion.valoracion',
        string='Valoración',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(string='Secuencia', default=10)

    # ====================================================================
    # Producto
    # ====================================================================
    product_id = fields.Many2one(
        'product.product',
        string='Producto VitalHealth',
        # Acepta productos con flag is_vitalhealth=True O productos en la
        # categoría VitalHealth (vía is_vitalhealth_effective). De esta forma,
        # un producto bien categorizado pero sin el flag manual sigue siendo
        # seleccionable.
        domain="[('product_tmpl_id.is_vitalhealth_effective', '=', True)]",
        ondelete='restrict',
        help='Productos del catálogo VitalHealth: aquellos con flag '
             'is_vitalhealth=True O en la categoría "VitalHealth" o '
             'descendientes.',
    )
    product_name_ia = fields.Char(
        string='Nombre devuelto por IA',
        readonly=True,
        help='Nombre exacto del producto tal como lo devolvió la IA. '
             'Útil para auditar coincidencias y detectar discrepancias.',
    )
    cantidad = fields.Float(
        string='Cantidad',
        default=1.0,
        digits=(12, 2),
        required=True,
        help='Cantidad de PAQUETES mensuales a comprar. Calculada por la '
             'regla del producto (valoracion.producto.regla) o 1 por default. '
             'NO confundir con la dosis diaria (que va en dosis_sugerida).',
    )
    razon = fields.Text(
        string='Razón de la recomendación',
        help='Justificación de la IA para incluir este producto en el plan.',
    )
    dosis_sugerida = fields.Text(
        string='Dosis sugerida',
        help='Dosis o forma de consumo informativa (ej: "2 cápsulas cada 12 horas"). '
             'NO afecta la cantidad de compra; solo es texto explicativo.',
    )

    # ====================================================================
    # Regla de producto aplicada
    # ====================================================================
    regla_producto_id = fields.Many2one(
        'valoracion.producto.regla',
        string='Regla aplicada',
        readonly=True,
        ondelete='set null',
        help='Regla del catálogo VitalHealth que se aplicó para calcular '
             'la cantidad mensual de paquetes.',
    )
    duracion_paquete_dias = fields.Integer(
        related='regla_producto_id.duracion_paquete_dias',
        readonly=True,
        string='Duración del paquete (días)',
    )
    cantidad_calculada_por_regla = fields.Boolean(
        string='Cantidad por regla',
        default=False,
        readonly=True,
        help='Verdadero si la cantidad se calculó usando una regla '
             'del modelo valoracion.producto.regla.',
    )

    # ====================================================================
    # Precios
    # ====================================================================
    company_id = fields.Many2one(
        related='valoracion_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency_id',
        store=False,
        readonly=True,
    )
    precio_unitario = fields.Monetary(
        string='Precio unitario',
        compute='_compute_precio_unitario',
        currency_field='currency_id',
        store=False,
        help='Precio sugerido tomado de la lista de precios del partner '
             'de la valoración (decisión E). Si no hay lista, se usa '
             'list_price del producto.',
    )
    subtotal = fields.Monetary(
        string='Subtotal',
        compute='_compute_subtotal',
        currency_field='currency_id',
        store=False,
    )

    # ====================================================================
    # Auditoría / matching
    # ====================================================================
    match_status = fields.Selection(
        selection=[
            ('exacto', 'Coincidencia exacta'),
            ('aproximado', 'Coincidencia aproximada'),
            ('no_encontrado', 'No encontrado'),
            ('manual', 'Asignado manualmente'),
        ],
        string='Estado de coincidencia',
        default='manual',
        readonly=True,
        help='Cómo se obtuvo la asignación de product_id:\n'
             ' * exacto: la IA devolvió un nombre que coincide exactamente.\n'
             ' * aproximado: coincidencia parcial; revisar.\n'
             ' * no_encontrado: la IA mencionó un producto inexistente.\n'
             ' * manual: el usuario lo asignó.',
    )

    # ====================================================================
    # Computeds
    # ====================================================================
    @api.depends('valoracion_id.company_id', 'valoracion_id.partner_id')
    def _compute_currency_id(self):
        for rec in self:
            partner = rec.valoracion_id.partner_id
            company = rec.valoracion_id.company_id or self.env.company
            pricelist = partner.property_product_pricelist if partner else False
            if pricelist and pricelist.currency_id:
                rec.currency_id = pricelist.currency_id
            else:
                rec.currency_id = company.currency_id

    @api.depends('product_id', 'valoracion_id.partner_id')
    def _compute_precio_unitario(self):
        """Calcula el precio según la lista del partner (decisión E).
        Si el partner no tiene lista, usa list_price del producto."""
        for rec in self:
            if not rec.product_id:
                rec.precio_unitario = 0.0
                continue
            partner = rec.valoracion_id.partner_id
            pricelist = partner.property_product_pricelist if partner else False
            if pricelist:
                # Odoo 18: pricelist._get_product_price devuelve el precio
                # con la moneda de la lista
                try:
                    price = pricelist._get_product_price(
                        rec.product_id,
                        rec.cantidad or 1.0,
                    )
                except Exception:
                    price = rec.product_id.list_price
                rec.precio_unitario = price
            else:
                rec.precio_unitario = rec.product_id.list_price

    @api.depends('precio_unitario', 'cantidad')
    def _compute_subtotal(self):
        for rec in self:
            rec.subtotal = (rec.precio_unitario or 0.0) * (rec.cantidad or 0.0)

    # ====================================================================
    # Onchange
    # ====================================================================
    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Cuando el usuario asigna manualmente un producto, marcar como manual."""
        if self.product_id and self.match_status == 'no_encontrado':
            self.match_status = 'manual'
            if not self.product_name_ia:
                self.product_name_ia = self.product_id.display_name

    # ====================================================================
    # Constraints
    # ====================================================================
    _sql_constraints = [
        (
            'cantidad_positiva',
            'CHECK (cantidad > 0)',
            'La cantidad debe ser mayor a cero.',
        ),
    ]

    @api.constrains('product_id')
    def _check_product_is_vitalhealth(self):
        """Refuerzo de seguridad: solo productos VitalHealth pueden estar aquí.
        Acepta productos con flag is_vitalhealth=True O en la categoría
        VitalHealth (vía is_vitalhealth_effective)."""
        for rec in self:
            if rec.product_id and not rec.product_id.product_tmpl_id.is_vitalhealth_effective:
                raise ValidationError(_(
                    "El producto '%s' no pertenece al catálogo VitalHealth. "
                    "Solo se permiten productos con flag 'is_vitalhealth' "
                    "marcado o pertenecientes a la categoría VitalHealth."
                ) % rec.product_id.display_name)

    # ====================================================================
    # Helper: cantidad mensual de COMPRA según producto
    # ====================================================================
    @api.model
    def _get_cantidad_mensual_producto(self, product):
        """Devuelve la cantidad mensual de COMPRA (paquetes a comprar) para
        el producto, ignorando la dosis diaria sugerida por la IA.

        Estrategia de búsqueda:
          1. Consulta valoracion.producto.regla activa para el product.template.
             Si existe, devuelve regla.cantidad_mensual.
          2. Fallback hardcoded por nombre normalizado (compatibilidad legacy
             con instalaciones que aún no han poblado el modelo de reglas):
                * VITAL PRO    → 2 paquetes/mes
                * V-TE DETOX   → 4 paquetes/mes
          3. Default: 1 paquete/mes.

        Args:
            product: recordset de product.product (1 registro) o False.

        Returns:
            float — cantidad de paquetes a comprar al mes.
        """
        if not product:
            return CANTIDAD_MENSUAL_DEFAULT

        # 1. Buscar regla configurada en valoracion.producto.regla (oficial)
        Regla = self.env['valoracion.producto.regla']
        tmpl = product.product_tmpl_id
        if tmpl:
            regla = Regla._get_regla_for_product(tmpl)
            if regla:
                return regla.cantidad_mensual or CANTIDAD_MENSUAL_DEFAULT

        # 2. Fallback legacy por nombre normalizado
        candidates = []
        try:
            candidates.append(_normalize_compact(product.display_name or ''))
        except Exception:
            pass
        try:
            candidates.append(_normalize_compact(product.name or ''))
        except Exception:
            pass
        try:
            candidates.append(_normalize_compact(product.default_code or ''))
        except Exception:
            pass
        try:
            if tmpl:
                candidates.append(_normalize_compact(tmpl.name or ''))
        except Exception:
            pass

        for normalized in candidates:
            if not normalized:
                continue
            for key, qty in CANTIDAD_MENSUAL_EXCEPCIONES.items():
                if key in normalized:
                    return qty

        return CANTIDAD_MENSUAL_DEFAULT
