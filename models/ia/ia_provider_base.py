# -*- coding: utf-8 -*-
"""Interfaz base para proveedores de IA.

En v1 solo se implementa Claude (decisión 2). La estructura permite añadir
OpenAI u otros proveedores en el futuro sin tocar el orquestador en
valoracion_valoracion._ejecutar_generacion_ia.

Convenciones:
  * El método generate() devuelve siempre un dict con la misma estructura,
    sea éxito o error. Esto simplifica el manejo en el orquestador.
  * Las excepciones específicas (timeout, auth, rate limit) se capturan
    DENTRO del provider y se traducen al formato dict de retorno; NO se
    propagan hacia arriba. Solo errores de configuración inicial (api_key
    vacía, modelo vacío) lanzan IAProviderError en el constructor.
"""
from abc import ABC, abstractmethod


# ============================================================
# Excepciones
# ============================================================

class IAProviderError(Exception):
    """Excepción base de proveedores IA. Se lanza solo en configuración inicial."""
    pass


class IAProviderConfigError(IAProviderError):
    """Configuración inválida (api_key vacía, modelo vacío, etc.)."""
    pass


# Estados normalizados que devuelve generate() en el campo 'estado'.
# Coinciden con los valores de la selección en valoracion.log.ia.
ESTADO_EXITO = 'exito'
ESTADO_ERROR_API = 'error_api'
ESTADO_TIMEOUT = 'timeout'
ESTADO_ERROR_PARSING = 'error_parsing'
ESTADO_ERROR_VALIDACION = 'error_validacion'
ESTADO_ERROR_CONFIG = 'error_config'


# ============================================================
# Helper de construcción de respuesta uniforme
# ============================================================

def make_result(success, data=None, tokens_input=0, tokens_output=0,
                duration_ms=0, error=None, estado=ESTADO_EXITO,
                raw_response=''):
    """Helper estándar para que todos los providers devuelvan la misma forma."""
    return {
        'success': bool(success),
        'data': data or {},
        'tokens_input': int(tokens_input or 0),
        'tokens_output': int(tokens_output or 0),
        'duration_ms': int(duration_ms or 0),
        'error': error,
        'estado': estado,
        'raw_response': raw_response or '',
    }


# ============================================================
# Clase base
# ============================================================

class IAProviderBase(ABC):
    """Clase base abstracta para proveedores IA.

    Subclases concretas deben:
      * Definir el atributo de clase 'name' (ej: 'claude', 'openai').
      * Implementar generate(system_prompt, user_prompt, output_schema, images).
    """

    name = 'base'

    def __init__(self, api_key, endpoint_url, model,
                 temperature=0.3, max_tokens=4096, timeout=120):
        """Valida configuración mínima y guarda parámetros.

        Lanza IAProviderConfigError si falta api_key o modelo. Esto se debe
        capturar en el orquestador para devolver UserError claro al usuario.
        """
        if not api_key:
            raise IAProviderConfigError("API Key del proveedor IA no configurada.")
        if not model:
            raise IAProviderConfigError("Modelo del proveedor IA no configurado.")
        self.api_key = api_key
        self.endpoint_url = endpoint_url
        self.model = model
        self.temperature = float(temperature) if temperature is not None else 0.3
        self.max_tokens = int(max_tokens) if max_tokens else 4096
        self.timeout = int(timeout) if timeout else 120

    @abstractmethod
    def generate(self, system_prompt, user_prompt, output_schema, images=None):
        """Llama al proveedor y devuelve el resultado normalizado.

        Args:
            system_prompt:  str con el prompt de sistema (rol/reglas).
            user_prompt:    str con el prompt de usuario (datos sustituidos).
            output_schema:  dict con el JSON Schema de la salida esperada.
            images:         lista opcional de dicts con
                            {'image_b64': str, 'image_media_type': str, 'name': str}.

        Returns:
            dict construido con make_result(...). Claves:
              * success (bool)
              * data (dict)            -> JSON parseado de la herramienta
              * tokens_input (int)
              * tokens_output (int)
              * duration_ms (int)
              * error (str|None)
              * estado (str)           -> uno de los ESTADO_* arriba
              * raw_response (str)     -> texto crudo para auditoría (capeado)
        """
        raise NotImplementedError
