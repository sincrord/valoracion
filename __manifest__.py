# -*- coding: utf-8 -*-
{
    'name': 'Valoración',
    'version': '18.0.3.3.0',
    'category': 'Salud/Salud y Bienestar',
    'summary': 'Valoración funcional NeuroVital con apoyo de IA',
    'description': """
Módulo Valoración NeroViatl
=============================

Automatiza el proceso de valoración funcional del Plan de Salud VitalHealth:

* Registro de información clínica funcional básica del contacto.
* Carga de archivos del cliente (PDF, XLS, XLSX, CSV, TXT, imágenes).
* Análisis de archivos y antecedentes con apoyo de IA (Claude).
* Consulta de fuentes internas autorizadas (catálogo VitalHealth, fichas técnicas).
* Generación de valoración funcional y propuesta de productos VitalHealth.
* Reportes PDF descargables.
* Integración con contactos, calendario y ventas.

Esta valoración es de carácter funcional y complementario; no sustituye
diagnóstico ni tratamiento médico profesional.
""",
    'author': 'SINCRO Recursos Digitales',
    'website': 'https://sincro.com.mx',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'contacts',
        'calendar',
        'product',
        'sale_management',
    ],
    'external_dependencies': {
        'python': [
            'requests',     # Llamadas HTTP a la API de Claude
            'pdfplumber',   # Extracción de texto de PDF (PyPDF2 es fallback opcional)
            'openpyxl',     # Extracción de texto de XLSX
            'chardet',      # Detección de encoding en CSV/TXT
        ],
    },
    'data': [
        # ===== Seguridad =====
        'security/valoracion_security.xml',
        'security/ir.model.access.csv',
        # ===== Datos base =====
        'data/ir_sequence_data.xml',
        'data/product_category_data.xml',
        'data/valoracion_prompt_default.xml',
        'data/biocuantico_data.xml',
        # ===== Wizards =====
        'wizards/valoracion_crear_cita_wizard_views.xml',
        # ===== Vistas =====
        # Vistas embebidas y de modelos hijos (cargan primero)
        'views/valoracion_archivo_cliente_views.xml',
        'views/valoracion_linea_producto_views.xml',
        # Vistas y acciones de los modelos principales
        'views/valoracion_valoracion_views.xml',
        'views/valoracion_fuente_views.xml',
        'views/valoracion_prompt_template_views.xml',
        'views/valoracion_log_ia_views.xml',
        'views/valoracion_producto_regla_views.xml',
        'views/biocuantico_views.xml',
        # Herencias de modelos nativos
        'views/res_partner_views.xml',
        'views/calendar_event_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/product_template_views.xml',
        # Menús (deben cargar al final de las vistas: referencian acciones)
        'views/valoracion_menus.xml',
        # ===== Reportes =====
        'reports/valoracion_report.xml',
        'reports/valoracion_report_template.xml',
    ],
    'demo': [],
    'images': ['static/description/icon.png'],
    'assets': {
        'web.assets_backend': [
            'valoracion/static/src/css/valoracion_overlay.css',
            'valoracion/static/src/js/valoracion_overlay.js',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
