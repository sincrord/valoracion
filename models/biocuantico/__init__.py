# -*- coding: utf-8 -*-
# Submódulo BioCuántico (Fase 2.1)
#
# Esta fase aporta únicamente la infraestructura base:
#   * Modelos de configuración (sistemas corporales y mapeo de estados).
#   * Detección heurística vía BioCuanticoParser.is_biocuantico().
#   * Esqueleto de BioCuanticoMasterSummary para Fase 2.2.
#
# El parser real (extracción tabular) y la generación del resumen maestro
# se activarán en Fase 2.2. Esta fase NO modifica el flujo IA actual.

from . import biocuantico_sistema
from . import biocuantico_estado_mapeo
from . import parser
from . import master_summary
