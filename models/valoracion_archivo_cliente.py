# -*- coding: utf-8 -*-
import base64
import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


# Mapa de extensión -> tipo lógico
EXT_MAP = {
    'pdf': 'pdf',
    'xls': 'xls',
    'xlsx': 'xlsx',
    'csv': 'csv',
    'txt': 'txt',
    'png': 'imagen',
    'jpg': 'imagen',
    'jpeg': 'imagen',
    'gif': 'imagen',
    'webp': 'imagen',
    'bmp': 'imagen',
}


class ValoracionArchivoCliente(models.Model):
    """Archivos subidos por el cliente como insumo de la valoración.

    Cada archivo:
      * Se almacena vía Binary(attachment=True) (queda en filestore como
        ir.attachment, no infla la BD).
      * Tiene su tamaño calculado y validado contra el límite por archivo
        (parámetro 'valoracion.ia_max_archivo_mb', default 10 MB).
      * Tiene su tipo deducido por extensión y restringido a tipos permitidos.
      * Mantiene un cache de texto extraído ('extracted_text') que llena el
        FileExtractor (Etapa 3). Eso evita re-extraer en cada llamada IA.
    """
    _name = 'valoracion.archivo.cliente'
    _description = 'Archivo del Cliente (Valoración VitalHealth)'
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
    name = fields.Char(
        string='Nombre',
        help='Nombre legible del archivo. Por defecto = nombre del archivo subido.',
    )

    # ====================================================================
    # Archivo
    # ====================================================================
    file_data = fields.Binary(
        string='Archivo',
        attachment=True,
        required=True,
        help='El archivo se almacena en el filestore de Odoo (ir.attachment).',
    )
    file_name = fields.Char(
        string='Nombre del archivo',
        required=True,
    )
    file_size = fields.Integer(
        string='Tamaño (bytes)',
        compute='_compute_file_size',
        store=True,
        help='Tamaño calculado a partir del binario base64.',
    )
    file_size_human = fields.Char(
        string='Tamaño',
        compute='_compute_file_size_human',
    )
    file_type = fields.Selection(
        selection=[
            ('pdf', 'PDF'),
            ('xlsx', 'Excel (XLSX)'),
            ('xls', 'Excel (XLS)'),
            ('csv', 'CSV'),
            ('txt', 'Texto'),
            ('imagen', 'Imagen'),
        ],
        string='Tipo',
        compute='_compute_file_type',
        store=True,
    )

    # ====================================================================
    # Extracción de texto (cache)
    # ====================================================================
    extracted_text = fields.Text(
        string='Texto extraído',
        readonly=True,
        help='Cache del texto extraído del archivo. Se llena automáticamente '
             'al generar la valoración (Etapa 3: FileExtractor).',
    )
    extracted_at = fields.Datetime(
        string='Fecha de extracción',
        readonly=True,
    )
    extraction_status = fields.Selection(
        selection=[
            ('pendiente', 'Pendiente'),
            ('ok', 'Extraído'),
            ('error', 'Error'),
            ('no_aplica', 'No aplica'),
        ],
        string='Estado de extracción',
        default='pendiente',
        readonly=True,
    )
    extraction_error = fields.Text(
        string='Error de extracción',
        readonly=True,
    )

    # ====================================================================
    # BioCuántico (Fase 2.1: solo detección — parser real en Fase 2.2)
    # ====================================================================
    biocuantico_detectado = fields.Boolean(
        string='Es reporte BioCuántico',
        readonly=True,
        help='Marcado automáticamente por el detector heurístico de Fase 2.1. '
             'No implica que el parser haya extraído mediciones.',
    )
    biocuantico_parse_status = fields.Selection(
        selection=[
            ('not_parsed', 'No analizado'),
            ('detected', 'BioCuántico detectado'),
            ('ok', 'Análisis BioCuántico exitoso'),
            ('partial', 'Análisis BioCuántico parcial'),
            ('not_biocuantico', 'No es BioCuántico'),
            ('failed', 'Falló el análisis'),
        ],
        string='Estado BioCuántico',
        default='not_parsed',
        readonly=True,
        help='Estado del análisis BioCuántico (Fase 2.2):\n'
             '* not_parsed: aún no se intentó.\n'
             '* ok: parser extrajo estructura útil (≥15 filas / ≥3 sistemas / ≥5 anormales).\n'
             '* partial: detectado como BC pero datos insuficientes; aun así se usa.\n'
             '* not_biocuantico: no parece reporte BioCuántico.\n'
             '* failed: error de parsing — la valoración cae a flujo heurístico.\n'
             '* detected: estado intermedio (compat. con Fase 2.1).',
    )
    biocuantico_summary_json = fields.Text(
        string='Resumen BioCuántico (JSON)',
        readonly=True,
        help='Cache del resumen maestro estructurado (Fase 2.2). '
             'En Fase 2.1 se deja vacío.',
    )
    biocuantico_summary_text = fields.Text(
        string='Resumen BioCuántico (texto)',
        readonly=True,
        help='Cache del resumen maestro en texto (Fase 2.2). '
             'En Fase 2.1 se deja vacío.',
    )
    biocuantico_parse_error = fields.Text(
        string='Error BioCuántico',
        readonly=True,
    )
    biocuantico_parsed_at = fields.Datetime(
        string='Fecha de análisis BioCuántico',
        readonly=True,
    )
    # ---- Metadata del motor (Fase 3.8: toggle controlado) ----
    biocuantico_motor_version = fields.Selection(
        selection=[
            ('legacy', 'Legacy'),
            ('v3', 'Motor v3'),
        ],
        string='Motor BioCuántico usado',
        readonly=True,
        help='Indica qué motor produjo el resumen actualmente cacheado. '
             'Se setea en cada corrida de _run_biocuantico_parser.',
    )
    biocuantico_parser_version = fields.Char(
        string='Versión del parser',
        readonly=True,
        help='Versión semántica del parser que generó este cache '
             '(p.ej. "3.5.0" para parser_v3).',
    )
    biocuantico_fallback_used = fields.Boolean(
        string='Fallback automático a legacy',
        readonly=True,
        help='True si el motor configurado era v3 pero falló y se cayó a '
             'legacy para no romper la valoración. El motivo queda en '
             'biocuantico_parse_error.',
    )
    biocuantico_motor_meta_json = fields.Text(
        string='Metadata motor (JSON)',
        readonly=True,
        help='Metadata adicional del motor (filas procesadas, descartados, '
             'prioridades generadas, chars del resumen). Solo lectura/log.',
    )

    # ====================================================================
    # Computeds
    # ====================================================================
    @api.depends('file_data')
    def _compute_file_size(self):
        for rec in self:
            if rec.file_data:
                try:
                    # file_data en lectura es bytes (base64-encoded)
                    data = rec.file_data
                    if isinstance(data, str):
                        data = data.encode('utf-8')
                    rec.file_size = len(base64.b64decode(data))
                except Exception:
                    _logger.warning(
                        "No se pudo calcular tamaño del archivo %s",
                        rec.file_name or '?',
                    )
                    rec.file_size = 0
            else:
                rec.file_size = 0

    @api.depends('file_size')
    def _compute_file_size_human(self):
        for rec in self:
            size = rec.file_size or 0
            if size < 1024:
                rec.file_size_human = "%d B" % size
            elif size < 1024 * 1024:
                rec.file_size_human = "%.1f KB" % (size / 1024.0)
            else:
                rec.file_size_human = "%.2f MB" % (size / (1024.0 * 1024.0))

    @api.depends('file_name')
    def _compute_file_type(self):
        for rec in self:
            if rec.file_name and '.' in rec.file_name:
                ext = rec.file_name.rsplit('.', 1)[-1].lower()
                rec.file_type = EXT_MAP.get(ext, False)
            else:
                rec.file_type = False

    # ====================================================================
    # Onchange / create defaults
    # ====================================================================
    @api.onchange('file_name')
    def _onchange_file_name(self):
        if self.file_name and not self.name:
            self.name = self.file_name

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') and vals.get('file_name'):
                vals['name'] = vals['file_name']
        return super().create(vals_list)

    # ====================================================================
    # Constraints
    # ====================================================================
    @api.constrains('file_name', 'file_type')
    def _check_file_type_allowed(self):
        for rec in self:
            if rec.file_name and not rec.file_type:
                raise ValidationError(_(
                    "El archivo '%s' tiene un tipo no permitido. "
                    "Tipos válidos: PDF, XLS, XLSX, CSV, TXT, "
                    "imágenes (PNG, JPG, JPEG, GIF, WEBP, BMP)."
                ) % rec.file_name)

    @api.constrains('file_data', 'file_size')
    def _check_file_size_limit(self):
        Param = self.env['ir.config_parameter'].sudo()
        max_mb = int(Param.get_param('valoracion.ia_max_archivo_mb', '10') or 10)
        limit_bytes = max_mb * 1024 * 1024
        for rec in self:
            if rec.file_size and rec.file_size > limit_bytes:
                raise ValidationError(_(
                    "El archivo '%(name)s' supera el límite de %(mb)d MB "
                    "(tamaño actual: %(size)s)."
                ) % {
                    'name': rec.name or rec.file_name or '?',
                    'mb': max_mb,
                    'size': rec.file_size_human,
                })

    # ====================================================================
    # Acciones
    # ====================================================================
    def action_extraer_texto(self):
        """Extrae el texto del archivo y lo guarda en extracted_text.

        Llama al FileExtractor (lazy import para evitar ciclos a la carga
        del módulo). Para imágenes, marca como 'ok' con un placeholder ya
        que el contenido se enviará como input multimodal en la generación
        IA, no como texto.
        """
        from .ia.file_extractor import FileExtractor

        for rec in self:
            if not rec.file_data or not rec.file_type:
                rec.write({
                    'extraction_status': 'error',
                    'extraction_error': _("Sin archivo o tipo no detectado."),
                    'extracted_at': fields.Datetime.now(),
                })
                continue

            result = FileExtractor.extract(
                file_data_b64=rec.file_data,
                file_name=rec.file_name,
                file_type=rec.file_type,
            )
            if result.get('success'):
                if rec.file_type == 'imagen':
                    text = _("[Imagen — se enviará como input multimodal: %s]") % (rec.file_name or '')
                else:
                    text = result.get('text') or ''
                rec.write({
                    'extraction_status': 'ok',
                    'extracted_text': text,
                    'extracted_at': fields.Datetime.now(),
                    'extraction_error': False,
                })
            else:
                rec.write({
                    'extraction_status': 'error',
                    'extraction_error': result.get('error') or _('Error desconocido'),
                    'extracted_at': fields.Datetime.now(),
                })
        return True

    def _invalidate_extraction_cache(self):
        """Limpia el cache de extracción. Se llama cuando file_data cambia."""
        self.write({
            'extracted_text': False,
            'extracted_at': False,
            'extraction_status': 'pendiente',
            'extraction_error': False,
        })

    def _get_biocuantico_motor_version(self):
        """Lee el setting `valoracion.biocuantico_motor_version`.

        Returns 'legacy' (default) o 'v3'. Cualquier valor inválido se
        normaliza a 'legacy' por seguridad — el toggle NUNCA debe romper
        la valoración."""
        try:
            val = (self.env['ir.config_parameter'].sudo()
                   .get_param('valoracion.biocuantico_motor_version',
                              'legacy') or 'legacy')
        except Exception:
            _logger.exception(
                "BioCuántico: no se pudo leer setting motor_version — "
                "usando legacy")
            return 'legacy'
        return val if val in ('legacy', 'v3') else 'legacy'

    def _invalidate_biocuantico_cache(self):
        """Limpia el cache BioCuántico (Fase 2.1+)."""
        super(ValoracionArchivoCliente, self).write({
            'biocuantico_detectado': False,
            'biocuantico_summary_json': False,
            'biocuantico_summary_text': False,
            'biocuantico_parse_status': 'not_parsed',
            'biocuantico_parse_error': False,
            'biocuantico_parsed_at': False,
            # Metadata motor (Fase 3.8)
            'biocuantico_motor_version': False,
            'biocuantico_parser_version': False,
            'biocuantico_fallback_used': False,
            'biocuantico_motor_meta_json': False,
        })

    def write(self, vals):
        # Si se reemplaza el archivo, invalidar cache de extracción y BioCuántico
        if 'file_data' in vals or 'file_name' in vals:
            res = super().write(vals)
            for rec in self:
                if rec.extraction_status == 'ok':
                    super(ValoracionArchivoCliente, rec).write({
                        'extracted_text': False,
                        'extracted_at': False,
                        'extraction_status': 'pendiente',
                        'extraction_error': False,
                    })
                if rec.biocuantico_parse_status != 'not_parsed':
                    rec._invalidate_biocuantico_cache()
            return res
        return super().write(vals)

    # ====================================================================
    # BioCuántico — parser real (Fase 2.2)
    # ====================================================================
    def action_detectar_biocuantico(self):
        """Botón Admin: corre el parser BioCuántico completo y guarda resumen.

        Fase 2.2:
          1. Asegura texto extraído (si aplica).
          2. Pasa el binario al BioCuanticoParser (rutas pdfplumber + textual).
          3. Si parser devuelve ok / partial → master_summary se persiste.
          4. Si devuelve not_biocuantico / failed → no se persiste resumen
             (el flujo IA caerá al heurístico).

        NUNCA levanta: ante cualquier excepción inesperada, status='failed'
        para que la generación IA siga funcionando.
        """
        for rec in self:
            rec._run_biocuantico_parser(force=True)
        return True

    def _run_biocuantico_parser(self, force=False):
        """Dispatcher Fase 3.8 — toggle motor BioCuántico con fallback.

        Lee `valoracion.biocuantico_motor_version` (legacy/v3, default legacy).
        Si el setting es v3, intenta el pipeline nuevo; si falla por cualquier
        razón hace fallback automático a legacy y registra el motivo en
        `biocuantico_parse_error` — la valoración NUNCA se rompe por el motor.

        Args:
            force: si False, no re-corre cuando ya hay status estable.
        Returns:
            dict del parser, o None si se saltó por cache válido.
        """
        self.ensure_one()

        # Skip si ya tenemos resultado estable y no estamos forzando
        if not force and self.biocuantico_parse_status in (
            'ok', 'partial', 'failed', 'not_biocuantico',
        ):
            return None

        motor = self._get_biocuantico_motor_version()
        if motor == 'v3':
            try:
                payload = self._run_biocuantico_parser_v3()
                if payload is not None:
                    return payload
                # v3 devolvió None sin excepción (caso edge) → fallback
                _logger.warning(
                    "BioCuántico v3 devolvió None (archivo %s) — "
                    "fallback a legacy", self.id,
                )
            except Exception as e:
                _logger.exception(
                    "BioCuántico v3 falló (archivo %s) — fallback a legacy",
                    self.id,
                )
                # Marca fallback antes de delegar al legacy
                try:
                    self.sudo().write({
                        'biocuantico_fallback_used': True,
                        'biocuantico_parse_error': _(
                            "Motor v3 falló, se usó legacy: %s"
                        ) % e,
                    })
                except Exception:  # pragma: no cover — defensivo
                    pass

        return self._run_biocuantico_parser_legacy(force=force)

    def _run_biocuantico_parser_legacy(self, force=False):
        """Implementación legacy (Fase 2.2). Conservada intacta como
        baseline estable y como destino del fallback v3→legacy."""
        from .biocuantico.parser import BioCuanticoParser
        from .biocuantico.master_summary import BioCuanticoMasterSummary

        self.ensure_one()
        # Fase 3.8: marca temprana del motor en uso. Si más tarde el legacy
        # escribe summary_json/text, este flag persiste como motor_version.
        # Si el legacy es invocado tras un fallback v3→legacy, conservamos
        # biocuantico_fallback_used=True (no se sobrescribe aquí).
        try:
            self.sudo().write({'biocuantico_motor_version': 'legacy'})
        except Exception:  # pragma: no cover — defensivo
            pass

        # Skip si ya tenemos resultado estable y no estamos forzando
        if not force and self.biocuantico_parse_status in (
            'ok', 'partial', 'failed', 'not_biocuantico',
        ):
            return None

        # Imágenes no son procesables sin OCR — no es BC desde nuestro lado
        if self.file_type == 'imagen':
            self.sudo().write({
                'biocuantico_detectado': False,
                'biocuantico_parse_status': 'not_biocuantico',
                'biocuantico_summary_json': False,
                'biocuantico_summary_text': False,
                'biocuantico_parse_error': _(
                    "Las imágenes no se procesan localmente "
                    "(se envían como input multimodal a la IA)."
                ),
                'biocuantico_parsed_at': fields.Datetime.now(),
            })
            return None

        # Asegurar extracción de texto (especialmente para no-PDF)
        if not self.extracted_text and self.file_type:
            try:
                self.action_extraer_texto()
            except Exception as e:  # pragma: no cover — defensivo
                _logger.exception(
                    "BioCuántico: extracción previa falló (archivo %s)", self.id,
                )
                self.sudo().write({
                    'biocuantico_parse_status': 'failed',
                    'biocuantico_parse_error': _("Error al extraer texto: %s") % e,
                    'biocuantico_parsed_at': fields.Datetime.now(),
                })
                return None

        text = self.extracted_text or ''
        pdf_b64 = self.file_data if self.file_type == 'pdf' else None

        try:
            payload = BioCuanticoParser.parse(
                text=text,
                pdf_b64=pdf_b64,
                env=self.env,
            )
        except Exception as e:  # pragma: no cover — el parser ya captura
            _logger.exception(
                "BioCuántico: parser lanzó excepción no controlada "
                "(archivo %s)", self.id,
            )
            self.sudo().write({
                'biocuantico_parse_status': 'failed',
                'biocuantico_parse_error': _("Error inesperado: %s") % e,
                'biocuantico_parsed_at': fields.Datetime.now(),
            })
            return None

        # Decisión de persistencia según status del parser
        status = payload.get('status')
        if status == 'not_biocuantico':
            self.sudo().write({
                'biocuantico_detectado': False,
                'biocuantico_parse_status': 'not_biocuantico',
                'biocuantico_summary_json': False,
                'biocuantico_summary_text': False,
                'biocuantico_parse_error': False,
                'biocuantico_parsed_at': fields.Datetime.now(),
            })
            return payload

        if status == 'failed':
            self.sudo().write({
                'biocuantico_detectado': bool(payload.get('is_biocuantico')),
                'biocuantico_parse_status': 'failed',
                'biocuantico_summary_json': False,
                'biocuantico_summary_text': False,
                'biocuantico_parse_error': payload.get('error') or _(
                    "El parser no pudo construir estructura mínima."
                ),
                'biocuantico_parsed_at': fields.Datetime.now(),
            })
            return payload

        # status ∈ {ok, partial} → construir resumen maestro y persistir.
        # Pasamos el texto del antecedente del cliente para que el ranking de
        # prioridades aplique boost a los sistemas alineados con el caso
        # (Fase 2.4: hipotiroidismo→endocrino, lumbar→musculoesquelético,
        # detox/metales→toxicidad, etc.).
        val = self.valoracion_id
        antecedente_text = '\n'.join(filter(None, [
            val.partner_padecimientos or '',
            val.partner_medicamentos or '',
            val.partner_objetivo or '',
            val.partner_alergias or '',
            val.partner_suplementos or '',
        ])) if val else ''
        master = BioCuanticoMasterSummary.build(
            payload, env=self.env, antecedente_text=antecedente_text,
        )
        if not master.get('available'):
            # Caso defensivo: parser dijo ok/partial pero summary no se pudo
            # construir → degradar a failed para mantener consistencia.
            self.sudo().write({
                'biocuantico_detectado': True,
                'biocuantico_parse_status': 'failed',
                'biocuantico_summary_json': False,
                'biocuantico_summary_text': False,
                'biocuantico_parse_error': _(
                    "Master summary no disponible: %s"
                ) % (master.get('reason') or '?'),
                'biocuantico_parsed_at': fields.Datetime.now(),
            })
            return payload

        try:
            import json as _json
            json_dump = _json.dumps(master['json'], ensure_ascii=False)
        except Exception as e:
            _logger.exception("BioCuántico: serializando JSON falló (%s)", e)
            self.sudo().write({
                'biocuantico_detectado': True,
                'biocuantico_parse_status': 'failed',
                'biocuantico_summary_json': False,
                'biocuantico_summary_text': False,
                'biocuantico_parse_error': _("Error serializando JSON: %s") % e,
                'biocuantico_parsed_at': fields.Datetime.now(),
            })
            return payload

        self.sudo().write({
            'biocuantico_detectado': True,
            'biocuantico_parse_status': status,
            'biocuantico_summary_json': json_dump,
            'biocuantico_summary_text': master.get('text') or False,
            'biocuantico_parse_error': False,
            'biocuantico_parsed_at': fields.Datetime.now(),
        })

        _logger.info(
            "BioCuántico parser | archivo=%s status=%s sistemas=%d "
            "filas_utiles=%d sev/mod/lev=%d/%d/%d método=%s",
            self.name or self.file_name or self.id,
            status,
            len(payload.get('sistemas') or []),
            (payload.get('estadisticas') or {}).get('filas_utiles', 0),
            (payload.get('estadisticas') or {}).get('hallazgos_severos', 0),
            (payload.get('estadisticas') or {}).get('hallazgos_moderados', 0),
            (payload.get('estadisticas') or {}).get('hallazgos_leves', 0),
            payload.get('metodo'),
        )
        return payload

    # ====================================================================
    # BioCuántico — Motor v3 (Fase 3.8: integración controlada con fallback)
    # ====================================================================
    def _run_biocuantico_parser_v3(self):
        """Pipeline del motor v3 (parser_v3 + master_summary_v3).

        Política:
          * No procesa imágenes (igual que legacy).
          * Si el setting es v3 pero el archivo no es PDF, devuelve None
            para que el dispatcher haga fallback ordenado al legacy.
          * Detección de BioCuántico se delega al heurístico legacy
            (`BioCuanticoParser.is_biocuantico`) para no introducir un
            detector paralelo.
          * Si v3 corre pero NO produce hallazgos clínicos, devuelve None
            (fallback al legacy) — el motor v3 sólo "gana" cuando aporta
            estructura útil.
          * Si v3 produce prioridades, persiste summary_json/text con la
            metadata del motor.

        Returns:
            dict-payload con shape mínima compatible con el resto del
            flujo, o None para activar fallback automático.

        Lanza:
            Cualquier excepción se propaga al dispatcher, que la
            convertirá en fallback a legacy + log.
        """
        import json as _json

        from .biocuantico.parser import BioCuanticoParser
        from .biocuantico.parser_v3 import parse_v3, PARSER_V3_VERSION
        from .biocuantico.master_summary_v3 import (
            summarize, MASTER_SUMMARY_V3_VERSION, DEFAULT_MAX_CHARS,
        )

        self.ensure_one()

        # 1. Tipos no soportados por v3 (sin extractor tabular textual aún)
        if self.file_type == 'imagen':
            self.sudo().write({
                'biocuantico_detectado': False,
                'biocuantico_parse_status': 'not_biocuantico',
                'biocuantico_summary_json': False,
                'biocuantico_summary_text': False,
                'biocuantico_parse_error': _(
                    "Las imágenes no se procesan localmente "
                    "(se envían como input multimodal a la IA)."
                ),
                'biocuantico_parsed_at': fields.Datetime.now(),
                'biocuantico_motor_version': 'v3',
                'biocuantico_parser_version': PARSER_V3_VERSION,
                'biocuantico_fallback_used': False,
            })
            return {'status': 'not_biocuantico', 'motor': 'v3'}

        if self.file_type != 'pdf':
            # v3 hoy sólo extrae tablas desde PDF. Otros formatos → fallback
            # ordenado al legacy (que sí maneja XLS/CSV/TXT).
            _logger.info(
                "BioCuántico v3 | archivo %s tipo=%s no soportado por v3, "
                "fallback a legacy", self.id, self.file_type,
            )
            return None

        # 2. Detección heurística (reusa la del legacy)
        text = self.extracted_text or ''
        if not text and self.file_type:
            try:
                self.action_extraer_texto()
                text = self.extracted_text or ''
            except Exception as e:  # pragma: no cover — defensivo
                _logger.exception(
                    "BioCuántico v3: extracción previa falló (archivo %s)",
                    self.id,
                )
                raise

        is_bc = False
        try:
            is_bc = bool(BioCuanticoParser.is_biocuantico(text or ''))
        except Exception:
            _logger.exception(
                "BioCuántico v3: is_biocuantico lanzó excepción — asumiendo "
                "false (archivo %s)", self.id,
            )

        if not is_bc:
            self.sudo().write({
                'biocuantico_detectado': False,
                'biocuantico_parse_status': 'not_biocuantico',
                'biocuantico_summary_json': False,
                'biocuantico_summary_text': False,
                'biocuantico_parse_error': False,
                'biocuantico_parsed_at': fields.Datetime.now(),
                'biocuantico_motor_version': 'v3',
                'biocuantico_parser_version': PARSER_V3_VERSION,
                'biocuantico_fallback_used': False,
            })
            return {'status': 'not_biocuantico', 'motor': 'v3'}

        # 3. Pipeline v3
        pdf_b64 = self.file_data
        val = self.valoracion_id
        antecedente_text = '\n'.join(filter(None, [
            val.partner_padecimientos or '',
            val.partner_medicamentos or '',
            val.partner_objetivo or '',
            val.partner_alergias or '',
            val.partner_suplementos or '',
        ])) if val else ''

        v3_out = parse_v3(
            pdf_b64=pdf_b64,
            antecedente_text=antecedente_text,
        )
        prioridades = v3_out.get('prioridades') or []
        anormales = v3_out.get('stats', {}).get('anormales', 0)

        # Si v3 no produjo nada útil → fallback (es BC según heurístico pero
        # v3 no encontró estructura). No marcamos failed: dejamos que legacy
        # lo intente.
        if not prioridades or anormales == 0:
            _logger.info(
                "BioCuántico v3 | archivo %s sin prioridades (anormales=%d) "
                "→ fallback a legacy", self.id, anormales,
            )
            return None

        # 4. Master summary v3
        summary = summarize(v3_out, max_chars=DEFAULT_MAX_CHARS)
        try:
            json_dump = _json.dumps(summary['json'], ensure_ascii=False,
                                    default=str)
        except Exception as e:
            _logger.exception(
                "BioCuántico v3: serializando JSON falló — fallback (%s)", e,
            )
            return None

        # 5. Decidir status (ok / partial) con el mismo umbral que legacy:
        # ≥3 sistemas afectados o ≥5 anormales → ok; si no → partial.
        sistemas_afectados = v3_out.get('stats', {}).get(
            'sistemas_afectados', 0)
        if sistemas_afectados >= 3 or anormales >= 5:
            status = 'ok'
        else:
            status = 'partial'

        motor_meta = {
            'motor_version': 'v3',
            'parser_version': PARSER_V3_VERSION,
            'master_summary_version': MASTER_SUMMARY_V3_VERSION,
            'generated_at': fields.Datetime.now().isoformat(),
            'fallback_used': bool(self.biocuantico_fallback_used),
            'filas_procesadas': v3_out.get('stats', {}).get('rows_raw', 0),
            'clasificadas': v3_out.get('stats', {}).get('classified', 0),
            'descartados': v3_out.get('stats', {}).get('descartados', 0),
            'sistemas_afectados': sistemas_afectados,
            'prioridades_generadas': len(prioridades),
            'anormales': anormales,
            'chars_resumen': len(summary.get('text') or ''),
            'boost_codes': v3_out.get('boost_codes') or [],
        }
        try:
            motor_meta_json = _json.dumps(motor_meta, ensure_ascii=False,
                                          default=str)
        except Exception:
            motor_meta_json = False

        self.sudo().write({
            'biocuantico_detectado': True,
            'biocuantico_parse_status': status,
            'biocuantico_summary_json': json_dump,
            'biocuantico_summary_text': summary.get('text') or False,
            'biocuantico_parse_error': False,
            'biocuantico_parsed_at': fields.Datetime.now(),
            'biocuantico_motor_version': 'v3',
            'biocuantico_parser_version': PARSER_V3_VERSION,
            # NO sobreescribir fallback_used si venía True (caso muy edge).
            'biocuantico_motor_meta_json': motor_meta_json,
        })

        _logger.info(
            "BioCuántico v3 | archivo=%s status=%s sistemas=%d "
            "prioridades=%d anormales=%d chars_resumen=%d boost=%s",
            self.name or self.file_name or self.id,
            status, sistemas_afectados, len(prioridades), anormales,
            len(summary.get('text') or ''),
            motor_meta.get('boost_codes'),
        )

        # Devolver payload con shape mínima compatible
        return {
            'status': status,
            'motor': 'v3',
            'is_biocuantico': True,
            'sistemas': [p['sistema_code'] for p in prioridades],
            'prioridades': prioridades,
            'summary_json': json_dump,
            'summary_text': summary.get('text') or '',
            'motor_meta': motor_meta,
        }

    # ====================================================================
    # BioCuántico — Auditoría textual del motor v3 (Fase 3.8.1)
    # ====================================================================
    def _render_audit_block_v3(self):
        """Construye el bloque '=== PARSER BIOCUÁNTICO V3 ===' para el log
        IA (truncado_detalle).

        Usa `biocuantico_motor_meta_json` (escrito por _run_biocuantico_
        parser_v3). Si está vacío, malformado o falla el parseo, devuelve
        un bloque mínimo con la marca "Auditoría v3 no disponible" sin
        propagar excepciones — el log IA NUNCA debe romperse por esto.

        El bloque legacy se renderiza en master_summary.py y no se toca.
        """
        import json as _json

        self.ensure_one()
        archivo_label = self.name or self.file_name or '?'

        # Helpers locales
        def _yes_no(b):
            return "Sí" if b else "No"

        def _fmt_list(v):
            if isinstance(v, (list, tuple)):
                return ", ".join(str(x) for x in v) if v else "(ninguno)"
            return str(v) if v else "(ninguno)"

        # Intento de parseo del JSON metadata
        meta = None
        meta_raw = self.biocuantico_motor_meta_json or ''
        if meta_raw:
            try:
                parsed = _json.loads(meta_raw)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                _logger.warning(
                    "BioCuántico v3: motor_meta_json inválido en archivo %s",
                    self.id,
                )

        # Nota: la línea "Usado: Sí/No" la agrega el llamador en
        # valoracion_valoracion.py (mismo patrón que el render legacy en
        # master_summary.BioCuanticoMasterSummary.render_audit_block).
        # Aquí NO se incluye para evitar duplicación.
        if not meta:
            # Bloque mínimo (no romper)
            return (
                "=== PARSER BIOCUÁNTICO V3 ===\n"
                "Archivo: %s\n"
                "Motor: v3\n"
                "Parser version: %s\n"
                "Fallback: %s\n"
                "Status: %s\n"
                "Auditoría v3 no disponible"
            ) % (
                archivo_label,
                self.biocuantico_parser_version or '?',
                _yes_no(self.biocuantico_fallback_used),
                self.biocuantico_parse_status or '?',
            )

        lines = [
            "=== PARSER BIOCUÁNTICO V3 ===",
            "Archivo: %s" % archivo_label,
            "Motor: v3",
            "Parser version: %s" % (
                self.biocuantico_parser_version
                or meta.get('parser_version') or '?'
            ),
            "Fallback: %s" % _yes_no(
                self.biocuantico_fallback_used
                or meta.get('fallback_used', False)
            ),
            "Status: %s" % (self.biocuantico_parse_status or '?'),
            "Filas procesadas: %s" % meta.get('filas_procesadas', '?'),
            "Clasificadas: %s" % meta.get('clasificadas', '?'),
            "Descartados: %s" % meta.get('descartados', '?'),
            "Sistemas afectados: %s" % meta.get('sistemas_afectados', '?'),
            "Prioridades generadas: %s" % meta.get(
                'prioridades_generadas', '?'),
            "Anormales: %s" % meta.get('anormales', '?'),
            "Chars resumen: %s" % meta.get('chars_resumen', '?'),
            "Boost codes: %s" % _fmt_list(meta.get('boost_codes')),
        ]
        return "\n".join(lines)
