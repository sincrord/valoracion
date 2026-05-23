# -*- coding: utf-8 -*-
from . import models
from . import wizards
# Nota: NO importar tests/ desde aquí. Odoo carga los tests automáticamente
# cuando se ejecuta con --test-enable, y mantenerlos fuera del __init__.py
# evita arrastrar dependencias de testing al boot de producción.
