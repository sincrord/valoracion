# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class ValoracionFuente(models.Model):
    """Fuente autorizada que alimenta el contexto de la IA al generar valoraciones.

    Decisiones aplicadas:
    - Decisión 13: las fuentes son globales para todos los usuarios; solo
      Administrador y Super Usuario pueden administrarlas (ver ir.model.access.csv).
    - Decisión 11: se incluye company_id (multi-compañía preparado).
    - Riesgo 11 (mitigado): el campo 'sequence' define la prioridad para
      truncar fuentes cuando el prompt excede el límite de tokens.
    """
    _name = 'valoracion.fuente'
    _description = 'Fuente VitalHealth para contexto de IA'
    _order = 'sequence, id'

    name = fields.Char(string='Nombre', required=True)
    description = fields.Text(string='Descripción')
    active = fields.Boolean(string='Activa', default=True)

    tipo = fields.Selection(
        selection=[
            ('catalogo', 'Catálogo VitalHealth'),
            ('fichas_tecnicas', 'Fichas técnicas'),
            ('lista_precios', 'Lista de precios'),
            ('referencia', 'Documento de referencia'),
            ('otro', 'Otro'),
        ],
        string='Tipo',
        default='catalogo',
        required=True,
    )

    sequence = fields.Integer(
        string='Secuencia',
        default=10,
        help='Orden de prioridad. Las fuentes con MENOR secuencia se incluyen '
             'primero al construir el contexto IA. Cuando el prompt excede '
             'el límite de tokens, se truncan/omiten las de mayor secuencia.',
    )

    attachment_ids = fields.Many2many(
        'ir.attachment',
        'valoracion_fuente_attachment_rel',
        'fuente_id',
        'attachment_id',
        string='Archivos',
        help='Archivos PDF, XLS, XLSX, CSV o TXT con contenido autorizado '
             'que la IA puede consultar (catálogo, fichas, listas de precios, etc.).',
    )

    extracted_text = fields.Text(
        string='Texto extraído (cache)',
        readonly=True,
        help='Cache concatenado del texto de todos los archivos. Se llena '
             'al generar la primera valoración que use esta fuente, o '
             'manualmente con el botón "Re-extraer".',
    )
    extracted_at = fields.Datetime(string='Última extracción', readonly=True)
    extraction_status = fields.Selection(
        selection=[
            ('pendiente', 'Pendiente'),
            ('ok', 'Extraído'),
            ('error', 'Error'),
        ],
        string='Estado de extracción',
        default='pendiente',
        readonly=True,
    )
    extraction_error = fields.Text(string='Error de extracción', readonly=True)

    total_size = fields.Integer(
        string='Tamaño total (bytes)',
        compute='_compute_total_size',
    )
    total_size_human = fields.Char(
        string='Tamaño',
        compute='_compute_total_size',
    )
    file_count = fields.Integer(
        string='Total archivos',
        compute='_compute_total_size',
    )

    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        help='Si vacía, la fuente es global y disponible en todas las compañías.',
    )

    # ====================================================================
    # Computeds
    # ====================================================================
    @api.depends('attachment_ids', 'attachment_ids.file_size')
    def _compute_total_size(self):
        for rec in self:
            total = sum((a.file_size or 0) for a in rec.attachment_ids)
            rec.total_size = total
            rec.file_count = len(rec.attachment_ids)
            if total < 1024:
                rec.total_size_human = "%d B" % total
            elif total < 1024 * 1024:
                rec.total_size_human = "%.1f KB" % (total / 1024.0)
            else:
                rec.total_size_human = "%.2f MB" % (total / (1024.0 * 1024.0))

    # ====================================================================
    # Cache de extracción
    # ====================================================================
    def _invalidate_extraction_cache(self):
        """Limpia el cache para forzar re-extracción en la siguiente generación."""
        for rec in self:
            super(ValoracionFuente, rec).write({
                'extracted_text': False,
                'extracted_at': False,
                'extraction_status': 'pendiente',
                'extraction_error': False,
            })

    def write(self, vals):
        """Si cambian los archivos, invalidar el cache de extracción."""
        invalidate = 'attachment_ids' in vals
        res = super().write(vals)
        if invalidate:
            for rec in self:
                if rec.extraction_status == 'ok':
                    rec._invalidate_extraction_cache()
        return res

    def action_reextraer(self):
        """Re-extrae el texto de todos los archivos de la fuente.

        Llama a FileExtractor (Etapa 3). Para imágenes, dado que las fuentes
        son contexto textual para la IA, las imágenes se ignoran (no se envían
        como multimodal aquí; eso es solo para archivos del cliente).
        """
        # Lazy import para evitar ciclos a la carga del módulo
        from .ia.file_extractor import FileExtractor

        for rec in self:
            chunks = []
            errores = []

            for att in rec.attachment_ids:
                if not att.datas:
                    errores.append(_("%s: sin datos") % (att.name or '?'))
                    continue

                # Detectar tipo por extensión
                ext_to_type = {
                    'pdf': 'pdf', 'xls': 'xls', 'xlsx': 'xlsx',
                    'csv': 'csv', 'txt': 'txt',
                }
                ext = ''
                if att.name and '.' in att.name:
                    ext = att.name.rsplit('.', 1)[-1].lower()
                ftype = ext_to_type.get(ext)

                if not ftype:
                    errores.append(_(
                        "%(n)s: tipo no soportado en fuentes (solo PDF/XLS/XLSX/CSV/TXT)"
                    ) % {'n': att.name or '?'})
                    continue

                result = FileExtractor.extract(att.datas, att.name, ftype)
                if result.get('success'):
                    chunks.append(
                        "=== %s ===\n%s" % (att.name, result.get('text', ''))
                    )
                else:
                    errores.append("%s: %s" % (att.name, result.get('error') or '?'))

            estado = 'ok' if chunks and not errores else (
                'error' if errores and not chunks else ('ok' if chunks else 'pendiente')
            )
            rec.write({
                'extracted_text': '\n\n'.join(chunks) if chunks else False,
                'extracted_at': fields.Datetime.now(),
                'extraction_status': estado,
                'extraction_error': '\n'.join(errores) if errores else False,
            })

            if errores:
                _logger.warning(
                    "Fuente '%s': errores de extracción: %s",
                    rec.name, errores,
                )

        return True
