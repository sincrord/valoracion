# -*- coding: utf-8 -*-
import logging
import unicodedata

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


# Plantilla del texto de consentimiento por defecto.
# Usa el placeholder {company_name} que se sustituye en runtime con
# self.env.company.name (decisión: el nombre de la empresa emisora es
# dinámico; los productos del catálogo VitalHealth no se renombran).
# Puede sobrescribirse vía ir.config_parameter:
#   'valoracion.consentimiento_texto_default'
# El override del admin también soporta el placeholder {company_name}.
DEFAULT_CONSENTIMIENTO_TEXTO_TEMPLATE = """
<p>Yo, en pleno uso de mis facultades, doy mi consentimiento para que el equipo de
{company_name} utilice mis datos personales y la información clínica funcional
que proporciono (incluidos antecedentes, archivos y resultados) con el único fin de generar
una valoración funcional con apoyo de inteligencia artificial.</p>
<p>Reconozco que:</p>
<ul>
<li>Esta valoración es de <b>carácter funcional y complementario</b>; no constituye
diagnóstico médico ni sustituye consulta o tratamiento profesional.</li>
<li>Los datos podrán ser procesados por el proveedor de inteligencia artificial contratado
por {company_name} con las medidas técnicas y organizativas correspondientes.</li>
<li>Puedo solicitar en cualquier momento el acceso, rectificación, cancelación
u oposición al tratamiento de mis datos personales.</li>
</ul>
""".strip()


def _normalize_product_name(value):
    """Normaliza un nombre de producto para hacer matching robusto.

    Aplica:
      * lowercase
      * quita acentos (NFKD + filtro de combinantes)
      * reemplaza guiones y guion-bajo por espacio
      * colapsa espacios múltiples
      * strip

    Ejemplo:
        'V-ITADOL'           -> 'v itadol'
        'V-Omega 3'          -> 'v omega 3'
        '  V-glucalose  '    -> 'v glucalose'
        'Visión Clarité'     -> 'vision clarite'
    """
    if not value:
        return ''
    s = unicodedata.normalize('NFKD', value)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace('-', ' ').replace('_', ' ')
    return ' '.join(s.split())


class Valoracion(models.Model):
    """Modelo principal de Valoración Funcional VitalHealth.

    Ciclo de vida (decisión 5):
        borrador --(action_generar_valoracion)--> generado
        borrador --(action_cancelar)--> cancelado
        generado --(action_cancelar)--> cancelado
        cancelado --(action_volver_borrador)--> borrador
        generado --(action_regenerar)--> borrador (interno) --> generado

    Folio (decisión F): asignado por ir.sequence al crear; los folios cancelados
    quedan quemados (el registro persiste con su número, no se reutiliza).
    """
    _name = 'valoracion.valoracion'
    _description = 'Valoración Funcional VitalHealth'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'
    _rec_name = 'name'

    # ====================================================================
    # Identificación y control
    # ====================================================================
    name = fields.Char(
        string='Folio',
        default=lambda self: _('Nuevo'),
        required=True,
        readonly=True,
        copy=False,
        index=True,
        tracking=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Contacto',
        required=True,
        ondelete='restrict',
        tracking=True,
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        string='Usuario responsable',
        default=lambda self: self.env.user,
        required=True,
        tracking=True,
        index=True,
    )
    date = fields.Date(
        string='Fecha',
        default=fields.Date.context_today,
        tracking=True,
    )
    state = fields.Selection(
        selection=[
            ('borrador', 'Borrador'),
            ('generado', 'Generado'),
            ('cancelado', 'Cancelado'),
        ],
        string='Estado',
        default='borrador',
        required=True,
        copy=False,
        tracking=True,
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True,
        index=True,
    )
    prompt_template_id = fields.Many2one(
        'valoracion.prompt.template',
        string='Plantilla de Prompt',
        ondelete='restrict',
        domain=[('active', '=', True)],
        default=lambda self: self.env['valoracion.prompt.template'].search(
            [('is_default', '=', True), ('active', '=', True)],
            limit=1,
        ),
        help='Plantilla utilizada para construir el prompt enviado a la IA.',
    )

    # ====================================================================
    # Indicadores de listo / advertencias
    # ====================================================================
    is_ready_to_generate = fields.Boolean(
        string='Listo para generar',
        compute='_compute_is_ready_to_generate',
        store=False,
        help='Verdadero si los requisitos a nivel de registro están completos. '
             'Las validaciones de configuración (IA activa, fuentes, etc.) '
             'se verifican al pulsar "Generar Valoración".',
    )
    is_ready_message = fields.Char(
        string='Estado de requisitos',
        compute='_compute_is_ready_to_generate',
        store=False,
    )
    is_partner_minor = fields.Boolean(
        string='Cliente menor de edad',
        compute='_compute_is_partner_minor',
        store=False,
        help='Si verdadero, se mostrará una advertencia en el formulario y '
             'la IA incluirá una nota sobre supervisión profesional. '
             'No bloquea la generación (decisión A).',
    )

    # ====================================================================
    # Consentimiento (decisión 8 + decisión C)
    # ====================================================================
    consentimiento_cliente = fields.Boolean(
        string='Consentimiento del cliente',
        default=False,
        copy=False,
        tracking=True,
        help='Consentimiento explícito del cliente para procesar sus datos '
             'con apoyo de IA. Sin este consentimiento, no se permite generar '
             'la valoración.',
    )
    consentimiento_fecha = fields.Datetime(
        string='Fecha del consentimiento',
        readonly=True,
        copy=False,
    )
    consentimiento_responsable_id = fields.Many2one(
        'res.users',
        string='Registrado por',
        readonly=True,
        copy=False,
        ondelete='restrict',
    )
    consentimiento_texto = fields.Html(
        string='Texto del consentimiento (snapshot)',
        copy=False,
        sanitize=True,
        help='Snapshot del texto del consentimiento aceptado en esta valoración. '
             'Se preserva aunque cambie la versión global posteriormente '
             '(decisión C).',
    )

    # ====================================================================
    # Datos del cliente (related, solo display)
    # ====================================================================
    partner_birthdate = fields.Date(
        related='partner_id.birthdate',
        string='Fecha de nacimiento',
        readonly=True,
    )
    partner_age = fields.Integer(
        related='partner_id.age',
        string='Edad',
        readonly=True,
    )
    partner_sexo = fields.Selection(
        related='partner_id.sexo',
        string='Sexo',
        readonly=True,
    )
    # Etiqueta legible del sexo, calculada para usar directamente en QWeb.
    # Evita expresiones como dict(_fields['partner_sexo'].selection) en el
    # template del reporte, que en Odoo 18 fallan cuando .selection es un
    # callable (TypeError: 'function' object is not iterable).
    partner_sexo_label = fields.Char(
        string='Sexo (etiqueta)',
        compute='_compute_partner_sexo_label',
        store=False,
    )
    partner_estatura_cm = fields.Integer(
        related='partner_id.estatura_cm',
        string='Estatura (cm)',
        readonly=True,
    )
    partner_peso_kg = fields.Float(
        related='partner_id.peso_kg',
        string='Peso (kg)',
        readonly=True,
    )
    partner_mobile = fields.Char(
        related='partner_id.mobile',
        string='Celular',
        readonly=True,
    )
    partner_objetivo = fields.Char(
        related='partner_id.objetivo_principal',
        string='Objetivo principal',
        readonly=True,
    )
    partner_medicamentos = fields.Text(
        related='partner_id.antecedente_medicamentos',
        string='Medicamentos actuales',
        readonly=True,
    )
    partner_padecimientos = fields.Text(
        related='partner_id.antecedente_padecimientos',
        string='Padecimientos diagnosticados',
        readonly=True,
    )
    partner_suplementos = fields.Text(
        related='partner_id.antecedente_suplementos',
        string='Suplementos actuales',
        readonly=True,
    )
    partner_alergias = fields.Text(
        related='partner_id.antecedente_alergias',
        string='Alergias o restricciones',
        readonly=True,
    )

    # ====================================================================
    # Archivos del cliente
    # ====================================================================
    archivo_cliente_ids = fields.One2many(
        'valoracion.archivo.cliente',
        'valoracion_id',
        string='Archivos del cliente',
        copy=False,
    )
    archivo_count = fields.Integer(
        string='Total de archivos',
        compute='_compute_archivo_totals',
        store=False,
    )
    archivo_total_size = fields.Integer(
        string='Tamaño total (bytes)',
        compute='_compute_archivo_totals',
        store=False,
    )
    archivo_total_size_human = fields.Char(
        string='Tamaño total',
        compute='_compute_archivo_totals',
        store=False,
    )

    # ====================================================================
    # Contenido generado por IA (HTML editable)
    # ====================================================================
    resultados_valoracion = fields.Html(
        string='Resultados de la Valoración',
        sanitize=True,
        copy=False,
    )
    prioridades_caso = fields.Html(
        string='Prioridades del caso',
        sanitize=True,
        copy=False,
    )
    plan_estrategico = fields.Html(
        string='Plan estratégico (narrativa)',
        sanitize=True,
        copy=False,
    )
    linea_producto_ids = fields.One2many(
        'valoracion.linea.producto',
        'valoracion_id',
        string='Productos VitalHealth recomendados',
        copy=False,
    )
    habitos_complementarios = fields.Html(
        string='Hábitos complementarios',
        sanitize=True,
        copy=False,
    )
    resumen_estrategico = fields.Html(
        string='Resumen estratégico',
        sanitize=True,
        copy=False,
    )
    resultados_esperados = fields.Html(
        string='Resultados esperados',
        sanitize=True,
        copy=False,
    )
    advertencias = fields.Html(
        string='Advertencias',
        sanitize=True,
        copy=False,
    )
    notas_internas = fields.Html(
        string='Notas internas',
        sanitize=True,
        copy=False,
        help='Notas privadas del equipo. No aparecen en el reporte PDF.',
    )

    # ====================================================================
    # PDF
    # ====================================================================
    pdf_attachment_id = fields.Many2one(
        'ir.attachment',
        string='Reporte PDF',
        readonly=True,
        copy=False,
        ondelete='set null',
    )
    pdf_generated_at = fields.Datetime(
        string='Última generación PDF',
        readonly=True,
        copy=False,
    )

    # ====================================================================
    # Ventas y citas (smart buttons)
    # ====================================================================
    venta_ids = fields.One2many(
        'sale.order',
        'valoracion_id',
        string='Ventas ligadas',
    )
    venta_count = fields.Integer(
        string='Total de ventas',
        compute='_compute_venta_count',
    )
    cita_count = fields.Integer(
        string='Total de citas del contacto',
        compute='_compute_cita_count',
    )

    # ====================================================================
    # Auditoría IA
    # ====================================================================
    ia_log_ids = fields.One2many(
        'valoracion.log.ia',
        'valoracion_id',
        string='Bitácora IA',
        copy=False,
    )
    ia_log_count = fields.Integer(
        string='Total de llamadas IA',
        compute='_compute_ia_log_count',
    )
    ai_raw_response = fields.Text(
        string='Última respuesta IA (cruda)',
        readonly=True,
        copy=False,
        groups='valoracion.group_valoracion_admin',
        help='Respuesta cruda de la última llamada IA. Solo visible para '
             'el Administrador para diagnóstico.',
    )
    prompt_enviado = fields.Text(
        string='Último prompt enviado',
        readonly=True,
        copy=False,
        groups='valoracion.group_valoracion_admin',
        help='Prompt enviado a la IA en la última generación. Solo visible '
             'para Administrador. Nunca contiene la API key.',
    )

    # ====================================================================
    # SQL constraints
    # ====================================================================
    _sql_constraints = [
        (
            'name_company_uniq',
            'UNIQUE(name, company_id)',
            'El folio de la valoración debe ser único por compañía.',
        ),
    ]

    # ====================================================================
    # Defaults / consentimiento
    # ====================================================================
    @api.model
    def _get_default_consentimiento_texto(self):
        """Devuelve el texto del consentimiento sustituyendo {company_name}
        por el nombre de la compañía actual (env.company.name).

        Si el admin configuró un texto custom en ir.config_parameter, ese
        texto también puede contener {company_name} y se sustituye igual.
        """
        Param = self.env['ir.config_parameter'].sudo()
        company_name = self.env.company.name or ''
        custom_text = Param.get_param('valoracion.consentimiento_texto_default')
        # Tomar plantilla custom o la base
        template = custom_text if custom_text else DEFAULT_CONSENTIMIENTO_TEXTO_TEMPLATE
        # Sustituir placeholder de forma segura (no usamos .format porque el
        # texto puede contener llaves de HTML/CSS que romperían format())
        return template.replace('{company_name}', company_name)

    # ====================================================================
    # Computeds
    # ====================================================================
    @api.depends(
        'state', 'consentimiento_cliente', 'archivo_cliente_ids',
        'partner_id.birthdate', 'partner_id.sexo',
        'partner_id.estatura_cm', 'partner_id.peso_kg',
        'partner_id.antecedente_medicamentos',
        'partner_id.antecedente_padecimientos',
        'partner_id.antecedente_suplementos',
        'partner_id.antecedente_alergias',
    )
    def _compute_is_ready_to_generate(self):
        for rec in self:
            faltantes = rec._faltantes_record_level()
            rec.is_ready_to_generate = not bool(faltantes)
            if faltantes:
                rec.is_ready_message = _("Faltan: ") + "; ".join(faltantes)
            else:
                rec.is_ready_message = _(
                    "Requisitos del registro completos. La configuración global "
                    "(IA activa, fuentes, plantilla) se valida al pulsar Generar."
                )

    @api.depends('partner_id.birthdate')
    def _compute_is_partner_minor(self):
        today = fields.Date.context_today(self)
        for rec in self:
            bd = rec.partner_id.birthdate if rec.partner_id else False
            if bd:
                age = today.year - bd.year
                if (today.month, today.day) < (bd.month, bd.day):
                    age -= 1
                rec.is_partner_minor = age < 18
            else:
                rec.is_partner_minor = False

    # Mapa estable de etiquetas para el campo sexo. Coincide con la
    # selección definida en res.partner.sexo (en res_partner.py). Si se
    # añaden o renombran opciones allí, actualizar también aquí.
    _SEXO_LABELS = {
        'masculino': 'Masculino',
        'femenino': 'Femenino',
        'otro': 'Otro',
        'no_especificado': 'No especificado',
    }

    @api.depends('partner_sexo')
    def _compute_partner_sexo_label(self):
        """Devuelve la etiqueta legible del sexo para usar en el reporte PDF.

        En Odoo 18, el atributo `.selection` de un fields.Selection puede
        ser una lista o un callable. Hacer dict() sobre el callable lanza
        TypeError en QWeb. Este compute usa un mapa local estable.
        """
        for rec in self:
            rec.partner_sexo_label = self._SEXO_LABELS.get(
                rec.partner_sexo or '', ''
            )

    @api.depends('archivo_cliente_ids', 'archivo_cliente_ids.file_size')
    def _compute_archivo_totals(self):
        for rec in self:
            total = sum(a.file_size or 0 for a in rec.archivo_cliente_ids)
            rec.archivo_count = len(rec.archivo_cliente_ids)
            rec.archivo_total_size = total
            if total < 1024:
                rec.archivo_total_size_human = "%d B" % total
            elif total < 1024 * 1024:
                rec.archivo_total_size_human = "%.1f KB" % (total / 1024.0)
            else:
                rec.archivo_total_size_human = "%.2f MB" % (total / (1024.0 * 1024.0))

    def _compute_venta_count(self):
        SaleOrder = self.env['sale.order']
        for rec in self:
            rec.venta_count = SaleOrder.search_count(
                ['|', ('valoracion_id', '=', rec.id),
                 '&', ('partner_id', '=', rec.partner_id.id),
                      ('valoracion_id', '=', False)]
            ) if rec.partner_id else 0

    def _compute_cita_count(self):
        Event = self.env['calendar.event']
        for rec in self:
            if rec.partner_id:
                rec.cita_count = Event.search_count([('partner_ids', 'in', rec.partner_id.id)])
            else:
                rec.cita_count = 0

    def _compute_ia_log_count(self):
        for rec in self:
            rec.ia_log_count = len(rec.ia_log_ids)

    # ====================================================================
    # Validaciones (separadas en niveles para reutilización)
    # ====================================================================
    def _faltantes_record_level(self):
        """Lista de mensajes de requisitos faltantes a nivel del registro.

        Estos requisitos se evalúan en el compute para mostrar en UI.
        No tocan configuración global ni fuentes (eso va en _faltantes_config_level).
        """
        self.ensure_one()
        faltantes = []

        if self.state != 'borrador':
            faltantes.append(_("La valoración debe estar en estado Borrador"))

        if not self.consentimiento_cliente:
            faltantes.append(_("Falta consentimiento explícito del cliente"))

        p = self.partner_id
        if not p:
            faltantes.append(_("Falta seleccionar el contacto"))
        else:
            if not p.birthdate:
                faltantes.append(_("Falta fecha de nacimiento del contacto"))
            if not p.sexo:
                faltantes.append(_("Falta sexo del contacto"))
            if not p.estatura_cm or p.estatura_cm <= 0:
                faltantes.append(_("Falta estatura del contacto"))
            if not p.peso_kg or p.peso_kg <= 0:
                faltantes.append(_("Falta peso del contacto"))
            if not (p.antecedente_medicamentos or '').strip():
                faltantes.append(_("Falta antecedente: medicamentos actuales"))
            if not (p.antecedente_padecimientos or '').strip():
                faltantes.append(_("Falta antecedente: padecimientos diagnosticados"))
            if not (p.antecedente_suplementos or '').strip():
                faltantes.append(_("Falta antecedente: suplementos actuales"))
            if not (p.antecedente_alergias or '').strip():
                faltantes.append(_("Falta antecedente: alergias o restricciones"))

        if not self.archivo_cliente_ids:
            faltantes.append(_("Falta al menos un archivo del cliente"))

        return faltantes

    def _faltantes_config_level(self):
        """Validaciones de configuración global y de fuentes.

        Separadas del compute por dos razones:
          1. Performance: leer ir.config_parameter en cada recompute es caro.
          2. Estas validaciones cambian a nivel sistema, no a nivel registro;
             el feedback inmediato en UI no es estrictamente necesario.
        """
        self.ensure_one()
        faltantes = []
        Param = self.env['ir.config_parameter'].sudo()

        # Tamaños de archivos
        max_archivo_mb = int(Param.get_param('valoracion.ia_max_archivo_mb', '10') or 10)
        max_total_mb = int(Param.get_param('valoracion.ia_max_total_mb', '50') or 50)
        for arch in self.archivo_cliente_ids:
            if arch.file_size and arch.file_size > max_archivo_mb * 1024 * 1024:
                faltantes.append(_(
                    "El archivo '%(name)s' supera %(mb)d MB"
                ) % {'name': arch.name or arch.file_name or '?', 'mb': max_archivo_mb})
                break  # evita inundar el mensaje
        if self.archivo_total_size and self.archivo_total_size > max_total_mb * 1024 * 1024:
            faltantes.append(_(
                "El total de archivos supera %d MB"
            ) % max_total_mb)

        # Configuración IA
        ia_activa = Param.get_param('valoracion.ia_activa', 'False')
        if str(ia_activa).lower() not in ('true', '1'):
            faltantes.append(_("La IA no está activa en la configuración global"))

        # Validación de credenciales según proveedor activo.
        # IMPORTANTE: cada proveedor tiene su propio par api_key/modelo en
        # ir.config_parameter. La validación DEBE leer las claves del
        # proveedor seleccionado, no las de Claude por defecto.
        proveedor = (
            Param.get_param('valoracion.ia_proveedor', 'claude') or 'claude'
        ).lower()
        if proveedor == 'claude':
            # Claude usa claves con prefijo 'ia_' (heredado de v1)
            claude_key = (Param.get_param('valoracion.ia_api_key') or '').strip()
            if not claude_key:
                faltantes.append(_("Falta API Key de Claude (proveedor activo: Claude)"))
            if not Param.get_param('valoracion.ia_modelo'):
                faltantes.append(_("Falta modelo Claude configurado"))
        elif proveedor == 'openai':
            # OpenAI usa claves SIN prefijo 'ia_' para evitar confusión con Claude.
            # Se hace .strip() para detectar correctamente keys solo-whitespace.
            openai_key = (Param.get_param('valoracion.openai_api_key') or '').strip()
            if not openai_key:
                faltantes.append(_("Falta API Key de OpenAI (proveedor activo: OpenAI)"))
            if not Param.get_param('valoracion.openai_modelo'):
                faltantes.append(_("Falta modelo OpenAI configurado"))
        else:
            faltantes.append(_(
                "Proveedor IA desconocido: '%s'. Valores válidos: 'claude', 'openai'."
            ) % proveedor)

        # Fuentes activas
        fuentes_activas = self.env['valoracion.fuente'].search_count([
            ('active', '=', True),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
        ])
        if not fuentes_activas:
            faltantes.append(_("Falta al menos una fuente VitalHealth activa"))

        if not self.prompt_template_id:
            faltantes.append(_("Falta plantilla de prompt"))

        return faltantes

    # ====================================================================
    # Constraints adicionales
    # ====================================================================
    @api.constrains('consentimiento_cliente', 'consentimiento_fecha', 'consentimiento_responsable_id')
    def _check_consentimiento_consistencia(self):
        for rec in self:
            if rec.consentimiento_cliente and not rec.consentimiento_fecha:
                raise ValidationError(_(
                    "El consentimiento marcado requiere fecha de registro."
                ))
            if rec.consentimiento_cliente and not rec.consentimiento_responsable_id:
                raise ValidationError(_(
                    "El consentimiento marcado requiere responsable de registro."
                ))

    @api.constrains('archivo_cliente_ids', 'archivo_total_size')
    def _check_archivo_total_size(self):
        Param = self.env['ir.config_parameter'].sudo()
        max_mb = int(Param.get_param('valoracion.ia_max_total_mb', '50') or 50)
        limit_bytes = max_mb * 1024 * 1024
        for rec in self:
            if rec.archivo_total_size and rec.archivo_total_size > limit_bytes:
                raise ValidationError(_(
                    "El total de archivos del cliente en la valoración '%(name)s' "
                    "supera el límite de %(mb)d MB (actual: %(size)s)."
                ) % {
                    'name': rec.name,
                    'mb': max_mb,
                    'size': rec.archivo_total_size_human,
                })

    # ====================================================================
    # Create / Write overrides
    # ====================================================================
    @api.model_create_multi
    def create(self, vals_list):
        """Asigna folio por secuencia y aplica defaults de consentimiento."""
        for vals in vals_list:
            # Folio (decisión 15: VAL/%(year)s/00001)
            if vals.get('name', _('Nuevo')) == _('Nuevo') or not vals.get('name'):
                seq_code = 'valoracion.valoracion'
                vals['name'] = self.env['ir.sequence'].next_by_code(seq_code) or _('VAL/SIN-SEC')

            # Defaults de consentimiento si se crea ya marcado
            if vals.get('consentimiento_cliente'):
                if 'consentimiento_fecha' not in vals:
                    vals['consentimiento_fecha'] = fields.Datetime.now()
                if 'consentimiento_responsable_id' not in vals:
                    vals['consentimiento_responsable_id'] = self.env.user.id
                if not vals.get('consentimiento_texto'):
                    vals['consentimiento_texto'] = self._get_default_consentimiento_texto()

        records = super().create(vals_list)

        # Mensaje inicial en chatter
        for rec in records:
            rec.message_post(body=_("Valoración creada en estado Borrador."))
        return records

    def write(self, vals):
        """Captura snapshot de consentimiento al activarlo (decisión C)."""
        res = super().write(vals)

        # Si se acaba de marcar el consentimiento, llenar fecha/responsable/texto
        if 'consentimiento_cliente' in vals and vals['consentimiento_cliente']:
            for rec in self.filtered(lambda r: r.consentimiento_cliente):
                updates = {}
                if not rec.consentimiento_fecha:
                    updates['consentimiento_fecha'] = fields.Datetime.now()
                if not rec.consentimiento_responsable_id:
                    updates['consentimiento_responsable_id'] = self.env.user.id
                if not rec.consentimiento_texto:
                    updates['consentimiento_texto'] = rec._get_default_consentimiento_texto()
                if updates:
                    super(Valoracion, rec).write(updates)

        return res

    # ====================================================================
    # Acciones de estado
    # ====================================================================
    def action_generar_valoracion(self):
        """Valida requisitos y dispara la generación con IA.

        Implementación:
          1. Lock pesimista para evitar doble disparo.
          2. Validaciones de registro y de configuración (UserError en orden).
          3. Llamada a _ejecutar_generacion_ia (stub Etapa 2; real en Etapa 3).
        """
        self.ensure_one()

        # 1. Lock pesimista
        self.env.cr.execute(
            "SELECT id FROM valoracion_valoracion WHERE id = %s FOR UPDATE",
            (self.id,),
        )

        # 2. Validaciones combinadas
        faltantes = self._faltantes_record_level() + self._faltantes_config_level()
        if faltantes:
            raise UserError(_(
                "No se puede generar la valoración. Requisitos faltantes:\n  • %s"
            ) % "\n  • ".join(faltantes))

        # 3. Aviso preliminar en chatter
        self.message_post(body=_(
            "Iniciando generación de la valoración con IA (proveedor: Claude)..."
        ))

        # 4. Llamada al motor IA
        return self._ejecutar_generacion_ia()

    def _ejecutar_generacion_ia(self):
        """Orquesta la generación con IA de extremo a extremo.

        Pasos:
          1. Lee configuración global (ir.config_parameter).
          2. Construye los 4 bloques de contexto (datos contacto, antecedente,
             archivos extraídos, fuentes con truncado controlado).
          3. Renderiza el prompt vía la plantilla activa.
          4. Llama a ClaudeProvider con manejo robusto de errores y timeout.
          5. Crea log IA siempre (éxito o error) con tokens, costo y duración.
          6. En éxito: procesa productos, persiste HTML y pasa a 'generado'.
          7. En error: deja state en 'borrador', guarda raw_response y prompt
             para diagnóstico del Administrador, lanza UserError claro.
        """
        self.ensure_one()
        # Lazy imports para evitar ciclos a la carga del módulo
        from .ia.file_extractor import FileExtractor  # noqa: F401  (usado vía _build_*)
        from .ia.ia_provider_base import IAProviderConfigError, ESTADO_ERROR_CONFIG

        Param = self.env['ir.config_parameter'].sudo()

        # 1. Configuración común
        max_prompt_tokens = int(
            Param.get_param('valoracion.ia_max_prompt_tokens', '150000') or 150000
        )
        temperatura = float(Param.get_param('valoracion.ia_temperatura', '0.3') or 0.3)
        max_tokens = int(Param.get_param('valoracion.ia_max_tokens', '4096') or 4096)
        timeout = int(Param.get_param('valoracion.ia_timeout_seconds', '120') or 120)
        proveedor_label = (
            Param.get_param('valoracion.ia_proveedor', 'claude') or 'claude'
        ).lower()

        # 2. Construir bloques de contexto.
        # Auditoría se recolecta en variables LOCALES (no atributos en self,
        # que rompería con el sistema de recordsets de Odoo).
        from .ia import context_builder as cb

        datos_contacto = self._build_datos_contacto_block()
        antecedente = self._build_antecedente_clinico_block()

        # Pre-cálculo de keywords con todo el material disponible del caso.
        # Si los archivos cliente ya tienen extracted_text en cache, alimentan
        # las keywords; si no, _build_archivos_cliente_block los extrae después
        # y solo se basan en antecedente + objetivo.
        case_text = self._build_case_keywords_text()
        keywords = cb.extract_keywords(case_text, max_keywords=80)

        archivos_block, archivos_images, archivos_aviso, archivos_audit = \
            self._build_archivos_cliente_block(keywords)

        # === FASE A: bloque oficial del catálogo VitalHealth ===
        # Siempre se construye desde Odoo (product.template + reglas).
        # Independiente de las "fuentes" tradicionales.
        catalogo_block, n_productos_catalogo, catalogo_audit = \
            self._build_catalogo_estructurado_block()

        # Validar que existan productos VitalHealth: si no, advertencia
        if n_productos_catalogo == 0:
            _logger.warning(
                "Valoración %s: catálogo VitalHealth VACÍO (0 productos con "
                "is_vitalhealth_effective=True). La IA no tendrá lista oficial "
                "para recomendar.",
                self.name,
            )
            self.message_post(body=_(
                "⚠ <b>Catálogo VitalHealth vacío.</b> No hay productos con "
                "<code>is_vitalhealth_effective=True</code> en el sistema. "
                "Marca productos como VitalHealth o asígnalos a la categoría "
                "VitalHealth antes de generar valoraciones, o la IA no podrá "
                "recomendar productos correctamente."
            ))

        # Validar que la plantilla activa contenga el placeholder.
        # Sin él, render() no inserta el catálogo (backward compatible).
        catalogo_placeholder_presente = False
        if self.prompt_template_id and catalogo_block:
            user_tmpl = self.prompt_template_id.user_prompt_template or ''
            sys_tmpl = self.prompt_template_id.system_prompt or ''
            catalogo_placeholder_presente = (
                '{{CATALOGO_VITALHEALTH}}' in user_tmpl
                or '{{CATALOGO_VITALHEALTH}}' in sys_tmpl
            )
            if not catalogo_placeholder_presente:
                _logger.warning(
                    "Valoración %s: plantilla '%s' NO contiene el placeholder "
                    "{{CATALOGO_VITALHEALTH}}. El bloque oficial del catálogo "
                    "(%d productos) NO se insertará en el prompt. "
                    "Edita la plantilla en Configuración → Plantillas de Prompt "
                    "para incluir el placeholder.",
                    self.name, self.prompt_template_id.name, n_productos_catalogo,
                )
                self.message_post(body=_(
                    "⚠ La plantilla de prompt activa <b>'%(name)s'</b> no contiene "
                    "el placeholder <code>{{CATALOGO_VITALHEALTH}}</code>. "
                    "El catálogo oficial VitalHealth (%(n)d productos) NO se insertará "
                    "en el prompt. Edita la plantilla en <i>Configuración → "
                    "Plantillas de Prompt</i> para incluir el placeholder."
                ) % {'name': self.prompt_template_id.name, 'n': n_productos_catalogo})

        fuentes_block, fuentes_aviso, fuentes_audit = self._build_fuentes_block(
            max_prompt_tokens=max_prompt_tokens,
            fixed_blocks_text=(
                datos_contacto + antecedente
                + (archivos_block or '') + (catalogo_block or '')
            ),
            keywords=keywords,
        )

        # Recolectar auditoría completa en un dict local
        audit_data = {
            'archivos': archivos_audit or [],
            'fuentes': fuentes_audit or [],
            'catalogo': catalogo_audit or {},
            'catalogo_n_productos': n_productos_catalogo,
            'catalogo_placeholder_presente': catalogo_placeholder_presente,
            'avisos_archivos': archivos_aviso or '',
            'avisos_fuentes': fuentes_aviso or '',
            'keywords_count': len(keywords),
            'keywords_top': sorted(keywords.items(), key=lambda x: -x[1])[:15],
        }

        # 2.bis Pre-validación: el archivo del cliente es FUENTE PRIMARIA.
        # Si llega vacío o con muy poco contenido útil, advertir al usuario
        # antes de invocar al modelo. No bloqueamos: la IA decidirá poner
        # archivo_fue_analizado=false y el backend evitará crear productos.
        archivos_chars_efectivos = self._calcular_chars_efectivos_archivos(
            archivos_block, archivos_audit,
        )
        if archivos_chars_efectivos < 300:
            _logger.warning(
                "Valoración %s: bloque de archivos cliente con poco contenido "
                "(%d chars efectivos). La IA podría reportar "
                "archivo_fue_analizado=false y NO se crearán productos.",
                self.name, archivos_chars_efectivos,
            )
            self.message_post(body=_(
                "⚠ El archivo del cliente aporta poco contenido útil al análisis "
                "(%d caracteres efectivos tras extracción). La IA podría no poder "
                "analizarlo y NO se generarían productos recomendados. Verifica "
                "que el archivo esté legible y contenga información clínica/funcional "
                "sustantiva."
            ) % archivos_chars_efectivos)

        # 3. Renderizar prompt (incluyendo el bloque oficial del catálogo)
        rendered = self.prompt_template_id.render(
            datos_contacto=datos_contacto,
            antecedente_clinico=antecedente,
            contenido_archivos=archivos_block,
            fuentes_modulo=fuentes_block,
            catalogo_vitalhealth=catalogo_block,
        )

        # 4. Snapshot del prompt para auditoría (solo Admin lo verá)
        prompt_snapshot = (
            "=== SYSTEM ===\n%s\n\n=== USER ===\n%s"
        ) % (rendered['system'], rendered['user'])
        prompt_snapshot_capped = prompt_snapshot[:200_000]

        # 5. Construir provider según proveedor activo.
        #    Se hace dispatch directo (sin pasar por la factory) porque cada
        #    proveedor lee SU propio set de claves en ir.config_parameter:
        #      * Claude:  valoracion.ia_api_key, valoracion.ia_modelo, valoracion.ia_endpoint_url
        #      * OpenAI:  valoracion.openai_api_key, valoracion.openai_modelo, valoracion.openai_endpoint_url
        #    Las api_keys se hacen .strip() para evitar falsos 401 por
        #    espacios pegados accidentalmente al copiar la key.
        try:
            if proveedor_label == 'claude':
                from .ia.ia_provider_claude import ClaudeProvider
                api_key_raw = Param.get_param('valoracion.ia_api_key') or ''
                provider = ClaudeProvider(
                    api_key=api_key_raw.strip(),
                    endpoint_url=(
                        Param.get_param('valoracion.ia_endpoint_url')
                        or 'https://api.anthropic.com/v1/messages'
                    ),
                    model=Param.get_param('valoracion.ia_modelo') or 'claude-sonnet-4-5',
                    temperature=temperatura,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                provider.costo_per_1m_input = float(
                    Param.get_param('valoracion.ia_costo_per_1m_input', '3.0') or 3.0
                )
                provider.costo_per_1m_output = float(
                    Param.get_param('valoracion.ia_costo_per_1m_output', '15.0') or 15.0
                )
            elif proveedor_label == 'openai':
                from .ia.ia_provider_openai import OpenAIProvider
                # IMPORTANTE: leemos EXACTAMENTE 'valoracion.openai_api_key'
                # (sin prefijo 'ia_'). Esto coincide con el config_parameter
                # del campo valoracion_ia_openai_api_key en res_config_settings.py.
                # Hacer .strip() es CRÍTICO: un espacio o salto de línea
                # accidental al pegar la key produce HTTP 401 aunque la key
                # sea correcta cuando se prueba con curl.
                api_key_raw = Param.get_param('valoracion.openai_api_key') or ''
                provider = OpenAIProvider(
                    api_key=api_key_raw.strip(),
                    endpoint_url=(
                        Param.get_param('valoracion.openai_endpoint_url')
                        or 'https://api.openai.com/v1/responses'
                    ),
                    model=Param.get_param('valoracion.openai_modelo') or 'gpt-4o',
                    temperature=temperatura,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                provider.costo_per_1m_input = float(
                    Param.get_param('valoracion.openai_costo_per_1m_input', '2.5') or 2.5
                )
                provider.costo_per_1m_output = float(
                    Param.get_param('valoracion.openai_costo_per_1m_output', '10.0') or 10.0
                )
            else:
                raise UserError(_(
                    "Proveedor IA desconocido: '%s'. Valores válidos: 'claude', 'openai'."
                ) % proveedor_label)
        except IAProviderConfigError as e:
            # Error de configuración temprana (api_key vacía o modelo vacío)
            self.env['valoracion.log.ia'].create({
                'valoracion_id': self.id,
                'user_id': self.env.user.id,
                'proveedor': proveedor_label,
                'modelo': '',
                'estado': ESTADO_ERROR_CONFIG,
                'error_message': str(e),
            })
            raise UserError(_("Configuración IA inválida: %s") % str(e))

        result = provider.generate(
            system_prompt=rendered['system'],
            user_prompt=rendered['user'],
            output_schema=rendered['schema'],
            images=archivos_images,
        )

        # 6. Calcular costo estimado usando los costos del provider activo
        #    (la factory los inyectó como atributos de instancia)
        costo_per_in = getattr(provider, 'costo_per_1m_input', 0.0)
        costo_per_out = getattr(provider, 'costo_per_1m_output', 0.0)
        costo_estimado = (
            (result.get('tokens_input', 0) / 1_000_000.0) * costo_per_in
            + (result.get('tokens_output', 0) / 1_000_000.0) * costo_per_out
        )

        # 7. Crear log IA siempre (éxito o error).
        # truncado = True si CUALQUIER fuente/archivo fue resumido u omitido
        # (no solo si hubo "aviso", para reflejar fielmente la realidad).
        def _audit_was_truncated(audit_list):
            for entry in (audit_list or []):
                method = entry.get('method')
                if method in ('summarized', 'truncated', 'omitted', 'extraction_error'):
                    return True
                kept = entry.get('kept_chars', 0)
                original = entry.get('original_chars', 0)
                if kept and original and kept < original:
                    return True
                if original and not kept:
                    return True
            return False

        truncado = (
            _audit_was_truncated(audit_data.get('archivos'))
            or _audit_was_truncated(audit_data.get('fuentes'))
        )

        # truncado_detalle ahora lleva auditoría rica con:
        #   1) Tokens estimados por bloque (Fase C).
        #   2) Resumen del catálogo oficial (Fase A).
        #   3) Keywords del caso (top 10).
        #   4) Detalle archivos cliente.
        #   5) Detalle fuentes auxiliares.
        truncado_detalle_parts = []

        # === 1) Tokens por bloque (Fase C) ===
        tokens_datos = self._estimate_tokens(datos_contacto)
        tokens_antecedente = self._estimate_tokens(antecedente)
        tokens_archivos = self._estimate_tokens(archivos_block or '')
        tokens_catalogo = self._estimate_tokens(catalogo_block or '')
        tokens_fuentes = self._estimate_tokens(fuentes_block or '')
        tokens_instrucciones = self._estimate_tokens(
            (self.prompt_template_id.system_prompt or '')
            + (self.prompt_template_id.user_prompt_template or '')
        )
        tokens_total_estimado = (
            tokens_datos + tokens_antecedente + tokens_archivos
            + tokens_catalogo + tokens_fuentes + tokens_instrucciones
        )
        tokens_lines = [
            "Tokens estimados por bloque:",
            "  Datos cliente:         %5d" % tokens_datos,
            "  Antecedente clínico:   %5d" % tokens_antecedente,
            "  Archivos cliente:      %5d" % tokens_archivos,
            "  Catálogo VitalHealth:  %5d  (%d productos)" % (
                tokens_catalogo, audit_data.get('catalogo_n_productos', 0),
            ),
            "  Fuentes auxiliares:    %5d" % tokens_fuentes,
            "  Instrucciones:         %5d" % tokens_instrucciones,
            "  " + ("-" * 40),
            "  Total estimado:        %5d tokens" % tokens_total_estimado,
        ]
        truncado_detalle_parts.append('\n'.join(tokens_lines))

        # === 2) Catálogo (Fase A) ===
        cat_audit = audit_data.get('catalogo') or {}
        if cat_audit.get('warning') == 'no_hay_productos':
            truncado_detalle_parts.append(
                "⚠ Catálogo VitalHealth: VACÍO (0 productos). "
                "Marca productos con is_vitalhealth=True o asígnalos a "
                "la categoría VitalHealth."
            )
        elif not audit_data.get('catalogo_placeholder_presente') and audit_data.get('catalogo_n_productos'):
            truncado_detalle_parts.append(
                "⚠ Catálogo NO insertado en el prompt: la plantilla activa "
                "no contiene el placeholder {{CATALOGO_VITALHEALTH}}. "
                "Hay %d productos disponibles que no llegaron a la IA."
                % audit_data.get('catalogo_n_productos', 0)
            )
        else:
            cat_line = "Catálogo VitalHealth: %d producto(s) enviados al prompt" % (
                cat_audit.get('incluidos', 0),
            )
            if cat_audit.get('omitidos_por_budget'):
                cat_line += " (+ %d omitidos por límite de tamaño del bloque)" % (
                    cat_audit['omitidos_por_budget'],
                )
            enriquecidos = cat_audit.get('enriquecidos_desde_fuentes', 0)
            if enriquecidos:
                cat_line += " | %d enriquecido(s) con Beneficio/Modo/Apoya desde fuentes" % enriquecidos
            truncado_detalle_parts.append(cat_line)

        # === 3) Keywords ===
        if audit_data.get('keywords_top'):
            kw_top = ', '.join('%s(%d)' % (k, v) for k, v in audit_data['keywords_top'][:10])
            truncado_detalle_parts.append("Keywords del caso (top 10): " + kw_top)

        # === 4) Detalle archivos cliente ===
        if audit_data.get('avisos_archivos'):
            truncado_detalle_parts.append(audit_data['avisos_archivos'])

        # === 4.bis) Auditoría parser BioCuántico (Fase 2.2) ===
        # Formato: cada bloque empieza con "=== PARSER BIOCUÁNTICO ===" y
        # contiene la línea "Usado: Sí" o "Usado: No" para confirmar si el
        # resumen reemplazó al flujo heurístico en este archivo.
        bq_blocks = []
        for entry in (audit_data.get('archivos') or []):
            bq = entry.get('biocuantico') or {}
            block = bq.get('audit_block_text')
            if block:
                etiqueta_uso = "Usado: Sí" if bq.get('used') else "Usado: No"
                bq_blocks.append(block + "\n" + etiqueta_uso)
        if bq_blocks:
            truncado_detalle_parts.append('\n\n'.join(bq_blocks))

        # === 5) Detalle fuentes auxiliares ===
        if audit_data.get('avisos_fuentes'):
            truncado_detalle_parts.append(audit_data['avisos_fuentes'])

        self.env['valoracion.log.ia'].create({
            'valoracion_id': self.id,
            'user_id': self.env.user.id,
            'proveedor': provider.name,
            'modelo': provider.model,
            'tokens_input': result.get('tokens_input', 0),
            'tokens_output': result.get('tokens_output', 0),
            'costo_estimado': round(costo_estimado, 6),
            'duracion_ms': result.get('duration_ms', 0),
            'estado': result.get('estado', 'error_api'),
            'error_message': result.get('error') or False,
            'request_size_bytes': len(prompt_snapshot.encode('utf-8')),
            'response_size_bytes': len((result.get('raw_response') or '').encode('utf-8')),
            'truncado': truncado,
            'truncado_detalle': '\n\n'.join(truncado_detalle_parts) or False,
        })

        # 8. Si falló, mensaje claro y persistir raw para diagnóstico
        if not result.get('success'):
            self.sudo().write({
                'ai_raw_response': result.get('raw_response') or '',
                'prompt_enviado': prompt_snapshot_capped,
            })
            self.message_post(body=_(
                "Falló la generación IA: %(estado)s — %(error)s"
            ) % {
                'estado': result.get('estado', '?'),
                'error': result.get('error') or _('sin detalle'),
            })
            raise UserError(_(
                "La generación con IA falló:\n\n%(error)s\n\n"
                "Detalles disponibles para el Administrador en la pestaña "
                "Auditoría IA."
            ) % {'error': result.get('error') or _('error desconocido')})

        # 9. Procesar respuesta exitosa
        data = result.get('data', {}) or {}

        # === FASE 1 (post-IA): validar archivo_fue_analizado ===
        # Si la IA reporta que NO pudo analizar el archivo del cliente, NO
        # creamos productos. La valoración se guarda con resultados/advertencias
        # para que el usuario sepa qué pasó y pueda cargar un archivo válido.
        analisis_archivo = data.get('analisis_archivo_cliente') or {}
        archivo_fue_analizado = True  # default permisivo para retrocompat
        if isinstance(analisis_archivo, dict):
            valor = analisis_archivo.get('archivo_fue_analizado')
            if valor is not None:
                archivo_fue_analizado = bool(valor)

        if not archivo_fue_analizado:
            productos_recomendados = []  # forzar lista vacía
            _logger.warning(
                "Valoración %s: la IA reporta archivo_fue_analizado=false. "
                "NO se crearán productos recomendados. Calidad: '%s'.",
                self.name, analisis_archivo.get('calidad_del_archivo') or '?',
            )
        else:
            productos_recomendados = data.get('productos_recomendados', []) or []

        # 10. Limpiar líneas previas si re-generación (decisión D: logs se conservan)
        if self.linea_producto_ids:
            self.linea_producto_ids.unlink()

        lineas_creadas, no_encontrados = self._process_productos(productos_recomendados)

        # === Construir HTML enriquecido ===
        # Análisis del archivo se prepende al campo resultados_valoracion para
        # que aparezca en form, PDF y reporte sin necesidad de campos nuevos.
        resultados_ia = data.get('resultados_valoracion') or ''
        analisis_html = self._render_analisis_archivo_html(analisis_archivo)
        resultados_combinados = (analisis_html + resultados_ia) if analisis_html else resultados_ia

        # prioridades_caso: la IA ahora devuelve array de objetos. Convertir
        # a HTML legible. Tolerante con string (versión antigua).
        prioridades_raw = data.get('prioridades_caso')
        prioridades_html = self._render_prioridades_caso_html(prioridades_raw)

        # 11. Persistir HTML, raw, prompt y cambiar estado
        self.sudo().write({
            'ai_raw_response': result.get('raw_response') or '',
            'prompt_enviado': prompt_snapshot_capped,
        })
        self.write({
            'resultados_valoracion': resultados_combinados or '',
            'prioridades_caso': prioridades_html or '',
            'plan_estrategico': data.get('plan_estrategico') or '',
            'habitos_complementarios': data.get('habitos_complementarios') or '',
            'resumen_estrategico': data.get('resumen_estrategico') or '',
            'resultados_esperados': data.get('resultados_esperados') or '',
            'advertencias': data.get('advertencias') or '',
            'state': 'generado',
        })

        # 12. Mensaje resumen en chatter
        msg = [_("✓ Valoración generada por IA. Revisa antes de descargar el PDF.")]

        # Advertencia destacada si la IA reportó que el archivo NO se pudo analizar
        if not archivo_fue_analizado:
            calidad_label = analisis_archivo.get('calidad_del_archivo') if isinstance(analisis_archivo, dict) else ''
            msg.append(_(
                "⚠ <b>La IA NO pudo analizar el archivo del cliente</b> "
                "(calidad reportada: %(cal)s). NO se generaron productos "
                "recomendados. Carga un archivo válido con información "
                "clínica/funcional sustantiva y vuelve a generar."
            ) % {'cal': calidad_label or 'no especificada'})

        msg.append(_(
            "Tokens: %(in)s entrada / %(out)s salida — Costo estimado: $%(cost)s USD — Duración: %(dur)s ms"
        ) % {
            'in': result.get('tokens_input', 0),
            'out': result.get('tokens_output', 0),
            'cost': '%.4f' % costo_estimado,
            'dur': result.get('duration_ms', 0),
        })
        if truncado:
            # Conteo rápido para el chatter; el detalle completo está en log IA
            archivos_resumidos = sum(
                1 for a in (audit_data.get('archivos') or [])
                if a.get('method') in ('summarized', 'truncated')
            )
            archivos_omitidos = sum(
                1 for a in (audit_data.get('archivos') or [])
                if a.get('method') in ('omitted', 'extraction_error')
            )
            fuentes_resumidas = sum(
                1 for f in (audit_data.get('fuentes') or [])
                if f.get('method') in ('summarized', 'truncated')
            )
            fuentes_omitidas = sum(
                1 for f in (audit_data.get('fuentes') or [])
                if f.get('method') == 'omitted'
            )
            msg.append(_(
                "⚠ Contexto procesado: %(ar)d archivos resumidos / %(ao)d omitidos · "
                "%(fr)d fuentes resumidas / %(fo)d omitidas. Detalle en Log IA."
            ) % {
                'ar': archivos_resumidos, 'ao': archivos_omitidos,
                'fr': fuentes_resumidas, 'fo': fuentes_omitidas,
            })
        if no_encontrados:
            msg.append(_(
                "⚠ Productos no encontrados en catálogo (asígnalos manualmente): %s"
            ) % ', '.join(no_encontrados[:10]))
        if self.is_partner_minor:
            msg.append(_(
                "ℹ Cliente menor de edad: la IA debe haber incluido nota de supervisión profesional."
            ))
        self.message_post(body='<br/>'.join(msg))

        return True

    # ====================================================================
    # Helpers de construcción de prompt y procesamiento de respuesta
    # ====================================================================
    def _build_datos_contacto_block(self):
        """Construye el bloque <contexto_cliente> con datos demográficos."""
        self.ensure_one()
        p = self.partner_id
        sexo_label = ''
        if p.sexo:
            sexo_label = dict(p._fields['sexo'].selection).get(p.sexo, '') or ''
        menor_warn = ''
        if self.is_partner_minor:
            menor_warn = _(
                "\n⚠ CLIENTE MENOR DE EDAD: incluye en 'advertencias' la "
                "necesidad explícita de supervisión profesional."
            )
        return (_(
            "Nombre: %(nombre)s\n"
            "Edad: %(edad)s años\n"
            "Sexo: %(sexo)s\n"
            "Estatura: %(est)s cm\n"
            "Peso: %(peso)s kg\n"
            "Celular: %(cel)s\n"
            "Objetivo principal: %(obj)s"
        ) % {
            'nombre': p.name or '',
            'edad': p.age if p.age else '?',
            'sexo': sexo_label or _('no especificado'),
            'est': p.estatura_cm or '?',
            'peso': p.peso_kg or '?',
            'cel': p.mobile or _('no proporcionado'),
            'obj': p.objetivo_principal or _('no especificado'),
        }) + menor_warn

    def _build_antecedente_clinico_block(self):
        """Construye el bloque <antecedente_clinico>."""
        self.ensure_one()
        p = self.partner_id
        return _(
            "Medicamentos actuales y dosis:\n%(med)s\n\n"
            "Padecimientos diagnosticados:\n%(pad)s\n\n"
            "Suplementos actuales:\n%(sup)s\n\n"
            "Alergias o restricciones:\n%(ale)s"
        ) % {
            'med': p.antecedente_medicamentos or _('ninguno reportado'),
            'pad': p.antecedente_padecimientos or _('ninguno reportado'),
            'sup': p.antecedente_suplementos or _('ninguno reportado'),
            'ale': p.antecedente_alergias or _('ninguna reportada'),
        }

    def _build_catalogo_estructurado_block(self):
        """Construye el bloque oficial del catálogo VitalHealth.

        FASE A — bloque que SIEMPRE viaja al prompt con la lista de productos
        VitalHealth disponibles desde Odoo (product.template) + sus reglas
        comerciales (valoracion.producto.regla).

        Estrategia:
          1. Buscar product.template con is_vitalhealth_effective=True,
             respetando multi-compañía (productos sin company_id o de la
             company actual).
          2. Pre-cargar reglas en bulk para evitar N consultas.
          3. Ordenar por prioridad: con regla > con flag manual > resto.
             Dentro, alfabético por categoría y nombre.
          4. Construir texto compacto por producto: nombre, código, precio,
             categoría, descripción corta (cap 200 chars), duración del
             paquete, cantidad mensual, dosis sugerida (de la regla).
          5. Truncar al cap MAX_CHARS_CATALOGO conservando los más
             prioritarios; loggear cuántos se omitieron.

        Returns:
            tupla (texto_bloque, n_productos_incluidos, audit_dict).
            audit_dict tiene: total_disponibles, incluidos,
                              omitidos_por_budget, chars_totales, warning.

        IMPORTANTE: este bloque se inserta vía placeholder
        {{CATALOGO_VITALHEALTH}} en la plantilla. Si la plantilla activa NO
        contiene el placeholder, el catálogo NO se inserta — el llamador
        debe detectar esa situación y avisar.
        """
        self.ensure_one()
        from .ia import context_builder as cb

        Product = self.env['product.template']
        Regla = self.env['valoracion.producto.regla']

        # Buscar productos VitalHealth respetando company.
        # is_vitalhealth_effective es un computed con search; combina flag
        # manual y categoría VitalHealth.
        domain = [('is_vitalhealth_effective', '=', True)]
        if self.company_id:
            domain += [
                '|',
                ('company_id', '=', False),
                ('company_id', '=', self.company_id.id),
            ]

        productos = Product.search(domain, order='categ_id, name')
        total_disponibles = len(productos)

        if total_disponibles == 0:
            return '', 0, {
                'total_disponibles': 0,
                'incluidos': 0,
                'omitidos_por_budget': 0,
                'chars_totales': 0,
                'warning': 'no_hay_productos',
            }

        # Pre-cargar reglas activas para los productos encontrados (bulk).
        regla_domain = [
            ('product_id', 'in', productos.ids),
            ('active', '=', True),
        ]
        if self.company_id:
            regla_domain += [
                '|',
                ('company_id', '=', False),
                ('company_id', '=', self.company_id.id),
            ]
        reglas = Regla.search(regla_domain, order='prioridad desc, id')
        reglas_by_product = {}
        for r in reglas:
            # Si hay varias, gana la primera (orden por prioridad desc)
            if r.product_id.id not in reglas_by_product:
                reglas_by_product[r.product_id.id] = r

        # Ordenar productos: con regla > con flag manual > resto;
        # luego por categoría y nombre.
        def _priority_key(prod):
            has_regla = 1 if reglas_by_product.get(prod.id) else 0
            has_flag = 1 if prod.is_vitalhealth else 0
            return (
                -has_regla,
                -has_flag,
                (prod.categ_id.name or '').lower(),
                (prod.name or '').lower(),
            )
        productos_sorted = sorted(productos, key=_priority_key)

        # === Enriquecimiento desde Fuentes activas (Fase 2.3) ===
        # Busca Beneficio / Modo de empleo / Puede apoyar a personas con
        # en el texto extraído de las fuentes y los inyecta por producto.
        # Si una fuente aún no se extrajo, se dispara su action_reextraer
        # (idempotente, cachea el resultado).
        enrichment = {}
        productos_enriquecidos = 0
        try:
            fuentes_text_parts = []
            for fuente in self._get_active_fuentes():
                if fuente.extraction_status != 'ok' or not fuente.extracted_text:
                    try:
                        fuente.sudo().action_reextraer()
                    except Exception:
                        _logger.exception(
                            "Catálogo: fallo re-extrayendo fuente %s, "
                            "se continúa sin enriquecer con ella.", fuente.id,
                        )
                if fuente.extracted_text:
                    fuentes_text_parts.append(fuente.extracted_text)
            if fuentes_text_parts:
                from .ia.catalog_enricher import (
                    extract_product_sections_from_fuentes,
                )
                enrichment = extract_product_sections_from_fuentes(
                    '\n\n'.join(fuentes_text_parts), productos_sorted,
                )
                productos_enriquecidos = len(enrichment)
                _logger.info(
                    "Catálogo: %d/%d productos enriquecidos desde fuentes activas",
                    productos_enriquecidos, len(productos_sorted),
                )
        except Exception:  # pragma: no cover — defensa última
            _logger.exception(
                "Catálogo: enriquecimiento desde fuentes falló — "
                "el catálogo se construirá sin Beneficio/Modo/Apoya."
            )
            enrichment = {}

        # Cabecera del bloque (instrucción inline para la IA)
        header_lines = [
            "=== CATÁLOGO OFICIAL VITALHEALTH (n=%d productos disponibles) ===" % total_disponibles,
            "Esta es la lista oficial de productos VitalHealth disponibles para recomendar.",
            "Usa estos nombres EXACTOS. NO recomiendes productos fuera de este catálogo.",
            "Las cantidades mensuales mostradas son las que Odoo aplicará automáticamente; tú solo devuelve nombre, dosis_sugerida y razón.",
            "",
        ]
        header = '\n'.join(header_lines) + '\n'

        max_chars = cb.Limits.MAX_CHARS_CATALOGO
        chars_used = len(header)
        product_chunks = []
        incluidos = 0

        company_currency = self.company_id.currency_id

        for prod in productos_sorted:
            regla = reglas_by_product.get(prod.id)

            # Construir el bloque de este producto
            block_lines = []

            # Línea 1: número, nombre, código, precio
            codigo_str = ' [%s]' % prod.default_code if prod.default_code else ''
            precio_str = ''
            try:
                if prod.list_price:
                    cur = prod.currency_id or company_currency
                    cur_name = cur.name if cur else ''
                    precio_str = ' — %.2f %s' % (prod.list_price, cur_name)
            except Exception:
                pass
            block_lines.append('%d. %s%s%s' % (
                incluidos + 1,
                prod.name or '(sin nombre)',
                codigo_str,
                precio_str,
            ))

            # Categoría
            if prod.categ_id and prod.categ_id.name:
                block_lines.append('   Categoría: %s' % prod.categ_id.name)

            # Descripción: preferir description_sale (descripción comercial),
            # con fallback a description (interna). Cap 350 chars para enriquecer
            # el contexto que la IA tiene de cada producto sin inflar el bloque
            # global más allá de Limits.MAX_CHARS_CATALOGO.
            desc = ''
            try:
                if hasattr(prod, 'description_sale') and prod.description_sale:
                    desc = prod.description_sale.strip()
                if not desc and hasattr(prod, 'description') and prod.description:
                    desc = prod.description.strip()
            except Exception:
                desc = ''
            if desc:
                if len(desc) > 350:
                    desc = desc[:350].rstrip() + '...'
                desc = ' '.join(desc.split())
                block_lines.append('   Descripción: %s' % desc)

            # Enriquecimiento desde fuente activa (Fase 2.3)
            enrich_data = enrichment.get(prod.id) or {}
            if enrich_data.get('beneficio'):
                block_lines.append('   Beneficio: %s' % enrich_data['beneficio'])
            if enrich_data.get('modo_empleo'):
                block_lines.append('   Modo de empleo: %s' % enrich_data['modo_empleo'])
            if enrich_data.get('apoya_a'):
                block_lines.append('   Puede apoyar a personas con: %s'
                                   % enrich_data['apoya_a'])

            # Datos de la regla (si existe)
            if regla:
                block_lines.append(
                    '   Duración del paquete: %d días' % regla.duracion_paquete_dias
                )
                cant_str = ('%g' % regla.cantidad_mensual) if regla.cantidad_mensual else '1'
                block_lines.append(
                    '   Cantidad mensual de compra: %s paquete(s)' % cant_str
                )
                if regla.dosis_sugerida:
                    dosis_short = regla.dosis_sugerida.strip()
                    if len(dosis_short) > 250:
                        dosis_short = dosis_short[:250].rstrip() + '...'
                    block_lines.append('   Dosis sugerida: %s' % dosis_short)
                # Restricciones / observaciones funcionales si existen.
                # Etiqueta clara para que la IA las tome en cuenta al
                # cruzar con alergias/medicamentos del cliente.
                if regla.observaciones:
                    obs = regla.observaciones.strip()
                    if len(obs) > 250:
                        obs = obs[:250].rstrip() + '...'
                    obs = ' '.join(obs.split())
                    block_lines.append(
                        '   Restricciones / Observaciones: %s' % obs
                    )
            else:
                block_lines.append(
                    '   Cantidad mensual de compra: 1 paquete (default, sin regla configurada)'
                )

            chunk = '\n'.join(block_lines) + '\n\n'

            # ¿Cabe en el budget?
            if chars_used + len(chunk) > max_chars:
                # No cabe: cortar y registrar omisión
                break

            product_chunks.append(chunk)
            chars_used += len(chunk)
            incluidos += 1

        omitidos = total_disponibles - incluidos

        body = header + ''.join(product_chunks)
        if omitidos > 0:
            body += '[+ %d producto(s) adicional(es) en catálogo, omitidos por límite de tamaño del bloque]\n' % omitidos

        audit = {
            'total_disponibles': total_disponibles,
            'incluidos': incluidos,
            'omitidos_por_budget': omitidos,
            'chars_totales': len(body),
            'enriquecidos_desde_fuentes': productos_enriquecidos,
            'warning': None,
        }
        return body, incluidos, audit

    def _build_archivos_cliente_block(self, keywords=None):
        """Extrae texto de los archivos del cliente CON RESUMEN INTELIGENTE.

        Args:
            keywords: dict {keyword: weight} producido por
                context_builder.extract_keywords. Si es None, se calcula
                aquí mismo a partir del antecedente del partner.

        Returns:
            tupla (texto_concatenado, lista_imagenes, aviso_o_None,
                   audit_metadatos_list).
            audit_metadatos_list es una lista de dicts con detalle por archivo
            (name, tipo, score, original_chars, kept_chars, included, etc.).

        IMPORTANTE: este método NO asigna atributos sobre self. Toda la
        información de auditoría se devuelve como cuarto valor de retorno
        para que el llamador la persista en el log IA.
        """
        self.ensure_one()
        from .ia.file_extractor import FileExtractor
        from .ia import context_builder as cb

        # Garantizar keywords: si no las recibimos, las calculamos
        if keywords is None:
            case_text = self._build_case_keywords_text()
            keywords = cb.extract_keywords(case_text, max_keywords=80)

        archivos_audit = []
        chunks = []
        images = []
        errores = []
        used_total = 0
        max_total = cb.Limits.MAX_CHARS_CLIENT_FILES_TOTAL

        for arch in self.archivo_cliente_ids:
            # Asegurar cache vigente
            cached_text = arch.extracted_text if arch.extraction_status == 'ok' else None

            if cached_text is None and arch.file_type != 'imagen':
                # Re-extraer
                result = FileExtractor.extract(
                    file_data_b64=arch.file_data,
                    file_name=arch.file_name,
                    file_type=arch.file_type,
                )
                if not result.get('success'):
                    arch.sudo().write({
                        'extraction_status': 'error',
                        'extraction_error': result.get('error') or '',
                        'extracted_at': fields.Datetime.now(),
                    })
                    errores.append("%s: %s" % (
                        arch.file_name or '?', result.get('error', '?'),
                    ))
                    archivos_audit.append({
                        'name': arch.file_name or '?',
                        'tipo': arch.file_type or '',
                        'score': 0,
                        'original_chars': 0,
                        'kept_chars': 0,
                        'method': 'extraction_error',
                        'included': 'omitida_error_extraccion',
                    })
                    continue
                cached_text = result.get('text') or ''
                arch.sudo().write({
                    'extraction_status': 'ok',
                    'extracted_text': cached_text,
                    'extracted_at': fields.Datetime.now(),
                    'extraction_error': False,
                })
            elif cached_text is None and arch.file_type == 'imagen':
                # Re-extraer imagen
                result = FileExtractor.extract(
                    file_data_b64=arch.file_data,
                    file_name=arch.file_name,
                    file_type=arch.file_type,
                )
                if result.get('success') and result.get('image_b64'):
                    images.append({
                        'image_b64': result['image_b64'],
                        'image_media_type': result.get('image_media_type', 'image/png'),
                        'name': arch.file_name or '',
                    })
                    arch.sudo().write({
                        'extraction_status': 'ok',
                        'extracted_text': _(
                            "[Imagen enviada como input multimodal: %s]"
                        ) % (arch.file_name or ''),
                        'extracted_at': fields.Datetime.now(),
                        'extraction_error': False,
                    })
                    chunks.append("=== %s (imagen, multimodal) ===" % (arch.file_name or ''))
                    archivos_audit.append({
                        'name': arch.file_name or '?',
                        'tipo': 'imagen',
                        'score': 0,
                        'original_chars': 0,
                        'kept_chars': 0,
                        'method': 'multimodal',
                        'included': 'multimodal',
                    })
                continue
            elif arch.file_type == 'imagen':
                # Imagen ya cacheada como texto placeholder; re-leer binario para multimodal
                result = FileExtractor.extract(
                    file_data_b64=arch.file_data,
                    file_name=arch.file_name,
                    file_type='imagen',
                )
                if result.get('success') and result.get('image_b64'):
                    images.append({
                        'image_b64': result['image_b64'],
                        'image_media_type': result.get('image_media_type', 'image/png'),
                        'name': arch.file_name or '',
                    })
                    chunks.append("=== %s (imagen, multimodal) ===" % (arch.file_name or ''))
                    archivos_audit.append({
                        'name': arch.file_name or '?',
                        'tipo': 'imagen',
                        'score': 0,
                        'original_chars': 0,
                        'kept_chars': 0,
                        'method': 'multimodal',
                        'included': 'multimodal',
                    })
                continue

            # === Resumir texto si supera presupuesto por archivo ===
            text = cached_text or ''
            score = cb.score_relevance(text, keywords)
            remaining_total = max_total - used_total

            if remaining_total < cb.Limits.MIN_USEFUL_BUDGET:
                archivos_audit.append({
                    'name': arch.name or arch.file_name or '?',
                    'tipo': arch.file_type or '',
                    'score': score,
                    'original_chars': len(text),
                    'kept_chars': 0,
                    'method': 'omitted',
                    'included': 'omitida_sin_presupuesto',
                })
                continue

            # ===========================================================
            # Fase 2.2 — Resumen Maestro BioCuántico
            # ===========================================================
            # Lazy-trigger: si el archivo nunca se analizó, se intenta una sola
            # vez aquí. _run_biocuantico_parser jamás levanta — ante error duro
            # deja status='failed' y se cae al heurístico normal.
            bq_payload = None
            bq_audit_block = None
            bq_used = False
            try:
                if arch.biocuantico_parse_status in ('not_parsed', 'detected', False, None):
                    bq_payload = arch._run_biocuantico_parser(force=False)
            except Exception:  # pragma: no cover — defensa última
                _logger.exception(
                    "Lazy parser BioCuántico levantó excepción inesperada "
                    "para archivo %s — usando flujo heurístico.", arch.id,
                )
                bq_payload = None

            # ¿Hay resumen utilizable? (ok / partial con texto cacheado)
            bq_status = arch.biocuantico_parse_status
            bq_summary_text = arch.biocuantico_summary_text or ''
            use_biocuantico = (
                bq_status in ('ok', 'partial') and bool(bq_summary_text)
            )

            # Cap por archivo: el menor entre per-file y remaining
            budget = min(cb.Limits.MAX_CHARS_PER_CLIENT_FILE, remaining_total)

            if use_biocuantico:
                # Reemplazo total del resumen heurístico para este archivo.
                # El bloque <archivos_cliente> sigue intacto: la IA recibe el
                # mismo header "=== <nombre> (<tipo>) ===" pero el cuerpo es
                # ahora el Resumen Maestro estructurado.
                bq_text = bq_summary_text
                if len(bq_text) > budget - 80:
                    # Cap defensivo si el remaining_total es muy chico
                    bq_text = bq_text[:max(budget - 80, 200)].rstrip() + (
                        "\n[Resumen BioCuántico recortado por presupuesto]"
                    )
                header = "=== %s (%s, resumen BioCuántico %s) ===\n" % (
                    arch.name or arch.file_name or '?',
                    arch.file_type or '',
                    bq_status,
                )
                chunk = header + bq_text
                chunks.append(chunk)
                used_total += len(chunk) + 2
                bq_used = True

                method_label = 'biocuantico_%s' % bq_status
                included_label = (
                    'biocuantico_ok' if bq_status == 'ok' else 'biocuantico_partial'
                )
                kept_chars = len(chunk)
                archivos_audit_entry = {
                    'name': arch.name or arch.file_name or '?',
                    'tipo': arch.file_type or '',
                    'score': score,
                    'original_chars': len(text),
                    'kept_chars': kept_chars,
                    'method': method_label,
                    'included': included_label,
                    'paragraphs_total': None,
                    'paragraphs_kept': None,
                }
            else:
                # === Flujo heurístico actual (fallback completo, intacto) ===
                summary, info = cb.summarize_by_keywords(text, keywords, budget)

                header = "=== %s (%s) ===\n" % (
                    arch.name or arch.file_name or '?',
                    arch.file_type or '',
                )
                chunk = header + summary
                chunks.append(chunk)
                used_total += len(chunk) + 2  # separador

                included_label = {
                    'full': 'completa',
                    'summarized': 'resumida',
                    'truncated': 'recortada',
                    'empty': 'vacia',
                }.get(info.get('method'), 'parcial')
                archivos_audit_entry = {
                    'name': arch.name or arch.file_name or '?',
                    'tipo': arch.file_type or '',
                    'score': score,
                    'original_chars': info.get('original_chars', len(text)),
                    'kept_chars': info.get('kept_chars', len(chunk)),
                    'method': info.get('method'),
                    'included': included_label,
                    'paragraphs_total': info.get('paragraphs_total'),
                    'paragraphs_kept': info.get('paragraphs_kept'),
                }

            # Render del bloque de auditoría BioCuántico (si aplica)
            if bq_payload is not None or arch.biocuantico_parse_status not in (
                'not_parsed', False, None,
            ):
                # Fase 3.8.1: si el motor v3 produjo este resumen, usar el
                # render específico de v3 (con biocuantico_motor_meta_json).
                # Legacy mantiene su render intacto.
                if arch.biocuantico_motor_version == 'v3':
                    try:
                        bq_audit_block = arch._render_audit_block_v3()
                    except Exception:  # pragma: no cover — defensivo
                        _logger.exception(
                            "BioCuántico v3: render auditoría falló")
                        bq_audit_block = (
                            "=== PARSER BIOCUÁNTICO V3 ===\n"
                            "Archivo: %s\n"
                            "Auditoría v3 no disponible"
                        ) % (arch.name or arch.file_name or '?')
                        # Nota: "Usado: Sí/No" lo agrega el llamador abajo.
                else:
                    try:
                        from .biocuantico.master_summary import BioCuanticoMasterSummary

                        # Resolución del payload para auditoría — ESTRICTAMENTE
                        # SIN re-correr el parser (cache fuerte):
                        #   1) Si el parser corrió en este request, usarlo (fresco).
                        #   2) Si no, reconstruir desde biocuantico_summary_json
                        #      (cache persistido — métricas reales).
                        #   3) Si tampoco hay JSON, stub mínimo con lo que tenemos
                        #      (caso: status='failed' o 'not_biocuantico').
                        payload_for_audit = bq_payload
                        if payload_for_audit is None and arch.biocuantico_summary_json:
                            payload_for_audit = BioCuanticoMasterSummary.payload_from_json(
                                arch.biocuantico_summary_json,
                                archivo_status=arch.biocuantico_parse_status,
                                parse_error=arch.biocuantico_parse_error or None,
                            )
                        if payload_for_audit is None:
                            # Stub final — sin JSON ni runtime (failed/not_biocuantico)
                            payload_for_audit = {
                                'is_biocuantico': bool(arch.biocuantico_detectado),
                                'status': arch.biocuantico_parse_status,
                                'metodo': None,
                                'estadisticas': {},
                                'sistemas': [],
                                'error': arch.biocuantico_parse_error or None,
                            }
                        bq_audit_block = BioCuanticoMasterSummary.render_audit_block(
                            payload_for_audit,
                            summary_text=bq_summary_text,
                            archivo_label=arch.name or arch.file_name or '?',
                        )
                    except Exception:  # pragma: no cover — auditoría no debe romper
                        _logger.exception("BioCuántico: render_audit_block falló")
                        bq_audit_block = None

            archivos_audit_entry['biocuantico'] = {
                'status': arch.biocuantico_parse_status,
                'used': bq_used,
                'audit_block_text': bq_audit_block,
            }
            archivos_audit.append(archivos_audit_entry)

        # Aviso legible para log
        partes = []
        if archivos_audit:
            partes.append(cb.format_audit_summary(
                archivos_audit, etiqueta='Archivos cliente',
            ))
        if errores:
            partes.append("Errores de extracción: " + '; '.join(errores))
        aviso = '\n'.join(partes) if partes else None

        texto = '\n\n'.join(chunks) if chunks else _('(sin archivos extraídos)')
        return texto, images, aviso, archivos_audit

    def _build_fuentes_block(self, max_prompt_tokens, fixed_blocks_text, keywords=None):
        """Construye el bloque de fuentes con SELECCIÓN INTELIGENTE.

        Args:
            max_prompt_tokens: límite total de tokens del prompt (config IA).
            fixed_blocks_text: texto ya construido de otros bloques (para
                calcular presupuesto restante).
            keywords: dict {keyword: weight}. Si es None, se calcula a partir
                del antecedente del partner.

        Returns:
            tupla (texto_combinado, aviso_legible_o_None, fuentes_audit_list).
            fuentes_audit_list contiene un dict por cada fuente con detalles
            de score, original_chars, kept_chars, included, etc.

        IMPORTANTE: este método NO asigna atributos sobre self. Los metadatos
        se devuelven como tercer valor para que el llamador los persista en
        el log IA.
        """
        self.ensure_one()
        from .ia import context_builder as cb

        fuentes = self._get_active_fuentes()
        if not fuentes:
            return '', None, []

        # Re-extraer fuentes sin cache vigente
        for fuente in fuentes:
            if fuente.extraction_status != 'ok' or not fuente.extracted_text:
                fuente.sudo().action_reextraer()

        # === Garantizar keywords ===
        if keywords is None:
            case_text = self._build_case_keywords_text()
            keywords = cb.extract_keywords(case_text, max_keywords=80)

        # === Calcular presupuesto disponible para fuentes ===
        # Convertir tokens a chars (heurística 3.5 char/token).
        fixed_tokens = self._estimate_tokens(fixed_blocks_text or '')
        template_tokens = self._estimate_tokens(
            (self.prompt_template_id.system_prompt or '')
            + (self.prompt_template_id.user_prompt_template or '')
        )
        available_tokens = max(
            max_prompt_tokens - fixed_tokens - template_tokens - 2000, 5000
        )
        available_chars = int(available_tokens * 3.5)
        # Cap superior por defecto (configurable). El menor entre Limits y available.
        max_total_chars = min(available_chars, cb.Limits.MAX_CHARS_SOURCES_TOTAL)

        # === FASE 2 + 3: scoring + resumen por fuente ===
        sources_input = []
        for fuente in fuentes:
            if not fuente.extracted_text:
                continue
            tipo_label = ''
            if fuente.tipo:
                tipo_label = dict(fuente._fields['tipo'].selection).get(fuente.tipo, '')
            sources_input.append({
                'name': fuente.name or '(sin nombre)',
                'tipo': tipo_label,
                'text': fuente.extracted_text or '',
                'sequence': fuente.sequence or 9999,
            })

        if not sources_input:
            return '', None, []

        body, metadatos = cb.select_and_summarize_sources(
            sources_input,
            keywords,
            max_chars_per_source=cb.Limits.MAX_CHARS_PER_SOURCE,
            max_chars_total=max_total_chars,
            min_useful_budget=cb.Limits.MIN_USEFUL_BUDGET,
        )

        # === Aviso legible ===
        aviso = cb.format_audit_summary(metadatos, etiqueta='Fuentes')

        return body, aviso, metadatos

    def _build_case_keywords_text(self):
        """Construye un único string con TODO el material clínico del caso
        para alimentar la extracción de keywords.

        Incluye antecedente, objetivo del partner, archivos cliente extraídos
        (cache) y línea narrativa de la valoración si existe.
        """
        self.ensure_one()
        parts = []
        p = self.partner_id
        if p:
            for f in ('antecedente_medicamentos', 'antecedente_padecimientos',
                      'antecedente_suplementos', 'antecedente_alergias',
                      'objetivo_principal'):
                v = getattr(p, f, None)
                if v:
                    parts.append(v)
        # Texto extraído de archivos cliente (cache)
        for arch in self.archivo_cliente_ids:
            if arch.extracted_text:
                parts.append(arch.extracted_text)
        return '\n\n'.join(parts)

    def _estimate_tokens(self, text):
        """Heurística simple: ~3.5 caracteres por token (conservador para es-MX)."""
        if not text:
            return 0
        return int(len(text) / 3.5)

    def _calcular_chars_efectivos_archivos(self, archivos_block, archivos_audit):
        """Devuelve los caracteres EFECTIVOS de archivos cliente, descontando
        headers ('=== ... ===') y placeholders de imágenes multimodales.

        Se usa para la pre-validación: si el archivo aporta menos contenido
        que un umbral, advertir al usuario antes de llamar a la IA.

        Args:
            archivos_block: str con el bloque completo concatenado.
            archivos_audit: list de dicts con metadatos por archivo
                            (resultado de _build_archivos_cliente_block).

        Returns:
            int — caracteres útiles aproximados.
        """
        if not archivos_block:
            return 0
        # Cálculo desde el audit (más preciso): suma kept_chars de archivos
        # cuyo método NO sea multimodal/error/omitted.
        if archivos_audit:
            chars = 0
            for entry in archivos_audit:
                method = entry.get('method')
                if method in ('multimodal', 'extraction_error', 'omitted', 'empty'):
                    continue
                chars += int(entry.get('kept_chars') or 0)
            if chars > 0:
                return chars
        # Fallback: contar chars del bloque sin headers
        useful = []
        for line in (archivos_block or '').split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('===') and stripped.endswith('==='):
                continue
            if stripped.startswith('[Imagen ') or stripped.startswith('(sin archivos'):
                continue
            useful.append(stripped)
        return sum(len(line) for line in useful)

    def _get_narrative_mode(self):
        """Lee el setting `valoracion.narrative_mode` con normalización
        defensiva (default 'legacy'). Fase 4.2."""
        try:
            v = (self.env['ir.config_parameter'].sudo()
                 .get_param('valoracion.narrative_mode', 'legacy')
                 or 'legacy')
        except Exception:
            return 'legacy'
        return v if v in ('legacy', 'wellness') else 'legacy'

    def _render_analisis_archivo_html(self, analisis):
        """Convierte el objeto analisis_archivo_cliente devuelto por la IA a
        un bloque HTML estructurado para incrustar al inicio de
        resultados_valoracion.

        Args:
            analisis: dict con keys: archivo_fue_analizado, calidad_del_archivo,
                hallazgos_principales, hallazgos_secundarios,
                senales_funcionales, restricciones_detectadas.

        Returns:
            str HTML. Vacío si analisis no es dict.
        """
        if not isinstance(analisis, dict):
            return ''

        # Fase 4.2: toggle wellness — delega al renderer puro si el
        # setting está en 'wellness'. Legacy queda intacto.
        if self._get_narrative_mode() == 'wellness':
            try:
                from .narrative_renderer import render_analisis_narrative
                return render_analisis_narrative(analisis)
            except Exception:  # pragma: no cover — defensivo
                _logger.exception(
                    "narrative_renderer.render_analisis_narrative falló — "
                    "cayendo a legacy")

        from markupsafe import escape

        parts = ['<h4 style="color:#714B67;margin-bottom:6px;">Análisis del archivo del cliente</h4>']

        analizado = analisis.get('archivo_fue_analizado', True)
        if analizado is False:
            parts.append(
                '<p style="color:#E57373;"><b>Archivo NO analizado:</b> '
                'el contenido del archivo del cliente no aportó información '
                'suficiente para emitir una valoración. Carga un archivo '
                'válido (estudio, reporte, anamnesis con datos legibles) '
                'y vuelve a generar.</p>'
            )

        # Fase 4.2: "Calidad del archivo" es metadato técnico/auditoría
        # interno; se omite del PDF cliente para no romper la sensación
        # wellness. Sigue disponible en el JSON crudo si se necesita
        # auditar (log IA / vista admin).

        for clave, etiqueta in (
            ('hallazgos_principales', 'Hallazgos principales'),
            ('hallazgos_secundarios', 'Hallazgos secundarios'),
            ('senales_funcionales', 'Señales funcionales'),
            ('restricciones_detectadas', 'Restricciones detectadas'),
            ('prioridades_detectadas', 'Prioridades funcionales detectadas'),
        ):
            items = analisis.get(clave) or []
            if not items:
                continue
            parts.append('<p style="margin-bottom:2px;"><b>%s:</b></p>' % etiqueta)
            parts.append('<ul style="margin-top:0;">')
            for it in items:
                if it:
                    parts.append('<li>%s</li>' % escape(str(it)))
            parts.append('</ul>')

        parts.append('<hr style="border:none;border-top:1px dashed #ccc;margin:8px 0;"/>')
        return ''.join(parts)

    def _render_prioridades_caso_html(self, prioridades):
        """Convierte prioridades_caso devuelto por la IA en HTML legible.

        Acepta dos formas (retrocompatibilidad):
          * list[dict] (formato nuevo v3.0): renderiza como <ol> con cada
            prioridad numerada y sus subcampos.
          * str (formato anterior): se devuelve tal cual.

        Args:
            prioridades: list de dicts o str.

        Returns:
            str HTML.
        """
        # Fase 4.2: toggle wellness — delega al renderer puro si el
        # setting está en 'wellness'. Legacy queda intacto.
        if self._get_narrative_mode() == 'wellness':
            try:
                from .narrative_renderer import render_prioridades_narrative
                return render_prioridades_narrative(prioridades)
            except Exception:  # pragma: no cover — defensivo
                _logger.exception(
                    "narrative_renderer.render_prioridades_narrative falló — "
                    "cayendo a legacy")

        if isinstance(prioridades, str):
            # Backward compat: la IA devolvió string (versión antigua del prompt)
            return prioridades
        if not isinstance(prioridades, list) or not prioridades:
            return ''

        from markupsafe import escape

        parts = ['<ol style="padding-left:18px;">']
        for entry in prioridades:
            if not isinstance(entry, dict):
                # Si por alguna razón llegó string en lugar de dict
                parts.append('<li>%s</li>' % escape(str(entry)))
                continue
            titulo = entry.get('titulo') or '(sin título)'
            parts.append('<li style="margin-bottom:8px;">')
            parts.append('<b>%s</b>' % escape(titulo))
            evidencia = entry.get('evidencia_archivo')
            if evidencia:
                parts.append(
                    '<br/><span style="color:#555;"><i>Evidencia (archivo):</i> %s</span>'
                    % escape(evidencia)
                )
            relacion = entry.get('relacion_antecedente')
            if relacion:
                parts.append(
                    '<br/><span style="color:#555;"><i>Relación con antecedentes:</i> %s</span>'
                    % escape(relacion)
                )
            importancia = entry.get('importancia')
            if importancia:
                parts.append(
                    '<br/><span style="color:#555;"><i>Importancia:</i> %s</span>'
                    % escape(importancia)
                )
            parts.append('</li>')
        parts.append('</ol>')
        return ''.join(parts)

    def _process_productos(self, productos_lista):
        """Crea valoracion.linea.producto con matching robusto contra el catálogo.

        Estrategia de matching (en orden, primer hit gana):
          1. Match exacto NORMALIZADO por nombre o default_code:
                lowercase + sin acentos + guiones->espacios + colapsar espacios.
             Ej: la IA devuelve "V-ITADOL" y el catálogo tiene "V-itadol"
             o "V Itadol" → coinciden tras normalizar.
          2. Match aproximado por substring normalizado en cualquier dirección
             (target dentro del nombre, o nombre dentro del target).
          3. Match aproximado por palabras: todas las palabras del target
             aparecen en el nombre del producto (no necesariamente en orden).
          4. Sin match -> match_status='no_encontrado', sin product_id.

        Para evitar N consultas SQL por cada producto recomendado, se
        pre-cargan TODOS los productos VitalHealth una sola vez al inicio
        y se itera en memoria.

        Retorna (dict_lineas_creadas, lista_nombres_no_encontrados).
        """
        self.ensure_one()
        Product = self.env['product.product']
        Linea = self.env['valoracion.linea.producto']

        # Pre-cargar productos VitalHealth y normalizar para matching rápido.
        # Usa is_vitalhealth_effective para incluir TANTO productos con el flag
        # manual is_vitalhealth=True COMO productos en la categoría VitalHealth.
        vh_products = Product.search([
            ('product_tmpl_id.is_vitalhealth_effective', '=', True),
        ])
        catalog_index = []
        for prod in vh_products:
            name_norm = _normalize_product_name(prod.name)
            code_norm = _normalize_product_name(prod.default_code or '')
            tmpl_name_norm = _normalize_product_name(prod.product_tmpl_id.name)
            catalog_index.append({
                'product': prod,
                'name_norm': name_norm,
                'code_norm': code_norm,
                'tmpl_name_norm': tmpl_name_norm,
                'name_words': set(name_norm.split()) if name_norm else set(),
            })

        lineas_creadas = {}
        no_encontrados = []
        sequence = 10

        # Si la IA devolvió orden_importancia, usamos ESE orden en lugar
        # de la posición en la lista. Si no, ordenamos por posición.
        productos_ordenados = list(productos_lista or [])
        if any(isinstance(p, dict) and p.get('orden_importancia') for p in productos_ordenados):
            productos_ordenados.sort(
                key=lambda p: (
                    int(p.get('orden_importancia') or 9999)
                    if isinstance(p, dict) else 9999
                )
            )

        for prod_data in productos_ordenados:
            nombre_ia = (prod_data.get('nombre') or '').strip()
            if not nombre_ia:
                continue

            # CAMBIO DE FLUJO: la IA ahora devuelve `dosis_sugerida` (texto
            # informativo) en lugar de cantidad. La cantidad de compra mensual
            # se calcula desde valoracion.producto.regla.
            #
            # v3.0: la IA también devuelve orden_importancia y prioridad_que_apoya.
            # orden_importancia → mapea a sequence (preserva ranking de IA).
            # prioridad_que_apoya → se prepende a razón con prefijo legible.
            #
            # Mantenemos compat con respuestas IA antiguas que omitan estos
            # campos: usamos sequence incremental y razón sin prefijo.
            dosis_sugerida_ia = (prod_data.get('dosis_sugerida') or '').strip()
            cantidad_ia_raw = prod_data.get('cantidad')  # solo log/auditoría
            razon = (prod_data.get('razon') or '').strip()
            orden_importancia_ia = prod_data.get('orden_importancia')
            prioridad_que_apoya = (prod_data.get('prioridad_que_apoya') or '').strip()
            # v4.0: cita textual del archivo del cliente que respalda esta
            # recomendación. Opcional pero recomendado. Se concatena al final
            # de la razón con prefijo "Cita archivo:" para auditoría humana.
            cita_archivo = (prod_data.get('cita_archivo_cliente') or '').strip()

            # Construir razón enriquecida con prefijo de prioridad
            if prioridad_que_apoya:
                if razon:
                    razon = "[%s] %s" % (prioridad_que_apoya, razon)
                else:
                    razon = "Apoya %s" % prioridad_que_apoya
            # Anexar cita del archivo si la IA la proporcionó
            if cita_archivo:
                cita_short = cita_archivo if len(cita_archivo) <= 300 else cita_archivo[:300].rstrip() + '...'
                if razon:
                    razon = '%s\nCita archivo: "%s"' % (razon, cita_short)
                else:
                    razon = 'Cita archivo: "%s"' % cita_short

            target_norm = _normalize_product_name(nombre_ia)
            target_words = set(target_norm.split()) if target_norm else set()
            product = None
            match_status = None

            # 1. Match exacto normalizado
            for entry in catalog_index:
                if not target_norm:
                    break
                if (entry['name_norm'] == target_norm
                        or entry['tmpl_name_norm'] == target_norm
                        or (entry['code_norm']
                            and entry['code_norm'] == target_norm)):
                    product = entry['product']
                    match_status = 'exacto'
                    break

            # 2. Match aproximado por substring (en cualquier dirección)
            if not product and target_norm:
                for entry in catalog_index:
                    nm = entry['name_norm']
                    if not nm:
                        continue
                    if target_norm in nm or nm in target_norm:
                        product = entry['product']
                        match_status = 'aproximado'
                        break

            # 3. Match aproximado por intersección de palabras
            if not product and target_words:
                # buscar producto cuyas palabras contengan todas las del target
                # (o al menos la mayoría: aceptamos >= 50% si target tiene 1-2 palabras)
                best_entry = None
                best_overlap = 0
                for entry in catalog_index:
                    if not entry['name_words']:
                        continue
                    overlap = len(target_words & entry['name_words'])
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_entry = entry
                # Aceptar si al menos 50% de las palabras del target coinciden
                # (con un mínimo de 1 palabra que tenga > 2 caracteres)
                if best_entry and best_overlap >= max(1, len(target_words) // 2):
                    significant = [w for w in target_words & best_entry['name_words'] if len(w) > 2]
                    if significant:
                        product = best_entry['product']
                        match_status = 'aproximado'

            # ============================================================
            # Cálculo de CANTIDAD desde valoracion.producto.regla
            # ============================================================
            # Estrategia:
            #   1. Si producto matcheó y existe regla → usar regla
            #      (cantidad_mensual y dosis_sugerida fallback).
            #   2. Si producto matcheó y NO existe regla → usar fallback
            #      legacy en _get_cantidad_mensual_producto.
            #   3. Si no hubo match → cantidad = 1 (default razonable).
            Regla = self.env['valoracion.producto.regla']
            regla_aplicada = Regla.browse()
            cantidad_mensual = 1.0
            cantidad_calc_por_regla = False
            dosis_final = dosis_sugerida_ia

            if product:
                regla_aplicada = Regla._get_regla_for_product(product.product_tmpl_id)
                if regla_aplicada:
                    cantidad_mensual = regla_aplicada.cantidad_mensual or 1.0
                    cantidad_calc_por_regla = True
                    # Si la IA NO mandó dosis, usar la de la regla como fallback
                    if not dosis_final and regla_aplicada.dosis_sugerida:
                        dosis_final = regla_aplicada.dosis_sugerida
                else:
                    # No hay regla configurada en el modelo: usar fallback
                    # legacy hardcoded (compatibilidad mientras se puebla
                    # valoracion.producto.regla en producción).
                    cantidad_mensual = Linea._get_cantidad_mensual_producto(product)

            try:
                cantidad_ia_num = float(cantidad_ia_raw) if cantidad_ia_raw is not None else None
            except (TypeError, ValueError):
                cantidad_ia_num = None

            # Sequence: si la IA mandó orden_importancia, lo usamos × 10 para
            # que el orden semántico de la IA se preserve incluso si después
            # se reordenan manualmente. Si no, fallback al contador incremental.
            try:
                seq_final = int(orden_importancia_ia) * 10 if orden_importancia_ia else sequence
            except (TypeError, ValueError):
                seq_final = sequence

            linea_vals = {
                'valoracion_id': self.id,
                'sequence': seq_final,
                'product_id': product.id if product else False,
                'product_name_ia': nombre_ia,
                'cantidad': cantidad_mensual,
                'razon': razon,
                'dosis_sugerida': dosis_final or False,
                'regla_producto_id': regla_aplicada.id if regla_aplicada else False,
                'cantidad_calculada_por_regla': cantidad_calc_por_regla,
                'match_status': match_status or 'no_encontrado',
            }
            linea = Linea.create(linea_vals)

            if product:
                lineas_creadas[nombre_ia] = linea
                _logger.info(
                    "Valoración %s: producto IA '%s' → catálogo '%s' "
                    "(match=%s, cantidad_ia=%s, cantidad_mensual=%s, "
                    "regla=%s, dosis='%s')",
                    self.name, nombre_ia, product.display_name,
                    match_status, cantidad_ia_num, cantidad_mensual,
                    regla_aplicada.id if regla_aplicada else 'sin_regla',
                    (dosis_final or '')[:80],
                )
            else:
                no_encontrados.append(nombre_ia)
                _logger.warning(
                    "Valoración %s: producto IA '%s' NO ENCONTRADO en catálogo "
                    "VitalHealth. Línea creada con product_name_ia y "
                    "precio_unitario=0. Asígnalo manualmente o añade el "
                    "producto al catálogo con is_vitalhealth=True.",
                    self.name, nombre_ia,
                )
            sequence += 10

        return lineas_creadas, no_encontrados

    def action_regenerar(self):
        """Re-genera una valoración ya generada (decisión D).

        Conserva los logs históricos en ia_log_ids (no se eliminan) y
        actualiza los campos HTML con la nueva respuesta. Internamente:
          generado --> borrador --> generado
        """
        self.ensure_one()
        if self.state != 'generado':
            raise UserError(_(
                "Solo se puede re-generar una valoración en estado 'Generado'. "
                "Estado actual: %s"
            ) % dict(self._fields['state'].selection).get(self.state, self.state))

        self.write({'state': 'borrador'})
        self.message_post(body=_("Re-generación solicitada. Se conservan logs históricos."))
        return self.action_generar_valoracion()

    def action_cancelar(self):
        """Cancela la valoración. Permitido desde borrador o generado."""
        for rec in self:
            if rec.state == 'cancelado':
                continue
            rec.write({'state': 'cancelado'})
            rec.message_post(body=_("Valoración cancelada."))
        return True

    def action_volver_borrador(self):
        """Revierte una valoración cancelada a borrador.

        El folio NO se reasigna (decisión F: queda quemado y permanece
        ligado al registro original).
        """
        for rec in self:
            if rec.state != 'cancelado':
                raise UserError(_(
                    "Solo se puede volver a borrador desde el estado 'Cancelado'."
                ))
            rec.write({'state': 'borrador'})
            rec.message_post(body=_("Valoración revertida a borrador. Folio: %s") % rec.name)
        return True

    # ====================================================================
    # Acciones: PDF / Cotización
    # ====================================================================
    def action_descargar_pdf(self):
        """Genera y descarga el PDF de la valoración.

        Decisión 9: el PDF se genera bajo demanda, NO automáticamente al
        terminar la IA. Esto permite al usuario revisar/editar el contenido
        generado por IA antes de imprimir.

        Registra pdf_generated_at para auditoría. El reporte se define en
        reports/valoracion_report.xml (XMLID action_report_valoracion).
        """
        self.ensure_one()
        if self.state != 'generado':
            raise UserError(_(
                "La valoración debe estar en estado 'Generado' para descargar el PDF."
            ))

        report = self.env.ref(
            'valoracion.action_report_valoracion',
            raise_if_not_found=False,
        )
        if not report:
            raise UserError(_(
                "El reporte PDF no está disponible. Verifica que el módulo "
                "esté correctamente instalado/actualizado."
            ))

        # Registrar timestamp de generación para auditoría
        self.sudo().write({'pdf_generated_at': fields.Datetime.now()})
        self.message_post(body=_("Reporte PDF generado por %s.") % self.env.user.display_name)

        return report.report_action(self)

    def action_crear_cotizacion(self):
        """Crea una sale.order a partir de los productos recomendados.

        Decisión E: usa la lista de precios del partner; si no tiene, default
        de la compañía (Odoo lo resuelve automáticamente al no especificar
        pricelist_id en el create).
        """
        self.ensure_one()
        if self.state != 'generado':
            raise UserError(_(
                "Solo se puede crear cotización cuando la valoración está "
                "en estado 'Generado'."
            ))

        # Considerar TODA línea con product_id asignado (exacto, aproximado o
        # manual). Las líneas con match_status='no_encontrado' tienen
        # product_id=False y se excluyen automáticamente: NO se crean
        # líneas de venta para productos no encontrados en el catálogo.
        productos_validos = self.linea_producto_ids.filtered(lambda l: l.product_id)
        if not productos_validos:
            raise UserError(_(
                "No hay productos VitalHealth válidos para crear la cotización.\n\n"
                "Posibles causas:\n"
                "  • La IA recomendó productos que no existen en tu catálogo "
                "(marcados como 'no_encontrado').\n"
                "  • Necesitas asignar manualmente los productos en la "
                "pestaña 'Plan Estratégico'.\n"
                "  • Verifica que tus productos VitalHealth tengan "
                "is_vitalhealth = True."
            ))

        # Construir descripción enriquecida por línea: producto + dosis + razón.
        # La dosis es texto INFORMATIVO debajo del nombre; product_uom_qty
        # usa la cantidad MENSUAL ya calculada por la regla, no la dosis.
        def _build_line_description(linea):
            parts = [linea.product_id.display_name]
            if linea.dosis_sugerida:
                parts.append(_("Dosis sugerida: %s") % linea.dosis_sugerida)
            if linea.razon:
                parts.append(_("Razón: %s") % linea.razon)
            return '\n'.join(parts)

        order_lines = [
            (0, 0, {
                'product_id': linea.product_id.id,
                'product_uom_qty': linea.cantidad,  # cantidad mensual por regla
                'name': _build_line_description(linea),
            })
            for linea in productos_validos
        ]

        order = self.env['sale.order'].create({
            'partner_id': self.partner_id.id,
            'user_id': self.user_id.id,
            'valoracion_id': self.id,
            'company_id': self.company_id.id,
            'note': _("Cotización estimada para programa de 30 días."),
            'order_line': order_lines,
        })

        self.message_post(body=_(
            "Cotización %(order)s creada con %(count)d producto(s) recomendado(s)."
        ) % {'order': order.name, 'count': len(productos_validos)})

        return {
            'type': 'ir.actions.act_window',
            'name': _('Cotización'),
            'res_model': 'sale.order',
            'view_mode': 'form',
            'res_id': order.id,
            'target': 'current',
        }

    # ====================================================================
    # Smart buttons
    # ====================================================================
    def action_view_ventas(self):
        """Smart button: ventas ligadas o del mismo contacto."""
        self.ensure_one()
        domain = ['|',
                  ('valoracion_id', '=', self.id),
                  ('partner_id', '=', self.partner_id.id)]
        return {
            'type': 'ir.actions.act_window',
            'name': _('Ventas'),
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': domain,
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_valoracion_id': self.id,
            },
        }

    def action_view_citas(self):
        """Smart button: citas del contacto en calendar.event."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Citas'),
            'res_model': 'calendar.event',
            'view_mode': 'calendar,list,form',
            'domain': [('partner_ids', 'in', self.partner_id.id)],
            'context': {
                'default_partner_ids': [(4, self.partner_id.id)],
                'default_user_id': self.user_id.id,
                'default_name': self.partner_id.name or '',
                'default_duration': 0.5,
                'default_valoracion_id': self.id,
            },
        }

    def action_abrir_wizard_crear_cita(self):
        """Abre el wizard de creación de cita con defaults de la valoración.

        El wizard se define en Etapa 4. Este método queda listo para apuntar
        a él desde la pestaña Citas del formulario.
        """
        self.ensure_one()
        wizard = self.env.ref(
            'valoracion.action_valoracion_crear_cita_wizard',
            raise_if_not_found=False,
        )
        if not wizard:
            raise UserError(_(
                "El wizard de creación de citas aún no está disponible "
                "(se habilita en la Etapa 4)."
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Crear Cita'),
            'res_model': 'valoracion.crear.cita.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_valoracion_id': self.id,
                'default_partner_id': self.partner_id.id,
                'default_user_id': self.env.user.id,
                'default_name': self.partner_id.name or '',
                'default_duration': 0.5,
            },
        }

    def action_view_ia_logs(self):
        """Smart button: bitácora IA de esta valoración."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Bitácora IA'),
            'res_model': 'valoracion.log.ia',
            'view_mode': 'list,form',
            'domain': [('valoracion_id', '=', self.id)],
            'context': {'default_valoracion_id': self.id},
        }

    # ====================================================================
    # Helpers internos
    # ====================================================================
    def _get_active_fuentes(self):
        """Devuelve las fuentes activas aplicables a esta valoración
        (mismas compañía o globales). Se usa al construir el prompt en Etapa 3.
        """
        self.ensure_one()
        return self.env['valoracion.fuente'].search([
            ('active', '=', True),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
        ], order='sequence, id')
