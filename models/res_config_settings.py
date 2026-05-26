# -*- coding: utf-8 -*-
"""
Configuración global del módulo Valoración VitalHealth.

==============================================================================
NOTA SOBRE config_parameter EN ODOO 18
==============================================================================
En Odoo 18, el atributo `config_parameter` de los campos de
res.config.settings solo soporta los tipos:

    boolean, integer, float, char, selection, many2one, datetime

Los tipos `Text` y `Html` NO están permitidos como config_parameter y
producen el error en el arranque:

    Exception: Field res.config.settings.X must have type 'boolean',
    'integer', 'float', 'char', 'selection', 'many2one' or 'datetime'

Para campos Text/Html que necesitan persistir en ir.config_parameter,
hay que:
  1. Declarar el campo SIN el atributo config_parameter.
  2. Sobrescribir get_values() para leer el valor de ir.config_parameter.
  3. Sobrescribir set_values() para escribir el valor a ir.config_parameter.

Este archivo aplica ese patrón al campo `valoracion_consentimiento_texto`,
que es Text (multilinea). El resto de campos siguen usando config_parameter
porque son tipos primitivos soportados.
==============================================================================
"""
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # =========================================================
    # Activación
    # =========================================================
    valoracion_ia_activa = fields.Boolean(
        string='IA activa',
        config_parameter='valoracion.ia_activa',
        default=False,
        help='Habilita la generación de valoraciones con IA. Si está '
             'desactivada, el botón "Generar Valoración" lanzará error.',
    )
    valoracion_ia_proveedor = fields.Selection(
        selection=[
            ('claude', 'Claude (Anthropic)'),
            ('openai', 'OpenAI'),
        ],
        string='Proveedor IA',
        config_parameter='valoracion.ia_proveedor',
        default='claude',
        help='Proveedor IA activo. Cada proveedor tiene su propio API Key, '
             'modelo y endpoint configurables abajo. La factory en '
             'models/ia/__init__.py.build_provider() instancia la clase '
             'correspondiente al pulsar "Generar Valoración".',
    )

    # =========================================================
    # Configuración de Claude (Anthropic)
    # =========================================================
    valoracion_ia_modelo = fields.Char(
        string='Modelo Claude',
        config_parameter='valoracion.ia_modelo',
        default='claude-sonnet-4-5',
        help='Identificador del modelo Claude. Ej: claude-sonnet-4-5, '
             'claude-opus-4-6, claude-haiku-4-5.',
    )
    valoracion_ia_api_key = fields.Char(
        string='API Key Claude',
        config_parameter='valoracion.ia_api_key',
        groups='valoracion.group_valoracion_admin',
        help='Clave de acceso a Claude (Anthropic). Solo el Administrador '
             'puede leerla o escribirla. Nunca se incluye en logs ni en el '
             'reporte PDF.',
    )
    valoracion_ia_endpoint_url = fields.Char(
        string='Endpoint Claude',
        config_parameter='valoracion.ia_endpoint_url',
        default='https://api.anthropic.com/v1/messages',
        groups='valoracion.group_valoracion_admin',
        help='URL del endpoint de Anthropic Messages API.',
    )

    # =========================================================
    # Configuración de OpenAI
    # =========================================================
    # NOTA: las claves de ir.config_parameter NO llevan el prefijo 'ia_' para
    # evitar confusión con las de Claude. El nombre Python del campo sí
    # mantiene 'ia_openai_' por compatibilidad con el XML de la vista.
    #
    # Claves usadas en ir.config_parameter (nombres EXACTOS):
    #   * valoracion.openai_api_key
    #   * valoracion.openai_modelo
    #   * valoracion.openai_endpoint_url
    #   * valoracion.openai_costo_per_1m_input
    #   * valoracion.openai_costo_per_1m_output
    # =========================================================
    valoracion_ia_openai_modelo = fields.Char(
        string='Modelo OpenAI',
        config_parameter='valoracion.openai_modelo',
        default='gpt-4o',
        help='Identificador del modelo OpenAI. Ej: gpt-4o, gpt-4.1, '
             'gpt-4o-mini. La Responses API en formato simple no soporta '
             'multimodal aquí; las imágenes del cliente se ignoran cuando '
             'OpenAI es el proveedor activo.',
    )
    valoracion_ia_openai_api_key = fields.Char(
        string='API Key OpenAI',
        config_parameter='valoracion.openai_api_key',
        groups='valoracion.group_valoracion_admin',
        help='Clave de acceso a OpenAI (formato sk-... o sk-proj-...). '
             'Solo el Administrador puede leerla o escribirla. Se hace '
             '.strip() antes de enviar a la API. Nunca se incluye completa '
             'en logs ni en el reporte PDF.',
    )
    valoracion_ia_openai_endpoint_url = fields.Char(
        string='Endpoint OpenAI',
        config_parameter='valoracion.openai_endpoint_url',
        default='https://api.openai.com/v1/responses',
        groups='valoracion.group_valoracion_admin',
        help='URL del endpoint de OpenAI Responses API. '
             'Por defecto: https://api.openai.com/v1/responses',
    )

    # =========================================================
    # Parámetros del modelo
    # =========================================================
    valoracion_ia_temperatura = fields.Float(
        string='Temperatura',
        config_parameter='valoracion.ia_temperatura',
        default=0.3,
        digits=(3, 2),
        help='Temperatura del modelo (0.0 = determinista; 1.0 = creativo). '
             'Para valoraciones funcionales, valores bajos (0.2-0.4) son '
             'preferibles para mantener consistencia.',
    )
    valoracion_ia_max_tokens = fields.Integer(
        string='Máximo tokens (output)',
        config_parameter='valoracion.ia_max_tokens',
        default=4096,
        help='Máximo de tokens que la IA puede generar en la respuesta.',
    )
    valoracion_ia_timeout_seconds = fields.Integer(
        string='Timeout (segundos)',
        config_parameter='valoracion.ia_timeout_seconds',
        default=120,
        help='Tiempo máximo de espera por la respuesta IA. Si se excede, '
             'se cancela la llamada y se registra como error en el log.',
    )

    # =========================================================
    # Límites de tamaño / tokens
    # =========================================================
    valoracion_ia_max_archivo_mb = fields.Integer(
        string='Máximo por archivo (MB)',
        config_parameter='valoracion.ia_max_archivo_mb',
        default=10,
        help='Tamaño máximo permitido por archivo de cliente (decisión 14).',
    )
    valoracion_ia_max_total_mb = fields.Integer(
        string='Máximo total por valoración (MB)',
        config_parameter='valoracion.ia_max_total_mb',
        default=50,
        help='Suma máxima de archivos en una valoración (decisión 14).',
    )
    valoracion_ia_max_prompt_tokens = fields.Integer(
        string='Máximo tokens del prompt',
        config_parameter='valoracion.ia_max_prompt_tokens',
        default=150000,
        help='Si el prompt construido excede este límite, las fuentes y '
             'archivos se truncan de forma controlada (decisión 14, riesgo #11).',
    )

    # =========================================================
    # Costos referenciales (USD)
    # =========================================================
    # ---- Claude ----
    valoracion_ia_costo_per_1m_input = fields.Float(
        string='Claude: costo por 1M tokens input (USD)',
        config_parameter='valoracion.ia_costo_per_1m_input',
        default=3.0,
        digits=(8, 4),
        help='Costo por millón de tokens de entrada para Claude. '
             'Solo referencial: consulta el pricing oficial de Anthropic.',
    )
    valoracion_ia_costo_per_1m_output = fields.Float(
        string='Claude: costo por 1M tokens output (USD)',
        config_parameter='valoracion.ia_costo_per_1m_output',
        default=15.0,
        digits=(8, 4),
        help='Costo por millón de tokens de salida para Claude.',
    )

    # ---- OpenAI ----
    valoracion_ia_openai_costo_per_1m_input = fields.Float(
        string='OpenAI: costo por 1M tokens input (USD)',
        config_parameter='valoracion.openai_costo_per_1m_input',
        default=2.5,
        digits=(8, 4),
        help='Costo por millón de tokens de entrada para OpenAI. '
             'Solo referencial: consulta el pricing oficial de OpenAI.',
    )
    valoracion_ia_openai_costo_per_1m_output = fields.Float(
        string='OpenAI: costo por 1M tokens output (USD)',
        config_parameter='valoracion.openai_costo_per_1m_output',
        default=10.0,
        digits=(8, 4),
        help='Costo por millón de tokens de salida para OpenAI.',
    )

    # =========================================================
    # Motor BioCuántico (Fase 3.8 — toggle controlado)
    # =========================================================
    # Toggle entre el parser legacy y el nuevo motor v3. Default: legacy.
    # Si v3 falla, el flujo en valoracion_archivo_cliente._run_biocuantico_
    # parser hace fallback automático a legacy y registra el motivo.
    valoracion_biocuantico_motor_version = fields.Selection(
        selection=[
            ('legacy', 'Legacy (parser actual)'),
            ('v3', 'Motor v3 (taxonomy + classifier + severity + ranker)'),
        ],
        string='Motor BioCuántico',
        config_parameter='valoracion.biocuantico_motor_version',
        default='v3',  # Fase 5: productivo. Rollback con setting → 'legacy'.
        help='Selecciona qué motor BioCuántico usa el parser de archivos. '
             'legacy: parser actual (estable). v3: pipeline puro nuevo '
             '(extractor → classifier → severity → ranker → master_summary). '
             'Si v3 falla por cualquier razón, el sistema hace fallback '
             'automático a legacy y registra el error en '
             'biocuantico_parse_error sin romper la valoración.',
    )

    # =========================================================
    # Narrative renderer (Fase 4.2 — modo wellness en el PDF)
    # =========================================================
    valoracion_pdf_template = fields.Selection(
        selection=[
            ('legacy', 'Legacy (clásico)'),
            ('premium', 'Premium wellness'),
        ],
        string='Plantilla del PDF cliente',
        config_parameter='valoracion.pdf_template',
        default='premium',  # Fase 5: productivo. Rollback con setting.
        help='Selector de la plantilla que usa el botón "Descargar PDF". '
             'legacy: layout clásico (estable). premium: layout wellness '
             'premium con cards, timeline y tipografía elegante. El cambio '
             'es 100% reversible (volver a legacy desliga el premium '
             'inmediatamente). No afecta motor v3 ni productos.',
    )

    valoracion_narrative_mode = fields.Selection(
        selection=[
            ('legacy', 'Legacy (labels técnicos)'),
            ('wellness', 'Wellness premium (prosa humanizada)'),
        ],
        string='Modo de narrativa del PDF',
        config_parameter='valoracion.narrative_mode',
        default='wellness',  # Fase 5: productivo. Rollback con setting.
        help='Controla cómo se renderizan las secciones analisis_archivo '
             'y prioridades_caso en el PDF cliente. legacy: bullets con '
             'labels como "Hallazgos principales", "Evidencia (archivo)", '
             '"Importancia". wellness: prosa humanizada vía '
             'narrative_renderer.py + blacklist de términos diagnósticos '
             '+ vocabulario wellness premium. El motor v3 y los productos '
             'NO se ven afectados — sólo cambia la presentación.',
    )

    # =========================================================
    # Texto del consentimiento por defecto
    # =========================================================
    # IMPORTANTE: este campo es Text (multilinea), por lo que NO puede usar
    # el atributo config_parameter en Odoo 18 (solo soporta boolean, integer,
    # float, char, selection, many2one, datetime).
    #
    # En su lugar, persistimos manualmente en ir.config_parameter con la
    # clave 'valoracion.consentimiento_texto_default', sobrescribiendo
    # get_values() y set_values() más abajo.
    #
    # La lectura desde el resto del módulo (valoracion.valoracion._get_default_
    # consentimiento_texto) ya consulta esta misma clave de ir.config_parameter,
    # así que la integración es transparente: el mismo parámetro alimenta
    # tanto la pantalla de Configuración como el snapshot que se guarda en
    # cada valoración cuando el cliente da su consentimiento (decisión C).
    # =========================================================
    valoracion_consentimiento_texto = fields.Text(
        string='Texto de consentimiento por defecto',
        help='Texto que se snapshotea en cada valoración cuando el cliente '
             'da su consentimiento (decisión C). Si lo dejas vacío, se usa '
             'el texto base interno del módulo.',
    )

    # =========================================================
    # Persistencia manual del campo Text en ir.config_parameter
    # =========================================================
    # Clave única usada en ir.config_parameter para este texto.
    # Debe coincidir con la clave que lee
    # valoracion.valoracion._get_default_consentimiento_texto.
    _CONSENTIMIENTO_PARAM_KEY = 'valoracion.consentimiento_texto_default'

    def get_values(self):
        """Carga los valores de configuración al abrir la página de Ajustes.

        El framework ya carga automáticamente todos los campos declarados
        con config_parameter; aquí solo añadimos manualmente el campo Text
        que no puede usarlo.
        """
        res = super().get_values()
        Param = self.env['ir.config_parameter'].sudo()
        res['valoracion_consentimiento_texto'] = Param.get_param(
            self._CONSENTIMIENTO_PARAM_KEY,
            default='',
        ) or ''
        return res

    def set_values(self):
        """Guarda los valores de configuración al pulsar "Guardar".

        El framework ya guarda automáticamente todos los campos declarados
        con config_parameter; aquí solo persistimos manualmente el campo
        Text que no puede usarlo.

        Si el usuario deja el texto vacío, escribimos cadena vacía a
        ir.config_parameter; al leerlo, _get_default_consentimiento_texto
        usará el DEFAULT_CONSENTIMIENTO_TEXTO interno como fallback.
        """
        super().set_values()
        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param(
            self._CONSENTIMIENTO_PARAM_KEY,
            self.valoracion_consentimiento_texto or '',
        )
