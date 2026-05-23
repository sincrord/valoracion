# -*- coding: utf-8 -*-
"""Sistema corporal BioCuántico (configurable por Admin).

Cada registro representa un sistema o eje fisiológico que aparece típicamente
en los reportes BioCuánticos (cardiovascular, digestivo, endocrino, etc.).

En Fase 2.1 este modelo se usa SOLO como catálogo configurable. El parser
real (Fase 2.2) usará `keywords` para mapear filas/secciones del reporte al
sistema correspondiente.
"""
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ValoracionBioCuanticoSistema(models.Model):
    _name = 'valoracion.biocuantico.sistema'
    _description = 'Sistema Corporal BioCuántico'
    _order = 'sequence, name'

    name = fields.Char(
        string='Nombre',
        required=True,
        help='Nombre legible del sistema corporal. Ej: "Sistema Cardiovascular".',
    )
    code = fields.Char(
        string='Código',
        required=True,
        help='Código interno único (snake_case). Ej: "cardiovascular".',
    )
    description = fields.Text(
        string='Descripción',
        help='Descripción funcional del sistema (uso interno y referencia para Admin).',
    )
    sequence = fields.Integer(
        string='Secuencia',
        default=10,
        help='Orden de aparición en el resumen maestro (Fase 2.2).',
    )
    active = fields.Boolean(string='Activo', default=True)

    keywords = fields.Text(
        string='Palabras clave',
        help='Palabras o frases (una por línea, o separadas por coma) que ayudan '
             'al parser a mapear filas/secciones del reporte BioCuántico a este '
             'sistema. Ej: "cardiovascular, corazón, presión arterial, ritmo".',
    )

    color = fields.Integer(
        string='Color',
        default=0,
        help='Color de la kanban (0-11). Solo presentación.',
    )

    _sql_constraints = [
        (
            'code_unique',
            'UNIQUE(code)',
            'El código del sistema BioCuántico debe ser único.',
        ),
    ]

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not rec.code:
                continue
            if not rec.code.replace('_', '').isalnum():
                raise ValidationError(_(
                    "El código '%s' debe contener solo letras, números y guiones bajos."
                ) % rec.code)

    def _get_keywords_list(self):
        """Devuelve la lista de palabras clave normalizadas (minúsculas, sin espacios extra).

        Acepta separación por comas o por líneas. Vacíos se descartan.
        """
        self.ensure_one()
        raw = self.keywords or ''
        # Aceptar tanto comas como saltos de línea
        items = []
        for chunk in raw.replace('\n', ',').split(','):
            kw = chunk.strip().lower()
            if kw:
                items.append(kw)
        return items
