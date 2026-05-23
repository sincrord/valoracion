# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProductTemplate(models.Model):
    """Extiende product.template para identificar productos del catálogo
    VitalHealth.

    Un producto se considera "VitalHealth" si CUALQUIERA de:
      * is_vitalhealth = True (flag manual), O
      * pertenece a la categoría 'VitalHealth' o a un descendiente de ella.

    El campo computed `is_vitalhealth_effective` aplica esta lógica OR y se
    usa tanto en domains de selección como en la búsqueda de la IA. Tiene
    `search='_search_is_vitalhealth_effective'` así que se puede usar en
    domains aunque no esté almacenado.
    """
    _inherit = 'product.template'

    is_vitalhealth = fields.Boolean(
        string='Es producto VitalHealth',
        default=False,
        index=True,
        copy=True,
        help='Marca manual: este producto es parte del catálogo VitalHealth. '
             'Esto NO es obligatorio: si el producto pertenece a la categoría '
             '"VitalHealth" también se considera válido aunque este flag esté '
             'en False.',
    )

    is_vitalhealth_effective = fields.Boolean(
        string='Es VitalHealth (efectivo)',
        compute='_compute_is_vitalhealth_effective',
        search='_search_is_vitalhealth_effective',
        store=False,
        help='Verdadero si is_vitalhealth=True O el producto pertenece a la '
             'categoría VitalHealth (o un descendiente). Usado por la IA y '
             'por valoracion.linea.producto para validar productos elegibles.',
    )

    # ====================================================================
    # Helpers de categoría
    # ====================================================================
    @api.model
    def _vh_category_descendant_ids(self):
        """Devuelve la lista de IDs de la categoría VitalHealth y sus
        descendientes. Lista vacía si la categoría no existe."""
        cat = self.env.ref(
            'valoracion.product_category_vitalhealth',
            raise_if_not_found=False,
        )
        if not cat:
            return []
        return self.env['product.category'].search(
            [('id', 'child_of', cat.id)]
        ).ids

    def _is_vitalhealth_category(self, category):
        """Verdadero si la categoría dada está dentro del árbol VitalHealth."""
        if not category:
            return False
        descendant_ids = self._vh_category_descendant_ids()
        return category.id in descendant_ids if descendant_ids else False

    # ====================================================================
    # Compute / Search del campo "efectivo"
    # ====================================================================
    @api.depends('is_vitalhealth', 'categ_id')
    def _compute_is_vitalhealth_effective(self):
        descendant_set = set(self._vh_category_descendant_ids())
        for rec in self:
            if rec.is_vitalhealth:
                rec.is_vitalhealth_effective = True
            elif rec.categ_id and rec.categ_id.id in descendant_set:
                rec.is_vitalhealth_effective = True
            else:
                rec.is_vitalhealth_effective = False

    @api.model
    def _search_is_vitalhealth_effective(self, operator, value):
        """Permite usar el campo en domains a pesar de no estar almacenado.

        Mapea (operator, value) a un domain real basado en is_vitalhealth y
        categ_id, usando OR para combinar las dos condiciones.
        """
        descendant_ids = self._vh_category_descendant_ids()

        # Caso "is_vitalhealth_effective truthy"
        truthy = (
            (operator == '=' and value is True) or
            (operator == '!=' and value is False) or
            (operator == 'in' and True in (value or [])) or
            (operator == '=' and value == 1)
        )
        if truthy:
            if descendant_ids:
                return [
                    '|',
                    ('is_vitalhealth', '=', True),
                    ('categ_id', 'in', descendant_ids),
                ]
            return [('is_vitalhealth', '=', True)]

        # Caso "is_vitalhealth_effective falsy"
        falsy = (
            (operator == '=' and value is False) or
            (operator == '!=' and value is True) or
            (operator == 'in' and False in (value or [])) or
            (operator == '=' and value == 0)
        )
        if falsy:
            if descendant_ids:
                return [
                    '&',
                    ('is_vitalhealth', '!=', True),
                    ('categ_id', 'not in', descendant_ids),
                ]
            return [('is_vitalhealth', '!=', True)]

        return []

    # ====================================================================
    # Auto-marcado por categoría (sugerencia, no obligatorio)
    # ====================================================================
    @api.onchange('categ_id')
    def _onchange_categ_id_vitalhealth(self):
        """Si el usuario asigna la categoría VitalHealth, sugiere marcar
        is_vitalhealth=True para hacer el flag explícito. NO revierte a False
        si el usuario cambia de categoría."""
        if self.categ_id and self._is_vitalhealth_category(self.categ_id):
            self.is_vitalhealth = True

    @api.model_create_multi
    def create(self, vals_list):
        """En backend (importaciones, API), auto-marca si la categoría es
        VitalHealth y el campo is_vitalhealth no se especifica explícitamente.
        """
        descendant_ids = self._vh_category_descendant_ids()
        if descendant_ids:
            for vals in vals_list:
                if 'is_vitalhealth' not in vals and vals.get('categ_id'):
                    if vals['categ_id'] in descendant_ids:
                        vals['is_vitalhealth'] = True
        return super().create(vals_list)
