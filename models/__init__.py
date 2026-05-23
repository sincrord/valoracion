# -*- coding: utf-8 -*-
# Orden de importación:
# 1. Modelos heredados de Odoo que añaden campos referenciables.
# 2. Modelos auxiliares referenciados por el principal.
# 3. Modelo principal valoracion.valoracion.
# 4. Herencias que dependen del modelo principal.
# 5. Configuración global y subpaquete IA.

from . import res_partner
from . import product_template
from . import valoracion_prompt_template
from . import valoracion_fuente
from . import valoracion_log_ia
from . import valoracion_producto_regla
from . import biocuantico            # Fase 2.1: catálogos BioCuántico + parser (detección)
from . import valoracion_archivo_cliente
from . import valoracion_linea_producto
from . import valoracion_valoracion
from . import calendar_event
from . import sale_order
from . import res_config_settings
from . import ia
