# -*- coding: utf-8 -*-
"""Mapeo de etiquetas de estado encontradas en reportes BioCuánticos
hacia una taxonomía normalizada interna.

Distintos reportes usan etiquetas variables ("Normal", "Levemente alterado",
"Leve", "Moderado", "Critico", "Bajo", "Alto", "Equilibrado"...). Para que
el resumen maestro (Fase 2.2) sea estable, traducimos esas etiquetas a una
escala normalizada con severidad numérica.

Fase 2.1: solo catálogo. El parser real lo consumirá en Fase 2.2.
"""
from odoo import api, fields, models, _


SEVERITY_MAP = {
    'optimo': 0,
    'normal': 1,
    'leve': 2,
    'moderado': 3,
    'severo': 4,
    'critico': 5,
    'desconocido': 9,
}


class ValoracionBioCuanticoEstadoMapeo(models.Model):
    _name = 'valoracion.biocuantico.estado.mapeo'
    _description = 'Mapeo de Estado BioCuántico'
    _order = 'severity, raw_label'
    _rec_name = 'raw_label'

    raw_label = fields.Char(
        string='Etiqueta en el reporte',
        required=True,
        help='Texto tal cual aparece en el reporte BioCuántico (se compara '
             'normalizado: minúsculas y sin acentos).',
    )
    normalized = fields.Selection(
        selection=[
            ('optimo', 'Óptimo'),
            ('normal', 'Normal'),
            ('leve', 'Levemente alterado'),
            ('moderado', 'Moderadamente alterado'),
            ('severo', 'Severamente alterado'),
            ('critico', 'Crítico'),
            ('desconocido', 'Desconocido'),
        ],
        string='Estado normalizado',
        required=True,
        default='desconocido',
        help='Estado interno al que se mapea la etiqueta del reporte.',
    )
    severity = fields.Integer(
        string='Severidad',
        compute='_compute_severity',
        store=True,
        help='Nivel numérico para ordenar de menos a más severo. '
             '0 = óptimo, 5 = crítico, 9 = desconocido.',
    )
    sistema_id = fields.Many2one(
        'valoracion.biocuantico.sistema',
        string='Sistema corporal',
        ondelete='set null',
        help='Si se indica, el mapeo solo aplica para este sistema. '
             'Si está vacío, aplica de forma global.',
    )
    active = fields.Boolean(string='Activo', default=True)
    notes = fields.Text(string='Notas')

    _sql_constraints = [
        (
            'raw_label_sistema_uniq',
            'UNIQUE(raw_label, sistema_id)',
            'Ya existe un mapeo con esta etiqueta para el mismo sistema.',
        ),
    ]

    @api.depends('normalized')
    def _compute_severity(self):
        for rec in self:
            rec.severity = SEVERITY_MAP.get(rec.normalized, 9)

    # =================================================================
    # API pública (será usada por el parser real en Fase 2.2)
    # =================================================================
    @api.model
    def _normalize_label(self, raw):
        """Normaliza una etiqueta para comparación (lower + sin acentos)."""
        import unicodedata
        if not raw:
            return ''
        text = raw.strip().lower()
        nfkd = unicodedata.normalize('NFKD', text)
        return ''.join(c for c in nfkd if not unicodedata.combining(c))

    @api.model
    def lookup(self, raw_label, sistema_code=None):
        """Devuelve el estado normalizado para una etiqueta dada.

        Args:
            raw_label: etiqueta del reporte.
            sistema_code: código del sistema (opcional). Si se indica, se
                preferirá un mapeo específico al sistema; si no hay, cae al
                mapeo global.

        Returns:
            ('estado_normalizado', severity_int) o ('desconocido', 9) si no
            se encuentra mapeo activo.
        """
        if not raw_label:
            return ('desconocido', SEVERITY_MAP['desconocido'])
        target = self._normalize_label(raw_label)

        candidates = self.search([('active', '=', True)])
        # Comparación normalizada
        best = None
        for c in candidates:
            if self._normalize_label(c.raw_label) != target:
                continue
            # Preferir mapeos específicos del sistema si coincide
            if sistema_code and c.sistema_id and c.sistema_id.code == sistema_code:
                return (c.normalized, c.severity)
            if not c.sistema_id and best is None:
                best = c
        if best:
            return (best.normalized, best.severity)
        return ('desconocido', SEVERITY_MAP['desconocido'])
